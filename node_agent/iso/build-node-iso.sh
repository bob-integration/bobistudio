***REMOVED***!/usr/bin/env bash
***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED***
***REMOVED*** build-node-iso.sh — fabrique une ISO/clé USB Debian préseedée qui installe un nœud Bobi.Studio
***REMOVED*** sans surveillance, puis le provisionne au 1er boot (bobi-node-bootstrap → install-node.sh).
***REMOVED***
***REMOVED*** Deux modes de config (écrits dans enroll.conf embarqué) :
***REMOVED***   ZÉRO-TOUCH : --controller-url URL --enroll-token TOK   (le nœud tire son profil du contrôleur)
***REMOVED***   ANSWER-FILE: --with CAPS [--macvlan-subnet ... --macvlan-gateway ... --macvlan-vlan ...
***REMOVED***                --token TOK --ptp-domain N --hugepages N --registry HOST]   (params en dur)
***REMOVED***
***REMOVED*** Réseau de CONTRÔLE (management) : DHCP par défaut. Pour une IP statique (recommandé en broadcast,
***REMOVED*** où l'on fait peu de DHCP), passer --ip/--netmask/--gateway [--nameservers] : le preseed est alors
***REMOVED*** basculé en config statique (l'autoconf DHCP est désactivée). N'affecte QUE le plan de contrôle ;
***REMOVED*** le réseau containers (macvlan) est assigné après l'enrôlement, depuis l'orchestrateur.
***REMOVED***
***REMOVED*** Multi-NIC : --mgmt-mac aa:bb:cc:dd:ee:ff épingle d-i sur la carte de gestion par son MAC (sinon d-i
***REMOVED*** prend la 1ʳᵉ carte qui répond, non déterministe). À utiliser quand le nœud a plusieurs cartes.
***REMOVED***
***REMOVED*** Partitionnement : --part-disk <by-id> cible un disque/volume précis par son identifiant by-id
***REMOVED*** (ex. wwn-0x600508b1… donné par l'inventaire iLO), au lieu de la recette /dev/sda… qui se bloque sur
***REMOVED*** les contrôleurs RAID HPe / disques multiples.
***REMOVED***
***REMOVED*** Usage :
***REMOVED***   sudo ./build-node-iso.sh --src-iso debian-13-netinst.iso --out bobi-node.iso \
***REMOVED***        --controller-url http://x.x.x.x:5000 --enroll-token <TOK> [--ssh-key ~/.ssh/id.pub] \
***REMOVED***        [--ip x.x.x.x --netmask 255.255.255.0 --gateway x.x.x.x --nameservers x.x.x.x]
***REMOVED***
***REMOVED*** Prérequis hôte de build : xorriso. (La création de clé : `cp bobi-node.iso /dev/sdX` ou dd.)
set -euo pipefail

SRC_ISO=""; OUT="bobi-node.iso"; SSH_KEY=""
CONTROLLER_URL=""; ENROLL_TOKEN=""
MGMT_IP=""; MGMT_NETMASK=""; MGMT_GATEWAY=""; MGMT_DNS=""; MGMT_MAC=""; PART_DISK=""
CAPS=""; MACVLAN_SUBNET=""; MACVLAN_GATEWAY=""; MACVLAN_VLAN=""; MACVLAN_NAME="bobimacvlan"
TOKEN=""; PTP_DOMAIN=""; HUGEPAGES=""; LCORES=""; REGISTRY=""
HERE="$(cd "$(dirname "$0")" && pwd)"        ***REMOVED*** node_agent/iso
NODE_AGENT="$(cd "$HERE/.." && pwd)"          ***REMOVED*** node_agent
PRESEED="$HERE/preseed.cfg"

die(){ echo "✗ $*" >&2; exit 1; }
while [ $***REMOVED*** -gt 0 ]; do case "$1" in
  --src-iso) SRC_ISO="$2"; shift 2;;
  --out) OUT="$2"; shift 2;;
  --ssh-key) SSH_KEY="$2"; shift 2;;
  --controller-url) CONTROLLER_URL="$2"; shift 2;;
  --enroll-token) ENROLL_TOKEN="$2"; shift 2;;
  --ip) MGMT_IP="$2"; shift 2;;
  --netmask) MGMT_NETMASK="$2"; shift 2;;
  --gateway) MGMT_GATEWAY="$2"; shift 2;;
  --nameservers) MGMT_DNS="$2"; shift 2;;
  --mgmt-mac) MGMT_MAC="$2"; shift 2;;
  --part-disk) PART_DISK="$2"; shift 2;;
  --with) CAPS="$2"; shift 2;;
  --macvlan-subnet) MACVLAN_SUBNET="$2"; shift 2;;
  --macvlan-gateway) MACVLAN_GATEWAY="$2"; shift 2;;
  --macvlan-vlan) MACVLAN_VLAN="$2"; shift 2;;
  --macvlan-name) MACVLAN_NAME="$2"; shift 2;;
  --token) TOKEN="$2"; shift 2;;
  --ptp-domain) PTP_DOMAIN="$2"; shift 2;;
  --hugepages) HUGEPAGES="$2"; shift 2;;
  --lcores) LCORES="$2"; shift 2;;
  --registry) REGISTRY="$2"; shift 2;;
  *) die "option inconnue : $1";;
esac; done

[ -f "$SRC_ISO" ] || die "--src-iso requis (ISO netinst Debian)"
command -v xorriso >/dev/null || die "xorriso manquant (apt install xorriso)"
[ -f "$PRESEED" ] || die "preseed.cfg introuvable ($PRESEED)"
if [ -z "$CONTROLLER_URL$ENROLL_TOKEN" ] && [ -z "$CAPS" ]; then
  die "fournir soit --controller-url + --enroll-token (zéro-touch), soit --with (answer-file)"
fi
if [ -n "$MGMT_IP" ]; then
  [ -n "$MGMT_NETMASK" ] || die "--ip nécessite --netmask"
  [ -n "$MGMT_GATEWAY" ] || die "--ip nécessite --gateway"
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
PAY="$WORK/bobi-node-src"; mkdir -p "$PAY"

***REMOVED*** ── 0. Preseed : copie de travail, basculée en IP statique si --ip fourni ───────
PRESEED_USE="$WORK/preseed.cfg"
cp "$PRESEED" "$PRESEED_USE"
***REMOVED*** Mot de passe root : le preseed versionné ne porte qu'un MARQUEUR, jamais une valeur. On en tire un
***REMOVED*** au hasard par ISO construite, on le substitue ici, et on l'affiche une seule fois à la fin. Il ne
***REMOVED*** sert qu'à la console physique ou iLO : l'exploitation passe par la clé SSH injectée plus bas.
***REMOVED*** Alphabet sans caractères ambigus (ni O/0, ni l/1/I) — il se tape à la main, souvent en QWERTY et
***REMOVED*** en pleine panne. Même alphabet dans app/pxe.py (PRESEED_MDP_ALPHABET).
MARQUEUR_MDP="@@ROOT_PASSWORD@@"
grep -q "$MARQUEUR_MDP" "$PRESEED_USE" || die \
  "preseed.cfg ne contient pas $MARQUEUR_MDP — mot de passe root non maîtrisé, construction refusée"
ROOT_PASSWORD="$(LC_ALL=C tr -dc 'a-km-zA-HJ-NP-Z2-9' < /dev/urandom | head -c 20)"
[ ${***REMOVED***ROOT_PASSWORD} -eq 20 ] || die "tirage du mot de passe root incomplet"
sed -i "s***REMOVED***$MARQUEUR_MDP***REMOVED***$ROOT_PASSWORD***REMOVED***g" "$PRESEED_USE"
***REMOVED*** Choix d'interface déterministe (multi-NIC) : épingle d-i sur le MAC du port de gestion si fourni
***REMOVED*** (lu localement avant la config réseau, donc la ligne preseed suffit — pas besoin de toucher KARGS).
if [ -n "$MGMT_MAC" ]; then
  sed -i -E "s***REMOVED***^(d-i netcfg/choose_interface select ).****REMOVED***\1$MGMT_MAC***REMOVED***" "$PRESEED_USE"
fi
***REMOVED*** Cible de partitionnement choisie via l'inventaire iLO : ciblage by-id déterministe (WWN du volume),
***REMOVED*** au lieu de la liste /dev/sda… historique qui se bloque sur RAID HPe / disques multiples.
if [ -n "$PART_DISK" ]; then
  sed -i -E "s***REMOVED***^(d-i partman-auto/disk string ).****REMOVED***\1/dev/disk/by-id/$PART_DISK***REMOVED***" "$PRESEED_USE"
fi
if [ -n "$MGMT_IP" ]; then
  ***REMOVED*** Désactive l'autoconf DHCP et injecte l'adressage statique du plan de CONTRÔLE juste après le
  ***REMOVED*** choix d'interface (preseed = ordre indifférent, mais on garde le bloc réseau groupé/lisible).
  awk -v ip="$MGMT_IP" -v mask="$MGMT_NETMASK" -v gw="$MGMT_GATEWAY" \
      -v dns="${MGMT_DNS:-$MGMT_GATEWAY}" '
    { print }
    /^d-i netcfg\/choose_interface/ {
      print "d-i netcfg/disable_autoconfig boolean true"
      print "d-i netcfg/get_ipaddress string " ip
      print "d-i netcfg/get_netmask string " mask
      print "d-i netcfg/get_gateway string " gw
      print "d-i netcfg/get_nameservers string " dns
      print "d-i netcfg/confirm_static boolean true"
    }' "$PRESEED_USE" > "$PRESEED_USE.tmp" && mv "$PRESEED_USE.tmp" "$PRESEED_USE"
fi

***REMOVED*** ── 1. Payload : node_agent + scripts first-boot ────────────────────────────────
for f in install-node.sh agent.py bobi-node-agent.service node-bootstrap.sh bobi-node-bootstrap.service; do
  [ -f "$NODE_AGENT/$f" ] || die "payload manquant : node_agent/$f"
  cp "$NODE_AGENT/$f" "$PAY/$f"
done
chmod +x "$PAY/install-node.sh" "$PAY/node-bootstrap.sh"
[ -n "$SSH_KEY" ] && cp "$SSH_KEY" "$PAY/authorized_keys"

***REMOVED*** ── 2. enroll.conf (config consommée par node-bootstrap.sh) ─────────────────────
{
  echo "***REMOVED*** Généré par build-node-iso.sh — $(date -Is)"
  if [ -n "$CONTROLLER_URL" ]; then echo "CONTROLLER_URL=\"$CONTROLLER_URL\""; fi
  if [ -n "$ENROLL_TOKEN" ];   then echo "ENROLL_TOKEN=\"$ENROLL_TOKEN\""; fi
  ***REMOVED*** Answer-file (utilisé si pas de mode zéro-touch, ou comme défauts) :
  [ -n "$CAPS" ]            && echo "CAPS=\"$CAPS\""
  [ -n "$MACVLAN_SUBNET" ]  && echo "MACVLAN_SUBNET=\"$MACVLAN_SUBNET\""
  [ -n "$MACVLAN_GATEWAY" ] && echo "MACVLAN_GATEWAY=\"$MACVLAN_GATEWAY\""
  [ -n "$MACVLAN_VLAN" ]    && echo "MACVLAN_VLAN=\"$MACVLAN_VLAN\""
  echo "MACVLAN_NAME=\"$MACVLAN_NAME\""
  [ -n "$TOKEN" ]           && echo "TOKEN=\"$TOKEN\""
  [ -n "$PTP_DOMAIN" ]      && echo "PTP_DOMAIN=\"$PTP_DOMAIN\""
  [ -n "$HUGEPAGES" ]       && echo "HUGEPAGES=\"$HUGEPAGES\""
  [ -n "$LCORES" ]          && echo "LCORES=\"$LCORES\""
  [ -n "$REGISTRY" ]        && echo "REGISTRY=\"$REGISTRY\""
} > "$PAY/enroll.conf"

***REMOVED*** ── 3. Boot configs : menu à 3 entrées (Auto / Semi-auto / Manuel), SEMI-AUTO par défaut ─────────
***REMOVED*** Auto = aucune question (priority=critical) ; Semi-auto = preseed complet SANS priority=critical →
***REMOVED*** automatique tant que ça passe, reprend la main au point qui coince ; Manuel = preseed sans partman.
COMMON="DEBIAN_FRONTEND=text ---"
AUTO_K="auto=true priority=critical preseed/file=/cdrom/preseed.cfg $COMMON"
SEMI_K="auto=true preseed/file=/cdrom/preseed.cfg $COMMON"
MAN_K="auto=true preseed/file=/cdrom/preseed-manual.cfg $COMMON"
***REMOVED*** preseed manuel embarqué = le même, bloc partman retiré (d-i pose les questions de partitionnement).
grep -v '^[[:space:]]*d-i partman' "$PRESEED_USE" > "$WORK/preseed-manual.cfg"

mkdir -p "$WORK/boot/grub" "$WORK/isolinux"
xorriso -osirrox on -indev "$SRC_ISO" -extract /boot/grub/grub.cfg "$WORK/boot/grub/grub.cfg" 2>/dev/null || true
xorriso -osirrox on -indev "$SRC_ISO" -extract /isolinux/txt.cfg   "$WORK/isolinux/txt.cfg"   2>/dev/null || true

MAPS=()
***REMOVED*** ── GRUB (UEFI — chemin des nœuds HPe Gen10 via iLO Virtual Media) : menu complet à 3 entrées ──
if [ -f "$WORK/boot/grub/grub.cfg" ]; then
  ***REMOVED*** Noyau : préférer l'installeur TEXTE (pas gtk) ; initrd dérivé du MÊME dossier (jamais dépareillé).
  GK=$(grep -oE '/[^ ]*vmlinuz' "$WORK/boot/grub/grub.cfg" | grep -v gtk | head -1)
  [ -n "$GK" ] || GK=$(grep -oE '/[^ ]*vmlinuz' "$WORK/boot/grub/grub.cfg" | head -1)
  GI=""; [ -n "$GK" ] && GI="$(dirname "$GK")/initrd.gz"
  if [ -n "$GK" ] && [ -n "$GI" ]; then
    cat > "$WORK/boot/grub/grub.cfg" <<EOF
set timeout=15
set default=1
menuentry "Bobi.Studio — installation AUTOMATIQUE (sans surveillance)" {
    linux $GK $AUTO_K
    initrd $GI
}
menuentry "Bobi.Studio — installation SEMI-AUTO (reprend la main si une étape coince)" {
    linux $GK $SEMI_K
    initrd $GI
}
menuentry "Bobi.Studio — installation MANUELLE (partitionnement à la main)" {
    linux $GK $MAN_K
    initrd $GI
}
EOF
  else
    ***REMOVED*** Repli si on ne détecte pas le noyau : défaut SEMI-AUTO sur l'entrée existante.
    sed -i -E "s***REMOVED***(^[[:space:]]*linux .*vmlinuz[^\n]*)***REMOVED***\1 $SEMI_K***REMOVED***" "$WORK/boot/grub/grub.cfg"
    sed -i -E "s/^set timeout=.*/set timeout=5/" "$WORK/boot/grub/grub.cfg"
  fi
  MAPS+=( -map "$WORK/boot/grub/grub.cfg" /boot/grub/grub.cfg )
fi
***REMOVED*** ── ISOLINUX (BIOS legacy) : défaut SEMI-AUTO (pas de menu 3-voies, l'iLO Gen10 amorce en UEFI) ──
if [ -f "$WORK/isolinux/txt.cfg" ]; then
  sed -i -E "s***REMOVED***(append[^\n]*vmlinuz[^\n]*)***REMOVED***\1 $SEMI_K***REMOVED***" "$WORK/isolinux/txt.cfg"
  MAPS+=( -map "$WORK/isolinux/txt.cfg" /isolinux/txt.cfg )
fi

***REMOVED*** ── 4. Repack : préserve l'amorçage, ajoute preseed + payload ───────────────────
echo "▶ Construction de $OUT …"
xorriso -indev "$SRC_ISO" -outdev "$OUT" \
  -boot_image any replay \
  -map "$PRESEED_USE" /preseed.cfg \
  -map "$WORK/preseed-manual.cfg" /preseed-manual.cfg \
  -map "$PAY" /bobi-node-src \
  "${MAPS[@]}"

echo "✓ ISO prête : $OUT"
echo "  Mode : $([ -n "$CONTROLLER_URL" ] && echo "zéro-touch ($CONTROLLER_URL)" || echo "answer-file (caps=$CAPS)")"
echo "  Contrôle : $([ -n "$MGMT_IP" ] && echo "IP statique $MGMT_IP/$MGMT_NETMASK gw $MGMT_GATEWAY" || echo "DHCP")"
echo "  Boot : menu Auto / Semi-auto (défaut) / Manuel — choisir dans la console au démarrage"
echo "  Clé USB : sudo cp $OUT /dev/sdX   (ou: dd if=$OUT of=/dev/sdX bs=4M status=progress oflag=sync)"
echo "  ⚠ Le preseed EFFACE le disque cible de la machine vierge."
echo
echo "  Mot de passe root de CETTE ISO : $ROOT_PASSWORD"
echo "  Noté nulle part ailleurs — il n'est ni versionné, ni enregistré. Relancer le script en"
echo "  produit un autre. Il ne sert qu'à la console physique ou iLO : l'exploitation passe par la"
echo "  clé SSH du contrôleur."
