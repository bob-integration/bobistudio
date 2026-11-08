#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Help page menu, reorganised on 2026-11-08 (templates/aide.html):
//   1. search finds the text of IMPORTED documents (INSTALL.md, CHANGELOG.md…). Their <template>
//      only holds « Chargement… »: until 2026-11-08 the index never saw a word of them;
//   2. every section is folded by default, except the one of the open article;
//   3. Plugins and Services are split into sub-sections that come from the MANIFESTS (labels and
//      order from the server), and an article shows its full place: « Services › Pupitres… ».
// The functions are taken from the page itself; the DOM is a minimal stand-in (no jsdom here).
'use strict';
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'aide.html'), 'utf8');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

function slice(from, to) {
    const a = html.indexOf(from), b = html.indexOf(to, a);
    ok(a > 0 && b > a, 'found in aide.html: ' + from.slice(0, 40));
    return html.slice(a, b);
}
const src = [
    slice('const CATEGORIES = [', '// Libellés des sous-rubriques'),
    slice('function esc(s)', '// ── Charger les articles'),
    slice('function articleText(a)', '// Charge en arrière-plan'),
    slice('// ── Rendu du nav', '// ── Replier'),
].join('\n');

// ── minimal DOM ───────────────────────────────────────────────────────────
const strip = h => String(h).replace(/<[^>]*>/g, ' ');
function tpl(inner, docs, changelog) {
    return {
        innerHTML: inner,
        content: {
            querySelectorAll: sel => sel === '.wiki-doc-content[data-doc]'
                ? docs.map(d => ({ dataset: { doc: d } })) : [],
            querySelector: sel => (sel === '#wiki-changelog-content' && changelog) ? {} : null,
        },
    };
}
const TEMPLATES = {
    'wiki-art-installation': tpl('<p>Contenu chargé depuis INSTALL.md</p><div>Chargement…</div>', ['install']),
    'wiki-art-mises-a-jour': tpl('<div id="wiki-changelog-content">Chargement…</div>', [], true),
    'wiki-art-vue-densemble': tpl('<p>Bobi.Studio orchestre</p>', []),
};
const document = {
    getElementById: id => TEMPLATES[id] || null,
    createElement: () => { const e = {}; Object.defineProperty(e, 'innerHTML', { set(v) { e.textContent = strip(v); } }); return e; },
};
const navEl = { innerHTML: '', querySelectorAll: () => [] };

const api = new Function('document', 'navEl', `
  let allArticles = [], currentId = null, servicesKnown = null, changelogCache = null;
  const expandedCats = new Set(), subLabels = {}, subOrder = {}, docCache = {};
  ${src}
  return {
    articleText, renderNav, placeLabel,
    set: o => { if ('allArticles' in o) allArticles = o.allArticles; if ('currentId' in o) currentId = o.currentId;
                if ('servicesKnown' in o) servicesKnown = o.servicesKnown; if ('changelog' in o) changelogCache = o.changelog; },
    docCache, expandedCats, subLabels, subOrder,
  };`)(document, navEl);

// ── 1. search sees imported documents ─────────────────────────────────────
const inst = { id: 'installation', title: 'Installer', cat: 'start' };
const chg = { id: 'mises-a-jour', title: 'Évolutions', cat: 'about' };
ok(!api.articleText(inst).includes('hugepages'), 'before the document arrives, its text is not invented');
api.docCache.install = { html: '<h2>Réserver les hugepages</h2>', mtime: 0 };
ok(api.articleText(inst).includes('hugepages'), 'once loaded, INSTALL.md text is searchable', api.articleText(inst));
ok(api.articleText(inst).includes('INSTALL.md'), 'the article\'s own text is still indexed');
api.set({ changelog: { html: '<p>0.9.10 : reprise accélérée</p>' } });
ok(api.articleText(chg).includes('reprise accélérée'), 'CHANGELOG text is searchable', api.articleText(chg));
ok(!api.articleText({ id: 'vue-densemble', cat: 'start' }).includes('reprise accélérée'),
   'the changelog text goes only to the article that holds it');

// ── 2. folded by default, open article's section unfolded ─────────────────
const parse = () => {
    const out = {};
    for (const m of navEl.innerHTML.matchAll(/<div class="wiki-cat( collapsed)?" data-cat="([^"]+)">/g)) out[m[2]] = !m[1];
    return out;
};
const arts = [
    { id: 'vue-densemble', title: "Vue d'ensemble", cat: 'start' },
    { id: 'depannage', title: 'Dépannage courant', cat: 'debug' },
    { id: 'dev-plugin', title: 'Écrire un plugin', cat: 'dev' },
    { id: 'plugin-mixer', title: 'Mélangeur', cat: 'plugins', sub: 'composition' },
    { id: 'plugin-scope', title: 'Scope', cat: 'plugins', sub: 'mesure' },
    { id: 'base-medias', title: 'Base des médias', cat: 'plugins', sub: 'medias' },
    { id: 'snmp-supervision', title: 'SNMP', cat: 'services', service: 'snmp' },
    { id: 'tsl-explique', title: 'TSL', cat: 'services', service: 'tsl' },
    { id: 'service-tts', title: 'Synthèse vocale', cat: 'services', service: 'tts' },
];
api.set({ allArticles: arts, currentId: 'vue-densemble' });
api.renderNav();
let open = parse();
ok(open.start === true, 'first page: Démarrer is unfolded', open);
ok(Object.entries(open).every(([k, v]) => k === 'start' || v === false), 'every other section is folded', open);
api.set({ currentId: 'depannage' });
api.renderNav();
open = parse();
ok(open.debug === true && open.start === false, 'opening an article unfolds ITS section, the one left folds back', open);
api.expandedCats.add('dev');
api.renderNav();
ok(parse().dev === true, 'a section the user unfolded stays unfolded');

// ── 3. sub-sections from the manifests ────────────────────────────────────
Object.assign(api.subLabels, { composition: 'Composition', mesure: 'Mesure', medias: 'Médias',
                               control: 'Pupitres et contrôle', supervision: 'Supervision', audio: 'Audio' });
Object.assign(api.subOrder, { control: 0, supervision: 3, audio: 5 });
api.set({ servicesKnown: { tsl: { category: 'control' }, snmp: { category: 'supervision' }, tts: { category: 'audio' } } });
api.renderNav();
const nav = navEl.innerHTML;
const subs = [...nav.matchAll(/<div class="wiki-sub-hd">([^<]+)<\/div>/g)].map(m => m[1]);
ok(['Composition', 'Médias', 'Mesure'].every(s => subs.includes(s)), 'Plugins shows its sub-sections, labels from the server', subs);
// « Audio » sorts first alphabetically, last in the server's order.
ok(subs.indexOf('Pupitres et contrôle') >= 0 && subs.indexOf('Pupitres et contrôle') < subs.indexOf('Supervision')
   && subs.indexOf('Supervision') < subs.indexOf('Audio'),
   'Services sub-sections follow the server\'s order, not the alphabet', subs);
ok(!/data-cat="debug"[\s\S]*?wiki-sub-hd[\s\S]*?data-cat="dev"/.test(nav), 'a section without sub-sections gets no sub-heading');
ok(api.placeLabel(arts[7]) === 'Services › Pupitres et contrôle', 'a service article names its full place', api.placeLabel(arts[7]));
ok(api.placeLabel(arts[1]) === 'Dépannage', 'a plain article names its section only', api.placeLabel(arts[1]));

console.log(fails ? `\n✗ ${fails} failure(s)` : '\n✓ all green');
process.exit(fails ? 1 : 0);
