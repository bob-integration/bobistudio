# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Multicast PLANS: validation, API shape, targets, flow classes and preview.

A plan = one target (a media network, a red/blue pair of one node, or one unpaired interface) +
shared settings (`b_rule`, `rank_mode`, `port_mode`, `reserve_last`) + lines (`mcast_ranges` rows
carrying `plan_id`). Addressing is per FABRIC: a line's A block lives on fabric a, its B block on
fabric b, and the rank of a flow is shared by both legs (see `allocations._allocate_in_plan`). The
allocation itself lives in `app/allocations.py`; this module only validates, describes and
simulates — nothing here reserves an address.

`b_rule` (how a line's B block follows its A block):
  {"mode": "octet", "octet": 2, "delta": 100}  B = A with that octet shifted (network part only);
  {"mode": "same"}                             same group on both fabrics;
  {"mode": "none"}                             no derivation: every B is typed (`b_manual`) or absent.
A line following the rule stores the DERIVED B, recomputed here at every save: the client's value
for a non-manual line is never trusted.

`port_mode` (`fixed`): a line gives the port of EVERY essence it covers (a video line its video
port, an "all essences" line the three). Refused at save otherwise: `rule_port` falls back to the
port each flow already holds when the line has none, so one plan would mix fixed and inherited
ports. The preview accepts an incomplete line (it runs while the operator types).

Preview (`preview(body)` → `POST /api/mcast_plans/preview`), result:
  flows     [{vmid, hostname, slot, essence, sub, label, flow_class, a, b, current_a, current_b,
              state, active, line}] — `a`/`b` = {ip, port} or None; `line` = index of the line that
              addresses it (None when unmatched); `state` = ok | move | new | manual | unmatched |
              full (no free address left in its line) | outside (rank mode "slot": the rank the
              plan gives the flow falls outside its line, so the allocator raises
              `plan_multicast_hors_plage` and scans to an unrelated rank — typically the 2nd audio
              of an output on a small audio line, the audio step defaulting to 100); `active` = the slot index is
              below the engine's ACTIVE output count (`active_output_count`): an engine provisions
              more slots than it runs, and every provisioned slot carries addresses;
  summary   {active: {outputs, flows, full, outside}, inactive: {...same}};
  capacity  {total, free}; overlaps [{plan_label, first, last}]; lines [{index, cidr_b, capacity,
            used}].
"""
import ipaddress
import json
import logging
import re

log = logging.getLogger(__name__)

MCAST_NET = ipaddress.ip_network("224.0.0.0/4")
PLAN_SCOPES = ("network", "pair", "interface")
RANK_MODES = ("slot", "counter")
PORT_MODES = ("fixed", "increment")
B_RULE_MODES = ("octet", "same", "none")

FLOW_CLASSES_SETTING = "mcast_flow_classes"

# ── Flow classes ──────────────────────────────────────────────────────────────────────────────────
# Nominal ST 2110 rates, computed rather than typed:
#  - 2110-20 video, 4:2:2 10 bits = 20 bits per pixel of ACTIVE video, `frames` per second (an
#    interlaced format counts frames, two fields each). Packet overhead for the usual 1200-byte
#    pgroup payload: Ethernet 14 + IPv4 20 + UDP 8 + RTP 12 + 2110-20 payload header 8 (extended
#    sequence number 2 + one sample row data header 6) = 62 bytes per packet.
#  - 2110-30 audio, L24 at 48 kHz: 48 × ptime samples per channel per packet, 3 bytes each, plus
#    Ethernet 14 + IPv4 20 + UDP 8 + RTP 12 = 54 bytes per packet, 1000 / ptime packets per second.
#    Channel counts follow the ST 2110-30 conformance levels within a 1500-byte MTU: up to 8 ch at
#    1 ms (levels A/B), up to 64 ch at 0.125 ms (level C).
#  - 2110-40 ANC has no nominal rate (bursty, a few packets per field): no figure is given.
_VIDEO_PAYLOAD = 1200
_VIDEO_OVERHEAD = 62
_AUDIO_OVERHEAD = 54


def _video_mbps(width, height, frames):
    return round(width * height * 20 * frames / 1e6 * (_VIDEO_PAYLOAD + _VIDEO_OVERHEAD) / _VIDEO_PAYLOAD, 1)


def _audio_mbps(channels, ptime_ms):
    payload = 48 * ptime_ms * channels * 3
    return round((payload + _AUDIO_OVERHEAD) * 8 * (1000 / ptime_ms) / 1e6, 3)


DEFAULT_FLOW_CLASSES = [
    # Catch-all per essence (empty match): "every video format" / "every audio format". A line on
    # a precise format of the same essence stays more specific and wins.
    {"key": "video_all", "label": "Video (all formats)", "essence": "video", "match": {},
     "bitrate_mbps": None, "payload_type": 96},
    {"key": "video_1080i50", "label": "1080i50", "essence": "video",
     "match": {"scan": "i", "width": 1920, "height": 1080, "fps": 25},
     "bitrate_mbps": _video_mbps(1920, 1080, 25), "payload_type": 96},
    {"key": "video_1080p50", "label": "1080p50", "essence": "video",
     "match": {"scan": "p", "width": 1920, "height": 1080, "fps": 50},
     "bitrate_mbps": _video_mbps(1920, 1080, 50), "payload_type": 96},
    {"key": "video_2160p50", "label": "2160p50", "essence": "video",
     "match": {"scan": "p", "width": 3840, "height": 2160, "fps": 50},
     "bitrate_mbps": _video_mbps(3840, 2160, 50), "payload_type": 96},
    {"key": "audio_all", "label": "Audio (all formats)", "essence": "audio", "match": {},
     "bitrate_mbps": None, "payload_type": 97},
    {"key": "audio_2ch_1ms", "label": "Audio 2 ch · 1 ms", "essence": "audio",
     "match": {"channels": 2, "ptime": 1}, "bitrate_mbps": _audio_mbps(2, 1), "payload_type": 97},
    {"key": "audio_8ch_1ms", "label": "Audio 8 ch · 1 ms", "essence": "audio",
     "match": {"channels": 8, "ptime": 1}, "bitrate_mbps": _audio_mbps(8, 1), "payload_type": 97},
    {"key": "audio_16ch_125us", "label": "Audio 16 ch · 0.125 ms", "essence": "audio",
     "match": {"channels": 16, "ptime": 0.125}, "bitrate_mbps": _audio_mbps(16, 0.125),
     "payload_type": 97},
    {"key": "audio_64ch_125us", "label": "Audio 64 ch · 0.125 ms", "essence": "audio",
     "match": {"channels": 64, "ptime": 0.125}, "bitrate_mbps": _audio_mbps(64, 0.125),
     "payload_type": 97},
    {"key": "anc", "label": "ANC", "essence": "anc", "match": {}, "bitrate_mbps": None,
     "payload_type": 100},
]


def flow_classes():
    """The flow class catalogue: the `mcast_flow_classes` setting (JSON list) when it holds a valid
    list, else the defaults above. Invalid entries are skipped (logged)."""
    raw = None
    try:
        from . import settings
        raw = settings.get(FLOW_CLASSES_SETTING)
    except Exception:
        raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, list) or not raw:
        return [dict(c, match=dict(c["match"])) for c in DEFAULT_FLOW_CLASSES]
    out = []
    for c in raw:
        if (isinstance(c, dict) and c.get("key") and c.get("essence") in ("video", "audio", "anc")
                and isinstance(c.get("match") or {}, dict)):
            out.append({"key": str(c["key"]), "label": c.get("label") or c["key"],
                        "essence": c["essence"], "match": dict(c.get("match") or {}),
                        "bitrate_mbps": c.get("bitrate_mbps"), "payload_type": c.get("payload_type")})
        else:
            log.warning("mcast_flow_classes: invalid entry skipped: %r", c)
    return out


# ── b_rule ────────────────────────────────────────────────────────────────────────────────────────

def validate_b_rule(raw):
    """`(rule, error)`: the normalised leg-B rule, or an error message. None/empty = 'none'."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None, "b_rule invalide (JSON attendu)"
    if raw is None:
        return {"mode": "none"}, None
    if not isinstance(raw, dict):
        return None, "b_rule invalide (objet attendu)"
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in B_RULE_MODES:
        return None, "b_rule.mode invalide (octet | same | none)"
    if mode != "octet":
        return {"mode": mode}, None
    try:
        octet, delta = int(raw.get("octet")), int(raw.get("delta"))
    except (TypeError, ValueError):
        return None, "b_rule : octet et delta entiers attendus"
    if not 1 <= octet <= 4:
        return None, "b_rule.octet invalide (1 à 4)"
    if delta == 0 or not -255 <= delta <= 255:
        return None, "b_rule.delta invalide (non nul, -255 à 255 ; « same » pour le même groupe)"
    return {"mode": "octet", "octet": octet, "delta": delta}, None


def derive_b(blk, rule):
    """`(base_b, error)`: first address of the B block/range a rule derives from the A block or
    explicit range `blk` (see `_parse_block`), same number of addresses. A CIDR block refuses an
    octet in its host part (the B block would not keep the ranks); an explicit range has no prefix:
    its B range is only checked against the A ranges (`_check_blocks`, partial overlap refused)."""
    mode = (rule or {}).get("mode") or "none"
    text = _span_text(blk["first"], blk["prefix"], blk["count"], blk["prefix"] is None)
    if mode == "none":
        return None, None
    if mode == "same":
        return str(ipaddress.IPv4Address(blk["first"])), None
    n, delta = int(rule["octet"]), int(rule["delta"])
    if blk["prefix"] is not None and blk["prefix"] < 8 * n:
        return None, ("b_rule : l'octet %d est dans la partie hôte de %s (préfixe /%d) — le bloc B "
                      "ne serait plus le même rang" % (n, text, blk["prefix"]))
    octs = list(ipaddress.IPv4Address(blk["first"]).packed)
    octs[n - 1] += delta
    if not 0 <= octs[n - 1] <= 255:
        return None, "b_rule : octet %d hors 0-255 pour %s" % (n, text)
    first_b = int(ipaddress.IPv4Address(bytes(octs)))
    last_b = first_b + blk["count"] - 1
    if not (_in_mcast(first_b) and _in_mcast(last_b)):
        return None, "b_rule : bloc B %s hors de 224.0.0.0/4" % _span_text(
            first_b, blk["prefix"], blk["count"], blk["prefix"] is None)
    return str(ipaddress.IPv4Address(first_b)), None


# ── Lines and plans ───────────────────────────────────────────────────────────────────────────────
# API line field → mcast_ranges column.
_LINE_PORTS = {"port_video": "port_default_video", "port_audio": "port_default_audio",
               "port_anc": "port_default_anc"}
_LINE_INTS = ("ip_offset_video", "ip_offset_anc", "ip_offset_audio", "ip_step_audio")


_RANGE_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*[-\u2013]\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$")


def _in_mcast(n):
    return 0 <= n <= 0xFFFFFFFF and ipaddress.IPv4Address(n) in MCAST_NET


def _parse_block(text, what):
    """`(block, error)`: `block = {first, count, prefix}` for "A.B.C.D/NN" (prefix set) or an
    EXPLICIT range "first-last" ("-" or "–", optional spaces; prefix None, both ends allocatable)."""
    text = (text or "").strip()
    m = _RANGE_RE.match(text)
    if m:
        try:
            first, last = (int(ipaddress.IPv4Address(x)) for x in m.groups())
        except ValueError:
            return None, "%s invalide (adresse IPv4 attendue de part et d'autre du tiret)" % what
        if first > last:
            return None, "%s invalide (première adresse après la dernière)" % what
        if not (_in_mcast(first) and _in_mcast(last)):
            return None, "%s n'est pas une plage multicast (224.0.0.0/4)" % what
        return {"first": first, "count": last - first + 1, "prefix": None}, None
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None, ("%s invalide (adresse/préfixe, ex. 239.10.30.0/24, ou plage explicite, "
                      "ex. 239.10.30.1-239.10.30.50)" % what)
    if net.version != 4 or not net.subnet_of(MCAST_NET):
        return None, "%s n'est pas un bloc multicast (224.0.0.0/4)" % what
    return {"first": int(net.network_address), "count": net.num_addresses, "prefix": net.prefixlen}, None


def _opt_int(v, name, lo, hi):
    if v in (None, ""):
        return None, None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, "%s : entier attendu" % name
    if not lo <= n <= hi:
        return None, "%s hors bornes (%d-%d)" % (name, lo, hi)
    return n, None


def _normalize_line(raw, idx, rule, classes):
    """`(columns, error)` for one API line (see module docstring for B)."""
    if not isinstance(raw, dict):
        return None, "ligne %d invalide" % (idx + 1)
    where = "ligne %d" % (idx + 1)
    blk, err = _parse_block(raw.get("cidr_a"), where + " : bloc A")
    if err:
        return None, err
    explicit = blk["prefix"] is None
    cols = {"base_ip": str(ipaddress.IPv4Address(blk["first"])), "size": blk["count"],
            "prefix_len": blk["prefix"], "explicit_span": 1 if explicit else None,
            "label": (raw.get("label") or "").strip() or None}
    if raw.get("id") not in (None, ""):
        try:
            cols["id"] = int(raw["id"])
        except (TypeError, ValueError):
            return None, where + " : id invalide"
    key = raw.get("flow_class") or None
    if key is not None:
        cls = next((c for c in classes if c["key"] == key), None)
        if cls is None:
            return None, "%s : classe de flux inconnue (%s)" % (where, key)
        cols["flow_class"], cols["essence"] = key, cls["essence"]
        cols["match_json"] = json.dumps(cls["match"]) if cls["match"] else None
    else:
        cols["flow_class"] = cols["essence"] = cols["match_json"] = None
    manual = 1 if raw.get("b_manual") in (True, 1, "1", "true", "on") else 0
    cols["b_manual"] = manual
    if manual:
        text = (raw.get("cidr_b") or "").strip()
        if text:
            blk_b, err = _parse_block(text, where + " : bloc B")
            if err:
                return None, err
            if not explicit and blk_b["prefix"] != blk["prefix"]:
                # A CIDR line keeps a CIDR B of the same prefix (it is displayed with A's prefix).
                return None, "%s : le bloc B doit avoir la taille du bloc A (/%d)" % (where, blk["prefix"])
            if blk_b["count"] != blk["count"]:
                return None, "%s : le bloc B doit compter autant d'adresses que le bloc A (%d)" % (
                    where, blk["count"])
            cols["base_ip_b"] = str(ipaddress.IPv4Address(blk_b["first"]))
        else:
            cols["base_ip_b"] = None
    else:                                 # follows the rule: client's cidr_b ignored ("" included)
        cols["base_ip_b"], err = derive_b(blk, rule)
        if err:
            return None, "%s : %s" % (where, err)
    for api, col in _LINE_PORTS.items():
        cols[col], err = _opt_int(raw.get(api), "%s : %s" % (where, api), 1, 65535)
        if err:
            return None, err
    for k in _LINE_INTS:
        cols[k], err = _opt_int(raw.get(k), "%s : %s" % (where, k), 1 if k == "ip_step_audio" else 0,
                                blk["count"])
        if err:
            return None, err
    return cols, None


def _block(base, size):
    a = int(ipaddress.IPv4Address(base))
    return a, a + int(size) - 1


def _overlap(x, y):
    return x[0] <= y[1] and y[0] <= x[1]


def _check_blocks(lines):
    """Error message if lines' blocks collide: A blocks disjoint; a B block equal to its own A block
    (same group on both fabrics) or disjoint from every A block; B blocks disjoint."""
    a = [_block(ln["base_ip"], ln["size"]) for ln in lines]
    b = [_block(ln["base_ip_b"], ln["size"]) if ln.get("base_ip_b") else None for ln in lines]
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if _overlap(a[i], a[j]):
                return "les blocs A des lignes %d et %d se chevauchent" % (i + 1, j + 1)
            if b[i] and b[j] and _overlap(b[i], b[j]) and not (b[i] == a[i] and b[j] == a[j]):
                return "les blocs B des lignes %d et %d se chevauchent" % (i + 1, j + 1)
    for i, bi in enumerate(b):
        if not bi:
            continue
        for j, aj in enumerate(a):
            if i == j and bi == aj:
                continue
            if _overlap(bi, aj):
                return ("le bloc B de la ligne %d chevauche le bloc A de la ligne %d (identique à son "
                        "propre bloc A, ou disjoint, attendu)" % (i + 1, j + 1))
    return None


def _iface_row(node_id, ifname):
    from .allocations import _media_ifaces
    return next((r for r in _media_ifaces(node_id) if r.get("ifname") == ifname), None)


def validate_plan(body, plan_id=None):
    """`(plan_columns, line_columns, error)` for an API PLAN. The network of a pair/interface plan is
    derived from its interface(s), whatever the client sent. A target holds at most one plan."""
    from . import allocations as al
    from .database import db_get_media_networks, db_get_mcast_plans
    if not isinstance(body, dict):
        return None, None, "corps invalide"
    scope = (body.get("scope") or "").strip().lower()
    if scope not in PLAN_SCOPES:
        return None, None, "scope invalide (network | pair | interface)"
    plan = {"scope": scope, "media_network_id": None, "node_id": None, "pair_group": None,
            "ifname": None, "label": (body.get("label") or "").strip() or None}
    try:
        if scope == "network":
            plan["media_network_id"] = int(body.get("media_network_id"))
            if plan["media_network_id"] not in {n["id"] for n in db_get_media_networks()}:
                return None, None, "réseau média inconnu"
        elif scope == "pair":
            plan["node_id"], plan["pair_group"] = int(body.get("node_id")), int(body.get("pair_group"))
            mem = al.pair_members(plan["node_id"], plan["pair_group"])
            if not mem:
                return None, None, "paire inconnue sur ce nœud (rouge + bleu attendus)"
            plan["media_network_id"] = mem["a"].get("media_network_id") or mem["b"].get("media_network_id")
        else:
            plan["node_id"], plan["ifname"] = int(body.get("node_id")), (body.get("ifname") or "").strip()
            row = _iface_row(plan["node_id"], plan["ifname"])
            if not row:
                return None, None, "interface média inconnue sur ce nœud"
            if al._is_paired(row):
                return None, None, "interface appairée : le plan se pose sur sa paire"
            plan["media_network_id"] = row.get("media_network_id")
    except (TypeError, ValueError):
        return None, None, "cible du plan incomplète"
    for p in db_get_mcast_plans():
        if p["id"] != plan_id and p["scope"] == scope and all(
                p.get(k) == plan[k] for k in {"network": ("media_network_id",),
                                              "pair": ("node_id", "pair_group"),
                                              "interface": ("node_id", "ifname")}[scope]):
            return None, None, "cette cible a déjà un plan (« %s »)" % (p.get("label") or p["id"])
    rule, err = validate_b_rule(body.get("b_rule"))
    if err:
        return None, None, err
    plan["b_rule"] = json.dumps(rule)
    rank = (body.get("rank_mode") or "slot").strip().lower()
    port = (body.get("port_mode") or "increment").strip().lower()
    if rank not in RANK_MODES:
        return None, None, "rank_mode invalide (slot | counter)"
    if port not in PORT_MODES:
        return None, None, "port_mode invalide (fixed | increment)"
    plan["rank_mode"], plan["port_mode"] = rank, port
    plan["reserve_last"] = 1 if body.get("reserve_last") in (True, 1, "1", "true", "on") else 0
    raw_lines = body.get("lines") or []
    if not isinstance(raw_lines, list):
        return None, None, "lines : liste attendue"
    classes = flow_classes()
    lines = []
    for i, raw in enumerate(raw_lines):
        cols, err = _normalize_line(raw, i, rule, classes)
        if err:
            return None, None, err
        lines.append(cols)
    err = _check_blocks(lines)
    if err:
        return None, None, err
    return plan, lines, None


def _span_text(base, prefix_len, size, explicit=False):
    """API text of a block: "first-last" (ASCII hyphen, no spaces) for an explicit range, else
    "A.B.C.D/NN". `base` = address string or int."""
    if base in (None, ""):
        return None
    first = int(ipaddress.IPv4Address(base))
    if explicit:
        return "%s-%s" % (ipaddress.IPv4Address(first), ipaddress.IPv4Address(first + int(size or 1) - 1))
    if prefix_len is None:
        prefix_len = 32 - max(int(size or 1) - 1, 0).bit_length()
    return "%s/%d" % (ipaddress.IPv4Address(first), int(prefix_len))


def _cidr(base, prefix_len, size, explicit=False):
    return _span_text(base, prefix_len, size, explicit)


def _fabrics(plan):
    """`{a: {color, ifname}, b: {...}|None}` of a plan's target (read-only, computed)."""
    from . import allocations as al
    if plan["scope"] == "pair":
        mem = al.pair_members(plan["node_id"], plan["pair_group"])
        if not mem:
            return {"a": None, "b": None}
        return {f: {"color": mem[f].get("pair_role"), "ifname": mem[f].get("ifname")} for f in ("a", "b")}
    if plan["scope"] == "interface":
        return {"a": {"color": None, "ifname": plan.get("ifname")}, "b": None}
    first = al._first_colours().get(plan.get("media_network_id"), "red")
    return {"a": {"color": first, "ifname": None},
            "b": {"color": "blue" if first == "red" else "red", "ifname": None}}


def _target_label(plan, nodes=None, nets=None):
    from .database import db_get_nodes, db_get_media_networks
    if plan["scope"] == "network":
        nets = nets if nets is not None else {n["id"]: n for n in db_get_media_networks()}
        return (nets.get(plan.get("media_network_id")) or {}).get("name") or "?"
    nodes = nodes if nodes is not None else {n["id"]: n for n in db_get_nodes()}
    node = (nodes.get(plan.get("node_id")) or {}).get("name") or "node %s" % plan.get("node_id")
    if plan["scope"] == "interface":
        return "%s · %s" % (node, plan.get("ifname"))
    fab = _fabrics(plan)
    names = "/".join(fab[f]["ifname"] for f in ("a", "b") if fab.get(f))
    return "%s · paire %s%s" % (node, plan.get("pair_group"), " (%s)" % names if names else "")


def line_to_api(ln, taken=None):
    from . import allocations as al
    taken = taken if taken is not None else al._groups_held_by_others(None)
    out = {"id": ln.get("id"), "flow_class": ln.get("flow_class"), "label": ln.get("label"),
           "cidr_a": _cidr(ln.get("base_ip"), ln.get("prefix_len"), ln.get("size"), ln.get("explicit_span")),
           "cidr_b": _cidr(ln.get("base_ip_b"), ln.get("prefix_len"), ln.get("size"), ln.get("explicit_span")),
           "b_manual": 1 if ln.get("b_manual") else 0}
    for api, col in _LINE_PORTS.items():
        out[api] = ln.get(col)
    for k in _LINE_INTS:
        out[k] = ln.get(k)
    out["capacity"] = al.range_capacity(ln)
    out["used"] = al.range_used(ln, taken)
    return out


def plan_to_api(p, taken=None):
    """API PLAN of a stored plan (with `lines`)."""
    try:
        rule = json.loads(p.get("b_rule") or "null") or {"mode": "none"}
    except ValueError:
        rule = {"mode": "none"}
    return {"id": p.get("id"), "scope": p["scope"], "media_network_id": p.get("media_network_id"),
            "node_id": p.get("node_id"), "pair_group": p.get("pair_group"), "ifname": p.get("ifname"),
            "label": p.get("label"), "b_rule": rule, "rank_mode": p.get("rank_mode") or "slot",
            "port_mode": p.get("port_mode") or "increment",
            "reserve_last": 1 if p.get("reserve_last") else 0,
            "fabrics": _fabrics(p), "target_label": _target_label(p),
            "lines": [line_to_api(dict(ln, reserve_last=p.get("reserve_last")), taken)
                      for ln in p.get("lines") or []]}


def plans_api():
    from . import allocations as al
    from .database import db_get_mcast_plans
    taken = al._groups_held_by_others(None)
    return [plan_to_api(p, taken) for p in db_get_mcast_plans()]


def save_plan(body, plan_id=None):
    """Validate and store a plan (create, or full replace of an existing one). `(id, error)`.
    The plan's leg-B rule becomes the default rule of its media network."""
    from .database import db_save_mcast_plan, db_update_media_network, db_get_mcast_plan
    if plan_id is not None and db_get_mcast_plan(plan_id) is None:
        return None, "plan introuvable"
    plan, lines, err = validate_plan(body, plan_id)
    if err:
        return None, err
    missing = missing_fixed_ports(plan, lines)
    if missing:
        i, e = missing[0]
        return None, ("ligne %d\u00a0: port %s requis. En mode «\u00a0un port par essence\u00a0», "
                      "chaque ligne donne le port de chaque essence qu'elle couvre." % (i + 1, _ESSENCE_FR[e]))
    pid = db_save_mcast_plan(plan, lines, plan_id)
    if plan.get("media_network_id"):
        db_update_media_network(plan["media_network_id"], default_b_rule=json.loads(plan["b_rule"]))
    return pid, None


def targets():
    """Everything a plan can target: networks, red/blue pairs, unpaired media interfaces."""
    from . import allocations as al
    from .database import db_get_media_networks, db_get_nodes
    nets = [{"id": n["id"], "name": n.get("name"), "first_fabric": n.get("first_fabric") or "red",
             "default_b_rule": n.get("default_b_rule")} for n in db_get_media_networks()]
    pairs, ifaces = [], []
    for node in db_get_nodes() or []:
        rows = al._media_ifaces(node["id"])
        seen = set()
        for r in rows:
            if al._is_paired(r):
                g = r.get("pair_group")
                if g in seen:
                    continue
                seen.add(g)
                mem = al.pair_members(node["id"], g, rows)
                if mem:
                    pairs.append({"node_id": node["id"], "node_name": node.get("name"), "pair_group": g,
                                  "media_network_id": mem["a"].get("media_network_id"),
                                  "a": {"color": mem["a"].get("pair_role"), "ifname": mem["a"]["ifname"]},
                                  "b": {"color": mem["b"].get("pair_role"), "ifname": mem["b"]["ifname"]}})
            else:
                ifaces.append({"node_id": node["id"], "node_name": node.get("name"),
                               "ifname": r.get("ifname"), "media_network_id": r.get("media_network_id")})
    return {"networks": nets, "pairs": pairs, "interfaces": ifaces}


# ── Preview ───────────────────────────────────────────────────────────────────────────────────────

def _engines():
    """2110 engines: [(vmid, hostname, node_id, params)]."""
    from .database import db_get_containers
    out = []
    for c in db_get_containers():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except ValueError:
            continue
        if dc.get("type") == "2110_io" and c.get("node_id"):
            out.append((c["vmid"], c.get("hostname") or str(c["vmid"]), c["node_id"],
                        dc.get("params") or {}))
    return sorted(out)


_PORT_FALLBACK = {"video": 5000, "audio": 5004, "anc": 5008}
_PORT_ESSENCES = ("video", "audio", "anc")


def active_output_count(params):
    """Number of ACTIVE TX outputs of a 2110 engine: slots `[0, n)` run, the others are only
    provisioned. Same rule as the engine page (`routes/nmos_detail.py`): `active_tx_count` when the
    key is set (an explicit 0 stays 0), 6 when it is absent or None, never more than the slots."""
    slots = params.get("tx_slots") or []
    raw = params.get("active_tx_count")
    try:
        n = 6 if raw is None else int(raw)
    except (TypeError, ValueError):
        n = 6
    return max(0, min(n, len(slots)))


def missing_fixed_ports(plan, lines):
    """`[(line_index, essence)]` a `fixed` port plan leaves without a port: every essence a line
    covers (its class's essence, or all three for an "all essences" line) needs its port."""
    if (plan or {}).get("port_mode") != "fixed":
        return []
    out = []
    for i, ln in enumerate(lines or []):
        for e in ((ln.get("essence"),) if ln.get("essence") else _PORT_ESSENCES):
            if not ln.get("port_default_%s" % e):
                out.append((i, e))
    return out


_ESSENCE_FR = {"video": "vidéo", "audio": "audio", "anc": "ANC"}


def preview(body):
    """Simulate a plan: see `_preview`. Runs inside a read snapshot (the simulation writes nothing,
    and reading the ledger and the NMOS registry once instead of once per flow is the difference
    between 1 s and a few tens of milliseconds)."""
    from . import allocations as al
    with al.read_snapshot():
        return _preview(body)


def _preview(body):
    """Simulate a plan (saved or not) against the TX flows of the 2110 engines it addresses. Nothing
    is written or reserved. `(result, error)`; `result` follows the API contract."""
    from . import allocations as al
    from . import io2110_flows as iof
    pid = body.get("id") if isinstance(body, dict) else None
    try:
        pid = int(pid) if pid not in (None, "") else None
    except (TypeError, ValueError):
        pid = None
    plan, lines, err = validate_plan(body, pid)
    if err:
        return None, err
    cand = dict(plan, id=pid if pid is not None else -1,
                lines=[dict(ln, id=ln.get("id") or -(i + 1)) for i, ln in enumerate(lines)])
    taken_all = al._groups_held_by_others(None)
    flows, sim, jobs = [], set(), []
    for vmid, host, node_id, params in _engines():
        slots = [dict(s or {}) for s in (params.get("tx_slots") or [])]
        n_active = active_output_count(params)
        aud_idxs = {}
        for i, essence, sub, e, kip, kport, ref_base, fmt in al._tx_flows_of(vmid, params, slots):
            ifn0, net0 = al._egress_iface(node_id, params, i, leg=0)
            if al._flow_plan(node_id, ifn0, net0, candidate=cand) is not cand:
                continue
            fab0 = al.fabric_of(node_id, ifn0)
            paired = al.slot_redondant(node_id, params, i)
            cur = {fab0: e.get(kip)}
            ports = {fab0: e.get(kport)}
            if paired:
                cur[al._other_fabric(fab0)] = e.get(kip + "_leg1")
                ports[al._other_fabric(fab0)] = e.get(kport + "_leg1") or e.get(kport)
            if essence == "audio":
                aud_idxs.setdefault(i, iof.tx_slot_audio_idxs(params.get("tx_flows") or [], i))
            label = "TX%d %s%s" % (i + 1, essence, " %d" % (sub + 1) if essence == "audio" else "")
            row = {"vmid": vmid, "hostname": host, "slot": i, "essence": essence, "sub": sub,
                   "label": label, "flow_class": None, "a": None, "b": None,
                   "current_a": cur.get("a"), "current_b": cur.get("b"), "active": i < n_active,
                   "line": None}
            view = al._plan_line(cand, essence, fmt)
            if view is None:
                row["state"] = "unmatched"
                flows.append(row)
                continue
            row["flow_class"] = view.get("flow_class")
            row["line"] = next((k for k, ln in enumerate(cand["lines"]) if ln.get("id") == view.get("id")), None)
            if e.get(kip + "_manual") or e.get(kip + "_leg1_manual"):
                row["state"] = "manual"
                row["a"] = {"ip": cur.get("a"), "port": ports.get("a")} if cur.get("a") else None
                row["b"] = {"ip": cur.get("b"), "port": ports.get("b")} if cur.get("b") else None
                sim.update(x for x in cur.values() if x)
                flows.append(row)
                continue
            first, end = al._alloc_span(view)
            rank = al._plan_rank(view, fab0, essence, i, sub, cur.get(fab0))
            plan_off = (al._plan_offset(view, essence, i, sub, 0)
                        if view.get("rank_mode") != "counter" else None)
            outside = plan_off is not None and not first <= plan_off < end
            if essence == "audio":
                aidx = (aud_idxs[i][sub] if sub < len(aud_idxs[i]) else sub)
                off = aidx * 2
            else:
                off = i * 2 if essence == "anc" else 0
            base_port = int(view.get("port_default_%s" % essence) or view.get("port_default")
                            or _PORT_FALLBACK[essence])
            fabs = ("a", "b") if paired else (fab0,)
            flows.append(row)
            jobs.append({"row": row, "view": view, "rank": rank, "outside": outside, "fabs": fabs,
                         "refs": al._flow_refs(ref_base + ":leg0", sub), "cur": cur,
                         "ports": {f: al.rule_port(view, essence, ports.get(f) or base_port + off)
                                   for f in fabs}})

    def place(job, rank):
        row, fabs = job["row"], job["fabs"]
        for f in fabs:
            ip = al._fabric_addr(job["view"], f, rank)
            if ip is None:
                continue
            row[f] = {"ip": ip, "port": job["ports"][f]}
            sim.add(ip)
        planned = [row[f]["ip"] if row[f] else None for f in fabs]
        current = [job["cur"].get(f) for f in fabs]
        if rank is None:
            row["state"] = "full"           # no free address left in its line
        elif job["outside"]:
            row["state"] = "outside"        # planned rank outside the line: scanned instead
        elif not any(current):
            row["state"] = "new"
        elif planned == current:
            row["state"] = "ok"
        else:
            row["state"] = "move"

    # Two passes, so that no address is shown twice: flows whose rank is set by the plan (or kept)
    # first, then the scanned ones around them. A set rank already given in this simulation is
    # scanned too, as the allocator does when the planned group is held.
    scanned = []
    for job in jobs:
        addrs = [al._fabric_addr(job["view"], f, job["rank"]) for f in job["fabs"]]
        if job["rank"] is None or any(x in sim for x in addrs if x):
            scanned.append(job)
        else:
            place(job, job["rank"])
    for job in scanned:
        first, end = al._alloc_span(job["view"])
        busy = al._groups_held_by_others(job["refs"]) | sim
        rank = None
        for r in range(first, end):
            addrs = [al._fabric_addr(job["view"], f, r) for f in job["fabs"]]
            if not any(x in busy for x in addrs if x):
                rank = r
                break
        place(job, rank)
    cap_lines = []
    for idx, ln in enumerate(cand["lines"]):
        v = al._line_view(ln, cand)
        cap_lines.append({"index": idx,
                          "cidr_b": _cidr(ln.get("base_ip_b"), ln.get("prefix_len"), ln.get("size"),
                                          ln.get("explicit_span")),
                          "capacity": al.range_capacity(v), "used": al.range_used(v, taken_all)})
    total = sum(x["capacity"] for x in cap_lines)
    overlaps = _overlaps(cand)
    summary = {}
    for key, act in (("active", True), ("inactive", False)):
        mine = [f for f in flows if f["active"] is act]
        summary[key] = {"outputs": len({(f["vmid"], f["slot"]) for f in mine}), "flows": len(mine),
                        "full": sum(1 for f in mine if f["state"] == "full"),
                        "outside": sum(1 for f in mine if f["state"] == "outside")}
    return {"flows": flows, "summary": summary, "capacity": {"total": total,
                                         "free": max(total - sum(x["used"] for x in cap_lines), 0)},
            "overlaps": overlaps, "lines": cap_lines}, None


def _overlaps(cand):
    """Blocks (A or B) of OTHER stored plans that overlap a block of `cand`."""
    from .database import db_get_mcast_plans
    mine = []
    for ln in cand["lines"]:
        mine.append(_block(ln["base_ip"], ln["size"]))
        if ln.get("base_ip_b"):
            mine.append(_block(ln["base_ip_b"], ln["size"]))
    out = []
    for p in db_get_mcast_plans():
        if p["id"] == cand.get("id"):
            continue
        for ln in p.get("lines") or []:
            theirs = [_block(ln["base_ip"], ln["size"])]
            if ln.get("base_ip_b"):
                theirs.append(_block(ln["base_ip_b"], ln["size"]))
            for t in theirs:
                for m in mine:
                    if _overlap(t, m):
                        out.append({"plan_label": p.get("label") or "plan#%s" % p["id"],
                                    "first": str(ipaddress.IPv4Address(max(t[0], m[0]))),
                                    "last": str(ipaddress.IPv4Address(min(t[1], m[1])))})
    return out
