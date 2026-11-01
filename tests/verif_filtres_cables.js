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

// Filtre projet : un membre PAR SNAPSHOT (project_id nul, project_ids rempli) est DANS le projet.
// Constaté le 2026-10-24 : l'intérieur d'AtelierTest montrait 4 conteneurs sur 6, sans decklink-io
// ni player, dont partaient pourtant les câbles du mélangeur.
eval(extraire('_inProject'));
eval(extraire('_nodeObjMatches'));
function membre(nom, n, pid, att) {
    _projFilter = pid; _nodeFilter = '';
    const ok = _nodeObjMatches(n) === att;
    if (!ok) ko++;
    console.log(`${ok ? '  ok  ' : ' ÉCHEC'} ${nom}`);
}
console.log('Appartenance au projet filtré :');
membre('membre par snapshot (project_ids) → affiché',      {project_id: null, project_ids: [16]}, '16', true);
membre('rattachement direct seul (ancienne topologie) → affiché', {project_id: 16},                  '16', true);
membre('membre d\'un autre projet → masqué',               {project_id: null, project_ids: [13]}, '16', false);
membre('hors projet → masqué',                             {project_id: null, project_ids: []},   '16', false);

// Ports virtuels (24/10) : hors projet, ni carte de ports aux bords ni câble qui en part — le câble
// PHYSIQUE (`phys_of_port`) les remplace ; dans le projet, c'est l'inverse.
membre('carte de ports, aucun projet filtré → masquée',    {project_ports_node: true, project_id: 15}, '', false);
membre('carte de ports, son projet filtré → affichée',     {project_ports_node: true, project_id: 15}, '15', true);
eval(extraire('_edgePassesFilter'));
_sigFilter = new Set(['video']);
nodesByVmid = {'-15': {project_ports_node: true, project_id: 15}, 1200: {project_ids: []},
               1257: {project_ids: [15]}};
function cable(nom, e, pid, att) {
    _projFilter = pid; _nodeFilter = '';
    const ok = _edgePassesFilter(e) === att;
    if (!ok) ko++;
    console.log(`${ok ? '  ok  ' : ' ÉCHEC'} ${nom}`);
}
console.log('Câbles des entrées qui suivent un port :');
cable('hors projet : câble physique producteur → entrée affiché', {from: 1200, to: 1257, phys_of_port: 23}, '', true);
cable('hors projet : câble qui part du port masqué',            {from: -15, to: 1257, port_id: 23}, '', false);
cable('dans le projet : câble qui part du port affiché',        {from: -15, to: 1257, port_id: 23}, '15', true);
nodesByVmid[1200] = {project_ids: [15]};
cable('dans le projet : câble physique masqué (même si le producteur est membre)',
      {from: 1200, to: 1257, phys_of_port: 23}, '15', false);

console.log(ko === 0 ? '\nTous les cas passent.' : `\n${ko} cas en échec.`);
process.exit(ko ? 1 : 0);
