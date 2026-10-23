# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""STATUS PER FLOW published by a producer, read for a consumer (the audio console's strips).

A producer that plays timed content (cart machine today; player and recorder tomorrow) DECLARES that
it publishes, per MXL flow it produces, what is playing on it:

    "control": { …, "flow_status": {"endpoint": "/flows", "panel": true} }

  - `endpoint`: a GET path whitelisted in `control.read_endpoints` (same whitelist as the proxy and
    `macros.fetch_state`), answering {<flow name>: {title, state, remaining_ms, elapsed_ms, duration_ms,
    intro_ms, outro_ms, player, alert_s?, …}} — contract in plugins/cartouchier/help.md;
  - `panel` (optional): the plugin's UI may be MOUNTED in a consumer's panel (the console's cart panel).

The orchestrator knows who produces a flow (`plugins.derive_wiring` of the producer: the exact names of
`wiring.produces`); the console knows which flow feeds a strip (`doc.inputs[].flow`). `console_sources`
joins the two: ONE read per producer whatever the number of flows / consoles / browsers (cache
CACHE_S, shared), a short timeout, and only producers that declare `flow_status` are ever called.

Pure where it can be: `fetch` and `containers` are injectable (tests run offline).
"""
import json
import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

CACHE_S = 0.5            # N browsers polling at 1 Hz → at most 2 reads/s per producer
READ_TIMEOUT_S = 0.6
FIELDS = ("title", "state", "player", "remaining_ms", "elapsed_ms", "duration_ms", "intro_ms", "outro_ms",
          "intro_remaining_ms", "outro_remaining_ms", "alert_s")
STATES = ("idle", "cued", "playing", "paused", "fading")


# ── declaration ──────────────────────────────────────────────────────────────────────
def validate_spec(manifest):
    """Errors of `control.flow_status` (empty list = valid or absent)."""
    ctrl = (manifest or {}).get("control") or {}
    fs = ctrl.get("flow_status")
    if fs is None:
        return []
    if not isinstance(fs, dict):
        return ["control.flow_status must be an object"]
    errs = []
    ep = fs.get("endpoint")
    if not isinstance(ep, str) or not ep.startswith("/"):
        errs.append("control.flow_status.endpoint must be a path starting with /")
    elif ep.split("?", 1)[0] not in set(ctrl.get("read_endpoints") or []):
        errs.append("control.flow_status.endpoint %s is not in control.read_endpoints" % ep)
    if "panel" in fs and not isinstance(fs["panel"], bool):
        errs.append("control.flow_status.panel must be a boolean")
    if not ((manifest or {}).get("wiring") or {}).get("produces"):
        errs.append("control.flow_status without wiring.produces: no flow to publish a status for")
    unknown = set(fs) - {"endpoint", "panel"}
    if unknown:
        errs.append("control.flow_status: unknown keys %s" % ", ".join(sorted(unknown)))
    return errs


def spec(manifest):
    """{endpoint, port, panel} when the manifest declares a VALID `control.flow_status`, else None."""
    m = manifest or {}
    fs = (m.get("control") or {}).get("flow_status")
    if not isinstance(fs, dict) or validate_spec(m):
        return None
    return {"endpoint": fs["endpoint"], "port": int((m.get("control") or {}).get("port") or 8082),
            "panel": bool(fs.get("panel"))}


def _dc(c):
    dc = c.get("deploy_config")
    if isinstance(dc, str):
        try:
            dc = json.loads(dc)
        except ValueError:
            dc = None
    return dc if isinstance(dc, dict) else {}


def declaring(containers):
    """[(container, manifest, spec)] of the containers whose plugin declares flow_status."""
    from . import plugins          # lazy: plugins imports this module at scan time (validate_spec)
    out = []
    for c in containers:
        m = plugins.get(_dc(c).get("type"))
        sp = spec(m) if m else None
        if sp:
            out.append((c, m, sp))
    return out


def produced_index(containers):
    """{flow name: (container, manifest, spec)} — only for producers that declare flow_status; the names
    are those of `wiring.produces` resolved for that container (hostname + params), never guessed."""
    from . import plugins
    idx = {}
    for c, m, sp in declaring(containers):
        dc = _dc(c)
        try:
            w = plugins.derive_wiring(m["type"], c.get("hostname") or "", dc.get("params") or {})
        except Exception as e:   # noqa: BLE001 — one broken producer must not blind the others
            log.debug("flow_status: wiring of #%s not derivable (%s)", c.get("vmid"), e)
            continue
        for p in w.get("produces") or []:
            if isinstance(p, dict) and isinstance(p.get("shm"), str) and p["shm"]:
                idx[p["shm"]] = (c, m, sp)
    return idx


def console_ports(doc):
    """[(port id, flow)] of a console document's wired input ports."""
    return [(i["id"], i["flow"]) for i in (doc or {}).get("inputs") or []
            if isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("flow"), str) and i["flow"]]


# ── reading (one call per producer, shared cache) ────────────────────────────────────
_cache = {}              # vmid -> (monotonic time, epoch ms of the read, data or None)
_locks = {}
_locks_guard = threading.Lock()


def _lock_of(vmid):
    with _locks_guard:
        return _locks.setdefault(vmid, threading.Lock())


def _http_fetch(vmid, sp):
    from .addressing import get_container_ip
    ip = get_container_ip(vmid)
    if not ip:
        return None
    try:
        r = requests.get(f"http://{ip}:{sp['port']}{sp['endpoint']}", timeout=READ_TIMEOUT_S)
        d = r.json() if r.status_code == 200 else None
        return d if isinstance(d, dict) else None
    except Exception:   # noqa: BLE001 — unreachable producer = no status, never an error page
        return None


def read_producer(vmid, sp, fetch=None, now=None):
    """(data or None, epoch ms of that read). One HTTP read per producer per CACHE_S whoever asks: the lock
    makes concurrent requests wait for the read in flight instead of each starting its own."""
    fetch = fetch or _http_fetch
    with _lock_of(vmid):
        t = time.monotonic() if now is None else now
        hit = _cache.get(vmid)
        if hit and t - hit[0] < CACHE_S:
            return hit[2], hit[1]
        data = fetch(vmid, sp)
        ts = int(time.time() * 1000)
        _cache[vmid] = (t, ts, data)
        return data, ts


def reset():
    _cache.clear()


def _entry(raw, producer_vmid, ts, now_ms):
    if not isinstance(raw, dict):
        return None
    e = {k: raw.get(k) for k in FIELDS if k in raw}
    if e.get("state") not in STATES:
        e["state"] = "idle"
    e["title"] = str(e.get("title") or "")
    e.update(producer_vmid=producer_vmid, ts=ts, age_ms=max(0, now_ms - ts))
    return e


def console_sources(doc, containers=None, fetch=None, now=None):
    """{port id: {title, remaining_ms, elapsed_ms, duration_ms, intro_ms, outro_ms, state, player,
    producer_vmid, ts, age_ms, …}} for the ports of `doc` fed by a producer that declares flow_status and
    publishes that flow. Ports without such a producer are absent (nothing to show)."""
    if containers is None:
        from .database import db_get_containers
        containers = db_get_containers()
    idx = produced_index(containers)
    by_prod = {}
    for pid, flow in console_ports(doc):
        hit = idx.get(flow)
        if hit:
            by_prod.setdefault(hit[0]["vmid"], (hit[2], []))[1].append((pid, flow))
    out, now_ms = {}, int(time.time() * 1000)
    for vmid, (sp, ports) in by_prod.items():
        data, ts = read_producer(vmid, sp, fetch=fetch, now=now)
        if not isinstance(data, dict):
            continue
        for pid, flow in ports:
            e = _entry(data.get(flow), vmid, ts, now_ms)
            if e:
                out[pid] = e
    return out


def console_producers(doc, containers=None):
    """{ports: {port id: producer vmid}, producers: [{vmid, instance_uuid, type, label, hostname, version,
    panel, node_id}]} — which ports have a status-publishing producer (the console polls only if any)."""
    if containers is None:
        from .database import db_get_containers
        containers = db_get_containers()
    idx = produced_index(containers)
    ports, prods = {}, {}
    for pid, flow in console_ports(doc):
        hit = idx.get(flow)
        if hit:
            ports[pid] = hit[0]["vmid"]
            prods[hit[0]["vmid"]] = hit
    return {"ports": ports, "producers": [describe(c, m, sp) for c, m, sp in prods.values()]}


def describe(c, m, sp):
    dc = _dc(c)
    return {"vmid": c["vmid"], "instance_uuid": c.get("instance_uuid"), "type": m["type"],
            "label": m.get("label") or m["type"], "hostname": c.get("hostname") or "",
            "version": (dc.get("params") or {}).get("plugin_version") or m.get("version"),
            "panel": bool(sp.get("panel")), "node_id": c.get("node_id")}
