#!/usr/bin/env python3
"""Rubrique plugin — on démonte l'UI MONTÉE, pas celle qui est enregistrée (2026-10-23).

Paul (Recette) : sur le mélangeur, les noms des boutons de bus ne correspondaient pas aux sources et
clignotaient, « comme un conflit avec les sources du mixer RL-mix ». rl-mix (0.23.2) et mixer-Satis
(0.23.3) n'ont pas la même version : chacune charge sa propre copie du script, et
`window.MXLPlugins.mixer` ne désigne que la DERNIÈRE chargée. tpMount démontait celle-là ; l'UI
réellement montée continuait son sondage de 2 s et réécrivait les boutons de l'autre instance.
On exécute le vrai tpMount de templates/plugin_section.html sous node :
  a. l'objet enregistré a été remplacé entre-temps (relecture de la liste) : l'UI montée est
     quand même démontée avant d'en monter une autre ;
  b. deux montages qui se chevauchent (le second clic arrive pendant le chargement du premier) :
     seul le second est monté, et rien du premier ne reste vivant.
"""
import json, os, shutil, subprocess, sys, tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(_R, "templates/plugin_section.html")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


NODE = r"""
const fs = require("fs"); const src = fs.readFileSync(process.argv[2], "utf8");
const i = src.indexOf("let tpMountedApi"), j = src.indexOf("// Sélectionne (et MONTE)", i);
if (i < 0 || j < 0) throw new Error("tpMount block not found");
const code = src.slice(i, j);
// Une copie de script = un objet qui compte ses montages vivants (son « sondage »).
const mkApi = (name, live) => ({ name,
  mount(el, vmid) { live[name] = vmid; },
  unmount() { delete live[name]; } });
const build = (live, delays) => new Function("live", "delays", "mkApi", `
  const window = { MXLPlugins: {}, t: (k) => k };
  let TP_TYPE = "mixer";
  const document = { querySelectorAll: () => [] };
  const tpToast = () => {};
  // Charger l'UI d'une version = exécuter SA copie du script, qui s'enregistre en dernier.
  const tpLoadUi = (type, vmid, version) => new Promise((res) => setTimeout(() => {
    window.MXLPlugins.mixer = mkApi(version, live); res("<div></div>"); }, delays[vmid] || 0));
  ${code}
  return { tpMount, window };`)(live, delays, mkApi);
(async () => {
  const out = {};
  { // a.
    const live = {}, T = build(live, {});
    const el = { classList: { remove() {} }, innerHTML: "" };
    await T.tpMount({ vmid: 1223, version: "0.23.2" }, el);
    T.window.MXLPlugins.mixer = mkApi("current", live);   // relecture de la liste : version courante
    await T.tpMount({ vmid: 1250, version: "0.23.3" }, el);
    out.a = live;
  }
  { // b.
    const live = {}, T = build(live, { 1223: 50, 1250: 5 });
    const el = { classList: { remove() {} }, innerHTML: "" };
    const p1 = T.tpMount({ vmid: 1223, version: "0.23.2" }, el);
    const p2 = T.tpMount({ vmid: 1250, version: "0.23.3" }, el);
    await Promise.all([p1, p2]);
    out.b = live;
  }
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
        ok(R["a"] == {"0.23.3": 1250}, "a. objet enregistré remplacé : l'UI de rl-mix est démontée, seule mixer-Satis vit", R["a"])
        ok(R["b"] == {"0.23.3": 1250}, "b. montages qui se chevauchent : seul le dernier est monté", R["b"])
print("\nÉCHECS : %d" % len(ko) if ko else "\nOK")
sys.exit(1 if ko else 0)
