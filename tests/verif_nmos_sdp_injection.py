#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Remote sender activation (services/nmos) runs curl ON A NODE with values that come from the
# network: the SDP of an IS-05 receiver PATCH, then the sender list and manifest URL answered
# by the remote device. None of them may ever be read by the node's shell as anything but one
# argument of that curl call.
#
# Two layers are checked separately, because either one alone hides the absence of the other:
#   1. values are validated where they enter (IPv4 literal, port range, plain UUID, http URL);
#   2. the command line quotes every word, whatever reaches it.
#
# The verdict is not a pattern search in the command: each captured command is EVALUATED by
# /bin/sh with `curl` shadowed by a function, in a throwaway directory. A payload that escapes
# its argument creates a marker file there; nothing else is ever run, nothing leaves the host.
#
#   $ ./venv/bin/python tests/verif_nmos_sdp_injection.py
import json
import os
import shlex
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── HERMETIC: throwaway database, never the production one ──────────────────────────────────
import tempfile                                                      # noqa: E402
TMP = tempfile.mkdtemp(prefix="verif_nmos_sdp_injection_")
from app import config, database, media_registry                     # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "principale.db")
media_registry.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
database._tls.__dict__.pop("conn", None)
database.init_db()

import app.host_ops as host_ops                                      # noqa: E402
import services.nmos as nmos                                         # noqa: E402

failures, passes = [], []


def check(title, condition, why=""):
    (passes if condition else failures).append(title)
    print("  %-5s %s" % ("OK" if condition else "FAIL", title))
    if not condition and why:
        print("        -> %s" % why)


SANDBOX = os.path.join(TMP, "sandbox")
os.mkdir(SANDBOX)
MARK = os.path.join(SANDBOX, "INJECTED")
ARGC = os.path.join(SANDBOX, "argc")

NODE = "node-under-test"
SRC, GROUP, PORT = "192.0.2.10", "239.1.1.1", 5004
SID = "0f1e2d3c-4b5a-4978-8695-a4b3c2d1e0f9"
BASE = "http://%s:8090" % SRC
MANIFEST = BASE + "/sdp/video"

# What a hostile peer may put in a field. `touch` is the only command any of them carries.
T = "touch %s" % MARK
PAYLOADS = {
    "quote":        "x'; %s; '" % T,
    "semicolon":    "x; %s" % T,
    "substitution": "$(%s)" % T,
    "quote+subst":  "x'$(%s)'" % T,
    "backtick":     "`%s`" % T,
    "quote+tick":   "x'`%s`'" % T,
    "newline":      "x\n%s\n" % T,
    "quote+nl":     "x'\n%s\n'" % T,
    "no blank":     "x';touch${IFS}%s;'" % MARK,
    "option":       "-o%s" % MARK,
}


def sdp(source=SRC, group=GROUP, port=PORT, source_filter=None):
    lines = ["v=0", "o=- 1 1 IN IP4 %s" % source, "s=test", "t=0 0",
             "m=video %s RTP/AVP 96" % port, "c=IN IP4 %s/64" % group]
    if source_filter is not None:
        lines.append("a=source-filter:incl IN IP4 %s %s" % (group, source_filter))
    return "\r\n".join(lines) + "\r\n"


# ── The node, replaced: records every command, answers like a remote NMOS device ────────────
commands = []
device = {}


def fake_ssh_run(host, cmd, input_data=None, timeout=300):
    commands.append(cmd)
    try:
        url = shlex.split(cmd)[-1]
    except ValueError:
        return (2, "", "")
    if "PATCH" in cmd:
        return (0, "204", "")
    if url.endswith("/x-nmos/node/v1.3/senders/"):
        return (0, json.dumps(device.get("senders", [])), "") if ":8090/" in url else (7, "", "")
    if url.endswith("/staged"):
        return (0, json.dumps({"master_enable": False}), "")
    return (0, device.get("manifest", sdp()), "")


host_ops.ssh_run = fake_ssh_run
nmos._node_host_for_vmid = lambda vmid: NODE
database.db_set_setting("nmos_auto_activate_senders", True)


def evaluate(cmd):
    """Let a real shell read `cmd`, curl being a function. (escaped, argc seen by curl)."""
    for f in (MARK, ARGC):
        if os.path.exists(f):
            os.remove(f)
    script = 'curl() { echo "$#" > %s; }\n%s\n' % (shlex.quote(ARGC), cmd)
    subprocess.run(["/bin/sh", "-c", script], cwd=SANDBOX, timeout=20,
                   env={"PATH": "/usr/bin:/bin"}, capture_output=True)
    escaped = os.path.exists(MARK)
    argc = int(open(ARGC).read()) if os.path.exists(ARGC) else None
    if escaped:
        os.remove(MARK)
    return escaped, argc


def unsafe(cmds):
    """First command a shell does not read as exactly one curl call, or None."""
    for cmd in cmds:
        escaped, argc = evaluate(cmd)
        try:
            words = shlex.split(cmd)
        except ValueError:
            return cmd
        if escaped or words[:1] != ["curl"] or argc != len(words) - 1 or "touch" in words:
            return cmd
    return None


def loads(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def run(fn, *args):
    """Calls `fn`, returns the commands that reached the node. A refusal by exception is fine."""
    del commands[:]
    try:
        fn(*args)
    except Exception:
        pass
    return list(commands)


CURL = ["curl", "-s", "-m", "2", "--proto", "=http,https"]

print("NMOS remote sender activation: network values on a node's command line\n")

# ── 0. The harness itself: a broken-out payload must be SEEN ────────────────────────────────
# Without this, every check below could be green because the detector is blind.
escaped, _ = evaluate("curl -s 'http://%s'" % PAYLOADS["quote"])
check("harness: an unquoted payload is detected", escaped,
      "the shell evaluation does not see an escape: no verdict below means anything")
escaped, argc = evaluate("curl -s %s" % shlex.quote("http://" + PAYLOADS["quote"]))
check("harness: a quoted payload is not flagged", not escaped and argc == 2)

# ── 1. The legitimate case still works, and says exactly what it runs ───────────────────────
device.update(senders=[{"id": SID, "manifest_href": MANIFEST}], manifest=sdp())
got = [shlex.split(c) for c in run(nmos._activate_remote_sender_if_needed, 101, 0, sdp())]
body = json.dumps({"master_enable": True, "activation": {"mode": "activate_immediate"}})
staged = "%s/x-nmos/connection/v1.0/single/senders/%s/staged" % (BASE, SID)
expected = [
    CURL + ["--", BASE + "/x-nmos/node/v1.3/senders/"],
    CURL + ["--", MANIFEST],
    CURL + ["--", staged],
    CURL + ["-o", "/dev/null", "-w", "%{http_code}", "-X", "PATCH",
            "-H", "Content-Type: application/json", "-d", body, "--", staged],
]
check("a valid SDP lists, reads, then activates the matching sender", got == expected,
      "got %r" % got)
check("... and each of those commands is one curl call for the shell", unsafe(commands) is None)

nmos.active_sdp_for = lambda vmid, idx, essence="video": sdp()
res = nmos.manual_activate_remote_sender(101, 0)
check("the manual path reports the activation", res.get("ok") and res.get("activated")
      and res.get("sender_id") == SID, "got %r" % res)

check("`a=source-filter` still takes precedence over `o=`",
      nmos._sdp_endpoint(sdp(source="192.0.2.99", source_filter=SRC)) == (SRC, GROUP, PORT))

# ── 2. Layer 1: a hostile SDP never reaches the node ────────────────────────────────────────
for name, bad in list(PAYLOADS.items()) + [("hostname", "device.example"),
                                            ("ipv4 + tail", SRC + "';" + T),
                                            ("ipv6", "2001:db8::1")]:
    for field, text in (("o=", sdp(source=bad)), ("source-filter", sdp(source_filter=bad))):
        cmds = run(nmos._activate_remote_sender_if_needed, 101, 0, text)
        check("SDP source (%s) = %s: nothing is run" % (field, name), cmds == [],
              "reached the node: %r" % cmds[:1])
        check("... and what would have run stays one curl call", unsafe(cmds) is None)
    nmos.active_sdp_for = lambda vmid, idx, essence="video", _t=sdp(source=bad): _t
    cmds = run(nmos.manual_activate_remote_sender, 101, 0)
    check("manual path, SDP source = %s: nothing is run" % name, cmds == [])

for name, text in (("group = hostname", sdp(group="group.example")),
                   ("group = payload", sdp(group=PAYLOADS["no blank"])),
                   ("port out of range", sdp(port=70000)),
                   ("port zero", sdp(port=0))):
    cmds = run(nmos._activate_remote_sender_if_needed, 101, 0, text)
    check("SDP %s: nothing is run" % name, cmds == [], "reached the node: %r" % cmds[:1])

cmds = run(nmos._try_activate_sender_on_ports, NODE, 101, 0, PAYLOADS["quote"], GROUP, PORT, (8090,))
check("a source that bypassed the SDP parser is refused where the URL is built", cmds == [],
      "reached the node: %r" % cmds[:1])
cmds = run(nmos._try_activate_sender_on_ports, NODE, 101, 0, SRC, GROUP, PORT, (PAYLOADS["quote"],))
check("a port that is not a number is refused where the URL is built", cmds == [])

# ── 3. Layer 1: the remote device's own answers are not trusted either ──────────────────────
def is_listing(cmd):
    words = shlex.split(cmd)
    return (words[:-1] == CURL + ["--"]
            and words[-1] in ["http://%s:%d/x-nmos/node/v1.3/senders/" % (SRC, p)
                              for p in nmos._KNOWN_NMOS_API_PORTS])


for name, bad in list(PAYLOADS.items()) + [("braces", "{%s}" % SID), ("urn", "urn:uuid:" + SID),
                                            ("not a string", 12)]:
    device.update(senders=[{"id": bad, "manifest_href": MANIFEST}], manifest=sdp())
    cmds = run(nmos._activate_remote_sender_if_needed, 101, 0, sdp())
    check("device sender id = %s: only the listing is requested" % name,
          bool(cmds) and unsafe(cmds) is None and all(is_listing(c) for c in cmds),
          "reached the node: %r" % [c for c in cmds if unsafe([c]) or not is_listing(c)][:1])

for name, bad in list(PAYLOADS.items()) + [("local file", "file:///etc/hostname"),
                                            ("not a string", ["x"])]:
    for href in (bad, BASE + "/" + bad if isinstance(bad, str) else bad):
        device.update(senders=[{"id": SID, "manifest_href": href}], manifest=sdp())
        cmds = run(nmos._activate_remote_sender_if_needed, 101, 0, sdp())
        check("device manifest URL = %s%s: no escape" % (name, "" if href is bad else " (in path)"),
              bool(cmds) and unsafe(cmds) is None, "unsafe: %r" % unsafe(cmds))
        check("... and a URL that is not http(s) is not fetched",
              all(shlex.split(c)[-1].startswith(("http://", "https://")) for c in cmds))

device.update(senders={"id": SID}, manifest=sdp())
check("a sender list that is not a list is ignored",
      len(run(nmos._activate_remote_sender_if_needed, 101, 0, sdp())) == 8)

# ── 4. Layer 2: the three helpers quote whatever they are given ─────────────────────────────
# Called directly on purpose: layer 1 stops these values upstream, which would leave the
# quoting untested — and it is the layer that protects the next caller someone adds.
for name, bad in PAYLOADS.items():
    url = "http://192.0.2.10:8090/" + bad
    for helper, args in (("_curl_json", (NODE, url)), ("_curl_text", (NODE, url)),
                         ("_curl_patch", (NODE, url, {"k": bad}))):
        cmds = run(getattr(nmos, helper), *args)
        check("%s, hostile URL (%s): one curl call" % (helper, name),
              len(cmds) == 1 and unsafe(cmds) is None and shlex.split(cmds[0])[-1] == url,
              "command: %r" % cmds[:1])
    cmds = run(nmos._curl_patch, NODE, BASE + "/staged", {"k": bad, bad: 1})
    words = shlex.split(cmds[0]) if cmds and unsafe(cmds) is None else []
    check("_curl_patch, hostile body (%s): the body arrives whole" % name,
          "-d" in words and loads(words[words.index("-d") + 1]) == {"k": bad, bad: 1},
          "command: %r" % cmds[:1])
    for helper in ("_curl_json", "_curl_text"):
        cmds = run(getattr(nmos, helper), NODE, BASE + "/x", "2 " + bad)
        check("%s, hostile timeout (%s): refused or harmless" % (helper, name), unsafe(cmds) is None,
              "command: %r" % cmds[:1])
    cmds = run(nmos._curl_patch, NODE, BASE + "/x", {}, "2 " + bad)
    check("_curl_patch, hostile timeout (%s): refused or harmless" % name, unsafe(cmds) is None)

cmds = run(nmos._curl_json, NODE, PAYLOADS["option"])
check("a URL that looks like an option stays an operand",
      len(cmds) == 1 and shlex.split(cmds[0])[-2:] == ["--", PAYLOADS["option"]])

print("\n%d passed, %d failed" % (len(passes), len(failures)))
if failures:
    for f in failures:
        print("  FAIL %s" % f)
    sys.exit(1)
print("OK")
