#!/usr/bin/env node
// Offline check of the project Resources window (templates/projects.html), the view chosen with
// Cyril on 2026-11-07 (mock-up D): every node's cores drawn whole, proposals drawn ON the schema.
//
// ★ WHAT THIS GUARD HOLDS:
//  1. a calibrated on-air container gets its proposal by default (the cores covering its peak);
//  2. ★ a resize in place draws the extra cores HATCHED in the container's colour, taken from the
//     node's dedicable shared cores, and the node says « shared a → b »;
//  3. ★ a move draws the leaving cores DOTTED on the source and the arriving ones hatched on the
//     target — the source's shared count grows, the target's shrinks;
//  4. a node without room says « n cores short » instead of drawing cores it does not have;
//  5. the plan lists a move with its cost (recreation) and a resize as hot;
//  6. « To handle » keeps on-air containers not yet guaranteed AND any container with a change;
//  7. the common hostname prefix of the project is dropped for display;
//  8. ★ a row and its cores carry the same vmid (cross highlight on hover), and a core of another
//     project names its container in its tooltip.
//
//   node tests/verif_projet_plan_vue.js
//
// MUTATION, PLAN_VUE_MUTATION=<name> — must give rc=1:
//   sans_hachure   a taken core is drawn as an existing one
//   depart_muet    a move no longer marks the source cores as leaving

const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'projects.html'), 'utf8');
const a = html.indexOf('const PROJ_RES_BADGE = {');
const b = html.indexOf('async function _resUpdateCampaign() {');
const c = html.indexOf('function closePorts()');
if (a < 0 || b < 0 || c < 0) { console.log('  ✗ code de la fenêtre introuvable dans projects.html'); process.exit(1); }
let code = html.slice(a, c);
const MUT = process.env.PLAN_VUE_MUTATION || '';
const MUTS = {
    sans_hachure: ["x.kind = 'project'; x.vmid = ch.vmid; x.state = 'taken';", "x.kind = 'project'; x.vmid = ch.vmid; x.state = 'full';"],
    depart_muet: [".forEach(x => { x.state = 'leaving'; });", ".forEach(x => {});"],
};
if (MUT) {
    const [o, n] = MUTS[MUT] || [];
    if (!o || code.split(o).length !== 2) { console.log('mutation périmée : ' + MUT); process.exit(1); }
    code = code.replace(o, n);
    console.log('MUTATION active : ' + MUT);
}

const els = {};
// Minimal DOM: an element keeps its classes, and `[data-vmid]` lookups read the rendered HTML.
function el(id) {
    const cls = new Set();
    const e = { id, innerHTML: '', textContent: '', hidden: false, _hlWired: false,
        classList: { toggle(c, on) { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); }, contains: (c) => cls.has(c) },
        addEventListener() {}, querySelector: () => null,
        querySelectorAll(sel) {
            if (sel !== '[data-vmid]') return [];
            return [...e.innerHTML.matchAll(/data-vmid="(\d+)"/g)].map((m) => {
                const k = new Set();
                return { dataset: { vmid: m[1] }, classList: { toggle(c, on) { if (on) k.add(c); else k.delete(c); }, has: (c) => k.has(c) } };
            });
        } };
    e._cls = cls;
    return e;
}
const doc = { getElementById: (id) => (els[id] = els[id] || el(id)) };
const T = (k) => k;
const sandbox = new Function('window', 'document', 'fetch', 'RES_CAN_DEPLOY', 'location', '_mEsc', 'MXLRessources', 'showWireToast', 'confirm',
    code + '\nreturn {_PROJ_RES, _resChange, _resToHandle, _resNodes, _resColors, _resRender, _resPrefix, _resShort, resMove, resCores, _resHl};');
const R = sandbox({ t: T }, doc, () => { throw new Error('aucun réseau'); }, true, { hash: '' }, (s) => String(s),
    { fetchState: async () => ({ etat: 'aucune' }), fmtChrono: () => '' }, () => {}, () => true);

let n = 0; const echecs = [];
function verifie(ok, titre, detail) {
    n++;
    console.log((ok ? '  ✓ ' : '  ✗ ') + titre + (!ok && detail !== undefined ? '  — ' + JSON.stringify(detail) : ''));
    if (!ok) echecs.push(titre);
}

// Décor : nœud A (8 cœurs : 2 au mixer du projet, 3 à d'autres, 3 libres), nœud B (6 cœurs, 6 libres).
const cell = (kind, vmid) => ({ cpu: 0, kind, vmid: vmid || null });
const J = {
    verdict: 'not_guaranteed', counts: { stopped: 0, undersized: 1, reserved_unmeasured: 0, shared: 0, guaranteed: 0 },
    nodes_without_pool: [], suggestions: [],
    nodes: [
        { id: 1, name: 'A', pool: true, dedicable: 3, cells: [cell('project', 10), cell('project', 10), cell('other'), cell('other'), cell('other'), cell('free'), cell('free'), cell('free')] },
        { id: 2, name: 'B', pool: true, dedicable: 6, cells: Array.from({ length: 6 }, () => cell('free')) },
    ],
    containers: [
        { vmid: 10, hostname: 'Projet-Atelier-rl-mix', type: 'mixer', node_id: 1, required: true, guarantee: 'undersized', alloc: 2, proposed: 3, level: 'dedicated', mesure: { max: 220 } },
        { vmid: 11, hostname: 'Projet-Atelier-rl-cam1', type: 'avsync', node_id: 1, required: false, guarantee: 'shared', alloc: 0, proposed: null, level: 'normal' },
    ],
};
const P = R._PROJ_RES;
P.data = J; P.pid = 7; P.filter = 'all'; P.type = '';
P.plan = { 10: { cores: 3, node: 1 } };                 // ce que loadResources pose : la proposition
P.prefix = R._resPrefix(J.containers.map((c) => c.hostname));

const coul = R._resColors(J);
let [na, nb] = R._resNodes(J);
const etats = (nd, v) => nd.view.filter((x) => x.vmid === v).map((x) => x.state);
verifie(JSON.stringify(etats(na, 10)) === JSON.stringify(['full', 'full', 'taken']),
    '2a. ★ agrandi sur place : 2 cœurs en place, 1 HACHURÉ pris sur le partagé', etats(na, 10));
verifie(na.sharedBefore === 3 && na.sharedAfter === 2 && !na.missing, '2b. le nœud dit « partagé 3 → 2 »', [na.sharedBefore, na.sharedAfter]);

R.resMove(10, 2);
[na, nb] = R._resNodes(J);
verifie(JSON.stringify(etats(na, 10)) === JSON.stringify(['leaving', 'leaving']), '3a. ★ déplacé : ses 2 cœurs du nœud A passent en POINTILLÉ', etats(na, 10));
verifie(JSON.stringify(etats(nb, 10)) === JSON.stringify(['taken', 'taken', 'taken']), '3b. ★ … et 3 cœurs hachurés arrivent sur B', etats(nb, 10));
verifie(na.sharedAfter === 5 && nb.sharedAfter === 3, '3c. partagé : A 3 → 5, B 6 → 3', [na.sharedAfter, nb.sharedAfter]);

P.plan = { 10: { cores: 9, node: 2 } };
[na, nb] = R._resNodes(J);
verifie(nb.missing === 3, '4. 9 cœurs sur un nœud de 6 → « manque 3 », pas de cœurs inventés', nb.missing);

P.plan = { 10: { cores: 3, node: 2 } };
R._resRender();
const out = els['proj-res-body'].innerHTML;
verifie(/projects\.res_plan_move/.test(out) && /projects\.res_cost_cut/.test(out), '5a. le plan liste le déplacement, coût « recréation »');
P.plan = { 10: { cores: 3, node: 1 } };
R._resRender();
verifie(/projects\.res_plan_resize/.test(els['proj-res-body'].innerHTML) && /projects\.res_cost_hot/.test(els['proj-res-body'].innerHTML),
    '5b. un agrandissement sur place est « à chaud »');

const tr = R._resToHandle(J).map((c) => c.vmid);
verifie(JSON.stringify(tr) === JSON.stringify([10]), '6. « À traiter » : le conteneur Dédié non garanti, pas celui au mieux', tr);
P.plan[11] = { cores: 1, node: 2 };
verifie(R._resToHandle(J).map((c) => c.vmid).includes(11), '6b. … et tout conteneur qui a un changement en attente');

// 8. Repérage croisé : la ligne et les cœurs d'un même conteneur portent son vmid.
J.nodes[0].cells[2].owner = 'autre-mv';
P.plan = { 10: { cores: 3, node: 1 } };
R._resRender();
const h = els['proj-res-body'].innerHTML;
verifie(/<tr data-vmid="10"/.test(h) && (h.match(/proj-core[^"]*" style="--c:[^"]*" data-vmid="10"/g) || []).length === 3,
    '8a. ★ la ligne du mixer et ses 3 cœurs (2 en place + 1 proposé) portent son vmid', (h.match(/data-vmid="10"/g) || []).length);
verifie(/title="CPU 0 · autre-mv"/.test(h), '8b. un cœur d\'un autre projet nomme son conteneur au survol');
R._resHl(10);
verifie(els['proj-res-body']._cls.has('hl-on'), '8c. survol → la fenêtre passe en mode repérage');
R._resHl(null);
verifie(!els['proj-res-body']._cls.has('hl-on'), '8d. fin du survol → plus rien d\'estompé');

verifie(R._resShort('Projet-Atelier-rl-mix') === 'mix', '7. le préfixe commun du projet est ôté à l\'affichage, coupé sur un tiret', R._resShort('Projet-Atelier-rl-mix'));
verifie(R._resPrefix(['seul-conteneur']) === '', '7b. un projet d\'un seul conteneur garde son nom entier');

console.log('\n' + (echecs.length ? '✗ ' + echecs.length + ' échec(s) sur ' + n : '✓ ' + n + ' vérifications'));
process.exit(echecs.length ? 1 : 0);
