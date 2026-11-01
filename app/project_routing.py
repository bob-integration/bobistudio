# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Project outputs as a macro provider (core, `core_plugins.CORE_PROVIDERS`): route a project
DESTINATION port onto one of the project's flows, and read what it publishes.

The gesture itself is `project_ports.route_dest` — the same one the Ports window of /projects and
the Dyvi panel's Outputs zone make. Without this provider, switching an output was possible from
a web page only, i.e. dead for macros, triggers and panels (CLAUDE.md, "Exposer aux macros").
"""
import logging

log = logging.getLogger(__name__)


def _dests(pid=None):
    from .database import db_project_ports, db_get_projects
    names = {p["id"]: p["name"] for p in db_get_projects()}
    return [dict(p, project_name=names.get(p["project_id"], "")) for p in db_project_ports(pid)
            if p.get("kind") == "dest" and not p.get("parent_id")]


def _port_label(p, with_project):
    return ("%s · %s" % (p["project_name"], p["name"])) if with_project else p["name"]


def flow_label(flow):
    return "%s · %s" % (flow["hostname"], flow["label"] or flow["shm"])


_PORT = {"key": "port", "label": "Sortie du projet", "type": "select", "options_hook": True}
_ACTIONS = [
    {"id": "route_output", "label": "Sortie de projet : publier un flux du projet", "availability": "project",
     "params": [_PORT, {"key": "source", "label": "Flux publié (vide = plus rien)", "type": "select",
                        "options_hook": True}]},
    {"id": "patch", "label": "Grille du projet : câbler une destination vidéo sur une source", "availability": "project",
     "params": [{"key": "dest", "label": "Destination (sortie du projet ou entrée d'un conteneur)", "type": "select",
                 "options_hook": True},
                {"key": "source", "label": "Source (entrée du projet ou flux d'un conteneur)", "type": "select",
                 "options_hook": True}]},
]


def _states():
    out = []
    try:
        for p in _dests():
            out.append({"id": "output:%d" % p["id"], "type": "text",
                        "label": "Sortie %s : flux publié" % _port_label(p, True)})
        from .database import db_get_projects
        for pr in db_get_projects():
            for g in crosspoints(pr["id"])["dests"]:
                for i in g["items"]:
                    if i["ref"].startswith("in:"):
                        out.append({"id": "input:%d|%s" % (pr["id"], i["ref"]), "type": "text",
                                    "label": "%s · %s %s : flux reçu" % (pr["name"], g["label"], i["label"])})
    except Exception:
        log.exception("project routing: macro states listing failed")
    return out


class _Provider(dict):
    def get(self, k, default=None):
        return _states() if k == "states" else super().get(k, default)

    def __getitem__(self, k):
        return _states() if k == "states" else super().__getitem__(k)


MACRO_PROVIDER = _Provider({"id": "project_routing", "label": "Sorties de projet", "actions": _ACTIONS})


def _xp_options(groups, pid):
    out = []
    for g in groups:
        for i in g["items"]:
            out.append({"value": "%d|%s" % (pid, i["ref"]),
                        "label": "%s · %s" % (g["label"], i["label"]) if g["label"] else i["label"]})
    return out


def action_options(action_id, key, pid=None):
    if action_id == "patch" and key in ("dest", "source"):
        from .database import db_get_projects
        out = []
        for q in [pid] if pid else [p["id"] for p in db_get_projects()]:
            out += _xp_options(crosspoints(q)["dests" if key == "dest" else "sources"], q)
        return out
    if action_id != "route_output":
        return []
    if key == "port":
        return [{"value": str(p["id"]), "label": _port_label(p, pid is None)} for p in _dests(pid)]
    if key == "source":
        from . import project_ports
        from .database import db_get_projects
        pids = [pid] if pid else [p["id"] for p in db_get_projects()]
        out, seen = [{"value": "", "label": "—"}], set()
        for q in pids:
            for f in project_ports.project_flows(q):
                if f["shm"] not in seen:
                    seen.add(f["shm"])
                    out.append({"value": f["shm"], "label": flow_label(f)})
        return out
    return []


def _split_xp(value):
    """"<project id>|<ref>" → (project id, ref)."""
    q, _, ref = str(value or "").partition("|")
    try:
        return int(q), ref
    except ValueError:
        raise RuntimeError("grille du projet : valeur invalide %r" % value)


def run_action(action_id, params, ctx):
    if action_id == "patch":
        pid, dest = _split_xp((params or {}).get("dest"))
        pid2, src = _split_xp((params or {}).get("source"))
        cpid = (ctx or {}).get("project_id")
        if pid != pid2 or (cpid and int(cpid) != pid):
            raise RuntimeError("grille du projet : destination et source doivent être dans le projet de la macro")
        ok, reason = patch(pid, dest, src, (ctx or {}).get("user"))
        if not ok:
            raise RuntimeError("grille du projet : câblage refusé (%s)" % reason)
        return {"ok": True}
    if action_id != "route_output":
        raise RuntimeError("action inconnue : %s" % action_id)
    from . import project_ports
    from .database import db_get_port
    try:
        port_id = int((params or {}).get("port"))
    except (TypeError, ValueError):
        raise RuntimeError("sortie de projet : port invalide")
    port = db_get_port(port_id) or {}
    pid = (ctx or {}).get("project_id")
    if pid and port.get("project_id") != int(pid):
        raise RuntimeError("sortie de projet : ce port n'est pas dans le projet de la macro")
    ok, reason, rewired = project_ports.route_dest(port_id, (params or {}).get("source") or "")
    if not ok:
        raise RuntimeError("sortie de projet refusée : %s" % reason)
    failed = [r for r in rewired if not r.get("ok")]
    log.info("project routing: port %s -> %r (%d input(s) rewired, %d failed) by %s", port_id,
             (params or {}).get("source") or "", len(rewired), len(failed), (ctx or {}).get("user") or "?")
    return {"ok": True, "rewired": len(rewired), "failed": len(failed)}


def read_state(state_id, ctx):
    kind, _, sid = str(state_id).partition(":")
    if kind == "input":
        pid, ref = _split_xp(sid)
        xp = crosspoints(pid)
        dest = _find(xp["dests"], ref)
        if dest is None or not dest["shm"]:
            return ""
        src = next((i for g in xp["sources"] for i in g["items"] if i["shm"] == dest["shm"]), None)
        return src["label"] if src else dest["shm"]
    if kind != "output":
        raise RuntimeError("état inconnu : %s" % state_id)
    from . import project_ports
    from .database import db_get_port
    port = db_get_port(int(sid))
    shm = project_ports.port_shm(port) if port else ""
    if not shm:
        return ""
    flow = next((f for f in project_ports.project_flows(port["project_id"]) if f["shm"] == shm), None)
    return flow_label(flow) if flow else shm


# ─── Crosspoints: every video destination and source of a project (2026-10-30) ─────────────
#
# A router view of the project: DESTINATIONS = its destination ports (what it publishes) + every
# video input of its containers; SOURCES = its source ports (what it receives) + every video flow
# its containers produce. Grouped by container, the project's ports first. Patching an input goes
# through `_apply_wire` / `project_ports.wire_port` — the Cables page's path, project boundary
# included — and a destination port through `route_dest`. Used by the Dyvi panel's router zone and
# by macros (action `patch`).
#
# Left out: the compositor fabric's internal containers (pyramid proxies, wall shards: they serve
# the multiview, nobody patches them by hand) and 2110_io outputs (changing a TX source can freeze
# the card and asks a human confirmation a panel or a macro cannot give).

def _members(pid):
    from . import plugins as _plugins
    from .compositor_fabric import fabric_layout
    from .database import db_get_containers, db_get_project
    from .routes.shared import _load_dc
    proj = db_get_project(pid) or {}
    snap = {sc.get("vmid") for sc in proj.get("snapshot") or []}
    cs = [c for c in db_get_containers() if c.get("project_id") == pid or c["vmid"] in snap]
    roles = fabric_layout(cs)
    out = []
    for c in sorted(cs, key=lambda x: (x.get("hostname") or "", x["vmid"])):
        if (roles.get(c["vmid"]) or {}).get("role") in ("proxy", "shard"):
            continue
        dc = _load_dc(c) or {}
        if _plugins.is_plugin(dc.get("type")):
            out.append((c, dc))
    return out


def crosspoints(pid):
    """{"dests": [group], "sources": [group]}; group = {id, label, items: [{ref, label, shm}]}.
    A destination's `shm` is the flow it carries now ("" = nothing), a source's the flow it is.
    Refs: `port:<id>`, `in:<instance uuid>:<slot>` (a container input), or a flow name."""
    from . import plugins as _plugins, project_ports
    from .database import db_project_ports
    ports = [p for p in db_project_ports(pid) if not p.get("parent_id") and p.get("media") == "video"]

    def port_items(kind):
        return [{"ref": "port:%d" % p["id"], "label": p["name"], "shm": project_ports.port_shm(p)}
                for p in ports if p.get("kind") == kind]

    dests = [{"id": "ports", "label": "", "items": port_items("dest")}]
    sources = [{"id": "ports", "label": "", "items": port_items("source")}]
    members = _members(pid)
    types = [dc.get("type") for _c, dc in members]
    for c, dc in members:
        t, params = dc["type"], dc.get("params") or {}
        hn = params.get("hostname") or c.get("hostname") or ""
        try:
            w = _plugins.derive_wiring(t, hn, params)
        except Exception as e:
            log.warning("crosspoints: wiring of #%s not derived (%s)", c["vmid"], e)
            continue
        gid = c.get("instance_uuid") or str(c["vmid"])
        label = ((_plugins.get(t) or {}).get("label") if types.count(t) == 1 else None) or hn
        ins = []
        for s in w.get("consumes") or [] if t != "2110_io" else []:
            if (s.get("essence") or "video") != "video":
                continue
            slot = s["key"] if s.get("key") is not None else s.get("slot")
            cur = s.get("shm") or (params.get(s["state_field"]) if s.get("state_field") else "") or ""
            ins.append({"ref": "in:%s:%s" % (gid, "" if slot is None else slot),
                        "label": s.get("label") or "%s %s" % (label, slot), "shm": project_ports._bare(str(cur))})
        outs = [{"ref": p["shm"], "label": p.get("label") or p["shm"], "shm": p["shm"]}
                for p in w.get("produces") or [] if (p.get("essence") or "video") == "video" and p.get("shm")]
        if ins:
            dests.append({"id": gid, "label": label, "items": ins})
        if outs:
            sources.append({"id": gid, "label": label, "items": outs})
    return {"dests": [g for g in dests if g["items"]], "sources": [g for g in sources if g["items"]]}


def _find(groups, ref):
    return next((i for g in groups for i in g["items"] if i["ref"] == ref), None)


def patch(pid, dest_ref, source_ref, actor=None):
    """Put `source_ref` on `dest_ref`, both from `crosspoints(pid)`. Returns (ok, reason); the
    same source again is a no-op (nothing redeployed)."""
    from . import project_ports
    xp = crosspoints(pid)
    dest, src = _find(xp["dests"], dest_ref), _find(xp["sources"], source_ref)
    if dest is None:
        return False, "unknown_dest"
    if src is None:
        return False, "unknown_source"
    if src["shm"] and src["shm"] == dest["shm"]:
        return True, None
    port_dest = project_ports.parse_ref(dest_ref)
    if port_dest is not None:
        if project_ports.parse_ref(source_ref) is not None:
            return False, "port_to_port"   # a destination publishes one of the project's flows
        ok, reason, rewired = project_ports.route_dest(port_dest, src["shm"])
    else:
        _in, uuid, slot = dest_ref.split(":", 2)
        vmid = project_ports._vmid_for_uuid(uuid)
        if vmid is None:
            return False, "unknown_dest"
        slot = project_ports.norm_slot(slot) if slot != "" else None
        port_src = project_ports.parse_ref(source_ref)
        if port_src is not None:
            ok, _status, payload = project_ports.wire_port(port_src, vmid, "video", slot)
        else:
            from .routes.cabling import _apply_wire
            ok, _status, payload = _apply_wire(project_ports.producer_vmid(src["shm"]), vmid, src["shm"],
                                               "video", slot)
        reason = None if ok else ((payload or {}).get("reason") or (payload or {}).get("error") or "refused")
    log.info("crosspoints: %s -> %s %s (%s) by %s", source_ref, dest_ref, "done" if ok else "refused",
             reason, actor or "?")
    return ok, reason
