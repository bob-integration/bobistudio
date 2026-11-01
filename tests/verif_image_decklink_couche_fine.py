#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le socle partagé `bobi-compute` ne dépend plus d'un plugin OPTIONNEL (2026-11-01).

★ CE QUE CETTE GARDE PROUVE, ET POURQUOI ELLE EXISTE.
Les quatre binaires Blackmagic (decklink_mxl, decklink_tx, decklink_probe, decklink_profile)
étaient compilés DANS `bobi-compute`, l'image de base de TOUS les plugins non-2110. Deux effets,
tous deux contraires à ce que le produit annonce — on installe ce dont on a besoin :

  · un nœud sans carte DeckLink portait quand même les binaires ;
  · surtout, le socle ne se mettait même pas EN CONTEXTE sans les sources de ce plugin :
    `_stage_tar` lève dès qu'un seul chemin de la liste manque. Le dépôt public publie la recette
    mais pas le plugin (sous-module privé) → AUCUNE image ne se construisait, et le produit ne
    faisait rien. Signalé de l'EXTÉRIEUR le 2026-09-23, resté six semaines sans réponse : rien
    chez nous ne pouvait le voir, puisque notre arbre de travail a toujours tous les sous-modules.

Le contrôle central (section 2) rejoue donc cette situation au lieu de la décrire : un arbre
temporaire qui porte tout ce que l'image compute réclame SAUF `plugins/decklink_io/`, et le
staging doit réussir. Remettre `_DECKLINK` dans la liste de l'image compute le fait échouer.

Hors ligne : base jetable, aucun réseau, aucun nœud, aucun docker. Sections :
  0. le décor est bien le vrai (les listes lues sont celles du produit) ;
  1. l'image `decklink` existe, en couche fine node_only conditionnée à la capacité ;
  2. ★ le socle se stage SANS le plugin — la situation du rapporteur ;
  3. la recette de la couche fine est complète (tout COPY a son entrée de staging) ;
  4. le socle ne porte plus ni l'étage ni les binaires ;
  5. le plugin part sur cette image, et la capacité exige « compute » ;
  6. les deux listes qui FILTRENT nomment la nouvelle image (page Nœuds, variantes) ;
  7. le refus de déploiement sans la capacité est traduit, FR et EN.

    ./venv/bin/python tests/verif_image_decklink_couche_fine.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
_TMP = tempfile.mkdtemp(prefix="verif_decklink_couche_")
from app import config, database                                          # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "db.sqlite")  # AVANT tout accès (get_db épingle)

from app.routes import images as IM                                       # noqa: E402
from app.routes import nodes as ND                                        # noqa: E402

ko = []
abstenus = []

# ★ CE QUE CETTE GARDE DOIT FAIRE DANS LE DÉPÔT PUBLIC (2026-11-01). `plugins/decklink_io` est un
# sous-module PRIVÉ : l'arbre public ne le porte pas, et c'est la situation NORMALE là-bas, pas une
# anomalie. Trois sections lisent ses fichiers — elles s'abstiennent explicitement plutôt que de
# rendre rouge une CI qui n'y peut rien. Le contrôle CENTRAL (section 2), lui, tourne dans les deux
# arbres, et c'est précisément celui dont le dépôt public a besoin : il prouve que le socle partagé
# se met en contexte sans ce plugin — le défaut signalé de l'extérieur le 2026-09-23.
#
# ⚠ Une abstention MUETTE serait pire que l'échec : elle laisserait croire que tout a été mesuré.
# Le mot « NON MESURÉ » est celui que le récapitulatif de la CI cherche (cf. .github/workflows/ci.yml,
# « PLUSIEURS VOCABULAIRES, PAS UN SEUL ») — ne pas le reformuler.
_PLUGIN = os.path.isdir(os.path.join(_R, "plugins/decklink_io"))


def abstention(titre, pourquoi):
    print("  NON MESURÉ  %s" % titre)
    print("              → %s" % pourquoi)
    abstenus.append(titre)


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        if detail:
            print("         %s" % (detail,))
        ko.append(titre)


def lire(rel):
    with open(os.path.join(_R, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


# ─── 0. Le décor est le vrai ─────────────────────────────────────────────────────────
print("\n0. Le décor est bien celui du produit")
ok("compute" in IM._IMAGES and "gui" in IM._IMAGES,
   "les images du produit sont lues depuis _IMAGES", sorted(IM._IMAGES))
ok(isinstance(IM._DECKLINK, list) and len(IM._DECKLINK) >= 6,
   "_DECKLINK énumère toujours les sources du plugin", repr(IM._DECKLINK)[:120])
if _PLUGIN:
    print("  ok   le sous-module decklink_io est présent : les 3 sections qui le lisent tournent")
else:
    abstention("les sections 3 et 5 lisent le sous-module decklink_io",
               "arbre SANS ce sous-module (dépôt public) — la section 2 reste mesurée, "
               "et c'est elle qui porte le verdict qui compte ici")

# ─── 1. L'image existe, en couche fine ──────────────────────────────────────────────
print("\n1. L'image « decklink » est une couche fine node_only")
spec = IM._IMAGES.get("decklink") or {}
ok(bool(spec), "l'image « decklink » est déclarée dans _IMAGES")
ok(spec.get("node_only") is True, "node_only : construite sur le nœud, jamais poussée à la flotte")
ok(spec.get("base_from") == "compute", "base_from = compute (le FROM est réécrit au staging)")
ok(spec.get("cap") == "decklink", "conditionnée à la capacité « decklink »")
ok(spec.get("setting") == "decklink_image" and spec.get("field") == "decklink_image",
   "tag commun dans le réglage decklink_image (pas de colonne nœud)")
ok((spec.get("prefix") or "") == "bobi-decklink", "préfixe de tag bobi-decklink")

# ─── 2. ★ LE CONTRÔLE CENTRAL : le socle se stage SANS le plugin ─────────────────────
print("\n2. ★ Le socle partagé se construit SANS les sources du plugin (cas du rapporteur)")
faux = os.path.join(_TMP, "arbre-sans-decklink")
manquants = []
for src, _dst in IM._IMAGES["compute"]["stage"]:
    s = os.path.join(_R, src)
    d = os.path.join(faux, src)
    if not os.path.exists(s):
        manquants.append(src)
        continue
    os.makedirs(os.path.dirname(d), exist_ok=True)
    if os.path.isdir(s):
        shutil.copytree(s, d, dirs_exist_ok=True)
    else:
        shutil.copy2(s, d)
ok(not manquants, "toutes les sources de l'image compute existent dans le dépôt", manquants)
ok(not os.path.exists(os.path.join(faux, "plugins/decklink_io")),
   "l'arbre d'essai ne porte AUCUN fichier de plugins/decklink_io")

_vrai_root = IM._repo_root
IM._repo_root = lambda: faux
try:
    tar = IM._stage_tar("compute")
    ok(isinstance(tar, bytes) and len(tar) > 0,
       "le contexte de build de bobi-compute se construit sans le plugin", "tar vide")
except Exception as e:                                             # noqa: BLE001 — c'est le verdict
    ok(False, "le contexte de build de bobi-compute se construit sans le plugin",
       "%s: %s" % (type(e).__name__, e))
finally:
    IM._repo_root = _vrai_root

# ─── 3. La recette de la couche fine est complète ────────────────────────────────────
print("\n3. La recette de bobi-decklink est complète")


def section_3():
    """Tout ce qui LIT le sous-module privé : absent du dépôt public (cf. _PLUGIN)."""
    try:
        tar = IM._stage_tar("decklink")
        ok(isinstance(tar, bytes) and len(tar) > 0,
           "le contexte de build de bobi-decklink se construit (tout COPY est stagé)")
    except Exception as e:                                         # noqa: BLE001 — c'est le verdict
        ok(False, "le contexte de build de bobi-decklink se construit (tout COPY est stagé)",
           "%s: %s" % (type(e).__name__, e))

    dk = lire("plugins/decklink_io/image/Dockerfile")
    ok(re.search(r"(?m)^ARG\s+BASE_IMAGE=", dk) is not None,
       "le Dockerfile porte un `ARG BASE_IMAGE=` (sans quoi base_from ne réécrit rien)")
    ok(dk.count("FROM ${BASE_IMAGE}") >= 2,
       "les deux étages partent de l'image de base (builder + runtime)")
    # ⚠ CHERCHER LES NOMS « ldd » ET « libDeckLinkAPI » DANS LE FICHIER NE PROUVE RIEN : ils
    # figurent dans le COMMENTAIRE qui explique le garde-fou, donc la garde restait verte alors
    # que la condition avait été remplacée par `if false` (mutation M13, 2026-11-01). On lit donc
    # le CODE, commentaires retirés, et on exige la condition elle-même.
    _code = "\n".join(l for l in dk.splitlines() if not l.lstrip().startswith("#"))
    ok(re.search(r'if\s+ldd\s+/usr/local/bin/\$b.*grep\s+-qv\s+"libDeckLinkAPI"', _code) is not None,
       "le garde-fou ldd est une CONDITION du code (libDeckLinkAPI écartée : filet, cf. dlopen)")
    ok("exit 1" in _code, "et il fait échouer le build (exit 1), pas seulement un message")

    meta_dk = json.loads(lire("plugins/decklink_io/image/meta.json"))
    ok(bool(meta_dk.get("version")) and bool(meta_dk.get("image_tag")),
       "le meta.json de l'image porte version ET image_tag")
    # ★ On n'invente pas la règle d'accord entre les deux : c'est `_image_tag` qui la porte, et il
    # LÈVE sur une divergence (bumper `version` en oubliant `image_tag` a failli écraser l'image
    # de référence du parc le 2026-09-12, cf. tests/verif_images_tags.py). On l'appelle donc.
    try:
        tag = IM._image_tag("decklink")
        ok(tag == meta_dk["image_tag"],
           "le tag se résout, et `version` ne diverge pas d'`image_tag`", repr(tag))
    except Exception as e:                                         # noqa: BLE001 — c'est le verdict
        ok(False, "le tag se résout, et `version` ne diverge pas d'`image_tag`",
           "%s: %s" % (type(e).__name__, e))


if _PLUGIN:
    section_3()
else:
    abstention("la recette de bobi-decklink (section 3)",
               "son Dockerfile et son meta.json vivent dans le sous-module privé")

# ─── 4. Le socle ne porte plus rien de DeckLink ──────────────────────────────────────
print("\n4. Le socle bobi-compute ne porte plus le chemin DeckLink")
cr = lire("plugins/_compute_runtime/Dockerfile")
ok("decklink-builder" not in cr, "plus d'étage decklink-builder dans le Dockerfile du socle")
ok(not re.search(r"(?m)^COPY\s+plugins/decklink_io", cr),
   "plus aucun COPY des sources du plugin dans le socle")
ok(not re.search(r"(?m)^COPY\s+--from=decklink-builder", cr),
   "plus aucun COPY depuis l'étage decklink dans l'étage livré")
ok(not any("decklink_io" in s for s, _d in IM._IMAGES["compute"]["stage"]),
   "la liste de staging de l'image compute ne nomme plus le plugin",
   [s for s, _d in IM._IMAGES["compute"]["stage"] if "decklink_io" in s])
ok(re.search(r"(?m)^COPY\s+--from=mxl-builder\s+/opt/mxl-install/include/", cr) is not None,
   "les en-têtes MXL restent dans l'image livrée (ce qui rend la couche fine compilable)")

# ─── 5. Le plugin part sur cette image ───────────────────────────────────────────────
print("\n5. Le plugin decklink_io déclare cette variante, et la capacité exige « compute »")
if _PLUGIN:
    man = json.loads(lire("plugins/decklink_io/plugin.json"))
    ok(man.get("image") == "decklink",
       "le manifeste déclare \"image\": \"decklink\"", repr(man.get("image")))
    ok(man.get("needs_decklink") is True,
       "le chemin d'EXÉCUTION est inchangé : needs_decklink toujours déclaré")
else:
    abstention("le manifeste du plugin (section 5)", "sous-module privé")
from app import plugins as PL                                             # noqa: E402
_sauve = PL.REGISTRY.get("decklink_io")
PL.REGISTRY["decklink_io"] = {"image": "decklink"}
try:
    ok(PL.image_variant("decklink_io") == "decklink",
       "image_variant accepte « decklink » (liste blanche étendue)")
    PL.REGISTRY["decklink_io"] = {"image": "nimportequoi"}
    ok(PL.image_variant("decklink_io") == "compute",
       "une variante inconnue retombe toujours sur compute")
finally:
    if _sauve is None:
        PL.REGISTRY.pop("decklink_io", None)
    else:
        PL.REGISTRY["decklink_io"] = _sauve
ok("decklink" in ND._CAP_ADDABLE, "la capacité « decklink » est déclarable sur un nœud")
ok(ND._CAP_REQUIRES.get("decklink") == "compute",
   "elle exige « compute » (la couche fine est un FROM bobi-compute)")

# ─── 6. Les listes qui FILTRENT nomment la nouvelle image ────────────────────────────
print("\n6. Les listes qui filtrent nomment la nouvelle image")
ok("decklink" in ND._IMG_ORDER,
   "_IMG_ORDER la nomme (absente, l'image ne s'affiche NULLE PART et personne ne la construit)")
ok("decklink" not in ND._SHARED_IMAGES,
   "elle n'est pas dans les images PARTAGÉES (node_only : jamais poussée à la flotte)")
ok(ND._node_wants("decklink", {}, ["compute", "decklink"]) is True
   and ND._node_wants("decklink", {}, ["compute"]) is False,
   "un nœud ne la veut QUE s'il déclare la capacité")

# ─── 7. Le refus de déploiement est traduit ──────────────────────────────────────────
print("\n7. Le refus sans capacité est traduit, FR et EN")
dc = lire("app/docker_compute.py")
ok('variant in ("gui", "decklink")' in dc,
   "docker_compute traite la variante en couche fine (tag du réglage + contrôle de capacité)")
ok("alert.deploy.compute.sans_capacite_decklink" in dc,
   "la clé d'alerte est écrite EN ENTIER dans le code (une clé assemblée échappe au contrôle i18n)")
for cat in ("fr", "en"):
    d = json.loads(lire("i18n/%s.json" % cat))
    cle = "alert.deploy.compute.sans_capacite_decklink"
    t = d.get(cle) or ""
    ok(bool(t) and "{n}" in t and "{vmid}" in t,
       "i18n/%s.json porte la clé, avec le nœud et le vmid" % cat, repr(t)[:90])

print()
if abstenus:
    print("── NON MESURÉ sur cet arbre (%d) : %s" % (len(abstenus), " · ".join(abstenus)))
if ko:
    print("✗ %d échec(s) : %s" % (len(ko), " · ".join(ko[:4])))
    sys.exit(1)
print("✓ tout est vert%s" % (" (avec %d abstention(s) ci-dessus)" % len(abstenus) if abstenus else ""))
sys.exit(0)
