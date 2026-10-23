#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Inventaire du CORPUS du player (banc de compatibilité) : ffprobe de chaque média d'un ou plusieurs
dossiers (partage monté, volume d'un nœud), une ligne JSON par fichier, puis un résumé des cas qui
comptent pour le player (codec, cadence, balayage, pistes audio).

Exige le corpus monté (ce n'est pas une garde de CI) :
    inventaire_corpus_player.py SORTIE.json [RACINE] [DOSSIER=ALIAS ...]
    ex. inventaire_corpus_player.py /tmp/inv.json /mnt/bobi-ext/conn/1 Clients=OMV-STC-Clients TEmp=OMV-STC-TEmp

Corrigé le 2026-10-11 : la version de travail plantait sur une valeur None. ffprobe rend `null`
ou omet des champs selon le conteneur (r_frame_rate « 0/0 », durée « N/A », disposition absente,
size absente sur un flux tronqué) : TOUT champ passe ici par _num / _int, et une erreur sur un
fichier devient une ligne `error` au lieu d'arrêter l'inventaire.
"""
import collections
import concurrent.futures as cf
import json
import os
import subprocess
import sys

EXTS = (".mxf", ".mov", ".mp4", ".mkv", ".mts", ".m2ts", ".ts", ".avi", ".mpg", ".m4v", ".webm", ".wav", ".mp3")


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rate(s):
    """« 25/1 » → 25.0 ; « 0/0 », None, « N/A » → 0.0 (jamais de division par zéro)."""
    num, _, den = str(s or "0/1").partition("/")
    d = _num(den or 1)
    return round(_num(num) / d, 3) if d else 0.0


def probe(t):
    full, rel = t
    r = {"rel": rel}
    try:
        o = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", full],
                           capture_output=True, timeout=60).stdout
        j = json.loads(o or b"{}") or {}
        streams = j.get("streams") or []
        fmt = j.get("format") or {}
        v = [s for s in streams if (s or {}).get("codec_type") == "video"
             and ((s.get("disposition") or {}).get("attached_pic") != 1)]
        a = [s for s in streams if (s or {}).get("codec_type") == "audio"]
        r.update(size=_int(fmt.get("size")), dur=_num(fmt.get("duration")), container=fmt.get("format_name"))
        if v:
            s = v[0]
            r.update(vcodec=s.get("codec_name"), profile=s.get("profile"), w=_int(s.get("width")),
                     h=_int(s.get("height")), fps=_rate(s.get("r_frame_rate")), field=s.get("field_order"),
                     pix=s.get("pix_fmt"), color=s.get("color_space"))
        r["audio"] = [(s.get("codec_name"), _int(s.get("channels"))) for s in a]
    except Exception as e:                                            # noqa: BLE001
        r["error"] = "%s: %s" % (type(e).__name__, e)
    return r


def resume(res):
    c = collections.Counter()
    for r in res:
        if r.get("error"):
            c["ERREUR"] += 1
            continue
        a = r.get("audio") or []
        pistes = "sans audio" if not a else "%d piste(s) %s" % (len(a), "+".join(sorted({str(x[1]) for x in a})) + " can.")
        c["%s %sx%s %s %s · %s" % (r.get("vcodec") or "audio seul", r.get("w") or "", r.get("h") or "",
                                    r.get("fps") or "", r.get("field") or "", pistes)] += 1
    for k, n in c.most_common():
        print("%4d  %s" % (n, k))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "inventaire.json"
    root = sys.argv[2] if len(sys.argv) > 2 else "/mnt/bobi-ext/conn/1"
    dirs = dict(x.split("=", 1) for x in sys.argv[3:]) or {"Clients": "OMV-STC-Clients", "TEmp": "OMV-STC-TEmp"}
    files = []
    for d, alias in dirs.items():
        base = os.path.join(root, d)
        for r, _ds, fs in os.walk(base):
            for f in fs:
                if f.lower().endswith(EXTS) and not f.startswith("."):
                    files.append((os.path.join(r, f), alias + os.path.join(r, f)[len(base):]))
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(probe, files))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    print("%d fichiers inventoriés (%d en erreur) → %s" % (len(res), sum(1 for r in res if r.get("error")), out))
    resume(res)


if __name__ == "__main__":
    main()
