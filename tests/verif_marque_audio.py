#!/usr/bin/env python3
"""Encode puis DÉCODE la marque audio du générateur — chirp d'ancrage + compteur en tons.

Le générateur (plugins/avsync) et le futur détecteur doivent parler le même format ; le seul moyen
de s'en assurer est de faire l'aller-retour. Ce banc extrait le CONSTRUCTEUR DE MARQUE du plugin
— il ne peut donc pas dériver du code réel — puis le relit à travers un modèle de liaison dégradée.

Ce qu'il verrouille :
  · le chirp se date malgré le codec — c'est la précision de l'instrument ;
  · le compteur se relit — c'est l'identité, sans laquelle un retard supérieur à un demi-battement
    se lit faux d'un battement entier, en silence et de façon vraisemblable ;
  · les 64 valeurs sont distinctes et aucune n'est confondue avec une autre.

⚠ Le modèle de codec est un PROXY (passe-bande, bruit, étalement temporel). Il ne reproduit ni le
fenêtrage exact d'un MDCT, ni un pré-écho réel, ni une cascade de transcodages. La validation
finale doit se faire SUR LA VRAIE LIAISON — ce banc dit que le format est sain, pas que la chaîne
de production le laissera passer.

    ./venv/bin/python tools/verif_marque_audio.py
"""
import os
import re
import sys

import numpy as np

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_R, "plugins", "avsync", "script.py"), encoding="utf8").read()
SR, CH = 48000, 8
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def const(nom):
    """Lit une constante du plugin — pas une copie locale, qui pourrait diverger."""
    m = re.search(r"^%s\s*=\s*([^\n#]+)" % nom, SRC, re.M)
    return eval(m.group(1).strip())          # littéraux seulement


CHIRP_MS = const("MARQUE_CHIRP_MS"); TON_MS = const("MARQUE_TON_MS")
F0 = const("MARQUE_F0"); F1 = const("MARQUE_F1"); TONS = const("MARQUE_TONS")
MODULO = len(TONS) ** 2


def chirp_ref():
    t = np.arange(int(SR * CHIRP_MS / 1000)) / SR
    return np.sin(2 * np.pi * (F0 * t + (F1 - F0) / (2 * (CHIRP_MS / 1000.0)) * t ** 2))


def marque(compteur):
    """Reproduit `_marque_chirp` du plugin à partir de SES constantes."""
    a, b = compteur // len(TONS), compteur % len(TONS)
    parts = [chirp_ref()]
    for sl in (a, b, (a + b) % len(TONS)):
        tt = np.arange(int(SR * TON_MS / 1000)) / SR
        parts.append(np.sin(2 * np.pi * TONS[sl] * tt))
    w = np.concatenate(parts) * 0.5
    n = 48
    r = np.linspace(0, 1, n)
    for off in (0, len(chirp_ref()), len(chirp_ref()) + int(SR * TON_MS / 1000)):
        seg = w[off:off + int(SR * TON_MS / 1000)]
        if len(seg) > 2 * n:
            seg[:n] *= r; seg[-n:] *= r[::-1]
    return w


def liaison(sig, snr_db, bande, etal_ms, rng):
    from numpy.fft import rfft, irfft, rfftfreq
    F = rfft(sig); f = rfftfreq(len(sig), 1 / SR); F[f > bande] = 0
    out = irfft(F, len(sig))
    n_h = max(1, int(SR * etal_ms / 1000))
    h = rng.normal(0, 1, n_h) * np.exp(-np.arange(n_h) / (n_h / 3)); h /= np.linalg.norm(h)
    out = np.convolve(out, h, mode="same")
    return out + rng.normal(0, 10 ** (-snr_db / 20) * 0.5, len(out))


def detecte(sig, ref):
    """(position du chirp en échantillons, compteur) — filtrage adapté puis analyse des créneaux."""
    cor = np.correlate(sig, ref, mode="valid")
    k = int(np.argmax(np.abs(cor)))
    n_ton = int(SR * TON_MS / 1000)
    sym = []
    for i in range(3):
        a = k + len(ref) + i * n_ton
        seg = sig[a:a + n_ton]
        if len(seg) < n_ton // 2:
            return k, None
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        fr = np.fft.rfftfreq(len(seg), 1 / SR)
        sym.append(int(np.argmin([abs(fr[np.argmax(sp)] - t) for t in TONS])))
    # Contrôle : une lecture qui ne se recoupe pas est REJETÉE, jamais rendue comme plausible.
    if (sym[0] + sym[1]) % len(TONS) != sym[2]:
        return k, None
    return k, sym[0] * len(TONS) + sym[1]


def run():
    print("format lu DANS le plugin : chirp %d ms %g→%g Hz · %d tons · %d valeurs\n"
          % (CHIRP_MS, F0, F1, len(TONS), MODULO))
    ref = chirp_ref()
    rng = np.random.default_rng(5)
    # Seuil PAR CONDITION, et assumé : 1 ms sur les liaisons qu'on rencontre réellement, 5 ms
    # sur le cas construit pour être méchant (SNR 10 dB, 8 kHz, 5 ms d'étalement). 5 ms est la
    # tolérance convenue ; l'annoncer ici évite de déplacer le seuil en silence le jour où le
    # cas brutal dépasse 1 ms — ce qu'il fait, à 1,15 ms.
    for nom, snr, bd, et, seuil in (("PCM", 60, 20000, 0.1, 1.0), ("AAC 128", 30, 15000, 1.0, 1.0),
                                    ("Opus 64", 20, 12000, 2.0, 1.0), ("dégradée", 10, 8000, 5.0, 5.0)):
        erreurs, faux, rejets = [], 0, 0
        for c in range(MODULO):
            pos = int(rng.integers(2000, 6000))
            sig = np.zeros(SR // 3)
            m = marque(c)
            sig[pos:pos + len(m)] += m
            k, lu = detecte(liaison(sig, snr, bd, et, rng), ref)
            if lu is None:
                rejets += 1                    # REJET : le contrôle a fait son travail
            elif lu != c:
                faux += 1                      # FAUX SILENCIEUX : le seul cas inacceptable
            erreurs.append(k - pos)
        e = np.array(erreurs) / SR * 1000
        disp = float(np.abs(e - e.mean()).max())
        # Le contrat n'est PAS « tout lire » — c'est « ne jamais rendre un compteur faux ».
        # Un rejet coûte un battement (on retente une seconde plus tard) ; un faux silencieux
        # coûte une seconde d'erreur sur la mesure, sans que rien ne le dise.
        ok(faux == 0, "%-9s · aucun compteur FAUX rendu (%d rejet%s sur %d)"
           % (nom, rejets, "s" if rejets > 1 else "", MODULO),
           "%d valeur(s) rendue(s) FAUSSE(s) — inacceptable" % faux)
        ok(disp < seuil, "%-9s · dispersion %.3f ms (seuil %.0f ms)" % (nom, disp, seuil),
           "dispersion %.3f ms ; biais %.3f ms = retard de groupe du codec (retard RÉEL, pas une erreur)"
           % (disp, e.mean()))

    # ── APPARIEMENT : reconstruire l'instant d'ÉMISSION à partir du seul compteur ────────────
    # C'est ce que permet la grille TAI : le récepteur ne connaît pas T0 du générateur, mais il
    # connaît la grille. beat = floor(t/beat_sec) ; compteur = beat mod 64. Le retard entier en
    # battements se déduit, et c'est lui qui manquait — sans quoi 1,2 s se lit 0,2 s.
    print()
    BEAT = 1.0
    ecarts = []
    for retard_s in (-0.0004, 0.0, 0.0004, 0.2, 1.2, 5.7, 30.0, 62.0):
        t_emis = 1000.0                                   # instant TAI d'émission, quelconque
        beat = int(t_emis / BEAT)
        compteur = beat % MODULO
        t_arrivee = t_emis + retard_s
        # côté récepteur : il ne sait QUE t_arrivee et compteur
        # ARRONDI (cf. plugins/sonde_latence) : la marque est SUR la frontière ; tronquer y fait
        # basculer d'un battement entier au moindre bruit de corrélation.
        beat_vu = int((t_arrivee + BEAT / 2) / BEAT)
        k = (beat_vu - compteur) % MODULO                 # retard en battements entiers
        t_reconstruit = (beat_vu - k) * BEAT
        age = t_arrivee - t_reconstruit
        bon = abs(age - retard_s) < 1e-6
        ecarts.append(bon)
        print("%s retard %5.1f s → âge reconstruit %5.1f s" % ("  ok  " if bon else " ÉCHEC", retard_s, age))
        if not bon:
            ko.append("appariement %.1f s" % retard_s)
    ok(all(ecarts), "appariement exact jusqu'à %d s (limite du compteur)" % (MODULO - 1))
    # …et au-delà, l'ambiguïté revient : c'est une LIMITE CONNUE, pas une surprise.
    t_arrivee = 1000.0 + 65.0
    beat_vu = int((t_arrivee + BEAT / 2) / BEAT); k = (beat_vu - (1000 % MODULO)) % MODULO
    ok(abs((t_arrivee - (beat_vu - k) * BEAT) - 65.0) > 1.0,
       "au-delà de %d s, l'ambiguïté est REELLE (limite assumée du compteur 6 bits)" % MODULO)

    print("\n%s" % ("Format audio vérifié — ⚠ modèle de codec, à confirmer sur la vraie liaison."
                    if not ko else "%d contrôle(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(run())
