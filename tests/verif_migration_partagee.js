#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Garde : « Migrer » est disponible sur toutes les pages (2026-11-07).
//
// ★ POURQUOI. La fenêtre de migration vivait dans static/scripts.js, que seules les pages
//   Containers et Projets chargent : le panneau « Réglages du conteneur », ouvert depuis la page
//   d'un plugin, ne pouvait pas la proposer. Elle est passée dans static/js/migration.js, chargé
//   par layout.html avec son dialogue. Ce que la garde tient :
//  1. ★ migration.js fonctionne SEUL (sans scripts.js ni son escapeHtml) : ouverture, simulation,
//     rendu d'un refus, échec réseau affiché ;
//  2. ★ layout.html charge le script ET inclut le dialogue, une seule fois dans toute page
//     (containers.html ne l'inclut plus : deux `#migration-modal` = un id dupliqué) ;
//  3. scripts.js ne redéfinit pas `ouvrirMigration` (une seule définition, sinon la dernière
//     chargée gagne en silence).
//
//   node tests/verif_migration_partagee.js          (MIGRATION_PARTAGEE_MUTATION=<nom>)
//   depend_scripts   migration.js dépend à nouveau d'escapeHtml (scripts.js)
//   pas_dans_layout  layout.html ne charge plus migration.js
const fs = require('fs'), path = require('path'), vm = require('vm');
const RAC = path.join(__dirname, '..');
let JS = fs.readFileSync(path.join(RAC, 'static', 'js', 'migration.js'), 'utf8');
let LAYOUT = fs.readFileSync(path.join(RAC, 'templates', 'layout.html'), 'utf8');
const MUT = process.env.MIGRATION_PARTAGEE_MUTATION || '';
if (MUT === 'depend_scripts') JS = JS.split('_migEsc(String(e))').join('escapeHtml(String(e))');
else if (MUT === 'pas_dans_layout') LAYOUT = LAYOUT.replace("/static/js/migration.js", "/static/js/absent.js");
else if (MUT) { console.log('mutation inconnue : ' + MUT); process.exit(2); }
if (MUT) console.log('MUTATION active : ' + MUT);

const echecs = [];
function ok(c, titre, detail) {
    console.log((c ? '  ✓ ' : '  ✗ ') + titre + (!c && detail !== undefined ? '  — ' + JSON.stringify(detail) : ''));
    if (!c) echecs.push(titre);
}

const els = {};
const el = (id) => (els[id] = els[id] || { id, textContent: '', innerHTML: '', style: {} });
let reponse = null;
const sandbox = {
    window: { t: (k) => k }, console,
    document: { getElementById: (id) => el(id) },
    fetch: async () => { if (reponse instanceof Error) throw reponse; return { ok: true, json: async () => reponse }; },
    confirm: () => false,
};
vm.createContext(sandbox);
vm.runInContext(JS, sandbox);
const attendre = () => new Promise((r) => setTimeout(r, 0));

(async () => {
    console.log('\n1. ★ migration.js seul, sans scripts.js');
    ok(typeof sandbox.ouvrirMigration === 'function' && typeof sandbox.fermerMigration === 'function',
       '1a. ouvrirMigration / fermerMigration définies par le fichier partagé');
    reponse = { candidats: [{ node_cible: 2, node_cible_nom: 'dell<1>', ok: false, refus: ['pas la place'] }] };
    let err = null;
    try { sandbox.ouvrirMigration(42, 'mix'); await attendre(); await attendre(); } catch (e) { err = e; }
    ok(!err && els['migration-modal'].style.display === 'flex' && els['migration-hote'].textContent === 'mix',
       '1b. ★ la fenêtre s\'ouvre et nomme le conteneur', String(err));
    ok(/dell&lt;1&gt;/.test(els['migration-corps'].innerHTML) && /pas la place/.test(els['migration-corps'].innerHTML),
       '1c. ★ la simulation est rendue, échappée', els['migration-corps'].innerHTML.slice(0, 200));
    reponse = new Error('réseau coupé');
    err = null;
    try { sandbox.ouvrirMigration(42, 'mix'); await attendre(); await attendre(); } catch (e) { err = e; }
    ok(/réseau coupé/.test(els['migration-corps'].innerHTML), '1d. ★ un échec réseau est affiché (et ne casse pas sur une dépendance absente)',
       els['migration-corps'].innerHTML);
    sandbox.fermerMigration();
    ok(els['migration-modal'].style.display === 'none', '1e. et elle se ferme');

    console.log('\n2. ★ Chargée et incluse une seule fois, par layout.html');
    ok(/<script src="\/static\/js\/migration\.js/.test(LAYOUT), '2a. ★ layout.html charge migration.js');
    ok((LAYOUT.match(/\{% include 'migration\.html' %\}/g) || []).length === 1, '2b. ★ et inclut le dialogue');
    const autres = fs.readdirSync(path.join(RAC, 'templates')).filter((f) => f.endsWith('.html') && f !== 'layout.html')
        .filter((f) => /\{% include 'migration\.html' %\}/.test(fs.readFileSync(path.join(RAC, 'templates', f), 'utf8')));
    ok(autres.length === 0, '2c. aucune autre page ne l\'inclut (id dupliqué)', autres);

    console.log('\n3. Une seule définition');
    const SCRIPTS = fs.readFileSync(path.join(RAC, 'static', 'scripts.js'), 'utf8');
    ok(!/function ouvrirMigration\b/.test(SCRIPTS) && !/function _migLancer\b/.test(SCRIPTS),
       '3. scripts.js ne la redéfinit pas');

    if (echecs.length) { console.log('\nÉCHEC — ' + echecs.length + ' vérification(s)'); process.exit(1); }
    console.log('\nOK — « Migrer » marche seul et est disponible sur toutes les pages');
})();
