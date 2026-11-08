"""Project ENVIRONMENT: the shared services a project relies on without owning them.

A plugin declares in its manifest how it relates to projects:

    "environment": {"scope": "partitioned" | "shared",
                    "wanted_by": {"sections": [...], "types": [...]}}

- `partitioned`: inside a project the container only serves THAT project (the transcode farm only
  hands a transcoder the jobs of its own project). Outside any project it serves everyone.
- `shared`: the container serves every project whatever its own project (a pyramide is picked by
  node and source, a plugin GUI by node). Putting it in a project only ties its life to that
  project: unloading the project destroys it for everybody.
- `wanted_by`: which project contents need it (a nav section, or types). Absent = always wanted.

Two uses (asked on 2026-11-07):
1. a REMINDER when such a container is attached to a project (`attach_notice`);
2. at save, the services the project used from OUTSIDE are recorded (`capture`), carried by the
   project export, and at recall the ones missing on the destination cluster are proposed for
   creation, outside any project (`missing`). A project saved before this record existed falls
   back on `wanted_by` alone.
"""
import json
import logging
from datetime import datetime, timezone

from . import plugins as _pl
from .database import get_db, db_get_containers, db_get_nodes

log = logging.getLogger(__name__)

SCOPES = ("partitioned", "shared")


def _ensure():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS project_environment (
        project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        services    TEXT NOT NULL DEFAULT '[]',
        captured_at TEXT)""")
    return db


def declared(type_):
    """The manifest's `environment` block of a type, normalized, or None."""
    env = (_pl.get(type_) or {}).get("environment")
    if not isinstance(env, dict) or env.get("scope") not in SCOPES:
        return None
    wb = env.get("wanted_by") if isinstance(env.get("wanted_by"), dict) else None
    return {"scope": env["scope"],
            "wanted_by": {"sections": [s for s in (wb or {}).get("sections") or [] if isinstance(s, str)],
                          "types": [t for t in (wb or {}).get("types") or [] if isinstance(t, str)]}
            if wb else None}


def declaring_types():
    return {m["type"]: declared(m["type"]) for m in _pl.all()
            if m.get("type") and declared(m["type"])}


def _section(type_):
    return ((_pl.get(type_) or {}).get("nav") or {}).get("section")


def wanted(type_, env, project_types):
    """True when a project made of `project_types` needs a `type_` service."""
    wb = env.get("wanted_by")
    if not wb:
        return True
    others = {t for t in project_types if t != type_}
    return bool(others & set(wb["types"])) or any(_section(t) in wb["sections"] for t in others)


def _dc(c):
    dc = c.get("deploy_config")
    if isinstance(dc, str):
        try:
            dc = json.loads(dc)
        except Exception:
            dc = {}
    return dc if isinstance(dc, dict) else {}


def _types_of(snapshot):
    return {_dc(c).get("type") for c in snapshot or []} - {None}


def serving(type_, env, pid, live=None):
    """Live containers of `type_` that serve project `pid`."""
    out = []
    for c in live if live is not None else db_get_containers():
        if _dc(c).get("type") != type_:
            continue
        if env["scope"] == "partitioned" and c.get("project_id") not in (None, pid):
            continue
        out.append(c)
    return out


def attach_notice(type_):
    """Reminder shown before a container of `type_` joins a project: {scope, label} or None."""
    env = declared(type_)
    if not env:
        return None
    return {"type": type_, "scope": env["scope"], "label": _pl.libelle_type(type_)}


def capture(pid, snapshot):
    """Records the services project `pid` uses from outside itself. Best-effort, never raises."""
    try:
        types = _types_of(snapshot)
        own = {c.get("vmid") for c in snapshot or []}
        live = db_get_containers()
        services = []
        for t, env in sorted(declaring_types().items()):
            if t in types or not wanted(t, env, types):
                continue
            serves = serving(t, env, pid, live)
            if any(c.get("vmid") in own or c.get("project_id") == pid for c in serves):
                continue        # the project has its own
            used = serves
            if not used:
                continue
            params = _dc(used[0]).get("params") or {}
            # Only what the user can set (config_schema): hostname, rings and the like belong to
            # the source cluster.
            keys = {f.get("key") for f in (_pl.get(t) or {}).get("config_schema") or []
                    if isinstance(f, dict)}
            services.append({"type": t, "count": len(used),
                             "plugin_version": params.get("plugin_version"),
                             "params": {k: v for k, v in params.items() if k in keys}})
        save(pid, services)
        return services
    except Exception as e:
        log.warning("project #%s environment capture failed: %s", pid, e)
        return None


def save(pid, services):
    clean = [s for s in services or [] if isinstance(s, dict) and isinstance(s.get("type"), str)]
    db = _ensure()
    db.execute("INSERT INTO project_environment (project_id, services, captured_at) VALUES (?,?,?) "
               "ON CONFLICT(project_id) DO UPDATE SET services=excluded.services, "
               "captured_at=excluded.captured_at",
               (pid, json.dumps(clean), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()


def recorded(pid):
    """Recorded services, or None when the project was never captured (saved before 2026-11-07)."""
    row = _ensure().execute("SELECT services FROM project_environment WHERE project_id=?",
                            (pid,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["services"]) or []
    except Exception:
        return []


def _free_hostname(base, taken):
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def missing(pid, snapshot):
    """Services the project needs that nothing on this cluster provides, with what is needed
    to propose their creation (outside any project)."""
    types = _types_of(snapshot)
    envs = declaring_types()
    rec = recorded(pid)
    wanted_types = {}
    for s in rec or []:
        if s["type"] in envs:
            wanted_types[s["type"]] = ("recorded", s)
    for t, env in envs.items():
        if t not in wanted_types and wanted(t, env, types):
            wanted_types[t] = ("needed", None)
    live = db_get_containers()
    taken = {(c.get("hostname") or "").lower() for c in live}
    out = []
    for t, (why, s) in sorted(wanted_types.items()):
        if t in types or serving(t, envs[t], pid, live):
            continue
        host = _free_hostname(t.replace("_", "-"), taken)
        taken.add(host.lower())
        out.append({"type": t, "label": _pl.libelle_type(t), "scope": envs[t]["scope"],
                    "reason": why, "hostname": host, "nodes": nodes(t),
                    "params": (s or {}).get("params") or {}})
    return out


def nodes(type_):
    """Nodes a missing `type_` service can be created on: up, and with the capability its image
    needs (same rule as the creation route — media image or volume, GUI image)."""
    from .node_driver import node_capabilities
    need = None
    if _pl.image_variant(type_) == "media" or _pl.wants_media_volume(type_):
        need = "media"
    elif _pl.image_variant(type_) == "gui":
        need = "gui"
    out = []
    for n in db_get_nodes():
        if n.get("status") != "up":
            continue
        try:
            if need and need not in node_capabilities(n):
                continue
        except Exception:
            continue
        out.append({"id": n["id"], "name": n["name"]})
    return out
