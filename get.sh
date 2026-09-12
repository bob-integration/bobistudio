***REMOVED***!/usr/bin/env bash
***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED***
***REMOVED*** get.sh — amorce d'installation depuis GitHub, sur une machine VIERGE.
***REMOVED***
***REMOVED***     bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobistudio/main/get.sh)
***REMOVED***
***REMOVED*** Équivalent, sans orchestrateur préexistant, du one-liner que sert une instance déjà installée
***REMOVED*** (`bash <(curl -fsSL http://<orchestrateur>:5000/install.sh)`) : il récupère la source et lance le
***REMOVED*** même installeur unifié (menu : nœud / orchestrateur / tout-en-un / désinstaller).
***REMOVED***
***REMOVED*** ★ CURL SEUL — NI git, NI paquet à construire au préalable.
***REMOVED*** GitHub sert une archive par dépôt (`codeload`), donc `curl` + `tar` suffisent : pas de `git` à
***REMOVED*** installer sur une machine vierge, pas de release à publier. C'est déjà le mécanisme du catalogue
***REMOVED*** de plugins (`app/catalogue.py`), on ne fait que l'appliquer plus tôt.
***REMOVED***
***REMOVED*** ★ CE QU'ON RÉCUPÈRE, ET POURQUOI PAS LE RESTE.
***REMOVED*** Le dépôt compte 28 sous-modules (plugins et services), et une archive de code source GitHub ne
***REMOVED*** contient PAS leur contenu. On ne prend donc QUE le minimum qui conditionne le démarrage :
***REMOVED***   · le dépôt principal — le produit, l'installeur, l'agent-nœud, et les contextes d'images
***REMOVED***     runtime (plugins/_compute_runtime, _media_runtime, _webrtc_runtime, qui n'en sont pas) ;
***REMOVED***   · services/nmos — SEUL service importé au niveau module par main.py. Un dossier vide serait
***REMOVED***     traité par Python comme un « namespace package » : l'import réussirait, le module serait
***REMOVED***     creux, et le démarrage casserait plus loin sur un AttributeError qui ne nomme pas la cause.
***REMOVED*** Tout le reste — les autres services, tous les plugins — s'installe APRÈS, depuis la page
***REMOVED*** Catalogue de l'interface, qui lit la même organisation GitHub. C'est le chemin prévu pour un
***REMOVED*** exploitant : il n'a pas à cloner un dépôt pour ajouter un traitement vidéo.
***REMOVED***
***REMOVED*** Options :
***REMOVED***   --ref <branche|tag>  version à installer, sans rien demander (défaut : la choisir au menu)
***REMOVED***   --liste              affiche les versions disponibles et s'arrête
***REMOVED***   --dry-run            récupère et vérifie la source, puis s'arrête — n'installe RIEN
***REMOVED***   --keep               conserve le dossier de travail (débogage)
***REMOVED***
***REMOVED*** Variables d'environnement :
***REMOVED***   BOBI_REPO      dépôt principal      (défaut : bob-integration/bobistudio)
***REMOVED***   BOBI_REF       comme --ref
***REMOVED***   GITHUB_TOKEN   jeton — nécessaire tant que les dépôts sont PRIVÉS (sinon GitHub renvoie une
***REMOVED***                  page de connexion, et l'on déballerait du HTML en croyant avoir une archive).
***REMOVED***   BOBI_CODELOAD  base des archives (défaut : https://codeload.github.com). Sert à un miroir
***REMOVED***                  interne, à un GitHub Enterprise, ou à éprouver ce script hors ligne.
***REMOVED***   BOBI_API       base de l'API (défaut : https://api.github.com) — même usage, pour la liste
***REMOVED***                  des versions.
set -euo pipefail

REPO="${BOBI_REPO:-bob-integration/bobistudio}"
CODELOAD="${BOBI_CODELOAD:-https://codeload.github.com}"
API="${BOBI_API:-https://api.github.com}"
REF="${BOBI_REF:-}"          ***REMOVED*** vide = on demandera, ou « main » hors terminal
LISTER=0
DRY=0
KEEP=0

***REMOVED*** Composants qu'on POSE D'EMBLÉE parce qu'ils rendent l'installation immédiatement utile, sans
***REMOVED*** aller au catalogue. Format : <chemin dans l'arbre>:<dépôt GitHub>.
***REMOVED***
***REMOVED*** ⚠ AUCUN N'EST INDISPENSABLE, et cette liste ne doit plus jamais le devenir. Elle l'a été :
***REMOVED*** `services/nmos` y figurait comme obligatoire, l'installation ABANDONNAIT s'il était injoignable,
***REMOVED*** et le message affirmait que « l'orchestrateur ne démarrerait pas ». C'était vrai à l'époque —
***REMOVED*** `main.py` l'importait sans garde — et ça ne l'est plus : l'import est enveloppé, l'API NMOS
***REMOVED*** n'est simplement pas servie, une alerte le dit, et le composant s'installe depuis le Catalogue.
***REMOVED***
***REMOVED*** Faire échouer une installation ENTIÈRE pour un composant absent est disproportionné, et le
***REMOVED*** remède qu'on suggérait — installer depuis le catalogue — exige que le produit TOURNE. Un cercle
***REMOVED*** dont l'utilisateur ne peut pas sortir. Constaté chez un installateur le 2026-09-02.
SOUS_MODULES_UTILES=(
  "services/nmos:bobistudio-service-nmos"
)

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "  ${c_b}·${c_0} $*"; }; ok(){ echo "  ${c_g}✓${c_0} $*"; }
warn(){ echo "  ${c_y}!${c_0} $*"; }; die(){ echo "  ${c_r}✗${c_0} $*" >&2; exit 1; }

while [ $***REMOVED*** -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2;;
    --liste|--list) LISTER=1; shift;;
    --dry-run) DRY=1; shift;;
    --keep) KEEP=1; shift;;
    -h|--help) sed -n '5,40p' "$0"; exit 0;;
    *) die "$(_t option_inconnue "$1")";;
  esac
done

***REMOVED*** Version de CET INSTALLATEUR — pas celle du produit, qui n'est pas encore choisie à ce stade.
***REMOVED***
***REMOVED*** ★ À QUOI ELLE SERT : quand une installation échoue, on demande à la personne le numéro affiché.
***REMOVED*** Sans lui, impossible de savoir si elle a un `get.sh` d'aujourd'hui ou celui de la semaine
***REMOVED*** dernière — le script est servi par le site, donc mis en cache un peu partout, et rien ne dit
***REMOVED*** lequel elle exécute. Une date se lit sans table de correspondance : « 2026-08-20 » dit tout de
***REMOVED*** suite qu'il est vieux de deux semaines.
***REMOVED***
***REMOVED*** ⚠ À BUMPER À CHAQUE MODIFICATION DE CE FICHIER, sinon elle ment — et une version qui ment est
***REMOVED*** pire que pas de version, puisqu'on lui fait confiance pour écarter une piste.
INSTALLEUR_VERSION="2026.09.02c"

***REMOVED*** ─── Langue de l'installateur ────────────────────────────────
***REMOVED***
***REMOVED*** ★ LE DÉFAUT VIENT DE L'ENVIRONNEMENT. Une machine configurée en anglais parle anglais sans
***REMOVED*** qu'on ait à le demander ; la question ne sert qu'à contredire ce défaut. Demander sans proposer
***REMOVED*** de défaut sensé ajoute une étape à qui n'en avait pas besoin.
***REMOVED***
***REMOVED*** `BOBI_LANG=fr|en` court-circuite tout — indispensable en non-interactif, où aucune question ne
***REMOVED*** peut être posée et où les journaux doivent pourtant être lisibles par leur destinataire.
UI="${BOBI_LANG:-}"
if [ -z "$UI" ]; then
  case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
    fr*|FR*) UI="fr" ;;
    "")      UI="fr" ;;      ***REMOVED*** environnement muet : on reste sur la langue du produit
    *)       UI="en" ;;
  esac
fi

***REMOVED*** Table des messages. Une clé, deux langues, et `printf` pour les valeurs interpolées : PAS de
***REMOVED*** concaténation, qui rendrait certaines phrases intraduisibles — l'ordre des mots change d'une
***REMOVED*** langue à l'autre, et un morceau de phrase ne se traduit pas isolément.
***REMOVED***
***REMOVED*** Une clé sans traduction anglaise retombe sur le français plutôt que d'afficher la clé brute :
***REMOVED*** un texte dans la mauvaise langue reste lisible, une clé ne l'est pas.
_t() {
  local k="$1"; shift
  local fr="" en=""
  case "$k" in
    sous_titre)   fr="Installation depuis GitHub";                      en="Install from GitHub" ;;
    installateur) fr="installateur %s";                                 en="installer %s" ;;
    langue)       fr="Langue / Language  [1] Français  [2] English : "; en="Langue / Language  [1] Français  [2] English : " ;;
    root)         fr="à lancer en root (l'installeur pose des services systemd)."
                  en="must be run as root (the installer sets up systemd services)." ;;
    requis)       fr="%s est requis (apt-get install -y %s)."
                  en="%s is required (apt-get install -y %s)." ;;
    py_absent)    fr="python3 est absent, et l'installeur en a besoin."
                  en="python3 is missing, and the installer needs it." ;;
    py_demande)   fr="  ${c_y}?${c_0} L'installer maintenant (apt-get install python3) ? [O/n] "
                  en="  ${c_y}?${c_0} Install it now (apt-get install python3)? [Y/n] " ;;
    py_auto)      fr="non interactif — installation de python3 sans demander"
                  en="non-interactive — installing python3 without asking" ;;
    py_refus)     fr="python3 requis : « apt-get install -y python3 », puis relancer."
                  en="python3 required: run « apt-get install -y python3 », then start again." ;;
    apt_update)   fr="apt-get update a échoué — dépôts injoignables ?"
                  en="apt-get update failed — repositories unreachable?" ;;
    apt_install)  fr="installation de python3 échouée."
                  en="python3 installation failed." ;;
    apt_muet)     fr="apt s'est terminé sans erreur mais python3 reste introuvable — dépôts incomplets ?"
                  en="apt exited without error yet python3 is still missing — incomplete repositories?" ;;
    py_ok)        fr="python3 installé (%s)";                           en="python3 installed (%s)" ;;
    jeton)        fr="jeton GitHub fourni (dépôts privés)"
                  en="GitHub token supplied (private repositories)" ;;
    lecture_vers) fr="lecture des versions disponibles…";               en="reading available versions…" ;;
    aucune_vers)  fr="aucune version étiquetée trouvée — installation de la branche « main » (développement)."
                  en="no tagged version found — installing the « main » branch (development)." ;;
    vers_dispo)   fr="  Versions disponibles :";                        en="  Available versions:" ;;
    plus_recente) fr="(la plus récente)";                               en="(most recent)" ;;
    branche_dev)  fr="main — branche de développement, non figée"
                  en="main — development branch, not frozen" ;;
    quelle_vers)  fr="  Version à installer [1] : ";                    en="  Version to install [1]: " ;;
    vers_retenue) fr="version retenue : %s";                            en="selected version: %s" ;;
    vers_publiees) fr="  Versions publiées de %s :";                    en="  Published versions of %s:" ;;
    branche_dev2) fr="    · main (branche de développement)";           en="    · main (development branch)" ;;
    non_interactif) fr="non interactif — branche « main »";             en="non-interactive — « main » branch" ;;
    dossier_garde) fr="dossier de travail conservé : %s";               en="work directory kept: %s" ;;
    recuperation) fr="récupération de la source (%s@%s)…";              en="fetching the source (%s@%s)…" ;;
    src_introuvable) fr="source introuvable : %s@%s\n       Trois causes possibles :\n         · le dépôt est encore PRIVÉ — exporter GITHUB_TOKEN=<jeton> avant de relancer ;\n         · la branche ou l'étiquette « %s » n'existe pas ;\n         · pas d'accès réseau à codeload.github.com."
                  en="source not found: %s@%s\n       Three possible causes:\n         · the repository is still PRIVATE — export GITHUB_TOKEN=<token> and start again;\n         · the branch or tag « %s » does not exist;\n         · no network access to codeload.github.com." ;;
    comp_absent)  fr="« %s » n'a pas pu être récupéré — l'installation continue sans lui."
                  en="« %s » could not be fetched — the installation continues without it." ;;
    comp_recap)   fr="%s composant(s) non récupéré(s) : %s\n     Ce n'est pas bloquant : Bobi.Studio démarre sans eux, et les signale dans ses alertes.\n     Installez-les ensuite depuis Réglages → Catalogue."
                  en="%s component(s) not fetched: %s\n     This is not blocking: Bobi.Studio starts without them, and reports them in its alerts.\n     Install them afterwards from Settings → Catalogue." ;;
    src_incomplete) fr="source incomplète — installation annulée."
                  en="incomplete source — installation cancelled." ;;
    src_complete) fr="source complète (les composants s'installent depuis la page Catalogue)"
                  en="source complete (components install from the Catalogue page)" ;;
    simulation)   fr="Simulation (--dry-run) : source récupérée et vérifiée, rien n'a été installé."
                  en="Dry run (--dry-run): source fetched and verified, nothing was installed." ;;
    contenu_dans) fr="contenu dans %s";                                 en="contents in %s" ;;
    lancement)    fr="lancement de l'installeur…";                      en="starting the installer…" ;;
    option_inconnue) fr="option inconnue : %s";                         en="unknown option: %s" ;;
    pause)        fr="tout est en place — l'installeur démarre dans %s s…"
                  en="everything is in place — the installer starts in %s s…" ;;
  esac
  if [ "$UI" = "en" ] && [ -n "$en" ]; then printf "%b" "$(printf "$en" "$@")"
  else printf "%b" "$(printf "$fr" "$@")"; fi
}

***REMOVED*** Centre un texte dans le cadre. CALCULÉ, pas compté à la main : le sous-titre était décalé de
***REMOVED*** deux caractères parce que son remplissage avait été posé à l'œil (corrigé le 2026-09-02), et un
***REMOVED*** numéro de version change de longueur à chaque bump.
_cadre_ligne() {   ***REMOVED*** <texte>
  local t="$1" l=54 g
  g=$(( (l - ${***REMOVED***t}) / 2 ))
  printf "  ║%*s%s%*s║\n" "$g" "" "$t" "$(( l - ${***REMOVED***t} - g ))" ""
}

***REMOVED*** La question n'est posée QU'EN INTERACTIF, et seulement si l'exploitant n'a pas déjà tranché
***REMOVED*** par BOBI_LANG. Elle est bilingue par nécessité : on ne sait pas encore quelle langue il lit.
if [ -z "${BOBI_LANG:-}" ] && [ -t 0 ]; then
  echo
  printf "%b" "$(_t langue)"
  read -r _lg || _lg=""
  case "$_lg" in 2|en|EN|e|E) UI="en" ;; 1|fr|FR|f|F) UI="fr" ;; esac
fi

echo
echo "  ╔══════════════════════════════════════════════════════╗"
_cadre_ligne "B O B I . S T U D I O"
_cadre_ligne "$(_t sous_titre)"
_cadre_ligne "$(_t installateur "$INSTALLEUR_VERSION")"
echo "  ╚══════════════════════════════════════════════════════╝"
echo

[ "$(id -u)" = "0" ] || die "$(_t root)"
command -v curl    >/dev/null 2>&1 || die "$(_t requis curl curl)"
command -v tar     >/dev/null 2>&1 || die "$(_t requis tar tar)"
***REMOVED*** python3 sert à déballer les métadonnées et à exécuter l'installeur. Sur une Debian minimale il
***REMOVED*** peut manquer : on le pose plutôt que de renvoyer l'exploitant à une commande qu'on sait taper.
if ! command -v python3 >/dev/null 2>&1; then
  warn "$(_t py_absent)"
  _rep="o"
  if [ -t 0 ]; then
    printf "%b" "$(_t py_demande)"
    read -r _rep || _rep="o"
  else
    log "$(_t py_auto)"
  fi
  case "${_rep:-o}" in
    [nN]*) die "$(_t py_refus)";;
  esac
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "$(_t apt_update)"
  apt-get install -y -qq python3 || die "$(_t apt_install)"
  ***REMOVED*** apt peut rendre 0 sans avoir posé le binaire (miroir partiel, paquet retenu). On CONSTATE.
  command -v python3 >/dev/null 2>&1 \
    || die "$(_t apt_muet)"
  ok "$(_t py_ok "$(python3 --version 2>&1)")"
fi

_curl=(curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15)
if [ -n "${GITHUB_TOKEN:-}" ]; then
  _curl+=(-H "Authorization: Bearer $GITHUB_TOKEN")
  log "$(_t jeton)"
fi

***REMOVED*** ─── Quelles versions existent ────────────────────────────────────────────────
***REMOVED*** Les étiquettes du dépôt principal font foi. UNE requête à l'API GitHub (quota anonyme : 60/h,
***REMOVED*** largement suffisant), et un `python3` déjà exigé par ailleurs — donc pas de `jq` à installer.
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
  log "$(_t lecture_vers)"
  mapfile -t tags < <(_etiquettes)
  if [ "${***REMOVED***tags[@]}" -eq 0 ]; then
    ***REMOVED*** Aucune étiquette (ou API injoignable) : la branche principale reste installable. On le DIT,
    ***REMOVED*** plutôt que d'installer « main » en laissant croire qu'une version a été choisie.
    warn "$(_t aucune_vers)"
    REF="main"; return 0
  fi
  echo
  echo "$(_t vers_dispo)"
  local i=1
  for t in "${tags[@]:0:10}"; do
    if [ "$i" = 1 ]; then echo "    ${c_g}$i${c_0}) $t   ${c_g}$(_t plus_recente)${c_0}"
    else echo "    ${c_g}$i${c_0}) $t"; fi
    i=$((i + 1))
  done
  echo "    ${c_g}d${c_0}) $(_t branche_dev)"
  echo
  local rep_
  printf "%b" "$(_t quelle_vers)"
  read -r rep_ || rep_=""
  case "${rep_:-1}" in
    d|D|main) REF="main";;
    *[!0-9]*|"") REF="${tags[0]}";;
    *) if [ "$rep_" -ge 1 ] && [ "$rep_" -le "${***REMOVED***tags[@]}" ]; then REF="${tags[$((rep_ - 1))]}"
       else REF="${tags[0]}"; fi;;
  esac
  ok "$(_t vers_retenue "$REF")"
}

if [ "$LISTER" = 1 ]; then
  echo "$(_t vers_publiees "$REPO")"
  _etiquettes | sed 's/^/    · /' || true
  echo "$(_t branche_dev2)"
  exit 0
fi

if [ -z "$REF" ]; then
  ***REMOVED*** Hors terminal (script, pipe), on ne peut pas demander : « main » et on le dit.
  if [ -t 0 ]; then _choisir_version; else REF="main"; log "$(_t non_interactif)"; fi
fi

TMP="$(mktemp -d)"
_menage() { if [ "$KEEP" != 1 ]; then rm -rf "$TMP"; fi; }
trap _menage EXIT
if [ "$KEEP" = 1 ]; then log "$(_t dossier_garde "$TMP")"; fi
SRC="$TMP/src"; mkdir -p "$SRC"

***REMOVED*** Récupère l'archive d'un dépôt et la déplie DANS $2. `--strip-components=1` retire le dossier
***REMOVED*** racine que GitHub ajoute (« <dépôt>-<ref>/ »), qu'on ne veut pas voir apparaître dans l'arbre.
***REMOVED*** SHA du sous-module `$1` tel qu'il est ÉPINGLÉ dans le dépôt principal à la ref installée.
***REMOVED***
***REMOVED*** ★ POURQUOI CE DÉTOUR. `_recuperer` employait la MÊME ref pour le produit et pour ses
***REMOVED*** sous-modules. Tant qu'on installait « main », présente partout, ça passait. Depuis qu'on
***REMOVED*** installe par ÉTIQUETTE, ça ne peut plus : il n'existe aucun « v0.9.3 » dans
***REMOVED*** `bobistudio-service-nmos`, qui vit sur ses propres numéros. L'installation échouait donc au
***REMOVED*** premier sous-module — constaté chez un installateur le 2026-09-02.
***REMOVED***
***REMOVED*** L'API rend le SHA exact que le dépôt principal épingle à cette étiquette : c'est LE commit
***REMOVED*** contre lequel la release a été construite, donc la seule réponse reproductible. Une étiquette
***REMOVED*** du composant serait un à-peu-près ; sa branche, pas une version du tout.
_sha_sous_module() {   ***REMOVED*** <chemin>
  "${_curl[@]}" "$API/repos/$REPO/contents/$1?ref=$REF" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    if d.get("type") == "submodule" and d.get("sha"): print(d["sha"])
except Exception:
    pass' 2>/dev/null
}

_recuperer() {   ***REMOVED*** <dépôt> <destination> <étiquette> [ref]
  local depot="$1" dest="$2" quoi="$3" ref="${4:-$REF}"
  local url="$CODELOAD/$depot/tar.gz/refs/heads/$ref"
  mkdir -p "$dest"
  ***REMOVED*** Les deux tentatives sont MUETTES : une ref peut être une branche ou une étiquette, GitHub les
  ***REMOVED*** sert sur des chemins différents, et l'échec de la première est donc NORMAL une fois sur deux.
  ***REMOVED*** Laisser curl le crier ferait passer une installation saine pour une panne. Si les deux
  ***REMOVED*** échouent, l'appelant produit un message qui, lui, nomme les causes possibles.
  if ! "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null; then
    url="$CODELOAD/$depot/tar.gz/refs/tags/$ref"
    if ! "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null; then
      ***REMOVED*** Ni branche ni étiquette : un SHA brut, que codeload sert aussi. C'est le cas d'un
      ***REMOVED*** sous-module épinglé, dont le commit ne porte ni l'une ni l'autre.
      url="$CODELOAD/$depot/tar.gz/$ref"
      "${_curl[@]}" -o "$TMP/a.tar.gz" "$url" 2>/dev/null || return 1
    fi
  fi
  ***REMOVED*** Un HTML de page de connexion se déballe mal : on le dit ici plutôt que de laisser un arbre
  ***REMOVED*** à moitié rempli passer pour une source valide.
  tar -xzf "$TMP/a.tar.gz" -C "$dest" --strip-components=1 2>/dev/null || return 2
  rm -f "$TMP/a.tar.gz"
  log "  $quoi ✓"
}

log "$(_t recuperation "$REPO" "$REF")"
if ! _recuperer "$REPO" "$SRC" "dépôt principal"; then
  echo
  die "$(_t src_introuvable "$REPO" "$REF" "$REF")"
fi

manques=()
for entree in "${SOUS_MODULES_UTILES[@]}"; do
  chemin="${entree%%:*}"; depot="${entree***REMOVED****:}"
  ***REMOVED*** Le SHA épinglé d'abord ; à défaut (API injoignable, quota épuisé, ref = branche) on retombe
  ***REMOVED*** sur « main » du composant, qui reste installable — mieux qu'un échec sec.
  ref_sm="$(_sha_sous_module "$chemin")"
  [ -n "$ref_sm" ] || ref_sm="main"
  if ! _recuperer "${REPO%/*}/$depot" "$SRC/$chemin" "$chemin" "$ref_sm"; then
    ***REMOVED*** ON CONTINUE. Le produit s'installe et démarre sans lui ; on le DIT ici, une fois, plutôt
    ***REMOVED*** que de laisser la personne le découvrir à l'usage.
    rmdir "$SRC/$chemin" 2>/dev/null || true
    manques+=("$chemin")
    warn "$(_t comp_absent "$chemin")"
  fi
done
if [ ${***REMOVED***manques[@]} -gt 0 ]; then
  echo
  warn "$(_t comp_recap "${***REMOVED***manques[@]}" "${manques[*]}")"
fi

***REMOVED*** Contrôle de ce qu'on a VRAIMENT obtenu, plutôt que de faire confiance à des codes retour :
***REMOVED*** l'installeur applique le même critère (install.py:_find_source), on échoue donc ici, où le
***REMOVED*** message peut encore être utile.
python3 - "$SRC" <<'PY' || die "$(_t src_incomplete)"
import os, sys
src = sys.argv[1]
***REMOVED*** ⚠ SEULEMENT LE PRODUIT. `services/nmos/__init__.py` figurait ici : le contrôle annulait donc
***REMOVED*** l'installation quelques lignes après qu'on ait décidé de continuer sans lui. Un composant
***REMOVED*** facultatif n'a rien à faire dans un contrôle d'intégrité — il s'installe depuis le Catalogue.
manque = [c for c in ("main.py", "app", "install/install.py", "node_agent/install-node.sh",
                      "plugins/_compute_runtime/meta.json")
          if not os.path.exists(os.path.join(src, c))]
if manque:
    sys.exit("absents de la source : " + ", ".join(manque))
PY
ok "$(_t src_complete)"

if [ "$DRY" = 1 ]; then
  echo
  ok "$(_t simulation)"
  if [ "$KEEP" = 1 ]; then log "$(_t contenu_dans "$SRC")"; fi
  exit 0
fi

echo
log "$(_t lancement)"

***REMOVED*** ★ UNE PAUSE COURTE, ET SEULEMENT EN INTERACTIF. L'installeur efface l'écran en démarrant : sans
***REMOVED*** ce temps d'arrêt, tout ce que get.sh vient d'afficher — versions, composants récupérés,
***REMOVED*** avertissements sur ce qui manque — disparaît avant d'avoir été lu. Or c'est précisément là que
***REMOVED*** se lisent les anomalies non bloquantes.
***REMOVED***
***REMOVED*** En non-interactif (CI, script), attendre ne montre rien à personne : on saute.
if [ -t 0 ]; then
  for _s in 3 2 1; do
    printf "\r  ${c_b}·${c_0} %s   " "$(_t pause "$_s")"
    sleep 1
  done
  printf "\r%*s\r" 70 ""
fi
echo
cd "$SRC"
***REMOVED*** La langue choisie plus haut suit : l'installeur ne repose pas la question.
export BOBI_LANG="$UI"
exec python3 install/install.py
