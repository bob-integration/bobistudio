# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""MTL (Media Transport Library / Intel Tiber Broadcast Suite) — prép host DPDK/E810.

Vérifie et applique les pré-requis kernel d'un host pour faire tourner MTL en DPDK :
- IOMMU activé (`intel_iommu=on iommu=pt`),
- hugepages 1G réservées au boot,
- module `vfio-pci` chargé,
- driver `ice` (E810) + DDP présents.

La prép ne concerne que l'**hôte local** de cette instance (= `proxmox_host`) ; il n'y a
pas de host distant paramétrable. Toutes les opérations passent par `ssh_run` (même canal
que PTP). Aucun reboot automatique : `appliquer()` écrit le cmdline et signale qu'un reboot
est requis ; `redemarrer()` n'est appelé que sur action confirmée.
"""
import hashlib
import json
import logging
import re

from .host_ops import ssh_run

log = logging.getLogger(__name__)

KERNEL_CMDLINE_PATH = "/etc/kernel/cmdline"        # systemd-boot (proxmox-boot-tool)
VFIO_MODULES_PATH   = "/etc/modules-load.d/vfio.conf"

# Flags requis dans le cmdline (hors hugepages, qui dépendent du nombre demandé)
REQUIRED_FLAGS = ["intel_iommu=on", "iommu=pt"]

# Générations ConnectX (vendor 0x15b3) — réutilisé pour info (MTL supporte mlx5 = CX-4+)
CONNECTX = {
    "0x1013": "ConnectX-4",    "0x1015": "ConnectX-4 Lx",
    "0x1017": "ConnectX-5",    "0x1019": "ConnectX-5 Ex", "0x101a": "ConnectX-5 Ex",
    "0x101b": "ConnectX-6",    "0x101d": "ConnectX-6 Dx",  "0x101e": "ConnectX-6 Dx",
    "0x101f": "ConnectX-6 Lx", "0x1021": "ConnectX-7",     "0x1023": "ConnectX-7",
}
E810 = {
    "0x1592", "0x1593", "0x1599", "0x159a", "0x159b", "0x159c",
    "0x1891", "0x188a", "0x188b", "0x188c",
}

# ── BIBLIOTHÈQUE DE CARTES : capacités NON identifiables → À CONNAÎTRE ───────────────────────────
# Le max de FILES/sessions TX narrow (RL) EFFECTIF n'est PAS lisible du PMD : rte_eth_dev_info.
# max_tx_queues rapporte le mur ice NATIF (8) que patch_tm_hierarchy transcende jusqu'à la vraie
# capacité (banc dl360-1 E810-C = 64 files → 63 sessions). C'est une propriété mtl+patch, invisible
# à l'introspection → il faut la CONNAÎTRE (biblio), pas la lire (cf. docs/chantiers/DPDK_NARROW.md §7).
#
# Clé = sous-chaîne de MODÈLE (node_interfaces.model). À terme keyer aussi le FIRMWARE/DDP (qui peut
# changer la capacité → une carte peut figurer plusieurs fois). Valeurs = MESURÉES au banc /
# qualification uniquement ; une carte inconnue prend le plancher sûr (jamais de boucle de relance)
# jusqu'à sa qualification. Ordre = du plus spécifique au plus générique.
NIC_RL_TX_CAP = [
    # (sous-chaîne modèle (lower), sessions TX narrow max/port, MESURÉ ?)
    # ⚠ Un cap TROP HAUT sur une carte non mesurée RE-BOUCLE (le clamp DOIT être ≤ capacité réelle) →
    # on n'inscrit QUE le modèle MESURÉ. Les variantes non qualifiées (ex. E810-XXVDA4 4-port, dont les
    # files sont partagées entre 4 ports → probablement < 63) tombent sur le plancher sûr ci-dessous
    # jusqu'à leur qualification au banc.
    ("e810-c", 63, True),    # E810-C-Q2 MESURÉ (dl360-1, 64 files − 1 contrôle).
]
# Carte NON profilée : plancher ice natif SÛR (le mur des 8 avant patch) → 7 sessions. Ne re-boucle
# JAMAIS (toujours ≤ capacité réelle) ; la qualification relève la valeur et l'inscrit dans le profil.
NIC_RL_TX_CAP_DEFAULT = 7


def nic_rl_tx_cap(model):
    """Sessions TX narrow (RL) MAX par port pour cette carte (biblio). model = node_interfaces.model.
    Inconnu → plancher sûr NIC_RL_TX_CAP_DEFAULT (à qualifier). Cf. docs/chantiers/DPDK_NARROW.md §7 (pourquoi ce
    n'est pas auto-découvrable)."""
    m = (model or "").lower()
    for key, cap, _measured in NIC_RL_TX_CAP:
        if key in m:
            return cap
    return NIC_RL_TX_CAP_DEFAULT


def _hugepages_flags(n):
    """Tokens cmdline pour réserver `n` hugepages de 1G."""
    return ["default_hugepagesz=1G", "hugepagesz=1G", f"hugepages={int(n)}"]


# ─── Isolation des cœurs DPDK du moteur 2110 (cmdline) ───────────────────────
# POURQUOI : sans isolation, IRQ/softirq/tick timer/callbacks RCU du noyau préemptent les
# schedulers libmtl (busy-poll <100 % → overflow des rings → trames incomplètes → flux qui
# tombent au-delà de la capacité « propre »).
#
# LA BANDE N'EST PAS CALCULÉE ICI : elle vient de `core_pool.engine_cpu_footprint`, LA source de
# vérité unique de l'empreinte moteur, partagée avec `derive_compute_cpuset` (pool de calcul).
# Deux calculs séparés divergent — c'est exactement ce qui s'est produit avec une bande PLATE
# `1-18` déclarée « identique au cpuset compute » : les siblings HT (49-66) restaient en
# housekeeping et RECEVAIENT les IRQ, sur les jumeaux physiques des cœurs busy-poll.
#
# Le cœur 0 (et TOUT son cœur physique, siblings compris) est EXCLU de la bande : c'est le
# housekeeping du noyau (et le main_lcore EAL, qui dort). Isoler cpu0 sans son sibling
# rebasculerait le housekeeping sur le jumeau d'un lcore.

def _parse_topologie(txt):
    """Blocs `<cpu>|<thread_siblings_list>` → {cpu logique: cœur physique canonique (= plus petit
    sibling)}. {} si rien d'exploitable (sysfs absent, conteneur restreint…)."""
    core_of = {}
    for ln in (txt or "").splitlines():
        p = ln.strip().split("|")
        if len(p) != 2 or not p[0].isdigit():
            continue
        sibs = set()
        for part in p[1].split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                if a.strip().isdigit() and b.strip().isdigit():
                    sibs.update(range(int(a), int(b) + 1))
            elif part.isdigit():
                sibs.add(int(part))
        if sibs:
            core_of[int(p[0])] = min(sibs)
    return core_of


_TOPO_PROBE = r"""echo "@@NPROC"; nproc --all 2>/dev/null || echo 0
echo "@@TOPO"
for d in /sys/devices/system/cpu/cpu[0-9]*; do
  c=$(basename "$d"); c=$(echo "$c" | tr -dc '0-9')
  s=$(cat "$d/topology/thread_siblings_list" 2>/dev/null || true)
  [ -n "$s" ] && echo "$c|$s"
done
echo "@@TOPOEND"
"""


def _lire_topologie(run):
    """Lit la topologie HT du nœud : (core_of, n_cpus, note). `core_of` = {cpu: cœur physique}
    depuis /sys/devices/system/cpu/cpu*/topology/thread_siblings_list (RIEN en dur : ni nombre de
    CPU, ni bande). ({} , n, raison) si indisponible → l'appelant DOIT signaler le repli plat."""
    try:
        rc, out, err = run(_TOPO_PROBE, timeout=20)
    except Exception as e:                                    # nœud injoignable
        return {}, 0, f"lecture topologie impossible ({e})"
    if rc != 0:
        return {}, 0, f"lecture topologie rc={rc} {(err or '').strip()[:120]}"
    blocks = _split_blocks(out or "")
    try:
        n_cpus = int((blocks.get("NPROC", "0").strip() or "0").splitlines()[0])
    except (ValueError, IndexError):
        n_cpus = 0
    core_of = _parse_topologie(blocks.get("TOPO", ""))
    if not core_of:
        return {}, n_cpus, "thread_siblings_list illisible (sysfs topology absent)"
    return core_of, n_cpus, ""


def _isolation_cpus(core_of=None, n_cpus=None):
    """Bande de cœurs à ISOLER du noyau : l'empreinte moteur (`core_pool.engine_cpu_footprint`,
    source de vérité unique) MOINS deux ensembles qui n'ont rien à y faire.

    1. **Le cœur physique 0 entier** (jumeaux HT compris) : c'est le housekeeping du noyau, et le
       main_lcore EAL — qui dort — le partage avec lui.
    2. **Les cœurs de SERVICE** (`engine_service_cpus`) : ils accueillent des threads ORDINAIRES,
       et un cœur isolé ne reçoit AUCUNE migration de l'ordonnanceur. Les isoler revenait à les
       rendre inutilisables par ce qu'ils sont censés porter — et à replier tout le pool de service
       sur le cœur 0. Constaté sur dl360-1 : 277 threads sur 289 sur le seul cœur 0, à 54,8 %,
       pendant que les CPU 9-15, réservés ET isolés, ne faisaient rien.

    Ne restent isolés que les lcores en busy-poll, les seuls qui l'exigent. Renvoie
    (cpulist, ht_aware) — ex. ('1-18,49-66', True) avec HT, ('1-18', False) sans topologie.
    """
    from .core_pool import engine_cpu_footprint, engine_service_cpus, fmt_cpuset
    cpus, ht_aware = engine_cpu_footprint(n_cpus=n_cpus, core_of=core_of)
    if not cpus:
        return "", ht_aware
    hk = {0}
    if core_of and 0 in core_of:
        hk = {c for c, k in core_of.items() if k == core_of[0]}   # cpu0 + ses siblings HT
    hk |= engine_service_cpus(n_cpus=n_cpus, core_of=core_of)
    band = sorted(c for c in cpus if c not in hk)
    return (fmt_cpuset(band) if band else ""), ht_aware


def _isolation_flags(band):
    """Flags cmdline qui DÉDIENT `band` aux busy-poll DPDK : `isolcpus=domain,managed_irq` (hors
    ordonnanceur + hors IRQ managées de la NIC), `nohz_full` (pas de tick timer), `rcu_nocbs`
    (callbacks RCU déportés). '' → aucun flag (config dégénérée)."""
    if not band:
        return []
    return [f"isolcpus=domain,managed_irq,{band}", f"nohz_full={band}", f"rcu_nocbs={band}"]


def plan_isolation(run):
    """Plan d'isolation POUR CE NŒUD : lit la topologie, dérive la bande, signale tout repli.
    Renvoie {band, ht_aware, n_cpus, note, core_of}. `ht_aware=False` ⇒ bande PLATE : les siblings
    HT des lcores ne seront PAS isolés (contention HT) — `note` dit pourquoi, et les appelants
    (appliquer/verifier) la remontent ; JAMAIS de repli muet."""
    core_of, n_cpus, note = _lire_topologie(run)
    band, ht_aware = _isolation_cpus(core_of=core_of or None, n_cpus=n_cpus or None)
    if not ht_aware and band:
        note = note or "carte des siblings HT indisponible"
        log.warning("isolation cœurs 2110 : REPLI MODÈLE PLAT (bande %s, siblings HT NON isolés) — %s",
                    band, note)
    return {"band": band, "ht_aware": ht_aware, "n_cpus": n_cpus, "note": note,
            "core_of": core_of}


# ─── Runner injectable (SSH legacy ↔ agent-nœud) ─────────────────────────────
# Chantier DPDK (Lot A) : les fonctions de host-prep acceptent un paramètre `run=`
# (signature (cmd, input_data=None, timeout=300) → (rc, stdout, stderr)). Défaut =
# comportement historique (ssh_run, qui route déjà vers l'agent pour un nœud enrôlé).
# Les façades `*_node(node)` routent EXPLICITEMENT par node_driver.host_exec (token
# HTTP, /v1/host/exec) — plus de dépendance à la résolution host→nœud de ssh_run.

def _run_ssh(host):
    """Runner par défaut : chemin historique `ssh_run(host, …)`."""
    def run(cmd, input_data=None, timeout=300):
        return ssh_run(host, cmd, input_data=input_data, timeout=timeout)
    return run


def _run_agent(node):
    """Runner agent-nœud : exécute via `node_driver.host_exec` (endpoint /v1/host/exec)."""
    from . import node_driver

    def run(cmd, input_data=None, timeout=300):
        return node_driver.host_exec(node, cmd, input_data=input_data, timeout=timeout)
    return run


def verifier_node(node):
    """Façade nœud de `verifier` : sonde l'état de prép MTL d'un NŒUD enrôlé via son agent."""
    return verifier((node or {}).get("host"), run=_run_agent(node))


def appliquer_node(node, hugepages_1g=16):
    """Façade nœud de `appliquer` : applique la prép (cmdline IOMMU + hugepages 1G + vfio.conf
    + sysctl) sur un NŒUD enrôlé via son agent. Renvoie (ok, msg, reboot_needed)."""
    ok, msg, reboot = appliquer((node or {}).get("host"), hugepages_1g, run=_run_agent(node))
    # Identité du domaine MXL (cf. ensure_mxl_domain_def) : posée ici parce que c'est le seul
    # chemin de prép qui connaisse le NŒUD (et donc son UUID de domaine en base) — `appliquer`,
    # lui, ne reçoit qu'un host. Best effort : n'invalide pas la prép cmdline déjà écrite.
    ok_dd, msg_dd = ensure_mxl_domain_def(node)
    msg += f" ; domaine MXL: {msg_dd}" if ok_dd else f" ; identité domaine MXL NON posée: {msg_dd}"
    return ok, msg, reboot


# ─── Vérification (lecture seule) ────────────────────────────────────────────

def verifier(host, run=None):
    """Sonde l'état de préparation MTL d'un host. Renvoie un dict de readiness.
    Probe strictement en lecture (aucune modification). `error` non vide => SSH KO.
    `run` : runner injectable (défaut = ssh_run sur `host`, cf. Lot A)."""
    out = {
        "host":            host,
        "iommu_cmdline":   False,
        "iommu_active":    False,
        "hugepages_total": 0,
        "hugepages_size_ok": False,
        "hugepages_1g_supported": False,
        "isolated_cpus":   "",       # /sys/devices/system/cpu/isolated (bande isolcpus ACTIVE)
        "ice_present":     False,
        "vfio_present":    False,
        "ddp_pkg":         None,
        "bootloader":      None,     # 'systemd-boot' | 'grub' | None
        "reboot_needed":   False,
        "nics":            [],       # [{iface, pci, family, model, slot_gen, slot_width}]
        "rdma_unit":       False,    # unité rdma-netns-exclusive installée + enabled
        "rdma_exclusive":  False,    # `rdma system show` == netns exclusive (actif)
        # Fréquence des cœurs ISOLÉS du moteur 2110. `risk` = LE piège : une bande isolée dont la
        # fréquence n'est PAS épinglée retombe au plancher (nohz_full prive intel_pstate du retour
        # d'utilisation sur les cœurs tickless) → le moteur s'étouffe. Sonde OBLIGATOIRE : sans elle
        # un simple reboot réintroduit le bug EN SILENCE. Rien en dur : la cible est `cpuinfo_max_freq`
        # lue sur chaque cœur, donc le seuil s'adapte au CPU présent.
        "cpufreq":         {"unit": False, "isolated": 0, "pinned": False, "governor": None,
                            "min_mhz": None, "cur_min_mhz": None, "max_mhz": None, "risk": False},
        # Isolation des cœurs DPDK du moteur 2110 : bande ACTIVE (cmdline en vigueur) vs bande
        # ATTENDUE (dérivée de core_pool.engine_cpu_footprint, HT-aware). `risk` = LE piège :
        # bande partielle (siblings HT laissés au noyau), unité IRQ absente, ou irqbalance actif
        # (il ré-étale les IRQ sur la bande isolée quelques minutes APRÈS le boot → régression
        # silencieuse à retardement).
        "isolation":       {"active": "", "expected": "", "match": False, "ht_aware": False,
                            "unit": False, "unit_state": None, "irqbalance": False,
                            "n_cpus": 0, "risk": False, "hint": None},
        "mtl_capable":     False,    # ≥1 carte CX-4+/E810 (dérivé des nics)
        # SR-IOV/MMIO (prérequis des VF DPDK/narrow, cf. docs/reference/PTP_CLOCK.md) — probe read-only SANS iLO :
        # `mmio_error` = le noyau a DÉJÀ échoué à créer un VF faute de MMIO (dmesg) → BIOS à régler
        # (HPe: PciResourcePadding=High / Above-4G). `capable` = ≥1 E810 avec sriov_totalvfs>0.
        "sriov":           {"capable": False, "mmio_error": False, "nics": [], "hint": None},
        "error":           None,
    }
    if not host:
        out["error"] = "host non configuré"
        return out

    # Probe unique (un seul SSH) : on émet des blocs balisés faciles à parser.
    script = r"""
echo "@@CMDLINE_LIVE"; cat /proc/cmdline 2>/dev/null
echo "@@CMDLINE_PENDING"; cat /etc/kernel/cmdline 2>/dev/null
# Le cmdline « en attente » vit à DEUX endroits selon le bootloader : /etc/kernel/cmdline
# (systemd-boot/proxmox-boot-tool) OU /etc/default/grub (grub). Ne lire que le premier rendait
# `pending` VIDE sur tout nœud grub → `reboot_needed` bloqué à False EN PERMANENCE : après une
# prép, rien n'invitait jamais à redémarrer pour l'activer. On émet les deux ; `_managed_tokens`
# filtre de toute façon sur les préfixes gérés, donc concaténer est sans risque.
grep -h "^GRUB_CMDLINE_LINUX" /etc/default/grub 2>/dev/null | sed 's/^[^=]*=//; s/"//g'
echo "@@IOMMU_GROUPS"; ls /sys/kernel/iommu_groups 2>/dev/null | wc -l
echo "@@HUGE"; grep -iE "HugePages_Total|Hugepagesize" /proc/meminfo 2>/dev/null
echo "@@PDPE1GB"; grep -o pdpe1gb /proc/cpuinfo 2>/dev/null | head -1
echo "@@ICE"; modinfo ice 2>/dev/null | grep -E "^filename" || true
echo "@@VFIO"; modinfo vfio-pci 2>/dev/null | grep -E "^filename" || true
echo "@@DDP"; (readlink -f /lib/firmware/intel/ice/ddp/ice.pkg 2>/dev/null | xargs -r basename) || true
echo "@@BOOTLOADER"; (proxmox-boot-tool status >/dev/null 2>&1 && echo systemd-boot) || (command -v update-grub >/dev/null 2>&1 && echo grub) || echo unknown
echo "@@RDMA_UNIT"; systemctl is-enabled rdma-netns-exclusive.service 2>/dev/null || true
echo "@@RDMA_MODE"; rdma system show 2>/dev/null || true
echo "@@ISOLATED"; cat /sys/devices/system/cpu/isolated 2>/dev/null
echo "@@IRQ_UNIT"; systemctl is-enabled bobi-irq-housekeeping.service 2>/dev/null || true
echo "@@IRQ_STATE"; systemctl is-active bobi-irq-housekeeping.service 2>/dev/null || true
echo "@@IRQBALANCE"; systemctl is-active irqbalance 2>/dev/null || true
echo "@@CPUFREQ_UNIT"; systemctl is-enabled bobi-cpufreq-perf.service 2>/dev/null || true
echo "@@CPUFREQ"
_iso=$(cat /sys/devices/system/cpu/isolated 2>/dev/null)
for _p in $(echo "$_iso" | tr ',' ' '); do
  _lo=$(echo "$_p" | cut -d- -f1); _hi=$(echo "$_p" | cut -d- -f2); _i=$_lo
  while [ -n "$_i" ] && [ "$_i" -le "$_hi" ] 2>/dev/null; do
    _d=/sys/devices/system/cpu/cpu$_i/cpufreq
    [ -d "$_d" ] && echo "$_i|$(cat $_d/scaling_governor 2>/dev/null)|$(cat $_d/scaling_min_freq 2>/dev/null)|$(cat $_d/scaling_cur_freq 2>/dev/null)|$(cat $_d/cpuinfo_max_freq 2>/dev/null)"
    _i=$((_i+1))
  done
done
echo "@@NICS"
for n in /sys/class/net/*; do
  ifc=$(basename "$n"); d="$n/device"
  [ -d "$d" ] || continue
  [ -e "$d/physfn" ] && continue
  v=$(cat "$d/vendor" 2>/dev/null); dev=$(cat "$d/device" 2>/dev/null)
  case "$v" in
    0x15b3|0x8086)
      pci=$(basename "$(readlink "$d" 2>/dev/null)" 2>/dev/null)
      drv=$(basename "$(readlink "$d/driver" 2>/dev/null)" 2>/dev/null)
      lnk=$(cat "$d/current_link_speed" 2>/dev/null)
      wid=$(cat "$d/current_link_width" 2>/dev/null)
      echo "$ifc|$v|$dev|$drv|$pci|$lnk|$wid"
      ;;
  esac
done
echo "@@SRIOV"
for n in /sys/class/net/*; do
  ifc=$(basename "$n"); d="$n/device"
  [ -d "$d" ] || continue
  [ -e "$d/physfn" ] && continue
  [ "$(cat "$d/vendor" 2>/dev/null)" = "0x8086" ] || continue
  tv=$(cat "$d/sriov_totalvfs" 2>/dev/null || echo 0)
  nv=$(cat "$d/sriov_numvfs" 2>/dev/null || echo 0)
  echo "$ifc|$tv|$nv"
done
echo "@@SRIOV_MMIO"; dmesg 2>/dev/null | grep -c "not enough MMIO resources for SR-IOV" 2>/dev/null || echo 0
echo "@@END"
"""
    run = run or _run_ssh(host)
    # + topologie HT (nproc/thread_siblings_list) : sert à recalculer la bande ATTENDUE dans le
    # MÊME aller-retour (la comparer à la bande active est la seule façon de voir qu'un cmdline
    # posé avant le fix HT n'isole que la moitié de l'empreinte).
    rc, txt, err = run(script + _TOPO_PROBE, timeout=25)
    if rc != 0:
        out["error"] = f"SSH rc={rc} {err.strip()[:200]}"
        return out

    blocks = _split_blocks(txt)
    live    = blocks.get("CMDLINE_LIVE", "").strip()
    pending = blocks.get("CMDLINE_PENDING", "").strip()

    out["iommu_cmdline"] = all(f in live for f in REQUIRED_FLAGS)
    try:
        out["iommu_active"] = int(blocks.get("IOMMU_GROUPS", "0").strip() or 0) > 0
    except ValueError:
        out["iommu_active"] = False

    huge = blocks.get("HUGE", "")
    m = re.search(r"HugePages_Total:\s*(\d+)", huge)
    if m:
        out["hugepages_total"] = int(m.group(1))
    m = re.search(r"Hugepagesize:\s*(\d+)\s*kB", huge)
    if m:
        out["hugepages_size_ok"] = int(m.group(1)) >= 1048576  # 1G
    out["hugepages_1g_supported"] = bool(blocks.get("PDPE1GB", "").strip())

    # Fréquence des cœurs isolés : AUCUN seuil en dur. La cible de chaque cœur est SON PROPRE
    # `cpuinfo_max_freq` (turbo max du CPU présent) ; « épinglé » = gouverneur performance ET
    # scaling_min_freq à ≥95 % de ce max sur TOUS les cœurs de la bande (95 % = tolérance aux
    # arrondis de la table P-state, pas une valeur métier). Bande isolée sans épinglage = `risk`.
    cf = out["cpufreq"]
    cf["unit"] = blocks.get("CPUFREQ_UNIT", "").strip() == "enabled"
    govs, mins, curs, maxs = set(), [], [], []
    for ln in blocks.get("CPUFREQ", "").splitlines():
        parts = ln.strip().split("|")
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        govs.add(parts[1].strip())
        for lst, raw in ((mins, parts[2]), (curs, parts[3]), (maxs, parts[4])):
            try:
                lst.append(int(raw.strip()))
            except (TypeError, ValueError):
                pass
        cf["isolated"] += 1
    if cf["isolated"] and maxs:
        cf["governor"]    = sorted(govs)[0] if len(govs) == 1 else "mixte"
        cf["max_mhz"]     = max(maxs) // 1000
        cf["min_mhz"]     = (min(mins) // 1000) if mins else None
        cf["cur_min_mhz"] = (min(curs) // 1000) if curs else None
        # Tous les cœurs sondés doivent être performance ET plancher ≥95 % de LEUR max.
        cf["pinned"] = (govs == {"performance"} and len(mins) == cf["isolated"]
                        and len(maxs) == cf["isolated"]
                        and all(mn >= 0.95 * mx for mn, mx in zip(mins, maxs)))
    cf["risk"] = bool(cf["isolated"]) and not cf["pinned"]

    # ── Isolation des cœurs DPDK : ACTIF (sysfs) vs ATTENDU (source de vérité core_pool) ──
    from .core_pool import parse_cpuset as _parse_cpuset, fmt_cpuset as _fmt_cpuset
    iso = out["isolation"]
    try:
        n_cpus = int((blocks.get("NPROC", "0").strip() or "0").splitlines()[0])
    except (ValueError, IndexError):
        n_cpus = 0
    core_of = _parse_topologie(blocks.get("TOPO", ""))
    expected, ht_aware = _isolation_cpus(core_of=core_of or None, n_cpus=n_cpus or None)
    active = blocks.get("ISOLATED", "").strip()
    out["isolated_cpus"] = active
    iso.update({
        "active": active, "expected": expected, "ht_aware": ht_aware, "n_cpus": n_cpus,
        "unit": blocks.get("IRQ_UNIT", "").strip() == "enabled",
        "unit_state": blocks.get("IRQ_STATE", "").strip() or None,
        "irqbalance": blocks.get("IRQBALANCE", "").strip() == "active",
    })
    act_set, exp_set = _parse_cpuset(active), _parse_cpuset(expected)
    iso["match"] = bool(exp_set) and act_set == exp_set
    manque = _fmt_cpuset(exp_set - act_set)
    hints = []
    if exp_set and not act_set:
        # Distinguer « prép jamais appliquée » de « prép appliquée, en attente de reboot » : sans
        # ça on envoie l'opérateur recliquer « Appliquer » alors qu'il ne manque qu'un redémarrage.
        _en_attente = any(t.split("=", 1)[0] == "isolcpus" for t in (pending or "").split())
        hints.append(
            f"bande {expected} écrite dans le cmdline mais PAS ACTIVE — redémarrer l'hôte pour "
            "l'activer" if _en_attente else
            f"aucun cœur isolé — bande attendue {expected} : appliquer la prép MTL "
            "(cmdline isolcpus/nohz_full/rcu_nocbs) puis redémarrer l'hôte")
    elif manque:
        hints.append(f"bande isolée INCOMPLÈTE : {manque} hors isolation alors que ces CPU "
                     "appartiennent à l'empreinte du moteur 2110"
                     + (" (threads siblings HyperThreading des lcores : le noyau tourne sur les "
                        "jumeaux physiques des cœurs busy-poll)" if ht_aware else "")
                     + " → réappliquer la prép MTL + redémarrer")
    if not ht_aware and exp_set:
        hints.append("topologie HyperThreading ILLISIBLE sur ce nœud → bande calculée en modèle "
                     "PLAT : les siblings HT des lcores ne sont pas couverts (repli signalé, "
                     "pas silencieux)")
    if act_set and not iso["unit"]:
        hints.append("unité bobi-irq-housekeeping absente : les IRQ non managées restent sur la "
                     "bande isolée")
    if iso["irqbalance"]:
        hints.append("irqbalance est ACTIF : il ré-étale les IRQ sur la bande isolée quelques "
                     "minutes après le boot (régression à retardement) → le masquer "
                     "(la prép MTL le fait)")
    if act_set and (blocks.get("IRQ_STATE", "").strip() == "failed"):
        hints.append("bobi-irq-housekeeping en échec (systemctl status) — IRQ non reposées")
    iso["hint"] = " ; ".join(hints) or None
    iso["risk"] = bool(hints)

    out["ice_present"]  = "filename:" in blocks.get("ICE", "")
    out["vfio_present"] = "filename:" in blocks.get("VFIO", "")
    ddp = blocks.get("DDP", "").strip()
    out["ddp_pkg"] = ddp or None
    bl = blocks.get("BOOTLOADER", "").strip()
    out["bootloader"] = bl if bl in ("systemd-boot", "grub") else None

    # reboot requis seulement si des tokens QUE NOUS GÉRONS (IOMMU/hugepages) sont présents
    # dans le cmdline pending mais pas encore actifs dans le live. Comparer les cmdline
    # entières donnait un faux positif permanent : /proc/cmdline contient toujours des tokens
    # de boot (initrd=, BOOT_IMAGE=…) absents de /etc/kernel/cmdline.
    if pending and (_managed_tokens(pending) - _managed_tokens(live)):
        out["reboot_needed"] = True

    out["rdma_unit"]      = blocks.get("RDMA_UNIT", "").strip() == "enabled"
    out["rdma_exclusive"] = "netns exclusive" in blocks.get("RDMA_MODE", "")

    out["nics"] = _parse_nics(blocks.get("NICS", ""))
    out["mtl_capable"] = any(n.get("mtl_capable") for n in out["nics"])

    # SR-IOV/MMIO (read-only, sans iLO) : capacité par E810 + erreur MMIO confirmée (dmesg). Un
    # nœud « capable » mais sans VF ni erreur reste indéterminé en read-only (le test définitif =
    # dry-run création de VF, mutant, hors de ce probe) ; l'erreur dmesg, elle, est un signal SÛR.
    sn = []
    for ln in blocks.get("SRIOV", "").splitlines():
        p = ln.split("|")
        if len(p) == 3 and p[0].strip():
            try:
                sn.append({"iface": p[0].strip(), "totalvfs": int(p[1] or 0), "numvfs": int(p[2] or 0)})
            except ValueError:
                pass
    try:
        mmio_err = int((blocks.get("SRIOV_MMIO", "0").strip() or "0").splitlines()[0]) > 0
    except (ValueError, IndexError):
        mmio_err = False
    out["sriov"] = {
        "capable":    any(n["totalvfs"] > 0 for n in sn),
        "mmio_error": mmio_err,
        "nics":       sn,
        "hint": ("SR-IOV bloqué faute de MMIO — régler le BIOS (HPe : PciResourcePadding=High ; "
                 "Dell : MmioAbove4GB=Enabled) + reboot. Requis pour les VF DPDK/narrow.")
                if mmio_err else None,
    }
    return out


def host_mtl_capable(host):
    """Le nœud a-t-il au moins une carte compatible MTL (ConnectX-4+ en mlx5, ou E810) ?
    Gate du mode « template MTL » (front + back). Renvoie (capable, reason, cards)."""
    s = verifier(host)
    if s.get("error"):
        return False, f"hôte injoignable : {s['error']}", []
    cards = [n for n in s.get("nics", []) if n.get("mtl_capable")]
    if cards:
        models = ", ".join(sorted({c["model"] for c in cards}))
        return True, f"carte(s) compatible(s) : {models}", cards
    seen = ", ".join(sorted({n["model"] for n in s.get("nics", [])})) or "aucune carte 15b3/8086"
    return False, f"aucune carte compatible MTL (CX-4+/E810) — détecté : {seen}", []


def _split_blocks(txt):
    """Découpe la sortie balisée `@@NAME` en {NAME: contenu}."""
    blocks, cur, buf = {}, None, []
    for line in txt.splitlines():
        if line.startswith("@@"):
            if cur is not None:
                blocks[cur] = "\n".join(buf)
            cur, buf = line[2:].strip(), []
        else:
            buf.append(line)
    if cur is not None:
        blocks[cur] = "\n".join(buf)
    return blocks


# Préfixes de tokens cmdline gérés par la prép MTL (IOMMU + hugepages + isolation cœurs 2110).
# ⚠ isolcpus/nohz_full/rcu_nocbs DOIVENT y figurer : c'est ce qui fait lever `reboot_needed` quand
# la bande isolée change (ex. élargissement HT) — sans ça le cmdline pending diffère du live sans
# que personne ne le voie.
_MANAGED_KEYS = {"intel_iommu", "iommu", "default_hugepagesz", "hugepagesz", "hugepages",
                 "isolcpus", "nohz_full", "rcu_nocbs"}

def _managed_tokens(s):
    """Sous-ensemble des tokens d'un cmdline que la prép MTL gère (ignore root=, initrd=…)."""
    return {t for t in s.split() if t.split("=", 1)[0] in _MANAGED_KEYS}


def _parse_nics(raw):
    nics = []
    for line in raw.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 7:
            continue
        ifc, vendor, device, driver, pci, lnk, wid = parts
        if vendor == "0x8086":
            family, model = ("e810", "Intel E810") if device in E810 else ("intel", "Intel")
        elif vendor == "0x15b3":
            family = "connectx"
            model = CONNECTX.get(device, "ConnectX")
        else:
            family, model = "?", "?"
        nics.append({
            "iface": ifc, "pci": pci, "family": family, "model": model,
            "driver": driver, "device": device,
            "slot_speed": lnk.strip(), "slot_width": wid.strip(),
            "mtl_capable": family == "e810" or (family == "connectx" and driver == "mlx5_core"),
        })
    return nics


# ─── Application de la prép (écriture cmdline + refresh) ──────────────────────

def appliquer(host, hugepages_1g=16, run=None):
    """Écrit les flags IOMMU + hugepages dans le cmdline (idempotent), charge vfio-pci
    au boot, et rafraîchit le bootloader. NE REBOOTE PAS. Renvoie (ok, msg, reboot_needed).
    `run` : runner injectable (défaut = ssh_run sur `host`, cf. Lot A)."""
    if not host:
        return False, "host non configuré", False
    run = run or _run_ssh(host)
    try:
        n = int(hugepages_1g)
    except (TypeError, ValueError):
        n = 16
    if n < 0:
        n = 0

    # Isolation des cœurs du moteur 2110 : la bande vient de core_pool.engine_cpu_footprint (source
    # de vérité unique) et est calculée SUR LE NŒUD (topologie HT lue en sysfs) — jamais en dur.
    plan = plan_isolation(run)
    flags = REQUIRED_FLAGS + _hugepages_flags(n) + _isolation_flags(plan["band"])
    flags_str = " ".join(flags)
    # Script bash idempotent : n'ajoute que les tokens absents, backup horodaté, remplace une
    # éventuelle valeur hugepages= différente. Le bootloader est choisi par l'OUTIL présent (comme
    # `verifier`) : proxmox-boot-tool (host Proxmox/systemd-boot) → sinon grub (Debian nue = nœud).
    # ⚠ NE PAS se baser sur l'existence de /etc/kernel/cmdline : il peut exister sans proxmox-boot-tool
    # alors que le boot réel passe par grub → on écrirait dans un fichier ignoré + proxmox-boot-tool
    # « commande introuvable ».
    script = r"""set -e
WANT="%FLAGS%"
apply_grub() {
  [ -f /etc/default/grub ] || { echo "@@ERR /etc/default/grub absent"; exit 1; }
  cp -a /etc/default/grub "/etc/default/grub.bak.$(date +%Y%m%d-%H%M%S)"
  cur=$(grep -oP 'GRUB_CMDLINE_LINUX_DEFAULT="\K[^"]*' /etc/default/grub || echo "")
  line="$cur"
  for f in $WANT; do
    key=${f%%=*}
    line=$(echo "$line" | sed -E "s/(^| )${key}=[^ ]*//g")
    line="$line $f"
  done
  line=$(echo "$line" | sed -E 's/^ +//; s/ +/ /g')
  sed -i "s|GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*\"|GRUB_CMDLINE_LINUX_DEFAULT=\"$line\"|" /etc/default/grub
  echo "vfio-pci" > /etc/modules-load.d/vfio.conf
  update-grub 2>&1
  echo "@@OK grub"
}
apply_systemd_boot() {
  CMDLINE=/etc/kernel/cmdline
  [ -f "$CMDLINE" ] && cp -a "$CMDLINE" "${CMDLINE}.bak.$(date +%Y%m%d-%H%M%S)"
  line=$(cat "$CMDLINE" 2>/dev/null || echo "")
  for f in $WANT; do
    key=${f%%=*}
    line=$(echo "$line" | sed -E "s/(^| )${key}=[^ ]*//g")
    line="$line $f"
  done
  line=$(echo "$line" | sed -E 's/^ +//; s/ +/ /g')
  echo "$line" > "$CMDLINE"
  echo "vfio-pci" > /etc/modules-load.d/vfio.conf
  proxmox-boot-tool refresh 2>&1
  echo "@@OK systemd-boot"
}
if command -v proxmox-boot-tool >/dev/null 2>&1; then
  apply_systemd_boot
elif command -v update-grub >/dev/null 2>&1; then
  apply_grub
else
  echo "@@ERR aucun bootloader géré (ni proxmox-boot-tool ni update-grub)"
  exit 1
fi
# GARDE-FOU sysctl (CRITIQUE) : avec default_hugepagesz=1G (posé ci-dessus), `vm.nr_hugepages`
# cible les pages 1G. Un fichier résiduel d'install 2M (`vm.nr_hugepages = 2048`) serait ré-appliqué
# APRÈS le cmdline par systemd-sysctl → 2048 pages de 1G, bridé par la RAM (quasi toute la RAM gelée
# en hugepages, nœud à ~2 Go libres = faux « plein » + thrash). On réécrit donc le fichier avec le
# compte 1G correct (idempotent, écrase tout résidu 2M) ET on ajuste le runtime (baisser nr_hugepages
# réussit toujours ; monter peut échouer sur mémoire fragmentée → le reboot garantit le compte).
echo "vm.nr_hugepages = %N%" > /etc/sysctl.d/10-bobi-hugepages.conf
[ -d /sys/kernel/mm/hugepages/hugepages-1048576kB ] && \
  echo "%N%" > /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null || true
echo "@@SYSCTL %N%"
""".replace("%FLAGS%", flags_str).replace("%N%", str(n))

    rc, txt, err = run(script, timeout=60)
    if rc != 0 or "@@OK" not in txt:
        detail = (txt + " " + err).strip()
        # extrait le message d'erreur balisé si présent
        m = re.search(r"@@ERR (.+)", txt)
        if m:
            detail = m.group(1).strip()
        return False, f"échec prép : {detail[:300]}", False
    bl = "grub" if "@@OK grub" in txt else "systemd-boot"

    # Pose aussi l'unité rdma-netns-exclusive (requise pour scoper une VF mlx5 par container).
    # Best effort : un échec ici ne fait pas échouer la prép cmdline (déjà écrite).
    ok_rdma, msg_rdma, _ = ensure_rdma_netns_exclusive(host, run=run)
    rdma_note = "rdma-netns-exclusive posée" if ok_rdma else f"rdma unit: {msg_rdma}"

    # Épinglage fréquence des cœurs isolés (cf. ensure_cpufreq_performance) : posé À CHAQUE prép,
    # y compris sur un nœud sans bande isolée — le script est alors un no-op, mais l'unité est en
    # place le jour où l'isolation arrive. Best effort : n'invalide pas la prép cmdline déjà écrite.
    ok_freq, msg_freq, _ = ensure_cpufreq_performance(host, run=run)
    freq_note = f"cpufreq: {msg_freq}" if ok_freq else f"cpufreq NON épinglée: {msg_freq}"

    # Profondeur des ring buffers MXL (cf. ensure_mxl_history) : posée À CHAQUE prép, comme
    # cpufreq — best effort, n'invalide pas la prép cmdline déjà écrite.
    ok_mxlh, msg_mxlh, _ = ensure_mxl_history(host, run=run)
    mxlh_note = f"mxl history: {msg_mxlh}" if ok_mxlh else f"mxl history NON posée: {msg_mxlh}"

    # IRQ housekeeping (complément de isolcpus=managed_irq, qui ne couvre que les IRQ MANAGÉES de
    # la NIC) + neutralisation d'irqbalance. Best effort : n'invalide pas le cmdline déjà écrit.
    ok_irq, msg_irq = ensure_irq_housekeeping(host, run=run, band=plan["band"])
    if plan["band"]:
        iso_note = (f"cœurs 2110 isolés {plan['band']} "
                    f"({'HT-aware, siblings compris' if plan['ht_aware'] else 'MODÈLE PLAT'})"
                    + ("" if plan["ht_aware"] else f" ⚠ siblings HT NON isolés — {plan['note']}")
                    + " ; " + ("IRQ: " + msg_irq if ok_irq else f"IRQ housekeeping KO: {msg_irq}"))
        if not plan["ht_aware"]:
            # Repli SIGNALÉ (jamais muet) : sans les siblings, le noyau garde des IRQ/timers sur les
            # jumeaux physiques des lcores busy-poll → contention HT invisible.
            try:
                from .database import db_add_alert
                db_add_alert(
                    "alert.prep.ht_topologie_illisible", "warning", kind="prep",
                    params={"host": host, "raison": plan["note"], "band": plan["band"]})
            except Exception as e:
                log.warning("alerte repli isolation non enregistrée: %s", e)
    else:
        iso_note = "isolation cœurs 2110 désactivée (bande dégénérée)"

    sysctl_note = "sysctl hugepages réconcilié" if "@@SYSCTL" in txt else "sysctl hugepages NON réconcilié (vérifier /etc/sysctl.d/10-bobi-hugepages.conf)"
    return True, (f"prép appliquée ({bl}, {n}×1G hugepages ; {iso_note} ; {sysctl_note} ; "
                  f"{rdma_note} ; {freq_note} ; {mxlh_note}) — reboot requis"), True


# ─── Unité RDMA netns exclusive (scoping VF mlx5 par container) ───────────────

RDMA_NETNS_UNIT_PATH = "/etc/systemd/system/rdma-netns-exclusive.service"
RDMA_NETNS_UNIT = """[Unit]
Description=RDMA netns exclusive (scoping VF mlx5 par container pour MTL/DPDK)
DefaultDependencies=no
After=systemd-modules-load.service
Before=pve-guests.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/rdma system set netns exclusive
[Install]
WantedBy=multi-user.target
"""


def ensure_rdma_netns_exclusive(host, run=None):
    """Installe (idempotent) l'unité systemd qui passe le RDMA en `netns exclusive` au boot,
    requise pour déplacer le device RDMA d'une VF dans le netns d'un container (MTL/DPDK).

    Ne tente PAS `rdma system set netns exclusive` à chaud : ça échoue (EBUSY) dès qu'il y a des
    netns de containers + des QP GSI de base. On pose l'unité + on signale qu'un reboot est requis.
    Renvoie (ok, msg, reboot_needed)."""
    if not host:
        return False, "host non configuré", False
    run = run or _run_ssh(host)
    import shlex
    cmd = (
        "set -e; "
        f"cat > {RDMA_NETNS_UNIT_PATH} << 'MXLEOF'\n{RDMA_NETNS_UNIT}MXLEOF\n"
        "systemctl daemon-reload; systemctl enable rdma-netns-exclusive.service >/dev/null 2>&1; "
        # état courant : déjà exclusive ?
        "rdma system show 2>/dev/null | grep -q 'netns exclusive' && echo ACTIVE || echo PENDING"
    )
    rc, out, err = run(cmd, timeout=20)
    if rc != 0:
        return False, f"rc={rc} {err.strip()[:200]}", False
    reboot_needed = "ACTIVE" not in out
    return True, ("unité posée + enabled"
                  + (" (reboot requis pour activer)" if reboot_needed else " (déjà actif)")), reboot_needed


# ─── Épinglage de la fréquence : TOUS les cœurs, plancher sur les cœurs isolés ───────────────
# DEUX couplages DISTINCTS, que ce script traite ensemble parce qu'ils ont le même remède :
#
# 1. Cœurs ISOLÉS (DPDK) — isoler impose d'épingler. `nohz_full` rend les cœurs tickless →
#    `intel_pstate` (active/HWP) n'a plus le retour d'utilisation qui lui sert à monter la
#    fréquence → les cœurs restent collés au PLANCHER (min_perf_pct) alors qu'ils sont à 100 %
#    de busy-poll. Le moteur 2110 s'étouffe et les flux tombent au bout de quelques heures.
#    Remède : gouverneur `performance` ET `scaling_min_freq` = max (le gouverneur seul ne suffit
#    pas quand le retour d'utilisation est cassé).
#
# 2. Cœurs DÉDIÉS À UNE TÂCHE TEMPS-RÉEL (murs, traitements — cpuset posé par `core_pool`).
#    Mesuré le 2026-08-08 sur dell-1 : un mur dont le fil de compo ne consomme que 57 % d'UN
#    cœur (GIL) sur trois alloués laisse les cœurs paraître oisifs → `schedutil` les gare au
#    plancher (1,2 GHz pour 3,6 max, facteur 3). Le fil migre, atterrit sur un cœur garé, et la
#    compo tourne à demi-vitesse PENDANT DES SECONDES : effondrements à 25 fps, une à deux fois
#    par minute, avec TOUS les postes qui doublent ensemble. A/B sur 4 blocs alternés de 60 s :
#    112 → 68 trames perdues/min, et zéro effondrement sur 300 s de contrôle.
#    Remède : gouverneur `performance`. Le plancher, lui, n'est PAS forcé (le retour
#    d'utilisation fonctionne sur ces cœurs tickful — inutile d'empêcher la descente au repos,
#    qui ne coûte rien puisque les C-states restent disponibles).
#
# La leçon commune : le couplage n'est pas « cœur isolé ⇒ épingler » mais « cœur porteur d'une
# échéance temps-réel ⇒ épingler ». Comme on ne sait pas, depuis l'hôte, quels cœurs `core_pool`
# dédiera demain, on pose `performance` PARTOUT : c'est le seul réglage qui survive à un
# ré-agencement du placement sans qu'on ait à le re-poser.
#
# AUCUNE FRÉQUENCE EN DUR : la cible de chaque cœur est SON PROPRE `cpuinfo_max_freq`, lu sur
# la machine. Le script s'adapte donc à n'importe quel CPU (et aux cœurs hétérogènes).
# AUCUNE BANDE EN DUR non plus : la liste des isolés vient de /sys/devices/system/cpu/isolated,
# donc elle suit automatiquement le cmdline ; celle des cœurs tout court vient de /sys.
CPUFREQ_SCRIPT_PATH = "/usr/local/sbin/bobi-cpufreq-perf.sh"
CPUFREQ_SCRIPT = """#!/bin/sh
# BOBI — gouverneur `performance` sur TOUS les coeurs + plancher au max sur les coeurs ISOLES.
# Genere par app/mtl.py (ensure_cpufreq_performance) — ne pas editer a la main.
set -u
ISO=$(cat /sys/devices/system/cpu/isolated 2>/dev/null || true)

# Bande isolee -> liste plate, pour tester l'appartenance cœur par cœur.
iso_list=""
for p in $(echo "$ISO" | tr ',' ' '); do
  lo=$(echo "$p" | cut -d- -f1); hi=$(echo "$p" | cut -d- -f2)
  [ -n "$lo" ] || continue
  i=$lo
  while [ "$i" -le "$hi" ] 2>/dev/null; do
    iso_list="$iso_list $i"
    i=$((i+1))
  done
done

rc=0; n=0; np=0; nofreq=0
for d in /sys/devices/system/cpu/cpu*/cpufreq; do
  [ -d "$d" ] || continue
  cpu=$(echo "$d" | sed 's|.*/cpu\\([0-9]*\\)/cpufreq|\\1|')
  n=$((n+1))
  echo performance > "$d/scaling_governor" 2>/dev/null || rc=1
  # Plancher force UNIQUEMENT sur les coeurs isoles : la ou le retour d'utilisation est casse.
  for k in $iso_list; do
    if [ "$k" = "$cpu" ]; then
      max=$(cat "$d/cpuinfo_max_freq" 2>/dev/null || true)
      if [ -n "$max" ]; then
        echo "$max" > "$d/scaling_min_freq" 2>/dev/null || rc=1
        np=$((np+1))
      else
        rc=1
      fi
      break
    fi
  done
done
if [ "$n" = 0 ]; then
  echo "bobi-cpufreq: aucune interface cpufreq (pilote absent ?) -> rien a faire" >&2
  nofreq=1
fi
echo "bobi-cpufreq: $n coeur(s) en gouverneur performance ; $np coeur(s) isole(s) plancher au max (bande [$ISO])"
[ "$nofreq" = 1 ] && exit 1
exit $rc
"""

CPUFREQ_UNIT_PATH = "/etc/systemd/system/bobi-cpufreq-perf.service"
CPUFREQ_UNIT = """[Unit]
Description=BOBI epingle la frequence CPU (performance partout, plancher au max sur les coeurs isoles)
DefaultDependencies=no
After=sysinit.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/bobi-cpufreq-perf.sh

[Install]
WantedBy=multi-user.target
"""


def ensure_cpufreq_performance(host, run=None):
    """Installe (idempotent) le script + l'unité systemd qui épinglent la fréquence des cœurs
    ISOLÉS au maximum du CPU présent, PUIS les applique à chaud (`systemctl start`).

    Contrairement au cmdline, le gouverneur se pose à chaud : pas de reboot requis, et l'unité
    garantit la persistance au prochain boot (sans elle, un simple reboot réintroduit le bug).
    Un échec PARTIEL (un cœur sans interface cpufreq) fait échouer l'unité VOLONTAIREMENT : on
    veut que la perte du pin soit visible, pas silencieuse. Renvoie (ok, msg)."""
    if not host:
        return False, "host non configuré", False
    run = run or _run_ssh(host)
    cmd = (
        "set -e; "
        f"cat > {CPUFREQ_SCRIPT_PATH} << 'MXLEOF'\n{CPUFREQ_SCRIPT}MXLEOF\n"
        f"chmod 0755 {CPUFREQ_SCRIPT_PATH}; "
        f"cat > {CPUFREQ_UNIT_PATH} << 'MXLEOF'\n{CPUFREQ_UNIT}MXLEOF\n"
        "systemctl daemon-reload; "
        "systemctl enable bobi-cpufreq-perf.service >/dev/null 2>&1; "
        # Application à chaud : on relaie la sortie du script (nb de cœurs, ou la raison du no-op).
        "systemctl restart bobi-cpufreq-perf.service 2>&1 || echo '@@UNITFAIL'; "
        f"{CPUFREQ_SCRIPT_PATH} 2>&1 || true"
    )
    rc, out, err = run(cmd, timeout=30)
    if rc != 0:
        return False, f"rc={rc} {(err or out).strip()[:200]}", False
    txt = (out or "").strip()
    if "@@UNITFAIL" in txt:
        return False, f"unité posée mais échec au démarrage : {txt.replace('@@UNITFAIL', '').strip()[:200]}", False
    detail = txt.splitlines()[-1].strip() if txt else "posée"
    return True, f"unité posée + enabled ; {detail[:160]}", False


# ─── IRQ housekeeping : déporte les IRQ hors des cœurs isolés du moteur 2110 ──────────────────
# `isolcpus=managed_irq` ne couvre QUE les IRQ MANAGÉES (multiqueue NIC ice/E810). Toutes les
# autres (disque, USB, iLO, mei, timers matériels…) restent affinées « partout », donc AUSSI sur
# la bande isolée. Cette unité les repose au boot sur le COMPLÉMENT (housekeeping).
#
# La logique vit dans un SCRIPT (comme bobi-cpufreq-perf.sh), pas dans un ExecStart en une ligne :
#   1. le complément se calcule sur une cpulist MULTI-PLAGES (`1-18,49-66` dès que l'HT est actif).
#      L'ancien `LO=${ISO%%-*}; HI=${ISO##*-}` ne voyait qu'UNE plage contiguë → complément FAUX
#      (il aurait rendu 0,67-95 en laissant 19-48 hors housekeeping) ;
#   2. `%` est un spécificateur systemd dans un fichier d'unité — à éviter dans un ExecStart ;
#   3. c'est lisible et testable à la main sur le nœud.
IRQ_SCRIPT_PATH = "/usr/local/sbin/bobi-irq-housekeeping.sh"
IRQ_SCRIPT = """#!/bin/sh
# BOBI — repose les IRQ NON managees hors des coeurs ISOLES du moteur 2110.
# Genere par app/mtl.py (ensure_irq_housekeeping) — ne pas editer a la main.
set -u
ISO=$(cat /sys/devices/system/cpu/isolated 2>/dev/null || true)
if [ -z "$ISO" ]; then
  echo "bobi-irq: aucun coeur isole -> rien a faire"
  exit 0
fi
N=$(nproc --all 2>/dev/null || echo 0)
if [ "$N" -le 0 ]; then
  echo "bobi-irq: nombre de CPU illisible (nproc) -> abandon" >&2
  exit 1
fi
# Expansion COMPLETE de la cpulist : plusieurs plages separees par des virgules ('1-18,49-66'),
# elements isoles ('3') compris. Un simple premier/dernier serait FAUX des que l'HT est actif.
ISOL=""
for p in $(echo "$ISO" | tr ',' ' '); do
  lo=$(echo "$p" | cut -d- -f1)
  hi=$(echo "$p" | cut -d- -f2)
  [ -n "$hi" ] || hi=$lo
  i=$lo
  while [ "$i" -le "$hi" ] 2>/dev/null; do
    ISOL="$ISOL $i"
    i=$((i+1))
  done
done
HK=""
i=0
while [ "$i" -lt "$N" ]; do
  keep=1
  for x in $ISOL; do
    if [ "$x" = "$i" ]; then keep=0; break; fi
  done
  if [ "$keep" = "1" ]; then
    if [ -z "$HK" ]; then HK="$i"; else HK="$HK,$i"; fi
  fi
  i=$((i+1))
done
if [ -z "$HK" ]; then
  echo "bobi-irq: complement housekeeping VIDE (la bande isolee couvre TOUS les CPU) -> abandon" >&2
  exit 1
fi
rc=0
# irqbalance re-etale les IRQ sur TOUS les CPU quelques minutes apres le boot : il defait ce
# travail EN SILENCE, longtemps apres. La prep MTL le masque ; s'il revient, on echoue BRUYAMMENT.
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet irqbalance 2>/dev/null; then
  echo "bobi-irq: irqbalance est ACTIF -> il va re-etaler les IRQ sur la bande isolee $ISO. Le masquer: systemctl mask --now irqbalance" >&2
  rc=1
fi
n=0; fixes=0
for f in /proc/irq/[0-9]*/smp_affinity_list; do
  [ -e "$f" ] || continue
  if echo "$HK" > "$f" 2>/dev/null; then
    n=$((n+1))
  else
    fixes=$((fixes+1))   # IRQ managees/per-cpu: non deplacables (attendu, pas une erreur)
  fi
done
echo "bobi-irq: $n IRQ posees sur le housekeeping $HK (bande isolee $ISO) ; $fixes IRQ non deplacables (managees/per-cpu)"
exit $rc
"""

IRQ_UNIT_PATH = "/etc/systemd/system/bobi-irq-housekeeping.service"
IRQ_UNIT = """[Unit]
Description=BOBI IRQ housekeeping (deporte les IRQ hors des coeurs isoles du moteur 2110)
DefaultDependencies=no
After=sysinit.target
Before=docker.service irqbalance.service
ConditionPathExists=/sys/devices/system/cpu/isolated

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/bobi-irq-housekeeping.sh

[Install]
WantedBy=multi-user.target
"""


def ensure_irq_housekeeping(host, run=None, band=None):
    """Installe (idempotent) le script + l'unité systemd qui reposent les IRQ non managées sur le
    COMPLÉMENT de la bande isolée, MASQUE irqbalance (s'il est présent et qu'une bande est prévue),
    puis applique à chaud. Renvoie (ok, msg).

    Le masquage d'irqbalance n'est pas cosmétique : installé et actif, il redistribue les IRQ sur
    tous les CPU quelques minutes après le boot → l'isolation se dégrade toute seule, longtemps
    après l'application, sans que rien ne le signale."""
    if not host:
        return False, "host non configuré"
    run = run or _run_ssh(host)
    masque = bool(band)      # pas de bande prévue → on ne touche pas au service de l'exploitant
    cmd = (
        "set -e; "
        f"cat > {IRQ_SCRIPT_PATH} << 'MXLEOF'\n{IRQ_SCRIPT}MXLEOF\n"
        f"chmod 0755 {IRQ_SCRIPT_PATH}; "
        f"cat > {IRQ_UNIT_PATH} << 'MXLEOF'\n{IRQ_UNIT}MXLEOF\n"
        "systemctl daemon-reload; "
        "systemctl enable bobi-irq-housekeeping.service >/dev/null 2>&1; "
    )
    if masque:
        cmd += (
            "if [ -n \"$(systemctl list-unit-files --no-legend irqbalance.service 2>/dev/null)\" ]; then "
            "  systemctl mask --now irqbalance >/dev/null 2>&1 && echo '@@IRQBAL masqué' "
            "    || echo '@@IRQBAL ÉCHEC du masquage (il ré-étalera les IRQ)'; "
            "else echo '@@IRQBAL absent'; fi; "
        )
    cmd += (
        # À chaud : no-op tant que le cmdline n'est pas actif (isolated vide) ; utile après reboot.
        "systemctl restart bobi-irq-housekeeping.service >/dev/null 2>&1 || echo '@@UNITFAIL'; "
        f"{IRQ_SCRIPT_PATH} 2>&1 || true"
    )
    rc, out, err = run(cmd, timeout=30)
    if rc != 0:
        return False, f"rc={rc} {(err or out).strip()[:200]}"
    txt = (out or "").strip()
    m = re.search(r"@@IRQBAL (.+)", txt)
    bal = f" ; irqbalance {m.group(1).strip()}" if m else ""
    detail = [l for l in txt.splitlines() if not l.startswith("@@")]
    detail = detail[-1].strip() if detail else "posée"
    if "@@UNITFAIL" in txt:
        return False, f"unité posée mais échec au démarrage : {detail[:200]}{bal}"
    return True, f"unité posée + enabled{bal} ; {detail[:200]}"


# ─── Profondeur des ring buffers MXL (réglage DE NŒUD, pas par-conteneur) ─────────────────────
# Vérifié le 2026-08-09 dans les sources du SDK MXL v1.1.0-beta-1 (lib/internal/src/Instance.cpp,
# lib/internal/src/PathUtils.cpp, docs/Configuration.md) : la profondeur d'un ring buffer MXL
# n'est PAS un nombre de trames, c'est une DURÉE — `urn:x-mxl:option:history_duration/v1.0`, en
# NANOSECONDES (défaut SDK 200_000_000 = 200 ms). `grainCount = historyDuration * grain_rate`
# (10 cases à 50 fps, 5 à 25 fps) ; l'audio reçoit le DOUBLE de la durée (une moitié seule du
# tampon est lisible à la fois). Le SDK REFUSE délibérément qu'elle soit posée par instance
# (commentaire du SDK : "we don't want per-instance history durations") : la seule prise en
# compte est un fichier `options.json` À LA RACINE DU DOMAINE MXL (`/dev/shm/mxl/options.json`).
# C'est donc un réglage DE NŒUD, partagé par TOUS les conteneurs du nœud — pas un choix qu'on
# pourrait faire par plugin/container. `/dev/shm` est un tmpfs : le fichier est perdu au reboot,
# d'où l'unité systemd ci-dessous (même schéma que bobi-cpufreq-perf.service) pour le reposer
# AVANT que docker (et donc les containers MXL) ne démarre. ⚠ Ne prend effet que pour les flux
# CRÉÉS ENSUITE : un flux existant garde la profondeur qu'il avait à sa création — changer ce
# réglage exige de recréer les flux (redéployer les producteurs) pour en bénéficier.
# ⚠ Le SDK règle la profondeur PAR DOMAINE, pas par nœud. Si on parle de « réglage de nœud »,
# c'est une HYPOTHÈSE DE NOTRE DÉPLOIEMENT : un nœud = un seul domaine, celui par défaut. Vérifié
# le 2026-08-09 — aucun conteneur de la flotte ne pose `MXL_DOMAIN`, et l'orchestrateur ne
# l'injecte nulle part, donc tous partagent `/dev/shm/mxl`. Le jour où un conteneur pointerait un
# autre domaine (variable `MXL_DOMAIN`, cf. `bobimxl.DEFAULT_DOMAIN`), il aurait son propre
# `options.json` — absent, donc 200 ms par défaut — INVISIBLE pour cette fonction comme pour la
# sonde de dérive de `node_health`, toutes deux codées sur ce chemin. Ce serait un angle mort
# silencieux : il faudrait alors énumérer les domaines au lieu d'en supposer un.
MXL_DOMAIN_DIR          = "/dev/shm/mxl"
MXL_OPTIONS_PATH        = f"{MXL_DOMAIN_DIR}/options.json"
MXL_HISTORY_OPTION_KEY  = "urn:x-mxl:option:history_duration/v1.0"
MXL_HISTORY_MS_DEFAULT  = 200

# ★ POURQUOI `tmpfiles.d` ET PAS UNE UNITÉ SYSTEMD (leçon payée le 2026-08-09).
# La première version posait une unité oneshot `bobi-mxl-options.service`. Installer une unité
# EXIGE un `systemctl daemon-reload` — lequel RÉVOQUE l'accès aux périphériques GPU des conteneurs
# DÉJÀ LANCÉS. Constaté le jour même sur dell-1 : la pose du fichier sur ce nœud a cassé NVML dans
# le conteneur du mur 906 (« Failed to initialize NVML: Unknown Error »), panne à retardement
# invisible tant que le process garde son contexte CUDA. Rendre le reload conditionnel ne suffit
# PAS : la toute première installation sur un nœud GPU le déclenche forcément.
# `systemd-tmpfiles` est l'outil prévu exactement pour ça — créer des fichiers dans un tmpfs au
# boot — et une config `tmpfiles.d` se dépose SANS aucun reload. `systemd-tmpfiles-setup.service`
# tourne bien avant docker. Cesse de valoir si systemd corrige la révocation de périphériques au
# reload, ou si le domaine MXL quitte un tmpfs.
MXL_TMPFILES_PATH = "/etc/tmpfiles.d/bobi-mxl.conf"

# Ancienne unité, désinstallée si on la trouve (cf. commentaire ci-dessus).
MXL_OPTIONS_UNIT_PATH = "/etc/systemd/system/bobi-mxl-options.service"
def ensure_mxl_history(host, run=None, duree_ms=None):
    """Installe (idempotent) `/dev/shm/mxl/options.json` (profondeur des ring buffers MXL) +
    l'unité systemd qui le repose au boot (tmpfs perdu au reboot, AVANT docker).

    RÉGLAGE DE NŒUD, PAS PAR-CONTENEUR : le SDK MXL ignore délibérément la durée passée à
    `mxlCreateInstance` et ne lit QUE ce fichier, à la racine du domaine (cf. commentaire ci-
    dessus) — tous les conteneurs du nœud partagent la même profondeur. `duree_ms` ou, à défaut,
    le réglage `mxl_history_ms` (défaut 200 ms si absent) ; converti en nanosecondes pour le SDK.

    ⚠ Ne s'applique qu'aux flux CRÉÉS APRÈS ce changement : un flux déjà ouvert garde sa
    profondeur d'origine — il faut recréer les flux (redéployer les producteurs) pour en
    bénéficier. Best-effort, jamais bloquant : un nœud injoignable ne fait pas échouer l'appelant.
    Renvoie (ok, msg, reboot_needed) — reboot_needed est toujours False : le fichier est écrit
    directement dans le tmpfs (effectif tout de suite pour les flux créés ensuite), la copie
    persistante + l'unité ne servent qu'à survivre à un futur reboot."""
    if not host:
        return False, "host non configuré", False
    if duree_ms is None:
        from . import settings
        duree_ms = settings.get("mxl_history_ms", MXL_HISTORY_MS_DEFAULT)
    try:
        duree_ms = int(duree_ms)
    except (TypeError, ValueError):
        duree_ms = MXL_HISTORY_MS_DEFAULT
    if duree_ms <= 0:
        duree_ms = MXL_HISTORY_MS_DEFAULT
    duree_ns = duree_ms * 1_000_000
    content = json.dumps({MXL_HISTORY_OPTION_KEY: duree_ns})
    run = run or _run_ssh(host)
    # `f` de tmpfiles.d : crée le fichier avec l'ARGUMENT comme contenu (une seule ligne — notre
    # JSON n'en contient pas). `d` garantit le répertoire du domaine. Les `%` sont doublés :
    # tmpfiles.d les interprète comme des spécificateurs.
    tmpf = (f"d {MXL_DOMAIN_DIR} 0755 root root -\n"
            f"f {MXL_OPTIONS_PATH} 0644 root root - {content.replace('%', '%%')}\n")
    cmd = (
        "set -e; "
        f"mkdir -p {MXL_DOMAIN_DIR} /etc/tmpfiles.d; "
        # Idempotent : ne réécrit rien si le contenu voulu est déjà là — évite de perturber un
        # domaine en place à chaque tour de prép.
        f"want='{content}'; "
        f"cur=$(cat {MXL_OPTIONS_PATH} 2>/dev/null || true); "
        f"if [ \"$cur\" != \"$want\" ]; then printf '%s' \"$want\" > {MXL_OPTIONS_PATH}; echo '@@WRITTEN'; fi; "
        # Persistance au boot : une config tmpfiles.d, déposée SANS `daemon-reload` (cf. le
        # commentaire de MXL_TMPFILES_PATH — le reload casse le GPU des conteneurs en marche).
        f"cat > /tmp/.mxl-tmpf.new << 'MXLEOF'\n{tmpf}MXLEOF\n"
        f"if ! cmp -s /tmp/.mxl-tmpf.new {MXL_TMPFILES_PATH} 2>/dev/null; then "
        f"  mv /tmp/.mxl-tmpf.new {MXL_TMPFILES_PATH}; echo '@@TMPF'; "
        f"else rm -f /tmp/.mxl-tmpf.new; fi; "
        # Ménage de l'ancienne unité si elle traîne. `disable` seul suffit à la neutraliser ; on
        # NE fait PAS de `daemon-reload` ici non plus — une unité orpheline est inoffensive, une
        # révocation GPU ne l'est pas.
        f"if [ -f {MXL_OPTIONS_UNIT_PATH} ]; then "
        "   systemctl disable --now bobi-mxl-options.service >/dev/null 2>&1 || true; "
        f"  rm -f {MXL_OPTIONS_UNIT_PATH}; echo '@@UNIT_RETIREE'; fi; "
        "echo '@@OK'"
    )
    rc, out, err = run(cmd, timeout=20)
    if rc != 0 or "@@OK" not in (out or ""):
        return False, f"rc={rc} {(err or out).strip()[:200]}", False
    ecrit = "@@WRITTEN" in (out or "")
    tmpf_pose = "@@TMPF" in (out or "")
    unite_off = "@@UNIT_RETIREE" in (out or "")
    detail = f"options.json {'écrit' if ecrit else 'déjà à jour'} ({duree_ms} ms)"
    detail += f" ; tmpfiles.d {'posé' if tmpf_pose else 'inchangé'}"
    if unite_off:
        detail += " ; ancienne unité systemd retirée"
    return True, detail, False


# ─── Identité du domaine MXL : `domain_def.json` (BCP-007-03) ────────────────────────────────
# AMWA BCP-007-03 « NMOS With MXL » (relevé sur `v1.0-dev`, commit 5ed4eb6 du 2026-08-05 — aucune
# release à ce jour) impose : « All MXL Domains MUST hold a definition json file `domain_def.json`
# in their host directory ». Schéma normatif `APIs/schemas/mxl_domain_definition.json` : QUATRE
# champs REQUIS — `id` (UUID canonique minuscule), `label`, `description`, `tags` (objet
# clé → tableau de chaînes, peut être vide) ; `additionalProperties: true`.
#
# POURQUOI ça nous revient, et pourquoi on le fait MAINTENANT alors que le reste de la BCP est
# différé (cf. TODO.md § BCP-007-03) : le SDK MXL LIT ce fichier (`tools/mxl-info` le parse et
# l'affiche) mais rien dans la lib ne l'ÉCRIT — la BCP pose que c'est l'orchestrateur qui
# configure l'emplacement du domaine, donc qui le nomme. Et sa valeur ne dépend pas de la BCP :
# elle règle l'identité de domaine dont on a besoin de toute façon pour la réplication RDMA
# inter-nœuds, où « le même bus » vu de deux nœuds n'a aujourd'hui aucun nom commun.
#
# Le problème exact que ça règle : le même domaine bind-monté sous deux chemins différents selon
# le conteneur (`/dev/shm/mxl` chez l'un, `/domain_a` chez l'autre) est le MÊME bus, et le chemin
# ne le dit pas. L'identité voyage donc DANS le domaine, pas dans son adresse.
MXL_DOMAIN_DEF_PATH      = f"{MXL_DOMAIN_DIR}/domain_def.json"
# Copie persistante hors tmpfs (même dossier que VFIO_BINDS_PATH) : c'est ELLE que tmpfiles.d
# recopie dans le domaine au boot. On ne met PAS le JSON en argument d'une ligne `f` de
# tmpfiles.d (contrairement à options.json) : cet argument subit le déséchappement C de systemd
# et l'expansion des spécificateurs, ce qui mutilerait un label contenant `\` ou `%`. Le type
# `C` recopie un fichier tel quel — aucune règle d'échappement à respecter.
MXL_DOMAIN_DEF_SRC       = "/etc/bobi/mxl-domain_def.json"
MXL_DOMAIN_TMPFILES_PATH = "/etc/tmpfiles.d/bobi-mxl-domain.conf"


def build_domain_def(domain_id, label, description=None, tags=None):
    """Construit le contenu de `domain_def.json` (BCP-007-03). Fonction PURE, testable sans nœud.

    Les quatre champs du schéma sont TOUS requis, `tags` compris (objet vide accepté) : un
    `domain_def.json` amputé d'un champ est invalide, et `mxl-info` affiche alors
    « -- Required field missing -- » plutôt que de le signaler comme une erreur."""
    return json.dumps({
        "id":          str(domain_id),
        "label":       label,
        "description": description or f"Domaine MXL du nœud {label} (Bobi.Studio)",
        # Le schéma impose des valeurs en TABLEAU de chaînes. Le nom du nœud est déjà dans
        # `label` ; on n'y remet donc que ce qu'un tiers ne peut pas déduire du reste.
        "tags":        tags if isinstance(tags, dict) else {"urn:x-bobi:orchestrator": ["bobi.studio"]},
    }, ensure_ascii=False)


def ensure_mxl_domain_def(node, run=None):
    """Pose (idempotent) `/dev/shm/mxl/domain_def.json` sur un nœud + sa persistance au boot.

    L'UUID vient de la DB (`db_node_mxl_domain_id`, créé au premier appel) et n'en bouge plus ;
    `label`/`description` suivent le nom du nœud et sont donc réécrits si on le renomme —
    l'identité, elle, ne change pas, c'est tout l'intérêt.

    Persistance : `/dev/shm` est un tmpfs, le fichier meurt au reboot. Même dispositif que
    `ensure_mxl_history` et pour la MÊME raison (cf. le commentaire de MXL_TMPFILES_PATH) : une
    config `tmpfiles.d`, JAMAIS une unité systemd — installer une unité exige un `daemon-reload`,
    lequel révoque l'accès GPU des conteneurs DÉJÀ lancés.

    ⚠ La ligne tmpfiles est de type `C` (copie si absent), pas `f` : elle ne réécrase donc PAS un
    `domain_def.json` déjà présent au boot. C'est voulu — le fichier vivant fait autorité pour les
    conteneurs qui tournent, et c'est cet appel-ci (pas le boot) qui le met à jour.

    Best-effort, jamais bloquant. Renvoie (ok, msg)."""
    if not node:
        return False, "nœud inconnu"
    from .database import db_node_mxl_domain_id
    nid = node.get("id")
    domain_id = db_node_mxl_domain_id(nid) if nid else None
    if not domain_id:
        return False, "identité de domaine indisponible (nœud absent de la base)"
    label = node.get("name") or node.get("host") or f"node-{nid}"
    content = build_domain_def(domain_id, label)
    tmpf = (f"d {MXL_DOMAIN_DIR} 0755 root root -\n"
            f"C {MXL_DOMAIN_DEF_PATH} 0644 root root - {MXL_DOMAIN_DEF_SRC}\n")
    run = run or _run_agent(node)
    cmd = (
        "set -e; "
        f"mkdir -p {MXL_DOMAIN_DIR} /etc/bobi /etc/tmpfiles.d; "
        # Heredoc quoté : le shell ne touche à rien du JSON.
        f"cat > /tmp/.mxl-domaindef.new << 'MXLEOF'\n{content}\nMXLEOF\n"
        # Idempotent des deux côtés : on ne réécrit que ce qui diffère, pour ne pas remuer un
        # domaine en place à chaque tour de sonde.
        f"if ! cmp -s /tmp/.mxl-domaindef.new {MXL_DOMAIN_DEF_SRC} 2>/dev/null; then "
        f"  cp /tmp/.mxl-domaindef.new {MXL_DOMAIN_DEF_SRC}; chmod 0644 {MXL_DOMAIN_DEF_SRC}; echo '@@SRC'; fi; "
        f"if ! cmp -s /tmp/.mxl-domaindef.new {MXL_DOMAIN_DEF_PATH} 2>/dev/null; then "
        f"  cp /tmp/.mxl-domaindef.new {MXL_DOMAIN_DEF_PATH}; chmod 0644 {MXL_DOMAIN_DEF_PATH}; echo '@@LIVE'; fi; "
        "rm -f /tmp/.mxl-domaindef.new; "
        f"cat > /tmp/.mxl-domaintmpf.new << 'MXLEOF'\n{tmpf}MXLEOF\n"
        f"if ! cmp -s /tmp/.mxl-domaintmpf.new {MXL_DOMAIN_TMPFILES_PATH} 2>/dev/null; then "
        f"  mv /tmp/.mxl-domaintmpf.new {MXL_DOMAIN_TMPFILES_PATH}; echo '@@TMPF'; "
        f"else rm -f /tmp/.mxl-domaintmpf.new; fi; "
        "echo '@@OK'"
    )
    rc, out, err = run(cmd, timeout=20)
    if rc != 0 or "@@OK" not in (out or ""):
        return False, f"rc={rc} {(err or out or '').strip()[:200]}"
    live = "@@LIVE" in (out or "")
    src = "@@SRC" in (out or "")
    tmpf_pose = "@@TMPF" in (out or "")
    detail = f"domain_def.json {'écrit' if live else 'déjà à jour'} (id {domain_id})"
    if src or tmpf_pose:
        detail += " ; persistance " + ", ".join(
            x for x in (("copie /etc/bobi" if src else ""), ("tmpfiles.d" if tmpf_pose else "")) if x)
    # C'EST ICI que la trace doit vivre, pas chez l'appelant (leçon payée le 2026-08-15). La
    # première version ne journalisait que depuis `node_health` : une pose faite par un AUTRE
    # chemin — la prép hôte, ou un appel direct à la main — écrivait trois fichiers sur un hôte
    # sans laisser la moindre ligne, et l'enquête sur « qui a écrit ce fichier ? » n'avait aucune
    # prise. Une fonction qui mute l'hôte se signale elle-même : elle couvre alors TOUS ses
    # appelants, présents et futurs. On ne journalise que l'ÉCRITURE — le cas « déjà à jour »
    # passe toutes les 30 min par nœud et n'apprend rien.
    if live or src or tmpf_pose:
        log.info("domaine MXL %s : %s", label, detail)
    return True, detail


# ─── Binding vfio-pci d'un port média (chantier DPDK, Lot A) ──────────────────
# Plans PURS (aucun accès hôte) : `vfio_bind_plan`/`vfio_unbind_plan` rendent le script shell
# idempotent complet + une liste de checks lisibles ; `*_apply` l'exécutent via le runner
# injecté (défaut : agent-nœud). Persistance au boot : fichier /etc/bobi/vfio-binds (un BDF
# par ligne) rejoué par une unité systemd oneshot unique (plus simple/robuste qu'une unité
# templatée : les BDF contiennent des caractères que systemd-escape mutile).

VFIO_BINDS_PATH     = "/etc/bobi/vfio-binds"
VFIO_BIND_UNIT      = "bobi-vfio-bind.service"
VFIO_BIND_UNIT_PATH = f"/etc/systemd/system/{VFIO_BIND_UNIT}"

# Driver kernel de retour au rollback (E810). Le plan unbind vérifie qu'on y revient.
VFIO_KERNEL_DRIVER = "ice"

# Activation SANS `systemctl enable` : le lien que `enable` poserait, posé à la main.
#
# ★★★ CAUSE RACINE D'UNE PANNE DE 9 JOURS (Horace, trouvée le 2026-08-28). Ce plan écrivait
# l'unité INCONDITIONNELLEMENT puis faisait `systemctl daemon-reload` — donc à CHAQUE déploiement
# de moteur, même quand le fichier était identique. Or un `daemon-reload` réapplique la politique
# de périphériques des cgroups et RETIRE l'accès GPU aux conteneurs DÉJÀ EN MARCHE (les nœuds
# `/dev/nvidia*` restent visibles, seule l'autorisation disparaît : `open()` rend EPERM, NVML dit
# « Unknown Error »). Le 2026-08-19 à 18:50, un redémarrage du moteur 2110 a ainsi révoqué la carte
# du conteneur de monitoring d'un utilisateur ; son ffmpeg est mort en `-22` et a été relancé en
# boucle pendant NEUF JOURS. Les conteneurs recréés APRÈS (18:57) n'ont rien eu.
#
# ⚠ CE QUI A RENDU LE DIAGNOSTIC SI LONG : `daemon-reload` **n'est pas journalisé** sur ce parc
# (systemd 257) — vérifié sur un nœud ayant 9 472 entrées de journal sur l'heure d'un reload
# documenté, et zéro trace. L'absence au journal ne prouve donc RIEN, et j'en avais conclu à tort
# qu'aucun reload n'avait eu lieu. Ne jamais réutiliser cet argument.
#
# La parade est la même que pour `options.json` du domaine MXL : **ne pas recharger**. Cette unité
# est un `oneshot` qui ne sert QU'AU BOOT — systemd relit tout à ce moment-là, l'état en mémoire
# n'a donc pas besoin d'être à jour. On écrit le fichier seulement s'il DIFFÈRE, et on pose le lien
# d'activation seulement s'il MANQUE. Un `systemctl enable` est proscrit ici : il avertit sur unité
# inconnue et, selon les versions, recharge.
VFIO_BIND_WANTS_PATH = "/etc/systemd/system/multi-user.target.wants/" + VFIO_BIND_UNIT

_VFIO_BIND_UNIT_TEXT = f"""[Unit]
Description=MXL vfio-pci bind (ports média DPDK — {VFIO_BINDS_PATH})
DefaultDependencies=no
After=systemd-modules-load.service local-fs.target
Before=network-pre.target docker.service
ConditionPathExists={VFIO_BINDS_PATH}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'modprobe vfio-pci; while read -r bdf; do [ -n "$bdf" ] || continue; d=/sys/bus/pci/devices/$bdf; [ -e "$d" ] || continue; cur=$(basename "$(readlink "$d/driver" 2>/dev/null)" 2>/dev/null); [ "$cur" = vfio-pci ] && continue; echo vfio-pci > "$d/driver_override"; [ -n "$cur" ] && echo "$bdf" > "/sys/bus/pci/drivers/$cur/unbind"; echo "$bdf" > /sys/bus/pci/drivers_probe; done < {VFIO_BINDS_PATH}'

[Install]
WantedBy=multi-user.target
"""


class GardeFouVfio(Exception):
    """Refus explicite d'un plan vfio (NIC PTP, NIC de management, BDF invalide)."""


_BDF_RE = re.compile(r"^(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def _normaliser_bdf(bdf):
    """BDF PCI canonique `0000:12:00.0` (minuscules, domaine préfixé). GardeFouVfio si invalide."""
    b = (bdf or "").strip().lower()
    if not _BDF_RE.match(b):
        raise GardeFouVfio(f"BDF PCI invalide : {bdf!r} (attendu dddd:bb:dd.f)")
    if len(b.split(":")) == 2:
        b = "0000:" + b
    return b


def _iface_pour_bdf(node, bdf):
    """(iface, toutes) : la ligne node_interfaces dont `pci` == bdf (normalisé), ou None."""
    from .database import db_get_node_interfaces
    rows = db_get_node_interfaces((node or {}).get("id")) or []
    for r in rows:
        pci = (r.get("pci") or "").strip().lower()
        if pci and (pci == bdf or ("0000:" + pci) == bdf):
            return r, rows
    return None, rows


def _vfio_gardefous(node, bdf):
    """Garde-fous du plan bind : refuse (GardeFouVfio) une NIC PTP ou de management ;
    warnings texte pour un pair 2022-7 laissé en kernel ou un BDF inconnu de node_interfaces.
    Renvoie (iface|None, checks:[str])."""
    checks = []
    iface, rows = _iface_pour_bdf(node, bdf)
    if iface is None:
        checks.append(f"warning: BDF {bdf} absent de node_interfaces — garde-fous "
                      "(PTP/management/paire 2022-7) non vérifiables")
        return None, checks
    ifn = iface.get("ifname") or "?"
    if iface.get("ptp_enabled"):
        # ⚠ Sens exact du refus : « ptp4l ne peut pas tourner sur une carte en vfio-pci » — PAS
        # « cette carte n'aura plus de PTP ». En DPDK l'horloge PTP existe toujours : elle est portée
        # par le moteur 2110 (libmtl, client PTP interne). Il faut donc RETIRER ptp4l de cette carte
        # (Réglages → Réseau → interface, section « Horloge PTP »), pas déplacer « le PTP ».
        raise GardeFouVfio(
            f"refus : {ifn} ({bdf}) porte ptp_enabled=1 (ptp4l tourne sur cette carte) — en vfio-pci "
            "il n'y a plus de netdev noyau ni de PHC, ptp4l ne peut pas y tourner. En DPDK, l'horloge "
            "PTP est portée par le moteur 2110 (libmtl) : retirer « ptp4l sur cette carte » dans "
            "Réglages → Réseau (ou porter ptp4l sur une autre carte du réseau).")
    from .database import role_is_management as _role_is_mgmt
    if _role_is_mgmt(iface.get("role")):
        raise GardeFouVfio(
            f"refus : {ifn} ({bdf}) est la NIC de management du nœud (rôle "
            f"{iface.get('role')}) — la passer en vfio-pci couperait le plan de "
            "contrôle (agent-nœud injoignable).")
    checks.append(f"ok: {ifn} ({bdf}) role={iface.get('role') or 'unused'}, "
                  f"ptp_enabled=0 — éligible vfio-pci")
    # Une PAIRE exige groupe ET rôle des deux côtés (même règle que docker_driver.media_port_pairs).
    # Tester `pair_group` seul faisait annoncer une « paire 2022-7 incohérente » à propos d'une paire
    # qui n'existe pas — dl360-1 portait un groupe sans aucun rôle.
    pg = iface.get("pair_group")
    if pg is not None and iface.get("pair_role") in ("red", "blue"):
        pair = next((r for r in rows
                     if r.get("pair_group") == pg and r.get("ifname") != iface.get("ifname")
                     and r.get("pair_role") in ("red", "blue")), None)
        if pair is not None and (pair.get("pmd") or "af_xdp") != "dpdk":
            checks.append(
                f"warning: pair 2022-7 {pair.get('ifname')} (pair_group {pg}) reste en kernel "
                "— paire red/blue temporairement incohérente (attendu pendant le banc)")
    return iface, checks


def vfio_bind_plan(node, bdf):
    """Plan PUR de binding vfio-pci du port `bdf` (E810) : renvoie (script_text, checks).

    Le script est idempotent : `readlink …/driver` d'abord (déjà vfio-pci → skip),
    `modprobe vfio-pci`, `driver_override` sysfs, unbind du driver courant (ice), bind via
    `drivers_probe` + vérification, puis persistance au boot ({VFIO_BINDS_PATH} + unité
    oneshot {VFIO_BIND_UNIT}). Aucune exécution ici — voir `vfio_bind_apply`.
    GardeFouVfio si la NIC est PTP (`ptp_enabled=1`) ou de management."""
    b = _normaliser_bdf(bdf)
    _, checks = _vfio_gardefous(node, b)
    script = f"""set -e
BDF="{b}"
DEV="/sys/bus/pci/devices/$BDF"
[ -e "$DEV" ] || {{ echo "@@ERR device PCI $BDF introuvable"; exit 1; }}
modprobe vfio-pci
CUR=$(basename "$(readlink "$DEV/driver" 2>/dev/null)" 2>/dev/null || true)
if [ "$CUR" = "vfio-pci" ]; then
  echo "@@SKIP $BDF deja sur vfio-pci"
else
  echo vfio-pci > "$DEV/driver_override"
  if [ -n "$CUR" ]; then echo "$BDF" > "/sys/bus/pci/drivers/$CUR/unbind"; fi
  echo "$BDF" > /sys/bus/pci/drivers_probe
  NOW=$(basename "$(readlink "$DEV/driver" 2>/dev/null)" 2>/dev/null || true)
  [ "$NOW" = "vfio-pci" ] || {{ echo "@@ERR bind vfio-pci echoue (driver=$NOW)"; exit 2; }}
  echo "@@OK $BDF lie a vfio-pci (etait: ${{CUR:-aucun}})"
fi
mkdir -p /etc/bobi
touch {VFIO_BINDS_PATH}
grep -qxF "$BDF" {VFIO_BINDS_PATH} || echo "$BDF" >> {VFIO_BINDS_PATH}
cat > /tmp/.vfio-unit.new << 'MXLEOF'
{_VFIO_BIND_UNIT_TEXT}MXLEOF
if ! cmp -s /tmp/.vfio-unit.new {VFIO_BIND_UNIT_PATH} 2>/dev/null; then
  mv /tmp/.vfio-unit.new {VFIO_BIND_UNIT_PATH}; echo "@@UNIT_ECRITE"
else rm -f /tmp/.vfio-unit.new; fi
if [ ! -L {VFIO_BIND_WANTS_PATH} ]; then
  mkdir -p "$(dirname {VFIO_BIND_WANTS_PATH})"
  ln -sf {VFIO_BIND_UNIT_PATH} {VFIO_BIND_WANTS_PATH}; echo "@@UNIT_ACTIVEE"
fi
echo "@@PERSIST $BDF dans {VFIO_BINDS_PATH} + {VFIO_BIND_UNIT}"
"""
    checks.append(f"persistance boot : {VFIO_BINDS_PATH} + {VFIO_BIND_UNIT} (oneshot)")
    return script, checks


def vfio_unbind_plan(node, bdf):
    """Plan PUR de ROLLBACK (Lot G) : retour du port `bdf` au driver kernel `{VFIO_KERNEL_DRIVER}`
    + retrait de la persistance ({VFIO_BINDS_PATH} ; unité supprimée si plus aucun BDF).
    Symétrique exact de `vfio_bind_plan`. AUCUN garde-fou bloquant : le rollback ne doit
    jamais être refusé. Renvoie (script_text, checks)."""
    b = _normaliser_bdf(bdf)
    iface, _ = _iface_pour_bdf(node, b)
    ifn = (iface or {}).get("ifname")
    checks = [f"rollback : {ifn or b} → retour driver {VFIO_KERNEL_DRIVER} + purge persistance"]
    script = f"""set -e
BDF="{b}"
DEV="/sys/bus/pci/devices/$BDF"
[ -e "$DEV" ] || {{ echo "@@ERR device PCI $BDF introuvable"; exit 1; }}
CUR=$(basename "$(readlink "$DEV/driver" 2>/dev/null)" 2>/dev/null || true)
if [ "$CUR" = "{VFIO_KERNEL_DRIVER}" ]; then
  echo "@@SKIP $BDF deja sur {VFIO_KERNEL_DRIVER}"
else
  echo > "$DEV/driver_override"
  if [ -n "$CUR" ]; then echo "$BDF" > "/sys/bus/pci/drivers/$CUR/unbind"; fi
  echo "$BDF" > /sys/bus/pci/drivers_probe
  NOW=$(basename "$(readlink "$DEV/driver" 2>/dev/null)" 2>/dev/null || true)
  [ "$NOW" = "{VFIO_KERNEL_DRIVER}" ] || {{ echo "@@ERR retour {VFIO_KERNEL_DRIVER} echoue (driver=$NOW)"; exit 2; }}
  echo "@@OK $BDF rendu a {VFIO_KERNEL_DRIVER} (etait: ${{CUR:-aucun}})"
fi
if [ -f {VFIO_BINDS_PATH} ]; then sed -i "\\|^$BDF$|d" {VFIO_BINDS_PATH}; fi
if [ ! -s {VFIO_BINDS_PATH} ]; then
  # Retirer le lien EST ce que fait `systemctl disable` ; ni lui ni un `daemon-reload` ne sont
  # nécessaires pour une unité de boot, et le reload révoquerait le GPU des conteneurs en marche.
  rm -f {VFIO_BIND_WANTS_PATH} {VFIO_BIND_UNIT_PATH} {VFIO_BINDS_PATH}
  echo "@@PERSIST purgee (plus aucun BDF vfio)"
else
  echo "@@PERSIST conservee (autres BDF encore lies)"
fi
"""
    return script, checks


def _vfio_executer(plan_fn, node, bdf, run=None, timeout=60):
    """Exécute un plan vfio via le runner injecté (défaut : agent-nœud). (ok, msg, checks)."""
    script, checks = plan_fn(node, bdf)
    run = run or _run_agent(node)
    rc, out, err = run(script, timeout=timeout)
    if rc != 0 or "@@ERR" in out:
        m = re.search(r"@@ERR (.+)", out or "")
        detail = m.group(1).strip() if m else (err or out or f"rc={rc}").strip()[:300]
        return False, f"échec vfio : {detail}", checks
    m = re.search(r"@@(?:OK|SKIP) (.+)", out or "")
    return True, (m.group(1).strip() if m else "ok"), checks


def vfio_bind_apply(node, bdf, run=None):
    """Applique `vfio_bind_plan` sur le nœud (runner injectable, défaut agent-nœud).
    Renvoie (ok, msg, checks). Propage GardeFouVfio (refus AVANT toute exécution)."""
    return _vfio_executer(vfio_bind_plan, node, bdf, run=run)


def vfio_unbind_apply(node, bdf, run=None):
    """Applique `vfio_unbind_plan` (rollback, Lot G). Renvoie (ok, msg, checks)."""
    return _vfio_executer(vfio_unbind_plan, node, bdf, run=run)


# ─── SR-IOV : PF kernel-PTP + VF DPDK-narrow (chantier narrow, cf. docs/chantiers/SRIOV_IMPL.md) ─────
# ≠ vfio_bind_plan (qui met le PF en vfio → tue le PTP). Ici la PF RESTE sur ice/kernel (ptp4l
# discipline le PHC) et on crée UNE VF, bindée vfio-pci, qui porte le moteur DPDK-narrow.
SRIOV_VFS_PATH     = "/etc/bobi/sriov-vfs"            # lignes: <pf_bdf> <pf_ifname> <vf_mac>
SRIOV_VF_UNIT      = "bobi-sriov-vf.service"
SRIOV_VF_UNIT_PATH = f"/etc/systemd/system/{SRIOV_VF_UNIT}"
# Activation sans rechargement — même raison que VFIO_BIND_WANTS_PATH (unité de BOOT).
SRIOV_VF_WANTS_PATH = f"/etc/systemd/system/multi-user.target.wants/{SRIOV_VF_UNIT}"

# Unité boot : pour chaque PF listé, recrée VF0 (numvfs=1) + mac/trust + bind la VF en vfio. La PF
# revient sur ice toute seule (kernel) → ptp4l repart dessus. numvfs ne survivant pas au reboot.
_SRIOV_VF_UNIT_TEXT = f"""[Unit]
Description=MXL SR-IOV VF (PF kernel-PTP + VF DPDK-narrow — {SRIOV_VFS_PATH})
DefaultDependencies=no
After=systemd-modules-load.service local-fs.target network-pre.target
Before=docker.service
ConditionPathExists={SRIOV_VFS_PATH}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'modprobe vfio-pci; while read -r pf ifc mac; do [ -n "$pf" ] || continue; d=/sys/bus/pci/devices/$pf; [ -e "$d" ] || continue; [ "$(cat $d/sriov_numvfs 2>/dev/null || echo 0)" -ge 1 ] || echo 1 > "$d/sriov_numvfs"; [ -n "$ifc" ] && [ -n "$mac" ] && ip link set "$ifc" vf 0 mac "$mac" trust on spoofchk off 2>/dev/null; vf=$(basename "$(readlink "$d/virtfn0" 2>/dev/null)" 2>/dev/null); [ -n "$vf" ] || continue; vd=/sys/bus/pci/devices/$vf; cur=$(basename "$(readlink "$vd/driver" 2>/dev/null)" 2>/dev/null); [ "$cur" = vfio-pci ] && continue; echo vfio-pci > "$vd/driver_override"; [ -n "$cur" ] && echo "$vf" > "/sys/bus/pci/drivers/$cur/unbind"; echo "$vf" > /sys/bus/pci/drivers_probe; done < {SRIOV_VFS_PATH}'

[Install]
WantedBy=multi-user.target
"""


def _vf_mac(pf_bdf, vf_index=0):
    """MAC localement administrée, DÉTERMINISTE (stable across recreations) pour la VF."""
    h = hashlib.md5(f"{pf_bdf}#{vf_index}".encode()).hexdigest()
    return "02:" + ":".join(h[i:i + 2] for i in (0, 2, 4, 6, 8))   # 02 = locally administered/unicast


def _sriov_gardefous(node, bdf):
    """Garde-fous SR-IOV : refuse (GardeFouVfio) une NIC de management. À la DIFFÉRENCE de
    `_vfio_gardefous`, on N'interdit PAS ptp_enabled — en sriov la PF RESTE kernel (ptp4l tourne
    dessus), c'est justement le but. Renvoie checks:[str]."""
    checks = []
    iface, _rows = _iface_pour_bdf(node, bdf)
    if iface is None:
        checks.append(f"warning: BDF {bdf} absent de node_interfaces — garde-fous non vérifiables")
        return checks
    ifn = iface.get("ifname") or "?"
    from .database import role_is_management as _role_is_mgmt
    if _role_is_mgmt(iface.get("role")):
        raise GardeFouVfio(
            f"refus : {ifn} ({bdf}) est la NIC de management (rôle {iface.get('role')}) "
            "— pas de SR-IOV dessus.")
    checks.append(f"ok: PF {ifn} ({bdf}) reste kernel (PTP) ; VF créée pour le moteur DPDK-narrow")
    return checks


def sriov_vf_plan(node, pf_bdf):
    """Plan PUR : crée 1 VF sur la PF `pf_bdf` (E810), mac/trust, découvre le VF BDF, bind la VF en
    vfio-pci, persiste au boot. La PF RESTE sur ice/kernel (jamais bindée) → ptp4l intact.
    Renvoie (script, checks). Le VF BDF est découvert à l'exécution → émis `@@VFBDF <bdf>` (cf.
    sriov_vf_apply). GardeFouVfio si la PF est de management."""
    b = _normaliser_bdf(pf_bdf)
    checks = _sriov_gardefous(node, b)
    mac = _vf_mac(b)
    script = f"""set -e
PF="{b}"; D="/sys/bus/pci/devices/$PF"
[ -e "$D" ] || {{ echo "@@ERR PF $PF introuvable"; exit 1; }}
[ "$(cat $D/sriov_totalvfs 2>/dev/null || echo 0)" -ge 1 ] || {{ echo "@@ERR $PF sans SR-IOV (totalvfs=0)"; exit 1; }}
modprobe vfio-pci
IFC=$(ls "$D/net" 2>/dev/null | head -1)
if [ "$(cat $D/sriov_numvfs 2>/dev/null || echo 0)" -lt 1 ]; then
  echo 1 > "$D/sriov_numvfs" || {{ echo "@@ERR sriov_numvfs=1 echoue — MMIO insuffisant (BIOS PciResourcePadding=High / Above-4G requis)"; exit 2; }}
fi
[ -n "$IFC" ] && ip link set "$IFC" vf 0 mac "{mac}" trust on spoofchk off 2>/dev/null || true
VF=$(basename "$(readlink "$D/virtfn0" 2>/dev/null)" 2>/dev/null)
[ -n "$VF" ] || {{ echo "@@ERR VF0 introuvable apres creation"; exit 3; }}
echo "@@VFBDF $VF"
VD="/sys/bus/pci/devices/$VF"
CUR=$(basename "$(readlink "$VD/driver" 2>/dev/null)" 2>/dev/null || true)
if [ "$CUR" = "vfio-pci" ]; then
  echo "@@SKIP VF $VF deja vfio-pci"
else
  echo vfio-pci > "$VD/driver_override"
  [ -n "$CUR" ] && echo "$VF" > "/sys/bus/pci/drivers/$CUR/unbind"
  echo "$VF" > /sys/bus/pci/drivers_probe
  NOW=$(basename "$(readlink "$VD/driver" 2>/dev/null)" 2>/dev/null || true)
  [ "$NOW" = "vfio-pci" ] || {{ echo "@@ERR bind VF vfio echoue (driver=$NOW)"; exit 4; }}
  echo "@@OK VF $VF liee a vfio-pci"
fi
mkdir -p /etc/bobi
grep -qE "^$PF " {SRIOV_VFS_PATH} 2>/dev/null || echo "$PF $IFC {mac}" >> {SRIOV_VFS_PATH}
cat > /tmp/.sriov-unit.new << 'MXLEOF'
{_SRIOV_VF_UNIT_TEXT}MXLEOF
if ! cmp -s /tmp/.sriov-unit.new {SRIOV_VF_UNIT_PATH} 2>/dev/null; then
  mv /tmp/.sriov-unit.new {SRIOV_VF_UNIT_PATH}; echo "@@UNIT_ECRITE"
else rm -f /tmp/.sriov-unit.new; fi
if [ ! -L {SRIOV_VF_WANTS_PATH} ]; then
  mkdir -p "$(dirname {SRIOV_VF_WANTS_PATH})"
  ln -sf {SRIOV_VF_UNIT_PATH} {SRIOV_VF_WANTS_PATH}; echo "@@UNIT_ACTIVEE"
fi
echo "@@PERSIST $PF dans {SRIOV_VFS_PATH} + {SRIOV_VF_UNIT}"
"""
    checks.append(f"VF créée + bindée vfio (PF {b} reste kernel/PTP)")
    checks.append(f"persistance boot : {SRIOV_VFS_PATH} + {SRIOV_VF_UNIT} (oneshot)")
    return script, checks


def sriov_vf_apply(node, pf_bdf, run=None, timeout=60):
    """Applique `sriov_vf_plan` via l'agent-nœud. Renvoie (ok, msg, vf_bdf, checks). `vf_bdf` = BDF
    de la VF découvert (@@VFBDF) → à persister dans node_interfaces.vf_bdf par l'appelant. Propage
    GardeFouVfio (refus AVANT exécution)."""
    script, checks = sriov_vf_plan(node, pf_bdf)
    run = run or _run_agent(node)
    rc, out, err = run(script, timeout=timeout)
    vf = None
    m = re.search(r"@@VFBDF (\S+)", out or "")
    if m:
        vf = m.group(1).strip()
    if rc != 0 or "@@ERR" in (out or ""):
        me = re.search(r"@@ERR (.+)", out or "")
        detail = me.group(1).strip() if me else (err or out or f"rc={rc}").strip()[:300]
        return False, f"échec SR-IOV VF : {detail}", vf, checks
    return True, (f"VF {vf} prête (vfio)" if vf else "ok"), vf, checks


# Build + install (NON-disruptif) du driver ice Kahawai 2.6.6 = prérequis du RL narrow sur VF
# (cf. docs/chantiers/SRIOV_IMPL.md §3). Bash brut (pas de str.format). Idempotent (skip si déjà Kahawai). Installe
# dans /lib/modules/<K>/updates + depmod → ACTIF AU PROCHAIN REBOOT (on ne rmmod PAS ice à chaud :
# ça couperait tous les ports E810 + ptp4l + irdma). ⚠ irdma ne recharge pas contre l'ice 2.6.6
# (symboles rdma) → au boot, irdma peut rester déchargé (bénin si pas de RDMA ; documenté SRIOV_IMPL).
INSTALL_PATCHED_ICE = r"""set -e
K=$(uname -r)
if modinfo ice 2>/dev/null | grep -q "Kahawai_2.6.6"; then echo "@@SKIP ice deja Kahawai_2.6.6 charge"; exit 0; fi
if [ -e "/lib/modules/$K/updates/ice.ko" ] && modinfo "/lib/modules/$K/updates/ice.ko" 2>/dev/null | grep -q "Kahawai_2.6.6"; then
  echo "@@SKIP ice Kahawai installe (updates/) — reboot pour activer"; exit 0
fi
echo ">>> deps build + headers noyau ($K)"
command -v make >/dev/null 2>&1 && command -v gcc >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential
dpkg -s "linux-headers-$K" >/dev/null 2>&1 || { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-headers-$K"; }
KSRC=$(ls -d /usr/src/linux-headers-*-common 2>/dev/null | head -1)
[ -n "$KSRC" ] || KSRC="/usr/src/linux-headers-$K"
SRC=/tmp/bobi-ice-build; rm -rf "$SRC"; mkdir -p "$SRC"; cd "$SRC"
echo ">>> clone MTL (patches ice_drv) + ice v2.6.6"
git clone --depth 1 https://github.com/OpenVisualCloud/Media-Transport-Library MTL
git clone --depth 1 -b v2.6.6 https://github.com/intel/ethernet-linux-ice ice
cd ice
for p in ../MTL/patches/ice_drv/2.6.6/*.patch; do echo ">>> $p"; patch -p1 --fuzz=3 < "$p"; done
echo ">>> build (KSRC=$KSRC KOBJ=/lib/modules/$K/build)"
cd src
make -j"$(nproc)" KSRC="$KSRC" KOBJ="/lib/modules/$K/build"
[ -e ice.ko ] || { echo "@@ERR ice.ko non produit"; exit 2; }
modinfo ./ice.ko 2>/dev/null | grep -q "Kahawai_2.6.6" || { echo "@@ERR ice.ko produit mais pas Kahawai"; exit 3; }
echo ">>> install updates/ + depmod"
mkdir -p "/lib/modules/$K/updates"
cp ice.ko "/lib/modules/$K/updates/ice.ko"
depmod "$K"
cd /tmp && rm -rf "$SRC"
echo "@@OK ice Kahawai_2.6.6 installe (/lib/modules/$K/updates) — reboot pour activer"
"""


def install_patched_ice(node, run=None, timeout=900):
    """Build+install le driver ice Kahawai 2.6.6 (prérequis RL narrow sur VF). NON-disruptif :
    install dans updates/ + depmod → actif au prochain reboot (pas de rmmod à chaud). Idempotent.
    Renvoie (ok, msg, reboot_needed)."""
    run = run or _run_agent(node)
    rc, out, err = run(INSTALL_PATCHED_ICE, timeout=timeout)
    if rc != 0 or "@@ERR" in (out or ""):
        m = re.search(r"@@ERR (.+)", out or "")
        detail = (m.group(1).strip() if m else (err or out or f"rc={rc}")).strip()[:300]
        return False, f"échec build ice patché : {detail}", False
    if "@@SKIP" in (out or ""):
        m = re.search(r"@@SKIP (.+)", out or "")
        txt = m.group(1).strip() if m else "déjà à jour"
        return True, txt, ("charge" not in txt)        # déjà chargé → pas de reboot ; installé → reboot
    return True, "ice Kahawai 2.6.6 installé (updates/) — reboot requis pour activer", True


# ─── Provisioning MTL du template (apt + build libmtl/DPDK) ───────────────────
# Bash bruts (pas de str.format) — pas de contrainte d'accolades doublées.

PROVISION_MTL_APT = r"""set -e
echo '>>> apt-get update'
apt-get update
echo '>>> apt-get install (build MTL/DPDK)'
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git build-essential meson ninja-build pkg-config python3-pyelftools \
    libnuma-dev libssl-dev libjson-c-dev libpcap-dev rdma-core libibverbs-dev \
    libcap-ng-dev m4 sudo unzip cmake systemtap-sdt-dev libgtest-dev libgmock-dev
echo '>>> apt MTL OK'
"""

# Build libmtl + DPDK patché MTL. Le composant ld_preload/udp casse (signature ioctl vs glibc) :
# on N'échoue PAS sur le rc de build.sh ; le gate de succès est `pkg-config --exists mtl` + libmtl.so.
PROVISION_MTL_BUILD = r"""set -e
SRC=/root/Media-Transport-Library
echo '>>> clone / update Media-Transport-Library'
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" pull --ff-only || true
else
  git clone --depth 1 https://github.com/OpenVisualCloud/Media-Transport-Library "$SRC"
fi
cd "$SRC"
echo '>>> build DPDK (patché MTL, tests désactivés) — long…'
# -Dtests=false : ne PAS construire les dpdk-test-* (binaires statiques énormes, inutiles à MTL
# et qui saturaient le rootfs « No space left on device »). Injecté via MTL_PREFIX_ARGS, ajouté
# tel quel à la ligne `meson build …` de build_dpdk.sh. ENV INLINE (scopé à cette commande
# seulement) : exporté globalement, il fuit dans le meson de build.sh (libmtl) qui n'a pas
# d'option `tests` → « Unknown options: tests ».
MTL_PREFIX_ARGS="-Dtests=false" ./script/build_dpdk.sh -f
echo '>>> build libmtl + apps (ld_preload/udp peut casser → ignoré)'
./build.sh || echo '>>> build.sh rc!=0 (probable ld_preload/udp) — on vérifie via pkg-config'
ldconfig
echo '>>> vérification pkg-config mtl'
export PKG_CONFIG_PATH=/usr/local/lib/x86_64-linux-gnu/pkgconfig:/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH
if ! pkg-config --exists mtl; then
  echo '@@ERR libmtl introuvable après build (pkg-config --exists mtl KO)'
  exit 2
fi
test -e /usr/local/lib/x86_64-linux-gnu/libmtl.so -o -e /usr/local/lib/libmtl.so || {
  echo '@@ERR libmtl.so absent'; exit 3; }
echo '>>> nettoyage arbre source (~917 Mo, artefacts installés dans /usr/local)'
cd /root && rm -rf "$SRC"
echo '>>> build MTL OK'
"""


def redemarrer(host):
    """Redémarre l'host cible. À n'appeler que sur action utilisateur confirmée."""
    if not host:
        return False, "host non configuré"
    # systemctl reboot coupe la session SSH → on ne lit pas la sortie, rc attendu non nul.
    ssh_run(host, "systemctl reboot", timeout=10)
    return True, "redémarrage demandé"


# ─── Binaire mtl_rx (réception MTL) — build une fois, cache hôte, push (C3b) ──

MTL_RX_CACHE_DIR = "/var/lib/vz/mtl"          # cache du binaire prébuildé, par version
MTL_RX_SRC       = "/opt/script/mtl_rx.c"     # source poussée dans le CT
MTL_RX_BIN       = "/opt/script/mtl_rx"       # destination dans le CT
_MTL_PKGCONFIG   = ("/usr/local/lib/x86_64-linux-gnu/pkgconfig:"
                    "/usr/local/lib/pkgconfig")


def _plugin_mtl_rx_source():
    """Lit plugins/2110_io/mtl_rx.c depuis le dépôt de l'orchestrateur."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "plugins", "2110_io", "mtl_rx.c")
    with open(path, "r") as f:
        return f.read()


def ensure_mtl_rx_binary(host, vmid, version):
    """Garantit la présence de /opt/script/mtl_rx dans le container `vmid`.

    Binaire **prébuildé puis poussé** : on build une seule fois par (nœud, version) dans un
    CT (qui dispose de libmtl) et on cache le résultat sur l'hôte
    (`/var/lib/vz/mtl/mtl_rx-<version>`) ; les déploiements suivants se contentent de
    `pct push`. L'hôte n'ayant pas libmtl, le premier build se fait DANS le CT cible.
    Renvoie (ok, msg)."""
    import shlex
    from .addressing import get_container_ip
    ver = re.sub(r"[^A-Za-z0-9._-]", "_", str(version or "dev"))
    cache = f"{MTL_RX_CACHE_DIR}/mtl_rx-{ver}"
    cache_q = shlex.quote(cache)

    # 1) Cache présent → push direct.
    rc, _, _ = ssh_run(host, f"test -s {cache_q}", timeout=10)
    if rc == 0:
        rc2, out, err = ssh_run(host, f"pct push {int(vmid)} {cache_q} {MTL_RX_BIN} && "
                                       f"pct exec {int(vmid)} -- chmod +x {MTL_RX_BIN}", timeout=30)
        if rc2 != 0:
            return False, f"push cache: rc={rc2} {err.strip() or out.strip()}"
        return True, f"mtl_rx poussé (cache {ver})"

    # 2) Pas de cache → pousser la source dans le CT via l'agent, builder, cacher, push.
    ip = get_container_ip(vmid)
    if not ip:
        return False, f"IP container {vmid} introuvable"
    try:
        from . import deploy
        r = deploy.agent_session().post(deploy.agent_url(ip, "/deploy"),
                          json={"path": MTL_RX_SRC, "content": _plugin_mtl_rx_source()},
                          headers=deploy.agent_headers(vmid),
                          timeout=15)
        if r.status_code != 200:
            return False, f"push source: agent {r.status_code}"
    except Exception as e:
        return False, f"push source: {e}"

    build = (f"PKG_CONFIG_PATH={_MTL_PKGCONFIG}:$PKG_CONFIG_PATH "
             f"cc -O2 -o {MTL_RX_BIN} {MTL_RX_SRC} "
             f"$(pkg-config --cflags --libs mtl) -lpthread -lm")
    rc, out, err = ssh_run(
        host,
        f"set -e; pct exec {int(vmid)} -- sh -lc {shlex.quote(build)}; "
        f"mkdir -p {shlex.quote(MTL_RX_CACHE_DIR)}; "
        f"pct pull {int(vmid)} {MTL_RX_BIN} {cache_q}; "
        f"echo ok",
        timeout=180)
    if rc != 0:
        return False, f"build mtl_rx: rc={rc} {err.strip() or out.strip()}"
    return True, f"mtl_rx buildé + mis en cache ({ver})"
