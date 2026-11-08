#!/usr/bin/env node
// Offline check of the drag-and-drop on the node map of the project Resources window
// (templates/projects.html, asked on 2026-11-07: « I tried to drag a container onto another node,
// nothing happened »).
//
// ★ WHAT THIS GUARD HOLDS:
//  1. a container that can move is draggable from its cells AND from the grip of its row; node I/O
//     (a DeckLink card, physically in its node), a type the migration refuses and a stopped
//     container are not — and their tooltip says why;
//  2. every node of the map is a drop target (data-node);
//  3. ★ a drop on the node it is already on does nothing (no request, plan unchanged);
//  4. ★ a drop elsewhere goes into the plan like a suggestion and is CHECKED right away by the route
//     of « Apply » with `check: true` — never applied from the drop;
//  5. ★ a refused drop (no room…) is taken back and its reason is shown;
//  6. dropping it back on its own node cancels the move, without a request;
//  7. ★ the gesture is made of POINTER events (mouse, pen, touch): a press + a move of a few pixels
//     + a release over a node = a drop; a press without moving stays a click.
//
//   node tests/verif_ressources_glisser.js
//
// MUTATIONS, GLISSER_MUTATION=<name> — each must give rc=1:
//   bloque_ignore   a container that cannot move is draggable anyway
//   meme_noeud      a drop on its own node is no longer ignored
//   sans_check      the drop no longer asks for a check only (it would apply)
//   refus_garde     a refused drop stays in the plan
//   clic_glisse     a press without moving starts a drag

const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'projects.html'), 'utf8');
const a = html.indexOf('const PROJ_RES_BADGE = {');
const c = html.indexOf('function closePorts()');
if (a < 0 || c < 0) { console.log('  ✗ code de la fenêtre introuvable dans projects.html'); process.exit(1); }
let code = html.slice(a, c);
const MUT = process.env.GLISSER_MUTATION || '';
const MUTS = {
    bloque_ignore: ['const drag = RES_CAN_DEPLOY && !block;', 'const drag = RES_CAN_DEPLOY;'],
    meme_noeud: ["if (node === ((p && p.node) || c.node_id)) return false;", ''],
    sans_check: ['JSON.stringify({check: true, changes:', 'JSON.stringify({changes:'],
    refus_garde: ['if (before) _PROJ_RES.plan[vmid] = before; else delete _PROJ_RES.plan[vmid];', ''],
    clic_glisse: ['Math.hypot(ev.clientX - d.x, ev.clientY - d.y) < 6', 'false'],
};
if (MUT) {
    const [o, n] = MUTS[MUT] || [];
    if (!o || code.split(o).length !== 2) { console.log('mutation périmée : ' + MUT); process.exit(1); }
    code = code.replace(o, n);
    console.log('MUTATION active : ' + MUT);
}

// ── Minimal DOM ──
const listeners = {};          // window
const bodyL = {};              // #proj-res-body
const els = {};
function classes() {
    const s = new Set();
    return { add: (...k) => k.forEach((x) => s.add(x)), remove: (...k) => k.forEach((x) => s.delete(x)),
             toggle(k, on) { if (on === undefined ? !s.has(k) : on) s.add(k); else s.delete(k); },
             contains: (k) => s.has(k), has: (k) => s.has(k) };
}
function el(id) {
    const e = { id, innerHTML: '', textContent: '', hidden: false, classList: classes(),
        addEventListener(t, f) { if (id === 'proj-res-body') bodyL[t] = f; },
        querySelector: () => null, querySelectorAll: () => [], contains: () => true };
    return e;
}
const nodeEls = { 1: { dataset: { node: '1' }, classList: classes() }, 2: { dataset: { node: '2' }, classList: classes() } };
let under = null;              // node element under the pointer
const doc = {
    getElementById: (id) => (els[id] = els[id] || el(id)),
    createElement: () => ({ className: '', textContent: '', style: { setProperty() {} }, remove() {} }),
    body: { appendChild() {} },
    elementFromPoint: () => (under ? { closest: () => under } : { closest: () => null }),
};
const win = { t: (k) => k,
    addEventListener(t, f) { listeners[t] = f; }, removeEventListener(t, f) { if (listeners[t] === f) delete listeners[t]; } };
const calls = [], toasts = [];
let reply = { status: 200, body: { ok: true, plan: [], errors: [] } };
const fakeFetch = async (url, opt) => {
    calls.push({ url, body: opt && opt.body ? JSON.parse(opt.body) : null });
    return { ok: reply.status < 300, status: reply.status, json: async () => reply.body };
};
const sandbox = new Function('window', 'document', 'fetch', 'RES_CAN_DEPLOY', 'location', '_mEsc', 'MXLRessources', 'showWireToast', 'confirm',
    code + '\nreturn {_PROJ_RES, _resRender, _resPrefix, resDrop, _resDragWire};');
const R = sandbox(win, doc, fakeFetch, true, { hash: '' }, (s) => String(s),
    { fetchState: async () => ({ etat: 'aucune' }), fmtChrono: () => '' }, (m, k) => toasts.push([m, k]), () => true);

let n = 0; const echecs = [];
function verifie(ok, titre, detail) {
    n++;
    console.log((ok ? '  ✓ ' : '  ✗ ') + titre + (!ok && detail !== undefined ? '  — ' + JSON.stringify(detail) : ''));
    if (!ok) echecs.push(titre);
}

// Décor : A (mixer 10 sur 2 cœurs, carte SDI 12 sur 2, pyramide 13 sur 1, arrêté 14 sur 1), B libre.
const cell = (kind, vmid) => ({ cpu: 0, kind, vmid: vmid || null });
const J = {
    verdict: 'no_dedicated', counts: { stopped: 0, undersized: 0, reserved_unmeasured: 0, shared: 0, guaranteed: 0 },
    nodes_without_pool: [], suggestions: [],
    nodes: [
        { id: 1, name: 'A', pool: true, dedicable: 1, cells: [cell('project', 10), cell('project', 10), cell('io', 12), cell('io', 12),
                                                            cell('project', 13), cell('project', 14), cell('free')] },
        { id: 2, name: 'B', pool: true, dedicable: 4, cells: Array.from({ length: 4 }, () => cell('free')) },
    ],
    containers: [
        { vmid: 10, hostname: 'Atelier-mix', type: 'mixer', node_id: 1, required: false, guarantee: 'shared', alloc: 2, proposed: null, level: 'normal', move_block: null },
        { vmid: 12, hostname: 'Atelier-sdi', type: 'decklink', node_id: 1, required: false, guarantee: 'shared', alloc: 2, proposed: null, level: 'normal', node_io: true, move_block: 'node_io' },
        { vmid: 13, hostname: 'Atelier-pyr', type: 'pyramide', node_id: 1, required: false, guarantee: 'shared', alloc: 1, proposed: null, level: 'normal', move_block: 'type' },
        { vmid: 14, hostname: 'Atelier-off', type: 'mixer', node_id: 1, required: false, guarantee: 'stopped', alloc: 1, proposed: null, level: 'normal', move_block: 'stopped' },
    ],
};
const P = R._PROJ_RES;
P.data = J; P.pid = 7; P.filter = 'all'; P.type = ''; P.plan = {};
P.prefix = R._resPrefix(J.containers.map((x) => x.hostname));
R._resRender();
let h = els['proj-res-body'].innerHTML;

console.log('1. Ce qui se glisse, ce qui ne se glisse pas');
verifie((h.match(/data-drag="10"/g) || []).length === 3, '1a. ★ le mixer se glisse depuis ses 2 cœurs ET la poignée de sa ligne',
    (h.match(/data-drag="10"/g) || []).length);
verifie(!/data-drag="12"/.test(h), '1b. ★ la carte SDI (entrée-sortie du nœud) ne se glisse pas');
verifie(/data-vmid="12" title="[^"]*projects\.res_move_block\.node_io"/.test(h), '1c. ★ … et son infobulle dit pourquoi');
verifie(!/data-drag="13"/.test(h) && /data-vmid="13" title="[^"]*projects\.res_move_block\.type"/.test(h),
    '1d. un type que la migration refuse (pyramide) ne se glisse pas, et le dit');
verifie(!/data-drag="14"/.test(h) && /data-vmid="14" title="[^"]*projects\.res_move_block\.stopped"/.test(h),
    '1e. un conteneur arrêté ne se glisse pas, et le dit');
verifie(/class="proj-node" data-node="1"/.test(h) && /class="proj-node" data-node="2"/.test(h), '2. chaque nœud de la carte reçoit un dépôt');
verifie(/onchange="resDrop\(10, /.test(h), '2b. sans geste : le choix « Déplacer vers… » de la ligne passe par le même chemin');

(async () => {
    console.log('3. Déposer');
    let ok = await R.resDrop(10, 1);
    verifie(ok === false && calls.length === 0 && !P.plan[10], '3a. ★ déposé sur son propre nœud : rien, aucune requête', { ok, calls, plan: P.plan });
    ok = await R.resDrop(12, 2);
    verifie(ok === false && calls.length === 0 && !P.plan[12] && toasts.some(([m]) => m === 'projects.res_move_block.node_io'),
        '3b. ★ la carte SDI déposée ailleurs : refus qui dit pourquoi, aucune requête', { calls, toasts });

    reply = { status: 409, body: { ok: false, plan: [], errors: [{ node_id: 2, name: 'B', error: 'capacity', need: 6, available: 4 }] } };
    ok = await R.resDrop(10, 2);
    verifie(calls.length === 1 && calls[0].url === '/api/projects/7/apply', '4a. ★ la vérification passe par la route de « Appliquer »', calls);
    verifie(calls[0] && calls[0].body.check === true, '4b. ★ … en vérification seule (check: true), jamais appliquée depuis le dépôt', calls[0]);
    verifie(calls[0] && JSON.stringify(calls[0].body.changes) === JSON.stringify([{ vmid: 10, cores: 2, node_id: 2 }]),
        '4c. le plan vérifié : le mixer vers B avec ses 2 cœurs', calls[0] && calls[0].body);
    verifie(ok === false && !P.plan[10], '5a. ★ refusé : le dépôt est repris, le plan est comme avant', P.plan);
    verifie(toasts.some(([m, k]) => k === 'error' && /projects\.res_refused_capacity/.test(m)), '5b. ★ … et la raison est affichée', toasts);

    reply = { status: 200, body: { ok: true, plan: [], errors: [] } };
    ok = await R.resDrop(10, 2);
    verifie(ok === true && P.plan[10] && P.plan[10].node === 2, '4d. ★ accepté : le déplacement entre dans le plan', P.plan);
    h = els['proj-res-body'].innerHTML;
    verifie(/projects\.res_plan_move/.test(h) && /resApply\(\)/.test(h), '4e. il s\'affiche dans le plan, appliqué par « Appliquer » (et sa confirmation)');
    const avant = calls.length;
    ok = await R.resDrop(10, 1);
    verifie(ok === true && calls.length === avant && !(P.plan[10] && P.plan[10].node === 2) && !/projects\.res_plan_move/.test(els['proj-res-body'].innerHTML),
        '6. reposé sur son nœud d\'origine : le déplacement est annulé, sans requête', P.plan);

    console.log('7. Le geste : événements pointeur');
    P.plan = {};
    R._resRender();
    const body = els['proj-res-body'];
    const press = (drag) => bodyL.pointerdown({ pointerId: 1, pointerType: 'touch', button: 0, clientX: 100, clientY: 100,
        target: { closest: (s) => (s === '[data-drag]' && drag ? { dataset: { drag: String(drag) } } : null) } });
    const ev = (t, x, y) => listeners[t] && listeners[t]({ type: t, pointerId: 1, clientX: x, clientY: y, preventDefault() {} });
    verifie(typeof bodyL.pointerdown === 'function', '7a. la fenêtre écoute pointerdown (souris, stylet, doigt)');
    let n0 = calls.length;
    press(10); under = nodeEls[2];
    ev('pointermove', 102, 101); ev('pointerup', 102, 101);
    await new Promise((r) => setTimeout(r, 0));
    verifie(calls.length === n0 && !P.plan[10], '7b. ★ appuyer sans bouger reste un clic (le repérage), pas un dépôt', calls.slice(n0));
    press(10); under = null;
    ev('pointermove', 140, 150);
    verifie(body.classList.contains('res-dragging'), '7c. au-delà de quelques pixels, le glisser commence');
    under = nodeEls[2];
    ev('pointermove', 160, 180);
    verifie(nodeEls[2].classList.contains('drop-ok'), '7d. le nœud survolé s\'allume comme cible');
    ev('pointerup', 160, 180);
    await new Promise((r) => setTimeout(r, 0));
    verifie(!body.classList.contains('res-dragging') && !nodeEls[2].classList.contains('drop-ok'), '7e. relâché : plus rien d\'allumé');
    verifie(calls.length === n0 + 1 && calls[n0].body.check === true && P.plan[10] && P.plan[10].node === 2,
        '7f. ★ relâché sur B : c\'est un dépôt vers B (vérifié, dans le plan)', calls.slice(n0));
    n0 = calls.length;
    press(null); ev('pointermove', 200, 200); ev('pointerup', 200, 200);
    verifie(calls.length === n0, '7g. appuyer ailleurs que sur un conteneur ne glisse rien');

    console.log('\n' + (echecs.length ? '✗ ' + echecs.length + ' échec(s) sur ' + n : '✓ ' + n + ' vérifications'));
    process.exit(echecs.length ? 1 : 0);
})().catch((e) => { console.log('  ✗ exception : ' + (e && e.stack || e)); process.exit(1); });
