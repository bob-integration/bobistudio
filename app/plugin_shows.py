# SPDX-License-Identifier: GPL-3.0-or-later
"""SHOWS of a « document » plugin (audio console, lot 3 of the memories — docs/chantiers/CONSOLE_MEMOIRES.md).

A show is the WHOLE document of a console (strips, buses, patch, routing, inserted plugins, zones, AND its cue list:
scenes, protections, scene defaults), saved under a name in a library of the INSTALLATION (one per plugin type,
readable from every console of that type) and loaded back — off air — by the very path of the structure editor:
dry-run validation by the container → persistence of the canonical document → application
(`plugin_persist.structure_apply`). Loading recompiles the console and may move output latencies.

What a show is NOT (and why it does not duplicate them):
- a PROJECT snapshot (app/projects.py) captures a whole PLATEAU (containers, cabling, placement) and restores it by
  re-creating containers: bound to this installation, coarse, never applied live to one console;
- a CONFIG snapshot (`/api/containers/<vmid>/config_snapshots`) keeps the raw `params` of ONE container and restores
  them by REDEPLOYING it: bound to that vmid, no validation report, no flow check;
- a plugin PRESET (`plugin_store`, exportable since 4da0595) is an opaque set of settings, never structure.
A show is the console alone, reusable from one programme to the next and on ANOTHER console (or installation, via
its JSON file): its flows are checked against this plateau, its outputs take the name of the console that loads it.

Generic and DECLARATIVE — the orchestrator does not know the console:

    "control": { …, "shows": {
        "permission": "console.build",                 # save / load / import / delete (list, export: login)
        "apply_flags": {"replace_scenes": true},       # extra body fields of validate + apply for a show
        "summary": {"strips": "strips", "buses": "buses", "outputs": "outputs", "scenes": "scenes"}
    }}

It requires `control.persist.structure`. Momentary states (PFL selections, talkback order, AFV « open »…) are left out
by the plugin itself: the capture reads the declared SNAPSHOT (`persist.snapshot`, the persistable form), never the
live view. Flows referenced by the show (manifest `wiring.consumes` in key mode) are compared with what this plateau
produces: absent ones are REPORTED (and optionally unwired), never fatal; a flow the source console produced itself
(loopback) is re-bound to the loading console's own flow — outputs are named after the hostname of the console that
runs the document (`wiring.produces` templates), so loading another console's show never usurps its flow names.

MACROS: loading a show is NOT exposed to macros, deliberately (same reasoning as `plugin_zones.macro_guard` for
structure): it recompiles the console (off air only), may move latencies that a HUMAN must accept after reading the
report, and replaces the zones that bound every operator. A trigger or a control surface has no one to read that
report. Only human routes call `load`.

KINDS (2026-10-18): a row is a `show` (the whole console as it was, settings and wiring included) or a `template`
(« Mes modèles » of the console creation wizard: the STRUCTURE only — neutral settings, no wired flow — prepared by the
plugin itself, `POST /templates/build {template: true}`, and stored here through the import route). Same table, same
exchange file (`kind` field), same rights; one name per plugin type across both kinds. Older rows are shows.

Errors are CODES (the interface translates them), never sentences.
"""
import datetime
import json
import logging
import re
import threading

import requests

log = logging.getLogger(__name__)

EXCHANGE_FORMAT = "bobistudio.plugin-show"
EXCHANGE_VERSION = 1
NAME_MAX = 80
KINDS = ("show", "template")
HTTP_TIMEOUT_S = 5.0

_ready = set()
_ready_lock = threading.Lock()


class ShowError(Exception):
    """A refusal with a stable code, an HTTP status and values for the interface."""

    def __init__(self, code, status=400, **extra):
        super().__init__(code)
        self.code, self.status, self.extra = code, status, extra

    def body(self):
        return dict(self.extra, error=self.code)


# ── declaration ──────────────────────────────────────────────────────────────────────
def validate_spec(manifest):
    """Errors of `control.shows` (empty list = valid or absent)."""
    ctrl = (manifest or {}).get("control") or {}
    s = ctrl.get("shows")
    if s is None:
        return []
    if not isinstance(s, dict):
        return ["control.shows must be an object"]
    errs = []
    st = (ctrl.get("persist") or {}).get("structure") if isinstance(ctrl.get("persist"), dict) else None
    if not isinstance(st, dict) or not st.get("apply") or not st.get("validate"):
        errs.append("control.shows requires control.persist.structure")
    if not isinstance(s.get("permission"), str) or not s.get("permission"):
        errs.append("control.shows.permission required")
    fl = s.get("apply_flags", {})
    if not isinstance(fl, dict) or any(k in ("doc", "base_version", "accept_latency_jump") for k in fl) \
            or not all(isinstance(v, (bool, int, float, str)) for v in fl.values()):
        errs.append("control.shows.apply_flags: object of scalars, without doc/base_version/accept_latency_jump")
    sm = s.get("summary", {})
    if not isinstance(sm, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in sm.items()):
        errs.append("control.shows.summary: {name: dotted path of a list}")
    return errs


def spec(manifest):
    """Normalised `control.shows` + persistence declaration, or None (absent or invalid)."""
    s = ((manifest or {}).get("control") or {}).get("shows")
    if not isinstance(s, dict) or validate_spec(manifest):
        return None
    from . import plugin_persist
    pp = plugin_persist.spec(manifest)
    if not pp or not pp.get("apply"):
        return None
    return {"permission": s["permission"], "apply_flags": dict(s.get("apply_flags") or {}),
            "summary": dict(s.get("summary") or {}), "persist": pp}


# ── storage (one library per installation and plugin type) ──────────────────────────
def _db(conn=None):
    if conn is None:
        from . import database
        conn = database.get_db()
    key = id(conn)
    if key not in _ready:
        with _ready_lock:
            conn.execute("""CREATE TABLE IF NOT EXISTS plugin_shows (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_type     TEXT NOT NULL,
                name            TEXT NOT NULL,
                author          TEXT,
                author_id       INTEGER,
                created_at      TEXT,
                updated_at      TEXT,
                plugin_version  TEXT,
                source_hostname TEXT,
                summary         TEXT,
                document        TEXT NOT NULL,
                last_loaded_at  TEXT,
                last_loaded_on  TEXT,
                UNIQUE (plugin_type, name))""")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(plugin_shows)").fetchall()}
            if "kind" not in cols:                 # 2026-10-18: templates (« Mes modèles ») share the table
                conn.execute("ALTER TABLE plugin_shows ADD COLUMN kind TEXT NOT NULL DEFAULT 'show'")
            if "zone_grants" not in cols:          # 2026-10-18: who operated the zones (NULL = not recorded)
                conn.execute("ALTER TABLE plugin_shows ADD COLUMN zone_grants TEXT")
            conn.commit()
            _ready.add(key)
    return conn


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _row(r, with_document=False):
    if r is None:
        return None
    d = dict(r)
    try:
        d["summary"] = json.loads(d.get("summary") or "{}")
    except ValueError:
        d["summary"] = {}
    if with_document:
        d["document"] = json.loads(d["document"])
    else:
        d.pop("document", None)
    raw = d.pop("zone_grants", None)
    try:
        g = json.loads(raw) if raw else None
    except ValueError:
        g = None
    d["has_grants"] = isinstance(g, list)
    if with_document:
        d["zone_grants"] = g if isinstance(g, list) else None
    return d


def check_kind(kind):
    k = str(kind or "show").strip().lower()
    if k not in KINDS:
        raise ShowError("bad_kind", kinds=list(KINDS))
    return k


def list_shows(type_, conn=None, kind="show"):
    """Library of a plugin type (one KIND), newest first, WITHOUT the documents (they can weigh hundreds of kB)."""
    rows = _db(conn).execute("SELECT * FROM plugin_shows WHERE plugin_type=? AND kind=? ORDER BY updated_at DESC, id DESC",
                             (type_, check_kind(kind))).fetchall()
    return [_row(r) for r in rows]


def get_show(type_, sid, conn=None):
    r = _db(conn).execute("SELECT * FROM plugin_shows WHERE plugin_type=? AND id=?", (type_, int(sid))).fetchone()
    return _row(r, with_document=True)


def delete_show(type_, sid, conn=None):
    c = _db(conn)
    n = c.execute("DELETE FROM plugin_shows WHERE plugin_type=? AND id=?", (type_, int(sid))).rowcount
    c.commit()
    if n > 0 and conn is None:
        try:                                         # its classification goes with it (one of the two kinds)
            from services.audio import console_library
        except ImportError:                          # audio service absent: no Libraries page, nothing to forget
            console_library = None
        if console_library is not None:
            console_library.forget("show", int(sid))
            console_library.forget("template", int(sid))
    return n > 0


def _clean_name(name):
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        raise ShowError("name_required")
    if len(n) > NAME_MAX:
        raise ShowError("name_too_long", max=NAME_MAX)
    return n


def _store(type_, rec, overwrite, conn=None):
    """Insert (or replace the show of the same name when `overwrite`). Returns the id."""
    c = _db(conn)
    kind = check_kind(rec.get("kind"))
    old = c.execute("SELECT id, created_at, kind FROM plugin_shows WHERE plugin_type=? AND name=?",
                    (type_, rec["name"])).fetchone()
    zg = rec.get("zone_grants")
    vals = (rec.get("author"), rec.get("author_id"), rec.get("plugin_version"), rec.get("source_hostname"),
            json.dumps(rec.get("summary") or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(rec["document"], ensure_ascii=False, sort_keys=True),
            json.dumps(zg, ensure_ascii=False, sort_keys=True) if isinstance(zg, list) else None)
    if old is not None:
        if not overwrite or (old["kind"] or "show") != kind:     # a template never replaces a show (nor the reverse)
            raise ShowError("name_taken", 409, name=rec["name"], id=old["id"])
        c.execute("UPDATE plugin_shows SET author=?, author_id=?, plugin_version=?, source_hostname=?, summary=?, "
                  "document=?, zone_grants=?, created_at=?, updated_at=? WHERE id=?",
                  vals + (rec.get("created_at") or old["created_at"], _now(), old["id"]))
        c.commit()
        return old["id"]
    cur = c.execute("INSERT INTO plugin_shows (plugin_type, name, author, author_id, plugin_version, source_hostname, "
                    "summary, document, zone_grants, created_at, updated_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (type_, rec["name"]) + vals + (rec.get("created_at") or _now(), _now(), kind))
    c.commit()
    return cur.lastrowid


# ── helpers ──────────────────────────────────────────────────────────────────────────
def _container(vmid):
    """(container, deploy_config, manifest, spec) — ShowError when it is not a plugin with shows."""
    from . import database, plugins, plugin_persist
    c = database.db_get_container(vmid)
    if not c:
        raise ShowError("not_found", 404, vmid=vmid)
    dc = plugin_persist._dc(vmid) or {}
    m = plugins.get(dc.get("type")) or {}
    sp = spec(m)
    if not sp:
        raise ShowError("no_shows", 400, type=dc.get("type"))
    return c, dc, m, sp


def type_spec(type_):
    from . import plugins
    if not plugins.is_plugin(type_):
        raise ShowError("unknown_type", 404, type=type_)
    sp = spec(plugins.get(type_))
    if not sp:
        raise ShowError("no_shows", 400, type=type_)
    return sp


def _hostname(c, dc):
    return ((dc.get("params") or {}).get("hostname") or c.get("hostname") or "mxl%s" % c.get("vmid"))


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (TypeError, ValueError):
        return None


def newer_than(show_version, plugin_version):
    """True when the show was written by a NEWER plugin than `plugin_version` (both readable)."""
    a, b = _version_tuple(show_version), _version_tuple(plugin_version)
    return a is not None and b is not None and a > b


def _snapshot(vmid, sp):
    """Fresh persistable document from the container (`persist.snapshot` — momentary states left out by the plugin),
    or None when the container does not answer."""
    from . import plugin_persist
    url = plugin_persist._url(vmid, sp["persist"]["snapshot"])
    if not url:
        return None
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT_S)
        if r.status_code == 200:
            j = r.json()
            return j if isinstance(j, dict) else None
    except Exception:
        return None
    return None


def _ports(manifest, param, doc, hostname, side):
    """[(key, entry, shm, spec)] of the wiring ports of `side` ("consumes" | "produces") in key mode, rooted at the
    document parameter."""
    from . import wiring_document as wd
    out = []
    for s in ((manifest.get("wiring") or {}).get(side) or []):
        if not isinstance(s, dict) or not s.get("key") or (s.get("from_list") or "").split(".")[0] != param:
            continue
        for key, entry in wd.expand(s, {param: doc}, hostname):
            out.append((key, entry, wd.port_shm(s, key, entry, hostname), s))
    return out


def summarize(manifest, sp, doc, hostname=""):
    """{<summary name>: count, …, inputs: [referenced flows]} of a document."""
    from . import wiring_document as wd
    out = {}
    for name, path in sp["summary"].items():
        v = wd.get_path(doc, path)
        out[name] = len(v) if isinstance(v, (list, dict)) else 0
    param = sp["persist"]["param"]
    out["inputs"] = sorted({shm for _k, _e, shm, _s in _ports(manifest, param, doc, hostname, "consumes") if shm})
    return out


def _plateau_produced(exclude_vmid):
    """{shm: {vmid, hostname}} of every flow produced on this plateau by the OTHER containers (same derivation as the
    Cables page: `produced_shms` hook, else `wiring.produces`)."""
    from . import database, plugins
    out = {}
    for c in database.db_get_containers():
        if c.get("vmid") == exclude_vmid:
            continue
        dc = c.get("deploy_config")
        try:
            dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
        except ValueError:
            continue
        t, p = dc.get("type"), dc.get("params") or {}
        if not t:
            continue
        hn = p.get("hostname") or c.get("hostname") or "mxl%s" % c["vmid"]
        who = {"vmid": c["vmid"], "hostname": hn}
        try:
            hook = plugins.get_hook(t, "produced_shms")
            if hook:
                for s in (hook(hn, p, {}) or []):
                    out.setdefault(s, who)
            elif plugins.is_plugin(t):
                for prod in plugins.derive_wiring(t, hn, p)["produces"]:
                    if prod.get("shm"):
                        out.setdefault(prod["shm"], who)
        except Exception:
            log.exception("shows: produced flows of #%s unreadable", c.get("vmid"))
    return out


def element_names(doc):
    """{id: label} of every element of the document (to name a latency report in the show's own words)."""
    names = {}

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("id"), str) and isinstance(o.get("label"), str) and o["label"]:
                names.setdefault(o["id"], o["label"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc)
    return names


# ── save ─────────────────────────────────────────────────────────────────────────────
def save(vmid, name, user=None, overwrite=False, conn=None):
    """Capture the console `vmid` as a show. Returns the stored record (without the document)."""
    c, dc, m, sp = _container(vmid)
    name = _clean_name(name)
    doc = _snapshot(vmid, sp)
    source = "container"
    if doc is None:                       # container silent: the persisted document is the same snapshot form
        doc = (dc.get("params") or {}).get(sp["persist"]["param"])
        source = "persisted"
    if not isinstance(doc, dict) or not doc:
        raise ShowError("snapshot_unreadable", 502, vmid=vmid)
    doc = json.loads(json.dumps(doc))
    doc.pop("version", None)              # the console's revision counter, meaningless elsewhere
    host = _hostname(c, dc)
    u = user or {}
    who = " ".join(x for x in (u.get("prenom"), u.get("nom")) if x) or u.get("username") or ""
    rec = {"name": name, "author": who, "author_id": u.get("id"),
           "plugin_version": (dc.get("params") or {}).get("plugin_version") or m.get("version"),
           "source_hostname": host, "summary": summarize(m, sp, doc, host), "document": doc,
           "zone_grants": console_grants(vmid, m, doc, conn)}
    sid = _store(m["type"], rec, overwrite, conn)
    out = get_show(m["type"], sid, conn)
    out.pop("document", None)
    out["captured_from"] = source
    return out


def console_grants(vmid, manifest, doc, conn=None):
    """Zone grants of console `vmid` on the zones of `doc`, by stable identity (plugin_zones.capture_grants), or None
    for a plugin without zones. Never raises: a show is saved even when the grants cannot be read (None = unknown)."""
    try:
        from . import plugin_zones
        if not plugin_zones.spec(manifest):
            return None
        return plugin_zones.capture_grants(plugin_zones.scope_of(vmid), doc, conn)
    except Exception:
        log.exception("shows: zone grants of #%s unreadable", vmid)
        return None


# ── exchange file ────────────────────────────────────────────────────────────────────
def export_document(type_, sid, conn=None, with_grants=False):
    """The exchange file. `with_grants` (the caller may manage zone grants, settings.edit) adds `zone_grants`: logins
    and labels of the operators of each zone — not for every logged-in reader."""
    s = get_show(type_, sid, conn)
    if not s:
        raise ShowError("not_found", 404, id=sid)
    out = {"format": EXCHANGE_FORMAT, "format_version": EXCHANGE_VERSION, "plugin_type": type_,
           "plugin_version": s.get("plugin_version"), "name": s["name"], "author": s.get("author") or "",
           "created_at": s.get("created_at"), "source_hostname": s.get("source_hostname") or "",
           "exported_at": _now(), "summary": s.get("summary") or {}, "kind": s.get("kind") or "show",
           "document": s["document"]}
    if with_grants and isinstance(s.get("zone_grants"), list):
        out["zone_grants"] = s["zone_grants"]
    return out


def import_document(type_, body, user=None, conn=None, grants_from=None):
    """Store the show of an exchange file. Body: the bare file, or {document: file, mode, name, kind}.
    Zone grants: those of the file (`zone_grants`, cleaned), or — `grants_from` = a console's vmid, checked by the
    route — that console's current grants on the document's zones (the wizard's « save as template »).
    mode `new` (default: a taken name is refused, 409 name_taken) | `overwrite`. kind (body, else the file's, else
    `show`): `template` stores a « Mes modèles » entry — its document is the structure the plugin prepared."""
    from . import plugins
    sp = type_spec(type_)
    m = plugins.get(type_)
    if not isinstance(body, dict):
        raise ShowError("bad_document")
    f = body["document"] if isinstance(body.get("document"), dict) and "format" in body["document"] else body
    mode = str(body.get("mode") or "new").strip().lower()
    if mode not in ("new", "overwrite"):
        raise ShowError("bad_mode")
    if f.get("format") != EXCHANGE_FORMAT:
        raise ShowError("bad_format", expected=EXCHANGE_FORMAT, found=f.get("format"))
    try:
        fv = int(f.get("format_version"))
    except (TypeError, ValueError):
        fv = -1
    if not 1 <= fv <= EXCHANGE_VERSION:
        raise ShowError("bad_format_version", found=f.get("format_version"), max=EXCHANGE_VERSION)
    if f.get("plugin_type") != type_:
        raise ShowError("wrong_plugin_type", 409, expected=type_, found=f.get("plugin_type"))
    if newer_than(f.get("plugin_version"), m.get("version")):
        raise ShowError("show_newer_than_plugin", 409, show_version=f.get("plugin_version"),
                        plugin_version=m.get("version"))
    doc = f.get("document")
    if not isinstance(doc, dict) or not doc:
        raise ShowError("bad_document")
    name = _clean_name(body.get("name") if isinstance(body.get("document"), dict) and body.get("name") else f.get("name"))
    host = str(f.get("source_hostname") or "")
    kind = check_kind(body.get("kind") if isinstance(body.get("document"), dict) and body.get("kind") else f.get("kind"))
    u = user or {}
    who = " ".join(x for x in (u.get("prenom"), u.get("nom")) if x) or u.get("username") or ""
    rec = {"name": name, "kind": kind,
           "author": str(f.get("author") or "") or (who if kind == "template" else ""),
           "author_id": u.get("id") if kind == "template" else None,
           "plugin_version": f.get("plugin_version"), "source_hostname": host, "created_at": f.get("created_at"),
           "summary": summarize(m, sp, doc, host),         # recomputed: a file is not trusted for what it says
           "document": doc}
    from . import plugin_zones
    if grants_from is not None:
        rec["zone_grants"] = console_grants(grants_from, m, doc, conn)
    else:
        rec["zone_grants"] = plugin_zones.clean_saved_grants(f.get("zone_grants"))
    sid = _store(type_, rec, mode == "overwrite", conn)
    out = get_show(type_, sid, conn)
    out.pop("document", None)
    return out


# ── load ─────────────────────────────────────────────────────────────────────────────
def prepare(vmid, sid, unwire_missing=False, rebind_own=True):
    """Document of show `sid` made ready for console `vmid`, and what it means there. ShowError if it cannot be.
    Returns (container, dc, manifest, spec, show, doc, report)."""
    c, dc, m, sp = _container(vmid)
    show = get_show(m["type"], sid)
    if not show:
        raise ShowError("not_found", 404, id=sid)
    target_ver = (dc.get("params") or {}).get("plugin_version") or m.get("version")
    if newer_than(show.get("plugin_version"), target_ver):
        raise ShowError("show_newer_than_plugin", 409, show_version=show.get("plugin_version"),
                        plugin_version=target_ver)
    from . import wiring_document as wd
    param = sp["persist"]["param"]
    host = _hostname(c, dc)
    src_host = show.get("source_hostname") or ""
    doc = json.loads(json.dumps(show["document"]))
    produced = _plateau_produced(vmid)
    own_target = {shm for _k, _e, shm, _s in _ports(m, param, doc, host, "produces") if shm}
    own_source = {}
    if src_host and src_host != host:
        tgt_by_key = {(id(s), k): shm for k, _e, shm, s in _ports(m, param, doc, host, "produces")}
        for k, _e, shm, s in _ports(m, param, doc, src_host, "produces"):
            if shm and (id(s), k) in tgt_by_key:
                own_source[shm] = tgt_by_key[(id(s), k)]
    flows = []
    for key, entry, shm, s in _ports(m, param, doc, host, "consumes"):
        f = {"key": key, "label": wd.fill(s.get("label") or "{id}", entry) or key, "flow": shm}
        if not shm:
            f["status"] = "none"
        elif shm in own_source and rebind_own:
            new = own_source[shm]
            wrapped, _err = wd.with_field({param: doc}, s, key, new, host)
            if wrapped is not None:
                doc = wrapped[param]
            f.update(status="rebound", flow=new, was=shm)
        elif shm in own_target:
            f["status"] = "present"
            f["producer"] = {"vmid": vmid, "hostname": host}
        elif shm in produced:
            f["status"] = "present"
            f["producer"] = produced[shm]
        elif unwire_missing:
            wrapped, _err = wd.with_field({param: doc}, s, key, "", host)
            if wrapped is not None:
                doc = wrapped[param]
            f.update(status="unwired", was=shm, flow="")
        else:
            f["status"] = "absent"
        flows.append(f)
    report = {"show": {k: show.get(k) for k in ("id", "name", "author", "created_at", "updated_at", "plugin_version",
                                                "source_hostname", "summary")},
              "target": {"vmid": vmid, "hostname": host, "plugin_version": target_ver},
              "off_air": True, "flows": flows,
              "missing": sum(1 for f in flows if f["status"] in ("absent", "unwired")),
              "zones": _zones_report(vmid, m, doc), "names": element_names(doc)}
    return c, dc, m, sp, show, doc, report


def _zones_report(vmid, manifest, doc):
    """Zone grants of THIS console whose zone the show does not have: those operators would be limited to elements
    outside every zone. Reported, never fatal (zones belong to the document, grants to the console)."""
    try:
        from . import plugin_zones
        if not plugin_zones.spec(manifest):
            return {"orphan_grants": []}
        have = {z.get("id") for z in doc.get("zones") or [] if isinstance(z, dict)}
        gr = plugin_zones.grants(plugin_zones.scope_of(vmid))
        return {"orphan_grants": sorted({g["zone_id"] for g in gr if g["zone_id"] not in have})}
    except Exception:
        log.exception("shows: zone report of #%s", vmid)
        return {"orphan_grants": []}


def _base_version(vmid, sp, dc):
    snap = _snapshot(vmid, sp)
    if snap is None:
        raise ShowError("container_unreachable", 502, vmid=vmid)
    return snap.get("version")


def check(vmid, sid, unwire_missing=False, rebind_own=True):
    """Dry run: the report BEFORE anything changes — validation by the container (errors, latency jumps), flows,
    zones. Nothing is persisted, nothing applied."""
    from . import plugin_persist
    c, dc, m, sp, show, doc, report = prepare(vmid, sid, unwire_missing, rebind_own)
    base = _base_version(vmid, sp, dc)
    url = plugin_persist._url(vmid, sp["persist"]["validate"])
    body = dict(sp["apply_flags"], doc=doc, base_version=base)
    try:
        r = requests.post(url, json=body, timeout=HTTP_TIMEOUT_S)
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") or r.text[:1] == "{" else {}
    except Exception as e:
        raise ShowError("container_unreachable", 502, vmid=vmid, detail=str(e)[:200])
    v = {"ok": r.status_code == 200 and bool(j.get("ok")), "status": r.status_code,
         "errors": j.get("errors") or [], "latency_jump": j.get("latency_jump") or []}
    if not v["ok"] and j.get("error"):
        v["error"] = j.get("error")
    report.update(base_version=base, validation=v, ok=v["ok"])
    return report


def load(vmid, sid, accept_latency_jump=False, base_version=None, unwire_missing=False, rebind_own=True):
    """Load show `sid` on console `vmid` by the STRUCTURE path (validate → persist → apply, previous document put
    back on refusal). `base_version`: the version the operator saw in the report (a console changed since is refused
    as stale rather than overwritten). Returns (status, body)."""
    from . import plugin_persist
    c, dc, m, sp, show, doc, report = prepare(vmid, sid, unwire_missing, rebind_own)
    if base_version is None:
        base_version = _base_version(vmid, sp, dc)
    body = dict(sp["apply_flags"], doc=doc, base_version=base_version,
                accept_latency_jump=bool(accept_latency_jump))
    res = plugin_persist.structure_apply(vmid, m, sp["persist"]["apply"], body)
    if res is None:                                  # cannot happen with a valid spec — never silent
        raise ShowError("no_shows", 400, type=m.get("type"))
    text, status = res[0], res[1]
    try:
        j = json.loads(text)
    except (TypeError, ValueError):
        j = {"error": "bad_response", "detail": str(text)[:200]}
    if not isinstance(j, dict):
        j = {"result": j}
    j["report"] = report
    if status == 200:
        try:
            c_ = _db()
            c_.execute("UPDATE plugin_shows SET last_loaded_at=?, last_loaded_on=? WHERE id=?",
                       (_now(), report["target"]["hostname"], show["id"]))
            c_.commit()
        except Exception:
            log.exception("shows: last load of #%s not recorded", show["id"])
        try:                                         # counter + last use (Libraries page); never raises
            from services.audio import console_library
            console_library.record_use("show", show["id"])
        except ImportError:                          # audio service absent: no Libraries page
            pass
        try:
            from .database import db_add_alert
            db_add_alert("alert.deploy.plugin_show_loaded", "info", vmid=vmid, kind="deploy",
                         params={"name": show["name"], "host": report["target"]["hostname"],
                                 "missing": report["missing"]})
        except Exception:
            log.exception("shows: load of #%s not journalled", show["id"])
    return status, j
