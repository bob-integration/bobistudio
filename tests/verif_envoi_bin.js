// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
/* Garde : l'envoi depuis l'ordinateur (Gestionnaire de médias) propose un BIN, plus de dossier
 * (recette du 2026-10-30 ; décision du 2026-10-29 : en local, l'utilisateur classe ses médias dans
 * la base, pas sur le disque — comme le recorder).
 *
 * Vraie fonction `chooseTarget` de static/js/media_upload.js, extraite et exécutée dans un DOM
 * minimal : ce que la fenêtre affiche, et ce qu'elle rend à l'appelant.
 *
 *     node tests/verif_envoi_bin.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const RACINE = path.join(__dirname, '..');
let echecs = 0;
function controle(lib, ok, detail) {
    console.log((ok ? '  OK    ' : '  ÉCHEC ') + lib + (ok || detail === undefined ? '' : '  → ' + detail));
    if (!ok) echecs++;
}
const src = fs.readFileSync(path.join(RACINE, 'static', 'js', 'media_upload.js'), 'utf8');
const lib = fs.readFileSync(path.join(RACINE, 'static', 'js', 'media_library.js'), 'utf8');
const m = /  async function chooseTarget\(defaults\)\{[\s\S]*?\n  \}\n/.exec(src);
if (!m) { console.log('chooseTarget introuvable'); process.exit(1); }

let dlg = null, choix = {};
function fakeDialog() {
    const els = {};
    const el = k => els[k] || (els[k] = {value: choix[k] || '', focus() {}, onclick: null, onsubmit: null});
    return dlg = {html: '', set innerHTML(v) { this.html = v; }, get innerHTML() { return this.html; },
                  className: '', querySelector: s => el(s), addEventListener() {}, close() {}, remove() {},
                  showModal() { setImmediate(() => el('form').onsubmit({preventDefault() {}})); }};
}
const prefs = {};
const ctx = {
    esc: s => String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'),
    T: (k, d) => d, toast() {}, setImmediate, Promise, Object,
    config: async () => ({nodes: [{id: 1, name: 'dell-1', capable: true}]}),
    pref: {get: (k, d) => prefs[k] || d, set: (k, v) => { prefs[k] = v; }},
    document: {createElement: fakeDialog, body: {appendChild() {}}},
};
vm.createContext(ctx);
vm.runInContext(m[0] + '\nthis.chooseTarget = chooseTarget;', ctx);

const BINS = [{id: 4, name: 'Rushes'}, {id: 7, name: 'Habillage'}];
(async () => {
    console.log('── 1. La fenêtre');
    choix = {'.mlu-node': '1', '.mlu-bin': '7'};
    const t = await ctx.chooseTarget({count: 2, bins: BINS, bin_id: 7});
    controle('plus de champ Dossier', !/mlu-folder|Dossier/.test(dlg.html), dlg.html);
    controle('un choix de bin : « Aucun » puis les bins', /class="mlu-bin"><option value="">Aucun<\/option><option value="4">Rushes<\/option><option value="7" selected>Habillage/.test(dlg.html), dlg.html);
    console.log('── 2. Ce qu\'elle rend');
    controle('le bin choisi, dossier vide (géré par le système)', t && t.bin_id === 7 && t.folder === '' && t.node_id === 1, JSON.stringify(t));
    controle('le bin n\'est pas retenu comme préférence de poste (il suit le bin affiché)', !('bin_id' in (prefs.target || {})), JSON.stringify(prefs));
    choix = {'.mlu-node': '1', '.mlu-bin': ''};
    const t2 = await ctx.chooseTarget({count: 1, bins: BINS, bin_id: null});
    controle('« Aucun » : pas de bin', t2 && t2.bin_id === null && !/selected>Rushes|selected>Habillage/.test(dlg.html), JSON.stringify(t2));
    console.log('── 3. L\'appelant (Gestionnaire)');
    controle('passe les bins MANUELS et le bin visé', /chooseTarget\(\{count: files\.length, bins: st\.bins\.filter\(x => !x\.smart\), bin_id: opts\.bin_id\}\)/.test(lib));
    controle('envoie sans dossier, rangé dans le bin choisi',
        /up\.enqueue\(files, \{node_id:t\.node_id, folder:'', [^}]*\},\s*\{bin_id: ob \? ob\.id : null/.test(lib));
    console.log(echecs ? '\n' + echecs + ' échec(s)' : '\nEnvoi depuis l\'ordinateur : un bin, plus de dossier.');
    process.exit(echecs ? 1 : 0);
})();
