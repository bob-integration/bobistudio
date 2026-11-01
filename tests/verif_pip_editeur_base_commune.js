// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Éditeur de modèles de PiP sur la SURFACE commune des éditeurs (BobiEditor.surface) — chantier
// « un seul moteur d'édition », étape 3 (30/10). Sélection, glisser, poignée, aimant, flèches,
// outils, annuler/rétablir et copier/coller viennent de la base, avec les règles arbitrées par
// Cyril : référence = dernier cliqué (Maj ou Ctrl/⌘), groupe borné en entier, poignée selon le
// format (la vidéo est verrouillée en 16:9), poignée d'une sélection = échelle depuis le coin,
// distribuer ET répartir les espaces, Remplir. Il ne reste dans le PiP aucune géométrie à lui.
// VRAIES fonctions de pip_editor.js et de editor.js.
//
//   $ node tests/verif_pip_editeur_base_commune.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const R = path.join(__dirname, '..');
const PIP = process.env.VERIF_PIP_JS || path.join(R, 'static', 'pip_editor.js');
const src = fs.readFileSync(PIP, 'utf8');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
const fn = n => { const i = src.indexOf('\nfunction ' + n + '('); if (i < 0) return '';
                  const re = /\n\}\n/g; re.lastIndex = i + 1; return src.slice(i, re.exec(src).index + 3); };
const ligne = d => { const i = src.indexOf(d); return i < 0 ? '' : src.slice(i, src.indexOf('\n', i)); };

// ═══ 1. Plus de géométrie ni de gestes écrits ici ═══
const gestes = ['alignComps', 'matchSizeComps', 'distributeComps', 'onDown', 'onMove', 'onUp', '_pipClavier'].map(fn).join('\n');
const copies = [["switch d'alignement", /case 'hcenter'/], ['bornes de groupe', /\bloX\b/], ["boucle d'aimant", /bestDx|xT\.forEach/],
                ["calcul d'aimant local", /computeSnap\(/], ['pas de distribution', /\(vN - v0\)/],
                ['flèches à la main', /NUDGE_KEYS\[/]].filter(([, re]) => re.test(gestes + src)).map(([n]) => n);
controle('★★★ aucune géométrie ni aucun geste réécrits dans l\'éditeur de PiP', copies.length === 0, copies.join(', '));
controle('★★★ les gestes passent par BobiEditor.surface (souris, outils, clavier)',
         /BobiEditor\.surface\(/.test(src) && /\.appui\(/.test(fn('onDown')) && /\.glisse\(/.test(fn('onMove'))
         && /\.relache\(/.test(fn('onUp')) && /\.outil\(/.test(fn('_outil')) && /S\.clavier\(/.test(fn('_pipClavier')));
controle('★★ pictos du CATALOGUE partagé, plus de copie locale', /const _SVG = \(window\.MXLControls/.test(src) && !/align_left: '<svg/.test(src));

controle('★★ barre « Édition » COMMUNE (boutons = actions du clavier, chemin au doigt)', /actionsEdition\(\)/.test(src) && /surf\(\)\.agir\(a\.action\)/.test(src));

// ═══ 2. Comportement, avec la vraie base ═══
const ecouteurs = [], store = {};
const code = [
    'let cur = null, cellW = 640, selIdxs = [], selIdx = -1, drag = null, snapEnabled = true, snapGuides = [], alignRef = "cell";',
    'const comps = () => (cur && cur.config && cur.config.components) || [];',
    ligne('const _videoK = '), ligne('const _ANCHOR_X_IMAGE_TYPES = '), ligne('const SNAP_PX = '), ligne('let _surf = '),
    ...['tplAspect', '_setSel', '_enforceVideoRatio', 'compAnchor', 'pxRect', 'framePxRect', 'imgPxRect', 'surf', '_surfIn',
        '_surfOut', '_outil', 'alignComps', 'matchSizeComps', 'distributeComps', 'spaceComps', 'fillComps', 'canvasPos', 'hit',
        'onDown', 'onMove', 'onUp', '_pipClavier', 'removeComp'].map(fn),
    'function refreshProps() {} function draw() {} function syncGeomFields() {} function refreshAll() {}',
    'function flash(m) { flashes.push(m); } function _t(k, d) { return d; }',
    'this.set = (c, s, a) => { cur = {config: {aspect: 16 / 9, components: c}}; _setSel(s); alignRef = a || "cell"; };',
    'this.c = () => cur.config.components; this.sel = () => [selIdxs.slice(), selIdx]; this.ev = s => eval(s);',
].join('\n');
const canvas = { width: 640, height: 360, offsetParent: {}, setPointerCapture() {},
                 getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 360 }) };
const ctx = { Math, String, parseInt, parseFloat, JSON, Date, flashes: [], cvs: () => canvas, $: () => canvas,
              navigator: { platform: 'Linux' },
              document: { addEventListener: (t, h) => ecouteurs.push(h), removeEventListener() {} },
              localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(R, 'static', 'js', 'editor.js'), 'utf8'), ctx);
let err = null;
try { vm.runInContext(code, ctx); } catch (e) { err = String(e); }
controle('les fonctions de l\'éditeur se chargent', !err, err);
if (err) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
const souris = (x, y, mods) => Object.assign({ clientX: x, clientY: y, pointerId: 1 }, mods || {});
const touche = (key, mods) => ecouteurs.forEach(h => h(Object.assign({ key, target: { tagName: 'CANVAS' }, preventDefault() {} }, mods || {})));
const T = (x, y, w, h, ex) => Object.assign({ type: 'text', x, y, w, h }, ex || {});

ctx.set([T(0.1, 0.1, 0.2, 0.1), T(0.5, 0.4, 0.2, 0.1)], [0, 1]);
ctx.alignComps('left');
controle('★★ aligner à gauche sur la référence (dernière sélectionnée)', ctx.c()[0].x === 0.5 && ctx.c()[1].x === 0.5, JSON.stringify(ctx.c()));
ctx.set([{ type: 'video', x: 0, y: 0, w: 0.6, h: 0.6 }, T(0.5, 0.8, 0.2, 0.1)], [1], 'video');
ctx.alignComps('right');
controle('★★ composant SEUL, référence « image » : calé sur le bord droit de la vidéo', ctx.c()[1].x === 0.4, ctx.c()[1].x);
ctx.set([T(0, 0, 0.1, 0.1), T(0.05, 0, 0.4, 0.1), T(0.9, 0, 0.1, 0.1)], [0, 1, 2]);
ctx.spaceComps('h');
controle('★★★ NOUVEAU : répartir les espaces', ctx.c()[1].x === 0.3, JSON.stringify(ctx.c()[1]));
ctx.set([T(0.6, 0.2, 0.1, 0.1), T(0.1, 0.5, 0.1, 0.1)], [0, 1]);
ctx.fillComps('h');
controle('★★★ NOUVEAU : Remplir tuile la sélection dans l\'ordre à l\'écran', ctx.c()[1].x === 0 && ctx.c()[0].x === 0.5 && ctx.c()[0].w === 0.5,
         JSON.stringify(ctx.c()));
ctx.set([T(0.1, 0.1, 0.2, 0.1), T(0.5, 0.6, 0.3, 0.2)], [0]);
ctx.onDown(souris(70, 40)); ctx.onUp();
ctx.onDown(souris(400, 250, { ctrlKey: true })); ctx.onUp();
controle('★★★ NOUVEAU : Ctrl+clic ajoute à la sélection, le dernier cliqué est la référence',
         JSON.stringify(ctx.sel()) === '[[0,1],1]', JSON.stringify(ctx.sel()));
ctx.set([T(0.7, 0.1, 0.2, 0.1), T(0.1, 0.5, 0.2, 0.1)], [0, 1]);
ctx.ev('snapEnabled = false');
ctx.onDown(souris(100, 190)); ctx.onMove(souris(420, 190)); ctx.onUp();
controle('★★★ glisser un groupe : TOUT le groupe s\'arrête au bord', ctx.c()[0].x === 0.8 && ctx.c()[1].x === 0.2, JSON.stringify(ctx.c()));
ctx.set([{ type: 'video', x: 0, y: 0, w: 0.5, h: 0.5 }], [0]);
const r0 = { x: 0, y: 0, w: 320, h: 180 };
ctx.onDown(souris(r0.w - 2, r0.h - 2)); ctx.onMove(souris(400, 190)); ctx.onUp();
controle('★★ poignée de la VIDÉO : format 16:9 tenu', ctx.c()[0].w === ctx.c()[0].h && ctx.c()[0].w > 0.5, JSON.stringify(ctx.c()[0]));
ctx.set([{ type: 'video', x: 0, y: 0, w: 0.5, h: 0.5 }], [0]);
ctx.ev('snapEnabled = false');
ctx.onDown(souris(318, 178)); ctx.onMove(souris(318, 300)); ctx.onUp();
controle('★★ vidéo verrouillée : tirer la poignée vers le BAS l\'agrandit aussi', ctx.c()[0].w > 0.6, JSON.stringify(ctx.c()[0]));
ctx.set([T(0, 0.5, 0.25, 0.2), T(0.25, 0.5, 0.25, 0.2)], [1, 0]);
ctx.onDown(souris(158, 250)); ctx.onMove(souris(222, 250)); ctx.onUp();
controle('★★★ NOUVEAU : poignée d\'une sélection = échelle depuis le coin, jointifs restent jointifs',
         ctx.c()[0].w > 0.25 && Math.abs(ctx.c()[1].x - (ctx.c()[0].x + ctx.c()[0].w)) < 0.0011, JSON.stringify(ctx.c()));
ctx.ev('snapEnabled = true');

// ═══ 3. Clavier ═══
ctx.set([{ type: 'video', x: 0, y: 0, w: 0.5, h: 0.5 }, T(0.1, 0.8, 0.2, 0.1)], [1]);
ctx._pipClavier();
ctx.ev('_surf.historiqueZero()');
touche('ArrowRight', { shiftKey: true });
controle('★★ flèche Maj : 10 px de la tuile simulée', Math.abs(ctx.c()[1].x - (0.1 + 10 / 640)) < 0.0011, ctx.c()[1].x);
touche('z', { ctrlKey: true });
controle('★★★ NOUVEAU : Ctrl+Z annule', ctx.c()[1].x === 0.1, ctx.c()[1].x);
ctx.set([{ type: 'video', x: 0, y: 0, w: 0.5, h: 0.5 }, T(0.1, 0.8, 0.2, 0.1)], [0, 1]);
touche('c', { ctrlKey: true }); touche('v', { ctrlKey: true });
controle('★★★ NOUVEAU : Ctrl+C / Ctrl+V — copie décalée, la vidéo n\'est jamais dupliquée',
         ctx.c().length === 3 && ctx.c().filter(c => c.type === 'video').length === 1 && ctx.c()[2].x === 0.13, JSON.stringify(ctx.c()));
touche('Delete');
controle('★★ Suppr retire la sélection', ctx.c().length === 2, ctx.c().length);

controle('★★ verrous AU DOIGT communs (Sélection multiple, Depuis le centre) et rectangle de sélection dessiné', /verrousHtml\(surf\(\)\)/.test(src) && /lierVerrous\(/.test(src) && /rectangle: true/.test(src) && /_surf\.rectSel/.test(src));

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
