// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Base commune des éditeurs (static/js/editor.js, window.BobiEditor) : ses deux parties restent
// INDÉPENDANTES (décision de Cyril, 30/10). L'édition (historique, presse-papiers, clavier,
// sélection multiple) ne connaît aucun rectangle ; la géométrie (BobiEditor.layout) ne connaît ni
// historique, ni clavier, ni DOM, ni stockage. Chaque partie est EXÉCUTÉE SEULE, sans l'autre.
//
//   $ node tests/verif_editor_independance.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ED = process.env.VERIF_EDITOR_JS || path.join(__dirname, '..', 'static', 'js', 'editor.js');
const src = fs.readFileSync(ED, 'utf8');
const R = path.join(__dirname, '..');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
function partie(nom) {
    const a = src.indexOf('// BEGIN:' + nom), b = src.indexOf('// END:' + nom);
    return (a >= 0 && b > a) ? src.slice(a, b) : '';
}
// Le code seul, sans les commentaires : la doc d'une partie a le droit de NOMMER l'autre.
const code = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
const edition = partie('edition'), layout = partie('layout');
controle('les deux parties sont délimitées', !!edition && !!layout);

// ═══ 1. La géométrie s'exécute SEULE, dans un monde sans window, DOM, stockage ni édition ═══
let L = null, err = null;
try {
    const c = { Math };
    vm.createContext(c);
    L = vm.runInContext(layout + '\n;layout', c);
} catch (e) { err = String(e); }
controle('★★★ la géométrie se charge seule (ni window, ni document, ni localStorage, ni édition)', !!L, err);
if (L) {
    let ok = true, e2 = null;
    try {
        const r = [{ x: 0, y: 0, w: 0.2, h: 0.2 }, { x: 0.2, y: 0, w: 0.2, h: 0.2 }, { x: 0.5, y: 0.5, w: 0.1, h: 0.1 }];
        L.aligner(r, [0, 1], 1, 'top'); L.egaliser(r, [0, 1], 0, 'both'); L.distribuer(r, [0, 1, 2], 'h');
        L.aimanter(r, [0], 0.01, 0, 0.2, 0.2, 0.02); L.sélection([0], 0, 1, { shiftKey: true });
        L.redimensionnerGroupe(r, [0, 1], 0, 0.3); L.redimensionnerAuFormat(r[0], 0.3, 1.5);
        L.deplacerGroupe(r, 0.1, 0.1);
    } catch (e) { ok = false; e2 = String(e); }
    controle('★★★ … et TOUTES ses opérations y tournent', ok, e2);
}

// ═══ 2. L'édition s'exécute SEULE, sans la géométrie ═══
let E = null; err = null;
try {
    const store = {};
    const c = { Math, JSON, Date, window: {}, navigator: { platform: 'Linux' },
                localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } };
    vm.createContext(c);
    E = vm.runInContext(edition + '\n;edition', c);
} catch (e) { err = String(e); }
controle('★★★ l\'édition se charge seule, sans la géométrie', !!E, err);
if (E) {
    let ok = true, e2 = null;
    try {
        const h = E.history(10); h.reset({ a: 1 }); h.commit({ a: 2 });
        ok = JSON.stringify(h.undo()) === '{"a":1}';
        E.clipboard.put('k', [1]); ok = ok && E.clipboard.get().kind === 'k';
        const a = { f: 'x' }, b = { f: 'x' };
        ok = ok && E.peers(a, [a, b], o => o.f).length === 2;
    } catch (e) { ok = false; e2 = String(e); }
    controle('★★★ … et historique, presse-papiers, sélection multiple y tournent', ok, e2);
}

// ═══ 3. Aucune ne NOMME l'autre (un appel paresseux passerait le chargement) ═══
const API_LAYOUT = ['layout', 'borner', 'aligner', 'egaliser', 'distribuer', 'aimanter', 'ciblesAimant',
                    'redimensionnerGroupe', 'redimensionnerAuFormat', 'deplacerGroupe'];
const API_EDITION = ['edition', 'history', 'clipboard', 'peers', 'bind', 'shortcut', 'isTyping',
                     'localStorage', 'document', 'window', 'navigator'];
const mot = (s, m) => new RegExp('(^|[^\\w$])' + m + '(?![\\w$])').test(s);
const fuiteE = API_LAYOUT.filter(m => mot(code(edition).replace(/^const edition = /m, ''), m));
const fuiteL = API_EDITION.filter(m => mot(code(layout).replace(/^const layout = /m, ''), m));
controle('★★ l\'édition ne nomme rien de la géométrie', fuiteE.length === 0, fuiteE.join(', '));
controle('★★ la géométrie ne nomme rien de l\'édition, ni du navigateur', fuiteL.length === 0, fuiteL.join(', '));

// ═══ 3 bis. La SURFACE compose les deux ; aucune des deux ne la connaît ═══
const surf = partie('surface');
controle('la surface est délimitée', !!surf);
const nomment = [['édition', edition], ['géométrie', layout]].filter(([, p]) => mot(code(p), 'surface')).map(([n]) => n);
controle('★★ ni l\'édition ni la géométrie ne nomment la surface', nomment.length === 0, nomment.join(', '));
let Sf = null; err = null;
try {
    const c3 = { Math };
    vm.createContext(c3);
    Sf = vm.runInContext(layout + '\n' + surf + '\n;surface', c3);   // SANS l'édition, sans window
} catch (e) { err = String(e); }
controle('★★ la surface se charge avec la seule géométrie (ni édition, ni DOM)', typeof Sf === 'function', err);

// ═══ 4. Un seul nom, pas d'alias ═══
const g = {};
const c = { Math, JSON, Date, navigator: { platform: 'Linux' }, window: g };
vm.createContext(c);
vm.runInContext(src, c);
controle('★★ window.BobiEditor porte les trois parties', g.BobiEditor && typeof g.BobiEditor.history === 'function'
         && g.BobiEditor.layout && typeof g.BobiEditor.layout.aligner === 'function' && typeof g.BobiEditor.surface === 'function');
controle('★ aucun alias des anciens noms', !('BobiEditKit' in g) && !('MXLLayout' in g), Object.keys(g).join(','));
const vieux = ['static/js/edit_kit.js', 'static/js/layout_engine.js'].filter(p => fs.existsSync(path.join(R, p)));
controle('★ les anciens fichiers ont disparu', vieux.length === 0, vieux.join(', '));

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
