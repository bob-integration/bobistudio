# SPDX-License-Identifier: GPL-3.0-or-later
"""KINDED preset library of a plugin: saves of several kinds (cart: grid, playlist, page), in the
COMMON library of the installation, captured from a container and recalled — live, by a macro or a
control surface — on any container of the same type.

It is NOT a parallel mechanism: it is the plugin PRESETS of `plugin_store` (scope '' = global to the
type, the very rows `presets-export` / `presets-import` carry since 4da0595 and `recall_presets` /
`recall_preset` list and apply for macros and Ember+), with two declarative additions to
`control.recall`:

    "recall": {
        "source": "plugin_store", "scope": "",
        "endpoint": "/recall",          # POST {kind, payload, name, **params} — the container applies
        "capture": "/capture",          # GET ?kind=&page=&zone= → {kind, payload, summary}
        "apply": "post",                # generic application (this module), no per-type branch
        "kinds": ["grid", "playlist", "page"]
    }

The stored VALUE describes itself: {kind, payload, summary, author, plugin_version}. The identity of a
save is (kind, name): a grid and a playlist may both be called « Matinale ». Lists are filtered by
kind (a macro « recall a playlist » only offers playlists).

Why not the SHOWS (app/plugin_shows.py): a show reloads a whole document by the structure path,
off air, and is deliberately kept away from macros; a cart save replaces ONE part (one kind) on air,
without cutting what plays, and must be recallable from a desk.

Medias are referenced by identity (`media.asset`), the path as a hint. On import from another
installation an asset unknown to the media base is REPORTED (counted in the value as
`missing_media`), never a refusal: the key is kept, its sound shows as missing on the node.

Errors are CODES (the interface translates them).
"""
import datetime
import json
import logging

log = logging.getLogger(__name__)

NAME_MAX = 60
HTTP_TIMEOUT_S = 5.0


class LibraryError(Exception):
    def __init__(self, code, status=400, **extra):
        super().__init__(code)
        self.code, self.status, self.extra = code, status, extra

    def body(self):
        return dict(self.extra, ok=False, error=self.code)


# ── declaration ──────────────────────────────────────────────────────────────────────────
def spec(manifest):
    """`control.recall` when it declares a kinded, generically applied library, else None."""
    rc = ((manifest or {}).get("control") or {}).get("recall")
    if isinstance(rc, dict) and rc.get("apply") == "post" and rc.get("kinds") \
            and rc.get("source") == "plugin_store":
        return rc
    return None


def _spec_of(type_):
    from . import plugins
    rc = spec(plugins.get(type_))
    if rc is None:
        raise LibraryError("no_library", 404, type=type_)
    return rc


def kind_of(value):
    return (value or {}).get("kind") if isinstance(value, dict) else None


def filter_kind(presets, kind):
    """Presets of one kind (None = all), order kept."""
    if not kind:
        return list(presets)
    return [p for p in presets if kind_of(p.get("value")) == kind]


def check_name(name):
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise LibraryError("bad_name", 400, max=NAME_MAX)
    return name


# ── library ──────────────────────────────────────────────────────────────────────────────
def _row_public(p):
    v = p.get("value") or {}
    return {"id": p["id"], "name": p["name"], "kind": v.get("kind"), "author": v.get("author") or "",
            "summary": v.get("summary") or {}, "missing_media": int(v.get("missing_media") or 0),
            "plugin_version": v.get("plugin_version"),
            "created_at": p.get("created_at"), "updated_at": p.get("updated_at")}


def list_saves(type_, kind=None):
    from .database import plugin_store_list
    rc = _spec_of(type_)
    if kind and kind not in rc["kinds"]:
        raise LibraryError("bad_kind", 400, kind=kind)
    return [_row_public(p) for p in filter_kind(plugin_store_list(type_, ""), kind)]


def find(type_, kind, name):
    from .database import plugin_store_list
    return next((p for p in plugin_store_list(type_, "")
                 if p["name"] == name and kind_of(p.get("value")) == kind), None)


def store(type_, kind, name, value, overwrite=False):
    """Create or (overwrite=True) replace the save (kind, name). Returns (id, created)."""
    from .database import plugin_store_create, plugin_store_update
    cur = find(type_, kind, name)
    if cur is not None:
        if not overwrite:
            raise LibraryError("name_taken", 409, id=cur["id"], name=name, kind=kind)
        plugin_store_update(cur["id"], value=value)
        return cur["id"], False
    return plugin_store_create(type_, "", name, value), True


def get(type_, sid):
    from .database import plugin_store_get
    p = plugin_store_get(sid)
    if not p or p.get("type") != type_ or p.get("scope") != "" or kind_of(p.get("value")) is None:
        raise LibraryError("unknown_save", 404, id=sid)
    return p


def delete(type_, sid):
    from .database import plugin_store_delete
    get(type_, sid)
    return plugin_store_delete(sid)


# ── container I/O ────────────────────────────────────────────────────────────────────────
def _type_of(vmid):
    from .database import db_get_container
    c = db_get_container(vmid)
    if not c:
        raise LibraryError("unknown_container", 404, vmid=vmid)
    dc = c.get("deploy_config")
    dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    return dc.get("type")


def _url(vmid, path):
    from .addressing import get_container_ip
    ip = get_container_ip(vmid)
    if not ip:
        raise LibraryError("container_unreachable", 502, vmid=vmid)
    return "http://%s:8082%s" % (ip, path)


def _http(method, vmid, path, **kw):
    """Indirection kept small so the offline bench can stand in for the container."""
    import requests
    return requests.request(method, _url(vmid, path), timeout=HTTP_TIMEOUT_S, **kw)


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def capture_and_save(vmid, kind, name, page=None, author="", overwrite=False, zone=None):
    """Capture the `kind` part of a running container and store it under `name`."""
    from . import plugins
    type_ = _type_of(vmid)
    rc = _spec_of(type_)
    if kind not in rc["kinds"]:
        raise LibraryError("bad_kind", 400, kind=kind)
    name = check_name(name)
    params = {"kind": kind}
    if page not in (None, ""):
        params["page"] = page
    if zone not in (None, ""):
        params["zone"] = zone           # cartouchier 0.1.8 : la zone capturée (absente = celle par défaut)
    try:
        r = _http("GET", vmid, rc.get("capture") or "/capture", params=params)
    except LibraryError:
        raise
    except Exception as e:                                               # noqa: BLE001
        log.warning("library capture #%s: %s", vmid, e)
        raise LibraryError("container_unreachable", 502, vmid=vmid)
    body = _json(r)
    if r.status_code >= 400 or not isinstance(body.get("payload"), dict):
        raise LibraryError(body.get("error") or "capture_failed", r.status_code if r.status_code >= 400 else 502)
    value = {"kind": kind, "payload": body["payload"], "summary": body.get("summary") or {},
             "author": author or "", "plugin_version": (plugins.get(type_) or {}).get("version"),
             "saved_at": datetime.datetime.now().isoformat(timespec="seconds")}
    sid, created = store(type_, kind, name, value, overwrite=overwrite)
    return {"ok": True, "id": sid, "created": created, "name": name, "kind": kind,
            "summary": value["summary"]}


def recall(vmid, type_, preset, extra=None):
    """Apply a save on a container: POST {kind, payload, name, **extra} to `control.recall.endpoint`
    then the declarative persistence (`control.persist.settings` lists the endpoint), exactly as a
    setting posted through the proxy. Returns the container's answer (its report: missing medias,
    renamed ids, relocated keys). Raises LibraryError."""
    from . import plugins
    rc = _spec_of(type_)
    value = preset.get("value") or {}
    kind = kind_of(value)
    if kind not in rc["kinds"] or not isinstance(value.get("payload"), dict):
        raise LibraryError("bad_save", 400, id=preset.get("id"))
    body = {k: v for k, v in (extra or {}).items() if k not in ("kind", "payload", "name")}
    body.update({"kind": kind, "payload": value["payload"], "name": preset.get("name")})
    endpoint = rc.get("endpoint") or "/recall"
    try:
        r = _http("POST", vmid, endpoint, json=body)
    except LibraryError:
        raise
    except Exception as e:                                               # noqa: BLE001
        log.warning("library recall #%s: %s", vmid, e)
        raise LibraryError("container_unreachable", 502, vmid=vmid)
    out = _json(r)
    if r.status_code >= 400:
        raise LibraryError(out.get("error") or "recall_failed", r.status_code, params=out.get("params") or {})
    try:
        from . import plugin_persist
        plugin_persist.after_post(vmid, plugins.get(type_) or {}, endpoint, r.status_code)
    except Exception:                                                    # noqa: BLE001
        log.exception("library recall #%s: persistence", vmid)
    return out


def recall_as_preset(vmid, type_, preset, extra=None):
    """`routes.recall_preset` contract (macros, Ember+): (ok, detail)."""
    try:
        out = recall(vmid, type_, preset, extra)
    except LibraryError as e:
        return False, "%s: %s" % (preset.get("name"), e.code)
    rep = out.get("recall") or {}
    n = int(rep.get("missing_count") or 0)
    return True, preset.get("name") + (" (%d media missing)" % n if n else "")


# ── exchange (the generic presets-export / presets-import routes call these) ─────────────
def _medias(value):
    p = (value or {}).get("payload") or {}
    out = []
    for pg in p.get("pages") or ([p["page"]] if isinstance(p.get("page"), dict) else []):
        out += [k for k in (pg or {}).get("keys") or [] if isinstance(k, dict)]
    out += [it for it in p.get("items") or [] if isinstance(it, dict)]
    return out


def asset_exists(aid):
    from . import media_registry
    return media_registry.db().execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone() is not None


def missing_media(value):
    """Keys / items whose media identity the media base of THIS installation does not know.
    A media given by path only cannot be checked here (the node resolves it): not reported."""
    out = []
    for it in _medias(value):
        m = it.get("media") or {}
        aid = m.get("asset")
        if aid and not asset_exists(aid):
            out.append({"title": it.get("title") or "", "asset": aid, "file": m.get("file") or ""})
    return out


def check_import_entry(rc, value):
    """Refusal reason (code) of an imported value for a kinded library, or None."""
    if kind_of(value) not in rc["kinds"]:
        return "bad_kind"
    if not isinstance(value.get("payload"), dict):
        return "bad_save"
    return None
