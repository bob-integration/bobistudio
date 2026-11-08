#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Help page: no-break space before « : ; ! ? » and inside « » (2026-11-08). « erreurs : en cas de
// doute » left the colon alone at the start of a line. Fixed at DISPLAY time (typoFr in
// templates/aide.html) for the hand-written articles, the loaded documents and the changelog:
//   1. a space before : ; ! ? » and after « becomes a no-break space;
//   2. code, pre, kbd are left alone;
//   3. English text (no space before the sign) is untouched;
//   4. it runs on the article, on each loaded document and on the changelog.
// The DOM below is a minimal stand-in (no jsdom here): element tree + text nodes + TreeWalker.
'use strict';
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'aide.html'), 'utf8');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

const start = html.indexOf('const TYPO_SKIP');
const end = html.indexOf('\n}\n', html.indexOf('function typoFr(')) + 3;
ok(start > 0 && end > start, 'typoFr found in aide.html');
const src = html.slice(start, end);

// ── minimal DOM ───────────────────────────────────────────────────────────
function el(tag, ...kids) {
    const e = { tagName: tag, children: [], parentElement: null };
    for (const k of kids) {
        const n = typeof k === 'string' ? { nodeValue: k, text: true } : k;
        n.parentElement = e;
        e.children.push(n);
    }
    return e;
}
const NodeFilter = { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 };
const document = {
    createTreeWalker(root, _what, filter) {
        const list = [];
        (function walk(e) { for (const c of e.children) { if (c.text) { if (filter.acceptNode(c) === 1) list.push(c); } else walk(c); } })(root);
        let i = -1;
        return { nextNode() { i++; this.currentNode = list[i]; return i < list.length ? list[i] : null; } };
    },
};
const typoFr = new Function('document', 'NodeFilter', src + '\nreturn typoFr;')(document, NodeFilter);

const NB = ' ';
const p = el('P', 'des erreurs : en cas de doute ; vraiment ? oui ! « citation »');
const code = el('CODE', 'a ? b : c');
const pre = el('PRE', el('SPAN', 'x ? y : z'));
const en = el('P', 'Note: this is English; really? yes!');
const root = el('DIV', p, code, pre, en);
typoFr(root);
const t = p.children[0].nodeValue;
ok(t === 'des erreurs' + NB + ': en cas de doute' + NB + '; vraiment' + NB + '? oui' + NB + '! «' + NB + 'citation' + NB + '»',
   'French text: no-break spaces', t);
ok(code.children[0].nodeValue === 'a ? b : c', 'code left alone', code.children[0].nodeValue);
ok(pre.children[0].children[0].nodeValue === 'x ? y : z', 'pre (and what it contains) left alone');
ok(en.children[0].nodeValue === 'Note: this is English; really? yes!', 'English untouched');

ok(/typoFr\(contentEl\.querySelector\('\.wiki-article'\)\);\s*buildToc\(\);/.test(html), 'runs on the article when shown');
ok((html.match(/typoFr\(target\);\s*if \(target\.isConnected/g) || []).length === 2,
   'runs on each loaded document and on the changelog');

console.log(fails ? '\nÉCHECS : ' + fails : '\nOK');
process.exit(fails ? 1 : 0);
