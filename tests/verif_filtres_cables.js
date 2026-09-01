#!/usr/bin/env node
// Vérification hors-ligne du GARDE-FOU DES FILTRES FANTÔMES de la page Câbles.
//
// Les listes de filtres (projet / nœud) sont ÉLAGUÉES côté serveur à ce qui est réellement
// présent dans la topologie (home_dashboard : « seuls les projets/nœuds RÉELLEMENT présents »).
// Un filtre mémorisé en localStorage dont la valeur quitte cette liste — projet terminé, nœud
// vidé ou tombé — n'a alors plus d'`<option>` : `select.value = …` échoue en SILENCE pendant que
// la variable JS reste armée, et `_nodeObjMatches` recale tous les nœuds. Page vide, aucun câble,
// aucun conteneur, et plus aucun moyen de lever le filtre depuis l'interface.
//
// La fonction est EXTRAITE du gabarit à chaque exécution : ce banc ne peut pas dériver de la
// page. Aucune base lue, aucun conteneur touché.
//
//   node tools/verif_filtres_cables.js                 # le gabarit courant
//   node tools/verif_filtres_cables.js <cables.html>    # une autre version (bissection)

const fs = require('fs'), path = require('path');
const cible = process.argv[2] || path.join(__dirname, '..', 'templates', 'cables.html');
const tpl = fs.readFileSync(cible, 'utf8');

function extraire(nom) {
    const i = tpl.indexOf(`function ${nom}(`);
    if (i < 0) throw new Error(`${nom} introuvable dans templates/cables.html`);
    let d = 0;
    for (let j = i; j < tpl.length; j++) {
        if (tpl[j] === '{') d++;
        else if (tpl[j] === '}' && --d === 0) return tpl.slice(i, j + 1);
    }
    throw new Error(`${nom} : accolade non refermée`);
}

// ─── Faux DOM / localStorage, réduits à ce que la fonction touche ────────────
const store = {};
const localStorage = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
};
let toasts = [];
const showToast = m => toasts.push(m);
const T = k => k;
const escapeHtml = x => x;
const selects = { 'filter-project': { value: '', innerHTML: '' },
                  'filter-node':    { value: '', innerHTML: '' } };
const document = { getElementById: id => selects[id] || null };
let _projFilter = '', _nodeFilter = '', _filterSig = '';

eval(extraire('_populateFilterSelects'));

// ─── Cas ─────────────────────────────────────────────────────────────────────
let ko = 0;
function cas(nom, proj, node, filtres, attProj, attNode, attToast) {
    _projFilter = proj; _nodeFilter = node; _filterSig = ''; toasts = [];
    store['mxl.cables.filter.project'] = proj;
    store['mxl.cables.filter.node'] = node;
    _populateFilterSelects(filtres);
    const ok = _projFilter === attProj && _nodeFilter === attNode && toasts.length === attToast;
    if (!ok) ko++;
    console.log(`${ok ? '  ok  ' : ' ÉCHEC'} ${nom}`);
    if (!ok) console.log(`         attendu proj=${JSON.stringify(attProj)} node=${JSON.stringify(attNode)} toasts=${attToast}\n` +
                         `         obtenu  proj=${JSON.stringify(_projFilter)} node=${JSON.stringify(_nodeFilter)} toasts=${toasts.length}`);
}

// Topologie sans aucun projet, deux nœuds — le parc observé le 2026-08-18.
const sansProjet = { projects: [], nodes: [{ id: 34, name: 'dl360-1' }, { id: 35, name: 'dell-1' }] };
const avecProjet = { projects: [{ id: 3, name: 'Projet' }], nodes: [{ id: 34, name: 'dl360-1' }] };

console.log('Filtres fantômes de la page Câbles :');
cas('projet disparu de la liste → filtre levé, et annoncé',      '7', '',   sansProjet, '',   '',   1);
cas('nœud hors topologie → filtre levé, et annoncé',              '',  '43', sansProjet, '',   '',   1);
cas('les deux fantômes à la fois → les deux levés',               '7', '43', sansProjet, '',   '',   1);
cas('nœud toujours présent → CONSERVÉ, silencieux',               '',  '35', sansProjet, '',   '35', 0);
cas('projet toujours présent → CONSERVÉ, silencieux',             '3', '',   avecProjet, '3',  '',   0);
cas('aucun filtre → rien à lever, silencieux',                    '',  '',   sansProjet, '',   '',   0);
cas('nœud « local » offert → CONSERVÉ',                           '',  'local',
    { projects: [], nodes: [{ id: null, name: 'local' }] },                  '',   'local', 0);
// Absence de référence : un passage à vide ne doit effacer AUCUN réglage.
cas('topologie vide (redémarrage) → filtre nœud CONSERVÉ, silencieux',
    '',  '35', { projects: [], nodes: [] },                                  '',   '35', 0);
cas('topologie vide (redémarrage) → filtre projet CONSERVÉ, silencieux',
    '7', '',   { projects: [], nodes: [] },                                  '7',  '',   0);

console.log(ko === 0 ? '\nTous les cas passent.' : `\n${ko} cas en échec.`);
process.exit(ko ? 1 : 0);
