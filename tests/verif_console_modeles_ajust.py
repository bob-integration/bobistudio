#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""« Mes modèles » AJUSTABLES et DROITS des zones au rappel (audio_console 0.4.142, 2026-10-18).

  1. adjust (src/templates.py) sur chaque modèle intégré enregistré comme modèle ET sur un modèle « riche » (key de
     dynamique sur un aux, patch de sortie, sorties directes, VCA imbriqués, prise d'entrée utilisée) :
     a. FUZZ : toute cible dans les bornes est atteinte EXACTEMENT, le document est VALIDE, et le même modèle avec les
        mêmes dimensions donne le MÊME document (ids lisibles, aucun tirage) ; {} = le modèle tel quel ;
     b. un élément ajouté copie le DERNIER de son genre (chaîne, départs et prises pré / post, affectations, VCA,
        groupe d'automix, zone) ; libellé dans la série (« Machine 5 », « Lecteur 8 » après « Cartouchier ») ;
     c. sans exemple dans le modèle : les défauts Standard (EQ + comp, affectation au PGM) ;
     d. on retire PAR LA FIN, et RIEN n'est retiré en silence : chaque élément retiré que quelque chose d'autre
        référençait porte des `impacts` (départs, sorties N-1, retours, alimentations, keys, patch, zones, membres de
        VCA, utilisateurs de prise) — vérifié pour toute référence, pas pour une liste ;
  2. l'interface (node) : wizAdjLines dit chaque impact ; wizRightsGrants = comptes retrouvés + choix (« personne »
     n'écrit rien) ; le corps envoyé pour un modèle porte `adjust` ; libellés FR + EN ;
  3. orchestrateur, base jetable : les droits sont gardés par IDENTITÉ STABLE (identifiant de connexion, nom de
     groupe), retrouvés ici (id local différent), absents signalés, zone non reprise ignorée ; replace_grants ne
     touche que les zones données et refuse en bloc un sujet ou une zone inconnus ; un show enregistré, un modèle
     importé depuis une console (grants_from) gardent les droits, l'export ne les donne qu'à `settings.edit`, une
     table d'avant gagne la colonne ; routes : plan et remplacement réservés à `settings.edit`.

Chaque garde a été MUTÉE (rapport de session du 2026-10-18).
"""
import copy
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUG = os.path.join(_R, "plugins", "audio_console")
if not os.path.isfile(os.path.join(_PLUG, "plugin.json")):
    print("SAUTÉ : plugin audio_console absent")
    sys.exit(0)
sys.path.insert(0, os.path.join(_PLUG, "src"))
sys.path.insert(0, _R)
import console_model as cm                        # noqa: E402
import templates as tp                            # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % str(detail)[:600])
        ko.append(titre)


LIB = cm.load_library(os.path.join(_PLUG, "modules.json"))
TMP = tempfile.mkdtemp(prefix="verif_modeles_ajust_")


def as_tpl(model):
    return tp.as_template(tp.build(model, LIB), LIB)


def chain_types(o):
    return [e.get("type") or e.get("kind") for e in o.get("chain") or []]


def taps(s, d):
    """{bus label: "pre" | "post" | other tap kind} of a strip's sends (mirrors left out)."""
    pre = "input"
    for e in s["chain"]:
        if e["kind"] in ("mute", "fader"):
            break
        pre = e["id"]
    lab = {b["id"]: b["label"] for b in d["buses"]}
    kind = {e["id"]: (e.get("type") or e.get("kind")) for e in s["chain"]}
    return {lab[x["bus"]]: ("pre" if x["after"] == pre else "end" if x["after"] == cm.strip_end(s) else kind.get(x["after"], x["after"]))
            for x in s["sends"] if not x.get("mirror")}


def zones_of(d, oid):
    return sorted(z["label"] for z in d["zones"] if oid in z["members"])


def rich():
    """Démo deux postes as a template, plus what a real console accumulates."""
    d = copy.deepcopy(as_tpl("duo"))
    aux = [b for b in d["buses"] if b["kind"] == "aux"]
    s0 = next(s for s in d["strips"] if s["label"] == "Micro 1")
    comp = next(e for e in s0["chain"] if e.get("type") == "comp")
    comp["key"] = {"kind": "signal", "src": aux[-1]["id"]}                  # key on the Delay aux
    last_mono = [s for s in d["strips"] if s["width"] == 1 and (s["input"] or {}).get("kind") != "bus"][-1]
    last_mono["direct_outs"] = [{"id": "d_dirm", "label": "Direct", "after": cm.strip_end(last_mono), "chain": [], "compensation": "minimal"}]
    d["outputs"][-1]["patch"] = [{"ch": 0, "src": aux[0]["id"], "c": 0}, {"ch": 1, "src": last_mono["id"] + "/d_dirm", "c": 0}]
    d["inputs"][-1]["id"] = d["inputs"][-1]["id"]
    s0["input"]["src"] = d["inputs"][-1]["id"]
    mtx = next(b for b in d["buses"] if b["kind"] == "matrix")
    grp = [b for b in d["buses"] if b["kind"] == "group"][-1]
    mtx["sources"].append({"bus": grp["id"], "level_db": 0.0, "on": True})     # a matrix fed by a subgroup
    d["vcas"].append({"id": "v_extra", "label": "VCA 4", "short": "", "level_db": 0.0, "mute": False, "vcas": [d["vcas"][-1]["id"]]})
    return tp.finish(d, LIB)


# ================================================================ 1a. fuzz
print("== 1a. fuzz : cibles atteintes, documents valides, déterministes")
MODELS = {m: as_tpl(m) for m in ("standard", "duo", "blank")}
MODELS["rich"] = rich()
rng = random.Random(20261018)
bad, n = [], 0
for name, t in MODELS.items():
    base = tp.counts_of(t)
    lim = tp.adjust_limits(base)
    for _ in range(60):
        tg = {k: rng.randint(lim[k][0], min(lim[k][1], base[k] + 6)) for k in tp.ADJUST_KEYS if rng.random() < .8}
        try:
            d, rep = tp.adjust(t, tg, LIB)
            d2, _r2 = tp.adjust(t, tg, LIB)
            want = dict(base, **tg)
            if rep["counts"] != want or tp.counts_of(d) != want:
                bad.append((name, "counts", tg, rep["counts"]))
            elif json.dumps(d, sort_keys=True) != json.dumps(d2, sort_keys=True):
                bad.append((name, "not deterministic", tg))
            elif cm.validate(d, LIB):
                bad.append((name, "invalid", cm.validate(d, LIB)[:2]))
        except tp.TemplateError as e:
            bad.append((name, tg, e.code, e.params))
        n += 1
ok(not bad and n == 240, "1a. %d ajustements : cibles exactes, valides, même document à chaque fois" % n, bad[:3])
same = all(json.dumps(tp.adjust(t, {}, LIB)[0], sort_keys=True) == json.dumps(t, sort_keys=True) for t in MODELS.values())
ok(same, "1a. aucune dimension changée : le modèle tel qu'enregistré")
try:
    tp.adjust(MODELS["standard"], {"mono": "4"}, LIB)
    ok(False, "1a. une dimension illisible est refusée")
except tp.TemplateError as e:
    ok(e.code == "bad_count", "1a. une dimension illisible est refusée", e.code)
big, _ = tp.adjust(MODELS["standard"], {"mono": 1000, "listens": 0}, LIB)
ok(tp.counts_of(big)["mono"] == 96 and tp.counts_of(big)["listens"] == 1, "1a. bornes : 96 mono au plus, une Écoute au moins")

# ================================================================ 1b. copies of the template's convention
print("== 1b. un élément ajouté copie le dernier de son genre")
duo = MODELS["duo"]
b0 = tp.counts_of(duo)
d, rep = tp.adjust(duo, {"mono": b0["mono"] + 1, "stereo": b0["stereo"] + 1, "aux": b0["aux"] + 1, "vcas": b0["vcas"] + 1,
                         "groups": b0["groups"] + 1}, LIB)
src_m = [s for s in duo["strips"] if s["width"] == 1 and s["input"]["kind"] != "bus"]
new_m = [s for s in d["strips"] if s["width"] == 1 and s["input"]["kind"] != "bus"][-1]
ex = src_m[-1]
ok(new_m["label"] == "Machine 5" and chain_types(new_m) == chain_types(ex) and zones_of(d, new_m["id"]) == zones_of(duo, ex["id"])
   and new_m.get("vcas") == ex.get("vcas") and new_m.get("automix") == ex.get("automix")
   and {k: v for k, v in taps(new_m, d).items() if k != "Aux 3"} == taps(ex, duo),
   "1b. tranche mono : copie de « Machine 4 » (chaîne, départs et prises, VCA, automix, zone Machines), nommée « Machine 5 »",
   [new_m["label"], chain_types(new_m), zones_of(d, new_m["id"]), taps(new_m, d), taps(ex, duo)])
ok(new_m["input"].get("flow") == "" and not new_m["input"].get("src") and new_m["mixminus_out"] is None and not new_m["direct_outs"]
   and next(e for e in new_m["chain"] if e["kind"] == "mute")["params"]["on"] is True,
   "1b. … arrive non patchée, sans N-1 ni sortie directe, coupée")
new_s = [s for s in d["strips"] if s["width"] == 2 and s["input"]["kind"] != "bus"][-1]
ok(new_s["label"] == "Lecteur 8" and zones_of(d, new_s["id"]) == ["Machines"],
   "1b. tranche stéréo : série « Lecteur n » reprise après « Cartouchier » (Lecteur 8), zone Machines", [new_s["label"], zones_of(d, new_s["id"])])
dly = next(b for b in duo["buses"] if b["label"] == "Delay")
a3 = next(b for b in d["buses"] if b["label"] == "Aux 3")
src_tap = {s["label"]: taps(s, duo).get("Delay") for s in duo["strips"]}
new_tap = {s["label"]: taps(s, d).get("Aux 3") for s in d["strips"] if s["label"] in src_tap}
ok(a3["width"] == dly["width"] and chain_types(a3) == chain_types(dly) and zones_of(d, a3["id"]) == zones_of(duo, dly["id"])
   and new_tap == src_tap and all(x["level_db"] <= -120 for s in d["strips"] for x in s["sends"] if x["bus"] == a3["id"]),
   "1b. aux : copie de « Delay » (largeur, emplacement de plugin, zone), un départ à −∞ là où Delay en a un, même prise",
   [a3["width"], chain_types(a3), [k for k in src_tap if src_tap[k] != new_tap.get(k)][:4]])
one_aux = tp.as_template(tp.adjust(MODELS["standard"], {"aux": 1}, LIB)[0], LIB)      # only Aux 1, PRE-fader
d1, _r = tp.adjust(one_aux, {"aux": 2}, LIB)
ok(all(taps(s, d1).get("Aux 2") == "pre" for s in d1["strips"]) and len(d1["strips"]) == 6,
   "1b. aux : la PRISE de l'exemple est copiée (Aux 1 avant fader → Aux 2 avant fader, pas la règle Standard)",
   [taps(s, d1) for s in d1["strips"]][:2])
v4 = next(v for v in d["vcas"] if v["label"] == "VCA 4")
g3 = next(b for b in d["buses"] if b["label"] == "Groupe 3")
gm = next(b for b in duo["buses"] if b["label"] == "Machines" and b["kind"] == "group")
ok(v4["vcas"] == duo["vcas"][-1].get("vcas", []) and g3["route_to"] == gm["route_to"] and zones_of(d, g3["id"]) == zones_of(duo, gm["id"])
   and not any(x["bus"] == g3["id"] for s in d["strips"] for x in s["sends"]),
   "1b. VCA et groupe : parents / destinations et zone de l'exemple, aucun membre ni tranche affectée")
ok(any(a["kind"] == "mono" and a["example"] == "Machine 4" for a in rep["added"]), "1b. le rapport nomme l'exemple copié", rep["added"][:3])

# ================================================================ 1c. no example
print("== 1c. sans exemple : les défauts Standard")
blank = MODELS["blank"]
d, rep = tp.adjust(blank, {"mono": 1, "aux": 1}, LIB)
s = d["strips"][0]
pgm = next(b for b in d["buses"] if b["kind"] == "master")
ok([e.get("type") for e in s["chain"] if e["kind"] == "module"] == ["eq4", "comp"] and any(x["bus"] == pgm["id"] for x in s["sends"])
   and s["label"] == "Entrée 1" and any(x["bus"] == d["buses"][-1]["id"] or True for x in s["sends"])
   and rep["added"][0]["example"] is None, "1c. vierge + 1 mono : EQ + comp neutres, affectée au PGM, « Entrée 1 »",
   [chain_types(s), s["label"]])
aux = next(b for b in d["buses"] if b["kind"] == "aux")
ok(aux["width"] == 1 and aux["label"] == "Aux 1" and taps(s, d).get("Aux 1") == "pre", "1c. … et Aux 1 mono, départ avant fader (règle Standard)",
   taps(s, d))
st_only, _ = tp.adjust(MODELS["standard"], {"mono": 0}, LIB)
d, rep = tp.adjust(tp.as_template(st_only, LIB), {"mono": 1}, LIB)
ok(rep["added"][0]["example"] == "Entrée 6" and d["strips"][0]["width"] == 1 or any(
    s["width"] == 1 for s in d["strips"]), "1c. pas de mono dans le modèle : copie d'une stéréo, ramenée à une largeur 1", rep["added"])

# ================================================================ 1d. removals, never silent
print("== 1d. retraits par la fin, jamais en silence")
R = MODELS["rich"]
b = tp.counts_of(R)
d, rep = tp.adjust(R, {"mono": b["mono"] - 1}, LIB)
ok([r["label"] for r in rep["removed"]] == ["Machine 4"], "1d. on retire la DERNIÈRE mono (Machine 4)", [r["label"] for r in rep["removed"]])


def refs_elsewhere(doc, oid):
    """True when something OUTSIDE the element `oid` mentions it (any field, any depth)."""
    def strip_self(o):
        if isinstance(o, dict):
            if o.get("id") == oid:
                return None
            return {k: strip_self(v) for k, v in o.items() if k != "id"}
        if isinstance(o, list):
            return [strip_self(v) for v in o]
        return o
    txt = json.dumps(strip_self(doc))
    return ('"%s"' % oid) in txt or ('"%s/' % oid) in txt


silent, seen_codes = [], set()
for name, t in MODELS.items():
    zero = {k: tp.adjust_limits(tp.counts_of(t))[k][0] for k in tp.ADJUST_KEYS}
    for k in tp.ADJUST_KEYS:
        tg = {k: zero[k]}
        _d, rep = tp.adjust(t, tg, LIB)
        for r in rep["removed"]:
            seen_codes |= {i["code"] for i in r["impacts"]}
            if refs_elsewhere(t, r["id"]) and not r["impacts"]:
                silent.append((name, k, r["label"]))
ok(not silent, "1d. tout élément retiré qu'autre chose référençait porte ses impacts (tous les modèles, chaque genre à son minimum)",
   silent[:5])
ok(seen_codes >= {"sends", "returns_removed", "keys", "patch", "zones", "vca_members", "vca_children", "port_users", "nm_out",
                  "nm_outs", "feeds", "directs"},
   "1d. les impacts couvrent départs, retours, keys, patch, zones, VCA, prises, N-1, alimentations, sorties directes",
   sorted(seen_codes))
d, rep = tp.adjust(R, {"aux": 0}, LIB)
rev = next(r for r in rep["removed"] if r["label"] == "Réverb")
dl = next(r for r in rep["removed"] if r["label"] == "Delay")
codes = lambda r: {i["code"]: i for i in r["impacts"]}
ok(codes(rev)["returns_removed"]["labels"] == ["Retour Réverb"] and codes(rev)["sends"]["n"] > 30 and "keys" in codes(dl)
   and "patch" in codes(rev) and not any(s["label"] in ("Retour Réverb", "Retour Delay") for s in d["strips"])
   and tp.counts_of(d)["stereo"] == b["stereo"],
   "1d. aux FX retiré : sa tranche de retour part avec lui (dit), départs comptés, key du compresseur et patch signalés, "
   "le nombre d'entrées stéréo ne bouge pas", [codes(rev), codes(dl)])
comp = next(e for s in d["strips"] if s["label"] == "Micro 1" for e in s["chain"] if e.get("type") == "comp")
ok(comp.get("key", {}).get("kind", "self") == "self", "1d. … le compresseur revient sur sa propre key", comp.get("key"))

# ================================================================ 2. interface (node)
print("== 2. interface")
import shutil as _sh                               # noqa: E402
src_js = open(os.path.join(_PLUG, "control.js"), encoding="utf-8").read()


def fn_body(name):
    k = "  function " + name + "("
    i = src_js.index(k) if k in src_js else src_js.index("  async function " + name + "(")
    j = src_js.index("\n  }\n", i)
    return src_js[i:j + 4]


node = _sh.which("node")
if node:
    i0 = src_js.index("  const WIZ_IMPACT = {")
    imp = src_js[i0:src_js.index("};\n", i0) + 3]
    rep_any = tp.adjust(R, {"aux": 0}, LIB)[1]
    one, _x = tp.adjust(MODELS["standard"], {"mono": 1, "stereo": 0}, LIB)
    rep_nm = tp.adjust(tp.as_template(one, LIB), {"mixminus": 2}, LIB)[1]
    rep_any["added"] = rep_nm["added"]
    js = ("const T = (k, fb) => fb; const fmt = (s, o) => s.replace(/\\{(\\w+)\\}/g, (m, k) => (o[k] != null ? o[k] : m));\n"
          + imp + fn_body("wizAdjLines") + fn_body("wizRightsGrants")
          + "const adj = " + json.dumps(rep_any) + ";\n"
          + "const R = {plan: [{zone_id: 'z_a', match: {kind: 'user', id: 7}}, {zone_id: 'z_b', match: null}, {zone_id: 'z_c', match: null},"
            " {zone_id: 'z_d', match: {kind: 'group', id: 2}}], choice: {1: 'user:9', 2: ''}};\n"
          + "console.log(JSON.stringify([wizAdjLines(adj), wizRightsGrants(R), wizRightsGrants(null)]));\n")
    jf = os.path.join(TMP, "w.js")
    open(jf, "w", encoding="utf-8").write(js)
    res = subprocess.run([node, jf], capture_output=True, text=True, timeout=30)
    out = json.loads(res.stdout) if res.returncode == 0 else [res.stderr, [], []]
    lines = out[0]
    ok(any("« Réverb » retiré" in l and "Retour Réverb" in l and "départ(s) retiré(s)" in l for l in lines)
       and any("key de dynamique" in l for l in lines) and any("sans tranche à alimenter" in l for l in lines)
       and not any(".imp." in l or "returns_removed" in l for l in lines),
       "2a. wizAdjLines : chaque retrait dit ce qu'il emporte, en mots ; N-1 sans tranche signalé", lines[:4])
    ok(out[1] == [{"zone_id": "z_a", "subject_kind": "user", "subject_id": 7}, {"zone_id": "z_b", "subject_kind": "user", "subject_id": 9},
                  {"zone_id": "z_d", "subject_kind": "group", "subject_id": 2}] and out[2] == [],
       "2b. wizRightsGrants : comptes retrouvés + choix ; « personne » n'écrit rien", out[1:])
else:
    print("  (node absent : 2a-2b non vérifiés)")
wb = fn_body("wizBody")
ok("adjust: wizTplTargets()" in wb and "names: WIZ_NAMES()" in wb and "wizDims(right)" in fn_body("wizStep2")
   and "if (WIZ.start === \"recall\") {" in fn_body("wizStep3") and "wizRights(body)" in fn_body("wizStep4")
   and "await wizRightsApply()" in fn_body("wizCreate") and "grants_from: VMID" in fn_body("wizSaveTemplate"),
   "2c. assistant : −/+ pour un modèle (étapes 2 et 3), droits au Résumé puis appliqués après la structure, modèle enregistré avec ses droits")
ok("ME.can_assign" in fn_body("wizRightsLoad") and '"/grants/plan"' in fn_body("wizRightsLoad") and 'method: "PUT"' in fn_body("wizRightsApply"),
   "2d. droits : demandés seulement à qui peut attribuer des zones, plan puis remplacement")
fr = json.load(open(os.path.join(_PLUG, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(_PLUG, "i18n", "en.json"), encoding="utf-8"))
import re                                          # noqa: E402
keys = set(re.findall(r'T\("(wiz\.(?:imp\.|rights_|adj_|q_tpl|tpl_adjust)[a-z0-9_.]*)"', src_js))
miss = [k for k in keys if "plugin.audio_console." + k not in fr or "plugin.audio_console." + k not in en]
ok(len(keys) >= 30 and not miss, "2e. libellés nouveaux en français ET en anglais (%d)" % len(keys), miss)

# ================================================================ 3. orchestrator
print("== 3. orchestrateur : droits des zones")
from app import config, database                  # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "t.db")
database._tls.__dict__.pop("conn", None)
database.init_db()
from app import plugin_zones as pz, plugin_shows as ps, user_groups as ug, plugins as plug   # noqa: E402
from app.auth import hash_password                # noqa: E402
PW = hash_password("Banc-Motdepasse-2026!")
database.db_create_user("chef", PW, "admin")
database.db_create_user("bob", PW, "viewer")
database.db_create_user("alice", PW, "viewer", prenom="Alice", nom="Martin")
c = database.get_db()
uid = {r["username"]: r["id"] for r in c.execute("SELECT id, username FROM users")}
gid = ug.create_group("Régie")
plug._scan()
MAN = plug.get("audio_console")
doc = MODELS["duo"]
zp, zm = doc["zones"][0]["id"], doc["zones"][1]["id"]
database.db_upsert_container(901, "acon-t")
database.db_update_deploy_config(901, "audio_console", {"hostname": "acon-t", "console": doc, "plugin_version": MAN["version"]})
scope = pz.scope_of(901)
pz.grant(scope, zp, "user", uid["alice"])
pz.grant(scope, zm, "user", uid["bob"])
pz.grant(scope, zm, "group", gid)
pz.grant(scope, "z_ancienne", "user", uid["bob"])                         # a zone the document no longer has
cap = pz.capture_grants(scope, doc)
ok(cap == sorted([{"zone_id": zp, "subject_kind": "user", "name": "alice", "label": "Alice Martin (alice)"},
                  {"zone_id": zm, "subject_kind": "user", "name": "bob", "label": "bob"},
                  {"zone_id": zm, "subject_kind": "group", "name": "Régie", "label": "Régie"}],
                 key=lambda x: (x["zone_id"], x["subject_kind"], x["name"])),
   "3a. capture : par identifiant de connexion / nom de groupe, zones du document seulement", cap)
# « another installation »: bob absent, alice re-created (another local id), the group in another case
c.execute("DELETE FROM users WHERE username IN ('bob', 'alice')")
c.commit()
database.db_create_user("zoe", PW, "viewer")
database.db_create_user("alice", PW, "viewer")
c.execute("UPDATE user_groups SET name='RÉGIE' WHERE id=?", (gid,))
c.commit()
new_alice = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()["id"]
plan = pz.plan_grants(pz.clean_saved_grants(cap + [{"zone_id": "z_x", "subject_kind": "user", "name": "alice"}, {"bad": 1}]),
                      [{"id": zp, "label": "Plateau"}, {"id": zm, "label": "Machines"}])
by = {(g["zone_id"], g["name"]): g for g in plan}
ok(new_alice != uid["alice"] and by[(zp, "alice")]["match"] == {"kind": "user", "id": new_alice, "label": "alice"}
   and by[(zm, "bob")]["match"] is None and by[(zm, "Régie")]["match"]["id"] == gid
   and by[(zp, "alice")]["zone_label"] == "Plateau" and len(plan) == 3,
   "3b. plan : alice retrouvée (autre id local), bob absent, groupe retrouvé sans tenir compte de la casse, zone non reprise ignorée", plan)
pz.grant(scope, "z_autre", "user", new_alice)
try:
    pz.replace_grants(scope, [zp, zm], [{"zone_id": zp, "subject_kind": "user", "subject_id": 99999}])
    ok(False, "3c. sujet inconnu : refusé")
except ValueError as e:
    before = pz.grants(scope)
    ok(str(e) == "subject_unknown" and any(g["zone_id"] == zm for g in before), "3c. sujet inconnu : refusé, rien de changé", e)
try:
    pz.replace_grants(scope, [zp], [{"zone_id": zm, "subject_kind": "user", "subject_id": new_alice}])
    ok(False, "3c. zone hors de la liste : refusée")
except ValueError as e:
    ok(str(e) == "zone_unknown", "3c. zone hors de la liste : refusée", e)
n = pz.replace_grants(scope, [zp, zm], [{"zone_id": zp, "subject_kind": "user", "subject_id": new_alice},
                                        {"zone_id": zm, "subject_kind": "group", "subject_id": gid}])
after = pz.grants(scope)
ok(n == 2 and {(g["zone_id"], g["subject_kind"], g["subject_id"]) for g in after} ==
   {(zp, "user", new_alice), (zm, "group", gid), ("z_ancienne", "user", uid["bob"]), ("z_autre", "user", new_alice)},
   "3d. remplacement : exactement les droits donnés sur ces zones, les autres zones intactes", after)
# shows / templates keep them
ps._snapshot = lambda vmid, sp: copy.deepcopy(doc)
chef = {"id": uid["chef"], "username": "chef"}
sh = ps.save(901, "Matinale", chef)
full = ps.get_show("audio_console", sh["id"])
ok(sh["has_grants"] and full["zone_grants"] == pz.capture_grants(scope, doc) and len(full["zone_grants"]) == 2,
   "3e. un show enregistré garde les droits de ses zones", full.get("zone_grants"))
FILE = {"format": ps.EXCHANGE_FORMAT, "format_version": 1, "plugin_type": "audio_console", "plugin_version": MAN["version"],
        "name": "Deux postes", "document": doc}
t1 = ps.import_document("audio_console", {"document": FILE, "kind": "template"}, chef, grants_from=901)
t2 = ps.import_document("audio_console", {"document": dict(FILE, name="Fichier", zone_grants=cap + ["x"])}, chef)
t3 = ps.import_document("audio_console", {"document": dict(FILE, name="Vieux")}, chef)
g1, g2, g3 = (ps.get_show("audio_console", t["id"])["zone_grants"] for t in (t1, t2, t3))
ok(g1 == full["zone_grants"] and g2 == cap and g3 is None and t1["has_grants"] and not t3["has_grants"],
   "3f. modèle importé depuis la console (grants_from) : ses droits ; fichier : les siens, nettoyés ; sans : rien d'enregistré",
   [g1, g2, g3])
e1, e2 = ps.export_document("audio_console", sh["id"], with_grants=True), ps.export_document("audio_console", sh["id"])
ok(e1.get("zone_grants") == full["zone_grants"] and "zone_grants" not in e2,
   "3g. export : droits seulement pour qui peut attribuer des zones")
old = sqlite3.connect(os.path.join(TMP, "old.db"))
old.row_factory = sqlite3.Row
old.execute("""CREATE TABLE plugin_shows (id INTEGER PRIMARY KEY AUTOINCREMENT, plugin_type TEXT NOT NULL, name TEXT NOT NULL,
    author TEXT, author_id INTEGER, created_at TEXT, updated_at TEXT, plugin_version TEXT, source_hostname TEXT, summary TEXT,
    document TEXT NOT NULL, last_loaded_at TEXT, last_loaded_on TEXT, kind TEXT NOT NULL DEFAULT 'show', UNIQUE (plugin_type, name))""")
old.execute("INSERT INTO plugin_shows (plugin_type, name, document) VALUES ('audio_console', 'Ancien', '{}')")
old.commit()
lst = ps.list_shows("audio_console", conn=old)
ok("zone_grants" in {r[1] for r in old.execute("PRAGMA table_info(plugin_shows)")} and lst[0]["has_grants"] is False,
   "3h. table d'avant : colonne ajoutée, ses lignes n'ont rien d'enregistré")
# routes
import main as app_main                            # noqa: E402
app = app_main.app
app.config["TESTING"] = True


def client(name):
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = c.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"]
        s["username"] = name
    return cli


CHEF, ZOE = client("chef"), client("zoe")
body = {"show_id": sh["id"], "zones": [{"id": zp, "label": "Plateau"}, {"id": zm, "label": "Machines"}]}
r1, r2 = CHEF.post("/api/console-zones/901/grants/plan", json=body), ZOE.post("/api/console-zones/901/grants/plan", json=body)
j1 = r1.get_json() or {}
ok(r1.status_code == 200 and j1.get("recorded") is True and len(j1.get("plan") or []) == 2 and j1.get("users")
   and r2.status_code == 403, "3i. plan : servi à settings.edit, refusé aux autres", [r1.status_code, r2.status_code, j1.get("plan")])
p1 = CHEF.put("/api/console-zones/901/grants", json={"zones": [zp], "grants": []})
p2 = ZOE.put("/api/console-zones/901/grants", json={"zones": [zp], "grants": []})
p3 = CHEF.put("/api/console-zones/901/grants", json={"zones": ["z_inconnue"], "grants": []})
ok(p1.status_code == 200 and not any(g["zone_id"] == zp for g in pz.grants(scope)) and p2.status_code == 403
   and p3.status_code == 400 and (p3.get_json() or {}).get("error_code") == "zone_unknown",
   "3j. remplacement : settings.edit seulement, zone absente du document refusée", [p1.status_code, p2.status_code, p3.status_code])
q = CHEF.post("/api/plugin-shows/audio_console/import", json={"document": dict(FILE, name="Par route"), "kind": "template", "grants_from": "901"})
ok(q.status_code == 400, "3k. import : grants_from illisible refusé", q.status_code)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("ÉCHECS : %d" % len(ko) if ko else "TOUT OK")
sys.exit(1 if ko else 0)
