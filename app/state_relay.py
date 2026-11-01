# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""STATE RELAY: one upstream subscription per container, fanned out to every reader.

Why (measured 2026-10-29 on the mixer): each client woken by a state change made the container
recompute `/state` in Python while the video thread was composing — up to +14 ms off-CPU (GIL
contention) on the frame of a cut or an AUTO with 8 subscribers and a poller, and SDI repeats.
The relay removes the cause: ONE reader per container, whatever the number of clients.

What is relayed: a stream declared in the manifest as
    "control": {"streams": [{"path": "/events", "method": "GET", "perm": "read",
                             "content_type": "text/event-stream"}]}
i.e. a server-sent-events stream whose every `data:` line is the FULL state document (it replaces
the previous one — latest wins). The mixer (>= 0.23.28) is the only publisher today. Optional keys
of that entry (all have defaults, none is required):
    "relay": false              opt out (the proxy then relays the stream byte for byte, as before)
    "mirrors": "/state"         the read endpoint the pushed document mirrors
    "volatile": [...]           top-level fields the stream does NOT keep fresh (measured fields)
    "motion_keys": [...]        a non-empty value of one of these = an operation in progress
    "motion_volatile": [...]    fields that move during an operation without a push
    "motion_pushed_since": "x.y.z"  from this plugin version (the document's `plugin_version`),
                                the stream pushes every tally change and keeps pushing during an
                                operation: no motion-volatile field, no settle read (see below)

Three kinds of readers:
- BROWSERS: the SAME URL `/api/containers/<vm>/plugin/events` is served by the relay (fan-out,
  same rights as the proxy). Bytes are STRICTLY the container's (`data: …\\n\\n` blocks and
  `: ka\\n\\n` keepalives); the first block sent on connection is the last known state. If the
  upstream is lost, every browser stream is CLOSED (as the plain proxy did): the page falls back
  to its own polling and reconnects — a lost source is never served as a live one.
  Where the web server allows it (Waitress, cheroot HTTPS), these streams are written by the
  stream hub (app/stream_hub.py) after the view: no server thread is held while a tab is open.
- INTERNAL consumers (tally, ATEM panel, macros/wall states): `read()` returns the last document
  when the relay is fresh, or None — the caller then takes its historical path, and that
  fallback is COUNTED by reason (visible in `stats()`, /api/state_relay, Monitoring page).
- Nobody else: a container whose plugin publishes no state stream is not a fallback, it is the
  normal path, and costs nothing here.

Freshness rules (a stale source is SIGNALLED, never served as fresh):
- connected and a byte (event or keepalive) received less than STALE_S ago, otherwise "stale";
- a field listed as `volatile` (`inputs_timing`: measured every frame, never pushed) is not
  served — the reader keeps its direct path;
- during an operation (`transition` / `manual_mix` non-null, a keyer AUTO running) the fields in
  `motion_volatile` (`tally`, `transition` — elapsed time, inputs at picture) move every frame
  without a push: not served either;
- the mixer computes its `tally` list on the video loop: the document pushed right after a cut
  can carry the previous frame's list, and nothing pushes again (the push signature does not
  include it). The relay therefore re-reads the mirrored endpoint ONCE, SETTLE_S after the last
  event, when an internal reader exists (a "settle read"; browsers never receive it — their
  stream stays the container's own bytes).
- both work-arounds above apply only to documents OLDER than the stream's `motion_pushed_since`
  (2026-10-29: mixer 0.23.48 puts its tally list in the push signature and pushes during an
  operation, at most 10 times a second, minimum 1/10 s + its 40 ms poll late). A document from
  that version on is served whole during an operation and is never re-read; an older one — or one
  without a readable `plugin_version` — keeps the work-arounds.

Lifecycle: a subscription starts on demand (first browser or first internal read), reconnects
with backoff, follows restarts and redeploys (the upstream drops, the IP is re-resolved), and
STOPS after IDLE_STOP_S without any browser and without any internal read. Containers followed
by the tally are not special-cased: the tally reads every 0.3 s, which keeps them subscribed by
itself; a mixer whose tally emission is switched off stops being read and its subscription ends —
no reader, no upstream connection.

HA: only the ACTIVE controller holds subscriptions (`ha.is_active()`). On a standby, `read()`
returns None (counted "standby") and the browser stream goes through the plain proxy.

Everything that touches the world goes through `World` — tests substitute a fake one."""
import json
import logging
import threading
import time

log = logging.getLogger("bobistudio.state_relay")

CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 3.0          # the mixer sends `: ka` every second: 3 s of silence = lost
STALE_S = 3.0
IDLE_STOP_S = 90.0           # > the period of a reader that passes every ~60 s (seen on #1250): it
                             # made the relay subscribe and unsubscribe in a loop at 30 s
SETTLE_S = 0.25               # > the mixer's /state cache (STATE_DOC_TTL_S = 0.1) + a few frames
SETTLE_NEEDED_S = 10.0        # settle reads only while an internal reader read this recently
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
UNSUPPORTED_RETRY_S = 60.0    # upstream answered 404 (engine too old): try again after a redeploy
CLIENT_KEEPALIVE_S = 1.0
FIRST_EVENT_WAIT_S = 3.0
READ_CHUNK = 65536
CTX_CACHE_S = 5.0

DEFAULT_MIRRORS = "/state"
DEFAULT_VOLATILE = ("inputs_timing",)
DEFAULT_MOTION_KEYS = ("transition", "manual_mix", "keyers[].auto")
DEFAULT_MOTION_VOLATILE = ("tally", "transition")
ALL_FIELDS = "*"

FALLBACK_REASONS = ("standby", "disabled", "starting", "unsupported", "disconnected", "stale", "unparsable",
                    "volatile_field", "motion")


# ─── declaration ─────────────────────────────────────────────────────────────────────────────
def stream_spec(m):
    """The relayable state stream of manifest `m` (normalised dict), or None."""
    ctrl = (m or {}).get("control") or {}
    for e in ctrl.get("streams") or []:
        if not isinstance(e, dict) or e.get("method") != "GET" or e.get("relay") is False:
            continue
        ct = str(e.get("content_type") or "").split(";", 1)[0].strip().lower()
        if ct != "text/event-stream" or not isinstance(e.get("path"), str):
            continue
        return {"path": e["path"],
                "mirrors": e.get("mirrors") or DEFAULT_MIRRORS,
                "volatile": tuple(e.get("volatile", DEFAULT_VOLATILE) or ()),
                "motion_keys": tuple(e.get("motion_keys", DEFAULT_MOTION_KEYS) or ()),
                "motion_volatile": tuple(e.get("motion_volatile", DEFAULT_MOTION_VOLATILE) or ()),
                "motion_pushed_since": _version(e.get("motion_pushed_since")),
                "port": int(ctrl.get("port") or 8082),
                "entry": e}
    return None


def _version(v):
    """'0.23.48' → (0, 23, 48); None if absent or unreadable."""
    try:
        t = tuple(int(x) for x in str(v).strip().split("."))
        return t if v is not None else None
    except (TypeError, ValueError):
        return None


def pushes_motion(spec, doc):
    """True if `doc` comes from a publisher that pushes during an operation (`motion_pushed_since`):
    its motion-volatile fields are fresh, and it needs no settle read."""
    since = (spec or {}).get("motion_pushed_since")
    if since is None or not isinstance(doc, dict):
        return False
    v = _version(doc.get("plugin_version"))
    return v is not None and v >= since


def in_motion(doc, keys):
    """True if `doc` announces an operation in progress (one of `keys` non-empty; `a[].b` = any
    item of list `a` with a truthy `b`)."""
    if not isinstance(doc, dict):
        return False
    for k in keys:
        if "[]." in k:
            lst, sub = k.split("[].", 1)
            items = doc.get(lst)
            if isinstance(items, list) and any(isinstance(it, dict) and it.get(sub) for it in items):
                return True
        elif doc.get(k):
            return True
    return False


# ─── world ───────────────────────────────────────────────────────────────────────────────────
class World:
    """Access to the world (database, HA role, network). Tests substitute a fake."""

    def __init__(self):
        self._ctx = {}             # vmid -> (monotonic, manifest, hostname)
        self._act = (0.0, True)

    def is_active(self):
        """Active controller? ★ Always False in a process launched from `tests/`: the real world
        would open real subscriptions to real containers from a test that only meant to exercise
        the tally or the macros with fake rows (a test vmid can be a live container's). Tests
        of the relay itself substitute their own World."""
        t, v = self._act
        if time.monotonic() - t > CTX_CACHE_S:
            from .database import running_under_tests
            if running_under_tests():
                v = False
            else:
                try:
                    from . import ha
                    v = ha.is_active()
                except Exception:                                     # noqa: BLE001
                    v = True                                          # ha.role() defaults to active
            self._act = (time.monotonic(), v)
        return v

    def enabled(self):
        """Kill switch: setting `state_relay_enabled` = "0" sends every reader back to the container
        (plain proxy for browsers, direct reads for internal consumers — counted "disabled").
        Meant for A/B measurement and as an escape hatch; cached CTX_CACHE_S."""
        t, v = getattr(self, "_en", (0.0, True))
        if time.monotonic() - t > CTX_CACHE_S:
            try:
                from . import settings
                v = str(settings.get("state_relay_enabled", "1") or "1").strip().lower() \
                    not in ("0", "false", "off", "no")
            except Exception:                                         # noqa: BLE001
                v = True
            self._en = (time.monotonic(), v)
        return v

    def context(self, vmid):
        """(manifest or None, hostname or None) of a container — cached CTX_CACHE_S."""
        hit = self._ctx.get(vmid)
        if hit and time.monotonic() - hit[0] < CTX_CACHE_S:
            return hit[1], hit[2]
        m, host = None, None
        try:
            from . import plugins
            from .database import db_get_container
            c = db_get_container(vmid)
            if c:
                dc = c.get("deploy_config")
                dc = json.loads(dc) if isinstance(dc, str) and dc else (dc or {})
                m, host = plugins.get(dc.get("type")), c.get("hostname")
        except Exception as e:                                        # noqa: BLE001
            log.debug("state relay: context of #%s unreadable: %s", vmid, e)
        self._ctx[vmid] = (time.monotonic(), m, host)
        return m, host

    def ip(self, vmid):
        from .addressing import get_container_ip
        return get_container_ip(vmid)

    def open_stream(self, url):
        import requests
        return requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                            headers={"Accept": "text/event-stream"})

    def get_text(self, url):
        import requests
        r = requests.get(url, timeout=1.0)
        if r.status_code != 200:
            raise RuntimeError("HTTP %s" % r.status_code)
        return r.text


def _read_some(up):
    raw = getattr(up, "raw", None)
    rd = getattr(raw, "read1", None) or getattr(raw, "read", None)
    return rd(READ_CHUNK)


# ─── one subscription ────────────────────────────────────────────────────────────────────────
class Subscription:
    def __init__(self, relay, vmid, spec):
        self.relay, self.vmid, self.spec = relay, vmid, spec
        self.cv = threading.Condition()
        self.raw = None             # last data block, bytes, exactly as the container sent it
        self.doc_text = None        # JSON text of the last document (event, or settle read)
        self.rev = 0                # data events received (all connections)
        self.gen = 0                # upstream connection generation (browsers close on change)
        self.event_wall = None      # time.time() of the last data event
        self.doc_wall = None        # time.time() the document in doc_text was produced
        self.rx = None              # monotonic of the last byte received
        self.settled_rev = None
        self.connected = False
        self.unsupported = False
        self.connects = 0
        self.reconnects = 0
        self.errors = 0
        self.last_error = None
        self.clients = 0
        self.need = time.monotonic()
        self.internal_need = 0.0
        self.started = time.time()
        self.settle_reads = 0
        self.served = {}            # consumer -> internal reads served
        self.fallbacks = {}         # (consumer, reason) -> n
        self.stop_evt = threading.Event()
        self._settle_evt = threading.Event()
        self._settle_due = None
        self._up = None
        self.feed = None            # stream_hub.Feed of the current upstream connection (gen)
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="state-relay-%s" % vmid)
        self.settler = threading.Thread(target=self._settle_loop, daemon=True,
                                        name="state-relay-settle-%s" % vmid)

    # -- state ---------------------------------------------------------------------------------
    def fresh(self):
        return self.connected and self.rx is not None and \
            time.monotonic() - self.rx < STALE_S and self.raw is not None

    def idle(self):
        return self.clients == 0 and time.monotonic() - self.need > IDLE_STOP_S

    def stop(self):
        self.stop_evt.set()
        self._settle_evt.set()
        self._end_feed()
        up = self._up
        if up is not None:
            try:
                up.close()
            except Exception:                                         # noqa: BLE001
                pass
        with self.cv:
            self.cv.notify_all()

    def _end_feed(self):
        """Browsers served by the stream hub follow ONE upstream connection: it ends → their streams
        end cleanly (the page reconnects), as the thread-per-browser generator did."""
        with self.cv:
            f, self.feed = self.feed, None
        if f is not None:
            f.end()

    def _hub_leave(self, _client):
        with self.cv:
            self.clients -= 1
            self.need = time.monotonic()

    # -- upstream ------------------------------------------------------------------------------
    def _run(self):
        w = self.relay.world
        attempt = 0
        reason = "stopped"
        log.info("state relay: subscribing to #%s %s", self.vmid, self.spec["path"])
        try:
            while not self.stop_evt.is_set():
                if not w.is_active():
                    reason = "standby"
                    break
                if not w.enabled():
                    reason = "disabled"
                    break
                if self.idle():
                    reason = "idle"
                    break
                m, _host = w.context(self.vmid)
                spec = stream_spec(m)
                if spec is None:
                    reason = "no longer published"
                    break
                self.spec = spec
                ip = w.ip(self.vmid)
                if not ip:
                    self._fail("no IP")
                    attempt = self._backoff(attempt)
                    continue
                url = "http://%s:%d%s" % (ip, spec["port"], spec["path"])
                try:
                    up = w.open_stream(url)
                except Exception as e:                                # noqa: BLE001
                    self._fail(str(e))
                    attempt = self._backoff(attempt)
                    continue
                if up.status_code != 200:
                    try:
                        up.close()
                    except Exception:                                 # noqa: BLE001
                        pass
                    if up.status_code == 404:
                        if not self.unsupported:
                            log.warning("state relay: #%s answers 404 on %s (engine without a state "
                                        "stream) — retrying in %.0f s", self.vmid, spec["path"],
                                        UNSUPPORTED_RETRY_S)
                        self.unsupported = True
                        self.last_error = "HTTP 404"
                        with self.cv:
                            self.cv.notify_all()
                        self.stop_evt.wait(UNSUPPORTED_RETRY_S)
                        continue
                    self._fail("HTTP %s" % up.status_code)
                    attempt = self._backoff(attempt)
                    continue
                self.unsupported = False
                got = self._pump(up)
                if got:
                    attempt = 0
                if not self.stop_evt.is_set():
                    attempt = self._backoff(attempt)
        finally:
            with self.cv:
                self.connected = False
                self.cv.notify_all()
            self._settle_evt.set()
            self.relay._forget(self)
            log.info("state relay: #%s unsubscribed (%s)", self.vmid, reason)

    def _fail(self, msg):
        self.errors += 1
        if msg != self.last_error:
            log.warning("state relay: #%s upstream unavailable: %s", self.vmid, msg)
        self.last_error = msg

    def _backoff(self, attempt):
        self.stop_evt.wait(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        return attempt + 1

    def _pump(self, up):
        """Read one upstream connection to its end. Returns True if a data event was received."""
        got = False
        with self.cv:
            self.connected = True
            self.connects += 1
            if self.connects > 1:
                self.reconnects += 1
            self.gen += 1
            self.rx = time.monotonic()
            self._up = up
            self._end_feed()
        if self.connects > 1:
            log.info("state relay: #%s reconnected (reconnect %d)", self.vmid, self.reconnects)
        buf = b""
        try:
            while not self.stop_evt.is_set():
                b = _read_some(up)
                if not b:
                    self._fail("upstream closed")
                    break
                self.rx = time.monotonic()
                buf += b
                if b"\r" in buf:
                    buf = buf.replace(b"\r\n", b"\n")
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    if self._block(block + b"\n\n"):
                        got = True
                if self.idle() or not self.relay.world.is_active() or not self.relay.world.enabled():
                    break
        except Exception as e:                                        # noqa: BLE001
            if not self.stop_evt.is_set():
                self._fail("read: %s" % e)
        finally:
            try:
                up.close()
            except Exception:                                         # noqa: BLE001
                pass
            with self.cv:
                self._up = None
                self.connected = False
                self._end_feed()
                self.cv.notify_all()
        return got

    def _block(self, block):
        lines = block.rstrip(b"\n").split(b"\n")
        data = [ln[5:].lstrip(b" ") if ln.startswith(b"data:") else None for ln in lines]
        data = [d for d in data if d is not None]
        if not data:
            return False                                              # comment / keepalive
        text = b"\n".join(data).decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
            ok = isinstance(parsed, dict)
        except ValueError:
            ok, parsed = False, None
        with self.cv:
            self.raw = block
            self.doc_text = text if ok else None
            self.rev += 1
            self.event_wall = self.doc_wall = time.time()
            if self.feed is not None:
                self.feed.publish(block, self.rev)     # under cv: ordered after any hand-off
            self.cv.notify_all()
        if ok and time.monotonic() - self.internal_need < SETTLE_NEEDED_S \
                and not pushes_motion(self.spec, parsed):
            self._settle_due = time.monotonic() + SETTLE_S
            self._settle_evt.set()
        return True

    # -- settle read ---------------------------------------------------------------------------
    def _settle_loop(self):
        while not self.stop_evt.is_set():
            self._settle_evt.wait()
            self._settle_evt.clear()
            if self.stop_evt.is_set():
                return
            while self._settle_due is not None and not self.stop_evt.is_set():
                delay = self._settle_due - time.monotonic()
                if delay > 0:
                    if self.stop_evt.wait(delay):
                        return
                    continue
                self._settle_due = None
                rev = self.rev
                self._settle_once(rev)

    def _settle_once(self, rev):
        w = self.relay.world
        try:
            ip = w.ip(self.vmid)
            if not ip:
                return
            text = w.get_text("http://%s:%d%s" % (ip, self.spec["port"], self.spec["mirrors"]))
            if not isinstance(json.loads(text), dict):
                return
        except Exception as e:                                        # noqa: BLE001
            log.debug("state relay: settle read of #%s failed: %s", self.vmid, e)
            return
        with self.cv:
            self.settle_reads += 1
            if self.rev == rev:          # a newer event arrived meanwhile: it supersedes this read
                self.doc_text = text
                self.doc_wall = time.time()
                self.settled_rev = rev

    # -- accounting ----------------------------------------------------------------------------
    def count_fallback(self, consumer, reason):
        k = (consumer, reason)
        self.fallbacks[k] = self.fallbacks.get(k, 0) + 1
        self.relay._count(consumer, reason)

    def snapshot(self, host=None):
        now_m, now_w = time.monotonic(), time.time()
        fb = {}
        for (cons, reason), n in self.fallbacks.items():
            fb.setdefault(cons, {})[reason] = n
        return {
            "vmid": self.vmid, "hostname": host, "path": self.spec.get("path"),
            "connected": self.connected, "fresh": self.fresh(),
            "state": ("unsupported" if self.unsupported else "fresh" if self.fresh()
                      else "stale" if self.connected else "disconnected"),
            "rev": self.rev, "settled_rev": self.settled_rev,
            "event_age_s": None if self.event_wall is None else round(now_w - self.event_wall, 3),
            "doc_age_s": None if self.doc_wall is None else round(now_w - self.doc_wall, 3),
            "rx_age_s": None if self.rx is None else round(now_m - self.rx, 3),
            "clients": self.clients, "connects": self.connects, "reconnects": self.reconnects,
            "errors": self.errors, "last_error": self.last_error,
            "settle_reads": self.settle_reads, "served": dict(self.served), "fallbacks": fb,
            "since": self.started,
        }


# ─── the relay ───────────────────────────────────────────────────────────────────────────────
class Relay:
    def __init__(self, world=None):
        self.world = world or World()
        self.lock = threading.Lock()
        self.subs = {}               # vmid -> Subscription
        self.fallbacks = {}          # (consumer, reason) -> n (all containers, including stopped)
        self.served = {}             # consumer -> n
        self.browser_streams = 0     # browser streams served since start

    def _count(self, consumer, reason):
        with self.lock:
            k = (consumer, reason)
            self.fallbacks[k] = self.fallbacks.get(k, 0) + 1

    def _forget(self, sub):
        with self.lock:
            if self.subs.get(sub.vmid) is sub:
                del self.subs[sub.vmid]

    def ensure(self, vmid, spec):
        """The live subscription of `vmid` (started if needed). Caller checked is_active()."""
        with self.lock:
            sub = self.subs.get(vmid)
            if sub is not None and not sub.stop_evt.is_set() and sub.thread.is_alive():
                sub.need = time.monotonic()
                return sub, False
            sub = Subscription(self, vmid, spec)
            self.subs[vmid] = sub
        sub.thread.start()
        sub.settler.start()
        return sub, True

    def stop_all(self):
        with self.lock:
            subs = list(self.subs.values())
        for s in subs:
            s.stop()

    # -- internal readers ----------------------------------------------------------------------
    def read(self, vmid, consumer, endpoint=None, fields=None, manifest=None):
        """Last state document of `vmid` for an internal reader, or None.

        None means "take your usual path": the container publishes no state stream (not counted),
        or the relay cannot vouch for the document (counted by reason). `fields` = top-level keys
        the caller reads (`ALL_FIELDS` = the whole document); None = the caller vouches it reads
        none of the stream's volatile fields. `endpoint` must be the mirrored one (default
        `/state`). Returns a NEW dict on each call (callers may keep or modify it)."""
        active = self.world.is_active()
        if manifest is None:
            if not active:
                return None          # no lookup on a standby (nor in tests: cf. World.is_active)
            manifest, _h = self.world.context(vmid)
        spec = stream_spec(manifest)
        if spec is None or (endpoint or DEFAULT_MIRRORS).split("?", 1)[0] != spec["mirrors"] \
                or "?" in (endpoint or ""):
            return None
        if not active:
            self._count(consumer, "standby")
            return None
        if not self.world.enabled():
            self._count(consumer, "disabled")
            return None
        sub, new = self.ensure(vmid, spec)
        sub.internal_need = time.monotonic()
        reason = None
        with sub.cv:
            text = sub.doc_text
            if new or (sub.raw is None and sub.connected):
                reason = "starting"
            elif sub.unsupported:
                reason = "unsupported"
            elif not sub.connected:
                reason = "disconnected"
            elif not sub.fresh():
                reason = "stale"
            elif text is None:
                reason = "unparsable"
        if reason is None:
            asked = set(fields or ())
            if ALL_FIELDS in asked or asked & set(spec["volatile"]):
                reason = "volatile_field"
        doc = None
        if reason is None:
            doc = json.loads(text)
            if (ALL_FIELDS in asked or asked & set(spec["motion_volatile"])) \
                    and in_motion(doc, spec["motion_keys"]) and not pushes_motion(spec, doc):
                reason, doc = "motion", None
        if reason is not None:
            sub.count_fallback(consumer, reason)
            return None
        sub.served[consumer] = sub.served.get(consumer, 0) + 1
        with self.lock:
            self.served[consumer] = self.served.get(consumer, 0) + 1
        return doc

    # -- browsers ------------------------------------------------------------------------------
    def serve(self, vmid, manifest, entry):
        """Flask response for a browser asking the state stream, or None (plain proxy: standby,
        or the entry is not the relayable stream)."""
        spec = stream_spec(manifest)
        if spec is None or spec["path"] != entry.get("path"):
            return None
        if not self.world.is_active() or not self.world.enabled():
            return None
        from flask import Response, jsonify
        sub, _new = self.ensure(vmid, spec)
        deadline = time.monotonic() + FIRST_EVENT_WAIT_S
        with sub.cv:
            while not (sub.fresh() or sub.unsupported or sub.stop_evt.is_set()):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sub.cv.wait(left)
            if sub.unsupported:
                return jsonify({"error": "no state stream on this engine"}), 404
            if not sub.fresh():
                return jsonify({"error": "state stream unavailable",
                                "detail": sub.last_error}), 502
            first, gen, rev = sub.raw, sub.gen, sub.rev
        with self.lock:
            self.browser_streams += 1
        from flask import request
        from . import stream_hub
        if stream_hub.detachable(request.environ):
            # The client socket goes to the stream hub once Flask has built this response (rights
            # already checked above): no server thread is held while the stream is open.
            request.environ[stream_hub.ENV_PENDING] = \
                lambda sock, tls, head, chunked, meta: self._hand_off(sub, gen, sock, tls, head, chunked, meta)

        def gen_():
            nonlocal rev
            with sub.cv:
                sub.clients += 1
            try:
                yield first
                while True:
                    with sub.cv:
                        if sub.rev == rev and sub.gen == gen and sub.connected:
                            sub.cv.wait(CLIENT_KEEPALIVE_S)
                        if sub.gen != gen or not sub.connected or sub.stop_evt.is_set():
                            return                       # upstream lost: close, the page reconnects
                        if sub.rev != rev:
                            rev, out = sub.rev, sub.raw  # latest wins: each block is a full state
                        else:
                            out = b": ka\n\n"
                    yield out
            finally:
                with sub.cv:
                    sub.clients -= 1
                    sub.need = time.monotonic()

        return Response(gen_(), status=200, content_type=entry.get("content_type") or "text/event-stream",
                        direct_passthrough=True,
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def _hand_off(self, sub, gen, sock, tls, head, chunked, meta):
        """Give a detached browser socket to the stream hub: head, then the LATEST block (taken now,
        not when the view ran), then every block of this upstream connection. The hand-off is
        posted while holding `sub.cv`, and so is every publish: no block can be lost or doubled
        between the two. Upstream lost meanwhile → the head is written and the stream ends."""
        from . import stream_hub
        hub = stream_hub.hub()
        meta = dict(meta or {}, vmid=sub.vmid)
        with sub.cv:
            if sub.gen == gen and sub.connected and not sub.stop_evt.is_set() and sub.raw is not None:
                if sub.feed is None:
                    sub.feed = hub.feed(label="state-relay #%s" % sub.vmid, keepalive_s=CLIENT_KEEPALIVE_S,
                                        on_leave=sub._hub_leave)
                sub.clients += 1
                sub.need = time.monotonic()
                hub.attach(sock, tls, head, sub.raw, sub.rev, sub.feed, chunked, meta)
                return
        hub.attach(sock, tls, head, None, 0, None, chunked, meta)

    # -- visibility ----------------------------------------------------------------------------
    def stats(self):
        with self.lock:
            subs = list(self.subs.values())
            fb = {}
            for (cons, reason), n in self.fallbacks.items():
                fb.setdefault(cons, {})[reason] = n
            served = dict(self.served)
            bs = self.browser_streams
        rows = []
        for s in sorted(subs, key=lambda x: x.vmid):
            _m, host = self.world.context(s.vmid)
            rows.append(s.snapshot(host))
        return {"active": self.world.is_active(), "enabled": self.world.enabled(), "subscriptions": rows,
                "upstream_connections": sum(1 for r in rows if r["connected"]),
                "clients": sum(r["clients"] for r in rows),
                "served": served, "fallbacks": fb, "browser_streams_total": bs,
                "limits": {"stale_s": STALE_S, "idle_stop_s": IDLE_STOP_S, "settle_s": SETTLE_S}}


RELAY = Relay()


def read(vmid, consumer, endpoint=None, fields=None, manifest=None):
    """Module-level shortcut on the process relay (cf. Relay.read). Never raises."""
    try:
        return RELAY.read(vmid, consumer, endpoint=endpoint, fields=fields, manifest=manifest)
    except Exception as e:                                            # noqa: BLE001
        log.warning("state relay: read of #%s by %s failed: %s", vmid, consumer, e)
        return None


def serve(vmid, manifest, entry):
    return RELAY.serve(vmid, manifest, entry)


def stats():
    return RELAY.stats()
