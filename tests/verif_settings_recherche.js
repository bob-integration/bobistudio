// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Search in Settings (templates/settings.html, setSearch*) — asked by a tester on 2026-11-22: about
// fifty tabs in ten groups, and the plugin catalogue was not found (its tab said « Updates »).
//   1. a tab is found by its name, accents ignored, and opening it switches group AND tab;
//   2. a field is found by its label, and opening it points at that field;
//   3. the catalogue is found by « catalog » in both languages;
//   4. keyboard: Enter opens the selected hit, Escape clears; nothing found is said.
//   node tests/verif_settings_recherche.js
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'settings.html'), 'utf8');
let ko = 0;
const ok = (c, t, d) => { console.log((c ? '  ok  ' : ' ÉCHEC ') + t); if (!c) { console.log('         ' + (d || '')); ko++; } };

const a = html.indexOf('// ─── Search in Settings');
const b = html.indexOf('function switchSetTab(name) {');
if (a < 0 || b < a) { console.log('ÉCHEC bloc introuvable'); process.exit(1); }
const bloc = html.slice(a, b);

function el(text, extra) { return Object.assign({ textContent: text, classList: { add() {}, remove() {}, toggle() {} },
                                                  scrollIntoView() { this.vu = true; } }, extra || {}); }
const champs = { reseau: [el('Mode réseau des conteneurs'), el('Passerelle')], catalogue: [el('Activer après installation')],
                 branding: [el('Nom du système'), el('Nom réseau du site')] };   // a field of an EARLIER tab
const els = {
  'set-group-btn-general': el('Général'), 'set-group-btn-nodes': el('Nœuds & Matériel'), 'set-group-btn-systeme': el('Système'),
  'set-tab-btn-branding': el('Personnalisation'), 'set-tab-btn-reseau': el('Réseau'), 'set-tab-btn-catalogue': el('Mises à jour et catalogue'),
  'set-search-results': { hidden: true, innerHTML: '', querySelectorAll: () => [] },
};
for (const t of ['branding', 'reseau', 'catalogue']) els['set-tab-' + t] = { querySelectorAll: () => champs[t] };
const ouverts = [];
const g = {
  SET_GROUPS: [{ id: 'general', tabs: ['branding'] }, { id: 'nodes', tabs: ['reseau'] }, { id: 'systeme', tabs: ['catalogue'] }],
  document: { getElementById: (id) => els[id] || null, addEventListener() {} },
  window: { t: (k) => k }, escapeHtml: (s) => String(s), setTimeout: () => 0,
  switchSetGroup: (x) => ouverts.push(['group', x]), switchSetTab: (x) => ouverts.push(['tab', x]),
};
const api = new Function(...Object.keys(g), bloc + '\nreturn {setSearchRun, setSearchOpen, setSearchKey, hits: () => _setSearchHits};')(...Object.values(g));
const box = els['set-search-results'];

console.log('\n1. a tab, accents ignored');
api.setSearchRun('reseau');
ok(api.hits()[0] && api.hits()[0].tab === 'reseau' && !api.hits()[0].el, '« reseau » finds the Réseau tab first', JSON.stringify(api.hits().map(h => h.text)));
api.setSearchOpen(0);
ok(JSON.stringify(ouverts) === JSON.stringify([['group', 'nodes'], ['tab', 'reseau']]), 'opening switches group then tab', JSON.stringify(ouverts));

console.log('\n2. a field');
ouverts.length = 0;
api.setSearchRun('passerelle');
const h = api.hits();
ok(h.length === 1 && h[0].el === champs.reseau[1] && h[0].where.includes('Réseau'), 'the field is found, with where it lives', JSON.stringify(h.map(x => x.where)));
api.setSearchOpen(0);
ok(champs.reseau[1].vu === true && ouverts.length === 2, 'opening it points at the field');

console.log('\n3. the catalogue, in both languages');
api.setSearchRun('catalog');
ok(api.hits().some(x => x.tab === 'catalogue' && !x.el), '« catalog » finds the catalogue tab');
for (const lang of ['fr', 'en']) {
  const cat = JSON.parse(fs.readFileSync(path.join(root, 'i18n', lang + '.json'), 'utf8'));
  ok(/catalog/i.test(cat['catalogue.title'] || ''), lang + ': the tab name says « catalogue »', cat['catalogue.title']);
  ok(cat['settings.search.placeholder'] && cat['settings.search.none'], lang + ': search texts exist');
}

console.log('\n4. keyboard, nothing found');
ouverts.length = 0;
api.setSearchRun('reseau');
api.setSearchKey({ key: 'Enter', preventDefault() {}, target: {} });
ok(ouverts.length === 2 && ouverts[1][1] === 'reseau', 'Enter opens the selected hit');
const champ = { value: 'x' };
api.setSearchRun('reseau'); api.setSearchKey({ key: 'Escape', target: champ });
ok(champ.value === '' && box.hidden === true, 'Escape clears and closes');
api.setSearchRun('zzzz');
ok(box.hidden === false && box.innerHTML.includes('settings.search.none') && api.hits().length === 0, 'nothing found is said');
api.setSearchRun('r');
ok(box.hidden === true, 'one letter: no list');

process.exit(ko ? 1 : 0);
