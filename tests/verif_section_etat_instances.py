#!/usr/bin/env python3
"""Rubrique plugin — l'état des instances se tient à jour (2026-10-18).

L'exploitant : une console qu'il venait de créer affichait « script arrêté » dans l'en-tête alors qu'elle tournait.
La liste des instances n'était lue qu'à l'ouverture de la rubrique et après un déploiement : l'état saisi pendant le
démarrage restait affiché pour toujours. On exécute le vrai tpRefreshStatuses de templates/plugin_section.html sous
node :
  a. un état qui change met à jour le point et le libellé de l'instance, et l'en-tête (tpRenderIdent) ;
  b. aucun changement : rien n'est redessiné (la console montée n'est jamais touchée) ;
  c. une instance apparue ou disparue : la liste est relue (tpLoadInstances) ;
  d. le relevé est ordonnancé par MXLPoll (sans recouvrement) toutes les 5 s.
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
const i = src.indexOf("async function tpRefreshStatuses()"), j = src.indexOf("document.addEventListener('DOMContentLoaded', () => { if(window.MXLPoll)", i);
if (i < 0 || j < 0) throw new Error("tpRefreshStatuses not found");
const code = src.slice(i, j);
const run = async (items0, served) => {
  const R = { render: 0, reload: 0 };
  const btns = {};
  items0.forEach((x) => { btns[x.vmid] = { dot: { className: "tp-dot warn" }, lab: { textContent: x.status } }; });
  const f = new Function("R", "btns", "items0", "served", `
    let TP_TYPE = "audio_console", tpItems = items0;
    const document = { hidden: false, querySelector: (q) => { const m = /data-vmid="(\\d+)"/.exec(q); const b = m && btns[m[1]];
      return b ? { querySelector: (s) => s === ".tp-dot" ? b.dot : b.lab } : null; } };
    const fetch = async () => ({ ok: true, json: async () => served });
    const tpStatusClass = (s) => s === "running" ? "" : "warn", tpStatusLabel = (s) => "L:" + s;
    const tpRenderIdent = () => { R.render++; }, tpLoadInstances = async () => { R.reload++; };
    ${code}
    return tpRefreshStatuses;`);
  await f(R, btns, items0, served)();
  R.btns = btns; R.items = items0;
  return R;
};
(async () => {
  const out = {};
  out.a = await run([{ vmid: 1178, status: "script_stopped" }], [{ vmid: 1178, status: "running" }]);
  out.b = await run([{ vmid: 1178, status: "running" }], [{ vmid: 1178, status: "running" }]);
  out.c = await run([{ vmid: 1178, status: "running" }], [{ vmid: 1178, status: "running" }, { vmid: 1179, status: "running" }]);
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
        a = R["a"]
        ok(a["render"] == 1 and a["btns"]["1178"]["lab"]["textContent"] == "L:running" and a["btns"]["1178"]["dot"]["className"] == "tp-dot "
           and a["items"][0]["status"] == "running", "a. « script arrêté » → « en marche » : point, libellé et en-tête suivent", a)
        ok(R["b"]["render"] == 0 and R["b"]["reload"] == 0, "b. rien n'a changé : rien n'est redessiné", R["b"])
        ok(R["c"]["reload"] == 1, "c. une instance apparue : la liste est relue", R["c"])
tpl = open(TPL, encoding="utf-8").read()
ok("window.MXLPoll(tpRefreshStatuses, TP_STATUS_MS" in tpl and "const TP_STATUS_MS = 5000;" in tpl, "d. relevé ordonnancé par MXLPoll toutes les 5 s")
print("\nÉCHECS : %d" % len(ko) if ko else "\nOK")
sys.exit(1 if ko else 0)
