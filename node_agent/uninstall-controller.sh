#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# uninstall-controller.sh — retire l'ORCHESTRATEUR Bobi.Studio d'une machine.
# Pendant de install.py (chemin « Orchestrateur » / « Tout-en-un »), et frère de uninstall-node.sh.
#
# DOCTRINE — la même que pour le nœud, plus une règle qui n'existe QUE ici :
#   1. ON MONTRE AVANT DE FAIRE (`--dry-run`), 2. ON N'EFFACE PAS CE QUI N'EST PAS À NOUS,
#   3. ON DIT CE QU'ON LAISSE.
#   4. ★ ON NE DÉTRUIT PAS LA BASE SANS EN LAISSER UNE COPIE. L'orchestrateur porte l'état de TOUTE
#      l'installation : la base (nœuds, conteneurs, projets, emplacements, macros, réglages), la CA
#      du plan de contrôle, les sauvegardes. Par défaut ce script en fait donc une ARCHIVE dans
#      /root avant d'effacer quoi que ce soit — `--purge-data` pour s'en passer sciemment. Un
#      désinstalleur qui emporte la seule copie d'une régie n'est pas un outil, c'est un accident.
#
# Usage :
#   ./uninstall-controller.sh --dry-run      # inventaire seul
#   ./uninstall-controller.sh                # retrait (archive les données, demande confirmation)
#   ./uninstall-controller.sh --yes --purge-data --purge-images --purge-packages   # table rase
#
# Options :
#   --dry-run          n'exécute RIEN : affiche l'inventaire et ce qui serait fait
#   --yes              pas de confirmation interactive (obligatoire en non-interactif)
#   --purge-data       n'archive PAS la base/les sauvegardes avant d'effacer (irréversible)
#   --purge-images     supprime aussi les images Docker bobi-* présentes sur cette machine
#   --purge-packages   désinstalle aussi ffmpeg, cifs-utils, nfs-common, python3-venv/pip,
#                      keepalived (JAMAIS python3 ni curl : le système en dépend)
#   --purge-key        supprime la clé SSH que l'orchestrateur utilisait pour joindre les nœuds
#   --keep-node        ne touche pas à l'agent-nœud de cette machine (cas « tout-en-un »)
#   --purge-media      transmis au désinstalleur de NŒUD local : supprime aussi ses médias
#   --app-dir <p>      racine de l'installation (défaut /opt/bobistudio)
#
# ⚠ Ce script ne touche QUE cette machine. Les NŒUDS enrôlés continueront de tourner avec leurs
# conteneurs : les retirer un par un avec `uninstall-node.sh` AVANT de supprimer l'orchestrateur,
# sinon plus rien ne sait où ils sont (la liste vit dans la base qu'on efface ici).
set -euo pipefail

DRY=0; ASSUME_YES=0; PURGE_DATA=0; PURGE_IMAGES=0; PURGE_PKGS=0; PURGE_KEY=0; KEEP_NODE=0
PURGE_MEDIA=0          # concerne le NŒUD local (tout-en-un) : simple passe-plat vers uninstall-node.sh
APP_DIR="/opt/bobistudio"; NODE_SRC="/opt/bobi-node-src"
SERVICE="bobistudio"
EXT_ROOT="/mnt/ext"                       # montages CIFS/NFS posés par le gestionnaire de médias
VIP_CONF="/etc/keepalived/keepalived.conf"
VIP_MARKER="# Généré par Bobi.Studio"     # ne jamais toucher une conf keepalived qui n'est pas la nôtre

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_d=$'\033[2m'; c_0=$'\033[0m'
log(){ echo "${c_b}▶${c_0} $*"; }; ok(){ echo "${c_g}✓${c_0} $*"; }
warn(){ echo "${c_y}!${c_0} $*"; }; die(){ echo "${c_r}✗${c_0} $*" >&2; exit 1; }
item(){ echo "      ${c_d}·${c_0} $*"; }
# Messages follow the installer's language: BOBI_LANG (exported by get.sh and install.py), else the
# locale, as get.sh decides. An English install used to get a French removal (tester, 2026-11-22).
# The confirmation word stays RETIRER in both: it is what `install.py --confirm` expects.
case "${BOBI_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}}" in fr*|FR*|"") UI=fr;; *) UI=en;; esac
T(){ if [ "$UI" = en ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }

# Arguments d'origine mémorisés AVANT le parsing : la boucle ci-dessous les consomme (`shift`), or
# on se relance plus bas (relocalisation hors de $APP_DIR) et « $@ » serait alors VIDE — un
# --dry-run perdu en route, c'est un inventaire qui devient un retrait. Trouvé au premier essai réel.
_ARGS=("$@")

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --yes|-y) ASSUME_YES=1; shift;;
    --purge-data) PURGE_DATA=1; shift;;
    --purge-images) PURGE_IMAGES=1; shift;;
    --purge-packages) PURGE_PKGS=1; shift;;
    --purge-key) PURGE_KEY=1; shift;;
    --keep-node) KEEP_NODE=1; shift;;
    --purge-media) PURGE_MEDIA=1; shift;;
    --app-dir) APP_DIR="$2"; shift 2;;
    -h|--help) sed -n '3,32p' "$0"; exit 0;;
    *) die "$(T "option inconnue : $1" "unknown option: $1")";;
  esac
done

[ "$(id -u)" = "0" ] || die "$(T "à lancer en root." "must run as root.")"

# ─── Ce script vit peut-être DANS ce qu'il doit effacer ──────────────────────
# `node_agent/uninstall-controller.sh` est sous $APP_DIR. Or bash lit son script au fil de
# l'exécution : effacer le fichier en cours de route ferait dérailler la fin (comportement
# indéfini, retrait à moitié fait). On se recopie donc ailleurs et on se relance de là.
# Relocalisation INCONDITIONNELLE (sauf déjà relocalisé) : ce script s'efface lui-même dans DEUX
# cas — lancé depuis $APP_DIR/node_agent/, et lancé depuis /opt/bobi-node-src/ (là où l'installeur
# extrait sa charge utile, effacée au §8). Plutôt que d'énumérer les emplacements dangereux, on
# travaille toujours depuis une copie : le seul cas sûr est celui où l'on ne se lit plus soi-même.
_self="$(readlink -f "$0")"
if [ "${BOBI_RELOCATED:-0}" != "1" ]; then
  _tmp="$(mktemp -d)/uninstall-controller.sh"
  cp "$_self" "$_tmp"; chmod +x "$_tmp"
  BOBI_RELOCATED=1 BOBI_SELF_ORIG="$_self" exec "$_tmp" ${_ARGS+"${_ARGS[@]}"}
fi
# Après relocalisation, « à côté de moi » ne désigne plus rien d'utile pour trouver
# uninstall-node.sh : on repart du chemin d'ORIGINE.
_self="${BOBI_SELF_ORIG:-$_self}"

# Le désinstalleur de NŒUD, s'il est à côté (cas « tout-en-un ») : on le met de côté AVANT
# d'effacer $APP_DIR, sinon il disparaît juste avant d'avoir servi.
NODE_UNINSTALLER=""
for _cand in "$(dirname "$_self")/uninstall-node.sh" "$APP_DIR/node_agent/uninstall-node.sh"; do
  if [ -f "$_cand" ]; then
    NODE_UNINSTALLER="$(mktemp -d)/uninstall-node.sh"
    cp "$_cand" "$NODE_UNINSTALLER"; chmod +x "$NODE_UNINSTALLER"
    break
  fi
done

_docker()   { command -v docker >/dev/null 2>&1 && docker "$@" 2>/dev/null || true; }
_images()   { _docker images --filter 'reference=bobi-*' --format '{{.Repository}}:{{.Tag}}' | sort -u; }
_taille()   { du -sh "$1" 2>/dev/null | cut -f1; }

_i_list="$(_images)"
_n_img="$(printf '%s\n' "$_i_list" | sed '/^$/d' | wc -l | tr -d ' ')"
_mounts="$(mount 2>/dev/null | awk -v r="$EXT_ROOT/" '$3 ~ "^"r {print $3}' || true)"
_agent_local=0
if [ "$KEEP_NODE" = 0 ] && { [ -d /etc/bobi-node-agent ] || [ -f /etc/systemd/system/bobi-node-agent.service ]; }; then
  _agent_local=1
fi

# ─── 1. Inventaire ────────────────────────────────────────────────────────────
echo
echo "  ╔══════════════════════════════════════════════════════╗"
if [ "$UI" = en ]; then
echo "  ║  B O B I . S T U D I O  —  orchestrator removal      ║"
else
echo "  ║ B O B I . S T U D I O  —  retrait de l'orchestrateur ║"
fi
echo "  ╚══════════════════════════════════════════════════════╝"
echo
log "$(T "Inventaire de ce qui est présent sur $(hostname) :" "Inventory of what is present on $(hostname):")"

if [ -d "$APP_DIR" ]; then
  item "application $APP_DIR ($(_taille "$APP_DIR")) — $(T "code, venv, base, sauvegardes, CA du plan de contrôle" "code, venv, database, backups, control-plane CA")"
  if [ -f "$APP_DIR/db_bobistudio.db" ]; then
    item "  $(T "base" "database") $APP_DIR/db_bobistudio.db ($(_taille "$APP_DIR/db_bobistudio.db")) — $(T "nœuds, conteneurs, projets, emplacements, macros" "nodes, containers, projects, slots, macros")"
  fi
  if [ -d "$APP_DIR/backups" ]; then item "  $(T "sauvegardes" "backups") $APP_DIR/backups ($(_taille "$APP_DIR/backups"))"; fi
  if [ -d "$APP_DIR/tls" ];     then item "$(T "  CA / certificats du plan de contrôle $APP_DIR/tls" "  control-plane CA / certificates $APP_DIR/tls")"; fi
else
  item "$(T "application : $APP_DIR absent" "application: $APP_DIR absent")"
fi
if [ -f "/etc/systemd/system/$SERVICE.service" ]; then
  item "$(T "service systemd" "systemd service") $SERVICE ($(systemctl is-active $SERVICE 2>/dev/null || T inactif inactive))"
fi
if [ -d "$NODE_SRC" ]; then item "$(T "charge utile nœud" "node payload") $NODE_SRC ($(_taille "$NODE_SRC"))"; fi
if [ "$_n_img" -gt 0 ]; then item "$(T "images Docker" "Docker images") ($_n_img) : $(echo "$_i_list" | tr '\n' ' ')"; fi
if [ -n "$_mounts" ]; then item "$(T "partages montés sous" "shares mounted under") $EXT_ROOT : $(echo "$_mounts" | tr '\n' ' ') ($(T "démontés, JAMAIS effacés" "unmounted, NEVER erased"))"; fi
if [ -f "$VIP_CONF" ] && grep -qs "$VIP_MARKER" "$VIP_CONF"; then item "$(T "VIP keepalived (conf posée par Bobi.Studio)" "keepalived VIP (configuration set by Bobi.Studio)")"; fi
if [ "$_agent_local" = 1 ]; then
  if [ -n "$NODE_UNINSTALLER" ]; then item "$(T "agent-nœud LOCAL (machine « tout-en-un ») → retiré aussi, via uninstall-node.sh" "LOCAL node agent (all-in-one machine) → removed too, via uninstall-node.sh")"
  else warn "$(T "agent-nœud LOCAL présent mais uninstall-node.sh introuvable — il faudra le retirer à part." "LOCAL node agent present but uninstall-node.sh not found — it will have to be removed separately.")"; fi
fi
if [ -f /root/.ssh/id_ed25519 ]; then
  item "$(T "clé SSH de l'orchestrateur" "orchestrator SSH key") /root/.ssh/id_ed25519 ($(awk '{print $NF}' /root/.ssh/id_ed25519.pub 2>/dev/null || echo '?'))"
fi

echo
if [ "$PURGE_DATA" = 0 ]; then
  echo "$(T "    Les DONNÉES (base, sauvegardes, config_local.py, CA) seront ARCHIVÉES dans /root avant effacement." "    The DATA (database, backups, config_local.py, CA) will be ARCHIVED in /root before erasing.")"
else
  warn "$(T "--purge-data : la base, les sauvegardes et la CA seront effacées SANS copie. Irréversible." "--purge-data: the database, the backups and the CA will be erased WITHOUT a copy. Irreversible.")"
fi
if [ "$PURGE_PKGS" = 1 ]; then
  warn "$(T "--purge-packages : ffmpeg, cifs-utils, nfs-common, python3-venv/pip et keepalived seront désinstallés." "--purge-packages: ffmpeg, cifs-utils, nfs-common, python3-venv/pip and keepalived will be uninstalled.")"
else
  echo "$(T "    Paquets système (ffmpeg, python3, rsync…) : conservés (--purge-packages pour les retirer)." "    System packages (ffmpeg, python3, rsync…): kept (--purge-packages to remove them).")"
fi
if [ "$PURGE_IMAGES" = 0 ] && [ "$_n_img" -gt 0 ]; then
  echo "$(T "    Images Docker bobi-* : conservées (--purge-images pour les retirer)." "    bobi-* Docker images: kept (--purge-images to remove them).")"
fi
echo
warn "$(T "Les NŒUDS enrôlés ne sont PAS touchés : leurs conteneurs continueront de tourner, et la liste" "Enrolled NODES are NOT touched: their containers will keep running, and the list")"
echo "$(T "    de ces nœuds disparaît avec la base. Les retirer AVANT (uninstall-node.sh sur chacun) si" "    of these nodes goes with the database. Remove them FIRST (uninstall-node.sh on each) if")"
echo "$(T "    l'installation entière est mise au rebut." "    the whole installation is being retired.")"
echo

if [ "$DRY" = 1 ]; then
  ok "$(T "Simulation (--dry-run) : rien n'a été modifié." "Dry run (--dry-run): nothing was changed.")"
  exit 0
fi

# ─── 2. Confirmation ──────────────────────────────────────────────────────────
if [ "$ASSUME_YES" = 0 ]; then
  [ -t 0 ] || die "$(T "non-interactif : relancer avec --yes pour confirmer (rien n'a été fait)." "non-interactive: run again with --yes to confirm (nothing was done).")"
  printf "%b" "${c_y}?${c_0} $(T "Retirer l'orchestrateur de cette machine ? Taper" "Remove the orchestrator from this machine? Type") ${c_r}RETIRER${c_0} $(T "pour confirmer :" "to confirm:") "
  read -r _ans || _ans=""
  [ "$_ans" = "RETIRER" ] || die "$(T "annulé (rien n'a été fait)." "cancelled (nothing was done).")"
fi
echo

# ─── 3. Archive des données (AVANT tout effacement) ──────────────────────────
# Faite en premier : si le reste du retrait échoue à mi-chemin, la copie existe déjà.
ARCHIVE=""
if [ "$PURGE_DATA" = 0 ] && [ -d "$APP_DIR" ]; then
  ARCHIVE="/root/bobistudio-retrait-$(date +%Y%m%d-%H%M%S).tar.gz"
  log "$(T "Archivage des données dans $ARCHIVE…" "Archiving the data in $ARCHIVE…")"
  # Liste EXPLICITE : la base et ses journaux WAL, la conf de site, les sauvegardes, la CA, l'état
  # de la recette et l'identité de build. Ni le code ni le venv (ils se réinstallent).
  _inc=""
  for f in db_bobistudio.db db_bobistudio.db-wal db_bobistudio.db-shm config_local.py \
           db_testplan.json build_info.json backups tls static/uploads; do
    if [ -e "$APP_DIR/$f" ]; then _inc="$_inc $f"; fi
  done
  if [ -n "$_inc" ]; then
    # shellcheck disable=SC2086  (word-splitting voulu : liste de chemins relatifs)
    if tar -czf "$ARCHIVE" -C "$APP_DIR" $_inc 2>/dev/null; then
      chmod 600 "$ARCHIVE"
      ok "$(T "archive écrite :" "archive written:") $ARCHIVE ($(_taille "$ARCHIVE"))"
    else
      die "$(T "archivage ÉCHOUÉ — rien n'a été effacé. Corriger (place disque ?) ou assumer --purge-data." "archiving FAILED — nothing was erased. Fix it (disk space?) or accept --purge-data.")"
    fi
  else
    warn "$(T "aucune donnée à archiver (installation vide ?)" "no data to archive (empty installation?)")"
    ARCHIVE=""
  fi
fi

# ─── 4. Service ───────────────────────────────────────────────────────────────
if [ -f "/etc/systemd/system/$SERVICE.service" ] || systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  log "$(T "Arrêt de l'orchestrateur…" "Stopping the orchestrator…")"
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload >/dev/null 2>&1 || true
  ok "$(T "service $SERVICE arrêté et retiré" "service $SERVICE stopped and removed")"
fi

# ─── 5. VIP keepalived ────────────────────────────────────────────────────────
# UNIQUEMENT si la conf porte NOTRE marqueur : un site peut avoir son propre VRRP, et l'écraser
# serait une panne réseau offerte (même garde que app/vip.py à l'écriture).
if [ -f "$VIP_CONF" ] && grep -qs "$VIP_MARKER" "$VIP_CONF"; then
  systemctl disable --now keepalived >/dev/null 2>&1 || true
  rm -f "$VIP_CONF"
  ok "$(T "VIP keepalived retirée (conf Bobi.Studio supprimée, service arrêté)" "keepalived VIP removed (Bobi.Studio configuration deleted, service stopped)")"
elif [ -f "$VIP_CONF" ]; then
  warn "$(T "keepalived présent avec une conf qui n'est PAS la nôtre — laissée intacte." "keepalived present with a configuration that is NOT ours — left untouched.")"
fi

# ─── 6. Partages montés ───────────────────────────────────────────────────────
# On démonte ce que nous avons monté ; le contenu est DISTANT, il ne nous appartient pas.
if [ -n "$_mounts" ]; then
  log "$(T "Démontage des partages sous $EXT_ROOT…" "Unmounting the shares under $EXT_ROOT…")"
  for m in $_mounts; do
    umount "$m" >/dev/null 2>&1 || umount -l "$m" >/dev/null 2>&1 || warn "$(T "  $m non démonté (occupé)" "  $m not unmounted (busy)")"
    rmdir "$m" 2>/dev/null || true
    item "$m"
  done
  rmdir "$EXT_ROOT" 2>/dev/null || true
  ok "$(T "partages démontés (aucun fichier distant touché)" "shares unmounted (no remote file touched)")"
fi

# ─── 7. Agent-nœud local (machine « tout-en-un ») ────────────────────────────
# AVANT d'effacer $APP_DIR (le désinstalleur de nœud en vient), et sans -o pipefail surprise :
# son échec ne doit pas interrompre le retrait de l'orchestrateur.
if [ "$_agent_local" = 1 ] && [ -n "$NODE_UNINSTALLER" ]; then
  log "$(T "── retrait de l'agent-nœud local ──" "── removing the local node agent ──")"
  _nargs="--yes"
  if [ "$PURGE_IMAGES" = 1 ]; then _nargs="$_nargs --purge-images"; fi
  if [ "$PURGE_MEDIA" = 1 ]; then _nargs="$_nargs --purge-media"; fi
  # shellcheck disable=SC2086
  bash "$NODE_UNINSTALLER" $_nargs || warn "$(T "le retrait de l'agent-nœud a signalé une erreur (voir ci-dessus)" "the node agent removal reported an error (see above)")"
  echo
fi

# ─── 8. Application ───────────────────────────────────────────────────────────
if [ -d "$APP_DIR" ]; then
  log "$(T "Suppression de $APP_DIR…" "Deleting $APP_DIR…")"
  rm -rf "${APP_DIR:?}"
  ok "$(T "application supprimée" "application deleted")"
fi
if [ -d "$NODE_SRC" ]; then rm -rf "${NODE_SRC:?}"; ok "$(T "charge utile nœud $NODE_SRC supprimée" "node payload $NODE_SRC deleted")"; fi

# ─── 9. Images Docker ─────────────────────────────────────────────────────────
if [ "$PURGE_IMAGES" = 1 ]; then
  # ★ On RELIT la liste ici plutôt que de réutiliser celle de l'inventaire : sur une machine
  # tout-en-un, le désinstalleur de NŒUD (§7) vient de purger ces mêmes images. Rejouer la liste
  # d'origine affichait huit images « supprimées » qui n'existaient déjà plus — inoffensif pour la
  # machine, mais c'est un compte rendu qui ment, et un compte rendu qui ment sur un retrait est
  # exactement ce qu'on ne veut pas (signalé en recette, 2026-08-21).
  _i_list="$(_images)"
  if [ -n "$_i_list" ]; then
    log "$(T "Suppression des images Docker bobi-*…" "Deleting the bobi-* Docker images…")"
    for i in $_i_list; do _docker rmi -f "$i" >/dev/null; item "$i"; done
    ok "$(T "images supprimées" "images deleted")"
  fi
fi

# ─── 10. Clé SSH de l'orchestrateur ──────────────────────────────────────────
# Elle sert à joindre les nœuds en root. On ne la supprime QUE si elle est manifestement la nôtre
# (commentaire posé par host_ops) ou sur demande : sur une machine partagée, /root/.ssh/id_ed25519
# peut très bien être la clé d'ops de l'exploitant, qui ouvre d'autres portes que les nôtres.
if [ -f /root/.ssh/id_ed25519 ]; then
  if [ "$PURGE_KEY" = 1 ] || grep -qs bobistudio-controller /root/.ssh/id_ed25519.pub; then
    rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519.pub
    ok "$(T "clé SSH de l'orchestrateur supprimée" "orchestrator SSH key deleted")"
  else
    CLE_RESTANTE=1
  fi
fi

# ─── 11. Paquets ──────────────────────────────────────────────────────────────
if [ "$PURGE_PKGS" = 1 ]; then
  log "$(T "Désinstallation des paquets posés par l'installation…" "Uninstalling the packages the installation added…")"
  export DEBIAN_FRONTEND=noninteractive
  # JAMAIS python3 ni curl : la machine (et apt lui-même) en dépendent.
  apt-get purge -y -qq ffmpeg cifs-utils nfs-common python3-venv python3-pip keepalived >/dev/null 2>&1 || true
  apt-get autoremove -y -qq >/dev/null 2>&1 || true
  ok "$(T "paquets désinstallés (python3, curl et rsync conservés : le système s'en sert)" "packages uninstalled (python3, curl and rsync kept: the system uses them)")"
fi

# ─── 12. Résumé ───────────────────────────────────────────────────────────────
echo
ok "$(T "Orchestrateur Bobi.Studio retiré de cette machine." "Bobi.Studio orchestrator removed from this machine.")"
if [ -n "$ARCHIVE" ]; then
  echo
  echo "    ${c_g}$(T "Vos données sont dans" "Your data is in") $ARCHIVE${c_0}"
  echo "$(T "    (base, sauvegardes, config_local.py, CA du plan de contrôle, téléversements)." "    (database, backups, config_local.py, control-plane CA, uploads).")"
  echo "$(T "    Pour repartir de cet état : réinstaller, puis restaurer l'archive dans /opt/bobistudio" "    To start again from this state: reinstall, then restore the archive into /opt/bobistudio")"
  echo "$(T "    AVANT le premier démarrage du service." "    BEFORE the service first starts.")"
fi
echo "$(T "    Conservé volontairement :" "    Deliberately kept:")"
if [ "$PURGE_PKGS" = 0 ]; then echo "$(T "      · les paquets système (ffmpeg, python3, rsync…)" "      · the system packages (ffmpeg, python3, rsync…)")"; fi
if [ "$PURGE_IMAGES" = 0 ] && [ "$_n_img" -gt 0 ]; then echo "      · $_n_img $(T "image(s) Docker bobi-*" "bobi-* Docker image(s)") (--purge-images)"; fi
echo "$(T "      · la configuration réseau, les comptes et les partages distants" "      · the network configuration, the accounts and the remote shares")"
if [ "${CLE_RESTANTE:-0}" = 1 ]; then
  echo
  _kc="$(awk '{print $NF}' /root/.ssh/id_ed25519.pub 2>/dev/null)"
  warn "$(T "À VÉRIFIER : /root/.ssh/id_ed25519 a été CONSERVÉE (commentaire « $_kc » : impossible d'affirmer qu'elle est la nôtre). Elle ouvre encore un accès root aux nœuds qui l'ont dans leur authorized_keys — la supprimer avec --purge-key si elle ne sert qu'à ça." "TO CHECK: /root/.ssh/id_ed25519 was KEPT (comment \"$_kc\": no way to tell it is ours). It still opens root access to the nodes that have it in their authorized_keys — delete it with --purge-key if that is all it is for.")"
fi
echo
echo "$(T "  → Les nœuds enrôlés tournent toujours. Sur chacun : bash uninstall-node.sh" "  → The enrolled nodes are still running. On each: bash uninstall-node.sh")"
