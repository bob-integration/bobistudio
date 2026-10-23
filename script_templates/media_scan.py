# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Analyse d'un volume média, pour le REGISTRE DES MÉDIAS (app/media_registry.py).

Exécuté SUR LE NŒUD, dans un conteneur jetable de l'image média (`docker run --rm -i`), le volume
monté en /m : l'image porte python3, ffprobe et ffmpeg, l'hôte n'a rien à installer. Entrée JSON
sur stdin, sortie JSON sur stdout. Modes :

- `scan` : relevé des fichiers médias (taille, date, identité lue dans le sidecar, empreinte du
  contenu, fiche technique par ffprobe pour ce qui est nouveau ou modifié).
- `stamp` : écrit l'identité décidée par l'orchestrateur dans le sidecar `.xml` de chaque média
  (le crée, au schéma <media> du recorder, s'il n'existe pas), et calcule les vignettes demandées.
- `check` : un fichier hors volume (source d'un import) est-il lisible ?
Les PROXYS de lecture (`<dossier>/.proxies/`, générés par la ferme de transcodage) ne sont pas
relevés (dossier caché) : `scan` vérifie seulement ceux qu'on lui nomme (`proxies`).

Empreinte : blake2b-128 de (taille, 4 premiers Mo, 4 derniers Mo). Quelques millisecondes même sur
100 Go, stable tant que le contenu ne change pas — ce qui permet de reconnaître un média déplacé
hors du système, ou dont le sidecar s'est perdu.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.sax.saxutils as sx

ROOT = "/m"
EXTS = (".mxf", ".mov", ".mp4", ".mkv", ".avi", ".ts", ".mts", ".m2ts", ".m4v", ".webm",
        ".wav", ".mp3", ".aac", ".flac", ".m4a",
        ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff")
IMAGE_CODECS = ("png", "mjpeg", "bmp", "gif", "webp", "tiff", "jpeg2000")
CHUNK = 4 << 20
ID_RE = re.compile(r'<media\b[^>]*?\bid="([^"]+)"')
STATUS_RE = re.compile(r'<media\b[^>]*?\bstatus="([^"]+)"|<status>\s*([a-z]+)\s*</status>')
ORIGIN_RE = re.compile(r'<media\b[^>]*?\borigin="([^"]+)"')
LOOP_RE = re.compile(r"<loop\b")
PRODUCER_RE = re.compile(r"<container>\s*([^<]+?)\s*</container>")
DERIVED_RE = re.compile(r'<media\b[^>]*?\bderived_from="([^"]*)"')
# VERSION du média que porte le fichier (transcodage, livraison…) ; absente = l'original.
VERSION_RE = re.compile(r'<media\b[^>]*?\bversion="([a-z][a-z0-9]{0,15})"')
PROCESS_RE = re.compile(r"<transcode><!\[CDATA\[(.*?)\]\]></transcode>", re.S)
AVID_AUDIO = re.compile(r"(.*)_a\d+\.mxf$")


def fingerprint(full, size):
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(full, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK:
            f.seek(max(CHUNK, size - CHUNK))
            h.update(f.read(CHUNK))
    return h.hexdigest()


def probe(full, err=None):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,width,height,r_frame_rate,field_order,pix_fmt,"
             "color_primaries,color_transfer,color_space,channels,sample_rate,bits_per_raw_sample"
             ":stream_tags=timecode:format=duration,format_name:format_tags=timecode",
             "-of", "json", full], capture_output=True, timeout=30)
        j = json.loads(out.stdout or b"{}")
        if err is not None and out.returncode != 0:
            # Toutes les lignes, sans le préfixe « [mov,mp4… @ 0x…] » ni le chemin : « moov atom
            # not found » (pas d'index : enregistrement interrompu) dit plus que la dernière ligne.
            lines = []
            for ln in (out.stderr or b"").decode("utf-8", "replace").strip().splitlines():
                ln = re.sub(r"^\[[^\]]*\]\s*", "", ln).replace(full + ": ", "").strip()
                if ln and ln not in lines:
                    lines.append(ln)
            err.append(["; ".join(lines) or "?"])
    except Exception as e:
        if err is not None:
            err.append([str(e)])
        return None
    fmt = j.get("format") or {}
    tech = {"container": fmt.get("format_name") or "", "duration": None, "video": None, "audio": [],
            "timecode": (fmt.get("tags") or {}).get("timecode")}
    try:
        tech["duration"] = float(fmt.get("duration") or 0) or None
    except (TypeError, ValueError):
        pass
    streams = j.get("streams") or []
    for s in streams:
        if s.get("codec_type") == "video" and tech["video"] is None:
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            try:
                fps = round(float(num) / float(den), 3) if float(den or 0) else None
            except ValueError:
                fps = None
            fo = s.get("field_order") or ""
            tech["video"] = {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
                             "fps": fps, "scan": "i" if fo in ("tt", "bb", "tb", "bt") else ("p" if fo == "progressive" else ""),
                             "pix_fmt": s.get("pix_fmt"), "primaries": s.get("color_primaries"),
                             "transfer": s.get("color_transfer"), "matrix": s.get("color_space")}
            tech["timecode"] = tech["timecode"] or (s.get("tags") or {}).get("timecode")
        elif s.get("codec_type") == "audio":
            tech["audio"].append({"codec": s.get("codec_name"), "channels": s.get("channels"),
                                  "rate": int(s.get("sample_rate") or 0) or None,
                                  "bits": int(s.get("bits_per_raw_sample") or 0) or None})
    v = tech["video"] or {}
    if v and (v.get("codec") in IMAGE_CODECS) and not tech["audio"] and (tech["duration"] or 0) < 0.5:
        tech["kind"], tech["duration"] = "image", None
    else:
        tech["kind"] = "video" if v else ("audio" if tech["audio"] else "")
    return tech


def scan(req):
    known = req.get("known") or {}          # rel -> [size, mtime, fp, has_tech]
    probe_max = int(req.get("probe_max", 200))
    probed = 0
    now = time.time()
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        names = set(files)
        for fn in sorted(files):
            if not fn.lower().endswith(EXTS):
                continue
            m = AVID_AUDIO.match(fn)
            if m and m.group(1) + "_v1.mxf.xml" in names:
                continue                    # piste audio d'un clip Avid : portée par son _v1
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, ROOT)
            try:
                st = os.stat(full)
            except OSError:
                continue
            size, mtime = st.st_size, int(st.st_mtime)
            e = {"path": rel, "size": size, "mtime": mtime}
            side = None
            if fn + ".xml" in names:
                try:
                    with open(full + ".xml", "r", encoding="utf-8", errors="replace") as f:
                        side = f.read(1 << 20)
                except OSError:
                    side = None
            if side is not None:
                e["sidecar"] = True
                mi = ID_RE.search(side)
                if mi:
                    e["id"] = mi.group(1)
                ms = STATUS_RE.search(side)
                if ms and (ms.group(1) or ms.group(2)) == "recording":
                    e["growing"] = True
                mo = ORIGIN_RE.search(side)
                if mo:
                    e["origin"] = mo.group(1)
                if LOOP_RE.search(side):
                    e["loop"] = True
                mp = PRODUCER_RE.search(side)
                if mp and e.get("origin"):
                    e["producer"] = mp.group(1)
                mv = VERSION_RE.search(side)
                if mv and mv.group(1) != "original":
                    e["version"] = mv.group(1)
                md = DERIVED_RE.search(side)
                if md and md.group(1):
                    e["derived_from"] = md.group(1)
                mpr = PROCESS_RE.search(side)
                if mpr:
                    try:
                        e["process"] = json.loads(mpr.group(1).replace("]]]]><![CDATA[>", "]]>"))
                    except ValueError:
                        pass
            if now - st.st_mtime < 5:
                e["growing"] = True
            k = known.get(rel)
            same = bool(k) and k[0] == size and k[1] == mtime
            if not e.get("growing"):
                if same and k[2]:
                    e["fp"] = k[2]
                else:
                    try:
                        e["fp"] = fingerprint(full, size)
                    except OSError:
                        pass
                if (not same or not k[3]) and probed < probe_max:
                    t = probe(full)
                    probed += 1
                    if t:
                        e["tech"] = t
            out.append(e)
    return {"files": out, "probed": probed, "proxies": stat_proxies(req.get("proxies"))}


def _media_attrs(ident, version):
    a = 'id="%s"' % sx.escape(ident)
    return a + (' version="%s"' % sx.escape(version) if version and version != "original" else "")


def write_sidecar(full, ident, tech, version=None):
    """Sidecar au schéma <media> du recorder et du Gestionnaire de Médias, + l'identité (et la
    version du média que porte le fichier, s'il n'est pas l'original)."""
    tech = tech or {}
    v = tech.get("video") or {}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<media %s>' % _media_attrs(ident, version),
             "  <file>%s</file>" % sx.escape(os.path.basename(full)), "  <origin>imported</origin>"]
    if tech.get("duration"):
        lines.append("  <duration>%.1f</duration>" % float(tech["duration"]))
    if v.get("width"):
        lines.append('  <format width="%s" height="%s" fps="%s" codec="%s"/>'
                     % (v.get("width"), v.get("height"), round(v.get("fps") or 0) or "", sx.escape(str(v.get("codec") or ""))))
    if tech.get("timecode"):
        lines.append('  <timecode source="file" present="true" start="%s"/>' % sx.escape(str(tech["timecode"])))
    lines.append("</media>\n")
    tmp = full + ".xml.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, full + ".xml")


def stamp_one(rel, ident, tech, version=None, replace=False):
    """Écrit l'identité (et la version) dans le sidecar. Une AUTRE identité déjà écrite est gardée
    (« conflict ») — sauf `replace` : l'orchestrateur sait qu'elle ne désigne aucun média de la base
    (sortie d'un transcodage dont la source était inconnue, rangée depuis sous son média)."""
    full = os.path.join(ROOT, rel)
    side = full + ".xml"
    if not os.path.exists(side):
        write_sidecar(full, ident, tech, version)
        return "created"
    with open(side, "r", encoding="utf-8", errors="replace") as f:
        txt = f.read()
    m = ID_RE.search(txt)
    mv = VERSION_RE.search(txt)
    if m and m.group(1) == ident and (mv.group(1) if mv else "original") == (version or "original"):
        return "kept"
    if m and m.group(1) != ident and not replace:
        return "conflict:" + m.group(1)
    # Identité (et version) posées sur la balise <media>, ses autres attributs gardés.
    prev = None
    while prev != txt:
        prev, txt = txt, re.sub(r'(<media\b[^>]*?)\s+(?:id|version)="[^"]*"', r"\1", txt, count=1)
    new, n = re.subn(r"<media\b", "<media " + _media_attrs(ident, version), txt, count=1)
    if not n:
        return "no-root"
    tmp = side + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, side)
    return "stamped"


def thumb(rel, t, width):
    """Vignette JPEG (base64). Une IMAGE fixe n'a qu'une trame : chercher à t > 0 ne sort rien —
    on retente alors au début (sinon aucune image n'avait de vignette, constaté le 2026-10-11)."""
    full = os.path.join(ROOT, rel)
    for at in ((max(0.0, float(t)), 0.0) if float(t) > 0 else (0.0,)):
        try:
            # Pas de `-ss` du tout au début : sur un JPEG, même `-ss 0.0` ne sort rien (mesuré).
            out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error"] + (["-ss", str(at)] if at else [])
                                 + ["-i", full, "-frames:v", "1", "-vf", "scale=%d:-2" % width, "-q:v", "4",
                                  "-f", "mjpeg", "pipe:1"], capture_output=True, timeout=30)
        except Exception:
            return None
        if out.stdout:
            return base64.b64encode(out.stdout).decode()
    return None


def stamp(req):
    res = {"stamped": {}, "thumbs": {}}
    for it in req.get("stamp") or []:
        try:
            res["stamped"][it["path"]] = stamp_one(it["path"], it["id"], it.get("tech"), it.get("version"),
                                                   bool(it.get("replace")))
        except OSError as e:
            res["stamped"][it["path"]] = "error:%s" % e
    for it in req.get("thumbs") or []:
        res["thumbs"][it["id"]] = thumb(it["path"], it.get("t", 1.0), int(it.get("w", 480)))
    return res


def stat_proxies(paths):
    """Existence et taille des proxys connus (le relevé ignore les dossiers cachés)."""
    out = {}
    for rel in paths or []:
        try:
            st = os.stat(os.path.join(ROOT, rel))
            out[rel] = [st.st_size, int(st.st_mtime)]
        except OSError:
            out[rel] = None
    return out


def check(req):
    """Un fichier HORS volume (source d'un import, partage monté en /x) : lisible ? empreinte,
    fiche technique. Un fichier sans flux lisible est refusé AVANT de le copier."""
    full = req["path"]
    try:
        st = os.stat(full)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    err = []
    tech = probe(full, err)
    if not tech or not (tech.get("video") or tech.get("audio")):
        msg = (err[0][0] if err and err[0] else "") or "aucun flux audio ou vidéo"
        return {"ok": False, "error": msg, "size": st.st_size}
    return {"ok": True, "size": st.st_size, "mtime": int(st.st_mtime), "fp": fingerprint(full, st.st_size), "tech": tech}


if __name__ == "__main__":
    req = json.loads(sys.stdin.read() or "{}")
    mode = req.get("mode")
    out = (scan(req) if mode == "scan" else stamp(req) if mode == "stamp" else check(req) if mode == "check"
           else {"error": "mode ?"})
    sys.stdout.write(json.dumps(out))
