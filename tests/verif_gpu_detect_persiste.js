#!/usr/bin/env node
// Le verdict de « Détecter GPU » doit SURVIVRE au redessin de la liste des nœuds.
//
// LA PANNE (Z8, 2026-10-28, recette custom-216e388666) : le testeur cliquait « Détecter GPU », le champ
// restait à « — » et le message explicatif n'était pas lisible. `detecterGpu` écrivait dans le span
// `#gpudet_<id>` puis appelait `rafraichirNodes()`, qui reconstruit `#nodes-list.innerHTML` — le span
// est donc RECRÉÉ VIDE et le message disparaissait aussitôt affiché. Or c'est exactement ce message
// (« aucun GPU NVIDIA détecté ») qui distingue « la carte est absente » de « je n'ai pas pu
// regarder » : l'effacer rend la panne muette.
//
// DEUX CONTRÔLES INDÉPENDANTS, et c'est volontaire :
//
//  (1) STATIQUE, lu dans le GABARIT : `rafraichirNodes` doit appeler `_restoreGpuDet()` après avoir
//      réassigné `innerHTML`. Sans ce contrôle, le contrôle dynamique ci-dessous ne prouverait rien :
//      ma doublure de `rafraichirNodes` appelle la restauration, donc elle passerait même si le vrai
//      `rafraichirNodes` ne l'appelait pas — une doublure ne peut pas contredire son auteur.
//
//  (2) DYNAMIQUE : on exécute le vrai `detecterGpu` dans un bac à sable dont le DOM REMPLACE
//      l'élément quand la liste est redessinée (ce que fait `innerHTML =`), et on exige que le
//      verdict soit de nouveau là à la fin.
//
// CE QUI N'EST PAS TESTÉ, ET POURQUOI : l'`await` devant `rafraichirNodes()`. On avait d'abord
// posé un contrôle d'ordre « redessin puis restauration » ; il passait AUSSI sans l'`await`, donc
// il ne distinguait rien. C'est que l'`await` n'est pas porteur : le message est sauvé par
// `_gpuDetMsg` + `_restoreGpuDet`, que le redessin appelle quand il arrive, awaité ou non. Un
// contrôle qui ne peut pas échouer sur ce qu'il prétend mesurer est pire que son absence.
//
//   node tests/verif_gpu_detect_persiste.js

const fs = require('fs'), path = require('path'), vm = require('vm');
const cible = process.argv[2] || path.join(__dirname, '..', 'templates', 'settings.html');
const tpl = fs.readFileSync(cible, 'utf8');
const echecs = [];

function extraire(entete) {
    const i = tpl.indexOf(entete);
    if (i < 0) throw new Error(`introuvable dans ${path.basename(cible)} : ${entete}`);
    let d = 0, vu = false;
    for (let j = i; j < tpl.length; j++) {
        if (tpl[j] === '{') { d++; vu = true; }
        else if (tpl[j] === '}') { if (--d === 0 && vu) return tpl.slice(i, j + 1); }
    }
    throw new Error(`accolade non refermée : ${entete}`);
}

// ── (1) Contrôle statique, dans le corps de `rafraichirNodes` ───────────────────────────────
// Les commentaires sont retirés d'abord : ce fichier et le gabarit CITENT tous deux le nom de la
// fonction de restauration, et une garde qui cherche un nom attrape volontiers sa propre note.
const corpsRafraichir = extraire('async function rafraichirNodes()');
const sansComm = corpsRafraichir.split('\n').map(l => l.split('//')[0]).join('\n');
if (!/\binnerHTML\s*=/.test(sansComm)) {
    echecs.push("rafraichirNodes ne réassigne plus innerHTML — cette garde a perdu son objet, la relire");
}
if (!/\b_restoreGpuDet\s*\(/.test(sansComm)) {
    echecs.push("rafraichirNodes reconstruit innerHTML sans appeler _restoreGpuDet() : le verdict de "
              + "« Détecter GPU » sera effacé par le redessin");
}

// ── (2) Contrôle dynamique ──────────────────────────────────────────────────────────────────
const src = [extraire('let _gpuDetMsg = {}') || '',
             extraire('function _restoreGpuDet()'),
             extraire('async function detecterGpu(id)')].join('\n');

// DOM minimal : un span par id. `redessiner()` le REMPLACE par un neuf et vide — c'est ce que fait
// `innerHTML =` sur le conteneur parent.
function faireDom() {
    const elements = new Map();
    const neuf = () => ({ style: {}, textContent: '' });
    elements.set('gpudet_1', neuf());
    return {
        elements,
        redessiner: () => elements.set('gpudet_1', neuf()),
        document: { getElementById: (id) => elements.get(id) || null },
    };
}

const MSG = 'aucun GPU NVIDIA détecté';

async function essai({ avecRestauration }) {
    const dom = faireDom();
    let ordre = [];
    const bac = {
        document: dom.document,
        T: (k) => k,
        fetch: async () => ({ json: async () => ({ gpu_capable: false, msg: MSG }) }),
        // La doublure fait ce que le gabarit a été prouvé faire en (1) : redessiner PUIS restaurer.
        rafraichirNodes: async () => {
            ordre.push('redessin');
            dom.redessiner();
            if (avecRestauration) { ordre.push('restauration'); bac._restoreGpuDet(); }
        },
        console,
    };
    bac.globalThis = bac;
    vm.createContext(bac);
    vm.runInContext(src + '\nthis._restoreGpuDet = _restoreGpuDet; this.detecterGpu = detecterGpu;', bac);
    await bac.detecterGpu(1);
    return { texte: dom.elements.get('gpudet_1').textContent, ordre };
}

(async () => {
    const r = await essai({ avecRestauration: true });
    if (!r.texte.includes(MSG)) {
        echecs.push(`après le redessin, le verdict a disparu (texte = ${JSON.stringify(r.texte)}) — `
                  + `le résultat de la détection n'est pas lisible par l'exploitant`);
    }

    // CONTRE-EXEMPLE : sans restauration, la garde DOIT constater la perte. Sans ce contrôle, un bac
    // à sable qui n'effacerait rien rendrait l'essai ci-dessus vert quoi qu'il arrive.
    const sans = await essai({ avecRestauration: false });
    if (sans.texte.includes(MSG)) {
        echecs.push("le bac à sable ne reproduit pas l'effacement : sans restauration le verdict "
                  + "survit quand même, donc l'essai positif ne mesure rien");
    }

    if (echecs.length) {
        console.log('ÉCHEC :');
        echecs.forEach(e => console.log('  - ' + e));
        process.exit(1);
    }
    console.log('OK — le verdict de « Détecter GPU » survit au redessin (contrôle statique + exécution, '
              + 'contre-exemple vérifié)');
})();
