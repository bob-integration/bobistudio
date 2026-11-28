# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""What must follow a media port when its PMD changes between AF-XDP (kernel `ice`) and DPDK
(`vfio-pci`). Two things were left behind on dl-360-1 (2026-11-23):

  1. the PCI address. A DPDK port has no netdev any more: the engine can only be given its BDF.
     Declared through the API on an interface the network page had never probed, `pci` was empty,
     the engine received the interface NAME and stopped on `mtl_init fail`. `resolve_dpdk_pci`
     reads the address on the node while the port is still on its kernel driver, or refuses.

  2. the node's single-interface ptp4l (`ptp_enabled` + `ptp_ifname` node settings). Once the port
     is on vfio-pci there is no netdev and no PHC: ptp4l kept running and looped on « No such
     device ». In DPDK the clock is carried by the engine (libmtl), so `park_ptp4l` stops the
     units when the port is bound, and `restore_ptp4l` starts them again when it comes back.
     The per-network ptp4l (interfaces flagged `ptp_enabled`) is not concerned: that combination
     is already refused at save and at bind (cf. mtl._vfio_gardefous).

Every access to the node, the settings and the alerts is injectable (cf.
tests/verif_pmd_switch.py)."""

import logging

log = logging.getLogger(__name__)

# Node setting holding the interface whose ptp4l was stopped by a DPDK bind — the only thing that
# tells « stopped by us, to start again » from « switched off by the operator ».
PARK_KEY = "ptp_parked_for_dpdk"


def resolve_dpdk_pci(ifname, declared, probe):
    """PCI address of a port about to run in DPDK → (pci, None), or (None, reason).

    `declared`: address from the request or the stored row (kept as it is when present).
    `probe()`: → (ok, error, nics) as routes.shared._fetch_host_nics gives them; only called when
    nothing is declared. reason: 'unreachable' (node not probed) | 'not_found' (no such netdev, or
    one without a PCI device)."""
    declared = (declared or "").strip()
    if declared:
        return declared, None
    try:
        ok, _err, nics = probe()
    except Exception as e:                     # the probe is a remote call: never a 500 for it
        log.warning("pmd_switch: NIC probe failed for %s: %s", ifname, e)
        return None, "unreachable"
    if not ok:
        return None, "unreachable"
    for n in nics or []:
        if n.get("name") == ifname and (n.get("pci") or "").strip():
            return n["pci"].strip(), None
    return None, "not_found"


def _deps(setting_for, set_setting, del_setting, groups, alert):
    """Real accessors for whatever was not injected."""
    if setting_for is None:
        from . import settings as _st
        setting_for = _st.setting_for
    if set_setting is None or del_setting is None:
        from .database import db_set_node_setting, db_delete_node_setting
        set_setting = set_setting or db_set_node_setting
        del_setting = del_setting or db_delete_node_setting
    if groups is None:
        from . import ptp as _ptp
        groups = _ptp.groups_from_node_interfaces
    if alert is None:
        from .database import db_add_alert
        alert = db_add_alert
    return setting_for, set_setting, del_setting, groups, alert


def park_ptp4l(node, ifname, host, stop=None, setting_for=None, set_setting=None,
               del_setting=None, groups=None, alert=None):
    """`ifname` of `node` is now on vfio-pci: stop the node's single-interface ptp4l/phc2sys when
    it runs on that interface, and remember it. → True when something was stopped."""
    nid = (node or {}).get("id")
    if nid is None or not ifname:
        return False
    sf, set_s, _del_s, grp, alert = _deps(setting_for, set_setting, del_setting, groups, alert)
    if grp(nid):                               # per-network ptp4l: refused elsewhere, not ours
        return False
    if not sf("ptp_enabled", nid) or (sf("ptp_ifname", nid) or "").strip() != ifname:
        return False
    if stop is None:
        from . import ptp as _ptp
        stop = _ptp.stop
    set_s(nid, "ptp_enabled", False)
    set_s(nid, PARK_KEY, ifname)
    stop(host)
    log.info("pmd_switch: node %s: ptp4l stopped, %s is on vfio-pci (clock carried by the engine)",
             nid, ifname)
    alert("alert.ptp.ptp4l_arrete_dpdk", "info", node_id=nid, kind="ptp",
          params={"n": (node or {}).get("name") or nid, "ifn": ifname})
    return True


def restore_ptp4l(node, ifname, host, deploy=None, start=None, setting_for=None,
                  set_setting=None, del_setting=None, groups=None, alert=None):
    """`ifname` of `node` is back on its kernel driver: start again the ptp4l that `park_ptp4l`
    stopped for it, with the node's stored profile. → True when ptp4l was started."""
    nid = (node or {}).get("id")
    if nid is None or not ifname:
        return False
    sf, set_s, del_s, grp, alert = _deps(setting_for, set_setting, del_setting, groups, alert)
    if (sf(PARK_KEY, nid) or "") != ifname:
        return False
    del_s(nid, PARK_KEY)
    # Switched on again by hand meanwhile, moved to another interface, or per-network ptp4l now
    # in charge: the mark is dropped, nothing else is ours to do.
    if grp(nid) or sf("ptp_enabled", nid) or (sf("ptp_ifname", nid) or "").strip() != ifname:
        return False
    if deploy is None or start is None:
        from . import ptp as _ptp
        deploy = deploy or _ptp.deploy_config
        start = start or _ptp.start

    def _int(key, default):
        v = sf(key, nid)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    _hw = sf("ptp_hw_ts", nid)
    ok, msg = deploy(host, ifname, _int("ptp_domain", 127), bool(True if _hw is None else _hw),
                     priority1=_int("ptp_priority1", 128), priority2=_int("ptp_priority2", 128),
                     log_announce=_int("ptp_log_announce", 0), log_sync=_int("ptp_log_sync", -3),
                     log_delay_req=_int("ptp_log_delay_req", -3),
                     announce_timeout=_int("ptp_announce_to", 3),
                     delay_thresh=_int("ptp_delay_thresh", 800),
                     utc_offset=_int("ptp_utc_offset", 37),
                     client_only=bool(sf("ptp_client_only", nid)))
    if ok:
        ok, msg = start(host)
    name = (node or {}).get("name") or nid
    if not ok:
        log.warning("pmd_switch: node %s: ptp4l not restarted on %s: %s", nid, ifname, msg)
        alert("alert.ptp.ptp4l_non_relance", "warning", node_id=nid, kind="ptp",
              params={"n": name, "ifn": ifname, "e": str(msg)[:200]})
        return False
    set_s(nid, "ptp_enabled", True)
    log.info("pmd_switch: node %s: ptp4l started again on %s (back on its kernel driver)", nid, ifname)
    alert("alert.ptp.ptp4l_relance", "info", node_id=nid, kind="ptp",
          params={"n": name, "ifn": ifname})
    return True
