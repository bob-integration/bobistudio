#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# uninstall-node.sh — retire Bobi.Studio d'un nœud. Pendant exact de install-node.sh : ce que
# l'installeur (et l'orchestrateur, via l'agent) a POSÉ sur l'hôte, ce script l'enlève.
#
# DOCTRINE — trois règles, dans cet ordre :
#   1. ON MONTRE AVANT DE FAIRE. Le script dresse d'abord l'inventaire de ce qu'il a RÉELLEMENT
#      trouvé sur cette machine, puis demande confirmation. `--dry-run` s'arrête après l'inventaire.
#   2. ON N'EFFACE PAS CE QUI N'EST PAS À NOUS. Les paquets tiers (Docker, pilote NVIDIA, linuxptp,
#      chrony), la configuration réseau de l'hôte et les DONNÉES média ne partent QUE sur demande
#      explicite (`--purge-packages`, `--purge-media`) : cette machine sert peut-être à autre chose,
#      et un média effacé ne revient pas.
#   3. ON DIT CE QU'ON LAISSE. Le résumé final liste ce qui reste volontairement en place et ce qui
#      exige un redémarrage — un retrait silencieusement partiel est pire qu'un retrait refusé.
#
# Usage :
#   ./uninstall-node.sh --dry-run                 # inventaire seul, ne touche à rien
#   ./uninstall-node.sh                           # retrait standard (demande confirmation)
#   ./uninstall-node.sh --yes                     # sans question (scripts / pilotage à distance)
#   ./uninstall-node.sh --yes --purge-images --purge-media --purge-packages   # table rase
#
# Options :
#   --dry-run           n'exécute RIEN : affiche l'inventaire et ce qui serait fait
#   --yes               pas de confirmation interactive (obligatoire en non-interactif)
#   --purge-images      supprime aussi les images Docker bobi-* (plusieurs Go)
#   --purge-media       supprime aussi le CONTENU de la racine média (DONNÉES — irréversible)
#   --purge-packages    désinstalle aussi les paquets posés par l'install (docker, pilote NVIDIA,
#                       nvidia-container-toolkit, linuxptp, chrony) — à n'utiliser que si la
#                       machine ne sert plus qu'à ça
#   --keep-cmdline      ne touche pas au cmdline noyau (hugepages/IOMMU/isolation de cœurs)
#   --media-mount <p>   racine média (défaut : lue dans config.json, sinon /srv/mxl-media)
#   --controller-key <k> clé publique SSH du contrôleur à retirer de authorized_keys (sinon on la
#                       cherche par son commentaire, et à défaut on le SIGNALE — cf. §7)
#
# Ce script est AUTONOME : il ne dépend ni du contrôleur, ni de l'agent, ni du reste de l'archive.
# Il peut donc être lancé à la main sur un nœud dont le contrôleur a déjà disparu.
#
# ⚠ Côté ORCHESTRATEUR : supprimer le nœud dans Réglages → Déploiement → Nœuds (c'est ce geste-là
# qui défait aussi les liens RDMA et les emplacements qui pointaient dessus). Ce script ne fait
# QUE le ménage sur l'hôte.
set -euo pipefail

DRY=0; ASSUME_YES=0; PURGE_IMAGES=0; PURGE_MEDIA=0; PURGE_PKGS=0; KEEP_CMDLINE=0
MEDIA_MOUNT=""; CTRL_KEY=""
AGENT_DIR="/opt/bobi-node-agent"; CONF_DIR="${CONF_DIR:-/etc/bobi-node-agent}"
STATE_DIR="/var/lib/bobi-node-agent"

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_d=$'\033[2m'; c_0=$'\033[0m'
log(){ echo "${c_b}▶${c_0} $*"; }; ok(){ echo "${c_g}✓${c_0} $*"; }
warn(){ echo "${c_y}!${c_0} $*"; }; die(){ echo "${c_r}✗${c_0} $*" >&2; exit 1; }
item(){ echo "      ${c_d}·${c_0} $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --yes|-y) ASSUME_YES=1; shift;;
    --purge-images) PURGE_IMAGES=1; shift;;
    --purge-media) PURGE_MEDIA=1; shift;;
    --purge-packages) PURGE_PKGS=1; shift;;
    --keep-cmdline) KEEP_CMDLINE=1; shift;;
    --media-mount) MEDIA_MOUNT="$2"; shift 2;;
    --controller-key) CTRL_KEY="$2"; shift 2;;
    -h|--help) sed -n '3,40p' "$0"; exit 0;;
    *) die "option inconnue : $1";;
  esac
done

[ "$(id -u)" = "0" ] || die "à lancer en root."

# ─── Réglages du nœud : on les LIT plutôt que de les deviner ─────────────────
# Le nom du réseau containers et la racine média ne sont pas figés (ils viennent de l'install et
# de l'orchestrateur). Les deviner ferait rater le ménage sur un nœud configuré autrement.
MACVLAN_NAME="bobimacvlan"; MXL_MOUNT="/dev/shm"
if [ -f "$CONF_DIR/config.json" ] && command -v python3 >/dev/null 2>&1; then
  _cfg="$(python3 - "$CONF_DIR/config.json" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
print("%s\t%s\t%s" % (c.get("macvlan_network") or "bobimacvlan",
                      c.get("media_mount") or "/srv/mxl-media",
                      c.get("mxl_mount") or "/dev/shm"))
PYEOF
)"
  if [ -n "${_cfg:-}" ]; then
    MACVLAN_NAME="$(echo "$_cfg" | cut -f1)"
    [ -n "$MEDIA_MOUNT" ] || MEDIA_MOUNT="$(echo "$_cfg" | cut -f2)"
    MXL_MOUNT="$(echo "$_cfg" | cut -f3)"
  fi
fi
[ -n "$MEDIA_MOUNT" ] || MEDIA_MOUNT="/srv/mxl-media"

# Unités systemd posées soit par l'installeur, soit par l'orchestrateur via l'agent (préparation
# hôte MTL, RDMA, VLAN…). Liste EXPLICITE : un glob « bobi-* » attraperait un jour l'unité de
# quelqu'un d'autre. Les sous-interfaces VLAN sont le seul motif, elles portent le nom de l'iface.
UNITS="bobi-node-agent bobi-node-bootstrap mxl-ptp4l mxl-phc2sys bobi-cpufreq-perf
       bobi-irq-housekeeping bobi-mxl-options bobi-nicqueues rdma-netns-exclusive
       bobi-vfio-bind bobi-sriov-vf"

# Fichiers déposés hors des dossiers applicatifs (chacun est un réglage HÔTE que nous avons posé).
FILES="/etc/systemd/journald.conf.d/10-bobi.conf
       /etc/sysctl.d/10-bobi-hugepages.conf
       /etc/sysctl.d/10-bobi-rdma-arp.conf
       /etc/modules-load.d/bobi-rdma.conf
       /etc/tmpfiles.d/bobi-mxl.conf
       /etc/tmpfiles.d/bobi-mxl-domain.conf
       /etc/chrony/conf.d/bobi-tai.conf
       /etc/apt/sources.list.d/bobi-kernel.list
       /etc/linuxptp/ptp4l.conf
       /usr/local/sbin/bobi-nicqueues.sh"

_present_units=""; _present_files=""; _vlan_units=""

_scan() {
  for u in $UNITS; do
    if [ -f "/etc/systemd/system/$u.service" ]; then _present_units="$_present_units $u"; fi
  done
  # Unités posées PAR INTERFACE par l'orchestrateur au moment de configurer le réseau containers :
  # `bobi-vlan-<iface>` quand le parent macvlan est une sous-interface VLAN, `bobi-link-<iface>`
  # quand c'est une carte nue (l'unité ne fait que la monter au boot). Ce sont les deux branches du
  # même code (app/routes/node_network.py) : n'en balayer qu'une laissait l'autre derrière — un
  # `bobi-link-eno1.service` survivait au retrait (signalé en recette, 2026-08-21).
  for f in $(ls /etc/systemd/system/bobi-vlan-*.service /etc/systemd/system/bobi-link-*.service 2>/dev/null || true); do
    _vlan_units="$_vlan_units $(basename "$f" .service)"
  done
  for f in $FILES; do
    if [ -e "$f" ]; then _present_files="$_present_files $f"; fi
  done
}
_scan

# Deux formes distinctes, et la distinction compte : `_docker` est TOLÉRANT (sortie vide si docker
# n'est pas là) et sert à LISTER ; `_docker_q` propage le vrai code retour et sert à TESTER — les
# confondre ferait « voir » un réseau ou une image qui n'existe pas.
_docker()   { command -v docker >/dev/null 2>&1 && docker "$@" 2>/dev/null || true; }
_docker_q() { command -v docker >/dev/null 2>&1 && docker "$@" >/dev/null 2>&1; }

# Conteneurs à retirer : ceux qui tournent sur une image bobi-* (les conteneurs managés) ET ceux
# attachés au réseau containers. On ne se fie PAS aux noms (ils viennent des hostnames choisis par
# l'exploitant) — un conteneur étranger portant un nom proche ne doit pas être détruit.
_containers() {
  { _docker ps -a --filter "ancestor=bobi-compute" --format '{{.Names}}'
    _docker ps -a --format '{{.Names}}\t{{.Image}}' | awk -F'\t' '$2 ~ /^bobi-/ {print $1}'
    _docker network inspect "$MACVLAN_NAME" --format '{{range .Containers}}{{.Name}}
{{end}}' 2>/dev/null
  } | sed '/^$/d' | sort -u
}
_images() { _docker images --filter 'reference=bobi-*' --format '{{.Repository}}:{{.Tag}}' | sort -u; }

# ─── 1. Inventaire ────────────────────────────────────────────────────────────
echo
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   B O B I . S T U D I O  —  retrait du nœud          ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo
log "Inventaire de ce qui est présent sur $(hostname) :"

_c_list="$(_containers)"; _i_list="$(_images)"
_n_cont="$(printf '%s\n' "$_c_list" | sed '/^$/d' | wc -l | tr -d ' ')"
_n_img="$(printf '%s\n' "$_i_list" | sed '/^$/d' | wc -l | tr -d ' ')"

if [ -d "$AGENT_DIR" ] || [ -d "$CONF_DIR" ]; then
  item "agent-nœud : $AGENT_DIR, $CONF_DIR (dont le token et le matériel mTLS), $STATE_DIR"
else
  item "agent-nœud : absent"
fi
if [ -n "$_present_units$_vlan_units" ]; then item "unités systemd :$_present_units$_vlan_units"; fi
if [ -n "$_present_files" ]; then item "réglages hôte :$(echo "$_present_files" | tr '\n' ' ')"; fi
if [ "$_n_cont" -gt 0 ]; then item "conteneurs ($_n_cont) : $(echo "$_c_list" | tr '\n' ' ')"; fi
if [ "$_n_img" -gt 0 ]; then item "images Docker ($_n_img) : $(echo "$_i_list" | tr '\n' ' ')"; fi
if _docker_q network inspect "$MACVLAN_NAME"; then item "réseau containers « $MACVLAN_NAME »"; fi
if [ -d "$MXL_MOUNT/mxl" ]; then item "domaine MXL $MXL_MOUNT/mxl ($(du -sh "$MXL_MOUNT/mxl" 2>/dev/null | cut -f1))"; fi
if [ -d /etc/bobi ]; then item "/etc/bobi (binds vfio, SR-IOV, domain_def)"; fi
if [ -d "$MEDIA_MOUNT" ]; then item "racine média $MEDIA_MOUNT ($(du -sh "$MEDIA_MOUNT" 2>/dev/null | cut -f1)) — CONSERVÉE sauf --purge-media"; fi
if [ -s /root/.ssh/authorized_keys ]; then
  item "clé(s) SSH autorisée(s) : $(awk '{print $NF}' /root/.ssh/authorized_keys | tr '\n' ' ')"
fi
if [ "$KEEP_CMDLINE" = 0 ] && grep -qsE 'hugepages=|isolcpus=|nohz_full=|rcu_nocbs=|intel_iommu=' /etc/default/grub; then
  item "cmdline noyau (hugepages / isolation de cœurs / IOMMU) → nettoyé, REBOOT nécessaire"
fi
if [ -s /etc/bobi/vfio-binds ]; then item "port(s) réseau bindés à vfio-pci → rendus au pilote noyau"; fi

echo
echo "    Conservés dans tous les cas : la configuration réseau de l'hôte, les dépôts apt Debian,"
echo "    les comptes et les clés SSH d'autrui."
if [ "$PURGE_PKGS" = 1 ]; then
  warn "--purge-packages : docker, pilote NVIDIA, nvidia-container-toolkit, linuxptp et chrony seront DÉSINSTALLÉS."
else
  echo "    Paquets tiers (docker, pilote NVIDIA, linuxptp, chrony) : conservés (--purge-packages pour les retirer)."
fi
if [ "$PURGE_IMAGES" = 0 ]; then echo "    Images Docker bobi-* : conservées (--purge-images pour les retirer)."; fi
if [ "$PURGE_MEDIA" = 1 ]; then warn "--purge-media : le CONTENU de $MEDIA_MOUNT sera SUPPRIMÉ (irréversible)."; fi
echo

if [ "$DRY" = 1 ]; then
  ok "Simulation (--dry-run) : rien n'a été modifié."
  exit 0
fi

# ─── 2. Confirmation ──────────────────────────────────────────────────────────
# En non-interactif SANS --yes on REFUSE plutôt que de supposer : ce script détruit, et il est
# appelable à distance (pipé sur stdin par l'orchestrateur) — le défaut doit être « ne rien faire ».
if [ "$ASSUME_YES" = 0 ]; then
  [ -t 0 ] || die "non-interactif : relancer avec --yes pour confirmer (rien n'a été fait)."
  printf "%b" "${c_y}?${c_0} Retirer Bobi.Studio de ce nœud ? Taper ${c_r}RETIRER${c_0} pour confirmer : "
  read -r _ans || _ans=""
  [ "$_ans" = "RETIRER" ] || die "annulé (rien n'a été fait)."
fi
echo

# ─── 3. Conteneurs ────────────────────────────────────────────────────────────
# D'abord les conteneurs : tant qu'ils tournent, ils tiennent le réseau macvlan et les fichiers du
# domaine MXL, et le réseau refuserait de partir.
if [ "$_n_cont" -gt 0 ]; then
  log "Arrêt et suppression de $_n_cont conteneur(s)…"
  for c in $_c_list; do
    _docker rm -f "$c" >/dev/null
    item "$c"
  done
  ok "conteneurs retirés"
fi

# ─── 4. Unités systemd ────────────────────────────────────────────────────────
# L'agent est traité EN DERNIER (§10) : ce script tourne peut-être dans un shell qu'il a lancé.
log "Arrêt des services…"
for u in $_present_units $_vlan_units; do
  if [ "$u" = "bobi-node-agent" ]; then continue; fi   # traité en dernier (§10), et détaché
  systemctl disable --now "$u.service" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$u.service"
  item "$u"
done
systemctl daemon-reload >/dev/null 2>&1 || true
ok "services retirés"

# ─── 5. Ports réseau rendus au noyau (vfio-pci → ice/mlx5) ───────────────────
# Un port laissé sur vfio-pci n'a plus de netdev : la machine perdrait une carte pour toujours,
# sans rien afficher d'anormal ailleurs que dans `ip link`. On le rend AVANT d'effacer /etc/bobi
# (c'est ce fichier qui dit lesquels sont à rendre).
if [ -s /etc/bobi/vfio-binds ]; then
  log "Restitution des ports bindés à vfio-pci…"
  while read -r bdf _rest; do
    [ -n "$bdf" ] || continue
    d="/sys/bus/pci/devices/$bdf"
    [ -e "$d" ] || continue
    cur="$(basename "$(readlink "$d/driver" 2>/dev/null)" 2>/dev/null || true)"
    [ "$cur" = "vfio-pci" ] || continue
    echo "" > "$d/driver_override" 2>/dev/null || true
    echo "$bdf" > /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null || true
    echo "$bdf" > /sys/bus/pci/drivers_probe 2>/dev/null || true
    item "$bdf rendu au pilote noyau"
  done < /etc/bobi/vfio-binds
  ok "ports restitués"
fi

# ─── 6. Réseau containers + domaine MXL ──────────────────────────────────────
if _docker_q network inspect "$MACVLAN_NAME"; then
  _docker network rm "$MACVLAN_NAME" >/dev/null && ok "réseau containers « $MACVLAN_NAME » supprimé" \
    || warn "réseau « $MACVLAN_NAME » non supprimé (un conteneur y est-il encore attaché ?)"
fi
if [ -d "$MXL_MOUNT/mxl" ]; then
  rm -rf "${MXL_MOUNT:?}/mxl"
  ok "domaine MXL $MXL_MOUNT/mxl effacé"
fi

# ─── 7. Réglages hôte ─────────────────────────────────────────────────────────
if [ -n "$_present_files" ] || [ -d /etc/bobi ]; then
  log "Retrait des réglages hôte…"
  for f in $_present_files; do rm -f "$f"; item "$f"; done
  rm -rf /etc/bobi
  # `vfio.conf` ne porte que « vfio-pci » et c'est NOUS qui l'avons écrit (préparation MTL) ; on ne
  # l'enlève que s'il ne contient rien d'autre, pour ne pas casser une conf préexistante.
  if [ -f /etc/modules-load.d/vfio.conf ] && [ "$(tr -d ' \n' < /etc/modules-load.d/vfio.conf)" = "vfio-pci" ]; then
    rm -f /etc/modules-load.d/vfio.conf; item "/etc/modules-load.d/vfio.conf"
  fi
  # Les valeurs sysctl restent APPLIQUÉES jusqu'au reboot : on les remet à leur défaut tout de suite
  # (un retrait qui ne se voit qu'au prochain démarrage n'est pas un retrait).
  sysctl -qw net.ipv4.conf.all.arp_ignore=0 net.ipv4.conf.all.arp_announce=0 2>/dev/null || true
  systemctl restart systemd-journald >/dev/null 2>&1 || true
  systemctl restart chrony >/dev/null 2>&1 || true
  ok "réglages hôte retirés"
fi

# Clé SSH du contrôleur (posée à l'enrôlement). ★ On ne peut pas la deviner à coup sûr : son
# commentaire dépend de QUAND elle a été générée (`bobistudio-controller` depuis host_ops, mais les
# clés plus anciennes portent le nom d'hôte du contrôleur, ex. « orchestrateur@orchestrateur »).
# Trois cas, et surtout : quand on ne sait pas, ON LE DIT. Effacer au hasard une ligne de
# authorized_keys couperait l'accès de quelqu'un d'autre ; la laisser en silence rendrait le retrait
# faussement complet — un accès root du contrôleur survivrait au « retrait ».
CLE_RESTANTE=0
if [ -s /root/.ssh/authorized_keys ]; then
  if [ -n "$CTRL_KEY" ]; then
    _b64="$(echo "$CTRL_KEY" | awk '{print $2}')"          # la partie clé, pas le commentaire
    if [ -n "$_b64" ] && grep -qF "$_b64" /root/.ssh/authorized_keys; then
      grep -vF "$_b64" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp \
        && mv /root/.ssh/authorized_keys.tmp /root/.ssh/authorized_keys
      chmod 600 /root/.ssh/authorized_keys
      ok "clé SSH du contrôleur retirée de /root/.ssh/authorized_keys"
    else
      warn "clé fournie (--controller-key) ABSENTE de authorized_keys — rien retiré."
    fi
  elif grep -qs bobistudio-controller /root/.ssh/authorized_keys; then
    sed -i '/bobistudio-controller/d' /root/.ssh/authorized_keys
    ok "clé SSH du contrôleur retirée de /root/.ssh/authorized_keys"
  else
    CLE_RESTANTE=1
  fi
fi

# ─── 8. cmdline noyau ─────────────────────────────────────────────────────────
# hugepages, isolation de cœurs et IOMMU ont été gravés au boot pour le moteur 2110. Les laisser
# geler de la RAM et sortir des cœurs de l'ordonnanceur sur une machine rendue à un autre usage.
REBOOT_REQUIS=0
if [ "$KEEP_CMDLINE" = 0 ] && [ -f /etc/default/grub ] \
   && grep -qE 'hugepages=|isolcpus=|nohz_full=|rcu_nocbs=|intel_iommu=|iommu=pt|hugepagesz=' /etc/default/grub; then
  log "Nettoyage du cmdline noyau…"
  cp -a /etc/default/grub "/etc/default/grub.bak.retrait-$(date +%Y%m%d-%H%M%S)"
  sed -i -E 's/(default_hugepagesz|hugepagesz|hugepages|isolcpus|nohz_full|rcu_nocbs|intel_iommu|iommu)=[^ "]*//g; s/  +/ /g; s/= "/="/; s/ "$/"/' /etc/default/grub
  if command -v update-grub >/dev/null 2>&1; then
    update-grub >/dev/null 2>&1 || warn "update-grub a échoué — vérifier /etc/default/grub à la main"
  elif command -v proxmox-boot-tool >/dev/null 2>&1; then
    proxmox-boot-tool refresh >/dev/null 2>&1 || true
  fi
  # Les hugepages déjà réservées le restent jusqu'au reboot : on les rend tout de suite (baisser
  # nr_hugepages réussit toujours, contrairement à monter).
  for h in /sys/kernel/mm/hugepages/hugepages-*/nr_hugepages; do
    [ -w "$h" ] && echo 0 > "$h" 2>/dev/null || true
  done
  REBOOT_REQUIS=1
  ok "cmdline nettoyé (sauvegarde /etc/default/grub.bak.retrait-*)"
fi

# ─── 9. Images / médias / paquets (sur demande explicite) ────────────────────
if [ "$PURGE_IMAGES" = 1 ] && [ "$_n_img" -gt 0 ]; then
  log "Suppression des images Docker bobi-*…"
  for i in $_i_list; do _docker rmi -f "$i" >/dev/null; item "$i"; done
  ok "images supprimées"
fi

if [ "$PURGE_MEDIA" = 1 ] && [ -d "$MEDIA_MOUNT" ]; then
  # On vide la racine SANS l'effacer : ce peut être un point de MONTAGE (baie, NFS), et le
  # supprimer masquerait le montage plutôt que d'en retirer le contenu.
  log "Suppression du contenu de $MEDIA_MOUNT…"
  find "$MEDIA_MOUNT" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  ok "racine média vidée (le point de montage lui-même est conservé)"
fi

if [ "$PURGE_PKGS" = 1 ]; then
  log "Désinstallation des paquets posés par l'install…"
  export DEBIAN_FRONTEND=noninteractive
  rm -f /etc/apt/sources.list.d/nvidia-container-toolkit.list \
        /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  # `nvidia-*` d'abord (le module DKMS se désinstalle avec son paquet), docker ensuite.
  apt-get purge -y -qq nvidia-driver nvidia-smi libcuda1 nvidia-kernel-dkms \
      nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 \
      libnvidia-container-tools >/dev/null 2>&1 || true
  apt-get purge -y -qq docker.io docker-cli docker-buildx >/dev/null 2>&1 || true
  apt-get purge -y -qq linuxptp chrony >/dev/null 2>&1 || true
  apt-get autoremove -y -qq >/dev/null 2>&1 || true
  apt-get update -qq >/dev/null 2>&1 || true
  ok "paquets désinstallés (les composants apt contrib/non-free restent activés : ils ne gênent rien)"
  REBOOT_REQUIS=1
fi

# ─── 10. L'agent, en dernier — et détaché ────────────────────────────────────
# Ce script peut avoir été lancé PAR l'agent (retrait piloté depuis l'orchestrateur : le script est
# pipé sur /v1/host/exec). S'arrêter soi-même tuerait le shell avant la fin, et le retrait
# resterait à moitié fait. On délègue donc la dernière étape à un processus DÉTACHÉ, qui laisse à
# l'appelant le temps de recevoir la sortie ci-dessous.
if [ -d "$AGENT_DIR" ] || [ -d "$CONF_DIR" ] || [ -f /etc/systemd/system/bobi-node-agent.service ]; then
  setsid nohup bash -c "
    sleep 3
    systemctl disable --now bobi-node-agent.service >/dev/null 2>&1
    rm -f /etc/systemd/system/bobi-node-agent.service
    systemctl daemon-reload >/dev/null 2>&1
    rm -rf '$AGENT_DIR' '$CONF_DIR' '$STATE_DIR'
  " >/dev/null 2>&1 < /dev/null &
  ok "agent-nœud : arrêt et effacement lancés (token et certificat mTLS compris) — quelques secondes"
fi

# ─── 11. Résumé ───────────────────────────────────────────────────────────────
echo
ok "Bobi.Studio retiré de ce nœud."
echo "    Conservé volontairement :"
if [ "$PURGE_PKGS" = 0 ]; then echo "      · paquets tiers (docker, pilote NVIDIA, linuxptp, chrony) et leurs réglages propres"; fi
if [ "$PURGE_IMAGES" = 0 ] && [ "$_n_img" -gt 0 ]; then echo "      · $_n_img image(s) Docker bobi-* (--purge-images)"; fi
if [ "$PURGE_MEDIA" = 0 ] && [ -d "$MEDIA_MOUNT" ]; then echo "      · les médias de $MEDIA_MOUNT (--purge-media)"; fi
echo "      · la configuration réseau de l'hôte et les dépôts apt"
if [ "${CLE_RESTANTE:-0}" = 1 ]; then
  echo
  warn "À VÉRIFIER À LA MAIN : /root/.ssh/authorized_keys contient des clés que ce script n'a pas su attribuer au contrôleur (commentaire non reconnu). Tant qu'elle y est, le contrôleur garde un accès root à cette machine :"
  awk '{print "      · " $NF}' /root/.ssh/authorized_keys
  echo "      → relancer avec --controller-key \"<clé>\", ou retirer la ligne à la main."
fi
if [ "$REBOOT_REQUIS" = 1 ]; then
  echo
  warn "REBOOT nécessaire : le cmdline noyau a changé (hugepages / isolation de cœurs / IOMMU restent actifs jusque-là)."
fi
echo
echo "  → Côté orchestrateur : supprimer ce nœud dans Réglages → Déploiement → Nœuds"
echo "    (c'est ce geste qui défait aussi ses liens RDMA et libère les emplacements de production)."
