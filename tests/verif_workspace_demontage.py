#!/usr/bin/env python3
"""Workspace — interfaces complètes de plugin : démontage et un type par vue (2026-10-23).

Même défaut que la rubrique plugin (cf. verif_section_demontage.py, noms des boutons du mélangeur qui
clignotaient chez Paul) : unmountAll démontait `window.MXLPlugins[type]`, le DERNIER script chargé,
pas l'objet monté. Et deux panneaux du même plugin dans une vue partageaient l'état de module de
l'interface (conteneur, élément, sondage, objet global des boutons) : un clic dans le premier
commandait le second. On exécute le vrai mountPlugin / unmountAll de templates/workspace.html :
  a. l'objet enregistré a été remplacé : unmountAll démonte quand même celui qui est monté ;
  b. deux panneaux du même type dans un rendu : un seul est monté, l'autre dit pourquoi ;
  c. une interface qui déclare `multiMount` est montée dans les deux ;
  d. un rendu plus récent pendant le chargement : le montage dépassé abandonne.
"""
import json, os, shutil, subprocess, sys, tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(_R, "templates/workspace.html")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


NODE = r"""
const fs = require("fs"); const src = fs.readFileSync(process.argv[2], "utf8");
const cut = (a, b) => { const i = src.indexOf(a), j = src.indexOf(b, i); if (i < 0 || j < 0) throw new Error("not found: " + a); return src.slice(i, j); };
const code = cut("const mountedApis", "const LS_KEY")
  + cut("async function mountPlugin(w, bodyEl)", "// ── Gestionnaire de médias COMPLET")
  + cut("function unmountAll()", "function widgetTitle");
const mkApi = (name, live, multi) => ({ name, multiMount: !!multi,
  mount(el, vmid) { live[name + "@" + vmid] = true; },
  unmount() { Object.keys(live).filter(k => k.startsWith(name + "@")).forEach(k => delete live[k]); } });
const build = (live, insts, opt) => new Function("live", "insts", "opt", "mkApi", `
  const window = { MXLPlugins: {}, t: (k) => k };
  const T = (k) => window.t(k), esc = (x) => String(x), toast = () => {};
  const document = { getElementById: () => null };
  const MOUNTED = new Map(); let monitorHb = null;
  const resolveInst = (w) => insts[w.vmid];
  const fetch = async () => ({ text: async () => "<div></div>" });
  const ensureExtra = async () => {};
  // Charger le script d'une version = exécuter SA copie, qui s'enregistre en dernier.
  const ensureAsset = (type, kind, vmid, version) => new Promise((res) => setTimeout(() => {
    if (kind === "js") window.MXLPlugins[type] = mkApi(type + "-" + version, live, opt.multi); res(); }, (opt.delay || {})[vmid] || 0));
  ${code}
  return { mountPlugin, unmountAll, window };`)(live, insts, opt, mkApi);
const el = () => ({ innerHTML: "" });
const INSTS = { 1223: { vmid: 1223, type: "mixer", plugin_version: "0.23.2" }, 1250: { vmid: 1250, type: "mixer", plugin_version: "0.23.3" } };
(async () => {
  const out = {};
  { const live = {}, W = build(live, INSTS, {});
    await W.mountPlugin({ id: "w1", vmid: 1223 }, el());
    W.window.MXLPlugins.mixer = mkApi("current", live);
    W.unmountAll(); out.a = live; }
  { const live = {}, W = build(live, INSTS, {}), b2 = el();
    await Promise.all([W.mountPlugin({ id: "w1", vmid: 1223 }, el()), W.mountPlugin({ id: "w2", vmid: 1250 }, b2)]);
    out.b = { live, msg: b2.innerHTML }; }
  { const live = {}, W = build(live, INSTS, { multi: true });
    await Promise.all([W.mountPlugin({ id: "w1", vmid: 1223 }, el()), W.mountPlugin({ id: "w2", vmid: 1250 }, el())]);
    out.c = live; }
  { const live = {}, W = build(live, INSTS, { delay: { 1223: 30 } });
    const p = W.mountPlugin({ id: "w1", vmid: 1223 }, el());
    W.unmountAll(); await p; out.d = live; }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""

node = shutil.which("node")
ok(bool(node), "node présent")
if node:
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "t.js")
        open(f, "w").write(NODE)
        res = subprocess.run([node, f, TPL], capture_output=True, text=True, timeout=60)
    ok(res.returncode == 0, "exécution sous node", res.stderr[-600:])
    R = json.loads(res.stdout.strip().splitlines()[-1]) if res.returncode == 0 else {}
    if R:
        ok(R["a"] == {}, "a. objet enregistré remplacé : l'interface montée est quand même démontée", R["a"])
        ok(len(R["b"]["live"]) == 1 and "projects.ws_ui_once" not in R["b"]["msg"] and "une seule" in R["b"]["msg"],
           "b. deux mélangeurs dans une vue : un seul monté, le second panneau le dit", R["b"])
        ok(len(R["c"]) == 2, "c. `multiMount` déclaré : les deux sont montés", R["c"])
        ok(R["d"] == {}, "d. rendu plus récent pendant le chargement : le montage dépassé abandonne", R["d"])
print("\nÉCHECS : %d" % len(ko) if ko else "\nOK")
sys.exit(1 if ko else 0)
