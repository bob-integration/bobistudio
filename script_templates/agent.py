#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Agent par-LXC : expose une API HTTP minimale sur :8081 pour que l'orchestrateur
puisse écrire un script Python sur disque, le démarrer, l'arrêter, et savoir
s'il tourne.

Contrat (voir CLAUDE.md projet) :
- POST /deploy          {path, content} → écrit le fichier
- POST /start                           → spawne le script
- POST /stop                            → tue le process
- GET  /status                          → {running, path, last_exit, last_signal, last_exit_ts}
- POST /nmos/subscribe  {receiver_index, essence, enabled, sdp, ...} → bascule SDP + restart

La sortie (stdout + stderr) du script déployé est HÉRITÉE de l'agent → elle sort sur la sortie
standard du CONTENEUR (`docker logs` / journald, rotation Docker 50 Mo × 5), plus dans un fichier
interne au rootfs éphémère.
"""
import hmac
import json
import os
import signal
import ssl
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8081
LAST_PATH = None
PROC = None
# Dernière mort du script : code de sortie et signal. Publié dans `/status` — sans ça, un conteneur
# tombe en « script arrêté » SANS CAUSE, et il faut aller lire `docker logs` à la main pour
# découvrir un SIGILL (code -4) ou une exception. C'est ce qui a coûté le plus de temps dans le
# diagnostic des nœuds Sandy Bridge : l'interface disait « arrêté », jamais « instruction illégale ».
DERNIERE_MORT = {"code": None, "signal": None, "ts": None}
NMOS_SDP_DIR = "/tmp"

# Durcissement (hardening/audit-2026-07) :
#  - ALLOWED_SCRIPT_DIR : /deploy ne peut écrire QUE sous ce préfixe (anti path-traversal).
#    Défense active pour TOUS les conteneurs, y compris ceux sans token (compat).
#  - MXL_AGENT_TOKEN : auth OPTIONNELLE. Si l'env est posée (au docker run par l'orchestrateur),
#    l'agent EXIGE l'en-tête X-MXL-Agent-Token (comparaison hmac.compare_digest). Absente → aucun
#    contrôle (comportement historique) : les conteneurs LIVE de l'ancienne image continuent de tourner.
ALLOWED_SCRIPT_DIR = "/opt/script"
AGENT_TOKEN = os.environ.get("MXL_AGENT_TOKEN") or ""
AUTH_HEADER = "X-MXL-Agent-Token"

# TLS du plan de contrôle (mTLS, chantier feat/mtls) : le CONTRÔLEUR est le seul appelant de :8081.
# Il génère (app/ca.py) un cert conteneur signé par la CA interne et l'INJECTE au `docker run` ;
# l'agent-nœud l'écrit ici (bind-mount) sous /etc/bobi-tls/{cert.pem,key.pem,ca.pem}. Présents →
# on sert :8081 en HTTPS (cert serveur signé CA + mTLS si ca.pem). Absents → HTTP clair (repli,
# ne casse rien). L'auth token (ci-dessus) reste EN PLUS dans les deux cas (double facteur).
TLS_DIR = "/etc/bobi-tls"

# IDENTITÉ DU PAIR CLIENT (mTLS) — pourquoi ce contrôle existe :
# `CERT_REQUIRED` + `load_verify_locations(ca)` ne prouvent qu'UNE chose : le cert client est signé
# par notre CA. Or les certs de CONTENEUR sont émis avec EKU serverAuth **+ clientAuth** (app/ca.py)
# → la clé privée d'UN conteneur suffit à se faire passer pour le contrôleur auprès de TOUS les
# agents de la flotte (mouvement latéral). Le cert du contrôleur porte CN=bobi-controller, ceux des
# conteneurs CN=mxl<vmid> : la distinction est donc possible SANS regénérer aucun certificat.
# On vérifie le CN (retenu plutôt qu'une URI SAN : le cert contrôleur existant n'en porte pas —
# ses SAN sont les IP/DNS de contrôle — et l'exiger rendrait la flotte injoignable le jour du
# déploiement d'image ; l'URI SAN `bobi://controller` est acceptée EN PLUS si elle apparaît un jour).
# ÉCHAPPATOIRE (une installation dont le CN diffère ne doit pas se verrouiller hors de sa flotte) :
#   MXL_TLS_CLIENT_CN=<cn>          → CN attendu (défaut bobi-controller)
#   MXL_TLS_VERIFY_CLIENT_CN=0      → vérification DÉSACTIVÉE (comportement historique)
# Le token X-MXL-Agent-Token reste le second facteur, indépendant de ce contrôle.
TLS_CLIENT_CN = os.environ.get("MXL_TLS_CLIENT_CN") or "bobi-controller"
TLS_CLIENT_URI = "bobi://controller"
TLS_VERIFY_CLIENT_CN = (os.environ.get("MXL_TLS_VERIFY_CLIENT_CN", "1").strip().lower()
                        not in ("0", "false", "off", "no"))

# TROISIÈME IDENTITÉ : l'AGENT-NŒUD (URI SAN `bobi://node/<id>`, cf. app/ca.py:_san_list).
# Motif : quand le contrôleur est absent, plus personne ne relève un script mort à l'intérieur
# d'un conteneur qui, lui, tourne toujours (l'état `script_stopped`). Docker ne voit pas ce
# niveau-là, et l'agent-nœud — le seul survivant sur place — n'avait pas le droit de nous parler.
# On lui ouvre STRICTEMENT ce qu'il faut pour ça, et rien d'autre : constater et relancer.
# `/deploy` reste FERMÉ (il n'a pas la base : il ne peut pas rendre un script, seulement en pousser
# un — donc lui ouvrir /deploy n'apporterait rien et lui donnerait l'écriture arbitraire de fichier).
# `/stop` aussi : un watchdog n'arrête rien, il maintient. La distinction ne coûte rien à établir —
# les certs de nœud portent `bobi://node/`, ceux de conteneur `bobi://container/` — donc la barrière
# anti-mouvement-latéral d'origine (un conteneur ne pilote pas ses pairs) reste entière.
NODE_URI_PREFIX = "bobi://node/"
NODE_ALLOWED = (("GET", "/status"), ("POST", "/start"))
TLS_ALLOW_NODE = (os.environ.get("MXL_TLS_ALLOW_NODE", "1").strip().lower()
                  not in ("0", "false", "off", "no"))

# ... ET LE CHEMIN QUI SERT VRAIMENT : le LOOPBACK.
# L'ouverture ci-dessus supposait que l'agent-nœud pouvait joindre notre `:8081`. Il ne le peut PAS :
# nos conteneurs sont en macvlan, et une interface macvlan enfant ne parle jamais à la pile de son
# interface PARENTE — un nœud atteint les conteneurs de ses voisins, jamais les siens (mesuré :
# 100 % de perte vers son propre conteneur, 0 % vers celui d'en face). L'agent-nœud passe donc par
# `docker exec` et nous appelle sur 127.0.0.1, depuis NOTRE PROPRE espace de noms réseau.
# Il présente alors le seul matériel disponible là : NOTRE cert de conteneur — celui que
# `_peer_role` refuse, à raison, quand il arrive par le réseau.
# POURQUOI C'EST SÛR : atteindre notre loopback exige d'exécuter du code DANS ce conteneur, donc
# d'avoir Docker, donc d'être root sur l'hôte — qui peut déjà tout nous faire. L'exemption
# n'accorde aucun pouvoir nouveau ; elle nomme un chemin qui existait déjà.
# Elle reste ÉTROITE : loopback ET les deux mêmes endpoints que le nœud (NODE_ALLOWED). Un paquet
# venu du réseau avec une source 127.0.0.1 n'arrive pas ici (le noyau jette les sources martiennes
# sur une interface non-loopback).
LOOPBACK = ("127.0.0.1", "::1")
TLS_ALLOW_LOOPBACK = (os.environ.get("MXL_TLS_ALLOW_LOOPBACK", "1").strip().lower()
                      not in ("0", "false", "off", "no"))


def _peer_identities(cert):
    """(CN(s), URI(s)) d'un dict `getpeercert()`. Retourne deux listes (éventuellement vides)."""
    cns, uris = [], []
    for rdn in (cert or {}).get("subject", ()):
        for key, val in rdn:
            if key == "commonName":
                cns.append(val)
    for typ, val in (cert or {}).get("subjectAltName", ()):
        if typ == "URI":
            uris.append(val)
    return cns, uris


def _peer_role(conn):
    """RÔLE du pair TLS : "controller" (tous droits), "node" (agent-nœud, droits restreints à
    NODE_ALLOWED) ou None (refusé). Renvoie "controller" d'office hors TLS (repli http) ou si la
    vérification est désactivée — le comportement historique. Refuse explicitement (et bruyamment)
    un cert de CONTENEUR : signé par la même CA, donc accepté par la seule vérification de chaîne."""
    if not TLS_VERIFY_CLIENT_CN:
        return "controller"
    getpeercert = getattr(conn, "getpeercert", None)
    if getpeercert is None:
        return "controller"            # socket non-TLS : l'agent tourne en clair (repli assumé)
    cert = getpeercert()
    if not cert:
        # CERT_REQUIRED est posé quand ca.pem est présent : pas de cert ici = pas de TLS mutuel.
        print("[agent] REFUS : connexion TLS sans certificat client", flush=True)
        return None
    cns, uris = _peer_identities(cert)
    if TLS_CLIENT_CN in cns or TLS_CLIENT_URI in uris:
        return "controller"
    if TLS_ALLOW_NODE and any(str(u).startswith(NODE_URI_PREFIX) for u in uris):
        return "node"
    print("[agent] REFUS : certificat client signé par la CA mais d'identité inattendue "
          "(CN=%s, URI=%s ; attendu CN=%s). Un cert de CONTENEUR ne pilote pas un agent."
          % (cns or "-", uris or "-", TLS_CLIENT_CN), flush=True)
    return None


def _peer_autorise(conn):
    """Compat : vrai si le pair a un rôle reconnu (quel qu'il soit). Le contrôle FIN par endpoint
    est fait par Handler._peer_ok, qui seul connaît méthode et chemin."""
    return _peer_role(conn) is not None


def _safe_script_path(path):
    """Résout le realpath de `path` et le renvoie SEULEMENT s'il est sous ALLOWED_SCRIPT_DIR.
    None si hors périmètre (traversal, chemin absolu arbitraire). N'exige pas que le fichier existe."""
    if not path:
        path = os.path.join(ALLOWED_SCRIPT_DIR, "main.py")
    rp = os.path.realpath(path)
    base = os.path.realpath(ALLOWED_SCRIPT_DIR)
    if rp == base or rp.startswith(base + os.sep):
        return rp
    return None

# ── Surface NMOS du conteneur (« plan 2 » : le conteneur est un Node) ────────────────────────
# Le conteneur NE CALCULE RIEN : l'orchestrateur lui POUSSE un document décrivant ses ressources
# (POST /nmos), et l'agent le sert découpé sur /x-nmos/. Toute la logique NMOS — dérivation depuis
# le manifeste du plugin, identités, contraintes — reste côté orchestrateur, où elle est déjà
# écrite et éprouvée. Le conteneur n'embarque pas une seconde implémentation qui divergerait.
#
# ⚠ Pourquoi un POST /nmos et pas /deploy : `/deploy` positionne `LAST_PATH`, que `/start` utilise
# pour lancer le script. Y pousser un JSON ferait démarrer le JSON au redémarrage suivant.
NMOS_FICHIER = os.path.join(ALLOWED_SCRIPT_DIR, "nmos.json")
NMOS_IS04 = "v1.3"
NMOS_IS05 = "v1.1"
NMOS_COLLECTIONS = ("devices", "sources", "flows", "senders", "receivers")


def _nmos_doc():
    """Document poussé par l'orchestrateur, ou None. Relu à CHAQUE requête : il est réécrit au
    redéploiement, et servir une version en cache ferait annoncer des ressources disparues."""
    try:
        with open(NMOS_FICHIER) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _nmos_get(chemin):
    """(status, payload) pour un GET sous /x-nmos/. 404 si inconnu, 503 si rien n'a été poussé."""
    d = _nmos_doc()
    if d is None:
        # 503 et pas 404 : la surface EXISTE, elle n'est simplement pas encore alimentée. Un 404
        # ferait conclure à un agent qui ne sait pas faire de NMOS.
        return 503, {"error": "aucune description NMOS poussée par l'orchestrateur"}
    p = [x for x in chemin.split("?")[0].strip("/").split("/") if x]   # ['x-nmos', ...]
    p = p[1:]
    if not p:
        return 200, ["node/", "connection/"]

    if p[0] == "node":
        if len(p) == 1:
            return 200, [NMOS_IS04 + "/"]
        if p[1] != NMOS_IS04:
            return 404, {"error": "version IS-04 non servie"}
        if len(p) == 2:
            return 200, ["self/"] + [c + "/" for c in NMOS_COLLECTIONS]
        if p[2] == "self" and len(p) == 3:
            return 200, d.get("node") or {}
        if p[2] in NMOS_COLLECTIONS:
            items = d.get(p[2]) or []
            if len(p) == 3:
                return 200, items
            if len(p) == 4:
                un = next((x for x in items if x.get("id") == p[3]), None)
                return (200, un) if un else (404, {"error": "ressource inconnue"})
        return 404, {"error": "not found"}

    if p[0] == "connection":
        if len(p) == 1:
            return 200, [NMOS_IS05 + "/"]
        if p[1] != NMOS_IS05:
            return 404, {"error": "version IS-05 non servie"}
        if len(p) == 2:
            return 200, ["single/"]
        if p[2] != "single":
            return 404, {"error": "not found"}
        if len(p) == 3:
            return 200, ["senders/", "receivers/"]
        genre = p[3]
        if genre not in ("senders", "receivers"):
            return 404, {"error": "not found"}
        conn = (d.get("connection") or {}).get(genre) or {}
        if len(p) == 4:
            return 200, [i + "/" for i in conn]
        etat = conn.get(p[4])
        if etat is None:
            return 404, {"error": "ressource inconnue"}
        if len(p) == 5:
            return 200, ["constraints/", "staged/", "active/", "transportfile/"]
        if p[5] == "transportfile":
            # BCP-007-03 : MXL n'a pas de fichier de transport. L'endpoint doit exister et rendre
            # 404 — c'est le pendant de `manifest_href: null` côté IS-04.
            return 404, {"error": "pas de transport file en MXL"}
        if p[5] in ("constraints", "staged", "active"):
            return 200, etat.get(p[5])
        return 404, {"error": "not found"}

    return 404, {"error": "not found"}


_cpu_last_usec = None
_cpu_last_time = None


def _cgroup_cpu_usec():
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _cgroup_mem():
    used = limit = None
    try:
        with open("/sys/fs/cgroup/memory.current") as f:
            used = int(f.read().strip())
    except Exception:
        pass
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            s = f.read().strip()
            limit = 0 if s == "max" else int(s)
    except Exception:
        pass
    return used, limit


def _get_n_cpus():
    """Cores alloués : quota cpu.max (--cpus X) prioritaire, sinon affinity (--cpuset-cpus)."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            parts = f.read().strip().split()
            if parts[0] != "max":
                return max(1, round(int(parts[0]) / int(parts[1])))
    except Exception:
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        pass
    return 1


def _json(self, status, payload):
    body = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


def _script_pids():
    """PIDs réels du script déployé, trouvés par balayage de /proc (cmdline contient
    LAST_PATH). Autoritaire : survit à un redémarrage de l'agent (qui remet PROC=None
    alors que le script tourne encore → orphelin) et détecte les doublons.
    Exclut l'agent lui-même."""
    path = LAST_PATH or "/opt/script/main.py"
    pids = []
    my_pid = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = f.read().split(b"\x00")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            # cmdline = [python, -u, <path>] : on matche le chemin exact du script.
            if any(a.decode("utf-8", "replace") == path for a in args if a):
                pids.append(pid)
    except Exception:
        pass
    return pids


def _is_running():
    """Autoritaire : vrai si AU MOINS un process du script tourne réellement.
    Ne se fie plus au seul PROC (perdu si l'agent redémarre)."""
    global PROC
    if _script_pids():
        return True
    # Fallback : objet Popen encore vivant (cas où LAST_PATH inconnu)
    if PROC is not None and PROC.poll() is None:
        return True
    if PROC is not None:
        # Remontée du code de sortie sur la sortie standard du conteneur : la mort du script est
        # désormais tracée AU MÊME ENDROIT que ce qu'il a imprimé juste avant (docker logs).
        rc = PROC.returncode
        # Convention POSIX de subprocess : un code NÉGATIF est un signal (-4 = SIGILL, -9 = SIGKILL,
        # -11 = SIGSEGV). Les séparer, sinon « -4 » se lit comme un code d'erreur applicatif.
        DERNIERE_MORT["code"] = rc if (rc is None or rc >= 0) else None
        DERNIERE_MORT["signal"] = (-rc) if (rc is not None and rc < 0) else None
        DERNIERE_MORT["ts"] = time.time()
        print("[agent] script terminé (code %s)" % rc, flush=True)
    PROC = None
    return False


def _nmos_sdp_path(idx, essence="video", leg=None):
    # Séparation video/audio : nmos_recv_v_<idx>.sdp ou nmos_recv_a_<idx>.sdp.
    # leg=None → nom historique (compat) ; leg=0/1 → nom dual-path SMPTE 2022-7.
    suffix = "a" if essence == "audio" else "v"
    if leg is None:
        return os.path.join(NMOS_SDP_DIR, f"nmos_recv_{suffix}_{int(idx)}.sdp")
    return os.path.join(NMOS_SDP_DIR, f"nmos_recv_{suffix}_{int(idx)}_leg{leg}.sdp")


def _stop_proc():
    """Tue TOUS les process réels du script (PROC tracké + orphelins d'un précédent
    cycle d'agent). SIGTERM puis SIGKILL après grâce. Autoritaire via /proc."""
    global PROC
    import time as _t
    pids = set(_script_pids())
    if PROC is not None:
        pids.add(PROC.pid)
    if not pids:
        PROC = None
        return
    # SIGTERM sur le groupe de session de chaque PID (le script + ses enfants ffmpeg).
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Grâce : jusqu'à 3 s (marge sous le timeout HTTP de 5 s côté orchestrateur).
    deadline = _t.monotonic() + 3
    while _t.monotonic() < deadline:
        if not _script_pids() and (PROC is None or PROC.poll() is not None):
            break
        _t.sleep(0.1)
    # SIGKILL sur ce qui reste.
    for pid in set(_script_pids()) | pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    print("[agent] script arrêté (pids %s)" % sorted(pids), flush=True)
    PROC = None


def _start_proc():
    """Spawne le script déployé en lui faisant HÉRITER la sortie standard de l'agent.

    JOURNALISATION (2026-07) : le script écrivait auparavant dans /var/log/mxl-script.log,
    fichier INTERNE au conteneur — donc invisible de `docker logs` (l'orchestrateur ne voyait
    que le bavardage de l'agent), SANS rotation (24 Mo mesurés sur un mur multiview, quand les
    logs Docker sont plafonnés à 50 Mo × 5), et posé sur le rootfs ÉPHÉMÈRE (perdu à chaque
    recréation de conteneur ; et un fichier ouvert par une génération d'agent n'a plus rien à
    voir avec le fichier visible après recréation — d'où des logs « figés » alors que le script
    tourne). On câble donc stdout ET stderr sur les fd 1/2 de l'agent : la sortie part sur la
    sortie standard du CONTENEUR → `docker logs` / journald, rotation Docker, lisible par
    l'orchestrateur (metrics._crash_loop_logs → node_driver.container_logs).

    BUFFERISATION : l'agent tourne en `python3 -u`, mais le script est un AUTRE processus.
    - le script lui-même est lancé avec `-u` (déjà le cas) → stdout/stderr non bufferisés ;
    - PYTHONUNBUFFERED=1 est posé dans son environnement pour que ses petits-enfants Python
      (sous-process lancés par le script) le soient aussi ;
    - les petits-enfants non-Python (ffmpeg…) loggent sur stderr, non bufferisé par la libc.
    Sans ça, une sortie bufferisée par blocs de plusieurs Ko est inexploitable en diagnostic.

    fd 1 et fd 2 pointent le même pipe vers le démon Docker : une écriture d'une ligne
    (< PIPE_BUF) est atomique, les lignes de l'agent et du script ne s'entremêlent pas."""
    global PROC
    path = LAST_PATH or "/opt/script/main.py"
    if not os.path.exists(path):
        raise FileNotFoundError(f"script absent: {path}")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    PROC = subprocess.Popen(
        [sys.executable, "-u", path],
        stdout=1,               # fd 1 de l'agent = sortie standard du conteneur
        stderr=1,               # stderr fusionné dans la même sortie (ordre préservé)
        env=env,
        preexec_fn=os.setsid)
    print("[agent] script démarré : %s (pid %d) — sortie sur stdout du conteneur"
          % (path, PROC.pid), flush=True)


LEGACY_LOG = "/var/log/mxl-script.log"


def _purge_legacy_log():
    """Suppression FRANCHE de l'ancien journal fichier (aucun lecteur restant dans le projet :
    ni orchestrateur, ni script de plugin, ni Dockerfile — seule une note de changelog le cite).
    Deux mécanismes de journalisation dont un sans rotation, c'est précisément ce qu'on élimine :
    on récupère au passage la place qu'il occupe encore sur le rootfs des conteneurs existants."""
    try:
        if os.path.exists(LEGACY_LOG):
            size = os.path.getsize(LEGACY_LOG)
            os.unlink(LEGACY_LOG)
            print("[agent] ancien journal %s supprimé (%.1f Mo) — la sortie du script va "
                  "désormais sur stdout" % (LEGACY_LOG, size / 1048576.0), flush=True)
    except Exception as e:
        print("[agent] purge %s impossible : %s" % (LEGACY_LOG, e), flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _authed(self):
        """True si l'auth n'est pas activée (pas de token env) OU si l'en-tête correspond.
        Comparaison à temps constant."""
        if not AGENT_TOKEN:
            return True
        got = self.headers.get(AUTH_HEADER, "") or ""
        return hmac.compare_digest(got, AGENT_TOKEN)

    def _peer_ok(self, method):
        """Identité du pair TLS (cf. _peer_role) ET droit d'accès à CE endpoint. Répond 403 et
        journalise si refusé. Le contrôleur a tout ; l'agent-nœud et le loopback n'ont que
        NODE_ALLOWED. La CHAÎNE du cert reste vérifiée par la couche TLS dans tous les cas."""
        chemin = (self.path or "").split("?", 1)[0]
        adresse = (self.client_address[0] if self.client_address else "")
        # Loopback d'abord : c'est le chemin du chien de garde (docker exec), et le tester avant
        # `_peer_role` évite de journaliser un REFUS à chaque sondage — le client y présente notre
        # propre cert de conteneur, que `_peer_role` rejette légitimement quand il vient du réseau.
        if TLS_ALLOW_LOOPBACK and adresse in LOOPBACK:
            if (method, chemin) in NODE_ALLOWED:
                return True
            print("[agent] REFUS : appel loopback sur %s %s (autorisé : %s)"
                  % (method, chemin, ", ".join("%s %s" % t for t in NODE_ALLOWED)), flush=True)
            _json(self, 403, {"error": "forbidden: endpoint réservé au contrôleur"})
            return False
        role = _peer_role(self.connection)
        if role is None:
            _json(self, 403, {"error": "forbidden: identité de certificat client inattendue"})
            return False
        if role == "controller":
            return True
        # role == "node" : liste blanche stricte. Le chemin est comparé sans query string.
        if (method, chemin) in NODE_ALLOWED:
            return True
        print("[agent] REFUS : l'agent-nœud n'a pas le droit d'appeler %s %s "
              "(autorisé : %s)" % (method, chemin,
                                   ", ".join("%s %s" % t for t in NODE_ALLOWED)), flush=True)
        _json(self, 403, {"error": "forbidden: endpoint réservé au contrôleur"})
        return False

    def do_GET(self):
        if not self._peer_ok("GET"):
            return
        if not self._authed():
            _json(self, 401, {"error": "unauthorized"})
            return
        if self.path == "/status":
            # `_is_running()` d'abord : c'est lui qui constate la mort et renseigne DERNIERE_MORT.
            run = _is_running()
            _json(self, 200, {"running": run, "path": LAST_PATH,
                              "last_exit": DERNIERE_MORT["code"],
                              "last_signal": DERNIERE_MORT["signal"],
                              "last_exit_ts": DERNIERE_MORT["ts"]})
        elif self.path == "/stats":
            global _cpu_last_usec, _cpu_last_time
            now   = time.monotonic()
            usec  = _cgroup_cpu_usec()
            mem_used, mem_limit = _cgroup_mem()
            n_cpus = _get_n_cpus()
            cpu_pct = None
            if usec is not None and _cpu_last_usec is not None and _cpu_last_time is not None:
                delta_wall = (now - _cpu_last_time) * 1_000_000
                if delta_wall > 0:
                    cpu_pct = round(
                        max(0.0, min(100.0, (usec - _cpu_last_usec) / delta_wall / n_cpus * 100)), 1
                    )
            if usec is not None:
                _cpu_last_usec = usec
                _cpu_last_time = now
            _json(self, 200, {"cpu_pct": cpu_pct, "mem_used": mem_used,
                              "mem_limit": mem_limit, "cpu_count": n_cpus})
        elif self.path.split("?")[0].startswith("/x-nmos/") or self.path.split("?")[0] == "/x-nmos":
            code, payload = _nmos_get(self.path)
            _json(self, code, payload)
        else:
            _json(self, 404, {"error": "not found"})

    def do_POST(self):
        global LAST_PATH, PROC
        if not self._peer_ok("POST"):
            return
        if not self._authed():
            _json(self, 401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode())
            except Exception:
                pass

        if self.path == "/deploy":
            path = _safe_script_path(body.get("path"))
            if path is None:
                _json(self, 400, {"error": "path hors du périmètre autorisé (%s)" % ALLOWED_SCRIPT_DIR})
                return
            content = body.get("content", "")
            try:
                os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)
                LAST_PATH = path
                _json(self, 200, {"status": "ok", "path": path})
            except Exception as e:
                _json(self, 500, {"error": str(e)})
            return

        if self.path == "/nmos":
            # Remplace intégralement la description servie. Écriture ATOMIQUE (fichier temporaire
            # puis rename) : une requête GET concurrente doit voir l'ancienne version ou la
            # nouvelle, jamais un fichier tronqué qu'elle prendrait pour une absence de surface.
            try:
                tmp = NMOS_FICHIER + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(body, f)
                os.replace(tmp, NMOS_FICHIER)
                _json(self, 200, {"status": "ok"})
            except Exception as e:
                _json(self, 500, {"error": str(e)})
            return

        if self.path == "/start":
            if _is_running():
                _json(self, 200, {"status": "already_running"})
                return
            try:
                _start_proc()   # chemin unique de spawn (idem /nmos/subscribe)
                _json(self, 200, {"status": "started", "pid": PROC.pid})
            except FileNotFoundError as e:
                _json(self, 400, {"error": str(e)})
            except Exception as e:
                _json(self, 500, {"error": str(e)})
            return

        if self.path == "/stop":
            if not _is_running():
                _json(self, 200, {"status": "not_running"})
                return
            try:
                _stop_proc()
                _json(self, 200, {"status": "stopped"})
            except Exception as e:
                _json(self, 500, {"error": str(e)})
            return

        if self.path == "/nmos/subscribe":
            idx = body.get("receiver_index", 0)
            essence = body.get("essence") or "video"
            enabled = bool(body.get("enabled"))
            sdp = body.get("sdp") or ""
            was_running = _is_running()
            try:
                if isinstance(sdp, list):
                    # SMPTE 2022-7 dual-path : sdp = [leg0_sdp, leg1_sdp]
                    sdp_paths = [_nmos_sdp_path(idx, essence, leg=i) for i in range(len(sdp))]
                    if enabled:
                        for s, p in zip(sdp, sdp_paths):
                            if s:
                                with open(p, "w") as f:
                                    f.write(s)
                    else:
                        for p in sdp_paths:
                            if os.path.exists(p):
                                os.unlink(p)
                    sdp_path = sdp_paths[0] if sdp_paths else _nmos_sdp_path(idx, essence)
                else:
                    # Single-path (compat)
                    sdp_path = _nmos_sdp_path(idx, essence)
                    if enabled:
                        if not sdp:
                            _json(self, 400, {"error": "sdp requis quand enabled=true"})
                            return
                        with open(sdp_path, "w") as f:
                            f.write(sdp)
                    else:
                        if os.path.exists(sdp_path):
                            os.unlink(sdp_path)
                _stop_proc()
                if enabled and LAST_PATH:
                    _start_proc()
                _json(self, 200, {
                    "status": "ok",
                    "sdp_path": sdp_path,
                    "enabled": enabled,
                    "restarted": enabled and was_running,
                })
            except Exception as e:
                _json(self, 500, {"error": str(e)})
            return

        _json(self, 404, {"error": "not found"})


def _maybe_wrap_tls(httpd):
    """Enveloppe le socket :8081 en TLS si /etc/bobi-tls/{cert.pem,key.pem} existent (cert conteneur
    injecté par le contrôleur au `docker run`). Avec ca.pem → mTLS (le conteneur exige le cert client
    du contrôleur). Sans les certs → HTTP clair (repli). Log le mode retenu."""
    cert = os.path.join(TLS_DIR, "cert.pem")
    key  = os.path.join(TLS_DIR, "key.pem")
    ca   = os.path.join(TLS_DIR, "ca.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        print("[agent] :%d HTTP clair (pas de certs sous %s)" % (PORT, TLS_DIR), flush=True)
        return httpd
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    mode = "TLS (serveur)"
    if os.path.exists(ca):
        ctx.load_verify_locations(cafile=ca)
        ctx.verify_mode = ssl.CERT_REQUIRED   # mTLS : exige le cert client du contrôleur (signé CA)
        # …et la CHAÎNE ne suffit pas : l'identité du pair est vérifiée par requête (_peer_autorise).
        mode = ("mTLS + identité client CN=%s" % TLS_CLIENT_CN if TLS_VERIFY_CLIENT_CN
                else "mTLS SANS vérification d'identité client (MXL_TLS_VERIFY_CLIENT_CN=0)")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("[agent] :%d %s (%s)" % (PORT, mode, TLS_DIR), flush=True)
    return httpd


if __name__ == "__main__":
    _purge_legacy_log()
    _maybe_wrap_tls(HTTPServer(("0.0.0.0", PORT), Handler)).serve_forever()
