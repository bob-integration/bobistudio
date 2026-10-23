#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Lecture synchrone (app/sync_groups.py) : MÊME ancre datée pour tous les canaux, instant commun dans le
futur et sur un créneau, règles de fin de ligne, bascule de ligne PRÉ-PROGRAMMÉE, et aucun échec silencieux
(commande refusée, appliquée en retard, jamais appliquée, canal repris en main, écart > 1 image).

Monde feint : une base jetable (DB_PATH posé AVANT tout accès — get_db tient une connexion par thread), des
players simulés qui parlent le protocole /sync (préparation de têtes, commandes datées, témoin) avec leur
propre horloge TAI, et une horloge qu'on avance. Le moteur et les opérations sont les vrais ; seuls les
accès au monde sont remplacés.

MUTATIONS (preuve que le banc mord), SYNC_MUT=<nom> :
    per_member_at     chaque canal reçoit son propre instant (pris à l'envoi)     (les canaux ne partent pas ensemble)
    no_snap           l'instant commun n'est pas ramené sur un créneau             (ancre hors grille)
    no_lead           l'instant commun est « maintenant »                          (les commandes arrivent en retard)
    longest_is_min    « fin du plus long » prend le plus court                     (règle de fin ignorée)
    no_late_alert     une commande appliquée en retard n'est pas signalée          (échec silencieux)
    no_refusal_alert  un refus de commande n'est pas signalé                       (échec silencieux)
    no_skew_alert     un écart d'horloge de plus d'une image n'est pas signalé     (dérive silencieuse)
    trust_db          la reprise après redémarrage croit la base sans les témoins  (la base fait foi)
    pause_no_freeze   la pause envoie la vitesse de lecture                        (pause sans effet)
    switch_unscheduled la ligne suivante n'est pas datée à l'avance                (bascule à la latence près)
    no_horizon        une bascule lointaine est datée tout de suite                (refusée par le player : > 60 s)
    tolerate_one_frame un écart d'UNE image entre canaux est toléré                (fill et key décalés d'une image)
    unc_masks_frames  l'incertitude d'horloge s'applique aussi à l'écart d'images  (± quelques ms masquent 40 ms)
    ignore_anchor     une ancre différente (temps média décalé) n'est pas vue       (décalage permanent muet)
    transient_two     une seule image répétée n'est pas signalée                    (désynchro passagère muette)
    pair_any_node     une paire sur deux nœuds est acceptée                          (horloges différentes dans un fill / key)
    pair_ignore_cells une ligne aux cellules / In / durées différentes passe          (fill et key ne se correspondent plus)
    pair_no_full      une paire part sans attendre ses files pleines                 (répétition au départ)
    pair_ignore_decode un clip décodé sous la cadence est accepté                    (répétitions à l'antenne)
    pair_open_proceeds un canal de paire fermé laisse partir l'autre                  (un fill sans son key)
    open_fail_ignored « refuser le départ » laisse partir les autres                 (réglage sans effet)
    pair_repeat_ok    une image répétée sur un canal de paire n'est pas un défaut    (tolérance de paire non tenue)
    no_proof          le constat « déjà jouée sans défaut » est ignoré                (boucle d'un clip lourd refusée)
    proof_survives_edit le constat survit à une modification de la ligne             (autre clip accepté sans mesure)
    proof_survives_defect le constat survit à un défaut de paire                     (clip qui a répété accepté)

    ./venv/bin/python tests/verif_sync_groups.py
"""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import database  # noqa: E402

database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="syncplay-"), "t.db")
database.init_db()
from app import sync_groups as sg  # noqa: E402

MUT = os.environ.get("SYNC_MUT", "")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


# ---------------------------------------------------------------- monde feint
PER = 40_000_000                      # 25 i/s
NOW = [1_792_000_000_000_000_000]     # TAI de l'orchestrateur (ns)
PLAYERS = {}
FAKE = {}                             # vmid → état du player simulé
CALLS = []
ALERTS = []
DURS = {"a.mov": 10.0, "b.mov": 6.0, "c.mov": 8.0, "long.mov": 100.0, "fill.mov": 10.0, "key.mov": 10.0,
        "key_lent.mov": 10.0, "key_court.mov": 9.0, "key_var.mov": 10.0}
DECODE = {"key_lent.mov": 20.0}
DECODE_SEQ = {}                             # décodages successifs d'un fichier (1re préparation, puis les suivantes)            # i/s de décodage libre mesurés à la préparation (défaut : rapide)
FMT = {"width": 1280, "height": 720, "chroma": "422", "bit_depth": 8, "fps_num": 25, "fps_den": 1, "scan": "progressive"}


def add_player(n, clock_off_ms=0.0, late_slots=0, node=34):
    u, vm = "uuid-%s" % n, 900 + len(PLAYERS)
    PLAYERS[u] = {"vmid": vm, "hostname": "player-%s" % n, "status": "running", "instance_uuid": u,
                  "project_id": None, "node_id": node, "deploy_config": '{"type": "player"}'}
    FAKE[vm] = {"on": False, "left": None, "off": int(clock_off_ms * 1e6), "decks": {}, "sched": [], "applied": [],
                "late": late_slots, "refuse": None, "lag": 0, "late_cum": 0, "anchor": None, "eos": False, "drop": []}
    return u


def fake_http(method, vmid, path, body, timeout=None):
    CALLS.append((vmid, path, dict(body or {})))
    p = FAKE[vmid]
    tai = NOW[0] + p["off"]
    if path == "/sync":
        NOW[0] += p.get("rtt", 0)               # aller-retour lent (relevé d'horloge médiocre)
        tai = NOW[0] + p["off"]
        for s in list(p["sched"]):
            if s["at"] <= tai:
                p["sched"].remove(s)
                p["applied"].append({"id": s["id"], "at": s["at"], "late_slots": p["late"]})
                p["anchor"] = {"g0": s["at"], "m0": s["m0"], "speed": s["speed"]}
        return 200, {"capable": True, "on": p["on"], "left": p["left"], "tai_ns": tai, "fps_num": 25, "fps_den": 1,
                     "format": p.get("format", FMT), "fresh_fps": p.get("fresh", 25.0),
                     "decks": p["decks"], "witness": {"applied": p["applied"][-16:], "late_slots": p["late_cum"],
                                                      "lag": p["lag"], "anchor": p["anchor"], "eos": p["eos"]}}
    if path == "/sync/enter":
        p["on"] = True
        return 200, {}
    if path == "/sync/leave":
        p["on"] = False
        return 200, {}
    if path == "/sync/prepare":
        f = body["file"]
        if p.get("prep_err"):
            p["decks"][body["key"]] = {"state": "error", "error": p["prep_err"]}
            return 202, {"ok": True}
        if f in DECODE_SEQ and DECODE_SEQ[f]:
            DECODE[f] = DECODE_SEQ[f].pop(0)
        p["decks"][body["key"]] = {"state": "ready", "file_duration": DURS.get(f), "in": body.get("in"),
                                   "full": not p.get("never_full"), "decode_fps": DECODE.get(f, 200.0)}
        return 202, {"ok": True}
    if path == "/sync/at":
        if p["refuse"]:
            return 409, {"ok": False, "code": p["refuse"], "tai_ns": tai, "at": body["at"]}
        if body.get("key") and body["key"] not in p["decks"]:
            return 409, {"code": "not_prepared"}
        p["sched"].append(dict(body))
        return 200, {"ok": True}
    if path == "/sync/cancel":
        p["sched"] = [s for s in p["sched"] if s["id"] != body.get("id")]
        return 200, {}
    if path == "/sync/drop":
        p["drop"].append(list(body.get("keep") or []))
        return 200, {}
    return 404, {}


class World(sg.Engine):
    def players(self):
        return dict(PLAYERS)

    def http(self, method, vmid, path, body, timeout=None):
        return fake_http(method, vmid, path, body, timeout)

    def tai(self):
        return NOW[0]

    def sleep(self, s):
        NOW[0] += int(s * 1e9)

    def alert(self, key, params, vmid=None, level="warning"):
        ALERTS.append((key, dict(params), vmid))


E = World()
sg.ENGINE = E
sg._players = lambda: dict(PLAYERS)


# ---------------------------------------------------------------- mutations
if MUT == "per_member_at":
    _orig_send = World._send

    def _send(self, g, rt, at, kind, per_member, players):
        cid = None
        for i, (u, b) in enumerate(per_member.items()):
            cid = _orig_send(self, g, rt, at + i * PER, kind, {u: b}, players)
        return cid
    World._send = _send
elif MUT == "no_snap":
    sg.snap_up = lambda t, p: int(t) + 7
elif MUT == "no_lead":
    _orig_at = World._at
    World._at = lambda self, g, lead_ms=None: self.group_now(g)
elif MUT == "longest_is_min":
    _ld = sg.line_duration

    def _line_duration(g, line, durations):
        if g.get("end_policy") == "longest":
            return min(d for d in durations.values() if d)
        return _ld(g, line, durations)
    sg.line_duration = _line_duration
elif MUT == "no_late_alert":
    _orig_alert = World.alert
    World.alert = lambda self, k, p, vmid=None, level="warning": None if k == "alert.sync.start_late" else _orig_alert(self, k, p, vmid)
elif MUT == "no_refusal_alert":
    _orig_alert2 = World.alert
    World.alert = lambda self, k, p, vmid=None, level="warning": None if k == "alert.sync.not_applied" else _orig_alert2(self, k, p, vmid)
elif MUT == "no_skew_alert":
    _sv0 = sg.sync_verdict

    def _no_clock(chans, period_ms):
        v = _sv0(chans, period_ms)
        v["clock"] = False
        v["defect"] = v["frames"] >= 1 or bool(v["anchors"])
        return v
    sg.sync_verdict = _no_clock
elif MUT == "trust_db":
    _orig_recover = World._recover

    def _recover(self, g, rt):
        tr = g.get("transport") or {}
        rt["recovered"] = True
        rt.update({k: tr.get(k) for k in ("state", "line", "key", "gen", "anchor", "speed_play")})
    World._recover = _recover
elif MUT == "pause_no_freeze":
    _orig_body = World._body_for
    World._body_for = lambda self, g, line, u, key, m, speed: _orig_body(self, g, line, u, key, m, speed or 1.0)
elif MUT == "no_horizon":
    sg.SCHEDULE_HORIZON_S = 1e9
elif MUT in ("tolerate_one_frame", "unc_masks_frames", "ignore_anchor"):
    _sv = sg.sync_verdict

    def _verdict(chans, period_ms):
        v = _sv(chans, period_ms)
        if MUT == "tolerate_one_frame" and v["frames"] == 1:
            v["frames"] = 0
        if MUT == "unc_masks_frames" and v["frames"] * period_ms - v["clock_unc_ms"] - v["skew_unc_ms"] < period_ms:
            v["frames"] = 0
        if MUT == "ignore_anchor":
            v["anchors"] = []
        v["defect"] = v["frames"] >= 1 or bool(v["anchors"]) or v["clock"]
        return v
    sg.sync_verdict = _verdict
elif MUT == "transient_two":
    _orig_alert3 = World.alert

    def _alert3(self, k, p, vmid=None, level="warning"):
        if k == "alert.sync.lag" and p.get("n", 0) < 2:
            return None
        return _orig_alert3(self, k, p, vmid)
    World.alert = _alert3
elif MUT == "pair_any_node":
    _vc = sg._validate_config

    def _vc2(cfg, players, gid=None):
        pl = {u: dict(c, node_id=34) for u, c in players.items()}
        return _vc(cfg, pl, gid)
    sg._validate_config = _vc2
elif MUT == "pair_ignore_cells":
    sg.pair_line_issues = lambda g, line: []
elif MUT in ("pair_no_full", "pair_ignore_decode", "pair_open_proceeds", "open_fail_ignored"):
    _gate0 = World._gate

    def _gate(self, g, line, key, errs, durs, speed, players, deadline=None, proven=()):
        if MUT == "pair_open_proceeds":
            errs = {u: e for u, e in errs.items() if not sg.pair_of(g, u)}
        if MUT == "open_fail_ignored":
            errs = {u: e for u, e in errs.items() if sg.pair_of(g, u)}
        r = _gate0(self, g, line, key, errs, durs, speed, players, deadline, proven)
        if MUT == "pair_no_full" and r == "wait":
            return None
        if MUT == "pair_ignore_decode" and isinstance(r, tuple) and r[0] == "pair_cadence":
            return None
        return r
    World._gate = _gate
elif MUT == "no_proof":
    sg.proven_pairs = lambda g, line, speed: set()
    _pn = World._plan_next

    def _plan_next_np(self, g, rt, players):
        cl = rt.get("clean")
        if cl:
            cl["ok_saved"], cl["ok"] = cl.get("ok"), False
        try:
            return _pn(self, g, rt, players)
        finally:
            if cl:
                cl["ok"] = cl.pop("ok_saved")
    World._plan_next = _plan_next_np
elif MUT == "proof_survives_edit":
    sg.pair_sig = lambda line, a, b, speed: "toujours"
    _lo = sg.lines_op

    def _lines_op(gid_, op, body):
        g_ = sg.get_group(gid_)
        keep = {ln["id"]: dict(ln.get("proven") or {}) for ln in g_["lines"]}
        r = _lo(gid_, op, body)
        g2 = sg.get_group(gid_)
        for ln in g2["lines"]:
            if keep.get(ln["id"]):
                ln["proven"] = keep[ln["id"]]
        sg.save_group(g2)
        return r
    sg.lines_op = _lines_op
elif MUT == "proof_survives_defect":
    World._forget_proof = lambda self, g, lid, pk: None
elif MUT == "pair_repeat_ok":
    World._pair_defect = lambda self, g, rt, pk, detail, vmid=None: None
elif MUT == "switch_unscheduled":
    World._plan_next = lambda self, g, rt, players: None


def ats(path="/sync/at", since=0):
    return [(vm, b) for vm, p, b in CALLS[since:] if p == path]


def tick(n=1, step_ms=250):
    for _ in range(n):
        E.last_poll = 0
        E.tick()
        NOW[0] += int(step_ms * 1e6)


# ---------------------------------------------------------------- fonctions pures
print("— fonctions pures")
ok(sg.snap_up(1_000_000_001, PER) == 1_040_000_000 and sg.snap_up(1_040_000_000, PER) == 1_040_000_000,
   "snap_up ramène au créneau suivant (et laisse un créneau exact)")
a = {"g0": 1_000_000_000, "t0": 2.0, "speed": 2.0}
ok(abs(sg.media_time(a, 1_500_000_000) - 3.0) < 1e-9, "media_time = t0 + v·(t − g0)")
g0 = {"members": ["x", "y", "z"], "end_policy": "longest"}
ln = {"clips": {"x": {"file": "a"}, "y": {"file": "b"}}, "duration": 7.0}
ok(sg.line_duration(g0, ln, {"x": 10.0, "y": 6.0}) == 10.0, "fin de ligne « le plus long » = max")
ok(sg.line_duration(dict(g0, end_policy="shortest"), ln, {"x": 10.0, "y": 6.0}) == 6.0, "« le plus court » = min")
ok(sg.line_duration(dict(g0, end_policy="duration"), ln, {"x": 10.0, "y": 6.0}) == 7.0, "« durée de la ligne »")
ok(sg.line_duration(g0, ln, {"x": 10.0, "y": None}) is None, "« le plus long » avec une durée inconnue : fin constatée")
ok(sg.parse_tc("00:00:02:12", 25) == 2.48 and sg.parse_tc("3.5", 25) == 3.5, "parse_tc (TC et secondes)")
ok(sg.clock_estimate([(100, 900), (40, 200), (70, 500)]) == (40, 100), "horloge : relevé au plus court aller-retour")
ok(sg.group_skew([{"clock_ms": 0.0, "lag": 0}, {"clock_ms": 2.0, "lag": 1}], 40.0)[0] == 42.0,
   "écart du groupe = horloge relative + retard d'image")

# ---------------------------------------------------------------- configuration
print("— configuration")
A, B, C = add_player("a"), add_player("b", node=35), add_player("c")
try:
    sg.create_group({"name": "x", "members": [A]})
    ok(False, "un seul canal refusé")
except sg.SyncError as e:
    ok(e.code == "too_few_members", "un seul canal refusé")
try:
    sg.create_group({"name": "x", "members": [A, B], "lead_ms": 20})
    ok(False, "marge hors bornes refusée")
except sg.SyncError as e:
    ok(e.code == "bad_lead", "marge hors bornes refusée")
gid = sg.create_group({"name": "Mur", "members": [A, B, C]})
g = sg.get_group(gid)
ok(g["end_policy"] == "longest" and g["loop_mode"] == "off" and g["lead_ms"] == 400,
   "défauts : fin au plus long, pas de boucle, marge 400 ms")
sg.lines_op(gid, "add", {"clips": {A: {"file": "a.mov"}, B: {"file": "b.mov"}, C: {"file": "c.mov", "in": 1.0}}})
sg.lines_op(gid, "add", {"clips": {A: {"file": "b.mov"}, B: {"file": "a.mov"}}})
g = sg.get_group(gid)
ok(len(g["lines"]) == 2 and g["cursor"] == g["lines"][0]["id"], "deux lignes, curseur sur la première")
L1, L2 = g["lines"][0]["id"], g["lines"][1]["id"]
sg.lines_op(gid, "save", {"name": "essai"})
ok("essai" in sg.get_group(gid)["saved_lists"], "playlist enregistrée")

# ---------------------------------------------------------------- départ commun
print("— départ commun")
tick(2)
n0 = len(CALLS)
v = sg.op(gid, "play")
sent = ats(since=n0)
at_set = {b["at"] for _, b in sent}
ok(len(sent) == 3 and len(at_set) == 1, "Play : la MÊME commande datée part vers les trois canaux",
   [(vm, b["at"]) for vm, b in sent])
at0 = sent[0][1]["at"]
ok(at0 % PER == 0, "l'instant commun est un créneau de la grille", at0 % PER)
ok(at0 - NOW[0] >= 400e6 - PER, "l'instant commun est dans le futur, à la marge près", (at0 - NOW[0]) / 1e6)
m0 = {vm: b["m0"] for vm, b in sent}
ok(m0 == {900: 0.0, 901: 0.0, 902: 1.0} and all(b["speed"] == 1.0 for _, b in sent),
   "ancre : temps média = point In de chaque clip, vitesse 1", m0)
keys = {b.get("key") for _, b in sent}
ok(len(keys) == 1 and None not in keys, "tous les canaux basculent sur la tête préparée de la même ligne")

# ---------------------------------------------------------------- bascule de ligne datée
print("— ligne suivante")
NOW[0] = at0 + 2 * PER
tick(2)
nxt = [(vm, b) for vm, b in ats(since=n0) if b["at"] != at0]
ok(len(nxt) == 3 and len({b["at"] for _, b in nxt}) == 1,
   "la bascule vers la ligne 2 est DATÉE à l'avance, la même pour tous", nxt)
if nxt:
    sw = nxt[0][1]["at"]
    ok(sw == sg.snap_up(at0 + 10 * 1e9, PER), "fin au plus long : bascule à g0 + 10 s (clips de 10, 6 et 8 s)",
       (sw - at0) / 1e9)
    blk = [b for vm, b in nxt if vm == 902]
    ok(blk and blk[0].get("black"), "un canal sans clip dans la ligne suivante passe au noir", blk)

# ---------------------------------------------------------------- pause, reprise, vitesse
print("— pause / reprise / vitesse")
NOW[0] = at0 + int(3.3e9)
tick(1)
n1 = len(CALLS)
sg.op(gid, "pause")
cancels = [b for vm, p, b in CALLS[n1:] if p == "/sync/cancel"]
ps = ats(since=n1)
ok(len(cancels) == 3, "pause : la bascule datée en attente est annulée sur chaque canal")
ok(len(ps) == 3 and all(b["speed"] == 0.0 for _, b in ps) and len({b["at"] for _, b in ps}) == 1,
   "pause : ancre figée (vitesse 0) au même instant pour tous", ps)
if ps:
    tp = (ps[0][1]["at"] - at0) / 1e9
    ok(abs(ps[0][1]["m0"] - tp) < 1e-6 and abs([b for vm, b in ps if vm == 902][0]["m0"] - (1.0 + tp)) < 1e-6,
       "pause : chaque canal fige sur SON temps média à l'instant commun", [b["m0"] for _, b in ps])
v = sg.group_view(gid)
ok(v["state"] == "paused", "état : en pause")
NOW[0] += int(2e9)
tick(1)
n2 = len(CALLS)
sg.op(gid, "play")
rs = ats(since=n2)
ok(len(rs) == 3 and all(b["speed"] == 1.0 for _, b in rs) and abs(rs[0][1]["m0"] - ps[0][1]["m0"]) < 1e-6,
   "reprise : repart du temps figé, vitesse 1, même instant", rs)
NOW[0] = rs[0][1]["at"] + 2 * PER
tick(2)
n3 = len(CALLS)
sg.op(gid, "speed", {"speed": 2.0})
sp = ats(since=n3)
ok(len(sp) == 3 and all(b["speed"] == 2.0 for _, b in sp), "vitesse 2× envoyée à tous au même instant")
NOW[0] = sp[0][1]["at"] + PER
tick(2)
re = [(vm, b) for vm, b in ats(since=n3) if b["at"] != sp[0][1]["at"]]
if re:
    t_at = sp[0][1]["m0"]
    want = sg.snap_up(sp[0][1]["at"] + (10.0 - t_at) / 2.0 * 1e9, PER)
    ok(re[0][1]["at"] == want, "à 2×, la bascule est re-datée : (durée − t) / 2", (re[0][1]["at"] - want) / 1e6)
else:
    ok(False, "à 2×, la bascule est re-datée")
sg.op(gid, "speed", {"speed": 1.0})

# ---------------------------------------------------------------- défauts signalés
print("— défauts signalés")
FAKE[901]["late"] = 3
ALERTS.clear()
n4 = len(CALLS)
sg.op(gid, "pause")
at_p = ats(since=n4)[0][1]["at"]
NOW[0] = at_p + 3 * PER
tick(2)
FAKE[901]["late"] = 0
ok(any(k == "alert.sync.start_late" and p["c"] == "B" and p["n"] == 3 for k, p, _ in ALERTS),
   "commande appliquée avec 3 créneaux de retard : alerte « en retard »", ALERTS)
FAKE[902]["refuse"] = "clock"
ALERTS.clear()
sg.op(gid, "step", {"frames": 1})
FAKE[902]["refuse"] = None
ok(any(k == "alert.sync.not_applied" and p["c"] == "C" for k, p, _ in ALERTS),
   "commande refusée par un canal (horloge) : alerte « pas appliquée »", ALERTS)
v = sg.group_view(gid)
ok([c["status"] for c in v["channels"] if c["letter"] == "C"] == ["error"] and "C" in v["faulty"],
   "le canal refusé est en défaut dans la vue (et reste en défaut au relevé suivant)", v["channels"][2])
st = [b for vm, b in ats(since=n4) if b["speed"] == 0.0 and vm == 900]
ok(st and abs(st[-1]["m0"] - st[0]["m0"] - 0.04) < 1e-6, "pas à pas : +1 image (40 ms) sur la même tête", [b["m0"] for b in st])
FAKE[900]["sched"].clear()
ALERTS.clear()
FAKE[900]["refuse"] = None
n5 = len(CALLS)
sg.op(gid, "step", {"frames": 1})
FAKE[900]["sched"].clear()                  # le player « perd » la commande : jamais appliquée
at_s = ats(since=n5)[0][1]["at"]
NOW[0] = at_s + int(1.5e9)
tick(2)
ok(any(k == "alert.sync.not_applied" and p["c"] == "A" for k, p, _ in ALERTS),
   "commande jamais constatée au témoin : alerte « pas appliquée »", ALERTS)
ALERTS.clear()
sg.op(gid, "play")
FAKE[901]["off"] = int(60e6)               # B : horloge à +60 ms (1,5 image à 25 i/s)
for _ in range(14):
    tick(1)
ok(any(k == "alert.sync.skew" for k, p, _ in ALERTS), "écart d'horloge de 60 ms (> 1 image) : alerte « écart »",
   ALERTS)
v = sg.group_view(gid)
ok(v["skew_ms"] > 40.0, "l'écart mesuré est publié (vue, macros)", v["skew_ms"])
v = sg.group_view(gid)
ca, cc = [c for c in v["channels"] if c["letter"] in ("A", "C")]
ok(ca["clock_ms"] is not None and ca["clock_ms"] == cc["clock_ms"], "deux canaux d'un même nœud : même horloge, écart nul par construction",
   (ca["clock_ms"], cc["clock_ms"]))
FAKE[901]["off"] = int(10e6)
E.mem[(gid, B)]["clock"].clear()
ALERTS.clear()
for _ in range(14):
    tick(1)
ok(not any(k == "alert.sync.skew" for k, p, _ in ALERTS), "écart de 10 ms (< 1 image) : pas d'alerte", ALERTS)
FAKE[901]["off"] = 0
E.mem[(gid, B)]["clock"].clear()

print("— tolérance zéro")
ALERTS.clear()
FAKE[901]["lag"] = 1                        # B : une image de retard, A et C à l'heure
E.mem[(gid, B)]["clock"].clear()
FAKE[901]["rtt"] = int(30e6)                # relevés d'horloge médiocres sur B : ± 15 ms
tick(4)
v = sg.group_view(gid)
ok(any(k == "alert.sync.offset" for k, p, _ in ALERTS), "UNE image de retard sur un canal : alerte « décalage »",
   ALERTS)
ok([c["status"] for c in v["channels"] if c["letter"] == "B"] == ["offset"] and (v["verdict"] or {}).get("frames") == 1,
   "le canal décalé est signalé dans la vue, écart = 1 image (l'incertitude d'horloge ne le masque pas)",
   (v["verdict"], [c["status"] for c in v["channels"]]))
ok(sg.read_state("in_sync:%d" % gid, {}) is False, "macro « à l'image près » : faux")
v0 = sg.group_view(gid)
ok((v0["verdict"] or {}).get("clock_unc_ms", 0) >= 20, "(l'incertitude d'horloge est bien grande dans ce cas)",
   v0["verdict"])
FAKE[901]["lag"] = 0
FAKE[901]["rtt"] = 0
E.mem[(gid, B)]["clock"].clear()
tick(3)
ALERTS.clear()
FAKE[901]["anchor"] = dict(FAKE[901]["anchor"], m0=FAKE[901]["anchor"]["m0"] + 0.04)   # B : +1 image de temps média
tick(4)
ok(any(k == "alert.sync.offset" and "B" in p["d"] for k, p, _ in ALERTS),
   "même image de grille, temps média différent (ancre) : alerte « décalage », canal nommé", ALERTS)
FAKE[901]["anchor"] = dict(FAKE[901]["anchor"], m0=FAKE[901]["anchor"]["m0"] - 0.04)
tick(3)
ALERTS.clear()
FAKE[900]["late_cum"] += 1                  # A : UNE image répétée puis rattrapée
tick(2)
ok(any(k == "alert.sync.lag" and p["c"] == "A" and p["n"] == 1 for k, p, _ in ALERTS),
   "désynchro passagère d'UNE image : alerte avec le nombre d'images", ALERTS)
v = sg.group_view(gid)
ok((v.get("transient") or {}).get("frames") == 1, "désynchro passagère publiée dans la vue", v.get("transient"))
ALERTS.clear()
FAKE[900]["on"], FAKE[900]["left"] = False, {"reason": "operator:/play", "at": 1}
tick(2)
ok(any(k == "alert.sync.left" and p["c"] == "A" for k, p, _ in ALERTS), "canal repris en main : alerte", ALERTS)
FAKE[900]["on"], FAKE[900]["left"] = True, None

# ---------------------------------------------------------------- macros
print("— macros")
ok(any(a["id"] == "seek" for a in sg.MACRO_PROVIDER["actions"]) and
   any(s["id"] == "skew_ms:%d" % gid for s in sg.MACRO_PROVIDER["states"]), "fournisseur : actions et états")
n6 = len(CALLS)
sg.run_action("seek", {"group": str(gid), "tc": "00:00:05:00"}, {})
sk = ats(since=n6)
ok(sk and all(abs(b["m0"] - (5.0 + (1.0 if vm == 902 else 0.0))) < 1e-6 for vm, b in sk if not b.get("black")),
   "macro « aller à un timecode » : chaque canal à son In + 5 s", sk)
ok(sg.read_state("state:%d" % gid, {}) in ("playing", "paused"), "état lisible en condition")

# ---------------------------------------------------------------- reprise après redémarrage
print("— reprise après redémarrage")
g = sg.get_group(gid)
tr = g["transport"]
ok(tr.get("state") in ("playing", "paused") and tr.get("key"), "l'hypothèse de reprise est persistée", tr)
E2 = World()
E2.last_poll = 0
sg.ENGINE = E2
for vm in FAKE:
    FAKE[vm]["decks"].pop(tr["key"], None) if vm == 901 else None
E2.tick()
ok(E2.rt[gid]["state"] == "stopped" and E2.rt[gid]["recovered"] is False,
   "un canal sans la tête annoncée : la base n'est PAS crue, lecture considérée arrêtée", E2.rt[gid])
sg.ENGINE = E

# ---------------------------------------------------------------- bascule lointaine
print("— bascule lointaine")
sg.op(gid, "stop")
D1, D2 = add_player("d"), add_player("e")
gid2 = sg.create_group({"name": "Long", "members": [D1, D2]})
sg.lines_op(gid2, "add", {"clips": {D1: {"file": "long.mov"}, D2: {"file": "long.mov"}}})
sg.lines_op(gid2, "add", {"clips": {D1: {"file": "a.mov"}, D2: {"file": "a.mov"}}})
tick(2)
n7 = len(CALLS)
sg.op(gid2, "play")
start = ats(since=n7)[0][1]["at"]
NOW[0] = start + PER
tick(4)
far = [b for vm, b in ats(since=n7) if b["at"] != start]
ok(not far, "ligne de 100 s : la bascule n'est PAS datée à +100 s (le player la refuserait)", far)
NOW[0] = start + int(85e9)
tick(4)
far = [b for vm, b in ats(since=n7) if b["at"] != start]
ok(len(far) == 2 and far[0]["at"] == sg.snap_up(start + 100e9, PER),
   "à moins de 20 s de la fin, la bascule est datée à l'instant exact", far)
sg.op(gid2, "stop")

# ---------------------------------------------------------------- paires fill / key
print("— paires fill / key")
sg.PAIR_FULL_TIMEOUT_S = 0.3
F1, K1, X1 = add_player("fill", node=36), add_player("key", node=36), add_player("autre", node=37)
try:
    sg.create_group({"name": "FK", "members": [F1, X1], "pairs": [[F1, X1]]})
    ok(False, "paire sur deux nœuds refusée")
except sg.SyncError as e:
    ok(e.code == "pair_nodes", "paire sur deux nœuds refusée, avec la raison", e)
gfk = sg.create_group({"name": "FK", "members": [F1, K1, X1], "pairs": [[F1, K1]]})
ok(sg.get_group(gfk)["pairs"] == [[F1, K1]] and sg.get_group(gfk)["open_fail"] == "proceed",
   "paire enregistrée ; défaut « partir sans lui »")
vf, vk, vx = PLAYERS[F1]["vmid"], PLAYERS[K1]["vmid"], PLAYERS[X1]["vmid"]
tick(2)


def try_play(clips, title):
    g_ = sg.get_group(gfk)
    for ln in list(g_["lines"]):
        sg.lines_op(gfk, "drop", {"line": ln["id"]})
    sg.lines_op(gfk, "add", {"clips": clips, "title": title})
    n = len(CALLS)
    ALERTS.clear()
    try:
        sg.op(gfk, "play")
        code = None
    except sg.SyncError as e:
        code = e.code
    sent = [vm for vm, b in ats(since=n)]
    sg.op(gfk, "stop")
    return code, sent


code, sent = try_play({F1: {"file": "fill.mov"}, X1: {"file": "a.mov"}}, "cellule seule")
ok(code == "pair_cell" and not sent and any(k == "alert.sync.refused" for k, p, _ in ALERTS),
   "ligne avec le fill sans le key : départ refusé, expliqué, rien n'est envoyé", (code, sent, ALERTS))
code, sent = try_play({F1: {"file": "fill.mov", "in": 1.0}, K1: {"file": "key.mov", "in": 1.04}}, "in")
ok(code == "pair_in" and not sent, "points In différents : refusé", (code, sent))
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key_court.mov"}}, "durée")
ok(code == "pair_duration" and not sent, "durées différentes (10 s / 9 s) : refusé", (code, sent))
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key_lent.mov"}}, "lent")
ok(code == "pair_cadence" and not sent, "clip décodé à 20 i/s pour 25 : refusé (ne tient pas la cadence)", (code, sent))
FAKE[vk]["never_full"] = True
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}}, "pas plein")
FAKE[vk]["never_full"] = False
ok(code == "pair_not_full" and not sent, "files pas pleines : la paire ne part pas", (code, sent))
FAKE[vk]["prep_err"] = "fichier illisible"
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}, X1: {"file": "a.mov"}}, "key fermé")
FAKE[vk]["prep_err"] = None
ok(code == "pair_open" and not sent, "key qui ne s'ouvre pas : refus imposé même en « partir sans lui »", (code, sent))
FAKE[vx]["prep_err"] = "fichier illisible"
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}, X1: {"file": "a.mov"}}, "autre fermé")
ok(code is None and vf in sent and vk in sent, "« partir sans lui » : un canal hors paire fermé, les autres partent",
   (code, sent))
sg.update_group(gfk, {"open_fail": "refuse"})
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}, X1: {"file": "a.mov"}}, "refus")
FAKE[vx]["prep_err"] = None
ok(code == "open_refused" and not sent, "« refuser le départ » : un canal fermé, personne ne part", (code, sent))
sg.update_group(gfk, {"open_fail": "proceed"})
code, sent = try_play({F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}}, "conforme")
ok(code is None and set(sent) >= {vf, vk}, "paire conforme : elle part", (code, sent))
sg.lines_op(gfk, "drop", {"line": sg.get_group(gfk)["lines"][0]["id"]})
sg.lines_op(gfk, "add", {"clips": {F1: {"file": "fill.mov"}, K1: {"file": "key.mov"}}})
sg.op(gfk, "play")
NOW[0] += int(1e9)
tick(2)
ALERTS.clear()
FAKE[vk]["late_cum"] += 1                   # le key répète UNE image
tick(2)
v = sg.group_view(gfk)
ok(any(k == "alert.sync.pair" for k, p, _ in ALERTS) and v["pairs"][0]["state"] == "defect",
   "une image répétée sur le key seul : défaut de paire signalé (vue, alerte)", (ALERTS, v["pairs"]))
ok(sg.read_state("pairs_ok:%d" % gfk, {}) is False and sg.read_state("in_sync:%d" % gfk, {}) is False,
   "macros : paire en défaut, groupe pas « à l'image près »")
ok([c["pair"] for c in v["channels"]][:2] == ["B", "A"], "les deux cartes de la paire sont liées dans la vue",
   [c["pair"] for c in v["channels"]])
sg.op(gfk, "stop")

print("— paire : ligne déjà jouée sans défaut")


def set_line(clips):
    g_ = sg.get_group(gfk)
    for ln in list(g_["lines"]):
        sg.lines_op(gfk, "drop", {"line": ln["id"]})
    sg.lines_op(gfk, "add", {"clips": clips})
    return sg.get_group(gfk)["lines"][0]["id"]


def play_to_end(lid):
    """Joue la ligne (10 s) jusqu'à sa bascule de boucle ; renvoie (bascule datée ?, refus)."""
    n = len(CALLS)
    sg.op(gfk, "play")
    st = ats(since=n)[0][1]["at"]
    NOW[0] = st + PER
    tick(3)
    sched = [b for vm, b in ats(since=n) if b["at"] != st]
    NOW[0] = st + int(10e9) + 2 * PER
    tick(3)
    return bool(sched), (sg.group_view(gfk).get("blocked") or [None])[0]


sg.update_group(gfk, {"loop_mode": "line"})
DECODE_SEQ["key_var.mov"] = [30.0, 20.0, 20.0, 20.0, 20.0]   # accepté seul ; en boucle, sous contention : 20 i/s
lid = set_line({F1: {"file": "fill.mov"}, K1: {"file": "key_var.mov"}})
sched, blocked = play_to_end(lid)
ok(sched and not blocked, "boucle d'un clip lourd (20 i/s mesurés en préparation) : accordée d'avance, la lecture en cours "
   "allant au bout sans défaut", (sched, blocked))
ln = sg.get_group(gfk)["lines"][0]
ok("A+B" in (ln.get("proven") or {}), "fin de ligne sans défaut : constat « déjà jouée sans défaut » enregistré", ln.get("proven"))
ok(sg.read_state("line_proven:%d" % gfk, {}) is True, "constat lisible par les macros")
ok("A+B" in sg.group_view(gfk)["lines"][0]["proven"], "constat visible dans la vue")
sg.op(gfk, "stop")
sg.update_group(gfk, {"loop_mode": "off"})
try:
    sg.op(gfk, "play")
    code = None
except sg.SyncError as e:
    code = e.code
ok(code is None, "relecture de la ligne prouvée : acceptée malgré 20 i/s mesurés", code)
NOW[0] += int(3e9)
tick(2)
FAKE[vk]["late_cum"] += 1                   # le key répète UNE image : défaut de paire
tick(2)
ok(not (sg.get_group(gfk)["lines"][0].get("proven") or {}), "défaut de paire sur la ligne : le constat se PERD",
   sg.get_group(gfk)["lines"][0].get("proven"))
sg.op(gfk, "stop")
try:
    sg.op(gfk, "play")
    code = None
except sg.SyncError as e:
    code = e.code
sg.op(gfk, "stop")
ok(code == "pair_cadence", "après le défaut, la mesure au remplissage refuse de nouveau", code)
sg.update_group(gfk, {"loop_mode": "line"})
DECODE_SEQ["key_var.mov"] = [30.0, 20.0, 20.0]
play_to_end(lid)
sg.op(gfk, "stop")
ok("A+B" in (sg.get_group(gfk)["lines"][0].get("proven") or {}), "(constat rétabli par une lecture entière sans défaut)")
for u in (F1, K1):
    c = dict(sg.get_group(gfk)["lines"][0]["clips"][u], **{"in": 1.0})
    sg.lines_op(gfk, "set_cell", {"line": lid, "member": u, "clip": c})
DECODE_SEQ["key_var.mov"] = [20.0, 20.0]
try:
    sg.op(gfk, "play")
    code = None
except sg.SyncError as e:
    code = e.code
sg.op(gfk, "stop")
ok(code == "pair_cadence", "ligne modifiée (points In) : le constat ne vaut plus, la mesure refuse", code)

# ---------------------------------------------------------------- arrêt
sg.op(gid, "stop")
ok(all(not FAKE[vm]["on"] for vm in (900, 901, 902)), "arrêt : chaque player quitte le mode synchrone")

print()
if ko:
    print("ÉCHECS (%d) : %s" % (len(ko), json.dumps(ko, ensure_ascii=False)))
    sys.exit(1)
print("verif_sync_groups : tout est vert%s" % (" (mutation %s NON détectée !)" % MUT if MUT else ""))
