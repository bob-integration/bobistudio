# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""REAL-TIME PRIORITY of a plugin's deadline thread — given by the PRODUCT, not by each plugin.

Who does what (decided with Cyril, 2026-11-23):
  - the PLUGIN declares the ROLE of its deadline thread in its manifest, `resources.realtime`
    (its author is the one who knows which thread holds the deadline);
  - the CORE turns the role into a SCHED_FIFO priority with the grid below, grants what it needs
    (CAP_SYS_NICE + an rtprio limit, `container_grants`) and passes it to the script as
    `rt_priority` (`plugins.render_config`);
  - `bobimxl.rt_promote` applies it to the thread inside the container and publishes what was
    OBTAINED on :8080 (`rt_priority`, `rt_priority_error`); the panel shows that, never the ask.
No operator setting: the number is a product decision, the same everywhere.

Why it matters (measured 2026-11-22, r620-2 and dell-1): on a PREEMPT_RT kernel a deadline thread
left in SCHED_OTHER on saturated shared cores missed ~4,000 slots per 50 fades (700-800 on the
normal kernel); in SCHED_FIFO it missed none on both kernels, and a FIFO neighbour did not make a
normal one worse. On reserved cores the mixer's p99.9 lost time went from 2.4-3.2 ms to 1.2-2.0 ms.
The RT kernel is the target for production nodes; this module is its precondition.

Higher number = runs first. The grid leaves room between roles; the kernel's own threads (IRQ
threads at 50 on PREEMPT_RT) stay above all of them.
"""

import logging

log = logging.getLogger(__name__)

ROLES = {
    "audio":      10,   # audio blocks of 1 ms (console, cart player)
    "output":      8,   # a physical output that cannot wait (SDI, ST 2110 TX)
    "program":     5,   # the programme picture (mixer PGM, stills, player)
    "monitoring":  3,   # what is watched, not aired (multiview)
}
# rtprio limit granted to a container with a role: above every grid value, so a plugin can place a
# second thread just below or above its main one without a new grant.
RTPRIO_LIMIT = 20
CAP = "SYS_NICE"


def role_of(manifest):
    """The declared role, or "" when the plugin declares none or an unknown one (logged: a typo in
    a manifest must not silently leave the deadline thread in SCHED_OTHER)."""
    r = ((manifest or {}).get("resources") or {}).get("realtime")
    if r in (None, ""):
        return ""
    if isinstance(r, str) and r in ROLES:
        return r
    log.warning("realtime: %s declares unknown role %r (known: %s), thread left in SCHED_OTHER",
                (manifest or {}).get("type") or "?", r, ", ".join(ROLES))
    return ""


def priority_of(manifest):
    """SCHED_FIFO priority for the declared role, 0 when there is none."""
    return ROLES.get(role_of(manifest), 0)


def container_grants(manifest):
    """(cap_add, ulimits) a container of this plugin needs for its role: ([], {}) without one."""
    if not role_of(manifest):
        return [], {}
    return [CAP], {"rtprio": RTPRIO_LIMIT}
