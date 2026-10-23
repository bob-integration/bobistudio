# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""LECTURE SYNCHRONE — des players ORDINAIRES qui lisent ENSEMBLE, à l'image près, audio compris.

★ CE QUE C'EST. Un player = un conteneur = un canal. Un groupe synchrone en réunit N ≥ 2 (A, B, C… = leur
ordre) et leur fait lire une playlist en LIGNES : une ligne = un clip par canal, lancés ensemble. Objet
DISTINCT des groupes A/B (app/playout_groups.py), par décision de l'exploitation (2026-10-17) : ici aucun
canal ne « prend l'antenne » à la place d'un autre, ils jouent tous en même temps.

★ LE PRINCIPE. Chaque player sort une image par créneau de la grille TAI de son nœud, choisie par
l'horloge de présentation `media_time(G) = m0 + v·(G − g0)` (élément bobiplayout). Pour que N players
montrent la même image au même créneau, il suffit qu'ils aient la MÊME ancre (g0, m0, v) : on la leur
envoie DATÉE — « à l'instant de grille G, pose cette ancre » — avec G choisi dans le futur (marge
`lead_ms`, réglable). Chaque commande synchrone (Play, Pause, Cue, recherche, pas, vitesse, changement de
ligne) est une ancre datée. Un player qui reçoit la sienne en retard l'applique quand même AU CRÉNEAU G :
il rattrape sur la grille, il ne glisse pas — et son témoin dit de combien de créneaux il était en retard.
Le changement de ligne est PRÉ-PROGRAMMÉ dans chaque player (têtes de la ligne suivante ouvertes à
l'avance, bascule datée) : il ne dépend pas de la latence de l'orchestrateur au moment de la bascule.

★ CE QUI FAIT FOI : l'état OBSERVÉ (`GET /sync` de chaque player : commandes appliquées et leur retard,
ancre en vigueur, image réellement écrite et son retard en images), jamais ce que la base prétend. La base
ne garde que la configuration et une HYPOTHÈSE de reprise (`transport`), confrontée aux témoins au
redémarrage avant d'être reprise.

★ L'ÉCART ENTRE CANAUX EST MESURÉ, PAS SUPPOSÉ. Deux termes :
  · l'horloge : à chaque relevé, l'orchestrateur date la réponse de chaque player (CLOCK_TAI du player
    contre la sienne, au milieu de l'aller-retour ; on garde le relevé au plus court aller-retour, dont
    l'erreur est bornée par RTT/2). Deux players d'un même nœud partagent son horloge : écart nul par
    construction. Sur deux nœuds, c'est l'écart de leurs PTP / NTP — que rien d'autre ne peut corriger ;
  · l'image : le retard en images que le player déclare sur l'horloge de présentation (décodeur en retard
    = image précédente répétée).
  Un écart au-delà d'UNE image (seuil `period`) est signalé (alerte + vue), comme un canal qui n'a pas
  appliqué une commande à l'instant commun, ou qui l'a appliquée en retard, ou qui a quitté le mode
  synchrone (geste d'opérateur sur sa console). Banc indépendant des témoins (lit le BUS MXL) :
  tools/bancs/banc_sync_play.py.

★ FIN DE LIGNE (réglage du groupe `end_policy`) : à la fin du plus LONG clip (défaut — les plus courts
tiennent leur dernière image), du plus COURT (les autres sont coupés), ou au bout de la DURÉE de la ligne
(point Out de ligne ; à défaut, le plus long). `loop_mode` : off | line (la ligne courante reboucle) |
playlist (la dernière ligne enchaîne sur la première).

★ IDENTITÉ. Membres = `instance_uuid` (2e barreau : survit au recreate), jamais le vmid. Un player ne sert
qu'un groupe synchrone, et pas en même temps un groupe A/B.

Contrôleur ACTIF seul (main.py) : deux boucles se disputeraient les mêmes players.
Spécification : docs/reference/SYNC_PLAY.md.
"""
import datetime
import json
import logging
import threading
import time
import uuid as _uuid
from collections import deque

log = logging.getLogger(__name__)

PLAYER_TYPE = "player"
CONTROL_PORT = 8082
TICK_S = 0.25
POLL_S = 0.5                 # relecture du témoin /sync de chaque membre
HTTP_TIMEOUT_S = 1.5
PREPARE_TIMEOUT_S = 12.0     # têtes non prêtes au-delà : le canal est signalé, les autres partent
APPLY_GRACE_S = 1.0          # commande datée non constatée G + ce délai : « pas appliquée »
LEAD_MS_DEFAULT = 400
LEAD_MS_MIN, LEAD_MS_MAX = 150, 5000
CLOCK_SAMPLES = 12           # relevés d'horloge gardés par membre (on retient le plus court aller-retour)
CLOCK_MIN_SAMPLES = 3        # en deçà, pas d'estimation : un premier relevé lent se lirait comme un décalage
SKEW_PERSIST = 2             # passes consécutives au-delà d'une image avant l'alerte (un relevé isolé ne suffit pas)
SCHEDULE_HORIZON_S = 20.0    # une bascule datée plus lointaine attend : le player refuse au-delà de 60 s (horloge)
STEP_SAME_DECK_MAX = 8       # pas avant au-delà : recherche (la file d'une tête tient ~12 images en HD)
SPEED_MIN, SPEED_MAX = 0.25, 4.0
END_POLICIES = ("longest", "shortest", "duration")
# Canal qui ne s'ouvre pas au départ d'une ligne (réglage du groupe, ⚙) : « proceed » = les autres partent et le
# canal est signalé (DÉFAUT : la continuité de l'antenne passe d'abord — une ligne qui ne part pas fige TOUS les
# canaux sur leur dernière image) ; « refuse » = aucun canal ne part, la ligne d'avant continue / tient sa fin.
# Une PAIRE fill / key refuse TOUJOURS : un fill sans son key n'a pas de sens.
OPEN_FAIL = ("proceed", "refuse")
PAIR_FULL_TIMEOUT_S = 8.0    # files pleines attendues au plus ce délai avant de lancer une paire
PAIR_DECODE_MARGIN = 1.1     # une paire exige un décodage libre ≥ 1,1 × la cadence (vitesse comprise)
PAIR_FRESH_GRACE_S = 2.0     # après un départ, délai avant de juger `fresh_fps` (fenêtre de mesure du player)
LOOP_MODES = ("off", "line", "playlist")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MAX_MEMBERS = len(LETTERS)
MAX_LINES = 2000

_lock = threading.RLock()
# Canaux dont l'horloge ne compte pas dans l'écart : sans relevé, ou hors jeu. Un canal EN DÉFAUT, lui, compte —
# c'est souvent son horloge qui l'a mis en défaut, et l'écart doit le montrer (mesuré : −250 s, refusé).
_NO_CLOCK = ("offline", "missing", "no_element", "idle")


class SyncError(RuntimeError):
    """Refus d'une opération : `code` est traduit par l'interface (`section.syncplay.err.<code>`)."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def letter(i):
    return LETTERS[i] if 0 <= i < len(LETTERS) else "?"


def _num(v):
    try:
        x = float(v)
        return x if x >= 0 else None
    except (TypeError, ValueError):
        return None


def new_clip(d):
    """Clip d'une cellule (ligne × canal) : média + points In / Out (s). None = cellule vide (noir)."""
    if not isinstance(d, dict) or not (d.get("file") or d.get("asset")):
        return None
    i, o = _num(d.get("in")), _num(d.get("out"))
    if o is not None and i is not None and o <= i:
        o = None
    return {"file": str(d.get("file") or ""), "asset": str(d.get("asset") or "") or None,
            "title": (str(d.get("title")).strip()[:200] or None) if d.get("title") else None,
            "in": i, "out": o}


def new_line(d, members=None):
    d = d if isinstance(d, dict) else {}
    clips = {}
    for u, c in (d.get("clips") or {}).items():
        if members is None or u in members:
            cc = new_clip(c)
            if cc:
                clips[str(u)] = cc
    return {"id": str(d.get("id") or _uuid.uuid4().hex[:10]),
            "title": (str(d.get("title")).strip()[:200] or None) if d.get("title") else None,
            "clips": clips, "duration": _num(d.get("duration")) or None,
            # CONSTAT par paire : « ligne déjà jouée en entier sans défaut » ({clé de paire: empreinte}). Il ne
            # vaut que tant que l'empreinte (fichiers, points In / Out, vitesse) est la même — cf. pair_sig.
            "proven": {str(k): str(v) for k, v in (d.get("proven") or {}).items()} if isinstance(d.get("proven"), dict) else {}}


def pair_sig(line, a, b, speed):
    """Empreinte de ce qu'une paire a joué : fichiers, identités, points In / Out des deux cellules, vitesse.
    Toute modification de la ligne la change — le constat « déjà jouée sans défaut » tombe de lui-même."""
    cs = [(line.get("clips") or {}).get(u) or {} for u in (a, b)]
    return json.dumps([[c.get("file"), c.get("asset"), c.get("in"), c.get("out")] for c in cs] + [round(float(speed or 0), 4)])


def pair_key(g, a, b):
    return "+".join(sorted([letter(g["members"].index(a)), letter(g["members"].index(b))]))


def proven_pairs(g, line, speed):
    """Paires dont le constat sur `line` vaut encore à la vitesse `speed`."""
    pr = (line or {}).get("proven") or {}
    return {pair_key(g, a, b) for a, b in g.get("pairs") or []
            if pr.get(pair_key(g, a, b)) == pair_sig(line, a, b, speed)}


def line_label(line, members=None):
    if not line:
        return ""
    if line.get("title"):
        return line["title"]
    ts = [(c.get("title") or (c.get("file") or "").rsplit("/", 1)[-1] or c.get("asset") or "")
          for u, c in (line.get("clips") or {}).items() if members is None or u in members]
    return " | ".join(t for t in ts if t)[:200]


def line_index(lines, lid):
    for i, ln in enumerate(lines):
        if ln["id"] == lid:
            return i
    return -1


def next_line_id(g, lid):
    """Ligne qui suit `lid` selon le bouclage ; None = fin de playlist."""
    lines = g["lines"]
    i = line_index(lines, lid)
    if i < 0:
        return lines[0]["id"] if lines else None
    if g.get("loop_mode") == "line":
        return lid
    if i + 1 < len(lines):
        return lines[i + 1]["id"]
    return lines[0]["id"] if g.get("loop_mode") == "playlist" else None


def line_duration(g, line, durations):
    """Durée de la ligne (s) d'après la règle de fin et la durée de CHAQUE clip (None = inconnue).
    `durations` = {uuid: durée du clip entre ses points In et Out | None}."""
    ds = [durations.get(u) for u in g["members"] if (line.get("clips") or {}).get(u)]
    known = [d for d in ds if d]
    pol = g.get("end_policy") or "longest"
    if pol == "duration" and line.get("duration"):
        return float(line["duration"])
    if not ds or len(known) < len(ds):
        # Une durée inconnue (flux, sonde en échec) : pour « le plus court », les durées connues
        # bornent quand même ; pour « le plus long », on ne sait pas — fin CONSTATÉE (tous finis).
        return min(known) if (pol == "shortest" and known) else None
    return min(known) if pol == "shortest" else max(known)


def snap_up(t_ns, period_ns):
    """Instant de grille (créneau) au plus tôt à `t_ns`."""
    k = -(-int(t_ns) // int(period_ns))
    return int(k * period_ns)


def media_time(anchor, t_ns):
    """Temps de ligne (s) à l'instant `t_ns` sous l'ancre {g0, t0, speed}."""
    return anchor["t0"] + anchor["speed"] * (int(t_ns) - anchor["g0"]) / 1e9


def clock_estimate(samples):
    """(écart d'horloge ns, incertitude ns) du relevé au plus court aller-retour — la méthode NTP : c'est
    l'échantillon qui a le moins de place pour être asymétrique. None si aucun relevé."""
    if not samples:
        return None
    off, rtt = min(samples, key=lambda s: s[1])
    return off, rtt // 2


def sync_verdict(chans, period_ms):
    """TOLÉRANCE ZÉRO (décision 2026-10-17 : « une image c'est déjà trop » — un fill et son key doivent être
    exactement ensemble). Verdict sur les canaux EN LECTURE, trois faits distincts :
      · `frames` : écart en IMAGES entre canaux au témoin (retards d'image déclarés, entiers EXACTS — aucune
        incertitude de mesure ne s'y applique) ; ≥ 1 = défaut ;
      · `anchors` : canaux dont l'ancre (instant, temps de ligne, vitesse) diffère de celle des autres — un
        temps média différent sur la même image de grille, même sans aucun retard déclaré ; défaut ;
      · `clock` : écart des HORLOGES de nœuds (ms) diminué de son incertitude (± RTT/2, de l'ordre de la ms) ;
        défaut s'il atteint une image. L'incertitude ne s'applique qu'à ce terme : elle ne masque jamais un
        écart d'image.
    `chans` : [{letter, lag, clock_ms, clock_unc_ms, anchor_rel}]."""
    lags = [int(c.get("lag") or 0) for c in chans]
    frames = (max(lags) - min(lags)) if len(lags) >= 2 else 0
    ref = {}
    for c in chans:
        a = c.get("anchor_rel")
        if a:
            ref[a] = ref.get(a, []) + [c["letter"]]
    anchors = []
    if len(ref) > 1:
        main = max(ref.values(), key=len)
        anchors = sorted(x for v in ref.values() if v is not main for x in v)
    skew, unc = group_skew(chans, period_ms)
    clk = [c for c in chans if c.get("clock_ms") is not None]
    cs = (max(c["clock_ms"] for c in clk) - min(c["clock_ms"] for c in clk)) if len(clk) >= 2 else 0.0
    cu = (max(c.get("clock_unc_ms") or 0.0 for c in clk) * 2) if len(clk) >= 2 else 0.0
    clock = cs - cu >= period_ms
    return {"frames": frames, "anchors": anchors, "clock_ms": round(cs, 3), "clock_unc_ms": round(cu, 3),
            "clock": clock, "skew_ms": skew, "skew_unc_ms": unc,
            "defect": frames >= 1 or bool(anchors) or clock}


def group_skew(chans, period_ms):
    """Écart entre canaux EN LECTURE (ms) = horloge relative + retard d'image ; et son incertitude.
    `chans` : [{clock_ms, clock_unc_ms, lag}] — les canaux sans mesure d'horloge sont ignorés."""
    vals = [(c["clock_ms"] + (c.get("lag") or 0) * period_ms, c.get("clock_unc_ms") or 0.0)
            for c in chans if c.get("clock_ms") is not None]
    if len(vals) < 2:
        return 0.0, 0.0
    xs = [v for v, _ in vals]
    return round(max(xs) - min(xs), 3), round(max(u for _, u in vals) * 2, 3)


# ═══ PERSISTANCE ══════════════════════════════════════════════════════════════════════════════
def _row(r):
    if r is None:
        return None
    g = dict(r)
    for k, d in (("members", []), ("lines", []), ("transport", {}), ("saved_lists", {}), ("pairs", [])):
        try:
            g[k] = json.loads(g.get(k) or "null") or d
        except ValueError:
            g[k] = d
    g["lines"] = [new_line(x, g["members"]) for x in g["lines"]]
    g["end_policy"] = g.get("end_policy") if g.get("end_policy") in END_POLICIES else "longest"
    g["loop_mode"] = g.get("loop_mode") if g.get("loop_mode") in LOOP_MODES else "off"
    g["open_fail"] = g.get("open_fail") if g.get("open_fail") in OPEN_FAIL else "proceed"
    g["pairs"] = [list(p) for p in g["pairs"] if isinstance(p, list) and len(p) == 2
                  and p[0] in g["members"] and p[1] in g["members"]]
    try:
        g["lead_ms"] = max(LEAD_MS_MIN, min(LEAD_MS_MAX, int(g.get("lead_ms") or LEAD_MS_DEFAULT)))
    except (TypeError, ValueError):
        g["lead_ms"] = LEAD_MS_DEFAULT
    return g


def list_groups(project_id=None):
    from .database import get_db
    gs = [_row(r) for r in get_db().execute("SELECT * FROM sync_groups ORDER BY id").fetchall()]
    return [g for g in gs if project_id is None or g.get("project_id") == project_id]


def get_group(gid):
    from .database import get_db
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return None
    return _row(get_db().execute("SELECT * FROM sync_groups WHERE id=?", (gid,)).fetchone())


def save_group(g):
    from .database import get_db
    with get_db() as db:
        db.execute("""UPDATE sync_groups SET name=?, project_id=?, members=?, lines=?, cursor=?, end_policy=?,
                      loop_mode=?, lead_ms=?, transport=?, saved_lists=?, pairs=?, open_fail=?, updated_at=? WHERE id=?""",
                   (g["name"], g.get("project_id"), json.dumps(g["members"]), json.dumps(g["lines"]), g.get("cursor"),
                    g.get("end_policy") or "longest", g.get("loop_mode") or "off", int(g.get("lead_ms") or LEAD_MS_DEFAULT),
                    json.dumps(g.get("transport") or {}), json.dumps(g.get("saved_lists") or {}),
                    json.dumps(g.get("pairs") or []), g.get("open_fail") or "proceed", _now_iso(), g["id"]))


def _dc(c):
    try:
        return json.loads(c.get("deploy_config") or "{}") if isinstance(c.get("deploy_config"), str) \
            else (c.get("deploy_config") or {})
    except ValueError:
        return {}


def _players():
    from .database import db_get_containers
    return {c["instance_uuid"]: c for c in db_get_containers() or []
            if c.get("instance_uuid") and _dc(c).get("type") == PLAYER_TYPE}


def _validate_config(cfg, players, gid=None):
    name = str(cfg.get("name") or "").strip()[:80]
    if not name:
        raise SyncError("name_required", "nom du groupe requis")
    members = []
    for u in cfg.get("members") or []:
        u = str(u)
        if u not in players:
            raise SyncError("not_a_player", "membre inconnu ou qui n'est pas un player : %s" % u)
        if u in members:
            raise SyncError("duplicate_member", "un player ne compte qu'une fois dans un groupe")
        members.append(u)
    if len(members) < 2:
        raise SyncError("too_few_members", "une lecture synchrone réunit au moins deux players")
    if len(members) > MAX_MEMBERS:
        raise SyncError("too_many_members", "au plus %d canaux" % MAX_MEMBERS)
    for other in list_groups():
        if other["id"] != gid and set(other["members"]) & set(members):
            raise SyncError("member_in_other_group", "un player ne sert qu'un groupe synchrone (déjà dans « %s »)"
                            % other["name"])
    try:
        from .playout_groups import list_groups as _ab_groups
        for ab in _ab_groups():
            if set(ab["members"]) & set(members):
                raise SyncError("member_in_ab_group", "un player d'un groupe A/B (« %s ») ne peut pas lire en "
                                "synchrone : les deux le chargeraient" % ab["name"])
    except SyncError:
        raise
    except Exception:                                                  # noqa: BLE001
        log.debug("sync groups: A/B groups not readable", exc_info=True)
    pol = cfg.get("end_policy") or "longest"
    if pol not in END_POLICIES:
        raise SyncError("bad_end_policy", "règle de fin de ligne inconnue : %s" % pol)
    loop = cfg.get("loop_mode") or "off"
    if loop not in LOOP_MODES:
        raise SyncError("bad_loop", "bouclage inconnu : %s" % loop)
    try:
        lead = int(cfg.get("lead_ms") or LEAD_MS_DEFAULT)
    except (TypeError, ValueError):
        raise SyncError("bad_lead", "marge de commande illisible")
    if not LEAD_MS_MIN <= lead <= LEAD_MS_MAX:
        raise SyncError("bad_lead", "marge de commande entre %d et %d ms" % (LEAD_MS_MIN, LEAD_MS_MAX))
    of = cfg.get("open_fail") or "proceed"
    if of not in OPEN_FAIL:
        raise SyncError("bad_open_fail", "réglage « canal qui ne s'ouvre pas » inconnu : %s" % of)
    # PAIRES fill / key : deux canaux du groupe, chacun dans une paire au plus, sur le MÊME nœud (horloge
    # commune par construction — entre nœuds, l'écart d'horloge ne se corrige pas).
    pairs, seen = [], set()
    for p in cfg.get("pairs") or []:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise SyncError("bad_pair", "une paire réunit exactement deux canaux")
        a, b = str(p[0]), str(p[1])
        if a == b or a not in members or b not in members:
            raise SyncError("bad_pair", "une paire réunit deux canaux distincts du groupe")
        if {a, b} & seen:
            raise SyncError("pair_overlap", "un canal n'appartient qu'à une paire")
        seen |= {a, b}
        if players[a].get("node_id") != players[b].get("node_id"):
            raise SyncError("pair_nodes", "une paire fill / key doit être sur le MÊME nœud (%s ≠ %s) : entre deux "
                            "nœuds, l'écart de leurs horloges ne se corrige pas" % (
                                players[a].get("hostname"), players[b].get("hostname")))
        pairs.append([a, b])
    pids = {players[u].get("project_id") for u in members}
    return {"name": name, "members": members, "end_policy": pol, "loop_mode": loop, "lead_ms": lead,
            "open_fail": of, "pairs": pairs, "project_id": pids.pop() if len(pids) == 1 else None}


_CFG_KEYS = ("name", "members", "end_policy", "loop_mode", "lead_ms", "open_fail", "pairs")


def pair_of(g, u):
    """Partenaire de `u` dans une paire fill / key, ou None."""
    for a, b in g.get("pairs") or []:
        if u == a:
            return b
        if u == b:
            return a
    return None


def pair_line_issues(g, line):
    """Ce qu'une LIGNE viole pour chaque paire, sans rien ouvrir : les deux cellules pleines ou vides
    ensemble, mêmes points In, mêmes points Out relatifs. [(a, b, code, détail)]."""
    out = []
    for a, b in g.get("pairs") or []:
        ca, cb = (line.get("clips") or {}).get(a), (line.get("clips") or {}).get(b)
        if bool(ca) != bool(cb):
            out.append((a, b, "pair_cell", "une seule des deux cellules est remplie"))
            continue
        if not ca:
            continue
        if abs((ca.get("in") or 0.0) - (cb.get("in") or 0.0)) > 1e-3:
            out.append((a, b, "pair_in", "points In différents (%.3f s / %.3f s)" % (ca.get("in") or 0.0, cb.get("in") or 0.0)))
        la = (ca["out"] - (ca.get("in") or 0.0)) if ca.get("out") is not None else None
        lb = (cb["out"] - (cb.get("in") or 0.0)) if cb.get("out") is not None else None
        if la is not None and lb is not None and abs(la - lb) > 1e-3:
            out.append((a, b, "pair_duration", "durées différentes (%.3f s / %.3f s)" % (la, lb)))
    return out


def _need(gid):
    g = get_group(gid)
    if not g:
        raise SyncError("unknown_group", "groupe synchrone introuvable")
    return g


def create_group(cfg):
    from .database import get_db
    with _lock:
        v = _validate_config(cfg, _players())
        with get_db() as db:
            cur = db.execute("""INSERT INTO sync_groups (name, project_id, members, end_policy, loop_mode, lead_ms,
                                open_fail, pairs, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                             (v["name"], v["project_id"], json.dumps(v["members"]), v["end_policy"], v["loop_mode"],
                              v["lead_ms"], v["open_fail"], json.dumps(v["pairs"]), _now_iso(), _now_iso()))
            gid = cur.lastrowid
    ENGINE.kick()
    log.info("sync group %s created: %s (%d channels)", gid, v["name"], len(v["members"]))
    return gid


def update_group(gid, cfg):
    with _lock:
        g = _need(gid)
        merged = {k: g.get(k) for k in _CFG_KEYS}
        merged.update({k: v for k, v in (cfg or {}).items() if k in _CFG_KEYS})
        if "pairs" not in (cfg or {}):
            merged["pairs"] = [p for p in merged.get("pairs") or [] if set(p) <= set(merged["members"])]
        if merged["members"] != g["members"] and ENGINE.state_of(g["id"]) not in ("stopped",):
            raise SyncError("running", "arrêtez la lecture avant de changer les canaux")
        v = _validate_config(merged, _players(), gid=g["id"])
        loop_changed = v["loop_mode"] != g.get("loop_mode")
        g.update(v)
        g["lines"] = [new_line(x, g["members"]) for x in g["lines"]]
        save_group(g)
    if loop_changed:
        ENGINE.replan(g["id"])
    ENGINE.kick()
    return g


def delete_group(gid):
    from .database import get_db
    with _lock:
        g = _need(gid)
    ENGINE.stop(g, reason="deleted")
    with _lock:
        with get_db() as db:
            db.execute("DELETE FROM sync_groups WHERE id=?", (int(gid),))
    ENGINE.forget(int(gid))


def group_of_member(instance_uuid):
    for g in list_groups():
        if instance_uuid in g["members"]:
            return g
    return None


def lines_op(gid, op, body):
    """Édition de la playlist en lignes : add, drop, up, down, set_cell, title, duration, clear, save, load."""
    with _lock:
        g = _need(gid)
        lines = g["lines"]
        cur = g.get("cursor")
        lid = str(body.get("line") or "")
        i = line_index(lines, lid) if lid else -1
        if op == "add":
            news = [new_line(x, g["members"]) for x in (body.get("lines") or [])]
            if body.get("clips") is not None:
                news.append(new_line({"clips": body.get("clips"), "title": body.get("title")}, g["members"]))
            if len(lines) + len(news) > MAX_LINES:
                raise SyncError("too_many_lines", "au plus %d lignes" % MAX_LINES)
            at = i + 1 if i >= 0 and body.get("after") else len(lines)
            lines[at:at] = news
        elif op in ("drop", "up", "down", "set_cell", "title", "duration"):
            if i < 0:
                raise SyncError("unknown_line", "ligne introuvable")
            if op == "drop":
                if lid == cur and ENGINE.state_of(g["id"]) != "stopped":
                    raise SyncError("line_on_air", "la ligne en cours ne se retire pas pendant la lecture")
                del lines[i]
            elif op in ("up", "down"):
                j = i - 1 if op == "up" else i + 1
                if 0 <= j < len(lines):
                    lines[i], lines[j] = lines[j], lines[i]
            elif op == "set_cell":
                u = str(body.get("member") or "")
                if u not in g["members"]:
                    raise SyncError("unknown_member", "canal inconnu")
                c = new_clip(body.get("clip"))
                if c:
                    lines[i]["clips"][u] = c
                else:
                    lines[i]["clips"].pop(u, None)
                lines[i]["proven"] = {}            # ligne modifiée : le constat « jouée sans défaut » tombe
            elif op == "title":
                lines[i]["title"] = (str(body.get("title") or "").strip()[:200]) or None
            elif op == "duration":
                lines[i]["duration"] = _num(body.get("duration")) or None
        elif op == "clear":
            if ENGINE.state_of(g["id"]) != "stopped":
                raise SyncError("running", "arrêtez la lecture avant de vider la playlist")
            lines[:] = []
            g["cursor"] = None
        elif op == "save":
            name = str(body.get("name") or "").strip()[:80]
            if not name:
                raise SyncError("name_required", "nom de playlist requis")
            g["saved_lists"][name] = [dict(x) for x in lines]
        elif op == "load":
            name = str(body.get("name") or "")
            src = (g.get("saved_lists") or {}).get(name)
            if src is None:
                raise SyncError("unknown_list", "playlist « %s » introuvable" % name)
            news = [new_line(dict(x, id=None), g["members"]) for x in src]
            if (body.get("mode") or "replace") == "replace":
                if ENGINE.state_of(g["id"]) != "stopped":
                    raise SyncError("running", "arrêtez la lecture avant de remplacer la playlist")
                lines[:] = news
                g["cursor"] = None
            else:
                lines.extend(news)
        elif op == "unsave":
            g["saved_lists"].pop(str(body.get("name") or ""), None)
        else:
            raise SyncError("unknown_op", "opération inconnue : %s" % op)
        if g.get("cursor") and line_index(lines, g["cursor"]) < 0:
            g["cursor"] = None
        if not g.get("cursor") and lines:
            g["cursor"] = lines[0]["id"]
        save_group(g)
    if op in ("add", "drop", "up", "down", "set_cell", "duration", "load"):
        ENGINE.replan(g["id"])
    ENGINE.kick()
    return g


# ═══ MOTEUR ═══════════════════════════════════════════════════════════════════════════════════
class Engine:
    """Une passe = `tick()` ; une commande = une méthode `op_*` (sous le verrou du groupe). Les accès au
    monde (base, réseau, horloge, alertes) sont des méthodes remplacées dans le banc."""

    def __init__(self):
        self.mem = {}            # (gid, uuid) → {obs, reachable, vmid, clock: deque[(off_ns, rtt_ns)], late0, ...}
        self.rt = {}             # gid → état de lecture (modèle) ; cf. _rt()
        self.glocks = {}
        self.alerted = set()
        self.cmd_seq = int(time.time() * 1000) % 1_000_000_000
        self.wake = threading.Event()
        self._pcache = None
        self.last_poll = 0.0
        self.ticks = 0
        self.errors = 0
        self.last_error = None

    # ── accès au monde ─────────────────────────────────────────────────────────────────────────
    def groups(self):
        return list_groups()

    def players(self):
        # Relu au plus une fois par seconde : la table des conteneurs coûte ~50 ms à lire et décoder
        # (mesuré le 2026-10-17), et une passe en demandait une demi-douzaine — la boucle tombait à une
        # passe par seconde, et la ligne suivante se préparait trop tard pour être datée à l'avance.
        now = time.monotonic()
        if self._pcache is None or now - self._pcache[0] > 1.0:
            self._pcache = (now, _players())
        return self._pcache[1]

    def ip(self, vmid):
        from .addressing import get_container_ip
        return get_container_ip(vmid)

    def http(self, method, vmid, path, body, timeout=HTTP_TIMEOUT_S):
        import requests
        ip = self.ip(vmid)
        if not ip:
            return 0, {"error": "no IP"}
        try:
            r = requests.request(method, "http://%s:%d%s" % (ip, CONTROL_PORT, path), json=body, timeout=timeout)
        except Exception as e:
            return 0, {"error": type(e).__name__}
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def tai(self):
        """CLOCK_TAI de l'orchestrateur (ns) — seulement pour DATER les relevés ; la grille commune est
        celle des players (cf. `group_now`)."""
        return time.clock_gettime_ns(time.CLOCK_TAI)

    def sleep(self, s):
        time.sleep(s)

    def alert(self, key, params, vmid=None, level="warning"):
        try:
            from .database import db_add_alert
            db_add_alert(key, level, vmid=vmid, params=params)
        except Exception:
            log.exception("sync groups: alert not written")

    def fetch_start(self, asset, vmid):
        from . import media_fetch
        return media_fetch.start(asset, vmid)

    # ── état ───────────────────────────────────────────────────────────────────────────────────
    def kick(self):
        self.wake.set()

    def glock(self, gid):
        with _lock:
            return self.glocks.setdefault(gid, threading.RLock())

    def _m(self, gid, u):
        return self.mem.setdefault((gid, u), {"obs": None, "reachable": False, "vmid": None, "error": None,
                                              "clock": deque(maxlen=CLOCK_SAMPLES), "late_seen": None,
                                              "late_recent": deque(maxlen=40), "late_unalerted": 0,
                                              "left_seen": None, "refused": None, "fetch": None})

    def _rt(self, gid):
        return self.rt.setdefault(gid, {"state": "stopped", "line": None, "key": None, "gen": 0, "anchor": None,
                                        "speed_play": 1.0, "durations": {}, "dur": None, "next": None,
                                        "sent": {}, "failure": None, "recovered": None, "ended": False})

    def state_of(self, gid):
        return (self.rt.get(gid) or {}).get("state", "stopped")

    def forget(self, gid):
        for k in [k for k in self.mem if k[0] == gid]:
            del self.mem[k]
        self.rt.pop(gid, None)
        self.alerted = {a for a in self.alerted if a[0] != gid}

    def _persist(self, g, rt):
        g["transport"] = {k: rt.get(k) for k in ("state", "line", "key", "gen", "anchor", "speed_play")}
        g["cursor"] = rt.get("line") or g.get("cursor")
        with _lock:
            cur = get_group(g["id"])
            if cur:
                cur["transport"], cur["cursor"] = g["transport"], g["cursor"]
                save_group(cur)

    def _fail(self, g, rt, code, detail, member=None):
        rt["failure"] = {"at": time.time(), "code": code, "detail": detail,
                         "channel": letter(g["members"].index(member)) if member in g["members"] else None}

    def _once(self, gid, kind, *key):
        k = (gid, kind) + key
        if k in self.alerted:
            return False
        self.alerted.add(k)
        return True

    # ── horloge commune ────────────────────────────────────────────────────────────────────────
    # L'horloge est celle du NŒUD (CLOCK_TAI de l'hôte, que tous ses conteneurs partagent) : deux canaux
    # d'un même nœud ont par construction la même, et on l'estime sur l'ensemble de leurs relevés. Sans
    # cela, deux players voisins paraissaient décalés de leur seule incertitude de mesure.
    def _node_key(self, g, u, players=None):
        c = (players or self.players()).get(u) or {}
        return c.get("node_id") if c.get("node_id") is not None else "u:" + u

    def clock_of(self, gid, u, g=None, players=None):
        g = g or get_group(gid)
        if not g:
            return None
        players = players or self.players()
        nk = self._node_key(g, u, players)
        samples = []
        for v in g["members"]:
            if self._node_key(g, v, players) == nk:
                samples.extend(self._m(gid, v)["clock"])
        if len(samples) < CLOCK_MIN_SAMPLES:
            return None
        return clock_estimate(samples)

    def consensus(self, g, players=None):
        """Écart (ns) de l'horloge COMMUNE des canaux à celle de l'orchestrateur : médiane des écarts mesurés
        des NŒUDS des canaux. None = aucun canal mesuré."""
        players = players or self.players()
        per_node = {}
        for u in g["members"]:
            nk = self._node_key(g, u, players)
            if nk not in per_node:
                e = self.clock_of(g["id"], u, g, players)
                if e:
                    per_node[nk] = e[0]
        offs = sorted(per_node.values())
        if not offs:
            return None
        return offs[len(offs) // 2]

    def group_now(self, g):
        c = self.consensus(g)
        return None if c is None else self.tai() + c

    def period_ns(self, g):
        for u in g["members"]:
            o = self._m(g["id"], u).get("obs") or {}
            if o.get("fps_num") and o.get("fps_den"):
                return int(round(1e9 * int(o["fps_den"]) / int(o["fps_num"])))
        return 40_000_000

    def _at(self, g, lead_ms=None):
        now = self.group_now(g)
        if now is None:
            raise SyncError("no_clock", "aucun canal joignable : instant commun impossible à choisir")
        return snap_up(now + int((lead_ms or g["lead_ms"]) * 1e6), self.period_ns(g))

    # ── relevés ────────────────────────────────────────────────────────────────────────────────
    def observe(self, gid, u, players):
        m = self._m(gid, u)
        c = players.get(u)
        if not c:
            m.update(reachable=False, obs=None, vmid=None, error="missing")
            return None
        m["vmid"] = c["vmid"]
        t0 = self.tai()
        code, st = self.http("GET", c["vmid"], "/sync", None)
        t1 = self.tai()
        if code == 200 and isinstance(st, dict) and st.get("tai_ns"):
            m.update(reachable=True, obs=st, error=None if st.get("capable") else "no_element")
            m["clock"].append((int(st["tai_ns"]) - (t0 + t1) // 2, t1 - t0))
            # Images écrites EN RETARD (compteur cumulé de l'élément) : le delta est compté ICI, au relevé,
            # pour que ni la vue ni l'alerte ne le « consomment » à la place de l'autre.
            cum = int(((st.get("witness") or {}).get("late_slots")) or 0)
            if m["late_seen"] is not None and cum > m["late_seen"]:
                m["late_recent"].append((time.time(), cum - m["late_seen"]))
                m["late_unalerted"] += cum - m["late_seen"]
                m["pair_late"] = m.get("pair_late", 0) + cum - m["late_seen"]
            m["late_seen"] = cum
            return st
        m.update(reachable=False, error="unreachable" if code == 0 else ("http_%s" % code))
        if code == 404:
            m["error"] = "old_player"
        return None

    def _poll(self, gs, players):
        for g in gs:
            for u in g["members"]:
                self.observe(g["id"], u, players)

    # ── commandes vers les players ─────────────────────────────────────────────────────────────
    def _vmid(self, g, u, players=None):
        c = (players or self.players()).get(u)
        return c["vmid"] if c else None

    def _enter(self, g, players):
        for u in g["members"]:
            vm = self._vmid(g, u, players)
            if vm is None:
                continue
            code, d = self.http("POST", vm, "/sync/enter", {"group": "%s:%s" % (g["id"], g["name"])})
            if code != 200:
                self._m(g["id"], u)["error"] = (d or {}).get("code") or (d or {}).get("error") or ("HTTP %s" % code)

    def _prepare(self, g, rt, line, t=0.0, players=None):
        """Ouvre une tête par canal pour `line` à `t` s de son début (non bloquant côté player). Renvoie
        (clé, {uuid: erreur}). Une cellule vide n'ouvre rien (le canal sortira du noir)."""
        players = players or self.players()
        rt["gen"] += 1
        key = "s%d-%s-%d" % (g["id"], line["id"], rt["gen"])
        errs = {}
        for u in g["members"]:
            clip = line["clips"].get(u)
            vm = self._vmid(g, u, players)
            if not clip:
                continue
            if vm is None:
                errs[u] = "missing"
                continue
            body = {"key": key, "file": clip["file"], "asset": clip.get("asset"), "title": clip.get("title"),
                    "in": (clip.get("in") or 0.0) + max(0.0, t), "out": clip.get("out")}
            code, d = self.http("POST", vm, "/sync/prepare", body)
            if code == 202:
                continue
            d = d or {}
            if code == 409 and d.get("missing") and clip.get("asset"):
                try:
                    j = self.fetch_start(clip["asset"], vm)
                    self._m(g["id"], u)["fetch"] = dict(j or {}, asset=clip["asset"])
                    errs[u] = "fetching"
                except Exception as e:                                 # noqa: BLE001
                    errs[u] = "fetch: %s" % e
            else:
                errs[u] = d.get("code") or d.get("error") or ("HTTP %s" % code)
        return key, errs

    def _deck(self, g, u, key):
        o = self._m(g["id"], u).get("obs") or {}
        return (o.get("decks") or {}).get(key)

    def _wait_ready(self, g, line, key, errs, players, timeout=PREPARE_TIMEOUT_S):
        """Attend que chaque canal qui a un clip ait sa tête prête (relu au /sync). Renvoie {uuid: durée}
        et complète `errs` (canaux en erreur ou en retard)."""
        want = [u for u in g["members"] if line["clips"].get(u) and u not in errs]
        t_end = time.time() + timeout
        durs = {}
        while want and time.time() < t_end:
            for u in list(want):
                self.observe(g["id"], u, players)
                d = self._deck(g, u, key)
                if not d:
                    continue
                if d.get("state") == "ready":
                    want.remove(u)
                elif d.get("state") == "error":
                    errs[u] = d.get("error") or "error"
                    want.remove(u)
            if want:
                self.sleep(0.05)
        for u in want:
            errs[u] = "not_ready"
        for u in g["members"]:
            clip = line["clips"].get(u)
            d = self._deck(g, u, key) or {}
            if clip and d.get("state") == "ready":
                full = clip.get("out") or d.get("file_duration")
                durs[u] = round(max(0.0, full - (clip.get("in") or 0.0)), 4) if full else None
        return durs

    def _send(self, g, rt, at, kind, per_member, players):
        """Envoie la MÊME commande datée à chaque canal : `per_member` = {uuid: corps sans at/id}. Refus d'un
        canal → signalé tout de suite (alerte + vue) ; les autres l'appliquent quand même à l'instant dit."""
        self.cmd_seq += 1
        cid = self.cmd_seq
        res = {}
        for u, body in per_member.items():
            vm = self._vmid(g, u, players)
            if vm is None:
                res[u] = "missing"
                continue
            code, d = self.http("POST", vm, "/sync/at", dict(body, id=cid, at=at))
            res[u] = "ok" if code == 200 else ((d or {}).get("code") or (d or {}).get("error") or "HTTP %s" % code)
            if code != 200:
                self._refused(g, u, res[u], d, vm)
            else:
                self._m(g["id"], u)["refused"] = None
        rt["sent"][cid] = {"at": at, "kind": kind, "res": res, "checked": False, "t": time.time()}
        while len(rt["sent"]) > 32:
            rt["sent"].pop(min(rt["sent"]))
        return cid

    def _refused(self, g, u, why, d, vm):
        i = g["members"].index(u)
        msg = why
        if why == "clock" and d and d.get("tai_ns") and d.get("at"):
            msg = "horloge du nœud à %+.3f s" % ((int(d["tai_ns"]) - int(d["at"])) / 1e9)
        # Persistant jusqu'à la prochaine commande ACCEPTÉE par ce canal : un relevé réussi entre-temps
        # ne doit pas le faire passer pour « à l'heure ».
        self._m(g["id"], u)["refused"] = {"why": why, "msg": msg, "at": time.time()}
        self.alert("alert.sync.not_applied", {"g": g["name"], "c": letter(i), "e": msg}, vmid=vm)

    def _body_for(self, g, line, u, key, m_line, speed):
        """Corps de la commande datée d'un canal pour la ligne `line`, temps de ligne `m_line` (s)."""
        clip = (line or {}).get("clips", {}).get(u)
        if not clip:
            return {"black": True, "m0": 0.0, "speed": speed}
        b = {"m0": round((clip.get("in") or 0.0) + max(0.0, m_line), 6), "speed": speed}
        if key:
            b["key"] = key
        return b

    def _line(self, g, lid):
        i = line_index(g["lines"], lid)
        return g["lines"][i] if i >= 0 else None

    # ── opérations synchrones ──────────────────────────────────────────────────────────────────
    def _gate(self, g, line, key, errs, durs, speed, players, deadline=None, proven=()):
        """Peut-on lancer `line` (têtes `key`) ? None = oui ; « wait » = files d'une paire pas encore pleines ;
        sinon (code, détail lisible). Règles : canal qui ne s'ouvre pas selon `open_fail` (une paire refuse
        toujours) ; pour chaque paire : même nœud, même format, cellules / In / durées identiques, deux files
        PLEINES, décodage libre ≥ PAIR_DECODE_MARGIN × la cadence."""
        L = lambda u: letter(g["members"].index(u))
        for u, e in errs.items():
            pu = pair_of(g, u)
            if pu:
                return ("pair_open", "paire %s+%s : le canal %s ne s'ouvre pas (%s)" % (L(u), L(pu), L(u), e))
            if g.get("open_fail") == "refuse":
                return ("open_refused", "le canal %s ne s'ouvre pas (%s)" % (L(u), e))
        iss = pair_line_issues(g, line)
        if iss:
            a, b, code, d = iss[0]
            return (code, "paire %s+%s : %s" % (L(a), L(b), d))
        waiting = False
        for a, b in g.get("pairs") or []:
            if not (line.get("clips") or {}).get(a):
                continue
            ca, cb = players.get(a) or {}, players.get(b) or {}
            if ca.get("node_id") != cb.get("node_id"):
                return ("pair_nodes", "paire %s+%s : nœuds différents" % (L(a), L(b)))
            fa = (self._m(g["id"], a).get("obs") or {}).get("format")
            fb = (self._m(g["id"], b).get("obs") or {}).get("format")
            if not fa or not fb:
                return ("pair_unverified", "paire %s+%s : format illisible (player injoignable ou trop ancien)" % (L(a), L(b)))
            if fa != fb:
                return ("pair_format", "paire %s+%s : formats différents (%s / %s)" % (L(a), L(b), fa, fb))
            da, db = durs.get(a), durs.get(b)
            if da is None or db is None:
                return ("pair_duration", "paire %s+%s : durée d'un clip inconnue" % (L(a), L(b)))
            if abs(da - db) >= self.period_ns(g) / 2e9:
                return ("pair_duration", "paire %s+%s : durées différentes (%.3f s / %.3f s)" % (L(a), L(b), da, db))
            fps = int(fa["fps_num"]) / float(fa["fps_den"] or 1)
            for u in (a, b):
                d = self._deck(g, u, key) or {}
                if not d.get("full"):
                    waiting = True
                    continue
                need = PAIR_DECODE_MARGIN * fps * max(1.0, speed or 1.0)
                # Ligne déjà jouée EN ENTIER par cette paire sans aucune répétition ni baisse de cadence : ce
                # constat fait foi, la mesure au remplissage (sous-estimée par la contention) ne la refuse plus.
                if pair_key(g, a, b) in proven:
                    continue
                if d.get("decode_fps") is not None and d["decode_fps"] < need:
                    return ("pair_cadence", "paire %s+%s : le clip du canal %s se décode à %.1f i/s, il en faut %.1f "
                            "pour tenir la cadence" % (L(a), L(b), L(u), d["decode_fps"], need))
        if waiting:
            if deadline is not None and time.time() > deadline:
                return ("pair_not_full", "files de la paire pas pleines à temps")
            return "wait"
        return None

    def _refuse(self, g, rt, why):
        code, detail = why
        self._fail(g, rt, code, detail)
        if self._once(g["id"], "refused", code, detail, int(time.time() // 30)):
            self.alert("alert.sync.refused", {"g": g["name"], "e": detail})

    def _start_line(self, g, rt, line, t, speed, players):
        """Ouvre `line` à `t` et la met à l'antenne sur tous les canaux à un instant commun (vitesse
        `speed`, 0 = figée). Les têtes de la ligne d'avant restent jusqu'à constat de la bascule."""
        self._enter(g, players)
        # Place pour la nouvelle tête : on ne garde que la courante et la ligne suivante préparée (une
        # recherche répétée vite en ouvrirait une par geste, au-delà de ce que tient le conteneur).
        keep = [k for k in (rt.get("key"), (rt.get("next") or {}).get("key")) if k]
        for u in g["members"]:
            vm = self._vmid(g, u, players)
            if vm is not None:
                self.http("POST", vm, "/sync/drop", {"keep": keep})
        iss = pair_line_issues(g, line)
        if iss:                                   # refusé AVANT d'ouvrir quoi que ce soit
            a, b, code, d = iss[0]
            why = (code, "paire %s+%s : %s" % (letter(g["members"].index(a)), letter(g["members"].index(b)), d))
            self._refuse(g, rt, why)
            raise SyncError(code, why[1])
        key, errs = self._prepare(g, rt, line, t, players)
        durs = self._wait_ready(g, line, key, errs, players)
        # Durées COMPLÈTES des clips (pas depuis `t`) : c'est ce qu'une paire doit avoir d'égal.
        deadline = time.time() + PAIR_FULL_TIMEOUT_S
        while True:
            for u in g["members"]:
                self.observe(g["id"], u, players)
            why = self._gate(g, line, key, errs, durs, speed, players, deadline,
                             proven=proven_pairs(g, line, speed if speed > 0 else rt.get("speed_play", 1.0)))
            if why != "wait":
                break
            self.sleep(0.05)
        if why:
            self._refuse(g, rt, why)
            keep = [k for k in (rt.get("key"), (rt.get("next") or {}).get("key")) if k]
            for u in g["members"]:
                vm = self._vmid(g, u, players)
                if vm is not None:
                    self.http("POST", vm, "/sync/drop", {"keep": keep})
            raise SyncError(why[0], why[1])
        ready = [u for u in g["members"] if line["clips"].get(u) and u not in errs]
        if line["clips"] and not ready:
            for u, e in errs.items():
                self._refused(g, u, e, None, self._vmid(g, u, players))
            raise SyncError("no_channel_ready", "aucun canal n'a pu ouvrir la ligne")
        for u, e in errs.items():
            self._refused(g, u, e, None, self._vmid(g, u, players))
        self._cancel_next(g, rt, players)
        at = self._at(g)
        per = {u: self._body_for(g, line, u, key if u in ready else None, t, speed) for u in g["members"]
               if u in ready or not line["clips"].get(u)}
        cid = self._send(g, rt, at, "line", per, players)
        old_key = rt.get("key")
        if line["id"] != rt.get("line"):
            # Autre ligne : la « suivante » préparée était celle de l'ANCIENNE ligne — oubliée (ses têtes partent
            # avec le ménage qui suit le constat), la boucle préparera la suivante de celle-ci.
            rt["next"] = None
        rt["pair_defects"] = {}
        # Lecture EN ENTIER en cours ? Seulement depuis le début de la ligne ; une recherche ou un pas l'interrompt.
        rt["clean"] = {"line": line["id"], "ok": t == 0, "speed": speed if speed > 0 else rt.get("speed_play", 1.0)}
        rt.update(line=line["id"], key=key, durations=dict(durs), dur=line_duration(g, line, durs),
                  anchor={"g0": at, "t0": float(t), "speed": float(speed)}, cmd=cid, ended=False,
                  state="playing" if speed > 0 else ("cued" if t == 0 else "paused"))
        rt["drop_after"] = {"cid": cid, "keep": key, "old": old_key}
        return cid

    def _settle_switch(self, g, rt):
        """Une bascule de ligne datée imminente ne se court-circuite pas : on la laisse se faire (et la
        boucle la constater) avant de commander autre chose."""
        nx = rt.get("next") or {}
        if not nx.get("cid"):
            return
        now = self.group_now(g) or 0
        if nx["at"] - now < (g["lead_ms"] * 1e6 + 3 * self.period_ns(g)):
            t_end = time.time() + max(0.0, (nx["at"] - now) / 1e9) + 1.5
            while (rt.get("next") or {}).get("cid") == nx["cid"] and time.time() < t_end:
                self._rollover(g, rt, self.players(), force_poll=True)
                self.sleep(0.05)

    def _cancel_next(self, g, rt, players):
        nx = rt.get("next")
        if nx and nx.get("cid"):
            s = rt["sent"].get(nx["cid"])
            if s:
                s.update(checked=True, cancelled=True)     # annulée : ne pas la chercher au témoin
            for u in g["members"]:
                vm = self._vmid(g, u, players)
                if vm is not None:
                    self.http("POST", vm, "/sync/cancel", {"id": nx["cid"]})
            nx.update(cid=None, at=None)

    def op(self, gid, name, body=None, source="ui"):
        body = body or {}
        g = _need(gid)
        if not g["lines"] and name not in ("stop", "speed", "loop"):
            raise SyncError("no_lines", "la playlist est vide")
        with self.glock(g["id"]):
            rt = self._rt(g["id"])
            players = self.players()
            rt["failure"] = None
            st = rt["state"]
            if name in ("pause", "step", "seek", "speed", "play", "goto", "next", "prev", "cue"):
                self._settle_switch(g, rt)
            if name in ("step", "seek", "speed") and rt.get("clean"):
                rt["clean"]["ok"] = False           # la ligne n'est plus jouée EN ENTIER, à une même vitesse
            cur = self._line(g, rt.get("line") or g.get("cursor")) or g["lines"][0]
            if name == "play":
                if st in ("cued", "paused") and rt.get("anchor") and rt.get("key"):
                    at = self._at(g)
                    t = rt["anchor"]["t0"]
                    sp = rt["speed_play"]
                    per = {u: self._body_for(g, cur, u, None, t, sp) for u in g["members"]}
                    cid = self._send(g, rt, at, "play", per, players)
                    rt.update(anchor={"g0": at, "t0": t, "speed": sp}, state="playing", cmd=cid)
                elif st == "playing":
                    return self.view(g)
                else:
                    if rt.get("ended"):
                        cur = g["lines"][0]
                    self._start_line(g, rt, cur, 0.0, rt["speed_play"], players)
            elif name == "pause":
                if st != "playing":
                    return self.view(g)
                at = self._at(g)
                t = media_time(rt["anchor"], at)
                if rt.get("dur"):
                    t = min(t, rt["dur"])
                self._cancel_next(g, rt, players)
                per = {u: self._body_for(g, cur, u, None, t, 0.0) for u in g["members"]}
                cid = self._send(g, rt, at, "pause", per, players)
                rt.update(anchor={"g0": at, "t0": t, "speed": 0.0}, state="paused", cmd=cid)
            elif name == "stop":
                self.stop(g, reason="stop")
                return self.view(_need(gid))
            elif name == "cue":
                lid = str(body.get("line") or "") or cur["id"]
                ln = self._line(g, lid)
                if not ln:
                    raise SyncError("unknown_line", "ligne introuvable")
                self._start_line(g, rt, ln, 0.0, 0.0, players)
            elif name in ("goto", "next", "prev"):
                i = line_index(g["lines"], cur["id"])
                if name == "goto":
                    lid = str(body.get("line") or "")
                    if not lid and body.get("index") is not None:
                        k = int(body["index"]) - 1
                        if not 0 <= k < len(g["lines"]):
                            raise SyncError("unknown_line", "pas de ligne au rang %s" % body["index"])
                        lid = g["lines"][k]["id"]
                    ln = self._line(g, lid)
                else:
                    j = i + (1 if name == "next" else -1)
                    if j >= len(g["lines"]):
                        j = 0 if g.get("loop_mode") == "playlist" else None
                    if j is not None and j < 0:
                        j = 0
                    ln = g["lines"][j] if j is not None else None
                if not ln:
                    raise SyncError("unknown_line", "pas de ligne à cet endroit")
                sp = rt["speed_play"] if st == "playing" else 0.0
                self._start_line(g, rt, ln, 0.0, sp, players)
            elif name == "seek":
                t = _num(body.get("t"))
                if t is None:
                    raise SyncError("bad_time", "instant de recherche illisible")
                if rt.get("dur"):
                    t = min(t, rt["dur"])
                sp = rt["speed_play"] if st == "playing" else 0.0
                self._start_line(g, rt, cur, t, sp, players)
                if sp == 0.0:
                    rt["state"] = "paused"
            elif name == "step":
                n = int(body.get("frames") or 1)
                if st == "playing":
                    self.op(gid, "pause", source=source)
                    rt = self._rt(g["id"])
                if not rt.get("anchor"):
                    self._start_line(g, rt, cur, 0.0, 0.0, players)
                per_s = self.period_ns(g) / 1e9
                t = max(0.0, rt["anchor"]["t0"] + n * per_s)
                if rt.get("dur"):
                    t = min(t, rt["dur"])
                if 0 < n <= STEP_SAME_DECK_MAX:
                    at = self._at(g)
                    per = {u: self._body_for(g, cur, u, None, t, 0.0) for u in g["members"]}
                    cid = self._send(g, rt, at, "step", per, players)
                    rt.update(anchor={"g0": at, "t0": t, "speed": 0.0}, state="paused", cmd=cid)
                else:
                    self._start_line(g, rt, cur, t, 0.0, players)
                    rt["state"] = "paused"
            elif name == "speed":
                s = _num(body.get("speed"))
                if s is None or not SPEED_MIN <= s <= SPEED_MAX:
                    raise SyncError("bad_speed", "vitesse entre %g× et %g×" % (SPEED_MIN, SPEED_MAX))
                rt["speed_play"] = s
                if st == "playing":
                    at = self._at(g)
                    t = media_time(rt["anchor"], at)
                    self._cancel_next(g, rt, players)
                    per = {u: self._body_for(g, cur, u, None, t, s) for u in g["members"]}
                    cid = self._send(g, rt, at, "speed", per, players)
                    rt.update(anchor={"g0": at, "t0": t, "speed": s}, cmd=cid)
            elif name == "loop":
                mode = str(body.get("mode") or "")
                if mode not in LOOP_MODES:
                    raise SyncError("bad_loop", "bouclage inconnu : %s" % mode)
                update_group(g["id"], {"loop_mode": mode})
                g = _need(gid)
            else:
                raise SyncError("unknown_op", "opération inconnue : %s" % name)
            self._persist(g, rt)
            log.info("sync group %s: %s (%s) → %s, line %s", g["id"], name, source, rt["state"], rt.get("line"))
        self.kick()
        return self.view(_need(gid))

    def stop(self, g, reason="stop"):
        with self.glock(g["id"]):
            players = self.players()
            for u in g["members"]:
                vm = self._vmid(g, u, players)
                if vm is not None:
                    self.http("POST", vm, "/sync/leave", {"reason": "sync group %s" % reason})
                m = self._m(g["id"], u)
                m["left_seen"] = None
            line = self._rt(g["id"]).get("line")
            self.rt.pop(g["id"], None)
            rt = self._rt(g["id"])
            rt.update(line=line, recovered=False)
            self._persist(g, rt)

    def replan(self, gid):
        """La playlist ou le bouclage a changé : la ligne suivante préparée est peut-être périmée."""
        rt = self.rt.get(gid)
        g = get_group(gid)
        if not rt or not g:
            return
        with self.glock(gid):
            nx = rt.get("next")
            if nx and nx.get("line") != next_line_id(g, rt.get("line")):
                self._cancel_next(g, rt, self.players())
                rt["next"] = None
            cur = self._line(g, rt.get("line"))
            if cur and rt.get("durations") is not None:
                rt["dur"] = line_duration(g, cur, rt["durations"])
                if nx and nx.get("cid"):
                    self._cancel_next(g, rt, self.players())

    # ── boucle ─────────────────────────────────────────────────────────────────────────────────
    def tick(self):
        self.ticks += 1
        gs = self.groups()
        players = self.players()
        if time.time() - self.last_poll >= POLL_S:
            self.last_poll = time.time()
            self._poll(gs, players)
        alive = set()
        for g in gs:
            alive.add(g["id"])
            try:
                with self.glock(g["id"]):
                    self._group(g, players)
            except Exception as e:
                self.errors += 1
                self.last_error = "%s: %s" % (g.get("name"), e)
                log.exception("sync group %s: pass failed", g.get("id"))
        for gid in {k[0] for k in self.mem} - alive:
            self.forget(gid)

    def _group(self, g, players):
        rt = self._rt(g["id"])
        if rt.get("recovered") is None:
            self._recover(g, rt)
        if rt["state"] == "stopped":
            return
        self._check_sent(g, rt, players)
        self._check_members(g, rt, players)
        self._drop_old(g, rt, players)
        self._rollover(g, rt, players)
        if rt["state"] == "playing":
            self._plan_next(g, rt, players)

    def _recover(self, g, rt):
        """Au redémarrage : l'hypothèse de la base n'est reprise que si TOUS les canaux joignables la
        confirment (même ancre, tête présente). Sinon la lecture est considérée arrêtée — et on le dit."""
        tr = g.get("transport") or {}
        rt["recovered"] = False
        if tr.get("state") not in ("playing", "paused", "cued") or not tr.get("anchor") or not tr.get("key"):
            return
        line = self._line(g, tr.get("line"))
        ok = bool(line)
        for u in g["members"]:
            o = self._m(g["id"], u).get("obs")
            if not o:
                continue
            w = o.get("witness") or {}
            a = w.get("anchor") or {}
            clip = (line or {}).get("clips", {}).get(u)
            if not o.get("on") or (clip and tr["key"] not in (o.get("decks") or {})):
                ok = False
                break
            if clip and abs((a.get("speed") or 0) - tr["anchor"]["speed"]) > 1e-6:
                ok = False
                break
        if ok and any(self._m(g["id"], u).get("obs") for u in g["members"]):
            rt.update({k: tr.get(k) for k in ("state", "line", "key", "gen", "anchor", "speed_play")})
            rt["gen"] = int(rt.get("gen") or 0) + 100
            rt["speed_play"] = rt.get("speed_play") or 1.0
            durs = {}
            for u in g["members"]:
                d = self._deck(g, u, rt["key"]) or {}
                clip = line["clips"].get(u)
                if clip and d.get("file_duration"):
                    durs[u] = max(0.0, (clip.get("out") or d["file_duration"]) - (clip.get("in") or 0.0))
            rt["durations"], rt["dur"] = durs, line_duration(g, line, durs)
            rt["recovered"] = True
            log.info("sync group %s: playback state recovered from witnesses (%s, line %s)",
                     g["id"], rt["state"], rt["line"])

    def _check_sent(self, g, rt, players):
        """Chaque commande datée est CONSTATÉE au témoin : appliquée, à l'heure ou en retard — ou pas du tout."""
        now = self.group_now(g)
        if now is None:
            return
        per = self.period_ns(g)
        for cid, s in list(rt["sent"].items()):
            if s["checked"] or now < s["at"] + 2 * per:
                continue
            done = True
            for u, r in s["res"].items():
                if r != "ok":
                    continue
                o = self._m(g["id"], u).get("obs") or {}
                ap = next((a for a in ((o.get("witness") or {}).get("applied") or []) if a.get("id") == cid), None)
                i = g["members"].index(u)
                vm = self._vmid(g, u, players)
                if ap is None:
                    if now >= s["at"] + APPLY_GRACE_S * 1e9:
                        self._m(g["id"], u)["refused"] = {"why": "not_applied", "msg": "aucune trace au témoin",
                                                          "at": time.time()}
                        if self._once(g["id"], "not_applied", cid, u):
                            self.alert("alert.sync.not_applied", {"g": g["name"], "c": letter(i),
                                                                  "e": "aucune trace au témoin"}, vmid=vm)
                            self._fail(g, rt, "not_applied", "aucune trace au témoin", u)
                    else:
                        done = False
                elif ap.get("late_slots"):
                    if self._once(g["id"], "late", cid, u):
                        self.alert("alert.sync.start_late", {"g": g["name"], "c": letter(i), "n": ap["late_slots"]},
                                   vmid=vm)
                        self._fail(g, rt, "late", "%d" % ap["late_slots"], u)
            s["checked"] = done

    def _check_members(self, g, rt, players):
        """Canal sorti du mode synchrone (geste d'opérateur), retard d'image, écart au-delà d'une image."""
        per_ms = self.period_ns(g) / 1e6
        chans = self.channels(g)
        for c in chans:
            m = self._m(g["id"], c["uuid"])
            o = m.get("obs") or {}
            if o and not o.get("on") and o.get("left") and o["left"] != m.get("left_seen"):
                m["left_seen"] = o["left"]
                self.alert("alert.sync.left", {"g": g["name"], "c": c["letter"], "r": o["left"].get("reason")},
                           vmid=c.get("vmid"))
                self._fail(g, rt, "left", o["left"].get("reason"), c["uuid"])
            # PAIRE fill / key : AUCUNE image répétée tolérée, ni une cadence d'images neuves sous celle du flux.
            # En lecture, un défaut est SIGNALÉ (alerte, paire « en défaut ») mais la lecture n'est pas coupée —
            # couper l'antenne d'office serait pire ; la ligne suivante, elle, repasse le contrôle de départ.
            pu = pair_of(g, c["uuid"])
            if pu and rt["state"] == "playing":
                pk = "+".join(sorted([c["letter"], letter(g["members"].index(pu))]))
                if m.get("pair_late"):
                    self._pair_defect(g, rt, pk, "canal %s : %d image(s) répétée(s)" % (c["letter"], m["pair_late"]),
                                      c.get("vmid"))
                    m["pair_late"] = 0
                a = rt.get("anchor") or {}
                ff, fmt = o.get("fresh_fps"), o.get("format") or {}
                fps = (int(fmt["fps_num"]) / float(fmt["fps_den"] or 1)) if fmt.get("fps_num") else None
                started = (self.group_now(g) or 0) - a.get("g0", 0) > PAIR_FRESH_GRACE_S * 1e9
                if (ff is not None and fps and started and a.get("speed", 0) >= 1.0 and not c.get("eos")
                        and ff < fps - 0.5):
                    m["fresh_low"] = m.get("fresh_low", 0) + 1
                    if m["fresh_low"] >= 2:
                        self._pair_defect(g, rt, pk, "canal %s : %.1f images neuves/s pour %.0f" % (c["letter"], ff, fps),
                                          c.get("vmid"))
                else:
                    m["fresh_low"] = 0
            # (b) DÉSYNCHRO PASSAGÈRE : images répétées (décodeur en retard) puis rattrapées. Une seule image
            # suffit (tolérance zéro) ; le compte n'est jamais perdu — il s'accumule entre deux alertes.
            if m["late_unalerted"] and rt["state"] != "stopped":
                if self._once(g["id"], "lag", c["uuid"], int(time.time() // 10)):
                    self.alert("alert.sync.lag", {"g": g["name"], "c": c["letter"], "n": m["late_unalerted"]},
                               vmid=c.get("vmid"))
                    rt["transient"] = {"at": time.time(), "channel": c["letter"], "frames": m["late_unalerted"]}
                    self._fail(g, rt, "transient", "%d" % m["late_unalerted"], c["uuid"])
                    m["late_unalerted"] = 0
        if rt["state"] != "playing":
            rt["skew_over"] = 0
            return
        v = sync_verdict([c for c in chans if c["status"] not in _NO_CLOCK], per_ms)
        rt["verdict"] = v
        # (a) DÉCALAGE QUI RESTE : constaté sur deux relevés de suite (un retard d'image vu une seule fois est
        # une désynchro passagère, déjà comptée image par image ci-dessus).
        # Une alerte par ÉPISODE : l'épisode se clôt quand le défaut disparaît (un nouveau décalage, même dans
        # la minute, est un autre fait et se signale).
        rt["skew_over"] = (rt.get("skew_over", 0) + 1) if v["defect"] else 0
        if not v["defect"]:
            rt["episode"] = rt.get("episode", 0) + (1 if rt.get("episode_open") else 0)
            rt["episode_open"] = False
        if rt["skew_over"] >= SKEW_PERSIST:
            rt["episode_open"] = True
            ep = rt.get("episode", 0)
            if v["clock"] and self._once(g["id"], "skew", ep):
                self.alert("alert.sync.skew", {"g": g["name"], "ms": round(v["clock_ms"], 1),
                                               "f": round(v["clock_ms"] / per_ms, 2)})
            if (v["frames"] or v["anchors"]) and self._once(g["id"], "offset", ep):
                d = ", ".join(v["anchors"]) or ", ".join("%s %+d" % (c["letter"], c["lag"]) for c in chans if c["lag"])
                self.alert("alert.sync.offset", {"g": g["name"], "f": max(v["frames"], 1 if v["anchors"] else 0),
                                                 "d": d})
                self._fail(g, rt, "offset", d)

    def _pair_defect(self, g, rt, pk, detail, vmid=None):
        # Le constat « déjà jouée sans défaut » de cette ligne se PERD, et une bascule accordée d'avance sur la
        # foi de cette lecture est annulée (la ligne suivante repassera le contrôle de départ, mesure comprise).
        if (rt.get("clean") or {}).get("ok"):
            rt["clean"]["ok"] = False
        self._forget_proof(g, rt.get("line"), pk)
        nx = rt.get("next") or {}
        if pk in (nx.get("provisional") or []):
            self._cancel_next(g, rt, self.players())
            nx.update(provisional=None, blocked=None)
            rt["next"] = None
        rt.setdefault("pair_defects", {}).setdefault(pk, [])
        if detail not in rt["pair_defects"][pk]:
            rt["pair_defects"][pk].append(detail)
            rt["pair_defects"][pk] = rt["pair_defects"][pk][-6:]
        if self._once(g["id"], "pair", pk, detail, int(time.time() // 10)):
            self.alert("alert.sync.pair", {"g": g["name"], "p": pk, "e": detail}, vmid=vmid)
            self._fail(g, rt, "pair", "%s — %s" % (pk, detail))

    def _forget_proof(self, g, lid, pk):
        with _lock:
            cur = get_group(g["id"])
            i = line_index(cur["lines"], lid) if cur else -1
            if i >= 0 and pk in (cur["lines"][i].get("proven") or {}):
                cur["lines"][i]["proven"].pop(pk, None)
                save_group(cur)
                log.info("sync group %s: line %s no longer proven for pair %s", g["id"], lid, pk)

    def _record_proof(self, g, rt):
        """Fin NATURELLE d'une ligne jouée en entier sans défaut de paire : constat enregistré pour chaque paire."""
        cl = rt.get("clean") or {}
        if not cl.get("ok") or cl.get("line") != rt.get("line") or not g.get("pairs"):
            return
        with _lock:
            cur = get_group(g["id"])
            i = line_index(cur["lines"], cl["line"]) if cur else -1
            if i < 0:
                return
            ln = cur["lines"][i]
            done = []
            for a, b in cur.get("pairs") or []:
                if (ln.get("clips") or {}).get(a) and (ln.get("clips") or {}).get(b):
                    pk = pair_key(cur, a, b)
                    ln.setdefault("proven", {})[pk] = pair_sig(ln, a, b, cl["speed"])
                    done.append(pk)
            if done:
                save_group(cur)
                log.info("sync group %s: line %s proven (played in full, no defect) for %s", g["id"], cl["line"], done)

    def _drop_old(self, g, rt, players):
        """Têtes de la ligne d'avant retirées une fois la bascule CONSTATÉE sur chaque canal."""
        da = rt.get("drop_after")
        if not da:
            return
        s = rt["sent"].get(da["cid"])
        if s and not s["checked"]:
            return
        keep = [k for k in (rt.get("key"), (rt.get("next") or {}).get("key")) if k]
        for u in g["members"]:
            vm = self._vmid(g, u, players)
            if vm is not None:
                self.http("POST", vm, "/sync/drop", {"keep": keep})
        rt["drop_after"] = None

    def _plan_next(self, g, rt, players):
        """Ligne suivante : têtes ouvertes à l'avance, puis bascule DATÉE envoyée à chaque canal dès que tout
        est prêt — à l'instant de fin de la ligne courante, selon la règle de fin du groupe."""
        nid = next_line_id(g, rt.get("line"))
        anchor = rt.get("anchor") or {}
        if nid is None or anchor.get("speed", 0) <= 0:
            return
        line = self._line(g, nid)
        nx = rt.get("next")
        if not nx or nx.get("line") != nid:
            self._enter(g, players)
            # Toute tête autre que la courante est périmée à cet instant (suivante d'une autre ligne, reste
            # d'avant un redémarrage) : place faite AVANT d'ouvrir la nouvelle, sinon le player la refuse.
            for u in g["members"]:
                vm = self._vmid(g, u, players)
                if vm is not None and rt.get("key"):
                    self.http("POST", vm, "/sync/drop", {"keep": [rt["key"]]})
            key, errs = self._prepare(g, rt, line, 0.0, players)
            rt["next"] = nx = {"line": nid, "key": key, "errs": errs, "since": time.time(), "cid": None, "at": None,
                               "durations": {}}
            for u, e in errs.items():
                self._refused(g, u, e, None, self._vmid(g, u, players))
        if nx.get("cid"):
            return
        want = [u for u in g["members"] if line["clips"].get(u) and u not in nx["errs"]]
        ready = []
        for u in want:
            d = self._deck(g, u, nx["key"]) or {}
            if not d and time.time() - nx["since"] > 2.0 and u not in nx.setdefault("reprepared", []):
                # Tête annoncée mais absente du témoin (player redémarré, têtes retirées) : on la redemande
                # une fois pour ce canal, sous la MÊME clé — la bascule datée la désignera.
                nx["reprepared"].append(u)
                clip = line["clips"][u]
                vm = self._vmid(g, u, players)
                if vm is not None:
                    self.http("POST", vm, "/sync/prepare", {"key": nx["key"], "file": clip["file"], "asset": clip.get("asset"),
                                                            "title": clip.get("title"), "in": clip.get("in") or 0.0,
                                                            "out": clip.get("out")})
                    log.warning("sync group %s: deck %s missing on channel %s, prepared again", g["id"], nx["key"],
                                letter(g["members"].index(u)))
            if d.get("state") == "ready":
                ready.append(u)
                clip = line["clips"][u]
                full = clip.get("out") or d.get("file_duration")
                nx["durations"][u] = round(max(0.0, full - (clip.get("in") or 0.0)), 4) if full else None
            elif d.get("state") == "error":
                nx["errs"][u] = d.get("error") or "error"
                self._refused(g, u, nx["errs"][u], None, self._vmid(g, u, players))
        timed_out = time.time() - nx["since"] > PREPARE_TIMEOUT_S
        if len(ready) < len([u for u in want if u not in nx["errs"]]) and not timed_out:
            return
        if nx.get("blocked"):
            return
        proven = proven_pairs(g, line, anchor["speed"])
        cl = rt.get("clean") or {}
        if (nid == rt.get("line") and cl.get("ok") and cl.get("line") == nid
                and abs(cl.get("speed", 0) - anchor["speed"]) < 1e-6):
            # Boucle sur la ligne qui joue : si CETTE lecture va au bout sans défaut, elle vaudra constat.
            # Accordé d'avance ; un défaut de paire avant la fin annule la bascule (cf. _pair_defect).
            prov = {pair_key(g, a, b) for a, b in g.get("pairs") or []} - proven
            if prov:
                nx["provisional"] = sorted(prov)
            proven = proven | prov
        why = self._gate(g, line, nx["key"], nx["errs"], nx["durations"], anchor["speed"], players,
                         deadline=nx["since"] + PREPARE_TIMEOUT_S + PAIR_FULL_TIMEOUT_S, proven=proven)
        if why == "wait":
            return
        if why:
            # Ligne suivante REFUSÉE (paire non conforme, ou canal fermé avec « refuser le départ ») : la bascule
            # n'est pas datée — la ligne courante va au bout et TIENT sa dernière image sur tous les canaux,
            # ensemble ; corriger la ligne (ou ses médias) relance la préparation.
            nx["blocked"] = why
            self._refuse(g, rt, why)
            return
        now = self.group_now(g)
        if now is None:
            return
        per = self.period_ns(g)
        if rt.get("dur"):
            end = anchor["g0"] + (rt["dur"] - anchor["t0"]) / anchor["speed"] * 1e9
            at = snap_up(end, per)
        else:
            # Durée inconnue : fin CONSTATÉE — tous les canaux qui jouent ont fini (eos au témoin).
            playing = [u for u in g["members"] if (self._line(g, rt["line"]) or {}).get("clips", {}).get(u)]
            if not playing or not all(((self._m(g["id"], u).get("obs") or {}).get("witness") or {}).get("eos")
                                      for u in playing):
                return
            at = None
        min_at = snap_up(now + g["lead_ms"] * 1e6, per)
        if at is not None and at - now > SCHEDULE_HORIZON_S * 1e9:
            # Trop loin (ligne longue, ralenti) : datée plus tard. Le player refuserait un instant à plus de
            # 60 s comme une horloge en désaccord — mesuré : ligne de 40 s à 0,25× → bascule à +97 s, refusée.
            nx["planned_at"] = at
            return
        if at is None or at < min_at:
            if at is not None and self._once(g["id"], "next_late", nx["key"]):
                self.alert("alert.sync.next_late", {"g": g["name"], "l": line_label(line, g["members"]),
                                                    "ms": round((min_at - at) / 1e6)})
            at = min_at
        per_m = {u: self._body_for(g, line, u, nx["key"] if u in ready else None, 0.0, anchor["speed"])
                 for u in g["members"] if u in ready or not line["clips"].get(u)}
        nx["cid"] = self._send(g, rt, at, "next_line", per_m, players)
        nx["at"] = at
        log.info("sync group %s: line %s scheduled at TAI %d (in %.2f s)", g["id"], nid, at, (at - now) / 1e9)

    def _rollover(self, g, rt, players, force_poll=False):
        """La bascule datée est passée : constatée au témoin, le modèle suit (ligne, ancre, durée)."""
        nx = rt.get("next")
        if not nx or not nx.get("cid"):
            if rt["state"] == "playing" and rt.get("dur") and next_line_id(g, rt.get("line")) is None:
                now = self.group_now(g)
                if now is not None and media_time(rt["anchor"], now) >= rt["dur"] and not rt.get("ended"):
                    rt["ended"] = True
                    self._record_proof(g, rt)
                    log.info("sync group %s: end of playlist", g["id"])
            return
        now = self.group_now(g)
        if now is None or now < nx["at"] + self.period_ns(g):
            return
        if force_poll:
            for u in g["members"]:
                self.observe(g["id"], u, players)
        line = self._line(g, nx["line"])
        self._record_proof(g, rt)
        rt["clean"] = {"line": nx["line"], "ok": True, "speed": rt["anchor"]["speed"]}
        rt["pair_defects"] = {}
        rt.update(line=nx["line"], key=nx["key"], durations=dict(nx["durations"]),
                  dur=line_duration(g, line, nx["durations"]) if line else None,
                  anchor={"g0": nx["at"], "t0": 0.0, "speed": rt["anchor"]["speed"]}, cmd=nx["cid"], next=None)
        rt["drop_after"] = {"cid": nx["cid"], "keep": nx["key"]}
        self._persist(g, rt)
        log.info("sync group %s: now on line %s", g["id"], rt["line"])

    # ── vue ────────────────────────────────────────────────────────────────────────────────────
    def channels(self, g):
        """Un canal par membre : état observé, écart d'horloge mesuré, retard d'image, écart au groupe."""
        rt = self.rt.get(g["id"]) or {}
        players = self.players()
        cons = self.consensus(g, players)
        per_ms = self.period_ns(g) / 1e6
        line = self._line(g, rt.get("line"))
        out = []
        for i, u in enumerate(g["members"]):
            m = self._m(g["id"], u)
            c = players.get(u) or {}
            o = m.get("obs") or {}
            w = o.get("witness") or {}
            est = self.clock_of(g["id"], u, g, players)
            clock_ms = round((est[0] - cons) / 1e6, 3) if (est and cons is not None) else None
            late_cum = int(w.get("late_slots") or 0)
            recent = sum(n for t, n in m["late_recent"] if time.time() - t < 10)
            ref = m.get("refused")
            clip = (line or {}).get("clips", {}).get(u)
            deck = (o.get("decks") or {}).get(rt.get("key")) if rt.get("key") else None
            if not c:
                status = "missing"
            elif not m["reachable"]:
                status = "offline"
            elif not o.get("capable"):
                status = "no_element"
            elif rt.get("state", "stopped") == "stopped":
                status = "idle"
            elif not o.get("on"):
                status = "left"
            elif ref:
                status = "fetching" if ref["why"] == "fetching" else "error"
            elif clip and deck and deck.get("state") == "error":
                status = "error"
            elif clip and (not deck or deck.get("state") != "ready"):
                status = "preparing"
            elif (w.get("lag") or 0) > 0 or recent:
                status = "late"
            else:
                status = "ok"
            if clock_ms is not None and abs(clock_ms) > per_ms and status in ("ok", "late"):
                status = "clock"
            out.append({
                "uuid": u, "letter": letter(i), "vmid": c.get("vmid"), "hostname": c.get("hostname"),
                "pair": letter(g["members"].index(pair_of(g, u))) if pair_of(g, u) else None,
                "fresh_fps": o.get("fresh_fps"),
                "node_id": c.get("node_id"), "status": status,
                "error": (ref or {}).get("msg") or (deck or {}).get("error") or m.get("error"),
                "clock_ms": clock_ms, "clock_unc_ms": round(est[1] / 1e6, 3) if est else None,
                "lag": int(w.get("lag") or 0), "late_total": late_cum, "late_recent": recent,
                "skew_ms": (round(clock_ms + int(w.get("lag") or 0) * per_ms, 3) if clock_ms is not None else None),
                # Ancre en vigueur RAMENÉE à la ligne (temps de ligne = temps média − point In du clip) : égale
                # sur tous les canaux quand ils jouent la même chose au même instant.
                "anchor_rel": ((int(w["anchor"]["g0"]), round(float(w["anchor"]["m0"]) - ((clip or {}).get("in") or 0.0), 4),
                                round(float(w["anchor"]["speed"]), 4))
                               if clip and isinstance(w.get("anchor"), dict) and w["anchor"].get("g0") is not None else None),
                "position": w.get("pts"), "eos": bool(w.get("eos")), "black": bool(w.get("black")),
                "clip": clip, "title": (clip or {}).get("title") or ((clip or {}).get("file") or "").rsplit("/", 1)[-1] or None,
                "duration": (rt.get("durations") or {}).get(u),
                "left": o.get("left") if not o.get("on") else None,
                "fetch": m.get("fetch"), "plugin_version": o.get("plugin_version")})
        return out

    def view(self, g):
        rt = self.rt.get(g["id"]) or self._rt(g["id"])
        chans = self.channels(g)
        per_ms = self.period_ns(g) / 1e6
        verdict = sync_verdict([c for c in chans if c["status"] not in _NO_CLOCK], per_ms)
        skew, unc = verdict["skew_ms"], verdict["skew_unc_ms"]
        if rt.get("state", "stopped") != "stopped":
            lo = min([c["lag"] for c in chans if c["status"] not in _NO_CLOCK] or [0])
            for c in chans:
                if c["letter"] in verdict["anchors"] or (c["status"] not in _NO_CLOCK and c["lag"] > lo):
                    c["status"] = "offset"
        now = self.group_now(g)
        a = rt.get("anchor")
        t = media_time(a, now) if (a and now is not None) else None
        if t is not None and now < a["g0"]:
            t = a["t0"]                 # commande datée pas encore appliquée : on montre le temps de départ
        if t is not None and rt.get("dur"):
            t = min(t, rt["dur"])
        idx = {u: letter(i) for i, u in enumerate(g["members"])}
        cur, nx = rt.get("line") or g.get("cursor"), (rt.get("next") or {})
        lines = []
        ci = line_index(g["lines"], cur)
        for i, ln in enumerate(g["lines"]):
            state = ("current" if i == ci else "next" if ln["id"] == nx.get("line") and i != ci
                     else "played" if 0 <= i < ci else "queued")
            lines.append({"id": ln["id"], "title": ln.get("title"), "label": line_label(ln, g["members"]),
                          "proven": sorted(proven_pairs(g, ln, rt.get("speed_play", 1.0))),
                          "duration": ln.get("duration"), "state": state,
                          "clips": {idx[u]: dict(c, uuid=u) for u, c in ln["clips"].items() if u in idx}})
        faulty = [c["letter"] for c in chans if c["status"] in ("offline", "missing", "left", "error", "no_element", "offset",
                                                                  "clock", "late")
                  and rt.get("state", "stopped") != "stopped"]
        return {"id": g["id"], "name": g["name"], "project_id": g.get("project_id"), "members": g["members"],
                "end_policy": g["end_policy"], "loop_mode": g["loop_mode"], "lead_ms": g["lead_ms"],
                "state": rt.get("state", "stopped"), "ended": bool(rt.get("ended")),
                "line": cur, "line_index": ci, "line_time": round(t, 3) if t is not None else None,
                "line_duration": rt.get("dur"), "speed": (a or {}).get("speed"), "speed_play": rt.get("speed_play", 1.0),
                "next": ({"line": nx.get("line"), "scheduled": bool(nx.get("cid")), "at": nx.get("at"),
                          "in_s": round((nx["at"] - now) / 1e9, 2) if (nx.get("at") and now) else None}
                         if nx else None),
                "channels": chans, "lines": lines, "skew_ms": skew, "skew_unc_ms": unc,
                "verdict": verdict if rt.get("state", "stopped") != "stopped" else None,
                "open_fail": g.get("open_fail") or "proceed",
                "pairs": self.pairs_view(g, chans, rt),
                "blocked": (list((rt.get("next") or {}).get("blocked") or []) or None),
                "transient": rt.get("transient"),
                "period_ms": round(per_ms, 3), "faulty": faulty, "failure": rt.get("failure"),
                "recovered": rt.get("recovered"), "saved_lists": sorted((g.get("saved_lists") or {}).keys())}

    def pairs_view(self, g, chans, rt):
        """Paires fill / key : lettres, état (ok | defect | idle) et défauts constatés (repli : aucun)."""
        by = {c["uuid"]: c for c in chans}
        out = []
        for a, b in g.get("pairs") or []:
            ca, cb = by.get(a) or {}, by.get(b) or {}
            key = pair_key(g, a, b)
            iss = list((rt.get("pair_defects") or {}).get(key) or [])
            cur = self._line(g, rt.get("line"))
            if cur:
                iss += ["%s" % d for (x, y, _c, d) in pair_line_issues(g, cur) if {x, y} == {a, b}]
            if ca.get("lag", 0) != cb.get("lag", 0):
                iss.append("retard d'image différent (%s / %s)" % (ca.get("lag"), cb.get("lag")))
            state = "idle" if rt.get("state", "stopped") == "stopped" else ("defect" if iss else "ok")
            out.append({"a": ca.get("letter"), "b": cb.get("letter"), "key": key, "state": state, "issues": iss,
                        "proven": bool(cur) and key in proven_pairs(g, cur, rt.get("speed_play", 1.0)),
                        "provisional": key in ((rt.get("next") or {}).get("provisional") or [])})
        return out

    def status(self):
        return {"ticks": self.ticks, "errors": self.errors, "last_error": self.last_error,
                "running": _thread is not None and _thread.is_alive()}


ENGINE = Engine()
_thread = None
_stop = threading.Event()


def _loop():
    while not _stop.is_set():
        try:
            ENGINE.tick()
        except Exception as e:
            ENGINE.errors += 1
            ENGINE.last_error = "%s: %s" % (type(e).__name__, e)
            log.exception("sync groups: pass failed")
        ENGINE.wake.wait(TICK_S)
        ENGINE.wake.clear()


def start():
    """Lance la boucle (contrôleur ACTIF seul, cf. main.py). Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="sync-groups")
    _thread.start()
    log.info("sync groups: engine started (tick %.2f s, witness poll %.1f s)", TICK_S, POLL_S)


def stop():
    _stop.set()


def op(gid, name, body=None, source="ui"):
    return ENGINE.op(gid, name, body, source)


def group_view(gid):
    return ENGINE.view(_need(gid))


# ═══ MACROS (fournisseur du cœur, app/core_plugins.py:CORE_PROVIDERS) ══════════════════════════
# Libellés en français dans le code, comme les autres fournisseurs du cœur : le catalogue des macros
# n'est pas encore traduit.
_GROUP = {"key": "group", "label": "Groupe synchrone", "type": "select", "options_hook": True}
_STATIC_ACTIONS = [
    {"id": "play", "label": "Lecture synchrone : Play (tous les canaux)", "availability": "project", "params": [_GROUP]},
    {"id": "pause", "label": "Lecture synchrone : Pause (tous les canaux)", "availability": "project", "params": [_GROUP]},
    {"id": "stop", "label": "Lecture synchrone : arrêt (les players reprennent leur liberté)", "availability": "project",
     "params": [_GROUP]},
    {"id": "cue", "label": "Lecture synchrone : Cue d'une ligne (1re image, figée)", "availability": "project",
     "params": [_GROUP, {"key": "index", "label": "Rang de la ligne (vide = la ligne courante)", "type": "number",
                         "min": 1}]},
    {"id": "goto", "label": "Lecture synchrone : aller à une ligne", "availability": "project",
     "params": [_GROUP, {"key": "index", "label": "Rang de la ligne (1 = la première)", "type": "number",
                         "default": "1", "min": 1}]},
    {"id": "next", "label": "Lecture synchrone : ligne suivante", "availability": "project", "params": [_GROUP]},
    {"id": "prev", "label": "Lecture synchrone : ligne précédente", "availability": "project", "params": [_GROUP]},
    {"id": "seek", "label": "Lecture synchrone : aller à un timecode de la ligne", "availability": "project",
     "params": [_GROUP, {"key": "tc", "label": "Timecode (hh:mm:ss:ii) ou secondes", "type": "text", "default": "00:00:00:00"}]},
    {"id": "step", "label": "Lecture synchrone : pas à pas", "availability": "project",
     "params": [_GROUP, {"key": "frames", "label": "Images (négatif = en arrière)", "type": "number", "default": "1"}]},
    {"id": "speed", "label": "Lecture synchrone : vitesse", "availability": "project",
     "params": [_GROUP, {"key": "speed", "label": "Vitesse (0,25 à 4)", "type": "number", "default": "1",
                         "min": SPEED_MIN, "max": SPEED_MAX}]},
    {"id": "loop", "label": "Lecture synchrone : bouclage", "availability": "project",
     "params": [_GROUP, {"key": "mode", "label": "Bouclage", "type": "select", "default": "off",
                         "options": [{"value": "off", "label": "Aucun"}, {"value": "line", "label": "La ligne courante"},
                                     {"value": "playlist", "label": "Toute la playlist"}]}]},
    {"id": "end_policy", "label": "Lecture synchrone : fin de ligne", "availability": "project",
     "params": [_GROUP, {"key": "policy", "label": "Fin de ligne", "type": "select", "default": "longest",
                         "options": [{"value": "longest", "label": "À la fin du plus long"},
                                     {"value": "shortest", "label": "À la fin du plus court"},
                                     {"value": "duration", "label": "À la durée de la ligne"}]}]},
    {"id": "load_playlist", "label": "Lecture synchrone : charger une playlist", "availability": "project",
     "params": [_GROUP, {"key": "name", "label": "Playlist", "type": "select", "options_hook": True},
                {"key": "mode", "label": "Mode", "type": "select", "default": "replace",
                 "options": [{"value": "replace", "label": "Remplacer"}, {"value": "append", "label": "Ajouter à la fin"}]}]},
]
_STATES = ["playing", "paused", "cued", "stopped"]
_GROUP_STATES = [("state", "état (playing, paused, cued, stopped)", "enum", _STATES),
                 ("line", "ligne courante (rang)", "number", None),
                 ("line_title", "ligne courante (titre)", "text", None),
                 ("time", "temps dans la ligne (s)", "number", None),
                 ("skew_ms", "écart mesuré entre canaux (ms)", "number", None),
                 ("faulty", "canaux en défaut (lettres)", "text", None),
                 ("in_sync", "tous les canaux sur la même image (tolérance zéro)", "bool", None),
                 ("frames_offset", "écart en images entre canaux (0 attendu)", "number", None),
                 ("speed", "vitesse", "number", None),
                 ("loop", "bouclage (off, line, playlist)", "text", None),
                 ("ended", "fin de playlist atteinte", "bool", None),
                 ("pairs_ok", "paires fill / key conformes (aucune en défaut)", "bool", None),
                 ("pair_issue", "défaut de paire fill / key (texte, vide si aucun)", "text", None),
                 ("blocked", "ligne suivante refusée (raison)", "text", None),
                 ("line_proven", "ligne courante déjà jouée en entier sans défaut par toutes ses paires", "bool", None)]


def _states():
    out = []
    try:
        for g in list_groups():
            for kind, lab, typ, opts in _GROUP_STATES:
                s = {"id": "%s:%d" % (kind, g["id"]), "label": "%s : %s" % (g["name"], lab), "type": typ}
                if opts:
                    s["options"] = opts
                out.append(s)
    except Exception:
        log.exception("sync groups: macro states listing failed")
    return out


class _Provider(dict):
    def get(self, k, default=None):
        return _states() if k == "states" else super().get(k, default)

    def __getitem__(self, k):
        return _states() if k == "states" else super().__getitem__(k)


MACRO_PROVIDER = _Provider({"id": "sync_group", "label": "Lecture synchrone", "actions": _STATIC_ACTIONS})


def macro_available():
    from . import plugins
    return bool(plugins.get(PLAYER_TYPE))


def action_options(action_id, key, pid=None):
    gs = list_groups(pid)
    if key == "group":
        return [{"value": str(g["id"]), "label": g["name"]} for g in gs]
    if action_id == "load_playlist" and key == "name":
        names = sorted({n for g in gs for n in (g.get("saved_lists") or {})})
        return [{"value": n, "label": n} for n in names]
    return []


def parse_tc(v, fps):
    """« hh:mm:ss:ii » (ou « hh:mm:ss.s », ou des secondes) → secondes."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        if ":" not in s:
            return max(0.0, float(s.replace(",", ".")))
        p = s.split(":")
        if len(p) == 4:
            h, m, sec, fr = (int(x) for x in p)
            return h * 3600 + m * 60 + sec + fr / float(fps or 25)
        if len(p) == 3:
            return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2].replace(",", "."))
        if len(p) == 2:
            return int(p[0]) * 60 + float(p[1].replace(",", "."))
    except ValueError:
        return None
    return None


def _macro_group(p, pid):
    g = get_group(str(p.get("group") or "").strip() or None)
    if not g or (pid is not None and g.get("project_id") != pid):
        raise RuntimeError("groupe synchrone introuvable dans ce projet")
    return g


def run_action(action_id, params, ctx):
    pid = (ctx or {}).get("project_id")
    p = params or {}
    try:
        g = _macro_group(p, pid)
        if action_id in ("play", "pause", "stop", "next", "prev"):
            op(g["id"], action_id, source="macro")
        elif action_id in ("goto", "cue"):
            body = {}
            if str(p.get("index") or "").strip():
                k = int(float(p["index"]))
                if not 1 <= k <= len(g["lines"]):
                    raise SyncError("unknown_line", "pas de ligne au rang %d" % k)
                body["line"] = g["lines"][k - 1]["id"]
            op(g["id"], action_id, body, source="macro")
        elif action_id == "seek":
            fps = 1e3 / (ENGINE.period_ns(g) / 1e6)
            t = parse_tc(p.get("tc"), fps)
            if t is None:
                raise SyncError("bad_time", "timecode illisible : %s" % p.get("tc"))
            op(g["id"], "seek", {"t": t}, source="macro")
        elif action_id == "step":
            op(g["id"], "step", {"frames": int(float(p.get("frames") or 1))}, source="macro")
        elif action_id == "speed":
            op(g["id"], "speed", {"speed": float(str(p.get("speed") or 1).replace(",", "."))}, source="macro")
        elif action_id == "loop":
            op(g["id"], "loop", {"mode": str(p.get("mode") or "off")}, source="macro")
        elif action_id == "end_policy":
            update_group(g["id"], {"end_policy": str(p.get("policy") or "longest")})
            ENGINE.replan(g["id"])
        elif action_id == "load_playlist":
            lines_op(g["id"], "load", {"name": str(p.get("name") or ""), "mode": str(p.get("mode") or "replace")})
        else:
            raise RuntimeError("action inconnue : %s" % action_id)
        return {"ok": True}
    except SyncError as e:
        raise RuntimeError(str(e))


def read_state(state_id, ctx):
    parts = str(state_id).split(":")
    kind = parts[0]
    g = get_group(parts[1] if len(parts) > 1 else None)
    pid = (ctx or {}).get("project_id")
    if not g or (pid is not None and g.get("project_id") != pid):
        return None
    v = ENGINE.view(g)
    if kind == "state":
        return v["state"]
    if kind == "line":
        return v["line_index"] + 1 if v["line_index"] >= 0 else None
    if kind == "line_title":
        i = v["line_index"]
        return v["lines"][i]["label"] if 0 <= i < len(v["lines"]) else None
    if kind == "time":
        return v["line_time"]
    if kind == "skew_ms":
        return v["skew_ms"]
    if kind == "faulty":
        return ",".join(v["faulty"]) or None
    if kind == "line_proven":
        i = v["line_index"]
        if not 0 <= i < len(v["lines"]):
            return None
        need = {p["key"] for p in v["pairs"]}
        return bool(need) and need <= set(v["lines"][i]["proven"])
    if kind == "pairs_ok":
        return all(p["state"] != "defect" for p in v["pairs"])
    if kind == "pair_issue":
        return " · ".join("%s : %s" % (p["key"], "; ".join(p["issues"])) for p in v["pairs"] if p["issues"]) or None
    if kind == "blocked":
        return (v.get("blocked") or [None, None])[1]
    if kind == "frames_offset":
        return (v.get("verdict") or {}).get("frames", 0)
    if kind == "in_sync":
        return (v["state"] != "stopped" and not v["faulty"] and not (v.get("verdict") or {}).get("defect")
                and all(p["state"] != "defect" for p in v["pairs"]))
    if kind == "speed":
        return v["speed"]
    if kind == "loop":
        return v["loop_mode"]
    if kind == "ended":
        return v["ended"]
    return None
