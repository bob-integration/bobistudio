# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Journaux de conteneurs DURABLES (journald) — pilote de log, prép nœud, interrogation.

POURQUOI. Les deux backends (`docker_driver` = moteur 2110 MTL, `docker_compute` = compute/média)
lançaient les conteneurs avec le pilote `json-file`. Ce journal vit dans
`/var/lib/docker/containers/<id>/` et **disparaît avec le conteneur** : le moteur 2110 tourne en
`--rm` et chaque redéploiement le recrée, donc les traces s'évaporaient exactement au moment où on
en avait besoin (post-mortem d'un crash-loop, d'une mort TX, d'un moteur revenu vide).

CE QU'ON POSE. Pilote `journald` : le journal appartient à l'**hôte** (systemd-journald), pas au
conteneur. Il survit à la destruction du conteneur ET au reboot du nœud (stockage persistant sous
`/var/log/journal`).

IDENTIFICATION D'UN CONTENEUR DANS LE JOURNAL. Le pilote journald de Docker pose sur CHAQUE entrée
(cf. docs Docker « journald logging driver ») :
  · `CONTAINER_NAME`      — nom du conteneur AU MOMENT DU RUN (ex. `bobi-mtl-140`) ;
  · `CONTAINER_ID` / `CONTAINER_ID_FULL` — id court / long ;
  · `CONTAINER_TAG` (= `SYSLOG_IDENTIFIER`) — le `--log-opt tag=…`, **par défaut l'id court**.
Un id est inutilisable après coup (on ne le connaît plus), donc on force `--log-opt tag={{.Name}}` :
le tag devient le NOM, ce qui rend les lignes lisibles dans un `journalctl` brut et greppables par
`SYSLOG_IDENTIFIER`. La clé de recherche est donc le **nom**, qui est DÉTERMINISTE à partir du vmid
(`docker_driver._name` → `bobi-mtl-<vmid>`, `docker_compute._name` → `bobi-cmp-<vmid>`) : on
retrouve l'historique d'un conteneur **détruit** sans rien avoir mémorisé de lui.
On interroge en OU (`CONTAINER_NAME=<n> + CONTAINER_TAG=<n>`) pour couvrir aussi les conteneurs
lancés avant l'ajout du `tag=` (CONTAINER_NAME est toujours posé, lui).

RATE LIMIT — le piège. journald jette **SILENCIEUSEMENT** les messages au-delà de
`RateLimitBurst` par `RateLimitIntervalSec` et par service (défaut amont : 10000/30 s ; c'était
1000 avant systemd 240 — donc « le défaut » n'est même pas stable dans le temps). Un moteur en
crash-loop ou un plugin bavard perdrait des lignes sans que rien ne le signale : c'est
exactement l'anti-patron n°1 du projet (l'échec silencieux), et il frapperait au pire moment.
On DÉSACTIVE donc la limitation (`RateLimitBurst=0`) et on borne par la TAILLE
(`SystemMaxUse`) : la perte devient une rotation du plus ancien — bornée, prévisible, et
VISIBLE (la route publie la plus ancienne entrée encore disponible). Le disque, lui, ne peut
plus être saturé comme l'a fait le json-file non borné (225 Go en ~13 h, dl360-1).

RÉTENTION. Journal plafonné en taille → l'historique n'est PAS infini. `retention()` publie
l'occupation, le plafond et la plus ancienne entrée disponible ; la route les renvoie avec les
lignes pour qu'on ne prenne jamais un trou de rotation pour un silence du conteneur.

MIGRATION. Le pilote de log est figé à la CRÉATION du conteneur : la bascule ne prend effet
qu'au prochain (re)déploiement de CHAQUE conteneur. Tant qu'un conteneur tourne en `json-file`,
`journalctl` ne renverra rien pour lui → la route retombe explicitement sur `docker logs`
(champ `source` de la réponse) au lieu de mentir avec une liste vide.
"""

import logging
import re
import shlex

log = logging.getLogger(__name__)

# Plafond dur du nombre de lignes servies par la route (cf. container_logs.py) : sans borne, un
# `?lines=1e7` ramènerait des centaines de Mo de journal dans la RAM du contrôleur.
MAX_LINES = 2000
# Plafond dur par ligne : une trace binaire/JSON d'un plugin peut faire des Mo à elle seule.
MAX_LINE_CHARS = 4000

JOURNALD_DROPIN_PATH = "/etc/systemd/journald.conf.d/10-bobi.conf"

# Valeurs EXPLICITES (ne jamais dépendre du défaut amont, cf. docstring) :
#  · Storage=persistent   → /var/log/journal, survit au reboot (le mode `auto` ne persiste que si
#                           le dossier existe déjà : on crée le dossier ET on l'écrit noir sur blanc) ;
#  · SystemMaxUse=4G      → plafond du journal ; c'est LE garde-fou disque (les nœuds ont ~600 Mo
#                           de journal pour ~1 mois : 4 Go laissent une marge confortable même avec
#                           des conteneurs bavards, sans risquer le disque) ;
#  · SystemKeepFree=4G    → journald s'efface avant de remplir le disque du nœud ;
#  · SystemMaxFileSize=256M / SystemMaxFiles=64 → rotation par petits fichiers = purge granulaire ;
#  · RateLimit* = 0       → AUCUN message jeté en silence (cf. docstring) ;
#  · ForwardToSyslog=no   → pas de double écriture (rsyslog n'est pas installé sur les nœuds).
JOURNALD_DROPIN = """# BOBI — journal systemd DURABLE pour les conteneurs (pilote docker `journald`).
# Genere par app/journal.py (ensure_journal_durable) — ne pas editer a la main.
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=4G
SystemKeepFree=4G
SystemMaxFileSize=256M
SystemMaxFiles=64
# Limitation de debit DESACTIVEE : au-dela du burst, journald jette les messages SANS RIEN DIRE.
# Un moteur 2110 en crash-loop perdrait justement les lignes qui expliquent le crash. La borne
# est la TAILLE (SystemMaxUse ci-dessus), dont la perte est bornee, previsible et OBSERVABLE.
RateLimitIntervalSec=0
RateLimitBurst=0
ForwardToSyslog=no
"""


# ─── Pilote de log des conteneurs ────────────────────────────────────────────
def driver():
    """Pilote de log à poser sur les conteneurs. Réglage `container_log_driver` (défaut `journald`) ;
    `json-file` reste sélectionnable pour un nœud dont le journal ne serait pas persistant."""
    try:
        from . import settings as _st
        v = str(_st.get("container_log_driver") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in ("journald", "json-file") else "journald"


def log_opts(name):
    """Options de log (driver + opts) pour un conteneur nommé `name`, sous forme de dict.
    Source unique de vérité partagée par les deux backends ET par la spec agent-nœud."""
    if driver() == "json-file":
        # Historique : json-file NON borné a déjà saturé un disque (225 Go en 13 h). Toujours borné.
        return {"driver": "json-file", "opts": {"max-size": "50m", "max-file": "5"}}
    # `tag={{.Name}}` : sans ça CONTAINER_TAG/SYSLOG_IDENTIFIER = l'id court du conteneur, inutilisable
    # a posteriori (on ne connaît plus l'id d'un conteneur détruit). Avec, le NOM sert de clé.
    return {"driver": "journald", "opts": {"tag": "{{.Name}}"}}


def docker_flags(name):
    """Fragment de ligne `docker run` (chemin SSH des deux backends), terminé par une espace."""
    lo = log_opts(name)
    s = f"--log-driver {shlex.quote(lo['driver'])} "
    for k, v in (lo.get("opts") or {}).items():
        s += f"--log-opt {shlex.quote(f'{k}={v}')} "
    return s


# ─── Prép nœud : journal persistant + limites explicites ─────────────────────
def verifier(host, run=None):
    """État (LECTURE SEULE) du journal d'un nœud : persistance, drop-in BOBI posé, limitation de
    débit effective, occupation. Retourne un dict (jamais d'exception)."""
    from .mtl import _run_ssh
    run = run or _run_ssh(host)
    cmd = (
        "echo '@@PERSIST='$([ -d /var/log/journal ] && echo yes || echo no); "
        f"echo '@@DROPIN='$([ -f {JOURNALD_DROPIN_PATH} ] && echo yes || echo no); "
        "echo '@@USAGE='$(journalctl --disk-usage 2>/dev/null | tr -d '\\n'); "
        # `#?` : les lignes COMMENTÉES du journald.conf amont sont la documentation du défaut —
        # les afficher permet de voir d'un coup d'œil ce qui est EXPLICITE (sans #) et ce qui est
        # implicite (avec #), au lieu d'un écran vide qui ne dit rien.
        "echo '@@CONF='; systemd-analyze cat-config systemd/journald.conf 2>/dev/null "
        "| grep -Ei '^#?(Storage|SystemMaxUse|SystemKeepFree|RateLimit)' | tr '\\n' ';'"
    )
    rc, out, err = run(cmd, timeout=30)
    txt = out or ""
    def _g(k):
        m = re.search(r"@@%s=(.*)" % k, txt)
        return (m.group(1).strip() if m else "")
    return {
        "ok": rc == 0,
        "persistent": _g("PERSIST") == "yes",
        "dropin": _g("DROPIN") == "yes",
        "disk_usage": _g("USAGE"),
        "conf": txt.split("@@CONF=")[-1].strip() if "@@CONF=" in txt else "",
        "error": (err or "").strip()[:200] if rc != 0 else "",
    }


def ensure_journal_durable(host, run=None):
    """Garantit un journal systemd DURABLE et EXPLICITEMENT borné sur le nœud (idempotent) :
    `/var/log/journal` + drop-in `10-bobi.conf` (Storage/SystemMaxUse/RateLimit), puis
    `systemctl restart systemd-journald` pour appliquer à chaud.

    Ne présume PAS que le nœud a déjà un journal persistant (les deux nœuds du parc l'ont, un
    futur nœud non). Ne recrée AUCUN conteneur : la bascule du pilote de log ne prend effet qu'au
    prochain (re)déploiement de chacun. Retourne (ok, msg, clef_i18n, params) : `msg` reste la
    phrase française historique (renvoyée telle quelle à l'appelant HTTP) ; `clef_i18n`/`params`
    portent le même contenu pour l'alerte, seule à être rendue dans la langue du lecteur."""
    from .mtl import _run_ssh
    if not host:
        return False, "host non configuré", "alert.prep.journal_host_non_configure", {}
    run = run or _run_ssh(host)
    cmd = (
        "set -e; "
        "mkdir -p /var/log/journal; "
        "systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true; "
        "mkdir -p /etc/systemd/journald.conf.d; "
        f"cat > {JOURNALD_DROPIN_PATH} << 'BOBIEOF'\n{JOURNALD_DROPIN}BOBIEOF\n"
        # Le redémarrage de journald ne perd RIEN (les entrées sont déjà sur disque) et ne
        # perturbe aucun conteneur : c'est le seul « restart » de cette prép.
        "systemctl restart systemd-journald 2>&1 || echo '@@FAIL'; "
        "journalctl --disk-usage 2>&1 | tr -d '\\n'"
    )
    rc, out, err = run(cmd, timeout=60)
    if rc != 0:
        detail = (err or out).strip()[:200]
        return (False, f"rc={rc} {detail}",
                "alert.prep.journal_echec_rc", {"rc": rc, "detail": detail})
    if "@@FAIL" in (out or ""):
        detail = (out or "").strip()[:200]
        return (False, f"drop-in posé mais journald n'a pas redémarré : {detail}",
                "alert.prep.journal_dropin_sans_redemarrage", {"detail": detail})
    detail = (out or "").strip()[:160]
    return (True, (f"journal persistant + limites explicites posées ; {detail} — "
                   "le pilote journald ne s'applique qu'aux conteneurs RECRÉÉS après cette prép"),
            "alert.prep.journal_durable_pose", {"detail": detail})


# ─── Interrogation ───────────────────────────────────────────────────────────
_SINCE_RE = re.compile(r"^[0-9a-zA-Z:\-\+ \.@]{1,40}$")   # « 2026-07-25 10:00:00 », « -2h », « today »
_PRIO_RE = re.compile(r"^[0-7](\.\.[0-7])?$|^(emerg|alert|crit|err|warning|notice|info|debug)$")


def _runner(node):
    """Runner d'exécution hôte du nœud : agent-nœud si enrôlé, sinon SSH."""
    from . import mtl, node_driver
    if node_driver.has_agent(node):
        return mtl._run_agent(node)
    return mtl._run_ssh((node or {}).get("host"))


def retention(node, run=None):
    """Ce que le journal du nœud peut encore raconter : occupation, plafond configuré, plus ANCIENNE
    entrée disponible. Publié avec les lignes pour qu'un trou de rotation ne passe jamais pour un
    silence du conteneur. Jamais d'exception : retourne un dict (éventuellement partiel)."""
    run = run or _runner(node)
    cmd = (
        "echo '@@USAGE='$(journalctl --disk-usage 2>/dev/null | sed 's/[^0-9.]*\\([0-9.]*[KMGT]\\).*/\\1/'); "
        "echo '@@MAX='$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null "
        "| grep -Ei '^SystemMaxUse=' | tail -1 | cut -d= -f2); "
        "echo '@@OLDEST='$(journalctl --no-pager -o short-iso -q 2>/dev/null | head -1 | cut -d' ' -f1)"
    )
    try:
        rc, out, _ = run(cmd, timeout=30)
    except Exception as e:
        return {"error": str(e)[:200]}
    txt = out or ""
    def _g(k):
        m = re.search(r"@@%s=(.*)" % k, txt)
        return (m.group(1).strip() if m else "")
    # `SystemMaxUse` non posé = défaut systemd (10 % du système de fichiers, plafonné à 4 Go) — on le
    # NOMME plutôt que de renvoyer un vide qui laisserait croire à un journal sans plafond.
    return {"disk_usage": _g("USAGE"),
            "max_use": _g("MAX") or "défaut systemd (10 % de /var, plafonné à 4 Go) — prép non appliquée",
            "oldest_entry": _g("OLDEST"), "ok": rc == 0}


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _chronologique(rows):
    """Normalise l'ordre des lignes en CHRONOLOGIQUE (plus ancienne en tête).

    PIÈGE VÉRIFIÉ sur dl360-1 : `journalctl -n N` sort en ordre chronologique, mais `journalctl -n N
    --grep …` sort en ordre INVERSE (il itère à rebours pour honorer `-n` et ne retrie pas). Sans
    cette normalisation, activer le filtre `grep` retournerait la fenêtre à l'envers SANS RIEN DIRE
    — une lecture de post-mortem à contresens (« l'erreur précède la cause »)."""
    ts = [(_TS_RE.match(r).group(1) if _TS_RE.match(r) else None) for r in rows]
    connus = [t for t in ts if t]
    if len(connus) >= 2 and connus[0] > connus[-1]:
        return list(reversed(rows))
    return rows


def lire(node, name, lines=200, since=None, until=None, priority=None, grep=None,
         boot=None, run=None):
    """Journal d'un conteneur, LU DEPUIS L'HÔTE (`journalctl`) — donc disponible même si le
    conteneur n'existe plus (recréé, `--rm`, nœud rebooté).

    `docker logs` est volontairement ÉCARTÉ comme source primaire : il ne fonctionne que tant que
    le conteneur existe, donc l'utiliser reviendrait à payer journald sans en tirer le bénéfice.
    Il ne sert que de REPLI explicite (`source == "docker"`) pour la flotte pas encore migrée.

    Retourne (dict, http_status). `lines` est plafonné par l'appelant (MAX_LINES)."""
    run = run or _runner(node)
    q = shlex.quote(name)
    n = max(1, min(int(lines or 200), MAX_LINES))
    args = ["journalctl", "--no-pager", "-o", "short-iso", "-q", "-n", str(n)]
    # OU explicite entre les deux champs : CONTAINER_NAME est posé par le pilote journald sur tout
    # conteneur ; CONTAINER_TAG ne vaut le nom que depuis qu'on force `--log-opt tag={{.Name}}`.
    # Des matchs sur des champs DIFFÉRENTS seraient ET-és par journalctl → le `+` est indispensable.
    args += [f"CONTAINER_NAME={name}", "+", f"CONTAINER_TAG={name}"]
    if boot not in (None, ""):
        args += ["-b", str(boot)] if re.match(r"^-?\d+$|^[0-9a-f]{32}$", str(boot)) else []
    if since and _SINCE_RE.match(str(since)):
        args += ["--since", str(since)]
    if until and _SINCE_RE.match(str(until)):
        args += ["--until", str(until)]
    if priority and _PRIO_RE.match(str(priority)):
        # Rappel : le pilote journald mappe stdout→PRIORITY=6 (info) et stderr→PRIORITY=3 (err).
        # `?priority=err` = « uniquement ce que le conteneur a écrit sur stderr ».
        args += ["-p", str(priority)]
    if grep:
        args += ["--grep", str(grep)[:200], "--case-sensitive=no"]
    cmd = " ".join(shlex.quote(a) if a != "+" else "+" for a in args)
    try:
        rc, out, err = run(cmd, timeout=45)
    except Exception as e:
        return {"ok": False, "name": name, "error": f"exécution hôte impossible : {e}"[:300]}, 502
    # `journalctl --grep` sort avec le code 1 quand RIEN NE CORRESPOND (comportement documenté) :
    # ce n'est PAS une erreur, c'est un résultat vide. Le confondre avec un échec afficherait un
    # 502 à chaque recherche infructueuse. Seul un rc≠0 AVEC du stderr est un vrai échec.
    if rc != 0 and not (out or "").strip() and (err or "").strip():
        return {"ok": False, "name": name, "source": "journald",
                "error": (err or "journalctl a échoué").strip()[:300]}, 502
    rows = _chronologique([l[:MAX_LINE_CHARS] for l in (out or "").splitlines() if l.strip()])
    src = "journald"
    fallback = None
    # Une requête FILTRÉE vide ne veut PAS dire « ce conteneur n'est pas dans journald » : demander
    # `priority=err` à un conteneur qui n'écrit rien sur stderr rend légitimement zéro ligne. Sans
    # cette distinction, on repliait sur `docker logs` — qui ne sait pas filtrer — et on rendait
    # donc des lignes NE CORRESPONDANT PAS au filtre demandé, en annonçant `source: "docker"` sur un
    # conteneur pourtant migré. Un opérateur cherchant les erreurs en voyait alors de fausses.
    # → on ne replie que si le conteneur est ABSENT du journal, filtres retirés.
    filtre_actif = bool(since or until or priority or grep or boot not in (None, ""))
    if not rows and filtre_actif:
        sonde = " ".join([shlex.quote(a) if a != "+" else "+" for a in
                          ["journalctl", "--no-pager", "-q", "-n", "1",
                           f"CONTAINER_NAME={name}", "+", f"CONTAINER_TAG={name}"]])
        try:
            rc3, out3, _e3 = run(sonde, timeout=20)
            if (out3 or "").strip():
                return {"ok": True, "name": name, "source": "journald", "lines": [], "count": 0,
                        "truncated": False,
                        "note": "aucune ligne pour ces critères — le conteneur EST bien dans le "
                                "journal, seul le filtre ne rend rien (élargir la période, le "
                                "niveau ou le motif)."}, 200
        except Exception:
            pass
    if not rows:
        # Rien dans le journal de l'hôte, filtres compris. Deux causes possibles, non confondues :
        # (a) le conteneur tourne encore en `json-file` (flotte pas encore migrée) → on va chercher
        #     ses lignes avec `docker logs` et on l'ANNONCE (`source: "docker"`) ;
        # (b) le conteneur n'a jamais rien écrit / a été purgé par rotation → liste vide assumée.
        rc2, out2, err2 = run(f"docker logs --tail {n} {q} 2>&1", timeout=30)
        if rc2 == 0:
            rows = [l[:MAX_LINE_CHARS] for l in (out2 or "").splitlines()]
            src = "docker"
            fallback = ("aucune entrée journald pour ce conteneur — repli sur `docker logs` : il "
                        "tourne encore avec l'ancien pilote `json-file`. Ces lignes seront PERDUES "
                        "à sa prochaine recréation ; redéployer le conteneur bascule le pilote. "
                        "ATTENTION : les filtres since/until/priority/grep/boot ne s'appliquent "
                        "PAS à ce repli (`docker logs` ne sait pas filtrer) — seul `lines` vaut.")
        elif "No such container" in (out2 or "") + (err2 or ""):
            # Ni journal hôte, ni conteneur vivant. On NOMME les deux causes possibles plutôt que
            # de renvoyer une liste vide muette (le pire des retours pour un post-mortem).
            fallback = ("aucune entrée dans le journal de l'hôte pour ce nom, et le conteneur "
                        "n'existe plus : soit il n'a jamais tourné avec le pilote `journald` "
                        "(fenêtre ANTÉRIEURE à la migration — ses logs sont définitivement perdus "
                        "avec son json-file), soit ses entrées ont été purgées par la rotation "
                        "(cf. `retention.oldest_entry`), soit le nom est faux.")
        else:
            fallback = ((err2 or out2 or "").strip()[:200] or
                        "aucune entrée dans le journal de l'hôte pour ce nom")
    return {"ok": True, "name": name, "source": src, "note": fallback,
            "lines": rows, "count": len(rows), "truncated": len(rows) >= n}, 200


def conteneurs_connus(node, run=None):
    """Conteneurs dont le journal de l'hôte garde une trace — VIVANTS OU NON.

    C'est tout l'intérêt de journald par rapport à `docker logs` : le journal appartient à l'hôte,
    donc il connaît des conteneurs qui n'existent plus (recréés, `--rm`, purgés de la base). Une
    liste bâtie sur `docker ps -a` ou sur la table `containers` raterait précisément les cas qu'on
    veut instruire en post-mortem.

    `journalctl -F CONTAINER_NAME` énumère les valeurs distinctes du champ. On croise ensuite avec
    les conteneurs vivants (`docker ps`) et avec la base pour rendre le nom d'hôte lisible.
    Renvoie (dict, http_status) : `{ok, node, containers:[{name, vmid, alive, in_db, hostname}]}`.
    """
    run = run or _runner(node)
    try:
        rc, out, err = run("journalctl -F CONTAINER_NAME 2>/dev/null; echo '@@VIVANTS'; "
                           "docker ps --format '{{.Names}}' 2>/dev/null", timeout=45)
    except Exception as e:
        return {"ok": False, "error": f"exécution hôte impossible : {e}"[:300]}, 502
    txt = out or ""
    bloc_j, _, bloc_v = txt.partition("@@VIVANTS")
    connus = sorted({l.strip() for l in bloc_j.splitlines() if l.strip()})
    vivants = {l.strip() for l in bloc_v.splitlines() if l.strip()}
    if not connus and (err or "").strip():
        return {"ok": False, "error": (err or "").strip()[:300]}, 502

    # Nom d'hôte lisible depuis la base quand le vmid y existe encore. Un conteneur détruit ET
    # purgé de la base ne garde que son nom Docker — c'est normal, et on ne l'invente pas.
    hostnames = {}
    try:
        from .database import db_get_containers
        for c in (db_get_containers() or []):
            if c.get("vmid") is not None:
                hostnames[int(c["vmid"])] = c.get("hostname")
    except Exception:
        pass

    lignes = []
    for nom in connus:
        m = re.match(r"^bobi-(?:mtl|cmp)-(\d+)$", nom)
        vmid = int(m.group(1)) if m else None
        lignes.append({"name": nom, "vmid": vmid, "alive": nom in vivants,
                       "in_db": vmid in hostnames if vmid is not None else False,
                       "hostname": hostnames.get(vmid)})
    # Vivants d'abord, puis par nom : on veut voir l'exploitation courante en haut, l'historique en bas.
    lignes.sort(key=lambda x: (not x["alive"], x["name"]))
    return {"ok": True, "node": {"id": node.get("id"), "name": node.get("name")},
            "containers": lignes, "count": len(lignes)}, 200
