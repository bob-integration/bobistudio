// SPDX-License-Identifier: GPL-3.0-or-later
// Monte la console de la sonde hors navigateur et vérifie ce qu'elle PRODUIT.
//
// Pourquoi : `node --check` ne valide que la syntaxe. Les pannes réelles de cette console ont
// toutes été sémantiques — une variable orpheline qui tue une colonne entière, un rendu qui
// lève sur un slot sans mesure. Il faut donc exécuter le rendu, avec des états qui ressemblent
// au parc : un slot complet, un slot sans audio, un slot sans AUCUNE mesure.
//
//   node tools/verif_sonde_chrono.js
const fs = require('fs');
let ko = 0;
const ok = (c, t, d) => { console.log((c ? '  ok  ' : ' ÉCHEC') + ' ' + t); if (!c) { ko++; if (d) console.log('         ' + d); } };

function noeud(tag) {
  const n = {
    tag, _html: '', dataset: {}, hidden: false, textContent: '', value: '', checked: false,
    children: [], attributes: {},
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    addEventListener() {}, setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    querySelector(sel) { return this._q(sel); },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  n._q = (sel) => { const m = sel.match(/data-el="([^"]+)"/); return noeud(m ? m[1] : 'div'); };
  return n;
}

const RACINE = noeud('div');
const zone = {};                       // data-el → nœud persistant
RACINE.querySelector = (sel) => {
  const m = sel.match(/data-el="([^"]+)"/) || sel.match(/data-act="([^"]+)"/);
  const k = m ? m[1] : sel;
  return (zone[k] = zone[k] || noeud(k));
};
RACINE.querySelectorAll = () => [];
RACINE.addEventListener = () => {};

global.window = { t: (k) => k, MXLPlugins: {} };
global.document = { activeElement: null };
const ETATS = {};
global.fetch = async () => ({ ok: true, json: async () => ETATS.courant });

eval(fs.readFileSync('plugins/sonde_latence/control.js', 'utf8'));
const P = global.window.MXLPlugins.sonde_latence;
ok(!!P && typeof P.mount === 'function', 'la console s\'enregistre dans window.MXLPlugins');

const slot = (o) => Object.assign({
  flux: 'x', etat: 'ok', etat_code: 'ok', age_moy: 1.0, age_trames: 1, age_min: 1, age_max: 1,
  bandes_valides: 8, bandes_total: 8, dechire_taux: 0, fps: 50, grain: 1, index_image: 1,
  mesures: 50, ecart_av_resolution_ms: 20, audio_flux: null, age_audio_ms: null, ecart_av_ms: null,
}, o);

const CAS = {
  'parc mixte': { plugin_version: '0.14.0', bands: 8, window: 50, regions: {}, panneau: {},
    slots: {
      '0': slot({ flux: 'avsync', audio_flux: 'avsync_audio', age_audio_ms: 2.0, ecart_av_ms: -18.0 }),
      '1': slot({ flux: 'mixer-pvw', etat_code: 'no_grain', age_moy: null, age_trames: null,
                  bandes_valides: null, bandes_total: null, fps: null }),
      '2': slot({ flux: 'stream-in', age_moy: 27.9, audio_flux: 'stream-in_audio',
                  age_audio_ms: 61725.7, ecart_av_ms: 61185.7 }),
    } },
  'aucun flux câblé': { plugin_version: '0.14.0', bands: 8, window: 50, regions: {}, panneau: {}, slots: {} },
  'un seul slot, sans audio': { plugin_version: '0.14.0', bands: 8, window: 50, regions: {}, panneau: {},
    slots: { '0': slot({ flux: 'solo' }) } },
};

for (const [nom, st] of Object.entries(CAS)) {
  ETATS.courant = st;
  let leve = null;
  try { P.mount(RACINE, 42, {}); } catch (e) { leve = e; }
  ok(!leve, 'montage sans exception — ' + nom, leve && leve.stack);
  P.unmount();
}

// Le rendu est asynchrone (rafraichir est async) : on laisse la microtâche se vider.
setTimeout(() => {
  ETATS.courant = CAS['parc mixte'];
  P.mount(RACINE, 42, {});
  setTimeout(() => {
    const corps = (zone['body'] || {})._html || '', vue = (zone['chrono-vue'] || {})._html || '';
    ok(!!corps, 'le corps du tableau a été rendu', 'zones connues : ' + Object.keys(zone).join(', '));
    if (process.env.SND_DEBUG) console.log('--- CORPS ---\n' + corps.slice(0, 600));
    ok(/avsync_audio/.test(corps), 'le tableau nomme le flux AUDIO câblé',
       'colonne audio absente du rendu');
    ok(/-18/.test(corps), 'le tableau affiche l\'écart A/V');
    ok(/<svg/.test(vue), 'le chronogramme produit un SVG', 'vue = ' + JSON.stringify(vue).slice(0, 120));
    ok(/#9bbcd6/.test(vue) && /#80c4aa/.test(vue),
       'les deux essences sont tracées (couleurs vidéo ET audio)');
    ok(!/NaN|undefined/.test(vue), 'aucun NaN ni undefined dans le SVG',
       (vue.match(/NaN|undefined/g) || []).join(' '));
    P.unmount();
    console.log(ko ? '\n' + ko + ' contrôle(s) en échec.' : '\nConsole vérifiée.');
    process.exit(ko ? 1 : 0);
  }, 30);
}, 30);
