# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""STREAM HUB: long server-sent-events streams served WITHOUT holding a web server thread.

WHY (measured 2026-10-29, Monitoring → Connexions, commit d554243). Waitress serves the product with
16 threads; each browser tab open on a mixer held ONE of them for the whole life of its
`/api/containers/<vm>/plugin/events` stream, even though the state relay (app/state_relay.py)
already had a single upstream subscription and a single buffer per container. 8 tabs = 8 threads;
22 streams = 16/16 for 39 s, 34 requests queued, the interface did not answer. A departed client
kept its thread 2 to 30 s more: the departure was only found when the next keepalive failed.

HOW. The request is served by the web server as usual up to the end of the Flask view: login,
permissions, project access and `read_endpoints` are checked there, BEFORE anything is handed over.
The view then leaves a hand-off in the WSGI environ (`ENV_PENDING`). `Detacher` (the outermost
middleware) sees it once Flask has built the response (after_request hooks included — security
headers, session cookie), DETACHES the client socket from the server (`ENV_DETACH`, provided by the
two adapters below), writes the response head itself and gives the socket to the hub. The server
thread returns at once. Nothing changes for the browser: same URL, same status and headers, same
bytes (the container's own `data:` blocks and `: ka` keepalives, chunked framing on HTTP/1.1 as
Waitress did), the first block is the last state.

The hub is ONE thread with a `selectors` loop over every client socket:
- a published block is framed once and queued on each client of the feed; "latest wins" — a block
  not yet started for a client is replaced by the newer one (each block is a FULL state), exactly
  what the thread-per-client generator did when it woke up late;
- writes are non-blocking; a client that does not drain is bounded twice: queued bytes above
  MAX_BUFFER, or unsent data that has not moved for STALL_S → disconnected and COUNTED (`slow_*`).
  Measured on the bench (2026-10-29): a client that never reads is usually cut by the KERNEL first
  — its small blocks fit in the socket buffers, the window closes, and TCP_USER_TIMEOUT (below)
  ends the connection after 20 s of unacknowledged zero-window probes: counted `slow_unacked`;
- each socket is also watched for READING: a departed client (FIN, RST, TLS close_notify) is
  detected when the kernel says so, not at the next keepalive. TCP_USER_TIMEOUT bounds a client
  that vanished without a FIN (cable, NAT) whose unacknowledged data would otherwise be retried for
  minutes;
- keepalive `: ka` after KEEPALIVE_S without a block, per client, as before.

SERVER ADAPTERS (`ENV_DETACH`):
- Waitress (`install_waitress`, the :5000 listener): our task class adds the callable. The socket is
  DUPLICATED (same TCP connection, new descriptor); the task writes nothing more and closes ITS
  descriptor at the end (close_on_finish) — a close without shutdown does not end a connection that
  another descriptor still holds. The Waitress main loop does not read a channel while it has a
  request in service (`readable()` is False), so nobody else touches the socket in between.
- cheroot (`install_cheroot`, the HTTPS listener of the `tls` mode): our gateway class adds the
  callable. The TLS session lives in the `SSLSocket` object, it cannot be duplicated: the object
  itself is taken; cheroot is told the headers are sent and the connection lingers (its close then
  neither shuts down nor closes the socket). Non-blocking TLS writes retry the same buffer on
  SSLWantWrite (CPython sets ACCEPT_MOVING_WRITE_BUFFER); a block already attempted is never
  replaced.
- any other server (the HA mTLS listener, a test client): no `ENV_DETACH`, and the view keeps its
  thread-holding generator — exactly the behaviour before the hub.
Behind the `proxy` mode the socket is the proxy's; the user/IP shown come from the request registry
(app/http_connections.py), which reads the TRUSTED address.

Kill switch: setting `stream_hub_enabled` = "0" → every stream goes back to a server thread
(A/B measurement, escape hatch). Read with a cache of CACHE_S.

Visibility: `stats()` (clients with user/IP/target/bytes/buffered, totals, disconnections by reason,
longest loop turn = upper bound of the departure detection delay) — merged into Monitoring →
Connexions by app/http_connections.py.

MEASURED 2026-10-29, local orchestrator, mixer bench #1278 read only (scratchpad hub_bench.py):
- before: 30 streams → 8 served in 3 s, the 22 others queued behind 16/16 threads, the API itself
  timed out (8 s); with 6 streams a departure was seen after 2.6 s (next keepalive write);
- after: 30 streams → 30 served, 1 thread busy (the measuring request), departure of 5 clients
  seen in 10 ms; overhead behind a direct client of the container p50 6.1 / p95 11.4 ms with 30
  clients (12 clients, A/B at the same moment: threads 3.9 / 7.0 ms, hub 4.3 / 6.9 ms); a client
  that never reads was cut by TCP_USER_TIMEOUT after ~20 s (`slow_unacked`), nobody else slowed."""
import collections
import email.utils
import errno
import logging
import selectors
import socket
import ssl
import threading
import time

log = logging.getLogger("bobistudio.stream_hub")

ENV_DETACH = "bobistudio.stream_hub.detach"     # set by a server adapter: callable() -> (sock, tls) | None
ENV_PENDING = "bobistudio.stream_hub.pending"   # set by a view: callable(sock, tls, head, chunked, meta)

MAX_BUFFER = 1 << 20          # bytes queued for one client (kernel send buffer not counted)
STALL_S = 10.0                # unsent data that did not move for that long = slow client
KEEPALIVE_S = 1.0             # default; a feed brings its own
CLOSE_GRACE_S = 2.0           # flushing the end of a stream before closing anyway
TCP_USER_TIMEOUT_MS = 20000   # unacknowledged data for that long → the kernel drops the connection
MAX_JUNK = 65536              # bytes a client may send on a stream before it is cut (protocol)
SEND_SLICE = 262144
CACHE_S = 5.0
CLOSE_REASONS = ("gone", "gone_error", "slow_buffer", "slow_stall", "slow_unacked", "ended", "protocol",
                 "server")
SLOW_REASONS = ("slow_buffer", "slow_stall", "slow_unacked")
_HOP = {"content-length", "transfer-encoding", "connection", "keep-alive"}


# ─── setting ─────────────────────────────────────────────────────────────────────────────────
_en = (0.0, True)


def enabled():
    global _en
    t, v = _en
    if time.monotonic() - t > CACHE_S:
        try:
            from . import settings
            v = str(settings.get("stream_hub_enabled", "1") or "1").strip().lower() \
                not in ("0", "false", "off", "no")
        except Exception:                                             # noqa: BLE001
            v = True
        _en = (time.monotonic(), v)
    return v


def detachable(environ):
    """True if this request's socket can be handed to the hub (server adapter present, switch on)."""
    return callable(environ.get(ENV_DETACH)) and enabled()


# ─── feed ────────────────────────────────────────────────────────────────────────────────────
class Feed:
    """A source of blocks fanned out to hub clients. `publish` and `end` are thread-safe (they post
    to the hub loop); `on_leave(client)` is called from the hub thread."""

    def __init__(self, hub, label="", keepalive_s=None, keepalive=b": ka\n\n", on_leave=None):
        self.hub, self.label = hub, label
        self.keepalive_s = KEEPALIVE_S if keepalive_s is None else keepalive_s
        self.keepalive = keepalive
        self.on_leave = on_leave
        self.clients = set()          # hub thread only
        self.ended = False
        self.published = 0

    def publish(self, block, rev):
        self.published += 1
        self.hub._post(("pub", self, block, rev))

    def end(self):
        if not self.ended:
            self.ended = True
            self.hub._post(("end", self))


# ─── client ──────────────────────────────────────────────────────────────────────────────────
class Client:
    __slots__ = ("id", "sock", "tls", "chunked", "feed", "rev", "out", "off", "attempted", "queued",
                 "last_enq", "last_progress", "closing", "close_deadline", "closed", "sent", "junk",
                 "meta", "start", "events", "want_write", "fd")

    def __init__(self, cid, sock, tls, chunked, feed, rev, meta):
        self.id, self.sock, self.tls, self.chunked, self.feed, self.rev = cid, sock, tls, chunked, feed, rev
        self.out = collections.deque()   # [bytes, kind] — kind "data" | "ka" | "head" | "end"
        self.off = 0                     # bytes of out[0] already sent
        self.attempted = False           # a send of out[0] was attempted (TLS: never replace it)
        self.queued = 0
        now = time.monotonic()
        self.last_enq = self.last_progress = now
        self.closing = False
        self.close_deadline = None
        self.closed = None
        self.sent = 0
        self.junk = 0
        self.meta = meta or {}
        self.start = time.time()
        self.events = 0
        self.want_write = False
        self.fd = sock.fileno()


def _error_reason(e):
    """ETIMEDOUT = TCP_USER_TIMEOUT: data (or zero-window probes) unacknowledged for that long — a
    client that stopped reading or vanished without a FIN. Counted with the slow clients."""
    return "slow_unacked" if getattr(e, "errno", None) == errno.ETIMEDOUT else "gone_error"


def frame(block, chunked):
    if not chunked or not block:
        return block
    return b"%X\r\n%s\r\n" % (len(block), block)


# ─── the hub ─────────────────────────────────────────────────────────────────────────────────
class Hub:
    def __init__(self, name="stream-hub"):
        self.name = name
        self.sel = None
        self.thread = None
        self._ops = collections.deque()
        self._lock = threading.Lock()          # clients dict + start
        self._wake_r = self._wake_w = None
        self.clients = {}                      # id -> Client
        self._ids = 0
        self.started_at = time.time()
        self.total_clients = 0
        self.bytes_sent = 0
        self.closes = {r: 0 for r in CLOSE_REASONS}
        self.peak_clients = 0
        self.loop_max_ms = 0.0                 # longest loop turn since start
        self.loop_recent_ms = 0.0              # longest loop turn over the last ~60 s
        self._recent = (time.monotonic(), 0.0)
        self.departures = 0
        self.departure_lag_max_ms = 0.0        # select() return → socket closed, for a departure

    # -- life ----------------------------------------------------------------------------------
    def start(self):
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.sel = selectors.DefaultSelector()
            self._wake_r, self._wake_w = socket.socketpair()
            self._wake_r.setblocking(False)
            self._wake_w.setblocking(False)
            self.sel.register(self._wake_r, selectors.EVENT_READ, None)
            self.thread = threading.Thread(target=self._loop, daemon=True, name=self.name)
            self.thread.start()

    def _post(self, op):
        # One byte per post, always. A "already woken" flag skipping the send LOST wake-ups: the
        # loop cleared it, then drained a byte posted in between — the next post saw the flag set,
        # sent nothing, and its op waited for a timeout (forever with no client: measured on the
        # bench 2026-10-29, hand-offs stuck after the last client left). A full pipe (BlockingIOError)
        # means a wake-up is already pending.
        self._ops.append(op)
        try:
            self._wake_w.send(b"\0")
        except (BlockingIOError, AttributeError, OSError):
            pass

    def feed(self, label="", keepalive_s=None, on_leave=None):
        self.start()
        return Feed(self, label, keepalive_s, on_leave=on_leave)

    def attach(self, sock, tls, head, first, rev, feed, chunked, meta=None):
        """Hand a detached client socket to the hub: `head` (response head, bytes) then `first`
        (the first block, may be None), then whatever `feed` publishes with a rev > `rev`. A None
        or ended feed: the head and first block are written and the stream ends cleanly."""
        self.start()
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if hasattr(socket, "TCP_USER_TIMEOUT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, TCP_USER_TIMEOUT_MS)
        except OSError:
            pass
        with self._lock:
            self._ids += 1
            cid = self._ids
        c = Client(cid, sock, tls, chunked, feed, rev, meta)
        c.out.append([head, "head"])
        c.queued += len(head)
        if first:
            fb = frame(first, chunked)
            c.out.append([fb, "data"])
            c.queued += len(fb)
            c.events += 1
        self._post(("add", c))
        return c

    def stop(self):
        self._post(("stop",))

    # -- loop ----------------------------------------------------------------------------------
    def _loop(self):
        while True:
            try:
                events = self.sel.select(self._timeout())
            except Exception as e:                                    # noqa: BLE001
                log.warning("stream hub: select failed: %s", e)
                time.sleep(0.05)
                continue
            t0 = time.monotonic()
            try:
                for key, mask in events:
                    c = key.data
                    if c is None:
                        self._drain_wake()
                        continue
                    if c.closed is not None:
                        continue
                    if mask & selectors.EVENT_READ:
                        self._on_readable(c, t0)
                    if c.closed is None and (mask & selectors.EVENT_WRITE or (c.tls and c.out)):
                        self._flush(c)
                if self._run_ops() == "stop":
                    self._close_all("server")
                    return
                self._tick(time.monotonic())
            except Exception as e:                                    # noqa: BLE001
                log.exception("stream hub: loop turn failed: %s", e)
            dt = (time.monotonic() - t0) * 1000.0
            if dt > self.loop_max_ms:
                self.loop_max_ms = dt
            rt, rv = self._recent
            if t0 - rt > 60.0:
                self._recent = (t0, dt)
            elif dt > rv:
                self._recent = (rt, dt)
            self.loop_recent_ms = self._recent[1]

    def _timeout(self):
        if not self.clients:
            return None if not self._ops else 0
        if self._ops:
            return 0
        now = time.monotonic()
        nxt = 0.5
        for c in self.clients.values():
            if c.feed is not None and not c.out and not c.closing:
                nxt = min(nxt, max(0.0, c.last_enq + c.feed.keepalive_s - now))
            if c.closing and c.close_deadline is not None:
                nxt = min(nxt, max(0.0, c.close_deadline - now))
        return max(0.002, nxt)

    def _drain_wake(self):
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _run_ops(self):
        while self._ops:
            op = self._ops.popleft()
            kind = op[0]
            if kind == "add":
                self._add(op[1])
            elif kind == "pub":
                _k, feed, block, rev = op
                if not feed.clients:
                    continue
                cache = {}
                for c in list(feed.clients):
                    if c.closed is not None or c.closing or rev <= c.rev:
                        continue
                    fb = cache.get(c.chunked)
                    if fb is None:
                        fb = cache[c.chunked] = frame(block, c.chunked)
                    c.rev = rev
                    c.events += 1
                    self._enqueue(c, fb, "data", coalesce=True)
            elif kind == "end":
                for c in list(op[1].clients):
                    self._finish(c)
            elif kind == "stop":
                return "stop"
        return None

    def _add(self, c):
        try:
            self.sel.register(c.sock, selectors.EVENT_READ, c)
        except (ValueError, KeyError, OSError) as e:
            log.info("stream hub: client socket unusable: %s", e)
            c.closed = time.time()
            self._release(c)
            if c.feed is not None and c.feed.on_leave:
                self._safe_leave(c)
            return
        with self._lock:
            self.clients[c.id] = c
            self.total_clients += 1
            self.peak_clients = max(self.peak_clients, len(self.clients))
        if c.feed is not None:
            c.feed.clients.add(c)
        self._flush(c)
        if c.closed is None and (c.feed is None or c.feed.ended):
            self._finish(c)

    def _enqueue(self, c, data, kind, coalesce=False):
        if coalesce:
            # latest wins: a block not yet started (or a pending keepalive) is superseded
            keep = collections.deque()
            for i, item in enumerate(c.out):
                started = i == 0 and (c.off > 0 or c.attempted)
                if started or item[1] in ("head", "end"):
                    keep.append(item)
                else:
                    c.queued -= len(item[0])
            c.out = keep
        c.out.append([data, kind])
        c.queued += len(data)
        c.last_enq = time.monotonic()
        if c.queued > MAX_BUFFER:
            self._close(c, "slow_buffer")
            return
        self._flush(c)

    def _finish(self, c):
        """End the stream cleanly: chunked terminator, close once flushed (or after a grace)."""
        if c.closing or c.closed is not None:
            return
        c.closing = True
        c.close_deadline = time.monotonic() + CLOSE_GRACE_S
        if c.chunked:
            c.out.append([b"0\r\n\r\n", "end"])
            c.queued += 5
        self._flush(c)

    def _flush(self, c):
        progressed = False
        while c.out:
            buf = c.out[0][0]
            try:
                c.attempted = True
                n = c.sock.send(memoryview(buf)[c.off:c.off + SEND_SLICE])
            except (BlockingIOError, InterruptedError, ssl.SSLWantWriteError, ssl.SSLWantReadError):
                break
            except OSError as e:
                self._close(c, _error_reason(e))
                return
            if n <= 0:
                break
            progressed = True
            c.off += n
            c.sent += n
            c.queued -= n
            self.bytes_sent += n
            if c.off >= len(buf):
                c.out.popleft()
                c.off = 0
                c.attempted = False
        now = time.monotonic()
        if progressed or not c.out:
            c.last_progress = now
        if c.out and not c.want_write:
            self._mod(c, selectors.EVENT_READ | selectors.EVENT_WRITE)
            c.want_write = True
        elif not c.out and c.want_write:
            self._mod(c, selectors.EVENT_READ)
            c.want_write = False
        if not c.out and c.closing:
            self._close(c, "ended")

    def _mod(self, c, events):
        try:
            self.sel.modify(c.sock, events, c)
        except (ValueError, KeyError, OSError):
            pass

    def _on_readable(self, c, t_sel):
        try:
            data = c.sock.recv(16384)
        except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return
        except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
            data = b""
        except OSError as e:
            self._depart(c, _error_reason(e), t_sel)
            return
        if not data:
            self._depart(c, "gone", t_sel)
            return
        c.junk += len(data)                     # a stream expects nothing from its client
        if c.junk > MAX_JUNK:
            self._close(c, "protocol")

    def _depart(self, c, reason, t_sel):
        self._close(c, reason)
        lag = (time.monotonic() - t_sel) * 1000.0
        self.departures += 1
        if lag > self.departure_lag_max_ms:
            self.departure_lag_max_ms = lag

    def _tick(self, now):
        for c in list(self.clients.values()):
            if c.closed is not None:
                continue
            if c.closing:
                if c.close_deadline is not None and now >= c.close_deadline:
                    self._close(c, "ended")
                continue
            if c.out:
                if now - c.last_progress > STALL_S:
                    self._close(c, "slow_stall")
                continue
            f = c.feed
            if f is not None and now - c.last_enq >= f.keepalive_s:
                self._enqueue(c, frame(f.keepalive, c.chunked), "ka")

    def _close(self, c, reason):
        if c.closed is not None:
            return
        c.closed = time.time()
        self.closes[reason] = self.closes.get(reason, 0) + 1
        if reason.startswith("slow"):
            log.info("stream hub: slow client cut (%s) — %s@%s %s, %d bytes queued", reason,
                     c.meta.get("user") or "?", c.meta.get("ip") or "?", c.meta.get("path") or "", c.queued)
        try:
            self.sel.unregister(c.sock)
        except (ValueError, KeyError, OSError):
            pass
        self._release(c)
        with self._lock:
            self.clients.pop(c.id, None)
        if c.feed is not None:
            c.feed.clients.discard(c)
            if c.feed.on_leave:
                self._safe_leave(c)

    def _release(self, c):
        try:
            c.sock.shutdown(socket.SHUT_RDWR)   # FIN now, whatever other descriptor still exists
        except OSError:
            pass
        try:
            c.sock.close()
        except OSError:
            pass

    def _safe_leave(self, c):
        try:
            c.feed.on_leave(c)
        except Exception as e:                                        # noqa: BLE001
            log.warning("stream hub: on_leave failed: %s", e)

    def _close_all(self, reason):
        for c in list(self.clients.values()):
            self._close(c, reason)

    # -- visibility ----------------------------------------------------------------------------
    def stats(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            cs = list(self.clients.values())
        rows = []
        for c in sorted(cs, key=lambda x: x.start):
            m = c.meta
            rows.append({"id": "hub-%d" % c.id, "start": c.start, "duration_s": round(now - c.start, 3),
                         "method": m.get("method") or "GET", "path": m.get("path"), "ip": m.get("ip"),
                         "uid": m.get("uid"), "user": m.get("user"), "auth": m.get("auth"),
                         "vmid": m.get("vmid"), "port": m.get("port"), "tls": c.tls,
                         "bytes": c.sent, "buffered": c.queued, "events": c.events,
                         "feed": c.feed.label if c.feed is not None else None})
        return {"running": self.thread is not None and self.thread.is_alive(),
                "enabled": enabled(), "clients": len(rows), "peak_clients": self.peak_clients,
                "total_clients": self.total_clients, "bytes_sent": self.bytes_sent,
                "buffered": sum(r["buffered"] for r in rows),
                "closes": dict(self.closes),
                "slow_cut": sum(self.closes.get(r, 0) for r in SLOW_REASONS),
                "departures": self.departures,
                "departure_lag_max_ms": round(self.departure_lag_max_ms, 2),
                "loop_max_ms": round(self.loop_max_ms, 2), "loop_recent_ms": round(self.loop_recent_ms, 2),
                "since": self.started_at, "rows": rows,
                "limits": {"max_buffer": MAX_BUFFER, "stall_s": STALL_S,
                           "tcp_user_timeout_ms": TCP_USER_TIMEOUT_MS}}


HUB = Hub()


def hub():
    return HUB


def stats():
    try:
        return HUB.stats()
    except Exception as e:                                            # noqa: BLE001
        log.warning("stream hub: stats failed: %s", e)
        return {}


# ─── response head + middleware ──────────────────────────────────────────────────────────────
def build_head(status, headers, environ):
    """HTTP response head for a detached stream → (bytes, chunked). Same status and headers as
    Flask produced; hop-by-hop headers replaced: chunked framing on HTTP/1.1 (what Waitress did
    for a body without length), connection closed at the end of the stream either way."""
    proto = environ.get("SERVER_PROTOCOL") or "HTTP/1.0"
    chunked = proto.upper() == "HTTP/1.1"
    lines = ["%s %s" % ("HTTP/1.1" if chunked else "HTTP/1.0", status)]
    names = set()
    for k, v in headers:
        kl = k.lower()
        if kl in _HOP:
            continue
        names.add(kl)
        lines.append("%s: %s" % (k, v))
    if "date" not in names:
        lines.append("Date: %s" % email.utils.formatdate(usegmt=True))
    lines.append("Connection: close")
    if chunked:
        lines.append("Transfer-Encoding: chunked")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"), chunked


class Detacher:
    """Outermost WSGI middleware. A view that left `ENV_PENDING` asks for its client to be handed to
    the hub: the response Flask built (status, headers — after_request hooks included) is written
    by the hub, and the server thread returns immediately. Any failure → the response is returned
    as is and served by the thread, as before."""

    def __init__(self, wsgi_app):
        self.app = wsgi_app

    def __call__(self, environ, start_response):
        cap = {}

        def sr(status, headers, exc_info=None):
            cap["status"], cap["headers"] = status, list(headers)
            if exc_info is not None:
                return start_response(status, headers, exc_info)
            return start_response(status, headers)

        result = self.app(environ, sr)
        pending = environ.pop(ENV_PENDING, None)
        if pending is None:
            return result
        detach = environ.get(ENV_DETACH)
        if not callable(detach) or not str(cap.get("status", "")).startswith("200"):
            return result
        try:
            got = detach()
        except Exception as e:                                        # noqa: BLE001
            log.warning("stream hub: detach failed, the stream keeps its thread: %s", e)
            got = None
        if not got:
            return result
        sock, tls = got
        try:
            c = getattr(result, "close", None)
            if c is not None:
                c()                     # never started: frees the request registry entry
        except Exception as e:                                        # noqa: BLE001
            log.debug("stream hub: closing the replaced response: %s", e)
        head, chunked = build_head(cap["status"], cap["headers"], environ)
        meta = _meta(environ)
        try:
            pending(sock, tls, head, chunked, meta)
        except Exception as e:                                        # noqa: BLE001
            log.warning("stream hub: hand-off failed: %s", e)
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        return []


def _meta(environ):
    """Who and what, from the request registry entry (trusted address, resolved account)."""
    m = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO"),
         "ip": environ.get("REMOTE_ADDR"), "port": str(environ.get("SERVER_PORT") or "")}
    try:
        from .http_connections import ENV_KEY
        e = environ.get(ENV_KEY)
        if e:
            for k in ("ip", "uid", "user", "auth", "vmid", "port", "path"):
                if e.get(k) is not None:
                    m[k] = e[k]
    except Exception:                                                 # noqa: BLE001
        pass
    return m


# ─── server adapters ─────────────────────────────────────────────────────────────────────────
def _waitress_task_class():
    from waitress.task import WSGITask

    class DetachableTask(WSGITask):
        _detached = False

        def get_environment(self):
            env = super().get_environment()
            env[ENV_DETACH] = self._detach
            return env

        def _detach(self):
            ch = self.channel
            if self.wrote_header or self._detached or len(ch.requests) != 1 or ch.total_outbufs_len:
                return None
            try:
                s = ch.socket.dup()
            except OSError:
                return None
            self._detached = True
            self.close_on_finish = True        # Waitress closes ITS descriptor; ours keeps the connection
            return s, False

        def write(self, data):
            if self._detached:
                return
            super().write(data)

        def finish(self):
            if self._detached:
                return
            super().finish()

    return DetachableTask


def install_waitress(server):
    """Give a Waitress server (`create_server` result) the detach capability."""
    from waitress.channel import HTTPChannel
    task_cls = _waitress_task_class()
    chan_cls = type("HubChannel", (HTTPChannel,), {"task_class": task_cls})
    servers = [server]
    m = getattr(server, "map", None)
    if m is not None and not hasattr(server, "channel_class"):     # MultiSocketServer
        servers = [s for s in m.values() if hasattr(s, "channel_class")]
    for s in servers:
        s.channel_class = chan_cls
    return server


def _cheroot_gateway_class():
    from cheroot.wsgi import Gateway_10

    class DetachableGateway(Gateway_10):
        def get_environ(self):
            env = super().get_environ()
            env[ENV_DETACH] = self._detach
            return env

        def _detach(self):
            req = self.req
            if req.sent_headers:
                return None
            conn = req.conn
            sock = conn.socket
            req.sent_headers = True            # cheroot writes nothing more on this request
            req.chunked_write = False
            req.close_connection = True
            conn.linger = True                 # its close() then neither shuts down nor closes the socket
            return sock, isinstance(sock, ssl.SSLSocket)

    return DetachableGateway


def install_cheroot(server):
    """Give a cheroot `wsgi.Server` the detach capability (HTTPS listener)."""
    server.gateway = _cheroot_gateway_class()
    return server
