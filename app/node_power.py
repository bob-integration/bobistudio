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
"""

import logging

from . import node_driver

log = logging.getLogger(__name__)

# Deux relevés à 2 s d'écart : un état D d'une milliseconde (attente disque ordinaire) n'est pas
# un blocage. Seul un processus en D aux DEUX relevés compte.
_PROBE = ("ps -eo pid=,stat=,comm=,wchan:40= | awk '$2 ~ /^D/'; sleep 2; echo ---; "
          "ps -eo pid=,stat=,comm=,wchan:40= | awk '$2 ~ /^D/'")

# setsid + nohup : l'appel à l'agent doit RENDRE LA MAIN avant que le noyau ne redémarre, sinon
# la requête meurt avec la machine et l'interface affiche une erreur pour un geste réussi.
_SYSRQ_REBOOT = ("setsid nohup sh -c 'sleep 3; echo 1 > /proc/sys/kernel/sysrq; sync; "
                 "echo s > /proc/sysrq-trigger; sleep 2; echo b > /proc/sysrq-trigger' "
                 ">/dev/null 2>&1 < /dev/null &")
_NORMAL_REBOOT = "systemd-run --on-active=3 systemctl reboot"


def _parse(out):
    """Processus en D aux deux relevés → [{pid, comm, wchan}]."""
    avant, _, apres = (out or "").partition("---")

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


def blocked_processes(node):
    """Processus durablement bloqués dans le noyau sur `node`, ou None si la sonde a échoué."""
    try:
        rc, out, _ = node_driver.host_exec(node, _PROBE, timeout=20)
    except Exception as e:                                                  # noqa: BLE001
        log.warning("D-state probe failed on node %s: %s", node.get("id"), e)
        return None
    if rc != 0:
        return None
    return _parse(out)


def reboot(node, force=False):
    """Redémarre `node`. Retourne (corps JSON, code HTTP).

    Codes d'erreur (traduits par l'interface, jamais une phrase) :
      probe_failed      — impossible de savoir ; on ne redémarre PAS à l'aveugle
      blocked_processes — processus en D, `force` absent ; `blocked` les liste
    """
    blocked = blocked_processes(node)
    if blocked is None:
        return {"ok": False, "error": "probe_failed"}, 502
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
    blocked = blocked_processes(node)
    if blocked is None:
        return {"ok": False, "error": "probe_failed"}, 502
    if blocked:
        return {"ok": False, "error": "blocked_processes", "blocked": blocked}, 409
    return None
