// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Menu « Insérer une variable » (multiview, modèles de PiP) : les états d'autres conteneurs sont
// rangés UN GROUPE PAR CONTENEUR, groupes triés par type puis par nom, entrées en libellé court.
//
// ★ LE CAS (2026-10-29) : « tout est en vrac » — les états d'un projet arrivaient à plat, plusieurs
// dizaines d'entrées « mixer-Atelier — … » mêlées à celles du player. On exécute ici le VRAI
// static/js/var_menu.js dans un faux DOM.
//
//   $ node tests/verif_var_menu.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}

function Option(text, value) { this.text = text; this.value = value; }
const document = {createElement: tag => ({tag, label: '', children: [], appendChild(c) { this.children.push(c); }})};
const ctx = {window: {}, document, Option};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'var_menu.js'), 'utf8'), ctx);
const M = ctx.window.BobiVarMenu;
controle('le module s\'expose (window.BobiVarMenu)', !!(M && M.appendGroups));

const e = (ct, type, sid, lab) => ({name: 'state:' + ct + '.' + sid, label: ct + ' — ' + lab,
                                    group: ct, type_label: type, short: lab});
const sel = {children: [], appendChild(c) { this.children.push(c); }};
M.appendGroups(sel, 'Ce projet', [
    e('player-2', 'Player', 'clip', 'Clip chargé'),
    e('mixer-Atelier', 'Mélangeur', 'pgm', 'Entrée PGM'),
    e('player-10', 'Player', 'clip', 'Clip chargé'),
    e('mixer-Atelier', 'Mélangeur', 'pvw', 'Entrée PVW'),
]);
const labels = sel.children.map(g => g.label);
controle('★★ un groupe par conteneur (3 conteneurs → 3 groupes)', sel.children.length === 3, JSON.stringify(labels));
controle('★★ groupes triés par type puis par nom (player-2 avant player-10)',
         JSON.stringify(labels) === JSON.stringify(['Ce projet · Mélangeur · mixer-Atelier',
                                                    'Ce projet · Player · player-2',
                                                    'Ce projet · Player · player-10']),
         JSON.stringify(labels));
const mix = sel.children[0].children;
controle('★ les états d\'un conteneur restent ensemble', mix.length === 2);
controle('★ libellé court dans le groupe, jeton complet en valeur',
         mix[0].text.startsWith('Entrée PGM') && mix[0].value === 'state:mixer-Atelier.pgm', mix[0].text);

const flat = {children: [], appendChild(c) { this.children.push(c); }};
M.appendGroups(flat, 'Système', [{name: 'host', label: 'Nom'}, {name: 'ip', label: 'IP'}]);
controle('entrées sans conteneur : un seul groupe, libellé long',
         flat.children.length === 1 && flat.children[0].label === 'Système' && flat.children[0].children[0].text.startsWith('Nom'));
M.appendGroups(flat, 'Vide', []);
controle('liste vide : aucun groupe ajouté', flat.children.length === 1);

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
