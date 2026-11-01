#!/usr/bin/env node
// Vérification hors-ligne de la DÉCISION de la boîte « Redémarrer / Arrêter un nœud »
// (static/node_sheet.js, fiche de nœud validée par Cyril le 2026-10-30).
//
// ★ CE QUE CETTE GARDE TIENT :
//  1. sonde muette → aucun geste permis : on ne coupe pas à l'aveugle ;
//  2. installation en cours → aucun geste permis, et pas de « forcer » proposé (le serveur refuse
//     aussi : un bouton qui mène à un refus certain est un piège) ;
//  3. processus bloqués dans le noyau → redémarrage FORCÉ seulement, arrêt impossible ;
//  4. l'arrêt — et lui seul — exige de taper le nom du nœud, exactement ;
//  5. les durées se lisent comme dans la maquette (« 1 min 50 », « 3 h 01 »).
//
// Le module est chargé tel quel ; aucun DOM n'est construit. Aucune base, aucun réseau.
//
//   node tests/verif_fiche_noeud_ui.js

const fs = require('fs'), path = require('path');
const code = fs.readFileSync(path.join(__dirname, '..', 'static', 'node_sheet.js'), 'utf8');
const win = { t: (k) => k, _escp: (s) => String(s) };
const doc = { addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
new Function('window', 'document', 'fetch', code)(win, doc, () => { throw new Error('aucun réseau'); });
const NS = win.NodeSheet;

let ko = 0;
function ok(cond, titre, detail) {
    console.log((cond ? '  ok   ' : ' ÉCHEC ') + titre);
    if (!cond) { ko++; if (detail !== undefined) console.log('         ' + JSON.stringify(detail)); }
}
const V = NS._verdict;
const D = [{pid: 6254, comm: 'mxl-fabrics-dem', wchan: 'cm_destroy_id'}];
const B = [{kind: 'packages', detail: 'dpkg'}];

console.log('\n1. Sonde muette');
for (const a of ['reboot', 'poweroff']) {
    const v = V({probe_ok: false, blocked: null, busy: null}, a);
    ok(!v.allowed && !v.force && !v.needName, a + ' : rien n\'est permis', v);
}
ok(!V(null, 'reboot').allowed, 'avant la réponse du serveur : rien n\'est permis');

console.log('\n2. Installation en cours');
for (const a of ['reboot', 'poweroff']) {
    const v = V({probe_ok: true, blocked: [], busy: B}, a);
    ok(!v.allowed && !v.force && !v.needName, a + ' : refusé, aucun forçage proposé', v);
}
const vb = V({probe_ok: true, blocked: D, busy: B}, 'reboot');
ok(!vb.allowed && !vb.force, '★ installation ET processus bloqués : pas de « forcer » non plus', vb);

console.log('\n3. Processus bloqués dans le noyau');
let v = V({probe_ok: true, blocked: D, busy: []}, 'reboot');
ok(v.allowed && v.force && !v.needName, 'redémarrage : permis, mais FORCÉ', v);
v = V({probe_ok: true, blocked: D, busy: []}, 'poweroff');
ok(!v.allowed && !v.needName, 'arrêt : impossible (ne finirait jamais)', v);

console.log('\n4. Voie libre');
v = V({probe_ok: true, blocked: [], busy: []}, 'reboot');
ok(v.allowed && !v.force && !v.needName, 'redémarrage : permis, normal, sans saisie', v);
v = V({probe_ok: true, blocked: [], busy: []}, 'poweroff');
ok(v.allowed && !v.force && v.needName, '★ arrêt : permis, saisie du nom EXIGÉE', v);
const M = NS._nameMatches;
ok(M('local', 'local') && M('  local ', 'local'), 'le nom exact (espaces de bord tolérés) débloque');
ok(!M('Local', 'local') && !M('loca', 'local') && !M('', 'local') && !M('local2', 'local'), 'casse, préfixe, vide, suffixe : refusés');

console.log('\n5. Durées');
const d = NS._dur;
ok(d(42) === '42 s' && d(110) === '1 min 50' && d(120) === '2 min' && d(10860) === '3 h 01', 'comme la maquette',
   [d(42), d(110), d(120), d(10860)]);
ok(d(90000) === 'settings.node_sheet.dur_days', 'au-delà d\'un jour : clé traduite', d(90000));

console.log('\n6. Images : un décompte des ATTENDUES, pas la liste');
const I = NS._imagesHtml;
const img = (present, expected, unknown) => ({tag: 'x', present, expected, unknown});
ok(I({images: {a: img(true, true), b: img(true, true), c: img(false, false)}}).includes('--status-running-fg'),
   'toutes les attendues présentes : vert (une image non attendue ne compte pas)');
ok(I({images: {a: img(true, true), b: img(false, true)}}).includes('--status-stopped-fg'), 'une attendue absente : rouge');
ok(I({images: {a: img(false, true, true)}}).includes('img_unknown'), 'agent muet : « non vérifiées », jamais un faux 0');
ok(I({images: {a: img(false, false)}}).includes('settings.node_sheet.none'), 'rien d\'attendu : « aucun »');
ok(!/<dt>/.test(I({images: {a: img(true, true), b: img(true, true)}})), 'plus de ligne par image');

console.log('\n6 bis. Reprise désactivée : ce que la réparation des certificats a fait');
const R = NS._recDisabled;
ok(R({state: 'disabled', certs: null}).includes('rec_disabled') && !R({state: 'disabled', certs: null}).includes('rec_certs'),
   'sans bilan de certificats : « désactivée », rien d\'inventé');
const rz = R({state: 'disabled', certs: {repaired: 12, broken: 12, failed: 0, duration_s: 110}});
ok(rz.includes('rec_certs') && rz.includes('rec_disabled_short'), '★ le cas Z8 : le résultat d\'abord, puis « désactivée »', rz);
ok(R({state: 'disabled', certs: {error: true}}).includes('--status-stopped-fg'), 'réparation impossible : en rouge');
ok(NS._airText({air: {kind: 'output'}}).includes('dlg_air_output') && NS._airText({air: {kind: 'feeds', output: 'dk'}}).includes('dlg_air_feeds'),
   'la raison d\'antenne est dite : sortie, ou ce qu\'il alimente');

console.log('\n7. Ordre de la fiche : les alertes en DERNIER, avec le lien vers le journal');
const iP = code.indexOf("section('power'"), iA = code.indexOf("section('alerts'");
ok(iP > 0 && iA > iP && code.indexOf("section('", iA + 1) < 0, 'alertes après « Arrêt et redémarrage », dernière section');
ok(/section\('alerts'[\s\S]{0,200}href="\/monitoring#logs"/.test(code), 'lien vers le journal des alertes (/monitoring#logs)');

console.log(ko ? '\nÉCHECS : ' + ko : '\nOK');
process.exit(ko ? 1 : 0);
