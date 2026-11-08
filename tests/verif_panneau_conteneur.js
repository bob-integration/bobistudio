#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Guard: the shared « container settings » panel (static/js/container_panel.js, 2026-11-07).
//
// Why: the same panel is opened from a plugin page's ⚙ and from a Containers card's menu, and it
// changes what costs a cut (CPU cap, memory: recreation) next to what does not (level, dedicated
// cores: hot). What is held, on the REAL file with a stubbed DOM and server:
//  1. ★ a level change sends {cpu_weight}; a 409 no_room shows the formatted message
//     (projects.res_level_no_room) and the control goes BACK to the level the container keeps;
//  2. a dedicated container gets − n + sending {cores}, « {free} libres sur {node} », its cores;
//  3. ★ a shared one shows its CPU cap (« Aucun ») and ASKS before a change that recreates;
//     declined → nothing sent; accepted → {cores:n} and « recréation en cours » said after;
//  3bis. ★ a set cap can be REMOVED (« Aucun plafond » → {cores:0}), asked first like a change;
//  4. ★ a DeckLink card (node_io) and a 2110 engine get no level control;
//  5. incoherent reservation → warning + Recréer (confirmed POST);
//  6. links: node cores window, project Resources (/projects#resources-<id>);
//  7. read-only without the deploy right; plugin settingsActions rendered and run;
//  8. ★ a deployed card's menu opens the panel (Configurer stays for a first deployment).
//
//   node tests/verif_panneau_conteneur.js      (PANNEAU_MUTATION=<name>, each must give rc=1)
//   niveau_garde       a refused level leaves the control on the refused choice
//   niveau_cle         the level is sent under another key
//   no_room_brut       the no_room refusal is shown raw, not formatted
//   plafond_sans_question  the CPU cap is sent without asking
//   moteur_niveau      the engine gets the level control
//   retrait_cle        « no cap » is sent as something else than {cores:0}
//   retrait_absent     a set cap offers no way to remove it
//   retrait_sans_question  removing the cap does not ask first
//   menu_configurer    a deployed card's menu keeps « Configurer » instead of the panel
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const RAC = path.join(__dirname, '..');
let JS = fs.readFileSync(path.join(RAC, 'static', 'js', 'container_panel.js'), 'utf8');
let SCRIPTS = fs.readFileSync(path.join(RAC, 'static', 'scripts.js'), 'utf8');
const MUT = process.env.PANNEAU_MUTATION || '';
const MUTATIONS = {
    niveau_garde: ['js', '            ctl.set(prev);\n', ''],
    niveau_cle: ['js', 'patchResources({ cpu_weight: v })', 'patchResources({ cpu_mode: v })'],
    no_room_brut: ['js', "if (r.j && r.j.error === 'no_room') return fmt(noRoomKey, r.j);", ''],
    plafond_sans_question: ['js', "if (!window.confirm(fmt('containers.panel.res_confirm_recreate'", "if (false && !window.confirm(fmt('containers.panel.res_confirm_recreate'"],
    retrait_cle: ['js', 'setRecreating({ cores: 0 })', 'setRecreating({ reset: true })'],
    retrait_absent: ['js', 'rv.cap ? push(T(\'containers.panel.res_cap_remove\')', 'false ? push(T(\'containers.panel.res_cap_remove\')'],
    retrait_sans_question: ['js', "() => setRecreating({ cores: 0 })", "() => patchResources({ cores: 0 })"],
    moteur_niveau: ['js', '        if (rv.engine) {\n', '        if (false) {\n'],
    menu_configurer: ['scripts', '    if (dc) items.push(item(', '    if (false) items.push(item('],
};
if (MUT) {
    if (!MUTATIONS[MUT]) { console.log('mutation inconnue : ' + MUT); process.exit(2); }
    const [f, a, b] = MUTATIONS[MUT];
    const src = f === 'js' ? JS : SCRIPTS;
    if (src.split(a).length !== 2) { console.log('mutation ' + MUT + ' : motif introuvable ou ambigu — la garde est périmée'); process.exit(1); }
    if (f === 'js') JS = JS.replace(a, b); else SCRIPTS = SCRIPTS.replace(a, b);
    console.log('MUTATION active : ' + MUT);
}
const echecs = [];
function ok(cond, titre, detail) {
    console.log((cond ? '  ✓ ' : '  ✗ ') + titre + (!cond && detail !== undefined ? '  — ' + JSON.stringify(detail) : ''));
    if (!cond) echecs.push(titre);
}
const FR = JSON.parse(fs.readFileSync(path.join(RAC, 'i18n', 'fr.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(RAC, 'i18n', 'en.json'), 'utf8'));
const fmt = (k, v) => Object.keys(v).reduce((s, n) => s.split('{' + n + '}').join(String(v[n])), FR[k]);

// ─── Stubbed DOM ──────────────────────────────────────────────────────────────────────────────
class N {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {};
        this._text = ''; this._html = ''; this.hidden = false; this.value = ''; this.disabled = false;
        this.className = ''; this.listeners = {}; this.parentNode = null; this.style = {};
        this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    }
    setAttribute(k, v) {
        this.attrs[k] = String(v);
        if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
    }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
    replaceChildren(...k) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = ''; k.forEach((c) => c != null && this.appendChild(c)); }
    remove() { const p = this.parentNode; if (p) { p.children = p.children.filter((x) => x !== this); this.parentNode = null; } }
    get firstChild() { return this.children[0] || null; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    set textContent(v) { this.children = []; this._text = String(v); }
    get innerHTML() { return this._html; }
    set innerHTML(v) { this._html = String(v); this.children = []; }
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
    removeEventListener() {}
    querySelector() { return null; }
    querySelectorAll() { return []; }
    focus() {}
}
function find(node, cp, all) {
    const out = [];
    (function walk(n) { if (n.dataset && n.dataset.cp === cp) out.push(n); (n.children || []).forEach(walk); })(node);
    return all ? out : out[0] || null;
}

function sandbox() {
    const env = { envois: [], questions: [], reponse: true, rv: null, patchReplies: [], ctls: [], cores: [], recreer: null };
    const body = new N('body');
    const doc = { body, createElement: (t) => new N(t), createTextNode: (t) => { const n = new N('#text'); n._text = String(t); return n; },
                  addEventListener: (ev, fn) => { (env.docListeners = env.docListeners || []).push([ev, fn]); } };
    const win = {
        t: (k) => (k in FR ? FR[k] : k),
        hasPerm: () => true,
        BobiControls: { create(kind, el, opts) {
            const c = { kind, el, opts, sets: [], handlers: {}, cur: opts.value,
                        on(ev, fn) { this.handlers[ev] = fn; }, set(v) { this.sets.push(v); this.cur = v; }, get() { return this.cur; } };
            env.ctls.push(c); return c;
        } },
        BobiCores: { ouvrir: (id, nom) => env.cores.push([id, nom]) },
        BobiLogs: { open() {} },
        confirm: (m) => { env.questions.push(m); return env.reponse; },
    };
    async function fetchStub(url, init) {
        const m = (init && init.method) || 'GET';
        const body = init && init.body ? JSON.parse(init.body) : undefined;
        env.envois.push({ url, m, body });
        const rep = (status, j) => ({ ok: status < 300, status, json: async () => j });
        if (url === '/api/containers/42/config') return rep(200, { vmid: 42, hostname: 'mix-1', status: 'running', project_id: null,
            deploy_config: JSON.stringify({ type: env.rv && env.rv.engine ? '2110_io' : 'mixer', params: {} }) });
        if (url === '/api/containers/42/resources_view') return rep(200, JSON.parse(JSON.stringify(env.rv)));
        if (url === '/api/containers/42/resources' && m === 'PATCH') { const r = env.patchReplies.shift() || [200, { status: 'ok' }]; return rep(r[0], r[1]); }
        if (url === '/api/containers/42/egress') return rep(200, { egress: 'node', internet_possible: false, internet_refus: 'pas de route' });
        if (url === '/api/containers/42/config_snapshots') return rep(200, { snapshots: [] });
        return rep(200, {});
    }
    const ctx = { window: win, document: doc, fetch: fetchStub, console, CSS: { escape: (s) => s }, setTimeout, Promise };
    vm.createContext(ctx);
    vm.runInContext(JS, ctx);
    env.P = win.BobiContainerPanel; env.win = win; env.body = body;
    env.root = () => body.children[0];
    env.patches = () => env.envois.filter((e) => e.m === 'PATCH' && e.url.endsWith('/resources')).map((e) => e.body);
    return env;
}
const settle = () => new Promise((r) => setTimeout(r, 0)).then(() => new Promise((r) => setTimeout(r, 0)));
const LEVELS = ['dedicated', 'normal', 'background', 'batch'].map((id, i) => ({ id, value: [0, 1024, 512, 64][i] }));
const base = (o) => Object.assign({ vmid: 42, hostname: 'mix-1', type: 'mixer', engine: false, levels: LEVELS,
    level_source: 'override', node_io: false, memory: 2048, reserved: '', assigned: '', node: { id: 3, name: 'dl360-1' },
    free_dedicated: 6, applied: '', coherent: true, projects: [{ id: 7, name: 'Atelier' }], media_volume: false }, o);

(async () => {
    if (!sandbox().P) { console.log('ÉCHEC — BobiContainerPanel introuvable'); process.exit(1); }

    console.log('\n1-2. Dédié : niveau et cœurs à chaud');
    let E = sandbox();
    E.rv = base({ level: 'dedicated', dedicated: true, cores: 2, cap: null, reserved: '4-5' });
    await E.P.open(42, { hostname: 'mix-1' }); await settle();
    const R = E.root();
    const lvl = E.ctls[0];
    ok(lvl && lvl.kind === 'buttons' && lvl.opts.options.map((o) => o.label).join() === 'Dédié,Normal,Arrière-plan,Batch'
       && lvl.opts.value === 'dedicated', '1a. niveau : contrôle du catalogue à 4 choix, sur Dédié', lvl && lvl.opts);
    ok(find(R, 'cores') && find(R, 'cores').textContent === '2', '2a. cœurs dédiés affichés');
    ok(find(R, 'free') && find(R, 'free').textContent === '6 libres sur dl360-1', '2b. « 6 libres sur dl360-1 »', find(R, 'free') && find(R, 'free').textContent);
    ok(find(R, 'reserved') && find(R, 'reserved').textContent === 'Cœurs attribués\u00a0: 4-5', '2c. cœurs attribués');
    ok(!find(R, 'cap') && !find(R, 'cap-apply'), '2d. pas de plafond CPU pour un Dédié');
    E.patchReplies.push([200, { status: 'ok', cores: { cores: 3, hot: true } }]);
    find(R, 'cores-plus').onclick(); await settle();
    ok(JSON.stringify(E.patches()) === JSON.stringify([{ cores: 3 }]), '2e. + → PATCH {cores:3}, sans question', E.patches());
    ok(E.questions.length === 0, '2f. à chaud : aucune confirmation demandée', E.questions);
    ok(find(R, 'cores-minus') && typeof find(R, 'cores-minus').onclick === 'function', '2g. − présent');
    ok(!find(R, 'incoherent'), '2h. réservation cohérente : pas d\'alerte');

    console.log('\n6. Liens');
    const nl = find(R, 'node-link');
    ok(nl && nl.textContent === 'Cœurs du serveur dl360-1 →', '6a. lien « Cœurs du serveur dl360-1 → »', nl && nl.textContent);
    if (nl) nl.onclick({ preventDefault() {} });
    ok(JSON.stringify(E.cores) === JSON.stringify([[3, 'dl360-1']]), '6b. il ouvre la fenêtre des cœurs du nœud', E.cores);
    const pl = find(R, 'project-link');
    ok(pl && pl.getAttribute('href') === '/projects#resources-7' && pl.textContent === 'Ressources du projet Atelier →',
       '6c. lien vers les Ressources du projet', pl && [pl.getAttribute('href'), pl.textContent]);

    console.log('\n1. ★ Niveau refusé (no_room)');
    E = sandbox();
    E.rv = base({ level: 'normal', dedicated: false, cores: null, cap: null, coherent: false, applied: '0-31', reserved: '' });
    await E.P.open(42, {}); await settle();
    const R2 = E.root(), l2 = E.ctls[0];
    E.patchReplies.push([409, { error: 'no_room', name: 'dl360-1', need: 3, available: 1 }]);
    await l2.handlers.change('dedicated'); await settle();
    ok(JSON.stringify(E.patches()) === JSON.stringify([{ cpu_weight: 'dedicated' }]), '1b. ★ changement de niveau → PATCH {cpu_weight}', E.patches());
    const st = find(R2, 'res-status');
    const attendu = fmt('projects.res_level_no_room', { need: 3, name: 'dl360-1', available: 1 });
    ok(st && st.textContent === '✕ ' + attendu, '1c. ★ refus no_room : message formaté', st && st.textContent);
    ok(l2.sets[l2.sets.length - 1] === 'normal', '1d. ★ le contrôle revient sur le niveau gardé', l2.sets);

    console.log('\n3. ★ Partagé : plafond CPU, recréation demandée');
    ok(find(R2, 'cap') && find(R2, 'cap').textContent === 'Aucun', '3a. plafond « Aucun » affiché');
    find(R2, 'cap-input').value = '4';
    E.reponse = false; E.questions.length = 0;
    find(R2, 'cap-apply').onclick(); await settle();
    ok(E.questions.length === 1 && E.patches().length === 1, '3b. ★ refusé à la question → rien n\'est envoyé', { q: E.questions, p: E.patches() });
    ok(/recrée le conteneur/.test(E.questions[0] || ''), '3c. la question dit la recréation', E.questions[0]);
    E.reponse = true;
    E.patchReplies.push([200, { status: 'recreation_en_cours', vmid: 42 }]);
    find(R2, 'cap-apply').onclick(); await settle();
    ok(JSON.stringify(E.patches()[1]) === JSON.stringify({ cores: 4 }), '3d. accepté → PATCH {cores:4}', E.patches());
    ok(find(R2, 'res-status').textContent === FR['containers.panel.res_recreating'], '3e. « recréation en cours » dit après', find(R2, 'res-status').textContent);
    ok(find(R2, 'mem-input') && find(R2, 'mem-apply'), '3f. mémoire réglable');
    ok(!find(R2, 'cap-remove'), '3g. aucun plafond posé : rien à retirer');

    console.log('\n3bis. ★ Retirer le plafond CPU');
    const E3 = sandbox();
    E3.rv = base({ level: 'normal', dedicated: false, cores: null, cap: 4 });
    await E3.P.open(42, {}); await settle();
    const R3 = E3.root(), rm = find(R3, 'cap-remove');
    ok(rm && rm.textContent === FR['containers.panel.res_cap_remove'], '3bis-a. ★ plafond posé : « Aucun plafond » proposé', rm && rm.textContent);
    E3.reponse = false; E3.questions.length = 0;
    if (rm) { rm.onclick(); await settle(); }
    ok(E3.questions.length === 1 && E3.patches().length === 0, '3bis-b. ★ refusé à la question → rien n\'est envoyé', { q: E3.questions, p: E3.patches() });
    E3.reponse = true;
    E3.patchReplies.push([200, { status: 'recreation_en_cours', vmid: 42 }]);
    if (rm) { rm.onclick(); await settle(); }
    ok(E3.questions.length === 2 && JSON.stringify(E3.patches()) === JSON.stringify([{ cores: 0 }]),
       '3bis-c. ★ accepté → PATCH {cores:0} (et rien d\'autre)', E3.patches());
    ok(find(R3, 'res-status').textContent === FR['containers.panel.res_recreating'], '3bis-d. « recréation en cours » dit après', find(R3, 'res-status').textContent);

    console.log('\n5. Réservation pas appliquée');
    const inc = find(R2, 'incoherent');
    ok(inc && /pas appliquée/.test(inc.textContent) && find(inc, 'recreate'), '5a. alerte + Recréer', inc && inc.textContent);
    E.questions.length = 0;
    if (inc) { find(inc, 'recreate').onclick(); await settle(); }
    const rec = E.envois.find((e) => e.url === '/api/containers/42/recreate');
    ok(E.questions.length === 1 && rec && rec.m === 'POST' && rec.body.confirm === true, '5b. demandé puis POST /recreate {confirm:true}', rec);

    console.log('\n4. ★ Carte SDI et moteur : pas de niveau');
    E = sandbox();
    E.rv = base({ level: 'dedicated', dedicated: true, cores: 2, cap: null, node_io: true });
    await E.P.open(42, {}); await settle();
    ok(E.ctls.length === 0 && !find(E.root(), 'level') && !find(E.root(), 'cores-plus'), '4a. ★ node_io : ni niveau ni −/+');
    ok(find(E.root(), 'res-node-io'), '4b. node_io : la ligne dit que ses cœurs suivent ses prises');
    E = sandbox();
    E.rv = { vmid: 42, hostname: 'mtl', status: 'running', type: '2110_io', engine: true, node: { id: 3, name: 'dl360-1' }, levels: LEVELS, projects: [] };
    await E.P.open(42, {}); await settle();
    ok(E.ctls.length === 0 && !find(E.root(), 'level') && !find(E.root(), 'mem-input'), '4c. ★ moteur : ni niveau ni mémoire');
    ok(find(E.root(), 'res-engine') && find(E.root(), 'node-link'), '4d. moteur : ligne lcores DPDK + lien cœurs du nœud');
    ok(!find(E.root(), 'act-recreate') && find(E.root(), 'act-restart'), '4e. moteur : Restart sans Recréer');

    console.log('\n7. Droits et actions du plugin');
    E = sandbox();
    E.rv = base({ level: 'normal', dedicated: false, cores: null, cap: 2 });
    await E.P.open(42, { canDeploy: false, canDestroy: false }); await settle();
    ok(find(E.root(), 'read-only') && !find(E.root(), 'act-restart') && !find(E.root(), 'cap-apply') && !find(E.root(), 'cap-remove')
       && E.ctls[0] && E.ctls[0].opts.disabled === true, '7a. sans droit : lecture seule, niveau désactivé, pas d\'action');
    ok(find(E.root(), 'cap').textContent === '2 cœur(s)', '7b. plafond posé affiché', find(E.root(), 'cap').textContent);
    E = sandbox();
    E.rv = base({ level: 'normal', dedicated: false, cores: null, cap: null });
    let ran = null;
    await E.P.open(42, { canDeploy: true, canDestroy: true, inst: { vmid: 42 },
                         settingsActions: (i) => [{ label: 'Assistant', run: () => { ran = i.vmid; } }] }); await settle();
    const pa = find(E.root(), 'plugin-actions');
    ok(pa && pa.children[0].className === 'ctl-push ctl-push--wide' && pa.children[0].textContent === 'Assistant', '7c. actions du plugin monté, contrôle du catalogue');
    ok(find(E.root(), 'act-destroy') && find(E.root(), 'act-stop'), '7d. Détruire et Stop présents avec les droits');
    if (pa) pa.children[0].onclick();
    ok(ran === 42 && E.root().hidden === true, '7e. l\'action reçoit l\'instance, le panneau se ferme');

    console.log('\n8. ★ Menu de la carte');
    const inerte = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => '' : inerte), apply: () => inerte, construct: () => inerte });
    const sb = { window: new Proxy({ t: (k) => (k in FR ? FR[k] : k) }, { get: (o, k) => (k in o ? o[k] : inerte), set: (o, k, v) => { o[k] = v; return true; } }),
                 document: inerte, localStorage: inerte, navigator: inerte, location: { hash: '', pathname: '/', search: '' },
                 history: inerte, sessionStorage: inerte, console, setInterval() {}, setTimeout() {}, clearTimeout() {}, clearInterval() {},
                 confirm: () => true, alert() {}, fetch: async () => ({ ok: true, json: async () => ({}) }) };
    vm.createContext(sb);
    try { vm.runInContext(SCRIPTS, sb); } catch (e) { if (process.env.DEBUG) console.log(e.stack); }
    const carte = (dc) => ({ vmid: 42, hostname: 'mix-1', status: 'running', deploy_config: dc ? JSON.stringify({ type: 'mixer' }) : null });
    const m1 = sb._renderCardInner(carte(true), true, true, true);
    ok(/BobiContainerPanel\.open\(42/.test(m1) && m1.includes(FR['containers.panel.open']) && !/modifier\(42/.test(m1),
       '8a. ★ conteneur déployé : « Réglages du conteneur… » ouvre le panneau', m1.slice(0, 400));
    ok(/modifier\(42/.test(sb._renderCardInner(carte(false), true, true, true)), '8b. premier déploiement : « Configurer » (palette)');
    ok(/BobiContainerPanel\.open\(42/.test(sb._renderCardInner(carte(true), false, false, false)), '8c. sans droit : le panneau s\'ouvre en lecture seule');
    ok(/restart\(42/.test(m1) && /detruire\(42/.test(m1), '8d. les raccourcis restent dans le menu');

    const cles = Object.keys(FR).filter((k) => k.startsWith('containers.panel.'));
    ok(cles.length > 30 && cles.every((k) => EN[k]), '9. libellés containers.panel.* en FR et EN', cles.filter((k) => !EN[k]));
    const usees = [...JS.matchAll(/'(containers\.panel\.[a-z_]+)'/g)].map((m) => m[1]);
    ok(usees.every((k) => FR[k]), '10. toute clé utilisée par le panneau existe', usees.filter((k) => !FR[k]));

    if (echecs.length) { console.log('\nÉCHEC — ' + echecs.length + ' vérification(s)'); process.exit(1); }
    console.log('\nOK — panneau « Réglages du conteneur » : niveau, cœurs, plafond, liens, droits, menu');
})().catch((e) => { console.log('PLANTAGE ' + (e && e.stack || e)); process.exit(1); });
