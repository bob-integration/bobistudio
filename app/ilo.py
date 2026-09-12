# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Client Redfish minimal multi-constructeur (BMC) — HPe iLO 5 ET Dell iDRAC 9 — pour le montage
automatique de l'ISO d'enrôlement en CD/DVD virtuel + boot one-time + redémarrage, plus la lecture
de l'état (power/health) affichée dans la vue réseau du nœud.

Flux `deploy_node()` : insère l'ISO servie par le contrôleur (cf. node_iso) dans le Virtual Media
CD du BMC, force le prochain boot sur CD (One-Time), puis (re)démarre le serveur. Le nœud vierge
boote alors l'installeur préseedé sans clé USB ni intervention sur l'interface BMC.

Le BMC présente un certificat auto-signé (réseau de gestion interne) → `verify=False`. Auth Basic.
Les chemins/IDs Redfish DIFFÈRENT par constructeur — sélectionnés par `node['bmc_vendor']`
('hpe' par défaut | 'dell'). Le module conserve son nom historique `ilo.py` ; les identifiants
restent transportés par les colonnes `ilo_host`/`ilo_user`/`ilo_password` (quel que soit le vendor).
"""
import logging

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

_TIMEOUT = 30

# Profils Redfish par constructeur. iLO 5 : Manager 1, Virtual Media index 2 = CD/DVD. iDRAC 9 :
# Manager iDRAC.Embedded.1, le Virtual Media CD est sous Systems (System.Embedded.1/VirtualMedia/CD).
_VENDORS = {
    "hpe": {
        "label":   "iLO",
        "cd":      "/redfish/v1/Managers/1/VirtualMedia/2",
        "sys":     "/redfish/v1/Systems/1",
        "manager": "/redfish/v1/Managers/1",
    },
    "dell": {
        "label":   "iDRAC",
        "cd":      "/redfish/v1/Systems/System.Embedded.1/VirtualMedia/CD",
        "sys":     "/redfish/v1/Systems/System.Embedded.1",
        "manager": "/redfish/v1/Managers/iDRAC.Embedded.1",
    },
}


def _vendor(node):
    v = (node.get("bmc_vendor") or "hpe").strip().lower()
    return v if v in _VENDORS else "hpe"


def _paths(node):
    return _VENDORS[_vendor(node)]


def _base(host):
    host = (host or "").strip().rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    return host


def _creds(node):
    host = (node.get("ilo_host") or "").strip()
    user = (node.get("ilo_user") or "").strip()
    pwd = node.get("ilo_password") or ""
    if not (host and user):
        raise ValueError("identifiants BMC incomplets (host/user requis)")
    return _base(host), (user, pwd)


def _req(method, node, path, token=None, **kw):
    base, auth = _creds(node)
    kw.setdefault("verify", False)
    kw.setdefault("timeout", _TIMEOUT)
    if token:
        # Réutilise une session ouverte (X-Auth-Token) au lieu d'une auth Basic (qui crée une session
        # iLO PAR requête → épuise le pool en rafale = NoValidSession).
        kw.setdefault("headers", {})["X-Auth-Token"] = token
    else:
        kw.setdefault("auth", auth)
    return requests.request(method, base + path, **kw)


def _session_open(node):
    """Ouvre UNE session Redfish (POST Sessions). Retourne (token, location) ou (None, None) en repli
    sur l'auth Basic. À fermer avec _session_close pour ne pas laisser de session pendre 30 min."""
    base, (user, pwd) = _creds(node)
    try:
        r = requests.post(base + "/redfish/v1/SessionService/Sessions",
                          json={"UserName": user, "Password": pwd}, verify=False, timeout=_TIMEOUT)
        if r.status_code in (200, 201):
            return r.headers.get("X-Auth-Token"), r.headers.get("Location")
    except Exception:
        pass
    return None, None


def _session_close(node, token, location):
    if not (token and location):
        return
    base, _ = _creds(node)
    url = location if str(location).startswith("http") else base + location
    try:
        requests.delete(url, headers={"X-Auth-Token": token}, verify=False, timeout=_TIMEOUT)
    except Exception:
        pass


def _err(r):
    """Extrait un message d'erreur Redfish lisible. Creuse @Message.ExtendedInfo (MessageId +
    Message + Resolution) car iLO renvoie souvent un message générique « See ExtendedInfo »."""
    try:
        j = r.json()
        err = j.get("error") or {}
        ext = err.get("@Message.ExtendedInfo") or j.get("@Message.ExtendedInfo") or []
        parts = []
        if isinstance(ext, list):
            for e in ext:
                mid = (e.get("MessageId") or "").split(".")[-1]
                m = e.get("Message") or ""
                res = e.get("Resolution") or ""
                seg = " ".join(s for s in [mid, m, res] if s and s != "None")
                if seg:
                    parts.append(seg)
        detail = " | ".join(parts) or err.get("message") or ""
        if detail:
            return f"{r.status_code} {detail}"
    except Exception:
        pass
    return f"{r.status_code} {(r.text or '')[:300]}"


def test_connection(node):
    """Ping Redfish (GET état du Virtual Media CD). Retourne (ok, msg). Signale aussi l'état de
    licence iLO (le montage Virtual Media par URL exige iLO Advanced ; Standard → KO)."""
    p = _paths(node)
    lbl = p["label"]
    try:
        r = _req("GET", node, p["cd"])
        if r.status_code != 200:
            return False, _err(r)
        j = r.json()
        inserted = j.get("Inserted")
        img = j.get("Image")
        lic = ""
        if _vendor(node) == "hpe":
            try:
                m = _req("GET", node, p["manager"]).json()
                lt = (((m.get("Oem") or {}).get("Hpe") or {}).get("License") or {}).get("LicenseType")
                if lt and lt != "Advanced":
                    lic = f" — ⚠ licence « {lt} » : Virtual Media par URL indisponible (iLO Advanced requis)"
                elif lt:
                    lic = f" — licence {lt}"
            except Exception:
                pass
        return True, f"{lbl} OK — CD virtuel {'monté: '+str(img) if inserted else 'libre'}{lic}"
    except Exception as e:
        return False, str(e)


def status(node):
    """État synthétique du BMC pour la tuile de la vue réseau : joignabilité, power, health.
    Retourne un dict {ok, vendor, label, reachable, power, health, error}. Best-effort (jamais lève)."""
    p = _paths(node)
    out = {"ok": False, "vendor": _vendor(node), "label": p["label"],
           "reachable": False, "power": None, "health": None, "error": None}
    if not (node.get("ilo_host") or "").strip():
        out["error"] = "non configuré"
        return out
    try:
        g = _req("GET", node, p["sys"])
        if g.status_code != 200:
            out["error"] = _err(g)
            return out
        j = g.json()
        out["reachable"] = True
        out["power"] = j.get("PowerState")
        out["health"] = ((j.get("Status") or {}).get("Health"))
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
    return out


def sriov_bios(node):
    """Vérifie via Redfish que le BIOS est réglé pour SR-IOV — prérequis des VF DPDK/narrow
    (PF kernel-PTP + VF DPDK, cf. docs/reference/PTP_CLOCK.md). Best-effort (jamais lève).
    Retourne {ok, ready, sriov_enabled, mmio, mmio_attr, hint, error}.
    - **HPe Gen10** : ready = `Sriov=Enabled` ET `PciResourcePadding=High`. Sans le padding, la
      création de VF échoue en `not enough MMIO resources for SR-IOV (-ENOMEM)` (fenêtre préfetchable
      32-bit saturée par les PF — vérifié dl360-1 2026-07-08). Il n'y a PAS de toggle « Above 4G »
      sur HPe : c'est `PciResourcePadding` qui réserve le MMIO.
    - **Dell** : ready = `SriovGlobalEnable=Enabled` ET `MmioAbove4GB=Enabled`."""
    p = _paths(node)
    out = {"ok": False, "ready": None, "sriov_enabled": None,
           "mmio": None, "mmio_attr": None, "hint": None, "error": None}
    if not (node.get("ilo_host") or "").strip():
        out["error"] = "non configuré"
        return out
    try:
        sysj = _get_json(node, p["sys"]) or {}
        bios_uri = (sysj.get("Bios") or {}).get("@odata.id")
        if not bios_uri:
            out["error"] = "lien BIOS Redfish absent"
            return out
        attrs = (_get_json(node, bios_uri) or {}).get("Attributes") or {}
        out["ok"] = True
        if _vendor(node) == "hpe":
            out["mmio_attr"] = "PciResourcePadding"
            out["sriov_enabled"] = (attrs.get("Sriov") == "Enabled")
            out["mmio"] = attrs.get("PciResourcePadding")
            out["ready"] = out["sriov_enabled"] and out["mmio"] == "High"
            if not out["ready"]:
                out["hint"] = ("RBSU/BIOS : Sriov=Enabled + PciResourcePadding=High "
                               "(réserve le MMIO pour l'aperture VF SR-IOV)")
        else:  # dell / iDRAC (et repli générique)
            out["mmio_attr"] = "MmioAbove4GB"
            out["sriov_enabled"] = (attrs.get("SriovGlobalEnable") == "Enabled")
            out["mmio"] = attrs.get("MmioAbove4GB")
            out["ready"] = out["sriov_enabled"] and out["mmio"] == "Enabled"
            if not out["ready"]:
                out["hint"] = "BIOS : SriovGlobalEnable=Enabled + MmioAbove4GB=Enabled"
    except Exception as e:
        out["error"] = str(e)
    return out


def _get_json(node, path, token=None):
    """GET Redfish best-effort → dict JSON ou None (jamais lève)."""
    try:
        r = _req("GET", node, path, token=token)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _nic_link_map(node, token=None):
    """Map MAC(min.) → {link, speed_mbps, model} depuis Chassis/*/NetworkAdapters/*/NetworkPorts/*.
    Tous les ports n'y figurent pas (ex. LOM 331i absent) ; on enrichit ce qu'on trouve, best-effort."""
    out = {}
    chassis = _get_json(node, "/redfish/v1/Chassis", token) or {}
    for cm in chassis.get("Members", []):
        cid = cm.get("@odata.id")
        if not cid:
            continue
        nas = _get_json(node, cid + "/NetworkAdapters", token) or {}
        for am in nas.get("Members", []):
            aid = am.get("@odata.id")
            if not aid:
                continue
            adapter = _get_json(node, aid, token) or {}
            model = adapter.get("Model") or adapter.get("Name") or ""
            ports = _get_json(node, aid + "/NetworkPorts", token) or {}
            for pm in ports.get("Members", []):
                pid = pm.get("@odata.id")
                port = _get_json(node, pid, token) if pid else None
                if not port:
                    continue
                link = (port.get("LinkStatus") or "").lower()  # "linkup"/"linkdown"/""
                link = "up" if "up" in link else ("down" if "down" in link else "unknown")
                for mac in (port.get("AssociatedNetworkAddresses") or []):
                    if mac:
                        out[mac.lower()] = {"link": link,
                                            "speed_mbps": port.get("CurrentLinkSpeedMbps"),
                                            "model": model,
                                            "port_id": port.get("Id")}
    return out


def _storage_inventory(node, p, token=None):
    """Cibles d'installation candidates (HPE SmartStorage). Volume → ciblage by-id déterministe via
    VolumeUniqueIdentifier (WWN). Best-effort, jamais lève."""
    out = []
    acs = _get_json(node, p["sys"] + "/SmartStorage/ArrayControllers", token) or {}
    for am in acs.get("Members", []):
        aid = am.get("@odata.id")
        if not aid:
            continue
        lds = _get_json(node, aid + "/LogicalDrives", token) or {}
        for lm in lds.get("Members", []):
            ld = _get_json(node, lm.get("@odata.id"), token) or {}
            vui = (ld.get("VolumeUniqueIdentifier") or "").strip()
            size_gb = round((ld.get("CapacityMiB") or 0) / 1024) if ld.get("CapacityMiB") else None
            raid = ld.get("Raid")
            media = ld.get("MediaType") or ""
            by_id = ("wwn-0x" + vui.lower()) if vui else ""
            label = f"Volume RAID{raid} · {size_gb} GiB {media}".strip()
            out.append({"kind": "volume", "label": label, "size_gb": size_gb,
                        "model": ld.get("LogicalDriveName") or "", "media": media,
                        "interface": ld.get("InterfaceType") or "", "by_id": by_id})
        dds = _get_json(node, aid + "/DiskDrives", token) or {}
        for dm in dds.get("Members", []):
            dd = _get_json(node, dm.get("@odata.id"), token) or {}
            loc = dd.get("Location") or ""
            cap = dd.get("CapacityGB")
            media = dd.get("MediaType") or ""
            model = dd.get("Model") or ""
            # Disque physique : pas toujours de WWN exposé → by-id par serial si présent (moins sûr).
            ser = (dd.get("SerialNumber") or "").strip()
            by_id = ("ata-" + model.replace(" ", "_") + "_" + ser) if (model and ser) else ""
            label = f"Disque {loc} · {cap} GB {media} {model}".strip()
            out.append({"kind": "disk", "label": label, "size_gb": cap, "model": model,
                        "media": media, "interface": dd.get("InterfaceType") or "", "by_id": by_id})
    return out


def _nic_inventory(node, p, token=None):
    """Cartes réseau (MAC + lien/débit/modèle). Liste COMPLÈTE des MAC via Systems/EthernetInterfaces,
    enrichie du lien/débit via les NetworkPorts du chassis. Best-effort, jamais lève."""
    link_map = _nic_link_map(node, token)
    out = []
    eth = _get_json(node, p["sys"] + "/EthernetInterfaces", token) or {}
    for em in eth.get("Members", []):
        ei = _get_json(node, em.get("@odata.id"), token) or {}
        mac = (ei.get("MACAddress") or ei.get("PermanentMACAddress") or "").lower()
        if not mac:
            continue
        info = link_map.get(mac, {})
        model = info.get("model") or ei.get("Name") or ""
        speed = info.get("speed_mbps")
        out.append({"mac": mac, "label": model or mac, "model": model,
                    "port": info.get("port_id") or ei.get("Id"),
                    "link": info.get("link", "unknown"), "speed_mbps": speed})
    # Lien up d'abord, puis par MAC pour un ordre stable.
    out.sort(key=lambda n: (0 if n["link"] == "up" else 1 if n["link"] == "unknown" else 2, n["mac"]))
    return out


def inventory(node):
    """Inventaire matériel pour l'UI d'enrôlement : {ok, storage:[…], nics:[…], error}. Sert à
    présenter à l'opérateur les cibles d'installation (disque) et les cartes (choix du MAC de gestion)
    découvertes sur le nœud via Redfish, plutôt que de les saisir à la main. Best-effort."""
    p = _paths(node)
    out = {"ok": False, "vendor": _vendor(node), "storage": [], "nics": [], "error": None}
    if not (node.get("ilo_host") or "").strip():
        out["error"] = "identifiants BMC non configurés"
        return out
    # UNE session réutilisée pour toute la rafale de GET (sinon Basic auth = 1 session iLO/requête →
    # épuise le pool → NoValidSession). Repli sur Basic auth si l'ouverture de session échoue.
    token, location = _session_open(node)
    try:
        g = _req("GET", node, p["sys"], token=token)
        if g.status_code != 200:
            out["error"] = _err(g)
            return out
        out["storage"] = _storage_inventory(node, p, token)
        out["nics"] = _nic_inventory(node, p, token)
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
    finally:
        _session_close(node, token, location)
    return out


def eject_media(node, token=None):
    """Éjecte le CD virtuel (idempotent : tolère « déjà éjecté »)."""
    try:
        r = _req("POST", node, _paths(node)["cd"] + "/Actions/VirtualMedia.EjectMedia", json={}, token=token)
        if r.status_code not in (200, 202, 204, 400):
            return False, _err(r)
        return True, "éjecté"
    except Exception as e:
        return False, str(e)


def insert_media(node, iso_url, token=None):
    """Monte `iso_url` (HTTP) dans le CD/DVD virtuel. Éjecte d'abord un média résiduel."""
    eject_media(node, token=token)
    r = _req("POST", node, _paths(node)["cd"] + "/Actions/VirtualMedia.InsertMedia",
             json={"Image": iso_url}, token=token)
    if r.status_code not in (200, 202, 204):
        detail = _err(r)
        # iLO Standard/Unlicensed → le Virtual Media par URL est une fonction iLO Advanced (firmware).
        if "LicenseKeyRequired" in detail:
            return False, ("iLO Advanced requis : le montage Virtual Media par URL est une fonction "
                           "licenciée (cet iLO est en Standard/Unlicensed). → utiliser la clé USB ou le boot PXE.")
        # Sinon, cause fréquente : le BMC ne joint pas l'URL (localhost/IP non routable depuis son réseau).
        return False, f"{detail} [URL envoyée : {iso_url}]"
    return True, "ISO montée"


def set_boot_once_cd(node, token=None):
    """Force le PROCHAIN boot sur le CD/DVD virtuel (One-Time).

    Sur HPe, on privilégie l'OEM `BootOnNextServerReset` de la ressource Virtual Media : il fonctionne
    MÊME pendant le POST, contrairement au PATCH du Boot système (`BootSourceOverride`) qui renvoie
    `UnableToModifyDuringSystemPOST` tant que le serveur n'a pas fini son POST. Repli sur le mécanisme
    standard (Dell, ou si l'OEM échoue)."""
    if _vendor(node) == "hpe":
        try:
            r = _req("PATCH", node, _paths(node)["cd"],
                     json={"Oem": {"Hpe": {"BootOnNextServerReset": True}}}, token=token)
            if r.status_code in (200, 202, 204):
                return True, "boot one-time = CD (BootOnNextServerReset)"
        except Exception:
            pass   # repli sur le BootSourceOverride standard ci-dessous
    r = _req("PATCH", node, _paths(node)["sys"],
             json={"Boot": {"BootSourceOverrideTarget": "Cd",
                            "BootSourceOverrideEnabled": "Once"}}, token=token)
    if r.status_code not in (200, 202, 204):
        return False, _err(r)
    return True, "boot one-time = CD"


def _power_state(node, token=None):
    g = _get_json(node, _paths(node)["sys"], token) or {}
    return g.get("PowerState")


def power_off_wait(node, token=None, timeout=90):
    """Éteint le serveur (ForceOff) et attend l'état Off. Indispensable avant de régler le boot : l'iLO
    refuse TOUTE modif de boot (BootSourceOverride ET l'OEM BootOnNextServerReset) pendant le POST
    (`UnableToModifyDuringSystemPOST`). Hors tension, la modif est acceptée. La cible est un nœud
    vierge/en cours d'install → ForceOff (pas d'arrêt gracieux requis)."""
    import time
    if _power_state(node, token) == "Off":
        return True, "déjà éteint"
    r = _req("POST", node, _paths(node)["sys"] + "/Actions/ComputerSystem.Reset",
             json={"ResetType": "ForceOff"}, token=token)
    if r.status_code not in (200, 202, 204):
        return False, _err(r)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if _power_state(node, token) == "Off":
            return True, "éteint"
    return False, "timeout en attendant l'extinction"


def power_reset(node, token=None):
    """(Re)démarre le serveur : ForceRestart s'il est allumé, On s'il est éteint."""
    sys_path = _paths(node)["sys"]
    state = "On"
    try:
        g = _req("GET", node, sys_path, token=token)
        if g.status_code == 200:
            state = g.json().get("PowerState") or "On"
    except Exception:
        pass
    reset = "ForceRestart" if state == "On" else "On"
    r = _req("POST", node, sys_path + "/Actions/ComputerSystem.Reset",
             json={"ResetType": reset}, token=token)
    if r.status_code not in (200, 202, 204):
        return False, _err(r)
    return True, f"reset envoyé ({reset})"


def deploy_node(node, iso_url):
    """Séquence complète : insert ISO → boot one-time CD → reset. Retourne (ok, steps).
    `steps` = liste de (label, ok, msg) pour le retour détaillé à l'UI. Réutilise UNE session Redfish
    pour toute la séquence (repli Basic auth si l'ouverture échoue)."""
    token, location = _session_open(node)
    steps = []
    try:
        # Ordre IMPÉRATIF : éteindre AVANT de régler le boot. L'iLO refuse toute modif de boot pendant
        # le POST → on passe par l'état Off (hors POST), puis on rallume (le boot CD est alors actif).
        for label, fn in (("Montage de l'ISO", lambda: insert_media(node, iso_url, token=token)),
                          ("Extinction (pour régler le boot)", lambda: power_off_wait(node, token=token)),
                          ("Boot one-time CD", lambda: set_boot_once_cd(node, token=token)),
                          ("Démarrage serveur", lambda: power_reset(node, token=token))):
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, str(e)
            steps.append((label, ok, msg))
            if not ok:
                return False, steps
        return True, steps
    finally:
        _session_close(node, token, location)
