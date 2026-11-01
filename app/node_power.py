# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Redémarrage et arrêt d'un nœud : le garde des processus BLOQUÉS DANS LE NOYAU.

"""Redémarrer ou arrêter un nœud sans le perdre.

★ UN PROCESSUS EN ÉTAT D REND `systemctl reboot` FATAL. L'arrêt propre attend la fin de chaque
processus ; un processus bloqué dans le noyau (état D) ne finit jamais, et SIGKILL ne l'atteint
pas. La machine coupe son réseau, puis reste là : ni éteinte, ni relancée, injoignable. Il faut
alors une coupure physique.

Vécu deux fois sur r620-1 (2026-08-12, puis 2026-10-19 avec le bouton « Redémarrer » de
l'interface) : des `mxl-fabrics-demo` bloqués dans `cm_destroy_id` (pile RDMA du noyau). Aucun
BMC enregistré pour ce nœud — il a fallu y aller à la main.

La seule voie qui marche avec un processus en D est `sysrq` : le noyau redémarre de lui-même,
sans rien demander à l'espace utilisateur (r620-2 de retour en 2 min 40 le 2026-08-12). Elle est
BRUTALE — les écritures en cours sont vidées sur disque (`s`), mais aucun conteneur n'est arrêté
proprement. D'où la règle :

  · aucun processus bloqué → redémarrage normal, comme avant ;
  · processus bloqués, sans `force` → REFUS, avec la liste : l'opérateur décide en connaissance ;
  · processus bloqués, avec `force` → redémarrage sysrq.

L'ARRÊT complet n'a pas d'équivalent sûr qui garde le marqueur de l'agent-nœud : il est refusé
tant qu'un processus est bloqué (redémarrer en forçant d'abord, puis arrêter).

★ NI REDÉMARRAGE NI ARRÊT PENDANT UNE INSTALLATION (2026-10-30, fiche de nœud validée par Cyril).
Couper un `dpkg` en cours laisse des paquets à moitié configurés ; couper l'installation d'un
pilote hôte ou le rattrapage de capacités (noyau MTL, DKMS) laisse un noyau ou un module dans un
état que personne n'a vérifié. Trois sources, chacune MESURÉE plutôt que déduite :
  · `apt`/`apt-get`/`aptitude`/`dpkg` vivants sur l'hôte — relevés par la MÊME sonde que l'état D
    (un seul aller-retour) ; couvre aussi les mises à jour automatiques, qui passent par dpkg ;
  · un job de pilote hôte `running` d'après l'agent-nœud (`/v1/host/drivers/<kind>/job`) ;
  · un rattrapage de capacités en cours dans CE processus (`routes.nodes._cap_jobs`).
Ce refus n'a PAS de `force` : contrairement à l'état D, attendre suffit toujours.
"""

import logging
import sys

from . import node_driver

log = logging.getLogger(__name__)

# Deux relevés à 2 s d'écart : un état D d'une milliseconde (attente disque ordinaire) n'est pas
# un blocage. Seul un processus en D aux DEUX relevés compte.
_PROBE = ("ps -eo pid=,stat=,comm=,wchan:40= | awk '$2 ~ /^D/'; sleep 2; echo ---; "
          "ps -eo pid=,stat=,comm=,wchan:40= | awk '$2 ~ /^D/'; echo ---; "
          "ps -eo comm= | grep -xE 'apt|apt-get|aptitude|dpkg' || true")

# setsid + nohup : l'appel à l'agent doit RENDRE LA MAIN avant que le noyau ne redémarre, sinon
# la requête meurt avec la machine et l'interface affiche une erreur pour un geste réussi.
_SYSRQ_REBOOT = ("setsid nohup sh -c 'sleep 3; echo 1 > /proc/sys/kernel/sysrq; sync; "
                 "echo s > /proc/sysrq-trigger; sleep 2; echo b > /proc/sysrq-trigger' "
                 ">/dev/null 2>&1 < /dev/null &")
_NORMAL_REBOOT = "systemd-run --on-active=3 systemctl reboot"


def _parse(out):
    """Processus en D aux deux relevés → [{pid, comm, wchan}]."""
    avant, apres = ((out or "").split("---") + ["", ""])[:2]

    def lire(bloc):
        vus = {}
        for ligne in bloc.splitlines():
            p = ligne.split()
            if len(p) >= 3 and p[0].isdigit():
                vus[p[0]] = {"pid": int(p[0]), "comm": p[2],
                             "wchan": p[3] if len(p) > 3 and p[3] != "-" else ""}
        return vus
    a, b = lire(avant), lire(apres)
    return [b[pid] for pid in b if pid in a]


def _parse_packages(out):
    """Gestionnaires de paquets vivants (3e section de la sonde) → noms triés, sans doublon."""
    parts = (out or "").split("---")
    if len(parts) < 3:
        return []
    return sorted({ligne.strip() for ligne in parts[2].splitlines() if ligne.strip()})


def _probe(node):
    """Un aller-retour : (processus en D, gestionnaires de paquets vivants), ou (None, None)."""
    try:
        rc, out, _ = node_driver.host_exec(node, _PROBE, timeout=20)
    except Exception as e:                                                  # noqa: BLE001
        log.warning("D-state probe failed on node %s: %s", node.get("id"), e)
        return None, None
    if rc != 0:
        return None, None
    return _parse(out), _parse_packages(out)


def blocked_processes(node):
    """Processus durablement bloqués dans le noyau sur `node`, ou None si la sonde a échoué."""
    return _probe(node)[0]


def _host_driver_jobs(node):
    """Pilotes hôte dont le job tourne sur le nœud (d'après l'agent). Un agent antérieur à 0.28.0
    n'a pas de pilotes hôte : rien à attendre, pas une erreur."""
    if not node_driver.has_agent(node):
        return []
    ok, data = node_driver.host_driver_call(node, "GET", "/v1/host/drivers", timeout=8)
    if not ok or not isinstance(data, dict):
        return []
    actifs = []
    for k in data.get("kinds") or []:
        kind = (k or {}).get("kind")
        if not kind:
            continue
        # Décalage hors fichier : on veut l'état du job, pas son journal.
        ok, job = node_driver.host_driver_call(node, "GET", "/v1/host/drivers/%s/job?offset=%d"
                                               % (kind, 1 << 40), timeout=8)
        if ok and isinstance(job, dict) and (job.get("job") or {}).get("status") == "running":
            actifs.append(kind)
    return actifs


def _capability_job(node_id):
    """Capacités en cours de rattrapage sur ce nœud, ou None. Lu dans le module déjà chargé :
    s'il ne l'est pas, aucun rattrapage n'a pu être lancé par ce processus."""
    mod = sys.modules.get("app.routes.nodes")
    if mod is None:
        return None
    with mod._cap_lock:
        job = dict(mod._cap_jobs.get(node_id) or {})
    if job.get("status") != "running":
        return None
    return job.get("caps") or []


def busy_reasons(node, packages=None):
    """Ce qui interdit de couper le nœud MAINTENANT → [{kind, detail}] ; [] si rien.
    `packages` : résultat de la sonde déjà faite (évite un second aller-retour)."""
    raisons = [{"kind": "packages", "detail": ", ".join(packages)}] if packages else []
    raisons += [{"kind": "host_driver", "detail": k} for k in _host_driver_jobs(node)]
    caps = _capability_job(node.get("id"))
    if caps is not None:
        raisons.append({"kind": "capabilities", "detail": ", ".join(caps)})
    return raisons


def check(node):
    """Lecture seule, pour la boîte de confirmation : ce que les gardes diraient MAINTENANT.
    {probe_ok, blocked, busy} — `blocked`/`busy` à None si la sonde n'a pas répondu."""
    blocked, packages = _probe(node)
    if blocked is None:
        return {"probe_ok": False, "blocked": None, "busy": None}
    return {"probe_ok": True, "blocked": blocked, "busy": busy_reasons(node, packages)}


def reboot(node, force=False):
    """Redémarre `node`. Retourne (corps JSON, code HTTP).

    Codes d'erreur (traduits par l'interface, jamais une phrase) :
      probe_failed      — impossible de savoir ; on ne redémarre PAS à l'aveugle
      node_busy         — installation en cours ; `busy` la décrit ; `force` n'y change rien
      blocked_processes — processus en D, `force` absent ; `blocked` les liste
    """
    blocked, packages = _probe(node)
    if blocked is None:
        return {"ok": False, "error": "probe_failed"}, 502
    busy = busy_reasons(node, packages)
    if busy:
        return {"ok": False, "error": "node_busy", "busy": busy}, 409
    if blocked and not force:
        return {"ok": False, "error": "blocked_processes", "blocked": blocked}, 409
    cmd = _SYSRQ_REBOOT if blocked else _NORMAL_REBOOT
    rc, _o, e = node_driver.host_exec(node, cmd, timeout=20)
    if rc != 0:
        return {"ok": False, "error": "reboot_failed", "detail": (e or "").strip()[:200]}, 500
    if blocked:
        log.warning("node %s: forced sysrq reboot, %d process(es) stuck in kernel: %s",
                    node.get("name"), len(blocked),
                    ", ".join("%s(%s)" % (p["comm"], p["wchan"]) for p in blocked[:5]))
    return {"ok": True, "mode": "sysrq" if blocked else "normal", "blocked": blocked}, 200


def poweroff_guard(node):
    """Avant un ARRÊT complet : None si l'arrêt peut partir, sinon (corps JSON, code HTTP)."""
    blocked, packages = _probe(node)
    if blocked is None:
        return {"ok": False, "error": "probe_failed"}, 502
    busy = busy_reasons(node, packages)
    if busy:
        return {"ok": False, "error": "node_busy", "busy": busy}, 409
    if blocked:
        return {"ok": False, "error": "blocked_processes", "blocked": blocked}, 409
    return None
