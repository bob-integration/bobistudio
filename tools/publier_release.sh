#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# publier_release.sh — construit le paquet de distribution et le publie en release GitHub.
#
# ⚠ CE N'EST PAS un prérequis de l'installation. `get.sh` récupère la source par archives (curl),
# sans release ni zip. Ce script sert aux cas où l'on veut un ARTEFACT FIGÉ et vérifiable :
# un site fermé qui installe hors ligne, une livraison client avec empreinte SHA-256, ou une
# version archivée telle qu'elle a été livrée. Le paquet produit est aussi celui qu'une instance
# déjà installée sert à ses nœuds (`/install.sh`).
#
#     tools/publier_release.sh v1.4.0
#     tools/publier_release.sh v1.4.0 --brouillon      # release en brouillon (relecture avant publication)
#     tools/publier_release.sh v1.4.0 --avec-modules-prives   # assume la publication de modules non publics
#
# Ce qui est publié (et rien d'autre) :
#   · bobistudio.zip — le paquet construit par app/builder.py : sous-modules APLATIS en fichiers
#     ordinaires, installeur embarqué, garde-fou anti-secret passé. C'est le seul artefact qui
#     produise une installation complète (une archive de code source GitHub, elle, livrerait des
#     dossiers plugins/ et services/ vides — cf. l'en-tête de get.sh) ;
#   · SHA256SUMS — l'empreinte que get.sh vérifie avant d'exécuter quoi que ce soit en root.
#
# Prérequis : `gh` authentifié (gh auth login), et un dépôt propre au commit qu'on publie.
set -euo pipefail

cd "$(cd "$(dirname "$0")/.." && pwd)"

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "  ${c_b}·${c_0} $*"; }; ok(){ echo "  ${c_g}✓${c_0} $*"; }
warn(){ echo "  ${c_y}!${c_0} $*"; }; die(){ echo "  ${c_r}✗${c_0} $*" >&2; exit 1; }

TAG="${1:-}"; shift || true
BROUILLON=0
AVEC_PRIVES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --brouillon|--draft) BROUILLON=1; shift;;
    # Confirme d'avance qu'on publie des modules non encore publics (cf. l'alerte
    # plus bas). Nécessaire hors terminal ; en interactif, la question est posée.
    --avec-modules-prives) AVEC_PRIVES=1; shift;;
    *) die "option inconnue : $1";;
  esac
done
[ -n "$TAG" ] || die "usage : tools/publier_release.sh <tag> [--brouillon]   (ex. v1.4.0)"

command -v gh >/dev/null 2>&1 || die "gh est requis (https://cli.github.com), puis « gh auth login »."
gh auth status >/dev/null 2>&1 || die "gh n'est pas authentifié — lancer « gh auth login »."

# Une release est un point FIXE auquel des machines vont se référer pendant des mois : la publier
# depuis un arbre modifié rendrait le tag mensonger, et personne ne pourrait reconstruire l'artefact.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  warn "l'arbre de travail contient des modifications non commitées :"
  git status --short --untracked-files=no | head -10
  die "publier depuis un arbre propre (committer, ou stasher)."
fi

log "construction du paquet de distribution…"
# Un seul chemin de build : l'enveloppe CLI existante, pas un appel direct au module.
./venv/bin/python tools/build_dist.py --all || die "build échoué (voir ci-dessus)."

ZIP="dist/bobistudio.zip"
[ -f "$ZIP" ] || die "$ZIP introuvable après le build."

# Contrôle de ce qu'on s'apprête à rendre public : un paquet dont les sous-modules seraient vides
# s'installerait quand même, sans aucun plugin. Mieux vaut refuser ici que le découvrir chez un client.
./venv/bin/python - "$ZIP" <<'PY' || die "paquet incomplet — publication annulée."
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); noms = z.namelist()
manque = [f for f in ("main.py", "install/install.py", "install/install_proxmox.py") if f not in noms]
if manque:
    sys.exit("fichiers absents du paquet : " + ", ".join(manque))
plugins = {n.split("/")[1] for n in noms if n.startswith("plugins/") and n.count("/") > 1}
vides = [p for p in plugins if not any(n.startswith(f"plugins/{p}/") and not n.endswith("/") for n in noms)]
if vides:
    sys.exit("sous-modules VIDES dans le paquet : " + ", ".join(sorted(vides)))
print("  -> %d plugin(s) peuplé(s), installeur embarqué" % len(plugins))
PY

# ─── Alerte : modules non publiés dans une release PUBLIQUE ──────────────────
# ⚠ LE PAQUET APLATIT LES SOUS-MODULES. C'est tout son intérêt — une archive de
# code source GitHub livrerait des dossiers plugins/ et services/ vides — mais ça
# veut dire que le zip contient le CODE SOURCE de chaque module embarqué. Sur une
# machine qui possède les modules privés (la nôtre), `--all` en met 33 dedans.
#
# ★ ON ALERTE, ON NE REFUSE PAS, et c'est un choix assumé : livrer à un client un
# paquet contenant des modules pas encore publics est un cas d'usage LÉGITIME.
# Refuser reviendrait à faire de l'outil le juge d'une décision commerciale.
#
# Mais ça ne doit pas pouvoir arriver PAR INADVERTANCE : une release ne se
# dépublie pas — le zip est téléchargé, mis en cache, réhébergé. D'où une
# confirmation explicite, et non un avertissement qu'on lit en diagonale.
# `--avec-modules-prives` la donne d'avance, pour un usage non interactif.
log "contrôle de ce que le paquet embarque…"
CIBLE_JSON="$(gh repo view --json nameWithOwner,visibility 2>/dev/null || echo '{}')"
CIBLE="$(printf '%s' "$CIBLE_JSON" | ./venv/bin/python -c 'import sys,json;print((json.load(sys.stdin) or {}).get("nameWithOwner",""))')"
VISIB="$(printf '%s' "$CIBLE_JSON" | ./venv/bin/python -c 'import sys,json;print((json.load(sys.stdin) or {}).get("visibility",""))')"
[ -n "$CIBLE" ] || die "dépôt cible indéterminable (gh repo view) — publication annulée."
if [ "$VISIB" != "PUBLIC" ]; then
  ok "cible $CIBLE ($VISIB) — le paquet ne devient pas public, rien à signaler"
else
  RAPPORT="$(./venv/bin/python - "$ZIP" <<'PY'
import sys, zipfile
sys.path.insert(0, ".")
z = zipfile.ZipFile(sys.argv[1]); noms = z.namelist()
def modules(prefixe):
    return {n.split("/")[1] for n in noms
            if n.startswith(prefixe + "/") and n.count("/") > 1 and not n.split("/")[1].startswith("_")}
embarques = modules("plugins") | modules("services")

# ★ UN MODULE SANS DÉPÔT PROPRE EST DÉJÀ PUBLIC. Certains plugins et services vivent DANS le
# dépôt principal et ne sont pas des sous-modules — `probe_2110`, `pyramide`, `v210_bridge`,
# `files`. Leur code est donc déjà en clair sur le dépôt public : les embarquer n'expose rien.
# Le contrôle ne les voyait pas ainsi (il ne connaissait que le CATALOGUE, c'est-à-dire les
# dépôts `bobistudio-plugin-*`), les déclarait privés et bloquait la publication. Il a fallu
# passer outre pour livrer du code déjà public — et un garde-fou qui crie au loup finit par se
# faire contourner par réflexe, y compris le jour où il a raison.
dans_le_depot = set()
try:
    with open(".gitmodules", encoding="utf-8") as f:
        sousmodules = {l.split("=", 1)[1].strip() for l in f if l.strip().startswith("path")}
except FileNotFoundError:
    sousmodules = set()
for prefixe in ("plugins", "services"):
    for m in modules(prefixe):
        if "%s/%s" % (prefixe, m) not in sousmodules:
            dans_le_depot.add(m)

try:
    from app import catalogue
    r = catalogue.lister(force=True)
    if r.get("erreur"):
        print("INCONNU|%s" % r["erreur"]); sys.exit(0)
    publies = {e["type"] for e in r["entrees"]} | dans_le_depot
except Exception as e:
    print("INCONNU|catalogue injoignable : %r" % (e,)); sys.exit(0)
prives = sorted(embarques - publies)
print(("PRIVES|%s" % ", ".join(prives)) if prives else "OK|%d" % len(embarques))
PY
)"
  ETAT="${RAPPORT%%|*}"; DETAIL="${RAPPORT#*|}"
  case "$ETAT" in
    OK)
      ok "cible $CIBLE (PUBLIC) — $DETAIL module(s), tous déjà publiés" ;;
    PRIVES|INCONNU)
      echo
      if [ "$ETAT" = "PRIVES" ]; then
        warn "CE PAQUET REND PUBLIC LE CODE SOURCE DE MODULES QUI NE LE SONT PAS :"
        echo "      $DETAIL"
      else
        warn "IMPOSSIBLE DE VÉRIFIER CE QUI EST DÉJÀ PUBLIÉ : $DETAIL"
      fi
      echo "      Les sous-modules sont APLATIS dans le zip : leur code part en clair."
      echo "      La cible est PUBLIQUE ($CIBLE), et une release ne se dépublie pas."
      echo "      Pour un paquet restreint :  python3 tools/build_dist.py --plugins <liste>"
      echo
      if [ "$AVEC_PRIVES" -eq 1 ]; then
        warn "poursuite demandée par --avec-modules-prives"
      elif [ -t 0 ]; then
        printf "  Publier quand même ? (tapez OUI en majuscules) : "
        read -r _rep
        [ "$_rep" = "OUI" ] || die "publication annulée."
      else
        die "sortie non interactive : relancer avec --avec-modules-prives pour publier quand même."
      fi ;;
  esac
fi

log "empreinte SHA-256…"
( cd dist && sha256sum bobistudio.zip > SHA256SUMS )
ok "$(cat dist/SHA256SUMS)"

_args=(release create "$TAG" "$ZIP" dist/SHA256SUMS
       --title "Bobi.Studio $TAG"
       --notes "Installation sur une machine vierge :

    bash <(curl -fsSL https://raw.githubusercontent.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/main/get.sh)

Le paquet ci-joint est l'artefact d'installation complet (code, plugins et services inclus).
L'archive « Source code » générée par GitHub, elle, ne contient PAS le contenu des sous-modules :
elle ne permet pas d'installer.")
if [ "$BROUILLON" = 1 ]; then _args+=(--draft); fi   # (pas de « && » : faux ⇒ rc=1 ⇒ set -e sortirait)

log "publication de la release $TAG…"
gh "${_args[@]}"
echo
ok "Release $TAG publiée."
echo "     Vérifier depuis une machine vierge :"
echo "       bash <(curl -fsSL https://raw.githubusercontent.com/$(gh repo view --json nameWithOwner -q .nameWithOwner)/main/get.sh) --dry-run"
