# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Suite de QUALIFICATION de carte réseau (chantier narrow, cf. docs/chantiers/DPDK_NARROW.md §7).
#
# Pourquoi : certaines capacités narrow (ST 2110 RL) ne sont PAS lisibles du PMD — le max de files TX
# EFFECTIF n'apparaît pas dans rte_eth_dev_info (qui rapporte le mur ice natif 8, que patch_tm_hierarchy
# transcende jusqu'à la vraie valeur). Il faut donc les MESURER une fois par carte (+ firmware, qui peut
# les changer) et enregistrer le profil dans `nic_profiles`. Ce profil MESURÉ prime ensuite sur la
# bibliothèque statique (app/mtl.py) au déploiement (docker_driver._node_rl_tx_cap).
#
# Le module est en deux couches :
#   - PARSEURS purs (device_id / firmware / files TX effectives) — testables sans nœud ;
#   - wrapper NŒUD (`qualify_node`) qui récolte les sondes via un `run(cmd)` (host-exec agent-nœud) +
#     le log du daemon mtl_rx d'un moteur 2110_io DÉJÀ déployé, puis écrit le profil.
#
# ⚠ La mesure de capacité s'appuie sur un moteur 2110_io tournant sur la carte (elle lit le log libmtl
# « tx_queues N malloc succ » = files réellement allouées). Elle n'a de sens que sous pacing narrow (RL).
#
# ⚠⚠ ET SEULEMENT SI LE PMD A CLAMPÉ. « tx_queues N malloc succ » rapporte ce que libmtl a DEMANDÉ ET
# OBTENU, pas le plafond de la carte. La demande suit le nombre de sessions du moteur (mtl_rx.c :
# « daemon up (… tx_q[0]=N) » = `p.tx_queues_cnt[0]`, l'ARGUMENT de mtl_init). Tant que la demande
# tient sous le plafond, la sonde ne fait que relire sa propre demande : c'est une BORNE INFÉRIEURE,
# jamais un cap. La mesure d'origine (banc 2026-07-10, docs/chantiers/DPDK_NARROW.md §7) était valide parce qu'elle
# SUR-DEMANDAIT exprès — 80 slots en cold-batch → clamp du PMD ice à 64 → cap 63.
# Sans cette garde, chaque requalification d'un moteur peu chargé RABAISSAIT le cap autoritaire d'un
# cran, en silence : 63 → 41 → 21 → 14 (dl360-1, E810-C-Q2), jusqu'à rendre rouge la page « Modèles de
# carte 2110 » et à brider RL_TX_QUEUES_CAP sur le moteur. D'où `measured_tx_cap()` : pas de preuve de
# clamp ⇒ pas de cap écrit (les autres champs mesurés le sont quand même).
import re

from .database import db_upsert_nic_profile


# ─── Parseurs purs (unit-testables) ──────────────────────────────────────────────────────────────
def parse_device_id(lspci_nn):
    """`lspci -nn` d'un port → 'device id' Intel ('0x1592'). Vendor ignoré (on identifie le modèle)."""
    m = re.search(r"\[[0-9a-fA-F]{4}:([0-9a-fA-F]{4})\]", lspci_nn or "")
    return ("0x" + m.group(1).lower()) if m else ""


def parse_firmware(ethtool_i):
    """`ethtool -i <port ice>` → version NVM (1er token de firmware-version), ex.
    'firmware-version: 4.80 0x80020543 1.3805.0' → '4.80'. Le firmware/DDP peut changer les capacités
    → il fait partie de la clé de profil."""
    m = re.search(r"firmware-version:\s*(\S+)", ethtool_i or "")
    return m.group(1) if m else ""


def parse_effective_tx_queues(mtl_log):
    """Log du daemon mtl_rx → nb de files TX ALLOUÉES par le PMD (max sur les occurrences
    « dev_if_init_tx_queues ... tx_queues N malloc succ »). 0 si absent.

    ⚠ Ce n'est PAS la capacité de la carte : c'est ce que libmtl a demandé ET obtenu. Ne l'interpréter
    comme un plafond que si la DEMANDE a été clampée — cf. `measured_tx_cap()`, seul point d'entrée
    autorisé pour en dériver un `rl_tx_cap`."""
    caps = [int(n) for n in re.findall(r"tx_queues\s+(\d+)\s+malloc\s+succ", mtl_log or "")]
    return max(caps) if caps else 0


def parse_requested_tx_queues(mtl_log):
    """Log du daemon mtl_rx → nb de files TX DEMANDÉES à `mtl_init` (max sur les ports), lu de notre
    propre ligne « mtl_rx: daemon up (N port(s), rx_q[0]=… tx_q[0]=…) » qui imprime `p.tx_queues_cnt[]`.
    0 si absent (moteur antérieur à cette trace, ou log tronqué).

    C'est le TÉMOIN DE CLAMP : alloué < demandé ⇒ le PMD a buté sur le plafond de la carte, la mesure
    est un vrai cap. alloué ≥ demandé ⇒ on n'a mesuré que la demande du moteur."""
    reqs = [int(n) for n in re.findall(r"tx_q\[\d+\]\s*=\s*(\d+)", mtl_log or "")]
    return max(reqs) if reqs else 0


def measured_tx_cap(mtl_log):
    """(cap, raison) — `rl_tx_cap` MESURABLE depuis le log, ou (None, raison lisible) si la mesure ne
    prouve rien. Point d'entrée UNIQUE : ne jamais dériver un cap de `parse_effective_tx_queues` seul.

    Valide UNIQUEMENT sur preuve de clamp (alloué < demandé) → cap = alloué − 1 (une file = contrôle).
    Sans preuve, on refuse : un cap trop BAS écrit en base prime sur la bibliothèque statique et bride
    le parc (cf. l'en-tête de module)."""
    alloc = parse_effective_tx_queues(mtl_log)
    if not alloc:
        return None, ("aucune ligne « tx_queues N malloc succ » dans le log du moteur (moteur non "
                      "narrow/DPDK, ou log roté avant le démarrage du daemon)")
    req = parse_requested_tx_queues(mtl_log)
    if not req:
        return None, ("demande de files inconnue (ligne « daemon up (… tx_q[0]=N) » absente du log) — "
                      "impossible de distinguer le plafond de la carte de la simple demande du moteur")
    if alloc >= req:
        return None, ("borne INFÉRIEURE seulement : le moteur n'a demandé que %d files TX et les a "
                      "obtenues (%d allouées) — le PMD n'a pas clampé, le plafond de la carte est "
                      "≥ %d mais reste inconnu. Mesurer en SUR-DEMANDANT (banc cold-batch, "
                      "cf. docs/chantiers/DPDK_NARROW.md §7)." % (req, alloc, alloc))
    return max(1, alloc - 1), ""


def parse_ptp_locked(mtl_log):
    """Log moteur → le PHC de la carte se DISCIPLINE-t-il ? (« system clock offset max N, locked »).
    True dès qu'une ligne 'locked' (≠ 'not locked') apparaît. None si aucune ligne PTP (indéterminé)."""
    if not mtl_log or "system clock offset" not in mtl_log:
        return None
    return bool(re.search(r"offset[^,\n]*,\s*locked", mtl_log))


def parse_conformity(metrics):
    """Métriques :8080 d'un moteur en TIMING_PARSER → verdict NARROW depuis les receivers vidéo.
    narrow FRANC = cinst_max ≤ 1 ET vrx_span ∈ [1,5] (invariants à la dérive). On NE lit PAS `compliant`
    directement : le verdict `failed` (fpt > tr_offset) sans grandmaster est STRUCTUREL (cf. §7), pas un
    échec de pacing. Retourne (narrow_ok, détail par receiver) ; (None, None) si aucun verdict présent."""
    recs = (metrics or {}).get("receivers") or []
    verdicts = []
    for r in recs:
        cinst, span = r.get("cinst_max"), r.get("vrx_span")
        if cinst is None or span is None:
            continue
        verdicts.append({"idx": r.get("idx"), "cinst_max": cinst, "vrx_span": span,
                         "compliant": r.get("compliant"),
                         "narrow": bool(cinst <= 1 and 1 <= span <= 5)})
    if not verdicts:
        return None, None
    return all(v["narrow"] for v in verdicts), verdicts


def parse_ddp(devlink_info):
    """`devlink dev info` d'un port de la NIC → (ddp_ok, version_ddp). Le DDP (« fw.app ») DOIT être
    chargé pour le narrow E810 (sans lui = Safe Mode, ptp4l/steering KO). None si non détecté."""
    if not devlink_info:
        return None, None
    ver = re.search(r"fw\.app\s+(\S+)", devlink_info)
    name = re.search(r"fw\.app\.name\s+(.+)", devlink_info)
    if not ver:
        return None, None
    v = ver.group(1).strip()
    label = ("{} {}".format(name.group(1).strip(), v) if name else v)
    return True, label


def parse_ddp_from_log(mtl_log):
    """DDP lu du LOG du PMD ice DPDK — repli quand `devlink` échoue (port en vfio-pci = pas de netdev
    kernel sur le socle full-PF DPDK). Le PMD logue au démarrage :
      `ICE_INIT: ice_load_pkg_type(): Active package is: 1.3.41.0, ICE OS Default Package (…)`
    ou bascule en « Safe Mode » si le DDP (ice.pkg) n'a pas chargé. Renvoie (ddp_ok, version) —
    (False, 'Safe Mode') si absent, (None, None) si la ligne n'est pas dans le log."""
    if not mtl_log:
        return None, None
    if re.search(r"[Ss]afe [Mm]ode", mtl_log):
        return False, "Safe Mode"
    m = re.search(r"Active package is:\s*([0-9][0-9.]*),\s*([^(\r\n]+)", mtl_log)
    if m:
        return True, "{} {}".format(m.group(2).strip(), m.group(1).strip())
    return None, None


def parse_stack_version(mtl_log):
    """Version de la stack logicielle (bibliothèque MTL + DPDK) loguée par `mtl_init` — CONTEXTE de la
    mesure de qualif : le cap RL / narrow dépendent du PMD ice DPDK, pas du driver kernel `ice` (non
    utilisé sur le socle vfio). Le firmware NVM de la carte, lui, reste l'identité (clé du profil) mais
    est illisible en full-vfio. Ex. « MTL 26.1.0.DEV / DPDK 26.03.90_mtl_0 ». '' si absent du log."""
    if not mtl_log:
        return ""
    parts = []
    m = re.search(r"MTL version:\s*(\S+)", mtl_log)
    if m:
        parts.append("MTL " + m.group(1).strip())
    d = re.search(r"dpdk version:\s*([^\r\n]+)", mtl_log)
    if d:
        parts.append(d.group(1).strip())
    return " / ".join(parts)


# ─── Calcul + écriture du profil ─────────────────────────────────────────────────────────────────
def parse_engine_healthy(mtl_log):
    """Le moteur 2110 est-il SAIN dans son log ? False si signature de CRASH-LOOP (échec d'init
    DPDK/mtl : `dev_eal_init fail`, `mtl_init fail`, …). Sur un moteur cassé, `tx_queues N malloc succ`
    est partiel/périmé → la capacité mesurée est GARBAGE (vécu : cap 41→21 mesuré pendant un crash-loop
    DPDK). On REFUSE alors d'enregistrer un profil (cf. nic-qualify-garbage-on-broken-engine)."""
    if not mtl_log:
        return False
    import re as _re
    return not _re.search(r"(mt_dev_eal_init|dev_eal_init|mtl_init)[^\n]*fail", mtl_log, _re.I)


def qualify_from_probes(device_id, firmware, model, mtl_log,
                        narrow_ok=None, ddp_ok=None, ptp_ok=None, ddp_ver=None):
    """Écrit le profil MESURÉ (measured=1) depuis les sorties de sondes.

    Retourne `(cap, raison)` : `cap` = rl_tx_cap écrit, ou None si la capacité n'est pas MESURABLE
    (`raison` explique alors pourquoi, cf. `measured_tx_cap`). Un cap non mesurable n'empêche PAS
    d'enregistrer le reste du profil (PTP/DDP/narrow/firmware) : on écrit tout SAUF `rl_tx_cap`, que
    `db_upsert_nic_profile` laisse alors intact (il ignore les champs None) → `_node_rl_tx_cap`
    retombe sur la bibliothèque statique, qui est la bonne réponse quand on n'a rien mesuré.
    Retourne `(None, raison)` sans rien écrire si device_id manque OU si le moteur n'est pas sain."""
    # ★ GARDE-FOU (point d'écriture UNIQUE) : ne JAMAIS enregistrer un profil depuis un moteur en
    # crash-loop — la mesure serait faussée et empoisonnerait le cap autoritaire en silence.
    if not parse_engine_healthy(mtl_log):
        return None, "moteur 2110 non sain (crash-loop)"
    if not device_id:
        return None, "device_id introuvable (lspci)"
    cap, cap_reason = measured_tx_cap(mtl_log)
    if cap is None:
        note = "qualifié banc : capacité TX NON mesurée (%s)" % cap_reason
    else:
        note = ("qualifié banc : tx_queues=%d allouées < %d demandées (clamp PMD) → %d sessions TX "
                "narrow/port" % (cap + 1, parse_requested_tx_queues(mtl_log), cap))
    note += " | PTP=%s | DDP=%s | narrow=%s" % (
        {True: "lock", False: "pas de lock", None: "?"}[ptp_ok],
        (ddp_ver or ("chargé" if ddp_ok else ("Safe Mode/absent" if ddp_ok is False else "?"))),
        {True: "OK (cinst≤1, vrx_span 1-5)", False: "NON (wide)", None: "non mesuré (pas de sonde)"}[narrow_ok])
    _stack = parse_stack_version(mtl_log)   # contexte logiciel de la mesure (PMD DPDK/MTL)
    if _stack:
        note += " | stack=%s" % _stack
    db_upsert_nic_profile(
        device_id, firmware=firmware, model=model, rl_tx_cap=cap, measured=1,
        narrow_ok=(None if narrow_ok is None else int(bool(narrow_ok))),
        ddp_ok=(None if ddp_ok is None else int(bool(ddp_ok))),
        ptp_ok=(None if ptp_ok is None else int(bool(ptp_ok))),
        notes=note)
    return cap, cap_reason


# ─── Wrapper nœud (déploiement) ──────────────────────────────────────────────────────────────────
def _media_nic(node):
    """(pci du port média2110, ifname d'un port ice sœur pour ethtool, model) depuis node_interfaces.
    Le port média est en vfio (pas de netdev) → on lit le firmware sur un AUTRE port ice de la même NIC
    (même BDF fonction ≠, ex. …00.0 pour …00.1). Repli : le port média lui-même."""
    from .database import db_get_node_interfaces
    ifaces = db_get_node_interfaces(node["id"])
    media = next((r for r in ifaces if r.get("role") == "media2110" and (r.get("pci") or "")), None)
    if not media:
        return None, None, None
    pci = (media.get("pci") or "").strip()
    model = (media.get("model") or "").strip()
    # port ice sœur = même NIC (préfixe BDF domaine:bus commun), role ≠ vfio/dpdk, ifname présent
    bus = pci.rsplit(".", 1)[0] if "." in pci else pci
    sib = next((r.get("ifname") for r in ifaces
                if (r.get("pci") or "").startswith(bus) and r.get("pmd") != "dpdk"
                and (r.get("ifname") or "").strip()), None)
    return pci, sib, model


def qualify_node(node, run, mtl_log, metrics=None):
    """Qualifie la carte média d'un nœud. `run(cmd)->str` = host-exec (agent-nœud) ; `mtl_log` = log du
    daemon mtl_rx d'un moteur 2110_io tournant sur la carte ; `metrics` = JSON :8080 du moteur (si
    TIMING_PARSER actif + un receiver lit un TX narrow → verdict conformité). Écrit le profil
    `nic_profiles` (measured=1) et retourne le dict de profil, ou None.

    Prérequis : moteur 2110_io déployé sur la carte en pacing narrow (log « tx_queues N malloc succ »).
    narrow_ok n'est renseigné QUE si `metrics` porte un verdict (setup sonde/loopback TIMING_PARSER)."""
    pci, sib, model = _media_nic(node)
    if not pci:
        return None
    # Refus EXPLICITE (message précis pour l'UI) si le moteur crash-loope : mesurer sur un moteur non
    # sain donne un cap GARBAGE qui empoisonnerait le profil autoritaire (cf. le garde-fou muet dans
    # qualify_from_probes). On distingue « moteur cassé » de « pas de données » par un dict d'erreur.
    if not parse_engine_healthy(mtl_log):
        return {"error": "moteur 2110 non sain (crash-loop : échec d'init DPDK/mtl dans le log) — la "
                         "mesure de capacité serait faussée. Réparer le moteur (binding vfio DPDK ou "
                         "AF-XDP) et le laisser tourner en pacing narrow avant de qualifier."}
    device_id = parse_device_id(run("lspci -nn -s %s" % pci) or "")
    firmware = parse_firmware(run("ethtool -i %s" % sib) or "") if sib else ""
    ptp_ok = parse_ptp_locked(mtl_log)                                  # le PHC se discipline (log moteur)
    ddp_ok, ddp_ver = parse_ddp(run("devlink dev info pci/%s" % pci) or "")  # DDP chargé (prérequis narrow)
    if ddp_ok is None:   # devlink KO (port en vfio-pci, full-PF DPDK → pas de netdev) → repli log PMD ice
        ddp_ok, ddp_ver = parse_ddp_from_log(mtl_log)
    narrow_ok, _detail = parse_conformity(metrics)                      # cinst/vrx_span (si sonde présente)
    cap, cap_reason = qualify_from_probes(device_id, firmware, model, mtl_log,
                                          narrow_ok=narrow_ok, ptp_ok=ptp_ok, ddp_ok=ddp_ok,
                                          ddp_ver=ddp_ver)
    if cap is None and not device_id:
        return None
    # cap None = capacité non MESURABLE (pas de clamp) : le reste du profil est écrit, et `cap_reason`
    # remonte à l'UI. On ne renvoie PAS None — ce serait un « échec » trompeur alors que la
    # qualification PTP/DDP a bien eu lieu, et ça masquerait la vraie raison.
    return {"device_id": device_id, "firmware": firmware, "model": model, "rl_tx_cap": cap,
            "cap_reason": cap_reason,
            "ptp_ok": ptp_ok, "ddp_ok": ddp_ok, "ddp": ddp_ver, "narrow_ok": narrow_ok}


def qualify_node_via_agent(node, container_name, tail=4000):
    """Branche `qualify_node` sur l'agent-nœud : host-exec pour les sondes + logs du conteneur moteur
    `container_name` (le bobi-mtl du nœud, en pacing narrow) pour le signal de capacité. Retourne le
    profil écrit (dict) ou None. À appeler quand un moteur 2110_io tourne sur la carte à qualifier."""
    from . import node_driver

    def _run(cmd):
        rc, out, _err = node_driver.host_exec(node, cmd, timeout=20)   # (rc, stdout, stderr)
        return out if rc == 0 else ""

    # Log du moteur : GREP des lignes utiles sur `docker logs 2>&1` (host-exec). Deux raisons :
    # (1) l'endpoint agent /logs ne renvoie QUE stdout, or les lignes libmtl (« tx_queues N malloc
    # succ », « offset … locked ») sont sur STDERR ; (2) la ligne « tx_queues » n'apparaît qu'AU
    # DÉMARRAGE du daemon → un `--tail N` la rate sur un moteur qui tourne depuis longtemps (log
    # volumineux). Le grep la capte quel que soit l'âge, et reste compact.
    import shlex as _shlex
    _cn = _shlex.quote(container_name)
    _logs = "docker logs %s 2>&1" % _cn
    # Deux passes : (1) lignes de DÉMARRAGE (tx_queues, package DDP, version stack, master GM) — en
    # TÊTE du log, prises en `head` ; (2) statut PTP RÉCENT (locked) — en `tail`. Un simple `tail` sur
    # un grep unique jetterait les lignes de démarrage dès que les lignes PTP périodiques s'accumulent.
    # « daemon up (… tx_q[0]=N) » est INDISPENSABLE : c'est la DEMANDE de files passée à mtl_init, donc
    # le témoin de clamp sans lequel « tx_queues N malloc succ » ne prouve aucun plafond (cf. en-tête).
    _startup = _run(_logs + " | grep -aE 'tx_queues [0-9]+ malloc succ|rx_queues [0-9]+ malloc succ|"
                    "daemon up \\(|Active package is:|Safe Mode|MTL version:|master initialized' | head -60")
    _ptp = _run(_logs + " | grep -aE 'offset max [0-9]+, (locked|not locked)' | tail -30")
    # Marqueurs de CRASH-LOOP (init DPDK/mtl échouée) — capturés en TAIL pour refléter l'état RÉCENT :
    # le grep de démarrage (head) les filtrait, d'où une qualification faussée sur moteur cassé.
    _fail = _run(_logs + " | grep -aiE 'mt_dev_eal_init[^\\n]*fail|dev_eal_init[^\\n]*fail|mtl_init fail' | tail -8")
    mtl_log = (_startup + "\n" + _ptp + "\n" + _fail).strip()
    if not mtl_log:   # repli : dernières lignes brutes, puis agent /logs si host-exec docker KO
        mtl_log = _run("docker logs --tail %d %s 2>&1" % (int(tail), _cn))
    if not mtl_log:
        lines = node_driver.container_logs(node, container_name, tail=tail)
        mtl_log = "\n".join(lines) if isinstance(lines, list) else str(lines or "")
    # Verdict conformité si le moteur tourne en TIMING_PARSER (sonde/loopback) : :8080 sur le host du nœud.
    import json as _json
    metrics = None
    try:
        metrics = _json.loads(_run("curl -s -m5 http://127.0.0.1:8080/") or "null")
    except Exception:
        metrics = None
    return qualify_node(node, _run, mtl_log, metrics=metrics)
