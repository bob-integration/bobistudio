#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# submodules-doctor.sh — audit de cohérence des sous-modules git de bobistudio.
#
# Pour CHAQUE sous-module déclaré dans .gitmodules, vérifie :
#   (a) initialisé / checkout (présent sur disque, enregistré)
#   (b) sur la branche attendue (main par défaut, ou `branch =` pinné dans .gitmodules)
#   (c) working tree propre (aucune modif non commit)
#   (d) SHA checkout == gitlink enregistré dans l'index du parent
#   (+) alerte si le nom du dépôt (basename de l'URL) != nom du chemin (path)
#
# Rapport lisible OK / DÉSYNC par sous-module. Exit != 0 si au moins un problème.
#
# Usage : bash tools/submodules-doctor.sh
set -u

# Racine du dépôt parent (le script peut être lancé de n'importe où).
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "${ROOT}" ]; then
    echo "ERREUR : pas dans un dépôt git." >&2
    exit 2
fi
cd "${ROOT}" || exit 2

GITMODULES="${ROOT}/.gitmodules"
if [ ! -f "${GITMODULES}" ]; then
    echo "ERREUR : ${GITMODULES} introuvable." >&2
    exit 2
fi

# Couleurs (désactivées si pas un TTY, ex. en CI).
if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_OK=""; C_BAD=""; C_WARN=""; C_DIM=""; C_RST=""
fi

problems=0
n_ok=0
n_bad=0

# Liste des noms de sous-modules déclarés.
names="$(git config -f "${GITMODULES}" --get-regexp '^submodule\..*\.path$' \
         | sed -E 's/^submodule\.(.*)\.path .*/\1/')"

if [ -z "${names}" ]; then
    echo "Aucun sous-module déclaré dans .gitmodules."
    exit 0
fi

echo "== submodules-doctor : audit de $(echo "${names}" | wc -l | tr -d ' ') sous-modules =="
echo

for name in ${names}; do
    path="$(git config -f "${GITMODULES}" --get "submodule.${name}.path" 2>/dev/null)"
    url="$(git config -f "${GITMODULES}" --get "submodule.${name}.url" 2>/dev/null)"
    want_branch="$(git config -f "${GITMODULES}" --get "submodule.${name}.branch" 2>/dev/null)"
    [ -z "${want_branch}" ] && want_branch="main"

    issues=()   # liste des problèmes pour ce sous-module

    # (a) initialisé ? Le préfixe de `git submodule status` fait foi :
    #     '-' = non initialisé, '+' = SHA != gitlink, 'U' = conflit de merge.
    status_line="$(git submodule status -- "${path}" 2>/dev/null)"
    prefix="${status_line:0:1}"

    if [ ! -d "${path}" ]; then
        issues+=("répertoire absent (${path})")
    fi

    if [ "${prefix}" = "-" ]; then
        issues+=("NON INITIALISÉ (git submodule update --init -- ${path})")
    fi

    # Nom dépôt (basename URL, sans .git) vs nom du path — piège type split->dve.
    # Convention projet : le dépôt s'appelle bobistudio-{plugin,service}-<slug>, où <slug>
    # DOIT correspondre au basename du path. On retire le préfixe conventionnel avant compare
    # pour ne signaler QUE les vraies anomalies (ex. split dont le dépôt est ...-dve).
    repo_base="$(basename "${url%.git}")"
    repo_slug="${repo_base#bobistudio-plugin-}"
    repo_slug="${repo_slug#bobistudio-service-}"
    path_base="$(basename "${path}")"
    if [ -n "${url}" ] && [ "${repo_slug}" != "${path_base}" ]; then
        issues+=("nom dépôt != path : dépôt=${repo_base} (slug '${repo_slug}') path=${path_base}")
    fi

    # Les vérifs suivantes n'ont de sens que si le checkout est exploitable.
    if [ -e "${path}/.git" ]; then
        head_sha="$(git -C "${path}" rev-parse HEAD 2>/dev/null)"
        cur_branch="$(git -C "${path}" rev-parse --abbrev-ref HEAD 2>/dev/null)"

        # (b) branche attendue
        if [ "${cur_branch}" = "HEAD" ]; then
            issues+=("HEAD détaché (attendu branche '${want_branch}')")
        elif [ "${cur_branch}" != "${want_branch}" ]; then
            issues+=("branche '${cur_branch}' != attendu '${want_branch}'")
        fi

        # (c) working tree propre
        if [ -n "$(git -C "${path}" status --porcelain 2>/dev/null)" ]; then
            issues+=("working tree SALE (modifs non commit)")
        fi

        # (d) SHA checkout == gitlink dans l'index du parent
        gitlink="$(git ls-files -s -- "${path}" 2>/dev/null | awk '{print $2}')"
        if [ -n "${gitlink}" ] && [ -n "${head_sha}" ] && [ "${gitlink}" != "${head_sha}" ]; then
            issues+=("SHA checkout ${head_sha:0:10} != gitlink index ${gitlink:0:10}")
        fi
    else
        issues+=("pas de .git dans ${path} (checkout absent/cassé)")
    fi

    if [ ${#issues[@]} -eq 0 ]; then
        printf "%s[ OK ]%s %-28s %sbranche=%s%s\n" "${C_OK}" "${C_RST}" "${path}" "${C_DIM}" "${want_branch}" "${C_RST}"
        n_ok=$((n_ok + 1))
    else
        printf "%s[DÉSYNC]%s %-28s %s(attendu branche=%s)%s\n" "${C_BAD}" "${C_RST}" "${path}" "${C_DIM}" "${want_branch}" "${C_RST}"
        for it in "${issues[@]}"; do
            printf "         %s- %s%s\n" "${C_WARN}" "${it}" "${C_RST}"
        done
        n_bad=$((n_bad + 1))
        problems=$((problems + 1))
    fi
done

echo
echo "== Résumé : ${n_ok} OK, ${n_bad} en DÉSYNC =="
if [ ${problems} -gt 0 ]; then
    echo "${C_BAD}Au moins un sous-module nécessite une intervention (voir ci-dessus).${C_RST}"
    exit 1
fi
echo "${C_OK}Tous les sous-modules sont cohérents.${C_RST}"
exit 0
