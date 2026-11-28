#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Image registry foundation (2026-11-28), offline: no network, no Zot, throwaway files.
  1. OCI publication (`app/oci_push.py`): both `docker save` layouts give the SAME digest, OCI media
     types only, layers compressed deterministically (same layer → same digest), OCI layout export;
  2. Zot config (`app/registry.render_config`): identity from the URI SAN, controller read+write,
     node read only, nothing anonymous; sync only on a STANDBY with LOCAL storage; S3 driver;
  3. storage (`registry.storage_check`): a « dedicated » path that is not a mount is REFUSED (a
     fallen mount would write on the system disk); « system » is accepted with the warning;
  4. the Zot binary is refused unless its sha256 is the pinned one.

    ./venv/bin/python tests/verif_registre_images.py
"""
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="verif_registre_")
from app import config, database                                              # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "t.db")
database._tls.__dict__.pop("conn", None)
database.init_db()
from app import oci_push as op, registry                                       # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{str(detail)[:300]}]"))
    if not cond:
        ko += 1


def layer(files):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for n, d in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
    return b.getvalue()


L1, L2 = layer({"etc/os": b"debian\n" * 500}), layer({"usr/bin/x": bytes(range(256)) * 400})
DIFF = ["sha256:" + hashlib.sha256(x).hexdigest() for x in (L1, L2)]
CFG = json.dumps({"architecture": "amd64", "os": "linux",
                  "rootfs": {"type": "layers", "diff_ids": DIFF}}).encode()


def save(path, oci):
    with tarfile.open(path, "w") as t:
        def add(n, d):
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
        if oci:
            ls = []
            for x in (L1, L2):
                h = hashlib.sha256(x).hexdigest()
                add("blobs/sha256/" + h, x)
                ls.append({"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": "sha256:" + h,
                           "size": len(x)})
            ch = hashlib.sha256(CFG).hexdigest()
            add("blobs/sha256/" + ch, CFG)
            man = json.dumps({"schemaVersion": 2, "mediaType": op.MT_MANIFEST, "layers": ls,
                              "config": {"mediaType": op.MT_CONFIG, "digest": "sha256:" + ch,
                                         "size": len(CFG)}}).encode()
            mh = hashlib.sha256(man).hexdigest()
            add("blobs/sha256/" + mh, man)
            add("index.json", json.dumps({"schemaVersion": 2, "manifests": [
                {"mediaType": op.MT_MANIFEST, "digest": "sha256:" + mh, "size": len(man)}]}).encode())
        else:
            add("a/layer.tar", L1)
            add("b/layer.tar", L2)
            add("cfg.json", CFG)
            add("manifest.json", json.dumps([{"Config": "cfg.json", "RepoTags": ["bobi-test:1.0"],
                                              "Layers": ["a/layer.tar", "b/layer.tar"]}]).encode())


print("1. OCI publication")
save(TMP + "/legacy.tar", False)
save(TMP + "/oci.tar", True)
i1 = op.build(TMP + "/legacy.tar", TMP, "bobi-test:1.0")
i2 = op.build(TMP + "/oci.tar", TMP, "bobi-test:1.0")
verifie(i1["digest"] == i2["digest"], "★ same digest from both docker save layouts", (i1["digest"], i2["digest"]))
man = json.loads(i1["manifest"])
verifie(man["mediaType"] == op.MT_MANIFEST and man["config"]["mediaType"] == op.MT_CONFIG
        and {l["mediaType"] for l in man["layers"]} == {op.MT_LAYER_GZ}, "★ OCI media types only")
verifie("vnd.docker" not in i1["manifest"].decode(), "no Docker media type anywhere")
idx1 = json.loads(i1["index"])
verifie(i1["digest"] == "sha256:" + hashlib.sha256(i1["index"]).hexdigest() and idx1["mediaType"] == op.MT_INDEX,
        "★ the identity is an image INDEX (multi-architecture ready), digest = sha256 of the index")
verifie(idx1["manifests"] == [{"mediaType": op.MT_MANIFEST, "digest": i1["manifest_digest"], "size": len(i1["manifest"]),
                               "platform": {"os": "linux", "architecture": "amd64"}}],
        "★ one entry per platform, read from the image config (linux/amd64)", idx1["manifests"])
verifie(i1["manifest_digest"] == "sha256:" + hashlib.sha256(i1["manifest"]).hexdigest(), "manifest digest")
i3 = op.build(TMP + "/legacy.tar", TMP, "bobi-test:1.0")
verifie([l["digest"] for l in i3["layers"]] == [l["digest"] for l in i1["layers"]],
        "★ deterministic compression: the same layer gives the same digest")
op.write_layout(i1, TMP + "/layout.tar", "bobi-test:1.0")
with tarfile.open(TMP + "/layout.tar") as t:
    names = set(t.getnames())
    idx = json.loads(t.extractfile("index.json").read())
verifie({"oci-layout", "index.json"} <= names and idx["manifests"][0]["digest"] == i1["digest"]
        and "blobs/sha256/" + i1["digest"][7:] in names and "blobs/sha256/" + i1["manifest_digest"][7:] in names,
        "OCI image layout export (oci-layout + index.json → image index → manifest)")
verifie("manifest.json" in names, "★ plus Docker's compatibility manifest: `docker load` (Docker 26) reads only that")
with tarfile.open(TMP + "/layout.tar") as t:
    dm = json.loads(t.extractfile("manifest.json").read())[0]
verifie(dm["RepoTags"] == ["bobi-test:1.0"] and all(x in names for x in [dm["Config"]] + dm["Layers"]),
        "the compatibility manifest points at the OCI blobs")
verifie(op.split_ref("bobi-mtl:0.108.6") == ("bobi-mtl", "0.108.6"), "tag split")

print("\n1b. offline package: images embedded in OCI form, with their digest")
from app import offline_bundle as ob                                           # noqa: E402
from app.routes import images as _im0                                          # noqa: E402
ob.IMAGES_DIR = os.path.join(TMP, "vendor-images")
_mat0, _tag0 = _im0._materialize_image_tar, _im0._image_tag
_im0._materialize_image_tar = lambda which, tag, dest, build_node=None: (save(dest, False) or (True, "ok"))
_im0._image_tag = lambda which: "bobi-test:1.0"
res = ob.ensure_images(["compute"])
_im0._materialize_image_tar, _im0._image_tag = _mat0, _tag0
ent = (res.get("images") or [{}])[0]
with tarfile.open(os.path.join(ob.IMAGES_DIR, ent.get("file", "x"))) as t:
    nm = set(t.getnames())
verifie(res.get("ok") and {"oci-layout", "index.json", "manifest.json"} <= nm, "★ the embedded image is an OCI layout", sorted(nm)[:4])
verifie(ent.get("digest") == i1["digest"], "★ with the SAME digest the registry publishes", ent.get("digest"))

print("\n2. Zot configuration")
V = dict(enabled=True, kind="dedicated", path="/srv/reg", port=5500, s3_endpoint="", s3_bucket="",
         s3_region="", s3_access_key="", s3_secret="")
TLS = {"cert": "/t/c.crt", "key": "/t/c.key", "ca": "/t/ca.crt"}
c = registry.render_config(V, TLS)
ac = c["http"]["accessControl"]["repositories"]["**"]
pol = {u: set(p["actions"]) for p in ac["policies"] for u in p["users"]}
verifie(c["http"]["auth"]["mtls"]["identityAttributes"] == ["URI"]
        and c["http"]["auth"]["mtls"]["uriSanPattern"] == "^bobi://(controller|node)/",
        "★ identity read from the URI SAN our CA fixes")
verifie(pol.get("node") == {"read"} and "create" in pol.get("controller", set()) and ac["defaultPolicy"] == [],
        "★ controller writes, node only reads, nothing anonymous", (pol, ac["defaultPolicy"]))
verifie(c["http"]["tls"]["cacert"] == "/t/ca.crt", "client certificates checked against our CA")
verifie("extensions" not in c, "the active registry does not sync")
sb = registry.render_config(V, TLS, role="standby", peer_url="https://10.0.0.1:5500")
verifie((sb.get("extensions") or {}).get("sync", {}).get("registries", [{}])[0].get("urls") == ["https://10.0.0.1:5500"],
        "★ a standby with local storage syncs from the active one")
sb_nfs = registry.render_config(dict(V, kind="nfs"), TLS, role="standby", peer_url="https://10.0.0.1:5500")
verifie("extensions" not in sb_nfs, "shared storage: no sync")
s3 = registry.render_config(dict(V, kind="s3", s3_endpoint="https://minio:9000", s3_bucket="b",
                                 s3_access_key="AK", s3_secret="SK"), TLS)
verifie(s3["storage"]["storageDriver"]["name"] == "s3" and s3["storage"]["dedupe"] is False, "S3 driver, no dedupe")
from app import settings                                                       # noqa: E402
verifie(settings._is_secret_key("registry_s3_secret"), "the S3 secret is a secret for settings.public()")

registry.address = lambda v=None: "10.0.0.5:5500"
verifie(registry.local_url(V) == "https://10.0.0.5:5500",
        "★ the controller reaches its registry by the address in its certificate, not the loopback")
import importlib                                                               # noqa: E402
importlib.reload(registry)

print("\n3. storage")
# A directory ON THE SYSTEM DISK (/var/tmp is on / here; /tmp may be a tmpfs, which would hide the case).
d = tempfile.mkdtemp(prefix="verif_registre_", dir="/var/tmp")
try:
    same_root = os.stat(d).st_dev == os.stat("/").st_dev
    st = registry.storage_check("dedicated", d)
    verifie(not same_root or (st["code"] == "not_mounted" and not st["ok"]),
            "★ « dedicated » on the system disk is refused (a fallen mount)", st)
    verifie(not same_root or registry.storage_check("nfs", d)["code"] == "not_mounted", "same for « nfs »")
    st = registry.storage_check("system", d)
    verifie(st["ok"] and st["same_disk_as_root"] == same_root, "« system » accepted, same-disk flag given", st)
finally:
    shutil.rmtree(d, ignore_errors=True)
if os.path.ismount("/dev/shm") and os.access("/dev/shm", os.W_OK):
    verifie(registry.storage_check("dedicated", "/dev/shm")["ok"], "a real mount point is accepted")
d = tempfile.mkdtemp(prefix="reg_", dir=TMP)
verifie(registry.storage_check("system", d + "/absent")["code"] == "missing", "missing path refused")
verifie(registry.storage_check("system", "")["code"] == "unset", "empty path refused")
verifie(registry.storage_check("system", d, min_free=1 << 62)["code"] == "full", "not enough free space refused")
verifie(registry.storage_check("x", d)["code"] == "unknown_kind", "unknown kind refused")

print("\n4. the binary is pinned")
registry.HOME = tempfile.mkdtemp(prefix="home_", dir=TMP)
# The LIVE controller certificate is never re-issued from here: apply() adds the local addresses
# to it, and ca.paths() is not redirected.
registry._ensure_certificate_addresses = lambda: None
# The LIVE registry's unit and systemctl are never reached from here (2026-11-28: a leaked stub let
# apply() run, rewrite /etc/systemd/system/bobi-registry.service to this test's /tmp and restart it).
registry.UNIT_PATH = os.path.join(registry.HOME, "bobi-registry.service")
_real_run = registry._run
registry._run = lambda cmd, timeout=60: ((1, "systemctl refused in tests") if cmd[0] == "systemctl"
                                        else _real_run(cmd, timeout))
bad = os.path.join(TMP, "fake-zot")
open(bad, "wb").write(b"#!/bin/sh\necho not zot\n")
ok, msg = registry.install_binary(source=bad)
verifie(not ok and "sha256" in msg and not os.path.exists(registry.paths()["bin"]),
        "★ a binary with another sha256 is refused", msg)

print("\n5. distribution: registry first, the tar when it fails, and a failure is said")
from app.routes import images as im                                            # noqa: E402
from app import node_driver                                                    # noqa: E402
calls = []
registry.node_pull = lambda node, tag, digest: (calls.append(("pull", digest)) or (False, "x509: unknown authority"))
node_driver.load_image_file = lambda node, tag, path: (calls.append(("tar", path)) or (True, tag))
NODE = {"id": 1, "name": "n1"}
ok, m = im._deliver(NODE, "bobi-test:1.0", "/tmp/x.tar", "sha256:abc")
verifie(ok and calls == [("pull", "sha256:abc"), ("tar", "/tmp/x.tar")], "★ a failed pull falls back to the tar", calls)
with database.get_db() as db:
    a = db.execute("SELECT id FROM alerts WHERE msg_key='alert.image.registre_tirage'").fetchall()
verifie(len(a) == 1, "★ and the fallback is SAID (alert)", len(a))
calls.clear()
im._deliver(NODE, "bobi-test:1.0", "/tmp/x.tar", None)
verifie(calls == [("tar", "/tmp/x.tar")], "nothing published: the tar, as before the registry", calls)
registry.usable = lambda: False
verifie(im._publish_to_registry("bobi-test:1.0", "/tmp/x.tar") is None, "registry off: nothing published")

print("\n5b. node_only images: built ONCE, pulled by the others")
import app.registry as _reg_mod                                                # noqa: E402
B, N1, N2 = {"id": 10, "name": "build"}, {"id": 11, "name": "n11"}, {"id": 12, "name": "n12"}
built, delivered, recorded = [], [], []
_saved = {k: getattr(im, k) for k in ("_build_target", "_build_one", "_materialize_image_tar",
                                      "_publish_to_registry", "_deliver", "_image_present_node",
                                      "record_node_only_tag")}
im._build_target = lambda: ("node", B)
im._build_one = lambda which, node, tag, ctx, key, nname, n_start: (built.append(node["id"]) or (True, "", None))
im._materialize_image_tar = lambda which, tag, dest, build_node=None: (True, "ok")
im._publish_to_registry = lambda tag, path: "sha256:feed"
im._deliver = lambda node, tag, path, digest: (delivered.append((node["id"], digest)) or (True, digest))
im._image_present_node = lambda node, tag, force=False: True
im.record_node_only_tag = lambda which, nid, tag: recorded.append(nid)
_reg_mod.usable = lambda: True
verifie(im._build_once_node("mtl") == B, "registry usable: the build host builds once")
res = im._build_once("mtl", "bobi-mtl:9.9.9", b"", [N1, B, N2], B)
verifie(built == [10], "★ built ONCE, on the build host only", built)
verifie(sorted(delivered) == [(11, "sha256:feed"), (12, "sha256:feed")], "★ the other targets pull by digest", delivered)
verifie(sorted(recorded) == [10, 11, 12] and all(r["ok"] for r in res), "the tag is recorded on every target", recorded)
built.clear(); delivered.clear(); recorded.clear()
im._build_one = lambda *a: (False, "boom", None)
res = im._build_once("mtl", "bobi-mtl:9.9.9", b"", [N1, N2], B)
verifie(not delivered and not recorded and not any(r["ok"] for r in res), "★ a failed build delivers nothing, every target fails")
_reg_mod.usable = lambda: False
verifie(im._build_once_node("mtl") is None, "no registry: built on every node, as before")
for k, v in _saved.items():
    setattr(im, k, v)

print("\n5c. rollback: pulled by digest, recorded only where it arrived")
pulled, recorded, filled = [], [], []
_saved = {k: getattr(im, k) for k in ("db_get_nodes", "_node_needs_image", "_node_expects_image",
                                      "record_node_only_tag", "_autofill_nodes_image")}
im.db_get_nodes = lambda: [dict(N1, agent_url="https://a"), dict(N2, agent_url="https://b")]
im._node_needs_image = im._node_expects_image = lambda which, n: True
im.record_node_only_tag = lambda which, nid, tag: recorded.append(nid)
im._autofill_nodes_image = lambda which, tag, alert=True: filled.append(tag)
_reg_mod.remote_digest = lambda tag: "sha256:old" if tag == "bobi-mtl:0.108.5" else None
_reg_mod.node_pull = lambda n, tag, digest: (pulled.append((n["id"], digest)) or ((True, digest) if n["id"] == 11 else (False, "down")))
ok, m = im.rollback_image("mtl", "bobi-mtl:0.108.5")
verifie(sorted(pulled) == [(11, "sha256:old"), (12, "sha256:old")], "★ every node pulls the old version BY DIGEST", pulled)
verifie(recorded == [11] and not ok, "★ the tag is recorded only where the image arrived; partial = not ok", (recorded, ok))
verifie(im.rollback_image("mtl", "bobi-compute:0.84")[0] is False, "a version of ANOTHER image is refused")
verifie(im.rollback_image("mtl", "bobi-mtl:0.0.1")[0] is False, "a version absent from the registry is refused")
pulled.clear()
_reg_mod.remote_digest = lambda tag: "sha256:c84"
_reg_mod.node_pull = lambda n, tag, digest: (True, digest)
im.rollback_image("compute", "bobi-compute:0.84")
verifie(filled == ["bobi-compute:0.84"], "shared image: the tag goes through the checked autofill", filled)
for k, v in _saved.items():
    setattr(im, k, v)

print("\n5d. drift: same tag, other content")
_sv, _pb, _nd_ = _reg_mod.settings_values, _reg_mod.published, _reg_mod.node_digest
_ut = im._used_tag
_reg_mod.settings_values = lambda: dict(V, enabled=True)
_reg_mod.published = lambda: {"bobi-compute:0.84": {"digest": "sha256:good"}, "bobi-mtl:0.108.6": {"digest": "sha256:m"}}
im._used_tag = lambda which, node: {"compute": "bobi-compute:0.84", "mtl": "bobi-mtl:0.108.6"}.get(which)
held = {"bobi-compute:0.84": "sha256:evil", "bobi-mtl:0.108.6": None}
_reg_mod.node_digest = lambda node, tag: held.get(tag)
PRES = {"bobi-compute:0.84", "bobi-mtl:0.108.6"}
verifie(im._digest_drift(N1, PRES) == ["bobi-compute:0.84"], "★ another content under the published tag is a drift")
with database.get_db() as db:
    n_al = db.execute("SELECT count(*) FROM alerts WHERE msg_key='alert.image.empreinte_ecart'").fetchone()[0]
verifie(n_al == 1, "★ and it is said", n_al)
held["bobi-compute:0.84"] = "sha256:good"
verifie(im._digest_drift(N2, PRES) == [], "the published content: no drift; came by archive (no digest): no drift")
_reg_mod.settings_values = lambda: dict(V, enabled=False)
verifie(im._digest_drift(N1, PRES) == [], "registry off: nothing compared")
_reg_mod.settings_values, _reg_mod.published, _reg_mod.node_digest, im._used_tag = _sv, _pb, _nd_, _ut

print("\n5e. an architecture the image was not published for is said, never pulled")
calls = []
_pb, _na = _reg_mod.published, _reg_mod.node_arch
_reg_mod.published = lambda: {"bobi-test:1.0": {"digest": "sha256:i", "platforms": ["linux/amd64"]}}
registry.node_pull = lambda node, tag, digest: (calls.append("pull") or (True, digest))
node_driver.load_image_file = lambda node, tag, path: (calls.append("tar") or (True, tag))
_reg_mod.node_arch = lambda node: "arm64"
ok, m = im._deliver({"id": 7, "name": "spark"}, "bobi-test:1.0", "/tmp/x.tar", "sha256:i")
verifie(not ok and not calls, "★ arm64 node, amd64-only image: neither pulled nor loaded from the archive", (ok, calls))
with database.get_db() as db:
    a = db.execute("SELECT msg_params FROM alerts WHERE msg_key='alert.image.architecture_absente'").fetchall()
verifie(len(a) == 1 and "arm64" in a[0][0], "★ and it is said, with the architecture", a)
_reg_mod.node_arch = lambda node: "amd64"
ok, m = im._deliver({"id": 7, "name": "x"}, "bobi-test:1.0", "/tmp/x.tar", "sha256:i")
verifie(ok and calls == ["pull"], "amd64 node: pulled", calls)
_reg_mod.published = lambda: {"bobi-old:1": {"digest": "sha256:m"}}
verifie(_reg_mod.missing_platform({"id": 1}, "bobi-old:1") is None, "a record without platforms is not blocked")
verifie(_reg_mod.holds_published("bobi-old:1", "sha256:m"), "an older record (manifest digest) still matches")
_reg_mod.published, _reg_mod.node_arch = _pb, _na

print("\n5f. HA standby: a copy at publication, checked manifest by manifest")


class FakeZot:
    """Minimal OCI distribution endpoint in memory (requests.Session look-alike)."""
    def __init__(self):
        self.man, self.blobs = {}, set()

    class R:
        def __init__(self, code, body=b"", headers=None):
            self.status_code, self.content, self.headers, self.text = code, body, headers or {}, ""

        def json(self):
            return json.loads(self.content)

    def _parse(self, url):
        p = url.split("/v2/", 1)[1].split("?")[0]
        repo, kind, ref = re.match(r"(.+)/(manifests|blobs|blobs/uploads)/?(.*)$", p).groups()
        return repo, kind, ref

    def head(self, url, **kw):
        return self.get(url, **kw)

    def get(self, url, **kw):
        repo, kind, ref = self._parse(url)
        if kind == "blobs":
            return self.R(200 if ref in self.blobs else 404)
        m = self.man.get((repo, ref))
        return self.R(200, m, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(m).hexdigest()}) if m else self.R(404)

    def post(self, url, **kw):
        return self.R(202, headers={"Location": "/v2/x/blobs/uploads/u"})

    def put(self, url, data=None, **kw):
        if "digest=" in url:
            self.blobs.add(url.split("digest=")[1])
            return self.R(201)
        repo, kind, ref = self._parse(url)
        d = "sha256:" + hashlib.sha256(data).hexdigest()
        self.man[(repo, ref)] = self.man[(repo, d)] = data
        return self.R(201, headers={"Docker-Content-Digest": d})


sb = FakeZot()
op.push(i1, "https://sb", "bobi-test", "1.0", sb)
verifie(op.missing("https://sb", "bobi-test", "1.0", sb, i1["digest"]) == [], "a complete copy: nothing missing")
verifie(op.missing("https://sb", "bobi-test", "1.0", sb, "sha256:other") == ["index " + i1["digest"]],
        "another content under the tag is said")
del sb.man[("bobi-test", i1["manifest_digest"])]
verifie(op.missing("https://sb", "bobi-test", "1.0", sb, i1["digest"]) == ["manifest " + i1["manifest_digest"]],
        "★ tag listed, platform manifest absent: NOT pullable (the 2026-11-28 false green)")
sb = FakeZot()
op.push(i1, "https://sb", "bobi-test", "1.0", sb)
sb.blobs.discard(i1["layers"][0]["digest"])
verifie(op.missing("https://sb", "bobi-test", "1.0", sb) == ["blob " + i1["layers"][0]["digest"]],
        "★ a missing layer is said")

from app import ha as _ha                                                      # noqa: E402
_sv, _ia, _pu, _ses = _reg_mod.settings_values, _ha.is_active, _ha.peer_url, _reg_mod.session
_v = dict(registry.settings_values(), enabled=True, kind="dedicated", port=5500)
_reg_mod.settings_values = lambda: dict(_v)
_ha.is_active, _ha.peer_url = (lambda: True), (lambda: "https://192.0.2.31:5000")
verifie(registry.standby_registry_url() == "https://192.0.2.31:5500", "the standby's registry, from the HA peer")
_reg_mod.settings_values = lambda: dict(_v, kind="nfs")
verifie(registry.standby_registry_url() is None, "shared storage: no copy, no check")
_reg_mod.settings_values = lambda: dict(_v)
_ha.is_active = lambda: False
verifie(registry.standby_registry_url() is None, "a standby does not copy to itself")
_ha.is_active = lambda: True
sb = FakeZot()
_reg_mod.session = lambda: sb
_stc = _reg_mod.storage_check
_reg_mod.storage_check = (lambda kind, path, min_free=0: {"ok": True, "code": "ok"})
_lu, _rec = _reg_mod.local_url, _reg_mod._record
local = FakeZot()
_reg_mod._record = lambda *a, **k: None
_reg_mod.local_url = lambda v=None: "https://local"
_sessions = {"https://local": local, "https://192.0.2.31:5500": sb}


class Router:
    def __getattr__(self, name):
        def call(url, *a, **k):
            for base, s in _sessions.items():
                if url.startswith(base):
                    return getattr(s, name)(url, *a, **k)
            raise AssertionError(url)
        return call


_reg_mod.session = lambda: Router()
ok, d = registry.publish_tar("bobi-test:1.0", TMP + "/legacy.tar")
verifie(ok and op.missing("https://sb", "bobi-test", "1.0", sb, d) == [],
        "★ a publication is copied to the standby at once, not left to the 5-min sync", (ok, d))
_reg_mod.local_url, _reg_mod._record, _reg_mod.storage_check = _lu, _rec, _stc

_lag, _pb = _reg_mod.standby_lag, _reg_mod.published
now = 1_000_000.0
_reg_mod.published = lambda: {"bobi-a:1": {"digest": "sha256:a", "ts": now - 60},
                              "bobi-b:1": {"digest": "sha256:b", "ts": now - 3600}}
_reg_mod.standby_lag = lambda v=None: {"bobi-a:1": ["index"]}


_raised, _dba = [], database.db_add_alert
database.db_add_alert = lambda key, *a, **k: _raised.append(key)   # calls, not rows: the anti-rebond hides repeats


def n_alerts(key):
    return _raised.count(key)


verifie(registry.watch_standby_once(now) == [] and n_alerts("alert.registre.secours_incomplet") == 0,
        "a copy younger than the grace window is not an alert yet")
_reg_mod.standby_lag = lambda v=None: {"bobi-b:1": ["manifest sha256:x"]}
registry.watch_standby_once(now)
registry.watch_standby_once(now)
verifie(n_alerts("alert.registre.secours_incomplet") == 1, "★ older than 15 min and still missing: ONE alert")
_reg_mod.standby_lag = lambda v=None: {}
registry.watch_standby_once(now)
verifie(n_alerts("alert.registre.secours_complet") == 1, "caught up: said once")
_reg_mod.standby_lag, _reg_mod.published = _lag, _pb
database.db_add_alert = _dba
_reg_mod.settings_values, _ha.is_active, _ha.peer_url, _reg_mod.session = _sv, _ia, _pu, _ses

print("\n6. routes")
from app.auth import hash_password                                             # noqa: E402
database.db_create_user("chef", hash_password("Banc-Motdepasse-2026!"), "admin")
database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")
import main as app_main                                                        # noqa: E402
app = app_main.app
app.config["TESTING"] = True


def client(nom):
    cli = app.test_client()
    with database.get_db() as db:
        u = db.execute("SELECT id, username FROM users WHERE username=?", (nom,)).fetchone()
    with cli.session_transaction() as s_:
        s_["user_id"], s_["username"] = u["id"], u["username"]
    return cli


CHEF, LECTEUR = client("chef"), client("lecteur")
r = LECTEUR.post("/api/registry/config", json={"registry_enabled": True})
verifie(r.status_code in (401, 403), "★ a viewer cannot change the registry", r.status_code)
r = LECTEUR.post("/api/registry/rollback", json={"which": "mtl", "tag": "bobi-mtl:0.108.5"})
verifie(r.status_code in (401, 403), "★ a viewer cannot roll back", r.status_code)
im._rollback["mtl"] = {"status": "running"}
verifie(CHEF.post("/api/registry/rollback", json={"which": "mtl", "tag": "bobi-mtl:0.108.5"}).status_code == 409,
        "one rollback at a time per image")
im._rollback.clear()
on_root = tempfile.mkdtemp(prefix="verif_registre_", dir="/var/tmp")
try:
    r = CHEF.post("/api/registry/config", json={"registry_enabled": True, "registry_storage_kind": "dedicated",
                                                "registry_storage_path": on_root})
    j = r.get_json() or {}
    if os.stat(on_root).st_dev == os.stat("/").st_dev:
        verifie(r.status_code == 400 and j.get("code") == "not_mounted" and not settings.get("registry_storage_path"),
                "★ a dedicated path on the system disk is refused, nothing saved", (r.status_code, j))
finally:
    shutil.rmtree(on_root, ignore_errors=True)
settings.set("registry_s3_secret", "S3-SECRET-VALUE")
body = CHEF.get("/api/settings").get_data(as_text=True) + CHEF.get("/api/registry/status").get_data(as_text=True)
verifie("S3-SECRET-VALUE" not in body, "★ the S3 secret never leaves through the API")
html = CHEF.get("/settings").get_data(as_text=True)
verifie('id="reg-status"' in html and "registreCharger()" in html, "the Registry card is on the Images tab")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n/%s.json" % lang), encoding="utf-8"))
    used = set(re.findall(r"T\('(settings\.registry\.[a-z_]+)'\)", html)) | \
        set(re.findall(r"_\('(settings\.registry\.[a-z_]+)'\)", open(os.path.join(ROOT, "templates/settings.html"), encoding="utf-8").read()))
    used |= {"settings.registry.kind_%s_d" % k for k in registry.STORAGE_KINDS}
    used |= {"settings.registry.storage_" + c for c in ("ok", "unset", "unknown_kind", "missing", "not_mounted",
                                                       "not_writable", "full", "bad_port")}
    used |= {"alert.image.architecture_absente", "settings.registry.missing_arch", "alert.image.empreinte_ecart", "alert.image.retour_arriere", "alert.image.registre_publication", "alert.image.registre_tirage", "alert.registre.stockage",
             "alert.registre.stockage_retabli", "alert.registre.secours_incomplet", "alert.registre.secours_complet"}
    missing = sorted(k for k in used if k not in cat)
    verifie(not missing, "every key exists in %s" % lang, missing)

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
