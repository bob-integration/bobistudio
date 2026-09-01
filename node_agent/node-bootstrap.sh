#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# node-bootstrap.sh — provisioning au PREMIER boot d'un nœud installé par la clé USB préseedée.
# Lancé une seule fois par le service systemd `bobi-node-bootstrap` (oneshot), réseau up.
#
# Deux modes, selon /etc/bobi-node/enroll.conf :
#   1) ZÉRO-TOUCH (recommandé) : CONTROLLER_URL + ENROLL_TOKEN définis → on POST nos faits matériels
#      à <CONTROLLER_URL>/api/nodes/enroll, le contrôleur renvoie le profil (capacités, macvlan,
#      token agent, domaine PTP, registry) → on lance install-node.sh avec.
#   2) ANSWER-FILE (repli/déterministe) : les paramètres sont écrits en dur dans enroll.conf
#      (CAPS, MACVLAN_*, TOKEN, MTL_IFACE…) → on lance install-node.sh directement.
#
# Idempotent : se désactive (systemctl disable) après un provisioning réussi.
set -uo pipefail

CONF=/etc/bobi-node/enroll.conf
SRC=/opt/bobi-node-src                       # payload copié par le preseed (install-node.sh, agent.py…)
STATE=/var/lib/bobi-node/bootstrap.done
LOG=/var/log/bobi-node-bootstrap.log
exec > >(tee -a "$LOG") 2>&1

log(){ echo "[$(date -Is)] $*"; }
fail(){ log "ÉCHEC: $*"; exit 1; }

[ -f "$STATE" ] && { log "déjà provisionné ($STATE) — rien à faire."; exit 0; }
[ -r "$CONF" ] || fail "config absente : $CONF"
[ -x "$SRC/install-node.sh" ] || fail "payload absent : $SRC/install-node.sh"

# shellcheck disable=SC1090
. "$CONF"

# ─── Détection matérielle ──────────────────────────────────────────────────────
detect_ice_iface() {
  for i in $(ls /sys/class/net 2>/dev/null); do
    [ "$i" = "lo" ] && continue
    if [ "$(ethtool -i "$i" 2>/dev/null | sed -n 's/^driver: //p')" = "ice" ]; then echo "$i"; return; fi
  done
}
ICE_IFACE="$(detect_ice_iface || true)"
# Interface de gestion (route par défaut) — repli pour macvlan parent si pas d'E810.
MGMT_IFACE="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
PRIMARY_MAC="$(cat "/sys/class/net/${MGMT_IFACE:-lo}/address" 2>/dev/null || echo '')"
NICS="$(for i in $(ls /sys/class/net); do [ "$i" = lo ] && continue; echo -n "$i:$(cat /sys/class/net/$i/address 2>/dev/null) "; done)"
log "iface ice=${ICE_IFACE:-aucune} mgmt=${MGMT_IFACE:-?} mac=$PRIMARY_MAC"

# ─── Mode ZÉRO-TOUCH : enrôlement auprès du contrôleur ──────────────────────────
if [ -n "${CONTROLLER_URL:-}" ] && [ -n "${ENROLL_TOKEN:-}" ]; then
  log "enrôlement zéro-touch auprès de $CONTROLLER_URL …"
  # Boucle d'attente : le contrôleur peut ne pas être joignable au tout 1er boot.
  PROFILE=""
  for attempt in $(seq 1 60); do
    PROFILE="$(python3 - "$CONTROLLER_URL" "$ENROLL_TOKEN" "$(hostname)" "$ICE_IFACE" "$PRIMARY_MAC" "$NICS" <<'PY'
import json, sys, urllib.request
base, tok, host, ice, mac, nics = sys.argv[1:7]
body = json.dumps({"hostname": host, "ice_iface": ice or None, "mac": mac,
                   "nics": nics.split(), "agent_port": 9100}).encode()
req = urllib.request.Request(base.rstrip("/") + "/api/nodes/enroll", data=body,
        headers={"Content-Type": "application/json", "X-MXL-Enroll-Token": tok})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.load(r)
    # Émet des lignes KEY=VALUE shell-safe pour eval côté bash.
    def out(k, v):
        if v is None: return
        print('%s=%s' % (k, json.dumps(str(v))))
    out("E_CAPS", ",".join(d.get("capabilities") or []))
    out("E_MACVLAN_SUBNET", d.get("macvlan_subnet"))
    out("E_MACVLAN_GATEWAY", d.get("macvlan_gateway"))
    out("E_MACVLAN_VLAN", d.get("macvlan_vlan"))
    out("E_MACVLAN_NAME", d.get("macvlan_name") or "bobimacvlan")
    out("E_AGENT_TOKEN", d.get("agent_token"))
    out("E_PTP_DOMAIN", d.get("ptp_domain"))
    out("E_HUGEPAGES", d.get("hugepages"))
    out("E_LCORES", d.get("lcores"))
    out("E_REGISTRY", d.get("registry"))
    out("E_CONTROLLER_SSH_KEY", d.get("controller_ssh_key"))
    out("E_KERNEL_PKG", d.get("kernel_pkg"))
    out("E_KERNEL_APT", d.get("kernel_apt"))
    out("E_OK", "1")
except Exception as e:
    print('E_ERR=%s' % json.dumps(str(e)))
PY
)"
    eval "$PROFILE" || true
    [ "${E_OK:-}" = "1" ] && break
    log "  pas encore enrôlé (tentative $attempt) : ${E_ERR:-?}"
    sleep 10
  done
  [ "${E_OK:-}" = "1" ] || fail "enrôlement impossible après plusieurs tentatives"
  CAPS="${E_CAPS:-compute}"; MACVLAN_SUBNET="${E_MACVLAN_SUBNET:-}"; MACVLAN_GATEWAY="${E_MACVLAN_GATEWAY:-}"
  MACVLAN_VLAN="${E_MACVLAN_VLAN:-}"; MACVLAN_NAME="${E_MACVLAN_NAME:-bobimacvlan}"
  TOKEN="${E_AGENT_TOKEN:-}"; PTP_DOMAIN="${E_PTP_DOMAIN:-127}"; HUGEPAGES="${E_HUGEPAGES:-2048}"
  LCORES="${E_LCORES:-}"; REGISTRY="${E_REGISTRY:-}"
  KERNEL_PKG="${E_KERNEL_PKG:-}"; KERNEL_APT="${E_KERNEL_APT:-}"
  log "profil reçu : caps=$CAPS macvlan=$MACVLAN_SUBNET vlan=${MACVLAN_VLAN:-—}"
  # Clé publique du contrôleur → authorized_keys (SSH root d'ops ; idempotent). L'orchestration
  # passe par l'agent, mais ça donne un shell direct `ssh root@<nœud>` sans manip de fichier.
  if [ -n "${E_CONTROLLER_SSH_KEY:-}" ]; then
    mkdir -p /root/.ssh; chmod 700 /root/.ssh
    if ! grep -qxF "$E_CONTROLLER_SSH_KEY" /root/.ssh/authorized_keys 2>/dev/null; then
      echo "$E_CONTROLLER_SSH_KEY" >> /root/.ssh/authorized_keys
      log "clé SSH du contrôleur ajoutée à authorized_keys"
    fi
    chmod 600 /root/.ssh/authorized_keys
  fi
fi

# ─── Interface VLAN parente du macvlan (remplace un bridge Proxmox) ─────────────
PARENT="${ICE_IFACE:-$MGMT_IFACE}"
if [ -n "${MACVLAN_VLAN:-}" ] && [ -n "$PARENT" ]; then
  VIF="${PARENT}.${MACVLAN_VLAN}"
  if ! ip link show "$VIF" >/dev/null 2>&1; then
    log "création de l'interface VLAN $VIF"
    modprobe 8021q 2>/dev/null || true
    ip link add link "$PARENT" name "$VIF" type vlan id "$MACVLAN_VLAN" 2>/dev/null || true
    ip link set "$VIF" up 2>/dev/null || true
    # Persistance (ifupdown).
    mkdir -p /etc/network/interfaces.d
    printf 'auto %s\niface %s inet manual\n  vlan-raw-device %s\n' "$VIF" "$VIF" "$PARENT" \
      > "/etc/network/interfaces.d/bobi-${VIF}"
  fi
  MACVLAN_PARENT="$VIF"
else
  MACVLAN_PARENT="${MACVLAN_PARENT:-$PARENT}"
fi

# ─── Lancement de l'installeur de nœud ──────────────────────────────────────────
ARGS=( --with "${CAPS:-compute}" --macvlan-name "${MACVLAN_NAME:-bobimacvlan}" )
[ -n "${TOKEN:-}" ]            && ARGS+=( --token "$TOKEN" )
[ -n "${MACVLAN_PARENT:-}" ]  && ARGS+=( --macvlan-parent "$MACVLAN_PARENT" )
[ -n "${MACVLAN_SUBNET:-}" ]  && ARGS+=( --macvlan-subnet "$MACVLAN_SUBNET" )
[ -n "${MACVLAN_GATEWAY:-}" ] && ARGS+=( --macvlan-gateway "$MACVLAN_GATEWAY" )
[ -n "${MACVLAN_RANGE:-}" ]   && ARGS+=( --macvlan-range "$MACVLAN_RANGE" )
[ -n "${ICE_IFACE:-}" ]       && ARGS+=( --mtl-iface "$ICE_IFACE" )
[ -n "${PTP_DOMAIN:-}" ]      && ARGS+=( --ptp-domain "$PTP_DOMAIN" )
[ -n "${HUGEPAGES:-}" ]       && ARGS+=( --hugepages "$HUGEPAGES" )
[ -n "${LCORES:-}" ]          && ARGS+=( --lcores "$LCORES" )
[ -n "${REGISTRY:-}" ]        && ARGS+=( --registry "$REGISTRY" )
[ -n "${KERNEL_PKG:-}" ]      && ARGS+=( --kernel-pkg "$KERNEL_PKG" )
[ -n "${KERNEL_APT:-}" ]      && ARGS+=( --kernel-apt "$KERNEL_APT" )

log "install-node.sh ${ARGS[*]}"
if "$SRC/install-node.sh" "${ARGS[@]}"; then
  mkdir -p "$(dirname "$STATE")"; date -Is > "$STATE"
  log "provisioning OK — désactivation du oneshot."
  systemctl disable bobi-node-bootstrap.service 2>/dev/null || true
else
  fail "install-node.sh a échoué (voir $LOG) — le oneshot réessaiera au prochain boot."
fi
