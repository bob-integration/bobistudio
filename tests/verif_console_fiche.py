#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Strip panel (« fiche ») of the audio console: filter and 4-band EQ are TWO tools, and the side fiche
is resizable within bounds (plugins/audio_console/control.js, operator's decision 2026-10-17). Offline,
node with a tiny DOM stub, no orchestrator, no container.

    ./venv/bin/python tests/verif_console_fiche.py

Covers (each one a SILENT failure if it broke — the panel would just look wrong):
  a. toolSections: a strip with a filter AND an EQ gives two distinct sections (« flt », « eq »), EQ alone
     one EQ section, filter alone one filter section, neither nothing (no empty section), two EQs numbered;
  b. ficheEq / ficheFilter rendered with stubs: the EQ curve carries handles 1-4 and NOT the filter (its
     response is the EQ's alone), the filter curve one handle and the filter's response alone; each tool
     has its own chip (filter → « flt », EQ → « eq ») and its own memories / copy / paste row;
  c. ficheWidth: never under the default width, never over 60 % of the view, null = default;
  d. wiring: renderFiche walks toolSections (one section per tool), the popup opens the touched tool,
     the Standard / Overview layouts carry separate « hpf » and « eq » rows, the EQ row no longer offers
     filter elements, the filter chip of a strip opens the FILTER; the CSS bounds the side width too.
MUTATED once per family (see the commit message): kinds merged in toolSections (a. fails), width not capped (c.),
the filter drawn on the EQ curve (b.), the filter chip opening the EQ (b.).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJS = os.path.join(ROOT, "plugins", "audio_console", "control.js")
CSS = os.path.join(ROOT, "plugins", "audio_console", "control.css")
FAILS = []


def ok(cond, name, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ("" if cond else "  " + str(detail)[:600]))
    if not cond:
        FAILS.append(name)


JS = r"""const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const cut = (a, b) => { const i = src.indexOf(a), j = src.indexOf(b, i); if (i < 0 || j < 0) throw new Error("marker: " + a); return src.slice(i, j); };
const fiche = cut("  // ═══ FICHE: TOOL SECTIONS", "  // Third-party plugin: state");
const curves = cut("  const FS_UI = 48000;", "  // Static transfer curve");
const layouts = cut("  const BUILTIN_LAYOUTS = [", "  const ROW_LABEL");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this.dataset = {}; this.textContent = ""; this.attrs = {}; this.isConnected = true; this.classList = { add() {}, remove() {} }; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  set innerHTML(v) { this._html = v; if (v === "") this.children = []; }
  get innerHTML() { return this._html || ""; }
}
global.document = { createElement: (t) => new El(t) };
const env = `
  let DOC = null, PUBLIC = true, CURDIS = false;
  const SYNC = [], FICHE = { band: {} }, LOG = { chips: [], tm: [], params: [] };
  const T = (k, fb) => fb, esc = (s) => String(s), syncAll = () => {};
  const getAt = (d, p) => p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), d);
  function div(cls, parent, html) { const d = new El("div"); d.className = cls || ""; if (html != null) d.innerHTML = html; if (parent) parent.appendChild(d); return d; }
  const chipOf = (host, it, base, m, sec) => LOG.chips.push(m.type + ":" + sec);
  const tmBar = (host, it, base, el) => { if (el) LOG.tm.push(el.id); };
  const paramControl = (host, path) => LOG.params.push(path);
  const bindParam = (kind, host, opt, path) => LOG.params.push(path);
  const MODS = { filter: { params: [{ k: "mode" }, { k: "freq" }, { k: "slope" }] },
    eq4: { params: [{ k: "enabled" }, { k: "type", options: ["peak", "lowshelf", "highshelf"] }, { k: "freq" }, { k: "gain" }, { k: "q", min: 0.1, max: 18 }] } };
  ${layouts}
  ${curves}
  ${fiche}
  return { toolSections, ficheWidth, ficheEq, ficheFilter, eqSvg, BUILTIN_LAYOUTS, ROW_KINDS, LOG, setDoc: (d) => { DOC = d; } };
`;
const api = new Function("El", env)(El);
const out = {};
const mods = (...ts) => ts.map((t, i) => ({ id: "m" + i + t, kind: "module", type: t,
  params: t === "filter" ? { mode: "hpf", freq: 120, slope: "24", bypass: false }
    : { bypass: false, b1: { enabled: true, type: "peak", freq: 3000, gain: 6, q: 1 }, b2: { enabled: false }, b3: { enabled: false }, b4: { enabled: false } } }));
const kinds = (ch) => api.toolSections({ chain: ch }).map((x) => x.k + (x.n ? x.n : ""));
out.both = kinds([{ id: "e_trim", kind: "element", type: "trim" }].concat(mods("filter", "eq4")));
out.eq_only = kinds(mods("eq4"));
out.flt_only = kinds(mods("filter"));
out.none = kinds([{ id: "e_pan", kind: "element", type: "pan" }].concat(mods("comp")));
out.two_eq = kinds(mods("eq4", "filter", "eq4"));
// b. render both tools with the stubs
const ch = mods("filter", "eq4"), strip = { id: "s1", chain: ch };
api.setDoc({ strips: { s1: { chain: { [ch[0].id]: ch[0], [ch[1].id]: ch[1] } } } });
const it = { o: strip, t: "strip" }, base = "strips.s1";
const sf = new El("div"), se = new El("div");
api.ficheFilter(sf, it, base, ch[0]);
api.ficheEq(se, it, base, ch[1]);
const svgOf = (s) => s.children.find((c) => c.className === "ac-eq-box").innerHTML;
const hs = (svg) => (svg.match(/data-b="([^"]+)"/g) || []).map((x) => x.slice(8, -1));
out.eq_handles = hs(svgOf(se));
out.flt_handles = hs(svgOf(sf));
// the curves are the tool's OWN response: equal to eqSvg of that module alone, different from the combined one
const eqAlone = api.eqSvg(ch[1], null, 360, 150, {}), both = api.eqSvg(ch[1], ch[0], 360, 150, {}), fltAlone = api.eqSvg(null, ch[0], 360, 150, {});
const line = (svg) => (svg.match(/class="ac-eq-line" d="([^"]+)"/) || [])[1];
out.eq_curve_alone = line(svgOf(se)) === line(eqAlone) && line(eqAlone) !== line(both);
out.flt_curve_alone = line(svgOf(sf)) === line(fltAlone) && line(fltAlone) !== line(both);
out.chips = api.LOG.chips; out.tm = api.LOG.tm;
out.flt_params = api.LOG.params.filter((p) => p.indexOf(ch[0].id) >= 0);
out.eq_params_touch_filter = api.LOG.params.some((p) => p.indexOf(ch[1].id) < 0 && p.indexOf(ch[0].id) < 0);
// c. width
out.w = [api.ficheWidth(100, 2000, 400), api.ficheWidth(900, 2000, 400), api.ficheWidth(1900, 2000, 400),
  api.ficheWidth(900, 500, 400), api.ficheWidth(null, 2000, 400), api.ficheWidth(0, 2000, 400)];
// d. layouts
const rowsOf = (id) => api.BUILTIN_LAYOUTS.find((l) => l.id === id).rows;
out.std = rowsOf("std").filter((r) => r.k === "hpf" || r.k === "eq");
out.overview = rowsOf("overview").filter((r) => r.k === "hpf" || r.k === "eq");
out.eq_items = api.ROW_KINDS.eq.items.map((x) => x[0]);
out.hpf_items = (api.ROW_KINDS.hpf.items || []).map((x) => x[0]);
console.log(JSON.stringify(out));
"""

node = shutil.which("node")
if not (node and os.path.isfile(CJS)):
    print("  (node or the console plugin absent: not checked)")
    sys.exit(0)
tmp = tempfile.mkdtemp(prefix="bobi_fiche_")
try:
    jsf = os.path.join(tmp, "fiche.js")
    open(jsf, "w", encoding="utf-8").write(JS)
    res = subprocess.run([node, jsf, CJS], capture_output=True, text=True, timeout=30)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
ok(res.returncode == 0, "node ran the extracted blocks", res.stderr)
r = json.loads(res.stdout.strip().splitlines()[-1]) if res.returncode == 0 else {}

# a.
ok(r.get("both") == ["flt", "eq"], "a. filter + EQ: two distinct sections, chain order", r.get("both"))
ok(r.get("eq_only") == ["eq"], "a. EQ alone: one EQ section", r.get("eq_only"))
ok(r.get("flt_only") == ["flt"], "a. filter alone: one filter section", r.get("flt_only"))
ok(r.get("none") == [], "a. neither: no section at all", r.get("none"))
ok(r.get("two_eq") == ["eq1", "flt", "eq2"], "a. two EQs: numbered, the filter apart", r.get("two_eq"))
# b.
ok(r.get("eq_handles") == ["b1", "b2", "b3", "b4"], "b. EQ curve: handles 1-4 only (no filter handle)", r.get("eq_handles"))
ok(r.get("flt_handles") == ["f"], "b. filter curve: one handle", r.get("flt_handles"))
ok(r.get("eq_curve_alone") is True, "b. EQ curve = the EQ's response alone (the filter is not in it)")
ok(r.get("flt_curve_alone") is True, "b. filter curve = the filter's response alone")
ok(r.get("chips") == ["filter:flt", "eq4:eq"], "b. own chip per tool (filter opens « flt », EQ « eq »)", r.get("chips"))
ok(r.get("tm") == ["m0filter", "m1eq4"], "b. own memories / copy / paste row per tool, once each", r.get("tm"))
ok(sorted(p.rsplit(".", 1)[1] for p in r.get("flt_params", [])) == ["freq", "mode", "slope"],
   "b. filter controls: mode, frequency, slope", r.get("flt_params"))
ok(r.get("eq_params_touch_filter") is False, "b. no control bound outside the two modules")
# c.
ok(r.get("w") == [400, 900, 1200, 400, None, None], "c. width bounded: min = default, max = 60 % of the view, null = default", r.get("w"))
# d. layouts
std, ov = r.get("std") or [], r.get("overview") or []
ok([x["k"] for x in std] == ["hpf", "eq"] and [i["el"] for i in std[0].get("items", [])] == ["hpf.on", "hpf.freq"]
   and [i["el"] for i in std[1]["items"]] == ["on"], "d. Standard: separate rows, filter chip + knob, EQ chip", std)
ok([x["k"] for x in ov] == ["hpf", "eq"] and [i["el"] for i in ov[0]["items"]] == ["hpf.curve"]
   and [i["el"] for i in ov[1]["items"]] == ["curve"], "d. Overview: filter mini-curve row, EQ mini-curve row", ov)
ok(not any(e.startswith("hpf.") for e in r.get("eq_items", [])) and "hpf.on" in r.get("hpf_items", []),
   "d. the EQ row no longer offers filter elements; the filter row does", (r.get("eq_items"), r.get("hpf_items")))

src = open(CJS, encoding="utf-8").read()
css = open(CSS, encoding="utf-8").read()


def body_of(name):
    i = src.index("  function " + name + "(")
    j = src.index("\n  }\n", i)
    return src[i:j]


rf, tb, ic = body_of("renderFiche"), body_of("toolBuild"), body_of("itemCell")
ok("toolSections(o)" in rf and "ficheFilter(sec" in rf and "ficheEq(sec" in rf and "eqPairs" not in src,
   "d. renderFiche: one section per tool (no more filter + EQ pairs)")
ok('secs.push(["flt"' in rf and 'secs.push(["eq"' in rf, "d. fiche tabs: FILTRE and EQ apart")
ok("ficheFilter(body" in tb and "ficheEq(body" in tb and "!tsec) { TOOL.cur = null; return; }" in tb,
   "d. popup: opens the touched tool, never an empty popup")
ok('a === "hpf" ? "flt"' in ic and 'miniCurve(host, it, f, "flt")' in ic and 'miniCurve(host, it, m, "eq")' in ic,
   "d. strip cells: the filter chip / curve opens the filter, the EQ ones the EQ")
ok('case "hpf": itemCell(c, it, base, r)' in src, "d. the hpf row renders its elements")
side = re.search(r"\.ac-cw-side \{[^}]*\}", css)
ok(side and "var(--ac-fiche-w" in side.group(0) and "60%" in side.group(0) and "min-width: min(25rem" in side.group(0),
   "d. CSS bounds the side width too (25rem … 60 %)", side and side.group(0))
grip = re.search(r"\.ac-cw-grip \{[^}]*\}", css)
ok(grip and "width: 44px" in grip.group(0) and "touch-action: none" in grip.group(0), "d. grip: 44 px finger target, no scroll on drag")
ok(re.search(r"repeat\(auto-fill, minmax\(min\(22rem, 100%\), 1fr\)\)", css) is not None, "d. wide fiche: sections in columns")

print("FAIL" if FAILS else "OK", len(FAILS))
sys.exit(1 if FAILS else 0)
