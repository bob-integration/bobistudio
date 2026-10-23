# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""GROUPES DE LECTURE (A/B/C…) — des players ORDINAIRES réunis en N sorties sur UNE playlist.

★ CE QUE C'EST. Un player = un conteneur = une sortie. Un groupe en réunit N ≥ 2 (A, B, C… = leur ordre)
et leur distribue une seule playlist TOUR À TOUR : pendant qu'une sortie est à l'antenne, les N−1 autres
ont déjà chargé l'élément qui leur revient et attendent sur sa première image (CUE du player). Aucune
option dans le player, aucun second plugin : la coordination vit ici, et parle aux players par leurs
endpoints existants (`/file` + `cue`, `/play`, `/pause`, `/queue`).

★ DEUX COMMANDES PAR SORTIE (interface, macros, mélangeur) :
  · PLAY lance l'élément déjà cué sur cette sortie. Il ne charge rien : une sortie qui n'a pas son élément
    calé refuse, et le refus est SIGNALÉ ;
  · CUE arrête cette sortie et la recue sur l'élément qui lui revient dans la playlist.
« Suivant » = PLAY de la sortie qui attend l'élément suivant. Le mélangeur qui passe sur cette sortie
(mode `mixer`) vaut aussi son PLAY.

★ RIEN N'EST LIBÉRÉ SUR UNE SUPPOSITION. Un PLAY ouvre une PRISE en attente ; la prise n'est acquise que
lorsque le /state du player MONTRE la lecture de cet élément (non en pause). Alors seulement le curseur
avance et la sortie précédente part en libération (= CUE : arrêtée et recuée sur son prochain élément).
Prise non constatée en TAKE_TIMEOUT_S, ou PLAY refusé : la sortie à l'antenne continue, rien n'est
libéré, et une alerte le dit (aucun échec silencieux).

★ LIBÉRATION : MANUELLE PAR DÉFAUT. La sortie qui a quitté l'antenne reste « en attente de libération »
jusqu'à son CUE. Libération AUTOMATIQUE (option du groupe, désactivée par défaut) : seulement si deux faits
sont LUS dans l'état réel — (1) le mélangeur DÉSIGNÉ n'utilise plus l'entrée DÉSIGNÉE de cette sortie : ni
au programme, ni dans une transition en cours (entrant comme sortant, fondu AUTO ou T-bar), ni au keyer
actif ou en mouvement (l'aperçu ne compte pas : il n'est pas à l'antenne ; le DSK du mélangeur est une page
HTML, pas une entrée — aucun player ne peut y être) ; (2) la tranche DÉSIGNÉE de la console DÉSIGNÉE est
fermée : fader à −∞ ou coupée (mute de la tranche, ou d'un VCA qui la commande hors Mute Safe — lecture
partagée avec le fader start, services/audio/fader_triggers.py). Une confirmation manque (conteneur supprimé ou
injoignable, entrée ou tranche inexistante, désignation incomplète) : on ne libère pas, et la vue dit
pourquoi. La désignation est DÉCLARÉE par l'humain dans la fenêtre ⚙ (instances, n° d'entrée, n° de
tranche) — jamais déduite du câblage ; le formulaire ne fait que la suggérer.

★ LA RÉPARTITION EST UNE FONCTION, PAS UN ÉTAT. Rien ne mémorise « l'élément 7 est sur B » : `plan()` le
recalcule de la playlist, du curseur et de la sortie de base (la dernière mise à l'antenne) — le k-ième
élément à venir va à la sortie (base + 1 + k) mod N. D'où Drop / Skip / réordonnancement : la répartition
change, et seules les sorties en CUE rechargent ce qui leur revient désormais. La boucle compare ce
qu'elle VOIT (/state : média chargé, pause) à ce que le plan veut, jamais à ce qu'elle croit avoir
commandé : un redémarrage de l'orchestrateur ne recharge rien qui soit déjà en place.

★ CE QUI NE SE RECHARGE JAMAIS D'OFFICE. La sortie à l'antenne ; une sortie en libération ; une sortie que
le mélangeur désigné UTILISE (recharger un player au programme coupe l'antenne). Seul un CUE explicite les
touche.

★ DÉCLENCHEMENT PAR LE MÉLANGEUR (mode `mixer`) : le programme qui passe sur l'entrée DÉSIGNÉE de la sortie
qui attend le suivant vaut son PLAY.

★ IDENTITÉ. Membres, mélangeur, console = `instance_uuid` (2e barreau : survit au recreate), jamais le vmid.
Les vues du mélangeur viennent de l'observateur AFV (services/audio/afv.py:subscribe : un seul interrogateur par
mélangeur, relu toutes les 0,25 s — pas à l'image près, et c'est voulu).

Contrôleur ACTIF seul (main.py) : deux boucles se disputeraient les mêmes players.
Spécification : docs/reference/PLAYOUT_GROUPS.md.
"""
import datetime
import json
import logging
import threading
import time
import uuid as _uuid

log = logging.getLogger(__name__)

PLAYER_TYPE = "player"
CONTROL_PORT = 8082
TICK_S = 0.25                # passe : prises, mélangeur, libérations
POLL_MEMBERS_S = 1.0         # relecture du /state de chaque membre
HTTP_TIMEOUT_S = 1.5
TAKE_TIMEOUT_S = 3.0         # un PLAY dont la lecture n'est pas CONSTATÉE dans ce délai a échoué
CMD_RETRY_S = 8.0            # chargement non suivi d'effet : renvoyé après ce délai
FETCH_RETRY_S = 30.0         # rapatriement en échec : nouvel essai
MIXER_FRESH_S = 3.0          # vue du mélangeur plus vieille : inconnue
CONSOLE_POLL_S = 0.5         # relecture des faders d'une console tant qu'une libération automatique attend
MINUS_INF_DB = -120.0        # fader « en bas » (−∞), même borne que services/audio/fader_triggers.py
MIXER_TYPE, CONSOLE_TYPE = "mixer", "audio_console"
TRIGGERS = ("manual", "mixer")
OUT_OF_ORDER = ("skip", "requeue", "refuse")   # Play d'une sortie qui n'attend PAS le suivant
END_WRAP_S = 1.5             # fin de clip par bouclage : la position revient en arrière depuis ses dernières secondes
CONSOLE_DOC_POLL_S = 1.0     # document VIVANT de la console (état AFV) : relu au plus à cette cadence
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MAX_MEMBERS = len(LETTERS)
MAX_ITEMS = 2000
ACTIVE_FETCH = ("queued", "copying", "verifying")

_lock = threading.RLock()    # toute lecture-modification-écriture d'un groupe


_SKIP_SYNC_CHECK = False      # banc seulement (mutation) : la garde des deux sens doit mordre


class PlayoutError(RuntimeError):
    """Refus d'une opération (message pour l'opérateur ; `code` pour l'interface, qui le traduit)."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ═══ FONCTIONS PURES (tests/verif_playout_groups.py) ═══════════════════════════════════════════

def letter(i):
    return LETTERS[i] if 0 <= i < len(LETTERS) else "?"


def new_item(d):
    """Élément de playlist normalisé. `asset` (base des médias) fait foi ; `file` n'est qu'un indice de chemin."""
    d = d if isinstance(d, dict) else {}
    return {"id": str(d.get("id") or _uuid.uuid4().hex[:10]),
            "file": str(d.get("file") or ""), "asset": (str(d["asset"]) if d.get("asset") else None),
            "title": (str(d["title"]) if d.get("title") else None),
            "skipped": bool(d.get("skipped")), "played_on": d.get("played_on") or None}


def item_label(it):
    return (it.get("title") or (it.get("file") or "").rsplit("/", 1)[-1] or it.get("asset") or "") if it else ""


def cursor_index(items, cursor):
    if cursor is None:
        return -1
    for i, it in enumerate(items):
        if it["id"] == cursor:
            return i
    return -1


def upcoming(g):
    """Éléments À VENIR (après le curseur, non sautés), dans l'ordre."""
    ci = cursor_index(g["items"], g.get("cursor"))
    return [it for it in g["items"][ci + 1:] if not it.get("skipped")]


def _base(g):
    """Sortie de la dernière prise (base de la rotation), à l'antenne ou non."""
    a = g.get("air_member")
    return a if a in g["members"] else None


def _air(g):
    """Sortie À L'ANTENNE (None après son CUE)."""
    b = _base(g)
    return b if b and g.get("air_live") else None


def assignment(g):
    """{id d'élément à venir: uuid de la sortie} — TOUS les éléments à venir, projetés tour à tour :
    le k-ième va à (base + 1 + k) mod N (sans base : k mod N)."""
    ms = g["members"]
    n = len(ms)
    if not n:
        return {}
    b = _base(g)
    base = ms.index(b) if b else -1
    return {it["id"]: ms[(base + 1 + k) % n] for k, it in enumerate(upcoming(g))}


def plan(g):
    """{uuid: {"role": air|cue|release|idle, "item": élément | None}}. Une sortie en `release` porte l'élément
    qu'elle chargera une fois libérée. Avec une sortie à l'antenne, N−1 éléments attendent ; sans, N."""
    ms = g["members"]
    a = _air(g)
    rp = g.get("release_pending") or {}
    cue_n = len(ms) - 1 if a else len(ms)
    asg = assignment(g)
    want = {u: None for u in ms}
    for it in upcoming(g)[:max(0, cue_n)]:
        want[asg[it["id"]]] = it
    ci = cursor_index(g["items"], g.get("cursor"))
    out = {}
    for u in ms:
        if u == a:
            out[u] = {"role": "air", "item": g["items"][ci] if ci >= 0 else None}
        elif u in rp:
            out[u] = {"role": "release", "item": want[u]}
        else:
            out[u] = {"role": "cue" if want[u] else "idle", "item": want[u]}
    return out


def next_target(g):
    """(élément suivant, uuid de la sortie qui l'attend) ou (None, None) en fin de playlist."""
    ups = upcoming(g)
    if not ups or not g["members"]:
        return None, None
    return ups[0], assignment(g)[ups[0]["id"]]


def confirm_take(g, u, item_id, now):
    """La lecture de `item_id` sur `u` est CONSTATÉE : le curseur avance, `u` est à l'antenne, la sortie
    précédente part en libération. Les éléments à venir qu'un PLAY HORS ORDRE a dépassés suivent le réglage
    `out_of_order` : « skip » = marqués sautés (visibles, rétablissables) ; « requeue » = remis juste après
    l'élément lancé, dans leur ordre. Modifie `g`."""
    items = g["items"]
    i = _find(items, item_id)
    ci = cursor_index(items, g.get("cursor"))
    passed = [it for it in items[ci + 1:i] if not it.get("skipped")]
    if passed and g.get("out_of_order") == "requeue":
        keep = [it for it in items[ci + 1:i] if it.get("skipped")]
        items[ci + 1:i + 1] = keep + [items[i]] + passed
        g["items"] = items
        i = _find(items, item_id)
    else:
        for it in passed:
            it["skipped"] = True
    items[i]["skipped"] = False
    items[i]["played_on"] = u
    prev = _air(g)
    rp = dict(g.get("release_pending") or {})
    rp.pop(u, None)
    if prev and prev != u:
        rp[prev] = now
    g.update(cursor=item_id, air_member=u, air_live=1, release_pending=rp)


def stop_air(g, u):
    """CUE de la sortie à l'antenne : elle quitte l'antenne (son élément reste joué), la base de la rotation
    ne bouge pas — les autres sorties gardent ce qui leur revient."""
    if _air(g) == u:
        g["air_live"] = 0
    rp = dict(g.get("release_pending") or {})
    rp.pop(u, None)
    g["release_pending"] = rp


def mixer_should_play(g, program_now, program_before):
    """Front montant du programme sur la sortie qui attend l'élément suivant (mode `mixer`) → son uuid, sinon
    None. Revenir sur la sortie qui vient de quitter l'antenne (en libération) n'est pas un « suivant »."""
    if program_before is None or program_now is None:
        return None
    it, target = next_target(g)
    if it is None or target in (g.get("release_pending") or {}) or target == _air(g):
        return None
    return target if (target in program_now and target not in program_before) else None


def _find(items, item_id):
    for i, it in enumerate(items):
        if it["id"] == item_id:
            return i
    raise PlayoutError("unknown_item", "élément introuvable dans la playlist")


def item_op(g, op, item_id, value=None):
    """skip / unskip / drop / up / down / move (value = rang cible 0-based) / goto (l'élément devient le
    SUIVANT : ceux qui le précèdent parmi les éléments à venir sont sautés, donc rétablissables). L'élément
    à l'antenne et les éléments joués ne bougent pas : un réordonnancement ne touche jamais l'antenne."""
    items = g["items"]
    ci = cursor_index(items, g.get("cursor"))
    i = _find(items, item_id)
    if op == "drop":
        if i == ci:
            raise PlayoutError("on_air", "l'élément à l'antenne ne se retire pas")
        items.pop(i)
        return
    if i <= ci:
        raise PlayoutError("not_upcoming", "élément déjà joué ou à l'antenne")
    if op == "skip":
        items[i]["skipped"] = True if value is None else bool(value)
    elif op == "unskip":
        items[i]["skipped"] = False
    elif op in ("up", "down", "move"):
        if op == "up":
            to = i - 1
        elif op == "down":
            to = i + 1
        else:
            try:
                to = int(value)
            except (TypeError, ValueError):
                raise PlayoutError("bad_position", "rang invalide")
        to = max(ci + 1, min(len(items) - 1, to))
        if to != i:
            items.insert(to, items.pop(i))
    elif op == "goto":
        for it in items[ci + 1:i]:
            it["skipped"] = True
        items[i]["skipped"] = False
    else:
        raise PlayoutError("unknown_op", "opération inconnue : %s" % op)


def add_items(g, new, mode="append"):
    """append | replace | next (juste après l'élément à l'antenne). `replace` repart du début : le curseur
    est effacé, mais la sortie à l'antenne le RESTE (elle continue son clip, rien ne la recharge)."""
    news = [new_item(x) for x in (new or []) if isinstance(x, dict) and (x.get("file") or x.get("asset"))]
    for x in news:
        x.update(skipped=False, played_on=None, id=_uuid.uuid4().hex[:10])
    if mode == "replace":
        g["items"] = news
        g["cursor"] = None
    elif mode == "next":
        ci = cursor_index(g["items"], g.get("cursor"))
        g["items"][ci + 1:ci + 1] = news
    else:
        g["items"].extend(news)
    if len(g["items"]) > MAX_ITEMS:
        raise PlayoutError("too_many_items", "playlist limitée à %d éléments" % MAX_ITEMS)
    return news


def reset(g):
    """Remettre la playlist au début. La sortie à l'antenne continue : elle n'est pas rechargée."""
    g["cursor"] = None
    for it in g["items"]:
        it["played_on"] = None


def matches(obs, item):
    """Le /state d'un player montre-t-il cet élément chargé ?"""
    if not obs or not item:
        return False
    if item.get("asset") and obs.get("asset"):
        return obs["asset"] == item["asset"]
    f, o = item.get("file") or "", obs.get("file") or ""
    return bool(f and o and (o == f or o.rsplit("/", 1)[-1] == f.rsplit("/", 1)[-1]))


def is_playing(obs, item):
    """Lecture CONSTATÉE : l'élément est chargé et le player n'est pas en pause."""
    return matches(obs, item) and obs.get("paused") is False


def member_status(role, obs, item, fetch, error, on_program, reachable, taking=False):
    """État d'une sortie : offline | taking | air | release | fetching | error | ready | loading | protected | idle."""
    if not reachable:
        return "offline"
    if taking:
        return "taking"
    if role == "air":
        return "air"
    if role == "release":
        return "release"
    if not item:
        return "idle"
    if fetch and fetch.get("state") in ACTIVE_FETCH:
        return "fetching"
    if matches(obs, item) and (obs or {}).get("paused"):
        return "ready"
    if on_program:
        return "protected"
    if error:
        return "error"
    return "loading"


def mixer_used_inputs(st):
    """Entrées (1-based) que le mélangeur UTILISE, d'après son /state : programme ; transition en cours —
    entrante ET sortante (fondu AUTO : `afv.on_air` range la sortante dans les fermetures, elle reste à
    l'image jusqu'à la fin) ; T-bar (les deux) ; keyer actif ou en mouvement. L'aperçu (pvw) N'EST PAS
    une utilisation : il n'est pas à l'antenne. → set, ou None si la vue n'est pas exploitable."""
    if not isinstance(st, dict) or not isinstance(st.get("pgm"), int):
        return None
    try:
        from services.audio import afv
    except ImportError:          # audio service absent: no reading of the mixer (and no views either, see start)
        return None
    opens, closes = afv.on_air(st, "pgm", True)
    return set(opens) | set(closes)


def program_members(st, channels):
    """Membres dont l'entrée DÉSIGNÉE est utilisée par le mélangeur. `channels` : {uuid: {mixer_input}}.
    → set d'uuid, ou None (vue inexploitable)."""
    used = mixer_used_inputs(st)
    if used is None:
        return None
    return {u for u, ch in (channels or {}).items() if isinstance(ch, dict) and ch.get("mixer_input") in used}


def doc_strip_ids(doc):
    """Ids des TRANCHES du document d'une console, dans l'ordre du document : le n° de tranche = rang + 1."""
    return [x["id"] for x in (doc or {}).get("strips") or [] if isinstance(x, dict) and x.get("id")]


def strip_closed(view, strip_id):
    """(fermée par le fader ?, raison si illisible). Fermée = fader à −∞ ou coupée (mute de la tranche ou d'un
    VCA qui la commande, hors Mute Safe) — même lecture que le fader start (services/audio/fader_triggers.py:reading)."""
    try:
        from services.audio.fader_triggers import reading
    except ImportError:          # audio service absent: the fader cannot be read
        return None, "strip_unknown"
    rd = reading(view, strip_id, with_vca=True)
    if rd is None:
        return None, "strip_unknown"
    level, muted = rd
    return (muted or level <= MINUS_INF_DB), None


def afv_closed(doc, strip_id):
    """La tranche est-elle FERMÉE par l'audio suit la vidéo, d'après le document VIVANT de la console
    (`/doc?live=1` : `afv.open` est momentané, jamais persisté) ? Même règle que console_model.afv_target :
    fermée si l'AFV général est en service, que la tranche suit (`on`) et que l'orchestrateur ne l'a pas
    ouverte. Décision 2026-10-17 : une tranche fermée par l'AFV compte comme fermée, quel que soit son
    niveau fermé (`closed_db`)."""
    if not isinstance(doc, dict) or (doc.get("afv") or {}).get("enabled", True) is False:
        return False
    st = next((x for x in doc.get("strips") or [] if isinstance(x, dict) and x.get("id") == strip_id), None)
    a = (st or {}).get("afv")
    return isinstance(a, dict) and bool(a.get("on")) and not a.get("open")


def strip_ref(ch, strips):
    """Id de la tranche désignée : son `strip_id` (fait foi, jamais le libellé) ; à défaut, désignation d'avant
    les ids — le rang `strip`, résolu dans l'ordre du document. None si elle n'existe pas (ou plus)."""
    sid = ch.get("strip_id")
    if sid:
        return sid if sid in (strips or []) else None
    n = ch.get("strip")
    return strips[n - 1] if isinstance(n, int) and 1 <= n <= len(strips or []) else None


def auto_release_check(g, u, mixer, n_inputs, console, strips, live_doc=None):
    """Peut-on libérer AUTOMATIQUEMENT la sortie `u` ? → (True, None) | (False, raison). Les deux faits sont
    LUS : `mixer` = /state du mélangeur désigné, ou "missing" (supprimé) / "unreachable" ; `n_inputs` = son
    nombre d'entrées déployé ; `console` = vue des faders de la console désignée, ou "missing" /
    "unreachable" ; `strips` = ids de ses tranches (ordre du document) ; `live_doc()` = son document VIVANT
    (pour l'AFV), appelé seulement si le fader ne suffit pas — None s'il est illisible.
    Raisons : disabled, no_mixer, no_console, no_mapping, mixer_missing, mixer_unreachable,
    mixer_input_unknown, mixer_uses, console_missing, console_unreachable, strip_unknown, strip_open."""
    if not g.get("auto_release"):
        return False, "disabled"
    if not g.get("mixer_uuid"):
        return False, "no_mixer"
    if not g.get("console_uuid"):
        return False, "no_console"
    ch = (g.get("channels") or {}).get(u) or {}
    if not isinstance(ch.get("mixer_input"), int) or not (ch.get("strip_id") or isinstance(ch.get("strip"), int)):
        return False, "no_mapping"
    if mixer == "missing":
        return False, "mixer_missing"
    used = mixer_used_inputs(mixer) if isinstance(mixer, dict) else None
    if used is None:
        return False, "mixer_unreachable"
    n_in = len(mixer.get("inputs") or []) or int(n_inputs or 0)
    if not 1 <= ch["mixer_input"] <= n_in:
        return False, "mixer_input_unknown"
    if ch["mixer_input"] in used:
        return False, "mixer_uses"
    if console == "missing":
        return False, "console_missing"
    if not isinstance(console, dict):
        return False, "console_unreachable"
    sid = strip_ref(ch, strips)
    if not sid:
        return False, "strip_unknown"
    closed, why = strip_closed(console, sid)
    if why:
        return False, why
    if closed:
        return True, None
    doc = live_doc() if callable(live_doc) else live_doc
    if doc is None:
        return False, "console_unreachable"
    return (True, None) if afv_closed(doc, sid) else (False, "strip_open")


# ═══ BASE ══════════════════════════════════════════════════════════════════════════════════════

def _row(r):
    if r is None:
        return None
    g = dict(r)
    for k, d in (("members", []), ("items", []), ("release_pending", {}), ("channels", {})):
        try:
            g[k] = json.loads(g.get(k) or "null") or d
        except ValueError:
            g[k] = d
    g["items"] = [new_item(x) for x in g["items"]]
    g["trigger"] = g.pop("trigger_mode", None) or "manual"
    g["air_live"] = int(g.get("air_live") or 0)
    g["auto_release"] = bool(g.get("auto_release"))
    g["auto_next"] = bool(g.get("auto_next"))
    g["out_of_order"] = g.get("out_of_order") if g.get("out_of_order") in OUT_OF_ORDER else "skip"
    return g


def list_groups(project_id=None):
    from .database import get_db
    gs = [_row(r) for r in get_db().execute("SELECT * FROM playout_groups ORDER BY id").fetchall()]
    return [g for g in gs if project_id is None or g.get("project_id") == project_id]


def get_group(gid):
    from .database import get_db
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return None
    return _row(get_db().execute("SELECT * FROM playout_groups WHERE id=?", (gid,)).fetchone())


def save_group(g):
    from .database import get_db
    with get_db() as db:
        db.execute("""UPDATE playout_groups SET name=?, project_id=?, members=?, items=?, cursor=?, air_member=?,
                      air_live=?, release_pending=?, trigger_mode=?, mixer_uuid=?, console_uuid=?, channels=?,
                      auto_release=?, out_of_order=?, auto_next=?, updated_at=? WHERE id=?""",
                   (g["name"], g.get("project_id"), json.dumps(g["members"]), json.dumps(g["items"]),
                    g.get("cursor"), g.get("air_member"), int(g.get("air_live") or 0),
                    json.dumps(g.get("release_pending") or {}), g.get("trigger") or "manual", g.get("mixer_uuid"),
                    g.get("console_uuid"), json.dumps(g.get("channels") or {}), int(bool(g.get("auto_release"))),
                    g.get("out_of_order") or "skip", int(bool(g.get("auto_next"))), _now_iso(), g["id"]))


def _dc(c):
    try:
        return json.loads(c.get("deploy_config") or "{}") if isinstance(c.get("deploy_config"), str) \
            else (c.get("deploy_config") or {})
    except ValueError:
        return {}


def _of_type(t):
    """{instance_uuid: conteneur} des conteneurs du type `t`."""
    from .database import db_get_containers
    return {c["instance_uuid"]: c for c in db_get_containers() or []
            if c.get("instance_uuid") and _dc(c).get("type") == t}


def _players():
    return _of_type(PLAYER_TYPE)


def mixer_n_inputs(c):
    return int((_dc(c).get("params") or {}).get("n_inputs") or 2) if c else 0


def console_strips(c):
    """Ids des tranches du document PERSISTÉ de la console (deploy_config, cf. services/audio/afv.py)."""
    return doc_strip_ids((_dc(c).get("params") or {}).get("console")) if c else []


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _validate_config(cfg, players, gid=None):
    name = str(cfg.get("name") or "").strip()[:80]
    if not name:
        raise PlayoutError("name_required", "nom du groupe requis")
    members = []
    for u in cfg.get("members") or []:
        u = str(u)
        if u not in players:
            raise PlayoutError("not_a_player", "membre inconnu ou qui n'est pas un player : %s" % u)
        if u in members:
            raise PlayoutError("duplicate_member", "un player ne compte qu'une fois dans un groupe")
        members.append(u)
    if len(members) < 2:
        raise PlayoutError("too_few_members", "un groupe de lecture réunit au moins deux players")
    if len(members) > MAX_MEMBERS:
        raise PlayoutError("too_many_members", "au plus %d sorties" % MAX_MEMBERS)
    for other in list_groups():
        if other["id"] != gid and set(other["members"]) & set(members):
            raise PlayoutError("member_in_other_group", "un player ne sert qu'un groupe (déjà dans « %s »)" % other["name"])
    # Exclusion dans les DEUX sens avec la lecture synchrone (app/sync_groups.py, qui refuse de son côté un membre
    # d'un groupe A/B) : les deux moteurs chargeraient et lanceraient le même player.
    if not _SKIP_SYNC_CHECK:
        from .sync_groups import list_groups as _sync_groups
        for sg in _sync_groups():
            if set(sg["members"]) & set(members):
                raise PlayoutError("member_in_sync_group", "un canal d'une lecture synchrone (« %s ») ne peut pas être "
                                   "une sortie A/B : les deux le chargeraient" % sg["name"])
    trigger = cfg.get("trigger") or "manual"
    if trigger not in TRIGGERS:
        raise PlayoutError("bad_trigger", "déclenchement inconnu : %s" % trigger)
    mixer_uuid = str(cfg.get("mixer_uuid") or "") or None
    console_uuid = str(cfg.get("console_uuid") or "") or None
    mixer = _of_type(MIXER_TYPE).get(mixer_uuid) if mixer_uuid else None
    console = _of_type(CONSOLE_TYPE).get(console_uuid) if console_uuid else None
    if mixer_uuid and not mixer:
        raise PlayoutError("mixer_not_found", "le mélangeur désigné n'existe pas (ou n'est pas un mélangeur)")
    if console_uuid and not console:
        raise PlayoutError("console_not_found", "la console désignée n'existe pas (ou n'est pas une console)")
    # Désignation DÉCLARÉE par sortie : n° d'entrée du mélangeur, n° de tranche de la console. Vérifiée ici
    # contre ce qui est DÉPLOYÉ (nombre d'entrées, tranches du document) ; revérifiée à l'exécution.
    raw = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    channels = {}
    n_in, strips = mixer_n_inputs(mixer), console_strips(console)
    for u in members:
        ch = raw.get(u) if isinstance(raw.get(u), dict) else {}
        mi, st = _int_or_none(ch.get("mixer_input")), _int_or_none(ch.get("strip"))
        sid = str(ch.get("strip_id") or "") or None
        if mi is not None and mixer and not 1 <= mi <= n_in:
            raise PlayoutError("mixer_input_unknown", "entrée %d inexistante sur le mélangeur (%d entrées)" % (mi, n_in))
        # La tranche se désigne par son ID (jamais son libellé : homonymes) ; le rang n'est qu'une indication.
        # Une désignation d'avant les ids (rang seul) est résolue en id ICI, au premier enregistrement.
        if console and (sid or st is not None):
            ref = strip_ref({"strip_id": sid, "strip": st}, strips)
            if not ref:
                raise PlayoutError("strip_unknown", "tranche inexistante sur la console (%d tranches)" % len(strips))
            sid, st = ref, strips.index(ref) + 1
        channels[u] = {"mixer_input": mi, "strip": st, "strip_id": sid}
    if trigger == "mixer" and (not mixer or any(channels[u]["mixer_input"] is None for u in members)):
        raise PlayoutError("mixer_required",
                           "le déclenchement par le mélangeur exige le mélangeur et l'entrée de chaque sortie")
    auto = bool(cfg.get("auto_release"))
    if auto and (not mixer or not console or any(channels[u]["mixer_input"] is None or not channels[u]["strip_id"]
                                                  for u in members)):
        raise PlayoutError("auto_release_incomplete", "la libération automatique exige le mélangeur, la console, "
                           "et pour chaque sortie son entrée de mélangeur et sa tranche de console")
    ooo = cfg.get("out_of_order") or "skip"
    if ooo not in OUT_OF_ORDER:
        raise PlayoutError("bad_out_of_order", "réglage « Play hors ordre » inconnu : %s" % ooo)
    pids = {players[u].get("project_id") for u in members}
    return {"name": name, "members": members, "trigger": trigger, "mixer_uuid": mixer_uuid,
            "console_uuid": console_uuid, "channels": channels, "auto_release": auto,
            "out_of_order": ooo, "auto_next": bool(cfg.get("auto_next")),
            "project_id": pids.pop() if len(pids) == 1 else None}


_CFG_KEYS = ("name", "members", "trigger", "mixer_uuid", "console_uuid", "channels", "auto_release",
             "out_of_order", "auto_next")


def create_group(cfg):
    from .database import get_db
    with _lock:
        v = _validate_config(cfg, _players())
        with get_db() as db:
            cur = db.execute("""INSERT INTO playout_groups (name, project_id, members, trigger_mode, mixer_uuid,
                                console_uuid, channels, auto_release, out_of_order, auto_next, created_at, updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (v["name"], v["project_id"], json.dumps(v["members"]), v["trigger"], v["mixer_uuid"],
                              v["console_uuid"], json.dumps(v["channels"]), int(v["auto_release"]),
                              v["out_of_order"], int(v["auto_next"]), _now_iso(), _now_iso()))
            gid = cur.lastrowid
    ENGINE.kick()
    log.info("playout group %s created: %s (%d outputs)", gid, v["name"], len(v["members"]))
    return gid


def update_group(gid, cfg):
    with _lock:
        g = _need(gid)
        merged = {k: g.get(k) for k in _CFG_KEYS}
        merged.update({k: v for k, v in (cfg or {}).items() if k in _CFG_KEYS})
        v = _validate_config(merged, _players(), gid=g["id"])
        g.update(v)
        if g.get("air_member") not in g["members"]:
            g.update(air_member=None, air_live=0)
        g["release_pending"] = {u: t for u, t in (g.get("release_pending") or {}).items() if u in g["members"]}
        save_group(g)
    ENGINE.kick()
    return g


def delete_group(gid):
    from .database import get_db
    with _lock:
        _need(gid)
        with get_db() as db:
            db.execute("DELETE FROM playout_groups WHERE id=?", (int(gid),))
    ENGINE.forget(int(gid))


def _need(gid):
    g = get_group(gid)
    if not g:
        raise PlayoutError("unknown_group", "groupe de lecture introuvable")
    return g


def group_of_member(instance_uuid):
    for g in list_groups():
        if instance_uuid in g["members"]:
            return g
    return None


def member_by_letter(g, out):
    """uuid de la sortie désignée par sa lettre (« B ») ou son uuid."""
    out = str(out or "").strip()
    if out in g["members"]:
        return out
    i = LETTERS.find(out.upper()) if len(out) == 1 else -1
    if 0 <= i < len(g["members"]):
        return g["members"][i]
    raise PlayoutError("unknown_output", "sortie inconnue dans ce groupe : %s" % (out or "—"))


# ═══ OPÉRATIONS (interface, macros) ════════════════════════════════════════════════════════════

def op_play(gid, out, source="ui"):
    """PLAY d'une sortie : lance l'élément déjà cué. La prise n'est acquise qu'une fois la lecture constatée
    (cf. Engine._check_take). → {pending, output, item}."""
    with _lock:
        g = _need(gid)
        return ENGINE.play(g, member_by_letter(g, out), source)


def op_next(gid, source="ui"):
    """« Suivant » = PLAY de la sortie qui attend l'élément suivant."""
    with _lock:
        g = _need(gid)
        it, target = next_target(g)
        if it is None:
            raise PlayoutError("end_of_playlist", "fin de la playlist : aucun élément à venir")
        return ENGINE.play(g, target, source)


def op_cue(gid, out, source="ui"):
    """CUE d'une sortie : arrêtée, recuée sur l'élément qui lui revient (à l'antenne : elle la quitte)."""
    with _lock:
        g = _need(gid)
        u = member_by_letter(g, out)
        if ENGINE.pending.get(g["id"], {}).get("member") == u:
            raise PlayoutError("take_pending", "une mise à l'antenne de cette sortie est en cours")
        stop_air(g, u)
        save_group(g)
        r = ENGINE.cue(g, u)
    ENGINE.kick()
    log.info("playout group %s: cue %s (%s)", gid, letter(g["members"].index(u)), source)
    return r


def op_item(gid, op, item_id, value=None):
    with _lock:
        g = _need(gid)
        item_op(g, op, item_id, value)
        save_group(g)
    ENGINE.kick()
    return g


def op_add(gid, items, mode="append"):
    with _lock:
        g = _need(gid)
        news = add_items(g, items, mode)
        save_group(g)
    ENGINE.kick()
    return news


def op_reset(gid):
    with _lock:
        g = _need(gid)
        reset(g)
        save_group(g)
    ENGINE.kick()
    return g


def first_member_vmid(g):
    players = _players()
    for u in g["members"]:
        c = players.get(u)
        if c and c.get("status") == "running":
            return c["vmid"]
    return None


def saved_lists(gid):
    """Playlists enregistrées sur le volume du premier membre joignable (celles de la console du player)."""
    g = _need(gid)
    vm = first_member_vmid(g)
    if vm is None:
        return []
    code, d = ENGINE.http("GET", vm, "/lists", None)
    return ((d or {}).get("lists") or []) if code == 200 else []


def op_load_list(gid, name, mode="replace"):
    """Charge une playlist enregistrée (volume du premier membre joignable) dans le groupe. Le player qui la
    lit sert de lecteur de fichier : SA playlist à lui est remplacée, sans effet sur sa sortie (charger une
    playlist ne change pas le média chargé)."""
    g = _need(gid)
    vm = first_member_vmid(g)
    if vm is None:
        raise PlayoutError("no_member_reachable", "aucun membre joignable pour lire la playlist")
    code, d = ENGINE.http("POST", vm, "/loadlist", {"name": name})
    if code != 200:
        raise PlayoutError("list_failed", "playlist « %s » : %s" % (name, (d or {}).get("error") or code))
    code, st = ENGINE.http("GET", vm, "/state", None)
    if code != 200:
        raise PlayoutError("list_failed", "playlist « %s » : état du player illisible" % name)
    items = [{"file": x.get("file"), "asset": x.get("asset"), "title": x.get("title")} for x in st.get("playlist") or []]
    return op_add(gid, items, mode if mode in ("replace", "append", "next") else "replace")


# ═══ LA BOUCLE ════════════════════════════════════════════════════════════════════════════════

class Engine:
    """Une passe = `tick()`. Les accès au monde (base, réseau, rapatriement, alertes) sont des méthodes
    remplacées dans le banc."""

    def __init__(self):
        self.rt = {}               # (gid, uuid) → {obs, reachable, cmd, cmd_at, fetch, fetch_at, error, joined, vmid}
        self.views = {}            # référence de mélangeur → (/state | None, horodatage)
        self.console_cache = {}    # instance de console → (vue des faders | None, horodatage)
        self.console_doc_cache = {}  # instance de console → (document vivant | None, horodatage)
        self.endwatch = {}         # gid → {item, pos, fired} : fin de clip à l'antenne (auto_next)
        self.release_why = {}      # (gid, uuid) → raison pour laquelle la libération automatique attend
        self.program_prev = {}     # gid → set d'uuid montrés au tour précédent (None = pas encore vu)
        self.pending = {}          # gid → prise en attente {member, item, since, source, prev}
        self.failures = {}         # gid → dernier échec de prise {at, output, item, reason}
        self.wake = threading.Event()
        self.last_poll = 0.0
        self._cache = []
        self.ticks = 0
        self.errors = 0
        self.last_error = None

    # ── accès au monde ─────────────────────────────────────────────────────────────────────────
    def groups(self):
        return list_groups()

    def players(self):
        return _players()

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

    def fetch_start(self, asset, vmid):
        from . import media_fetch
        return media_fetch.start(asset, vmid)

    def fetch_get(self, job):
        from . import media_fetch
        return media_fetch.get(job)

    def alert(self, key, params, vmid=None):
        try:
            from .database import db_add_alert
            db_add_alert(key, "warning", vmid=vmid, params=params)
        except Exception:
            log.exception("playout groups: alert not written")

    def now(self):
        return time.time()

    # ── abonnement mélangeur (appelé SOUS le verrou de l'observateur AFV : copier, rendre la main) ──
    def mixer_refs(self):
        return {"uuid:" + g["mixer_uuid"] for g in self._cache if g.get("mixer_uuid")}

    def on_views(self, views):
        t = self.now()
        for ref, st in views.items():
            self.views[ref] = (st if isinstance(st, dict) else None, t)
        self.wake.set()

    def kick(self):
        self.wake.set()

    def forget(self, gid):
        for k in [k for k in self.rt if k[0] == gid]:
            del self.rt[k]
        for d in (self.program_prev, self.pending, self.failures, self.endwatch):
            d.pop(gid, None)
        for k in [k for k in self.release_why if k[0] == gid]:
            del self.release_why[k]

    # ── commandes ──────────────────────────────────────────────────────────────────────────────
    def _r(self, gid, u):
        return self.rt.setdefault((gid, u), {"obs": None, "reachable": False, "cmd": None, "cmd_at": 0.0,
                                             "fetch": None, "fetch_at": 0.0, "error": None, "joined": False,
                                             "vmid": None})

    @staticmethod
    def _body(item):
        b = {"cue": True}
        for k in ("file", "asset", "title"):
            if item.get(k):
                b[k] = item[k]
        return b

    def _load(self, gid, u, vmid, item):
        """POST /file + cue ; 409 « missing » → rapatriement suivi (media_fetch), rechargé à la fin."""
        r = self._r(gid, u)
        r.update(cmd=item["id"], cmd_at=self.now(), error=None)
        code, d = self.http("POST", vmid, "/file", self._body(item))
        if code == 200:
            return True
        if code == 409 and (d or {}).get("missing") and item.get("asset"):
            try:
                j = self.fetch_start(item["asset"], vmid)
                r.update(fetch=dict(j, item=item["id"]), fetch_at=self.now())
            except Exception as e:
                r["error"] = "fetch: %s" % e
            return False
        r["error"] = (d or {}).get("error") or ("HTTP %s" % code)
        return False

    def _fail(self, g, u, item, reason, source):
        """Échec d'une prise : la sortie à l'antenne continue, rien n'est libéré — et on le DIT."""
        self.pending.pop(g["id"], None)
        o = letter(g["members"].index(u)) if u in g["members"] else "?"
        self.failures[g["id"]] = {"at": self.now(), "output": o, "item": item_label(item), "reason": reason,
                                  "source": source}
        log.warning("playout group %s: PLAY of output %s (%s) failed: %s — on-air output kept",
                    g["id"], o, item_label(item), reason)
        c = self.players().get(u) or {}
        self.alert("alert.playout.take_failed", {"g": g["name"], "o": o, "i": item_label(item), "e": reason},
                   vmid=c.get("vmid"))

    def play(self, g, u, source):
        """PLAY de la sortie `u` (sous _lock). Sortie à l'antenne en pause : reprise, sans prise. Sinon l'élément
        qui lui revient doit être CUÉ (constaté au /state) ; la prise reste en attente jusqu'au constat."""
        gid = g["id"]
        if self.pending.get(gid):
            raise PlayoutError("take_pending", "une mise à l'antenne est déjà en cours dans ce groupe")
        c = self.players().get(u)
        o = letter(g["members"].index(u))
        if not c:
            raise PlayoutError("output_gone", "la sortie %s n'existe plus" % o)
        if u == _air(g):
            code, d = self.http("POST", c["vmid"], "/play", {})
            if code != 200:
                raise PlayoutError("play_failed", "sortie %s : %s" % (o, (d or {}).get("error") or "HTTP %s" % code))
            return {"pending": False, "output": o, "resumed": True}
        item = plan(g)[u]["item"]
        nxt, _t = next_target(g)
        if item and nxt and item["id"] != nxt["id"] and g.get("out_of_order") == "refuse":
            # Réglage du groupe : un Play hors ordre est refusé (geste d'opérateur, rien à l'antenne n'a bougé :
            # refus rendu à l'appelant, pas d'alerte).
            raise PlayoutError("out_of_order", "sortie %s : elle n'attend pas le suivant (Play hors ordre refusé)" % o)
        code, obs = self.http("GET", c["vmid"], "/state", None)
        if code != 200:
            self._fail(g, u, item, "unreachable", source)
            raise PlayoutError("output_unreachable", "sortie %s injoignable" % o)
        if not item or not matches(obs, item) or plan(g)[u]["role"] != "cue":
            self._fail(g, u, item, "not_cued", source)
            raise PlayoutError("not_cued", "sortie %s : son élément n'est pas cué" % o)
        code, d = self.http("POST", c["vmid"], "/play", {})
        if code != 200:
            self._fail(g, u, item, "HTTP %s" % code, source)
            raise PlayoutError("play_failed", "sortie %s : %s" % (o, (d or {}).get("error") or "HTTP %s" % code))
        self.pending[gid] = {"member": u, "item": item["id"], "since": self.now(), "source": source}
        self.failures.pop(gid, None)
        self.kick()
        log.info("playout group %s: PLAY %s (%s) requested by %s", gid, o, item_label(item), source)
        return {"pending": True, "output": o, "item": item}

    def cue(self, g, u):
        """CUE explicite (sous _lock) : recharge l'élément qui revient à `u`, même s'il est déjà là (retour à
        sa première image) ; sans élément, la sortie est mise en pause."""
        c = self.players().get(u)
        if not c:
            raise PlayoutError("output_gone", "cette sortie n'existe plus")
        item = plan(g)[u]["item"]
        if not item:
            code, _d = self.http("POST", c["vmid"], "/pause", {})
            return {"ok": code == 200, "item": None}
        return {"ok": self._load(g["id"], u, c["vmid"], item), "item": item}

    # ── une passe ──────────────────────────────────────────────────────────────────────────────
    def tick(self):
        self.ticks += 1
        now = self.now()
        gs = self.groups()
        self._cache = gs
        players = self.players()
        if now - self.last_poll >= POLL_MEMBERS_S:
            self.last_poll = now
            self._poll(gs, players)
        alive = set()
        for g in gs:
            alive.add(g["id"])
            try:
                self._group(g["id"], players)
            except Exception as e:
                self.errors += 1
                self.last_error = "%s: %s" % (g.get("name"), e)
                log.exception("playout group %s: pass failed", g.get("id"))
        for gid in ({k[0] for k in self.rt} | set(self.pending)) - alive:
            self.forget(gid)

    def _poll(self, gs, players):
        """/state de chaque membre — HORS du verrou des groupes (une commande d'opérateur n'attend pas un
        player injoignable)."""
        for g in gs:
            for u in g["members"]:
                self._observe(g["id"], u, players)

    def _observe(self, gid, u, players):
        r = self._r(gid, u)
        c = players.get(u)
        if not c or c.get("status") not in (None, "running"):
            r.update(reachable=False, obs=None, vmid=(c or {}).get("vmid"))
            return None
        r["vmid"] = c["vmid"]
        code, st = self.http("GET", c["vmid"], "/state", None)
        if code == 200 and isinstance(st, dict):
            r.update(reachable=True, obs=st)
            if not r["joined"]:
                # Entrée dans le groupe : le « suivant » propre au player (hors playlist) partirait tout seul
                # à la fin du clip, à côté de la répartition du groupe.
                self.http("POST", c["vmid"], "/queue", {"clear": True})
                r["joined"] = True
            return st
        r["reachable"] = False
        return None

    def mixer_state(self, g):
        """/state du mélangeur désigné (frais), ou "missing" (conteneur supprimé / pas un mélangeur) /
        "unreachable" (vue absente ou périmée) / None (aucun mélangeur désigné)."""
        mu = g.get("mixer_uuid")
        if not mu:
            return None
        if mu not in self.mixers():
            return "missing"
        st, t = self.views.get("uuid:" + mu, (None, 0.0))
        if st is None or self.now() - t > MIXER_FRESH_S:
            return "unreachable"
        return st

    def program(self, g):
        """Membres dont l'entrée DÉSIGNÉE est utilisée par le mélangeur (set), ou None (inconnu)."""
        st = self.mixer_state(g)
        return program_members(st, g.get("channels")) if isinstance(st, dict) else None

    def mixers(self):
        return _of_type(MIXER_TYPE)

    def consoles(self):
        return _of_type(CONSOLE_TYPE)

    def console_view(self, vmid):
        """Vue des faders d'une console (services/audio/fader_triggers.py:read_view), ou None si injoignable."""
        try:
            from services.audio.fader_triggers import read_view
        except ImportError:      # audio service absent: console unreadable, like an unreachable one
            return None
        view, _fb = read_view(vmid)
        return view

    def console_state(self, g, now):
        """(vue | "missing" | "unreachable" | None, ids des tranches du document) de la console désignée.
        Relue au plus toutes les CONSOLE_POLL_S (seulement quand une libération automatique attend)."""
        cu = g.get("console_uuid")
        if not cu:
            return None, []
        c = self.consoles().get(cu)
        if not c:
            return "missing", []
        hit = self.console_cache.get(cu)
        if hit is None or now - hit[1] >= CONSOLE_POLL_S:
            v = self.console_view(c["vmid"]) if c.get("status") in (None, "running") else None
            hit = (v, now)
            self.console_cache[cu] = hit
        return (hit[0] if hit[0] is not None else "unreachable"), console_strips(c)

    def console_live_doc(self, vmid):
        """Document VIVANT d'une console (`/doc?live=1`, état AFV compris), ou None. Lu seulement quand le fader
        ne suffit pas à dire la tranche fermée, au plus toutes les CONSOLE_DOC_POLL_S : ce document est gros
        (≈ 21 Ko) et sa relecture rapide coûte des blocs audio en retard (mesuré, cf. services/audio/fader_triggers.py)."""
        code, d = self.http("GET", vmid, "/doc?live=1", None)
        return d if code == 200 and isinstance(d, dict) and isinstance(d.get("strips"), list) else None

    def _live_doc_fn(self, g, now):
        cu = g.get("console_uuid")
        c = self.consoles().get(cu) if cu else None

        def fn():
            if not c:
                return None
            hit = self.console_doc_cache.get(cu)
            if hit is None or now - hit[1] >= CONSOLE_DOC_POLL_S:
                hit = (self.console_live_doc(c["vmid"]), now)
                self.console_doc_cache[cu] = hit
            return hit[0]
        return fn

    def _end_of_clip(self, g, players):
        """Enchaînement automatique (option `auto_next`) : la fin CONSTATÉE du clip à l'antenne — `eof` du
        player, ou position qui repart en arrière depuis ses dernières secondes (clip en boucle) — vaut Suivant,
        une fois par élément. Même règle que tout Play : prise constatée avant de libérer."""
        a = _air(g)
        if not (g.get("auto_next") and a) or self.pending.get(g["id"]):
            return
        obs = self._observe(g["id"], a, players)
        ci = cursor_index(g["items"], g.get("cursor"))
        item = g["items"][ci] if ci >= 0 else None
        w = self.endwatch.get(g["id"])
        if not w or w["item"] != g.get("cursor"):
            w = self.endwatch[g["id"]] = {"item": g.get("cursor"), "pos": None, "fired": False}
        if not (obs and item and matches(obs, item)) or w["fired"]:
            return
        pos, dur = float(obs.get("position") or 0), float(obs.get("duration") or 0)
        ended = bool(obs.get("eof")) or (w["pos"] is not None and dur > 0 and w["pos"] >= dur - END_WRAP_S
                                         and pos < w["pos"] - 1.0)
        w["pos"] = pos
        if not ended or next_target(g)[0] is None:
            return
        w["fired"] = True
        log.info("playout group %s: end of %s observed -> next", g["id"], item_label(item))
        try:
            self.play(g, next_target(g)[1], "auto_next")
        except PlayoutError as e:
            log.warning("playout group %s: automatic next not taken: %s", g["id"], e)

    def _check_take(self, g, players):
        """Prise en attente : CONSTATÉE (le /state montre la lecture) → acquise ; délai dépassé → échec."""
        p = self.pending.get(g["id"])
        if not p:
            return False
        u = p["member"]
        try:
            item = g["items"][_find(g["items"], p["item"])]
        except PlayoutError:
            self._fail(g, u, None, "item_removed", p["source"])
            return False
        obs = self._observe(g["id"], u, players)
        if is_playing(obs, item):
            confirm_take(g, u, item["id"], self.now())
            self.pending.pop(g["id"], None)
            log.info("playout group %s: output %s is on air with %s (confirmed)", g["id"],
                     letter(g["members"].index(u)), item_label(item))
            return True
        if self.now() - p["since"] > TAKE_TIMEOUT_S:
            self._fail(g, u, item, "not_observed", p["source"])
            # Elle partirait peut-être en retard, à côté de l'antenne : on la recue — sauf si le mélangeur la
            # montre (recharger un player au programme se verrait).
            prog = self.program(g)
            if not (prog and u in prog):
                try:
                    self.cue(g, u)
                except PlayoutError:
                    pass
        return False

    def _group(self, gid, players):
        now = self.now()
        with _lock:
            g = get_group(gid)                    # relu sous verrou : une opération a pu passer entre-temps
            if not g:
                return
            prog = self.program(g)
            changed = self._check_take(g, players)
            self._end_of_clip(g, players)
            # 1. le mélangeur passe sur la sortie qui attend le suivant : vaut son PLAY
            before = self.program_prev.get(gid)
            self.program_prev[gid] = prog
            if g["trigger"] == "mixer" and not self.pending.get(gid):
                u = mixer_should_play(g, prog, before)
                if u:
                    try:
                        self.play(g, u, "mixer")
                    except PlayoutError as e:
                        log.warning("playout group %s: mixer cut to %s not taken: %s", gid, u, e)
            # 2. libérations : seulement après une prise CONSTATÉE (confirm_take les pose). Par défaut MANUELLES
            # (CUE de la sortie) ; automatiques si l'option est active ET que mélangeur et console le
            # confirment (auto_release_check). Sinon la raison est gardée, la vue la montre.
            rp = dict(g.get("release_pending") or {})
            released = []
            if rp:
                mst = self.mixer_state(g)
                n_in = mixer_n_inputs(self.mixers().get(g.get("mixer_uuid")))
                cview, strips = self.console_state(g, now) if g.get("auto_release") else (None, [])
                for u in rp:
                    ok_, why = auto_release_check(g, u, mst, n_in, cview, strips, self._live_doc_fn(g, now))
                    self.release_why[(gid, u)] = why
                    if ok_:
                        released.append(u)
                        log.info("playout group %s: output %s released automatically (mixer input free, strip closed)",
                                 gid, letter(g["members"].index(u)))
            for u in released:
                del rp[u]
            if released:
                changed = True
                g["release_pending"] = rp
            if changed:
                save_group(g)
            for u in released:
                try:
                    self.cue(g, u)
                except PlayoutError as e:
                    log.warning("playout group %s: release of %s failed: %s", gid, u, e)
            # 3. chargements : chaque sortie en CUE doit montrer SON élément
            taking = (self.pending.get(gid) or {}).get("member")
            for u, want in plan(g).items():
                if want["role"] != "cue" or u == taking:
                    continue
                r = self._r(gid, u)
                c = players.get(u)
                if not c or not r["reachable"] or (prog is not None and u in prog):
                    continue
                it = want["item"]
                if matches(r["obs"], it):
                    continue
                f = r.get("fetch")
                if f and f.get("item") == it["id"]:
                    j = self.fetch_get(f["job"]) if f.get("job") else None
                    if j:
                        r["fetch"] = dict(j, item=it["id"])
                    st = (r["fetch"] or {}).get("state")
                    if st in ACTIVE_FETCH:
                        continue
                    if st == "failed" and now - r["fetch_at"] < FETCH_RETRY_S:
                        r["error"] = "fetch: %s" % ((r["fetch"] or {}).get("error") or "failed")
                        continue
                    r["fetch"] = None              # fini (ou à réessayer) : on recharge
                elif r["cmd"] == it["id"] and now - r["cmd_at"] < CMD_RETRY_S:
                    continue                       # commande en vol : le player charge
                self._load(gid, u, c["vmid"], it)

    # ── vue ────────────────────────────────────────────────────────────────────────────────────
    def view(self, g):
        """Groupe + état vivant de chaque sortie, pour l'interface et les macros."""
        players = self.players()
        pl = plan(g)
        prog = self.program(g)
        chans = g.get("channels") or {}
        cc = self.consoles().get(g.get("console_uuid")) if g.get("console_uuid") else None
        strips = console_strips(cc)

        def chan(u):
            # Rang COURANT de la tranche désignée (l'id fait foi ; le rang suit un réordonnancement de la console).
            ch = dict(chans.get(u) or {})
            ref = strip_ref(ch, strips) if cc else None
            ch["strip_now"] = strips.index(ref) + 1 if ref else None
            return ch
        p = self.pending.get(g["id"]) or {}
        outs = []
        for i, u in enumerate(g["members"]):
            r = self.rt.get((g["id"], u)) or {}
            c = players.get(u) or {}
            want = pl.get(u) or {"role": "idle", "item": None}
            obs = r.get("obs") or {}
            on_prog = (u in prog) if prog is not None else None
            f = r.get("fetch")
            outs.append({
                "uuid": u, "letter": letter(i), "vmid": c.get("vmid"), "hostname": c.get("hostname"),
                "role": want["role"], "item_id": (want["item"] or {}).get("id"),
                "item_title": item_label(want["item"]),
                "status": member_status(want["role"], obs, want["item"], f, r.get("error"), on_prog,
                                        bool(r.get("reachable")) and bool(c), taking=p.get("member") == u),
                "on_program": on_prog, "mixer_input": (chans.get(u) or {}).get("mixer_input"),
                "strip": chan(u)["strip_now"],
                # Pourquoi elle attend d'être libérée : « disabled » = libération manuelle (CUE).
                "release_block": (self.release_why.get((g["id"], u)) or "disabled") if want["role"] == "release" else None,
                "loaded": {"title": obs.get("title"), "file": obs.get("file"), "asset": obs.get("asset")} if obs else None,
                "paused": obs.get("paused"), "position": obs.get("position"), "duration": obs.get("duration"),
                "fetch": ({k: f.get(k) for k in ("state", "done_bytes", "total_bytes", "eta_s", "error")} if f else None),
                "error": r.get("error"), "release_since": (g.get("release_pending") or {}).get(u)})
        asg = assignment(g)
        ci = cursor_index(g["items"], g.get("cursor"))
        idx = {u: i for i, u in enumerate(g["members"])}
        items = []
        for i, it in enumerate(g["items"]):
            if i == ci:
                state, on = ("air" if _air(g) else "played"), g.get("air_member")
            elif i < ci:
                state, on = ("skipped" if it.get("skipped") else "played"), it.get("played_on")
            elif it.get("skipped"):
                state, on = "skipped", None
            else:
                on = asg.get(it["id"])
                w = pl.get(on) or {}
                state = "cued" if (w.get("role") == "cue" and (w.get("item") or {}).get("id") == it["id"]) else "queued"
            items.append(dict(it, state=state, output=letter(idx[on]) if on in idx else None))
        nxt, tgt = next_target(g)
        return {"id": g["id"], "name": g["name"], "project_id": g.get("project_id"), "trigger": g["trigger"],
                "mixer_uuid": g.get("mixer_uuid"), "console_uuid": g.get("console_uuid"),
                "channels": {letter(i): chan(u) for i, u in enumerate(g["members"])},
                "out_of_order": g.get("out_of_order") or "skip", "auto_next": bool(g.get("auto_next")),
                "auto_release": bool(g.get("auto_release")), "mixer_known": prog is not None,
                "designation_issues": self.designation_issues(g),
                "members": g["members"], "outputs": outs, "items": items, "cursor": g.get("cursor"),
                "air_output": letter(idx[_air(g)]) if _air(g) in idx else None,
                "next": ({"item": nxt, "output": letter(idx[tgt])} if nxt else None),
                "taking": ({"output": letter(idx[p["member"]]), "item": p["item"]} if p.get("member") in idx else None),
                "last_failure": self.failures.get(g["id"]),
                "remaining": len(upcoming(g))}

    def designation_issues(self, g):
        """Désignation vérifiée à l'EXÉCUTION (elle l'a été à l'enregistrement) : conteneur supprimé ou
        injoignable, entrée ou tranche qui n'existe plus. [{output: lettre | None, reason}]."""
        out = []
        mst = self.mixer_state(g)
        if mst in ("missing", "unreachable"):
            out.append({"output": None, "reason": "mixer_" + mst})
        cu = g.get("console_uuid")
        cc = self.consoles().get(cu) if cu else None
        if cu and not cc:
            out.append({"output": None, "reason": "console_missing"})
        elif cu and (self.console_cache.get(cu) or (True,))[0] is None:
            out.append({"output": None, "reason": "console_unreachable"})
        n_in = len(mst.get("inputs") or []) if isinstance(mst, dict) else mixer_n_inputs(self.mixers().get(g.get("mixer_uuid")))
        strips = console_strips(cc)
        for i, u in enumerate(g["members"]):
            ch = (g.get("channels") or {}).get(u) or {}
            if g.get("mixer_uuid") and isinstance(ch.get("mixer_input"), int) and n_in and not 1 <= ch["mixer_input"] <= n_in:
                out.append({"output": letter(i), "reason": "mixer_input_unknown"})
            if cc and (ch.get("strip_id") or isinstance(ch.get("strip"), int)) and not strip_ref(ch, strips):
                out.append({"output": letter(i), "reason": "strip_unknown"})
            if (g.get("auto_release") or g.get("trigger") == "mixer") and ch.get("mixer_input") is None:
                out.append({"output": letter(i), "reason": "no_mapping"})
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
            log.exception("playout groups: pass failed")
        ENGINE.wake.wait(TICK_S)
        ENGINE.wake.clear()


def start():
    """Lance la boucle (contrôleur ACTIF seul, cf. main.py) et s'abonne aux vues des mélangeurs. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    try:
        from services.audio import afv
        afv.subscribe(ENGINE.mixer_refs, ENGINE.on_views)
    except ImportError:
        log.info("playout groups: audio service absent, no mixer views (program protection off)")
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="playout-groups")
    _thread.start()
    log.info("playout groups: engine started (tick %.2f s, member poll %.1f s, take timeout %.1f s)",
             TICK_S, POLL_MEMBERS_S, TAKE_TIMEOUT_S)


def stop():
    _stop.set()


def group_view(gid):
    return ENGINE.view(_need(gid))


# ═══ MACROS (fournisseur du cœur, app/core_plugins.py:CORE_PROVIDERS) ══════════════════════════
# Libellés en français dans le code, comme les autres fournisseurs du cœur (ferme, AFV) : le catalogue
# des macros n'est pas encore traduit.
_GROUP = {"key": "group", "label": "Groupe de lecture", "type": "select", "options_hook": True}
_OUT = {"key": "output", "label": "Sortie (A, B, C…)", "type": "select", "options_hook": True, "default": "A"}
_POS = {"key": "position", "label": "Rang parmi les éléments à venir (1 = le suivant)", "type": "number",
        "default": "1", "min": 1}
_STATIC_ACTIONS = [
    {"id": "next", "label": "Groupe de lecture : suivant (Play de la sortie qui attend)", "availability": "project",
     "params": [_GROUP]},
    {"id": "play", "label": "Groupe de lecture : Play d'une sortie", "availability": "project", "params": [_GROUP, _OUT]},
    {"id": "cue", "label": "Groupe de lecture : Cue d'une sortie (arrêt, recue sur son élément)",
     "availability": "project", "params": [_GROUP, _OUT]},
    {"id": "skip", "label": "Groupe de lecture : sauter un élément", "availability": "project",
     "params": [_GROUP, _POS, {"key": "skipped", "label": "Sauté", "type": "bool", "default": "true"}]},
    {"id": "drop", "label": "Groupe de lecture : retirer un élément", "availability": "project",
     "params": [_GROUP, _POS]},
    {"id": "goto", "label": "Groupe de lecture : aller à l'élément", "availability": "project",
     "params": [_GROUP, {"key": "index", "label": "Rang dans la playlist (1 = le premier)", "type": "number",
                         "default": "1", "min": 1}]},
    {"id": "load_playlist", "label": "Groupe de lecture : charger une playlist", "availability": "project",
     "params": [_GROUP, {"key": "name", "label": "Playlist", "type": "select", "options_hook": True},
                {"key": "mode", "label": "Mode", "type": "select", "default": "replace",
                 "options": [{"value": "replace", "label": "Remplacer"}, {"value": "append", "label": "Ajouter à la fin"},
                             {"value": "next", "label": "Insérer après l'antenne"}]}]},
    {"id": "reset", "label": "Groupe de lecture : remettre au début", "availability": "project", "params": [_GROUP]},
    {"id": "auto_release", "label": "Groupe de lecture : libération automatique (selon mélangeur et console)",
     "availability": "project",
     "params": [_GROUP, {"key": "enabled", "label": "Activée", "type": "bool", "default": "false"}]},
    {"id": "auto_next", "label": "Groupe de lecture : enchaînement automatique en fin de clip", "availability": "project",
     "params": [_GROUP, {"key": "enabled", "label": "Activé", "type": "bool", "default": "false"}]},
    {"id": "out_of_order", "label": "Groupe de lecture : Play hors ordre", "availability": "project",
     "params": [_GROUP, {"key": "mode", "label": "Comportement", "type": "select", "default": "skip",
                         "options": [{"value": "skip", "label": "Marquer sautés (rétablissables)"},
                                     {"value": "requeue", "label": "Remettre juste après l'élément lancé"},
                                     {"value": "refuse", "label": "Refuser le Play hors ordre"}]}]},
    {"id": "trigger", "label": "Groupe de lecture : déclenchement", "availability": "project",
     "params": [_GROUP, {"key": "trigger", "label": "Déclenché par", "type": "select", "default": "manual",
                         "options": [{"value": "manual", "label": "Interface et macros"},
                                     {"value": "mixer", "label": "Le mélangeur (et interface, macros)"}]}]},
]
_OUT_STATUS = ["offline", "taking", "air", "release", "fetching", "error", "ready", "loading", "protected", "idle"]
_GROUP_STATES = [("on_air_output", "sortie à l'antenne (lettre)", "text", None),
                 ("on_air_title", "élément à l'antenne", "text", None),
                 ("next_title", "élément suivant", "text", None),
                 ("next_output", "sortie du suivant (lettre)", "text", None),
                 ("next_ready", "le suivant est prêt", "bool", None),
                 ("remaining", "éléments restants", "number", None),
                 ("last_failure", "dernier échec de mise à l'antenne", "text", None),
                 ("auto_release", "libération automatique activée", "bool", None),
                 ("auto_next", "enchaînement automatique activé", "bool", None),
                 ("out_of_order", "Play hors ordre (skip, requeue, refuse)", "text", None),
                 ("designation", "désignation (mélangeur, console, entrée et tranche par sortie)", "text", None)]


def _states():
    """États DYNAMIQUES (un état ne prend pas de paramètre dans le moteur de macros) : un jeu par groupe
    `<sorte>:<id>`, et deux par sortie `output_status:<id>:<lettre>` / `output_item:<id>:<lettre>`."""
    out = []
    try:
        for g in list_groups():
            for kind, lab, typ, _o in _GROUP_STATES:
                out.append({"id": "%s:%d" % (kind, g["id"]), "label": "%s : %s" % (g["name"], lab), "type": typ})
            for i in range(len(g["members"])):
                L = letter(i)
                out.append({"id": "output_status:%d:%s" % (g["id"], L), "label": "%s · %s : état" % (g["name"], L),
                            "type": "enum", "options": _OUT_STATUS})
                out.append({"id": "output_item:%d:%s" % (g["id"], L),
                            "label": "%s · %s : élément cué / à l'antenne" % (g["name"], L), "type": "text"})
                out.append({"id": "output_release_block:%d:%s" % (g["id"], L),
                            "label": "%s · %s : pourquoi elle n'est pas libérée" % (g["name"], L), "type": "text"})
    except Exception:
        log.exception("playout groups: macro states listing failed")
    return out


class _Provider(dict):
    def get(self, k, default=None):
        return _states() if k == "states" else super().get(k, default)

    def __getitem__(self, k):
        return _states() if k == "states" else super().__getitem__(k)


MACRO_PROVIDER = _Provider({"id": "playout_group", "label": "Groupes de lecture", "actions": _STATIC_ACTIONS})


def macro_available():
    from . import plugins
    return bool(plugins.get(PLAYER_TYPE))


def action_options(action_id, key, pid=None):
    gs = list_groups(pid)
    if key == "group":
        return [{"value": str(g["id"]), "label": g["name"]} for g in gs]
    if key == "output":
        n = max([len(g["members"]) for g in gs] or [2])
        return [{"value": letter(i), "label": letter(i)} for i in range(n)]
    if action_id == "load_playlist" and key == "name":
        seen, out = set(), []
        for g in gs:
            try:
                for x in saved_lists(g["id"]):
                    if x.get("name") and x["name"] not in seen:
                        seen.add(x["name"])
                        out.append({"value": x["name"], "label": x["name"]})
            except Exception:
                continue
        return out
    return []


def _macro_group(p, pid):
    g = get_group(str(p.get("group") or "").strip() or None)
    if not g or (pid is not None and g.get("project_id") != pid):
        raise RuntimeError("groupe de lecture introuvable dans ce projet")
    return g


def _int(v, default):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "oui", "on")


def run_action(action_id, params, ctx):
    pid = (ctx or {}).get("project_id")
    p = params or {}
    try:
        g = _macro_group(p, pid)
        if action_id == "next":
            return op_next(g["id"], source="macro")
        if action_id == "play":
            return op_play(g["id"], p.get("output") or "A", source="macro")
        if action_id == "cue":
            return op_cue(g["id"], p.get("output") or "A", source="macro")
        if action_id in ("skip", "drop"):
            ups = upcoming(g)
            k = _int(p.get("position"), 1)
            if not 1 <= k <= len(ups):
                raise PlayoutError("bad_position", "pas d'élément à venir au rang %d" % k)
            if action_id == "drop":
                op_item(g["id"], "drop", ups[k - 1]["id"])
            else:
                op_item(g["id"], "skip", ups[k - 1]["id"], _truthy(p.get("skipped", "true")))
            return {"ok": True}
        if action_id == "goto":
            k = _int(p.get("index"), 1)
            if not 1 <= k <= len(g["items"]):
                raise PlayoutError("bad_position", "pas d'élément au rang %d" % k)
            op_item(g["id"], "goto", g["items"][k - 1]["id"])
            return {"ok": True}
        if action_id == "load_playlist":
            news = op_load_list(g["id"], str(p.get("name") or ""), str(p.get("mode") or "replace"))
            return {"ok": True, "count": len(news)}
        if action_id == "reset":
            op_reset(g["id"])
            return {"ok": True}
        if action_id == "auto_release":
            update_group(g["id"], {"auto_release": _truthy(p.get("enabled", "false"))})
            return {"ok": True}
        if action_id == "auto_next":
            update_group(g["id"], {"auto_next": _truthy(p.get("enabled", "false"))})
            return {"ok": True}
        if action_id == "out_of_order":
            update_group(g["id"], {"out_of_order": str(p.get("mode") or "skip")})
            return {"ok": True}
        if action_id == "trigger":
            update_group(g["id"], {"trigger": str(p.get("trigger") or "manual")})
            return {"ok": True}
    except PlayoutError as e:
        raise RuntimeError(str(e))
    raise RuntimeError("action inconnue : %s" % action_id)


def designation_text(g):
    """Désignation lisible : « mélangeur X · console Y · A = entrée 3 / tranche 5 · B = … »."""
    mx = ENGINE.mixers().get(g.get("mixer_uuid")) if g.get("mixer_uuid") else None
    cs = ENGINE.consoles().get(g.get("console_uuid")) if g.get("console_uuid") else None
    parts = ["mixer %s" % ((mx or {}).get("hostname") or g.get("mixer_uuid") or "—"),
             "console %s" % ((cs or {}).get("hostname") or g.get("console_uuid") or "—")]
    for i, u in enumerate(g["members"]):
        ch = (g.get("channels") or {}).get(u) or {}
        sid = ch.get("strip_id")
        parts.append("%s = in %s / strip %s%s" % (letter(i), ch.get("mixer_input") or "—", ch.get("strip") or "—",
                                                  " [%s]" % sid if sid else ""))
    return " · ".join(parts)


def read_state(state_id, ctx):
    parts = str(state_id).split(":")
    kind = parts[0]
    g = get_group(parts[1] if len(parts) > 1 else None)
    pid = (ctx or {}).get("project_id")
    if not g or (pid is not None and g.get("project_id") != pid):
        return None
    if kind == "remaining":
        return len(upcoming(g))
    v = ENGINE.view(g)
    if kind in ("output_status", "output_item", "output_release_block"):
        o = next((o for o in v["outputs"] if len(parts) > 2 and o["letter"] == parts[2]), None)
        if not o:
            return None
        if kind == "output_release_block":
            return o["release_block"]
        return o["status"] if kind == "output_status" else (o["item_title"] or None)
    if kind == "auto_release":
        return bool(g.get("auto_release"))
    if kind == "auto_next":
        return bool(g.get("auto_next"))
    if kind == "out_of_order":
        return g.get("out_of_order") or "skip"
    if kind == "designation":
        return designation_text(g)
    nxt = v.get("next") or {}
    if kind == "on_air_output":
        return v.get("air_output")
    if kind == "on_air_title":
        if not v.get("air_output"):
            return None
        ci = cursor_index(g["items"], g.get("cursor"))
        return item_label(g["items"][ci]) if ci >= 0 else None
    if kind == "next_title":
        return item_label(nxt.get("item")) or None
    if kind == "next_output":
        return nxt.get("output")
    if kind == "next_ready":
        o = next((o for o in v["outputs"] if nxt and o["letter"] == nxt["output"]), None)
        return bool(o and o["status"] == "ready")
    if kind == "last_failure":
        f = v.get("last_failure")
        return ("%s · %s : %s" % (f["output"], f["item"], f["reason"])) if f else None
    return None
