#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Verdict sur la section « disques locaux » de l'agent-nœud (`node_agent/agent.py`, `_dsk_*`).

HORS LIGNE ET SANS EFFET DE BORD : aucun lsblk, mkfs, mount ou hdparm réel n'est lancé. Les
commandes passent par la couture `_dsk_run`, remplacée par un FAUX SYSTÈME qui garde l'état
(disques, partitions, montages) et le journal des appels. fstab et hdparm.conf sont des fichiers
temporaires. Ce que l'on contrôle surtout : qu'un refus n'a lancé AUCUNE commande destructive.

    ./venv/bin/python tests/verif_disques_noeud.py

`AGENT_PATH=<fichier>` fait tourner le banc sur une copie mutée de l'agent (cf. la preuve que
chaque garde mord : on casse la garde, le banc doit échouer).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.environ.get("AGENT_PATH") or os.path.join(RACINE, "node_agent", "agent.py")
FIX = os.path.join(RACINE, "tests", "fixtures_disques")

spec = importlib.util.spec_from_file_location("agent_sous_test", AGENT)
ag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ag)

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


DESTRUCTIVE = ("sfdisk", "mkfs.ext4", "mount", "umount", "hdparm-S")


class FauxSysteme:
    """lsblk/findmnt/mount/… à état. Les disques sont des dict ; les montages cible → uuid."""

    def __init__(self, fstab_path):
        self.fstab = fstab_path
        self.calls = []
        self.mounts = {}                  # cible -> uuid
        self.containers = {}              # nom -> [sources montées]
        self.umount_busy = False
        self.kernel_stale = False         # sfdisk réussit mais le noyau ne relit pas la table
        self.n = 0
        self.disks = []
        self.fs_by_uuid = {}
        self.udev_lag = 0                 # lectures lsblk pendant lesquelles l'UUID neuf reste invisible
        self.blkid_ok = True              # blkid -p sait lire le superbloc

    # — construction du parc —
    def add_disk(self, name, size, serial, rota=False, tran="nvme", parts=(), ro=False):
        d = {"name": name, "path": "/dev/" + name, "type": "disk", "size": size, "model": "FAKE",
             "serial": serial, "tran": tran, "rota": rota, "ro": ro, "rm": False,
             "fstype": None, "uuid": None, "label": None, "mp": [], "children": [], "pending": False}
        for (pname, psize, fstype, uuid, mp, kids) in parts:
            d["children"].append({"name": pname, "path": "/dev/" + pname, "type": "part", "size": psize,
                                  "fstype": fstype, "uuid": uuid, "label": None, "mp": list(mp),
                                  "children": kids})
        self.disks.append(d)
        return d

    def _render(self, n, disk=False):
        uuid = n.get("uuid")
        if n.get("udev_hide", 0) > 0:     # udev n'a pas encore relu le superbloc écrit par mkfs
            n["udev_hide"] -= 1
            uuid = None
        mps = list(n.get("mp") or [])
        if n.get("uuid"):
            mps += [t for t, u in self.mounts.items() if u == n["uuid"]]
        out = {"name": n["name"], "path": n["path"], "kname": n["name"], "type": n["type"],
               "size": n["size"], "model": n.get("model"), "serial": n.get("serial"),
               "tran": n.get("tran"), "rota": n.get("rota", False), "ro": n.get("ro", False),
               "rm": False, "fstype": n.get("fstype"), "uuid": uuid, "label": n.get("label"),
               "mountpoints": mps or [None], "pkname": None}
        if n.get("children"):
            out["children"] = [self._render(c) for c in n["children"]]
        return out

    # — la couture —
    def run(self, argv, timeout=60, input_bytes=None):
        a = list(argv)
        c = a[0]
        key = c + ("-S" if c == "hdparm" and "-S" in a else "")
        self.calls.append((key, a))
        if c == "lsblk":
            tree = [self._render(d) for d in self.disks if not (d["pending"] and False)]
            return 0, json.dumps({"blockdevices": tree}), ""
        if c == "findmnt":
            t = a[-1]
            u = self.mounts.get(t)
            if not u:
                return 1, "", ""
            return 0, ("/dev/x %s\n" % u if "SOURCE,UUID" in a else "%s\n" % u), ""
        if c == "mount":
            t = a[1]
            for ln in open(self.fstab).read().splitlines():
                p = ln.split()
                if len(p) >= 3 and not ln.startswith("#") and p[1] == t and p[0].startswith("UUID="):
                    self.mounts[t] = p[0][5:]
                    return 0, "", ""
            return 32, "", "mount: can't find %s in fstab" % t
        if c == "umount":
            if self.umount_busy:
                return 32, "", "target is busy"
            self.mounts.pop(a[1], None)
            for d in self.disks:
                for p in d["children"]:
                    if a[1] in p["mp"]:
                        p["mp"].remove(a[1])
            return 0, "", ""
        if c == "sfdisk":
            dev = a[-1]
            d = next(x for x in self.disks if x["path"] == dev)
            if self.kernel_stale:
                return 0, "", ""
            self.n += 1
            sfx = ("p1" if d["name"][-1].isdigit() else "1")
            d["children"] = [{"name": d["name"] + sfx, "path": d["path"] + sfx, "type": "part",
                              "size": d["size"] - 2 ** 20, "fstype": None, "uuid": None, "label": None,
                              "mp": [], "children": []}]
            return 0, "", ""
        if c == "mkfs.ext4":
            part = a[-1]
            for d in self.disks:
                for p in d["children"]:
                    if p["path"] == part:
                        self.n += 1
                        p["fstype"], p["uuid"] = "ext4", "uuid-new-%d" % self.n
                        p["label"] = a[a.index("-L") + 1]
                        p["udev_hide"] = self.udev_lag
                        return 0, "", ""
            return 1, "", "no such partition"
        if c == "blkid":
            for d in self.disks:
                for p in d["children"]:
                    if p["path"] == a[-1] and p.get("uuid") and self.blkid_ok:
                        return 0, p["uuid"] + "\n", ""
            return 2, "", ""
        if c == "docker":
            if a[1] == "ps":
                return 0, " ".join(self.containers) + "\n", ""
            return 0, "".join("/%s|%s\n" % (n, ";".join(s) + ";") for n, s in self.containers.items()), ""
        if c == "hdparm":
            if "-C" in a:
                return 0, "\n/dev/sda:\n drive state is:  standby\n", ""
            return 0, "", ""
        return 0, "", ""                  # udevadm, partprobe…


def monde():
    """Le parc du cas d'usage : NVMe système en LVM, NVMe 2 To médias, HDD 1 To archive."""
    tmp = tempfile.mkdtemp(prefix="dsk_")
    fstab = os.path.join(tmp, "fstab")
    shutil.copy(os.path.join(FIX, "fstab_debian.txt"), fstab)
    ag._DSK_FSTAB = fstab
    ag._DSK_HDPARM_CONF = os.path.join(tmp, "hdparm.conf")
    ag.CONFIG["media_mount"] = os.path.join(tmp, "srv", "mxl-media")
    ag._DSK_DEFAULT_ARCHIVE = os.path.join(tmp, "srv", "archive")
    ag._DSK_MOUNT_PREFIXES = (tmp + "/",)
    sysm = FauxSysteme(fstab)
    lv_root = {"name": "vg-root", "path": "/dev/mapper/vg-root", "type": "lvm", "size": 400 * 10 ** 9,
               "fstype": "ext4", "uuid": "u-root", "mp": ["/"], "children": []}
    lv_swap = {"name": "vg-swap", "path": "/dev/mapper/vg-swap", "type": "lvm", "size": 8 * 10 ** 9,
               "fstype": "swap", "uuid": "u-swap", "mp": ["[SWAP]"], "children": []}
    sysm.add_disk("nvme0n1", 512 * 10 ** 9, "SYS-512", parts=[
        ("nvme0n1p1", 10 ** 9, "vfat", "EFI-1", ["/boot/efi"], []),
        ("nvme0n1p2", 511 * 10 ** 9, "LVM2_member", "u-pv", [], [lv_root, lv_swap])])
    sysm.add_disk("nvme1n1", 2 * 10 ** 12, "MEDIA-2T")
    sysm.add_disk("sda", 10 ** 12, "HDD-1T", rota=True, tran="sata")
    ag._dsk_run = sysm.run
    ag._dsk_which = lambda n: True
    ag._dsk_statvfs = lambda p: None
    ag._dsk_byid = lambda p: "/dev/disk/by-id/ata-FAKE_" + os.path.basename(p)
    ag.time.sleep = lambda s: None
    return sysm, tmp


def destructifs(sysm):
    return [k for (k, _a) in sysm.calls if k in DESTRUCTIVE]


def fstab_lignes(sysm):
    return [ln for ln in open(sysm.fstab).read().splitlines() if ln.strip() and not ln.startswith("#")]


def fmt(dev, serial, size, **kw):
    d = {"action": "format", "device": dev, "role": "media", "confirm_serial": serial, "confirm_size": size}
    d.update(kw)
    return d


# ── 1. Fixtures RÉELLES (nœuds du banc) : l'inventaire les comprend ─────────────────────────────
for nom, attendu in (("lsblk_dl360-1.json", {"sda": True, "sdb": False, "sdc": False}),
                     ("lsblk_r620-1.json", {"sda": True})):
    sysm, tmp = monde()
    brut = open(os.path.join(FIX, nom)).read()
    ag._dsk_run = lambda argv, timeout=60, input_bytes=None, _b=brut: (0, _b, "") if argv[0] == "lsblk" else (1, "", "")
    inv = {d["path"].split("/")[-1]: d for d in ag._dsk_inventory()}
    ok(set(inv) == set(attendu), "fixture %s : disques entiers seulement (pas de lecteur optique)" % nom, str(set(inv)))
    ok(all(inv[k]["system"] == v for k, v in attendu.items()), "fixture %s : drapeau system correct" % nom,
       str({k: inv[k]["system"] for k in inv}))
    ok(all(inv[k]["serial"] for k in inv), "fixture %s : séries lus" % nom)
    shutil.rmtree(tmp)

# ── 2. Disque système refusé — dont root sur LVM ───────────────────────────────────────────────
sysm, tmp = monde()
inv = {d["path"]: d for d in ag._dsk_inventory()}
nv = inv["/dev/nvme0n1"]
ok(nv["system"] and nv["in_use"], "root sur LVM : le disque est system ET in_use", str(nv["mountpoints"]))
ok(not inv["/dev/nvme1n1"]["system"] and not inv["/dev/sda"]["system"], "disques de données non système")
for act, extra in (("format", {"role": "media"}), ("mount", {"role": "data", "mountpoint": tmp + "/x"}),
                   ("unmount", {}), ("spindown", {"minutes": 10})):
    r = ag._dsk_execute(dict({"action": act, "device": "/dev/nvme0n1", "confirm_serial": "SYS-512",
                              "confirm_size": 512 * 10 ** 9}, **extra))
    ok(not r["ok"] and "system_disk" in r.get("refusals", []), "disque système refusé : %s (même série+taille exactes)" % act, str(r))
ok(destructifs(sysm) == [], "disque système : aucune commande destructive lancée", str(destructifs(sysm)))
# swap seul (sans montage racine) suffit aussi
sysm.disks[0]["children"][1]["children"] = [sysm.disks[0]["children"][1]["children"][1]]
r = ag._dsk_plan({"action": "format", "device": "/dev/nvme0n1", "role": "data", "mountpoint": tmp + "/y"})
ok("system_disk" in r["refusals"], "swap sur le disque ⇒ système, même sans « / »")
shutil.rmtree(tmp)

# ── 3. Série / taille ────────────────────────────────────────────────────────────────────────
sysm, tmp = monde()
for titre, kw, code in (
        ("mauvaise série", {"confirm_serial": "WRONG"}, "serial_mismatch"),
        ("série vide", {"confirm_serial": ""}, "serial_mismatch"),
        ("série absente", {"confirm_serial": None}, "serial_mismatch"),
        ("série en minuscules (comparaison exacte)", {"confirm_serial": "media-2t"}, "serial_mismatch"),
        ("taille différente", {"confirm_size": 2 * 10 ** 12 + 1}, "size_mismatch"),
        ("taille absente", {"confirm_size": None}, "size_mismatch"),
        ("taille booléenne", {"confirm_size": True}, "size_mismatch")):
    d = fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12)
    d.update(kw)
    r = ag._dsk_execute(d)
    ok(not r["ok"] and code in r.get("refusals", []), "format refusé : %s" % titre, str(r))
ok(destructifs(sysm) == [], "refus de confirmation : aucune commande destructive", str(destructifs(sysm)))
sysm.disks[1]["serial"] = None
r = ag._dsk_plan(fmt("/dev/nvme1n1", "", 2 * 10 ** 12))
ok("serial_missing" in r["refusals"], "disque sans série : refusé (rien à saisir)")
sysm.disks[1]["serial"] = "MEDIA-2T"
r = ag._dsk_execute(fmt("/dev/nvme1n1; rm -rf /", "MEDIA-2T", 2 * 10 ** 12))
ok(r.get("refusals") == ["disk_not_found"], "périphérique hors inventaire : refusé", str(r))
ok(ag._dsk_plan({"action": "format", "device": "/dev/nvme1n1", "role": "media"})["disk"]["serial"] == "MEDIA-2T",
   "plan : annonce la série à saisir")
ok(destructifs(sysm) == [], "plan et refus : toujours rien de destructif")
shutil.rmtree(tmp)

# ── 4. Formatage nominal : GPT, ext4 + label, fstab par UUID nofail/noatime, rôle media ────────
sysm, tmp = monde()
media = ag.CONFIG["media_mount"]
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, label="mxl-media"))
ok(r["ok"], "format nominal", str(r))
ordre = [k for (k, _a) in sysm.calls if k in ("sfdisk", "mkfs.ext4", "mount")]
ok(ordre == ["sfdisk", "mkfs.ext4", "mount"], "ordre : partition → mkfs → mount", str(ordre))
mk = next(a for (k, a) in sysm.calls if k == "mkfs.ext4")
ok(mk[-1] == "/dev/nvme1n1p1" and mk[mk.index("-L") + 1] == "mxl-media", "mkfs.ext4 -L sur la partition NVMe (p1)", str(mk))
sf = next(a for (k, a) in sysm.calls if k == "sfdisk")
ok(sf[-1] == "/dev/nvme1n1", "sfdisk vise le disque entier")
lg = fstab_lignes(sysm)
mine = [x for x in lg if x.startswith("UUID=uuid-new-")]
ok(len(mine) == 1 and media in mine[0], "fstab : UNE ligne par UUID vers media_mount", str(mine))
ok("nofail" in mine[0] and "noatime" in mine[0], "fstab : options nofail,noatime", str(mine))
ok(sysm.mounts.get(media, "").startswith("uuid-new-"), "monté sur media_mount (rôle media) et vérifié par findmnt")
ok(all(x in open(sysm.fstab).read() for x in ("UUID=fd9cbd8b-e602-4e3b-a68e-eef67c6b3924",)), "lignes du système conservées")
ok(os.path.isdir(media), "cible créée (mkdir)")

# ── 5. Remontage idempotent ─────────────────────────────────────────────────────────────────
n0 = len(sysm.calls)
for titre, bad in (("sans confirm", {"confirm": None}), ("confirm=False", {"confirm": False}),
                   ("confirm en chaîne", {"confirm": "true"})):
    r = ag._dsk_execute({"action": "mount", "device": "/dev/nvme1n1", "partition": "/dev/nvme1n1p1",
                         "role": "media", **bad})
    ok(not r["ok"] and r.get("refusals") == ["confirm_required"], "montage %s : refusé" % titre, str(r))
ok(all(k not in DESTRUCTIVE for (k, _a) in sysm.calls[n0:]), "montage refusé faute de confirm : rien lancé")
nb_mount = sum(1 for k in destructifs(sysm) if k == "mount")
part = ag._dsk_inventory()[1]
mreq = {"action": "mount", "device": "/dev/nvme1n1", "partition": "/dev/nvme1n1p1", "role": "media",
        "confirm": True}
avant = open(sysm.fstab).read()
r1 = ag._dsk_execute(mreq)
r2 = ag._dsk_execute(mreq)
ok(r1["ok"] and r2["ok"], "remontage ×2 : succès", str((r1, r2)))
ok(open(sysm.fstab).read() == avant, "remontage ×2 : fstab inchangé octet pour octet")
ok(sum(1 for k in destructifs(sysm) if k == "mount") == nb_mount, "remontage : aucun nouvel appel mount (déjà monté)")
ok(not r1["changed"], "remontage : changed=false")

# ── 6. Disque monté : format refusé ; démontage retire la ligne ──────────────────────────────
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(not r["ok"] and "in_use" in r["refusals"], "disque monté : format refusé (in_use)", str(r))
n_sfd = sum(1 for k in destructifs(sysm) if k == "sfdisk")
ok(n_sfd == 1, "disque monté : pas de second sfdisk")
ureq = {"action": "unmount", "device": "/dev/nvme1n1", "partition": "/dev/nvme1n1p1",
        "confirm": True}
for titre, bad in (("sans confirm", {"confirm": None}), ("confirm=False", {"confirm": False}),
                   ("confirm en chaîne", {"confirm": "true"}), ("confirm=1", {"confirm": 1})):
    r = ag._dsk_execute(dict(ureq, **bad))
    ok(not r["ok"] and r.get("refusals") == ["confirm_required"] and media in sysm.mounts,
       "démontage %s : refusé, toujours monté" % titre, str(r))
ok("confirm_serial" not in ureq, "démontage : aucune série n'est demandée")
r = ag._dsk_execute(ureq)
ok(r["ok"] and media not in sysm.mounts, "démontage : umount effectué", str(r))
ok(not any(x.startswith("UUID=uuid-new-") for x in fstab_lignes(sysm)), "démontage : ligne fstab retirée")
ok("bobi-storage role" not in open(sysm.fstab).read(), "démontage : marqueur retiré aussi")
ok(all(x in open(sysm.fstab).read() for x in ("fd9cbd8b", "6AAA-205C", "/dev/sr0")), "démontage : lignes du système intactes")
r = ag._dsk_execute(ureq)
ok(not r["ok"] and r["refusals"] == ["nothing_to_do"], "démontage rejoué : rien à faire")
sysm.umount_busy = True
ag._dsk_execute(dict(mreq))
r = ag._dsk_execute(ureq)
ok(not r["ok"] and r["error"] == "umount_busy" and "UUID=uuid-new-" in open(sysm.fstab).read(),
   "umount occupé : erreur, et la ligne fstab N'EST PAS retirée", str(r))
sysm.umount_busy = False
shutil.rmtree(tmp)

# ── 7. Archive, chemins refusés, cible occupée / non vide ───────────────────────────────────
sysm, tmp = monde()
r = ag._dsk_execute(fmt("/dev/sda", "HDD-1T", 10 ** 12, role="archive"))
ok(r["ok"] and r["target"] == ag._DSK_DEFAULT_ARCHIVE, "rôle archive : /srv/archive par défaut", str(r))
for bad in ("/etc", "/boot/efi", "/usr/x", "relatif", "/srv/../etc", tmp + "/a b", "/var/lib/docker/x", "/"):
    p = ag._dsk_plan(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, role="data", mountpoint=bad))
    ok("bad_mountpoint" in p["refusals"], "chemin refusé : %r" % bad, str(p["refusals"]))
p = ag._dsk_plan(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, role="media"))
ok("mount_busy" not in p["refusals"], "media : pas de conflit hors cas occupé")
# media déjà tenu par un autre disque
sysm.mounts[ag.CONFIG["media_mount"]] = "autre-uuid"
p = ag._dsk_plan(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, role="media"))
ok("mount_busy" in p["refusals"], "cible déjà montée par un autre volume : refus (pas de remplacement discret)")
del sysm.mounts[ag.CONFIG["media_mount"]]
os.makedirs(ag.CONFIG["media_mount"])
open(os.path.join(ag.CONFIG["media_mount"], "vieux.mxf"), "w").close()
p = ag._dsk_plan(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, role="media"))
ok("target_not_empty" in p["refusals"], "cible non vide : refus sans accord explicite")
p = ag._dsk_plan(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, role="media", allow_non_empty=True))
ok(not p["refusals"] and "target_not_empty" in p["warnings"], "cible non vide + accord : autorisé, averti")
# ligne fstab de l'exploitant déjà sur la cible
open(sysm.fstab, "a").write("/dev/sdz1 %s ext4 defaults 0 0\n" % ag._DSK_DEFAULT_ARCHIVE.replace("archive", "autre"))
shutil.rmtree(tmp)

# ── 8. Disque tenu par LVM/RAID, formatage refusé ───────────────────────────────────────────
sysm, tmp = monde()
sysm.add_disk("nvme2n1", 10 ** 12, "PV-1T", parts=[("nvme2n1p1", 10 ** 12 - 2 ** 20, "LVM2_member", "u-pv2", [], [])])
p = ag._dsk_plan(fmt("/dev/nvme2n1", "PV-1T", 10 ** 12, role="data", mountpoint=tmp + "/z"))
ok("held" in p["refusals"], "membre LVM non monté : refus (held)", str(p["refusals"]))
sysm.add_disk("sdc", 10 ** 12, "RO-1", tran="sata", ro=True)
ok("read_only" in ag._dsk_plan(fmt("/dev/sdc", "RO-1", 10 ** 12))["refusals"], "disque en lecture seule : refus")
shutil.rmtree(tmp)

# ── 9. Conteneurs média en cours ────────────────────────────────────────────────────────────
sysm, tmp = monde()
sysm.containers = {"recorder-1": [ag.CONFIG["media_mount"]], "autre": ["/etc/x"]}
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(not r["ok"] and "media_containers_running" in r["refusals"] and r["media_containers"] == ["recorder-1"],
   "format du volume média sous conteneurs qui le montent : refus signalé", str(r))
ok(destructifs(sysm) == [], "…sans rien toucher")
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12, ack_media_containers=True))
ok(r["ok"] and "media_containers_running" in r["warnings"], "avec accusé : exécuté, averti", str(r))
r = ag._dsk_execute({"action": "unmount", "device": "/dev/nvme1n1", "partition": "/dev/nvme1n1p1",
                     "confirm": True})
ok(not r["ok"] and "media_containers_running" in r["refusals"], "démontage du média sous conteneurs : refus sans accusé")
r = ag._dsk_execute({"action": "unmount", "device": "/dev/nvme1n1", "partition": "/dev/nvme1n1p1",
                     "confirm": True, "ack_media_containers": True})
ok(r["ok"] and r.get("restart_containers") == ["recorder-1"], "avec accusé : la liste à relancer est rendue", str(r))
shutil.rmtree(tmp)

# ── 10. Mise en veille ──────────────────────────────────────────────────────────────────────
sysm, tmp = monde()
ok([ag._dsk_spindown_value(m) for m in (0, 5, 20, 30, 60, 120)] == [0, 60, 240, 241, 242, 244], "table minutes → -S")
ok(ag._dsk_spindown_value(25) is None and ag._dsk_spindown_value(-1) is None
   and ag._dsk_spindown_value("5") is None and ag._dsk_spindown_value(360) is None, "minutes invalides refusées")
r = ag._dsk_execute({"action": "spindown", "device": "/dev/sda", "minutes": 10})
ok(r["ok"], "spindown HDD", str(r))
hd = next(a for (k, a) in sysm.calls if k == "hdparm-S")
ok(hd[:3] == ["hdparm", "-S", "120"] and hd[3] == "/dev/sda", "hdparm -S 120 /dev/sda", str(hd))
conf = open(ag._DSK_HDPARM_CONF).read()
ok("/dev/disk/by-id/ata-FAKE_sda {" in conf and "spindown_time = 120" in conf, "hdparm.conf par by-id", conf)
ag._dsk_execute({"action": "spindown", "device": "/dev/sda", "minutes": 30})
conf = open(ag._DSK_HDPARM_CONF).read()
ok(conf.count("begin") == 1 and "spindown_time = 241" in conf and "120" not in conf, "réglage remplacé, pas dupliqué", conf)
ok(ag._dsk_spindown_conf("/dev/disk/by-id/ata-FAKE_sda", ag._DSK_HDPARM_CONF) == 241, "valeur persistée relue")
ag._dsk_execute({"action": "spindown", "device": "/dev/sda", "minutes": 0})
ok("begin" not in open(ag._DSK_HDPARM_CONF).read(), "minutes=0 : bloc retiré")
r = ag._dsk_execute({"action": "spindown", "device": "/dev/nvme1n1", "minutes": 10})
ok("not_rotational" in r["refusals"], "spindown d'un NVMe refusé")
r = ag._dsk_execute({"action": "spindown", "device": "/dev/sda", "minutes": 7777})
ok("bad_minutes" in r["refusals"], "spindown : minutes invalides refusées")
lst = ag._dsk_list()
ok(next(d for d in lst["disks"] if d["path"] == "/dev/sda")["power_state"] == "standby", "état d'alimentation rapporté")
ok(next(d for d in lst["disks"] if d["path"] == "/dev/nvme1n1")["power_state"] is None, "NVMe : pas d'état d'alimentation")
shutil.rmtree(tmp)

# ── 11. Table non relue ⇒ reboot demandé, jamais automatique ─────────────────────────────────
sysm, tmp = monde()
sysm.kernel_stale = True
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(not r["ok"] and r["reboot_required"] and "mkfs.ext4" not in [k for (k, _a) in sysm.calls],
   "partition invisible : reboot_required, pas de mkfs à l'aveugle", str(r))
ok(not any(a[0] == "systemctl" or a[0] == "reboot" for (_k, a) in sysm.calls), "l'agent ne redémarre jamais seul")
shutil.rmtree(tmp)

# ── 11 bis. UUID pas encore vu par udev après mkfs (Z8, 2026-10-23) ──────────────────────────
# lsblk lit l'UUID dans la base d'udev : juste après mkfs il est vide. Le formatage abandonnait
# alors en « verify_failed: uuid », disque formaté mais ni fstab ni montage.
sysm, tmp = monde()
sysm.udev_lag = 3
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(r["ok"] and r.get("uuid", "").startswith("uuid-new-"), "udev en retard de 3 lectures : UUID relu, formatage abouti", str(r))
ok(any(x.startswith("UUID=" + r.get("uuid", "?")) for x in fstab_lignes(sysm)), "udev en retard : ligne fstab écrite")
ok(bool(r.get("uuid")) and sysm.mounts.get(ag.CONFIG["media_mount"]) == r["uuid"], "udev en retard : volume monté")
shutil.rmtree(tmp)

sysm, tmp = monde()
sysm.udev_lag = 10 ** 6
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(r["ok"] and r.get("uuid", "").startswith("uuid-new-"), "udev muet : UUID obtenu par blkid -p", str(r))
ok(any(k == "blkid" and "-p" in a for (k, a) in sysm.calls), "udev muet : blkid -p interrogé")
shutil.rmtree(tmp)

sysm, tmp = monde()
sysm.udev_lag = 10 ** 6
sysm.blkid_ok = False
avant = fstab_lignes(sysm)
r = ag._dsk_execute(fmt("/dev/nvme1n1", "MEDIA-2T", 2 * 10 ** 12))
ok(not r["ok"] and r["error"] == "verify_failed", "aucun UUID nulle part : verify_failed", str(r))
ok(fstab_lignes(sysm) == avant and not sysm.mounts, "aucun UUID : ni ligne fstab, ni montage")
shutil.rmtree(tmp)

# ── 12. HTTP ─────────────────────────────────────────────────────────────────────────────────
sysm, tmp = monde()
ok(ag._dsk_http("GET", "/v1/health", None) is None, "aiguillage : routes étrangères ignorées")
code, o = ag._dsk_http("GET", "/v1/host/disks", None)
ok(code == 200 and len(o["disks"]) == 3 and o["media_mount"] == ag.CONFIG["media_mount"], "GET /v1/host/disks")
code, o = ag._dsk_http("POST", "/v1/host/disks/plan", fmt("/dev/nvme1n1", "", 0))
ok(code == 200 and o["steps"] and destructifs(sysm) == [], "plan : décrit sans rien exécuter")
code, o = ag._dsk_http("POST", "/v1/host/disks/execute", [])
ok(code == 400, "corps non-objet : 400")
shutil.rmtree(tmp)

print()
if ko:
    print("%d ÉCHEC(S) :" % len(ko))
    for t in ko:
        print("  - %s" % t)
    sys.exit(1)
print("Tous les contrôles passent.")
