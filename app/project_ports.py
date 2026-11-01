# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Ports virtuels de projet : résolution « option C » (décision du 2026-10-23).

Le PROJET ne retient que le virtuel : une entrée câblée sur un port s'écrit `port:<id>` dans
son snapshot, jamais le flux physique. Le CONTENEUR, lui, ne reçoit que du physique — les
plugins ignorent tout des ports. L'orchestrateur traduit à CHAQUE envoi (R1) et tient les liens à
jour à chaque changement de source (R2) — cf. « Les deux règles des ports » plus bas. Les gestes
qui posent ou rejouent un lien :

  - au câblage (`wire_port`) : `port:<id>` → flux affecté → `_apply_wire` ;
  - à la réaffectation d'un port (`replay_port`) : chaque entrée qui le référence est recâblée
    à chaud, y compris quand le port n'était pas affecté avant ;
  - au rechargement du projet (`materialize_params` puis `restore_links`).

Entre deux, la table `port_links` (instance_uuid du consommateur, essence, slot → port) dit
QUELLES entrées suivent quel port. C'est elle qui fait autorité ; le flux physique écrit dans
les params n'est que la résolution courante — ce qui a été envoyé au conteneur. Choisi plutôt
que d'écrire `port:<id>` dans les params vivants, que relisent une dizaine de consommateurs
côté orchestrateur (topologie, RDMA, contrôle de format, NMOS, tally…) qui ont besoin du
physique.

Limites connues, assumées :
  - les flux SUIVEURS (audio/ANC d'une entrée de multiview qui suit sa vidéo) sont recalculés
    par `_apply_wire` à chaque recâblage, mais ne sont pas virtualisés dans le snapshot : ils
    sont corrigés au rechargement par `restore_links`, pas avant ;
  - (levée le 2026-10-24) un câblage direct venu d'Ember+ ou de NMOS ne défaisait pas le lien :
    c'est désormais `_apply_wire` qui le fait pour tous les chemins, cf. `wire_check` ;
  - (levée le 2026-10-30) les `port_id` d'un snapshot étaient ceux de CETTE instance : l'export
    v4 emporte désormais les ports et `port_links`, re-mappés à l'import (`projects.export_ports`).
"""

import copy
import logging
import re

log = logging.getLogger(__name__)

_REF = re.compile(r"^port:(\d+)$")
_SHM_PREFIX = "/dev/shm/"           # seul préfixe de shm déclaré par les manifestes (shm_prefix)

# Essence d'un câble (vocabulaire du câblage) attendue pour chaque média de port.
MEDIA_KIND = {"video": "video", "audio": "audio", "anc": "data"}


def parse_ref(value):
    """`port:<id>` → id (int) ; toute autre valeur → None."""
    if not isinstance(value, str):
        return None
    m = _REF.match(value.strip())
    return int(m.group(1)) if m else None


def port_shm(port, get_port=None):
    """Flux physique d'un port : la source affectée (port source) ou la sortie interne publiée
    (port destination). "" si le port n'est pas affecté. `tally._port_shm` délègue ici.

    Un port source peut SUIVRE le port destination d'un autre projet (`binding.port`, câble
    inter-projets, décision du 2026-10-24) : son flux est alors celui que ce port publie EN CE
    MOMENT. Seule une destination peut être suivie, et une destination ne suit jamais rien :
    la chaîne a un maillon, aucune boucle n'est possible. `get_port(id)` : lecteur de port
    (défaut : la base) — le distributeur passe son instantané."""
    port = port or {}
    b = port.get("binding") or {}
    if port.get("kind") != "source":
        return b.get("internal_shm") or ""
    if b.get("port") is not None:
        if get_port is None:
            from .database import db_get_port as get_port
        try:
            up = get_port(int(b["port"]))
        except (TypeError, ValueError):
            up = None
        return port_shm(up, get_port) if up and up.get("kind") == "dest" else ""
    return b.get("shm") or ""


def followers(port_id):
    """Ports sources qui suivent ce port destination (câbles inter-projets)."""
    from .database import db_project_ports
    return [p for p in db_project_ports(None)
            if p.get("kind") == "source" and (p.get("binding") or {}).get("port") == port_id]


def follower_vmids(port_id):
    """Conteneurs dont une entrée SUIT ce port ou un de ses rattachés (`port_links`)."""
    from .database import db_port_links
    ids = [port_id] + [k["id"] for k in children(port_id)]
    out = set()
    for pid in ids:
        for l in db_port_links(port_id=pid):
            v = _vmid_for_uuid(l.get("consumer_uuid"))
            if v is not None:
                out.add(v)
    return out


def is_reusable(port, get_port=None):
    """Destination proposée en renvoi dans son projet : le drapeau de la vidéo de tête, que ses
    audios / son ANC suivent."""
    if not port or port.get("kind") != "dest":
        return False
    if port.get("parent_id"):
        from .database import db_get_port
        port = (get_port or db_get_port)(port["parent_id"]) or {}
    return bool(port.get("reusable"))


def norm_slot(to_slot):
    """Slot tel que le câblage le reçoit : le front envoie tantôt 0, tantôt "0". Une clé de
    document (texte non numérique) reste du texte."""
    if isinstance(to_slot, str) and to_slot.strip().lstrip("-").isdigit():
        return int(to_slot)
    return to_slot


def producer_vmid(shm):
    """vmid d'un conteneur qui produit `shm` (le plus petit s'il y en a plusieurs), ou None."""
    if not shm:
        return None
    from .auth import shm_producer_vmids
    vmids = shm_producer_vmids([shm]).get(shm) or set()
    return min(vmids) if vmids else None


def _uuid_for_vmid(vmid):
    from .database import db_get_container
    return (db_get_container(vmid) or {}).get("instance_uuid")


def _vmid_for_uuid(uuid):
    from .database import db_get_containers
    for c in db_get_containers():
        if c.get("instance_uuid") == uuid:
            return c["vmid"]
    return None


def current_input_shm(vmid, kind, to_slot):
    """Flux physique actuellement câblé sur l'entrée (essence, slot) du conteneur, d'après ses
    params. "" si rien."""
    from .database import db_get_container
    from .routes.shared import _load_dc
    c = db_get_container(vmid)
    dc = _load_dc(c) or {}
    if not c:
        return ""
    params = dc.get("params") or {}
    return get_input(dc.get("type"), params.get("hostname") or c.get("hostname") or "",
                     params, kind, to_slot)


# ─── Lire / écrire UNE entrée dans des params (2026-10-31) ─────────────────────────────────
#
# Un plugin porte une entrée de quatre façons : un crochet `wire_input` (streamer), un élément de
# DOCUMENT désigné par sa clé (`wiring_document`), un élément d'une LISTE à géométrie (multiview
# `flux_config`), ou un champ d'état (`state_field`). Ces deux fonctions sont PURES — ni réseau,
# ni base — : c'est sur elles que s'appuient la tenue des liens de port (R2) et la traduction
# avant envoi (R1), qui ne doivent rien envoyer d'elles-mêmes.

def _input_spec(t, hostname, params, kind, to_slot):
    """Spécification de l'entrée (essence, slot) d'après le manifeste, ou None. Même choix que le
    câblage : clé de document ; sinon slot exact, puis entrée sans slot ; sans slot, la première."""
    from . import plugins as _pl
    if not _pl.is_plugin(t):
        return None
    try:
        cons = _pl.derive_wiring(t, hostname or "", params or {})["consumes"]
    except Exception:
        return None
    cands = [x for x in cons if (x.get("essence") or "video") == (kind or "video")]
    slot = norm_slot(to_slot)
    keyed = [x for x in cands if x.get("key") is not None]
    if keyed:
        from .wiring_document import pick
        return pick(keyed, slot if slot is not None else 0)
    return (next((x for x in cands if x.get("slot") == slot), None) if slot is not None else None) \
        or next((x for x in cands if x.get("slot") is None), None) \
        or (cands[0] if cands and slot is None else None)


def get_input(t, hostname, params, kind, to_slot):
    """Flux physique de l'entrée (essence, slot) dans `params` : "" si rien ou inconnue."""
    spec = _input_spec(t, hostname, params, kind, to_slot)
    if not spec:
        return ""
    return _bare(spec.get("shm") or (params.get(spec["state_field"]) if spec.get("state_field") else "") or "")


def set_input(t, hostname, params, kind, to_slot, shm):
    """Copie de `params` où l'entrée (essence, slot) lit `shm` ("" = vidée), ou None si le
    manifeste ne dit pas où l'écrire. N'applique rien : c'est à l'appelant d'envoyer."""
    from . import plugins as _pl
    shm = _bare(shm)
    hook = _pl.get_hook(t, "wire_input" if shm else "unwire_input")
    if hook:
        res = hook(kind or "video", shm, norm_slot(to_slot), copy.deepcopy(params or {}),
                   {"vmid": None, "type": t, "hostname": hostname or "", "audio_shm": None})
        return res["params"] if res is not None else None
    spec = _input_spec(t, hostname, params, kind, to_slot)
    if not spec:
        return None
    if spec.get("key") is not None:
        from .wiring_document import with_field
        new, err = with_field(params, spec, spec["key"], shm, hostname or "")
        return None if err else new
    new = copy.deepcopy(params or {})
    if spec.get("from_list"):
        lst = list(new.get(spec["from_list"]) or [])
        slot = spec.get("index")
        if slot is None or not 0 <= slot < len(lst):
            return None
        entry = dict(lst[slot])
        entry[spec.get("shm_field", "shm")] = (spec.get("shm_prefix", "") + shm) if shm else ""
        lst[slot] = entry
        new[spec["from_list"]] = lst
        return new
    if spec.get("state_field"):
        new[spec["state_field"]] = shm
        return new
    return None


# ─── Les deux règles des ports (décision de Cyril, 31/10 : à la racine, pas de réconciliateur) ──
#
# R2 — tout changement de source tient `port_links` à jour : `input_changed`, appelée par le seul
#      écrivain de la configuration d'un conteneur (`database.db_update_deploy_config`).
# R1 — tout envoi vers un conteneur traduit depuis `port_links` : `resolve_inputs`, appelée via
#      `prepare_send` par `deployer_script` et par chaque envoi qui relit la base.
# Toujours R2 AVANT R1 : sinon un câblage direct (Ember+, NMOS, macro) serait aussitôt réécrit avec
# le flux de l'ancien port.

def _spec_id(spec):
    """Identité d'une entrée dans les params (deux slots qui désignent la même entrée → même id)."""
    if not spec:
        return None
    return (spec.get("essence") or "video", spec.get("from_list"), spec.get("state_field"),
            spec.get("key"), spec.get("slot"))


def input_changed(vmid, t, old_params, new_params):
    """R2. Compare chaque entrée de `vmid` entre `old_params` (enregistrés) et `new_params` (ceux
    qu'on enregistre) et tient les liens de port à jour :
      - entrée liée, nouveau flux = celui du port : le lien reste ;
      - entrée liée qui lit autre chose, ou plus rien : le lien suit le port source du projet qui
        apporte ce flux, sinon il est défait ;
      - entrée non liée qui se met à lire le flux d'un port source du projet : le lien est posé.
    Ne touche à rien d'autre. Retourne [(action, kind, slot, port_id)] pour les journaux et tests."""
    from .database import db_get_port, db_port_links, db_port_link_set, db_port_link_delete
    uuid = _uuid_for_vmid(vmid)
    if not uuid or not isinstance(new_params, dict):
        return []
    old_params = old_params if isinstance(old_params, dict) else {}
    hn = new_params.get("hostname") or old_params.get("hostname") or ""
    out, seen = [], set()
    for l in db_port_links(consumer_uuid=uuid):
        kind, slot = l["kind"], l["to_slot"]
        spec = _input_spec(t, hn, new_params, kind, slot)
        seen.add(_spec_id(spec))
        new_v = get_input(t, hn, new_params, kind, slot)
        if new_v == get_input(t, hn, old_params, kind, slot):
            continue
        port = db_get_port(l["port_id"])
        if port and new_v == port_shm(port):
            db_port_link_set(port["id"], uuid, kind, slot, new_v)
            continue
        follow = wire_check(vmid, new_v)[1] if new_v else None
        if follow is not None:
            db_port_link_set(follow, uuid, kind, slot, new_v)
            out.append(("moved", kind, slot, follow))
        else:
            db_port_link_delete(uuid, kind, slot)
            out.append(("dropped", kind, slot, l["port_id"]))
    from . import plugins as _pl
    try:
        cons = _pl.derive_wiring(t, hn, new_params)["consumes"] if _pl.is_plugin(t) else []
    except Exception:
        cons = []
    for spec in cons:
        if _spec_id(spec) in seen:
            continue
        kind, slot = spec.get("essence") or "video", spec.get("slot")
        new_v = get_input(t, hn, new_params, kind, slot)
        if not new_v or new_v == get_input(t, hn, old_params, kind, slot):
            continue
        follow = wire_check(vmid, new_v)[1]
        if follow is not None:
            db_port_link_set(follow, uuid, kind, norm_slot(slot), new_v)
            out.append(("linked", kind, slot, follow))
    if out:
        log.info("port links of #%s follow the source change: %s", vmid, out)
    return out


def keep_linked_inputs(vmid, t, params, seen_rev):
    """Envoi PÉRIMÉ (2026-11-01). `params` a été bâti sur la révision `seen_rev` de la config de
    `vmid` (une page ouverte plus tôt, qui renvoie TOUT ce qu'elle a chargé). Une entrée liée dont
    le flux a changé APRÈS cette révision (réaffectation du port) n'a pas été vue par le client :
    la valeur qu'il renvoie est l'ancienne, pas un recâblage. On garde la valeur actuelle, et le
    lien avec. Une entrée que le client a vue puis changée reste un recâblage (R2).
    Retourne (params, [(kind, slot, port_id)] des entrées gardées)."""
    import json
    from .database import db_get_container, db_port_links
    uuid = _uuid_for_vmid(vmid)
    try:
        seen = int(seen_rev)
    except (TypeError, ValueError):
        return params, []
    if not uuid or not isinstance(params, dict):
        return params, []
    dc = (db_get_container(vmid) or {}).get("deploy_config")
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    stored = (dc or {}).get("params") or {}
    hn = params.get("hostname") or stored.get("hostname") or ""
    kept = []
    for l in db_port_links(consumer_uuid=uuid):
        if int(l.get("rev") or 0) <= seen:
            continue
        kind, slot = l["kind"], l["to_slot"]
        cur = get_input(t, hn, stored, kind, slot)
        if get_input(t, hn, params, kind, slot) == cur:
            continue
        new = set_input(t, hn, params, kind, slot, cur)
        if new is None:
            continue
        params = new
        kept.append((kind, slot, l["port_id"]))
    if kept:
        log.info("stale send to #%s (rev %s): linked inputs kept %s", vmid, seen, kept)
    return params, kept


def resolve_inputs(vmid, t, params):
    """R1. Copie de `params` où chaque entrée liée à un port lit le flux du port EN CE MOMENT
    ("" s'il n'est pas affecté). Le lien retient ce flux. Une entrée que le manifeste ne sait plus
    désigner est laissée telle quelle (journal)."""
    from .database import db_get_port, db_port_links, db_port_link_set
    uuid = _uuid_for_vmid(vmid)
    if not uuid or not isinstance(params, dict):
        return params
    hn = params.get("hostname") or ""
    for l in db_port_links(consumer_uuid=uuid):
        port = db_get_port(l["port_id"])
        if not port:
            continue
        want = port_shm(port)
        if get_input(t, hn, params, l["kind"], l["to_slot"]) != want:
            new = set_input(t, hn, params, l["kind"], l["to_slot"], want)
            if new is None:
                log.warning("port %s: input %s/%s of #%s cannot be written", port["id"], l["kind"],
                            l["to_slot"], vmid)
                continue
            params = new
        if l.get("shm") != want:
            db_port_link_set(port["id"], uuid, l["kind"], l["to_slot"], want)
    return params


def prepare_send(vmid, t, params):
    """Params à envoyer à `vmid` : R2 (les liens suivent ce que l'appelant a changé par rapport à
    la base) puis R1 (chaque entrée liée lit le flux actuel de son port). Le point UNIQUE par où
    passe tout envoi vers un conteneur."""
    import json
    from .database import db_get_container
    try:
        # Lecture directe (pas `routes.shared._load_dc`) : appelée depuis des fils de fond, elle ne
        # doit pas charger le paquet des routes Flask.
        dc = (db_get_container(vmid) or {}).get("deploy_config")
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
        stored = (dc or {}).get("params") or {}
        input_changed(vmid, t, stored, params)
        return resolve_inputs(vmid, t, params)
    except Exception as e:                                                 # noqa: BLE001
        log.warning("prepare_send #%s: %s", vmid, e)
        return params


def _apply(vmid, kind, to_slot, new_shm, old_shm, force=False):
    """Pose `new_shm` sur l'entrée, ou la vide (`old_shm` décâblé) quand le port n'a rien."""
    from .routes.cabling import _apply_wire, _apply_unwire
    if new_shm:
        return _apply_wire(producer_vmid(new_shm), vmid, new_shm, kind, to_slot, force=force,
                           via_port=True)
    if old_shm:
        return _apply_unwire(vmid, old_shm, kind, to_slot)
    return True, 200, {}


def wire_port(port_id, to_vmid, kind, to_slot, force=False):
    """Câble l'entrée (kind, slot) de `to_vmid` sur le port : résolution + `_apply_wire` + lien.
    Port non affecté : l'entrée est vidée et le lien posé quand même — l'affectation du port la
    remplira. Retourne (ok, status, payload) comme `_apply_wire`."""
    from .database import db_get_port, db_port_link_set
    port = db_get_port(port_id)
    if not port:
        return False, 404, {"error": "port introuvable"}
    uuid = _uuid_for_vmid(to_vmid)
    if not uuid:
        return False, 404, {"error": f"container #{to_vmid} introuvable"}
    slot = norm_slot(to_slot)
    shm = port_shm(port)
    if port.get("kind") == "dest" and shm and producer_vmid(shm) == int(to_vmid):
        # Renvoi (2026-10-29) : un conteneur ne lit pas une destination qu'il alimente lui-même.
        return False, 409, {"error": "port_loop", "reason": "port_loop", "port_id": port_id}
    ok, status, payload = _apply(to_vmid, kind, slot, shm,
                                 "" if shm else current_input_shm(to_vmid, kind, slot), force)
    if not ok:
        return ok, status, payload
    db_port_link_set(port_id, uuid, kind, slot, shm)
    payload = dict(payload or {}, port_id=port_id)
    if not shm:
        payload["port_unassigned"] = True
    return True, status, payload


# ─── Frontière du projet (décision du 2026-10-24) ───────────────────────────────────────────
#
# Une entrée d'un conteneur de projet ne lit que ce que le projet produit, ou ce qu'un de ses
# ports SOURCES apporte. Pour tout le monde, admin compris, et pour tous les chemins de câblage
# (page Câbles, Ember+, NMOS, instantanés de câblage, insertion d'UDC) : la garde vit dans
# `_apply_wire`, par où ils passent tous. Un flux extérieur qui est justement celui affecté à un
# port est accepté et enregistré comme SUIVANT ce port — le câble reste juste au prochain rappel.
# Au rechargement, une entrée qui lisait un flux extérieur hors port est VIDÉE
# (`strip_external_inputs`), pas rejouée.

def _bare(s):
    s = (s or "").strip()
    return s[len(_SHM_PREFIX):] if s.startswith(_SHM_PREFIX) else s


def project_port_flows(pids):
    """{flux physique: id du port} des ports SOURCES affectés des projets `pids` (le plus petit id
    quand deux ports portent le même flux)."""
    from .database import db_project_ports
    ports = db_project_ports(None)
    by_id = {p["id"]: p for p in ports}
    out = {}
    for p in sorted(ports, key=lambda x: x["id"]):
        if p["project_id"] in pids and p["kind"] == "source":
            s = port_shm(p, by_id.get)
            if s:
                out.setdefault(s, p["id"])
    return out


def member_project_ids(vmid, _projects=None):
    """Projets dont `vmid` est VRAIMENT membre : rattachement direct, ou présence dans un snapshot
    dont l'entrée désigne bien CE conteneur (même instance_uuid, sinon même hostname). Le vmid
    seul ne suffit pas : c'est un handle jetable, réattribué — un snapshot ancien rattacherait
    sinon au projet un conteneur sans rapport, et la frontière refuserait ses câbles."""
    from .auth import vmid_project_ids
    from .database import db_get_container, db_get_projects
    pids = vmid_project_ids(vmid)
    if not pids:
        return set()
    c = db_get_container(vmid) or {}
    out = {c["project_id"]} & pids if c.get("project_id") else set()
    rest = pids - out
    if not rest:
        return out
    for pr in (_projects if _projects is not None else db_get_projects()):
        if pr["id"] not in rest:
            continue
        for sc in pr.get("snapshot") or []:
            if sc.get("vmid") != vmid:
                continue
            u, su = c.get("instance_uuid"), sc.get("instance_uuid")
            if (u and su and u == su) or (not (u and su) and sc.get("hostname") == c.get("hostname")):
                out.add(pr["id"])
    return out


def wire_check(to_vmid, shm, extra_shms=()):
    """Frontière du projet pour un câble vers `to_vmid`. Retourne (refusés, port suivi) :
    `refusés` = flux que ni le projet ne produit ni aucun de ses ports n'apporte ; `port suivi` =
    id du port dont `shm` (le flux principal) est l'affectation, quand c'est par là qu'il passe.
    Conteneur hors projet : ([], None)."""
    from .auth import shm_producer_vmids
    from .database import db_get_projects
    projects = db_get_projects()
    pids = member_project_ids(to_vmid, projects)
    if not pids:
        return [], None
    main = _bare(shm)
    want = [s for s in [main] + [_bare(x) for x in extra_shms] if s]
    prods = shm_producer_vmids(want)
    flows, refused, follow = None, [], None
    for s in want:
        if any(member_project_ids(v, projects) & pids for v in prods.get(s) or ()):
            continue
        if flows is None:
            flows = project_port_flows(pids)
        if s in flows:
            if s == main and follow is None:
                follow = flows[s]
            continue
        if s not in refused:
            refused.append(s)
    return refused, follow


_FLOW_KEYS = frozenset(("shm", "shm_name", "path", "source", "input", "flow", "src"))


def _body_flow_candidates(body, out=None):
    out = set() if out is None else out
    if isinstance(body, dict):
        for k, v in body.items():
            if isinstance(v, str) and str(k).lower() in _FLOW_KEYS and v.strip():
                out.add(_bare(v))
            elif isinstance(v, (dict, list)):
                _body_flow_candidates(v, out)
    elif isinstance(body, list):
        for v in body:
            _body_flow_candidates(v, out)
    return out


def body_flow_refusal(vmid, body):
    """Frontière du projet pour une commande de CONTRÔLE (relais des plugins, action de macro) :
    une requête qui pointe une entrée sur un flux (`POST /input {shm}`, tuile de multiview
    `{path}`…) ne passe pas par `_apply_wire`. Retourne les flux refusés ([] = rien à redire).
    Ne retient que les valeurs qui SONT des flux produits quelque part : une URL, un nom de
    fichier ou un flux que personne ne produit ne branchent rien. Bon marché quand le corps ne
    nomme aucun flux — le cas d'un fader, appelé à haute cadence."""
    cands = _body_flow_candidates(body) - {""}
    if not cands:
        return []
    if not member_project_ids(vmid):
        return []
    from .auth import shm_producer_vmids
    prods = shm_producer_vmids(sorted(cands))
    known = sorted(c for c in cands if prods.get(c))
    if not known:
        return []
    refused, _ = wire_check(vmid, known[0], known[1:])
    return refused


def _clear_by_spec(params, spec, hostname=""):
    """Copie de `params` où l'entrée décrite par `spec` (consommateur de `derive_wiring`) est vidée,
    ou None si la forme n'est pas connue."""
    if spec.get("key") is not None:
        from .wiring_document import with_field
        new, err = with_field(params, spec, spec["key"], "", hostname)
        return None if err else new
    new = copy.deepcopy(params or {})
    if spec.get("from_list"):
        lst = list(new.get(spec["from_list"]) or [])
        slot = spec.get("index")
        if slot is None or not 0 <= slot < len(lst):
            return None
        lst[slot] = dict(lst[slot], **{spec.get("shm_field", "shm"): ""})
        new[spec["from_list"]] = lst
        return new
    if spec.get("state_field"):
        new[spec["state_field"]] = ""
        return new
    return None


def strip_external_inputs(dc, produced):
    """Rechargement : vide chaque entrée qui lit un flux que le projet ne produit pas (`produced`)
    et qui n'est pas un port. Retourne (params, flux retirés). À appeler AVANT
    `materialize_params` : un port s'écrit encore `port:<id>` et reste intact.
    Seules les ENTRÉES déclarées par le manifeste sont vidées (`set_input`) : un champ qui n'est
    pas une entrée (ex. `afv.follow` de la console audio) peut porter le même nom de flux sans
    que le conteneur le LISE — le vider le détruirait pour rien (audit du 31/10, point C)."""
    from . import plugins as _pl
    params = (dc or {}).get("params") or {}
    t = (dc or {}).get("type")
    if not _pl.is_plugin(t):
        return params, []
    hn = params.get("hostname") or ""
    try:
        cons = _pl.derive_wiring(t, hn, params)["consumes"]
    except Exception:
        return params, []
    ext, out = set(), params
    for spec in cons:
        s = _bare(spec.get("shm") or (params.get(spec["state_field"]) if spec.get("state_field") else ""))
        if not s or parse_ref(s) is not None or s in produced:
            continue
        ext.add(s)
        kind, slot = spec.get("essence") or "video", spec.get("slot")
        if _spec_id(_input_spec(t, hn, out, kind, slot)) != _spec_id(spec):
            # Entrée que (essence, slot) ne désigne pas seule (vu-mètres `meter_blocks` du
            # multiview, découpes de decklink_io…) : la frontière du projet vaut aussi pour elle,
            # on la vide par sa propre spécification.
            new = _clear_by_spec(out, spec, hn)
            if new is not None:
                out = new
            continue
        if get_input(t, hn, out, kind, slot) == s:
            new = set_input(t, hn, out, kind, slot, "")
            if new is not None:
                out = new
    if not ext:
        return params, []
    return out, sorted(ext)


def forget_input(to_vmid, kind, to_slot=None, shm=None):
    """Un câblage DIRECT ou un décâblage remplace ce que l'entrée suivait : on oublie le lien.
    Par slot quand on le connaît (câblage), sinon par flux physique (le décâblage ne donne que
    le shm)."""
    from .database import db_port_links, db_port_link_delete
    uuid = _uuid_for_vmid(to_vmid)
    if not uuid:
        return
    if shm is None:
        db_port_link_delete(uuid, kind, norm_slot(to_slot))
        return
    for link in db_port_links(consumer_uuid=uuid):
        if link["kind"] == kind and link["shm"] == shm:
            db_port_link_delete(uuid, kind, link["to_slot"])


def unwire_port_ref(port_id, to_vmid, kind):
    """Décâbler `port:<id>` d'une entrée : vide ce qu'il y avait et oublie le lien."""
    from .database import db_port_links, db_port_link_delete
    uuid = _uuid_for_vmid(to_vmid)
    links = [l for l in db_port_links(port_id=port_id, consumer_uuid=uuid) if l["kind"] == kind] \
        if uuid else []
    if not links:
        return False, 404, {"error": "aucune entrée liée à ce port"}
    for link in links:
        if link["shm"]:
            from .routes.cabling import _apply_unwire
            ok, status, payload = _apply_unwire(to_vmid, link["shm"], kind, link["to_slot"])
            if not ok:
                return ok, status, payload
        db_port_link_delete(uuid, kind, link["to_slot"])
    return True, 200, {"to_vmid": to_vmid, "port_id": port_id}


def replay_port(port_id, release=False):
    """Réaffectation : recâble à chaud chaque entrée liée au port sur son flux ACTUEL (ou la
    vide). `release=True` (port supprimé) vide les entrées et efface les liens. Les liens dont
    le conteneur n'existe plus (projet déchargé) sont effacés : le snapshot les garde.
    Retourne [{vmid, ok, error?}]."""
    from .database import db_get_port, db_port_links, db_port_link_set, db_port_link_delete
    port = db_get_port(port_id)
    shm = "" if (release or not port) else port_shm(port)
    out = []
    for link in db_port_links(port_id=port_id):
        vmid = _vmid_for_uuid(link["consumer_uuid"])
        if vmid is None:
            db_port_link_delete(link["consumer_uuid"], link["kind"], link["to_slot"])
            continue
        try:
            ok, _st, payload = (True, 200, {}) if (shm and shm == link["shm"]) else \
                _apply(vmid, link["kind"], link["to_slot"], shm, link["shm"])
        except Exception as e:
            ok, payload = False, {"error": str(e)}
        if ok and not release:
            db_port_link_set(port_id, link["consumer_uuid"], link["kind"], link["to_slot"], shm)
        elif release:
            db_port_link_delete(link["consumer_uuid"], link["kind"], link["to_slot"])
        out.append({"vmid": vmid, "ok": bool(ok),
                    **({} if ok else {"error": (payload or {}).get("error")})})
    # Les ports qui SUIVENT celui-ci suivent aussi ; port supprimé → ils deviennent non affectés
    # (affectation effacée : pas de référence vers un port disparu).
    from .database import db_update_port
    for f in followers(port_id):
        if release:
            db_update_port(f["id"], binding={})
        out += replay_port(f["id"])
    return out


# ─── Virtuelles : une vidéo et ses flux rattachés (2026-10-28) ─────────────────────────────
#
# Une « virtuelle » se compose : une vidéo, 0..8 audios (un flux MXL de 8 canaux chacun), un ANC.
# Avec une vidéo, c'est elle le PARENT : les autres ports lui sont rattachés (`parent_id`, `suffix`
# A1, A2…, Anc), se raccordent AVEC elle et héritent de ses libellés (`tally._port_row`). Sans
# vidéo, chaque flux est un port indépendant. Décisions de Cyril du 28/10 (maquette A).

MAX_AUDIOS = 8
ANC_SUFFIX = "Anc"


def children(port_id, ports=None):
    """Ports rattachés à ce port, audios (A1, A2…) puis ANC."""
    from .database import db_project_ports
    pid_ports = ports if ports is not None else db_project_ports(None)
    kids = [p for p in pid_ports if p.get("parent_id") == port_id]

    def rank(p):
        sfx = p.get("suffix") or ""
        return (1, 0) if sfx == ANC_SUFFIX else (0, int(sfx[1:]) if sfx[1:].isdigit() else 99)
    return sorted(kids, key=rank)


def split_companions(produces, video_shm):
    """Audios et ANC qui vont avec `video_shm` parmi les flux d'UN producteur : {"audio": [...],
    "anc": [...]}. Même règle de rang que « câbler aussi l'audio associé » de la page Câbles
    (`associatedAudioShm`) : la k-ième vidéo va avec le k-ième lot ; un producteur à une vidéo
    donne tous ses audios, dans l'ordre."""
    # `essence` : la forme de derive_wiring (les manifestes) ; `kind` : celle de la topologie.
    ess = lambda p: p.get("kind") or p.get("essence")   # noqa: E731
    vids = [p.get("shm") for p in produces if ess(p) == "video"]
    out = {}
    for key, kind in (("audio", "audio"), ("anc", "data")):
        flows = [p.get("shm") for p in produces if ess(p) == kind]
        if len(vids) <= 1 or video_shm not in vids:
            out[key] = flows
            continue
        per = max(1, len(flows) // len(vids))
        k = vids.index(video_shm)
        out[key] = flows[k * per:(k + 1) * per]
    return out


def produces_of_shm(shm):
    """Flux déclarés par le conteneur qui produit `shm` ([{shm, kind}]), [] s'il n'y en a pas."""
    vmid = producer_vmid(shm)
    if vmid is None:
        return []
    from . import plugins as _plugins
    from .database import db_get_container
    from .routes.shared import _load_dc
    c = db_get_container(vmid) or {}
    dc = _load_dc(c) or {}
    p = dc.get("params") or {}
    try:
        return _plugins.derive_wiring(dc.get("type"), p.get("hostname") or c.get("hostname")
                                      or f"mxl{vmid}", p)["produces"]
    except Exception as e:
        log.warning("project ports: produces of #%s not derived (%s)", vmid, e)
        return []


def child_bindings(parent, kids):
    """{child_id: binding} que l'affectation de la vidéo parente impose à ses ports rattachés.

    Affectée à un flux → les audios / l'ANC du MÊME producteur, au même rang ; un rattaché sans
    flux correspondant devient non affecté. Suivant un port d'un autre projet → chaque rattaché
    suit le rattaché de même suffixe de ce port, s'il existe. Non affectée → tous non affectés."""
    b = parent.get("binding") or {}
    out = {k["id"]: {} for k in kids}
    if b.get("port") is not None:
        up_kids = {k.get("suffix"): k["id"] for k in children(int(b["port"]))}
        for k in kids:
            if k.get("suffix") in up_kids:
                out[k["id"]] = {"port": up_kids[k["suffix"]]}
        return out
    field = "shm" if parent.get("kind") == "source" else "internal_shm"
    vshm = b.get(field)
    if not vshm:
        return out
    comp = split_companions(produces_of_shm(vshm), vshm)
    audios = iter(comp["audio"])
    for k in kids:
        if k.get("media") == "anc":
            flow = comp["anc"][0] if comp["anc"] else None
        else:
            flow = next(audios, None)
        if flow:
            out[k["id"]] = {field: flow}
    return out


def rebind_children(parent_id):
    """Applique `child_bindings` et rejoue les entrées qui suivent chaque rattaché. [{vmid, ok…}]."""
    from .database import db_get_port, db_update_port
    parent = db_get_port(parent_id)
    if not parent:
        return []
    out = []
    for kid_id, binding in child_bindings(parent, children(parent_id)).items():
        db_update_port(kid_id, binding=binding)
        out += replay_port(kid_id)
    return out


# ─── Commuter une sortie du projet (2026-10-30) ────────────────────────────────────────────
#
# Une DESTINATION publie un flux interne du projet (`binding.internal_shm`) ; la commuter, c'est
# changer ce flux. Trois appelants font le même geste : la fenêtre Ports du projet (PATCH du port),
# le pupitre Dyvi (zone Sorties) et les macros (`app/project_routing.py`). Le geste vit donc ICI,
# une fois : une garde recopiée dans un seul appelant ne protège que celui-là.

def project_flows(pid):
    """Flux que produisent les conteneurs du projet, ceux qu'une destination peut publier :
    [{shm, kind, label, vmid, hostname}]. Même appartenance que le résumé du projet
    (`/api/projects/<pid>/summary`) : rattachement direct OU référencé par le snapshot."""
    from . import plugins as _plugins
    from .database import db_get_containers, db_get_project
    from .routes.shared import _load_dc
    proj = db_get_project(pid) or {}
    snap_vmids = {sc.get("vmid") for sc in (proj.get("snapshot") or [])}
    out = []
    for c in sorted(db_get_containers(), key=lambda x: (x.get("hostname") or "", x["vmid"])):
        if c.get("project_id") != pid and c["vmid"] not in snap_vmids:
            continue
        dc = _load_dc(c) or {}
        kind, p = dc.get("type"), dc.get("params") or {}
        if not _plugins.is_plugin(kind):
            continue
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        try:
            prods = _plugins.derive_wiring(kind, hn, p)["produces"]
        except Exception as e:
            log.warning("project ports: produces of #%s not derived (%s)", c["vmid"], e)
            continue
        for pr in prods:
            if pr.get("shm"):
                out.append({"shm": pr["shm"], "kind": pr.get("essence") or "video", "label": pr.get("label") or "",
                            "vmid": c["vmid"], "hostname": c.get("hostname") or hn})
    return out


def apply_binding(port, binding):
    """Écrit l'affectation d'un port puis fait suivre ce qui en dépend : entrées liées
    (`replay_port`), rattachés d'une virtuelle, et — pour une destination — les consommateurs
    EXTERNES câblés en direct sur l'ancienne sortie interne. Retourne [{vmid, ok, error?}].
    Les contrôles (droits, forme du binding, boucle) sont ceux de l'appelant."""
    from .database import db_update_port
    old = port.get("binding") or {}
    db_update_port(port["id"], binding=binding)
    rewired = replay_port(port["id"])
    # Virtuelle : ses audios / son ANC se raccordent AVEC la vidéo (décision du 28/10).
    rewired += rebind_children(port["id"])
    if port["kind"] != "source" and old.get("internal_shm") and binding.get("internal_shm"):
        # Les entrées liées ont déjà quitté l'ancien flux ci-dessus : elles ne sont pas revues.
        from .routes.projects_api import _rewire_shm_consumers
        rewired += _rewire_shm_consumers(old.get("internal_shm"), binding.get("internal_shm"),
                                         exclude_pids={port["project_id"]})
    return rewired


def admit_flow(pid, shm, kind="video"):
    """Fait ENTRER un flux extérieur dans le projet par un port source (TODO de Cyril, 31/10 :
    proposé par la page Câbles au lieu du refus « source hors projet »). Réutilise le port source
    de tête déjà affecté à ce flux, sinon en crée un, nommé d'après le libellé de la source, et
    l'affecte. Retourne (port_id, created). Le câblage de l'entrée reste à faire (`wire_port`)."""
    from .database import db_get_port, db_project_ports
    shm = _bare(shm)
    media = {"video": "video", "audio": "audio", "data": "anc"}.get(kind or "video", "video")
    for p in db_project_ports(pid):
        if p.get("kind") == "source" and not p.get("parent_id") and p.get("media") == media \
                and (p.get("binding") or {}).get("shm") == shm:
            return p["id"], False
    from .tally import libelle_de
    base = (libelle_de(shm, 2) or shm).strip() or shm
    name, n = base, 2
    while duplicate_names(pid, [name]):
        name, n = "%s %d" % (base, n), n + 1
    new = create_virtuals(pid, "source", [name], video=media == "video",
                          audios=1 if media == "audio" else 0, anc=media == "anc")[0]
    apply_binding(db_get_port(new), {"shm": shm})
    return new, True


def route_dest(port_id, internal_shm):
    """Commute la destination `port_id` sur le flux interne `internal_shm` ("" = ne plus rien
    publier). Retourne (ok, reason, rewired) ; reason ∈ not_found | not_a_dest | child |
    not_in_project | kind_mismatch | port_loop. Un rattaché (audio, ANC d'une virtuelle) suit sa vidéo : on commute
    la vidéo. Le flux doit être produit par un conteneur DU projet — la frontière du projet
    (décision du 2026-10-24) vaut pour toutes les surfaces."""
    from .database import db_get_port
    port = db_get_port(port_id)
    if not port:
        return False, "not_found", []
    if port.get("kind") != "dest":
        return False, "not_a_dest", []
    if port.get("parent_id"):
        return False, "child", []
    shm = (internal_shm or "").strip()
    if shm:
        flow = next((f for f in project_flows(port["project_id"]) if f["shm"] == shm), None)
        if flow is None:
            return False, "not_in_project", []
        # A video output publishes a video flow: the Ports window filters its list, a macro or a
        # panel key must be refused the same.
        if MEDIA_KIND.get(port.get("media")) != flow["kind"]:
            return False, "kind_mismatch", []
        # Renvoi : la destination ne publie pas la sortie d'un conteneur qui la suit (boucle).
        prod = producer_vmid(shm)
        if prod is not None and prod in follower_vmids(port_id):
            return False, "port_loop", []
    if shm == ((port.get("binding") or {}).get("internal_shm") or ""):
        return True, None, []
    binding = dict(port.get("binding") or {})
    if shm:
        binding["internal_shm"] = shm
    else:
        binding.pop("internal_shm", None)
    return True, None, apply_binding(port, binding)


def next_suffix(parent_id, media):
    """Suffixe du prochain rattaché : A<n> (premier numéro libre) ou Anc ; None si refusé."""
    kids = children(parent_id)
    if media == "anc":
        return None if any(k.get("suffix") == ANC_SUFFIX for k in kids) else ANC_SUFFIX
    used = {k.get("suffix") for k in kids if k.get("media") == "audio"}
    if len(used) >= MAX_AUDIOS:
        return None
    n = 1
    while "A%d" % n in used:
        n += 1
    return "A%d" % n


def add_child(parent, media):
    """Rattache un audio ou un ANC à la virtuelle `parent` (port vidéo). Retourne l'id ou None."""
    from .database import db_create_port
    if parent.get("media") != "video" or parent.get("parent_id") or media not in ("audio", "anc"):
        return None
    sfx = next_suffix(parent["id"], media)
    if not sfx:
        return None
    return db_create_port(parent["project_id"], parent["kind"], media,
                          "%s %s" % (parent["name"], sfx), ord_=parent.get("ord") or 0,
                          parent_id=parent["id"], suffix=sfx)


def duplicate_names(pid, names):
    """Noms déjà pris dans le projet (ports, rattachés compris ; sans casse) ou répétés dans `names`."""
    from .database import db_project_ports
    taken = {(p.get("name") or "").strip().lower() for p in db_project_ports(pid)}
    seen, dups = set(), []
    for n in names:
        key = n.strip().lower()
        if key in taken or key in seen:
            dups.append(n)
        seen.add(key)
    return dups


def create_virtuals(pid, kind, names, video=True, audios=0, anc=False):
    """Crée une virtuelle par nom ; retourne les ids des ports de tête (vidéo, ou chaque flux seul).

    Avec une vidéo : la vidéo + ses rattachés (A1…An, Anc). Sans vidéo : chaque flux est un port
    indépendant, nommé d'après la virtuelle (« Mic 1 » ; « Mic 1 A2 » s'il y a plusieurs audios)."""
    from .database import db_create_port, db_project_ports, db_get_port
    audios = max(0, min(MAX_AUDIOS, int(audios or 0)))
    ord_ = max([p.get("ord") or 0 for p in db_project_ports(pid) if p.get("kind") == kind] or [-1])
    heads = []
    for name in names:
        ord_ += 1
        if video:
            vid = db_create_port(pid, kind, "video", name, ord_=ord_)
            heads.append(vid)
            parent = db_get_port(vid)
            for _ in range(audios):
                add_child(parent, "audio")
            if anc:
                add_child(parent, "anc")
            continue
        flows = [("audio", ("%s A%d" % (name, i + 1)) if audios > 1 else name) for i in range(audios)]
        if anc:
            flows.append(("anc", "%s %s" % (name, ANC_SUFFIX) if audios else name))
        for media, n in flows:
            heads.append(db_create_port(pid, kind, media, n, ord_=ord_))
    return heads


def virtual_port_names(names, video=True, audios=0, anc=False):
    """Tous les noms de ports que `create_virtuals` créerait (pour le contrôle des doublons)."""
    out = []
    for name in names:
        if video:
            out.append(name)
            out += ["%s A%d" % (name, i + 1) for i in range(audios)]
            out += ["%s %s" % (name, ANC_SUFFIX)] if anc else []
        else:
            out += [("%s A%d" % (name, i + 1)) if audios > 1 else name for i in range(audios)]
            out += [("%s %s" % (name, ANC_SUFFIX)) if audios else name] if anc else []
    return out


# ─── Snapshot : le projet ne retient que le virtuel ───────────────────────────────────────

def _map_strings(obj, fn):
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    return fn(obj) if isinstance(obj, str) else obj


def virtualize_snapshot_entry(c):
    """Entrée de snapshot (deploy_config décodé) : chaque ENTRÉE posée par un lien de port est
    réécrite `port:<id>` dans les params (via `set_input`, au seul endroit que le manifeste
    déclare), et la liste des liens est jointe (`port_links`) — elle seule garde la trace d'un
    port NON affecté, qui n'a rien laissé dans les params. Aucun autre champ ne change : un champ
    qui n'est pas une entrée (ex. `afv.follow`) peut porter le même nom de flux sans être lié au
    port (audit du 31/10, point C). Sans lien, l'entrée ressort telle quelle."""
    from .database import db_port_links
    uuid = c.get("instance_uuid")
    links = db_port_links(consumer_uuid=uuid) if uuid else []
    if not links:
        return c
    c = dict(c)
    c["port_links"] = [{"port_id": l["port_id"], "kind": l["kind"], "to_slot": l["to_slot"]}
                       for l in links]
    dc = c.get("deploy_config")
    if isinstance(dc, dict) and any(l["shm"] for l in links):
        dc = dict(dc)
        params = dc.get("params") or {}
        t, hn = dc.get("type"), params.get("hostname") or ""
        for l in links:
            if l["shm"] and get_input(t, hn, params, l["kind"], l["to_slot"]) == l["shm"]:
                new = set_input(t, hn, params, l["kind"], l["to_slot"], "port:%d" % l["port_id"])
                if new is not None:
                    params = new
        dc["params"] = params
        c["deploy_config"] = dc
    return c


def materialize_params(params, t=None, hostname=None):
    """Rechargement : `port:<id>` → flux affecté MAINTENANT. C'est ce qui part au conteneur ; le
    virtuel reste dans le snapshot.
      - une ENTRÉE (déclarée par le manifeste du type `t`) qui lit `port:<id>` reçoit le flux du
        port, ou "" si le port n'est pas affecté ou n'existe plus : jamais l'ancien flux ;
      - toute AUTRE chaîne `port:<id>` (snapshot d'avant la restriction aux entrées, où elle
        avait été écrite dans un champ qui n'est pas une entrée) devient le flux du port s'il est
        affecté, sinon reste TELLE QUELLE : la détruire perdrait la donnée pour de bon.
    Sans `t`, seul ce second traitement s'applique."""
    from .database import db_get_port
    cache = {}

    def _shm_of(pid):
        if pid not in cache:
            cache[pid] = port_shm(db_get_port(pid))
        return cache[pid]

    out = copy.deepcopy(params or {})
    if t:
        hn = hostname or out.get("hostname") or ""
        try:
            from . import plugins as _pl
            cons = _pl.derive_wiring(t, hn, out)["consumes"] if _pl.is_plugin(t) else []
        except Exception:
            cons = []
        for spec in cons:
            kind, slot = spec.get("essence") or "video", spec.get("slot")
            if _spec_id(_input_spec(t, hn, out, kind, slot)) != _spec_id(spec):
                continue                # entrée non adressable seule : traitée par le repli
            pid = parse_ref(get_input(t, hn, out, kind, slot))
            if pid is not None:
                new = set_input(t, hn, out, kind, slot, _shm_of(pid))
                if new is not None:
                    out = new

    def _mat(s):
        pid = parse_ref(s)
        if pid is not None:
            return _shm_of(pid) or s
        if s.startswith(_SHM_PREFIX):
            pid = parse_ref(s[len(_SHM_PREFIX):])
            if pid is not None:
                v = _shm_of(pid)
                return (_SHM_PREFIX + v) if v else s
        return s

    return _map_strings(out, _mat)


def restore_links(vmid, port_links):
    """Après le déploiement d'un conteneur rechargé : recrée ses liens et recâble chaque entrée
    dont le flux ne correspond pas à l'affectation actuelle du port (port affecté depuis le
    snapshot, deux ports sur un même flux, suiveurs). Retourne [{port_id, ok, error?}]."""
    from .database import db_get_port, db_port_link_set
    uuid = _uuid_for_vmid(vmid)
    out = []
    if not uuid:
        return out
    for l in port_links or []:
        port = db_get_port(l.get("port_id"))
        if not port:
            out.append({"port_id": l.get("port_id"), "ok": False, "error": "port introuvable"})
            continue
        kind, slot = l.get("kind") or "video", norm_slot(l.get("to_slot"))
        shm = port_shm(port)
        cur = current_input_shm(vmid, kind, slot)
        ok, payload = True, {}
        if cur != shm:
            try:
                ok, _st, payload = _apply(vmid, kind, slot, shm, cur)
            except Exception as e:
                ok, payload = False, {"error": str(e)}
        db_port_link_set(port["id"], uuid, kind, slot, shm if ok else cur)
        out.append({"port_id": port["id"], "ok": bool(ok),
                    **({} if ok else {"error": (payload or {}).get("error")})})
    return out


# ─── Libellés des ports (étape 3, décision du 2026-10-24) ───────────────────────────────────
#
# Un conteneur ne connaît que le flux physique ; c'est donc ICI qu'on remonte du flux au port,
# du point de vue du conteneur qui affiche (`input_port_id`). Les libellés d'un port vivent dans
# la table des libellés sous la clé `port:<id>`, avec les mêmes colonnes qu'une source : chaque
# consommateur garde la colonne qu'il a choisie. Nom long et nom court = deux de ces colonnes,
# désignées par le réglage `port_label_cols`.
#
# Par port, `label_source` choisit la LIGNE lue, entière : `port` (la ligne `port:<id>`) ou
# `origin` (celle du flux affecté en ce moment). Jamais de mélange entre les deux — cf.
# `tally.row_ref`.

LABEL_SOURCES = ("port", "origin")
TSL_IN_MODES = ("port", "external")
LABEL_COLS_DEFAULT = {"long": 2, "short": 3}
_IDX_TTL_S = 2.0
_idx = {"ts": 0.0, "links": {}, "slots": {}, "proj": {}, "cols": dict(LABEL_COLS_DEFAULT)}


def port_ref(port_id):
    return "port:%d" % int(port_id)


def input_ports(vmid, kind="video"):
    """Ports qu'une entrée de `vmid` peut suivre, et le port que suit chacune de ses entrées.

    Pour le sélecteur « Source » d'un consommateur (tuile de multiview, demande du testeur du 30/10 :
    « sélectionner les ports d'entrée du projet et non les entrées de la DeckLink ») : on choisit un
    PORT, l'orchestrateur résout le flux physique (option C, décision du 23/10). Ports = ceux des
    projets dont `vmid` est membre, du média de l'essence `kind`, SAUF une destination que `vmid`
    alimente lui-même (il ne se lit pas : `wire_port` le refuserait). `links` = {slot: {port_id,
    shm}} ; l'appelant ne retient un lien que si `shm` est bien ce que l'entrée reçoit."""
    from .database import db_project_ports, db_port_links, db_get_projects
    media = {v: k for k, v in MEDIA_KIND.items()}.get(kind, kind)
    pids = member_project_ids(vmid)
    names = {p["id"]: p["name"] for p in db_get_projects()} if pids else {}
    allp = db_project_ports(None) if pids else []
    by_id = {p["id"]: p for p in allp}
    ports = []
    for p in sorted(allp, key=lambda x: (x["project_id"], x.get("ord") or 0, x["id"])):
        if p["project_id"] not in pids or p.get("media") != media:
            continue
        shm = port_shm(p, by_id.get)
        if p.get("kind") == "dest" and shm and producer_vmid(shm) == int(vmid):
            continue
        lab = port_labels(p["id"])
        ports.append({"id": p["id"], "ref": port_ref(p["id"]), "name": lab["long"] or p.get("name") or port_ref(p["id"]),
                      "short": lab["short"] or "", "kind": p.get("kind"), "project": names.get(p["project_id"]),
                      "shm": shm or ""})
    uuid = _uuid_for_vmid(vmid)
    links = {}
    for link in (db_port_links(consumer_uuid=uuid) if uuid else []):
        if (link.get("kind") or "video") == kind:
            links[str(norm_slot(link.get("to_slot")))] = {"port_id": link["port_id"], "shm": link.get("shm") or ""}
    return {"ports": ports, "links": links}


def _valid_cols(raw):
    """{"long": N, "short": M} validé (2 à 9, distincts), sinon le défaut."""
    if isinstance(raw, dict):
        lo, sh = raw.get("long"), raw.get("short")
        if (all(isinstance(v, int) and not isinstance(v, bool) and 2 <= v <= 9 for v in (lo, sh))
                and lo != sh):
            return {"long": lo, "short": sh}
    return dict(LABEL_COLS_DEFAULT)


def invalidate_label_index():
    """À appeler après toute écriture sur un port, un lien ou le réglage des colonnes."""
    _idx["ts"] = 0.0


def _label_index():
    """Index relu au plus toutes les 2 s : le distributeur interroge chaque tuile à 10 Hz.

    `links` : (uuid consommateur, flux) → port, d'après `port_links` (flux dernier appliqué).
    `slots` : (uuid consommateur, essence, slot) → port — seul moyen de nommer une entrée VIDE
              (port non affecté) : elle n'a plus de flux par lequel remonter au port.
    `proj`  : (projet, flux) → port ; à flux égal, un port SOURCE passe avant une destination
              (un consommateur du projet lit ses sources), puis l'ordre d'affichage."""
    import time as _t
    now = _t.monotonic()
    if now - _idx["ts"] <= _IDX_TTL_S:
        return _idx
    try:
        from .database import db_get_setting, db_port_links, db_project_ports
        ports = db_project_ports(None) or []
        links = db_port_links() or []
        cols = _valid_cols(db_get_setting("port_label_cols", None))
    except Exception as e:
        log.debug("project_ports: label index not rebuilt (%s)", e)
        return _idx
    proj = {}
    by_id = {p["id"]: p for p in ports}
    for p in sorted(ports, key=lambda p: (p.get("kind") != "source", p.get("ord") or 0, p["id"])):
        shm = port_shm(p, by_id.get)
        if shm:
            proj.setdefault((p["project_id"], shm), p["id"])
    _idx.update(ts=now, cols=cols, proj=proj,
                links={(l["consumer_uuid"], l["shm"]): l["port_id"] for l in links if l.get("shm")},
                slots={(l["consumer_uuid"], l.get("kind") or "video", norm_slot(l.get("to_slot"))):
                       l["port_id"] for l in links})
    return _idx


def label_cols():
    """Colonnes du nom long et du nom court des ports : {"long": N, "short": M}."""
    return dict(_label_index()["cols"])


def input_port_id(ct, shm, kind="video", slot=None):
    """Port qui alimente `shm` (ou l'entrée `slot`) chez ce conteneur (ligne `containers`), ou None.

    Le lien de l'étape 2 d'abord — par l'entrée quand l'appelant la connaît (c'est le seul moyen
    pour une entrée VIDE, celle d'un port non affecté), sinon par le flux ; il dit exactement
    quelle entrée suit quel port, même quand deux ports portent le même flux. À défaut, les ports
    des projets du conteneur. Un conteneur hors projet n'a pas de port : il garde le libellé de
    son flux."""
    shm = (shm or "").strip()
    if shm.startswith(_SHM_PREFIX):
        shm = shm[len(_SHM_PREFIX):]
    if not ct or (not shm and slot is None):
        return None
    idx = _label_index()
    uuid = ct.get("instance_uuid")
    if uuid and slot is not None and (uuid, kind or "video", norm_slot(slot)) in idx["slots"]:
        return idx["slots"][(uuid, kind or "video", norm_slot(slot))]
    if uuid and (uuid, shm) in idx["links"]:          # aucun index ne porte de flux vide
        return idx["links"][(uuid, shm)]
    pids = set()
    if ct.get("project_id") is not None:
        pids.add(ct["project_id"])
    if ct.get("vmid") is not None:
        from .auth import vmid_project_ids
        pids |= vmid_project_ids(ct["vmid"])
    for pid in sorted(pids):
        if (pid, shm) in idx["proj"]:
            return idx["proj"][(pid, shm)]
    return None


def label_ref_for(ct, shm, kind="video", slot=None):
    """Référence de libellé que ce conteneur doit lire pour `shm` : `port:<id>` si un port de son
    projet le porte, sinon le flux lui-même. Le choix virtuel/réel du port se fait à la lecture
    (`tally.row_ref`), pas ici."""
    port_id = input_port_id(ct, shm, kind=kind, slot=slot)
    return port_ref(port_id) if port_id else shm


def port_labels(port_id):
    """Libellés SAISIS d'un port (sans repli) : {"long", "short", "long_col", "short_col"}."""
    from .database import db_get_source_label_for_shm
    cols = label_cols()
    ref = port_ref(port_id)
    return {"long": db_get_source_label_for_shm(ref, cols["long"]),
            "short": db_get_source_label_for_shm(ref, cols["short"]),
            "long_col": cols["long"], "short_col": cols["short"]}


def set_port_labels(port_id, long=None, short=None):
    """Écrit le nom long et/ou court d'un port (None = inchangé, "" = effacé)."""
    from .database import db_upsert_source_label
    cols = label_cols()
    fields = {}
    if long is not None:
        fields["label_%d" % cols["long"]] = str(long).strip()
    if short is not None:
        fields["label_%d" % cols["short"]] = str(short).strip()
    if fields:
        db_upsert_source_label(port_ref(port_id), fields)
        from .tally import invalider_libelles
        invalider_libelles()
