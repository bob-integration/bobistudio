#!/usr/bin/env node
// Vérification hors-ligne de la fenêtre « Gérer les cœurs » (static/js/cores.js).
//
// ★ CE QUE CETTE GARDE TIENT, et pourquoi chacun compte :
//  1. le BARÈME DES POIDS vient du serveur, jamais recopié dans le JS — deux barèmes
//     divergeraient en silence, et la vue classerait les conteneurs selon l'ancien ;
//  2. la recherche par NUMÉRO DE CŒUR trouve les conteneurs qui tiennent ce cœur (c'est la
//     question qu'on se pose devant la grille : « qui est sur 24 ? ») ;
//  3. les filtres de protection et le filtre « poids bas » sont deux axes distincts ;
//  4. le tri met les non protégés en bas et les poids forts en haut (lecture par gravité) ;
//  5. un cœur cliqué filtre le tableau sur les conteneurs qui le touchent, frère HT compris ;
//  6. ★ « temps réel » n'est JAMAIS rendu pour un dédié (le serveur ne le publie plus ; le JS
//     ne doit pas le réinventer) ;
//  7. l'infobulle nomme QUI utilise chaque thread, avec sa place ET son poids.
//
// Le module est chargé tel quel (aucun accès DOM au chargement). Aucune base, aucun réseau.
//
//   node tests/verif_coeurs_ui.js

const fs = require('fs'), path = require('path');
const src = path.join(__dirname, '..', 'static', 'js', 'cores.js');
const code = fs.readFileSync(src, 'utf8');

// Faux global minimal : le module s'installe sur `window`.
const win = { t: (k) => k };
global.window = win;
global.document = { getElementById: () => null, createElement: () => ({ setAttribute() {}, appendChild() {}, querySelector: () => ({}) }), body: { appendChild() {} } };
global.fetch = () => { throw new Error('aucun réseau dans cette garde'); };
new Function('window', 'document', 'fetch', code)(win, global.document, global.fetch);
const BC = win.BobiCores;
const I = BC._interne;

let n = 0; const echecs = [];
function verifie(ok, titre, detail) {
    n++;
    console.log((ok ? '  ✓ ' : '  ✗ ') + titre + (!ok && detail ? '  — ' + detail : ''));
    if (!ok) echecs.push(titre);
}

// ─── Décor : la Z8 à l'échelle 1/6 — 8 cœurs logiques, 4 physiques (CORE = CPU mod 4) ────────
const L = (o) => Object.assign({
    vmid: 0, hostname: '', statut: 'running', cpuset_applique: '', place: 'partage', plafond: null,
    poids: { niveau: 'normal', valeur: 1024, source: 'default' }, poids_applique: 1024,
    poids_coherent: true, charge: null, charge_minorant: false, protection: 'non',
    protection_raison: {}, partage_avec: [], physiques: null,
}, o);

const J = {
    pool: '0-7', pool_cores: 8, ht_connu: true, releve: true,
    dedies: [
        L({ vmid: 101, hostname: 'mur', place: 'dedie', cpuset_reserve: '0-2', cores: 3,
            cpuset_applique: '0-2', coherent: true, exclusif: false, freres_partages: '4-6',
            siblings_dedicated: [], protection: 'partie', physiques: 3,
            poids: { niveau: 'onair', valeur: 2048, source: 'override' },
            partage_avec: [{ vmid: 104, hostname: 'stills', via: 'frere_ht' }], charge: 2.4 }),
    ],
    partages: [
        L({ vmid: 104, hostname: 'stills', cpuset_applique: '3-7', temps_reel: true, charge: 0.3,
            partage_avec: [{ vmid: 105, hostname: 'transcodeur', via: 'cpuset' }] }),
        L({ vmid: 105, hostname: 'transcodeur', cpuset_applique: '3-7', temps_reel: false,
            place: 'partage_plafonne', plafond: 2, charge: 1.9, charge_minorant: true,
            poids: { niveau: 'batch', valeur: 128, source: 'priority' },
            poids_applique: 1024, poids_coherent: false }),
    ],
    orphelins: [],
    coeurs: [0, 1, 2, 3].map((p) => ({
        phys: p,
        threads: [
            { cpu: p, kind: p < 3 ? 'dedie' : 'partage', vmids: p < 3 ? [101] : [], noms: p < 3 ? ['mur'] : [] },
            { cpu: p + 4, kind: 'partage', vmids: [], noms: [] },
        ],
        kind: p < 3 ? 'mixte' : 'partage', frere_partage: p < 3,
    })),
    synthese: { protege: 0, partie: 1, non: 2, inconnu: 0, poids_bas: 1, poids_haut: 1, poids_divergents: 1 },
    poids_niveaux: [{ niveau: 'onair', valeur: 2048 }, { niveau: 'normal', valeur: 1024 },
                    { niveau: 'background', valeur: 256 }, { niveau: 'batch', valeur: 128 }],
    partage: { cpuset: '3-7', cores: 5, conteneurs: 2, temps_reel: 1, physiques: 4,
               physiques_exclusifs: 1, freres_de_dedies: '4-6', bas: false, capacite_comptee: 1 },
    status: {},
};
const etat = (o) => Object.assign(I.nouvelEtat(1, 'z8'), { j: J }, o || {});

console.log('\n1. Lecture d\'un cpuset');
verifie(JSON.stringify(I.parseCpuset('0-2,5')) === '[0,1,2,5]', '1a. plages et listes',
        JSON.stringify(I.parseCpuset('0-2,5')));
verifie(I.parseCpuset('').length === 0 && I.parseCpuset(null).length === 0,
        '1b. vide et absent ne valent pas « cœur 0 »');

console.log('\n2. ★ Le barème des poids vient du SERVEUR');
verifie(I.seuilNormal(J) === 1024, '2a. le seuil « normal » est lu dans la réponse',
        String(I.seuilNormal(J)));
const J2 = Object.assign({}, J, { poids_niveaux: [{ niveau: 'normal', valeur: 4096 },
                                                  { niveau: 'batch', valeur: 512 }] });
verifie(I.poidsBas(J2, J.partages[1]) === true && I.poidsBas(J2, J.dedies[0]) === true,
        '2b. ★ si le serveur change le barème, le classement SUIT (2048 devient « bas » face à '
        + 'un normal à 4096) — un 1024 recopié dans le JS aurait figé l\'ancien barème');
verifie(I.poidsBas({}, J.partages[1]) === false,
        '2c. barème absent → on ne CLASSE PAS (pas de « bas » inventé sans référence)');
verifie(!/[^0-9]1024[^0-9]/.test(code.replace(/\/\/[^\n]*/g, '')),
        '2d. ★ et aucune valeur de poids n\'est écrite en dur dans le module (hors commentaire)');

console.log('\n3. Recherche et filtres');
const noms = (list) => list.map((l) => l.hostname).sort().join(',');
verifie(noms(I.filtrer(etat({ q: '5' }))) === 'stills,transcodeur',
        '3a. ★ « 5 » trouve les conteneurs qui TIENNENT le cœur 5 — la question qu\'on se pose '
        + 'devant la grille', noms(I.filtrer(etat({ q: '5' }))));
verifie(noms(I.filtrer(etat({ q: '1' }))) === 'mur',
        '3b. et « 1 » le dédié qui tient ce cœur-là', noms(I.filtrer(etat({ q: '1' }))));
verifie(noms(I.filtrer(etat({ q: 'trans' }))) === 'transcodeur', '3c. recherche par nom');
verifie(noms(I.filtrer(etat({ prot: 'partie' }))) === 'mur', '3d. filtre de protection');
verifie(noms(I.filtrer(etat({ prot: 'poids_bas' }))) === 'transcodeur',
        '3e. ★ « poids bas » est un AXE DISTINCT de la protection (un conteneur peut être '
        + 'protégé et à poids bas, ou non protégé et en antenne)',
        noms(I.filtrer(etat({ prot: 'poids_bas' }))));
verifie(noms(I.filtrer(etat({ core: 0 }))) === 'mur,stills,transcodeur',
        '3f. ★ cliquer le cœur physique 0 montre tout le monde dessus — le dédié ET les '
        + 'partagés sur son FRÈRE HT (c\'est le voisinage que rien d\'autre ne révèle)',
        noms(I.filtrer(etat({ core: 0 }))));
verifie(noms(I.filtrer(etat({ core: 3 }))) === 'stills,transcodeur',
        '3g. et un cœur sans dédié ne montre que les partagés', noms(I.filtrer(etat({ core: 3 }))));

console.log('\n4. Tri');
const ordre = (st) => I.trier(st, I.lignes(J)).map((l) => l.hostname).join(',');
verifie(ordre(etat({ sort: 'prot', dir: 1 })) === 'mur,stills,transcodeur',
        '4a. protection : « en partie » avant « non protégé »', ordre(etat({ sort: 'prot', dir: 1 })));
verifie(ordre(etat({ sort: 'poids', dir: 1 })) === 'mur,stills,transcodeur',
        '4b. poids : le plus fort en haut', ordre(etat({ sort: 'poids', dir: 1 })));
verifie(ordre(etat({ sort: 'charge', dir: 1 })) === 'mur,transcodeur,stills',
        '4c. charge mesurée : la plus forte en haut', ordre(etat({ sort: 'charge', dir: 1 })));
verifie(ordre(etat({ sort: 'charge', dir: -1 })) === 'stills,transcodeur,mur',
        '4d. et le sens s\'inverse');

console.log('\n5. ★ Ce que la fenêtre NE DIT PAS');
// Le COMPTE du pool (`partage.temps_reel`) reste légitime : c'est lui qui dit « trop peu de
// cœurs pour les conteneurs temps réel qui s'y partagent ». Ce qui est interdit, c'est de
// l'accrocher à une LIGNE de conteneur.
verifie(!/\bl\.temps_reel\b/.test(code) && /p\.temps_reel/.test(code),
        '5a. ★ aucune LIGNE de conteneur n\'affiche « temps réel » (seul le compte du POOL le '
        + 'fait) : sur un dédié c\'était une tautologie présentée comme une qualité, juste à '
        + 'côté d\'un dédié qui ne tenait pas sa cadence (Z8, 30/10)');
verifie(/settings\.cores\.poids_aide/.test(code),
        '5b. et la limite du poids est ÉCRITE dans la fenêtre (il n\'agit que sous saturation, '
        + 'rien sur un cœur dédié, aucune protection contre le frère HT)');

console.log('\n6. ★ Les orphelins sont NOMMÉS, pas seulement comptés');
// Régression attrapée en relecture : la première version de la fenêtre n'avait qu'un bouton
// « Libérer les orphelins (n) ». C'est l'erreur du 28/10 remontée d'un cran — « used 49 /
// free 2 » était exact et inutilisable parce qu'il ne désignait personne.
verifie(/settings\.cores\.orphelin'\)/.test(code) && /bc-orph-l/.test(code),
        '6z-a. ★ chaque réservation orpheline est listée avec son cpuset');
verifie(/bc-lib-un/.test(code) && /data-vmid="' \+ x\.vmid/.test(code),
        '6z-b. et chacune a son propre bouton de libération (libérer UNE réservation, pour voir '
        + 'l\'effet avant de tout rendre)');
verifie(/settings\.cores\.perime/.test(code) && /settings\.cores\.incoherent/.test(code),
        '6z-c. ★ deux défauts de cpuset, deux phrases : un DÉDIÉ sur son ancienne réservation '
        + '(non recréé) et un PARTAGÉ au cpuset PÉRIMÉ (que « Réappliquer » règle)');
verifie(/settings\.cores\.reappliquer_aide/.test(code) && /settings\.cores\.sursouscrit/.test(code),
        '6z-d. l\'aide du bouton « Réappliquer » et l\'avis de sur-souscription sont conservés');

console.log('\n6. Place, poids, protection : le vocabulaire');
verifie(I.libellePlace(J.partages[1]) === 'settings.cores.place.partage_plafonne'.replace('x', 'x'),
        '6a. une place « plafonnée » a son propre libellé (ce n\'est pas un poids)',
        I.libellePlace(J.partages[1]));
verifie(I.libellePlace(J.dedies[0]) === 'settings.cores.place.dedie', '6b. et « dédié » le sien');
verifie(/BobiControls\.create\('buttons'/.test(code),
        '6c. ★ le sélecteur de poids est un contrôle du CATALOGUE PARTAGÉ, pas un bouton '
        + 'redessiné ici (règle CLAUDE.md)');
verifie(/data-phys/.test(code) && /<button type="button" class="bc-phys/.test(code),
        '6d. chaque cœur de la grille est un VRAI bouton : focusable, activable au clavier');
verifie(/onmouseenter = b\.onfocus/.test(code),
        '6e. ★ et l\'infobulle sort au SURVOL comme au FOCUS clavier — une information, pas une '
        + 'récompense de souris');

console.log('\n7. Aucun texte en dur');
// Deux formes de texte en dur, deux contrôles — la première version n'attrapait que la
// première, et un libellé glissé DANS un fragment HTML (« …class="bc-tip-t">Occupation du
// cœur… ») passait au travers.
const litteraux = code.match(/'(?:[^'\\\n]|\\.)*'/g) || [];
const mot = /[A-Za-zÀ-ÿ]{4,}/;
const dur = litteraux
    .filter((s) => /^'[A-ZÀ-Ý]/.test(s) && s.length > 8)
    .filter((s) => !/^'(settings|monitoring|layout|js)\./.test(s))
    .filter((s) => !/^'(Content-Type|POST|GET)/.test(s));
verifie(dur.length === 0, '7a. aucun libellé littéral : tout passe par i18n',
        dur.slice(0, 4).join(' | '));
// Fragments HTML : on retire le balisage et il ne doit RESTER aucun mot — seulement de la
// ponctuation (« · », « (», « +# »). Un libellé oublié entre deux balises se voit ainsi.
// Jetons TECHNIQUES tolérés : ce sont du balisage, pas de la langue. Liste courte et FERMÉE —
// y ajouter un mot français viderait le contrôle de son sens.
const TECH = /^(class|style|type|button|div|span|table|thead|tbody|tr|td|th|code|input|label|search|placeholder|title|role|tooltip|group|radiogroup|aria|hidden|pressed|selected|colspan|data|vmid|phys|sort|prot|onglet|true|false|text|number|flex|margin|meta|warn|bad|info|null)$/;
const fragments = litteraux.filter((s) => s.indexOf('<') >= 0 || s.indexOf('="') >= 0)
    .map((s) => s.replace(/^'|'$/g, '')
                 .replace(/<[^>]*>?/g, ' ')
                 .replace(/[a-zA-Z-]+="?[^"']*"?/g, ' ')
                 .replace(/[<>]/g, ' '))
    .map((t) => (t.match(/[A-Za-zÀ-ÿ]{4,}/g) || []).filter((w) => !TECH.test(w.toLowerCase())))
    .filter((w) => w.length);
verifie(fragments.length === 0,
        '7b. ★ et aucun mot ne subsiste dans un fragment HTML après retrait des balises — '
        + 'un libellé glissé juste après un « > » n\'est pas moins en dur',
        fragments.slice(0, 4).map((w) => w.join(' ')).join(' | '));

console.log('\n' + (echecs.length ? 'ÉCHEC' : 'OK') + ' — ' + n + ' contrôle(s), '
            + echecs.length + ' échec(s)');
echecs.forEach((e) => console.log('  ✗ ' + e));
process.exit(echecs.length ? 1 : 0);
