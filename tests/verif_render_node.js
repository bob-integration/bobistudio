#!/usr/bin/env node
// Détecte les LOCALES ORPHELINES de `renderNode` (page Câbles) — un usage qui a survécu à sa
// déclaration après un retrait de code.
//
// Le 2026-08-18 la page n'affichait plus que 4 cartes sur 7 et AUCUN câble. Le commit 6042578
// avait supprimé en bloc la ligne
//
//     const _fpsC = n.fps_content, _fpsN = parseFloat(n.fps);
//
// alors que son SECOND terme servait encore trente lignes plus bas. `_fpsN` n'était plus déclaré
// nulle part → `ReferenceError`, mais SEULEMENT sur les nœuds sans `fps_nominal` (streamer,
// moniteur, pyramide) : la branche fautive est le `else` d'un ternaire. `renderNode` étant appelé
// dans un `.map()` par colonne, la colonne entière échouait et `renderTopology` rendait les armes
// AVANT `drawTopoEdges()` — cartes partielles ET zéro câble, deux symptômes sans lien apparent.
// `node --check` est aveugle : c'est une erreur d'EXÉCUTION, pas de syntaxe.
//
// PRINCIPE — `renderNode` est extraite du gabarit et exécutée dans un bac à sable dont la portée
// globale est un Proxy qui ENREGISTRE chaque identifiant lu. Un nom lu globalement est ensuite
// confronté aux DÉCLARATIONS du gabarit : est orpheline la locale lue globalement ET déclarée
// nulle part. Le préfixe `_` ne suffit pas comme critère — il sert dans ce fichier AUSSI BIEN
// aux locales de fonction qu'aux globales de module (`_latMode`, `_nidOf`, `_isCollapsed`).
// Aucun lexer : on ne cherche une déclaration que pour la poignée de noms réellement atteints.
//
// La matrice couvre les formes du parc, `fps_nominal` absent EN TÊTE : c'est le cas qui cassait.
//
//   node tools/verif_render_node.js                 # le gabarit courant
//   node tools/verif_render_node.js <cables.html>    # une autre version (bissection)

const fs = require('fs'), path = require('path'), vm = require('vm');
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

// Bouchon universel : appelable, indexable, concaténable — pour que l'exécution aille au BOUT
// de la fonction plutôt que de s'arrêter au premier appel non bouchonné.
function bouchon() {
    const f = function () { return f; };
    return new Proxy(f, {
        get: (t, k) => (k === Symbol.toPrimitive || k === 'toString' || k === Symbol.toStringTag)
            ? (() => '') : (k === 'length' ? 0 : bouchon()),
        apply: () => bouchon(),
    });
}

// Déclarations du gabarit. Les commentaires de ligne sont retirés d'abord : ce fichier CITE
// abondamment du code — le commentaire qui documente le présent défaut mentionne `_fpsN`, et
// sans ce retrait le test se satisferait de sa propre note explicative.
const _sansCommentaires = tpl.split('\n').map(l => l.split('//')[0]).join('\n');
function declare(nom) {
    const n = nom.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`(?:const|let|var)\\s+(?:[^;\n]*,\\s*)?${n}\\b`).test(_sansCommentaires)
        || new RegExp(`function\\s+${n}\\s*\\(`).test(_sansCommentaires);
}

const lus = new Set();
// `T` et `escapeHtml` sont RÉELS (identité) et non bouchonnés : sans eux le HTML rendu serait
// vide de tout libellé, et on ne pourrait rien affirmer sur le CONTENU des badges.
// `T` sert le VRAI catalogue : renvoyer la clé suffirait à « ne pas lever », mais les gabarits
// à trous (`{s}`, `{n}`) ne seraient jamais substitués et le banc validerait des badges vides.
const CATALOGUE = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'i18n', 'fr.json'), 'utf8'));
const reels = { Math, JSON, Object, Array, String, Number, Boolean,
                parseFloat, parseInt, isNaN, isFinite, encodeURIComponent,
                T: (k) => (CATALOGUE[k] !== undefined ? CATALOGUE[k] : String(k)),
                escapeHtml: (x) => String(x),
                // `renderNode` emploie DEUX portes d'entrée i18n : `T()` et `window.t()` (module
                // projet). Remplacer `window` par un objet réel sans `t` faisait lever — l'oubli a
                // été révélé par le cas « module projet », pas deviné.
                window: { LANG: 'fr', t: (k) => (CATALOGUE[k] !== undefined ? CATALOGUE[k] : String(k)) } };

// Les helpers d'axe B vivent hors de `renderNode` : sans les extraire aussi, ils seraient des
// bouchons et tout ce qu'ils calculent (budget, trames, ms) sortirait vide — le banc « passerait »
// en ne mesurant rien. Constaté : `/ 20 ms`, `21 %` et `40 ms` manquaient tous.
// `renderPort` était un BOUCHON : aucun badge de PORT n'était rendu, donc le banc ne pouvait
// rien affirmer dessus. C'est ce qui lui a fait rater le ⇣ de latence de réception, resté visible
// sous « Masqué » — signalé par l'utilisateur. On l'extrait donc pour de bon.
const HELPERS = ['_periodeMs', '_fmtTrames', 'renderPlaceholderInput', 'renderPort'];

// ⚠ `_latMode` doit être une VRAIE valeur, pas un bouchon : avec un bouchon, `_latMode === 'charge'`
// est faux partout et AUCUNE branche de mode n'est exercée — le banc passerait sans rien tester du
// rendu des badges. Chaque cas est donc rejoué dans les trois modes.
const MODES = ['charge', 'delai', 'delai_cum'];

function lancer(n, mode, latShow = true) {
    const sandbox = new Proxy({}, {
        has: () => true,
        get: (t, k) => {
            if (k === Symbol.unscopables) return undefined;
            const nom = String(k);
            if (nom === '_latMode') return mode;
            if (nom === '_latShow') return latShow;
            if (nom in reels) return reels[nom];
            lus.add(nom);
            return bouchon();
        },
    });
    // IIFE : les helpers et `renderNode` partagent une portée LEXICALE (donc renderNode les voit),
    // et tout identifiant non déclaré retombe malgré tout sur le Proxy global — l'un n'annule pas
    // l'autre, la détection d'orphelines reste valide.
    const prog = '(function(){\n' + HELPERS.map(extraire).join('\n') + '\n'
               + extraire('renderNode') + '\nreturn renderNode;})()';
    const fn = vm.runInContext(prog, vm.createContext(sandbox));
    return String(fn(n) ?? '');
}

// ─── Matrice : les formes réellement observées dans le parc ──────────────────
const base = { kind: 'streamer', status: 'running', vmid: 986, fps: 25,
               consumes: [], produces: [] };
const SORTIE = (sig) => [{ shm: 'out', kind: 'video', delai_signal: sig }];
const cas = [
    ['fps_nominal ABSENT (streamer, moniteur) — le cas qui cassait', { fps_nominal: null }],
    ['fps_nominal absent + shards mesurés',        { fps_nominal: null, fps_shard_min: 22, fps_shard_maillon: 's1' }],
    ['fps_nominal absent + trames perdues',        { fps_nominal: null, shard_frames_missed: 3 }],
    ['fps_nominal absent + cadence tenue',         { fps_nominal: null, cadence: { cible: 25, mesure: 25, tenue: true } }],
    ['fps_nominal = 0',                            { fps_nominal: 0 }],
    ['fps_nominal présent (mur, moteur 2110)',     { fps_nominal: 50, fps: 50 }],
    ['fps absent ET fps_nominal absent',           { fps_nominal: null, fps: null }],
    ['verdict de contenu neuf en défaut',          { fps_nominal: null, contenu_etat: { tenue: false, mesure: 12, ref: 25 } }],
    ['verdict de contenu neuf, maillon nommé',     { fps_nominal: 50, contenu_etat: { tenue: false, maillon: 'shard-2', maillon_mesure: 20, ref: 50 } }],
    ['module projet',                              { fps_nominal: null, project_module: true, project_state: 'active' }],
    ['arrêté',                                     { fps_nominal: null, status: 'stopped' }],
    ['multiview (entrées fantômes)',               { fps_nominal: 50, kind: 'multiview', max_inputs: 8 }],
    // ── Axe B : les formes que produit home_dashboard ────────────────────────────────────
    ['délai d\'étage mesuré',                       { fps_nominal: 50, delai_etage: { trames: 2, trames_max: 2, ms: 40 },
                                                     produces: SORTIE({ ms: 60, trames: 3, complet: true, manquants: [] }) }],
    ['délai d\'étage NON mesuré (doit rester muet)', { fps_nominal: 50, delai_etage: null,
                                                     produces: SORTIE({ ms: 20, trames: 1, complet: false, manquants: ['pyramide'] }) }],
    ['cumul INCOMPLET, étages nommés',              { fps_nominal: 50,
                                                     produces: SORTIE({ ms: 20, trames: 1, complet: false, manquants: ['pyramide', 'udc'] }) }],
    ['émission 2110 estimée (moitié TX)',           { fps_nominal: 50, col: 'sinks',
                                                     delai_emission: { trames: 1, ms: 20, mesure: false, estime: true } }],
    ['cadence nominale absente → pas de budget',    { fps_nominal: null, cadence: {}, delai_etage: { trames: 2, ms: null },
                                                     produces: SORTIE({ ms: 60, trames: null, complet: true, manquants: [] }) }],
    ['delai_signal à zéro (générateur)',            { fps_nominal: 50, produces: SORTIE({ ms: 0, trames: 0, complet: true, manquants: [] }) }],
];

let ko = 0;
console.log(`renderNode — locales orphelines (chaque cas rejoué en ${MODES.length} modes) :`);
for (const [nom, sur] of cas) {
    lus.clear();
    let err = null, ouMode = '', rendu = {};
    for (const mode of MODES) {
        try { rendu[mode] = lancer({ ...base, ...sur }, mode); lancer({ ...base, ...sur }, mode, false); }
        catch (e) { if (!err) { err = e; ouMode = mode; } }
    }
    const orphelines = [...lus].filter(x => x.startsWith('_') && !declare(x)).sort();
    const ok = !err && orphelines.length === 0;
    if (!ok) ko++;
    console.log(`${ok ? '  ok  ' : ' ÉCHEC'} ${nom}`);
    if (err) console.log(`         [mode ${ouMode}] ${err.constructor.name}: ${err.message}`);
    if (orphelines.length) console.log(`         orpheline(s) : ${orphelines.join(', ')}`);
}
// ── Le SÉLECTEUR doit produire des modes que le rendu connaît ─────────────────────────────
// Le banc injectait `_latMode` directement : il validait le RENDU des trois modes sans jamais
// vérifier que l'interface sait les PRODUIRE. Le setter écrivait 'step' pour les trois boutons,
// valeur morte — badges disparus au premier clic, et le banc restait vert.
console.log('\nSélecteur de mode :');
{
    const src = extraire('cablesSetLatMode');
    const boutons = [...tpl.matchAll(/cablesSetLatMode\('([^']+)'\)/g)].map(m => m[1]);
    const modesConnus = new Set(MODES);
    for (const b of boutons) {
        let ecrit = null;
        const bac = { _LAT_MODES: MODES, _latShow: true, _latMode: null,
                      localStorage: { setItem: (k, v) => { if (k.endsWith('latmode')) ecrit = v; } },
                      _latSyncButtons: () => {}, renderTopology: () => {}, window: {},
                      document: { getElementById: () => null } };
        const sandbox = new Proxy(bac, { has: () => true,
            get: (t, k) => (k === Symbol.unscopables ? undefined : (k in t ? t[k] : undefined)),
            set: (t, k, v) => { t[k] = v; return true; } });
        try { vm.runInContext('(' + src + ')', vm.createContext(sandbox))(b); } catch (e) { ecrit = 'ERREUR ' + e.message; }
        const ok_ = (b === 'off') ? (ecrit === null) : modesConnus.has(ecrit);
        if (!ok_) ko++;
        console.log(`${ok_ ? '  ok  ' : ' ÉCHEC'} bouton « ${b} » → mode « ${ecrit} »`);
        if (!ok_ && b !== 'off') console.log(`         attendu l'un de : ${MODES.join(', ')}`);
    }
}

// ── Contenu des badges : les deux axes doivent rendre des choses DIFFÉRENTES ──────────────
console.log('\nContenu des badges par mode :');
function contenu(nom, surcharge, mode, doit, nedoitpas, latShow = true) {
    let h = '';
    try { h = lancer({ ...base, ...surcharge }, mode, latShow); } catch (e) { h = 'ERREUR: ' + e.message; }
    const okDoit = doit.every(x => h.includes(x));
    const okPas  = (nedoitpas || []).every(x => !h.includes(x));
    const bon = okDoit && okPas;
    if (!bon) ko++;
    console.log(`${bon ? '  ok  ' : ' ÉCHEC'} [${mode}] ${nom}`);
    if (!bon) {
        if (!okDoit) console.log(`         manque : ${doit.filter(x => !h.includes(x)).join(' | ')}`);
        if (!okPas)  console.log(`         ne devrait pas contenir : ${(nedoitpas || []).filter(x => h.includes(x)).join(' | ')}`);
    }
}
const AVEC_CALCUL = { fps_nominal: 50, own_latency_ms: 4.3 };
const AVEC_ETAGE  = { fps_nominal: 50, own_latency_ms: 4.3,
                      delai_etage: { trames: 2, trames_max: 2, ms: 40 },
                      produces: SORTIE({ ms: 60, trames: 3, complet: true, manquants: [] }) };
const INCOMPLET   = { fps_nominal: 50, own_latency_ms: 4.3,
                      produces: SORTIE({ ms: 20, trames: 1, complet: false, manquants: ['pyramide'] }) };
const TX_ESTIME   = { fps_nominal: 50, col: 'sinks',
                      delai_emission: { trames: 1, ms: 20, mesure: false, estime: true } };

// AXE A : le calcul s'affiche en RATIO DE BUDGET, jamais en ms nue.
// 4,3 / 20 ms = 21,5 % → 22 % (arrondi au supérieur à la demie). Attendu écrit d'après le calcul,
// pas d'après l'intuition : la première rédaction attendait « 21 % » et le banc l'a démentie.
contenu('calcul rendu en budget « 4.3 / 20 ms · 22 % »', AVEC_CALCUL, 'charge', ['⧖', '4.3', '/ 20 ms', '22 %']);
// …et il DISPARAÎT sur l'axe B : c'est tout l'objet de la séparation.
contenu('le temps de calcul n\'apparaît PAS', AVEC_ETAGE, 'delai', ['⧗'], ['⧖', '4.3']);
contenu('le temps de calcul n\'apparaît PAS', AVEC_ETAGE, 'delai_cum', ['Σ'], ['⧖', '4.3']);
// AXE B : trames mesurées, minorant nommé, estimation étiquetée, absence assumée.
contenu('délai d\'étage en TRAMES', AVEC_ETAGE, 'delai', ['⧗', '2 img', '40 ms'], ['propagé']);
// Zéro STRUCTUREL (coordonnée source propagée) vs zéro MESURÉ : les deux doivent rester lisibles
// l'un de l'autre, sinon « 0,00 img » veut dire deux choses différentes sous le même badge.
contenu('index propagé → 0 nommé « (propagé) » et marqué partiel',
        { fps_nominal: 50, delai_etage: { trames: 0, ms: 0, propage: true } }, 'delai',
        ['⧗', '0 img', 'propagé', 'topo-delay-partiel']);
contenu('zéro MESURÉ d\'un étage qui re-cadence → pas de mention « propagé »',
        { fps_nominal: 50, delai_etage: { trames: 0, ms: 0, propage: false } }, 'delai',
        ['⧗', '0 img'], ['propagé', 'topo-delay-partiel']);
contenu('cumul complet préfixé Σ', AVEC_ETAGE, 'delai_cum', ['Σ', '3', '60 ms'], ['≥']);
contenu('cumul incomplet préfixé ≥ et étage NOMMÉ', INCOMPLET, 'delai_cum',
        ['≥', 'pyramide', 'MINORANT', 'topo-delay-partiel'], ['Σ ']);
contenu('étage non mesuré → dit « non mesuré », pas « 0 »', INCOMPLET, 'delai',
        ['délai non mesuré'], ['⧗ 0']);
contenu('émission 2110 préfixée ~ et marquée partielle', TX_ESTIME, 'delai',
        ['~', 'topo-delay-partiel', 'ESTIMÉE']);

// ── « Masqué » : plus AUCUN badge de latence, dans les TROIS modes ────────────────
// `_latShow` ne conditionnait que l'étiquette des câbles ; depuis que les axes Délai n'en portent
// plus, « Masqué » y était un no-op silencieux. Signalé par l'utilisateur, pas par le banc.
console.log('\nBouton « Masqué » :');
for (const m of MODES) {
    contenu('aucun badge de latence', AVEC_ETAGE, m, [], ['⧖', '⧗', 'Σ ', 'topo-node-own-lat'], false);
}
contenu('la carte reste rendue (fps conservé)',
        { ...AVEC_ETAGE, cadence: { cible: 50, mesure: 50, tenue: true } },
        'charge', ['topo-node-fps'], ['⧖'], false);

// ── Badges de PORT : le ⇣ de réception est une LATENCE (masquable), le ⚠ une PANNE (jamais) ──
const RX_2110 = { fps_nominal: 50, produces: [{ shm: 'rx0', kind: 'video', rx_latency_ms: 19.3 }] };
const RX_STALL = { fps_nominal: 50, produces: [{ shm: 'rx0', kind: 'video', rx_stalled: true }] };
contenu('⇣ segment A visible en mode Charge', RX_2110, 'charge', ['⇣', '19 ms']);
contenu('⇣ segment A ÉTEINT par Masqué', RX_2110, 'charge', [], ['⇣', '19 ms'], false);
contenu('⚠ abonné sans flux : PANNE, reste sous Masqué', RX_STALL, 'charge', ['⚠'], [], false);
const ALIGNE = { fps_nominal: 50, kind: 'mixer',
                 consumes: [{ shm: 'a', kind: 'video', slot: 0, is_ref: false, skew_ms: 12, late: false }] };
const RETARD = { fps_nominal: 50, kind: 'mixer',
                 consumes: [{ shm: 'a', kind: 'video', slot: 0, is_ref: false, skew_ms: 40, late: true }] };
contenu('écart d\'alignement visible en Charge', ALIGNE, 'charge', ['+12 ms']);
contenu('écart d\'alignement ÉTEINT par Masqué', ALIGNE, 'charge', [], ['+12 ms'], false);
contenu('entrée EN RETARD : panne, reste sous Masqué', RETARD, 'charge', ['align-late'], [], false);

// ── Cumul sur un SINK : porté par ses ENTRÉES, et en TRAMES SEULES (anti-débordement) ─────
// « de combien est décalé ce que je regarde ? » n'avait aucune réponse : le cumul n'était posé
// que sur des sorties, et un moniteur n'en a pas. Signalé par l'utilisateur.
const SINK = { fps_nominal: 50, kind: 'streamer', produces: [],
               consumes: [{ shm: 'mix', kind: 'video', slot: 0,
                            delai_signal: { ms: 79.3, trames: 3.96, complet: true, manquants: [] } }] };
const SINK_INC = { fps_nominal: 50, kind: 'streamer', produces: [],
                   consumes: [{ shm: 'mix', kind: 'video', slot: 0,
                                delai_signal: { ms: 19.3, trames: 0.97, complet: false, manquants: ['mixer'] } }] };
// ⚠ Attendu écrit d'après le rendu réel : `toLocaleString('fr')` met une VIRGULE décimale.
// Ma première rédaction attendait « 3.96 img » et le banc l'a démentie.
contenu('sink : cumul lu sur son ENTRÉE', SINK, 'delai_cum', ['Σ', '3,96 img']);
// Le badge de PORT ne porte que les trames ; le badge de NŒUD garde les ms (un seul par carte,
// donc sans risque de débordement). L'assertion vise donc la fermeture exacte du span de port —
// une vérification sur toute la carte attrapait le badge de nœud et mentait.
contenu('badge de PORT : trames seules, ms en infobulle', SINK, 'delai_cum',
        ['>Σ 3,96 img</span>', 'title="', ' — 79 ms']);
contenu('badge de NŒUD : garde les ms', SINK, 'delai_cum', ['3,96 img (79 ms)']);
contenu('sink incomplet : ≥ et étage nommé', SINK_INC, 'delai_cum', ['≥', 'mixer']);
contenu('sink : rien en mode Charge', SINK, 'charge', [], ['Σ', '3,96 img']);
contenu('sink : rien sous Masqué', SINK, 'delai_cum', [], ['Σ', '3,96 img'], false);

// ── Axe Délai : AUCUNE tuile muette sur un nœud qui tourne ────────────────────────────────
// La condition portait `produces.length` : tous les sinks (moniteur, streamer, TX) rendaient
// un badge VIDE, ce qui se lit comme un mode cassé. Signalé par l'utilisateur sur le 987.
const SINK_NU = { fps_nominal: 50, kind: 'streamer', status: 'running', produces: [],
                  consumes: [{ shm: 'mix', kind: 'video', slot: 0 }] };
contenu('sink sans mesure → « non mesuré », jamais vide', SINK_NU, 'delai', ['délai non mesuré']);
contenu('nœud ARRÊTÉ → pas de badge (rien à dire)',
        { ...SINK_NU, status: 'stopped' }, 'delai', [], ['délai non mesuré']);
// Le premier maillon : la réception 2110 est MESURÉE, elle doit se voir sur l'axe Délai.
const RX_ETAGE = { fps_nominal: 50, status: 'running',
                   delai_etage: { trames: 0.97, trames_max: 0.97, ms: 19.3, propage: false, reception: true },
                   produces: [{ shm: 'rx0', kind: 'video', rx_latency_ms: 19.3 }] };
contenu('réception 2110 affichée et NOMMÉE', RX_ETAGE, 'delai', ['⧗', '0,97 img', 'réception']);
contenu('réception ≠ propagé (libellés distincts)', RX_ETAGE, 'delai', ['réception'], ['propagé']);

// ── TX 2110 : le cumul de la carte doit inclure l'émission ────────────────────────────────
// Il affichait exactement le chiffre de sa source alors qu'on lui compte une image : l'écart
// passait à la trappe. Signalé par l'utilisateur.
const TX_FIL = { fps_nominal: 50, status: 'running', col: 'sinks', produces: [],
                 consumes: [{ shm: 'mv', kind: 'video', slot: 0,
                              delai_signal: { ms: 59.3, trames: 2.96, complet: true, manquants: [] } }],
                 delai_emission: { trames: 1.0, ms: 20.0, mesure: false, estime: true },
                 delai_fil: { ms: 79.3, trames: 3.96, complet: true, manquants: [], estime: true } };
contenu('TX : la carte montre le FIL (3,96), pas l\'arrivée (2,96)', TX_FIL, 'delai_cum',
        ['3,96 img'], ['≈ 2,96 img']);
contenu('TX : préfixe ≈ (contient une estimation), pas Σ', TX_FIL, 'delai_cum',
        ['≈ ', 'topo-delay-partiel'], ['Σ 3,96']);
// L'incomplétude prime sur l'estimation : un minorant reste un minorant.
const TX_INC = { ...TX_FIL, delai_fil: { ms: 79.3, trames: 3.96, complet: false,
                                          manquants: ['mixer'], estime: true } };
contenu('TX incomplet ET estimé → ≥ l\'emporte, étage nommé', TX_INC, 'delai_cum',
        ['≥ ', 'mixer'], ['≈ ']);

console.log(ko === 0 ? '\nTout est conforme.' : `\n${ko} contrôle(s) en échec.`);
process.exit(ko ? 1 : 0);
