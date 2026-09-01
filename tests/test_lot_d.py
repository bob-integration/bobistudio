# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Tests Lot D — observabilité sans ethtool (port média en vfio/DPDK).

Vérifie `app.node_health._merge_dpdk_net` (+ la forme agent 0.15.0) contre des snapshots
moteur FORGÉS (contrat `nic.ports[i].mtl_stats` de docs/chantiers/DPDK_NARROW.md — aucun accès réseau) :

  1. Port `pmd=dpdk` vivant → débits rx/tx calculés par delta rx_bytes/tx_bytes (source mtl).
  2. Compteurs figés + sessions actives → UNE alerte `warning` « muet », puis `info` au retour.
  3. Port absent du snapshot moteur (ou moteur injoignable) → pas de crash, entrée affichable.
  4. Nœud 100 % af_xdp → comportement STRICTEMENT inchangé (snapshot intact, zéro alerte,
     zéro fetch moteur).

DB sqlite jetable (DB_PATH surchargé AVANT init_db, motif tests/smoke_test.py) — la DB de
prod n'est JAMAIS touchée.

Usage :
    ./venv/bin/python tests/test_lot_d.py

Exit 0 si tout passe, 1 sinon.
"""
import copy
import os
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


# ─── DB jetable + fixtures node_interfaces ────────────────────────────────────
_tmpdir = tempfile.mkdtemp(prefix="bobi-test-lot-d-")
_db_path = os.path.join(_tmpdir, "db_test.db")

import app.database as database                       # noqa: E402
database.DB_PATH = _db_path
import app.config as config                           # noqa: E402
config.DB_PATH = _db_path
database.init_db()

with database.get_db() as _db:
    # Nœud 1 : banc DPDK (blue en vfio, red resté af_xdp) — nœud 2 : flotte 100 % af_xdp.
    _db.execute("INSERT INTO node_interfaces (node_id, ifname, pci, role, pmd) "
                "VALUES (1, 'ens1f0np0', '0000:12:00.0', 'media2110', 'dpdk')")
    _db.execute("INSERT INTO node_interfaces (node_id, ifname, pci, role, pmd) "
                "VALUES (1, 'ens1f1np1', '0000:12:00.1', 'media2110', 'af_xdp')")
    _db.execute("INSERT INTO node_interfaces (node_id, ifname, role) "
                "VALUES (2, 'ens2f0', 'media2110')")
    _db.commit()

import app.node_health as nh                          # noqa: E402

# Capture des alertes (db_add_alert est importé DANS le namespace node_health).
ALERTS = []
# ⚠ Le bouchon doit suivre la VRAIE signature, sinon il ment sur le contrat.
# Celui-ci est resté à `(msg, niveau)` alors que `db_add_alert` accepte depuis 2026-07
# le contexte structuré (`vmid`, `node_id`, `kind`, `params`) — cf. CLAUDE.md. Le code
# de production passe `node_id=`, la lambda le refusait, et le test échouait sur un
# TypeError qui ne désignait AUCUN défaut du produit. Un bouchon trop étroit transforme
# une évolution correcte en échec, ce qui revient à décourager l'évolution.
nh.db_add_alert = (lambda msg, niveau="info", vmid=None, node_id=None, kind=None,
                          params=None: ALERTS.append((niveau, msg, params or {})))

# Stub du snapshot moteur (compte les appels pour le test « af_xdp intact »).
ENGINE = {"snap": None, "calls": 0}


def _fake_engine(node_id):
    ENGINE["calls"] += 1
    return copy.deepcopy(ENGINE["snap"])


nh._io_engine_snapshot = _fake_engine


def _reset_state():
    ALERTS.clear()
    ENGINE["calls"] = 0
    nh._mtl_prev.clear()
    nh._vfio_frozen_cnt.clear()
    nh._vfio_alert_state.clear()


def _engine_snap(rx_bytes, tx_bytes, rx_pkts, tx_pkts, active=2, iface="0000:12:00.0"):
    """Snapshot :8080 forgé, conforme au contrat docs/chantiers/DPDK_NARROW.md (« Contrats de la nuit »)."""
    return {
        "fps": 50.0,
        "receivers": [{"idx": 0, "essence": "video", "mode": "mtl", "fps": 50.0}],
        "nic": {"ports": [
            {"iface": iface, "link_up": True, "active": active, "port_capacity_gbps": 100,
             "mtl_stats": {"port": iface, "pmd": "dpdk",
                           "rx_packets": rx_pkts, "tx_packets": tx_pkts,
                           "rx_bytes": rx_bytes, "tx_bytes": tx_bytes,
                           "rx_err": 0, "tx_err": 0, "rx_hw_dropped": 0, "rx_nombuf": 0}},
            {"iface": "ens1f1np1", "link_up": True, "active": 1, "port_capacity_gbps": 100,
             "rx_gbps": 1.2, "tx_gbps": 0.0},
        ]},
    }


def _node_snap():
    """Snapshot /v1/health forgé (agent < 0.15.0 : le port vfio a DISPARU de net)."""
    return {"ts": time.time(), "ok": True, "name": "dl360-1", "host": "198.51.100.251",
            "net": {"ens1f1np1": {"rx_bps": 1000, "tx_bps": 2000, "speed_mbps": 100000}}}


# ─── 1) Port dpdk vivant : débits calculés par delta mtl_stats ────────────────
def test_dpdk_alive():
    _reset_state()
    ENGINE["snap"] = _engine_snap(rx_bytes=0, tx_bytes=0, rx_pkts=0, tx_pkts=0)
    snap = _node_snap()
    nh._merge_dpdk_net(1, snap)                        # 1er passage : amorçage des deltas
    e = snap["net"].get("ens1f0np0")
    check("dpdk vivant : entrée net créée malgré l'absence côté agent", e is not None)
    check("dpdk vivant : 1er passage sans débit (amorçage)",
          e and e["rx_bps"] is None and e["tx_bps"] is None, str(e))
    check("dpdk vivant : provenance/pmd marqués",
          e and e.get("pmd") == "dpdk" and e.get("source") == "mtl", str(e))
    check("dpdk vivant : speed_mbps dérivé de port_capacity_gbps",
          e and e.get("speed_mbps") == 100000, str(e))

    # 2e passage : +1,25 Go RX / +0,125 Go TX sur 10 s ⇒ 1 Gb/s RX, 100 Mb/s TX.
    key = (str(1), "ens1f0np0")
    nh._mtl_prev[key]["ts"] = time.time() - 10.0
    ENGINE["snap"] = _engine_snap(rx_bytes=1_250_000_000, tx_bytes=125_000_000,
                                  rx_pkts=1_000_000, tx_pkts=100_000)
    snap2 = _node_snap()
    nh._merge_dpdk_net(1, snap2)
    e2 = snap2["net"]["ens1f0np0"]
    check("dpdk vivant : rx_bps ≈ 1 Gb/s",
          e2["rx_bps"] is not None and abs(e2["rx_bps"] - 1e9) < 1e7, str(e2.get("rx_bps")))
    check("dpdk vivant : tx_bps ≈ 100 Mb/s",
          e2["tx_bps"] is not None and abs(e2["tx_bps"] - 1e8) < 1e6, str(e2.get("tx_bps")))
    check("dpdk vivant : aucune alerte", not ALERTS, str(ALERTS))
    check("dpdk vivant : l'interface af_xdp du nœud reste sourcée agent (intacte)",
          snap2["net"]["ens1f1np1"] == {"rx_bps": 1000, "tx_bps": 2000, "speed_mbps": 100000},
          str(snap2["net"]["ens1f1np1"]))

    # Restart moteur (compteurs cumulés qui RECULENT) : pas de débit négatif, on saute un cycle.
    nh._mtl_prev[key]["ts"] = time.time() - 5.0
    ENGINE["snap"] = _engine_snap(rx_bytes=1_000, tx_bytes=0, rx_pkts=10, tx_pkts=0)
    snap3 = _node_snap()
    nh._merge_dpdk_net(1, snap3)
    e3 = snap3["net"]["ens1f0np0"]
    check("dpdk vivant : delta négatif (restart moteur) → débit non calculé, pas de négatif",
          e3["rx_bps"] is None and e3["tx_bps"] is None, str(e3))


# ─── 2) Compteurs figés + sessions actives → alerte « port vfio muet » ────────
def test_dpdk_frozen():
    _reset_state()
    ENGINE["snap"] = _engine_snap(rx_bytes=5_000, tx_bytes=5_000, rx_pkts=500, tx_pkts=500,
                                  active=3)
    for _ in range(nh.VFIO_FROZEN_SAMPLES + 2):       # compteurs identiques à chaque échantillon
        nh._merge_dpdk_net(1, _node_snap())
    warns = [a for a in ALERTS if a[0] == "warning"]
    check("figé : exactement UNE alerte warning (à transition)", len(warns) == 1, str(ALERTS))
    # ⚠ UNE ALERTE EST UNE CLÉ + DES PARAMS, plus une phrase. Ce contrôle cherchait
    # « ens1f0np0 » et « dl360-1 » DANS LE MESSAGE : il datait d'avant l'i18n des alertes, où
    # le texte était interpolé à l'émission. Depuis, `db_add_alert` reçoit `alert.net.port_muet`
    # et un dict — le port n'est plus dans le message, il est dans les params, et le rendu se
    # fait à l'affichage, dans la langue du lecteur. On vérifie donc ce qui est vrai
    # aujourd'hui : la bonne clé, et le port nommé dans les params.
    check("figé : l'alerte porte la clé port_muet et nomme le port",
          warns and warns[0][1] == "alert.net.port_muet"
          and warns[0][2].get("ifname") == "ens1f0np0", str(warns))
    check("figé : niveau conforme (warning, pas error)",
          all(a[0] in ("info", "warning", "error") for a in ALERTS) and not
          [a for a in ALERTS if a[0] == "error"], str(ALERTS))
    snap = _node_snap()
    nh._merge_dpdk_net(1, snap)
    check("figé : entrée marquée frozen", snap["net"]["ens1f0np0"].get("frozen") is True)

    # Retour du trafic → info « revenu », une seule fois.
    ENGINE["snap"] = _engine_snap(rx_bytes=9_000_000, tx_bytes=6_000, rx_pkts=9_000, tx_pkts=600,
                                  active=3)
    nh._merge_dpdk_net(1, _node_snap())
    nh._merge_dpdk_net(1, _node_snap())
    infos = [a for a in ALERTS if a[0] == "info"]
    # Même correction que ci-dessus : la clé fait foi, pas le mot français « revenu »
    # qui n'apparaît plus que dans le catalogue i18n.
    check("figé : retour du trafic → UNE alerte info port_retabli",
          len(infos) == 1 and infos[0][1] == "alert.net.port_retabli", str(ALERTS))

    # Compteurs figés SANS session active (rien d'abonné) → silence normal, aucune alerte.
    _reset_state()
    ENGINE["snap"] = _engine_snap(rx_bytes=5_000, tx_bytes=5_000, rx_pkts=500, tx_pkts=500,
                                  active=0)
    for _ in range(nh.VFIO_FROZEN_SAMPLES + 2):
        nh._merge_dpdk_net(1, _node_snap())
    check("figé sans session : aucune alerte (port silencieux = normal)", not ALERTS, str(ALERTS))


# ─── 3) Port absent du snapshot moteur / moteur injoignable → pas de crash ────
def test_dpdk_absent():
    _reset_state()
    # a) Le moteur ne connaît pas ce port (vieux controller.py sans mtl_stats).
    ENGINE["snap"] = {"fps": 50.0, "nic": {"ports": [
        {"iface": "ens1f1np1", "rx_gbps": 1.0, "active": 1}]}}
    snap = _node_snap()
    nh._merge_dpdk_net(1, snap)
    e = snap["net"].get("ens1f0np0")
    check("port absent : pas de crash, entrée affichable quand même",
          e is not None and e.get("pmd") == "dpdk", str(e))
    check("port absent : débits inconnus (pas inventés)",
          e and e["rx_bps"] is None and e["tx_bps"] is None, str(e))
    check("port absent : aucune alerte", not ALERTS, str(ALERTS))
    # b) Moteur injoignable / pas de moteur sur le nœud.
    ENGINE["snap"] = None
    snap2 = _node_snap()
    nh._merge_dpdk_net(1, snap2)
    check("moteur injoignable : pas de crash, entrée affichable",
          snap2["net"].get("ens1f0np0", {}).get("pmd") == "dpdk")
    check("moteur injoignable : aucune alerte", not ALERTS, str(ALERTS))
    # c) Snapshot moteur malformé (nic = None) → pas de crash.
    ENGINE["snap"] = {"fps": 50.0, "nic": None}
    nh._merge_dpdk_net(1, _node_snap())
    check("nic:None : aucune alerte, pas de crash", not ALERTS, str(ALERTS))
    # d) Forme agent ≥ 0.15.0 : l'iface est déjà là avec state:"vfio" → enrichie, pas écrasée.
    ENGINE["snap"] = _engine_snap(rx_bytes=0, tx_bytes=0, rx_pkts=0, tx_pkts=0)
    snap4 = _node_snap()
    snap4["net"]["ens1f0np0"] = {"state": "vfio"}     # posé par le futur agent
    nh._merge_dpdk_net(1, snap4)
    e4 = snap4["net"]["ens1f0np0"]
    check("agent 0.15.0 (state:vfio) : entrée conservée et enrichie",
          e4.get("state") == "vfio" and e4.get("pmd") == "dpdk" and "rx_bps" in e4, str(e4))


# ─── 4) Nœud 100 % af_xdp : comportement STRICTEMENT inchangé ────────────────
def test_afxdp_untouched():
    _reset_state()
    ENGINE["snap"] = _engine_snap(rx_bytes=123, tx_bytes=456, rx_pkts=1, tx_pkts=2)
    snap = {"ts": time.time(), "ok": True, "name": "dl360-2", "host": "198.51.100.252",
            "net": {"ens2f0": {"rx_bps": 5000, "tx_bps": 6000, "speed_mbps": 25000}}}
    ref = copy.deepcopy(snap)
    nh._merge_dpdk_net(2, snap)                        # nœud 2 : aucune interface pmd=dpdk
    check("af_xdp : snapshot STRICTEMENT inchangé", snap == ref,
          f"{snap} != {ref}")
    check("af_xdp : aucune alerte", not ALERTS, str(ALERTS))
    check("af_xdp : aucun fetch du moteur :8080", ENGINE["calls"] == 0, str(ENGINE["calls"]))
    # Idem pour un nœud inconnu de node_interfaces.
    snap9 = copy.deepcopy(ref)
    nh._merge_dpdk_net(999, snap9)
    check("nœud sans node_interfaces : inchangé, aucun fetch",
          snap9 == ref and ENGINE["calls"] == 0 and not ALERTS)


# ─── 5) Agent : _all_nics/_nic exposent state:"vfio" (forme neuve) ────────────
def test_agent_vfio_shape():
    sys.path.insert(0, os.path.join(ROOT, "node_agent"))
    import agent
    agent.CONFIG["mtl_iface"] = "ifacetest-vfio0"      # déclaré mais absent de /sys/class/net
    nics = agent._all_nics()
    entry = next((n for n in nics if n.get("name") == "ifacetest-vfio0"), None)
    check("agent : iface déclarée absente de /sys/class/net présente dans _all_nics",
          entry is not None, str([n.get("name") for n in nics]))
    check("agent : entrée marquée state:vfio", entry and entry.get("state") == "vfio", str(entry))
    check("agent : les vraies interfaces n'ont PAS de champ state",
          all("state" not in n for n in nics if n is not entry), str(nics))
    one = agent._nic("ifacetest-vfio0")
    check("agent : _nic(iface vfio) → state:vfio (au lieu de None ambigus)",
          one and one.get("state") == "vfio" and one.get("iface") == "ifacetest-vfio0", str(one))


if __name__ == "__main__":
    for fn in (test_dpdk_alive, test_dpdk_frozen, test_dpdk_absent,
               test_afxdp_untouched, test_agent_vfio_shape):
        print(f"\n── {fn.__name__} ──")
        try:
            fn()
        except Exception:
            traceback.print_exc()
            FAILURES.append(f"{fn.__name__}: exception")
    print()
    if FAILURES:
        print(f"ÉCHEC — {len(FAILURES)} problème(s) :")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("OK — tous les tests Lot D passent.")
    sys.exit(0)
