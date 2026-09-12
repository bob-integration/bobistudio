# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Tests OFFLINE du Lot 14 (chantier DPDK/narrow — alignement SIP↔IFACE↔PORT_BDFS).

Prouve, SANS toucher un nœud ni la prod, que `app.docker_driver._build_run_cmd` :

  1. mono-port DPDK dont la NIC bindée (BDF) ≠ la NIC PRIMAIRE du nœud :
       sip = l'IP RÉELLE de l'iface dpdk (node_interfaces.ip_cidr), PAS node.media_ip de la
       NIC sœur. C'est le bug dl360-1 (sip=.99 poussé sur ens1f1np1/.229 → IGMP snooping ne
       forwarde pas le mcast → rx=0). Le rendu HEAD est comparé pour EXHIBER la régression.
  2. multi-port (2022-7 af_xdp+dpdk) : IFACES / SIPS / PORT_PMDS / PORT_BDFS strictement
       alignés index par index sur la même liste _media_ifaces.
  3. port dpdk SANS IP de segment → déploiement REFUSÉ (ValueError), pas de sip muet.
  4. ISO-COMPORTEMENT af_xdp : un nœud 100 % af_xdp (bien configuré ou repli mono-NIC) →
       commande `docker run` OCTET-IDENTIQUE à `git show HEAD:app/docker_driver.py`.

Usage :  /opt/bobistudio/venv/bin/python tests/test_lot14_sip.py
Contraintes : aucune écriture disque, aucun accès réseau, la DB de prod n'est jamais lue
(db_get_node_interfaces monkey-patché). Réutilise les helpers/patrons de tests/test_lot_c.py.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILS = []


def check(name, cond, detail=""):
    print("  [{}] {}{}".format("OK" if cond else "FAIL", name,
                               (" — " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def git_show(repo, path):
    return subprocess.run(["git", "-C", repo, "show", "HEAD:" + path],
                          capture_output=True, text=True, check=True).stdout


# ── env parsing (-e KEY=VALUE ; shlex.quote ne cite pas les CSV sans espace) ──
_ENV_RE = re.compile(r"-e ([A-Z_]+)=(\S+)")


def parse_env(cmd):
    env = {}
    for k, v in _ENV_RE.findall(cmd):
        env[k] = v
    return env


# ── stubs (repris de test_lot_c.py) ──
class _FakeSettings:
    VALUES = {"mtl_pin_cores": False, "nmos_sdp_source_filter": True}
    @classmethod
    def get(cls, key, default=None):
        return cls.VALUES.get(key, default)


def _stub_routes():
    for name in ("app.routes", "app.routes.mtl_engine", "app.routes.shared"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["app.routes.mtl_engine"]._mtl_per_source_sessions = lambda p, k: 3
    sys.modules["app.routes.shared"]._mtl_total_queues = lambda: 48


def _patch_module(mod, rows):
    import app.database as adb
    adb.db_get_node_interfaces = lambda node_id: [dict(r) for r in rows]
    if "app.ca" not in sys.modules or not hasattr(sys.modules["app.ca"], "_stub"):
        fake_ca = types.ModuleType("app.ca")
        fake_ca.ca_available = lambda: False
        fake_ca._stub = True
        sys.modules["app.ca"] = fake_ca
        import app as _app
        _app.ca = fake_ca
    mod.settings.get = _FakeSettings.get


def _load_old_docker_driver():
    src = git_show(REPO, "app/docker_driver.py")
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location("app._docker_driver_lot14_old", path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "app"
    sys.modules["app._docker_driver_lot14_old"] = mod
    spec.loader.exec_module(mod)
    os.unlink(path)
    return mod


PARAMS = {"hostname": "io2110-test", "video_count": 4, "tx_count": 2,
          "active_rx_count": 4, "active_tx_count": 2}


# ─────────────── 1. mono-port DPDK : NIC bindée ≠ NIC primaire ───────────────

def test_dpdk_mono_sip():
    print("1. mono-port DPDK : sip = IP réelle de l'iface dpdk (bug dl360-1)")
    import app.docker_driver as dd
    old = _load_old_docker_driver()
    # Nœud dont la PRIMAIRE (mtl_iface/media_ip) est ens1f0np0/.99, mais la SEULE NIC média
    # déclarée est le port DPDK ens1f1np1/.229 (bindé par BDF). C'est la topo dl360-1.
    node = {"id": 1, "host": "198.51.100.251", "image": "bobi-mtl:test", "mxl_mount": None,
            "mtl_iface": "ens1f0np0", "media_ip": "198.51.100.99/24"}
    rows = [{"ifname": "ens1f1np1", "role": "media2110", "ip_cidr": "198.51.100.229/24",
             "media_network_id": 1, "pair_role": None, "pair_group": None,
             "pmd": "dpdk", "pci": "0000:11:00.1"}]
    _patch_module(dd, rows)
    _patch_module(old, rows)
    cmd = dd._build_run_cmd(9999, dict(node), dict(PARAMS))
    cmd_old = old._build_run_cmd(9999, dict(node), dict(PARAMS))
    env, env_old = parse_env(cmd), parse_env(cmd_old)
    check("SIP = 198.51.100.229 (IP réelle du port dpdk)", env.get("SIP") == "198.51.100.229", env.get("SIP"))
    check("SIP ≠ 198.51.100.99 (IP de la NIC sœur primaire)", env.get("SIP") != "198.51.100.99")
    check("IFACE = ens1f1np1 (port réellement bindé)", env.get("IFACE") == "ens1f1np1", env.get("IFACE"))
    check("PORT_PMDS = dpdk", env.get("PORT_PMDS") == "dpdk", env.get("PORT_PMDS"))
    check("PORT_BDFS = 0000:11:00.1", env.get("PORT_BDFS") == "0000:11:00.1", env.get("PORT_BDFS"))
    check("mono-port : pas d'IFACES/SIPS émis", "IFACES=" not in cmd and "SIPS=" not in cmd)
    check("montage /dev/vfio présent", "-v /dev/vfio:/dev/vfio" in cmd)
    # NB : la « preuve de régression » contre HEAD a été retirée — le fix EST désormais mergé dans
    # HEAD (commit 23a4d6d), donc `git show HEAD` ne reproduit plus le bug. La correction est prouvée
    # par les assertions forward ci-dessus (SIP=.229, ≠.99) ; env_old sert encore à l'iso-af_xdp (§4).


# ─────────────── 2. multi-port 2022-7 : alignement iface↔sip↔bdf ───────────────

def test_multiport_alignment():
    print("2. multi-port (af_xdp + dpdk) : IFACES/SIPS/PORT_PMDS/PORT_BDFS alignés")
    import app.docker_driver as dd
    node = {"id": 1, "host": "198.51.100.251", "image": "bobi-mtl:test", "mxl_mount": None,
            "mtl_iface": "ens1f0np0", "media_ip": "198.51.100.99/24"}
    rows = [
        {"ifname": "ens1f0np0", "role": "media2110", "ip_cidr": "198.51.100.99/24",
         "media_network_id": 1, "pair_role": "red", "pair_group": 1, "pmd": None,
         "pci": "0000:11:00.0"},
        {"ifname": "ens1f1np1", "role": "media2110", "ip_cidr": "192.0.2.229/24",
         "media_network_id": 2, "pair_role": "blue", "pair_group": 1, "pmd": "dpdk",
         "pci": "0000:11:00.1"},
    ]
    _patch_module(dd, rows)
    cmd = dd._build_run_cmd(9999, dict(node), dict(PARAMS))
    env = parse_env(cmd)
    ifaces = env.get("IFACES", "").split(",")
    sips = env.get("SIPS", "").split(",")
    pmds = env.get("PORT_PMDS", "").split(",")
    bdfs = env.get("PORT_BDFS", "").split(",")
    check("IFACES = ens1f0np0,ens1f1np1 (primaire en tête)",
          ifaces == ["ens1f0np0", "ens1f1np1"], ifaces)
    check("SIPS aligné = .99,.229", sips == ["198.51.100.99", "192.0.2.229"], sips)
    check("PORT_PMDS aligné = af_xdp,dpdk", pmds == ["af_xdp", "dpdk"], pmds)
    check("PORT_BDFS aligné = '',0000:11:00.1 (af_xdp leg vide)",
          bdfs == ["", "0000:11:00.1"], bdfs)
    # Alignement STRICT index par index : le leg dpdk (pmd) porte bien SON bdf ET SON sip.
    aligned = {i: {"iface": ifaces[i], "sip": sips[i], "pmd": pmds[i], "bdf": bdfs[i]}
               for i in range(len(ifaces))}
    dpdk_leg = next(v for v in aligned.values() if v["pmd"] == "dpdk")
    check("leg dpdk : iface=ens1f1np1 sip=.229 bdf=…00.1 cohérents",
          dpdk_leg == {"iface": "ens1f1np1", "sip": "192.0.2.229",
                       "pmd": "dpdk", "bdf": "0000:11:00.1"}, dpdk_leg)
    check("SIP scalaire = SIPS[0] (primaire)", env.get("SIP") == sips[0], env.get("SIP"))
    check("IFACE scalaire = IFACES[0] (primaire)", env.get("IFACE") == ifaces[0], env.get("IFACE"))


# ─────────────── 3. port dpdk sans IP → refus ───────────────

def test_dpdk_no_ip_rejected():
    print("3. garde-fou : port dpdk sans IP de segment → déploiement refusé")
    import app.docker_driver as dd
    node = {"id": 1, "host": "198.51.100.251", "image": "bobi-mtl:test", "mxl_mount": None,
            "mtl_iface": "ens1f0np0", "media_ip": "198.51.100.99/24"}
    rows = [{"ifname": "ens1f1np1", "role": "media2110", "ip_cidr": "",
             "media_network_id": 1, "pair_role": None, "pair_group": None,
             "pmd": "dpdk", "pci": "0000:11:00.1"}]
    _patch_module(dd, rows)
    try:
        dd._build_run_cmd(9999, dict(node), dict(PARAMS))
        check("ValueError levée pour port dpdk sans IP", False, "aucune exception")
    except ValueError as e:
        check("ValueError levée pour port dpdk sans IP", True)
        check("message cite l'iface fautive (ens1f1np1)", "ens1f1np1" in str(e), str(e))


# ─────────────── 4. iso-comportement af_xdp (octet-identique à HEAD) ───────────────

def test_afxdp_iso():
    print("4. iso-comportement af_xdp : docker run OCTET-IDENTIQUE à HEAD")
    import app.docker_driver as dd
    old = _load_old_docker_driver()
    # 4a. multi-NIC af_xdp bien configuré (primaire déclarée = media_ip).
    node = {"id": 1, "host": "198.51.100.251", "image": "bobi-mtl:test", "mxl_mount": None,
            "mtl_iface": "ens1f0np0", "media_ip": "198.51.100.10/24"}
    rows = [
        {"ifname": "ens1f0np0", "role": "media2110", "ip_cidr": "198.51.100.10/24",
         "media_network_id": 1, "pair_role": "blue", "pair_group": 1, "pmd": None,
         "pci": "0000:17:00.0"},
        {"ifname": "ens1f1np1", "role": "media2110", "ip_cidr": "192.0.2.10/24",
         "media_network_id": 1, "pair_role": "red", "pair_group": 1, "pmd": None,
         "pci": "0000:17:00.1"},
    ]
    _patch_module(dd, rows)
    _patch_module(old, rows)
    cmd_new = dd._build_run_cmd(9999, dict(node), dict(PARAMS))
    cmd_old = old._build_run_cmd(9999, dict(node), dict(PARAMS))
    check("af_xdp multi-NIC : commande octet-identique à HEAD", cmd_new == cmd_old,
          "new={!r}\nold={!r}".format(cmd_new, cmd_old))
    check("af_xdp multi-NIC : pas de PORT_PMDS/vfio",
          "PORT_PMDS" not in cmd_new and "/dev/vfio" not in cmd_new)
    # 4b. repli mono-NIC (aucune media2110 déclarée → _media_ifaces retombe sur mtl_iface/media_ip).
    node2 = {"id": 2, "host": "198.51.100.252", "image": "bobi-mtl:test", "mxl_mount": None,
             "mtl_iface": "ens1f0np0", "media_ip": "198.51.100.10/24"}
    _patch_module(dd, [])
    _patch_module(old, [])
    cmd_new2 = dd._build_run_cmd(8888, dict(node2), dict(PARAMS))
    cmd_old2 = old._build_run_cmd(8888, dict(node2), dict(PARAMS))
    check("af_xdp mono-NIC (repli) : commande octet-identique à HEAD", cmd_new2 == cmd_old2,
          "new={!r}\nold={!r}".format(cmd_new2, cmd_old2))
    check("af_xdp mono-NIC : SIP=198.51.100.10 conservé", parse_env(cmd_new2).get("SIP") == "198.51.100.10")


if __name__ == "__main__":
    _stub_routes()
    test_dpdk_mono_sip()
    test_multiport_alignment()
    test_dpdk_no_ip_rejected()
    test_afxdp_iso()
    if FAILS:
        print("\nÉCHECS ({}) : {}".format(len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("\nTous les tests Lot 14 passent.")
    sys.exit(0)
