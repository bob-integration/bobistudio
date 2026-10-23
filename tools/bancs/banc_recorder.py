#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc du RECORDER, à lancer DANS son conteneur (il a ffmpeg, ffprobe et l'API :8082 en local).

Source attendue : un générateur avsync (flash plein écran + bip synchrones, 1 par seconde), sa
vidéo câblée sur l'entrée vidéo et son audio sur CHAQUE entrée audio. Pour chaque profil demandé :
enregistre N secondes, puis mesure sur le FICHIER (pas sur les compteurs du recorder) :
  - trames vidéo écrites vs attendues (durée réelle × cadence) — doublons/pertes de cadence ;
  - durée de chaque flux (vidéo, chaque piste audio) — dérive A/V cumulée ;
  - décalage A/V flash par flash (bip − flash, ms) : moyenne, dispersion, dérive début→fin ;
  - décalage entre pistes audio (même source sur chaque entrée → doit être nul).

Usage : banc_recorder.py SECONDES PROFIL [PROFIL…]   (ex. : banc_recorder.py 20 proxy archive)
Autres modes : seg:<s>:<profil> · cut:<n>:<profil> · loop:<min>:<profil> · grow:<mov|mxf>:<profil>
(fichier lisible pendant l'écriture, relu à mi-parcours)."""
import json, os, statistics, subprocess, sys, time, urllib.request

API = "http://127.0.0.1:8082"


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=(json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
                          "-of", "json", path], capture_output=True, timeout=600).stdout
    return json.loads(out or b"{}")


def luma_series(path, fps, fields=False):
    """Luma moyenne par trame (vignette 32×18) → liste ; la trame n est à n/fps s. `fields` : par
    CHAMP (`separatefields`, 2 par trame, dans l'ordre temporel) — en entrelacé, la trame seule ne
    situe un flash qu'à 1/25 s près."""
    vf = ("separatefields," if fields else "") + "scale=32:18,format=gray"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-vf", vf,
                          "-f", "rawvideo", "-"], capture_output=True, timeout=600).stdout
    n = 32 * 18
    return [sum(raw[i:i + n]) / n for i in range(0, len(raw) - n + 1, n)]


def audio_series(path, track):
    """Énergie par milliseconde de la piste `track` (somme des canaux, 48 kHz)."""
    import array
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:%d" % track, "-ac", "1",
                          "-ar", "48000", "-f", "f32le", "-"], capture_output=True, timeout=600).stdout
    a = array.array("f"); a.frombytes(raw[:len(raw) // 4 * 4])
    return [sum(x * x for x in a[i:i + 48]) for i in range(0, len(a) - 47, 48)]


def onsets(series, step_s, thresh_frac=0.5, min_gap_s=0.5):
    """Instants (s) où la série franchit le seuil vers le haut (front montant), espacés ≥ min_gap."""
    if not series:
        return []
    lo, hi = min(series), max(series)
    th = lo + (hi - lo) * thresh_frac
    out, last = [], -1e9
    for i in range(1, len(series)):
        t = i * step_s
        if series[i] >= th > series[i - 1] and t - last >= min_gap_s:
            out.append(t); last = t
    return out


def pair(flash, beep):
    """Décalages bip − flash (ms), en appariant chaque flash au bip le plus proche (±400 ms)."""
    d = []
    for f in flash:
        near = min(beep, key=lambda b: abs(b - f), default=None)
        if near is not None and abs(near - f) < 0.4:
            d.append((near - f) * 1000.0)
    return d


def sidecar(path):
    """(status, durée) du sidecar <fichier>.xml, ou (None, None)."""
    import re
    try:
        x = open(path + ".xml", encoding="utf-8").read()
    except OSError:
        return None, None
    st = re.search(r"<status>(\w+)</status>", x); du = re.search(r"<duration>([0-9.]+)</duration>", x)
    return (st.group(1) if st else None), (float(du.group(1)) if du else None)


def one(profile, seconds, growing="", audio=None):
    """Un enregistrement de `seconds`. `growing` (mov|mxf) : fichier lisible pendant l'écriture —
    à mi-parcours, on le relit AVANT sa fermeture (trames déjà lisibles contre durée annoncée par
    le sidecar), puis tout le reste est mesuré sur le fichier clos, comme sans."""
    body = {"profile": profile, "name_template": "banc_" + profile + "_%heure%",
            "segment_duration": 0, "growing": growing or "off", "loop_minutes": 0}
    body.update(audio or {"audio_layout": "multi", "audio_channels": 8})
    call("POST", "/start", body)
    t0 = time.time()
    mid = None
    if growing:
        time.sleep(seconds / 2)
        p = call("GET", "/state").get("file")
        sc_st, sc_du = sidecar(p)
        t_probe = time.time() - t0
        # Paquets, pas trames décodées : décoder un fichier qui grossit plus vite que le décodeur
        # (ProRes 4444, ~40 Mo/s) ne finit jamais — la sonde a tourné 600 s le 2026-10-11.
        out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_streams", "-of", "json", p],
                             capture_output=True, timeout=120).stdout
        pr = json.loads(out or b"{}")
        v = [x for x in pr.get("streams", []) if x.get("codec_type") == "video"]
        mid = {"at_s": round(t_probe, 1), "sidecar": sc_st, "sidecar_s": sc_du,
               "readable_frames": int(v[0].get("nb_read_packets") or 0) if v else 0,
               "audio_streams": len([x for x in pr.get("streams", []) if x.get("codec_type") == "audio"])}
        time.sleep(max(0.0, seconds - (time.time() - t0)))
    else:
        time.sleep(seconds)
    st = call("GET", "/state")
    path = st.get("file")
    call("POST", "/stop")
    wall = time.time() - t0
    time.sleep(1.5)
    st2 = call("GET", "/state")
    res = {"profile": profile, "file": os.path.basename(path or "?"), "wall_s": round(wall, 2),
           "record_error": st2.get("record_error"), "ffmpeg_log": (st2.get("ffmpeg_log") or [])[-3:]}
    if growing:
        res["growing"] = growing; res["mid"] = mid
        res["sidecar_final"] = sidecar(path) if path else None
    if growing == "avid" and path and os.path.isfile(path):
        return avid_files(res, path)
    if not path or not os.path.isfile(path) or os.path.getsize(path) < 1000:
        res["verdict"] = "AUCUN FICHIER"
        return res
    pr = probe(path)
    vs = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
    aus = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"]
    res["size_mb"] = round(os.path.getsize(path) / 1e6, 1)
    if vs:
        v = vs[0]
        num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0
        res["video"] = {"codec": v.get("codec_name"), "profile": v.get("profile"), "pix_fmt": v.get("pix_fmt"),
                        "size": "%sx%s" % (v.get("width"), v.get("height")), "fps": round(fps, 3),
                        "field_order": v.get("field_order"), "frames": int(v.get("nb_read_frames") or 0),
                        "duration": float(v.get("duration") or 0)}
        res["frames_expected_from_wall"] = round(wall * fps)
    res["audio"] = [{"codec": a.get("codec_name"), "ch": a.get("channels"), "rate": a.get("sample_rate"),
                     "duration": float(a.get("duration") or 0)} for a in aus]
    res["tc"] = (pr.get("format", {}).get("tags", {}) or {}).get("timecode") or \
        next((s.get("tags", {}).get("timecode") for s in pr.get("streams", []) if s.get("tags", {}).get("timecode")), None)
    # Calage : flashs (vidéo) contre bips (chaque piste)
    if vs and res["video"]["fps"]:
        il = res["video"].get("field_order") not in (None, "progressive", "unknown")
        rate = res["video"]["fps"] * (2 if il else 1)
        fl = onsets(luma_series(path, res["video"]["fps"], fields=il), 1.0 / rate)
        res["flashes"] = len(fl)
        tracks = []
        beeps0 = None
        for k in range(len(aus)):
            bp = onsets(audio_series(path, k), 0.001)
            if k == 0:
                beeps0 = bp
            d = pair(fl, bp)
            tr = {"track": k + 1, "beeps": len(bp)}
            if d:
                tr.update({"av_ms_mean": round(statistics.mean(d), 1),
                           "av_ms_min": round(min(d), 1), "av_ms_max": round(max(d), 1),
                           "av_ms_first": round(d[0], 1), "av_ms_last": round(d[-1], 1)})
            if k > 0 and beeps0:
                dd = [(b - min(beeps0, key=lambda x: abs(x - b))) * 1000 for b in bp]
                tr["vs_track1_ms"] = round(statistics.mean(dd), 2) if dd else None
            tracks.append(tr)
        res["sync"] = tracks
    return res


def avid_files(res, v1):
    """Avid OP-Atom : un fichier par piste (<clip>_v1.mxf, <clip>_a1.mxf…). Trames du fichier
    vidéo, durée de chaque fichier audio, et calage flash (v1) / bip (1er canal de chaque entrée)."""
    pre = v1[:-len("_v1.mxf")]
    d = os.path.dirname(pre)
    aud = sorted((f for f in os.listdir(d) if f.startswith(os.path.basename(pre) + "_a") and f.endswith(".mxf")),
                 key=lambda f: int(f.rsplit("_a", 1)[1][:-4]))
    pr = probe(v1)
    v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"][0]
    num, _, den = v["r_frame_rate"].partition("/"); fps = float(num) / float(den)
    res["video"] = {"codec": v.get("codec_name"), "profile": v.get("profile"), "field_order": v.get("field_order"),
                    "frames": int(v.get("nb_read_frames") or 0), "fps": fps}
    durs = []
    for f in aud:
        a = [s for s in probe(os.path.join(d, f)).get("streams", []) if s.get("codec_type") == "audio"]
        durs.append(round(float(a[0].get("duration") or 0), 2) if a else None)
    res["audio_files"] = len(aud); res["audio_durations"] = sorted(set(durs))
    il = v.get("field_order") not in (None, "progressive", "unknown")
    fl = onsets(luma_series(v1, fps, fields=il), 1.0 / (fps * (2 if il else 1)))
    res["flashes"] = len(fl); res["sync"] = []
    for k in sorted({0, min(8, len(aud) - 1)}):
        dd = pair(fl, onsets(audio_series(os.path.join(d, aud[k]), 0), 0.001))
        res["sync"].append({"file": aud[k], "av_ms_mean": round(statistics.mean(dd), 1) if dd else None})
    return res


def segmented(profile, seconds, seg_s, cuts=0):
    """Segmentation (seg_s > 0) ou coupes manuelles /cut (cuts > 0, à intervalles réguliers) : N
    fichiers consécutifs. Mis bout à bout, ils doivent former UN flux continu — flashs espacés
    d'exactement 1 s par-dessus les jonctions, TC qui se suivent, aucune trame perdue."""
    tag = "%s%d_%s" % ("cut" if cuts else "seg", int(time.time()) % 100000, profile)
    call("POST", "/start", {"profile": profile, "name_template": "banc_" + tag + "_%n%", "segment_duration": seg_s})
    t0 = time.time()
    if cuts:
        for _ in range(cuts):
            time.sleep(seconds / (cuts + 1)); call("POST", "/cut")
        time.sleep(seconds / (cuts + 1))
    else:
        time.sleep(seconds)
    call("POST", "/stop"); wall = time.time() - t0
    time.sleep(1.5)
    root = "/mnt/media"
    files = sorted(f for f in os.listdir(root) if f.startswith("banc_" + tag + "_") and not f.endswith(".xml"))
    off = 0.0; flashes = []; per = []
    for f in files:
        p = os.path.join(root, f); pr = probe(p)
        v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"][0]
        num, _, den = v["r_frame_rate"].partition("/"); fps = float(num) / float(den)
        n = int(v.get("nb_read_frames") or 0)
        fl = onsets(luma_series(p, fps), 1.0 / fps)
        bp = onsets(audio_series(p, 0), 0.001)
        tc = next((s.get("tags", {}).get("timecode") for s in pr.get("streams", []) if s.get("tags", {}).get("timecode")), None)
        per.append({"file": f, "frames": n, "tc": tc, "av_ms": sorted(set(round(x, 1) for x in pair(fl, bp)))})
        flashes += [off + t for t in fl]; off += n / fps
    gaps = [round((b - a) * 1000, 1) for a, b in zip(flashes, flashes[1:])]
    return {"profile": profile, "segment_s": seg_s, "wall_s": round(wall, 2), "files": per,
            "frames_total": sum(x["frames"] for x in per), "frames_expected_from_wall": round(wall * 50),
            "flash_gaps_ms": sorted(set(gaps))}


def loop(profile, seconds, window_min, growing=""):
    """Boucle : après `seconds` d'enregistrement, seuls les segments de la fenêtre restent ; la
    sauvegarde les met à l'abri, et mis bout à bout ils doivent rester continus."""
    tag = "loop%d_%s" % (int(time.time()) % 100000, profile)
    call("POST", "/start", {"profile": profile, "name_template": "banc_" + tag + "_%n%",
                            "segment_duration": 0, "loop_minutes": window_min, "growing": growing or "off"})
    time.sleep(seconds)
    st = call("GET", "/state")
    root = "/mnt/media"
    present = sorted(f for f in os.listdir(root) if f.startswith("banc_" + tag + "_") and not f.endswith(".xml"))
    sv = call("POST", "/loop_save", {})
    call("POST", "/stop"); time.sleep(1.5)
    folder = os.path.join(root, sv.get("folder", ""))
    saved = sorted(f for f in os.listdir(folder) if not f.endswith(".xml")) if sv.get("ok") else []
    off = 0.0; flashes = []; frames = []
    for f in saved:
        p = os.path.join(folder, f); pr = probe(p)
        v = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"][0]
        num, _, den = v["r_frame_rate"].partition("/"); fps = float(num) / float(den)
        n = int(v.get("nb_read_frames") or 0); frames.append(n)
        flashes += [off + t for t in onsets(luma_series(p, fps), 1.0 / fps)]; off += n / fps
    gaps = [round((b - a) * 1000, 1) for a, b in zip(flashes, flashes[1:])]
    return {"profile": profile, "window_min": window_min, "seconds": seconds,
            "segments_on_disk_before_save": len(present), "first_on_disk": present[:1],
            "loop_files_state": st.get("loop_files"), "save": {k: sv.get(k) for k in ("ok", "folder", "error")},
            "saved_files": saved, "saved_frames": frames, "saved_s": round(off, 2),
            "flash_gaps_ms": sorted(set(gaps))}


if __name__ == "__main__":
    secs = float(sys.argv[1])
    for prof in sys.argv[2:]:
        if prof.startswith("seg:"):            # seg:<secondes>:<profil>
            _s, sg, pf = prof.split(":")
            r = segmented(pf, secs, int(sg))
        elif prof.startswith("loop:"):         # loop:<fenêtre en minutes>:<profil>[:<mov|mxf>]
            parts = prof.split(":")
            r = loop(parts[2], secs, float(parts[1]), parts[3] if len(parts) > 3 else "")
        elif prof.startswith("cut:"):          # cut:<nombre de coupes>:<profil>
            _s, nc, pf = prof.split(":")
            r = segmented(pf, secs, 0, cuts=int(nc))
        elif prof.startswith("aud:"):          # aud:<multi|stereo|mono>:<canaux>:<profil>[:<mov|mxf|avid>]
            parts = prof.split(":")
            r = one(parts[3], secs, growing=parts[4] if len(parts) > 4 else "",
                    audio={"audio_layout": parts[1], "audio_channels": int(parts[2])})
            r["audio_layout"] = "%s/%s" % (parts[1], parts[2])
        elif prof.startswith("grow:"):         # grow:<mov|mxf|avid>:<profil>
            _s, gf, pf = prof.split(":")
            r = one(pf, secs, growing=gf)
        else:
            r = one(prof, secs)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        time.sleep(1)
