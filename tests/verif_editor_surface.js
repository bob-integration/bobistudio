// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Surface d'édition commune (BobiEditor.surface, static/js/editor.js) — étape 3 du chantier « un
// seul moteur d'édition ». Elle porte les gestes de TOUS les éditeurs de disposition, avec les
// règles arbitrées par Cyril le 30/10 : référence = dernier cliqué (Maj ou Ctrl/⌘) ; groupe contre
// un bord = tout le groupe s'arrête ; poignée seule selon le format ; poignée d'une sélection =
// échelle depuis le coin ; hors-cadre selon l'éditeur ; flèches et outils partout.
//
//   $ node tests/verif_editor_surface.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ED = process.env.VERIF_EDITOR_JS || path.join(__dirname, '..', 'static', 'js', 'editor.js');
let echecs = 0;
function controle(nom, ok, pourquoi) {
    console.log('  ' + (ok ? 'OK   ' : 'ÉCHEC') + ' ' + nom);
    if (!ok) { echecs++; if (pourquoi) console.log('        → ' + pourquoi); }
}
const ecouteurs = [];
const store = {};
const ctx = { Math, JSON, Date, window: {}, navigator: { platform: 'Linux' },
              document: { addEventListener: (t, h) => ecouteurs.push(h), removeEventListener: () => {} },
              localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } };
const touche = (key, mods) => ecouteurs.forEach(h => h(Object.assign({ key, target: { tagName: 'CANVAS' },
                                                                      preventDefault() {} }, mods || {})));
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(ED, 'utf8'), ctx);
const B = ctx.window.BobiEditor;
controle('la surface est exposée (BobiEditor.surface)', B && typeof B.surface === 'function');
if (!B || typeof B.surface !== 'function') { console.log('\n1 échec'); process.exit(1); }

// Éditeur factice : objets en pixels, cadre 1920×1080 (ou décalé), journal des validations.
function editeur(objets, opts, cadre) {
    const ed = { L: objets, valides: [], rendus: 0 };
    ed.S = B.surface({
        objets: () => ed.L, cadre: () => cadre || { x: 0, y: 0, w: 1920, h: 1080 },
        ecrire: (i, r) => { ed.L[i] = Object.assign({}, ed.L[i], r); },
        rendu: () => { ed.rendus++; }, valider: idx => { ed.valides.push(idx.slice()); },
    }, Object.assign({ tol: () => 8 }, opts || {}));
    return ed;
}
const R = (x, y, w, h, ex) => Object.assign({ x, y, w, h }, ex || {});

// ═══ Sélection ═══
let e = editeur([R(0, 0, 100, 100), R(200, 0, 100, 100), R(400, 0, 100, 100)]);
e.S.selectionner(0).selectionner(2, { shiftKey: true }).selectionner(1, { ctrlKey: true });
controle('★★★ Maj ET Ctrl/⌘ ajoutent ; la référence est le DERNIER cliqué', e.S.sel.length === 3 && e.S.primaire === 1,
         JSON.stringify([e.S.sel, e.S.primaire]));
e.S.selectionner(2, { metaKey: true });
controle('★★ ⌘+clic sur un sélectionné le retire', e.S.sel.length === 2 && !e.S.sel.includes(2));
e.S.appui({ x: 5, y: 5 }, null, {});
controle('★ clic dans le vide : sélection vidée', e.S.sel.length === 0);

// ═══ Glisser un groupe contre un bord ═══
e = editeur([R(1700, 100, 100, 100), R(100, 700, 300, 200)]);
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.appui({ x: 150, y: 750 }, { i: 1 }, {});
e.S.glisse({ x: 1850, y: 750 }); e.S.relache();
controle('★★★ groupe contre un bord : TOUT le groupe s\'arrête (agencement préservé)',
         e.L[0].x === 1820 && e.L[1].x === 220, JSON.stringify(e.L));
controle('★★ fin de geste : une validation qui nomme les objets modifiés', JSON.stringify(e.valides) === '[[0,1]]', JSON.stringify(e.valides));
e = editeur([R(1700, 100, 100, 100), R(100, 700, 300, 200)], { horsCadre: true });
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.appui({ x: 0, y: 0 }, { i: 1 }, {}); e.S.glisse({ x: 1700, y: 0 }); e.S.relache();
controle('★★ hors-cadre permis (Split) : pas de bornage', e.L[0].x === 3400, JSON.stringify(e.L[0]));
e = editeur([R(1700, 100, 100, 100), R(100, 700, 300, 200)], { horsCadre: true,
             bornesCentre: () => ({ x0: -500, x1: 2400, y0: -500, y1: 1600 }) });
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.appui({ x: 0, y: 0 }, { i: 1 }, {}); e.S.glisse({ x: 1700, y: 0 }); e.S.relache();
controle('★★★ hors-cadre AVEC bornes de centre : tout le groupe s\'arrête quand l\'un atteint la sienne',
         e.L[0].x + 50 === 2400 && e.L[1].x === 750, JSON.stringify(e.L));
e = editeur([R(100, 100, 100, 100), R(503, 400, 100, 100)]);
e.S.appui({ x: 150, y: 150 }, { i: 0 }, {}); e.S.glisse({ x: 450, y: 150 }); e.S.relache();
controle('★★ aimant au glisser : bord collé à l\'autre objet, repère', e.L[0].x === 403, JSON.stringify(e.L[0]));
e = editeur([R(100, 100, 100, 100), R(503, 400, 100, 100)], { aimant: () => false });
e.S.appui({ x: 150, y: 150 }, { i: 0 }, {}); e.S.glisse({ x: 450, y: 150 }); e.S.relache();
controle('★ aimant coupé : position exacte', e.L[0].x === 400);
e = editeur([R(10, 10, 100, 100)]);
e.S.appui({ x: 50, y: 50 }, { i: 0 }, {}); e.S.relache();
controle('★ simple clic : aucune validation (rien n\'a bougé)', e.valides.length === 0);

// ═══ Poignée seule ═══
e = editeur([R(100, 100, 320, 180, { format: 16 / 9 }), R(900, 0, 100, 100)], { arrondi: Math.round, min: 16 });
e.S.selectionner(0); e.S.appui({ x: 420, y: 280 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 690, y: 290 }); e.S.relache();
controle('★★★ format verrouillé : la poignée garde le format', Math.abs(e.L[0].w / e.L[0].h - 16 / 9) < 0.01, JSON.stringify(e.L[0]));
e = editeur([R(100, 100, 320, 180, { format: 16 / 9 }), R(600, 0, 100, 100)], { arrondi: Math.round, min: 16 });
e.S.selectionner(0); e.S.appui({ x: 420, y: 280 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 603, y: 280 });               // bord droit à 603 : la cible 600 est à 3
controle('★★ … et s\'aimante sur UNE cible, format tenu', e.L[0].w === 500 && e.L[0].h === 281, JSON.stringify(e.L[0]));
e = editeur([R(100, 100, 300, 50), R(600, 0, 100, 400)], { min: 16 });
e.S.selectionner(0); e.S.appui({ x: 400, y: 150 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 597, y: 397 });               // bords à 597 et 397 : cibles 600 et 400
controle('★★★ objet libre : largeur et hauteur aimantées séparément', e.L[0].w === 500 && e.L[0].h === 300, JSON.stringify(e.L[0]));

e = editeur([R(100, 100, 200, 100, { format: 2 })]);
e.S.selectionner(0); e.S.appui({ x: 300, y: 200 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 350, y: 200 }, { altKey: true }); e.S.relache();
controle('★★★ Alt sur la poignée : grandit autour du CENTRE (format tenu)',
         e.L[0].w === 300 && e.L[0].h === 150 && e.L[0].x === 50 && e.L[0].y === 75, JSON.stringify(e.L[0]));
e = editeur([R(100, 100, 200, 100)]);
e.S.selectionner(0); e.S.appui({ x: 300, y: 200 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 350, y: 220 }); e.S.relache();
controle('★★ sans Alt : le coin opposé reste fixe', e.L[0].x === 100 && e.L[0].y === 100 && e.L[0].w === 250 && e.L[0].h === 120,
         JSON.stringify(e.L[0]));

// ═══ Poignée d'une sélection multiple ═══
e = editeur([R(40, 540, 442, 272), R(482, 540, 438, 268), R(920, 540, 446, 274)], { arrondi: v => Math.round(v / 2) * 2, min: 32 });
[0, 1, 2].forEach(i => e.S.selectionner(i, { shiftKey: true })); e.S.selectionner(0);
e.S.appui({ x: 482, y: 812 }, { i: 0, poignee: true }, {});
e.S.glisse({ x: 511, y: 812 }); e.S.relache();
controle('★★★ poignée d\'un groupe : échelle depuis le coin, tuiles collées restent collées',
         e.L[1].x === e.L[0].x + e.L[0].w && e.L[2].x === e.L[1].x + e.L[1].w && e.L[0].x === 40 && e.L[0].w > 442,
         JSON.stringify(e.L));

// ═══ Flèches ═══
e = editeur([R(1915, 10, 5, 5), R(500, 500, 10, 10)]);
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.fleche(1, 0, true);
controle('★★ flèche (Maj = 10) : bornée en GROUPE', e.L[0].x === 1915 && e.L[1].x === 500, JSON.stringify(e.L));
e.S.fleche(-1, 0, true);
controle('★ flèche gauche Maj : 10 unités', e.L[0].x === 1905 && e.L[1].x === 490);

// ═══ Outils ═══
e = editeur([R(100, 100, 100, 50), R(900, 600, 300, 200)]);
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.outil('aligner', 'top');
controle('★★ aligner sur la référence (dernier cliqué)', e.L[0].y === 600 && e.L[1].y === 600);
e = editeur([R(100, 100, 100, 50, { format: 2 }), R(900, 600, 300, 200)]);
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.outil('egaliser', 'w');
controle('★★★ même largeur, format verrouillé : la hauteur suit le format', e.L[0].w === 300 && e.L[0].h === 150, JSON.stringify(e.L[0]));
e = editeur([R(0, 0, 100, 10), R(10, 0, 500, 10), R(900, 0, 100, 10)]);
[0, 1, 2].forEach(i => e.S.selectionner(i, { shiftKey: true }));
e.S.outil('espaces', 'h');
controle('★★ répartir les ESPACES', e.L[1].x === 250, JSON.stringify(e.L[1]));
e = editeur([R(0, 0, 100, 10), R(10, 0, 500, 10), R(900, 0, 100, 10)]);
[0, 1, 2].forEach(i => e.S.selectionner(i, { shiftKey: true }));
e.S.outil('distribuer', 'h');
controle('★★ distribuer les POSITIONS', e.L[1].x === 450, JSON.stringify(e.L[1]));
e = editeur([R(900, 0, 100, 100, { format: 16 / 9 }), R(10, 0, 160, 90, { format: 16 / 9 })]);
e.S.selectionner(0); e.S.selectionner(1, { shiftKey: true });
e.S.outil('remplir', 'h');
controle('★★ Remplir : tuilage au format, ordre à l\'écran', e.L[1].x === 0 && e.L[0].x === 960 && e.L[0].h === 540, JSON.stringify(e.L));
controle('★ outil sans sélection suffisante : null (à dire à l\'exploitant)', editeur([R(0, 0, 1, 1)]).S.outil('distribuer', 'h') === null);

// ═══ Cadre décalé (marges du Split) ═══
e = editeur([R(150, 150, 100, 100)], {}, { x: 100, y: 100, w: 800, h: 400 });
e.S.selectionner(0); e.S.outil('aligner', 'right');
controle('★★ cadre décalé : aligner sur SON bord droit', e.L[0].x === 800, JSON.stringify(e.L[0]));
e.S.appui({ x: 0, y: 0 }, { i: 0 }, {}); e.S.glisse({ x: -2000, y: 0 }); e.S.relache();
controle('★★ cadre décalé : le glisser bute sur SON bord gauche', e.L[0].x === 100, JSON.stringify(e.L[0]));

// ═══ Seuil de départ et rectangle de sélection (Scénario des macros, 30/10) ═══
e = editeur([R(100, 100, 100, 50), R(300, 100, 100, 50), R(100, 400, 100, 50)], { seuil: 6, rectangle: true });
e.S.appui({ x: 150, y: 120 }, { i: 0 }, {}); e.S.glisse({ x: 153, y: 122 }); e.S.relache();
controle('★★ sous le seuil : un clic, rien ne bouge, rien n\'est validé', e.L[0].x === 100 && e.valides.length === 0, JSON.stringify(e.L[0]));
e.S.appui({ x: 150, y: 120 }, { i: 0 }, {}); e.S.glisse({ x: 170, y: 120 }); e.S.relache();
controle('★★ au-delà : le glisser part', e.L[0].x === 120, JSON.stringify(e.L[0]));
e.S.vider();
controle('★★★ Maj + glisser dans le vide : rectangle de sélection', e.S.appui({ x: 50, y: 50 }, null, { shiftKey: true }) === true);
e.S.glisse({ x: 450, y: 200 });
controle('★★★ … sélectionne ce qu\'il touche, l\'éditeur a le rectangle à dessiner',
         JSON.stringify(e.S.sel.slice().sort()) === '[0,1]' && e.S.rectSel && e.S.rectSel.w === 400, JSON.stringify([e.S.sel, e.S.rectSel]));
e.S.relache();
controle('★ relâché : plus de rectangle, aucune géométrie touchée', !e.S.rectSel && e.L[1].x === 300);
e.S.appui({ x: 50, y: 350 }, null, { shiftKey: true }); e.S.glisse({ x: 250, y: 500 }); e.S.relache();
controle('★★ un second rectangle AJOUTE à la sélection', JSON.stringify(e.S.sel.slice().sort()) === '[0,1,2]', JSON.stringify(e.S.sel));
e = editeur([R(100, 100, 100, 50)]);
controle('★ sans l\'option, Maj dans le vide ne fait rien (éditeur qui ne dessine pas le rectangle)',
         e.S.appui({ x: 0, y: 0 }, null, { shiftKey: true }) === false);

// ═══ Verrous AU DOIGT (arbitrage du 30/10) ═══
e = editeur([R(100, 100, 100, 50), R(300, 100, 100, 50)], { rectangle: true });
e.S.multi = true;
e.S.appui({ x: 150, y: 120 }, { i: 0 }, {}); e.S.relache();
e.S.appui({ x: 350, y: 120 }, { i: 1 }, {}); e.S.relache();
controle('★★★ « Sélection multiple » : chaque appui AJOUTE (comme Maj+clic)', e.S.sel.length === 2, JSON.stringify(e.S.sel));
e.S.vider();
controle('★★★ … et un glisser dans le vide trace le RECTANGLE de sélection', e.S.appui({ x: 0, y: 0 }, null, {}) === true);
e.S.glisse({ x: 500, y: 200 }); e.S.relache();
controle('★ … qui sélectionne ce qu\'il touche', e.S.sel.length === 2);
e = editeur([R(100, 100, 200, 100, { format: 2 })]);
e.S.centre = true; e.S.selectionner(0);
e.S.appui({ x: 300, y: 200 }, { i: 0, poignee: true }, {}); e.S.glisse({ x: 350, y: 200 }); e.S.relache();
controle('★★★ « Depuis le centre » : la poignée agrandit autour du centre (comme Alt)', e.L[0].x === 50 && e.L[0].w === 300, JSON.stringify(e.L[0]));
const vz = B.verrousHtml({ multi: true, centre: false });
controle('★★ boutons COMMUNS : deux bascules à témoin du catalogue, état affiché',
         (vz.match(/data-bek-verrou=/g) || []).length === 2 && /class="ctl-push ctl-push--small ctl-push--led" data-bek-verrou="multi" aria-pressed="true"/.test(vz), vz);
const etat = { multi: false }, btns = [];
const racine = { querySelectorAll: () => btns };
btns.push({ dataset: { bekVerrou: 'multi' }, attrs: {}, addEventListener(t, f) { this.f = f; }, setAttribute(k, v) { this.attrs[k] = v; } });
B.lierVerrous(racine, etat); btns[0].f();
controle('★★ un appui sur le bouton bascule l\'état de la surface et son témoin', etat.multi === true && btns[0].attrs['aria-pressed'] === 'true');

// ═══ Clavier et historique (mêmes raccourcis partout) ═══
e = editeur([R(100, 100, 50, 50), R(300, 100, 50, 50)]);
let actif = true, ro = false;
const dits = [];
e.ed = e;
const snap = () => JSON.parse(JSON.stringify(e.L));
e.S.clavier({
    actif: () => actif, kind: 'test-objets', decalage: 20,
    etat: snap, restaurer: st => { e.L = st; },
    fragment: () => e.S.sel.length ? e.S.sel.map(i => Object.assign({}, e.L[i])) : null,
    poser: (frag, d) => { const n0 = e.L.length; frag.forEach(o => e.L.push(Object.assign({}, o, { x: o.x + d, y: o.y + d })));
                          e.S.vider(); for (let i = n0; i < e.L.length; i++) e.S.selectionner(i, { shiftKey: true }); return true; },
    supprimer: () => { const s = e.S.sel.slice().sort((a, b) => b - a); s.forEach(i => e.L.splice(i, 1)); return s.length > 0; },
    tout: () => { e.L.forEach((o, i) => e.S.selectionner(i, { shiftKey: true })); },
    lectureSeule: () => ro, dire: m => dits.push(m),
});
const valider0 = e.S;
e.S.selectionner(0);
touche('ArrowRight', { shiftKey: true });
e.S.memoriser();
controle('★★ flèche au clavier (Maj = 10)', e.L[0].x === 110, JSON.stringify(e.L[0]));
touche('z', { ctrlKey: true });
controle('★★★ Ctrl+Z annule', e.L[0].x === 100, JSON.stringify(e.L[0]));
touche('z', { ctrlKey: true, shiftKey: true });
controle('★★★ Ctrl+Maj+Z rétablit', e.L[0].x === 110, JSON.stringify(e.L[0]));
e.S.selectionner(0);
touche('c', { metaKey: true }); touche('v', { metaKey: true });
controle('★★★ ⌘+C / ⌘+V : copie posée, décalée, sélectionnée', e.L.length === 3 && e.L[2].x === 130 && e.S.sel.join() === '2',
         JSON.stringify([e.L, e.S.sel]));
touche('Delete');
controle('★★ Suppr retire la sélection', e.L.length === 2);
touche('z', { ctrlKey: true });
controle('★★ … et Ctrl+Z la rend', e.L.length === 3, e.L.length);
touche('a', { ctrlKey: true });
controle('★ Ctrl+A sélectionne tout', e.S.sel.length === 3);
touche('Escape');
controle('★ Échap désélectionne', e.S.sel.length === 0);
ro = true; e.S.selectionner(0); touche('Delete');
controle('★★ lecture seule : aucun geste qui écrit', e.L.length === 3 && dits.includes('lecture seule'));
ro = false; const n0 = e.L.length;
e.S.selectionner(0); e.S.agir('delete');
controle('★★★ les BOUTONS passent par les mêmes actions (S.agir) — le seul chemin au doigt', e.L.length === n0 - 1);
e.S.agir('undo');
controle('★★ … annuler par bouton', e.L.length === n0);
const acts = B.actionsEdition().map(x => x.action).join();
controle('★★ liste COMMUNE des actions d\'édition (une barre « Édition » identique partout)',
         acts === 'undo,redo,cut,copy,paste,duplicate,delete,help', acts);
ro = false; actif = false; touche('Delete');
controle('★ éditeur inactif : les raccourcis ne le touchent pas', e.L.length === 3);

if (echecs) { console.log('\n' + echecs + ' échec(s)'); process.exit(1); }
console.log('\nTout est vert.');
