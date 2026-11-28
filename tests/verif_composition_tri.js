#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Composition: sort order of the instance list (templates/plugin_section.html), asked by the tester
// on 2026-11-27. Sorting by project or by capacity also GROUPS, behind drop-down headers (the
// tester's second request the same day); the two other sorts stay flat. Checked here:
//   1. 'deploy' (default) keeps the server order untouched;
//   2. 'alpha' is natural (multiview-2 before multiview-10), ties broken by vmid;
//   3. 'project' sorts by project name, instances without a project LAST, then by name;
//   4. 'inputs' puts the largest capacity first, unknown capacity (null) last;
//   5. no sort control on the page (another rubrique) → the order never changes;
//   6. the input array is never mutated (tpItems keeps being the server's list until re-render).
// The functions are taken from the page itself; the DOM is a minimal stand-in (no jsdom here).
'use strict';
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'plugin_section.html'), 'utf8');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

const a = html.indexOf('const TP_SORT_KEY'), b = html.indexOf('function tpSortRender', a);
ok(a > 0 && b > a, 'sort functions found in plugin_section.html');
if (!(a > 0 && b > a)) process.exit(1);
const src = html.slice(a, b);

function load(sortValue, hasControl) {
    const store = { 'ps-instances-sort': sortValue };
    const ctx = {
        localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
        document: { documentElement: { lang: 'fr' },
                    getElementById: id => (id === 'tp-sort' && hasControl) ? {} : null },
    };
    const fn = new Function('localStorage', 'document', src + '\nreturn [tpSortItems, tpGroups];');
    const [sortFn, groupFn] = fn(ctx.localStorage, ctx.document);
    sortFn.groups = groupFn;
    return sortFn;
}

const P = name => ({ id: 1, name });
// The data is chosen so that EVERY rule is exercised — a rule the data cannot tell apart from its
// absence is not checked, whatever the label says (review of 2026-11-27):
//   · 'aaa-mur' has NO project and sorts first by name → only the « no project last » rule puts it
//     after the projects;
//   · the two 'multiview-2' come in DESCENDING vmid order → a stable sort without the vmid
//     tie-break keeps 1431 before 1407.
const items = [
    { vmid: 1420, hostname: 'multiview-10', project: null, n_inputs: 4 },
    { vmid: 1431, hostname: 'multiview-2', project: null, n_inputs: 9 },
    { vmid: 1406, hostname: 'multiview', project: P('Atelier'), n_inputs: 16 },
    { vmid: 1407, hostname: 'multiview-2', project: P('Atelier'), n_inputs: null },
    { vmid: 1430, hostname: 'Régie B', project: P('Antenne'), n_inputs: 9 },
    { vmid: 1440, hostname: 'aaa-mur', project: null, n_inputs: 2 },
];
const names = l => l.map(x => x.hostname + '#' + x.vmid);
const before = names(items);

ok(names(load('deploy', true)(items)).join() === before.join(), "'deploy' keeps the server order");
ok(names(load(null, true)(items)).join() === before.join(), 'no stored choice → deployment order');
ok(names(load('bogus', true)(items)).join() === before.join(), 'an unknown stored value is ignored');

const alpha = names(load('alpha', true)(items));
ok(alpha.join() === ['aaa-mur#1440', 'multiview#1406', 'multiview-2#1407', 'multiview-2#1431', 'multiview-10#1420', 'Régie B#1430'].join(),
   "'alpha' is natural and ties go by vmid", alpha);

const proj = names(load('project', true)(items));
ok(proj.join() === ['Régie B#1430', 'multiview#1406', 'multiview-2#1407', 'aaa-mur#1440', 'multiview-2#1431', 'multiview-10#1420'].join(),
   "'project': by project name, no project last", proj);

const inp = names(load('inputs', true)(items));
ok(inp.join() === ['multiview#1406', 'multiview-2#1431', 'Régie B#1430', 'multiview-10#1420', 'aaa-mur#1440', 'multiview-2#1407'].join(),
   "'inputs': largest first, unknown last", inp);

ok(names(load('alpha', false)(items)).join() === before.join(), 'no sort control on the page → order unchanged');
ok(names(items).join() === before.join(), 'the input list is never mutated');

// Drop-down groups (second request of 2026-11-27): one group per project / per input count, in the
// sorted order, each holding its instances; flat sorts give a single header-less group.
const shape = gs => gs.map(g => g.label + ':' + g.items.map(x => x.vmid).join('/'));
{
    const s = load('project', true);
    const gs = shape(s.groups(s(items)));
    ok(gs.join() === ['Antenne:1430', 'Atelier:1406/1407', 'section.sort.group_no_project:1440/1431/1420'].join(),
       'project groups: by name, no-project group last', gs);
}
{
    const s = load('inputs', true);
    const gs = shape(s.groups(s(items)));
    ok(gs.join() === ['section.sort.group_inputs_other:1406', 'section.sort.group_inputs_other:1431/1430',
                      'section.sort.group_inputs_other:1420', 'section.sort.group_inputs_other:1440',
                      'section.sort.group_inputs_unknown:1407'].join(),
       'inputs groups: one per count, largest first, unknown last', gs);
    const keys = s.groups(s(items)).map(g => g.key);
    ok(new Set(keys).size === keys.length, 'one group per distinct value', keys);
}
for (const mode of ['deploy', 'alpha']) {
    const s = load(mode, true);
    const gs = s.groups(s(items));
    ok(gs.length === 1 && gs[0].key === null && gs[0].items.length === items.length,
       `'${mode}' stays a flat list (no header)`);
}
{
    const s = load('project', false);
    ok(s.groups(items).length === 1 && s.groups(items)[0].key === null, 'no sort control → no groups');
}

process.exit(fails ? 1 : 0);
