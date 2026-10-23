# SPDX-License-Identifier: GPL-3.0-or-later
"""Voice identification of a console strip or bus (generator `sig = voice`), done by the CONTROLLER.

The clip is synthesized here (optional service `services/tts`, cached by text) and pushed to the console
(`POST :8082/ident.pcm?node=<id>&key=<k>`, float32 LE 48 kHz mono). Two callers, one code path:

- the route `/api/console-zones/<vmid>/ident` (the console UI, `identPush`), which applies zones first;
- the macro action `core: "voice_ident"` declared by a plugin manifest (`announce` / `announce_stop`):
  without it a macro setting `gen.sig = voice` produced no voice unless someone had the UI open.

Every failure is an `IdentError` carrying a CODE (translated by the interface as `ident.err_<code>`), never
a sentence, and never silent: a missing tts service fails the macro step with `tts_not_installed`."""
import json
import logging
import re
import threading

import requests

log = logging.getLogger(__name__)

PUSH_TIMEOUT_S = 10.0
SET_TIMEOUT_S = 5.0
SAYS = ("long", "short", "custom")


class IdentError(RuntimeError):
    """`code` = machine code (UI key `ident.err_<code>`), `http_status` = status for the route, `extra` =
    context joined to the JSON body (e.g. the console's own `status`)."""

    def __init__(self, code, http_status=400, **extra):
        super().__init__(code + (" " + json.dumps(extra, ensure_ascii=False) if extra else ""))
        self.code, self.http_status, self.extra = code, http_status, extra

    def body(self):
        return dict(self.extra, error=self.code)


def _tts():
    try:
        from services import tts
    except Exception:
        raise IdentError("tts_missing", 409)
    if not tts.installed():
        raise IdentError("tts_not_installed", 409)
    return tts


def _control_url(vmid, path):
    from . import plugin_persist
    url = plugin_persist._url(vmid, path)
    if not url:
        raise IdentError("no_address", 503)
    return url


def push(vmid, node, text, lang="fr"):
    """Synthesizes `text` and pushes the clip to node `node` of console `vmid`. {key, seconds}."""
    node, text = str(node or ""), str(text or "")
    if not node or not text.strip():
        raise IdentError("bad_request", 400)
    tts = _tts()
    try:
        key, pcm = tts.synthesize(text, lang or "fr")
    except Exception as e:
        log.warning("console ident: synthesis failed for %s: %s", vmid, e)
        raise IdentError("synthesis_failed", 500)
    url = _control_url(vmid, "/ident.pcm?node=%s&key=%s" % (node, key))
    try:
        r = requests.post(url, data=pcm, headers={"Content-Type": "application/octet-stream"}, timeout=PUSH_TIMEOUT_S)
    except Exception as e:
        log.warning("console ident: push to %s failed: %s", vmid, e)
        raise IdentError("unreachable", 502)
    if r.status_code == 404:
        raise IdentError("unknown_node", 404, node=node)
    if r.status_code >= 300:
        raise IdentError("refused", 502, status=r.status_code)
    return {"key": key, "seconds": round(len(pcm) / 4 / 48000, 2)}


# ── What is said: same rules as the console UI (control.js identText / shortOf) ──────────────────────
def short_of(o):
    """Short name: the one typed, else derived from the long one, keeping a trailing number (« Invit 3 »)."""
    own = str(o.get("short") or "").strip()
    if own:
        return own
    lab = str(o.get("label") or o.get("id") or "").strip()
    m = re.match(r"^(.*?)[\s_-]*(\d+)$", lab)
    if len(lab) <= 7 or not m or not m.group(1):
        return lab[:7].strip()
    head, num = m.group(1), m.group(2)
    return (head[:max(2, 6 - len(num))].strip() + " " + num)[:8]


def text_for(o, say=None, text=None):
    """Text announced for strip/bus `o` (`say` and `text` override its `gen`)."""
    g = o.get("gen") or {}
    say = say or g.get("say") or "long"
    long_name = o.get("label") or o.get("id") or ""
    if say == "custom":
        return (text if text is not None else g.get("text")) or long_name
    if say == "short":
        return short_of(o)
    return long_name


def _live_doc(vmid):
    try:
        r = requests.get(_control_url(vmid, "/doc?live=1"), timeout=SET_TIMEOUT_S)
    except IdentError:
        raise
    except Exception:
        raise IdentError("unreachable", 502)
    if r.status_code != 200:
        raise IdentError("refused", 502, status=r.status_code)
    try:
        return r.json()
    except ValueError:
        raise IdentError("refused", 502, status=r.status_code)


def _node(doc, nid):
    for kind in ("strips", "buses"):
        for o in doc.get(kind) or []:
            if isinstance(o, dict) and o.get("id") == nid:
                return kind, o
    return None, None


def _set(vmid, manifest, sets):
    try:
        r = requests.post(_control_url(vmid, "/set"), json={"sets": sets}, timeout=SET_TIMEOUT_S)
    except IdentError:
        raise
    except Exception:
        raise IdentError("unreachable", 502)
    if r.status_code >= 300:
        raise IdentError("refused", 502, status=r.status_code)
    try:   # same declarative persistence as a fader moved by hand or by a plain macro action
        from . import plugin_persist
        plugin_persist.after_post(vmid, manifest, "/set", r.status_code)
    except Exception:
        log.exception("console ident: persistence after /set (#%s)", vmid)


def _truthy(v):
    return v is True or str(v).strip().lower() in ("1", "true", "on", "yes", "oui")


def announce(vmid, manifest, node, say=None, text=None, lang=None):
    """Macro « Announce »: synthesize what `say` designates, push it, then gen.sig=voice and gen.on=true
    (with say / lang / text written to the document, so the UI and its restart repair say the same)."""
    node = str(node or "")
    if not node:
        raise IdentError("bad_request", 400)
    if say not in (None, "") and say not in SAYS:
        raise IdentError("bad_say", 400, say=say)
    doc = _live_doc(vmid)
    kind, o = _node(doc, node)
    if not o:
        raise IdentError("unknown_node", 404, node=node)
    if not isinstance(o.get("gen"), dict):
        raise IdentError("no_generator", 409, node=node)
    g = o["gen"]
    say = say or g.get("say") or "long"
    lang = lang or g.get("lang") or "fr"
    custom = text if (say == "custom" and str(text or "").strip()) else None
    res = push(vmid, node, text_for(o, say, custom), lang)
    base = "%s.%s.gen." % (kind, node)
    sets = [{"path": base + "say", "value": say}, {"path": base + "lang", "value": lang}]
    if custom is not None:
        sets.append({"path": base + "text", "value": custom})
    sets += [{"path": base + "sig", "value": "voice"}, {"path": base + "on", "value": True}]
    _set(vmid, manifest, sets)
    return res


def stop(vmid, manifest, node):
    """Macro « Stop the announcement »: gen.on=false (the clip stays, a later Announce is instant)."""
    node = str(node or "")
    if not node:
        raise IdentError("bad_request", 400)
    kind, o = _node(_live_doc(vmid), node)
    if not o:
        raise IdentError("unknown_node", 404, node=node)
    _set(vmid, manifest, [{"path": "%s.%s.gen.on" % (kind, node), "value": False}])
    return {"ok": True}


def run_macro_action(vmid, manifest, act, params, variables=None):
    """Plugin action `core: "voice_ident"` (app/macros.exec_action). Params: `node`, and for an announce
    `say`, `text`, `lang`; `body.on = false` makes it the stop action. Raises IdentError (a RuntimeError)."""
    from .macros import _tmpl
    variables = variables or {}
    vals = dict(act.get("body") or {})
    for p in act.get("params") or []:
        k = p["key"]
        if params and k in params and params[k] not in (None, ""):
            vals[k] = _tmpl(params[k], variables)
        elif p.get("default") is not None:
            vals.setdefault(k, p["default"])
    if "on" in vals and not _truthy(vals["on"]):
        return stop(vmid, manifest, vals.get("node"))
    return announce(vmid, manifest, vals.get("node"), vals.get("say"), vals.get("text"), vals.get("lang"))



# ── A plain /set that switches a generator to voice (2026-10-17) ─────────────────────────────────────
# The UI pushes the clip itself (identPush) and the macro action « Announce » too; but a raw macro POST, a
# parameter-tree macro or a control surface writing `gen.sig = voice` used to leave the strip SILENT: the
# console had the setting and no clip. Every /set relayed by the orchestrator now goes through here: for each
# generator it touched that ends up on voice, the clip the console holds is compared with the one its text
# calls for (tts cache key) and pushed when missing or stale. Background thread — never delays the gesture.
_GEN_PATH = re.compile(r"^(strips|buses)\.([^.]+)\.gen\.(sig|on|say|text|lang)$")


def _touched_nodes(body):
    items = body.get("sets") if isinstance(body, dict) and isinstance(body.get("sets"), list) else [body]
    out = []
    for it in items:
        m = _GEN_PATH.match(str((it or {}).get("path") or "")) if isinstance(it, dict) else None
        if m and m.group(2) not in out:
            out.append(m.group(2))
    return out


def declares_voice(manifest):
    m = manifest or {}
    acts = list(m.get("actions") or []) + list((m.get("control") or {}).get("actions") or [])
    return any(isinstance(a, dict) and a.get("core") == "voice_ident" for a in acts)


def follow_set(vmid, manifest, endpoint, body, status=200, sync=False):
    """To call after a /set relayed to a plugin (proxy, macros). Does nothing unless the plugin declares the
    voice_ident action, the console accepted the write, and a generator field was written."""
    if endpoint != "/set" or not (200 <= int(status or 0) < 300) or not declares_voice(manifest):
        return None
    nodes = _touched_nodes(body)
    if not nodes:
        return None
    if sync:
        return _follow(vmid, nodes)
    threading.Thread(target=_follow, args=(vmid, nodes), daemon=True, name="voice-follow").start()
    return None


def _follow(vmid, nodes):
    pushed = []
    try:
        doc = _live_doc(vmid)
        r = requests.get(_control_url(vmid, "/state"), timeout=SET_TIMEOUT_S)
        have = (r.json() or {}).get("voices") or {} if r.status_code == 200 else {}
        for nid in nodes:
            _kind, o = _node(doc, nid)
            g = (o or {}).get("gen")
            if not isinstance(g, dict) or g.get("sig") != "voice":
                continue
            text, lang = text_for(o), g.get("lang") or "fr"
            key, _pcm = _tts().synthesize(text, lang)          # cached: the key the clip should carry
            if (have.get(nid) or {}).get("key") == key:
                continue
            push(vmid, nid, text, lang)
            pushed.append(nid)
    except Exception as e:                       # never silent: the strip would stay mute with no trace
        code = e.code if isinstance(e, IdentError) else ("tts_not_installed" if "not installed" in str(e) else type(e).__name__)
        log.warning("console ident: clip not pushed after /set on #%s (%s)", vmid, code)
        try:
            from .database import db_add_alert
            db_add_alert("alert.console.ident_follow_failed", "warning", vmid=vmid, params={"vmid": vmid, "e": code})
        except Exception:
            pass
    return pushed
