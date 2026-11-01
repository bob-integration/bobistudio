// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Noyau d'édition partagé (static/js/editor.js) : historique, raccourcis, presse-papiers.
// Éditeurs de macros (Blocs, Scénario) et layout du multiview s'appuient dessus (2026-10-29) —
// un défaut ici est un défaut dans les trois. On exécute le VRAI fichier dans un faux DOM.
//
//   $ node tests/verif_editor.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}

const listeners = [];
const store = {};
let storageOk = true;
const ctx = {
    window: {},
    navigator: {platform: 'Linux x86_64'},
    document: {addEventListener: (t, f) => listeners.push(f), removeEventListener: (t, f) => {
        const i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1); }},
    localStorage: {
        getItem: k => { if (!storageOk) throw new Error('blocked'); return store[k] ?? null; },
        setItem: (k, v) => { if (!storageOk) throw new Error('blocked'); store[k] = v; },
    },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'editor.js'), 'utf8'), ctx);
const K = ctx.window.BobiEditor;
controle('le module s\'expose (window.BobiEditor)', !!K);

// ═══ 1. HISTORIQUE ═══
const h = K.history(3);
h.reset({v: 0});
controle('rien à annuler juste après l\'ouverture', !h.canUndo() && h.undo() === undefined);
h.commit({v: 1}); h.commit({v: 2});
controle('★★ annuler rend l\'état PRÉCÉDENT', h.undo().v === 1);
controle('★★ rétablir rend l\'état suivant', h.redo().v === 2);
const st = h.undo();
st.v = 99;   // l'éditeur modifie l'état rendu…
controle('★★ l\'état rendu est une COPIE (le modifier n\'altère pas l\'historique)', h.redo().v === 2);
h.undo(); h.commit({v: 5});
controle('★ une modification après annulation efface le « rétablir »', !h.canRedo());
controle('★ un instantané identique n\'occupe pas un Ctrl+Z', h.commit({v: 5}) === false);
h.commit({v: 6}); h.commit({v: 7});
let n = 0; while (h.undo() !== undefined) n++;
controle('★ l\'historique est borné (max 3 → 2 annulations)', n === 2, 'obtenu ' + n);

// ═══ 2. RACCOURCIS ═══
const ev = (key, mods) => Object.assign({key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false}, mods || {});
const act = (key, mods) => (K.shortcut(ev(key, mods)) || {}).action;
controle('★★ Ctrl+Z = annuler, Ctrl+Maj+Z et Ctrl+Y = rétablir',
         act('z', {ctrlKey: true}) === 'undo' && act('Z', {ctrlKey: true, shiftKey: true}) === 'redo' &&
         act('y', {ctrlKey: true}) === 'redo');
controle('★★ ⌘ vaut Ctrl (Mac, clavier d\'iPad)', act('c', {metaKey: true}) === 'copy' && act('v', {metaKey: true}) === 'paste');
controle('★ Ctrl+X / D / A / S / F / /',
         act('x', {ctrlKey: true}) === 'cut' && act('d', {ctrlKey: true}) === 'duplicate' &&
         act('a', {ctrlKey: true}) === 'selectAll' && act('s', {ctrlKey: true}) === 'save' &&
         act('f', {ctrlKey: true}) === 'find' && act('/', {ctrlKey: true}) === 'toggleDisable');
const nd = K.shortcut(ev('ArrowLeft', {shiftKey: true}));
controle('★ flèches = déplacer, Maj = pas ×10', nd.action === 'nudge' && nd.dx === -10 && nd.dy === 0);
controle('★ Alt+touche n\'est pas un raccourci (AltGr des claviers français, Alt+flèche du navigateur)',
         K.shortcut(ev('z', {ctrlKey: true, altKey: true})) === null && K.shortcut(ev('ArrowLeft', {altKey: true})) === null);
controle('une lettre seule n\'est pas un raccourci', K.shortcut(ev('z')) === null);

// ═══ 3. BIND : champs de saisie ═══
const got = [];
const unbind = K.bind({active: () => true, on: {undo: () => got.push('undo'), save: () => got.push('save')}});
const fire = (key, mods, target) => {
    const e = Object.assign(ev(key, mods), {target: target || {tagName: 'DIV'}, prevented: false,
                                            preventDefault() { this.prevented = true; }});
    listeners.forEach(f => f(e));
    return e;
};
const input = {tagName: 'INPUT', blur() {}};
let e = fire('z', {ctrlKey: true}, input);
controle('★★★ Ctrl+Z DANS un champ est laissé au navigateur (annule la frappe, pas l\'éditeur)',
         got.length === 0 && !e.prevented);
e = fire('s', {ctrlKey: true}, input);
controle('★★ Ctrl+S passe même dans un champ', got[0] === 'save' && e.prevented);
e = fire('z', {ctrlKey: true});
controle('★★ Ctrl+Z hors champ → l\'éditeur, et le navigateur n\'agit pas', got[1] === 'undo' && e.prevented);
e = fire('c', {ctrlKey: true});
controle('★ action sans gestionnaire : laissée au navigateur', !e.prevented);
unbind();
fire('z', {ctrlKey: true});
controle('unbind détache', got.length === 2);
K.bind({active: () => false, on: {undo: () => got.push('x')}});
fire('z', {ctrlKey: true});
controle('★ éditeur fermé (active() faux) : rien', got.length === 2);

// ═══ 4. PRESSE-PAPIERS ═══
K.clipboard.put('macro-steps', [{type: 'sleep', ms: 5}]);
controle('★★ le presse-papiers survit à la page (localStorage) : on colle dans une autre macro',
         JSON.parse(store['bobi.editkit.clipboard']).data[0].ms === 5);
storageOk = false;
K.clipboard.put('mv-boxes', [1]);
const c = K.clipboard.get();
controle('★ stockage bloqué (navigation privée) : le presse-papiers reste en mémoire',
         c && c.kind === 'mv-boxes');

// ═══ 5. SÉLECTION MULTIPLE ═══
const a1 = {f: 'k'}, a2 = {f: 'k'}, b1 = {f: 'j'}, z1 = {f: ''}, z2 = {f: ''};
const kf = x => x.f;
controle('★★ même fonction : la valeur va à toute la sélection', K.peers(a1, [a1, a2], kf).length === 2);
controle('★★ sélection mêlée : l\'élément affiché seul', K.peers(a1, [a1, b1], kf).length === 1);
const a3 = {f: 'k'};
controle('★ affiché HORS sélection, même fonction : lui seul', K.peers(a3, [a1, a2], kf).length === 1 && K.peers(a3, [a1, a2], kf)[0] === a3);
controle('★ un seul sélectionné : lui seul', K.peers(a1, [a1], kf).length === 1);
controle('★ sans fonction (clé vide) : jamais partagé', K.peers(z1, [z1, z2], kf).length === 1);

// ═══ 6. LIBELLÉS ═══
controle('libellé lisible d\'un raccourci', K.keyLabel('mod+shift+z') === 'Ctrl + Maj + Z', K.keyLabel('mod+shift+z'));

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
