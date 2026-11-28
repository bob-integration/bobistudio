# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Publish an image as OCI, by digest — chantier « registre d'images », 2026-11-28.

Input: the tar of `docker save` (what the build node already exports for the tar distribution).
Output: an OCI image in the registry — an image INDEX under the tag (one entry per platform,
amd64 alone today), the platform manifest `application/vnd.oci.image.manifest.v1+json`, config
`application/vnd.oci.image.config.v1+json`, layers `…layer.v1.tar+gzip` — and the index DIGEST,
which is the image's identity from then on (what a node must hold, what a rollback names).
Publishing a SECOND architecture under the same tag will have to merge its entry into the
existing index (not done yet: no arm64 build exists).

Why here and not `docker push` / buildx on the build node: the controller has no Docker, the nodes
may lack buildx, and `docker push` from the classic image store publishes Docker media types. Read
by Docker today, the OCI form is also read by containerd, K3s or Podman tomorrow without a rebuild.

Both forms of `docker save` are read: the OCI layout written by Docker >= 25 (`index.json` +
`blobs/sha256/…`) and the legacy one (`manifest.json` + `<id>/layer.tar`). Layers are compressed
DETERMINISTICALLY (gzip, mtime 0, no name): the same layer gives the same digest from one build to
the next, so a node only pulls the layers that changed.

`write_layout` writes the same image as an OCI image layout tar (`oci-layout`, `index.json`,
`blobs/`) plus Docker's compatibility `manifest.json`: the offline / fallback form, loadable by
`docker load` today and by any OCI tool tomorrow.
"""
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile

MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_INDEX = "application/vnd.oci.image.index.v1+json"
MT_CONFIG = "application/vnd.oci.image.config.v1+json"
MT_LAYER_GZ = "application/vnd.oci.image.layer.v1.tar+gzip"
GZIP_LEVEL = 1          # LAN transfer: speed matters more than the last 10 % of size


class OciError(Exception):
    pass


# ── Reading `docker save` ───────────────────────────────────────────────────────────────────────
def read_save(tar_path):
    """→ (config_bytes, [layer member names], tar) from a `docker save` tar, either layout.
    Layers are returned UNCOMPRESSED as stored (Docker saves them so); the caller compresses."""
    t = tarfile.open(tar_path, "r:")
    names = set(t.getnames())

    def read(name):
        f = t.extractfile(name)
        if f is None:
            raise OciError("missing member: %s" % name)
        return f.read()

    def blob(digest):
        algo, hexd = digest.split(":", 1)
        return "blobs/%s/%s" % (algo, hexd)

    if "manifest.json" in names:
        m = json.loads(read("manifest.json"))
        if len(m) != 1:
            raise OciError("expected one image in the tar, found %d" % len(m))
        m = m[0]
        return read(m["Config"]), list(m["Layers"]), t
    if "index.json" in names:
        idx = json.loads(read("index.json"))
        desc = (idx.get("manifests") or [None])[0]
        if not desc:
            raise OciError("empty index.json")
        man = json.loads(read(blob(desc["digest"])))
        if man.get("mediaType") == MT_INDEX or "manifests" in man:
            man = json.loads(read(blob(man["manifests"][0]["digest"])))
        return read(blob(man["config"]["digest"])), [blob(l["digest"]) for l in man["layers"]], t
    raise OciError("neither manifest.json nor index.json: not a docker save tar")


def _gzip_member(t, name, workdir):
    """Compress one layer member deterministically to a temp file. → (path, digest, size)."""
    src = t.extractfile(name)
    if src is None:
        raise OciError("missing layer: %s" % name)
    head = src.read(2)
    fd, path = tempfile.mkstemp(prefix="layer-", suffix=".gz", dir=workdir)
    h = hashlib.sha256()
    with os.fdopen(fd, "wb") as raw:
        class _Tee:
            def write(self, b):
                h.update(b)
                return raw.write(b)

            def flush(self):
                raw.flush()
        if head == b"\x1f\x8b":            # stored compressed already: keep the bytes as they are
            tee = _Tee()
            tee.write(head)
            for chunk in iter(lambda: src.read(1 << 20), b""):
                tee.write(chunk)
        else:
            with gzip.GzipFile(filename="", mode="wb", fileobj=_Tee(), mtime=0,
                               compresslevel=GZIP_LEVEL) as gz:
                gz.write(head)
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    gz.write(chunk)
    return path, "sha256:" + h.hexdigest(), os.path.getsize(path)


def build(tar_path, workdir, tag=None, progress=None):
    """Prepare the OCI image from a `docker save` tar. → dict(config, config_digest, layers=[{path,
    digest, size}], manifest (bytes), digest). Layer files live in `workdir` (caller deletes it)."""
    say = progress or (lambda _t: None)
    cfg, layer_names, t = read_save(tar_path)
    try:
        layers = []
        for i, name in enumerate(layer_names):
            say("layer %d/%d: compressing" % (i + 1, len(layer_names)))
            p, d, s = _gzip_member(t, name, workdir)
            layers.append({"path": p, "digest": d, "size": s})
    finally:
        t.close()
    cfg_digest = "sha256:" + hashlib.sha256(cfg).hexdigest()
    man = {"schemaVersion": 2, "mediaType": MT_MANIFEST,
           "config": {"mediaType": MT_CONFIG, "digest": cfg_digest, "size": len(cfg)},
           "layers": [{"mediaType": MT_LAYER_GZ, "digest": l["digest"], "size": l["size"]}
                      for l in layers]}
    raw = json.dumps(man, separators=(",", ":"), sort_keys=True).encode()
    man_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    plat = platform_of(cfg)
    # The TAG points at an image INDEX, one entry per platform (2026-11-28): amd64 alone today, but
    # an arm64 node (DGX Spark class) pulls the same tag and gets its own image tomorrow. Docker
    # resolves the index to the entry of its own platform.
    index = {"schemaVersion": 2, "mediaType": MT_INDEX,
             "manifests": [{"mediaType": MT_MANIFEST, "digest": man_digest, "size": len(raw),
                            "platform": plat}]}
    if tag:
        index["annotations"] = {"org.opencontainers.image.ref.name": tag}
    iraw = json.dumps(index, separators=(",", ":"), sort_keys=True).encode()
    return {"config": cfg, "config_digest": cfg_digest, "layers": layers, "manifest": raw,
            "manifest_digest": man_digest, "platform": plat, "index": iraw,
            "digest": "sha256:" + hashlib.sha256(iraw).hexdigest()}


def platform_of(cfg):
    """OCI platform of an image config: {"os", "architecture"[, "variant"]}."""
    c = json.loads(cfg)
    p = {"os": c.get("os") or "linux", "architecture": c.get("architecture") or "amd64"}
    if c.get("variant"):
        p["variant"] = c["variant"]
    return p


def platform_name(p):
    """{"os": "linux", "architecture": "arm64", "variant": "v8"} → "linux/arm64/v8"."""
    return "/".join(x for x in (p.get("os"), p.get("architecture"), p.get("variant")) if x)


# ── Registry push (OCI distribution API) ────────────────────────────────────────────────────────
def split_ref(tag):
    """`bobi-mtl:0.108.6` → ("bobi-mtl", "0.108.6")."""
    repo, _, ref = tag.rpartition(":")
    if not repo or "/" in ref:
        raise OciError("tag without a version: %s" % tag)
    return repo, ref


def push(img, base_url, repo, ref, session, progress=None):
    """Upload blobs (skipping those the registry has) then the manifest. `session` = a requests
    Session carrying the client certificate and the CA. → manifest digest, checked against the
    registry's answer."""
    say = progress or (lambda _t: None)

    def have(digest):
        r = session.head("%s/v2/%s/blobs/%s" % (base_url, repo, digest), timeout=30)
        return r.status_code == 200

    def upload(digest, body, size):
        r = session.post("%s/v2/%s/blobs/uploads/" % (base_url, repo), timeout=60)
        if r.status_code != 202:
            raise OciError("upload start %s: HTTP %d %s" % (digest, r.status_code, r.text[:200]))
        loc = r.headers["Location"]
        if loc.startswith("/"):
            loc = base_url + loc
        sep = "&" if "?" in loc else "?"
        r = session.put(loc + sep + "digest=" + digest, data=body, timeout=3600,
                        headers={"Content-Type": "application/octet-stream",
                                 "Content-Length": str(size)})
        if r.status_code != 201:
            raise OciError("upload %s: HTTP %d %s" % (digest, r.status_code, r.text[:200]))

    sent = skipped = 0
    for i, l in enumerate(img["layers"]):
        if have(l["digest"]):
            skipped += 1
            say("layer %d/%d: already in the registry" % (i + 1, len(img["layers"])))
            continue
        say("layer %d/%d: sending %.0f MB" % (i + 1, len(img["layers"]), l["size"] / 1e6))
        with open(l["path"], "rb") as f:
            upload(l["digest"], f, l["size"])
        sent += 1
    if not have(img["config_digest"]):
        upload(img["config_digest"], io.BytesIO(img["config"]), len(img["config"]))
    # The platform manifest by its digest (no tag of its own), then the index under the tag.
    r = session.put("%s/v2/%s/manifests/%s" % (base_url, repo, img["manifest_digest"]),
                    data=img["manifest"], headers={"Content-Type": MT_MANIFEST}, timeout=120)
    if r.status_code != 201:
        raise OciError("manifest: HTTP %d %s" % (r.status_code, r.text[:200]))
    r = session.put("%s/v2/%s/manifests/%s" % (base_url, repo, ref), data=img["index"],
                    headers={"Content-Type": MT_INDEX}, timeout=120)
    if r.status_code != 201:
        raise OciError("index: HTTP %d %s" % (r.status_code, r.text[:200]))
    got = r.headers.get("Docker-Content-Digest") or ""
    if got and got != img["digest"]:
        raise OciError("registry digest %s differs from ours %s" % (got, img["digest"]))
    say("published %s:%s@%s (%d layers sent, %d already there)" % (repo, ref, img["digest"],
                                                                  sent, skipped))
    return img["digest"]


def remote_digest(base_url, repo, ref, session):
    """Digest the registry holds for repo:ref, or None."""
    r = session.head("%s/v2/%s/manifests/%s" % (base_url, repo, ref), timeout=30,
                     headers={"Accept": MT_MANIFEST + ", " + MT_INDEX})
    return r.headers.get("Docker-Content-Digest") if r.status_code == 200 else None


def missing(base_url, repo, ref, session, expected=None):
    """What a registry lacks to serve repo:ref ENTIRELY: the index (or another digest than
    `expected`), each platform manifest, its config and layers. [] = pullable. A listed tag proves
    nothing: on a syncing standby the index was there 9 min before its platform manifest
    (2026-11-28, Zot « skipping missing manifest blob in image index »)."""
    acc = {"Accept": MT_MANIFEST + ", " + MT_INDEX}
    r = session.get("%s/v2/%s/manifests/%s" % (base_url, repo, ref), headers=acc, timeout=30)
    if r.status_code != 200:
        return ["index"]
    got = r.headers.get("Docker-Content-Digest") or ""
    if expected and got != expected:
        return ["index " + (got or "?")]
    doc = r.json()
    children = ([m["digest"] for m in doc.get("manifests") or []] if "manifests" in doc else [])
    mans = []
    out = []
    for d in children:
        rm = session.get("%s/v2/%s/manifests/%s" % (base_url, repo, d), headers=acc, timeout=30)
        if rm.status_code != 200:
            out.append("manifest " + d)
        else:
            mans.append(rm.json())
    if not children:
        mans.append(doc)
    for m in mans:
        for b in [m.get("config") or {}] + list(m.get("layers") or []):
            d = b.get("digest")
            if d and session.head("%s/v2/%s/blobs/%s" % (base_url, repo, d),
                                  timeout=30).status_code != 200:
                out.append("blob " + d)
    return out


def list_tags(base_url, repo, session):
    r = session.get("%s/v2/%s/tags/list" % (base_url, repo), timeout=30)
    return (r.json().get("tags") or []) if r.status_code == 200 else []


# ── OCI image layout (offline / fallback) ───────────────────────────────────────────────────────
def write_layout(img, dest_tar, tag):
    """Write `img` as an OCI image layout tar (`oci-layout`, `index.json`, `blobs/sha256/…`)."""
    def add_bytes(t, name, data):
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mtime = 0
        t.addfile(ti, io.BytesIO(data))

    # index.json names the IMAGE INDEX (the identity, same digest as in the registry), which
    # itself lists the platform manifests.
    index = {"schemaVersion": 2, "mediaType": MT_INDEX, "manifests": [{
        "mediaType": MT_INDEX, "digest": img["digest"], "size": len(img["index"]),
        "annotations": {"org.opencontainers.image.ref.name": tag,
                        "io.containerd.image.name": tag}}]}
    with tarfile.open(dest_tar, "w") as t:
        add_bytes(t, "oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}).encode())
        add_bytes(t, "index.json", json.dumps(index).encode())
        # Docker compatibility manifest, as `docker save` >= 25 writes it next to the OCI layout:
        # the classic image store of `docker load` (Docker 26 on our nodes) reads ONLY this file,
        # and refused an index.json-only archive (« blobs/json: no such file », 2026-11-28).
        add_bytes(t, "manifest.json", json.dumps([{
            "Config": "blobs/sha256/" + img["config_digest"].split(":")[1],
            "RepoTags": [tag],
            "Layers": ["blobs/sha256/" + l["digest"].split(":")[1] for l in img["layers"]]}]).encode())
        add_bytes(t, "blobs/sha256/" + img["digest"].split(":")[1], img["index"])
        add_bytes(t, "blobs/sha256/" + img["manifest_digest"].split(":")[1], img["manifest"])
        add_bytes(t, "blobs/sha256/" + img["config_digest"].split(":")[1], img["config"])
        for l in img["layers"]:
            ti = t.gettarinfo(l["path"], "blobs/sha256/" + l["digest"].split(":")[1])
            ti.mtime = 0
            with open(l["path"], "rb") as f:
                t.addfile(ti, f)
