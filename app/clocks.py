# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Horloges du cluster — sonde et cohérence inter-nœuds.

**Pourquoi ce module existe.** Le bus MXL indexe ses grains sur le TEMPS : `GrainIndex =
Timestamp / GrainDurationNs`, epoch SMPTE 2059-1 (docs/Timing.md du SDK, normatif). Deux nœuds qui
ne tombent pas sur la même grille ne peuvent NI aligner leurs flux entre eux, NI se les répliquer.
Or cet écart ne se voit nulle part : un nœud est resté 37 s à côté de tout le monde pendant des
semaines sans qu'aucun indicateur ne le dise — les flux répliqués depuis ce nœud paraissaient
simplement « périmés » chez leurs consommateurs, et la réplication RDMA n'y comprenait rien.

**Ce qui compte, et dans cet ordre :**
  1. `CLOCK_TAI` — la SEULE horloge que libmxl lit (`Clock::TAI` → `CLOCK_TAI`, offset logiciel
     NUL). C'est elle, et elle seule, qui définit la grille des grains.
  2. L'ÉCART entre les `CLOCK_TAI` des nœuds, rapporté à la durée d'un grain : c'est la mesure qui
     dit si le cluster peut travailler ensemble. Un écart absolu vis-à-vis d'un temps « vrai »
     n'aurait pas le même sens — c'est l'accord mutuel qui fait le genlock.
  3. Le `tai_offset` du NOYAU : `CLOCK_TAI = CLOCK_REALTIME + tai_offset`. Laissé à 0 (le défaut de
     Debian, ni timesyncd ni chrony ne le posent d'eux-mêmes), `CLOCK_TAI` vaut l'UTC — soit 37 s
     d'erreur, invisible tant qu'on ne regarde que l'heure civile.

**Deux conventions coexistent dans la flotte, et c'est voulu :**
  · nœud 2110 (`REALTIME = TAI`) : son horloge système est disciplinée par le client PTP interne de
    libmtl, qui y écrit du TAI. `tai_offset` y vaut 0, donc `CLOCK_TAI = REALTIME = TAI` — juste,
    mais par un chemin où `REALTIME` ment sur l'UTC (l'affichage civil est compensé ailleurs).
  · nœud compute (`REALTIME = UTC`) : chrony tient l'UTC et `leapsectz`/`leapseclist` pose
    `tai_offset = 37`, donc `CLOCK_TAI = UTC + 37 = TAI`.
Les deux donnent le bon `CLOCK_TAI` par des routes opposées. On EXPOSE donc la convention détectée
plutôt que d'en imposer une : ce qu'on vérifie, c'est le résultat.
"""

import json
import logging
import os
import socket
from collections import deque
from urllib.parse import urlparse
import threading
import time

from . import node_driver
from .database import DB_PATH, db_get_nodes

log = logging.getLogger(__name__)

# Décalage TAI↔UTC courant (secondes). Constante des tables de secondes intercalaires, pas un
# réglage : la changer ici ne changerait pas l'heure des nœuds, seulement notre jugement sur elle.
TAI_UTC_OFFSET_S = 37

# Seuil d'écart inter-nœuds : UN GRAIN. Au-delà, deux nœuds ne désignent plus la même image, ce
# qui est le point de rupture pour MXL. Dérivé de la cadence du site (`_fps_reference`) plutôt que
# posé en dur — 20 ms à 50 fps, 40 ms à 25, 16,7 ms à 60 — et surchargeable par le réglage
# `clock_spread_ms` pour l'exploitant qui sait quelque chose que la dérivation ignore.
#
# Ce seuil est resté INAPPLIQUÉ tant que l'écart se mesurait à travers `host/exec` : avec ±25 ms
# d'incertitude, il n'aurait fait que signaler le temps de réponse du réseau. L'endpoint natif
# (agent ≥ 0.18.0) descend à quelques centaines de µs, donc il peut enfin porter quelque chose.
def _seuil_ecart_ms():
    from . import settings as _s
    try:
        forcee = float(_s.get("clock_spread_ms") or 0)
        if forcee > 0:
            return forcee, "seuil forcé"
    except Exception:
        pass
    fps = _fps_reference()
    return 1000.0 / fps, "un grain à %g fps" % fps

# ─── Seuils de discipline locale : DÉRIVÉS du métier du nœud, pas posés au jugé ────────────────
#
# Il n'y a pas UN seuil, il y en a deux, parce que les nœuds ne font pas le même métier.
#
# · Nœud COMPUTE : son horloge ne sert qu'à tomber sur le MÊME GRAIN que ses pairs. Au-delà d'un
#   DEMI-GRAIN, il désigne l'image d'à côté — c'est là, et pas ailleurs, qu'est la limite de tenue.
#   La durée de grain se déduit du format vidéo par défaut (réglage `video_format_default`).
# · Nœud portant un MOTEUR 2110 : il ne se contente pas de désigner des grains, il CADENCE DES
#   PAQUETS sur le fil. C'est le modèle temporel ST 2110-21 qui commande, à la microseconde.
#
# Un seuil unique serait à la fois dix fois trop strict pour le compute et des milliers de fois
# trop permissif pour le 2110 : il ne protégerait personne.
SEUIL_2110_US = 1.0

# Cadence PLANCHER pour la dérivation. Entre 50 et 60 l'écart est mince (10 ms contre 8,33 ms de
# demi-grain) : on prend systématiquement la plus contraignante, ce qui évite de re-régler le seuil
# à chaque changement de format et ne coûte que 1,7 ms de marge.
FPS_PLANCHER = 60.0


def _fps_reference():
    """Cadence de référence du site : celle du format vidéo par défaut, plancher à FPS_PLANCHER.
    Repli sur le plancher si le réglage est absent ou illisible — on ne devine jamais plus lâche."""
    from . import settings as _s
    try:
        lab = (_s.get("video_format_default") or "").strip()
        for ligne in (_s.get("video_formats") or "").splitlines():
            champs = ligne.split(";")
            if len(champs) >= 4 and champs[0].strip() == lab:
                return max(float(champs[3]), FPS_PLANCHER)
    except Exception:
        pass
    return FPS_PLANCHER


def _seuil_derive_us(node):
    """Seuil de discipline locale pour CE nœud, en µs, et la raison qui le fonde."""
    if _a_un_moteur_2110(node):
        return SEUIL_2110_US, "pacing ST 2110 (µs)"
    demi_grain_us = (1_000_000.0 / _fps_reference()) / 2.0
    return demi_grain_us, "demi-grain à %g fps" % _fps_reference()


def _srv_commun():
    from . import settings as _s
    try:
        return (_s.get("ntp_servers") or "").replace(",", " ").strip()
    except Exception:
        return ""


def _seuil_local_us(node):
    """Seuil sur la PRÉCISION LOCALE de CE nœud (µs) : la QUALITÉ de sa discipline, relevée SUR le
    nœud (offset chrony, ou offset du client PTP du moteur), donc sans aller-retour réseau.

    À ne pas confondre avec `_seuil_ecart_ms`, qui porte sur la POSITION relative des nœuds. Deux
    questions distinctes : un nœud peut être parfaitement tenu (offset local nul) sur une grille
    décalée de ses pairs, et c'est justement le cas que le second seuil attrape.

    Dérivé par défaut (cf. _seuil_derive_us). Le réglage `clock_local_offset_us` REMPLACE la
    dérivation quand il est non nul — pour le cas où l'exploitant sait quelque chose que la
    dérivation ignore. À 0 (défaut), c'est la dérivation qui parle."""
    from . import settings as _s
    try:
        forcee = float(_s.get("clock_local_offset_us") or 0)
        if forcee > 0:
            return forcee, "seuil forcé"
    except Exception:
        pass
    return _seuil_derive_us(node)


def _alertes_actives():
    from . import settings as _s
    try:
        v = _s.get("clock_alerts_enabled")
        return True if v is None else bool(int(v))
    except Exception:
        return True

_TTL_S = 20.0          # les horloges ne bougent pas vite ; inutile de harceler les nœuds
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()

# Historique par nœud, pour la courbe : un point par relevé NON caché (~20 s), 90 points ≈ 30 min.
#
# PERSISTÉ, contrairement à ce que j'avais d'abord décidé. Le raisonnement « une horloge qui dérive
# se voit en minutes, pas en jours » était juste sur la physique et faux sur l'exploitation : cet
# orchestrateur redémarre à CHAQUE livraison, et un anneau en mémoire pure repart alors de zéro.
# Résultat observé — la page n'affichait jamais que « historique en cours de constitution », parce
# qu'on ne la regardait jamais plus de quelques minutes après un redémarrage. Une fenêtre de 30 min
# qui ne survit pas à un redémarrage n'est pas une fenêtre de 30 min.
# Même mécanique que les autres samplers (node_health, cpu_pressure) : écriture atomique throttlée.
_HIST_MAX = 90
_HIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "clocks_hist.json")
_HIST_FLUSH_S = 60.0
_hist = {}
_dernier_flush = 0.0


def _charger_hist():
    """Relit l'historique au démarrage. Les points plus vieux que la fenêtre sont jetés : rouvrir
    la page après une nuit ne doit pas ressusciter une courbe d'hier collée à celle d'aujourd'hui."""
    global _hist
    try:
        with open(_HIST_PATH) as f:
            brut = json.load(f) or {}
    except (OSError, ValueError):
        return
    limite = time.time() - _HIST_MAX * 30       # marge : ~30 s par point au pire
    for k, pts in brut.items():
        gardes = [p for p in pts if isinstance(p, dict) and (p.get("t") or 0) >= limite]
        if gardes:
            _hist[int(k)] = deque(gardes[-_HIST_MAX:], maxlen=_HIST_MAX)


def _flush_hist():
    """Écriture ATOMIQUE (tmp + replace) : un redémarrage au milieu du dump ne doit pas laisser un
    fichier tronqué qui empêcherait toute relecture ultérieure."""
    global _dernier_flush
    if time.time() - _dernier_flush < _HIST_FLUSH_S:
        return
    _dernier_flush = time.time()
    tmp = _HIST_PATH + ".tmp"
    try:
        with _lock:
            instantane = {str(k): list(v) for k, v in _hist.items()}
        with open(tmp, "w") as f:
            json.dump(instantane, f)
        os.replace(tmp, _HIST_PATH)
    except OSError as e:
        log.debug("clocks flush historique: %s", e)


def _pousser_hist(n):
    """Empile un point d'historique pour un nœud. On garde l'écart à la grille ET la précision
    locale : le premier dit « suis-je à l'heure », la seconde « à quel point mon horloge est-elle
    tenue » — deux questions différentes, et c'est la seconde qui bouge de façon lisible."""
    if not n.get("joignable"):
        return
    dq = _hist.setdefault(n["id"], deque(maxlen=_HIST_MAX))
    dq.append({"t": round(time.time()),
               # La MÉTHODE est estampillée avec le point, parce que la fenêtre survit aux
               # redémarrages et donc au changement de règle graduée. Sans ça, le passage de la
               # sonde shell (±25 ms) à l'endpoint natif (±0,2 ms) fabriquerait une marche de
               # ~25 ms au milieu de la série, que la régression lirait comme une dérive
               # spectaculaire — une panne inventée par notre propre correctif.
               "m": n.get("mesure"),
               "ecart_ms": n.get("ecart_ms"),
               "offset_us": (round(n["offset_s"] * 1e6, 1)
                             if n.get("offset_s") is not None else None)})

# Sonde exécutée SUR le nœud, en un seul appel : les trois horloges et le tai_offset doivent être
# lus au même instant, sinon on mesure le RTT en plus de l'écart. Sortie JSON sur une ligne.
_SONDE = r"""python3 -c '
import ctypes, json, subprocess
class TS(ctypes.Structure): _fields_=[("s",ctypes.c_long),("ns",ctypes.c_long)]
class TX(ctypes.Structure):
    _fields_=[("modes",ctypes.c_uint),("offset",ctypes.c_long),("freq",ctypes.c_long),
              ("maxerror",ctypes.c_long),("esterror",ctypes.c_long),("status",ctypes.c_int),
              ("constant",ctypes.c_long),("precision",ctypes.c_long),("tolerance",ctypes.c_long),
              ("ts",ctypes.c_long),("tus",ctypes.c_long),("tick",ctypes.c_long),
              ("ppsfreq",ctypes.c_long),("jitter",ctypes.c_long),("shift",ctypes.c_int),
              ("stabil",ctypes.c_long),("jitcnt",ctypes.c_long),("calcnt",ctypes.c_long),
              ("errcnt",ctypes.c_long),("stbcnt",ctypes.c_long),("tai",ctypes.c_int),
              ("pad",ctypes.c_int*11)]
l = ctypes.CDLL("libc.so.6")
def clk(cid):
    t = TS()
    return (t.s*10**9 + t.ns) if l.clock_gettime(cid, ctypes.byref(t)) == 0 else None
tx = TX(); l.adjtimex(ctypes.byref(tx))
# Les horloges se lisent ICI, en tete de programme, PAS dans le print final : les appels
# systemctl/chronyc qui suivent prennent une dizaine de millisecondes, et les y laisser
# revenait a dater le noeud a la FIN de la fenetre en pretendant le dater au MILIEU.
# (Chemin de repli seulement : le chemin nominal passe par /v1/host/clock, cf. _echange_clock.)
_rt, _tai, _mono = clk(0), clk(11), clk(1)
def actif(u):
    try:
        return subprocess.run(["systemctl","is-active",u], capture_output=True,
                              text=True, timeout=4).stdout.strip() == "active"
    except Exception:
        return False
def unites_actives(motif):
    # Les unites PTP sont nommees PAR RESEAU (mxl-ptp4l-net1, mxl-phc2sys-net1) depuis le
    # multi-NIC ; interroger le nom NU (mxl-ptp4l) demande a systemd des nouvelles dune unite
    # qui nexiste pas, et repond « inactive » quel que soit letat reel. Cest exactement ce qui
    # sest passe en prod sur Horace (2026-07-28) : ptp4l verrouille depuis quatre jours, sonde
    # aveugle, page qui conclut « aucune source de temps ». On enumere donc par GLOB.
    try:
        r = subprocess.run(["systemctl","list-units",motif,"--state=active",
                            "--no-legend","--plain","--type=service"],
                           capture_output=True, text=True, timeout=6).stdout
        return [l.split()[0] for l in r.splitlines() if l.strip()]
    except Exception:
        return []
src, det = "aucune", ""
ptp_unites = unites_actives("mxl-ptp4l*") + [u for u in ("ptp4l.service",) if actif(u)]
phc_unites = unites_actives("mxl-phc2sys*") + [u for u in ("phc2sys.service",) if actif(u)]
if actif("chrony") or actif("chronyd"):
    src = "chrony"
    try:
        det = subprocess.run(["chronyc","-n","tracking"], capture_output=True,
                             text=True, timeout=4).stdout
    except Exception:
        det = ""
elif actif("systemd-timesyncd"):
    src = "timesyncd"
elif ptp_unites:
    src = "ptp4l"
leap = ""
try:
    import glob
    for f in ["/etc/chrony/chrony.conf"] + sorted(glob.glob("/etc/chrony/conf.d/*.conf")):
        for ligne in open(f):
            if ligne.strip().startswith(("leapsectz","leapseclist")):
                leap = ligne.strip(); break
        if leap: break
except Exception:
    pass
offset_s = None
ref = ""
strate = None
maj = ""
for ligne in (det or "").splitlines():
    if ligne.startswith("Last offset"):
        try: offset_s = float(ligne.split(":",1)[1].strip().split()[0])
        except Exception: pass
    elif ligne.startswith("Reference ID"):
        # « B92D7079 (185.45.112.121) » -> on garde IP ou nom, pas identifiant hexa.
        # (aucune apostrophe ici : ce programme est enveloppe dans des quotes simples cote shell)
        v = ligne.split(":",1)[1].strip()
        ref = v.split("(")[-1].rstrip(")").strip() if "(" in v else v
    elif ligne.startswith("Stratum"):
        try: strate = int(ligne.split(":",1)[1].strip())
        except Exception: pass
    elif ligne.startswith("Ref time"):
        # Instant du DERNIER echange retenu avec la source. Une horloge peut paraitre saine sans
        # avoir rien recu depuis des heures : ce champ est le seul a le dire.
        maj = ligne.split(":",1)[1].strip()
if src == "timesyncd":
    try:
        ts = subprocess.run(["timedatectl","timesync-status"], capture_output=True, text=True, timeout=4).stdout
        for ligne in ts.splitlines():
            if ligne.strip().startswith("Server:"):
                ref = ligne.split(":",1)[1].strip()
            elif ligne.strip().startswith("Stratum:"):
                try: strate = int(ligne.split(":",1)[1].strip())
                except Exception: pass
            elif ligne.strip().startswith("Offset:") and not offset_s:
                v = ligne.split(":",1)[1].strip()
                try:
                    offset_s = float(v.rstrip("ms")) / 1000.0 if v.endswith("ms") else float(v.rstrip("s"))
                except Exception: pass
    except Exception:
        pass
print(json.dumps({"realtime_ns": _rt, "tai_ns": _tai, "monotonic_ns": _mono,
                  "tai_offset": tx.tai, "status": tx.status, "maxerror_us": tx.maxerror,
                  "source": src, "leap": leap, "offset_s": offset_s,
                  "ref": ref, "strate": strate, "maj": maj,
                  "ptp_unites": ptp_unites, "phc2sys_unites": phc_unites}))
' 2>/dev/null || true"""


# ─── Source NTP commune ────────────────────────────────────────────────────────────────────────
# Une source COMMUNE rend les nœuds comparables : deux serveurs différents, ce sont deux idées du
# temps qui divergent de leur propre écart, et cet écart se retrouve entre les nœuds sans qu on
# sache d ou il vient.
#
# Test SNTP en clair (48 octets, mode 3) plutot que de faire confiance a chrony pour le dire APRES
# coup : on veut savoir si le serveur repond AVANT de le poser sur toute la flotte, et depuis
# CHAQUE noeud — un pare-feu ou une route peuvent differer d une machine a l autre.
# (Programme execute a distance : AUCUNE apostrophe, cf. le garde-fou de _SONDE.)
_TEST_NTP = r"""python3 -c '
import socket, struct, sys, time
h = sys.argv[1]
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    t0 = time.time()
    s.sendto(b"\x1b" + 47 * b"\0", (h, 123))
    d, _ = s.recvfrom(1024)
    t3 = time.time()
    li = d[0] >> 6
    strate = d[1]
    # Horodatage de transmission : secondes depuis 1900 -> epoch Unix.
    tx = struct.unpack("!I", d[40:44])[0] - 2208988800
    frac = struct.unpack("!I", d[44:48])[0] / 2**32
    # Un serveur qui REPOND n est pas un serveur UTILISABLE. Deux refus explicites du protocole :
    #   LI = 3        -> alarme : le serveur declare lui-meme ne PAS etre synchronise ;
    #   strate 0      -> non specifiee, ou Kiss-o-Death (le serveur demande qu on cesse).
    # Les ignorer donnait un feu vert mensonger : chrony, lui, ecarte ces sources, et on cherchait
    # ensuite pourquoi « rien ne change » alors que le serveur repondait parfaitement.
    if li == 3:
        print("KO le serveur se declare NON SYNCHRONISE (LI=3), strate annoncee %d" % strate)
    elif strate == 0:
        print("KO strate 0 (non specifiee ou Kiss-o-Death)")
    else:
        print("OK %d %.6f %.1f %d" % (strate, (tx + frac) - (t0 + t3) / 2, (t3 - t0) * 1000, li))
except Exception as e:
    print("KO %s" % str(e)[:60])
' """


def tester_ntp(serveurs, node=None):
    """Interroge chaque serveur en SNTP, depuis `node` (ou le contrôleur si None).
    Renvoie [{serveur, ok, strate, offset_s, rtt_ms, erreur}]."""
    import shlex as _sh
    import subprocess as _sp
    out = []
    for srv in [x.strip() for x in (serveurs or "").replace(",", " ").split() if x.strip()]:
        cmd = _TEST_NTP + _sh.quote(srv)
        try:
            if node is None:
                r = _sp.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=12)
                txt = (r.stdout or "").strip()
            else:
                _, txt, _ = node_driver.host_exec(node, cmd, timeout=15)
                txt = (txt or "").strip()
        except Exception as e:
            out.append({"serveur": srv, "ok": False, "erreur": str(e)[:80]}); continue
        ch = txt.split()
        if ch[:1] == ["OK"] and len(ch) >= 5:
            out.append({"serveur": srv, "ok": True, "strate": int(ch[1]),
                        "offset_s": float(ch[2]), "rtt_ms": float(ch[3]), "li": int(ch[4])})
        else:
            # Le programme distant prefixe ses refus par « KO » : c est un marqueur de protocole
            # entre lui et nous, pas un message pour l exploitant. On le retire.
            motif = (txt or "sans reponse")
            if motif.startswith("KO "):
                motif = motif[3:]
            out.append({"serveur": srv, "ok": False, "erreur": motif[:100]})
    return out


_ETAT_SRC = {"*": ("selectionnee", "sélectionnée"),
             "+": ("combinee", "combinée"),
             "-": ("ecartee", "écartée du calcul"),
             "x": ("faux_ticker", "REJETÉE : faux-ticker"),
             "?": ("injoignable", "injoignable"),
             "~": ("instable", "trop instable")}


def _etat_source(node, serveur):
    """Que fait chrony de la source qu on lui a demandée ? La question que la page ne posait pas.

    chrony peut très bien PRENDRE une source et la REJETER ensuite : le marqueur `x` (faux-ticker)
    signale une source qui désaccorde avec la majorité des autres. Vécu : un serveur local strate 1
    à 101 ms des serveurs publics, écarté en silence — l exploitant voyait « rien n a changé »
    alors que tout avait fonctionné, sauf le verdict."""
    if not serveur:
        return None
    try:
        rc, out, _ = node_driver.host_exec(
            node, "chronyc -n sources 2>/dev/null | tail -n +3", timeout=10)
    except Exception:
        return None
    for ligne in (out or "").splitlines():
        ch = ligne.split()
        if len(ch) >= 7 and ch[1] == serveur:
            code, libelle = _ETAT_SRC.get(ligne[1:2], ("inconnu", ligne[:2]))
            # « ^x x.x.x.x 1 6 37 47 +101ms[ +101ms] +/- 10ms » : le dernier echantillon est en
            # 7e colonne (index 6), suffixe [ a retirer. Les colonnes suivantes sont la marge.
            ecart = ch[6].rstrip("[") if len(ch) > 6 else ""
            return {"serveur": serveur, "code": code, "libelle": libelle, "dernier_ecart": ecart}
    return {"serveur": serveur, "code": "absente", "libelle": "pas dans la liste de chrony"}


def appliquer_ntp(serveurs):
    """Pose la source NTP commune sur tous les nœuds qui tiennent leur heure de chrony.

    Ni le nœud 2110 (son heure vient du grandmaster via libmtl — lui donner des serveurs NTP
    n'aurait aucun effet et brouillerait la lecture), ni le contrôleur (il n'est pas un nœud du
    cluster ; sa configuration relève de l'hôte). Vérifie APRÈS coup que chaque nœud a bien
    sélectionné une source, au lieu de supposer que l'écriture a suffi."""
    from .database import db_get_nodes
    from . import settings as _s
    _s.set("ntp_servers", serveurs or "")
    lignes = "\n".join("server %s iburst" % x.strip()
                        for x in (serveurs or "").replace(",", " ").split() if x.strip())
    res = []
    for node in db_get_nodes():
        if _a_un_moteur_2110(node):
            res.append({"node": node.get("name"), "ok": None, "msg": "nœud 2110 : heure du grandmaster, non concerné"})
            continue
        cmd = ("mkdir -p /etc/chrony/sources.d && printf '%s\n' > /etc/chrony/sources.d/bobi.sources "
               "&& (chronyc reload sources >/dev/null 2>&1 || systemctl restart chrony) "
               "&& sleep 2 && chronyc -n sources 2>/dev/null | tail -n +3 | wc -l || true") % lignes
        try:
            rc, out, err = node_driver.host_exec(node, cmd, timeout=60)
            nb = int((out or "0").strip().splitlines()[-1] or 0)
            res.append({"node": node.get("name"), "ok": nb > 0,
                        "msg": ("%d source(s) prise(s) en compte" % nb) if nb else
                               "aucune source retenue — vérifier la joignabilité depuis ce nœud"})
        except Exception as e:
            res.append({"node": node.get("name"), "ok": False, "msg": str(e)[:100]})
    # L orchestrateur n est pas un nœud, mais c est une horloge du site — et surtout celle par
    # laquelle passent toutes les mesures. Le laisser sur une autre source, c est garder l écart
    # qu on cherche justement à supprimer. Il tourne sous timesyncd : drop-in, pas de chrony.
    import subprocess as _sp
    try:
        if lignes:
            # chrony si présent (c est ce que pose install.sh), sinon repli timesyncd pour ne pas
            # laisser un contrôleur ancien sans configuration. Les deux écrivent les MÊMES serveurs.
            a_chrony = _sp.run(["/bin/sh", "-c", "command -v chronyd >/dev/null && echo oui"],
                               capture_output=True, text=True, timeout=10).stdout.strip() == "oui"
            if a_chrony:
                cmd = ("mkdir -p /etc/chrony/sources.d && printf '%s\\n' > /etc/chrony/sources.d/bobi.sources "
                       "&& (chronyc reload sources >/dev/null 2>&1 || systemctl restart chrony) "
                       "&& sleep 2 && chronyc -n sources 2>/dev/null | tail -n +3 | wc -l") % lignes
            else:
                liste = " ".join(x.strip() for x in (serveurs or "").replace(",", " ").split() if x.strip())
                cmd = ("mkdir -p /etc/systemd/timesyncd.conf.d && "
                       "printf '[Time]\\nNTP=%s\\n' > /etc/systemd/timesyncd.conf.d/bobi.conf && "
                       "systemctl restart systemd-timesyncd && echo 1") % liste
            r = _sp.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=60)
            nb = int((r.stdout or "0").strip().splitlines()[-1] or 0)
            res.append({"node": "orchestrateur", "ok": nb > 0,
                        "msg": ("%s : %d source(s) prise(s) en compte" % ("chrony" if a_chrony else "timesyncd", nb))
                               if nb else "aucune source retenue"})
    except Exception as e:
        res.append({"node": "orchestrateur", "ok": False, "msg": str(e)[:100]})
    with _lock:
        _cache["data"] = None
    return res


# Qui discipline l'horloge d'un nœud — caché 5 min (ça ne change qu'au redéploiement du moteur).
_disc_cache = {}
_DISC_TTL_S = 300.0


def _discipline(node):
    """QUI tient l'horloge de ce nœud, et par quel moyen.

    ⚠ PIÈGE : sur un nœud full-PF DPDK, TOUTES les sondes hôte disent « aucune horloge » —
    `systemctl` ne montre aucun service, `ps` aucun processus, `timedatectl` annonce
    « System clock synchronized: no » et le noyau garde STA_UNSYNC armé. C'est FAUX. Le discipliné
    est libmtl, DANS le conteneur moteur : `ENGINE_PTP=libmtl` en fait un esclave PTPv2 sur le port
    DPDK, et `ENGINE_PHC2SYS=1` lui fait discipliner CLOCK_REALTIME depuis le PHC, en remplacement
    de phc2sys noyau. Rien de tout ça n'est visible côté hôte, et un programme qui pose l'heure ne
    se déclare pas comme démon de discipline — d'où le drapeau trompeur.

    On va donc lire l'ENV DU CONTENEUR plutôt que de conclure depuis l'hôte."""
    nid = node.get("id")
    ent = _disc_cache.get(nid)
    if ent and (time.time() - ent[0]) < _DISC_TTL_S:
        return ent[1]
    d = {"par": None, "detail": "", "phc2sys": None}
    try:
        if _a_un_moteur_2110(node):
            rc, out, _ = node_driver.host_exec(
                node, "docker ps --format '{{.Names}}' | grep -m1 '^bobi-mtl-' "
                      "| xargs -r -I{} docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' {} "
                      "| grep -E '^ENGINE_(PTP|PHC2SYS)=' || true", timeout=15)
            env = dict(l.split("=", 1) for l in (out or "").split() if "=" in l)
            interne = env.get("ENGINE_PTP") == "libmtl"
            phc = env.get("ENGINE_PHC2SYS", "0") not in ("0", "", None)
            d["phc2sys"] = phc
            if interne and phc:
                d["par"], d["detail"] = "moteur 2110", "libmtl : esclave PTPv2 sur le port DPDK, et discipline CLOCK_REALTIME depuis le PHC"
            elif interne:
                d["par"], d["detail"] = "moteur 2110 (partiel)", ("libmtl est esclave PTPv2 mais NE discipline PAS l'horloge système "
                                                                 "(ENGINE_PHC2SYS absent) — elle court libre")
            else:
                d["par"], d["detail"] = "aucun", "moteur présent mais son client PTP interne est désactivé (ENGINE_PTP)"
    except Exception as e:
        log.debug("clocks discipline nœud %s: %s", nid, e)
    _disc_cache[nid] = (time.time(), d)
    return d


def _derive(pts):
    """Dérive de l'écart (ms/h) et répétabilité (ms) sur la fenêtre d'historique.

    La dérive reste plus robuste que la position, et pour une raison qui n'a pas changé avec
    l'endpoint natif : tout biais résiduel du chemin de mesure est à peu près CONSTANT, donc il
    s'annule dans une DIFFÉRENCE. C'est ce qui rendait cette colonne exploitable à l'époque où la
    position, elle, portait ±25 ms de biais d'asymétrie (cf. `_echange_clock`) — la dérive voyait
    partir une horloge que la position ne savait pas situer.

    Renvoie (None, None) tant que la fenêtre est trop courte pour conclure."""
    # On ne régresse QUE sur des points mesurés avec la même règle graduée (cf. `_pousser_hist`) :
    # celle du point le plus récent. Mélanger deux méthodes ne donnerait pas une dérive bruitée,
    # mais une dérive FAUSSE — la marche entre les deux biais dominerait la pente.
    courante = next((p.get("m") for p in reversed(pts) if p.get("ecart_ms") is not None), None)
    v = [(p["t"], p["ecart_ms"]) for p in pts
         if p.get("ecart_ms") is not None and p.get("m") == courante]
    if len(v) < 10 or (v[-1][0] - v[0][0]) < 600:      # < 10 points ou < 10 min : on ne conclut pas
        return None, None
    n = len(v)
    tm = sum(t for t, _ in v) / n
    em = sum(e for _, e in v) / n
    denom = sum((t - tm) ** 2 for t, _ in v)
    if denom <= 0:
        return None, None
    pente = sum((t - tm) * (e - em) for t, e in v) / denom          # ms par seconde
    resid = [e - (em + pente * (t - tm)) for t, e in v]
    ecart_type = (sum(r * r for r in resid) / n) ** 0.5
    return round(pente * 3600.0, 2), round(ecart_type, 2)


def _a_un_moteur_2110(node):
    """Un moteur 2110 est-il déclaré sur ce nœud ? Lecture DB seule (pas d'appel distant)."""
    try:
        import json as _j
        from .database import db_get_containers
        for c in db_get_containers():
            if int(c.get("node_id") or 0) != int(node.get("id") or -1):
                continue
            if (_j.loads(c.get("deploy_config") or "{}") or {}).get("type") == "2110_io":
                return True
    except Exception:
        pass
    return False


_APOS = chr(39)
if _SONDE.count(_APOS) != 2:
    raise RuntimeError(
        "clocks._SONDE contient %d apostrophes au lieu des 2 delimiteurs : le programme est "
        "enveloppe dans des quotes SIMPLES cote shell, une apostrophe de plus (y compris dans un "
        "commentaire francais) termine la chaine et casse la sonde EN SILENCE — elle repond alors "
        "« noeud injoignable ». Ecrire les commentaires de la sonde sans apostrophe."
        % _SONDE.count(_APOS))


# ─── Mesure de précision : /v1/host/clock, modèle NTP à 4 estampilles ─────────────────────────
# La sonde shell ci-dessus reste utile pour le CONTEXTE (qui discipline, quelle référence, quelle
# strate). Elle est en revanche incapable de dater un nœud à mieux que la dizaine de millisecondes,
# et pas pour une raison de réseau : le lien est à 0,3 ms. Le retard est logiciel et, surtout,
# ASYMÉTRIQUE — `host/exec` monte la connexion, lance un `sh -c`, démarre un interpréteur Python,
# et tout cela est sur l'ALLER. Dater le nœud au milieu de l'aller-retour revient alors à le
# déclarer systématiquement EN AVANCE de ~25 ms. Les 20 à 30 ms qu'affichait cette page étaient
# cela, et rien d'autre : les horloges, elles, étaient d'accord à quelques dizaines de µs.
#
# On mesure donc comme NTP mesure, avec quatre estampilles :
#     t0 contrôleur émet · t1 nœud reçoit · t2 nœud répond · t3 contrôleur reçoit
#     offset = ((t1 - t0) + (t2 - t3)) / 2        délai = (t3 - t0) - (t2 - t1)
# Le temps passé DANS le nœud (t2 - t1) sort ainsi de l'équation : seul le trajet reste, et lui est
# symétrique. Reste l'hypothèse de NTP — aller et retour de même durée — qui borne l'erreur à la
# demi-asymétrie résiduelle, d'où `incertitude_ms = délai / 2`.
#
# ⚠ Le chronomètre ne démarre qu'APRÈS l'établissement de la connexion (TCP + poignée de main TLS).
# Cet établissement coûte plusieurs millisecondes et tombe entièrement sur l'aller : le laisser
# dans la fenêtre réintroduirait, à l'identique, le biais qu'on vient de retirer.
def _echange_clock(node, timeout=5.0):
    """Un aller-retour horaire avec l'agent-nœud. Retourne
    `(offset_utc_ns, offset_tai_ns, tai_offset_s, delai_ns)` ou lève."""
    base = node_driver._agent_base(node)
    u = urlparse(base)
    if not u.hostname:
        raise RuntimeError("nœud sans agent_url")
    port = u.port or 9100
    req = ("GET /v1/host/clock HTTP/1.0\r\nHost: %s\r\n%s: %s\r\nConnection: close\r\n\r\n"
           % (u.hostname, node_driver.TOKEN_HEADER, node.get("agent_token") or "")).encode()
    s = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        # Nagle retiendrait la requête ; et c'est la fenêtre de mesure elle-même qu'il retarderait.
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if u.scheme == "https":
            from . import ca
            s = ca.controller_client_context().wrap_socket(s, server_hostname=u.hostname)
        s.settimeout(timeout)
        # ── Fenêtre de mesure : tout ce qui précède (connexion, TLS) en est DEHORS ──────────────
        t0 = time.clock_gettime_ns(time.CLOCK_REALTIME)
        s.sendall(req)
        brut = b""
        while True:
            bloc = s.recv(4096)
            if not bloc:
                break
            brut += bloc
            if b"\r\n\r\n" in brut and brut.split(b"\r\n\r\n", 1)[1].endswith(b"}"):
                break
        t3 = time.clock_gettime_ns(time.CLOCK_REALTIME)
        # ───────────────────────────────────────────────────────────────────────────────────────
    finally:
        try:
            s.close()
        except Exception:
            pass
    tete, _, corps = brut.partition(b"\r\n\r\n")
    if b" 200 " not in tete.split(b"\r\n")[0]:
        raise RuntimeError(tete.split(b"\r\n")[0].decode("utf-8", "replace") or "réponse illisible")
    d = json.loads(corps.decode())
    r_utc, r_tai = int(d["recv_utc_ns"]), int(d["recv_tai_ns"])
    e_utc, e_tai = int(d["send_utc_ns"]), int(d["send_tai_ns"])
    delai = (t3 - t0) - (e_utc - r_utc)
    return (((r_utc - t0) + (e_utc - t3)) // 2,
            ((r_tai - t0) + (e_tai - t3)) // 2,
            round((e_tai - e_utc) / 1e9),
            delai)


def _mesurer_clock(node, essais=9):
    """Garde l'échange au DÉLAI le plus court sur `essais` — la méthode NTP, et pour sa raison :
    l'échantillon le plus rapide est celui qui a le moins de place pour être asymétrique, donc
    celui dont l'erreur est le mieux bornée. Une moyenne ferait l'inverse (elle intègre les
    échanges retardés). Retourne None si l'agent ne connaît pas l'endpoint (< 0.18.0) ou échoue —
    l'appelant retombe alors sur la sonde shell, avec son incertitude annoncée."""
    meilleur, erreur = None, None
    for _ in range(max(1, int(essais))):
        try:
            o_utc, o_tai, tai_off, delai = _echange_clock(node)
        except Exception as e:
            erreur = e
            break                     # inutile d'insister : c'est l'endpoint ou le lien, pas l'aléa
        if meilleur is None or delai < meilleur["delai_ns"]:
            meilleur = {"offset_utc_ns": o_utc, "offset_tai_ns": o_tai,
                        "tai_offset": tai_off, "delai_ns": delai}
    if meilleur is None:
        log.debug("clocks : /v1/host/clock indisponible sur %s (%s)", node.get("name"), erreur)
    return meilleur


def _sonder(node, essais=9):
    """Relève l'état d'horloge d'UN nœud : le CONTEXTE une fois, la MESURE `essais` fois.

    Deux relevés de nature différente, et il faut les distinguer pour lire la page :

    · le **contexte** (qui discipline l'horloge, quelle référence, quelle strate, quel offset
      rapporté localement) vient de la sonde shell. Il est cher (~50 ms) mais ne demande aucune
      précision temporelle : un seul passage suffit ;
    · la **position** du nœud sur la grille vient de `/v1/host/clock` (agent ≥ 0.18.0), en modèle
      NTP à quatre estampilles. Elle coûte ~1 ms, donc on la répète et on garde le meilleur.

    Ce que ça change : l'incertitude passe de ±25 ms à quelques centaines de µs. La page n'est plus
    condamnée à ne détecter que les erreurs GROSSIÈRES (`tai_offset` oublié, aucune source) — elle
    peut désormais juger l'accord SOUS-GRAIN, qui est la question qu'on lui posait depuis le début.

    Agent trop ancien ou endpoint injoignable → repli sur la sonde shell, best-of-N comme avant,
    avec son incertitude annoncée. C'est `mesure` qui dit laquelle des deux a parlé."""
    e = _sonder_une_fois(node)
    if not e.get("joignable"):
        return e
    m = _mesurer_clock(node, essais)
    if m:
        e["realtime_vs_utc_ns"] = m["offset_utc_ns"]
        e["tai_vs_utc_ns"] = m["offset_tai_ns"]
        e["tai_offset"] = m["tai_offset"]      # CLOCK_TAI − CLOCK_REALTIME : la définition même
        e["incertitude_ms"] = round(m["delai_ns"] / 2e6, 3)
        e["mesure"] = "agent_natif"
        _poser_verdicts(e)
        return e
    # Repli : on rejoue la sonde shell pour garder le relevé au RTT le plus court, comme avant.
    for _ in range(4):
        e2 = _sonder_une_fois(node)
        if e2.get("joignable") and e2.get("incertitude_ms", 1e9) < e.get("incertitude_ms", 1e9):
            e = e2
    return e


def _sonder_une_fois(node):
    t0 = time.time()
    try:
        rc, out, _ = node_driver.host_exec(node, _SONDE, timeout=15)
    except Exception as e:
        return {"id": node.get("id"), "name": node.get("name"), "joignable": False, "erreur": str(e)[:120]}
    t1 = time.time()
    txt = (out or "").strip()
    if rc != 0 or not txt.startswith("{"):
        return {"id": node.get("id"), "name": node.get("name"), "joignable": False,
                "erreur": (txt or "sonde sans réponse")[:120]}
    try:
        d = json.loads(txt.splitlines()[-1])
    except Exception as e:
        return {"id": node.get("id"), "name": node.get("name"), "joignable": False, "erreur": str(e)[:120]}

    utc_ctrl_ns = int(((t0 + t1) / 2.0) * 1e9)     # milieu de la fenêtre = meilleure estimation
    tai_ns = d.get("tai_ns")
    rt_ns = d.get("realtime_ns")
    e = {
        "id": node.get("id"), "name": node.get("name"), "joignable": True,
        "realtime_ns": rt_ns, "tai_ns": tai_ns,
        "tai_offset": d.get("tai_offset"), "source": d.get("source"),
        "leap": d.get("leap") or "", "offset_s": d.get("offset_s"),
        "ref": d.get("ref") or "", "strate": d.get("strate"), "maj": d.get("maj") or "",
        "maxerror_us": d.get("maxerror_us"),
        # Unités RÉELLEMENT actives (nommées par réseau) — sert au diagnostic et, surtout, à
        # savoir si un servo tient déjà CLOCK_REALTIME avant de proposer d'en poser un second.
        "ptp_unites": d.get("ptp_unites") or [],
        "phc2sys_unites": d.get("phc2sys_unites") or [],
        # ± DEMI aller-retour : la lecture a eu lieu quelque part dans [t0, t1], on l'estime au
        # milieu, donc l'erreur est bornée par la demi-fenêtre — pas par la fenêtre entière.
        "incertitude_ms": round((t1 - t0) * 500.0, 1),
        # Convention détectée : REALTIME porte-t-il du TAI (nœud 2110 discipliné par libmtl) ou de
        # l'UTC (nœud NTP) ? Déduit de l'écart REALTIME↔UTC du contrôleur, arrondi à la seconde.
        "convention": None,
    }
    if e["source"] == "aucune" and _a_un_moteur_2110(node):
        # Pas « aucune » : sur un nœud 2110 en DPDK, il n'y a ni ptp4l ni chrony parce que l'horloge
        # système est disciplinée par le client PTP interne de libmtl, DANS le moteur. Annoncer
        # « aucune source » là où il y en a une, et une bonne, c'est envoyer chercher un problème
        # qui n'existe pas.
        e["source"] = "moteur 2110"
    # Grandeurs PRIMITIVES : les écarts bruts nœud↔contrôleur. Tous les verdicts en découlent, et
    # eux seuls changent selon la méthode de mesure — d'où la séparation avec `_poser_verdicts`,
    # qu'on rejoue tel quel après une mesure fine.
    e["realtime_vs_utc_ns"] = (rt_ns - utc_ctrl_ns) if rt_ns else None
    e["tai_vs_utc_ns"] = (tai_ns - utc_ctrl_ns) if tai_ns else None
    e["mesure"] = "sonde_shell"
    _poser_verdicts(e)
    return e


def _poser_verdicts(e):
    """Dérive les verdicts d'horloge des écarts bruts (`realtime_vs_utc_ns` / `tai_vs_utc_ns`).

    Séparé de la mesure exprès : la sonde shell et l'endpoint natif produisent les mêmes deux
    grandeurs avec des précisions différentes, et rien d'autre ne doit changer entre les deux."""
    rt_delta = e.get("realtime_vs_utc_ns")
    tai_delta = e.get("tai_vs_utc_ns")
    # Convention détectée : REALTIME porte-t-il du TAI (nœud 2110 discipliné par libmtl) ou de
    # l'UTC (nœud NTP) ? Déduit de l'écart REALTIME↔UTC du contrôleur, arrondi à la seconde.
    e["convention"] = None
    if rt_delta is not None:
        delta_s = round(rt_delta / 1e9)
        e["convention"] = "realtime_tai" if abs(delta_s - TAI_UTC_OFFSET_S) <= 1 else (
            "realtime_utc" if abs(delta_s) <= 1 else "inconnue")
        e["realtime_vs_utc_s"] = delta_s
    # Le verdict par nœud ne porte QUE sur CLOCK_TAI — le reste est du contexte pour comprendre.
    #
    # L'écart est gardé en MILLISECONDES depuis les nanosecondes brutes, JAMAIS reconstruit depuis
    # une valeur arrondie à la seconde : la question posée est « ces nœuds tombent-ils sur le même
    # grain » (20 ms à 50 fps), donc un écart arrondi à la seconde ne répondrait à rien — il ne
    # pourrait valoir que 0 ou ±1000 et laisserait passer exactement les dérives qu'on traque.
    if tai_delta is not None:
        e["tai_vs_utc_ms"] = round(tai_delta / 1e6, 3)
        # Écart à la grille de référence (UTC contrôleur + 37 s) : c'est ça, « être à l'heure ».
        e["ecart_ms"] = round(e["tai_vs_utc_ms"] - TAI_UTC_OFFSET_S * 1000.0, 3)
        # Le verdict « juste » tolère la seconde : le contrôleur n'est pas une référence de
        # précision, et un décalage qui compte se voit en secondes entières (tai_offset oublié).
        e["tai_juste"] = abs(e["ecart_ms"]) < 1000.0
    else:
        e["tai_juste"] = None
    # Offset noyau ATTENDU pour CE nœud : 0 là où REALTIME porte déjà du TAI, 37 là où il porte
    # l'UTC. Sans cette valeur à côté, un « 0 » et un « 37 » dans la même colonne se lisent comme
    # une incohérence alors que les deux sont corrects, chacun chez soi.
    e["tai_offset_attendu"] = 0 if e.get("convention") == "realtime_tai" else TAI_UTC_OFFSET_S
    e["tai_offset_ok"] = (e.get("tai_offset") == e["tai_offset_attendu"])
    # (6) Y a-t-il quelque chose à corriger ? L'action ne doit être proposée que dans ce cas.
    #
    # …et SEULEMENT si l'action proposée est la bonne. Le bouton n'a qu'un geste : installer
    # chrony. Sur un nœud où un servo tient déjà CLOCK_REALTIME — phc2sys depuis le PHC, ou le
    # client PTP interne de libmtl — le proposer revient à offrir un SECOND maître pour la même
    # horloge. Deux servos sur un même CLOCK_REALTIME, c'est la panne PROD-009, et sur un nœud
    # qui cadence du 2110 elle se paie en pacing.
    #
    # Le garde-fou de `appliquer_ntp_tai` ne couvrait que la convention `realtime_tai`. Il ratait
    # donc le cas d'Horace (2026-07-28) : phc2sys verrouillé à ±50 ns, REALTIME portant de l'UTC,
    # nœud hors grille pour une tout autre raison — le bouton était proposé et serait passé.
    e["servo_present"] = bool(e.get("phc2sys_unites") or e.get("convention") == "realtime_tai")
    e["a_corriger"] = bool((e.get("tai_juste") is False or not e["tai_offset_ok"]
                            or e.get("source") == "aucune")
                           and not e["servo_present"])
    if e["servo_present"] and (e.get("tai_juste") is False or not e["tai_offset_ok"]):
        # Rien à proposer ne veut pas dire rien à signaler : on nomme ce qu'on voit, et on laisse
        # le diagnostic à l'humain plutôt que de tendre un bouton qui aggraverait.
        e["a_diagnostiquer"] = ("un servo discipline déjà cette horloge (%s) — le décalage vient de "
                                "sa RÉFÉRENCE, pas du nœud : vérifier le grandmaster PTP"
                                % (", ".join(e.get("phc2sys_unites") or []) or "client PTP du moteur"))
    return e


def _sonder_controleur():
    """Le contrôleur EST une horloge du cluster : il mérite sa ligne. Sonde LOCALE (pas d'agent,
    donc pas d'aller-retour) — ses champs propres sont exacts. Son écart, lui, est nul par
    construction tant qu'il sert de repère de mesure : ce qui l'intéresse, c'est sa SOURCE."""
    import subprocess as _sp
    e = {"id": None, "name": "orchestrateur", "joignable": True, "controleur": True}
    try:
        r = _sp.run(["/bin/sh", "-c", _SONDE], capture_output=True, text=True, timeout=15)
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as ex:
        return {**e, "joignable": False, "erreur": str(ex)[:120]}
    rt, tai = d.get("realtime_ns"), d.get("tai_ns")
    e.update({"realtime_ns": rt, "tai_ns": tai, "tai_offset": d.get("tai_offset"),
              "source": d.get("source"), "ref": d.get("ref") or "", "strate": d.get("strate"),
              "maj": d.get("maj") or "",
              "leap": d.get("leap") or "", "offset_s": d.get("offset_s"), "offset_src": d.get("source"),
              "incertitude_ms": 0.0, "hist": [], "derive_ms_h": None, "repetabilite_ms": None,
              "discipline": {"par": d.get("source"), "detail": "horloge locale du contrôleur", "phc2sys": None}})
    if rt and tai:
        delta = round((tai - rt) / 1e9)
        e["convention"] = "realtime_tai" if abs(delta) < 1 and False else "realtime_utc"
        e["tai_offset_attendu"] = TAI_UTC_OFFSET_S
        e["tai_offset_ok"] = (e["tai_offset"] == TAI_UTC_OFFSET_S)
        e["ecart_ms"] = 0.0                    # repère de mesure : nul par construction
        e["tai_juste"] = e["tai_offset_ok"]
    e["a_corriger"] = not e.get("tai_offset_ok", False) or e.get("source") == "aucune"
    return e


# Traçabilité d'une source, du plus au moins solide. Sert à choisir le RÉFÉRENT du cluster : c'est
# le nœud le mieux accroché qui fait foi, pas le contrôleur — une machine NTP ordinaire n'a aucune
# raison d'arbitrer des horloges verrouillées sur un grandmaster.
_RANG_SOURCE = {"moteur 2110": 3, "ptp4l": 3, "chrony": 2, "timesyncd": 1, "aucune": 0}


def etat(force=False):
    """État d'horloge de TOUS les nœuds + verdict de cohérence du cluster. Caché `_TTL_S`.

    `ecart_max_ms` = plus grand écart entre les `CLOCK_TAI` des nœuds joignables, ramené au même
    instant via l'horloge du contrôleur. C'est LE chiffre qui dit si le cluster peut travailler
    ensemble : au-delà d'un grain, deux nœuds ne désignent plus la même image."""
    with _lock:
        if not force and _cache["data"] and (time.time() - _cache["ts"]) < _TTL_S:
            return _cache["data"]
    noeuds = [_sonder(n) for n in db_get_nodes()]
    # Indicateur de précision LOCAL au nœud, donc sans RTT — la seule mesure fine crédible ici :
    #   · nœud NTP    → offset rapporté par chrony (relevé par la sonde) ;
    #   · nœud 2110   → offset du client PTP interne du moteur, déjà échantillonné par node_health.
    # Absent tant que le sampler n'a pas tourné : on laisse vide plutôt que d'inventer.
    try:
        from . import node_health
        snaps = (node_health.latest() or {}).get("nodes") or {}
        for n in noeuds:
            snap = snaps.get(str(n.get("id"))) or snaps.get(n.get("id")) or {}
            ptp = snap.get("ptp") or {}
            off = ptp.get("offset_ns")
            if off is not None and n.get("offset_s") is None:
                n["offset_s"] = float(off) / 1e9
                n["offset_src"] = "ptp_moteur"
                # La SOURCE d'un nœud 2110, c'est le grandmaster PTP — pas « le moteur », qui n'est
                # que l'outil, exactement comme chrony n'est pas un serveur NTP.
                if ptp.get("gm_id") and not n.get("ref"):
                    n["ref"] = "GM %s" % ptp["gm_id"]
                # ── Qualité de la RÉFÉRENCE, distincte de la qualité du verrou ────────────────
                # Un esclave se verrouille à la nanoseconde sur un grandmaster en roue libre
                # exactement comme sur du GPS. Sans ce report, `offset_s = 121 ns` se lit comme
                # « horloge excellente » alors que le nœud peut être à des minutes de l'UTC —
                # mesuré sur Horace le 2026-07-28 (clockClass 248, 16,2 min d'écart).
                n["gm_clock_class"] = ptp.get("gm_clock_class")
                n["utc_offset_valid"] = ptp.get("utc_offset_valid")
                # Le verdict est DÉRIVÉ ici des champs bruts, jamais repris tel quel du producteur.
                # Deux producteurs alimentent ce bloc — le relevé pmc de l'orchestrateur et
                # l'agent-nœud — et seul le premier calcule `gm_saine`. Le lire au lieu de le
                # dériver rendait l'alarme muette sur l'autre chemin, en silence.
                from .ptp import gm_reference_saine
                n["gm_saine"], n["gm_raison"] = gm_reference_saine(ptp)
                if ptp.get("phc2sys_running"):
                    n["servo_present"] = True
            elif n.get("offset_s") is not None:
                n["offset_src"] = "chrony"
    except Exception as _e:
        log.debug("clocks : offset local indisponible (%s)", _e)
    par_id0 = {int(x.get("id")): x for x in db_get_nodes()}
    for n in noeuds:
        _pousser_hist(n)
        n["hist"] = list(_hist.get(n.get("id")) or [])
        n["discipline"] = _discipline(par_id0.get(n.get("id")) or {})
        if n.get("source") == "chrony":
            srv = (_srv_commun().split() or [None])[0]
            n["ntp_etat"] = _etat_source(par_id0.get(n.get("id")) or {}, srv)
    _flush_hist()
    mesurables = [n for n in noeuds if n.get("joignable") and n.get("ecart_ms") is not None]
    ecarts = [n["ecart_ms"] for n in mesurables]
    ecart_max = round(max(ecarts) - min(ecarts), 3) if len(ecarts) >= 2 else 0.0
    problemes = []
    for n in noeuds:
        if not n.get("joignable"):
            problemes.append("%s : injoignable" % (n.get("name") or n.get("id")))
        elif n.get("tai_juste") is False:
            problemes.append("%s : CLOCK_TAI décalé de %+.1f s (tai_offset=%s, source=%s)"
                             % (n.get("name"), (n.get("ecart_ms") or 0) / 1000.0,
                                n.get("tai_offset"), n.get("source")))
        elif n.get("source") == "aucune" and n.get("convention") != "realtime_tai":
            problemes.append("%s : aucune source de temps active" % n.get("name"))
    flou = max([n.get("incertitude_ms") or 0.0 for n in mesurables] or [0.0])

    # ── Écart inter-nœuds : le seuil ne s'applique QUE si la mesure peut le trancher ────────────
    # La règle est celle de tout le reste de ce module : on ne signale que ce qu'on sait mesurer.
    # Ici elle devient une CONDITION explicite plutôt qu'un renoncement — l'écart n'est jugé que
    # s'il dépasse à la fois le grain ET l'incertitude du relevé. Sur un agent ancien (repli sonde
    # shell, ±25 ms) la seconde condition n'est jamais remplie et l'alarme se tait d'elle-même :
    # pas de cas particulier à écrire, c'est la mesure qui dit si elle a le droit de conclure.
    seuil_ecart, raison_ecart = _seuil_ecart_ms()
    if len(ecarts) >= 2 and ecart_max > seuil_ecart and ecart_max > flou:
        pire = max(mesurables, key=lambda n: n["ecart_ms"])
        moins = min(mesurables, key=lambda n: n["ecart_ms"])
        problemes.append("écart inter-nœuds de %.1f ms entre %s et %s (seuil %.1f ms — %s ; "
                         "mesure ±%.1f ms) : ils ne désignent plus le même grain"
                         % (ecart_max, moins.get("name"), pire.get("name"),
                            seuil_ecart, raison_ecart, flou))

    # ── La RÉFÉRENCE vaut-elle quelque chose ? ─────────────────────────────────────────────────
    # Signalé AVANT les seuils de précision locale, et c'est délibéré : un nœud verrouillé sur un
    # grandmaster en roue libre passe tous les tests de précision avec les meilleures notes du
    # parc. Sa discipline est parfaite ; c'est ce sur quoi il est discipliné qui ne vaut rien.
    # Aucun autre indicateur de cette page n'attrape ce cas — celui-ci existe pour ça.
    for n in noeuds:
        if n.get("joignable") and n.get("gm_saine") is False:
            problemes.append("%s : %s" % (n.get("name"), n.get("gm_raison") or "référence PTP douteuse"))

    # La discipline LOCALE est relevée sur le nœud (offset chrony / verrou PTP du moteur), donc
    # sans aller-retour : elle se juge en microsecondes, indépendamment de tout ça.
    par_id = {int(x.get("id")): x for x in db_get_nodes()}
    for n in noeuds:
        seuil_n, raison = _seuil_local_us(par_id.get(n.get("id")) or {})
        n["seuil_us"], n["seuil_raison"] = round(seuil_n, 2), raison
        o = n.get("offset_s")
        if n.get("joignable") and o is not None and abs(o) * 1e6 > seuil_n:
            problemes.append("%s : horloge locale à %.0f µs de sa référence (seuil %g µs — %s)"
                             % (n.get("name"), abs(o) * 1e6, seuil_n, raison))
    if not _alertes_actives():
        problemes = []          # l'exploitant a coupé le signalement : on mesure toujours, on ne crie plus
    # ── Choix du RÉFÉRENT ─────────────────────────────────────────────────────────────────────
    # Le contrôleur n'a aucune légitimité pour arbitrer : c'est une machine NTP ordinaire, et sa
    # propre dérive se retrouvait à l'identique sur toutes les lignes. On prend le nœud le mieux
    # TRACÉ (PTP verrouillé sur un grandmaster > NTP > rien) et on exprime les écarts par rapport à
    # LUI. Le biais du contrôleur, commun à toutes les mesures, s'annule dans la différence —
    # exactement le même mécanisme que pour la dérive.
    candidats = [n for n in noeuds if n.get("joignable") and n.get("ecart_ms") is not None]
    referent = max(candidats, key=lambda n: (_RANG_SOURCE.get(n.get("source"), 0),
                                             -abs(n.get("offset_s") or 1))) if candidats else None
    # Historique du RÉFÉRENT, indexé par instant : sert à rendre les séries relatives.
    href = {p["t"]: p["ecart_ms"] for p in (referent or {}).get("hist", [])
            if p.get("ecart_ms") is not None}
    for n in noeuds:
        if referent is not None and n.get("ecart_ms") is not None:
            n["ecart_ref_ms"] = round(n["ecart_ms"] - referent["ecart_ms"], 3)
            n["est_referent"] = (n is referent)
        else:
            n["ecart_ref_ms"], n["est_referent"] = None, False
        # ── La DÉRIVE se mesure CONTRE LE RÉFÉRENT, pas contre le contrôleur ──────────────────
        # Sinon elle mesure le mauvais couple : chaque nœud paraissait dériver de ~10 ms/h alors
        # que c était le contrôleur — simple machine NTP — qui bougeait sous eux tous. Un nœud
        # verrouillé sur le grandmaster ne « dérive » pas par rapport à la grille : il EST la
        # grille. Ce qui intéresse ici, c est la vitesse à laquelle un nœud s en éloigne.
        if n["est_referent"]:
            n["derive_ms_h"], n["repetabilite_ms"] = None, None      # référence : 0 par définition
        else:
            rel = [{"t": p["t"], "m": p.get("m"), "ecart_ms": p["ecart_ms"] - href[p["t"]]}
                   for p in (n.get("hist") or [])
                   if p.get("ecart_ms") is not None and p.get("t") in href]
            n["derive_ms_h"], n["repetabilite_ms"] = _derive(rel)
    ctrl = _sonder_controleur()
    if ctrl.get("joignable") and referent is not None:
        # Le contrôleur est mesuré SANS aller-retour, mais le référent l'est AVEC : son écart au
        # référent porte donc l'incertitude de cette mesure-là, pas zéro.
        ctrl["ecart_ref_ms"] = round(-referent["ecart_ms"], 3)
        ctrl["incertitude_ms"] = referent.get("incertitude_ms") or 0.0
        # Le contrôleur n a pas d historique propre (il est le point de mesure) : sa dérive
        # relative au référent est l OPPOSÉE de celle du référent vue depuis lui.
        rel = [{"t": p["t"], "m": p.get("m"), "ecart_ms": -p["ecart_ms"]}
               for p in (referent.get("hist") or []) if p.get("ecart_ms") is not None]
        ctrl["derive_ms_h"], ctrl["repetabilite_ms"] = _derive(rel)
    ctrl["est_referent"] = False
    noeuds.append(ctrl)
    locaux = [abs(n["offset_s"]) * 1e6 for n in noeuds if n.get("offset_s") is not None]
    from . import settings as _st
    try:
        _force = float(_st.get("clock_local_offset_us") or 0)
    except Exception:
        _force = 0
    data = {"nodes": noeuds, "ecart_max_ms": ecart_max, "fps_reference": _fps_reference(),
            "referent": (referent or {}).get("name"),
            "ntp_servers": (lambda: __import__("app.settings", fromlist=["get"]).get("ntp_servers") or "")(),
            "seuil_force": _force,
            "pire_local_us": round(max(locaux), 1) if locaux else None,
            "seuil_ecart_ms": round(seuil_ecart, 1), "seuil_ecart_raison": raison_ecart,
            # Quelle méthode a parlé — l'endpoint natif de l'agent (µs) ou le repli sonde shell
            # (dizaines de ms). Ça se voit dans l'incertitude, mais autant le nommer : sans ça, un
            # nœud à l'agent périmé affiche un écart dix fois plus gros que ses voisins sans que
            # rien ne dise que c'est la RÈGLE qui a changé, pas l'horloge.
            "mesure": ("agent_natif" if all(n.get("mesure") == "agent_natif" for n in mesurables)
                       else "mixte" if any(n.get("mesure") == "agent_natif" for n in mesurables)
                       else "sonde_shell"),
            "incertitude_ms": flou, "alertes": _alertes_actives(),
            "tai_utc_offset_s": TAI_UTC_OFFSET_S, "problemes": problemes,
            "ok": not problemes, "ts": time.time()}
    with _lock:
        _cache["ts"] = time.time(); _cache["data"] = data
    return data


def resume():
    """Agrégat pour le Monitoring : juste « est-ce que tout va bien », sans le détail. Le détail et
    les réglages vivent dans Réglages → Réseau → Horloges."""
    d = etat()
    return {"ok": d["ok"], "ecart_max_ms": d["ecart_max_ms"], "problemes": len(d["problemes"]),
            "detail": d["problemes"][:3]}


def appliquer_ntp_tai(node_id):
    """Met un nœud sur la grille : chrony + `leapseclist` (qui pose et MAINTIENT le `tai_offset`,
    secondes intercalaires comprises), en remplacement de timesyncd qui ne le pose jamais.

    Ne touche PAS un nœud dont `REALTIME` porte du TAI (nœud 2110 discipliné par libmtl) : y
    installer chrony ferait battre deux disciplines sur la même horloge. Renvoie
    (ok, message, clef_i18n, params) : `message` reste la phrase française historique (renvoyée
    telle quelle à l'appelant HTTP) ; `clef_i18n`/`params` portent le MÊME contenu pour l'alerte
    — seule cette dernière est rendue dans la langue du lecteur. None/None quand l'appelant ne
    lève pas d'alerte pour cette branche (échecs, non repris côté alerte aujourd'hui)."""
    from .database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return False, "nœud introuvable", None, None
    e = _sonder(node)
    if not e.get("joignable"):
        return False, "nœud injoignable : %s" % e.get("erreur", ""), None, None
    if e.get("convention") == "realtime_tai":
        return False, ("ce nœud tient son heure du client PTP interne du moteur 2110 "
                       "(REALTIME = TAI) — installer chrony ferait battre deux disciplines "
                       "sur la même horloge"), None, None
    # Même refus, cause différente : phc2sys discipline CLOCK_REALTIME depuis le PHC. La
    # convention reste `realtime_utc` (phc2sys applique le décalage), donc le test ci-dessus ne
    # voit rien — c'est le cas d'Horace, où le bouton était proposé sur un nœud dont l'horloge
    # était tenue à ±50 ns. Ce qu'il faut réparer là, c'est le grandmaster, pas le nœud.
    if e.get("phc2sys_unites"):
        return False, ("phc2sys discipline déjà CLOCK_REALTIME sur ce nœud (%s) : installer chrony "
                       "mettrait deux servos sur la même horloge. Si le nœud est hors grille alors "
                       "que phc2sys est verrouillé, le décalage vient de la RÉFÉRENCE PTP "
                       "(grandmaster) — vérifier son clockClass avant toute chose."
                       % ", ".join(e["phc2sys_unites"])), None, None
    conf = ("# Bobi.Studio — la grille média MXL est indexée sur CLOCK_TAI ; sans table de\n"
            "# secondes intercalaires le noyau garde tai_offset=0 et CLOCK_TAI vaut l'UTC,\n"
            "# soit %d s d'écart avec la grille du cluster.\n"
            "leapseclist /usr/share/zoneinfo/leap-seconds.list\n" % TAI_UTC_OFFSET_S)
    cmd = ("set -e; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony >/dev/null 2>&1; "
           "mkdir -p /etc/chrony/conf.d; "
           "cat > /etc/chrony/conf.d/bobi-tai.conf <<'EOF'\n%s\nEOF\n"
           "systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true; "
           "systemctl restart chrony; echo applique" % conf)
    rc, out, err = node_driver.host_exec(node, cmd, timeout=180)
    if rc != 0 or "applique" not in (out or ""):
        return False, (err or out or "échec")[:200], None, None
    with _lock:
        _cache["data"] = None          # le prochain affichage doit montrer le résultat, pas le cache
    nom = node.get("name")
    return (True, "chrony + leapseclist appliqués sur %s" % nom,
            "alert.ptp.ntp_tai_applique", {"n": nom})


_charger_hist()      # au chargement du module : la fenêtre reprend là où le redémarrage l'a laissée
