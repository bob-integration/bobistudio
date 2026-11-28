#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Tab strips: the active tab is bold, and a bold label is wider — without a reserved width it
// pushes its neighbours a few pixels at every click (seen 2026-11-28 on the strip under the top
// bar: Monitoring, plugin sections, 2110 probe, operations journal, Containers). The cure is an
// invisible bold twin of the label (`data-label`, base.css), set by static/subnav_deck.js.
//   1. CSS: every strip whose active tab is bold reserves that width, at the same weight;
//   2. every page with such a strip loads the script that sets `data-label`;
//   3. reserve(): text-only tabs get the label and a span, richer tabs are left alone, twice is
//      once, and a tab added later is caught by the observer;
//   4. a `.tabs-line` (not handled by the script) is built with its `data-label`.
// The DOM below is a minimal stand-in (no jsdom here).
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const read = p => fs.readFileSync(path.join(ROOT, p), 'utf8');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c || d === undefined ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

// ── 1. CSS ────────────────────────────────────────────────────────────────
const css = read('static/css/base.css').replace(/\/\*[\s\S]*?\*\//g, '');
const rules = [];
for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) rules.push({ sels: m[1].split(',').map(s => s.trim().replace(/\s+/g, ' ')), body: m[2] });
const weightOf = body => { const m = /font-weight:\s*([0-9a-z]+)/.exec(body); return m ? m[1] : null; };
const bold = w => w === 'bold' || parseInt(w, 10) >= 600;
const strips = new Map();            // "<strip> > .tab" -> weight of its active tab
for (const r of rules) {
    const w = weightOf(r.body);
    if (!w || !bold(w)) continue;
    for (const s of r.sels) { const m = /^(\.[\w-]+) > \.tab\.active$/.exec(s); if (m) strips.set(m[1], w); }
}
ok(strips.has('.subnav-deck') && strips.has('.tabs-line') && strips.has('.navpair-deck'),
   'the three strips with a bold active tab are found', [...strips.keys()]);
const has = (sel, test) => rules.some(r => r.sels.includes(sel) && test(r.body));
for (const [strip, w] of strips) {
    const t = strip + ' > .tab[data-label]';
    ok(has(t, b => /display:\s*inline-grid/.test(b)), strip + ': labelled tab is a grid');
    ok(has(t + '::before', b => /content:\s*attr\(data-label\)/.test(b) && weightOf(b) === w && /visibility:\s*hidden/.test(b)),
       strip + ': invisible twin of the label at weight ' + w);
    ok(has(t + '::before', b => /grid-area:\s*1 \/ 1/.test(b)) && has(t + ' > span', b => /grid-area:\s*1 \/ 1/.test(b)),
       strip + ': twin and label share one cell');
}

// ── 2. pages ──────────────────────────────────────────────────────────────
const tpl = fs.readdirSync(path.join(ROOT, 'templates')).filter(f => f.endsWith('.html'));
const pages = tpl.filter(f => /class="[^"]*\b(subnav-deck|navpair-deck)\b/.test(read('templates/' + f)));
ok(pages.length >= 6, 'pages with a strip found', pages);
for (const f of pages) ok(/src="\/static\/subnav_deck\.js/.test(read('templates/' + f)), f + ' loads subnav_deck.js');

// ── 3. reserve() ──────────────────────────────────────────────────────────
function tab(text, kids) {
    const e = { dataset: {}, children: kids || [], _text: text,
        get textContent() { return this.children.length ? this.children.map(c => c.textContent).join('') : this._text; },
        set textContent(v) { this._text = v; this.children = []; },
        appendChild(c) { this.children.push(c); } };
    return e;
}
const tabs = [tab('  Vue d’ensemble '), tab('Nœuds'), tab('', [{ textContent: 'ico' }, { textContent: 'Mur' }]), tab('   ')];
let observed = null, observedOn = 0;
const document = {
    readyState: 'complete',
    querySelectorAll: sel => (/\.tab/.test(sel) ? tabs : sel.includes('.navpair-items') ? [{}, {}] : []),
    querySelector: () => null,
    createElement: () => tab(''),
    addEventListener() {},
};
const window = { addEventListener() {},
    MutationObserver: function (cb) { observed = cb; this.observe = (_d, o) => { if (o && o.childList) observedOn++; }; } };
new Function('document', 'window', read('static/subnav_deck.js'))(document, window);

ok(tabs[0].dataset.label === 'Vue d’ensemble' && tabs[0].children.length === 1 && tabs[0].children[0].textContent === 'Vue d’ensemble',
   'text-only tab: trimmed label + one span', tabs[0].dataset);
ok(tabs[1].dataset.label === 'Nœuds', 'second tab too');
ok(tabs[2].dataset.label === undefined && tabs[2].children.length === 2, 'tab with elements left alone');
ok(tabs[3].dataset.label === undefined, 'empty tab left alone');
const span = tabs[0].children[0];
window.subnavDeckReserve();
ok(tabs[0].children.length === 1 && tabs[0].children[0] === span, 'twice is once');
ok(typeof observed === 'function' && observedOn >= 1, 'the strips are observed for added tabs', observedOn);
tabs.push(tab('Murs'));
if (observed) observed();
ok(tabs[4].dataset.label === 'Murs', 'a tab added later is reserved');

// ── 4. .tabs-line ─────────────────────────────────────────────────────────
for (const f of tpl) {
    const s = read('templates/' + f);
    const i = s.search(/class="[^"]*\btabs-line\b/);
    if (i < 0) continue;
    ok(/class="tab"[^>]*data-label=/.test(s.slice(i, i + 600)), f + ': .tabs-line tabs are built with data-label');
}

console.log(fails ? '\nÉCHECS : ' + fails : '\nOK');
process.exit(fails ? 1 : 0);
