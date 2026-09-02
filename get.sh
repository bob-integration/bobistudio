#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# get.sh — amorce d'installation depuis GitHub, sur une machine VIERGE.
#
#     bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobistudio/main/get.sh)
#
# Équivalent, sans orchestrateur préexistant, du one-liner que sert une instance déjà installée
# (`bash <(curl -fsSL http://<orchestrateur>:5000/install.sh)`) : il récupère la source et lance le
# même installeur unifié (menu : nœud / orchestrateur / tout-en-un / désinstaller).
#
# ★ CURL SEUL — NI git, NI paquet à construire au préalable.
# GitHub sert une archive par dépôt (`codeload`), donc `curl` + `tar` suffisent : pas de `git` à
# installer sur une machine vierge, pas de release à publier. C'est déjà le mécanisme du catalogue
# de plugins (`app/catalogue.py`), on ne fait que l'appliquer plus tôt.
#
# ★ CE QU'ON RÉCUPÈRE, ET POURQUOI PAS LE RESTE.
# Le dépôt compte 28 sous-modules (plugins et services), et une archive de code source GitHub ne
# contient PAS leur contenu. On ne prend donc QUE le minimum qui conditionne le démarrage :
#   · le dépôt principal — le produit, l'installeur, l'agent-nœud, et les contextes d'images
#     runtime (plugins/_compute_runtime, _media_runtime, _webrtc_runtime, qui n'en sont pas) ;
#   · services/nmos — SEUL service importé au niveau module par main.py. Un dossier vide serait
#     traité par Python comme un « namespace package » : l'import réussirait, le module serait
#     creux, et le démarrage casserait plus loin sur un AttributeError qui ne nomme pas la cause.
# Tout le reste — les autres services, tous les plugins — s'installe APRÈS, depuis la page
# Catalogue de l'interface, qui lit la même organisation GitHub. C'est le chemin prévu pour un
# exploitant : il n'a pas à cloner un dépôt pour ajouter un traitement vidéo.
#
# Options :
#   --ref <branche|tag>  version à installer, sans rien demander (défaut : la choisir au menu)
#   --liste              affiche les versions disponibles et s'arrête
#   --dry-run            récupère et vérifie la source, puis s'arrête — n'installe RIEN
#   --keep               conserve le dossier de travail (débogage)
#
# Variables d'environnement :
#   BOBI_REPO      dépôt principal      (défaut : bob-integration/bobistudio)
#   BOBI_REF       comme --ref
#   GITHUB_TOKEN   jeton — nécessaire tant que les dépôts sont PRIVÉS (sinon GitHub renvoie une
#                  page de connexion, et l'on déballerait du HTML en croyant avoir une archive).
#   BOBI_CODELOAD  base des archives (défaut : https://codeload.github.com). Sert à un miroir
#                  interne, à un GitHub Enterprise, ou à éprouver ce script hors ligne.
#   BOBI_API       base de l'API (défaut : https://api.github.com) — même usage, pour la liste
#                  des versions.
set -euo pipefail

REPO="${BOBI_REPO:-bob-integration/bobistudio}"
CODELOAD="${BOBI_CODELOAD:-https://codeload.github.com}"
API="${BOBI_API:-https://api.github.com}"
REF="${BOBI_REF:-}"          # vide = on demandera, ou « main » hors terminal
LISTER=0
DRY=0
KEEP=0

# Composants qu'on POSE D'EMBLÉE parce qu'ils rendent l'installation immédiatement utile, sans
# aller au catalogue. Format : <chemin dans l'arbre>:<dépôt GitHub>.
#
# ⚠ AUCUN N'EST INDISPENSABLE, et cette liste ne doit plus jamais le devenir. Elle l'a été :
# `services/nmos` y figurait comme obligatoire, l'installation ABANDONNAIT s'il était injoignable,
# et le message affirmait que « l'orchestrateur ne démarrerait pas ». C'était vrai à l'époque —
# `main.py` l'importait sans garde — et ça ne l'est plus : l'import est enveloppé, l'API NMOS
# n'est simplement pas servie, une alerte le dit, et le composant s'installe depuis le Catalogue.
#
# Faire échouer une installation ENTIÈRE pour un composant absent est disproportionné, et le
# remède qu'on suggérait — installer depuis le catalogue — exige que le produit TOURNE. Un cercle
# dont l'utilisateur ne peut pas sortir. Constaté chez un installateur le 2026-09-02.
SOUS_MODULES_UTILES=(
  "services/nmos:bobistudio-service-nmos"
)

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "  ${c_b}·${c_0} $*"; }; ok(){ echo "  ${c_g}✓${c_0} $*"; }
warn(){ echo "  ${c_y}!${c_0} $*"; }; die(){ echo "  ${c_r}✗${c_0} $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2;;
    --liste|--list) LISTER=1; shift;;
    --dry-run) DRY=1; shift;;
    --keep) KEEP=1; shift;;
    -h|--help) sed -n '5,40p' "$0"; exit 0;;
    *) die "option inconnue : $1";;
  esac
done

# Version de CET INSTALLATEUR — pas celle du produit, qui n'est pas encore choisie à ce stade.
#
# ★ À QUOI ELLE SERT : quand une installation échoue, on demande à la personne le numéro affiché.
# Sans lui, impossible de savoir si elle a un `get.sh` d'aujourd'hui ou celui de la semaine
# dernière — le script est servi par le site, donc mis en cache un peu partout, et rien ne dit
# lequel elle exécute. Une date se lit sans table de correspondance : « 2026-08-20 » dit tout de
# suite qu'il est vieux de deux semaines.
#
# ⚠ À BUMPER À CHAQUE MODIFICATION DE CE FICHIER, sinon elle ment — et une version qui ment est
# pire que pas de version, puisqu'on lui fait confiance pour écarter une piste.
INSTALLEUR_VERSION="2026.09.02b"

# Centre un texte dans le cadre. CALCULÉ, pas compté à la main : le sous-titre était décalé de
# deux caractères parce que son remplissage avait été posé à l'œil (corrigé le 2026-09-02), et un
# numéro de version change de longueur à chaque bump.
_cadre_ligne() {   # <texte>
  local t="$1" l=54 g
  g=$(( (l - ${#t}) / 2 ))
  printf "  ║%*s%s%*s║\n" "$g" "" "$t" "$(( l - ${#t} - g ))" ""
}

echo
echo "  ╔══════════════════════════════════════════════════════╗"
_cadre_ligne "B O B I . S T U D I O"
_cadre_ligne "Installation depuis GitHub"
_cadre_ligne "installateur $INSTALLEUR_VERSION"
echo "  ╚══════════════════════════════════════════════════════╝"
echo

[ "$(id -u)" = "0" ] || die "à lancer en root (l'installeur pose des services systemd)."
command -v curl    >/dev/null 2>&1 || die "curl est requis (apt-get install -y curl)."
command -v tar     >/dev/null 2>&1 || die "tar est requis (apt-get install -y tar)."
# python3 sert à déballer les métadonnées et à exécuter l'installeur. Sur une Debian minimale il
# peut manquer : on le pose plutôt que de renvoyer l'exploitant à une commande qu'on sait taper.
if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 est absent, et l'installeur en a besoin."
  _rep="o"
  if [ -t 0 ]; then
    printf "%b" "  ${c_y}?${c_0} L'installer maintenant (apt-get install python3) ? [O/n] "
    read -r _rep || _rep="o"
  else
    log "non interactif — installation de python3 sans demander"
  fi
  case "${_rep:-o}" in
    [nN]*) die "python3 requis : « apt-get install -y python3 », puis relancer.";;
  esac
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update a échoué — dépôts injoignables ?"
  apt-get install -y -qq python3 || die "installation de python3 échouée."
  # apt peut rendre 0 sans avoir posé le binaire (miroir partiel, paquet retenu). On CONSTATE.
  command -v python3 >/dev/null 2>&1 \
    || die "apt s'est terminé sans erreur mais python3 reste introuvable — dépôts incomplets ?"
  ok "python3 installé ($(python3 --version 2>&1))"
fi

_curl=(curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15)
if [ -n "${GITHUB_TOKEN:-}" ]; then
  _curl+=(-H "Authorization: Bearer $GITHUB_TOKEN")
  log "jeton GitHub fourni (dépôts privés)"
fi

# ─── Quelles versions existent ────────────────────────────────────────────────
# Les étiquettes du dépôt principal font foi. UNE requête à l'API GitHub (quota anonyme : 60/h,
# largement suffisant), et un `python3` déjà exigé par ailleurs — donc pas de `jq` à installer.
_etiquettes() {
  "${_curl[@]}" "$API/repos/$REPO/tags?per_page=20" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    for t in json.load(sys.stdin):
        n = t.get("name") or ""
        if n: print(n)
except Exception:
    pass' 2>/dev/null || true
}

_choisir_version() {
  local -a tags=()
  log "lecture des versions disponibles…"
  mapfile -t tags < <(_etiquettes)
  if [ "${#tags[@]}" -eq 0 ]; then
    # Aucune étiquette (ou API injoignable) : la branche principale reste installable. On le DIT,
    # plutôt que d'installer « main » en laissant croire qu'une version a été choisie.
    warn "aucune version étiquetée trouvée — installation de la branche « main » (développement)."
    REF="main"; return 0
  fi
  echo
  echo "  Versions disponibles :"
  local i=1
  for t in "${tags[@]:0:10}"; do
    if [ "$i" = 1 ]; then echo "    ${c_g}$i${c_0}) $t   ${c_g}(la plus récente)${c_0}"
    else echo "    ${c_g}$i${c_0}) $t"; fi
    i=$((i + 1))
  done
  echo "    ${c_g}d${c_0}) main — branche de développement, non figée"
  echo
  local rep_
  printf "%b" "  Version à installer [1] : "
  read -r rep_ || rep_=""
  case "${rep_:-1}" in
    d|D|main) REF="main";;
    *[!0-9]*|"") REF="${tags[0]}";;
    *) if [ "$rep_" -ge 1 ] && [ "$rep_" -le "${#tags[@]}" ]; then REF="${tags[$((rep_ - 1))]}"
       else REF="${tags[0]}"; fi;;
  esac
  ok "version retenue : $REF"
}

if [ "$LISTER" = 1 ]; then
  echo "  Versions publiées de $REPO :"
  _etiquettes | sed 's/^/    · /' || true
  echo "    · main (branche de développement)"
  exit 0
fi

if [ -z "$REF" ]; then
  # Hors terminal (script, pipe), on ne peut pas demander : « main » et on le dit.
  if [ -t 0 ]; then _choisir_version; else REF="main"; log "non interactif — branche « main »"; fi
fi

TMP="$(mktemp -d)"
_menage() { if [ "$KEEP" != 1 ]; then rm -rf "$TMP"; fi; }
trap _menage EXIT
if [ "$KEEP" = 1 ]; then log "dossier de travail conservé : $TMP"; fi
SRC="$TMP/src"; mkdir -p "$SRC"

# Récupère l'archive d'un dépôt et la déplie DANS $2. `--strip-components=1` retire le dossier
# racine que GitHub ajoute (« <dépôt>-<ref>/ »), qu'on ne veut pas voir apparaître dans l'arbre.
# SHA du sous-module `$1` tel qu'il est ÉPINGLÉ dans le dépôt principal à la ref installée.
#
# ★ POURQUOI CE DÉTOUR. `_recuperer` employait la MÊME ref pour le produit et pour ses
# sous-modules. Tant qu'on installait « main », présente partout, ça passait. Depuis qu'on
# installe par ÉTIQUETTE, ça ne peut plus : il n'existe aucun « v0.9.3 » dans
# `bobistudio-service-nmos`, qui vit sur ses propres numéros. L'installation échouait donc au
# premier sous-module — constaté chez un installateur le 2026-09-02.
#
# L'API rend le SHA exact que le dépôt principal épingle à cette étiquette : c'est LE commit
# contre lequel la release a été construite, donc la seule réponse reproductible. Une étiquette
# du composant serait un à-peu-près ; sa branche, pas une version du tout.
_sha_sous_module() {   # <chemin>
  "${_curl[@]}" "$API/repos/$REPO/contents/$1?ref=$REF" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    if d.get("type") == "submodule" and d.get("sha"): print(d["sha"])
except Exception:
    pass' 2>/dev/null
}

_recuperer() {   # <dépôt> <destination> <étiquette> [ref]
  local depot="$1" dest="$2" quoi="$3" ref="${4:-$REF}"
  local url="$CODELOAD/$depot/tar.gz/refs/heads/$ref"
  mkdir -p "$dest"
  # Les deux tentatives sont MUETTES : une ref peut être une branche ou une étiquette, GitHub les
  # sert sur des chemins différents, et l'échec de la première est donc NORMAL une fois sur deux.
  # Laisser curl le crier ferait passer une installation saine pour une panne. Si les deux
  # échouent, l'appelant produit un message qui, lui, nomme les causes possibles.
  if ! "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null; then
    url="$CODELOAD/$depot/tar.gz/refs/tags/$ref"
    if ! "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null; then
      # Ni branche ni étiquette : un SHA brut, que codeload sert aussi. C'est le cas d'un
      # sous-module épinglé, dont le commit ne porte ni l'une ni l'autre.
      url="$CODELOAD/$depot/tar.gz/$ref"
      "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null || return 1
    fi
  fi
  # Un HTML de page de connexion se déballe mal : on le dit ici plutôt que de laisser un arbre
  # à moitié rempli passer pour une source valide.
  tar -xzf "$TMP/a.tar.gz" -C "$dest" --strip-components=1 2>/dev/null || return 2
  rm -f "$TMP/a.tar.gz"
  log "  $quoi ✓"
}

log "récupération de la source ($REPO@$REF)…"
if ! _recuperer "$REPO" "$SRC" "dépôt principal"; then
  echo
  die "source introuvable : $REPO@$REF
     Trois causes possibles :
       · le dépôt est encore PRIVÉ — exporter GITHUB_TOKEN=<jeton> avant de relancer ;
       · la branche ou l'étiquette « $REF » n'existe pas ;
       · pas d'accès réseau à codeload.github.com."
fi

manques=()
for entree in "${SOUS_MODULES_UTILES[@]}"; do
  chemin="${entree%%:*}"; depot="${entree#*:}"
  # Le SHA épinglé d'abord ; à défaut (API injoignable, quota épuisé, ref = branche) on retombe
  # sur « main » du composant, qui reste installable — mieux qu'un échec sec.
  ref_sm="$(_sha_sous_module "$chemin")"
  [ -n "$ref_sm" ] || ref_sm="main"
  if ! _recuperer "${REPO%/*}/$depot" "$SRC/$chemin" "$chemin" "$ref_sm"; then
    # ON CONTINUE. Le produit s'installe et démarre sans lui ; on le DIT ici, une fois, plutôt
    # que de laisser la personne le découvrir à l'usage.
    rmdir "$SRC/$chemin" 2>/dev/null || true
    manques+=("$chemin")
    warn "« $chemin » n'a pas pu être récupéré — l'installation continue sans lui."
  fi
done
if [ ${#manques[@]} -gt 0 ]; then
  echo
  warn "${#manques[@]} composant(s) non récupéré(s) : ${manques[*]}
     Ce n'est pas bloquant : Bobi.Studio démarre sans eux, et les signale dans ses alertes.
     Installez-les ensuite depuis Réglages → Catalogue."
fi

# Contrôle de ce qu'on a VRAIMENT obtenu, plutôt que de faire confiance à des codes retour :
# l'installeur applique le même critère (install.py:_find_source), on échoue donc ici, où le
# message peut encore être utile.
python3 - "$SRC" <<'PY' || die "source incomplète — installation annulée."
import os, sys
src = sys.argv[1]
# ⚠ SEULEMENT LE PRODUIT. `services/nmos/__init__.py` figurait ici : le contrôle annulait donc
# l'installation quelques lignes après qu'on ait décidé de continuer sans lui. Un composant
# facultatif n'a rien à faire dans un contrôle d'intégrité — il s'installe depuis le Catalogue.
manque = [c for c in ("main.py", "app", "install/install.py", "node_agent/install-node.sh",
                      "plugins/_compute_runtime/meta.json")
          if not os.path.exists(os.path.join(src, c))]
if manque:
    sys.exit("absents de la source : " + ", ".join(manque))
PY
ok "source complète (les composants s'installent depuis la page Catalogue)"

if [ "$DRY" = 1 ]; then
  echo
  ok "Simulation (--dry-run) : source récupérée et vérifiée, rien n'a été installé."
  if [ "$KEEP" = 1 ]; then log "contenu dans $SRC"; fi
  exit 0
fi

echo
log "lancement de l'installeur…"
echo
cd "$SRC"
exec python3 install/install.py
