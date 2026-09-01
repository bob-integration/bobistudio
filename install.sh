#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# install.sh — amorce d'installation depuis une source DÉJÀ PRÉSENTE (clone git, archive dépliée,
# clé USB). Son seul travail : réunir les conditions minimales, puis passer la main à l'installeur
# unifié `install/install.py`, qui pose tout le reste (venv, dépendances, horloge TAI, service
# systemd, base) et propose le menu — nœud, orchestrateur, tout-en-un, désinstallation.
#
#     bash install.sh
#
# ★ POURQUOI CE FICHIER EXISTE ENCORE.
# Il a longtemps fait l'installation lui-même, en parallèle de install.py. Deux chemins pour la même
# cible finissent toujours par DIVERGER : celui-ci posait l'horloge TAI, l'autre non — donc un
# contrôleur installé par le menu restait à 37 s de la grille média, sans que rien ne le signale.
# L'étape a été repliée dans install.py, et ce script est redevenu ce qu'il aurait dû rester : une
# amorce. Il subsiste parce qu'il ne demande RIEN — pas même python3, qu'il propose d'installer —
# et parce que « bash install.sh » est dans les doigts de tout le monde.
#
# Cf. get.sh pour l'équivalent depuis GitHub, sur une machine vierge.
set -euo pipefail
cd "$(dirname "$0")"

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "  ${c_b}·${c_0} $*"; }; ok(){ echo "  ${c_g}✓${c_0} $*"; }
warn(){ echo "  ${c_y}!${c_0} $*"; }; die(){ echo "  ${c_r}✗${c_0} $*" >&2; exit 1; }

echo
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║                B O B I . S T U D I O                 ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo

[ "$(id -u)" = "0" ] || die "à lancer en root (l'installation pose des services systemd)."

INSTALLEUR="install/install.py"
[ -f "$INSTALLEUR" ] || die "$INSTALLEUR introuvable — ce script doit être lancé depuis la racine de la source."

# ─── python3 : le seul prérequis, et on ne se contente pas de le constater ────
# Sur une Debian minimale il peut manquer. Échouer en disant « installez python3 » renvoie
# l'exploitant à une commande qu'on sait taper à sa place — autant la proposer.
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
  apt-get update -qq || die "apt-get update a échoué — dépôts injoignables ? Rien n'a été modifié."
  apt-get install -y -qq python3 || die "installation de python3 échouée — rien n'a été modifié."
  # apt peut rendre 0 sans avoir posé le binaire (miroir partiel, paquet retenu). On CONSTATE.
  command -v python3 >/dev/null 2>&1 \
    || die "apt s'est terminé sans erreur mais python3 reste introuvable — dépôts incomplets ?"
  ok "python3 installé ($(python3 --version 2>&1))"
fi

exec python3 "$INSTALLEUR" "$@"
