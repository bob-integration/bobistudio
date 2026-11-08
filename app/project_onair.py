# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""On-air state of a project's containers (2026-11-08).

A project keeps the CONFIGURATION of its containers, never what is on air: a mixer started again
comes back with input 1 on PGM, a player with no clip loaded. A plugin that wants better DECLARES
its on-air state and the orchestrator stores it without understanding it:

    "control": { …, "onair": {"read": "/onair", "restore": "/onair"} }

- `read`    GET (one of `read_endpoints`): a JSON object, opaque here — the plugin decides what is
            in it (mixer: PGM, PVW, keyers; player: clip, position, playing…). Put a `version` in
            it and refuse a document you no longer understand.
- `restore` POST (one of `endpoints`): applies that document. 2xx = applied; 4xx = refused (no
            retry); no answer / 5xx = not ready yet (retried for a while).

It is NOT the configuration (`control.persist`, deploy_config.params): it is kept apart, in table
`project_onair`, so that it can be resumed or not. When:
- captured at « Arrêter » (just before the containers go), and every `onair_capture_s` seconds
  while the project runs if the project asks for it (off by default — decided 2026-11-08);
- restored after « Démarrer » when the operator ticks « Reprendre l'état d'antenne » (unticked by
  default), and AUTOMATICALLY after the supervision restarted a crashed script — only from a
  capture taken during the current run, never from the state of an earlier show.

Rows are keyed by the entry's hostname as the started project names it (`restored_hostname`):
vmids change at every start, the entry does not."""
import json
import logging
import threading
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 5.0
CAPTURE_CHOICES_S = (0, 10, 30, 60, 300, 600)          # 0 = no periodic capture (default)
RESTORE_DELAYS_S = (2.0, 3.0, 5.0, 10.0, 20.0)          # rounds while the scripts come up
SAMPLER_TICK_S = 5.0


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ── Declaration ──────────────────────────────────────────────────────────────────────
def validate_spec(m):
    """Errors of the `control.onair` declaration (empty = valid or absent)."""
    ctrl = (m or {}).get("control") or {}
    o = ctrl.get("onair")
    if o is None:
        return []
    if not isinstance(o, dict):
        return ["control.onair must be an object"]
    eps = ctrl.get("endpoints") or []
    eps = set(eps) if isinstance(eps, list) else {("/" + k) for k in eps}
    errs = []
    if o.get("read") not in set(ctrl.get("read_endpoints") or []):
        errs.append("control.onair.read must be one of read_endpoints")
    if o.get("restore") not in eps:
        errs.append("control.onair.restore must be one of control.endpoints")
    return errs


def spec(m):
    o = ((m or {}).get("control") or {}).get("onair")
    if not isinstance(o, dict) or validate_spec(m):
        return None
    return {"read": o["read"], "restore": o["restore"]}


def spec_for_type(type_):
    from . import plugins
    return spec(plugins.get(type_) or {}) if type_ else None


# ── Storage ──────────────────────────────────────────────────────────────────────────
def _key(hostname, project_name):
    from .projects import restored_hostname
    return restored_hostname(hostname or "", project_name or "")


def stored(pid):
    """{key: {type, doc, captured_at}} of project `pid`."""
    from .database import get_db
    with get_db() as db:
        rows = db.execute("SELECT entry, type, doc, captured_at FROM project_onair WHERE project_id=?",
                          (pid,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["entry"]] = {"type": r["type"], "doc": json.loads(r["doc"]), "captured_at": r["captured_at"]}
        except (TypeError, ValueError):
            continue
    return out


def _store(pid, key, type_, doc):
    """Write the row only when the document changed. Returns True when it did."""
    from .database import get_db
    text = json.dumps(doc, sort_keys=True)
    with get_db() as db:
        old = db.execute("SELECT doc FROM project_onair WHERE project_id=? AND entry=?", (pid, key)).fetchone()
        if old and old["doc"] == text:
            return False
        db.execute("INSERT OR REPLACE INTO project_onair (project_id, entry, type, doc, captured_at) "
                   "VALUES (?,?,?,?,?)", (pid, key, type_, text, _now()))
        db.commit()
    return True


def interval_s(p):
    try:
        v = int((p or {}).get("onair_capture_s") or 0)
    except (TypeError, ValueError):
        return 0
    return v if v in CAPTURE_CHOICES_S else 0


def set_interval(pid, seconds):
    from .database import get_db
    seconds = int(seconds)
    if seconds not in CAPTURE_CHOICES_S:
        raise ValueError("onair_capture_s: one of %s" % ", ".join(map(str, CAPTURE_CHOICES_S)))
    with get_db() as db:
        db.execute("UPDATE projects SET onair_capture_s=? WHERE id=?", (seconds, pid))
        db.commit()


# ── Which containers ─────────────────────────────────────────────────────────────────
def _dc(c):
    dc = (c or {}).get("deploy_config")
    try:
        return json.loads(dc) if isinstance(dc, str) else (dc or {})
    except ValueError:
        return {}


def _live_entries(p):
    """[(key, vmid, type, spec)] for the RUNNING containers of project `p` that declare an on-air
    state. The snapshot's vmids follow the live containers after a start (_remap_project_snapshot)."""
    from .database import db_get_containers
    live = {c["vmid"]: c for c in db_get_containers()}
    out = []
    for e in p.get("snapshot") or []:
        c = live.get(e.get("vmid"))
        if not c or c.get("project_id") not in (p["id"], None):
            continue                     # stopped, or the vmid now belongs to someone else
        t = _dc(c).get("type")
        sp = spec_for_type(t)
        if sp:
            out.append((_key(e.get("hostname"), p.get("name")), c["vmid"], t, sp))
    return out


def _url(vmid, path):
    from .plugin_persist import _url as persist_url
    return persist_url(vmid, path)


# ── Capture ──────────────────────────────────────────────────────────────────────────
def _read(vmid, sp):
    url = _url(vmid, sp["read"])
    if not url:
        raise RuntimeError("container IP not found")
    r = requests.get(url, timeout=HTTP_TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError("%s -> HTTP %s" % (sp["read"], r.status_code))
    doc = r.json()
    if not isinstance(doc, dict):
        raise RuntimeError("%s: JSON object expected" % sp["read"])
    return doc


def capture(pid, alert=True):
    """Read and store the on-air state of every running container of `pid` that declares one.
    A container that does not answer keeps its previous capture. Returns {read, changed, failed}."""
    from .database import db_get_project, db_add_alert
    p = db_get_project(pid)
    if not p:
        return {"read": 0, "changed": 0, "failed": []}
    n = changed = 0
    failed = []
    for key, vmid, type_, sp in _live_entries(p):
        try:
            doc = _read(vmid, sp)
        except Exception as e:
            failed.append({"entry": key, "vmid": vmid, "error": str(e)})
            continue
        n += 1
        changed += _store(pid, key, type_, doc)
    if failed and alert:
        try:
            db_add_alert("alert.projet.onair_capture_failed", "warning", kind="deploy",
                         params={"p": p["name"], "list": ", ".join(f["entry"] for f in failed)})
        except Exception:
            pass
    if failed:
        log.warning("project #%s: on-air state not read from %s", pid,
                    "; ".join("%s (%s)" % (f["entry"], f["error"]) for f in failed))
    return {"read": n, "changed": changed, "failed": failed}


# ── Restore ──────────────────────────────────────────────────────────────────────────
def _post(vmid, sp, doc):
    """'ok' | 'refused: …' | None (not ready, try again)."""
    url = _url(vmid, sp["restore"])
    if not url:
        return None
    try:
        r = requests.post(url, json=doc, timeout=HTTP_TIMEOUT_S)
    except Exception:
        return None
    if 200 <= r.status_code < 300:
        return "ok"
    if 400 <= r.status_code < 500 and r.status_code != 404:
        try:
            why = (r.json() or {}).get("error") or r.text[:200]
        except ValueError:
            why = r.text[:200]
        return "refused: %s" % why
    return None


def _restore_targets(targets, delays=RESTORE_DELAYS_S, progress=None):
    """targets: [(key, vmid, sp, doc)]. Rounds until every one answered. Returns {key: outcome}.
    `progress` lines join the start log, which is written in French like the rest of it."""
    _p = progress or (lambda m: None)
    todo, done = list(targets), {}
    for d in delays:
        if not todo:
            break
        time.sleep(d)
        left = []
        for t in todo:
            res = _post(t[1], t[2], t[3])
            if res is None:
                left.append(t)
                continue
            done[t[0]] = res
            _p(("  ✓ " if res == "ok" else "  ✕ ") + t[0] + "\u00a0: " + ("état d'antenne repris" if res == "ok" else res))
        todo = left
    for t in todo:
        done[t[0]] = "no answer"
        _p("  ✕ " + t[0] + "\u00a0: pas de réponse")
    return done


def restore_after_start(pid, entries, vmid_map, progress=None, delays=RESTORE_DELAYS_S):
    """After « Démarrer » with the box ticked. `entries`: the project's snapshot as read BEFORE the
    start (hostnames as saved, original vmids); `vmid_map`: original vmid -> created vmid."""
    from .database import db_get_project, db_get_container, db_add_alert
    p = db_get_project(pid) or {}
    saved = stored(pid)
    targets = []
    for e in entries or []:
        new = vmid_map.get(e.get("vmid"))
        key = _key(e.get("hostname"), p.get("name"))
        row = saved.get(key)
        if new is None or not row:
            continue
        sp = spec_for_type(_dc(db_get_container(new)).get("type"))
        if sp:
            targets.append((key, new, sp, row["doc"]))
    if not targets:
        return {}
    if progress:
        progress("Reprise de l'état d'antenne (%d conteneur(s))…" % len(targets))
    out = _restore_targets(targets, delays, progress)
    bad = [k for k, v in out.items() if v != "ok"]
    try:
        db_add_alert("alert.projet.onair_restored_partial" if bad else "alert.projet.onair_restored",
                     "warning" if bad else "info", kind="deploy",
                     params={"p": p.get("name"), "ok": len(out) - len(bad), "total": len(out),
                             "list": ", ".join(bad)})
    except Exception:
        pass
    return out


def after_auto_restart(vmid, delays=RESTORE_DELAYS_S):
    """The supervision restarted a crashed script: put back what was on air, from a capture taken
    since the project last started (never the state of an earlier show). Returns the outcome or
    None when there is nothing to resume. Never raises."""
    try:
        from .database import db_get_container, db_get_project, db_add_alert
        c = db_get_container(vmid) or {}
        pid = c.get("project_id")
        sp = spec_for_type(_dc(c).get("type"))
        if not pid or not sp:
            return None
        p = db_get_project(pid) or {}
        entry = next((e for e in p.get("snapshot") or [] if e.get("vmid") == vmid), None)
        if not entry:
            return None
        key = _key(entry.get("hostname"), p.get("name"))
        row = stored(pid).get(key)
        if not row or (p.get("started_at") and (row["captured_at"] or "") < p["started_at"]):
            return None
        res = _restore_targets([(key, vmid, sp, row["doc"])], delays).get(key)
        db_add_alert("alert.projet.onair_after_restart" if res == "ok" else "alert.projet.onair_after_restart_failed",
                     "info" if res == "ok" else "warning", vmid=vmid, kind="agent",
                     params={"h": key, "at": row["captured_at"], "e": res})
        return res
    except Exception as e:
        log.warning("on-air restore after restart #%s: %s", vmid, e)
        return None


# ── Summary for the window ───────────────────────────────────────────────────────────
def summary(pid):
    """What the project window and the start dialog show."""
    from .database import db_get_project, db_get_containers
    p = db_get_project(pid)
    if not p:
        return None
    live = {c["vmid"]: c for c in db_get_containers()}
    saved = stored(pid)
    rows = []
    for e in p.get("snapshot") or []:
        c = live.get(e.get("vmid"))
        if c and c.get("project_id") not in (pid, None):
            c = None
        t = _dc(c).get("type") or (e.get("deploy_config") or {}).get("type")
        if not spec_for_type(t):
            continue
        key = _key(e.get("hostname"), p.get("name"))
        rows.append({"entry": key, "type": t, "captured_at": (saved.get(key) or {}).get("captured_at")})
    dates = [r["captured_at"] for r in rows if r["captured_at"]]
    return {"interval_s": interval_s(p), "choices": list(CAPTURE_CHOICES_S), "containers": rows,
            "supported": len(rows), "saved": len(dates), "latest": max(dates) if dates else None}


# ── Periodic capture ─────────────────────────────────────────────────────────────────
_last = {}
_sampler = None


def _tick(now=None):
    from .database import get_db
    now = now if now is not None else time.monotonic()
    with get_db() as db:
        rows = db.execute("SELECT id, onair_capture_s FROM projects WHERE state='active' "
                          "AND onair_capture_s > 0").fetchall()
    for r in rows:
        pid, every = r["id"], int(r["onair_capture_s"] or 0)
        if every and now - _last.get(pid, 0.0) >= every:
            _last[pid] = now
            try:
                capture(pid, alert=False)          # a periodic miss is logged, not alerted
            except Exception as e:
                log.warning("project #%s: periodic on-air capture failed: %s", pid, e)


def start_sampler():
    global _sampler
    if _sampler:
        return _sampler

    def loop():
        while True:
            time.sleep(SAMPLER_TICK_S)
            try:
                _tick()
            except Exception as e:
                log.warning("on-air sampler: %s", e)
    _sampler = threading.Thread(target=loop, daemon=True, name="onair-sampler")
    _sampler.start()
    return _sampler
