"""ZONES ET DROITS d'un plugin « document » (console audio) — contrôle GÉNÉRIQUE et DÉCLARATIF.

Une console a plusieurs opérateurs ; le document porte des ZONES (`zones: [{id, label, members}]`,
membres = ids de tranches / bus) et l'orchestrateur décide, dans le proxy, qui peut toucher quoi
(docs/chantiers/CONSOLE_AUDIO.md § 10). Le plugin ne connaît ni utilisateur ni droit.

Le manifeste déclare comment une requête se rattache à un élément :

    "control": { …, "zones": {
        "param": "console",                              # clé de params portant le document (+ `zones`)
        "set":  {"endpoint": "/set", "batch": "sets", "path_field": "path",
                 "owner": "^(?:strips|buses)\\\\.([^.]+)"},   # groupe 1 = id de l'élément propriétaire
        "mute": {"endpoint": "/mute", "id_field": "id"},
        "structure": {"endpoints": ["/structure/validate", "/structure/apply"],
                      "permission": "console.build"},    # droit GLOBAL, pas de zone
        "scoped": {"endpoints": ["/scenes/store", "/scenes/recall", …], "field": "only"},
        "admin":  {"endpoints": ["/safe"], "permission": "console.build"}
    }}

SCOPED (scènes, 2026-10-17) : la requête ne vise pas UN élément mais « ce que l'opérateur a le droit de toucher ».
Pour un utilisateur restreint, le proxy pose dans `body[field]` la liste des éléments (tranches, bus, VCA) qu'il
peut opérer — intersection avec celle que le client aurait envoyée, jamais plus large ; le plugin n'applique / ne
capture que ça et SIGNALE le reste. Sans restriction, le corps passe inchangé (le client peut se restreindre
lui-même). ADMIN : un droit GLOBAL sans zone, comme la structure, mais PAS une édition de structure — une macro
peut l'appeler (la protection au rappel se pilote par macro).

Règles (décidées par l'exploitant, 2026-10-13) :
- OUVERT par défaut : sans aucune attribution sur cette console, un utilisateur (avec
  `plugins.operate`, vérifié par le proxy) opère tout. Dès qu'il a ≥ 1 attribution (en son nom ou
  via un groupe), il est limité à ses zones. Un élément hors de toute zone reste ouvert à tous.
- Un élément appartient aux zones qui le listent OU dont un ANCÊTRE dans le document est membre
  (un module de chaîne hérite de sa tranche).
- Lot mixte : la partie autorisée passe, les refus sont listés. Un refus unique → 403 nommant l'élément.
- Structure : permission globale seule ; les zones ne la concernent pas.
- Un chemin impossible à rattacher à un élément est REFUSÉ à un utilisateur restreint (jamais
  laissé passer faute de savoir à qui il appartient).
- Scènes (capture, rappel, conduite) : filtrées par zone via `scoped` (cf. plus haut), 2026-10-17.

Attributions : table `console_zone_grants`, créée ici au premier appel (app/database.py n'en sait
rien), clé = EMPLACEMENT servi (`role:<num>`, survit au remplacement du conteneur) sinon
`inst:<instance_uuid>`. Sujets : utilisateur ou groupe (app/user_groups.py).

Tout est injectable (`conn`, `doc`, `scope`, `group_ids`) : les tests n'ouvrent jamais la base de prod.
"""
import json
import re
import threading

_ready = set()
_ready_lock = threading.Lock()


def spec(manifest):
    """Déclaration `control.zones` normalisée, ou None (absente ou invalide)."""
    z = ((manifest or {}).get("control") or {}).get("zones")
    if not isinstance(z, dict) or validate_spec(manifest):
        return None
    return z


def validate_spec(manifest):
    """Erreurs de `control.zones` (liste vide = valide ou absente)."""
    ctrl = (manifest or {}).get("control") or {}
    z = ctrl.get("zones")
    if z is None:
        return []
    if not isinstance(z, dict):
        return ["control.zones doit être un objet"]
    eps = ctrl.get("endpoints") or []
    eps = set(eps) if isinstance(eps, list) else {("/" + k) for k in eps}
    errs = []
    if not isinstance(z.get("param"), str) or not z.get("param"):
        errs.append("control.zones.param requis")
    s = z.get("set")
    if s is not None:
        if not isinstance(s, dict) or s.get("endpoint") not in eps or not s.get("path_field"):
            errs.append("control.zones.set : endpoint (dans control.endpoints) et path_field requis")
        else:
            try:
                if re.compile(s.get("owner") or "").groups < 1:
                    errs.append("control.zones.set.owner : un groupe de capture (id) requis")
            except re.error as e:
                errs.append("control.zones.set.owner : regex invalide (%s)" % e)
    m = z.get("mute")
    if m is not None and (not isinstance(m, dict) or m.get("endpoint") not in eps or not m.get("id_field")):
        errs.append("control.zones.mute : endpoint (dans control.endpoints) et id_field requis")
    st = z.get("structure")
    if st is not None:
        if (not isinstance(st, dict) or not st.get("permission")
                or not all(e in eps for e in (st.get("endpoints") or [])) or not st.get("endpoints")):
            errs.append("control.zones.structure : endpoints (dans control.endpoints) et permission requis")
    ad = z.get("admin")
    if ad is not None:
        if (not isinstance(ad, dict) or not ad.get("permission")
                or not all(e in eps for e in (ad.get("endpoints") or [])) or not ad.get("endpoints")):
            errs.append("control.zones.admin : endpoints (dans control.endpoints) et permission requis")
    sc = z.get("scoped")
    if sc is not None:
        if (not isinstance(sc, dict) or not isinstance(sc.get("field"), str) or not sc.get("field")
                or not all(e in eps for e in (sc.get("endpoints") or [])) or not sc.get("endpoints")):
            errs.append("control.zones.scoped : endpoints (dans control.endpoints) et field requis")
    return errs


# ── grants ───────────────────────────────────────────────────────────────────────────
def _db(conn=None):
    if conn is None:
        from . import database
        conn = database.get_db()
    key = id(conn)
    if key not in _ready:
        with _ready_lock:
            conn.execute("""CREATE TABLE IF NOT EXISTS console_zone_grants (
                scope        TEXT NOT NULL,
                zone_id      TEXT NOT NULL,
                subject_kind TEXT NOT NULL CHECK (subject_kind IN ('user','group')),
                subject_id   INTEGER NOT NULL,
                PRIMARY KEY (scope, zone_id, subject_kind, subject_id))""")
            conn.commit()
            _ready.add(key)
    return conn


def scope_of(vmid):
    """Clé d'attribution d'une console : l'emplacement qu'elle sert, sinon son instance."""
    from . import database
    c = database.db_get_container(vmid) or {}
    iu = c.get("instance_uuid")
    role = database.db_role_for_instance(iu) if iu else None
    if role:
        return "role:%s" % role["num"]
    return ("inst:%s" % iu) if iu else "vmid:%s" % vmid


def grant(scope, zone_id, kind, subject_id, conn=None):
    c = _db(conn)
    c.execute("INSERT OR IGNORE INTO console_zone_grants VALUES (?,?,?,?)",
              (scope, str(zone_id), kind, int(subject_id)))
    c.commit()


def revoke(scope, zone_id, kind, subject_id, conn=None):
    c = _db(conn)
    n = c.execute("DELETE FROM console_zone_grants WHERE scope=? AND zone_id=? AND subject_kind=? AND subject_id=?",
                  (scope, str(zone_id), kind, int(subject_id))).rowcount
    c.commit()
    return n > 0


def grants(scope, conn=None):
    return [dict(r) for r in _db(conn).execute(
        "SELECT zone_id, subject_kind, subject_id FROM console_zone_grants WHERE scope=? "
        "ORDER BY zone_id, subject_kind, subject_id", (scope,))]


def granted_zones(scope, user_id, group_ids=None, conn=None):
    """Zones attribuées à l'utilisateur (directement ou par groupe) sur cette console.
    Ensemble VIDE = aucune attribution = ouvert (règle par défaut)."""
    if group_ids is None:
        try:
            from . import user_groups
            group_ids = user_groups.groups_of_user(user_id)
        except Exception:
            group_ids = set()
    out = set()
    for r in grants(scope, conn):
        if r["subject_kind"] == "user" and r["subject_id"] == int(user_id):
            out.add(r["zone_id"])
        elif r["subject_kind"] == "group" and r["subject_id"] in set(int(g) for g in group_ids):
            out.add(r["zone_id"])
    return out


# ── grants carried by a saved console (2026-10-18) ─────────────────────────────────────
# A show or a « Mes modèles » template keeps its ZONES (they are document); since 2026-10-18 it also keeps WHO operated
# them, so recalling it gives the rights back. Local ids are not an identity across installations (nor after an account
# is deleted and re-created): a saved grant names its subject by its STABLE identity — the login (`users.username`,
# unique) or the group name (`user_groups.name`, unique, case-insensitive) — plus the label shown when it was saved.
# On recall, a subject that exists here gets its zone back; one that does not is for the person recalling to decide
# (give the zone to an existing account, or leave it to nobody). Restoring is a grant like any other: `settings.edit`.
def _subjects(conn=None):
    """({id: user}, {id: group name}) of this installation."""
    c = conn
    if c is None:
        from . import database
        c = database.get_db()
    users = {r["id"]: dict(r) for r in c.execute("SELECT id, username, prenom, nom FROM users").fetchall()}
    try:
        groups = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM user_groups").fetchall()}
    except Exception:
        groups = {}
    return users, groups


def _label(u):
    full = " ".join(x for x in ((u.get("prenom") or "").strip(), (u.get("nom") or "").strip()) if x)
    return "%s (%s)" % (full, u.get("username")) if full else (u.get("username") or "#%s" % u.get("id"))


def capture_grants(scope, doc, conn=None):
    """Grants of console `scope` on the zones of `doc`, by stable identity:
    [{zone_id, subject_kind, name, label}] (sorted). A grant whose subject no longer exists is left out."""
    have = {z.get("id") for z in (doc or {}).get("zones") or [] if isinstance(z, dict)}
    users, groups = _subjects(conn)
    out = []
    for g in grants(scope, conn):
        if g["zone_id"] not in have:
            continue
        if g["subject_kind"] == "user" and g["subject_id"] in users:
            u = users[g["subject_id"]]
            out.append({"zone_id": g["zone_id"], "subject_kind": "user", "name": u["username"], "label": _label(u)})
        elif g["subject_kind"] == "group" and g["subject_id"] in groups:
            n = groups[g["subject_id"]]
            out.append({"zone_id": g["zone_id"], "subject_kind": "group", "name": n, "label": n})
    return sorted(out, key=lambda x: (x["zone_id"], x["subject_kind"], x["name"]))


def clean_saved_grants(v):
    """A saved grants list made safe (from a file: never trusted): None when absent / unreadable (= nothing recorded),
    else at most 500 well-formed entries."""
    if not isinstance(v, list):
        return None
    out = []
    for g in v[:500]:
        if not isinstance(g, dict) or g.get("subject_kind") not in ("user", "group"):
            continue
        z, n = g.get("zone_id"), g.get("name")
        if not (isinstance(z, str) and z and len(z) <= 64 and isinstance(n, str) and n.strip() and len(n) <= 200):
            continue
        lab = g.get("label") if isinstance(g.get("label"), str) else n
        out.append({"zone_id": z, "subject_kind": g["subject_kind"], "name": n.strip(), "label": lab[:200]})
    return out


def plan_grants(saved, zones, conn=None):
    """What recalling `saved` (clean_saved_grants form) means HERE, for the zones `zones` ([{id, label}] of the document
    about to be applied): [{zone_id, zone_label, subject_kind, name, label, match: {kind, id, label} | None}].
    Grants on a zone the document does not have are left out (the zones were not taken)."""
    zl = {z.get("id"): (z.get("label") or z.get("id")) for z in zones or [] if isinstance(z, dict) and z.get("id")}
    users, groups = _subjects(conn)
    by_login = {u["username"]: u for u in users.values()}
    by_group = {n.lower(): (gid, n) for gid, n in groups.items()}
    out = []
    for g in saved or []:
        if g["zone_id"] not in zl:
            continue
        m = None
        if g["subject_kind"] == "user" and g["name"] in by_login:
            u = by_login[g["name"]]
            m = {"kind": "user", "id": u["id"], "label": _label(u)}
        elif g["subject_kind"] == "group" and g["name"].lower() in by_group:
            gid, n = by_group[g["name"].lower()]
            m = {"kind": "group", "id": gid, "label": n}
        out.append(dict(g, zone_label=zl[g["zone_id"]], match=m))
    return out


def replace_grants(scope, zone_ids, new, conn=None):
    """The grants of console `scope` on the zones `zone_ids` become exactly `new` ([{zone_id, subject_kind, subject_id}],
    each zone among `zone_ids`, each subject existing here). ValueError(code) otherwise, nothing changed. Returns the
    number of grants written."""
    c = _db(conn)
    zs = {str(z) for z in zone_ids or []}
    users, groups = _subjects(c)
    rows = set()
    for g in new or []:
        if not isinstance(g, dict) or str(g.get("zone_id")) not in zs:
            raise ValueError("zone_unknown")
        k = g.get("subject_kind")
        try:
            sid = int(g.get("subject_id"))
        except (TypeError, ValueError):
            raise ValueError("subject_unknown")
        if (k == "user" and sid not in users) or (k == "group" and sid not in groups) or k not in ("user", "group"):
            raise ValueError("subject_unknown")
        rows.add((str(g["zone_id"]), k, sid))
    for z in zs:
        c.execute("DELETE FROM console_zone_grants WHERE scope=? AND zone_id=?", (scope, z))
    for z, k, sid in sorted(rows):
        c.execute("INSERT OR IGNORE INTO console_zone_grants VALUES (?,?,?,?)", (scope, z, k, sid))
    c.commit()
    return len(rows)


# ── document ─────────────────────────────────────────────────────────────────────────
def load_doc(vmid, param):
    """Document PERSISTÉ (deploy_config.params[param]) — jamais un instantané de la requête."""
    from . import database
    c = database.db_get_container(vmid) or {}
    dc = c.get("deploy_config")
    try:
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    except ValueError:
        dc = {}
    d = (dc.get("params") or {}).get(param)
    return d if isinstance(d, dict) else {}


def element_zones(doc, eid):
    """Zones d'un élément : celles dont lui OU un de ses ancêtres (dans le document) est membre."""
    members = {}
    for z in doc.get("zones") or []:
        if isinstance(z, dict) and z.get("id"):
            for mem in z.get("members") or []:
                members.setdefault(mem, set()).add(z["id"])

    def walk(o, chain):
        if isinstance(o, dict):
            oid = o.get("id") if isinstance(o.get("id"), str) else None
            ch = chain + [oid] if oid else chain
            if oid == eid:
                return ch
            for k, v in o.items():
                if k == "zones":
                    continue
                r = walk(v, ch)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v, chain)
                if r:
                    return r
        return None
    found = walk(doc, []) or [eid]
    zs = set()
    for i in found:
        zs |= members.get(i, set())
    return zs


# ── filtre ───────────────────────────────────────────────────────────────────────────
def _forbidden(refused, why="zone"):
    return (403, {"error": "forbidden", "reason": why, "refused": refused})


def filter_request(vmid, manifest, user, endpoint, body, *, perms=None, scope=None, doc=None,
                   group_ids=None, conn=None):
    """Applique zones et droits à UN POST de plugin. Rend (corps_autorisé | None, refusés, erreur).

    - `erreur` = (statut, corps JSON) à renvoyer tel quel, sinon None ;
    - `refusés` = [{"id"|"path", …}] retirés d'un lot mixte (à joindre à la réponse) ;
    - plugin sans `control.zones`, ou endpoint non concerné : le corps passe inchangé."""
    z = spec(manifest)
    if not z:
        return body, [], None
    ep = "/" + endpoint.lstrip("/") if endpoint else ""
    for key in ("structure", "admin"):
        st = z.get(key) or {}
        if ep in (st.get("endpoints") or []):
            if st["permission"] not in (perms or set()):
                return None, [], (403, {"error": "forbidden", "missing_permission": st["permission"]})
            return body, [], None
    s, m, sc = z.get("set") or {}, z.get("mute") or {}, z.get("scoped") or {}
    is_scoped = ep in (sc.get("endpoints") or [])
    if ep not in (s.get("endpoint"), m.get("endpoint")) and not is_scoped:
        return body, [], None
    uid = (user or {}).get("id")
    scope = scope or scope_of(vmid)
    mine = granted_zones(scope, uid, group_ids, conn) if uid is not None else set()
    if not mine:
        return body, [], None                      # aucune attribution : ouvert
    doc = doc if doc is not None else load_doc(vmid, z["param"])

    def allowed(eid):
        if not eid:
            return False                           # non rattachable : refusé à un restreint
        zs = element_zones(doc, eid)
        return not zs or bool(zs & mine)

    if is_scoped:
        # « ce que je peux toucher » : tranches, bus, VCA de MES zones + ceux hors de toute zone ; une liste
        # envoyée par le client est INTERSECTÉE (il peut se restreindre, jamais s'élargir)
        ids = [o.get("id") for k in ("strips", "buses", "vcas") for o in doc.get(k) or []
               if isinstance(o, dict) and isinstance(o.get("id"), str)]
        mine_ids = [i for i in ids if allowed(i)]
        body = dict(body) if isinstance(body, dict) else {}
        asked = body.get(sc["field"])
        if isinstance(asked, list):
            mine_ids = [i for i in mine_ids if i in set(asked)]
        body[sc["field"]] = mine_ids
        return body, [], None

    if ep == m.get("endpoint"):
        eid = (body or {}).get(m["id_field"]) if isinstance(body, dict) else None
        if allowed(eid):
            return body, [], None
        return None, [{"id": eid}], _forbidden([{"id": eid}])
    rx = re.compile(s["owner"])
    batch = s.get("batch")
    body = body if isinstance(body, dict) else {}
    items = body.get(batch) if batch and isinstance(body.get(batch), list) else [body]
    keep, refused = [], []
    for it in items:
        p = str((it or {}).get(s["path_field"]) or "") if isinstance(it, dict) else ""
        mt = rx.match(p)
        if mt and allowed(mt.group(1)):
            keep.append(it)
        else:
            refused.append({"path": p, "id": mt.group(1) if mt else None})
    if not keep:
        return None, refused, _forbidden(refused)
    if not refused:
        return body, [], None
    return dict(body, **{batch: keep}), refused, None


def macro_guard(manifest, endpoint, perms=None):
    """Garde des MACROS. Choix : une macro s'exécute sans utilisateur humain (déclencheur, pupitre) —
    elle contourne les zones de /set et /mute (« droits des macros » : reporté, § 10), mais ne peut
    JAMAIS appeler un endpoint de structure (permission globale d'un humain, absente ici).
    Same rule for SHOWS (app/plugin_shows.py): loading one is a structure application with a report a human must
    read (latency jumps, absent flows, zones) — it is not an endpoint, and no macro action reaches it.
    Lève RuntimeError si l'endpoint est de structure."""
    z = spec(manifest)
    if z and ("/" + str(endpoint).lstrip("/")) in ((z.get("structure") or {}).get("endpoints") or []):
        raise RuntimeError("%s : édition de structure interdite à une macro (permission %s)"
                              % (endpoint, z["structure"]["permission"]))


def annotate(result, refused):
    """Joint les refus d'un lot mixte à la réponse du conteneur `(texte, statut, en-têtes)` :
    l'opérateur doit VOIR ce qui n'a pas été appliqué. Réponse illisible → rendue telle quelle."""
    if not refused or not isinstance(result, tuple) or not result or not isinstance(result[0], str):
        return result
    try:
        j = json.loads(result[0])
    except ValueError:
        return result
    if not isinstance(j, dict):
        return result
    j["refused"] = refused
    return (json.dumps(j),) + tuple(result[1:])
