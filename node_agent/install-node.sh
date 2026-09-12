#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# install-node.sh — Bring-up d'un nœud Bobi.Studio SANS Proxmox (Phase C).
# Idempotent, capability-selectable. Sur une box Debian 13 (trixie) nue, provisionne UNIQUEMENT
# les capacités demandées et installe bobi-node-agent (Python + systemd). Cf. NODE_AGENT.md.
#
# Usage :
#   ./install-node.sh --with compute,media,webrtc \
#       --macvlan-parent eno1 --macvlan-subnet x.x.x.x/24 --macvlan-gateway x.x.x.x
#   ./install-node.sh --with io2110,compute --mtl-iface ens1f0np0 --ptp-domain 127 \
#       --hugepages 2048 --macvlan-parent ens1f0np0 --macvlan-subnet x.x.x.x/24 ...
#
# Options :
#   --with <caps>          liste CSV parmi io2110,compute,media,webrtc,gpu      (défaut: compute)
#                          (gpu = pilote NVIDIA Debian + container-toolkit ; reboot requis)
#   --add-caps <caps>      RATTRAPAGE : ajoute une/des capacité(s) à un nœud DÉJÀ installé, puis sort.
#                          Ne provisionne QUE ces capacités et FUSIONNE la liste dans config.json
#                          (token, TLS, unité systemd, réglages non concernés : intacts).
#                          Accepte les 5 capacités. Réglages amenés par la capacité : passer
#                          --mtl-iface/--lcores (io2110), --media-mount (media) comme à l'install.
#                          Code de sortie ≠ 0 si le provisioning hôte a échoué.
#   DDP_SRC=<path>         (env) chemin du blob DDP E810 quand le script est PIPÉ sur stdin (le
#                          répertoire firmware/ ne voyage pas avec) — cf. --add-caps io2110.
#   --token <tok>          token agent (défaut: généré)                          [affiché à la fin]
#   --controller-url <u>   URL du contrôleur, mémorisée dans config.json (page :80 / référence)
#   --port <n>             port agent                                            (défaut: 9100)
#   --macvlan-name <n>     nom du réseau docker macvlan                          (défaut: bobimacvlan)
#   --macvlan-parent <if>  interface parente du macvlan                          (requis si compute/media/webrtc)
#   --macvlan-subnet <c>   sous-réseau (CIDR)                                    (requis si macvlan)
#   --macvlan-gateway <ip> passerelle du sous-réseau
#   --macvlan-range <cidr> plage d'auto-allocation IPAM (hors IP fixes du LAN)
#   --mtl-iface <if>       interface E810 (io2110)
#   --ptp-domain <n>       domaine PTP                                           (défaut: 127)
#   --ptp-priority1 <n>    priority1 BMCA                                        (défaut: 128)
#   --ptp-priority2 <n>    priority2 BMCA                                        (défaut: 128)
#   --ptp-log-announce <n> logAnnounceInterval                                   (défaut: 0)
#   --ptp-log-sync <n>     logSyncInterval (-3 = 8 sync/s, profil 2110)          (défaut: -3)
#   --ptp-log-delay-req <n> logMinDelayReqInterval                               (défaut: -3)
#   --ptp-announce-to <n>  announceReceiptTimeout                                (défaut: 3)
#   --ptp-delay-thresh <n> neighborPropDelayThresh                               (défaut: 800)
#   --ptp-utc-offset <n>   utc_offset (TAI-UTC)                                  (défaut: 37)
#   --ptp-sw-ts            timestamping logiciel                                 (défaut: hardware)
#   --hugepages <n>        nb de pages 2M à réserver (io2110)                    (défaut: 2048 = 4 Go)
#   --hugepages-1g <n>     nb de pages 1G (profil MTL ; ajoute le cmdline kernel, reboot requis)
#   --lcores <spec>        cœurs réservés MTL (ex. 2-9)
# Le profil PTP par défaut = SMPTE 2059-2 (ST 2110-10), identique à app/ptp.py:_ptp4l_conf
# (source de vérité côté orchestrateur) — garder les deux synchrones.
#   --mxl-mount <path>     point de montage shm                                  (défaut: /dev/shm)
#   --media-mount <path>   racine du stockage média (capacité media)             (défaut: /srv/mxl-media)
#   --registry <host>      registry pour `docker pull <registry>/<image>` (sinon: images supposées présentes)
#   --images <csv>         tags d'images d'intérêt (défaut dérivé des capacités)
#   --no-start             installe sans démarrer l'agent
#   --reset-tls            SUPPRIME sans demander un certificat mTLS déjà présent (ré-enrôlement).
#                          Sans ça, un cert d'un enrôlement PRÉCÉDENT fait redémarrer l'agent
#                          verrouillé sur l'ANCIENNE CA → impossible de le ré-enrôler ailleurs.
#   --keep-tls             conserve explicitement un cert existant (pas de question, pas de reset)
set -euo pipefail

# ─── Défauts ────────────────────────────────────────────────────────────────
CAPS="compute"; ADD_CAPS=""; TOKEN=""; PORT=9100; CONTROLLER_URL=""
MACVLAN_NAME="bobimacvlan"; MACVLAN_PARENT=""; MACVLAN_SUBNET=""; MACVLAN_GW=""; MACVLAN_RANGE=""
MTL_IFACE=""; PTP_DOMAIN=127; HUGEPAGES=2048; HUGEPAGES_1G=""; LCORES=""; MXL_MOUNT="/dev/shm"
MEDIA_MOUNT="/srv/mxl-media"
# Profil PTP SMPTE 2059-2 par défaut (cf. app/ptp.py:_ptp4l_conf) — la valeur par défaut suffit
# pour un nœud 2110 ; surchargeable par flag.
PTP_PRIO1=128; PTP_PRIO2=128; PTP_LOG_ANNOUNCE=0; PTP_LOG_SYNC=-3; PTP_LOG_DELAY_REQ=-3
PTP_ANNOUNCE_TO=3; PTP_DELAY_THRESH=800; PTP_UTC_OFFSET=37; PTP_HW_TS=1
REGISTRY=""; IMAGES=""; IMAGES_EXPLICIT=0; START=1; RESET_TLS=""; KEEP_TLS=""
# Noyau compatible MTL (io2110). NON figé : fournis par le profil (réglage cluster). Vide = aucun
# changement de noyau (avertissement seulement). KERNEL_APT = ligne sources.list optionnelle.
KERNEL_PKG=""; KERNEL_APT=""
# CONF_DIR surchargeable par l'environnement : sert à éprouver le mode --add-caps (fusion de
# config.json) hors d'un vrai nœud. Défaut inchangé en exploitation.
AGENT_DIR="/opt/bobi-node-agent"; CONF_DIR="${CONF_DIR:-/etc/bobi-node-agent}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "${c_b}▶${c_0} $*"; }; ok(){ echo "${c_g}✓${c_0} $*"; }
warn(){ echo "${c_y}!${c_0} $*"; }; die(){ echo "${c_r}✗${c_0} $*" >&2; exit 1; }
has_cap(){ case ",$CAPS," in *",$1,"*) return 0;; *) return 1;; esac; }

# ─── Mode hors-ligne : bundle vendor/debs embarqué (dépôt apt local file://) ──
# Si le paquet a été déployé avec un bundle offline, `apt-get "${APT_OPTS[@]}" …` puise dans un
# dépôt local (aucun réseau ; seul ce dépôt est visible → une dép absente échoue net, jamais de
# réseau). Sinon APT_OPTS reste vide = apt en ligne normal. NE PAS ajouter --no-download (chemin
# relatif passé à dpkg sur file://). Le noyau MTL (io2110) n'est PAS dans le bundle → étape en ligne.
APT_OPTS=()
_vendor_debs="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)/vendor/debs"
if [ -d "$_vendor_debs" ] && ls "$_vendor_debs"/*.deb >/dev/null 2>&1; then
  _apt_src="$(mktemp)"
  echo "deb [trusted=yes] file://$_vendor_debs ./" > "$_apt_src"
  APT_OPTS=(-o "Dir::Etc::sourcelist=$_apt_src" -o "Dir::Etc::sourceparts=-" -o "APT::Get::List-Cleanup=0")
  trap 'rm -f "$_apt_src"' EXIT
fi

# ─── Provisioning PAR CAPACITÉ (fonctions) ───────────────────────────────────
# Chaque capacité qui pose quelque chose sur l'HÔTE est une fonction autonome, appelée soit par
# l'install complète (« if has_cap X; then provision_X; fi »), soit par le mode --add-caps (rattrapage
# d'une capacité oubliée à l'enrôlement, cf. plus bas). Deux exigences pour ce double usage :
#   · IDEMPOTENCE : re-jouer la fonction sur un nœud déjà provisionné ne casse rien ;
#   · ÉCHEC OBSERVABLE : les étapes restent best-effort (une install pilote ratée ne doit pas bloquer
#     l'agent) MAIS elles POSENT un drapeau `<CAP>_FAIL`. Sans ça, le rattrapage piloté depuis l'UI
#     renverrait rc=0 et afficherait « ✓ fait » sur un stack absent — exactement le mensonge dell-1
#     (gpu_capable=1 alors que CUDA était cassé). Le drapeau devient le code de sortie en --add-caps.
GPU_FAIL=0; IO2110_FAIL=0; MACVLAN_FAIL=0

# _extra_set <json> <clé> <valeur> → json avec la clé posée. Passe par python3 (déjà exigé par le
# mode --add-caps) plutôt que par de la concaténation de chaînes : une valeur contenant un guillemet
# ou une barre oblique produirait sinon un JSON invalide, et la fusion de config.json échouerait
# APRÈS que l'hôte a été modifié.
_extra_set() {
  ES_JSON="$1" ES_KEY="$2" ES_VAL="$3" python3 -c 'import json, os
d = json.loads(os.environ["ES_JSON"]); d[os.environ["ES_KEY"]] = os.environ["ES_VAL"]; print(json.dumps(d))'
}

# ─ Capacités macvlan (compute / media / webrtc) ────────────────────────────────
# Ces trois-là ne posent PRESQUE RIEN sur l'hôte, et c'est voulu : depuis le « réseau containers
# différé » (phase 2, cf. NODE_AGENT.md §2), le macvlan n'est plus figé à l'install — l'orchestrateur
# l'assigne APRÈS l'enrôlement en choisissant la carte parent dans l'inventaire remonté par l'agent
# (`/v1/host/networks/ensure`). Le parent macvlan n'est d'ailleurs stocké NULLE PART en base : il est
# ressaisi dans la page Réseau du nœud. Le provisioning de ces capacités se réduit donc à :
#   · vérifier que le réseau containers existe (sinon DIRE lequel, et où le créer) ;
#   · pour `media`, poser la racine média ;
# le reste (images runtime) étant poussé par le contrôleur. On garde malgré tout une fonction par
# capacité : c'est la DÉCLARATION qui compte (l'agent gate ses services hôte dessus, et le contrôleur
# lit `/v1/capabilities`), et une fonction vide est plus honnête qu'un cas particulier dans le
# dispatch — quand l'une d'elles gagnera une étape hôte, elle a déjà sa place.

# Vérifie la présence du réseau containers (macvlan). Facteur commun aux trois capacités macvlan :
# sans lui, un conteneur compute/media/webrtc ne démarrera pas — autant le dire tout de suite plutôt
# que de laisser découvrir la panne au premier déploiement.
_check_macvlan() {
  if docker network inspect "$MACVLAN_NAME" >/dev/null 2>&1; then
    ok "$1 : réseau containers « $MACVLAN_NAME » présent"
  else
    # PAS un échec (pas de `MACVLAN_FAIL`) : le réseau containers est DIFFÉRÉ par conception — la
    # carte parente ne se devine pas, elle se choisit dans la page Réseau du nœud. Le drapeau d'échec
    # ferait sortir en rc≠0 et l'UI dirait « capacité non utilisable », ce qui pousserait à ré-appuyer
    # sur le bouton alors que l'action utile est ailleurs. On avertit, et l'orchestrateur relaie
    # l'étape restante dans ses « Suites ».
    warn "$1 : réseau containers « $MACVLAN_NAME » ABSENT — à créer depuis l'orchestrateur (Réglages → nœud → Configuration → Réseau), qui demande la carte parente. Sans lui, aucun conteneur $1 ne démarrera."
  fi
}

provision_compute() { _check_macvlan compute; }
provision_webrtc()  { _check_macvlan webrtc; }

provision_media() {
  _check_macvlan media
  # Racine média : montée en bind dans les conteneurs media (cf. agent.py, CONFIG.media_mount).
  # Docker la créerait à la volée en root:root ; on la pose explicitement pour que le montage ne
  # dépende pas d'un effet de bord du moteur.
  if mkdir -p "$MEDIA_MOUNT" 2>/dev/null; then
    ok "media : racine média $MEDIA_MOUNT prête"
  else
    warn "media : création de $MEDIA_MOUNT impossible — les conteneurs média n'auront pas de stockage."
    MACVLAN_FAIL=1
  fi
}

# Capacité io2110 (E810 / hugepages / PTP / noyau MTL).
provision_io2110() {
  # ★ HORLOGE : un nœud io2110 ne doit PAS être discipliné par chrony — son horloge système porte du
  # TAI, posée par le client PTP interne de libmtl (cf. §2a bis). Sur un nœud qui DEVIENT io2110 après
  # coup, chrony est déjà installé et configuré : deux disciplines battraient alors sur la même
  # horloge et chrony ramènerait REALTIME vers l'UTC, soit 37 s d'erreur pour le moteur — et rien ne
  # le signalerait (les flux paraîtraient seulement « périmés » chez leurs consommateurs). On retire
  # donc la config TAI et on arrête chrony. Idempotent : sans chrony, ne fait rien.
  if [ -f /etc/chrony/conf.d/bobi-tai.conf ] || systemctl is-active chrony >/dev/null 2>&1; then
    rm -f /etc/chrony/conf.d/bobi-tai.conf
    systemctl disable --now chrony >/dev/null 2>&1 || true
    warn "io2110 : chrony DÉSACTIVÉ (l'horloge d'un nœud 2110 relève du moteur, pas de NTP). ⚠ Tant que le moteur MTL ne tourne pas, ce nœud n'a plus de discipline d'horloge."
  fi

  if [ -n "$MTL_IFACE" ]; then
    ip link show "$MTL_IFACE" >/dev/null 2>&1 || die "interface $MTL_IFACE introuvable."
    drv="$(ethtool -i "$MTL_IFACE" 2>/dev/null | sed -n 's/^driver: //p')"
    [ "$drv" = "ice" ] || warn "interface $MTL_IFACE pilotée par '$drv' (attendu: ice/E810)."
  else
    # DIFFÉRÉ (recommandé) : aucune carte E810 fournie à l'install → on installe ce qui est
    # indépendant de l'iface (image MTL, noyau, hugepages) et on DIFFÈRE le PTP + le binding NIC.
    # L'orchestrateur choisit la carte (inventaire remonté par l'agent) puis pousse le PTP après
    # l'enrôlement (Déploiement → Nœuds). Provisioning non bloqué.
    warn "io2110 : aucune carte E810 fournie à l'install — config PTP/binding NIC différés : à configurer depuis l'orchestrateur (Déploiement → Nœuds) une fois l'agent enrôlé (le binaire linuxptp est tout de même installé)."
  fi

  # ─── Package DDP Intel E810 (intel/ice/ddp/ice.pkg) ─────────────────────────
  # SANS ce package, le driver `ice` démarre en SAFE MODE : il désactive l'horloge PTP matérielle
  # (`ethtool -T` → « PTP Hardware Clock: none » → ptp4l « failed to create a clock ») ET le flow
  # steering/RSS indispensable au 2110/MTL. Debian ne le fournit PAS (absent de firmware-misc-nonfree).
  # On le vendore dans node_agent/firmware/ice/ (binaire Intel licencié — cf. licence à côté) et on le
  # pose ici, indépendamment de l'iface (comme linuxptp). Le DDP est lu par le driver au PROBE : on
  # NE recharge PAS `ice` ici (couperait les ports ice, dont parfois le contrôle) — le REBOOT déjà
  # imposé par io2110 (noyau MTL + hugepages 1G au cmdline, plus bas) le chargera.
  # `--ddp-src` : en rattrapage (--add-caps), le script est PIPÉ sur stdin — il n'a donc pas de
  # répertoire à côté de lui, et le blob (1,4 Mo) ne voyage pas avec. Le contrôleur le dépose d'abord
  # sur le nœud puis passe son chemin ici. En install normale, le défaut vaut le fichier vendoré.
  _ddp_src="${DDP_SRC:-$SCRIPT_DIR/firmware/ice/ice_comms-1.3.63.0.pkg}"
  if [ -f "$_ddp_src" ]; then
    mkdir -p /lib/firmware/intel/ice/ddp
    install -m 0644 "$_ddp_src" /lib/firmware/intel/ice/ddp/ice-1.3.63.0.pkg
    ln -sf ice-1.3.63.0.pkg /lib/firmware/intel/ice/ddp/ice.pkg
    if dmesg 2>/dev/null | grep -q 'ice.*Safe Mode'; then
      warn "DDP E810 posé — driver ice actuellement en SAFE MODE : REBOOT (déjà requis par io2110) nécessaire pour le charger."
    else
      ok "DDP E810 (ICE COMMS 1.3.63.0) posé dans /lib/firmware/intel/ice/ddp/ (chargé au prochain probe/reboot)."
    fi
  else
    warn "DDP E810 introuvable ($_ddp_src) — sans lui, ice démarrera en Safe Mode (pas de PTP HW ni de steering 2110). Fournir node_agent/firmware/ice/."
    # Safe Mode = PAS d'horloge PTP matérielle ni de steering : la capacité io2110 serait déclarée
    # mais inopérante. C'est un échec, pas un détail (cf. [e810-ddp-safe-mode-ptp]).
    IO2110_FAIL=1
  fi

  # ─── Noyau compatible MTL ──────────────────────────────────────────────────
  # MTL/DPDK (ice + AF_XDP) exige un noyau validé (6.14 sur la pile de référence) ; le noyau Debian
  # stock de l'install ne convient pas forcément. KERNEL_PKG/KERNEL_APT viennent du profil (réglage
  # cluster) — RIEN n'est figé ici. Sans paquet fourni : on AVERTIT, on ne casse rien (cf. option a).
  if [ -n "$KERNEL_PKG" ]; then
    if [ -n "$KERNEL_APT" ] && ! grep -qrF "$KERNEL_APT" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
      echo "$KERNEL_APT" > /etc/apt/sources.list.d/bobi-kernel.list
      log "source apt noyau ajoutée : $KERNEL_APT"
      apt-get update -qq || warn "apt-get update (source noyau) a échoué"
    fi
    log "Installation du noyau MTL : $KERNEL_PKG"
    # shellcheck disable=SC2086  (word-splitting voulu : image + headers éventuels)
    if apt-get install -y -qq $KERNEL_PKG; then
      update-grub >/dev/null 2>&1 || true
      ok "noyau '$KERNEL_PKG' installé — REBOOT requis pour basculer dessus (courant : $(uname -r))"
    else
      warn "échec installation du noyau '$KERNEL_PKG' — io2110/MTL risque de ne pas fonctionner."
      IO2110_FAIL=1
    fi
  else
    warn "io2110 : aucun noyau MTL épinglé (réglage vide). Noyau courant $(uname -r) — vérifier la compatibilité MTL (6.14 validé) ; sinon MTL ne démarrera pas."
  fi

  mkdir -p /dev/hugepages
  mountpoint -q /dev/hugepages || mount -t hugetlbfs none /dev/hugepages || true
  if [ -n "$HUGEPAGES_1G" ]; then
    # Pages 1 Go (profil MTL/DPDK). Fiable UNIQUEMENT si réservées au boot via le cmdline kernel :
    # on tente le runtime (best-effort, peut échouer sur mémoire fragmentée) ET on grave le cmdline
    # pour le prochain boot. Reboot requis pour garantir les N pages.
    log "Réservation de $HUGEPAGES_1G page(s) 1G (profil MTL)…"
    if [ -d /sys/kernel/mm/hugepages/hugepages-1048576kB ]; then
      echo "$HUGEPAGES_1G" > /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null \
        || warn "réservation 1G runtime échouée (normal sur mémoire fragmentée → reboot)"
    fi
    cmdline="default_hugepagesz=1G hugepagesz=1G hugepages=$HUGEPAGES_1G"
    if [ -f /etc/default/grub ]; then
      # Idempotent : retire toute directive hugepage* préexistante de la ligne, puis ré-ajoute la nôtre.
      cur="$(sed -n 's/^GRUB_CMDLINE_LINUX="\(.*\)"$/\1/p' /etc/default/grub | head -1)"
      cleaned="$(echo "$cur" | sed -E 's/(default_hugepagesz|hugepagesz|hugepages)=[^ ]*//g' | xargs)"
      sed -i "s|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"$cleaned $cmdline\"|" /etc/default/grub
      command -v update-grub >/dev/null 2>&1 && update-grub >/dev/null 2>&1 || warn "update-grub indisponible — graver $cmdline manuellement au cmdline kernel"
      warn "hugepages 1G : cmdline kernel mis à jour → REBOOT requis pour garantir $HUGEPAGES_1G page(s)"
    else
      warn "/etc/default/grub absent — graver « $cmdline » au cmdline kernel manuellement"
    fi
    # GARDE-FOU CRITIQUE : `vm.nr_hugepages` cible la taille hugepage PAR DÉFAUT — ici 1G (posée par
    # `default_hugepagesz=1G` ci-dessus). Un fichier sysctl résiduel d'une install 2M
    # (`vm.nr_hugepages = 2048`) serait alors ré-appliqué par systemd-sysctl APRÈS le cmdline et
    # tenterait 2048 pages de 1G → bridé par la RAM, quasi toute la RAM gelée en hugepages
    # (nœud inutilisable, ~2 Go libres). On réécrit donc le fichier avec le compte 1G correct pour
    # qu'il reste cohérent avec le cmdline (idempotent ; écrase tout résidu 2M).
    echo "vm.nr_hugepages = $HUGEPAGES_1G" > /etc/sysctl.d/10-bobi-hugepages.conf
    ok "hugepages 1G : $(cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null || echo '?') page(s) actives (def. boot=$HUGEPAGES_1G)"
  else
    log "Réservation de $HUGEPAGES hugepages 2M…"
    echo "$HUGEPAGES" > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages \
      || { warn "réservation hugepages échouée — MTL/DPDK ne démarrera pas sans hugepages."; IO2110_FAIL=1; }
    echo "vm.nr_hugepages = $HUGEPAGES" > /etc/sysctl.d/10-bobi-hugepages.conf
    ok "hugepages : $(cat /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages 2>/dev/null) page(s) 2M"
  fi

  # Durcissement ARP pour les nœuds MULTI-HOMÉS (plusieurs ports RDMA agrégés, plans média séparés).
  # Défaut Linux (arp_ignore=0, « weak host ») : le nœud répond à une requête ARP pour n'importe
  # laquelle de ses adresses locales depuis N'IMPORTE quelle interface. Sur deux ports RDMA, le pair
  # apprend alors la MAC du premier port pour l'adresse du second, et tout le trafic ressort par un
  # seul fil : deux liens ACTIVE, aucune erreur, la moitié de la bande passante perdue en silence.
  # arp_ignore=1 (ne répondre que pour l'adresse de l'interface qui reçoit) + arp_announce=2
  # (annoncer l'adresse source de l'interface de sortie). Sans effet sur un nœud mono-port, donc posé
  # inconditionnellement. Le service RDMA le rejoue à chaque établissement de lien (auto-réparation
  # après un ré-enrôlement, qui remet la configuration réseau à plat).
  printf 'net.ipv4.conf.all.arp_ignore = 1\nnet.ipv4.conf.all.arp_announce = 2\n' \
    > /etc/sysctl.d/10-bobi-rdma-arp.conf
  sysctl -qw net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2 2>/dev/null \
    || warn "durcissement ARP non appliqué à chaud (effectif au prochain boot)"
  ok "ARP durci (arp_ignore=1, arp_announce=2) — requis dès qu'un nœud porte 2 ports RDMA"

  # Paquet linuxptp : indépendant de l'iface (comme le noyau/hugepages), donc TOUJOURS posé en
  # capacité io2110 — même en mode différé. Sinon `ptp4l` manque sur le nœud et l'orchestrateur
  # (Réglages → PTP / Rendre Opérationnel) ne peut pas démarrer le service après l'enrôlement.
  log "Installation de linuxptp (ptp4l/phc2sys)…"
  # PAS de `die` ici : en rattrapage, sortir au milieu laisserait l'hôte à moitié provisionné ET la
  # capacité non déclarée (le merge de config.json vient après). On drapeaute — la capacité est alors
  # déclarée mais annoncée comme inutilisable, ce qui est vérifiable, contrairement à un abandon muet.
  apt-get "${APT_OPTS[@]}" install -y -qq linuxptp >/dev/null \
    || { warn "échec install linuxptp — ptp4l/phc2sys absents : le PTP du nœud ne pourra pas démarrer."; IO2110_FAIL=1; }

  # Config PTP : liée à l'iface E810 → sautée si différé (l'orchestrateur la poussera après
  # l'enrôlement, une fois la carte choisie). Seul le binaire est posé inconditionnellement ci-dessus.
  if [ -n "$MTL_IFACE" ]; then
  log "Config PTP sur $MTL_IFACE, domaine $PTP_DOMAIN (profil SMPTE 2059-2)…"
  mkdir -p /etc/linuxptp
  # Profil complet identique à app/ptp.py:_ptp4l_conf (source de vérité). NE PAS réduire à
  # domain/slaveOnly : les intervalles sync/announce/delay conditionnent la qualité du lock 2110.
  ts_mode="hardware"; ts_flag=""
  [ "$PTP_HW_TS" = "0" ] && ts_mode="software" && ts_flag="-S"
  cat > /etc/linuxptp/ptp4l.conf <<PTPCONF
# Géré par install-node.sh (profil SMPTE 2059-2 / ST 2110-10) — cf. app/ptp.py
[global]
domainNumber              $PTP_DOMAIN
priority1                 $PTP_PRIO1
priority2                 $PTP_PRIO2
logAnnounceInterval       $PTP_LOG_ANNOUNCE
logSyncInterval           $PTP_LOG_SYNC
logMinDelayReqInterval    $PTP_LOG_DELAY_REQ
announceReceiptTimeout    $PTP_ANNOUNCE_TO
syncReceiptTimeout        0
neighborPropDelayThresh   $PTP_DELAY_THRESH
utc_offset                $PTP_UTC_OFFSET
time_stamping             $ts_mode
gmCapable                 1
free_running              0
clock_servo               pi
[$MTL_IFACE]
PTPCONF
  # Noms d'unités mxl-* : alignés sur app/ptp.py (source de vérité) + la sonde/contrôle de l'agent.
  cat > /etc/systemd/system/mxl-ptp4l.service <<UNIT
[Unit]
Description=MXL ptp4l (IEEE 1588) — bobi node
After=network-online.target
Wants=network-online.target
[Service]
# L'iface PTP peut être DOWN au boot (hors /etc/network/interfaces) → la monter avant ptp4l.
ExecStartPre=-/sbin/ip link set $MTL_IFACE up
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/ptp4l.conf -i $MTL_IFACE $ts_flag
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  # phc2sys : -n <domain> OBLIGATOIRE (sinon il interroge ptp4l sur le domaine 0 et reste bloqué
  # « Waiting for ptp4l » quand ptp4l tourne sur 127 — cf. PROD-007). HW → -s <iface> -O 0 -w ;
  # software → -a -r -r (suit ptp4l via socket).
  if [ "$PTP_HW_TS" = "0" ]; then
    phc2sys_exec="/usr/sbin/phc2sys -a -r -r -n $PTP_DOMAIN"
  else
    phc2sys_exec="/usr/sbin/phc2sys -s $MTL_IFACE -O 0 -w -n $PTP_DOMAIN"
  fi
  cat > /etc/systemd/system/mxl-phc2sys.service <<UNIT
[Unit]
Description=MXL phc2sys (PHC → système) — bobi node
After=mxl-ptp4l.service
Requires=mxl-ptp4l.service
[Service]
ExecStart=$phc2sys_exec
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now mxl-ptp4l mxl-phc2sys 2>/dev/null || warn "mxl-ptp4l/mxl-phc2sys non démarrés (vérifier le lien/horloge)"
  ok "PTP configuré (profil 2059-2, ts=$ts_mode, sync=$PTP_LOG_SYNC, domaine=$PTP_DOMAIN)"
  fi
}

# Capacité GPU (NVIDIA : pilote Debian + container-toolkit).
# Pose le stack NVIDIA : pilote (paquet Debian `nvidia-driver`, module DKMS) + `nvidia-container-toolkit`
# (dépôt NVIDIA) + configuration du runtime `nvidia` de Docker. Après un REBOOT (chargement du module),
# `nvidia-smi` répond et « Détecter GPU » passe au vert. Cf. NODE_AGENT.md.
provision_gpu() {
  # Garde matérielle : pas de GPU NVIDIA présent → inutile d'installer un pilote. On avertit et on
  # saute (même logique que io2110 qui diffère sans carte E810). Provisioning non bloqué.
  # ATTENTION `set -o pipefail` (haut du script) : ne PAS utiliser `lspci | grep -qi` — `grep -q`
  # quitte au 1er match et ferme le tube → lspci meurt en SIGPIPE (141) → pipefail rend le tube
  # non-zéro ALORS QU'un GPU EST présent → fausse conclusion « aucun GPU ». On compte (grep -c lit
  # tout le flux, pas de SIGPIPE) et on tolère l'absence (|| true) → robuste sous pipefail.
  if [ "$(lspci 2>/dev/null | grep -ci nvidia || true)" -eq 0 ]; then
    warn "gpu : aucun GPU NVIDIA détecté (lspci) — stack NVIDIA non installé. Cocher « gpu » n'a d'effet que sur un nœud équipé."
    # PAS un échec de provisioning : le script a fait ce qu'il pouvait sur un nœud sans carte. Le
    # drapeau reste à 0 (rc=0) ; l'UI distingue le cas au texte, et « Détecter GPU » restera rouge.
    return 0
  fi
  # `head` ferme le tube tôt → SIGPIPE en amont → sous set -e+pipefail l'assignation tuerait le
  # script ; `|| true` neutralise (le modèle n'est qu'informatif).
  _gpu_model="$(lspci 2>/dev/null | grep -iE 'vga|3d|display' | grep -i nvidia | head -1 | sed 's/.*: //' || true)"
  log "gpu : GPU NVIDIA détecté (${_gpu_model:-inconnu}) — installation pilote + container-toolkit…"

  # 1) Composants apt `contrib` + `non-free` + `non-free-firmware` : le pilote Debian a besoin des
  #    TROIS — `nvidia-driver` et `nvidia-kernel-dkms` sont en non-free, ses dépendances
  #    `nvidia-support` / `nvidia-installer-cleanup` sont en CONTRIB, et `nvidia-kernel-dkms` a un
  #    `Depends: firmware-nvidia-gsp (= <version>)` (firmware GSP) qui vit en NON-FREE-FIRMWARE —
  #    dépendance DURE et versionnée, donc composant manquant = `apt-get install nvidia-driver`
  #    échoue franchement (vérifié sur r620-3, trixie/550.163.01-2). Ne PAS confondre avec
  #    `firmware-nvidia-graphics` (source firmware-nonfree) qui est le firmware du pilote LIBRE.
  #    L'installateur trixie active non-free-firmware par défaut, mais notre preseed ne fixe aucun
  #    `apt-setup/*` : on ne dépend pas d'un défaut qu'on ne contrôle pas. On ajoute chaque composant
  #    manquant (comme composant autonome), deb822 (debian.sources) ou une-ligne. Garde
  #    `(^|[ ,])$_c([ ,]|$)` : « non-free » ne matche PAS dans « non-free-firmware » (le tiret n'est
  #    ni [ ,] ni fin) — les deux sont donc traités indépendamment, et un composant déjà présent
  #    n'est pas dupliqué ; ne PAS utiliser `\bnon-free\b` (il matcherait DANS non-free-firmware).
  #    Idempotent.
  #    ★ On ne regarde QUE les lignes ACTIVES : l'installeur trixie laisse en tête de
  #    `/etc/apt/sources.list` une ligne `#deb cdrom:[…]/ trixie contrib main non-free-firmware`
  #    COMMENTÉE. Un grep naïf y voyait « contrib » et concluait « déjà présent » → contrib jamais
  #    ajouté → `apt-get install nvidia-driver` échoue (nvidia-installer-cleanup / nvidia-support /
  #    glx-alternative-nvidia / nvidia-legacy-check vivent en CONTRIB) → pas de pilote, et le nœud
  #    invitait quand même à rebooter (vécu sur r620-3, 2026-08-19). D'où le filtre `^[[:space:]]*#`.
  #    `grep -c` (pas `-q`) : lit tout le flux, donc pas de SIGPIPE sous `set -o pipefail`.
  _apt_changed=0
  for _c in contrib non-free non-free-firmware; do
    _vu="$(grep -rh '' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null \
             | grep -vE '^[[:space:]]*#' \
             | grep -cE "(^|[ ,])$_c([ ,]|\$)" || true)"
    if [ "${_vu:-0}" -gt 0 ]; then continue; fi
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then
      sed -i -E "/^Components:/ s/\$/ $_c/" /etc/apt/sources.list.d/debian.sources; _apt_changed=1
    elif [ -f /etc/apt/sources.list ]; then
      sed -i -E "/^deb .*debian/ s/\$/ $_c/" /etc/apt/sources.list; _apt_changed=1
    fi
  done
  if [ "$_apt_changed" = 1 ]; then
    log "gpu : composants apt contrib/non-free/non-free-firmware activés"
    apt-get "${APT_OPTS[@]}" update -qq || warn "gpu : apt-get update après activation contrib/non-free/non-free-firmware a échoué."
  fi

  # 2) En-têtes noyau (compilation DKMS) + pilote Debian + nvidia-smi + libcuda1. `nvidia-smi` ET
  #    `libcuda1` sont des paquets SÉPARÉS (Recommends de nvidia-driver) : avec --no-install-recommends
  #    il faut les NOMMER, sinon ils sont sautés. `nvidia-smi` manquant → détection « command not
  #    found ». ★ `libcuda1` manquant (= libcuda.so.1, driver CUDA userspace) = PIÈGE SILENCIEUX :
  #    nvidia-smi liste quand même le GPU (via libnvidia-ml) mais affiche « CUDA Version: Not Found »,
  #    et TOUT cupy échoue (cudaErrorInsufficientDriver) → un mur GPU retombe en numpy CPU SANS un mot,
  #    alors que « Détecter GPU » passait au vert (bug vécu sur dell-1, cf. mémoire). On l'installe
  #    explicitement + on vérifie le CUDA userspace côté détection (_probe_gpu).
  log "gpu : installation linux-headers + nvidia-driver + nvidia-smi + libcuda1 (DKMS)…"
  apt-get "${APT_OPTS[@]}" install -y -qq linux-headers-amd64 "linux-headers-$(uname -r)" >/dev/null 2>&1 \
    || apt-get "${APT_OPTS[@]}" install -y -qq linux-headers-amd64 >/dev/null 2>&1 \
    || warn "gpu : en-têtes noyau non installés — la compilation DKMS du pilote peut échouer."
  if apt-get "${APT_OPTS[@]}" install -y -qq nvidia-driver nvidia-smi libcuda1 >/dev/null 2>&1; then
    ok "gpu : nvidia-driver + nvidia-smi + libcuda1 installés (module DKMS)."
    # Le code retour d'apt ne dit PAS que le module existe : DKMS peut avoir échoué à compiler
    # (en-têtes absents, noyau exotique) et l'install se terminerait sur un « REBOOT requis »
    # suivi d'un « Détecter GPU » rouge, sans que rien n'ait signalé le vrai problème.
    if ! ls /lib/modules/"$(uname -r)"/updates/dkms/nvidia*.ko* >/dev/null 2>&1 \
       && ! dkms status 2>/dev/null | grep -q ': *installed'; then
      warn "gpu : pilote installé mais AUCUN module noyau compilé pour $(uname -r) (DKMS) — le GPU restera indisponible. Vérifier « dkms status » et les en-têtes du noyau courant."
      GPU_FAIL=1
    fi
    # Secure Boot : le module DKMS n'est pas signé par une clé connue du firmware → le noyau
    # REFUSE de le charger au boot. Le stack paraît parfait jusqu'au reboot, où rien ne marche.
    if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
      warn "gpu : Secure Boot ACTIVÉ — le module DKMS non signé sera REFUSÉ au chargement. Enrôler la clé (mokutil --import /var/lib/dkms/mok.pub, puis validation à l'écran au reboot) ou désactiver Secure Boot dans le BIOS."
      GPU_FAIL=1
    fi
  else
    warn "gpu : échec install nvidia-driver/nvidia-smi/libcuda1 (composants contrib+non-free actifs ? accès réseau ?) — GPU non exploitable."
    GPU_FAIL=1
  fi

  # 3) nvidia-container-toolkit (dépôt NVIDIA, hors Debian → Internet requis) + runtime Docker.
  #    Sans lui, `docker run --gpus` échoue et « Détecter GPU » reste rouge malgré le pilote.
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    log "gpu : installation nvidia-container-toolkit (dépôt NVIDIA)…"
    _nvkey=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    _nvrepo=/etc/apt/sources.list.d/nvidia-container-toolkit.list
    # `gpg` (gnupg) est requis pour dérober la clé du dépôt ; souvent absent d'une install minimale.
    command -v gpg >/dev/null 2>&1 || apt-get "${APT_OPTS[@]}" install -y -qq gnupg >/dev/null 2>&1 || true
    # N'écrire le dépôt QUE si la clé a bien été posée : sinon le .list non signé casse tout
    # `apt-get update` du nœud (dépôt « InRelease n'est pas signé »).
    install -m 0755 -d /usr/share/keyrings
    if curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey 2>/dev/null | gpg --dearmor -o "$_nvkey" 2>/dev/null && [ -s "$_nvkey" ]; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list 2>/dev/null \
        | sed "s#deb https://#deb [signed-by=$_nvkey] https://#g" > "$_nvrepo"
      apt-get update -qq || true
      apt-get install -y -qq nvidia-container-toolkit >/dev/null 2>&1 \
        && ok "gpu : nvidia-container-toolkit installé" \
        || { warn "gpu : échec install nvidia-container-toolkit (dépôt NVIDIA joignable ?) — « docker run --gpus » indispo."; GPU_FAIL=1; }
    else
      rm -f "$_nvkey"
      warn "gpu : clé GPG NVIDIA non récupérée (réseau ? gnupg ?) — nvidia-container-toolkit NON installé (dépôt non ajouté pour ne pas casser apt)."
      GPU_FAIL=1
    fi
  fi
  if command -v nvidia-ctk >/dev/null 2>&1; then
    nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1 \
      && { systemctl restart docker 2>/dev/null || true; ok "gpu : runtime « nvidia » configuré dans Docker (restart docker)."; } \
      || { warn "gpu : « nvidia-ctk runtime configure » a échoué — configurer le runtime nvidia à la main."; GPU_FAIL=1; }
  fi
  # Ne PAS inviter à rebooter quand le pilote n'est pas installé : un reboot ne charge pas un module
  # absent, et le testeur repart sur un « Détecter GPU » rouge sans savoir que l'install a échoué.
  if [ "${GPU_FAIL:-0}" = 1 ]; then
    warn "gpu : stack NVIDIA INCOMPLET (cf. échec(s) ci-dessus) — un REBOOT n'y changera rien tant que le pilote n'est pas installé."
  else
    warn "gpu : REBOOT du nœud requis pour charger le pilote NVIDIA — ensuite « Détecter GPU » passera au vert."
  fi
  return 0
}

# ─── Parsing des arguments ──────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --with) CAPS="$2"; shift 2;;
    --add-caps) ADD_CAPS="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --controller-url) CONTROLLER_URL="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --macvlan-name) MACVLAN_NAME="$2"; shift 2;;
    --macvlan-parent) MACVLAN_PARENT="$2"; shift 2;;
    --macvlan-subnet) MACVLAN_SUBNET="$2"; shift 2;;
    --macvlan-gateway) MACVLAN_GW="$2"; shift 2;;
    --macvlan-range) MACVLAN_RANGE="$2"; shift 2;;
    --mtl-iface) MTL_IFACE="$2"; shift 2;;
    --ptp-domain) PTP_DOMAIN="$2"; shift 2;;
    --ptp-priority1) PTP_PRIO1="$2"; shift 2;;
    --ptp-priority2) PTP_PRIO2="$2"; shift 2;;
    --ptp-log-announce) PTP_LOG_ANNOUNCE="$2"; shift 2;;
    --ptp-log-sync) PTP_LOG_SYNC="$2"; shift 2;;
    --ptp-log-delay-req) PTP_LOG_DELAY_REQ="$2"; shift 2;;
    --ptp-announce-to) PTP_ANNOUNCE_TO="$2"; shift 2;;
    --ptp-delay-thresh) PTP_DELAY_THRESH="$2"; shift 2;;
    --ptp-utc-offset) PTP_UTC_OFFSET="$2"; shift 2;;
    --ptp-sw-ts) PTP_HW_TS=0; shift;;
    --hugepages) HUGEPAGES="$2"; shift 2;;
    --hugepages-1g) HUGEPAGES_1G="$2"; shift 2;;
    --lcores) LCORES="$2"; shift 2;;
    --mxl-mount) MXL_MOUNT="$2"; shift 2;;
    --media-mount) MEDIA_MOUNT="$2"; shift 2;;
    --registry) REGISTRY="$2"; shift 2;;
    --images) IMAGES="${2//,/ }"; IMAGES_EXPLICIT=1; shift 2;;   # CSV ou espaces (itéré sur les espaces)
    --reset-tls) RESET_TLS=1; shift;;
    --keep-tls) KEEP_TLS=1; shift;;
    --kernel-pkg) KERNEL_PKG="$2"; shift 2;;
    --kernel-apt) KERNEL_APT="$2"; shift 2;;
    --no-start) START=0; shift;;
    *) die "option inconnue : $1";;
  esac
done

[ "$(id -u)" = "0" ] || die "à lancer en root."

# ─── Mode --add-caps : RATTRAPAGE d'une capacité oubliée à l'enrôlement ──────
# Ajoute une capacité à un nœud DÉJÀ installé, sans réinstaller quoi que ce soit d'autre. Piloté
# depuis l'UI (Réglages → nœud → « Ajouter une capacité »), qui pipe ce script sur stdin de
# /v1/host/exec — donc AUCUNE dépendance à un payload présent sur le nœud, et pas besoin d'agent.py.
#
# Pourquoi un mode dédié plutôt qu'un simple re-run de l'install complète — deux pièges MORTELS :
#   1. l'install complète RÉÉCRIT config.json en entier → mtl_iface / lcores / macvlan_network sont
#      blanchis si les arguments d'origine ne sont pas tous reproduits (ils ne sont pas tous en base :
#      le parent macvlan, par exemple, n'y est pas) ;
#   2. sans --token elle RÉGÉNÈRE le token de l'agent → le contrôleur perd le nœud.
# Ici on FUSIONNE la capacité dans config.json (tout le reste est préservé à l'octet près, token
# compris) et on ne touche NI au TLS, NI à l'unité systemd, NI aux images, NI au réseau.
if [ -n "$ADD_CAPS" ]; then
  # 1) VALIDER d'abord (pur, sans effet de bord) — un nom refusé ne doit pas dépendre de l'état de
  #    l'hôte, et surtout : aucune capacité ne doit être provisionnée si une AUTRE de la liste est
  #    invalide (sinon on sort en erreur après avoir déjà modifié la machine).
  for _cap in ${ADD_CAPS//,/ }; do
    case "$_cap" in
      gpu|io2110|compute|media|webrtc) ;;
      *) die "--add-caps : capacité inconnue « $_cap »";;
    esac
  done
  # 2) Puis l'environnement, AVANT de provisionner : sans config.json fusionnable, poser le stack sur
  #    l'hôte laisserait une capacité installée mais jamais annoncée par l'agent — le pire des deux.
  [ -f "$CONF_DIR/config.json" ] || die "--add-caps : $CONF_DIR/config.json introuvable — ce nœud n'a pas d'agent installé (utiliser l'installation complète)."
  command -v python3 >/dev/null 2>&1 || die "--add-caps : python3 requis (fusion de config.json)."
  # 3) Provisionner. `_extra` accumule les clés de config.json à poser EN PLUS des capacités : une
  #    capacité ajoutée après coup amène ses propres réglages (la carte E810 et les lcores pour
  #    io2110, la racine média pour media), que l'agent lit dans son config.json. Les omettre
  #    déclarerait la capacité sans les moyens de l'exercer.
  _fail=0; _reboot=0; _extra="{}"
  for _cap in ${ADD_CAPS//,/ }; do
    case "$_cap" in
      gpu)
        log "── rattrapage capacité « gpu » ──"; provision_gpu
        [ "$GPU_FAIL" = 0 ] || _fail=1
        _reboot=1;;            # module DKMS à charger
      io2110)
        log "── rattrapage capacité « io2110 » ──"; provision_io2110
        [ "$IO2110_FAIL" = 0 ] || _fail=1
        _extra="$(_extra_set "$_extra" mtl_iface "$MTL_IFACE")"
        _extra="$(_extra_set "$_extra" lcores "$LCORES")"
        _reboot=1;;            # noyau MTL + cmdline hugepages 1G + DDP lu au probe
      compute) log "── rattrapage capacité « compute » ──"; provision_compute; [ "$MACVLAN_FAIL" = 0 ] || _fail=1;;
      webrtc)  log "── rattrapage capacité « webrtc » ──";  provision_webrtc;  [ "$MACVLAN_FAIL" = 0 ] || _fail=1;;
      media)
        log "── rattrapage capacité « media » ──"; provision_media
        [ "$MACVLAN_FAIL" = 0 ] || _fail=1
        _extra="$(_extra_set "$_extra" media_mount "$MEDIA_MOUNT")";;
    esac
  done

  # Fusion dans config.json : on ne réécrit QUE `capabilities` (union, ordre stable) et les clés de
  # `_extra` non vides ; le reste du fichier est relu/réécrit tel quel. Écriture atomique (tmp +
  # replace) puis mode 0600 restauré : le fichier porte le token de l'agent.
  log "fusion de « $ADD_CAPS » dans $CONF_DIR/config.json…"
  ADD_CAPS="$ADD_CAPS" ADD_EXTRA="$_extra" python3 - "$CONF_DIR/config.json" <<'PYEOF' || die "--add-caps : fusion de config.json échouée (fichier laissé intact)."
import json, os, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
caps = list(cfg.get("capabilities") or [])
for c in (os.environ.get("ADD_CAPS") or "").split(","):
    c = c.strip()
    if c and c not in caps:
        caps.append(c)
cfg["capabilities"] = caps
# Réglages amenés par la capacité. On n'ÉCRASE une clé existante que si la nouvelle valeur est non
# vide : un rattrapage lancé sans --mtl-iface ne doit pas effacer la carte déjà configurée.
extra = json.loads(os.environ.get("ADD_EXTRA") or "{}")
for k, val in extra.items():
    if val not in (None, ""):
        cfg[k] = val
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("capabilities = " + ", ".join(caps))
if extra:
    print("réglages posés : " + ", ".join("%s=%s" % (k, v) for k, v in extra.items() if v not in (None, "")))
PYEOF
  # ★ Redémarrage DÉTACHÉ, et non inline. En rattrapage, ce script est exécuté PAR l'agent (le
  # contrôleur le pipe sur /v1/host/exec) : le redémarrer ici tue le processus qui sert la requête
  # HTTP en cours. Le contrôleur reçoit alors « Remote end closed connection without response »,
  # conclut « ÉCHEC, rc=255 » et affiche « capacité non utilisable »… alors que tout a réussi, et
  # que la capacité est bien posée. Vécu en recette le 2026-08-21 (ajout de « media » sur
  # r620-3-test : config.json et /srv/mxl-media corrects, verdict faux). On rend donc la main
  # d'abord, l'agent redémarre 2 s plus tard.
  if setsid nohup bash -c 'sleep 2; systemctl restart bobi-node-agent' >/dev/null 2>&1 </dev/null & then
    ok "agent : redémarrage programmé dans 2 s (capacités rechargées ensuite)"
  else
    warn "agent non redémarré — « systemctl restart bobi-node-agent » à la main, sinon /v1/capabilities annonce encore l'ancienne liste."
  fi
  echo
  if [ "$_fail" = 0 ]; then
    ok "Capacité(s) « $ADD_CAPS » ajoutée(s)."
  else
    warn "Capacité(s) « $ADD_CAPS » DÉCLARÉE(S) mais le provisioning hôte a échoué (voir ci-dessus) — ne pas considérer la capacité comme utilisable."
  fi
  [ "$_reboot" = 0 ] || warn "REBOOT du nœud requis pour que cette/ces capacité(s) soient effectives."
  # Le code de sortie PORTE le résultat du provisioning : l'appelant (UI) ne doit jamais afficher
  # « ✓ fait » sur un stack absent.
  exit "$_fail"
fi

[ -f "$SCRIPT_DIR/agent.py" ] || die "agent.py introuvable à côté de ce script ($SCRIPT_DIR)."
log "Capacités demandées : ${CAPS}"

USE_MACVLAN=0
has_cap compute && USE_MACVLAN=1; has_cap media && USE_MACVLAN=1; has_cap webrtc && USE_MACVLAN=1

# Images par défaut dérivées des capacités (si --images non fourni).
# Tags d'images ATTENDUS. La source de vérité, ce sont les `meta.json` des runtimes, qui voyagent
# avec la charge utile ($SCRIPT_DIR/.. = l'archive extraite) — pas une valeur codée ici. Les défauts
# en dur ne sont qu'un DERNIER recours (script lancé seul, hors archive) : ils vieillissent, et un
# tag périmé fait annoncer « image ABSENTE » sur un nœud sain, ou pire, avec --registry, TIRER pour
# de bon une version périmée en la croyant bonne. Vécu : « bobi-compute:0.2 » face à une flotte en
# 0.29 (recette du 2026-08-21). `--images` explicite l'emporte toujours sur tout ceci.
_tag_meta() {   # <chemin meta.json> → image_tag, ou vide
  [ -f "$1" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("image_tag") or "")
except Exception: print("")' "$1" 2>/dev/null || true
}
if [ -z "$IMAGES" ]; then
  _racine="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)"
  imgs=""
  for _duo in "compute:_compute_runtime:bobi-compute:0.2" "media:_media_runtime:bobi-media:0.1" \
              "webrtc:_webrtc_runtime:bobi-webrtc:0.1" "io2110:2110_io:bobi-mtl:latest"; do
    _cap="${_duo%%:*}"; _reste="${_duo#*:}"; _dir="${_reste%%:*}"; _defaut="${_reste#*:}"
    has_cap "$_cap" || continue
    _t="$(_tag_meta "${_racine:-/nonexistent}/plugins/$_dir/meta.json")"
    imgs="$imgs ${_t:-$_defaut}"
  done
  IMAGES="$(echo "$imgs" | xargs)"
fi
# Re-run sur un nœud DÉJÀ installé : sans --token, on REPREND le token existant. Sinon l'agent
# repart avec un secret neuf que le contrôleur ignore → le nœud tombe « down » sans raison visible,
# alors que relancer l'installeur est LE réflexe après un échec (vécu en recette).
if [ -z "$TOKEN" ] && [ -f "$CONF_DIR/config.json" ]; then
  TOKEN="$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("token") or "")
except Exception: print("")' "$CONF_DIR/config.json" 2>/dev/null || true)"
  if [ -n "$TOKEN" ]; then
    warn "install déjà présente : token de l'agent CONSERVÉ (le contrôleur garde ce nœud). Passer --token pour en imposer un autre."
  fi
fi
[ -n "$TOKEN" ] || TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"

# ─── 1. Dépendances de base ─────────────────────────────────────────────────
log "Paquets de base (curl, ethtool, ca-certificates)…"
[ ${#APT_OPTS[@]} -gt 0 ] && log "bundle hors-ligne détecté ($_vendor_debs) — apt en dépôt local"
export DEBIAN_FRONTEND=noninteractive
# `set -e` ferait sortir le script SANS UN MOT si les dépôts sont injoignables (nœud sans route,
# proxy, DNS, miroir HS) : l'installeur « s'arrête », et le testeur n'a aucune cause à rapporter.
apt-get "${APT_OPTS[@]}" update -qq \
  || die "apt-get update a échoué — dépôts apt injoignables (réseau/DNS/proxy ?) ou sources invalides. Rien n'a été installé ; corriger l'accès aux dépôts (ou fournir un bundle hors-ligne) puis relancer."
apt-get "${APT_OPTS[@]}" install -y -qq --no-install-recommends python3 curl ethtool ca-certificates >/dev/null
ok "base OK"

# ─── 1b. Journal systemd DURABLE (journaux de conteneurs) ────────────────────
# Les conteneurs tournent avec le pilote de log `journald` : leur journal appartient à l'HÔTE, donc
# il survit à la destruction du conteneur (le moteur 2110 tourne en `--rm` et est recréé à chaque
# redéploiement) et au reboot du nœud. Deux conditions, posées ici et JAMAIS présumées :
#   1. stockage PERSISTANT (/var/log/journal) — sinon tout part au reboot ;
#   2. limites EXPLICITES. Piège majeur : au-delà de `RateLimitBurst` par `RateLimitIntervalSec`,
#      journald jette les messages SANS RIEN DIRE (défaut amont 10000/30 s, et 1000 avant
#      systemd 240 — même « le défaut » n'est pas stable). Un conteneur bavard perdrait
#      silencieusement les lignes qui expliquent sa panne. On DÉSACTIVE la limitation et on borne
#      par la TAILLE (SystemMaxUse) : la perte devient une rotation du plus ancien, bornée et
#      observable (la route /api/containers/<vmid>/logs publie la plus ancienne entrée dispo).
# Doit rester IDENTIQUE à app/journal.py:JOURNALD_DROPIN (même fichier, même contenu, idempotent).
log "Journal systemd persistant + limites explicites (journaux de conteneurs durables)…"
mkdir -p /var/log/journal /etc/systemd/journald.conf.d
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
cat > /etc/systemd/journald.conf.d/10-bobi.conf <<'JOURNALEOF'
# BOBI — journal systemd DURABLE pour les conteneurs (pilote docker `journald`).
# Genere par node_agent/install-node.sh — miroir de app/journal.py (ensure_journal_durable).
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=4G
SystemKeepFree=4G
SystemMaxFileSize=256M
SystemMaxFiles=64
# Limitation de debit DESACTIVEE : au-dela du burst, journald jette les messages SANS RIEN DIRE.
RateLimitIntervalSec=0
RateLimitBurst=0
ForwardToSyslog=no
JOURNALEOF
systemctl restart systemd-journald >/dev/null 2>&1 \
  || warn "systemd-journald n'a pas redémarré — journal conteneur potentiellement non persistant"
ok "journal persistant ($(journalctl --disk-usage 2>/dev/null | tr -d '\n'))"

# ─── 2. Docker ──────────────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  ok "docker déjà présent ($(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,))"
else
  # Debian 13 (trixie) a scindé `docker.io` : client `docker` → paquet `docker-cli`, builder BuildKit
  # → `docker-buildx`, tous deux en Recommends seulement. Sans eux le daemon tourne mais `docker`
  # manque (preflight KO) et `docker build` échoue (« buildx component is missing »).
  #
  # ⚠ MAIS ce découpage est PROPRE À DEBIAN. `docker-cli` n'existe dans AUCUNE version d'Ubuntu
  # (vérifié sur packages.ubuntu.com le 2026-09-02) : là-bas le client est dans `docker.io`. La
  # liste en dur faisait donc échouer `apt-get install` sur « Unable to locate package docker-cli »,
  # et le `die` arrêtait net l'installation du nœud — sur TOUTE Ubuntu, alors que le README,
  # INSTALL.md et le site public annonçaient « Debian/Ubuntu ». Un testeur y a laissé sa journée.
  # Ces annonces disent « Debian 13 » depuis le 2026-09-02 : la cible est UNE distribution, et
  # celle-là. Ce garde-fou reste néanmoins, parce qu'une liste de paquets en dur se périme aussi
  # d'une version de Debian à l'autre — c'est exactement ce qui a créé le problème en trixie.
  #
  # On n'interroge donc PAS la distribution (`ID`/`ID_LIKE` se trompe sur les dérivées) : on
  # demande à apt ce qu'il connaît, et on n'installe que ça. `docker.io` reste obligatoire ;
  # le reste est un complément quand il existe.
  _dk_pkgs="docker.io"
  for _p in docker-cli docker-buildx; do
    # `grep "^Package: <nom>$"` plutôt que le seul code de retour : sur un paquet purement
    # VIRTUEL, `apt-cache show` sort 0 en n'affichant qu'une note — on croirait le paquet réel.
    if apt-cache show "$_p" 2>/dev/null | grep -q "^Package: $_p\$"; then _dk_pkgs="$_dk_pkgs $_p"; fi
  done
  log "Installation de Docker ($_dk_pkgs)…"
  # shellcheck disable=SC2086  (word-splitting voulu : liste de paquets)
  apt-get "${APT_OPTS[@]}" install -y -qq $_dk_pkgs >/dev/null || die "échec install Docker ($_dk_pkgs)"
  systemctl enable --now docker
  # Le client DOIT être là : sur une distribution qui n'a ni `docker-cli` ni client dans
  # `docker.io`, tout le reste du script échouerait plus loin, avec un message moins clair.
  command -v docker >/dev/null 2>&1 \
    || die "Docker installé mais la commande « docker » est absente — paquet client introuvable pour cette distribution ($_dk_pkgs)."
  ok "docker installé ($_dk_pkgs)"
fi

# ─── 2a bis. HORLOGE : mettre le nœud sur la grille TAI ───────────────────────
# Le bus MXL indexe ses grains sur le TEMPS : index = TAI / durée du grain. Un nœud qui n'est pas
# sur la même grille que ses pairs ne peut ni aligner ses flux, ni se les répliquer.
#
# Le piège, et il est silencieux : libmxl ne lit QUE `CLOCK_TAI`, et sous Linux
#     CLOCK_TAI = CLOCK_REALTIME + tai_offset (noyau)
# Or NI systemd-timesyncd NI chrony ne posent `tai_offset` d'eux-mêmes. Une Debian fraîche a donc
# une heure civile juste et un `tai_offset` à 0 : CLOCK_TAI vaut l'UTC, soit 37 s d'écart avec la
# grille. Rien ne le signale — les flux du nœud paraissent seulement « périmés » chez leurs
# consommateurs, et la réplication RDMA depuis ce nœud ne transporte rien. Vécu : une journée de
# diagnostic à chercher un problème de réseau. Cf. l'article d'aide « Horloges du cluster (TAI) ».
#
# `leapseclist` fait poser et MAINTENIR `tai_offset` par chrony depuis la table officielle des
# secondes intercalaires (fournie par tzdata) — y compris aux futures secondes intercalaires.
# (`leapsectz right/UTC` ferait la même chose mais exigerait le paquet `tzdata-legacy`.)
#
# ⚠ PAS sur un nœud io2110 : son horloge système porte du TAI (et non de l'UTC), posée par le
# client PTP interne de libmtl. Y ajouter chrony ferait battre DEUX disciplines sur la même
# horloge, et ramènerait REALTIME vers l'UTC — soit 37 s d'erreur pour le moteur. La grille d'un
# nœud 2110 relève du moteur, pas de NTP.
if has_cap io2110; then
  log "horloge : nœud io2110 — l'horloge système est du ressort du moteur (client PTP libmtl), chrony NON installé"
else
  log "horloge : mise sur la grille TAI (chrony + leapseclist)…"
  if apt-get "${APT_OPTS[@]}" install -y -qq chrony >/dev/null 2>&1; then
    mkdir -p /etc/chrony/conf.d
    cat > /etc/chrony/conf.d/bobi-tai.conf <<'EOFCLK'
# Bobi.Studio — offset TAI du noyau.
# La grille média MXL est indexée sur CLOCK_TAI ; sans table de secondes intercalaires le noyau
# garde tai_offset=0 et CLOCK_TAI vaut l'UTC, soit 37 s d'écart avec le reste du cluster.
leapseclist /usr/share/zoneinfo/leap-seconds.list
EOFCLK
    systemctl disable --now systemd-timesyncd >/dev/null 2>&1 || true
    systemctl restart chrony >/dev/null 2>&1 || true
    # Contrôle : on VÉRIFIE que l'offset est effectivement posé plutôt que de supposer que
    # l'installation a suffi — c'est précisément ce qui manquait pour que le défaut se voie.
    # ★ MAIS chrony ne pose `tai_offset` qu'APRÈS sa première synchro : mesurer dans la seconde qui
    # suit son démarrage donne 0 et faisait crier « le nœud n'est PAS sur la grille TAI » sur un
    # nœud parfaitement sain (vécu sur r620-3, 2026-08-19 : 37 deux minutes plus tard). On laisse
    # donc le temps à la synchro, et à défaut on dit « pas encore posé » — pas une fausse alarme.
    _tai_offset() { python3 -c 'import ctypes
class T(ctypes.Structure): _fields_=[("m",ctypes.c_uint)]+[(n,ctypes.c_long) for n in "o f x e".split()]+[("s",ctypes.c_int)]+[(n,ctypes.c_long) for n in "c p t ts tus tk pf j".split()]+[("sh",ctypes.c_int)]+[(n,ctypes.c_long) for n in "st jc cc ec sc".split()]+[("tai",ctypes.c_int)]+[("pad",ctypes.c_int*11)]
t=T(); ctypes.CDLL("libc.so.6").adjtimex(ctypes.byref(t)); print(t.tai)' 2>/dev/null || echo '?'; }
    chronyc waitsync 6 0 0 5 >/dev/null 2>&1 || true      # ≤30 s d'attente de la 1ʳᵉ synchro
    _tai="$(_tai_offset)"
    _n=0
    while [ "$_tai" != "37" ] && [ "$_n" -lt 10 ]; do sleep 2; _tai="$(_tai_offset)"; _n=$((_n + 1)); done
    if [ "$_tai" = "37" ]; then
      ok "horloge : sur la grille (tai_offset=${_tai}, chrony + leapseclist)"
    else
      warn "horloge : tai_offset=${_tai} — PAS ENCORE posé par chrony (il l'applique après sa première synchro). Revérifier dans quelques minutes (Réglages → Réseau → Horloges) ; s'il reste à 0, vérifier l'accès NTP et /usr/share/zoneinfo/leap-seconds.list (paquet tzdata)."
    fi
  else
    warn "horloge : installation de chrony impossible — le nœud restera à 37 s de la grille TAI. Corriger via Réglages → Réseau → Horloges."
  fi
fi

# ─── 2b. Capacité GPU (NVIDIA : pilote Debian + container-toolkit) ────────────
if has_cap gpu; then provision_gpu; fi

# ─── 3. Capacité io2110 (E810 / hugepages / PTP) ────────────────────────────
if has_cap io2110; then provision_io2110; fi

# ─── 4. Réseau macvlan ───────────────────────────────────────────────────────
if [ "$USE_MACVLAN" = "1" ]; then
  if docker network inspect "$MACVLAN_NAME" >/dev/null 2>&1; then
    ok "réseau macvlan '$MACVLAN_NAME' déjà présent"
  elif [ -z "$MACVLAN_PARENT" ] || [ -z "$MACVLAN_SUBNET" ]; then
    # DIFFÉRÉ (recommandé) : pas de parent/subnet fournis à l'install → on ne fige PAS le réseau
    # containers ici. L'orchestrateur l'assignera après l'enrôlement (choix de la NIC parent dans
    # l'inventaire remonté par l'agent → POST /v1/host/networks/ensure). Provisioning non bloqué.
    warn "réseau containers non fourni à l'install — différé : à configurer depuis l'orchestrateur (Déploiement → Nœuds) une fois l'agent enrôlé."
  else
    # Si le parent est une sous-interface VLAN (<base>.<vid>) absente, la créer + la persister.
    # (Remplace les bridges Proxmox vmbrX.Y : sur Debian bare l'orchestrateur ne les crée pas.)
    if echo "$MACVLAN_PARENT" | grep -qE '^[^.]+\.[0-9]+$' && ! ip link show "$MACVLAN_PARENT" >/dev/null 2>&1; then
      vbase="${MACVLAN_PARENT%.*}"; vid="${MACVLAN_PARENT##*.}"
      ip link show "$vbase" >/dev/null 2>&1 || die "base VLAN '$vbase' introuvable (pour $MACVLAN_PARENT)."
      log "Création de la sous-interface VLAN $MACVLAN_PARENT (base $vbase, id $vid)…"
      ip link add link "$vbase" name "$MACVLAN_PARENT" type vlan id "$vid" || die "échec création VLAN $MACVLAN_PARENT"
      ip link set "$vbase" up; ip link set "$MACVLAN_PARENT" up
      # Persistance indépendante du gestionnaire réseau de l'hôte : unité oneshot au boot.
      cat > /etc/systemd/system/bobi-vlan-${MACVLAN_PARENT}.service <<VLANUNIT
[Unit]
Description=Sous-interface VLAN $MACVLAN_PARENT (parent macvlan bobi) — base $vbase id $vid
After=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '/sbin/ip link show $MACVLAN_PARENT >/dev/null 2>&1 || /sbin/ip link add link $vbase name $MACVLAN_PARENT type vlan id $vid; /sbin/ip link set $vbase up; /sbin/ip link set $MACVLAN_PARENT up'
[Install]
WantedBy=multi-user.target
VLANUNIT
      systemctl daemon-reload
      systemctl enable "bobi-vlan-${MACVLAN_PARENT}.service" >/dev/null 2>&1 || true
      ok "VLAN $MACVLAN_PARENT créé + persisté (unit bobi-vlan-$MACVLAN_PARENT)"
    fi
    [ -n "$MACVLAN_RANGE" ] || warn "--macvlan-range non fourni : l'IPAM macvlan peut entrer en collision avec des IP fixes du LAN (recommandé de borner la plage)."
    log "Création du réseau macvlan '$MACVLAN_NAME' (parent $MACVLAN_PARENT, $MACVLAN_SUBNET)…"
    args=(network create -d macvlan -o "parent=$MACVLAN_PARENT" --subnet "$MACVLAN_SUBNET")
    [ -n "$MACVLAN_GW" ]    && args+=(--gateway "$MACVLAN_GW")
    [ -n "$MACVLAN_RANGE" ] && args+=(--ip-range "$MACVLAN_RANGE")
    args+=("$MACVLAN_NAME")
    docker "${args[@]}" >/dev/null || die "échec création macvlan"
    ok "macvlan créé"
  fi
fi

# ─── 4b. Capacités macvlan : vérification + racine média ─────────────────────
# Après la création (ou le report) du réseau ci-dessus : on CONSTATE l'état réel plutôt que de le
# supposer, et on pose la racine média. Mêmes fonctions qu'en rattrapage (--add-caps) — un seul
# chemin de code pour les deux usages.
if has_cap compute; then provision_compute; fi
if has_cap media;   then provision_media;   fi
if has_cap webrtc;  then provision_webrtc;  fi

# ─── 5a. Images embarquées (bundle offline) : docker load ──────────────────────
# Si le paquet embarque vendor/images/ (build --images), on charge les tars des capacités
# demandées dans le Docker local (aucun registre requis). Les tags du manifeste font FOI et
# remplacent les défauts codés en dur (sauf --images explicite) → pas de décalage de version.
_vendor_images="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)/vendor/images"
if [ -f "$_vendor_images/manifest.json" ]; then
  log "Bundle d'images détecté — chargement (docker load)…"
  _bundled_tags=""
  while IFS=$'\t' read -r _cap _file _tag; do
    [ -n "$_file" ] || continue
    if ! has_cap "$_cap"; then log "  $_tag ($_cap) ignorée — capacité non demandée"; continue; fi
    if [ ! -f "$_vendor_images/$_file" ]; then warn "  fichier image absent : $_file"; continue; fi
    if docker load -i "$_vendor_images/$_file" >/dev/null 2>&1; then
      ok "  image $_tag chargée"
      _bundled_tags="$_bundled_tags $_tag"
    else
      warn "  docker load $_file échoué"
    fi
  done < <(python3 -c 'import json,sys
for e in json.load(open(sys.argv[1])):
    print("%s\t%s\t%s" % (e.get("capability",""), e.get("file",""), e.get("tag","")))
' "$_vendor_images/manifest.json" 2>/dev/null)
  # Les tags embarqués font foi (remplacent les défauts), sauf si --images a été passé explicitement.
  if [ -n "$_bundled_tags" ] && [ "$IMAGES_EXPLICIT" = 0 ]; then
    IMAGES="$(echo "$_bundled_tags" | xargs)"
  fi
fi

# ─── 5. Images runtime ────────────────────────────────────────────────────────
log "Vérification des images : $IMAGES"
for img in $IMAGES; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    ok "image $img présente"
  elif [ -n "$REGISTRY" ]; then
    log "pull $REGISTRY/$img…"
    docker pull "$REGISTRY/$img" && docker tag "$REGISTRY/$img" "$img" && ok "image $img tirée" \
      || warn "pull $img échoué"
  else
    warn "image $img ABSENTE (fournir --registry, ou la charger : docker load / build sur le nœud)"
  fi
done

# ─── 6. Agent (Python + systemd) ──────────────────────────────────────────────
log "Installation de bobi-node-agent…"
mkdir -p "$AGENT_DIR" "$CONF_DIR"
# Dossier du matériel mTLS du plan de contrôle (node.key/node.crt/ca.crt). 0700 : la clé privée
# de l'agent y vit (0600). Vide au 1er boot → l'agent sert en HTTP clair jusqu'à ce que le
# contrôleur pousse un cert signé (enrôlement ou /v1/tls/*), puis l'agent repart en HTTPS/mTLS.
# Chemin surchargeable via config.json:tls_dir ou env BOBI_NODE_AGENT_TLS_DIR.
TLS_DIR="$CONF_DIR/tls"

# ─── Garde-fou : matériel mTLS d'un enrôlement PRÉCÉDENT ────────────────────────────────────────
# L'install NE VIDE PAS le TLS_DIR (il vit dans /etc, hors du dossier applicatif). Si un cert d'un
# enrôlement antérieur y traîne, l'agent redémarre EN HTTPS VERROUILLÉ SUR L'ANCIENNE CA et ne
# repasse jamais par le bootstrap HTTP+token → tout ré-enrôlement vers un AUTRE contrôleur échoue
# EN SILENCE (le contrôleur ping en HTTP clair, l'agent TLS reset). Piège vécu : une réinstallation
# complète « ne prenait pas » car le cert survivait dans /etc/bobi-node-agent/tls.
_existing_crt="$TLS_DIR/node.crt"; _existing_key="$TLS_DIR/node.key"; _existing_ca="$TLS_DIR/ca.crt"
if [ -s "$_existing_crt" ] || [ -s "$_existing_key" ] || [ -s "$_existing_ca" ]; then
  warn "Un certificat mTLS d'un enrôlement PRÉCÉDENT est présent dans $TLS_DIR :"
  if [ -s "$_existing_crt" ] && command -v openssl >/dev/null 2>&1; then
    _sub="$(openssl x509 -in "$_existing_crt" -noout -subject 2>/dev/null | sed 's/^subject=//')"
    _iss="$(openssl x509 -in "$_existing_crt" -noout -issuer  2>/dev/null | sed 's/^issuer=//')"
    echo "    nœud     : ${_sub:-?}"
    echo "    signé par: ${_iss:-?}"
  fi
  echo "    → tant qu'il est là, l'agent redémarre sur CETTE CA et NE POURRA PAS se ré-enrôler ailleurs."
  _do_reset=""
  if [ -n "$KEEP_TLS" ]; then
    _do_reset=""; warn "--keep-tls : cert conservé (le ré-enrôlement vers un autre contrôleur restera bloqué)."
  elif [ -n "$RESET_TLS" ]; then
    _do_reset=1
  elif [ -t 0 ]; then
    printf "%b" "${c_y}?${c_0} Supprimer ce certificat pour permettre un enrôlement propre ? [O/n] "
    read -r _ans || _ans=""
    case "${_ans:-O}" in [nN]*) _do_reset="";; *) _do_reset=1;; esac
  else
    # Non-interactif SANS drapeau : on NE touche à rien (ne pas détruire un cert valide sur un
    # re-run idempotent), mais on le DIT fort — pas d'échec silencieux.
    warn "Non-interactif : cert CONSERVÉ. Relance avec --reset-tls pour l'effacer, ou à la main :"
    echo "    systemctl stop bobi-node-agent; rm -f $TLS_DIR/node.key $TLS_DIR/node.crt $TLS_DIR/ca.crt; systemctl start bobi-node-agent"
  fi
  if [ -n "$_do_reset" ]; then
    rm -f "$_existing_key" "$_existing_crt" "$_existing_ca"
    ok "Ancien matériel mTLS supprimé → l'agent repartira en HTTP clair (bootstrap), prêt à s'enrôler."
  fi
fi

mkdir -p "$TLS_DIR"; chmod 700 "$TLS_DIR"
install -m 0644 "$SCRIPT_DIR/agent.py" "$AGENT_DIR/agent.py"
# config.json (capabilities en tableau JSON, images en tableau JSON)
caps_json="$(echo "$CAPS"  | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd, -)"
imgs_json="$(echo "$IMAGES"| tr ' ' '\n' | grep -v '^$' | sed 's/.*/"&"/' | paste -sd, -)"
cat > "$CONF_DIR/config.json" <<JSON
{
  "token": "$TOKEN",
  "port": $PORT,
  "controller_url": "$CONTROLLER_URL",
  "capabilities": [${caps_json}],
  "mxl_mount": "$MXL_MOUNT",
  "macvlan_network": "$MACVLAN_NAME",
  "media_mount": "$MEDIA_MOUNT",
  "mtl_iface": "$MTL_IFACE",
  "lcores": "$LCORES",
  "tls_dir": "$TLS_DIR",
  "images": [${imgs_json}]
}
JSON
chmod 0600 "$CONF_DIR/config.json"
install -m 0644 "$SCRIPT_DIR/bobi-node-agent.service" /etc/systemd/system/bobi-node-agent.service
# mTLS : la bascule HTTP→HTTPS se fait par redémarrage du process (l'agent os._exit(0) après avoir
# écrit son cert via /v1/tls/install). Il FAUT donc Restart=always (un exit 0 ne relance pas avec
# on-failure). On force la directive dans l'unité installée (idempotent).
if grep -q '^Restart=' /etc/systemd/system/bobi-node-agent.service; then
  sed -i 's/^Restart=.*/Restart=always/' /etc/systemd/system/bobi-node-agent.service
else
  sed -i '/^\[Service\]/a Restart=always' /etc/systemd/system/bobi-node-agent.service
fi
systemctl daemon-reload
systemctl enable bobi-node-agent >/dev/null 2>&1 || true
if [ "$START" = "1" ]; then
  systemctl restart bobi-node-agent
  sleep 2
  state="$(systemctl is-active bobi-node-agent || true)"
  [ "$state" = "active" ] && ok "agent démarré (:$PORT)" || warn "agent non actif ($state) — journalctl -u bobi-node-agent"
else
  ok "agent installé (non démarré : --no-start)"
fi

# ─── 7. Résumé / enregistrement contrôleur ─────────────────────────────────────
# Adresse annoncée : celle par laquelle ce nœud ATTEINT le contrôleur (ou la route par défaut),
# et non le premier champ de `hostname -I` — sur un nœud multi-homé (2110/RDMA/média) c'est
# souvent une adresse de plan média, injoignable depuis l'orchestrateur.
_ctrl_host="$(echo "${CONTROLLER_URL:-}" | sed -E 's#^[a-z]+://##; s#[:/].*##')"
HOSTIP="$(ip -4 route get "${_ctrl_host:-1.1.1.1}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
[ -n "$HOSTIP" ] || HOSTIP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
[ -n "$HOSTIP" ] || HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
ok "Nœud prêt."
echo "    capacités   : $CAPS"
echo "    agent_url   : http://${HOSTIP:-$(hostname)}:$PORT"
echo "    token       : $TOKEN"
echo
echo "  → Enregistrer ce nœud dans le contrôleur (Réglages → Déploiement → Nœuds), ou côté serveur :"
echo "      node_driver.register(\"${HOSTIP:-<host>}\", $PORT, \"$TOKEN\")"
