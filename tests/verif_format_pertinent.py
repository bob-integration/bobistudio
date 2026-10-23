#!/usr/bin/env python3
"""Garde : le formulaire de création ne propose un FORMAT VIDÉO qu'aux plugins qui en ont l'usage.

Avant (2026-10-17), `video_format` absent valait « oui » : un plugin purement audio ou sans flux
(transcoder) demandait un format que rien n'utilise. Règle : la déclaration explicite fait foi,
sinon le câblage décide (vidéo produite ou consommée).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.plugins import needs_video_format          # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


V = {"essence": "video", "shm": "x"}
A = {"essence": "audio", "shm": "y"}
verifier(needs_video_format({"wiring": {"produces": [V]}}), "produit de la vidéo, sans déclaration : oui")
verifier(needs_video_format({"wiring": {"consumes": [V]}}), "consomme de la vidéo, sans déclaration : oui")
verifier(needs_video_format({"wiring": {"produces": [{"shm": "z"}]}}), "essence absente = vidéo (convention du câblage) : oui")
verifier(not needs_video_format({"wiring": {"produces": [A]}}), "★ purement audio, sans déclaration : non")
verifier(not needs_video_format({}), "★ aucun flux (transcoder) : non")
verifier(not needs_video_format({"video_format": False, "wiring": {"produces": [V]}}), "déclaration false : fait foi")
verifier(needs_video_format({"video_format": True, "wiring": {"produces": [A]}}), "déclaration true : fait foi")

print()
if ECHECS:
    print("ÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("TOUT PASSE")
