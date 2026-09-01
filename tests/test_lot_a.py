# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Tests offline du Lot A (chantier DPDK) : plans vfio (app/mtl.py), runner injectable,
préflight PTP Phase 1 (app/ptp.py:render_switch_plan).

100 % hors ligne : DB sqlite TEMPORAIRE (schéma minimal node_interfaces), settings mockés,
runner mocké — n'ouvre JAMAIS la DB de prod, aucun SSH/agent, aucun accès réseau.

Usage :
    /opt/bobistudio/venv/bin/python tests/test_lot_a.py          # sans pytest
    /opt/bobistudio/venv/bin/python -m pytest tests/test_lot_a.py -v   # si pytest présent
"""
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ─── DB temporaire AVANT toute utilisation (jamais la DB de prod) ─────────────
_TMPDIR = tempfile.mkdtemp(prefix="bobi-test-lot-a-")
_DB = os.path.join(_TMPDIR, "test.db")

import app.database as database  # noqa: E402
database.DB_PATH = _DB           # get_db() lit le global du module → surchargé ici

from app import mtl  # noqa: E402
from app import ptp  # noqa: E402
from app import settings as settings_mod  # noqa: E402

# Settings mockés (render_switch_plan lit ptp_domain/ptp_hw_ts/ptp_client_only)
_FAKE_SETTINGS = {"ptp_domain": 127, "ptp_hw_ts": True, "ptp_client_only": True}
settings_mod.setting_for = lambda key, node_id=None: _FAKE_SETTINGS.get(key)

NODE = {"id": 1, "host": "10.0.0.1", "name": "dl360-test"}


def _seed_db():
    """Schéma minimal node_interfaces + banc type dl360-1 (E810 bi-port, paire 2022-7,
    PTP sur f1, mgmt tg3)."""
    db = sqlite3.connect(_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS node_interfaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER NOT NULL,
        ifname TEXT, mac TEXT, pci TEXT, role TEXT NOT NULL DEFAULT 'unused',
        pair_role TEXT, pair_group INTEGER, ip_cidr TEXT, gateway TEXT, vlan TEXT,
        ptp_enabled INTEGER DEFAULT 0, mtu INTEGER, notes TEXT, created_at TEXT,
        ptp_domain INTEGER, model TEXT, speed_mbps INTEGER,
        rx_reserve INTEGER, tx_reserve INTEGER, pmd TEXT)""")
    # `settings` : le plan lit un réglage depuis que la vérification passe par `settings.get`.
    # Une table VIDE suffit — on veut les défauts du code, pas des valeurs de site — mais elle
    # doit EXISTER, sinon le test meurt sur « no such table » au lieu de vérifier quoi que ce soit.
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("DELETE FROM node_interfaces")
    # ⚠ `pair_role` EST OBLIGATOIRE pour qu'une paire 2022-7 existe. Le jeu d'essai ne posait
    # qu'un `pair_group`, ce qui suffisait AVANT que `vfio_bind_plan` ne soit corrigé : il
    # annonçait alors une « paire 2022-7 incohérente » à propos d'une paire INEXISTANTE (dl360-1
    # portait un groupe sans aucun rôle). Depuis, le garde exige groupe ET rôle des deux côtés,
    # et ce jeu d'essai ne décrivait plus aucune paire — le contrôle ne pouvait que rater.
    rows = [
        # (ifname, pci, role, pair_group, pair_role, ptp_enabled, pmd)
        ("ens1f0np0", "0000:12:00.0", "media2110", 1, "red",  0, None),   # candidat vfio
        ("ens1f1np1", "0000:12:00.1", "media2110", 1, "blue", 1, None),   # son pair, resté kernel
        ("eno1",      "0000:02:00.0", "management", None, None, 0, None), # mgmt (refus)
        ("ens2f0",    "0000:37:00.0", "media2110", None, None, 0, "dpdk"),# déjà vfio (checklist)
    ]
    for ifn, pci, role, pg, pr, ptp_en, pmd in rows:
        db.execute("INSERT INTO node_interfaces (node_id, ifname, pci, role, pair_group,"
                   " pair_role, ptp_enabled, pmd) VALUES (1,?,?,?,?,?,?,?)",
                   (ifn, pci, role, pg, pr, ptp_en, pmd))
    db.commit()
    db.close()


_seed_db()


# ─── vfio_bind_plan ───────────────────────────────────────────────────────────

def test_bind_plan_contenu_et_idempotence():
    script, checks = mtl.vfio_bind_plan(NODE, "0000:12:00.0")
    # Idempotence textuelle : le driver courant est lu (readlink) et vfio-pci → skip
    assert 'readlink "$DEV/driver"' in script
    assert '"$CUR" = "vfio-pci"' in script and "@@SKIP" in script
    # Séquence bind complète
    assert "modprobe vfio-pci" in script
    assert 'echo vfio-pci > "$DEV/driver_override"' in script
    assert '/unbind' in script
    assert "drivers_probe" in script
    # Vérification post-bind (échec explicite si le driver n'est pas vfio-pci)
    assert '"$NOW" = "vfio-pci"' in script and "@@ERR" in script
    # Persistance au boot : fichier + unité oneshot enabled
    assert mtl.VFIO_BINDS_PATH in script
    assert mtl.VFIO_BIND_UNIT_PATH in script
    # ★★★ GARDE DE NON-RÉGRESSION — NE PAS « RÉPARER » EN REMETTANT `enable`.
    # Ce contrôle exigeait `systemctl enable`. Il a été RETIRÉ du produit le 2026-08-28 : il
    # entraîne un `daemon-reload`, lequel réapplique la politique de périphériques des cgroups
    # et RÉVOQUE le GPU des conteneurs DÉJÀ EN MARCHE. Un moteur redémarré le 2026-08-19 a
    # ainsi tué le monitoring d'un utilisateur, relancé en boucle pendant NEUF JOURS.
    # Le lien d'activation est posé À LA MAIN, et seulement s'il manque.
    # Le contrôle est donc INVERSÉ : il échoue si `enable` revient.
    assert f"systemctl enable {mtl.VFIO_BIND_UNIT}" not in script, \
        "systemctl enable est proscrit ici — cf. la panne GPU de 9 jours (mtl.py:1255)"
    assert mtl.VFIO_BIND_WANTS_PATH in script and "ln -sf" in script, \
        "l'activation doit se faire par un lien posé à la main dans multi-user.target.wants"
    assert "Type=oneshot" in script and "RemainAfterExit=yes" in script
    # Deux exécutions du même plan = même script (fonction pure)
    script2, _ = mtl.vfio_bind_plan(NODE, "0000:12:00.0")
    assert script == script2
    # Checks : éligibilité + persistance + warning pair 2022-7 laissé en kernel
    assert any(c.startswith("ok:") for c in checks), checks
    assert any("persistance" in c for c in checks), checks
    assert any("pair 2022-7" in c and "ens1f1np1" in c for c in checks), checks


def test_bind_plan_refuse_nic_ptp():
    try:
        mtl.vfio_bind_plan(NODE, "0000:12:00.1")
    except mtl.GardeFouVfio as e:
        assert "ptp_enabled" in str(e)
    else:
        raise AssertionError("GardeFouVfio attendu pour la NIC ptp_enabled")


def test_bind_plan_refuse_nic_management():
    try:
        mtl.vfio_bind_plan(NODE, "0000:02:00.0")
    except mtl.GardeFouVfio as e:
        assert "management" in str(e)
    else:
        raise AssertionError("GardeFouVfio attendu pour la NIC de management")


def test_bind_plan_bdf_invalide_et_normalisation():
    for bad in ("", "n'importe quoi", "0000:12:00", "12:00.9"):
        try:
            mtl.vfio_bind_plan(NODE, bad)
        except mtl.GardeFouVfio:
            pass
        else:
            raise AssertionError(f"GardeFouVfio attendu pour {bad!r}")
    # Forme courte + majuscules → normalisée (et matche la ligne node_interfaces)
    script, checks = mtl.vfio_bind_plan(NODE, "12:00.0".upper())
    assert 'BDF="0000:12:00.0"' in script
    assert any(c.startswith("ok:") for c in checks)


def test_bind_plan_bdf_inconnu_warning():
    script, checks = mtl.vfio_bind_plan(NODE, "0000:99:00.0")
    assert any("absent de node_interfaces" in c for c in checks), checks
    assert "0000:99:00.0" in script


# ─── vfio_unbind_plan (symétrie / rollback) ───────────────────────────────────

def test_unbind_plan_symetrie():
    script, checks = mtl.vfio_unbind_plan(NODE, "0000:12:00.0")
    # Retour au driver kernel ice, avec le même schéma idempotent readlink→skip
    assert 'readlink "$DEV/driver"' in script
    assert '"$CUR" = "ice"' in script and "@@SKIP" in script
    # driver_override VIDÉ (echo > …), unbind du driver courant, re-probe, vérif ice
    assert re.search(r'^\s*echo > "\$DEV/driver_override"$', script, re.M), script
    assert '/unbind' in script
    assert "drivers_probe" in script
    assert '"$NOW" = "ice"' in script and "@@ERR" in script
    # Suppression de la persistance : retrait du BDF + purge unité si fichier vide
    assert f"sed -i" in script and mtl.VFIO_BINDS_PATH in script
    # Symétrique du bind : pas de `systemctl disable` (même raison), on retire le lien.
    assert f"systemctl disable {mtl.VFIO_BIND_UNIT}" not in script, \
        "systemctl disable entraîne lui aussi un reload — on retire le lien à la main"
    assert mtl.VFIO_BIND_WANTS_PATH in script
    # `rm -f` purge les trois chemins d'un coup (lien, unité, liste de BDF) : chercher
    # « rm -f <unité> » en un seul morceau ne correspond plus au texte produit.
    assert "rm -f " in script and mtl.VFIO_BIND_UNIT_PATH in script
    assert checks and "rollback" in checks[0]
    # Le rollback n'est JAMAIS refusé, même sur la NIC PTP ou management
    mtl.vfio_unbind_plan(NODE, "0000:12:00.1")
    mtl.vfio_unbind_plan(NODE, "0000:02:00.0")


# ─── Runner injectable (verifier / apply à sec) ───────────────────────────────

_CANNED_VERIF = """@@CMDLINE_LIVE
BOOT_IMAGE=/vmlinuz root=/dev/sda1 intel_iommu=on iommu=pt default_hugepagesz=1G hugepagesz=1G hugepages=16
@@CMDLINE_PENDING
root=/dev/sda1 intel_iommu=on iommu=pt default_hugepagesz=1G hugepagesz=1G hugepages=16
@@IOMMU_GROUPS
42
@@HUGE
HugePages_Total:      16
Hugepagesize:    1048576 kB
@@PDPE1GB
pdpe1gb
@@ICE
filename: /lib/modules/x/ice.ko
@@VFIO
filename: /lib/modules/x/vfio-pci.ko
@@DDP
ice-1.3.30.0.pkg
@@BOOTLOADER
grub
@@RDMA_UNIT
enabled
@@RDMA_MODE
netns exclusive
@@NICS
ens1f0np0|0x8086|0x1593|ice|0000:12:00.0|16.0 GT/s PCIe|8
@@END
"""


def test_verifier_runner_injecte():
    calls = []

    def fake_run(cmd, input_data=None, timeout=300):
        calls.append(cmd)
        return 0, _CANNED_VERIF, ""

    s = mtl.verifier("10.0.0.1", run=fake_run)
    assert len(calls) == 1, "verifier doit faire UNE probe via le runner injecté"
    assert s["error"] is None
    assert s["iommu_cmdline"] and s["iommu_active"]
    assert s["hugepages_total"] == 16 and s["hugepages_size_ok"]
    assert s["ice_present"] and s["vfio_present"]
    assert s["bootloader"] == "grub"
    assert not s["reboot_needed"]          # pending ⊆ live (tokens gérés)
    assert s["mtl_capable"] and s["nics"][0]["family"] == "e810"


def test_vfio_apply_a_sec_via_runner():
    seen = {}

    def fake_run(cmd, input_data=None, timeout=300):
        seen["script"] = cmd
        return 0, "@@OK 0000:12:00.0 lie a vfio-pci (etait: ice)\n@@PERSIST x", ""

    ok, msg, checks = mtl.vfio_bind_apply(NODE, "0000:12:00.0", run=fake_run)
    assert ok, msg
    assert "vfio-pci" in msg
    plan_script, _ = mtl.vfio_bind_plan(NODE, "0000:12:00.0")
    assert seen["script"] == plan_script, "apply doit exécuter EXACTEMENT le plan"
    # Échec remonté proprement
    ok2, msg2, _ = mtl.vfio_bind_apply(
        NODE, "0000:12:00.0",
        run=lambda c, input_data=None, timeout=300: (2, "@@ERR bind vfio-pci echoue (driver=ice)", ""))
    assert not ok2 and "bind vfio-pci echoue" in msg2


# ─── render_switch_plan (préflight PTP Phase 1) ───────────────────────────────

def test_render_switch_plan():
    plan = ptp.render_switch_plan(1, "ens1f0np0")
    assert plan["new_iface"] == "ens1f0np0"
    assert plan["current_ifaces"] == ["ens1f1np1"]
    assert plan["domain"] == 127 and plan["hw_ts"] is True
    # Conf ptp4l : domaine + profil SMPTE rendus par _ptp4l_conf
    assert "domainNumber              127" in plan["ptp4l_conf"]
    assert "clientOnly                1" in plan["ptp4l_conf"]
    # Unités : la NOUVELLE interface est câblée dans ptp4l ET phc2sys (hw_ts)
    assert "ptp4l -f" in plan["ptp4l_unit"] and "-i ens1f0np0" in plan["ptp4l_unit"]
    assert "phc2sys -s ens1f0np0" in plan["phc2sys_unit"]
    assert "-n 127" in plan["phc2sys_unit"]
    cl = "\n".join(plan["checklist"])
    # PHC partagé E810 détecté (0000:12:00.0 / 0000:12:00.1 = même carte)
    assert "PHC partagé E810" in cl and "ens1f1np1" in cl
    # ts-refclk SDP : inchangé tant que domaine/GM identiques
    assert "ts-refclk" in cl
    # Rien n'est posé
    assert "AUCUNE unité posée" in cl


def test_render_switch_plan_iface_dpdk_et_inconnue():
    plan = ptp.render_switch_plan(1, "ens2f0")     # pmd=dpdk → PTP impossible
    assert any("pmd=dpdk" in c for c in plan["checklist"]), plan["checklist"]
    plan2 = ptp.render_switch_plan(1, "ethX")      # inconnue de node_interfaces
    assert any("absente de node_interfaces" in c for c in plan2["checklist"])
    # PHC différent signalé quand la carte n'est pas la même
    plan3 = ptp.render_switch_plan(1, "eno1")
    assert any("PHC différent" in c for c in plan3["checklist"]), plan3["checklist"]


# ─── Lanceur sans pytest ──────────────────────────────────────────────────────

if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"OK    {name}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    sys.exit(1 if failed else 0)
