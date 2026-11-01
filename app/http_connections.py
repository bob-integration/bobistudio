# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Requests in flight on the orchestrator's web servers, and how many service threads they hold.

WHY. Waitress serves the whole product with a FIXED pool of threads (16 on :5000, see main.py). A
request holds its thread for as long as its response is being written — a few milliseconds for a
JSON read, but the WHOLE LIFE of a stream: an SSE state stream (state relay), a deploy log, a media
read. Sixteen browsers with a stream each, and the seventeenth request — any request, a login, the
dashboard poll — waits in the queue. Nothing said so until now: the server looked idle (no CPU), the
pages simply stopped answering.

WHAT. A WSGI middleware (`install`) registers each request on entry and removes it when the server
closes its response (`close()`, PEP 3333 — that is when the thread is given back). Per request:
thread, start, method, path (secret-looking segments masked, never the query string), client
address, authenticated user NAME, kind (sse / stream / request), target container, bytes sent.
Per server pool: threads busy / total (the server's own counter when it exposes one), queue depth,
peak, number of times every thread was taken, and a saturation alert (rising AND falling edge only).

COST on the hot path: one small dict, one dict insert and one pop (atomic under the GIL — no lock),
one integer add per response chunk. Nothing is read from the database on the request path; user
names and container hostnames are resolved only when an administrator READS the snapshot.

NEVER exposed: cookie, session id, API token, Authorization header, password, query string. The
user is identified by the account Flask already resolved (`g._user`) — this module never resolves
a token itself.
"""
import itertools
import logging
import math
import re
import threading
import time

log = logging.getLogger("bobistudio.http_connections")

ENV_KEY = "bobistudio.connection"

# Saturation alert: busy >= ceil(total * ALERT_RATIO) for ALERT_HOLD_S → one alert; back under the
# threshold for ALERT_HOLD_S → one "back to normal". 16 threads → threshold 14.
ALERT_RATIO = 0.875
ALERT_HOLD_S = 10.0
SAMPLE_S = 0.5

_ids = itertools.count(1)
_entries = {}            # rid -> entry dict (owned by the serving thread; read by snapshots)
_pools = {}              # SERVER_PORT (str) -> Pool
_started_at = time.time()


# ─── Server pools ──────────────────────────────────────────────────────────────────────────────

class Pool:
    """One listening server and its thread pool. `busy_fn`/`queue_fn`/`open_fn` read the server's
    OWN counters when it has them (Waitress, cheroot); without them, the registry count is used."""

    def __init__(self, port, name, threads, busy_fn=None, queue_fn=None, open_fn=None):
        self.port = str(port)
        self.name = name
        self.threads = int(threads) if threads else None
        self.busy_fn, self.queue_fn, self.open_fn = busy_fn, queue_fn, open_fn
        self.max_busy = 0
        self.max_busy_at = None
        self.max_queue = 0
        self.all_busy_events = 0         # rising edges of "every thread taken"
        self.all_busy_s = 0.0            # cumulated time with every thread taken (sampled)
        self._full = False
        self._last_sample = None
        self.watch = SaturationWatch(self.threads) if self.threads else None

    def registry_busy(self):
        return sum(1 for e in list(_entries.values()) if e.get("port") == self.port)

    def busy(self):
        if self.busy_fn is not None:
            try:
                return max(0, int(self.busy_fn()))
            except Exception:                                            # noqa: BLE001
                pass
        return self.registry_busy()

    def queue(self):
        if self.queue_fn is None:
            return None
        try:
            return max(0, int(self.queue_fn()))
        except Exception:                                                # noqa: BLE001
            return None

    def open_connections(self):
        if self.open_fn is None:
            return None
        try:
            return max(0, int(self.open_fn()))
        except Exception:                                                # noqa: BLE001
            return None

    def note_busy(self, busy, now=None):
        if busy > self.max_busy:
            self.max_busy = busy
            self.max_busy_at = now or time.time()

    def sample(self, now):
        """One sampler tick → ('raise'|'clear'|None, busy)."""
        busy = self.busy()
        self.note_busy(busy, now)
        q = self.queue()
        if q is not None and q > self.max_queue:
            self.max_queue = q
        full = bool(self.threads) and busy >= self.threads
        if full and self._last_sample is not None and self._full:
            self.all_busy_s += now - self._last_sample
        if full and not self._full:
            self.all_busy_events += 1
        self._full = full
        self._last_sample = now
        edge = self.watch.step(now, busy) if self.watch else None
        return edge, busy


def register_pool(port, name, threads, busy_fn=None, queue_fn=None, open_fn=None):
    p = Pool(port, name, threads, busy_fn, queue_fn, open_fn)
    _pools[p.port] = p
    return p


def attach_waitress(server, name="http"):
    """Waitress (`create_server` result): its dispatcher counts the threads actually servicing a
    task, and its queue holds the requests WAITING for one — the number nobody could see."""
    disp = getattr(server, "task_dispatcher", None)
    port = getattr(server, "effective_port", None) or getattr(getattr(server, "adj", None), "port", None)
    threads = getattr(getattr(server, "adj", None), "threads", None)
    smap = getattr(server, "_map", None) or getattr(server, "map", None)
    return register_pool(
        port, name, threads,
        busy_fn=(lambda: disp.active_count) if disp is not None else None,
        queue_fn=(lambda: len(disp.queue)) if disp is not None else None,
        # the socket map holds the listening socket(s) + one channel per open client connection
        open_fn=(lambda: max(0, len(smap) - 1)) if smap is not None else None)


def attach_cheroot(server, port, name):
    """cheroot `wsgi.Server` (HTTPS listener, HA listener)."""
    def _pool():
        return getattr(server, "requests", None)

    def _busy():
        rp = _pool()
        return len(getattr(rp, "_threads", ()) or ()) - int(getattr(rp, "idle", 0))

    def _queue():
        return int(getattr(_pool(), "qsize", 0))
    return register_pool(port, name, getattr(server, "numthreads", None) or
                         getattr(_pool(), "min", None), busy_fn=_busy, queue_fn=_queue)


# ─── Saturation watch (pure: testable without threads or clock) ─────────────────────────────────

class SaturationWatch:
    """Rising edge after `hold_s` at or above the threshold, falling edge after `hold_s` under it.
    Nothing in between: an alert line per episode, not per tick."""

    def __init__(self, total, ratio=None, hold_s=None):
        self.total = int(total)
        r = ALERT_RATIO if ratio is None else ratio
        self.high = max(1, min(self.total, int(math.ceil(self.total * r))))
        self.hold_s = ALERT_HOLD_S if hold_s is None else hold_s
        self.alarmed = False
        self._above_since = None
        self._below_since = None
        self.peak = 0

    def step(self, now, busy):
        if busy >= self.high:
            self._below_since = None
            if self.alarmed:
                self.peak = max(self.peak, busy)
                return None
            if self._above_since is None:
                self._above_since = now
                self.peak = busy
            self.peak = max(self.peak, busy)
            if now - self._above_since >= self.hold_s:
                self.alarmed = True
                self._above_since = None
                return "raise"
            return None
        self._above_since = None
        if not self.alarmed:
            return None
        if self._below_since is None:
            self._below_since = now
        if now - self._below_since >= self.hold_s:
            self.alarmed = False
            self._below_since = None
            return "clear"
        return None


# ─── Path masking ──────────────────────────────────────────────────────────────────────────────

# Segments that FOLLOW one of these words are capabilities (share links, public pages, ISO links,
# listen sessions): masked whatever their shape.
_SECRET_AFTER = {"p", "w", "share", "session", "iso", "token", "tokens", "jeton", "enroll", "join"}
# And whatever their position: a long token-shaped segment with letters AND digits. A rule on the
# SHAPE rather than a list of routes — a list does not see the route added next month.
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-.~=]{16,}$")


def mask_path(path):
    out = []
    prev = ""
    for seg in (path or "").split("/"):
        if seg and (prev.lower() in _SECRET_AFTER or (
                _TOKENISH.match(seg) and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg))):
            ext = ""
            m = re.search(r"\.(iso|img|m3u8?)$", seg, re.I)
            if m:
                ext = m.group(0)
            out.append("…" + ext)
        else:
            out.append(seg)
        prev = seg
    return "/".join(out)[:240]


_VMID_RE = re.compile(r"/containers/(\d+)(?:/|$)")


# ─── WSGI middleware ───────────────────────────────────────────────────────────────────────────

class _Tracked:
    """Response wrapper: counts bytes, unregisters on close() (= thread given back)."""
    __slots__ = ("_it", "_rid", "_e")

    def __init__(self, it, rid, e):
        self._it, self._rid, self._e = it, rid, e

    def __iter__(self):
        e = self._e
        for chunk in self._it:
            e["bytes"] += len(chunk)
            yield chunk

    def close(self):
        try:
            c = getattr(self._it, "close", None)
            if c is not None:
                c()
        finally:
            _entries.pop(self._rid, None)


class _TrackedSized(_Tracked):
    """Same, for a sized iterable: Waitress derives Content-Length from len() == 1."""
    __slots__ = ()

    def __len__(self):
        return len(self._it)


class Tracker:
    def __init__(self, wsgi_app):
        self.app = wsgi_app

    def __call__(self, environ, start_response):
        now = time.time()
        port = str(environ.get("SERVER_PORT") or "")
        path = environ.get("PATH_INFO") or "/"
        m = _VMID_RE.search(path)
        e = {"thread": threading.current_thread().name, "tid": threading.get_ident(),
             "start": now, "method": environ.get("REQUEST_METHOD") or "?", "path": path,
             "ip": environ.get("REMOTE_ADDR"), "port": port,
             "vmid": int(m.group(1)) if m else None,
             "uid": None, "user": None, "auth": None,
             "kind": "request", "status": None, "bytes": 0}
        rid = next(_ids)
        _entries[rid] = e
        environ[ENV_KEY] = e
        pool = _pools.get(port)
        if pool is not None:
            pool.note_busy(pool.busy(), now)

        def _sr(status, headers, exc_info=None):
            try:
                e["status"] = int(str(status)[:3])
                ct, cl = "", False
                for k, v in headers:
                    kl = k.lower()
                    if kl == "content-type":
                        ct = (v or "").lower()
                    elif kl == "content-length":
                        cl = True
                if "text/event-stream" in ct:
                    e["kind"] = "sse"
                elif "multipart/x-mixed-replace" in ct or (not cl and e["status"] in (200, 206)
                                                            and e["method"] != "HEAD"):
                    e["kind"] = "stream"
            except Exception:                                            # noqa: BLE001
                pass
            if exc_info is not None:
                return start_response(status, headers, exc_info)
            return start_response(status, headers)

        try:
            result = self.app(environ, _sr)
        except BaseException:
            _entries.pop(rid, None)
            raise
        fw = environ.get("wsgi.file_wrapper")
        if isinstance(fw, type) and isinstance(result, fw):
            # Waitress hands a file wrapper to its main loop: the worker thread is free as soon as
            # we return. Wrapping it would also cost the zero-copy path of every download.
            _entries.pop(rid, None)
            return result
        cls = _TrackedSized if hasattr(result, "__len__") else _Tracked
        return cls(result, rid, e)


def _before_request():
    """Session user id as a HINT for requests still in their view (no database access)."""
    try:
        from flask import request, session
        e = request.environ.get(ENV_KEY)
        if e is not None:
            uid = session.get("user_id")
            if uid:
                e["uid"] = uid
    except Exception:                                                    # noqa: BLE001
        pass


def _after_request(resp):
    """The account the request was actually served as (already resolved by `current_user`)."""
    try:
        from flask import g, request, session
        e = request.environ.get(ENV_KEY)
        u = g.get("_user") if e is not None else None
        if u:
            e["uid"] = u.get("id")
            e["user"] = u.get("username")
            e["auth"] = "session" if session.get("user_id") == u.get("id") else "token"
    except Exception:                                                    # noqa: BLE001
        pass
    return resp


def install(app):
    """Wrap `app.wsgi_app` (call it BEFORE the proxy-headers middleware is mounted, so that
    REMOTE_ADDR is the trusted one) and hook the user resolution."""
    app.wsgi_app = Tracker(app.wsgi_app)
    app.before_request_funcs.setdefault(None, []).insert(0, _before_request)
    app.after_request(_after_request)
    return app


# ─── Sampler + alert ───────────────────────────────────────────────────────────────────────────

def _long_streams_summary(port, limit=6):
    rows = [e for e in list(_entries.values()) if e.get("port") == port and e.get("kind") != "request"]
    rows.sort(key=lambda e: e["start"])
    return ", ".join("%s@%s %s" % (e.get("user") or "?", e.get("ip") or "?", mask_path(e.get("path")))
                     for e in rows[:limit])


def _emit(pool, edge, busy):
    from .database import db_add_alert
    params = {"srv": pool.name, "port": pool.port, "busy": busy, "total": pool.threads,
              "high": pool.watch.high, "hold": int(pool.watch.hold_s)}
    if edge == "raise":
        params["peak"] = pool.watch.peak
        log.warning("web threads: %s :%s %d/%d busy for %ds — long streams: %s", pool.name, pool.port,
                    busy, pool.threads, int(pool.watch.hold_s), _long_streams_summary(pool.port) or "none")
        db_add_alert("alert.resource.http_threads_saturation", "warning", kind="resource", params=params)
    else:
        log.info("web threads: %s :%s back to %d/%d", pool.name, pool.port, busy, pool.threads)
        db_add_alert("alert.resource.http_threads_saturation_retour", "info", kind="resource", params=params)


def sample_once(now=None, emit=None):
    """One tick over every pool; also drops entries whose thread died without close()."""
    now = time.time() if now is None else now
    alive = {t.ident for t in threading.enumerate()}
    for rid, e in list(_entries.items()):
        if e.get("tid") not in alive:
            _entries.pop(rid, None)
    for pool in list(_pools.values()):
        edge, busy = pool.sample(now)
        if edge:
            try:
                (emit or _emit)(pool, edge, busy)
            except Exception as ex:                                      # noqa: BLE001
                log.warning("web threads: alert not written: %s", ex)


_sampler = None


def start_sampler():
    global _sampler
    if _sampler is not None:
        return _sampler

    def _loop():
        while True:
            try:
                sample_once()
            except Exception as ex:                                      # noqa: BLE001
                log.warning("web threads: sampler tick failed: %s", ex)
            time.sleep(SAMPLE_S)
    _sampler = threading.Thread(target=_loop, daemon=True, name="http-conn-sampler")
    _sampler.start()
    return _sampler


# ─── Snapshot (administrators only — see routes/monitoring_api.py) ──────────────────────────────

def _names(uids, vmids):
    users, hosts = {}, {}
    try:
        from .database import get_db
        db = get_db()
        if uids:
            q = ",".join("?" * len(uids))
            for r in db.execute("SELECT id, username FROM users WHERE id IN (%s)" % q, list(uids)):
                users[r[0]] = r[1]
        if vmids:
            q = ",".join("?" * len(vmids))
            for r in db.execute("SELECT vmid, hostname FROM containers WHERE vmid IN (%s)" % q, list(vmids)):
                hosts[r[0]] = r[1]
    except Exception as ex:                                              # noqa: BLE001
        log.warning("web threads: name resolution failed: %s", ex)
    return users, hosts


def _relayed_vmids():
    try:
        from . import state_relay
        return {s["vmid"] for s in (state_relay.stats().get("subscriptions") or [])}
    except Exception:                                                    # noqa: BLE001
        return set()


# The ONLY fields that leave this module. A whitelist, so that a field added to the entry for the
# hot path can never reach the API by accident.
_PUBLIC = ("id", "thread", "start", "duration_s", "method", "path", "ip", "user", "auth", "kind",
           "relay", "vmid", "hostname", "status", "bytes", "port", "server", "detached", "buffered")
_HUB_PUBLIC = ("clients", "peak_clients", "total_clients", "bytes_sent", "buffered", "closes", "slow_cut",
               "departures", "departure_lag_max_ms", "loop_max_ms", "loop_recent_ms", "since",
               "running", "enabled", "limits")


def _hub():
    """Streams served by the stream hub (app/stream_hub.py): they hold NO server thread, so they
    are not in the registry above once handed over — listed here instead."""
    try:
        from . import stream_hub
        return stream_hub.stats() or {}
    except Exception:                                                    # noqa: BLE001
        return {}


def snapshot(now=None):
    now = time.time() if now is None else now
    rows = [(rid, dict(e)) for rid, e in list(_entries.items())]
    hub = _hub()
    for h in hub.get("rows") or []:
        rows.append((h["id"], dict(h, kind="sse", thread=None, tid=None, status=200, detached=True,
                                   relay_feed=str(h.get("feed") or "").startswith("state-relay"))))
    uids = {e["uid"] for _r, e in rows if e.get("uid") and not e.get("user")}
    vmids = {e["vmid"] for _r, e in rows if e.get("vmid")}
    users, hosts = _names(uids, vmids)
    relayed = _relayed_vmids() if any(e.get("kind") == "sse" for _r, e in rows) else set()
    out = []
    for rid, e in sorted(rows, key=lambda x: x[1]["start"]):
        pool = _pools.get(e.get("port"))
        user = e.get("user")
        auth = e.get("auth")
        if not user and e.get("uid"):
            user = users.get(e["uid"])
            auth = "session?" if user else None
        r = dict(e, id=rid, duration_s=round(now - e["start"], 3), path=mask_path(e.get("path")),
                 user=user, auth=auth, hostname=hosts.get(e.get("vmid")),
                 relay=bool(e.get("relay_feed") or (e.get("kind") == "sse" and e.get("vmid") in relayed
                                                    and "/plugin/" in (e.get("path") or ""))),
                 server=pool.name if pool else None, detached=bool(e.get("detached")))
        out.append({k: r.get(k) for k in _PUBLIC})
    pools = []
    for p in list(_pools.values()):
        mine = [r for r in out if r["port"] == p.port and not r["detached"]]
        pools.append({
            "name": p.name, "port": p.port, "threads": p.threads, "busy": p.busy(),
            "busy_registry": len(mine),
            "streams": sum(1 for r in mine if r["kind"] != "request"),
            "queue": p.queue(), "open_connections": p.open_connections(),
            "max_busy": p.max_busy, "max_busy_at": p.max_busy_at, "max_queue": p.max_queue,
            "all_busy_events": p.all_busy_events, "all_busy_s": round(p.all_busy_s, 1),
            "alert_threshold": p.watch.high if p.watch else None,
            "alarmed": bool(p.watch and p.watch.alarmed)})
    return {"now": now, "since": _started_at, "pools": pools, "connections": out,
            "hub": {k: hub.get(k) for k in _HUB_PUBLIC} if hub else None,
            "limits": {"alert_ratio": ALERT_RATIO, "alert_hold_s": ALERT_HOLD_S, "sample_s": SAMPLE_S}}
