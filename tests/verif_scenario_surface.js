// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Scénario des macros (templates/workspace.html) sur la SURFACE commune des éditeurs — confié le
// 30/10 (chantier « un seul moteur d'édition ») : sélection (Maj OU Ctrl/⌘, référence = dernier
// cliqué), glisser d'un nœud ou de toute la sélection (seuil de 6 px d'écran), rectangle de
// sélection (Maj + glisser dans le vide), appui long au doigt. Restent au Scénario : monde sans
// bord, panoramique, liens, arêtes. SCN.msel reste la sélection (lue par scnPeers / BobiEditor.peers).
// VRAIS gestionnaires de pointeur de workspace.html, VRAIE base ; DOM et rendu en doublures.
//
//   $ node tests/verif_scenario_surface.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const R = path.join(__dirname, '..');
const WS = process.env.VERIF_WS || path.join(R, 'templates', 'workspace.html');
const src = fs.readFileSync(WS, 'utf8');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
function entre(debut, fin) {
    const i = src.indexOf(debut), j = src.indexOf(fin, i);
    if (i < 0 || j < 0) { console.log('  ÉCHEC introuvable : ' + debut); process.exit(1); }
    return src.slice(i, j);
}
function fonction(nom) {
    const i = src.indexOf('function ' + nom + '(');
    const re = /\n    \}\n/g; re.lastIndex = i;
    return src.slice(i, re.exec(src).index + 6);
}

controle('★★★ le Scénario passe par BobiEditor.surface (plus de glisser ni de rectangle maison)',
         /BobiEditor\.surface\(/.test(src) && !/mode: 'node', n, group/.test(src) && !/mode: 'rect', x0: p\.x/.test(src));

const handlers = {};
const code = [
    fonction('scnEvtWorld'),
    entre('    let scnSurf = null;', "    scnWrap.addEventListener('pointerdown'"),
    entre("    scnWrap.addEventListener('pointerdown'", '    // Clavier : cf. « Édition au clavier »'),
    'this.S = () => scnSurf; this.drag = () => scnDrag;',
].join('\n');
const els = {};
const mkEl = () => ({ style: {}, classList: { add() {}, remove() {}, contains: () => false }, remove() { this.removed = true; } });
const ctx = {
    Math, JSON, Object, Array, Set, setTimeout: (f, ms) => { ctx.timers.push(f); return ctx.timers.length; }, clearTimeout: () => {},
    timers: [], navigator: { platform: 'Linux' }, dirty: 0,
    document: { createElement: () => mkEl(), createElementNS: () => mkEl(), elementFromPoint: () => null, addEventListener() {} },
    scnWrap: { addEventListener: (t, f) => { handlers[t] = f; }, getBoundingClientRect: () => ({ left: 0, top: 0 }),
               setPointerCapture() {}, classList: { add() {}, remove() {} }, clientWidth: 800, clientHeight: 600 },
    scnWorld: { appendChild(el) { ctx.rectEl = el; }, querySelectorAll: () => [] },
    scnSvg: { appendChild() {} },
    scnDirty: () => { ctx.dirty++; }, scnDrawEdges() {}, scnPaintSel() {}, scnRenderInsp() {}, scnApplyView() {},
    scnEdToolsState() {}, scnSelect(sel) { ctx.SCN.sel = sel; ctx.SCN.msel = new Set(sel && sel.t === 'node' ? [sel.id] : []); },
    scnNodeById: id => ctx.SCN.nodes.find(n => n.id === id),
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(R, 'static', 'js', 'editor.js'), 'utf8'), ctx);
let err = null;
try { ctx.SCN = null; vm.runInContext(code, ctx); }
catch (e) { err = String(e); }
controle('le code du Scénario se charge', !err, err);
if (err) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }

function scn(nodes, sel, view) {
    const S = { nodes, edges: [], els: {}, view: view || { x: 0, y: 0, s: 1 }, msel: new Set(sel || []), sel: null };
    nodes.forEach(n => { S.els[n.id] = Object.assign(mkEl(), { offsetWidth: 200, offsetHeight: 60 }); });
    if (sel && sel.length) S.sel = { t: 'node', id: sel[sel.length - 1] };
    ctx.SCN = S; ctx.dirty = 0; ctx.timers = [];
    return S;
}
const tgt = (zone, id) => ({ classList: { contains: () => false },
    closest: q => (q === '.scn-node .hd' && zone === 'hd') || (q === '.scn-node' && (zone === 'hd' || zone === 'nd'))
                  ? { dataset: { id } } : null });
const ev = (x, y, zone, id, mods) => Object.assign({ clientX: x, clientY: y, pointerId: 1, preventDefault() {},
                                                     target: tgt(zone, id) }, mods || {});
const down = (...a) => handlers.pointerdown(ev(...a));
const move = (x, y, mods) => handlers.pointermove(Object.assign({ clientX: x, clientY: y }, mods || {}));
const up = (x, y) => handlers.pointerup({ clientX: x || 0, clientY: y || 0 });

let S = scn([{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 300, y: 0 }, { id: 'c', x: 0, y: 300 }]);
down(10, 10, 'hd', 'a'); up();
down(310, 10, 'hd', 'b', { ctrlKey: true }); up();
controle('★★★ Ctrl+clic ajoute (Maj aussi) ; la référence est le DERNIER cliqué',
         JSON.stringify([...S.msel]) === '["a","b"]' && S.sel.id === 'b', JSON.stringify([[...S.msel], S.sel]));
controle('★★ un clic ne déplace rien et ne marque rien comme modifié', S.nodes[0].x === 0 && ctx.dirty === 0);
down(310, 10, 'hd', 'b'); move(313, 12); up();
controle('★★ sous 6 px d\'écran : rien ne bouge (doigt qui tremble)', S.nodes[1].x === 300 && ctx.dirty === 0, JSON.stringify(S.nodes[1]));
down(310, 10, 'hd', 'b'); move(410, 60); up();
controle('★★★ glisser un nœud de la sélection emmène TOUTE la sélection', S.nodes[0].x === 100 && S.nodes[1].x === 400
         && S.nodes[0].y === 50 && S.nodes[2].x === 0 && ctx.dirty === 1, JSON.stringify(S.nodes));
S = scn([{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 300, y: 0 }, { id: 'c', x: 0, y: 300 }], [], { x: 0, y: 0, s: 0.5 });
down(5, 5, 'hd', 'a'); move(9, 5); up();
controle('★ le seuil est en px d\'ÉCRAN (zoom 0,5 : 4 px d\'écran = 8 du monde, pas de départ)', S.nodes[0].x === 0, JSON.stringify(S.nodes[0]));
S = scn([{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 300, y: 0 }, { id: 'c', x: 0, y: 300 }]);
down(-50, -50, null, null, { shiftKey: true }); move(400, 100); 
controle('★★★ Maj + glisser dans le vide : rectangle de sélection, dessiné', ctx.rectEl && ctx.rectEl.style.width === '450px'
         && JSON.stringify([...S.msel].sort()) === '["a","b"]', JSON.stringify([[...S.msel], ctx.rectEl && ctx.rectEl.style]));
up();
controle('★ relâché : rectangle retiré, référence posée, rien de modifié', ctx.rectEl.removed && S.sel && ctx.dirty === 0);
down(-50, 250, null, null, { metaKey: true }); move(100, 400); up();
controle('★★ un second rectangle (⌘) AJOUTE à la sélection', S.msel.size === 3, JSON.stringify([...S.msel]));
down(500, 500, null, null);
controle('★★ clic dans le vide : désélection puis panoramique', S.msel.size === 0 && ctx.drag() && ctx.drag().mode === 'pan');
up();
S = scn([{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 300, y: 0 }]);
down(10, 10, 'hd', 'a', { pointerType: 'touch' }); up();                       // toucher simple : a seul
down(310, 10, 'hd', 'b', { pointerType: 'touch' }); ctx.timers.slice(-1).forEach(f => f()); up();
controle('★★ au doigt : appui long = AJOUTER à la sélection (pas de Maj sur iPad)', S.msel.size === 2 && S.sel.id === 'b',
         JSON.stringify([...S.msel]));
down(310, 10, 'hd', 'b', { pointerType: 'touch' }); ctx.timers.slice(-1).forEach(f => f()); up();
controle('★★ … et un second appui long le RETIRE', JSON.stringify([...S.msel]) === '["a"]', JSON.stringify([...S.msel]));
S = scn([{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 300, y: 0 }], ['a']);
down(310, 30, 'nd', 'b', { shiftKey: true }); up();
controle('★ Maj+clic sur le CORPS d\'un nœud : ajoute, sans glisser', S.msel.size === 2 && !ctx.drag(), JSON.stringify([...S.msel]));
controle('★★ SCN.msel reste la sélection que lit scnPeers (BobiEditor.peers)', /Array\.from\(SCN\.msel \|\| \[\]\)/.test(src));

controle('★★ verrous AU DOIGT communs (Sélection multiple, Depuis le centre) et rectangle de sélection dessiné', /verrousHtml\(scnS\(\)\)/.test(src) && /lierVerrous\(/.test(src));

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
