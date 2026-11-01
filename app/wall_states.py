# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""États d'AUTRES conteneurs affichés sur un mur multiview : `%state:<emplacement>.<état>%`.

Exemples : `%state:player_1.clip%` (nom du clip chargé), `%state:player_1.remaining%` (décompte),
`%state:mixer_1.pgm%`. Demandé par l'utilisateur le 2026-10-28.

★ C'EST L'ORCHESTRATEUR QUI LIT ET QUI POUSSE. Le multiview n'interroge jamais un autre conteneur :
il n'en a pas les identifiants, et c'est voulu (CLAUDE.md, « aucun code de plugin n'est exécuté
in-process » — le cloisonnement vaut dans les deux sens). On lit par `macros.fetch_state`, le chemin
déjà borné aux `control.read_endpoints` que les conditions de macro empruntent.

★ ADRESSÉ PAR NOM, jamais par vmid (il change à chaque recréation). Deux portées, la plus locale
d'abord (décision de l'utilisateur, 2026-10-28) :
  1. un conteneur du MÊME PROJET que le mur, par son hostname — la restauration d'un projet garde
     les noms (elle refuse même un nom déjà pris, app/projects.py), donc un mur de projet n'a besoin
     d'aucun réglage de plus ;
  2. sinon un EMPLACEMENT (`production_roles.key`) — hors projet, ou pour viser un autre projet.
Un nom introuvable rend « — » : un conteneur RENOMMÉ se voit ainsi tout de suite à l'écran (choix
assumé : on ne réécrit pas en silence les murs qui le citaient).

★ UN DÉCOMPTE N'EST PAS UNE VALEUR PAR SECONDE. On pousse l'instant de FIN et l'état lecture/pause ;
le multiview calcule le reste à chaque image (comme ses horloges). Aucun trafic par seconde, et un
décompte régulier même si une lecture d'état arrive en retard.

États DÉRIVÉS, pour tout conteneur dont le catalogue porte `position` et `duration` (le player) :
`remaining` (décompte), `elapsed` (écoulé), et `clip` (nom du fichier `file`, sans dossier ni
extension). Le reste est l'état du catalogue tel quel, en texte.
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

STATE_VAR_RE = re.compile(r"%state:([A-Za-z0-9_\-]+)\.([A-Za-z0-9_.\-]+)%")
PERIOD_S = 0.5            # lecture des états référencés ; rien n'est lu pour un mur qui n'en a pas
IDLE_PERIOD_S = 3.0       # aucun mur n'en référence : on ne fait que re-scanner les configs
COUNTDOWN_JITTER_S = 0.25 # une fin qui ne bouge que de la gigue de lecture n'est pas repoussée
# ★ Re-poussée PÉRIODIQUE, même sans changement. Le mur garde les valeurs EN MÉMOIRE : redémarré
# (redéploiement, relance), il repart vide — et si rien n'avait changé ici, on ne lui renvoyait
# plus jamais rien. Relevé Z8 le 2026-10-29 : mur #225 relancé à 15:31, trois %state% à « — »
# indéfiniment alors que les valeurs étaient justes côté orchestrateur. Un petit POST par mur
# toutes les 10 s borne ce trou à 10 s, quoi qui arrive au mur.
REPUSH_S = 10.0
_stop = threading.Event()
_thr = None
_pushed_at = {}           # vmid du mur → time.monotonic() de la dernière poussée acceptée
_refused = {}             # vmid du mur → dernier statut de refus journalisé (un message par changement)
# Port de la route /states : 8082 (contrôle, comme /telemetry) depuis multiview 0.116.13 ; 8080 de
# 0.116.8 à 0.116.12 — repli sur 404, pour servir les murs déployés sans les redéployer.
STATES_PORTS = (8082, 8080)


def refs_in(params):
    """{(emplacement, état)} référencés par la config d'un mur (overlays, modèles, tout le reste)."""
    try:
        blob = json.dumps(params or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return set()
    return set(STATE_VAR_RE.findall(blob))


def _clip_name(path):
    base = os.path.basename(str(path or ""))
    return os.path.splitext(base)[0] if base else ""


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Sources NOMMÉES d'un mélangeur : `pgm` / `pvw` sont des INDICES (0 = 1re entrée) — affichés tels
# quels, « Entrée PGM » montrait « 0 » à l'écran (relevé Z8, 2026-10-29). Le nom de la source se lit
# dans la liste `inputs` du même /state, puis son libellé affiché (page Libellés).
SOURCE_NAME_STATES = {"pgm_name": "pgm", "pvw_name": "pvw"}


def value_of(state, read, now=None, ctx=None):
    """Valeur poussée au mur pour UN état d'un conteneur. `read(state_id)` lit le catalogue du
    conteneur (None si absent ou injoignable). Rend un dict `t` = text | countdown | elapsed, ou
    None quand on ne sait pas — le mur affiche alors « — ».

    `ctx` (facultatif) : `kind` = type déclaré de l'état ; `answered()` = le conteneur a répondu ;
    `raw(path)` = lecture d'un chemin de son /state ; `label_of(nom)` = libellé affiché d'une source.
    ★ Un booléen publié `null` par un conteneur qui RÉPOND est faux, pas inconnu : le mélangeur
    publie `transition: null` au repos, et le mur affichait « — » comme pour un conteneur éteint."""
    now = time.time() if now is None else now
    ctx = ctx or {}
    if state in SOURCE_NAME_STATES:
        idx, raw = read(SOURCE_NAME_STATES[state]), ctx.get("raw")
        names = raw("inputs") if raw else None
        try:
            name = names[int(idx)]
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if not name:
            return {"t": "text", "v": ""}
        label_of = ctx.get("label_of")
        return {"t": "text", "v": str((label_of(name) if label_of else "") or name)}
    if state in ("remaining", "elapsed"):
        pos, dur = _as_float(read("position")), _as_float(read("duration"))
        if pos is None or dur is None or dur <= 0:
            return None
        run = not bool(read("paused")) and not bool(read("eof"))
        if state == "remaining":
            rest = max(0.0, dur - pos)
            return {"t": "countdown", "end": now + rest, "run": run, "frozen": rest}
        return {"t": "elapsed", "start": now - pos, "run": run, "frozen": pos}
    if state == "clip":
        f = read("file")
        return None if f is None else {"t": "text", "v": _clip_name(f)}
    v = read(state)
    if v is None:
        answered = ctx.get("answered")
        if ctx.get("kind") == "bool" and answered and answered():
            v = False
        else:
            return None
    if isinstance(v, bool):
        v = "●" if v else "○"
    return {"t": "text", "v": str(v)}


def same_value(a, b):
    """Deux valeurs poussées sont-elles équivalentes ? Un décompte dont la fin ne bouge que de la
    gigue de lecture n'est pas un changement — sinon on repousserait à chaque tour."""
    if a == b:
        return True
    if not a or not b or a.get("t") != b.get("t") or a.get("run") != b.get("run"):
        return False
    if a["t"] == "countdown":
        return abs(a["end"] - b["end"]) <= COUNTDOWN_JITTER_S and \
            (a["run"] or abs(a["frozen"] - b["frozen"]) < 0.5)
    if a["t"] == "elapsed":
        return abs(a["start"] - b["start"]) <= COUNTDOWN_JITTER_S and \
            (a["run"] or abs(a["frozen"] - b["frozen"]) < 0.5)
    return False


def resolve(key, project_id, project_hosts, roles, vmid_of_instance):
    """vmid du conteneur désigné par `key` pour un mur du projet `project_id`, ou None.
    Le hostname DANS LE PROJET d'abord, l'emplacement ensuite."""
    if project_id is not None:
        vm = project_hosts.get((project_id, key))
        if vm:
            return vm
    return vmid_of_instance.get(roles.get(key) or "")


def wall_payloads(walls, roles, vmid_of_instance, fetch, now=None, project_of=None, project_hosts=None,
                  ctx_of=None):
    """{vmid du mur: {"<nom>.<état>": valeur|None}} pour tous les murs.
    `walls` : {vmid: refs}. `roles` : {clé: instance_uuid}. `fetch(vmid, state_id)` lit un état.
    `project_of` : {vmid du mur: projet} ; `project_hosts` : {(projet, hostname): vmid}.
    `ctx_of(vmid, state)` : contexte de value_of (type, conteneur joignable, lecture brute)."""
    out = {}
    project_of, project_hosts = project_of or {}, project_hosts or {}
    for wall, refs in walls.items():
        vals = {}
        for key, state in sorted(refs):
            vm = resolve(key, project_of.get(wall), project_hosts, roles, vmid_of_instance)
            vals["%s.%s" % (key, state)] = (
                value_of(state, lambda sid, vm=vm: fetch(vm, sid), now,
                         ctx_of(vm, state) if ctx_of else None) if vm else None)
        out[wall] = vals
    return out


DERIVED_LABELS = {"clip": "Clip chargé", "remaining": "Temps restant", "elapsed": "Temps écoulé",
                  "pgm_name": "Source PGM", "pvw_name": "Source PVW"}


def _path_out_of_range(doc, path):
    """Le chemin vise une case de liste qui n'existe PAS dans ce /state (`keyers.2.active` sur un
    mélangeur à 2 keyers) : l'état ne vaudra jamais rien, on ne le propose pas. Un chemin vers une
    clé simplement nulle ou absente d'un dict, lui, reste proposé — elle peut apparaître."""
    val = doc
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(val, list) and part.isdigit():
            if int(part) >= len(val):
                return True
            val = val[int(part)]
        elif isinstance(val, dict):
            val = val.get(part)
        else:
            return False
    return False


def needs_doc(manifest):
    """Le catalogue de ce type a-t-il besoin du /state vivant pour être filtré ?"""
    return any(any(p.isdigit() for p in str(s.get("path") or "").split("."))
               for s in (manifest or {}).get("state") or [])


def _entries(name, who, ct, manifest_of, doc_of=None):
    """Variables offertes par UN conteneur, adressé par `name` (hostname ou clé d'emplacement).
    `doc_of(ct)` : son /state vivant, pour écarter les états qui visent une case inexistante."""
    try:
        t = (json.loads(ct.get("deploy_config") or "{}") or {}).get("type")
    except (TypeError, ValueError):
        return []
    man = manifest_of(t) or {}
    states = [s for s in (man.get("state") or []) if s.get("id")]
    if doc_of and needs_doc(man):
        doc = doc_of(ct)
        if isinstance(doc, (dict, list)):
            states = [s for s in states if not _path_out_of_range(doc, s.get("path"))]
    ids = {s["id"] for s in states}
    derived = [d for d in ("pgm_name", "pvw_name", "clip", "remaining", "elapsed")
               if (d == "clip" and "file" in ids)
               or (d in SOURCE_NAME_STATES and SOURCE_NAME_STATES[d] in ids)
               or (d in ("remaining", "elapsed") and {"position", "duration"} <= ids)]
    # `group` / `type_label` / `short` : le sélecteur range un groupe PAR CONTENEUR, groupes triés
    # par type (demandé le 2026-10-29 : « tout est en vrac »). `label` reste la forme longue, pour
    # qui affiche une liste plate.
    base = {"group": who, "type": t or "", "type_label": man.get("label") or t or ""}
    pairs = [(d, DERIVED_LABELS[d]) for d in derived] + [(s["id"], s.get("label") or s["id"]) for s in states]
    return [dict(base, name="state:%s.%s" % (name, sid), label="%s — %s" % (who, lab), short=lab)
            for sid, lab in pairs]


def project_catalogue(containers, project_id, manifest_of, exclude_vmid=None, doc_of=None):
    """Variables des conteneurs du MÊME projet que le mur, par hostname (le mur lui-même exclu)."""
    if project_id is None:
        return []
    out = []
    for ct in sorted(containers, key=lambda c: c.get("hostname") or ""):
        if ct.get("project_id") != project_id or ct.get("vmid") == exclude_vmid or not ct.get("hostname"):
            continue
        out += _entries(ct["hostname"], ct["hostname"], ct, manifest_of, doc_of)
    return out


def catalogue(roles_with_containers, manifest_of, doc_of=None):
    """Variables `%state:…%` proposées par le sélecteur de l'éditeur, une par (emplacement, état).
    Seuls les emplacements SERVIS en offrent : un état ne se choisit que sur un conteneur qui le
    publie. `roles_with_containers` : [(role, conteneur|None)] ; `manifest_of(type)` : manifeste."""
    out = []
    for role, ct in roles_with_containers:
        if ct:
            out += _entries(role["key"], role.get("label") or role["key"], ct, manifest_of, doc_of)
    return out


def should_push(prev, vals, age_s):
    """Pousser `vals` à un mur dont la dernière poussée acceptée valait `prev`, il y a `age_s` s ?"""
    if age_s is None or age_s >= REPUSH_S:
        return True
    return not (set(prev or {}) == set(vals) and all(same_value(prev[k], vals[k]) for k in vals))


def _tick(last):
    from .database import db_get_containers, db_roles_list
    from . import macros
    from .addressing import get_container_ip
    import requests
    walls, ips = {}, {}
    containers = db_get_containers()
    for ct in containers:
        try:
            dc = json.loads(ct.get("deploy_config") or "{}")
        except (TypeError, ValueError):
            continue
        if dc.get("type") != "multiview":
            continue
        refs = refs_in(dc.get("params"))
        if refs:
            walls[ct["vmid"]] = refs
    if not walls:
        last.clear()
        return False
    roles = {r["key"]: r.get("instance_uuid") for r in db_roles_list()}
    by_inst = {ct.get("instance_uuid"): ct["vmid"] for ct in containers if ct.get("instance_uuid")}
    project_of = {ct["vmid"]: ct.get("project_id") for ct in containers if ct["vmid"] in walls}
    project_hosts = {(ct.get("project_id"), ct.get("hostname")): ct["vmid"] for ct in containers
                     if ct.get("project_id") is not None and ct.get("hostname")}
    cache = {}
    from . import plugins
    from .tally import libelle_de
    type_of = {}
    for ct in containers:
        try:
            type_of[ct["vmid"]] = (json.loads(ct.get("deploy_config") or "{}") or {}).get("type")
        except (TypeError, ValueError):
            pass

    def ctx_of(vm, state):
        sdef = next((s for s in ((plugins.get(type_of.get(vm)) or {}).get("state") or [])
                     if s.get("id") == state), {})
        ep = sdef.get("endpoint") or "/state"
        return {"kind": sdef.get("type"),
                "answered": lambda: cache.get((vm, ep)) is not None,
                "raw": lambda path: macros.fetch_state(vm, endpoint="/state", path=path, cache=cache,
                                                       consumer="wall_states"),
                "label_of": lambda n: libelle_de(n, 2)}
    payloads = wall_payloads(walls, roles, by_inst,
                             lambda vm, sid: macros.fetch_state(vm, state_id=sid, cache=cache,
                                                                 consumer="wall_states"),
                             project_of=project_of, project_hosts=project_hosts, ctx_of=ctx_of)
    for wall, vals in payloads.items():
        t_push = _pushed_at.get(wall)
        if not should_push(last.get(wall), vals, None if t_push is None else time.monotonic() - t_push):
            continue
        ip = ips.get(wall) or get_container_ip(wall)
        if not ip:
            continue
        status = None
        try:
            for port in STATES_PORTS:
                status = requests.post("http://%s:%d/states" % (ip, port), json={"values": vals},
                                       timeout=1.0).status_code
                if status != 404:
                    break
        except Exception as e:
            status = "%s" % type(e).__name__
        if status == 200:
            last[wall] = vals
            _pushed_at[wall] = time.monotonic()
            _refused.pop(wall, None)
        elif _refused.get(wall) != status:
            # ★ Jamais muet : un refus laissait les %state% à « — » sans une ligne nulle part.
            _refused[wall] = status
            log.warning("wall states: wall %s refused the push (%s) — its %%state%% variables show '—'",
                        wall, status)
    for gone in set(last) - set(walls):
        last.pop(gone, None)
        _pushed_at.pop(gone, None)
        _refused.pop(gone, None)
    return True


def _loop():
    last = {}
    while not _stop.is_set():
        busy = False
        try:
            busy = _tick(last)
        except Exception as e:
            log.warning("wall states: tick failed (%s)", e)
        _stop.wait(PERIOD_S if busy else IDLE_PERIOD_S)


def start():
    """Contrôleur ACTIF seul (appelé depuis main.py, à côté des autres boucles)."""
    global _thr
    if _thr and _thr.is_alive():
        return
    _stop.clear()
    _thr = threading.Thread(target=_loop, name="wall-states", daemon=True)
    _thr.start()


def stop():
    _stop.set()
