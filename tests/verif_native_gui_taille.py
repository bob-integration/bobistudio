#!/usr/bin/env python3
"""TAILLE JUSTE et PANNEAU MOBILE de l'interface native d'un plugin LV2 (plugin plugin_gui 0.1.3, 2026-10-18) —
garde hors ligne.

Ce qui est vérifié :
  1. hôte (image/uihost.c, compilé ici avec -DPROBE_ONLY, sans X ni lilv) : la taille d'une interface embarquée est
     le coin le plus lointain de ses fenêtres VISIBLES (une fenêtre d'aide minuscule, non affichée ou décalée ne
     compte pas), bornée à 4096 ; la source est « resize » quand l'interface a demandé exactement cette taille
     (ui:resize), « x11 » sinon ;
  2. service (src/service.py) : la ligne `geom W H SOURCE FIXED` de l'hôte est lue strictement (tout le reste est
     refusé entier), la zone capturée est la taille de l'interface bornée à l'écran virtuel (`clipped` dit qu'elle
     est rognée), l'état publié (/status, /open, HELLO / SIZE du flux) la porte ; une image d'avant `geom` (bare
     `ready` / `size`) reste lue ; un redimensionnement fait par l'interface après `ready` suit jusqu'à la capture ;
  3. panneau (viewer.js, exécuté dans node) : échelle 1:1 quand elle tient, réduction proportionnelle sinon, jamais
     d'agrandissement ; conversion d'un point de l'écran en pixel de l'interface à partir de la boîte DESSINÉE,
     juste à toute échelle ; rectangle du panneau ramené dans la fenêtre ; mémoire du navigateur (position, taille
     par plugin) relue sans confiance, plafonnée, sans planter quand le stockage refuse ;
  4. panneau, de bout en bout avec un DOM simulé : taille prise sur l'interface, coin bas-droit par défaut ;
     interface plus grande que la fenêtre : réduite pour tenir, un clic au milieu du dessin vise le milieu de
     l'interface, puis 1:1 ; barre de titre déplacée à la souris (position mémorisée) ; au doigt seulement après
     un appui long ; poignée de redimensionnement (taille mémorisée pour ce plugin, reprise à la réouverture) ;
     fenêtre qui rétrécit : panneau ramené dedans ; changement de taille de l'interface annoncé par le flux.

plugin_gui est un dépôt à part : sans lui (CI), tout est SAUTÉ et le test le dit (comme tests/verif_plugin_gui.py).
Sans gcc ou sans node, la partie concernée ÉCHOUE : elle ne passe jamais au vert sur ce qu'elle n'a pas vérifié.
Mutations faites le 2026-10-18 : voir docs/chantiers/PROTO_GUI_PLUGINS.md.

    ./venv/bin/python tests/verif_native_gui_taille.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.environ.get("PLUGIN_GUI_DIR") or os.path.join(_R, "plugins", "plugin_gui")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


# ------------------------------------------------------------------ 5. console : l'URI part avec l'ouverture (tourne aussi sans plugin_gui)
AC = os.environ.get("AUDIO_CONSOLE_DIR") or os.path.join(_R, "plugins", "audio_console")
acjs = os.path.join(AC, "control.js")
if not os.path.isfile(acjs):
    print("  --   console absente (%s) : partie 5 SAUTÉE" % AC)
else:
    import re
    js = open(acjs, encoding="utf-8").read()
    call = re.search(r"BobiNativeGui\.open\(\{(.*?)onClosed", js, re.S)
    ok(call is not None and re.search(r"(^|[\s,{])uri\s*[,:]", call.group(1)) is not None,
       "5a. la console passe l'URI du plugin au panneau (taille mémorisée par plugin)", call.group(1) if call else "appel introuvable")
    calls = re.findall(r"ngOpen\(([^)]*)\)", js)
    ok(calls and all(c.replace(" ", "").endswith(",uri") for c in calls),
       "5b. chaque ouverture (directe, « Ouvrir quand même ») porte l'URI", calls)

if not os.path.isfile(os.path.join(GUI, "viewer.js")):
    print("  --   plugin plugin_gui absent (%s) : parties 1-4 SAUTÉES, rien de l'hôte ni du panneau n'est vérifié" % GUI)
    print("\n%d échec(s)" % len(ko) if ko else "\nPartie 5 verte (1-4 sautées).")
    sys.exit(1 if ko else 0)
TMP = tempfile.mkdtemp(prefix="verif_ng_taille_")

# ------------------------------------------------------------------ 1. hôte : sz_extent / sz_source (C)
C_TEST = r'''
#define PROBE_ONLY
#define main uihost_main
#include "uihost.c"
#undef main
#include <stdio.h>
int main(void) {
    int w = -1, h = -1, r;
    SzRect a[] = { {0, 0, 800, 107, 1} };
    r = sz_extent(a, 1, &w, &h);                                   printf("one %d %d %d\n", r, w, h);
    SzRect b[] = { {0, 0, 1, 1, 1}, {0, 0, 590, 170, 1}, {0, 0, 2000, 2000, 0} };
    r = sz_extent(b, 3, &w, &h);                                   printf("helpers %d %d %d\n", r, w, h);
    SzRect c[] = { {10, 20, 300, 200, 1}, {0, 0, 100, 400, 1} };
    r = sz_extent(c, 2, &w, &h);                                   printf("offset %d %d %d\n", r, w, h);
    SzRect d[] = { {-50, -50, 300, 200, 1} };
    r = sz_extent(d, 1, &w, &h);                                   printf("negative %d %d %d\n", r, w, h);
    SzRect e[] = { {0, 0, 9000, 300, 1} };
    r = sz_extent(e, 1, &w, &h);                                   printf("huge %d %d %d\n", r, w, h);
    w = h = -1;
    SzRect f[] = { {0, 0, 800, 400, 0}, {0, 0, 7, 7, 1} };
    r = sz_extent(f, 2, &w, &h);                                   printf("none %d %d %d\n", r, w, h);
    printf("src %s %s %s %s\n", SZ_NAMES[sz_source(800, 418, 800, 418)], SZ_NAMES[sz_source(1000, 500, 800, 418)],
           SZ_NAMES[sz_source(800, 418, 0, 0)], SZ_NAMES[sz_source(800, 419, 800, 418)]);
    return 0;
}
'''
if not shutil.which("gcc"):
    ok(False, "1. gcc présent (la lecture de la taille côté hôte n'est PAS vérifiée)")
else:
    src = os.path.join(TMP, "sz.c")
    open(src, "w").write(C_TEST)
    cc = subprocess.run(["gcc", "-O1", "-Wall", "-I", os.path.join(GUI, "image"), "-o", os.path.join(TMP, "sz"), src, "-lm"],
                        capture_output=True, text=True)
    ok(cc.returncode == 0, "1a. uihost.c (partie taille) compile sans X ni lilv", cc.stderr[-800:])
    if cc.returncode == 0:
        out = dict((l.split(" ", 1)[0], l.split(" ", 1)[1]) for l in
                   subprocess.run([os.path.join(TMP, "sz")], capture_output=True, text=True).stdout.splitlines())
        ok(out.get("one") == "1 800 107", "1b. une fenêtre : sa taille", out.get("one"))
        ok(out.get("helpers") == "1 590 170", "1c. fenêtre d'aide minuscule et fenêtre non affichée : ignorées", out.get("helpers"))
        ok(out.get("offset") == "1 310 400", "1d. fenêtres décalées : coin le plus lointain (x + w, y + h)", out.get("offset"))
        ok(out.get("negative") == "1 300 200", "1e. position négative : comptée depuis 0", out.get("negative"))
        ok(out.get("huge") == "1 4096 300", "1f. bornée à 4096 (même borne que ui:resize)", out.get("huge"))
        ok(out.get("none") == "0 -1 -1", "1g. rien de visible : pas de taille (et rien d'écrit)", out.get("none"))
        ok(out.get("src") == "resize x11 x11 x11", "1h. source : resize seulement si la taille est EXACTEMENT la demandée",
           out.get("src"))

# ------------------------------------------------------------------ 2. service
sys.path.insert(0, os.path.join(GUI, "src"))
import service as sv   # noqa: E402

ok(sv.parse_geom("geom 800 418 resize 0".split()) == (800, 418, "resize", False), "2a. geom lu",
   sv.parse_geom("geom 800 418 resize 0".split()))
ok(sv.parse_geom("geom 367 542 external 1".split()) == (367, 542, "external", True), "2b. fixed = 1 lu")
bad = ["geom 800 418 resize", "geom 800 418 resize 0 x", "geom 0 418 x11 0", "geom 800 5000 x11 0", "geom a 418 x11 0",
       "geom 800 418 magic 0", "geom 800 418 x11 2", "size 800 418 x11 0"]
ok(all(sv.parse_geom(b.split()) is None for b in bad), "2c. ligne hors de ce que l'hôte sait dire : refusée entière",
   [b for b in bad if sv.parse_geom(b.split()) is not None])
ok(sv.capture_size(800, 418) == (800, 418) and sv.capture_size(2500, 1300) == (sv.SCREEN_W, sv.SCREEN_H),
   "2d. zone capturée = taille de l'interface, bornée à l'écran virtuel", (sv.SCREEN_W, sv.SCREEN_H))


class FakeCap:
    def __init__(self):
        self.ui = None

    def set_ui(self, w, h):
        self.ui = (w, h)


rt = sv.Runtime()
rt.cap = FakeCap()
ch = sv.apply_geom(rt, 2500, 300, "x11", False)
z = sv.size_state(rt)
ok(ch and rt.cap.ui == (sv.SCREEN_W, 300) and z == {"w": sv.SCREEN_W, "h": 300, "ui_w": 2500, "ui_h": 300, "source": "x11",
                                                   "fixed": False, "clipped": True},
   "2e. plus grande que l'écran : capturée jusqu'au bord, publiée rognée avec sa vraie taille", (rt.cap.ui, z))
ok(not sv.apply_geom(rt, 2500, 300, "x11", False), "2f. même taille : rien ne change (pas de trame SIZE inutile)")
sv.apply_geom(rt, 800, 418, "resize", False)
ok(sv.size_state(rt)["clipped"] is False and rt.cap.ui == (800, 418), "2g. retour sous l'écran : plus rognée")


class FakeMgr:
    def __init__(self):
        self.ended = []

    def end(self, s, reason):
        self.ended.append(reason)


class S:
    console, uri, slot = "c1", "urn:x", 0


L = sv.Launcher({})
L.mgr = FakeMgr()


def host(lines):
    # the host stays alive after its lines (a real one does): _wait_ready reads byte by byte and stops on an exit
    return subprocess.Popen(["sh", "-c", "printf '%s'; sleep 1" % "".join(l + "\\n" for l in lines)], stdout=subprocess.PIPE,
                            stdin=subprocess.PIPE)


p = host(["geom 320 200 initial 0", "geom 590 170 x11 0", "u 1 urn:a", "ready 590 170"])
geoms, early = [], []
r = L._wait_ready(p, early, geoms)
ok(r == (True, (590, 170)) and geoms[-1:] == [(590, 170, "x11", False)] and early == [["u", "1", "urn:a"]],
   "2h. avant ready : les lignes geom sont gardées (la dernière fait foi), les atomes aussi", (r, geoms, early))
p = host(["ready 640 480"])
geoms = []
r = L._wait_ready(p, [], geoms)
ok(r == (True, (640, 480)) and geoms == [], "2i. image d'avant geom : ready seul suffit toujours", (r, geoms))
# after ready: the interface resizes itself (a tab of another size)
rt = sv.Runtime()
rt.cap = FakeCap()
rt.ui = host(["geom 1000 500 x11 0", "size 1000 500", "geom 9 9 bogus 0", "size 12 12"])
L._ui_stdout(S(), rt)
ok(sv.size_state(rt) == {"w": 1000, "h": 500, "ui_w": 1000, "ui_h": 500, "source": "x11", "fixed": False, "clipped": False}
   and rt.cap.ui == (1000, 500), "2j. après ready : geom suivi jusqu'à la capture ; size ignoré dès que geom est parlé ; geom faux refusé",
   (sv.size_state(rt), rt.cap.ui))
rt = sv.Runtime()
rt.cap = FakeCap()
rt.ui = host(["size 700 300"])
L._ui_stdout(S(), rt)
ok(rt.size == (700, 300) and rt.cap.ui == (700, 300), "2k. image d'avant geom : size suivi comme avant", (rt.size, rt.cap.ui))

# ------------------------------------------------------------------ 3-4. panneau (node)
NODE_TEST = r'''
"use strict";
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const OUT = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// ---- minimal DOM: just what viewer.js reads. Layout: panel border 1, head 60, foot 25, stage padding 8, canvas border 1.
const HEAD = 60, FOOT = 25, PAD = 8;
let removed = 0;
function mkEl(tag) {
  const e = { tagName: tag, children: [], style: {}, attrs: {}, listeners: {}, className: "", textContent: "", hidden: false, parentNode: null,
    classList: { s: new Set(), add(c) { this.s.add(c); }, remove(c) { this.s.delete(c); }, contains(c) { return this.s.has(c); },
                 toggle(c) { this.s.has(c) ? this.s.delete(c) : this.s.add(c); } },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; }, remove() { removed++; },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    fire(t, ev) { ev = Object.assign({ target: this, preventDefault() {}, stopPropagation() {}, button: 0, pointerId: 1 }, ev); (this.listeners[t] || []).forEach((f) => f(ev)); },
    setPointerCapture() {}, focus() {},
    closest(sel) { return sel === "button" && this.tagName === "button" ? this : null; },
    getContext() { return { fillRect() {}, drawImage() {} }; },
  };
  const px = (v) => parseFloat(v) || 0;
  const cls = () => e.className;
  const panelOf = () => { let n = e; while (n && n.className !== "bngui-panel") n = n.parentNode; return n; };
  Object.defineProperty(e, "offsetWidth", { get() {
    if (cls() === "bngui-panel") return px(e.style.width);
    if (cls() === "bngui-canvas") return px(e.style.width) + 2;
    return 0; } });
  Object.defineProperty(e, "offsetHeight", { get() {
    if (cls() === "bngui-panel") return px(e.style.height);
    if (cls() === "bngui-canvas") return px(e.style.height) + 2;
    return 0; } });
  Object.defineProperty(e, "clientWidth", { get() {
    if (cls() === "bngui-body") return Math.max(0, px(panelOf().style.width) - 2);
    if (cls() === "bngui-canvas") return px(e.style.width);
    return 0; } });
  Object.defineProperty(e, "clientHeight", { get() {
    if (cls() === "bngui-body") return Math.max(0, px(panelOf().style.height) - 2 - HEAD - FOOT);
    if (cls() === "bngui-canvas") return px(e.style.height);
    return 0; } });
  e.getBoundingClientRect = () => {
    const p = panelOf();
    if (cls() === "bngui-canvas") {
      const l = px(p.style.left) + 1 + PAD, t = px(p.style.top) + 1 + HEAD + PAD;
      return { left: l, top: t, width: e.offsetWidth, height: e.offsetHeight };
    }
    if (cls() === "bngui-body") return { left: px(p.style.left) + 1, top: px(p.style.top) + 1 + HEAD, width: e.clientWidth, height: e.clientHeight };
    return { left: 0, top: 0, width: 0, height: 0 };
  };
  e.scrollLeft = 0; e.scrollTop = 0;
  return e;
}
const winL = {};
let STORE = {};
const storage = { getItem: (k) => (k in STORE ? STORE[k] : null), setItem: (k, v) => { STORE[k] = String(v); } };
let inputs = [], viewFeed = null;
global.window = { innerWidth: 1280, innerHeight: 800, localStorage: storage,
  addEventListener(t, f) { (winL[t] = winL[t] || []).push(f); }, removeEventListener(t, f) { winL[t] = (winL[t] || []).filter((x) => x !== f); } };
global.document = { body: mkEl("body"), documentElement: { get clientWidth() { return window.innerWidth; }, get clientHeight() { return window.innerHeight; } },
  createElement: mkEl };
global.getComputedStyle = (el) => el.className === "bngui-stage"
  ? { paddingLeft: PAD + "px", paddingRight: PAD + "px", paddingTop: PAD + "px", paddingBottom: PAD + "px", backgroundColor: "black" }
  : { backgroundColor: "black" };
global.performance = global.performance || { now: () => Date.now() };
global.fetch = (url, o) => {
  if (url.indexOf("/input") >= 0) { const dv = new DataView(o.body); for (let i = 0; i < dv.byteLength; i += 6) inputs.push([dv.getUint8(i), dv.getUint8(i + 1), dv.getUint16(i + 2), dv.getUint16(i + 4)]); return Promise.resolve({ status: 204 }); }
  if (url.indexOf("/view") >= 0 && viewFeed) {
    const q = viewFeed; viewFeed = null;
    return Promise.resolve({ ok: true, status: 200, body: { getReader() { return { read() { return q.length ? Promise.resolve({ done: false, value: q.shift() }) : new Promise(() => {}); } }; } } });
  }
  return new Promise(() => {});
};
function frame(t, w, h, obj) {
  const p = Buffer.from(obj ? JSON.stringify(obj) : ""), b = Buffer.alloc(14 + p.length);
  b.writeUInt8(t, 0); b.writeUInt16BE(w, 6); b.writeUInt16BE(h, 8); b.writeUInt32BE(p.length, 10); p.copy(b, 14);
  return new Uint8Array(b);
}
eval(src);
const B = window.BobiNativeGui, G = B.geo;
const panels = () => document.body.children.filter((c) => c.className === "bngui-panel");
const find = (root, c) => { if (root.className === c) return root; for (const k of root.children) { const f = find(k, c); if (f) return f; } return null; };
const rect = (p) => [parseFloat(p.style.left), parseFloat(p.style.top), parseFloat(p.style.width), parseFloat(p.style.height)];
const btnByText = (p, txt) => { const tools = find(p, "bngui-tools"); return tools.children.find((b) => b.textContent === txt); };

// ---- 3. pure geometry
OUT.fit = [G.fitScale(590, 170, 800, 600), G.fitScale(1600, 1000, 800, 1000), G.fitScale(1600, 1000, 1600, 500),
           G.fitScale(100, 100, 5, 5), G.fitScale(0, 0, 800, 600), G.fitScale(300, 200, 3000, 2000)];
OUT.toUi = [G.toUi(150, 70, { left: 100, top: 50, width: 400, height: 125 }, 800, 250),          // 0.5: (50,20)*2
            G.toUi(100 + 299.9, 50, { left: 100, top: 50, width: 300, height: 100 }, 881, 293),   // non-integer ratio
            G.toUi(10, 10, { left: 100, top: 50, width: 300, height: 100 }, 600, 200),            // outside: clamped
            G.toUi(5000, 5000, { left: 100, top: 50, width: 300, height: 100 }, 600, 200),
            G.toUi(100 + 590, 50 + 170, { left: 100, top: 50, width: 590, height: 170 }, 590, 170)];  // 1:1, far edge
OUT.clamp = [G.clampRect({ x: 1200, y: 700, w: 600, h: 400 }, 1000, 700), G.clampRect({ x: -30, y: -30, w: 3000, h: 3000 }, 1000, 700),
             G.clampRect({ x: 10, y: 10, w: 50, h: 40 }, 1000, 700), G.clampRect({ x: NaN, y: undefined, w: 400, h: 300 }, 1000, 700),
             G.clampRect({ x: 100, y: 100, w: 400, h: 300 }, 200, 150)];
OUT.panelFor = [G.panelFor(590, 170, 22, 109, 1280, 800), G.panelFor(1600, 1000, 22, 109, 1280, 800), G.panelFor(100, 50, 22, 109, 1280, 800)];
let s = { getItem: () => null, setItem: (k, v) => { s.v = v; } };
const m = G.load(s); m.pos = { x: 12.4, y: 30 }; G.remember(m, "urn:a", 500.6, 300); G.remember(m, "urn:b", 700, 400); G.remember(m, "urn:a", 510, 310);
OUT.saved = [G.save(s, m), JSON.parse(s.v)];
s.getItem = () => s.v; OUT.reload = G.load(s);
OUT.garbage = [G.load({ getItem: () => "{nope" }), G.load({ getItem: () => JSON.stringify({ x: "1", y: 2, sizes: { u: [1, -2], v: "x", w: [300, 200] } }) }),
               G.load({ getItem: () => { throw new Error("denied"); } })];
OUT.saveDenied = G.save({ setItem: () => { throw new Error("quota"); } }, { pos: null, sizes: {} });
const many = { pos: null, sizes: {} }; for (let i = 0; i < 60; i++) G.remember(many, "u" + i, 300, 200);
G.save({ setItem: (k, v) => { many.json = JSON.parse(v); } }, many);
OUT.capped = Object.keys(many.json.sizes);

(async () => {
  // ---- 4. end to end
  let a = B.open({ base: "/b", sid: "s", w: 590, h: 170, uri: "urn:geq", title: "GEQ" });
  let p = panels()[0];
  OUT.e1 = { rect: rect(p), label: find(p, "bngui-size").textContent };
  a.detach();
  // larger than the window: fit, click in the middle of the drawing -> middle of the interface; then 1:1
  a = B.open({ base: "/b", sid: "s", w: 1600, h: 1000, uri: "urn:big" });
  p = panels()[panels().length - 1];
  const cv = find(p, "bngui-canvas"), r0 = cv.getBoundingClientRect();
  inputs = [];
  cv.fire("pointerdown", { pointerType: "mouse", clientX: r0.left + 1 + (r0.width - 2) / 2, clientY: r0.top + 1 + (r0.height - 2) / 2 });
  // the last drawn pixel, at its centre: the last pixel of the interface (a border counted as picture shifts it)
  cv.fire("pointerup", { pointerType: "mouse", clientX: r0.left + r0.width - 1 - 0.5, clientY: r0.top + r0.height - 1 - 0.5 });
  await sleep(20);
  OUT.e2 = { rect: rect(p), label: find(p, "bngui-size").textContent, click: inputs[0], cw: cv.style.width,
             corner: inputs[1] ? inputs[1].slice(2) : null };
  btnByText(p, "1:1").fire("click", {});
  OUT.e2.label11 = find(p, "bngui-size").textContent;
  a.detach();
  // move with the mouse, remembered
  STORE = {};
  a = B.open({ base: "/b", sid: "s", w: 590, h: 170, uri: "urn:geq" });
  p = panels()[panels().length - 1];
  const head = find(p, "bngui-head"), r1 = rect(p);
  head.fire("pointerdown", { pointerType: "mouse", clientX: r1[0] + 50, clientY: r1[1] + 20 });
  head.fire("pointermove", { pointerType: "mouse", clientX: r1[0] + 50 - 600, clientY: r1[1] + 20 - 470 });
  head.fire("pointerup", { pointerType: "mouse" });
  OUT.e3 = { before: r1, after: rect(p), stored: JSON.parse(STORE["bobi.ngui.panel"] || "null") };
  // a button of the title bar does not move it
  const b11 = btnByText(p, "1:1"), r2 = rect(p);
  head.fire("pointerdown", { pointerType: "mouse", target: b11, clientX: 10, clientY: 10 });
  head.fire("pointermove", { pointerType: "mouse", clientX: 400, clientY: 400 });
  head.fire("pointerup", { pointerType: "mouse" });
  OUT.e3.button = rect(p).join() === r2.join();
  // finger: a slide before the long press does nothing; after it, the panel follows
  head.fire("pointerdown", { pointerType: "touch", pointerId: 7, clientX: 200, clientY: 100 });
  head.fire("pointermove", { pointerType: "touch", pointerId: 7, clientX: 260, clientY: 140 });
  head.fire("pointerup", { pointerType: "touch", pointerId: 7 });
  OUT.e4 = { slide: rect(p).join() === r2.join() };
  head.fire("pointerdown", { pointerType: "touch", pointerId: 8, clientX: 200, clientY: 100 });
  await sleep(450);
  head.fire("pointermove", { pointerType: "touch", pointerId: 8, clientX: 300, clientY: 300 });
  head.fire("pointerup", { pointerType: "touch", pointerId: 8 });
  OUT.e4.long = rect(p);
  OUT.e4.from = r2;
  // grip: resize, remembered for this plugin, taken again at the next opening
  const grip = find(p, "bngui-grip"), r3 = rect(p);
  grip.fire("pointerdown", { pointerType: "touch", pointerId: 9, clientX: 500, clientY: 500 });
  grip.fire("pointermove", { pointerType: "touch", pointerId: 9, clientX: 650, clientY: 600 });
  grip.fire("pointerup", { pointerType: "touch", pointerId: 9 });
  OUT.e5 = { before: r3, after: rect(p), stored: JSON.parse(STORE["bobi.ngui.panel"]) };
  a.detach();
  a = B.open({ base: "/b", sid: "s", w: 590, h: 170, uri: "urn:geq" });
  p = panels()[panels().length - 1];
  OUT.e5.reopen = rect(p);
  // the window shrinks: brought back inside
  window.innerWidth = 500; window.innerHeight = 400;
  (winL.resize || []).forEach((f) => f());
  OUT.e6 = rect(p);
  a.detach();
  window.innerWidth = 1280; window.innerHeight = 800;
  // the interface resizes itself (stream SIZE frame): the panel follows, the label says where the size comes from
  STORE = {};
  viewFeed = [frame(1, 0, 0, { w: 800, h: 418, ui_w: 800, ui_h: 418, source: "resize", fixed: false, clipped: false, uri: "urn:fil4" }),
              frame(4, 1000, 500, { w: 1000, h: 500, ui_w: 1000, ui_h: 500, source: "x11", fixed: false, clipped: false })];
  a = B.open({ base: "/b", sid: "s", w: 800, h: 418, title: "fil4" });
  p = panels()[panels().length - 1];
  await sleep(50);
  OUT.e7 = { rect: rect(p), canvas: [find(p, "bngui-canvas").width, find(p, "bngui-canvas").height], hint: find(p, "bngui-size").title };
  a.close();
  OUT.removed = removed;
  console.log(JSON.stringify(OUT));
  process.exit(0);
})();
'''
if not shutil.which("node"):
    ok(False, "3. node présent (le panneau n'est PAS vérifié)")
else:
    jf = os.path.join(TMP, "t.js")
    open(jf, "w").write(NODE_TEST)
    pr = subprocess.run(["node", jf, os.path.join(GUI, "viewer.js")], capture_output=True, text=True, timeout=60)
    try:
        O = json.loads(pr.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        O = None
    ok(O is not None, "3a. viewer.js s'exécute (géométrie + panneau simulé)", (pr.stdout[-400:], pr.stderr[-1500:]))
    if O:
        f = O["fit"]
        ok(f[0] == 1 and abs(f[1] - 0.5) < 1e-9 and abs(f[2] - 0.5) < 1e-9 and f[3] == 0.1 and f[4] == 1 and f[5] == 1,
           "3b. échelle : 1:1 quand ça tient, réduction proportionnelle (le côté le plus contraint), plancher 0,1, jamais agrandie", f)
        t = O["toUi"]
        ok(t[0] == [100, 40], "3c. clic à l'échelle 0,5 : coordonnées doublées", t[0])
        ok(t[1] == [880, 0], "3d. rapport non entier (881 px dessinés sur 300) : le bord droit vise le dernier pixel", t[1])
        ok(t[2] == [0, 0] and t[3] == [599, 199], "3e. hors du dessin : borné à l'interface", t[2:4])
        ok(t[4] == [589, 169], "3f. 1:1, coin opposé : dernier pixel, jamais au-delà", t[4])
        c = O["clamp"]
        ok(c[0] == {"x": 392, "y": 292, "w": 600, "h": 400}, "3g. hors de la fenêtre : ramené dedans (marge 8)", c[0])
        ok(c[1] == {"x": 8, "y": 8, "w": 984, "h": 684}, "3h. plus grand que la fenêtre : borné, coin haut-gauche dans l'écran", c[1])
        ok(c[2]["w"] == 280 and c[2]["h"] == 180, "3i. plancher 280 x 180", c[2])
        ok(c[3] == {"x": 592, "y": 392, "w": 400, "h": 300}, "3j. position illisible : coin bas-droit", c[3])
        ok(c[4] == {"x": 8, "y": 8, "w": 184, "h": 134}, "3k. fenêtre plus petite que le plancher : tient quand même", c[4])
        ok(O["panelFor"] == [{"w": 612, "h": 279}, {"w": 1264, "h": 784}, {"w": 280, "h": 180}],   # cadre 22 x 109 donné
           "3l. taille du panneau = interface 1:1 + cadre, dans la fenêtre", O["panelFor"])
        sv_ok, sj = O["saved"]
        ok(sv_ok and sj["x"] == 12 and sj["y"] == 30 and list(sj["sizes"]) == ["urn:b", "urn:a"] and sj["sizes"]["urn:a"] == [510, 310],
           "3m. mémoire : position arrondie, taille par plugin, la plus récente en dernier", sj)
        ok(O["reload"] == {"pos": {"x": 12, "y": 30}, "sizes": {"urn:b": [700, 400], "urn:a": [510, 310]}}, "3n. relue telle quelle", O["reload"])
        g = O["garbage"]
        ok(g[0] == {"pos": None, "sizes": {}} and g[1] == {"pos": None, "sizes": {"w": [300, 200]}} and g[2] == {"pos": None, "sizes": {}},
           "3o. mémoire illisible, valeurs fausses, stockage refusé : oubliées, jamais crues", g)
        ok(O["saveDenied"] is False, "3p. stockage plein ou refusé : save rend false, sans exception")
        ok(len(O["capped"]) == 40 and O["capped"][0] == "u20" and O["capped"][-1] == "u59", "3q. 40 tailles au plus, les plus récentes",
           (len(O["capped"]), O["capped"][:1], O["capped"][-1:]))
        e1 = O["e1"]
        # cadre simulé : bordures 2 + titre 60 + pied 25 ; marge de scène 16 + bord du canevas 2 + 2 de garde
        ok(e1["rect"] == [660, 515, 612, 277] and e1["label"].startswith("590 × 170 · 100 %"),
           "4a. panneau à la taille de l'interface (1:1 + cadre), coin bas-droit", e1)
        e2 = O["e2"]
        ok(e2["rect"] == [8, 8, 1264, 784] and e2["label"].startswith("1600 × 1000 · 68 %"),   # (784-87-18)/1000
           "4b. plus grande que la fenêtre : panneau borné, image réduite pour tenir", e2)
        ok(e2["click"] is not None and e2["click"][2:] == [800, 500],
           "4c. clic au milieu du dessin réduit : milieu de l'interface", e2["click"])
        ok(O["e2"].get("corner") == [1599, 999], "4c'. dernier pixel dessiné (réduit, bord du canevas exclu) : dernier pixel de l'interface",
           O["e2"].get("corner"))
        ok(e2["label11"].startswith("1600 × 1000 · 100 %"), "4d. bouton 1:1 : taille réelle (défilement)", e2["label11"])
        e3 = O["e3"]
        ok(e3["after"][:2] == [60, 45] and e3["after"][2:] == e3["before"][2:] and e3["stored"] and e3["stored"]["x"] == 60
           and e3["stored"]["y"] == 45, "4e. barre de titre à la souris : déplacé, position mémorisée", e3)
        ok(e3["button"], "4f. un bouton de la barre ne déplace pas le panneau")
        e4 = O["e4"]
        ok(e4["slide"], "4g. au doigt, glisser AVANT l'appui long : rien ne bouge")
        ok(e4["long"][:2] == [e4["from"][0] + 100, e4["from"][1] + 200], "4h. au doigt après l'appui long : le panneau suit", e4)
        e5 = O["e5"]
        ok(e5["after"][2:] == [e5["before"][2] + 150, e5["before"][3] + 100] and e5["stored"]["sizes"].get("urn:geq") == e5["after"][2:],
           "4i. poignée : redimensionné, taille mémorisée pour ce plugin", e5)
        ok(e5["reopen"][2:] == e5["after"][2:], "4j. réouverture du même plugin : la taille choisie revient", e5["reopen"])
        e6 = O["e6"]
        ok(e6[0] >= 8 and e6[1] >= 8 and e6[0] + e6[2] <= 492 and e6[1] + e6[3] <= 392, "4k. fenêtre rétrécie : panneau ramené dedans", e6)
        e7 = O["e7"]
        ok(e7["canvas"] == [1000, 500] and e7["rect"][2:] == [1022, 607] and "x11" in e7["hint"].lower(),
           "4l. l'interface change de taille (trame SIZE) : le panneau suit, la source est dite", e7)
        ok(O["removed"] == 5, "4m. fermer / détacher retire le panneau (5 ouvertures, 5 retraits)", O["removed"])

if os.path.isdir(GUI):
    _vj = open(os.path.join(GUI, "viewer.js"), encoding="utf-8").read()
    _vc = open(os.path.join(GUI, "viewer.css"), encoding="utf-8").read()
    ok('body.classList.toggle("bngui-body--fit", st.mode === "fit")' in _vj and ".bngui-body--fit { overflow: hidden; }" in _vc,
       "4n. « Ajuster » sans barre de défilement : le cadrage et la barre ne se poursuivent plus (image qui clignotait 95 % ↔ 100 %)")

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d échec(s)" % len(ko) if ko else "\nTout est vert.")
sys.exit(1 if ko else 0)
