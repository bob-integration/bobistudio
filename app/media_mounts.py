# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Does a container see the media volume it was given? Asked when a load fails.

A container's `/mnt/media` is a bind of a host folder, resolved when the container STARTS. If a
storage volume is mounted on that folder afterwards (`/srv/mxl-media` on a new disk), the
container keeps the old, empty folder underneath: the media base sees the files on the node, the
library marks them local, and the container answers « fichier introuvable » (2026-11-14, stills
on a node whose storage volume was created after the container). The disk panel already warns
when mounting; this answers the question afterwards, when a file cannot be found.

Compares the device and inode of the host folder with what the container sees under
`/mnt/media`, read from the host through `/proc/<pid>/root`: no `docker exec`, nothing runs
inside the container. On demand only — one node command per question.
"""
import shlex


def _probe_cmd(name, host_dir):
    """`<host dev:ino> <container dev:ino>`; a missing value prints `-`."""
    n, h = shlex.quote(name), shlex.quote(host_dir)
    return ("p=$(docker inspect -f '{{.State.Pid}}' %s 2>/dev/null); "
            "echo $(stat -Lc %%d:%%i %s 2>/dev/null || echo -) "
            "$( [ -n \"$p\" ] && [ \"$p\" != 0 ] && stat -Lc %%d:%%i /proc/$p/root/mnt/media 2>/dev/null || echo -)"
            % (n, h))


def check_container(vmid):
    """{ok, sees_volume: True|False|None, host_dir, node}. None = could not tell (container
    stopped, no media volume, node unreachable) — never a guess."""
    import json
    from . import plugins
    from .database import db_get_container, db_get_node
    from .docker_compute import _name, project_media_dir
    from .node_driver import host_exec
    c = db_get_container(int(vmid)) or {}
    try:
        t = (json.loads(c.get("deploy_config") or "{}") or {}).get("type") or ""
    except ValueError:
        t = ""
    node = db_get_node(c.get("node_id")) if c.get("node_id") else None
    if not node or not t or not plugins.wants_media_volume(t):
        return {"ok": True, "sees_volume": None, "reason": "no_media_volume"}
    host_dir = project_media_dir(node, c.get("project_id"))
    out = {"ok": True, "sees_volume": None, "host_dir": host_dir, "node": node.get("name") or ""}
    rc, so, se = host_exec(node, _probe_cmd(_name(int(vmid)), host_dir), timeout=20)
    f = (so or "").split()
    if rc != 0 or len(f) != 2 or "-" in f:
        out["reason"] = "probe_failed" if rc != 0 else "not_running"
        return out
    out["sees_volume"] = f[0] == f[1]
    return out
