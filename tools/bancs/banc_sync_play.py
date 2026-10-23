# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""BANC — écart RÉEL entre les canaux d'une lecture synchrone, lu sur le BUS MXL (pas dans les témoins).

Ce que les players disent d'eux-mêmes (témoin `/sync`) est une déclaration ; ce banc lit ce qu'ils ont
ÉCRIT. Il s'exécute DANS un conteneur d'un nœud (image média : bobimxl + numpy), sur les flux de ce
nœud-là, et sort pour chaque flux une suite (index de grille, n° d'image lu dans l'image) et les
débuts de salves audio (index d'échantillon). Deux flux d'un même nœud se comparent directement ;
deux nœuds se comparent en rapprochant leurs sorties (`--compare a.json b.json`) : l'écart sur la
grille, auquel il faut ajouter l'écart des horloges (Réglages → Réseau → Horloges) pour l'écart
physique — la grille d'un nœud EST son horloge, aucun flux ne peut le révéler seul.

Média de mesure : `SyncTest/sync_test_*.mov` (fabriqué par ffmpeg, cf. docs/reference/SYNC_PLAY.md) —
le n° d'image est écrit en binaire dans les 40 premières lignes (32 cases de 40 px, blanc = 1), et
une salve de 10 ms à 1 kHz marque le début de chaque seconde (image n° multiple de 25 à 25 i/s).

  docker exec -i <conteneur> python3 - <flux1> [<flux2> …] [--seconds 20] < tools/bancs/banc_sync_play.py
  python3 tools/bancs/banc_sync_play.py --compare a.json b.json      (code de sortie 1 = défaut de synchro)

Tolérance ZÉRO : une seule image d'écart, ou une seule image répétée sur un flux et pas sur l'autre, fait
échouer le banc (`verdict`, gardé hors ligne par tests/verif_banc_sync_play.py). `--different-content` : les
flux lisent des médias différents (fill / key d'un autre clip) — on exige alors un écart CONSTANT.

Exige un système vivant : ne tourne pas en CI (cf. CLAUDE.md, « Trois dossiers »).
"""
import json
import sys
import time

BITS, CELL, ROW = 32, 40, 20


def decode_frame_no(y, width):
    """N° d'image écrit en binaire dans la bande du haut ; None si la bande n'est pas lisible."""
    n = 0
    for b in range(BITS):
        x = b * CELL + CELL // 2
        if x >= width:
            return None
        v = int(y[ROW * width + x])
        if 60 < v < 190:                     # ni noir ni blanc : pas notre mire (ou image mêlée)
            return None
        if v >= 190:
            n |= 1 << b
    return n


def capture(names, seconds):
    sys.path.insert(0, "/opt/script")
    import numpy as np
    import bobimxl
    inst = bobimxl.Instance()
    vids, auds = {}, {}
    for n in names:
        vids[n] = bobimxl.Reader(inst, n)
        try:
            auds[n] = bobimxl.AudioReader(inst, n + "_audio")
        except Exception:                                  # noqa: BLE001
            auds[n] = None
    fmt = vids[names[0]].format()
    w = fmt["width"]
    seen = {n: {} for n in names}            # k → n° d'image
    onsets = {n: [] for n in names}          # index d'échantillon des débuts de salve
    a_next = {n: None for n in names}
    a_prev_loud = {n: False for n in names}     # index du dernier échantillon fort (False = aucun)
    t_end = time.time() + seconds
    while time.time() < t_end:
        for n in names:
            r = vids[n]
            head = r.head_index()
            for k in range(max(0, head - 6), head + 1):
                if k in seen[n]:
                    continue
                g = r.get(k, timeout_ns=1_000_000)
                if g is None:
                    continue
                seen[n][k] = decode_frame_no(g[2], w)
            ar = auds[n]
            if ar is None:
                continue
            ah = ar.head_index()
            if a_next[n] is None:
                a_next[n] = ah - 4800
            cnt = ah - a_next[n]
            if cnt <= 0:
                continue
            blk = ar.read_from(a_next[n], cnt)
            if blk is None:
                a_next[n] = ah
                continue
            x = np.abs(np.asarray(blk)[:, 0])
            # Début de salve = premier échantillon fort après au moins 50 ms de silence (une salve
            # de 1 kHz repasse par zéro deux fois par milliseconde : sans ce seuil, 20 « débuts »).
            for i in np.nonzero(x > 0.1)[0]:
                s_abs = int(a_next[n] + i)
                if a_prev_loud[n] is False or s_abs - a_prev_loud[n] > 2400:
                    onsets[n].append(s_abs)
                a_prev_loud[n] = s_abs
            a_next[n] += cnt
        time.sleep(0.01)
    rate = (fmt["fps_num"], fmt["fps_den"])
    return {"rate": rate, "frames": {n: {str(k): v for k, v in seen[n].items()} for n in names},
            "onsets": onsets, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def analyse(cap_list):
    """Écarts entre flux : vidéo au même index de grille, audio par salve, A/V par flux."""
    frames, onsets, rate = {}, {}, None
    for c in cap_list:
        rate = rate or c["rate"]
        frames.update({n: {int(k): v for k, v in f.items()} for n, f in c["frames"].items()})
        onsets.update(c["onsets"])
    num, den = rate
    period = den / float(num)
    names = sorted(frames)
    out = {"rate": rate, "flows": names, "video": {}, "audio": {}, "av": {}}
    ref = names[0]
    for n in names[1:]:
        common = sorted(set(frames[ref]) & set(frames[n]))
        d = [frames[n][k] - frames[ref][k] for k in common
             if frames[ref][k] is not None and frames[n][k] is not None]
        # Désynchro passagère : n répète (même n° qu'au grain d'avant) pendant que la référence avance, ou
        # l'inverse — compté en images, des deux côtés.
        rep_n = 0
        for k in common:
            a0, a1, b0, b1 = frames[ref].get(k - 1), frames[ref][k], frames[n].get(k - 1), frames[n][k]
            if None in (a0, a1, b0, b1):
                continue
            if (a1 == a0) != (b1 == b0):
                rep_n += 1
        out.setdefault("repeats", {})[n] = rep_n
        out["video"][n] = {"vs": ref, "grains": len(d), "offsets": sorted(set(d)),
                           "max_abs": max([abs(x) for x in d] or [0]),
                           "unreadable": len(common) - len(d)}
    # A/V : la salve marque le début d'une seconde du média, c.-à-d. l'image n° fps·s. Son instant
    # (index d'échantillon / 48 kHz) est comparé à celui du PREMIER grain qui montre cette image.
    fps_i = int(round(1 / period))
    for n in names:
        av = []
        for s in onsets.get(n) or []:
            t = s / 48000.0
            k0 = int(t / period)
            shown = frames[n].get(k0)
            if shown is None:
                continue
            n0 = int(round(shown / float(fps_i))) * fps_i
            # Premier grain qui montre l'image n0, cherché AUTOUR de la salve (une playlist repasse par
            # les mêmes n° d'image dans d'autres clips : le premier du fichier serait le mauvais).
            ks = [k for k in range(k0 - fps_i, k0 + fps_i) if frames[n].get(k) == n0]
            if ks:
                av.append(round((t - min(ks) * period) * 1000.0, 2))
        out["av"][n] = {"bursts": len(av), "audio_minus_video_ms": sorted(set(av))[:12]}
    # Ruptures de continuité (changement de ligne, recherche, boucle) : l'index de grille où chaque flux
    # passe à une autre image que la suivante — deux canaux synchrones rompent au MÊME index.
    for n in names:
        cuts, prev = [], None
        for k in sorted(frames[n]):
            v = frames[n][k]
            if v is None:
                continue
            if prev is not None and v != prev and abs(v - prev - 1) > 1:
                cuts.append([k, prev, v])
            prev = v
        out.setdefault("cuts", {})[n] = cuts
    for n in names[1:]:
        a, b = onsets.get(ref) or [], onsets.get(n) or []
        pairs = []
        for s in b:
            m = min(a, key=lambda x: abs(x - s)) if a else None
            if m is not None and abs(m - s) < 24000:
                pairs.append(s - m)
        out["audio"][n] = {"vs": ref, "bursts": len(pairs), "offsets_samples": sorted(set(pairs))[:12]}
    return out


def verdict(an, same_content=True):
    """TOLÉRANCE ZÉRO (décision 2026-10-17 : un fill et son key doivent être exactement ensemble). Renvoie la
    liste des DÉFAUTS — vide = synchrone :
      · décalage qui RESTE : au même index de grille, n° d'image différent (contenu identique : écart ≠ 0 ;
        contenus différents, `same_content=False` : écart non constant) ;
      · désynchro PASSAGÈRE : un flux répète une image pendant qu'un autre avance (fill qui répète pendant que
        le key avance), avec le nombre d'images ;
      · ruptures (changement de ligne, recherche) à des index différents ;
      · audio décalé d'un échantillon ou plus.
    Un écart d'UNE image suffit à faire échouer le banc."""
    bad = []
    for n, v in an["video"].items():
        offs = v["offsets"]
        if same_content and offs and offs != [0]:
            bad.append("%s : décalage de %s image(s) sur %s" % (n, offs, v["vs"]))
        elif not same_content and len(offs) > 1:
            bad.append("%s : écart non constant %s sur %s" % (n, offs, v["vs"]))
    for n, r in (an.get("repeats") or {}).items():
        # Contenus différents : un clip plus court qui TIENT sa dernière image (fin au plus long) n'est pas
        # une désynchro — la répétition n'est jugée que sur un même contenu (paire fill / key incluse).
        if r and same_content:
            bad.append("%s : désynchro passagère, %d image(s) répétée(s) pendant que l'autre avançait" % (n, r))
    cuts = {n: [c[0] for c in v] for n, v in (an.get("cuts") or {}).items()}
    if len({tuple(v) for v in cuts.values()}) > 1:
        bad.append("ruptures à des index différents : %s" % cuts)
    for n, v in an["audio"].items():
        if any(x != 0 for x in v["offsets_samples"]):
            bad.append("%s : audio décalé de %s échantillon(s)" % (n, v["offsets_samples"]))
    return bad


if __name__ == "__main__":
    args = sys.argv[1:]
    diff = "--different-content" in args
    args = [a for a in args if a != "--different-content"]
    if args and args[0] == "--compare":
        an = analyse([json.load(open(p)) for p in args[1:]])
        an["defects"] = verdict(an, same_content=not diff)
        print(json.dumps(an, indent=1))
        sys.exit(1 if an["defects"] else 0)
    secs = 20.0
    if "--seconds" in args:
        i = args.index("--seconds")
        secs = float(args[i + 1])
        del args[i:i + 2]
    raw = "--raw" in args
    args = [a for a in args if a != "--raw"]
    cap = capture(args, secs)
    if raw:
        print(json.dumps(cap, indent=1))
        sys.exit(0)
    an = analyse([cap])
    an["defects"] = verdict(an, same_content=not diff)
    print(json.dumps(an, indent=1))
    sys.exit(1 if an["defects"] else 0)
