#!/usr/bin/env node
// Vérification hors-ligne de la tuile CPU de la vue d'un nœud (Monitoring) et de ce qui a
// rejoint la fenêtre « Gérer les cœurs » (décision de Cyril, 2026-10-31).
//
// ★ CE QUE CETTE GARDE TIENT :
//  1. la tuile ne garde qu'un RÉSUMÉ : 3 plus gros consommateurs + tout conteneur affamé
//     (PSI full ≥ 1 %), même s'il consomme peu — c'est justement le symptôme de la famine ;
//  2. la liste des cœurs d'un socket entrelacé (0,2,4…94) se lit « 0–94 pairs » au lieu de
//     sortir de la tuile ;
//  3. le tableau complet et le relevé des cœurs ont QUITTÉ la tuile ; un lien ouvre la fenêtre ;
//  4. la fenêtre a la colonne Famine (mêmes seuils) et le relevé des cœurs.
//
//   node tests/verif_tuile_cpu.js

const fs = require('fs'), path = require('path');
const R = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(R, 'templates', 'monitoring.html'), 'utf8');
const coresJs = fs.readFileSync(path.join(R, 'static', 'js', 'cores.js'), 'utf8');

let ko = 0;
function ok(cond, titre, detail) {
    console.log((cond ? '  ok   ' : ' ÉCHEC ') + titre);
    if (!cond) { ko++; if (detail !== undefined) console.log('         ' + JSON.stringify(detail)); }
}
// Extrait une fonction de premier niveau du gabarit (jusqu'à la ligne « } » en colonne 0).
function fn(name) {
    const i = html.indexOf('function ' + name + '(');
    if (i < 0) throw new Error('fonction absente : ' + name);
    return html.slice(i, html.indexOf('\n}\n', i) + 2);
}
const win = { t: (k) => ({ 'monitoring.cpus_even': '{a}–{b} pairs', 'monitoring.cpus_odd': '{a}–{b} impairs' }[k] || k) };
const M = new Function('window', fn('parseCpusetJS') + fn('cpuTopConsumers') + fn('cpusCompact')
    + 'return { cpuTopConsumers, cpusCompact };')(win);

console.log('\n1. Résumé : 3 plus gros + les affamés');
const C = (vmid, cpu, full) => ({ vmid, hostname: 'c' + vmid, cpu_percent: cpu, psi: full == null ? {} : { full } });
const top = M.cpuTopConsumers([C(1, 90), C(2, 80), C(3, 70), C(4, 60), C(5, 2, 12), C(6, 1, 0.5), C(7, 1)]);
ok(top.map(c => c.vmid).join() === '1,2,3,5', '★ trois plus gros, puis l\'affamé qui consomme 2 %', top.map(c => c.vmid));
ok(M.cpuTopConsumers([C(1, 9, 30)]).length === 1 && M.cpuTopConsumers(null).length === 0, 'affamé déjà dans le haut : pas en double ; liste vide tolérée');

console.log('\n2. Cœurs d\'un socket');
const pairs = Array.from({ length: 48 }, (_, i) => 2 * i).join(',');
ok(M.cpusCompact(pairs) === '0–94 pairs', '★ socket entrelacé (dell-1) : « 0–94 pairs »', M.cpusCompact(pairs));
ok(M.cpusCompact(Array.from({ length: 48 }, (_, i) => 2 * i + 1).join(',')) === '1–95 impairs', 'impairs');
ok(M.cpusCompact('0-23,48-71') === '0–23, 48–71', 'plages contiguës', M.cpusCompact('0-23,48-71'));
ok(M.cpusCompact('3,7') === '3, 7' && M.cpusCompact('5') === '5', 'courtes listes : telles quelles, avec espaces');
ok(/socket-cpus" title="\$\{escapeHtml\(s\.cpus\)\}">\$\{escapeHtml\(cpusCompact\(s\.cpus\)\)\}/.test(html),
   'la tuile affiche la forme courte, la liste complète en infobulle');
ok(/\.socket-cpus\s*\{[^}]*overflow-wrap:\s*anywhere/.test(html), 'et peut passer à la ligne si besoin');

console.log('\n3. Le tableau complet a quitté la tuile');
ok(!/cpu-who-table|coreSnapshot|renderCoreSnapHtml|_coreSnap/.test(html), 'ni tableau « qui consomme quoi » ni relevé dans Monitoring');
ok(/BobiCores\.ouvrir\(\$\{\+key\}, this\.dataset\.n\)">\$\{window\.t\('monitoring\.cpu_see_all'\)\}/.test(html),
   'lien « Tout voir » qui ouvre la fenêtre des cœurs');

console.log('\n4. La fenêtre des cœurs');
global.window = { t: (k) => k };
const doc = { getElementById: () => null };
new Function('window', 'document', 'fetch', coresJs)(global.window, doc, () => { throw new Error('aucun réseau'); });
const I = global.window.BobiCores._interne;
ok(I.famineHtml(null).includes('—') && I.famineHtml(0.4).includes('cpu_psi_ok'), 'famine inconnue « — », sous 1 % « sain »');
ok(I.famineHtml(5).includes('bc-famine-warn') && I.famineHtml(25).includes('bc-famine-bad'), 'seuils de la tuile : ≥ 1 % orange, ≥ 25 % rouge');
const st = I.nouvelEtat(35, 'dell-1');
ok(I.releve(st) === '', 'pas de relevé tant qu\'on ne l\'a pas demandé');
st.snap = { ts: 0, data: { ok: true, duration_ms: 512, cores: [{ cpu: 4, pct: 91.5, top: [{ hostname: 'mix', vmid: 7, comm: 'python3', pct: 88 }] }] } };
const r = I.releve(st);
ok(r.includes('#4') && r.includes('mix (7)') && r.includes('bc-snap-hot') && r.includes('512 ms'), '★ relevé : cœur, conteneur nommé, cœur chaud signalé', r);
st.snap = { ts: 0, data: { ok: false, error: 'agent_too_old' } };
ok(I.releve(st).includes('core_snapshot_agent_old'), 'agent trop ancien : dit comme tel');
ok(/\['famine', 'monitoring\.cpu_col_psi'\]/.test(coresJs) && /famine: \(l\) =>/.test(coresJs), 'colonne Famine, triable');
ok(/class="btn bc-releve"/.test(coresJs) && /clic\('\.bc-releve'/.test(coresJs), 'bouton « relevé des cœurs » branché');

console.log(ko ? '\nÉCHECS : ' + ko : '\nOK');
process.exit(ko ? 1 : 0);
