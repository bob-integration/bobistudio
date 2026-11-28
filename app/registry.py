# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Internal image registry (Zot) on the controller — chantier « registre d'images », 2026-11-28.

Images were distributed as FULL tars (docker save → docker load, several GB per node and per update,
no layer shared) or built on every node that runs them. The registry replaces both: an image is
built once, pushed here in OCI format, and nodes pull only the layers they lack. The tar path stays
as the fallback (isolated site, registry down).

Choices made with the operator on 2026-11-28:
  · Zot, not registry:2 (accounts, garbage collection, sync extension for HA);
  · HA by REPLICATION: the standby runs its own Zot that syncs from the active one. With SHARED
    storage (NFS, S3) the sync is pointless and stays off: both controllers serve the same data;
  · OCI media types only, identity by sha256 digest (`app/oci_push.py`).

IDENTITY WITHOUT A PASSWORD. Zot runs TLS with OUR CA and reads the client identity from the URI SAN
of the client certificate. Every certificate our CA signs carries one, fixed by us, never taken from
a CSR (`ca._san_list`, `ca.MARQUEUR_CONTROLEUR`):
  · `bobi://controller/…` → identity `controller` → read + write (the controller pushes);
  · `bobi://node/<id>`    → identity `node`       → read only (a node pulls with its agent's cert);
  · `bobi://container/…`  → no identity            → refused: containers run third-party plugin code.
The server certificate is the controller's own (it already carries the control IPs and the VIP).

Zot is a single static binary: the controller has no Docker, so it runs as a systemd unit, not as a
container. The binary is pinned by version AND sha256; a download that does not match is refused.

STORAGE (`registry_storage_*`): a path the administrator mounts himself (dedicated disk recommended,
NFS, or the system disk) or an S3 bucket. The product never mounts anything. A path declared as a
dedicated disk or NFS must BE a mount point: otherwise a fallen mount would make the registry write
into the empty directory beneath, i.e. on the system disk (`storage_check`).
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import config

log = logging.getLogger(__name__)

ZOT_VERSION = "v2.1.23"
# sha256 of `zot-linux-amd64` as published in the release's checksums.sha256.txt (checked 2026-11-28).
ZOT_SHA256 = "1f6bab0b9eee426d0785de701ff510df5c02bdb8d7287494a582634abfc5d638"
ZOT_URL = "https://github.com/project-zot/zot/releases/download/%s/zot-linux-amd64" % ZOT_VERSION

HOME = getattr(config, "REGISTRY_HOME", "/opt/bobi-registry")    # binary + config, never the data
UNIT_NAME = "bobi-registry.service"
UNIT_PATH = "/etc/systemd/system/" + UNIT_NAME
DEFAULT_PORT = 5500

# Storage kinds, closed vocabulary. `system` = a plain directory on the system disk (allowed, warned).
STORAGE_KINDS = ("dedicated", "nfs", "system", "s3")
MIN_FREE_BYTES = 2 * 1024 ** 3        # below this, a push is refused (an image layer is ~1-3 GB)

# Identities read from the client certificate (cf. module docstring).
URI_SAN_PATTERN = "^bobi://(controller|node)/"
ID_CONTROLLER, ID_NODE = "controller", "node"


def paths():
    return {"bin": os.path.join(HOME, "zot-" + ZOT_VERSION),
            "config": os.path.join(HOME, "config.json"),
            "sync_certs": os.path.join(HOME, "sync-certs"),
            "log": os.path.join(HOME, "zot.log")}


# Settings a standby reads from the ACTIVE controller's database (the staged replica), not from its
# own: a standby's database is frozen until it is promoted, and its write API is refused (409).
REPLICATED_KEYS = ("registry_enabled", "registry_port", "registry_storage_kind",
                   "registry_storage_path", "registry_s3_endpoint", "registry_s3_bucket",
                   "registry_s3_region", "registry_s3_access_key", "registry_s3_secret")


def _replica_settings():
    """Registry settings of the active controller, read from the replica staged on THIS standby.
    → dict, or None when this controller is not a standby or holds no replica (its own then apply).
    2026-11-28, on VMs: the standby could not be configured at all — POST /api/registry/config
    answered 409 and the boot never read anything else than its own frozen settings."""
    from . import ha
    if not ha.is_standby() or not os.path.exists(ha.STAGING_DB):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % ha.STAGING_DB, uri=True)
        try:
            rows = con.execute("SELECT key, value FROM settings WHERE key IN (%s)"
                               % ",".join("?" * len(REPLICATED_KEYS)), REPLICATED_KEYS).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning("registry: replica unreadable, own settings used (%s)", e)
        return None
    out = {}
    for k, val in rows:
        try:
            out[k] = json.loads(val)
        except ValueError:
            continue
    return out


def settings_values():
    from . import settings
    rep = _replica_settings()
    g = settings.get if rep is None else rep.get
    return {"enabled": str(g("registry_enabled") or "").lower() in ("1", "true", "on", "yes"),
            "port": int(g("registry_port") or DEFAULT_PORT),
            "kind": str(g("registry_storage_kind") or "dedicated").strip().lower(),
            "path": str(g("registry_storage_path") or "").strip(),
            "s3_endpoint": str(g("registry_s3_endpoint") or "").strip(),
            "s3_bucket": str(g("registry_s3_bucket") or "").strip(),
            "s3_region": str(g("registry_s3_region") or "us-east-1").strip(),
            "s3_access_key": str(g("registry_s3_access_key") or "").strip(),
            "s3_secret": str(g("registry_s3_secret") or "")}


# ── Storage ─────────────────────────────────────────────────────────────────────────────────────
def storage_check(kind, path, min_free=0):
    """Can the registry write its data at `path`? → dict(ok, code, free, total, pct, same_disk_as_root).

    `code` (closed vocabulary, i18n `registry.storage.<code>`):
      ok · unset · unknown_kind · missing · not_mounted · not_writable · full
    `same_disk_as_root` is a WARNING, never a refusal, for kind `system`; for `dedicated` and `nfs`
    it is the very failure `not_mounted` names (the mount fell, the directory beneath is on /)."""
    out = {"ok": False, "code": "unset", "free": None, "total": None, "pct": None,
           "same_disk_as_root": None}
    if kind not in STORAGE_KINDS:
        out["code"] = "unknown_kind"
        return out
    if kind == "s3":
        out.update(ok=True, code="ok")         # checked by Zot itself at start (bucket, keys)
        return out
    if not path:
        return out
    if not os.path.isabs(path) or not os.path.isdir(path):
        out["code"] = "missing"
        return out
    try:
        same = os.stat(path).st_dev == os.stat("/").st_dev
    except OSError:
        out["code"] = "missing"
        return out
    out["same_disk_as_root"] = same
    if kind in ("dedicated", "nfs") and (same or not _under_a_mount(path)):
        out["code"] = "not_mounted"
        return out
    if not os.access(path, os.W_OK | os.X_OK):
        out["code"] = "not_writable"
        return out
    try:
        st = os.statvfs(path)
        out["free"] = st.f_bavail * st.f_frsize
        out["total"] = st.f_blocks * st.f_frsize
        out["pct"] = round(100.0 * (1 - st.f_bavail / st.f_blocks), 1) if st.f_blocks else None
    except OSError:
        out["code"] = "not_writable"
        return out
    if min_free and (out["free"] or 0) < min_free:
        out["code"] = "full"
        return out
    out.update(ok=True, code="ok")
    return out


def _under_a_mount(path):
    """True when `path` or one of its parents below / is a mount point."""
    p = os.path.realpath(path)
    while p not in ("/", ""):
        if os.path.ismount(p):
            return True
        p = os.path.dirname(p)
    return False


# ── Zot configuration ───────────────────────────────────────────────────────────────────────────
def render_config(v, tls, role="active", peer_url=None, storage_dir=None):
    """Zot config as a dict. `tls` = {cert, key, ca} paths (the controller's own certificate).
    `role` = HA role; a STANDBY syncs everything from `peer_url` unless the storage is shared."""
    storage = {"rootDirectory": storage_dir or v["path"], "dedupe": True, "gc": True,
               "gcDelay": "1h", "gcInterval": "24h"}
    if v["kind"] == "s3":
        # Zot keeps its metadata (boltdb) under rootDirectory even with S3: a local directory.
        storage["rootDirectory"] = storage_dir or os.path.join(HOME, "s3-meta")
        storage["dedupe"] = False                  # S3 has no hard links
        storage["storageDriver"] = {
            "name": "s3", "regionendpoint": v["s3_endpoint"], "region": v["s3_region"],
            "bucket": v["s3_bucket"], "rootdirectory": "/bobi-registry",
            "accesskey": v["s3_access_key"], "secretkey": v["s3_secret"],
            "forcepathstyle": True, "secure": v["s3_endpoint"].startswith("https")}
    cfg = {
        "distSpecVersion": "1.1.1",
        "storage": storage,
        "http": {
            "address": "0.0.0.0", "port": str(v["port"]), "realm": "bobi",
            "tls": {"cert": tls["cert"], "key": tls["key"], "cacert": tls["ca"]},
            "auth": {"mtls": {"identityAttributes": ["URI"], "uriSanPattern": URI_SAN_PATTERN}},
            "accessControl": {"repositories": {"**": {
                "policies": [
                    {"users": [ID_CONTROLLER], "actions": ["read", "create", "update", "delete"]},
                    {"users": [ID_NODE], "actions": ["read"]}],
                "defaultPolicy": []}}},
        },
        "log": {"level": "info", "output": paths()["log"]},
    }
    if role == "standby" and peer_url and shared_storage(v) is False:
        cfg["extensions"] = {"sync": {"enable": True, "registries": [{
            "urls": [peer_url], "onDemand": False, "pollInterval": "5m", "tlsVerify": True,
            "certDir": paths()["sync_certs"], "maxRetries": 3, "retryDelay": "1m",
            "content": [{"prefix": "**"}]}]}}
    return cfg


def shared_storage(v):
    """Both controllers see the same data (NFS, S3) → no sync needed. Local disk → False."""
    return v["kind"] in ("nfs", "s3")


def peer_registry_url(v):
    """URL of the ACTIVE controller's registry, for the standby's sync. None if unknown."""
    try:
        from . import ha
        u = ha.peer_url() or ""
    except Exception:
        u = ""
    if not u:
        return None
    from urllib.parse import urlparse
    host = urlparse(u).hostname
    return "https://%s:%d" % (host, v["port"]) if host else None


# ── Binary ──────────────────────────────────────────────────────────────────────────────────────
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def binary_ok():
    p = paths()["bin"]
    return os.path.isfile(p) and _sha256(p) == ZOT_SHA256


def install_binary(source=None, timeout=600):
    """Put the pinned Zot binary in place. `source`: a local file (offline package) or None to
    download. Refused unless its sha256 is ZOT_SHA256. → (ok, message)."""
    if binary_ok():
        return True, "present"
    os.makedirs(HOME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zot-", dir=HOME)
    os.close(fd)
    try:
        if source:
            shutil.copyfile(source, tmp)
        else:
            import requests
            with requests.get(ZOT_URL, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    return False, "download failed: HTTP %d" % r.status_code
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
        got = _sha256(tmp)
        if got != ZOT_SHA256:
            return False, "sha256 mismatch: got %s, expected %s" % (got, ZOT_SHA256)
        os.chmod(tmp, 0o755)
        os.replace(tmp, paths()["bin"])
        return True, "installed"
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── Service ─────────────────────────────────────────────────────────────────────────────────────
def render_unit():
    # TimeoutStopSec: Zot on NFS ignored SIGTERM for the whole 90 s default, holding the data lock
    # the promoted controller needed (2026-11-28, VMs).
    p = paths()
    return ("[Unit]\nDescription=Bobi.Studio image registry (Zot %s)\nAfter=network-online.target\n"
            "Wants=network-online.target\n\n[Service]\nExecStart=%s serve %s\nRestart=on-failure\n"
            "RestartSec=5\nTimeoutStopSec=15\nLimitNOFILE=65536\n\n[Install]\nWantedBy=multi-user.target\n"
            % (ZOT_VERSION, p["bin"], p["config"]))


def _run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _sync_certs():
    """Client certificate the standby presents to the active registry (Zot's certDir layout)."""
    from . import ca
    cert, key, cacert = ca.controller_client_files()
    d = paths()["sync_certs"]
    os.makedirs(d, mode=0o700, exist_ok=True)
    for src, name, mode in ((cert, "client.cert", 0o644), (key, "client.key", 0o600),
                            (cacert, "ca.crt", 0o644)):
        with open(src) as f:
            _write(os.path.join(d, name), f.read(), mode)


def apply():
    """Write config + unit, verify, (re)start. Refuses when the storage is not usable. → (ok, msg)."""
    from . import ca, ha
    v = settings_values()
    if not v["enabled"]:
        return False, "disabled"
    if ha.role() == "standby" and shared_storage(v):
        # Two Zot on the same data would both write its metadata: the standby serves nothing
        # until it is promoted, then starts on the very same data.
        stop()
        return True, "standby on shared storage: stopped until promotion"
    st = storage_check(v["kind"], v["path"])
    if not st["ok"]:
        return False, "storage: " + st["code"]
    if not ca.ca_available():
        return False, "no CA"
    _ensure_certificate_addresses()
    ok, msg = install_binary()
    if not ok:
        return False, "binary: " + msg
    cert, key, cacert = ca.controller_client_files()
    role = ha.role()
    peer = peer_registry_url(v) if role == "standby" else None
    cfg = render_config(v, {"cert": cert, "key": key, "ca": cacert}, role=role, peer_url=peer)
    if "extensions" in cfg:
        _sync_certs()
    if v["kind"] == "s3":
        os.makedirs(cfg["storage"]["rootDirectory"], exist_ok=True)
    p = paths()
    _write(p["config"], json.dumps(cfg, indent=1), 0o600)      # may hold the S3 secret
    rc, out = _run([p["bin"], "verify", p["config"]])
    if rc != 0:
        return False, "config refused by zot: " + out.strip()[-300:]
    _write(UNIT_PATH, render_unit())
    _run(["systemctl", "daemon-reload"])
    rc, out = _run(["systemctl", "enable", UNIT_NAME])
    rc, out = _run(["systemctl", "restart", UNIT_NAME])
    if rc != 0:
        return False, "systemctl restart: " + out.strip()[-300:]
    # « started » only once Zot answers: on NFS it took 27 s (boltdb), and a publication made in
    # between fell back to the tar without a word (2026-11-28, VMs).
    deadline = time.time() + READY_TIMEOUT_S
    while True:
        ok, detail = health(url=own_url(v))
        if ok:
            return True, "started"
        if time.time() > deadline:
            return False, "started, but not answering after %d s: %s" % (READY_TIMEOUT_S, detail)
        time.sleep(2)


READY_TIMEOUT_S = 90


def _local_ipv4():
    """Every global IPv4 address of this machine."""
    rc, out = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    if rc != 0:
        return []
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] == "inet":
            ips.append(parts[3].split("/")[0])
    return ips


def _ensure_certificate_addresses():
    """Put every local address (and the VIP) in this controller's certificate. The standby's sync
    reaches the active one by the address of the HA pairing, and Zot checks it against the
    certificate — which carried only the address of the default route and the VIP. 2026-11-28 on
    VMs: « certificate is valid for <default route>, <VIP>, not <pairing address> » every two
    minutes, nothing synced, no alert. Zot watches its certificate file and reloads it."""
    from . import ca, vip
    ips = _local_ipv4()
    if vip.ip_certificat():
        ips.append(vip.ip_certificat())
    try:
        if ca.assurer_certificat_controleur(ips=ips):
            vip.recharger_ecoutes()
    except Exception as e:                                       # noqa: BLE001
        log.error("registry: controller certificate not updated with %s: %s", ips, e)


_reconcile_lock = threading.Lock()
_reconciled = {"sig": None}


def reconcile(force=False):
    """Bring the local Zot in line with the settings AND the HA role, in both roles: at boot, and
    on a standby after each replica it receives (the active's settings may have changed).
    Active → serve; standby on local storage → sync from the active; standby on shared storage
    (NFS, S3) → stopped, or the promoted one could not open the data (boltdb lock: 2026-11-28,
    the new active's Zot looped on « boltdb file is already in use » while the demoted one kept
    running). Disabled → stopped. → (ok, msg), or (None, "unchanged") when nothing moved."""
    from . import ha
    with _reconcile_lock:
        v = settings_values()
        sig = json.dumps([v, ha.role(), ha.peer_url()], sort_keys=True)
        if not force and sig == _reconciled["sig"]:
            return None, "unchanged"
        if v["enabled"]:
            ok, msg = apply()
        elif os.path.exists(UNIT_PATH):
            ok, msg = stop()
        else:
            ok, msg = True, "disabled"
        if ok:
            _reconciled["sig"] = sig
        return ok, msg


def stop():
    _run(["systemctl", "disable", "--now", UNIT_NAME])
    return True, "stopped"


def local_url(v=None):
    """The controller talks to its own registry through the address the NODES use, never
    127.0.0.1: TLS checks the name, and the controller certificate carries the control IPs and
    the VIP, not the loopback (2026-11-28: the first real start answered nothing on 127.0.0.1)."""
    v = v or settings_values()
    return "https://" + (address(v) or "127.0.0.1:%d" % v["port"])


def own_url(v=None):
    """THIS controller's registry, never the VIP: on a standby the VIP leads to the active one, so
    a health check through it described the other machine. The control IP is in the certificate."""
    from . import ca
    v = v or settings_values()
    return "https://%s:%d" % (ca._detect_control_ip() or "127.0.0.1", v["port"])


def health(timeout=5, url=None):
    """GET /v2/ with the controller's certificate. → (ok, detail). `url` defaults to the address
    the nodes use (what a publication goes through)."""
    import requests
    from . import ca
    cert, key, cacert = ca.controller_client_files()
    try:
        r = requests.get((url or local_url()) + "/v2/", cert=(cert, key), verify=cacert, timeout=timeout)
        return r.status_code == 200, "HTTP %d" % r.status_code
    except Exception as e:
        return False, str(e)[:200]


def status():
    v = settings_values()
    st = storage_check(v["kind"], v["path"])
    active = _run(["systemctl", "is-active", UNIT_NAME])[1].strip() if os.path.exists(UNIT_PATH) else "absent"
    from . import ha
    ok, detail = (health(url=own_url(v) if ha.is_standby() else None) if active == "active"
                  else (False, active))
    return {"enabled": v["enabled"], "version": ZOT_VERSION, "port": v["port"], "kind": v["kind"],
            "path": v["path"], "storage": st, "service": active, "healthy": ok, "detail": detail,
            "shared_storage": shared_storage(v)}


# ── Address seen by the nodes ───────────────────────────────────────────────────────────────────
def address(v=None):
    """`host:port` the nodes pull from. The VIP when one is set (it follows the active controller,
    and the controller certificate carries it), else `registry_host`, else the control IP.
    The host must be in the controller certificate's SANs: Docker checks it."""
    from . import settings
    v = v or settings_values()
    host = ""
    try:
        from . import vip
        host = vip.ip_certificat() or ""
    except Exception:
        host = ""
    if not host:
        host = str(settings.get("registry_host") or "").strip()
    if not host:
        try:
            from . import ca
            host = ca._detect_control_ip() or ""
        except Exception:
            host = ""
    return "%s:%d" % (host, v["port"]) if host else None


def session():
    """requests Session that speaks to the registry as the controller (write identity)."""
    import requests
    from . import ca
    cert, key, cacert = ca.controller_client_files()
    s = requests.Session()
    s.cert, s.verify = (cert, key), cacert
    return s


def usable():
    """Registry enabled AND answering: the only case where distribution goes through it."""
    v = settings_values()
    if not v["enabled"]:
        return False
    return health()[0]


# ── Publication record: what each tag IS (the intent the nodes are compared to) ─────────────────
PUBLISHED_KEY = "registry_published"       # {tag: {"digest", "ts", "size"}}


def published():
    from . import settings
    d = settings.get(PUBLISHED_KEY)
    return d if isinstance(d, dict) else {}


def expected_digest(tag):
    return (published().get(tag) or {}).get("digest")


def _record(tag, digest, size, manifest=None, platforms=None):
    import time
    from . import settings
    d = dict(published())
    d[tag] = {"digest": digest, "ts": int(time.time()), "size": int(size)}
    if manifest:
        d[tag]["manifest"] = manifest
    if platforms:
        d[tag]["platforms"] = list(platforms)
    settings.set(PUBLISHED_KEY, d)


def holds_published(tag, got):
    """Does a node digest `got` designate what was published for `tag`? The tag's identity is
    the INDEX digest; a record from before the index (2026-11-28 morning) is a manifest digest."""
    rec = published().get(tag) or {}
    return bool(got) and got in {rec.get("digest"), rec.get("manifest")} - {None}


def platforms(tag):
    """Platforms published for `tag` (["linux/amd64"]), or None when not recorded (older record)."""
    return (published().get(tag) or {}).get("platforms")


_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_node_arch = {}


def node_arch(node):
    """OCI architecture of a node (`uname -m`, read once per node), or None if unreadable."""
    nid = node.get("id")
    if nid in _node_arch:
        return _node_arch[nid]
    from . import node_driver
    rc, out, _err = node_driver.host_exec(node, "uname -m", timeout=15)
    a = _ARCH.get(out.strip(), out.strip() or None) if rc == 0 else None
    if a:
        _node_arch[nid] = a
    return a


def missing_platform(node, tag):
    """The node's architecture when `tag` was published WITHOUT it, else None. Said before any
    pull: an amd64-only image on an arm64 node is not a network failure, and the archive fallback
    would only load the wrong architecture."""
    plats = platforms(tag)
    if not plats:
        return None
    a = node_arch(node)
    if a and not any(p.split("/")[1:2] == [a] for p in plats):
        return a
    return None


def publish_tar(tag, tar_path, progress=None):
    """`docker save` tar → OCI image in the registry. Refused when the storage lacks room.
    → (ok, digest or message)."""
    v = settings_values()
    st = storage_check(v["kind"], v["path"])
    if not st["ok"]:
        return False, "storage: " + st["code"]
    work = tempfile.mkdtemp(prefix="bobi-oci-", dir=os.path.dirname(tar_path) or None)
    try:
        from . import oci_push as op
        img = op.build(tar_path, work, tag, progress)
        need = sum(l["size"] for l in img["layers"])
        if v["kind"] != "s3":
            st = storage_check(v["kind"], v["path"], min_free=need + MIN_FREE_BYTES)
            if not st["ok"]:
                return False, "storage: %s (%.1f GB needed)" % (st["code"], need / 1e9)
        repo, ref = op.split_ref(tag)
        digest = op.push(img, local_url(v), repo, ref, session(), progress)
        _record(tag, digest, need, manifest=img["manifest_digest"],
                platforms=[op.platform_name(img["platform"])])
        _push_standby(img, repo, ref, v)
        return True, digest
    except Exception as e:
        return False, str(e)[:300]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def standby_registry_url(v=None):
    """On the ACTIVE controller with its own storage: the standby's registry. None otherwise."""
    from . import ha
    v = v or settings_values()
    if shared_storage(v) or not ha.is_active():
        return None
    return peer_registry_url(v)


def _push_standby(img, repo, ref, v):
    """Copy a fresh publication to the standby right away (the controller identity may write
    there). Zot's sync took 6 to 28 min for two small images (2026-11-28) and stays as the
    catch-up path. A failure is logged only: watch_standby alerts if the copy is still missing."""
    url = standby_registry_url(v)
    if not url:
        return
    from . import oci_push as op
    try:
        op.push(img, url, repo, ref, session())
    except Exception as e:
        log.warning("registry: copy of %s:%s to the standby failed: %s", repo, ref, e)


def standby_lag(v=None):
    """Published tags the standby cannot serve in full, checked manifest by manifest and blob by
    blob → {tag: [what is missing]}. None when there is no standby to check."""
    url = standby_registry_url(v)
    if not url:
        return None
    from . import oci_push as op
    s = session()
    out = {}
    for tag, rec in published().items():
        repo, ref = op.split_ref(tag)
        try:
            miss = op.missing(url, repo, ref, s, rec.get("digest"))
        except Exception as e:
            miss = ["unreachable: %s" % str(e)[:120]]
        if miss:
            out[tag] = miss
    return out


STANDBY_GRACE_S = 900          # the sync's catch-up window before a missing copy is an alert
_standby_state = {"checked": None, "lag": None, "alerted": False}


def watch_standby_once(now=None):
    """One pass: alert once when a tag published more than STANDBY_GRACE_S ago is still not
    pullable from the standby; an info line when it catches up."""
    from .database import db_add_alert
    now = now or time.time()
    lag = standby_lag()
    _standby_state.update(checked=now, lag=lag)
    pub = published()
    late = sorted(t for t in (lag or {})
                  if now - float((pub.get(t) or {}).get("ts") or 0) > STANDBY_GRACE_S)
    if late and not _standby_state["alerted"]:
        db_add_alert("alert.registre.secours_incomplet", "warning", kind="node",
                     params={"n": len(late), "tags": ", ".join(late[:5])})
        _standby_state["alerted"] = True
    elif not late and _standby_state["alerted"]:
        db_add_alert("alert.registre.secours_complet", "info", kind="node", params={})
        _standby_state["alerted"] = False
    return late


# ── Node side (through the agent's host exec: no agent upgrade needed) ──────────────────────────
NODE_TLS_DIR = "/etc/bobi-node-agent/tls"


def node_prepare(node, addr=None):
    """Make Docker on `node` trust the registry and present the node's OWN agent certificate
    (identity `bobi://node/<id>` → read only). No key material leaves the node: client.cert and
    client.key are links to the agent's files. → (ok, message)."""
    from . import ca, node_driver
    addr = addr or address()
    if not addr:
        return False, "registry address unknown"
    script = ("set -e; d=/etc/docker/certs.d/%s; test -s %s/node.key && test -s %s/node.crt "
              "|| { echo 'agent certificate missing (node not on mTLS)'; exit 3; }; "
              "mkdir -p \"$d\"; cat > \"$d/ca.crt\"; ln -sf %s/node.crt \"$d/client.cert\"; "
              "ln -sf %s/node.key \"$d/client.key\"" % (addr, NODE_TLS_DIR, NODE_TLS_DIR,
                                                       NODE_TLS_DIR, NODE_TLS_DIR))
    rc, out, err = node_driver.host_exec(node, script, input_data=ca.ca_cert_pem().decode(), timeout=30)
    return rc == 0, (out + err).strip()[-300:] or "ok"


def node_pull(node, tag, digest=None, addr=None, timeout=1800):
    """Pull `tag` BY DIGEST on `node`, tag it under its usual local name (the rest of the product
    runs `docker run <tag>`), then CHECK what the node holds. → (ok, message)."""
    from . import node_driver
    from .oci_push import split_ref
    addr = addr or address()
    digest = digest or expected_digest(tag)
    if not (addr and digest):
        return False, "nothing published for %s" % tag
    repo, _ref = split_ref(tag)
    remote = "%s/%s@%s" % (addr, repo, digest)
    ok, msg = node_prepare(node, addr)
    if not ok:
        return False, "prepare: " + msg
    script = ("set -e; docker pull -q %s >/dev/null; docker tag %s %s; "
              "docker image inspect --format '{{json .RepoDigests}}' %s" % (remote, remote, tag, tag))
    rc, out, err = node_driver.host_exec(node, script, timeout=timeout)
    if rc != 0:
        return False, (err or out).strip()[-300:]
    if remote not in out:
        return False, "pulled, but %s does not carry %s" % (tag, digest)
    return True, digest


def node_digest(node, tag, addr=None):
    """Digest `tag` has on `node` (from the registry), None if absent or loaded from a tar."""
    from . import node_driver
    from .oci_push import split_ref
    addr = addr or address()
    if not addr:
        return None
    repo, _ref = split_ref(tag)
    rc, out, _err = node_driver.host_exec(
        node, "docker image inspect --format '{{json .RepoDigests}}' %s 2>/dev/null" % tag, timeout=20)
    if rc != 0:
        return None
    try:
        for rd in json.loads(out.strip() or "[]"):
            if rd.startswith("%s/%s@" % (addr, repo)):
                return rd.split("@", 1)[1]
    except ValueError:
        return None
    return None


def remote_digest(tag):
    """Digest the registry holds for `tag`, or None."""
    from .oci_push import remote_digest as _rd, split_ref
    repo, ref = split_ref(tag)
    try:
        return _rd(local_url(), repo, ref, session())
    except Exception:
        return None


# ── Storage watch: same thresholds and hysteresis as a node's disks (node_health) ───────────────
def watch_storage(period=600):
    """Loop: alert when the registry storage passes 85 % / 95 % (node_health_disk_* settings),
    once per episode, back to normal 5 points under the warning threshold. Never returns."""
    import time
    from . import settings
    from .database import db_add_alert
    prev = None
    while True:
        v = settings_values()
        if v["enabled"] and v["kind"] != "s3":
            st = storage_check(v["kind"], v["path"])
            warn = float(settings.get("node_health_disk_warn_pct") or 85.0)
            err = float(settings.get("node_health_disk_err_pct") or 95.0)
            pct = st.get("pct")
            if not st["ok"]:
                level = "error"
            elif pct is None:
                level = None
            else:
                level = "error" if pct >= err else ("warning" if pct >= warn else None)
            if level and level != prev:
                db_add_alert("alert.registre.stockage", level, kind="disk",
                             params={"path": v["path"], "pct": pct, "code": st["code"]})
                prev = level
            elif not level and prev and (pct is None or pct <= warn - 5):
                db_add_alert("alert.registre.stockage_retabli", "info", kind="disk",
                             params={"path": v["path"], "pct": pct})
                prev = None
        if v["enabled"]:
            try:
                watch_standby_once()
            except Exception as e:
                log.warning("registry: standby check failed: %s", e)
        time.sleep(period)


def versions(repo):
    """Every tag the registry keeps for `repo`, with its digest and when WE published it.
    → [{tag, digest, ts}] newest first (by publication time, unknown ones last)."""
    from .oci_push import list_tags, remote_digest as _rd
    s, base = session(), local_url()
    pub = published()
    out = []
    for t in list_tags(base, repo, s):
        full = "%s:%s" % (repo, t)
        out.append({"tag": full, "digest": _rd(base, repo, t, s),
                    "ts": (pub.get(full) or {}).get("ts")})
    out.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return out
