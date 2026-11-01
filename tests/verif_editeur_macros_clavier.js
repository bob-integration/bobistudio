// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Éditeurs de macros (workspace, vues Blocs et Scénario) : annuler/rétablir, copier/couper/coller,
// dupliquer, désactiver — demandés le 2026-10-29.
//
// On exécute les VRAIES fonctions de templates/workspace.html, extraites du gabarit, avec le VRAI
// static/js/editor.js. Seul l'affichage est remplacé (renderMacros, scnRenderAll… ne font rien) :
// on vérifie ce que les commandes font aux DONNÉES de la macro, c'est-à-dire ce qui sera enregistré.
//
//   $ node tests/verif_editeur_macros_clavier.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const RACINE = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(RACINE, 'templates', 'workspace.html'), 'utf8');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
// Une définition de premier niveau du script (indentée de 4) jusqu'à sa ligne fermante « }» / « };».
function extrait(debut) {
    const i = src.indexOf(debut);
    if (i < 0) { console.log('  ÉCHEC introuvable dans workspace.html : ' + debut); process.exit(1); }
    if (debut.includes('=>') && !debut.endsWith('{')) return src.slice(i, src.indexOf('\n', i));
    const re = /\n    \};?\n/g;
    re.lastIndex = i;
    const m = re.exec(src);
    return src.slice(i, m.index + m[0].length);
}
const code = [
    extrait("const M_DISABLEABLE = ["),
    "const mClone = o => JSON.parse(JSON.stringify(o));",
    extrait("    const mDirty = () =>").trim(),
    extrait("function mSteps(path) {"),
    extrait("function mIsSel(path, i) {"),
    extrait("function mSelRange() {"),
    extrait("function mInsert(path, at, steps) {"),
    extrait("function mSetOff(steps, on) {"),
    extrait("window.wspMToggleOff = function"),
    extrait("function mStepsFromNodes(data) {"),
    extrait("window.wspMCmd = async function"),
    extrait("const scnDirty = () => {"),
    "const scnNodeById = id => SCN && SCN.nodes.find(n => n.id === id);",
    extrait("function scnSelNodes() {"),
    extrait("function scnFragment(nodes) {"),
    extrait("function scnFragmentFromSteps(steps) {"),
    extrait("function scnPasteFragment(frag, at) {"),
    extrait("window.wspScnCmd = async function"),
].join('\n');

const toasts = [];
const ctx = {
    console, JSON, Math, Object, Array, Set,
    navigator: {platform: 'Linux'},
    document: {addEventListener() {}, getElementById: () => null, querySelectorAll: () => []},
    localStorage: (() => { const s = {}; return {getItem: k => s[k] ?? null, setItem: (k, v) => { s[k] = v; }}; })(),
    T: k => k, toast: (m, lvl) => toasts.push([m, lvl || 'ok']),
    renderMacros() {}, mPaintSel() {}, mEdToolsState() {}, mFindApply() {}, mSaveBlocks: async () => true,
    scnRenderAll() {}, scnRenderInsp() {}, scnPaintSel() {}, scnEdToolsState() {}, scnFindApply() {},
    scnEl: () => null, scnCenter: () => ({x: 0, y: 0}), wspScnDelSel() {}, wspScnSave() {},
    uid6: (() => { let k = 0; return () => 'u' + (++k); })(),
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(RACINE, 'static', 'js', 'editor.js'), 'utf8'), ctx);
vm.runInContext('var _mEdit = null, _mSel = null, _mHist = null, _mFind = "", SCN = null;\n' + code +
                '\nthis.mDirty = mDirty; this.scnDirty = scnDirty; this.mSteps = mSteps;', ctx);
const run = s => vm.runInContext(s, ctx);
const steps = () => run('_mEdit.steps');

(async () => {
    // ═══ BLOCS ═══════════════════════════════════════
    run(`_mEdit = {id: 1, steps: [{type: 'sleep', ms: 1}, {type: 'set_var', name: 'a', value: '1'},
                                  {type: 'if', cond: {}, then: [], else: []}], dirty: false};
         _mHist = BobiEditor.history(100); _mHist.reset(_mEdit.steps);`);
    run(`_mSel = {path: '', a: 0, b: 1}`);
    await ctx.wspMCmd('copy');
    run(`_mSel = {path: '', a: 2, b: 2}`);
    await ctx.wspMCmd('paste');
    controle('★★ Blocs : coller insère APRÈS la sélection', steps().length === 5 && steps()[3].type === 'sleep' &&
             steps()[4].type === 'set_var', JSON.stringify(steps().map(s => s.type)));
    controle('★ Blocs : les étapes collées sont sélectionnées', run('_mSel.a') === 3 && run('_mSel.b') === 4);
    await ctx.wspMCmd('undo');
    controle('★★★ Blocs : annuler retire le collage', steps().length === 3, JSON.stringify(steps().map(s => s.type)));
    await ctx.wspMCmd('redo');
    controle('★★ Blocs : rétablir le remet', steps().length === 5);
    run(`_mSel = {path: '', a: 0, b: 0}`);
    await ctx.wspMCmd('cut');
    controle('★★ Blocs : couper retire la sélection', steps().length === 4 && steps()[0].type === 'set_var');
    const iIf = steps().findIndex(s => s.type === 'if');
    run(`_mSel = null; _mEdit.steps[${iIf}].then.push({type: 'sleep', ms: 9}); mDirty();
         _mSel = {path: '${iIf}.then', a: 0, b: 0}`);
    run(`BobiEditor.clipboard.put('macro-steps', [{type: 'parallel', branches: [[], []]}])`);
    await ctx.wspMCmd('paste');
    controle('★ Blocs : un parallèle ne se colle pas DANS un si (un seul niveau à l\'interface)',
             steps()[iIf].then.length === 1 && toasts.some(t => t[0] === 'projects.ed_paste_nested'));
    run(`_mSel = {path: '', a: 0, b: ${iIf}}`);
    await ctx.wspMCmd('toggleDisable');
    controle('★★ Blocs : désactiver marque les étapes de la plage', steps()[0].disabled === true);
    controle('★★ Blocs : une STRUCTURE de la plage n\'est jamais marquée', !steps()[iIf].disabled);
    await ctx.wspMCmd('toggleDisable');
    controle('★ Blocs : une 2e fois réactive (le drapeau disparaît, il n\'est pas mis à false)',
             !('disabled' in steps()[0]));
    run(`_mSel = {path: '', a: 1, b: 1}`);
    const avant = JSON.stringify(steps());
    await ctx.wspMCmd('nudge', {action: 'nudge', dx: 0, dy: -1});
    controle('★ Blocs : ↑ monte l\'étape sélectionnée', JSON.stringify(steps()[0]) === JSON.stringify(JSON.parse(avant)[1]));
    await ctx.wspMCmd('delete');
    controle('★ Blocs : Suppr supprime la sélection', steps().length === JSON.parse(avant).length - 1);

    // ═══ SCÉNARIO ═════════════════════════════════════
    run(`SCN = {nodes: [{id: 'e', type: 'entry', params: {mode: 'manual'}, x: 0, y: 0},
                        {id: 'a', type: 'sleep', params: {ms: 1}, x: 200, y: 0},
                        {id: 'b', type: 'set_var', params: {name: 'x'}, x: 400, y: 0},
                        {id: 'j', type: 'join', params: {mode: 'all'}, x: 600, y: 0}],
                edges: [{from: 'e', port: 0, to: 'a'}, {from: 'a', port: 0, to: 'b'}, {from: 'b', port: 0, to: 'j'}],
                msel: new Set(['a', 'b']), sel: {t: 'node', id: 'a'}, lastPt: {x: 1000, y: 500},
                hist: BobiEditor.history(100), dirty: false};
         SCN.hist.reset({nodes: SCN.nodes, edges: SCN.edges});`);
    await ctx.wspScnCmd('copy');
    await ctx.wspScnCmd('paste');
    const N = () => run('SCN.nodes'), E = () => run('SCN.edges');
    const neufs = N().slice(4);
    controle('★★ Scénario : coller ajoute les nœuds copiés, avec de NOUVEAUX identifiants',
             neufs.length === 2 && neufs.every(n => !['a', 'b'].includes(n.id)), JSON.stringify(neufs.map(n => n.id)));
    const ids = new Set(neufs.map(n => n.id));
    const internes = E().filter(e => ids.has(e.from) && ids.has(e.to));
    controle('★★★ Scénario : l\'arête ENTRE les nœuds copiés suit, aucune vers l\'extérieur',
             internes.length === 1 && E().filter(e => ids.has(e.from) !== ids.has(e.to)).length === 0,
             JSON.stringify(E()));
    controle('★ Scénario : collé sous le dernier geste', neufs[0].x === 1000 && neufs[0].y === 500, JSON.stringify(neufs[0]));
    await ctx.wspScnCmd('undo');
    controle('★★★ Scénario : annuler retire le collage (nœuds ET arêtes)', N().length === 4 && E().length === 3);
    run(`SCN.msel = new Set(['b']); SCN.sel = {t: 'node', id: 'b'}`);
    await ctx.wspScnCmd('cut');
    controle('★★ Scénario : couper retire le nœud et ses arêtes', !N().some(n => n.id === 'b') &&
             !E().some(e => e.from === 'b' || e.to === 'b'));
    await ctx.wspScnCmd('undo');
    controle('★★ Scénario : annuler la coupe rend le nœud et ses arêtes', N().some(n => n.id === 'b') && E().length === 3);
    run(`SCN.msel = new Set(['a', 'j']);`);
    await ctx.wspScnCmd('toggleDisable');
    controle('★★ Scénario : désactiver marque le nœud d\'action, jamais la jointure',
             N().find(n => n.id === 'a').params.disabled === true && !N().find(n => n.id === 'j').params.disabled);
    run(`SCN.msel = new Set(['a']);`);
    await ctx.wspScnCmd('nudge', {action: 'nudge', dx: 1, dy: 0});
    controle('★ Scénario : → déplace de 5 px', N().find(n => n.id === 'a').x === 205);
    await ctx.wspScnCmd('duplicate');
    controle('★ Scénario : dupliquer décale la copie (+30 px)', N().slice(-1)[0].x === 235 && N().slice(-1)[0].params.disabled);

    // ═══ D'UNE VUE À L'AUTRE ══════════════════════════
    run(`BobiEditor.clipboard.put('macro-steps', [{type: 'sleep', ms: 7}, {type: 'if', cond: {}, then: [], else: []},
                                                  {type: 'set_var', name: 'z'}])`);
    const n0 = N().length, e0 = E().length;
    await ctx.wspScnCmd('paste');
    controle('★★ Blocs → Scénario : les étapes simples deviennent des nœuds CHAÎNÉS', N().length === n0 + 2 && E().length === e0 + 1);
    controle('★ Blocs → Scénario : le « si » est signalé, pas collé en silence',
             toasts.some(t => t[0] === 'projects.ed_paste_partial'));
    run(`SCN.msel = new Set(['a', 'b']);`);
    await ctx.wspScnCmd('copy');
    run(`_mSel = null`);
    const l0 = steps().length;
    await ctx.wspMCmd('paste');
    controle('★★ Scénario → Blocs : les nœuds deviennent des étapes, dans l\'ordre gauche → droite',
             steps().length === l0 + 2 && steps()[l0].type === 'sleep' && steps()[l0 + 1].type === 'set_var',
             JSON.stringify(steps().slice(l0)));

    if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
    console.log('\nTout est vert.');
})();
