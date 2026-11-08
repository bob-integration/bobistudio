#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Guard: the « Recréer » entry of a container card's action menu (added on 2026-11-07).
//
// Why: the recreate route existed with no button, so the only way to give a container the
// resources today's rules assign it (a DeckLink card created before its dedicated cores) was an
// API call. What is held, on the real menu builder and the real handler of static/scripts.js:
//  1. ★ a compute container's menu offers Recréer; an engine's (2110_io) does not (the route
//     refuses it: Restart already re-runs an engine);
//  2. a user without the deploy permission does not get it;
//  3. ★ the gesture asks first; declined → nothing is sent;
//  4. ★ accepted → ONE POST to /recreate with confirm:true (the route refuses without it).
//
//   node tests/verif_carte_recreer.js      (CARTE_RECREER_MUTATION=<name>, each must give rc=1)
//   moteur_aussi    the engine gets the entry
//   sans_question   the gesture does not ask
//   sans_confirm    the POST omits confirm:true
//   colonne_brute   the card shows the `cores` column again
const fs = require('fs'), path = require('path'), vm = require('vm');
const RAC = path.join(__dirname, '..');
let JS = fs.readFileSync(path.join(RAC, 'static', 'scripts.js'), 'utf8');
const MUT = process.env.CARTE_RECREER_MUTATION || '';
const MUTATIONS = {
    moteur_aussi: ['if (canDeploy && !_isRx2110(dc && dc.type)) items.push(', 'if (canDeploy) items.push('],
    sans_question: ["    if (!confirm(window.t('containers.card.recreate_confirm')", "    if (false && !confirm(window.t('containers.card.recreate_confirm')"],
    sans_confirm: ["body: JSON.stringify({confirm: true})});", "body: JSON.stringify({})});"],
    colonne_brute: ["<dd>${escapeHtml(_cardCores(c))}</dd>", "<dd>${c.cores}</dd>"],
};
if (MUT) {
    if (!MUTATIONS[MUT]) { console.log('mutation inconnue : ' + MUT); process.exit(2); }
    const [a, b] = MUTATIONS[MUT];
    if (JS.split(a).length !== 2) { console.log('mutation ' + MUT + ' : motif introuvable ou ambigu — la garde est périmée'); process.exit(1); }
    JS = JS.replace(a, b);
    console.log('MUTATION active : ' + MUT);
}
const echecs = [];
function ok(cond, titre, detail) {
    console.log((cond ? '  ✓ ' : '  ✗ ') + titre + (!cond && detail !== undefined ? '  — ' + JSON.stringify(detail) : ''));
    if (!cond) echecs.push(titre);
}
const FR = JSON.parse(fs.readFileSync(path.join(RAC, 'i18n', 'fr.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(RAC, 'i18n', 'en.json'), 'utf8'));
const envois = [];
let reponse = true, questions = 0;
// Any global the file touches at load time resolves to an inert stub.
const inerte = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => '' : inerte), apply: () => inerte, construct: () => inerte });
const sandbox = {
    window: new Proxy({ t: (k) => (k in FR ? FR[k] : k) }, { get: (o, k) => (k in o ? o[k] : inerte), set: (o, k, v) => { o[k] = v; return true; } }),
    document: inerte, localStorage: inerte, navigator: inerte, location: { hash: '', pathname: '/', search: '' },
    history: inerte, sessionStorage: inerte, console,
    setInterval() {}, setTimeout() {}, clearTimeout() {}, clearInterval() {},
    confirm: () => { questions++; return reponse; }, alert() {},
    fetch: async (url, init) => { envois.push({ url, init }); return { ok: true, json: async () => ({}) }; },
};
vm.createContext(sandbox);
try { vm.runInContext(JS, sandbox); } catch (e) { if (process.env.DEBUG) console.log(e.stack); }
const R = sandbox._renderCardInner, recreer = sandbox.recreer;
if (typeof R !== 'function' || typeof recreer !== 'function') { console.log('ÉCHEC — fonctions introuvables'); process.exit(1); }

const carte = (type) => ({ vmid: 42, hostname: 'deck-1', status: 'running', deploy_config: JSON.stringify({ type }) });
const menu = (type, canDeploy) => { try { return R(carte(type), canDeploy, true, true); } catch (e) { return 'ERREUR ' + e.message; } };
console.log('\n1. ★ Qui a l\'entrée');
ok(/recreer\(42/.test(menu('decklink_io', true)) && menu('decklink_io', true).includes(FR['containers.card.recreate']),
   '1a. ★ un conteneur compute a « Recréer »', menu('decklink_io', true).slice(0, 300));
ok(!/recreer\(/.test(menu('2110_io', true)), '1b. ★ le moteur 2110 ne l\'a pas');
console.log('\n2. Droits');
ok(!/recreer\(/.test(menu('decklink_io', false)), '2. sans la permission de déployer : pas d\'entrée');
console.log('\n6. ★ « Cœurs » dit ce que le conteneur reçoit vraiment (07/11)');
const coeurs = (r) => { const h = R(Object.assign(carte('mixer'), { cores: 2, resources_effective: r }), true, true, true);
                        const m = /containers\.card\.cores<\/dt><dd>([^<]*)<\/dd>|Cœurs<\/dt><dd>([^<]*)<\/dd>/.exec(h);
                        return m ? (m[1] || m[2]) : null; };
ok(coeurs({ dedicated: true, cores: 3 }) === FR['containers.card.cores_dedicated'].replace('{n}', 3), '6a. ★ Dédié : ses cœurs dédiés', coeurs({ dedicated: true, cores: 3 }));
ok(coeurs({ dedicated: false, cores: 4 }) === FR['containers.card.cores_cap'].replace('{n}', 4), '6b. partagé plafonné : son plafond');
ok(coeurs({ dedicated: false, cores: null }) === FR['containers.card.cores_pool'],
   '6c. ★ plafond retiré : « sans plafond », pas l\'ancienne valeur de la colonne (2)', coeurs({ dedicated: false, cores: null }));
ok(coeurs({ engine: true }) === FR['containers.card.cores_engine'], '6d. le moteur : ses lcores');

(async () => {
    console.log('\n3. ★ On demande d\'abord');
    reponse = false; await recreer(42, 'deck-1');
    ok(questions === 1 && envois.length === 0, '3. ★ refusé à la question → rien n\'est envoyé', { questions, envois });
    console.log('\n4. ★ Un envoi, confirmé');
    reponse = true; await recreer(42, 'deck-1');
    const e = envois[0] || {};
    ok(envois.length === 1 && e.url === '/api/containers/42/recreate' && e.init.method === 'POST'
       && JSON.parse(e.init.body).confirm === true, '4. ★ un POST /recreate avec confirm:true', envois);
    ok(['containers.card.recreate', 'containers.card.recreate_title', 'containers.card.recreate_confirm',
        'containers.card.recreate_failed'].every(k => FR[k] && EN[k]), '5. libellés FR et EN présents');
    if (echecs.length) { console.log('\nÉCHEC — ' + echecs.length + ' vérification(s)'); process.exit(1); }
    console.log('\nOK — « Recréer » est dans le menu des conteneurs compute, demandé puis envoyé confirmé');
})();
