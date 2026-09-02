#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# EMPREINTE du modèle de tally — le filet du chantier de séparation TSL / modèle.
#
# ★ POURQUOI CELUI-CI EN PLUS DES ONZE AUTRES. Les bancs existants vérifient chacun une
# propriété, et bien. Celui-ci fait autre chose : il rejoue une séquence FIXE et compare la
# sortie mot pour mot à une référence enregistrée. Il n'attrape pas « la propriété P est
# fausse » mais « quelque chose a changé, et personne ne l'avait demandé » — le seul défaut
# qu'un refactoring produit vraiment.
#
# Il visait `services.tsl` — qui portait alors le modèle ET le protocole — et vise `app.tally`
# depuis que la séparation est faite. La référence, elle, N'A PAS BOUGÉ : elle a été figée AVANT
# le chantier, sur l'ancien module, et c'est exactement ce qui lui donne sa valeur. Le module
# extrait doit s'y conformer, pas s'y substituer.
#
# `--module services.tsl` ne fonctionne plus, et c'est voulu : ce module n'a plus d'état de
# tally à montrer. Le pont de réexports qui l'aurait laissé passer a été retiré.
#
#   $ ./venv/bin/python tests/verif_tally_empreinte.py                  # vérifie
#   $ ./venv/bin/python tests/verif_tally_empreinte.py --enregistrer    # fige la référence
#   $ ./venv/bin/python tests/verif_tally_empreinte.py --module app.tally
import importlib
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
REF = None   # calculé après lecture de --module : une référence PAR module

MODULE = "app.tally"
ENREGISTRER = False
for i, a in enumerate(sys.argv[1:]):
    if a == "--enregistrer":
        ENREGISTRER = True
    elif a == "--module":
        MODULE = sys.argv[i + 2]
m = importlib.import_module(MODULE)

# ★ UNE SEULE RÉFÉRENCE, ET C'EST TOUT L'INTÉRÊT. Enregistrer une référence PAR module
# rendrait le banc circulaire : on figerait ce que `app.tally` fait, puis on vérifierait
# qu'il fait bien ce qu'il fait — ce qui ne prouve rien. La référence est celle prise sur
# `services.tsl` AVANT le chantier ; le module extrait doit s'y conformer, pas s'y substituer.
REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verif_tally_empreinte.json")

NA, NB = "niveau-a", "niveau-b"


def _etat():
    """Les DEUX couches : le cumul que lisent les consommateurs, et qui affirme quoi."""
    return {"cumul": dict(sorted(m.get_tally_state().items())),
            "sources": {k: dict(sorted(v.items()))
                        for k, v in sorted(m.sources_du_tally().items())}}


def _vider():
    for src in list({s for par in m._tally_par_source.values() for s in par}):
        m.poser_tally(src, {}, reveiller=False)


SEQ = []


def pas(intitule, fn):
    """Un pas = un geste, puis l'état complet. C'est la séquence qui fait l'empreinte."""
    r = fn()
    SEQ.append({"pas": intitule, "retour": r, "etat": _etat()})


# ─── A2/A5/A7 — cumul, retrait, idempotence ──────────────────────────────────
_vider()
pas("A pose rouge", lambda: m.poser_tally("srcA", {(5, NA): "red"}))
pas("A repose l'identique (doit rendre False)", lambda: m.poser_tally("srcA", {(5, NA): "red"}))
pas("B pose vert sur la MEME case", lambda: m.poser_tally("srcB", {(5, NA): "green"}))
pas("B dit 'off' — c'est un RETRAIT, pas une couleur", lambda: m.poser_tally("srcB", {(5, NA): "off"}))
pas("B repose vert", lambda: m.poser_tally("srcB", {(5, NA): "green"}))
pas("A se tait — B ne doit PAS etre coupe", lambda: m.poser_tally("srcA", {}))
pas("B se tait — la case doit DISPARAITRE", lambda: m.poser_tally("srcB", {}))

# ─── A4 — remplacement integral de la contribution d'une source ──────────────
_vider()
pas("A pose deux cases", lambda: m.poser_tally("srcA", {(1, NA): "red", (2, NA): "green"}))
pas("C pose sur la case 1", lambda: m.poser_tally("srcC", {(1, NA): "green"}))
pas("A ne garde que la case 3 — 1 et 2 tombent, C SURVIT",
    lambda: m.poser_tally("srcA", {(3, NA): "red"}))

# ─── A8 — etancheite des niveaux ─────────────────────────────────────────────
_vider()
pas("meme index, deux niveaux", lambda: m.poser_tally("srcA", {(9, NA): "red", (9, NB): "green"}))
pas("lecture niveau a", lambda: m.get_tally_level(9, NA))
pas("lecture niveau b", lambda: m.get_tally_level(9, NB))
pas("lecture d'une case vide", lambda: m.get_tally_level(9, "niveau-inexistant"))

# ─── A3 — table de verite du cumul ───────────────────────────────────────────
_vider()
CUMUL = [[a, b, m.cumuler(a, b)] for a in ("off", "red", "green", "amber", None)
         for b in ("off", "red", "green", "amber", None)]

# ─── D8 — l'aller-retour du PROTOCOLE, seulement si le module en porte un ────
# ⚠ Ce banc sert DEUX modules : `services.tsl` (protocole + modèle aujourd'hui) et `app.tally`
# (le modèle seul). L'encodage de trame n'existe que du côté protocole — l'exiger des deux
# ferait échouer le modèle pour la seule raison qu'il fait correctement son travail.
if hasattr(m, "encode_tsl_frame"):
    TRAMES = [m.encode_tsl_frame(i, c, t).hex()
              for i, c, t in ((0, 0, ""), (1, 1, "CAM 1"), (255, 0x3F, "ÉÀ"), (4096, 42, "x" * 32))]
    CONTROLES = [m.build_control(r, g, rf, vf)
                 for r in (False, True) for g in (False, True)
                 for rf, vf in (("tt", "lh"), ("rh", "tt"), ("lh", "rh"), ("inconnu", "inconnu"))]
else:
    TRAMES = CONTROLES = "∅ module sans protocole"

# ─── C1 — resolution de reference ────────────────────────────────────────────
REFS = [[r, m.resolve_ref(r)] for r in ("", "  ", "cam1", " cam1 ", "port:", "port:abc",
                                        "port:999999", "/dev/shm/x")]

EMPREINTE = {"sequence": SEQ, "cumuler": CUMUL, "trames": TRAMES,
             "controles": CONTROLES, "resolve_ref": REFS}
_vider()

if ENREGISTRER:
    with open(REF, "w", encoding="utf-8") as f:
        json.dump(EMPREINTE, f, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    print(f"  référence enregistrée : {REF}")
    sys.exit(0)

if not os.path.exists(REF):
    print("  ✗ aucune référence — lancez d'abord --enregistrer")
    sys.exit(2)
with open(REF, encoding="utf-8") as f:
    attendu = json.load(f)
obtenu = json.loads(json.dumps(EMPREINTE, ensure_ascii=False, sort_keys=True, default=str))

SANS_PROTOCOLE = "\u2205 module sans protocole"
ecarts, exemptes = [], []
for cle in sorted(set(attendu) | set(obtenu)):
    a, b = attendu.get(cle), obtenu.get(cle)
    if a == b:
        continue
    # ★ EXEMPTION EXPLICITE, ET SEULEMENT CELLE-CI. Un module qui ne porte pas le protocole
    # n'a ni trames ni mots de contrôle : le lui reprocher serait lui reprocher d'avoir été
    # correctement séparé. Toute AUTRE différence reste un écart — c'est la nuance qui
    # empêche cette exemption de devenir un trou.
    if b == SANS_PROTOCOLE and cle in ("trames", "controles"):
        exemptes.append(cle)
        continue
    if cle == "sequence" and isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            pa = a[i] if i < len(a) else None
            pb = b[i] if i < len(b) else None
            if pa != pb:
                ecarts.append(f"pas {i} « {(pb or pa or {}).get('pas', '?')} »\n"
                              f"        attendu : {json.dumps(pa, ensure_ascii=False)}\n"
                              f"        obtenu  : {json.dumps(pb, ensure_ascii=False)}")
    else:
        ecarts.append(f"{cle}\n        attendu : {json.dumps(a, ensure_ascii=False)[:200]}\n"
                      f"        obtenu  : {json.dumps(b, ensure_ascii=False)[:200]}")

if ecarts:
    print(f"\n  ✗ {len(ecarts)} écart(s) avec la référence — module {MODULE} :\n")
    for e in ecarts[:12]:
        print("    " + e)
    if len(ecarts) > 12:
        print(f"    … et {len(ecarts) - 12} autre(s)")
    print("\n  Si le changement est VOULU, relancez avec --enregistrer et relisez le diff du .json.")
    sys.exit(1)
note = f", protocole absent ({', '.join(exemptes)}) — attendu" if exemptes else ""
print(f"  ✓ empreinte identique à la référence ({len(SEQ)} pas, module {MODULE}{note})")
