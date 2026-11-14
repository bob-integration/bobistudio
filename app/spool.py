"""Where large request bodies land before the application sees them.

Waitress does not stream: past `inbuf_overflow` (512 KiB) it writes the WHOLE request body into an
anonymous temp file in TMPDIR and only then calls the WSGI app (`waitress/buffers.py`). A route
reading `request.stream` byte by byte is therefore reading something already fully materialised.

On a stock Debian, TMPDIR is `/tmp`, a tmpfs — RAM. So the transit space is a slice of memory,
usually half of it. Measured on a test install (2026-11-13): 3.8 GiB of RAM, `/tmp` a 2 GiB tmpfs,
`max_request_body_size` 4 GiB, host-driver cap 2 GiB. The product promised 4 GiB through a 2 GiB
buffer carved out of memory. A 2.09 GB Blackmagic archive was accepted by every cap, filled the
tmpfs 38 MB before the end, died on ENOSPC — and left its buffer behind, so `/tmp` stayed full and
EVERY later request with a body failed, including importing a plugin, which has nothing to do with
driver uploads. The operator saw « HTTP 500 » on an unrelated screen.

The same archive had already been refused on 2026-10-27, by the 1 GiB Waitress default. The answer
then was to raise the cap to 4 GiB — comparing the caps to one another, never to the place the
bytes had to land. Raising a ceiling without checking the floor is how a clean refusal became a
machine-wide outage.

So: this module PICKS the transit directory instead of inheriting the distribution's, and refuses
to pretend. `capacity()` is the number every upload cap must be clamped to, and it is measured,
not declared.
"""
import logging
import os
import shutil
import tempfile

log = logging.getLogger(__name__)

# Filesystems that live in RAM. A transit buffer here competes with the containers' own memory,
# and the failure it produces is an out-of-space error that names no cause.
_RAM_FS = ("tmpfs", "ramfs")

# Default transit directory, outside the install tree (FHS: /var/tmp survives reboots and is
# expected to hold large, short-lived files). Overridable through config_local.SPOOL_DIR.
_DEFAUT = "/var/tmp/bobistudio-spool"

_chosen = None          # the directory in use, once chosen
_reason = ""            # why, in one line, for the Settings page and the log


def _mount_fstype(path):
    """Filesystem type of the mount point that carries `path` ('' if it cannot be told).

    Walks up until a real mount point is found, because the directory itself may not be one.
    """
    try:
        monts = {}
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for ligne in f:
                bouts = ligne.split()
                if len(bouts) >= 3:
                    monts[os.path.realpath(bouts[1])] = bouts[2]
    except OSError:
        return ""
    p = os.path.realpath(path)
    while True:
        if p in monts:
            return monts[p]
        parent = os.path.dirname(p)
        if parent == p:
            return ""
        p = parent


def is_ram_backed(path):
    return _mount_fstype(path) in _RAM_FS


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _usable(path, required):
    """(ok, reason). A directory is usable when it exists, is writable, is not RAM-backed, and
    has room for `required` bytes."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        return False, "cannot be created (%s)" % e
    if not os.access(path, os.W_OK):
        return False, "not writable"
    fs = _mount_fstype(path)
    if fs in _RAM_FS:
        return False, "on %s — that is RAM, not storage" % (fs or "tmpfs")
    libre = free_bytes(path)
    if required and libre < required:
        return False, "%d byte(s) free, %d needed" % (libre, required)
    return True, "%s, %d byte(s) free" % (fs or "unknown fs", libre)


def configure(required=0, candidates=None):
    """Pick the transit directory and make it the process-wide default, BEFORE the server starts.

    Sets both `tempfile.tempdir` (what Waitress's TemporaryFile honours) and TMPDIR (for anything
    spawned later). Returns (directory, reason). Falls back to the system default rather than
    refusing to boot — but says so at WARNING, because that fallback is the failure mode above.
    """
    global _chosen, _reason
    if candidates is None:
        try:
            from . import config
            propose = getattr(config, "SPOOL_DIR", "") or ""
        except Exception:
            propose = ""
        candidates = ([propose] if propose else []) + [_DEFAUT]
    for c in candidates:
        ok, pourquoi = _usable(c, required)
        if ok:
            tempfile.tempdir = c
            os.environ["TMPDIR"] = c
            _chosen, _reason = c, pourquoi
            log.info("request bodies over 512 KiB transit through %s (%s); %d byte(s) required",
                     c, pourquoi, required)
            return c, pourquoi
        log.warning("transit directory %s unusable: %s", c, pourquoi)
    defaut = tempfile.gettempdir()
    _chosen = defaut
    _reason = "system default, %s, %d byte(s) free" % (_mount_fstype(defaut) or "unknown fs",
                                                       free_bytes(defaut))
    log.warning("no usable transit directory — falling back to %s (%s). An upload larger than "
                "that will fail part-way AND leave its buffer behind.", defaut, _reason)
    return defaut, _reason


def directory():
    return _chosen or tempfile.gettempdir()


def state():
    """What the Settings page shows: the directory, whether it is RAM, and the room left."""
    d = directory()
    return {"dir": d, "ram_backed": is_ram_backed(d), "free": free_bytes(d), "reason": _reason}


def capacity(margin=64 << 20):
    """Bytes a request body can actually occupy right now, margin kept for everything else.

    This is the number a cap must be clamped to. A cap declared above it is a promise the machine
    cannot keep, and the operator only finds out at the end of a long upload.
    """
    return max(0, free_bytes(directory()) - margin)
