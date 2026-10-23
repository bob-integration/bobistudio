// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// BIBLIOTHÈQUE DE MÉDIAS — composant commun (Player, Images fixes, Recorder, Gestionnaire).
//
// Lit la BASE DES MÉDIAS de l'orchestrateur (/api/media/*, cf. app/media_registry.py) : bins
// manuels et intelligents, recherche sur n'importe quel champ, serveurs réseau (navigation +
// import), fiche « Infos » (infos, métadonnées, emplacements, vignette). Un plugin ne fournit
// que ce qui lui est propre : ses ACTIONS sur un média (« Charger », « Afficher »…), la vue
// (tableau ou vignettes), les types qu'il lit, et au besoin des onglets à lui (Récents…).
//
//   const ml = MediaLibrary.mount(el, {
//     vmid, T, toast,                       // conteneur dont on prend la vue (nœud + projet)
//     kinds: ['video','audio'],             // types proposés (null = tous)
//     view: 'table' | 'grid',
//     actions: [{id:'load', label:'Charger', primary:true, needsLocal:true, multi:false}, …],
//                                           // multi:true = proposée aussi sur une sélection multiple
//     onAction(id, {asset, path, name}),    // path = chemin vu par le conteneur (copie locale)
//     current: () => cheminChargé,          // ligne surlignée
//     extraTabs: [{id, label, rows: async () => [{path, name, size, gone}]}],
//     framePreview: (path, t, w) => url,    // aperçu rapide fourni par le conteneur (facultatif)
//     position: () => secondes,             // « Image du conteneur » dans le choix de vignette
//     onImported(path, asset),              // après un import depuis un serveur réseau
//     filters: [{field, op, value}],        // conditions posées à l'ouverture (visibles, retirables)
//     projectFilter: true,                  // conteneur d'un projet : filtre « projet » posé d'office
//     selectable: true,                     // barre d'actions groupées dès UNE ligne sélectionnée
//     bulk: [{label, items: rows => [{label, run}] | run: rows => …}],   // actions sur la sélection
//     rowMenu: row => [{label, run} | {header}],                          // ajouts au menu d'une ligne
//     dateColumn: true,                     // colonne « Entré le » (Gestionnaire)
//     fill: true,                           // occupe toute la hauteur de son conteneur
//     imageFormat: {w, h} | () => {w, h},   // format proposé à l'éditeur d'image (« Éditer… »)
//     primary: () => {label, run: row => …} | null,   // SANS `actions` : ce que fait le double-clic
//                                           // (run rend false = pas parti) ; null → fiche Infos
//   Sans `vmid` (Gestionnaire) : vue GLOBALE — la colonne dit combien de copies existent.
//   Avec `vmid` : une action qui exige une copie locale RAPATRIE d'abord le média (copie vérifiée),
//   et une petite marque ✓ signale les médias présents sur le nœud du conteneur.
//   });
//   ml.reload(); ml.byPath(p); ml.assetThumb(a); ml.openInfo(id); ml.destroy();
//   upload: false                         // masque « Importer depuis l'ordinateur… » (défaut : affiché)
//   ml.getState() → {tab, bin, q, tree, view} (petit, sérialisable) ; ml.setState(s) le réapplique (validé).
//
// PROXY de lecture (MP4 540 p, ou M4A pour l'audio seul) : la fiche Infos le LIT (lecteur HTML5,
// servi avec `Range` par /api/media/assets/<id>/proxy) ; sans proxy, la vignette et « Créer le
// proxy ». Menus d'une ligne, d'une sélection, d'un bin : « Créer le(s) proxy(s) ».
// SURVOL-DÉFILEMENT : la vignette d'un média VIDÉO qui a un proxy présent (`has_proxy`, déjà dans
// les lignes de la liste/recherche) devient, après ~250 ms de survol à la souris, une <video> muette
// du proxy ; la position horizontale du pointeur = l'instant (0 % début, 100 % fin), barre + m:ss en
// surimpression. UNE recherche en vol (la suivante attend `seeked`, seule la dernière voulue compte),
// `fastSeek` pendant le mouvement puis une recherche exacte à l'arrêt. Sortie, glisser, re-rendu :
// `src` retiré + `load()` (plus aucune connexion). Une seule vidéo de survol dans la PAGE. Tactile :
// rien. L'instant survolé est repris par le lecteur de la fiche Infos (`#t=`) s'il est ouvert dans la minute.
//
// Sélection : clic sur la LIGNE (Ctrl/Cmd : ajouter/retirer, Maj : plage, Ctrl+A : tout). Clic
// droit : menu de la ligne (ou de la sélection), et d'un bin. Double-clic : action principale.
//
// Glisser une ligne : `application/x-bobi-media` (chemin local de la ligne glissée),
// `application/x-bobi-media-list` (chemins locaux de toute la sélection, JSON),
// `application/x-bobi-asset` (identité de la ligne) et `application/x-bobi-assets` (identités de
// toute la sélection, JSON) — un bin manuel accepte les identités, une playlist les chemins. Une
// ligne glissée qui fait partie de la sélection emporte TOUTE la sélection.
//
// MODES D'AFFICHAGE (sélecteur d'icônes de la barre ; retenu par panneau dans getState/setState,
// et par poste pour le conteneur) : `table` (liste, colonnes au choix par clic droit sur l'en-tête,
// largeurs retenues), `grid` (vignettes, curseur de taille), `cards` (cartes à métadonnées, champs au
// choix), `strip` (pellicule : 8 images de la PLANCHE du média, `has_storyboard` +
// /api/media/assets/<id>/storyboard.json), `preview` (liste + aperçu : lecteur du proxy, fiche,
// actions ; ↑ ↓ et Espace). Option « Grouper par » (jour, projet, nœud, origine, codec) pour liste,
// vignettes et cartes. Rendu PAR LOTS (IntersectionObserver) : 2000 médias sans geler la page ; la
// sélection (Maj, Ctrl+A) porte sur les DONNÉES affichées, pas sur ce qui est déjà dans le DOM.
//
// ENVOI DEPUIS LE POSTE : « Importer depuis l'ordinateur… », ou des FICHIERS déposés depuis le bureau
// (type `Files`, jamais nos types internes) sur la liste ou sur un bin manuel. Machinerie dans
// static/js/media_upload.js, chargé à la demande : conteneur → son nœud et le dossier de son
// projet ; Gestionnaire → nœud et dossier choisis. Fichier importé : `ml:uploaded`, on relit.
(function(){
  'use strict';
  const LIB_MAX = 2000;            // = plafond de la recherche serveur ; rendu par lots (cf. batched)
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const baseName = p => { const s = String(p || ''); const i = s.lastIndexOf('/'); return i >= 0 ? s.slice(i + 1) : s; };
  const fmtSize = b => !b ? '' : b >= 1e9 ? (b / 1e9).toFixed(1) + ' Go' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' Mo' : Math.max(1, Math.round(b / 1e3)) + ' Ko';
  const post = (url, body) => fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});
  // Préférences PAR POSTE (navigateur) : bins pliés, contenu des sous-bins. Jamais bloquant.
  const pref = {
    get(k, d){ try{ const v = localStorage.getItem('ml.' + k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } },
    set(k, v){ try{ localStorage.setItem('ml.' + k, JSON.stringify(v)); }catch(e){} },
  };
  // Groupes de l'éditeur de recherche ; un champ inconnu tombe dans « Général ».
  const SF_GROUPS = [
    ['general', ['name','title','description','keywords','kind','origin','producer','project','created_at','updated_at']],
    ['tech', ['duration','width','height','fps','scan','vcodec','audio_codec','audio_tracks','container','timecode','size','has_video']],
    ['where', ['on_node','on_share','copies','has_proxy']],
    ['proc', ['versions','version','derived','relation','preset','loudness_lufs','true_peak','realtime_factor','submitted_by','watchfolder']],
  ];
  // Champs dont les valeurs présentes dans la base sont proposées (/api/media/search_values).
  const SUGGEST = new Set(['origin','producer','scan','vcodec','audio_codec','container','width','height','fps','audio_tracks',
                           'relation','preset','submitted_by','watchfolder','on_node','on_share','project']);
  let _uid = 0;

  // ── Survol-défilement : logique pure (garde sous node : tests/verif_media_survol.py) ──
  // Position du pointeur → fraction [0, 1] de la largeur de la vignette.
  // Taille de l'aperçu agrandi du survol : `zoom` × la vignette, PLAFONNÉE à `cap` (75 %) de la
  // fenêtre en largeur ET en hauteur, les deux réduites dans la même proportion (rapport d'image
  // conservé) — décision du 2026-10-12, sensible surtout en vue Cartes (grandes vignettes).
  const SCRUB_VIEW_CAP = 0.75;
  const scrubPopSize = (w, h, zoom, vw, vh, cap = SCRUB_VIEW_CAP) => {
    const W = w * zoom, H = h * zoom;
    const k = Math.min(1, W > 0 ? cap * vw / W : 1, H > 0 ? cap * vh / H : 1);
    return [Math.round(W * k), Math.round(H * k)];
  };
  const scrubFrac = (x, left, width) => !(width > 0) ? 0 : Math.min(1, Math.max(0, (x - left) / width));
  // Fraction → instant. 100 % = la DERNIÈRE image (une recherche à `duration` pile peut ne rien montrer).
  const scrubTime = (f, dur) => !(dur > 0) ? 0 : Math.min(1, Math.max(0, f)) * Math.max(0, dur - 0.05);
  const fmtClock = t => { t = Math.max(0, Math.floor(t || 0)); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), x = t % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };
  // File de recherche à UNE requête en vol : chaque recherche d'un proxy servi par Range coûte un
  // aller-retour orchestrateur → nœud (~40 ms mesuré le 2026-10-12) ; en lancer une à chaque
  // mouvement les empilerait. On garde la DERNIÈRE position voulue et on la sert au `seeked`.
  // `fastSeek` (image clé : une par seconde dans le proxy) pendant le mouvement, puis, à l'arrêt
  // (`settleMs`), une recherche exacte. `guardMs` : un `seeked` qui ne vient pas ne bloque pas la file.
  function createSeeker(v, opt){
    opt = opt || {};
    const settleMs = opt.settleMs != null ? opt.settleMs : 200, guardMs = opt.guardMs != null ? opt.guardMs : 1500;
    const S = {busy:false, pending:null, last:null, issued:null, seeks:0, dead:false};
    let _settle = null, _guard = null;
    function pump(){
      if(S.dead || S.busy || !S.pending || !(v.readyState >= 1)) return;
      const p = S.pending; S.pending = null;
      if(S.issued && S.issued.t === p.t && S.issued.precise === p.precise) return;   // déjà servi
      S.busy = true; S.seeks++; S.issued = p;
      clearTimeout(_guard); _guard = setTimeout(done, guardMs);
      if(!p.precise && typeof v.fastSeek === 'function') v.fastSeek(p.t); else v.currentTime = p.t;
    }
    function done(){ clearTimeout(_guard); S.busy = false; pump(); }
    const onMeta = () => pump();
    v.addEventListener('seeked', done); v.addEventListener('loadedmetadata', onMeta);
    return {
      want(t){
        if(S.dead) return;
        S.last = t; S.pending = {t, precise:false}; pump();
        clearTimeout(_settle);
        if(typeof v.fastSeek === 'function') _settle = setTimeout(() => { if(!S.dead){ S.pending = {t:S.last, precise:true}; pump(); } }, settleMs);
      },
      state: () => ({busy:S.busy, pending:S.pending, last:S.last, seeks:S.seeks, dead:S.dead}),
      stop(){ S.dead = true; S.pending = null; clearTimeout(_settle); clearTimeout(_guard);
        v.removeEventListener('seeked', done); v.removeEventListener('loadedmetadata', onMeta); },
    };
  }
  // Couper une vidéo POUR DE BON : sans `src` retiré puis `load()`, le navigateur garde la
  // connexion Range ouverte (et le relais orchestrateur → nœud avec elle).
  function teardownVideo(v){ try{ v.pause(); }catch(_){} v.removeAttribute('src'); try{ v.load(); }catch(_){} }
  // Une seule vidéo de survol dans la PAGE (le Gestionnaire monte plusieurs bibliothèques).
  let _scrubActive = null;          // fonction d'arrêt de la vidéo de survol en cours
  let _lastScrub = null;            // {aid, t, at} : instant survolé, repris par la fiche Infos

  // ── Modes d'affichage : logique pure (garde sous node : tests/verif_media_modes.py) ──
  const VIEWS = ['table', 'grid', 'cards', 'strip', 'preview'];
  const GROUPS = ['none', 'day', 'project', 'node', 'origin', 'codec'];
  const GROUPABLE = new Set(['table', 'grid', 'cards']);
  // Clé de colonne / de champ de carte : un champ connu (`duration`…) ou un champ personnalisé (`meta.<clé>`).
  const FIELD_RE = /^(?:[a-z][a-z_]{0,23}|meta\.[A-Za-z0-9_.-]{1,40})$/;
  const CELL_MIN = 110, CELL_MAX = 320, COLW_MIN = 40, COLW_MAX = 800;
  const clampInt = (v, a, b) => Math.min(b, Math.max(a, Math.round(v)));
  function fieldList(l, max){
    if(!Array.isArray(l)) return null;
    const out = [];
    for(const k of l){ if(out.length >= max) break; if(typeof k === 'string' && FIELD_RE.test(k) && !out.includes(k)) out.push(k); }
    return out;
  }
  // Réglages d'affichage relus (disposition du Gestionnaire, préférence du poste) : seul ce qui est
  // valide passe ; le reste est ignoré (jamais d'exception sur un état ancien ou trafiqué).
  function sanitizeViewState(s){
    const out = {};
    if(!s || typeof s !== 'object') return out;
    if(VIEWS.includes(s.view)) out.view = s.view;
    if(GROUPS.includes(s.group)) out.group = s.group;
    const c = fieldList(s.cols, 24); if(c) out.cols = c;
    const f = fieldList(s.cardF, 16); if(f) out.cardF = f;
    if(s.colW && typeof s.colW === 'object' && !Array.isArray(s.colW)){
      const w = {}; let n = 0;
      for(const [k, v] of Object.entries(s.colW)){
        if(n >= 24) break;
        if(FIELD_RE.test(k) && typeof v === 'number' && isFinite(v)){ w[k] = clampInt(v, COLW_MIN, COLW_MAX); n++; }
      }
      out.colW = w;
    }
    if(typeof s.cell === 'number' && isFinite(s.cell)) out.cell = clampInt(s.cell, CELL_MIN, CELL_MAX);
    return out;
  }
  // Colonne (ou champ de carte) cochée / décochée : retirée si présente, ajoutée EN FIN sinon.
  // `cur` null = la liste par défaut, dont on part.
  function toggleField(cur, key, defaults){
    const l = (cur || defaults).slice(), i = l.indexOf(key);
    if(i >= 0) l.splice(i, 1); else l.push(key);
    return l;
  }
  // Clés de groupe d'un média. Un média peut tomber dans PLUSIEURS groupes (deux projets, copies sur
  // deux nœuds) ; '' = sans valeur (sans projet, hors ligne…). `projects` : {asset_id: [project_id]}.
  function groupKeys(a, by, projects){
    if(!a) return [''];
    const t = a.tech || {};
    switch(by){
      case 'day': return [String(a.created_at || '').slice(0, 10)];
      case 'project': return ((projects || {})[a.id] || []).map(String);
      case 'node': return (a.available_nodes || []).map(String);
      case 'origin': return [String(a.origin || '')];
      case 'codec': return [String((t.video && t.video.codec) || ((t.audio || [])[0] || {}).codec || '')];
    }
    return [''];
  }
  // Groupes : `items` = [{k, keys, dur}] dans l'ordre d'affichage (gardé dans chaque groupe). Durée
  // totale = somme des durées connues. Tri par `sortKey(clé)` (croissant, ou décroissant pour les
  // jours : le plus récent d'abord) ; le groupe '' toujours en dernier.
  function buildGroups(items, desc, sortKey){
    const m = new Map(), sk = sortKey || (x => x);
    for(const it of items){
      const ks = it.keys && it.keys.length ? it.keys : [''];
      for(const key of new Set(ks)){
        let g = m.get(key); if(!g){ g = {key, ks:[], dur:0}; m.set(key, g); }
        g.ks.push(it.k); if(typeof it.dur === 'number' && isFinite(it.dur) && it.dur > 0) g.dur += it.dur;
      }
    }
    return [...m.values()].sort((a, b) => {
      if(a.key === b.key) return 0; if(a.key === '') return 1; if(b.key === '') return -1;
      const x = sk(a.key), y = sk(b.key);
      return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1);
    });
  }
  // Pellicule : `k` cases réparties sur les `n` de la planche — le MILIEU de chaque huitième (ni la
  // toute première image, souvent noire, ni la dernière) ; toutes si la planche en a moins.
  function pickFrames(n, k){
    n = Math.floor(n) || 0; k = k || 8;
    if(n <= 0) return [];
    if(n <= k) return Array.from({length:n}, (_, i) => i);
    return Array.from({length:k}, (_, i) => Math.min(n - 1, Math.floor((i + 0.5) * n / k)));
  }
  const sbValid = sb => !!sb && typeof sb === 'object' && sb.n > 0 && sb.cols > 0 && sb.rows > 0 && sb.n <= sb.cols * sb.rows
    && typeof sb.url === 'string' && sb.url.charAt(0) === '/' && sb.url.charAt(1) !== '/';
  // Case `i` de la planche (c×r cases, ligne par ligne) en arrière-plan CSS : taille et position en %.
  function sbCell(sb, i){
    const c = Math.max(1, sb.cols | 0), r = Math.max(1, sb.rows | 0), x = i % c, y = Math.floor(i / c);
    return {size:[c * 100, r * 100], pos:[c > 1 ? x / (c - 1) * 100 : 0, r > 1 ? y / (r - 1) * 100 : 0]};
  }
  // Instant de la case `i` : celui de la planche, sinon le milieu de sa part de durée.
  // Icônes du sélecteur de mode (maquette validée le 2026-10-12), décoratives : le nom est en aria-label.
  const _svg = b => '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false">' + b + '</svg>';
  const VIEW_ICONS = {
    table: _svg('<rect x="1" y="2" width="14" height="2" rx="1"/><rect x="1" y="7" width="14" height="2" rx="1"/><rect x="1" y="12" width="14" height="2" rx="1"/>'),
    grid: _svg('<rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/>'),
    cards: _svg('<rect x="1" y="1" width="6" height="4" rx="1"/><rect x="1" y="6" width="6" height="1.4" rx=".7"/><rect x="9" y="1" width="6" height="4" rx="1"/><rect x="9" y="6" width="6" height="1.4" rx=".7"/><rect x="1" y="9" width="6" height="4" rx="1"/><rect x="1" y="14" width="6" height="1.4" rx=".7"/><rect x="9" y="9" width="6" height="4" rx="1"/><rect x="9" y="14" width="6" height="1.4" rx=".7"/>'),
    strip: _svg('<rect x="1" y="3" width="3" height="4" rx=".5"/><rect x="5" y="3" width="3" height="4" rx=".5"/><rect x="9" y="3" width="3" height="4" rx=".5"/><rect x="13" y="3" width="2" height="4" rx=".5"/><rect x="1" y="9" width="3" height="4" rx=".5"/><rect x="5" y="9" width="3" height="4" rx=".5"/><rect x="9" y="9" width="3" height="4" rx=".5"/><rect x="13" y="9" width="2" height="4" rx=".5"/>'),
    preview: _svg('<rect x="1" y="2" width="5" height="1.6" rx=".8"/><rect x="1" y="5.5" width="5" height="1.6" rx=".8"/><rect x="1" y="9" width="5" height="1.6" rx=".8"/><rect x="1" y="12.5" width="5" height="1.6" rx=".8"/><rect x="8" y="2" width="7" height="7" rx="1"/><rect x="8" y="10.5" width="7" height="1.4" rx=".7"/><rect x="8" y="13" width="5" height="1.4" rx=".7"/>'),
  };
  // Case de la planche qui montre l'instant `t` : la dernière dont l'instant est <= t (planche à `times`),
  // sinon la part proportionnelle de la durée. Toujours dans [0, n-1].
  function sbIndexAt(sb, t, dur){
    const n = sb.n | 0; if(!(n > 0)) return 0;
    if(Array.isArray(sb.times) && sb.times.length >= n){
      let lo = 0, hi = n - 1;
      while(lo < hi){ const mid = (lo + hi + 1) >> 1; if(Number(sb.times[mid]) <= t) lo = mid; else hi = mid - 1; }
      return lo;
    }
    return !(dur > 0) ? 0 : Math.min(n - 1, Math.max(0, Math.floor(t / dur * n)));
  }
  function sbTime(sb, i, dur){
    const t = Array.isArray(sb.times) ? Number(sb.times[i]) : NaN;
    return isFinite(t) && t >= 0 ? t : (dur > 0 && sb.n > 0 ? (i + 0.5) * dur / sb.n : 0);
  }

  function mount(root, o){
    const T = o.T || ((k, d) => (window.t ? window.t(k) : '') || d);
    const toast = o.toast || ((m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m)));
    const vmid = o.vmid;
    const global = vmid == null;
    const actions = o.actions || [];
    const L = (k, d) => { const v = T('medialib.' + k, d); return (v && v !== 'medialib.' + k) ? v : d; };
    const uid = 'ml' + (++_uid) + '-' + Date.now().toString(36);
    // Mode d'affichage et ses réglages : défaut de l'hôte (liste, ou vignettes aux Images fixes), puis
    // la préférence du POSTE pour ce conteneur (ou pour le Gestionnaire), puis setState (disposition).
    const imagesOnly = Array.isArray(o.kinds) && o.kinds.length === 1 && o.kinds[0] === 'image';
    const viewsHere = imagesOnly ? VIEWS.filter(v => v !== 'strip') : VIEWS;   // pas de pellicule pour des images fixes
    let view = viewsHere.includes(o.view) ? o.view : 'table';
    const DEF_COLS = ['duration', 'format', ...(o.flag ? ['compat'] : []), 'audio', 'copies', ...(o.dateColumn ? ['created_at'] : [])];
    const DEF_CARDF = ['description', 'file', 'format', 'project', 'keywords'];
    const vs = {group:'none', cols:null, colW:{}, cell:168, cardF:null};
    const viewKey = 'view.' + (o.viewKey || (global ? 'global' : 'c' + vmid));
    function applyViewState(p){
      if(p.view && viewsHere.includes(p.view)) view = p.view;
      ['group', 'cols', 'colW', 'cell', 'cardF'].forEach(k => { if(k in p) vs[k] = p[k]; });
    }
    applyViewState(sanitizeViewState(pref.get(viewKey, null)));
    const saveView = () => pref.set(viewKey, {view, ...vs});

    root.classList.add('ml');
    root.classList.toggle('ml-fill', !!o.fill);
    root.innerHTML = `
      <div class="ml-bar">
        <div class="ml-tabs" role="tablist">
          <button type="button" role="tab" class="ml-tab on" data-tab="base">${esc(L('tab_base','Médias'))}</button>
          <button type="button" role="tab" class="ml-tab" data-tab="net" hidden>${esc(L('tab_net','Serveur réseau'))}</button>
          ${(o.extraTabs || []).map(t => `<button type="button" role="tab" class="ml-tab" data-tab="${esc(t.id)}">${esc(t.label)}</button>`).join('')}
        </div>
        <input class="ml-search" type="search" placeholder="${esc(L('search_ph','Rechercher : nom, titre, mots-clés…'))}">
        <button type="button" class="btn ml-filters-btn" aria-expanded="false">${esc(L('filters','Filtres'))}<span class="ml-filters-n"></span></button>
        <label class="ml-vopt ml-grp-l" hidden><span>${esc(L('group_by','Grouper par'))}</span>
          <select class="ml-group">${GROUPS.map(g => `<option value="${g}">${esc(L('group.' + g, g))}</option>`).join('')}</select></label>
        <label class="ml-vopt ml-size-l" hidden><span>${esc(L('thumb_size','Taille'))}</span>
          <input type="range" class="ml-size" min="${CELL_MIN}" max="${CELL_MAX}" step="10"></label>
        <button type="button" class="btn ml-compat" aria-pressed="false" hidden></button>
        <button type="button" class="btn ml-onetap" aria-pressed="false" hidden>${esc(L('one_click','1 clic'))}</button>
        <button type="button" class="btn ml-cardf-btn" aria-haspopup="menu" hidden>${esc(L('card_fields','Champs'))} ▾</button>
        <div class="ml-modes" role="group" aria-label="${esc(L('view_label','Affichage'))}">${viewsHere.map(v =>
          `<button type="button" class="ml-mode" data-view="${v}" aria-pressed="false" title="${esc(L('view.' + v, v))}" aria-label="${esc(L('view.' + v, v))}">${VIEW_ICONS[v]}</button>`).join('')}</div>
        <span class="ml-count"></span>
        <button type="button" class="btn ml-upload"${o.upload === false ? ' hidden' : ''}>${esc(L('up_btn','Importer depuis l\'ordinateur…'))}</button>
        <input type="file" class="ml-upload-in" multiple hidden>
        <button type="button" class="btn ml-refresh" title="${esc(L('refresh','Rafraîchir'))}" aria-label="${esc(L('refresh','Rafraîchir'))}">↻</button>
      </div>
      <div class="ml-chips" hidden></div>
      <div class="ml-filters" hidden>
        <div class="ml-flt-rows" data-g=""></div>
        <div class="ml-flt-acts">
          <button type="button" class="btn ml-flt-add">${esc(L('flt_add','+ Condition'))}</button>
          <button type="button" class="btn ml-flt-addg">${esc(L('flt_add_grp','+ Groupe'))}</button>
          <span class="ml-sp"></span>
          <button type="button" class="btn ml-flt-clear">${esc(L('flt_clear_all','Tout effacer'))}</button>
          <button type="button" class="btn btn-green ml-flt-save">${esc(L('flt_save','Enregistrer comme bin intelligent…'))}</button>
        </div>
        <p class="ml-flt-read" aria-live="polite" hidden></p>
      </div>
      <div class="ml-bulk" hidden><span class="ml-bulk-n"></span><span class="ml-bulk-acts"></span>
        <span class="ml-sp"></span><button type="button" class="btn ml-bulk-all">${esc(L('sel_all','Tout sélectionner'))}</button>
        <button type="button" class="btn ml-bulk-clear">${esc(L('sel_clear','Désélectionner'))}</button></div>
      <div class="ml-job" hidden role="status" aria-live="polite"><span class="ml-job-txt"></span><progress class="ml-job-bar" max="1" value="0"></progress></div>
      <div class="ml-crumbs" hidden></div>
      <div class="ml-menu" hidden role="menu"></div>
      <div class="ml-body">
        <aside class="ml-bins"><label class="ml-bins-opt"><input type="checkbox" class="ml-bin-kids"> <span>${esc(L('bin_children','Avec le contenu des sous-bins'))}</span></label>
          <div class="ml-bins-list" role="tree"></div>
          <button type="button" class="btn ml-bin-new">${esc(L('bin_new','+ Bin'))}</button></aside>
        <div class="ml-main" tabindex="0"></div>
      </div>
      <p class="ml-hint">${esc(o.hint || L('hint2','Clic : sélectionner (Ctrl : ajouter, Maj : plage). Double-clic : action principale. Clic droit : menu. Glisser sur un bin : l\'y ranger.'))}</p>`;
    const $ = s => root.querySelector(s), $$ = s => root.querySelectorAll(s);

    // `filters` : conditions posées à l'ouverture (ex. le recorder montre d'abord SES clips) ; on les retire comme les autres.
    const st = {tab:'base', assets:[], byPath:{}, all:[], regOk:true, bins:[], bin:null, tree:{items:(o.filters || []).map(f => ({...f})), links:(o.filters || []).slice(1).map(() => 'and')}, editBin:null, sf:null, q:'',
                net:{path:'', dirs:[], files:[]}, extra:{}, netOk:false, sel:new Set(), anchor:null, sort:{key:'', dir:1},
                folded:new Set(pref.get('bins.folded', [])), kids:!!pref.get('bins.children', false), vals:{}, ctx:null,
                order:[], groups:[], sharedMine:{bins:[], assets:[]}};   // order : clés affichées, dans l'ordre (sélection Maj / Ctrl+A) ; groups : groupes affichés
    $('.ml-bin-kids').checked = st.kids;

    // ── Fiches techniques lisibles ──
    const CODECS = {h264:'H.264', hevc:'H.265', dnxhd:'DNxHD/HR', prores:'ProRes', mpeg2video:'MPEG-2', ffv1:'FFV1',
                    aac:'AAC', mp3:'MP3', opus:'Opus', flac:'FLAC', ac3:'AC-3', eac3:'E-AC-3', mp2:'MPEG-1 L2',
                    png:'PNG', mjpeg:'JPEG', bmp:'BMP', gif:'GIF', webp:'WebP', tiff:'TIFF'};
    const codecName = c => { c = String(c || ''); const m = c.match(/^pcm_([su])(\d+)/); if(m) return 'PCM ' + m[2] + ' ' + L('bits','bits');
      if(/^pcm_f(32|64)/.test(c)) return 'PCM float'; return CODECS[c] || c.toUpperCase(); };
    const chName = n => n === 1 ? 'mono' : n === 2 ? L('stereo','stéréo') : n === 6 ? '5.1' : n + ' ' + L('channels','canaux');
    const kindOf = a => { const t = (a && a.tech) || {}; return t.kind || (t.video ? 'video' : (t.audio || []).length ? 'audio' : ''); };
    function fmtDur(s){ if(!(s > 0)) return '—'; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
      return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); }
    function fmtVideo(v){ const rate = v.fps ? (v.scan === 'i' ? Math.round(v.fps * 2) + 'i' : (Math.round(v.fps * 100) / 100) + 'p') : '';
      return [`${v.width}×${v.height}`, rate, codecName(v.codec)].filter(Boolean).join(' · '); }
    function fmtShort(a){ const t = a.tech || {}, v = t.video || {};
      if(kindOf(a) === 'image') return v.width ? `${v.width}×${v.height} ${codecName(v.codec)}` : '';
      if(!v.width) return (t.audio || []).length ? L('audio_only','audio seul') : '';
      const r = v.fps ? (v.scan === 'i' ? Math.round(v.fps * 2) + 'i' : (Math.round(v.fps * 100) / 100) + 'p') : '';
      return `${v.height}${r ? ' ' + r : ''} ${codecName(v.codec)}`.trim(); }
    function fmtAudioShort(a){ const au = (a.tech || {}).audio || []; if(!au.length) return kindOf(a) === 'image' ? '' : '—';
      const mono = au.every(x => x.channels === 1), same = au.every(x => x.channels === au[0].channels);
      if(mono) return au.length === 1 ? 'mono' : `${au.length} × mono`;
      if(same && au[0].channels === 2) return au.length === 1 ? L('stereo','stéréo') : `${au.length} × ${L('stereo','stéréo')}`;
      return au.map(x => x.channels + ' can.').join(' + '); }
    function audioGroups(au){
      if(!au.length) return '—';
      const g = [];
      au.forEach((x, k) => { const key = [x.channels, x.codec, x.rate].join('|'); const last = g[g.length - 1];
        if(last && last.key === key) last.n++; else g.push({key, n:1, first:k + 1, x}); });
      const f = x => [codecName(x.codec), x.rate ? (x.rate / 1000) + ' kHz' : ''].filter(Boolean);
      if(g.length === 1){ const {n, x} = g[0];
        return [[(n > 1 ? n + ' ' + L('tracks','pistes') : '1 ' + L('track','piste')) + ' ' + chName(x.channels || 0), ...f(x)].join(' · ')]; }
      return g.map(({n, first, x}) => (n > 1 ? L('tracks_n','Pistes {a}–{b}').replace('{a}', first).replace('{b}', first + n - 1)
        : L('track_n','Piste {a}').replace('{a}', first)) + ' : ' + [chName(x.channels || 0), ...f(x)].join(' · '));
    }
    function fmtColor(v){
      const pf = String(v.pix_fmt || ''), m = pf.match(/(4[24][024])p?(\d+)?/);
      const samp = m ? `${m[1][0]}:${m[1][1]}:${m[1][2]} ${m[2] || 8} ${L('bits','bits')}` : pf;
      const cs = [v.primaries, v.transfer, v.matrix].filter(Boolean), uniq = [...new Set(cs)];
      const space = uniq.length === 1 ? uniq[0].toUpperCase().replace(/^BT(\d)/, 'BT.$1') : cs.join(' / ');
      return [samp, space].filter(Boolean).join(' · ') || '—'; }
    const assetThumb = a => `/api/media/assets/${encodeURIComponent(a.id)}/thumb${a.thumb && a.thumb.v ? '?v=' + encodeURIComponent(a.thumb.v) : ''}`;
    const tc = sec => { sec = Math.max(0, sec || 0); const p = n => String(n).padStart(2, '0');
      return `${p(Math.floor(sec / 3600))}:${p(Math.floor(sec % 3600 / 60))}:${p(Math.floor(sec % 60))}.${String(Math.floor((sec % 1) * 100)).padStart(2, '0')}`; };

    // Échec → notification et null ; `err` (facultatif) reçoit le message en `err.message` pour qui
    // veut aussi le montrer sur place (champ refusé).
    async function regCall(method, url, body, err){
      const fail = m => { toast(m, 'error'); if(err) err.message = m; return null; };
      try{
        const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
        const d = await r.json().catch(() => ({}));
        if(!r.ok || d.ok === false) return fail(d.error || (r.status === 403 ? L('forbidden','Droit « gestion des médias » requis') : 'HTTP ' + r.status));
        return d;
      }catch(e){ return fail(e.message); }
    }
    // Bins changés ici (création, rangement, suppression) : les autres bibliothèques ouvertes (autres
    // panneaux du Gestionnaire) relisent leurs bins et, si elles en affichent un, son contenu.
    const binsChanged = () => document.dispatchEvent(new CustomEvent('ml:bins', {detail:{src:uid}}));
    const _onBins = async e => { if(!root.isConnected || (e.detail && e.detail.src === uid)) return;
      await loadBins(); if(st.bin != null && st.tab === 'base'){ await loadAssets(); render(); } };
    document.addEventListener('ml:bins', _onBins);
    // Image enregistrée par l'éditeur (static/js/image_editor.js), d'où qu'il ait été ouvert.
    const _onImgSaved = () => { if(root.isConnected) reload(); };
    window.addEventListener('imageeditor:saved', _onImgSaved);

    // ── Chargement ──
    function query(){
      const t = toQuery(st.tree);
      return {q: st.q || '', kinds: o.kinds || [], match: t.match, items: t.items};
    }
    async function loadAssets(){
      try{
        const q = query();
        const r = st.bin && st.bin !== 'shared' ? await post(`/api/media/bins/${st.bin}/assets`, {vmid, query:q, children:st.kids})
                         : await post('/api/media/search', {vmid, query:q});
        if(!r.ok){ const d = await r.json().catch(() => ({})); if(d.error) toast(d.error, 'error'); throw new Error('HTTP ' + r.status); }
        st.assets = (await r.json()).assets || []; st.regOk = true;
        // « Partagés avec moi » : les médias partagés UN PAR UN (les bins partagés sont dans l'arbre).
        if(st.bin === 'shared'){ const ids = new Set((st.sharedMine.assets || []).map(x => x.asset_id)); st.assets = st.assets.filter(a => ids.has(a.id)); }
        // `byPath` (copies que le conteneur voit) sur TOUTE la base : la fiche du média chargé en dépend.
        st.all = (st.bin || q.q || q.items.length)
          ? ((await (await post('/api/media/search', {vmid, query:{kinds:o.kinds || []}})).json()).assets || []) : st.assets;
        st.byPath = {}; st.all.forEach(a => { if(a.local_path) st.byPath[a.local_path] = a; });
      }catch(e){ st.regOk = false; }
    }
    async function loadBins(){
      try{ st.bins = ((await (await fetch('/api/media/bins')).json()).bins) || []; }catch(e){ st.bins = []; }
      try{ st.sharedMine = await (await fetch('/api/media/shares/mine')).json(); }catch(e){ st.sharedMine = {bins:[], assets:[]}; }
      if(st.bin === 'shared' && !(st.sharedMine.bins || []).length && !(st.sharedMine.assets || []).length) st.bin = null;
      renderBins();
    }
    async function loadNet(path){
      try{
        const r = await fetch(`/api/media/browse_share?vmid=${vmid == null ? '' : vmid}&path=${encodeURIComponent(path || '')}`);
        const d = await r.json();
        if(d.error){ toast(d.error, 'error'); return; }
        st.net = {path:d.path || '', dirs:d.dirs || [], files:d.files || []};
      }catch(e){ return; }
      $('.ml-crumbs').textContent = L('tab_net','Serveur réseau') + ' / ' + (st.net.path || '');
      render();
    }
    async function reload(){
      if(st.tab === 'net') await loadNet(st.net.path);
      else if(st.tab !== 'base'){ const t = (o.extraTabs || []).find(x => x.id === st.tab); if(t) st.extra[t.id] = await t.rows().catch(() => []); }
      _projP = null; _sb.clear();               // projets et planches relus avec la liste
      await loadAssets(); render(); loadBins();
    }

    // ── Rendu ──
    function rows(){
      const q = st.q;
      if(st.tab === 'net'){
        const base = st.net.path ? st.net.path + '/' : '';
        const out = [];
        if(st.net.path) out.push({dir:'..', name:'..'});
        st.net.dirs.filter(n => !q || n.toLowerCase().includes(q)).forEach(n => out.push({dir:n, name:n}));
        st.net.files.filter(f => !q || f.name.toLowerCase().includes(q)).forEach(f =>
          out.push({ext: base + f.name, name:f.name, size:f.size, inBase:f.asset_id || null}));
        return out;
      }
      if(st.tab === 'base'){
        const out = st.assets.map(a => ({asset:a, name:(a.meta || {}).title || a.name, path:a.local_path || ''})).filter(compatOk);
        const k = st.sort.key, d = st.sort.dir;
        if(k){
          // Colonne quelconque (champs personnalisés compris) : sa valeur de tri, sinon son texte.
          const def = k !== 'name' ? fieldDef(k) : null;
          const v = r => k === 'name' ? r.name.toLowerCase() : !def ? (r.asset.size || 0)
            : def.sv ? def.sv(r.asset) : String(def.text ? def.text(r.asset) : '').toLowerCase();
          out.sort((x, y) => (v(x) < v(y) ? -1 : v(x) > v(y) ? 1 : 0) * d);
        }
        return out;
      }
      return (st.extra[st.tab] || []).filter(f => !q || String(f.path).toLowerCase().includes(q)).map(f => {
        const a = st.byPath[f.path]; return a ? {...f, asset:a, name:(a.meta || {}).title || a.name} : {...f, name:f.name || baseName(f.path)}; }).filter(compatOk);
    }
    const keyOf = r => r.asset ? r.asset.id : r.ext ? 'ext:' + r.ext : '';
    // « Présent en local » : une copie sur le nœud du conteneur, dans son champ de vision. Une simple
    // marque — ni nom de nœud ni texte (le détail est dans Infos → Emplacements).
    const isLocal = r => !global && !!r.path && !r.gone && !r.ext;
    // Homonymes (plusieurs médias de la portée portent le même nom — fréquent avec plusieurs
    // projets) : le serveur donne `homonyms` et `name_hint` (projets, à défaut nœud · dossier) ;
    // affiché en petit à côté du nom, et seulement dans ce cas.
    // Média vu seulement par un PARTAGE (app/media_sharing.py) : marqué, discrètement, du nom du partageur.
    const homTag = r => ((r && r.asset && r.asset.homonyms > 0 && r.asset.name_hint)
      ? ` <span class="ml-hom" title="${esc(L('hom_tip','Plusieurs médias portent ce nom'))}">${esc(r.asset.name_hint)}</span>` : '')
      + ((r && r.asset && r.asset.shared)
      ? ` <span class="ml-hom ml-shared-by" title="${esc(L('sh_by_tip','Partagé avec vous par {u}').replace('{u}', r.asset.shared.by || '?'))}">↗ ${esc(r.asset.shared.by || '')}</span>` : '');
    const localMark = r => isLocal(r) ? `<span class="ml-local" title="${esc(L('local_tip','Présent en local'))}" aria-label="${esc(L('local_tip','Présent en local'))}">✓</span>` : '';
    function copiesCell(r){
      const a = r.asset;
      if(!a) return r.size ? fmtSize(r.size) : '';
      const n = a.copies_n != null ? a.copies_n : (a.available_nodes || []).length;
      const tip = n ? [...(a.available_nodes || []), ...(a.on_share ? [L('loc_share','sur un partage')] : [])].join(', ')
                    : L('loc_offline','hors ligne');
      return `<span class="ml-ncopies${n ? '' : ' off'}" title="${esc(tip)}">${n}</span>`;
    }
    function actionBtns(r){
      const a = r.asset, cur = st.bins.find(b => b.id === st.bin);
      let h = '';
      const menu = (a || r.ext) ? `<button type="button" class="btn ml-more" data-act="__menu" aria-haspopup="menu" title="${esc(L('more','Plus d\'actions'))}" aria-label="${esc(L('more','Plus d\'actions'))}">⋯</button>` : '';
      if(r.ext){
        if(global) return menu;
        const prim = actions.find(x => x.primary) || actions[0];
        h += prim ? `<button type="button" class="btn" data-act="${esc(prim.id)}">${esc(prim.label)}</button>` : '';
        h += `<button type="button" class="btn" data-act="__import">${esc(L('import','Importer'))}</button>`;
        return h + menu;
      }
      if(a && st.tab === 'base' && cur && !cur.smart) h += `<button type="button" class="btn" data-act="__unbin" title="${esc(L('bin_remove','Retirer du bin'))}" aria-label="${esc(L('bin_remove','Retirer du bin'))}">−</button>`;
      if(a) h += `<button type="button" class="btn" data-act="__info">${esc(L('info','Infos…'))}</button>`;
      // Sans copie locale mais présent sur un autre nœud : l'action RAPATRIE d'abord (copie vérifiée).
      const canFetch = !global && a && !r.path && (a.available_nodes || []).length > 0;
      actions.forEach(x => { const needs = x.needsLocal !== false && !r.path; const dis = needs && !canFetch;
        const tip = dis ? L('no_local','Pas de copie accessible à ce conteneur') : needs ? L('fetch_tip','Copie d\'abord le média sur ce nœud') : '';
        h += `<button type="button" class="btn${x.primary ? ' ml-prim' : ''}" data-act="${esc(x.id)}"${dis ? ' disabled' : ''}${tip ? ' title="' + esc(tip) + '"' : ''}>${esc(x.label)}${needs && !dis ? ' ⇣' : ''}</button>`; });
      return h + menu;
    }
    function badge(r){
      return baseBadge(r) + hostFlag(r);
    }
    function baseBadge(r){
      if(r.inBase) return `<span class="ml-badge ok">${esc(L('in_base','dans la base'))}</span>`;
      if(r.asset && r.asset.growing) return `<span class="ml-badge rec">${esc(L('recording','en cours'))}</span>`;
      if(r.gone) return `<span class="ml-badge">${esc(L('gone','introuvable'))}</span>`;
      return '';
    }
    // Repère fourni par l'HÔTE (`o.flag(asset) → {label, title, ok?} | null`) : ce que la bibliothèque
    // ne peut pas savoir seule — p. ex. le player, qui signale un média pas au format de sa sortie.
    // `ok: true` = une INFORMATION, pas un avertissement (« ✓ version hd1080p50 ») : jamais masquée.
    function flagOf(a){
      try{ return (o.flag && a) ? o.flag(a) : null; }catch(_){ return null; }
    }
    function hostFlag(r){
      const f = flagOf(r.asset);
      return f && !f.ok ? `<span class="ml-badge warn" title="${esc(f.title || '')}">${esc(f.label || '')}</span>` : '';
    }
    // « Compatibles seulement » : masque les médias que l'hôte signale (o.flag). Un fichier sans
    // fiche n'est pas masqué — on ne sait rien de son format, l'hôte le signale au chargement.
    const compatOk = r => { if(!st.compatOnly || !r.asset) return true; const f = flagOf(r.asset); return !f || !!f.ok; };
    // ── Champs affichables : colonnes de la liste et champs des cartes (personnalisés compris) ──
    const kwOf = a => String(((a && a.meta) || {}).keywords || '').split(/[,;]/).map(x => x.trim()).filter(Boolean);
    const rateOf = v => !v || !v.fps ? '' : v.scan === 'i' ? 'i' + Math.round(v.fps * 2) : 'p' + Math.round(v.fps * 100) / 100;
    // « 1080p25 » (la définition ET la cadence, comme on les dit en régie) ; une image : sa taille.
    const defRate = a => { const v = (a.tech || {}).video || {}; if(!v.width) return kindOf(a) === 'audio' ? L('audio_only','audio seul') : '';
      return kindOf(a) === 'image' ? `${v.width}×${v.height}` : `${v.height}${rateOf(v)}`; };
    const codecOf = a => { const t = a.tech || {}; return codecName((t.video && t.video.codec) || ((t.audio || [])[0] || {}).codec || ''); };
    const originLabel = x => x ? L('origin.' + x, x) : '';
    const projName = id => { const p = ((st.sf && st.sf.projects) || []).find(x => String(x.id) === String(id)); return p ? p.name : '#' + id; };
    const projNames = a => ((_proj && a && _proj[a.id]) || []).map(projName);
    const FIELDS = {
      title:{label:() => L('if_title','Titre'), text:a => (a.meta || {}).title || ''},
      description:{label:() => L('if_desc','Description'), text:a => (a.meta || {}).description || ''},
      keywords:{label:() => L('if_kw','Mots-clés'), text:a => kwOf(a).join(', ')},
      file:{label:() => L('if_file','Fichier'), text:a => a.name || ''},
      duration:{label:() => L('col_dur','Durée'), mono:1, sv:a => (a.tech || {}).duration || 0,
        text:a => a.growing ? L('growing','croissant') : kindOf(a) === 'image' ? '' : fmtDur((a.tech || {}).duration)},
      format:{label:() => L('col_fmt','Format'), mono:1, text:fmtShort},
      audio:{label:() => L('col_audio','Audio'), mono:1, text:fmtAudioShort},
      copies:{label:() => L('col_copies','Copies'), mono:1, cls:'ml-c-n', sv:a => a.copies_n || 0, html:r => copiesCell(r)},
      created_at:{label:() => L('col_date','Entré le'), mono:1, sv:a => a.created_at || '', text:a => (a.created_at || '').replace('T', ' ').slice(0, 16)},
      project:{label:() => L('if_projects','Projets'), text:a => projNames(a).join(', ')},
      origin:{label:() => L('if_origin','Origine'), text:a => originLabel(a.origin)},
      producer:{label:() => L('if_producer','Producteur'), text:a => a.producer || ''},
      size:{label:() => L('col_size','Taille'), mono:1, sv:a => a.size || 0, text:a => fmtSize(a.size)},
      resolution:{label:() => L('col_res','Définition'), mono:1, sv:a => ((a.tech || {}).video || {}).height || 0,
        text:a => { const v = (a.tech || {}).video || {}; return v.width ? `${v.width}×${v.height}` : ''; }},
      fps:{label:() => L('col_fps','Cadence'), mono:1, sv:a => ((a.tech || {}).video || {}).fps || 0, text:a => kindOf(a) === 'image' ? '' : rateOf((a.tech || {}).video).replace(/^([pi])(.*)$/, '$2$1')},
      vcodec:{label:() => L('col_codec','Codec'), text:codecOf},
      timecode:{label:() => L('if_tc','TC de départ'), mono:1, text:a => (a.tech || {}).timecode || ''},
      container:{label:() => L('if_container','Conteneur'), text:a => (a.tech || {}).container || ''},
      proxy:{label:() => L('col_proxy','Proxy'), sv:a => a.has_proxy ? 1 : 0, text:a => a.has_proxy ? '✓' : ''},
      netsize:{label:() => L('col_size','Taille'), mono:1, html:r => r.size ? fmtSize(r.size) : ''},
    };
    // Colonne de l'HÔTE (o.flag) : triable — signalés d'abord, ou compatibles d'abord ; « ✓ » quand le format
    // est connu et sans remarque, vide quand la bibliothèque ne sait rien du média.
    if(o.flag) FIELDS.compat = {label:() => o.flagColumn || L('col_compat','Compatibilité'),
      sv:a => { const f = flagOf(a); return f ? (f.ok ? 0.5 : 2) : (a && a.tech && a.tech.video ? 0 : 1); },   // 1er clic (décroissant) : les ⚠ d'abord
      html:r => { if(!r.asset) return ''; const f = flagOf(r.asset);
        if(f && f.ok) return `<span class="ml-compat-ok" title="${esc(f.title || '')}">${esc(f.label || '✓')}</span>`;
        if(f) return `<span class="ml-badge warn" title="${esc(f.title || '')}">⚠ ${esc(f.title || f.label || '')}</span>`;
        return r.asset.tech && r.asset.tech.video ? `<span class="ml-compat-ok" title="${esc(L('compat_ok','Au format de la sortie'))}">✓</span>` : ''; }};
    const COL_CHOICES = ['duration', 'format', ...(o.flag ? ['compat'] : []), 'audio', 'copies', 'created_at', 'title', 'description', 'keywords', 'project', 'origin',
                         'producer', 'size', 'resolution', 'fps', 'vcodec', 'timecode', 'container', 'proxy'];
    const CARD_CHOICES = ['description', 'file', 'format', 'audio', 'duration', 'project', 'keywords', 'origin', 'created_at', 'size', 'copies', 'timecode'];
    function fieldDef(k){
      if(FIELDS[k]) return FIELDS[k];
      if(!/^meta\./.test(String(k))) return null;
      const mk = k.slice(5), f = ((st.sf && st.sf.fields) || []).find(x => x.key === k);
      const raw = a => (a.meta || {})[mk];
      return {label:() => f ? (f.label || mk) : mk, sv:a => { const v = raw(a); return typeof v === 'number' ? v : String(v == null ? '' : v).toLowerCase(); },
        text:a => { const v = raw(a); return v == null ? '' : typeof v === 'boolean' ? (v ? '✓' : '') : String(v); }};
    }
    const customKeys = () => ((st.sf && st.sf.fields) || []).filter(f => f.custom).map(f => f.key);
    const colsOf = () => (vs.cols || DEF_COLS).filter(k => FIELDS[k] && k !== 'netsize' || /^meta\./.test(k));
    const cardFieldsOf = () => (vs.cardF || DEF_CARDF).filter(k => FIELDS[k] && k !== 'netsize' || /^meta\./.test(k));

    // Projets de chaque média (regroupement, colonne, cartes, aperçu) : une recherche par projet, à la
    // demande seulement, relue avec la liste. Les lignes de recherche ne portent pas leurs projets.
    let _proj = null, _projP = null;
    function loadProjects(){
      if(_projP) return _projP;
      _projP = (async () => {
        if(!st.sf) await loadSF();
        const m = {};
        await Promise.all(((st.sf && st.sf.projects) || []).map(async p => {
          try{ const r = await post('/api/media/search', {vmid, query:{kinds:o.kinds || [], match:'all', items:[{field:'project', op:'eq', value:p.id}]}});
            ((await r.json()).assets || []).forEach(a => { (m[a.id] = m[a.id] || []).push(p.id); }); }catch(_){}
        }));
        _proj = m; return m;
      })();
      return _projP;
    }
    const needsProjects = mode => st.tab !== 'net' && ((GROUPABLE.has(mode) && vs.group === 'project' && st.tab === 'base')
      || (mode === 'table' && colsOf().includes('project')) || (mode === 'cards' && cardFieldsOf().includes('project')));

    // ── Rendu PAR LOTS : le premier tout de suite, les suivants quand la fin approche du bas ──
    const BATCH = 150;
    let _io = null, _more = null;
    function stopBatches(){ if(_io){ _io.disconnect(); _io = null; } _more = null; }
    function batched(scroller, entries, emit){
      let i = 0;
      const step = () => { if(i >= entries.length) return false; const part = entries.slice(i, i + BATCH); i += part.length; emit(part); return i < entries.length; };
      if(!step()) return;
      const sent = document.createElement('div'); sent.className = 'ml-sentinel'; sent.setAttribute('aria-hidden', 'true');
      scroller.appendChild(sent);
      const near = () => sent.isConnected && sent.getBoundingClientRect().top < scroller.getBoundingClientRect().bottom + 800;
      const more = () => { const left = step(); if(left) scroller.appendChild(sent); else { sent.remove(); stopBatches(); } return left; };
      _more = more;
      if(typeof IntersectionObserver !== 'function'){ while(more()); return; }
      _io = new IntersectionObserver(es => { if(es.some(x => x.isIntersecting)) while(more() && near()); }, {root:scroller, rootMargin:'0px 0px 800px 0px'});
      _io.observe(sent);
    }

    // ── Morceaux communs aux modes ──
    const attrs = (r, k, drag) => `data-k="${k}" data-key="${esc(keyOf(r))}" data-path="${esc(r.path || '')}" data-aid="${esc(r.asset ? r.asset.id : '')}" data-ext="${esc(r.ext || '')}"` +
      (drag === false ? '' : ` draggable="${!!(r.path || r.asset)}"`);
    const selCls = r => keyOf(r) && st.sel.has(keyOf(r)) ? ' is-sel' : '';
    const thumbImg = (r, cls) => `<img class="ml-thumb${cls || ''}" loading="lazy" decoding="async" alt="" src="${assetThumb(r.asset)}"${canScrub(r.asset) ? ` data-scrub="1" title="${esc(L('scrub_tip','Survoler pour parcourir'))}"` : ''}>`;
    const thumbOf = r => r.asset && (kindOf(r.asset) === 'video' || kindOf(r.asset) === 'image') ? thumbImg(r) : `<span class="ml-thumb"></span>`;
    // Vignette d'une cellule ou d'une carte : l'image, la durée et un badge P (proxy) / REC dessus.
    function thumbBox(r){
      const a = r.asset;
      if(!a) return '<div class="ml-th is-none"></div>';
      const k = kindOf(a), t = a.tech || {};
      const img = k === 'video' || k === 'image' ? thumbImg(r) : '<span class="ml-th-wave" aria-hidden="true"></span>';
      const tag = a.growing ? `<span class="ml-th-rec">${esc(L('rec_badge','REC'))}</span>`
        : a.has_proxy && k !== 'image' ? `<span class="ml-th-px" title="${esc(L('px_has','A un proxy de lecture'))}">P</span>` : '';
      const dur = k !== 'image' && t.duration > 0 ? `<span class="ml-th-dur">${esc(fmtDur(t.duration))}</span>` : '';
      return `<div class="ml-th${k === 'audio' ? ' is-audio' : ''}">${img}${tag}${dur}</div>`;
    }
    // Présence : un point plein = une copie sur le nœud du conteneur (au Gestionnaire : au moins une
    // copie) ; un rond vide = pas de copie ici (hors ligne au Gestionnaire).
    function locDot(r){
      if(!r.asset) return '';
      const on = global ? (r.asset.copies_n || (r.asset.available_nodes || []).length) > 0 : isLocal(r);
      const tip = on ? (global ? L('loc_some','Au moins une copie') : L('local_tip','Présent en local')) : (global ? L('loc_offline','hors ligne') : L('loc_none','Pas de copie ici'));
      return `<i class="ml-loc${on ? '' : ' off'}" title="${esc(tip)}" aria-label="${esc(tip)}" role="img"></i>`;
    }
    const cellHtml = (r, key) => { const d = fieldDef(key); if(!d) return ''; if(d.html) return d.html(r); return r.asset && d.text ? esc(d.text(r.asset)) : ''; };
    // Libellé d'un groupe (« Aujourd'hui », nom du projet, du nœud…), et sa clé de tri.
    function groupLabel(by, key){
      if(key === '') return L('group_empty.' + by, '—');
      if(by === 'day'){
        const d = new Date(key + 'T00:00:00'), now = new Date(), day = 864e5;
        const t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
        if(d.getTime() === t0) return L('group_today','Aujourd\'hui');
        if(d.getTime() === t0 - day) return L('group_yesterday','Hier');
        try{ return d.toLocaleDateString(document.documentElement.lang || undefined, {weekday:'long', day:'numeric', month:'long', year:'numeric'}); }catch(_){ return key; }
      }
      if(by === 'project') return projName(key);
      if(by === 'origin') return originLabel(key);
      if(by === 'codec') return codecName(key);
      return key;
    }
    function groupHead(gi){
      const g = st.groups[gi], n = g.sk.length;
      const cnt = (g.dur > 0 ? L('group_count_dur','{n} médias · {d}').replace('{d}', fmtDur(g.dur)) : L('group_count','{n} médias')).replace('{n}', n);
      return `<div class="ml-grp-h" role="button" tabindex="0" data-grp="${gi}" title="${esc(L('group_sel_tip','Cliquer : sélectionner tout le groupe (Ctrl : l\'ajouter)'))}">`
        + `<span class="ml-grp-nm">${esc(g.label)}</span><span class="ml-grp-n">${esc(cnt)}</span></div>`;
    }
    function selectGroup(gi, e){
      const g = st.groups[gi]; if(!g) return;
      const add = e && (e.ctrlKey || e.metaKey);
      if(add && g.sk.every(k => st.sel.has(k))) g.sk.forEach(k => st.sel.delete(k));
      else { if(!add) st.sel.clear(); g.sk.forEach(k => st.sel.add(k)); }
      st.anchor = g.sk[0] || null; paintSel();
    }

    // ── Barre : sélecteur de mode et options du mode ──
    const curMode = () => st.tab === 'net' ? 'table' : view;
    function syncBar(mode){
      $$('.ml-mode').forEach(b => { const on = b.dataset.view === view; b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on)); });
      $('.ml-modes').hidden = st.tab === 'net';
      $('.ml-grp-l').hidden = !(st.tab === 'base' && GROUPABLE.has(mode));
      $('.ml-group').value = vs.group;
      $('.ml-size-l').hidden = mode !== 'grid';
      $('.ml-size').value = vs.cell;
      $('.ml-cardf-btn').hidden = mode !== 'cards';
      root.style.setProperty('--ml-cell', vs.cell + 'px');
      root.dataset.view = mode;
    }
    function setView(v){
      if(!viewsHere.includes(v) || v === view) return;
      view = v; saveView(); render();
      const b = $(`.ml-mode[data-view="${v}"]`); if(b && document.activeElement !== b && root.contains(document.activeElement)) b.focus();
    }
    $('.ml-modes').onclick = e => { const b = e.target.closest('[data-view]'); if(b) setView(b.dataset.view); };
    $('.ml-group').onchange = e => { if(!GROUPS.includes(e.target.value)) return; vs.group = e.target.value; saveView(); render(); };
    $('.ml-size').oninput = e => { vs.cell = clampInt(+e.target.value || 168, CELL_MIN, CELL_MAX); root.style.setProperty('--ml-cell', vs.cell + 'px'); };
    $('.ml-size').onchange = () => saveView();

    // Déplace le repère de l'élément COURANT sans reconstruire la liste. `render()` recrée toutes les vignettes :
    // choisir une image les rechargeait toutes (images fixes, 2026-10-18), pour un simple liseré à déplacer.
    function markCurrent(){
      const cur = o.current ? o.current() : '';
      $$('.ml-main [data-k][data-path]').forEach(el => el.classList.toggle('is-cur', !!cur && el.dataset.path === cur));
    }
    function render(){
      scrubStop();                                   // la vignette survolée va disparaître : couper son flux d'abord
      stopBatches(); sbStop();
      const main = $('.ml-main'), rs = rows(), shown = rs.slice(0, LIB_MAX), mode = curMode();
      const cur = o.current ? o.current() : '';
      syncBar(mode);
      if(mode !== 'preview') pvStop(true);
      $('.ml-count').textContent = rs.length > LIB_MAX ? L('count_more','{n} sur {t} — affinez la recherche').replace('{n}', LIB_MAX).replace('{t}', rs.length)
        : L('count','{n} éléments').replace('{n}', rs.length);
      renderChips();
      if(!shown.length){
        const msg = st.tab === 'base' && !st.regOk ? L('reg_ko','Base des médias inaccessible.')
          : st.tab === 'base' && !st.q && !st.tree.items.length && !st.bin ? L('reg_empty','Aucun média dans la base. On en importe depuis l\'onglet Serveur réseau, ou dans Réglages → Médias → Base des médias.')
          : L('no_match','Rien ne correspond.');
        st.order = []; st.groups = [];
        main.innerHTML = `<div class="ml-empty">${esc(msg)}</div>`; renderBulk(); return;
      }
      // Libellés des champs personnalisés affichés (colonnes, cartes) : le catalogue des champs d'abord.
      if(!st.sf && (mode === 'table' || mode === 'cards') && (mode === 'table' ? colsOf() : cardFieldsOf()).some(k => /^meta\./.test(k)))
        loadSF().then(() => { if(root.isConnected) render(); });
      // Les projets des médias ne sont lus qu'à la demande : on rend tout de suite, puis à leur arrivée.
      if(needsProjects(mode) && !_projP) loadProjects().then(() => { if(root.isConnected && needsProjects(curMode())) render(); });
      // Regroupement (liste, vignettes, cartes ; onglet Médias)
      const grouped = st.tab === 'base' && GROUPABLE.has(mode) && vs.group !== 'none';
      let entries;
      if(grouped){
        const by = vs.group;
        st.groups = buildGroups(shown.map((r, k) => ({k, keys:groupKeys(r.asset, by, _proj || {}), dur:((r.asset || {}).tech || {}).duration})),
          by === 'day', key => by === 'day' ? key : groupLabel(by, key).toLowerCase());
        entries = [];
        st.groups.forEach((g, gi) => { g.label = groupLabel(by, g.key); g.sk = g.ks.map(k => keyOf(shown[k])).filter(Boolean);
          entries.push({g:gi}); g.ks.forEach(k => entries.push({r:shown[k], k})); });
      } else { st.groups = []; entries = shown.map((r, k) => ({r, k})); }
      const seen = new Set();
      st.order = entries.filter(x => x.r).map(x => keyOf(x.r)).filter(k => k && !seen.has(k) && seen.add(k));
      const rowCls = r => `${r.path && r.path === cur ? ' is-cur' : ''}${r.path || r.ext || global ? '' : ' is-remote'}${selCls(r)}`;

      if(mode === 'grid' || mode === 'cards'){
        main.innerHTML = '<div class="ml-scroll ml-gscroll"></div>';
        const sc = main.firstChild; let box = null;
        const item = mode === 'grid' ? gridCell : cardOf;
        const open = () => { box = document.createElement('div'); box.className = mode === 'grid' ? 'ml-grid' : 'ml-cards';
          box.setAttribute('role', 'listbox'); box.setAttribute('aria-multiselectable', 'true'); box.setAttribute('aria-label', L('view.' + mode, mode)); };
        batched(sc, entries, part => {
          let html = '';
          const flush = () => { if(html){ if(!box.isConnected) sc.insertBefore(box, sc.querySelector('.ml-sentinel')); box.insertAdjacentHTML('beforeend', html); html = ''; } };
          for(const en of part){
            if(en.g != null){
              if(box) flush();
              const sec = document.createElement('section'); sec.className = 'ml-grp';
              sec.innerHTML = groupHead(en.g); sc.insertBefore(sec, sc.querySelector('.ml-sentinel'));
              open(); sec.appendChild(box);
            } else { if(!box) open(); html += item(en.r, en.k, rowCls(en.r)); }
          }
          flush();
        });
        renderBulk(); return;
      }
      if(mode === 'strip'){ renderStrip(main, entries, rowCls); renderBulk(); return; }
      if(mode === 'preview'){ renderPreview(main, entries, rowCls); renderBulk(); return; }

      // Liste : colonnes au choix (base, onglets de l'hôte) ; serveur réseau : nom et taille.
      const sortable = st.tab === 'base';
      const cols = st.tab === 'net' ? ['netsize'] : colsOf();
      const ncol = cols.length + 2;
      const wrapW = (key, h) => { const w = vs.colW[key]; return w ? `<div class="ml-cw" style="width:${w}px">${h}</div>` : `<div class="ml-cw is-auto">${h}</div>`; };
      const arrow = key => st.sort.key === key && key ? (st.sort.dir > 0 ? ' ▲' : ' ▼') : '';
      const thCol = key => { const d = fieldDef(key);
        return `<th data-col="${esc(key)}"${sortable ? ` data-sort="${esc(key)}"` : ''} class="${sortable ? 'ml-sortable ' : ''}${d.cls || ''}">${wrapW(key, esc(d.label()) + arrow(key))}`
          + (st.tab === 'net' ? '' : `<span class="ml-col-rs" data-rs="${esc(key)}" title="${esc(L('col_resize','Glisser : largeur ; double-clic : automatique'))}"></span>`) + '</th>'; };
      main.innerHTML = `<div class="ml-scroll"><table class="ml-table" aria-multiselectable="true"><thead><tr>
          <th${sortable ? ' data-sort="name" class="ml-sortable ml-c-name"' : ' class="ml-c-name"'}>${esc(L('col_name','Nom'))}${arrow('name')}</th>${cols.map(thCol).join('')}<th></th></tr></thead><tbody></tbody></table></div>`;
      const sc = main.firstChild, tb = sc.querySelector('tbody');
      const tr = (r, k) => {
        if(r.dir != null) return `<tr class="ml-dir" data-dir="${esc(r.dir)}"><td colspan="${ncol}"><span aria-hidden="true">${r.dir === '..' ? '⬆' : '📁'}</span> ${esc(r.name)}</td></tr>`;
        return `<tr class="ml-row${rowCls(r)}" ${attrs(r, k)} aria-selected="${!!selCls(r)}">
          <td class="ml-c-name">${thumbOf(r)}<span class="ml-nm">${localMark(r)}<span title="${esc(r.path || r.ext || r.name)}">${esc(r.name)}</span>${homTag(r)}${cols.includes('compat') ? baseBadge(r) : badge(r)}</span></td>
          ${cols.map(key => { const d = fieldDef(key); return `<td class="${d.mono ? 'ml-mono ' : ''}${d.cls || ''}">${wrapW(key, cellHtml(r, key))}</td>`; }).join('')}
          <td><span class="ml-acts">${actionBtns(r)}</span></td></tr>`;
      };
      batched(sc, entries, part => tb.insertAdjacentHTML('beforeend', part.map(en => en.g != null
        ? `<tr class="ml-grp-row"><td colspan="${ncol}">${groupHead(en.g)}</td></tr>` : tr(en.r, en.k)).join('')));
      const th = sc.querySelector('thead'); if(th) root.style.setProperty('--ml-thh', th.offsetHeight + 'px');
      renderBulk();
    }
    // Vignettes : nom sur deux lignes ; présence, définition·cadence, codec ; durée et P sur l'image.
    function gridCell(r, k, cls){
      const a = r.asset;
      return `<div class="ml-cell${cls}" role="option" aria-selected="${!!selCls(r)}" ${attrs(r, k)}>${thumbBox(r)}
        <div class="ml-cell-nm" title="${esc(r.path || r.ext || r.name)}">${esc(r.name)}${homTag(r)}</div>
        <div class="ml-cell-meta">${locDot(r)}${a ? `<span class="ml-mono">${esc(defRate(a))}</span>${codecOf(a) ? `<span aria-hidden="true">·</span><span class="ml-mono">${esc(codecOf(a))}</span>` : ''}` : (r.size ? `<span class="ml-mono">${esc(fmtSize(r.size))}</span>` : '')}${badge(r)}</div>
        <div class="ml-acts">${actionBtns(r)}</div></div>`;
    }
    // Cartes : titre, description (2 lignes), champs choisis, mots-clés ; une fiche incomplète se voit.
    function cardOf(r, k, cls){
      const a = r.asset, fl = cardFieldsOf();
      let body = '';
      if(a){
        const m = a.meta || {};
        if(fl.includes('description')) body += `<p class="ml-cd-desc">${m.description ? esc(m.description) : `<span class="ml-warn">${esc(L('card_no_desc','Pas de description'))}</span>`}</p>`;
        const kv = fl.filter(x => x !== 'description' && x !== 'keywords').map(x => { const d = fieldDef(x); if(!d) return '';
          const v = d.html ? d.html(r) : esc(d.text(a)); return `<dt>${esc(d.label())}</dt><dd>${v || '—'}</dd>`; }).join('');
        if(kv) body += `<dl class="ml-cd-kv">${kv}</dl>`;
        const kws = kwOf(a);
        body += `<div class="ml-cd-chips">${locDot(r)}${fl.includes('keywords') ? (kws.length ? kws.map(x => `<span class="ml-kw">${esc(x)}</span>`).join('')
          : `<span class="ml-kw ml-warn">${esc(L('card_no_kw','aucun mot-clé'))}</span>`) : ''}${badge(r)}</div>`;
      }
      return `<article class="ml-cd${cls}" role="option" aria-selected="${!!selCls(r)}" ${attrs(r, k)}>${thumbBox(r)}
        <div class="ml-cd-body"><h4 class="ml-cd-title" title="${esc(r.path || r.ext || r.name)}">${esc(r.name)}${homTag(r)}</h4>${body}
        <div class="ml-acts">${actionBtns(r)}</div></div></article>`;
    }

    // ── Pellicule : 8 images de la planche du média, réparties sur sa durée ──
    const SB_FRAMES = 8;
    const _sb = new Map();                          // asset_id → Promise<planche | null>
    let _sbIo = null;
    function sbStop(){ if(_sbIo){ _sbIo.disconnect(); _sbIo = null; } }
    function loadSb(a){
      if(!_sb.has(a.id)) _sb.set(a.id, fetch('/api/media/assets/' + encodeURIComponent(a.id) + '/storyboard.json')
        .then(r => r.ok ? r.json() : null).then(d => sbValid(d) ? d : null).catch(() => null));
      return _sb.get(a.id);
    }
    const frameBtn = (t, inner, style) => `<button type="button" class="ml-st-f" data-sbt="${t.toFixed(2)}"${style ? ` style="${esc(style)}"` : ''}`
      + ` title="${esc(L('st_open_at','Ouvrir la fiche Infos à {t}').replace('{t}', fmtClock(t)))}">${inner}<span class="ml-st-t">${esc(fmtClock(t))}</span></button>`;
    // Sans planche (pas encore fabriquée) : un bandeau, et la vignette répétée en attendant.
    function stripWaiting(r){
      const dur = (r.asset.tech || {}).duration || 0;
      return `<div class="ml-st-wait">${esc(L('st_pending','Planche d\'images en cours de préparation'))}</div>`
        + Array.from({length:SB_FRAMES}, (_, i) => frameBtn(dur > 0 ? (i + 0.5) * dur / SB_FRAMES : 0, `<img loading="lazy" decoding="async" alt="" src="${assetThumb(r.asset)}">`)).join('');
    }
    function stripFrames(sb, a){
      const dur = (a.tech || {}).duration || 0, url = sb.url.replace(/["'()\\\s]/g, c => '%' + c.charCodeAt(0).toString(16).padStart(2, '0'));
      return pickFrames(sb.n, SB_FRAMES).map(i => { const c = sbCell(sb, i);
        return frameBtn(sbTime(sb, i, dur), '', `background-image:url('${url}');background-size:${c.size[0]}% ${c.size[1]}%;background-position:${c.pos[0].toFixed(3)}% ${c.pos[1].toFixed(3)}%`); }).join('');
    }
    function stripOf(r, k, cls){
      const a = r.asset, kd = a ? kindOf(a) : '';
      let strip;
      if(!a) strip = '';
      else if(kd === 'audio') strip = `<div class="ml-st-note">${esc(L('st_audio','Son seul : pas d\'images'))}</div>`;
      else if(kd === 'image') strip = `<div class="ml-st-one">${thumbImg(r)}</div>`;
      else if(a.growing) strip = `<div class="ml-st-note">${esc(L('st_growing','Enregistrement en cours'))}</div>`;
      else if(a.has_storyboard) strip = `<div class="ml-st-strip is-pending" data-sb="${esc(a.id)}">${'<span class="ml-st-f is-blank"></span>'.repeat(SB_FRAMES)}</div>`;
      else strip = `<div class="ml-st-strip is-wait">${stripWaiting(r)}</div>`;
      const meta = a ? `${locDot(r)}<span class="ml-mono">${esc(kd === 'image' ? '' : fmtDur((a.tech || {}).duration))}</span><span class="ml-mono">${esc(defRate(a))}</span>${badge(r)}` : '';
      return `<div class="ml-st-r${cls}" role="option" aria-selected="${!!selCls(r)}" ${attrs(r, k)}>
        <div class="ml-st-info"><div class="ml-st-nm" title="${esc(r.path || r.ext || r.name)}">${esc(r.name)}${homTag(r)}</div><div class="ml-st-meta">${meta}</div>
        <div class="ml-acts">${actionBtns(r)}</div></div>${strip}</div>`;
    }
    function renderStrip(main, entries, rowCls){
      main.innerHTML = `<div class="ml-scroll ml-st" role="listbox" aria-multiselectable="true" aria-label="${esc(L('view.strip','Pellicule'))}"></div>`;
      const sc = main.firstChild;
      // Planches lues quand la ligne approche de l'écran, pas pour 2000 médias d'un coup.
      _sbIo = typeof IntersectionObserver === 'function' ? new IntersectionObserver(es => es.forEach(x => {
        if(!x.isIntersecting) return; _sbIo && _sbIo.unobserve(x.target); fillStrip(x.target); }), {root:sc, rootMargin:'0px 0px 400px 0px'}) : null;
      batched(sc, entries, part => {
        sc.insertAdjacentHTML('beforeend', part.map(en => stripOf(en.r, en.k, rowCls(en.r))).join(''));
        const sent = sc.querySelector('.ml-sentinel'); if(sent) sc.appendChild(sent);
        sc.querySelectorAll('.ml-st-strip.is-pending:not([data-obs])').forEach(el => { el.dataset.obs = '1'; if(_sbIo) _sbIo.observe(el); else fillStrip(el); });
      });
    }
    async function fillStrip(el){
      const a = assetById(el.dataset.sb); if(!a) return;
      const sb = await loadSb(a);
      if(!el.isConnected) return;
      el.classList.remove('is-pending');
      if(sb){ el.style.setProperty('--sb-ar', (sb.w > 0 && sb.h > 0 ? sb.w / sb.h : 16 / 9).toFixed(4)); el.innerHTML = stripFrames(sb, a); }
      else { el.classList.add('is-wait'); el.innerHTML = stripWaiting({asset:a}); }
    }

    // ── Liste + aperçu : liste compacte, et à côté le lecteur du proxy, la fiche résumée, les actions ──
    const pv = {aid:null, t:null, poll:null};
    // Arrêt du lecteur d'aperçu POUR DE BON (son, connexion Range) ; `forget` : le prochain rendu repart de zéro.
    function pvStop(forget){
      clearTimeout(pv.t); clearTimeout(pv.poll);
      const m = $('.ml-pv-side video, .ml-pv-side audio'); if(m) teardownVideo(m);
      if(forget) pv.aid = null;
    }
    function renderPreview(main, entries, rowCls){
      const old = main.querySelector('.ml-pv-side');
      main.innerHTML = `<div class="ml-pv"><div class="ml-scroll ml-pv-list" role="listbox" aria-multiselectable="true" aria-label="${esc(L('view.preview','Liste + aperçu'))}"></div>
        <div class="ml-pv-side" aria-live="polite"></div></div>`;
      // Le lecteur en place reste (relire la liste ne coupe pas la lecture) ; il est remplacé au besoin.
      // Ses boutons d'action désignent la ligne par son INDEX : on le remet à jour (la liste a pu changer).
      const keep = old && pv.aid ? entries.find(x => x.r && x.r.asset && x.r.asset.id === pv.aid) : null;
      if(keep){ old.querySelectorAll('[data-k]').forEach(x => { x.dataset.k = keep.k; }); main.querySelector('.ml-pv-side').replaceWith(old); }
      else { const m = old && old.querySelector('video,audio'); if(m) teardownVideo(m); pv.aid = null; }
      const sc = main.querySelector('.ml-pv-list');
      batched(sc, entries, part => {
        sc.insertAdjacentHTML('beforeend', part.map(({r, k}) => `<div class="ml-pv-r${rowCls(r)}" role="option" aria-selected="${!!selCls(r)}" ${attrs(r, k)}>
          ${thumbOf(r)}<span class="ml-pv-nm">${localMark(r)}<span title="${esc(r.path || r.ext || r.name)}">${esc(r.name)}</span>${homTag(r)}${badge(r)}</span>
          <span class="ml-mono">${r.asset ? esc(FIELDS.duration.text(r.asset)) : esc(fmtSize(r.size))}</span></div>`).join(''));
        const sent = sc.querySelector('.ml-sentinel'); if(sent) sc.appendChild(sent);
      });
      // Rien de sélectionné : le premier média, pour que l'aperçu ne soit jamais vide à l'arrivée.
      if(!st.sel.size && st.order.length){ st.sel.add(st.order[0]); st.anchor = st.order[0]; paintSel(); }
      else pvRefresh();
    }
    const pvKey = () => st.anchor && st.sel.has(st.anchor) ? st.anchor : [...st.sel][0] || null;
    function pvRefresh(){
      if(curMode() !== 'preview') return;
      const side = $('.ml-pv-side'); if(!side) return;
      const key = pvKey(), all = rows(), k = key ? all.findIndex(x => keyOf(x) === key) : -1, r = k >= 0 ? all[k] : null;
      const aid = r && r.asset ? r.asset.id : null;
      if(key && aid && aid === pv.aid && side.childElementCount) return;
      pvStop(); pv.aid = aid;
      if(!r){ side.innerHTML = `<p class="ml-pv-empty">${esc(L('pv_none','Sélectionnez un média : son aperçu s\'affiche ici.'))}</p>`; return; }
      const acts = `<div class="ml-pv-acts" ${attrs(r, k, false)}><span class="ml-acts">${actionBtns(r)}</span></div>`;
      if(!aid){ side.innerHTML = `<h4 class="ml-pv-title">${esc(r.name)}${homTag(r)}</h4>${acts}`; return; }
      // Tout de suite la vignette et le titre ; la fiche complète (proxys, projets) suit.
      side.innerHTML = `<div class="ml-pv-media">${r.asset && (r.asset.tech || {}).video ? `<img class="ml-if-thumbbig" alt="" src="${esc(assetThumb(r.asset))}">` : ''}</div>
        <h4 class="ml-pv-title">${esc(r.name)}${homTag(r)}</h4>${acts}`;
      pv.t = setTimeout(() => pvLoad(aid), 120);
    }
    async function pvLoad(aid){
      let a;
      try{ const r = await fetch('/api/media/assets/' + encodeURIComponent(aid)); if(!r.ok) throw new Error('HTTP ' + r.status); a = await r.json(); }
      catch(e){ return; }
      if(!st.sf) await loadSF();                    // noms des projets
      const side = $('.ml-pv-side');
      if(!side || pv.aid !== aid || curMode() !== 'preview') return;
      const all = rows(), k = all.findIndex(x => x.asset && x.asset.id === aid), r = all[k]; if(!r) return;
      const t = a.tech || {}, v = t.video || {}, m = a.meta || {}, kd = kindOf(a), kws = kwOf(a);
      const facts = [];
      if(v.width) facts.push([kd === 'image' ? L('if_image','Image') : L('if_video','Vidéo'), kd === 'image' ? `${v.width}×${v.height} · ${codecName(v.codec)}` : fmtVideo(v)]);
      if(kd !== 'image'){ facts.push([L('col_dur','Durée'), a.growing ? L('growing','croissant') : fmtDur(t.duration)]); facts.push([L('col_audio','Audio'), [].concat(audioGroups(t.audio || [])).join(' ; ')]); }
      const cps = (a.copies || []).filter(c => c.state === 'present');
      facts.push([L('col_copies','Copies'), cps.length ? [...new Set(cps.map(c => c.node))].join(', ') : L('loc_offline','hors ligne')]);
      const pn = (a.projects || []).map(projName);
      facts.push([L('if_projects','Projets'), pn.length ? pn.join(', ') : '—']);
      const kvHtml = facts.map(([x, y]) => `<dt>${esc(x)}</dt><dd>${esc(y)}</dd>`).join('')
        + `<dt>${esc(L('if_kw','Mots-clés'))}</dt><dd>${kws.length ? kws.map(x => `<span class="ml-kw">${esc(x)}</span>`).join(' ') : `<span class="ml-warn">${esc(L('card_no_kw','aucun mot-clé'))}</span>`}</dd>`;
      const keepMedia = side.querySelector(`.ml-pv-media [data-aid="${CSS.escape(a.id)}"]`) && (a.proxies || []).some(p => p.state === 'present');
      const media = keepMedia ? side.querySelector('.ml-pv-media') : null;
      side.innerHTML = `<div class="ml-pv-media">${mediaBlock(a, null)}</div>
        <h4 class="ml-pv-title" title="${esc(a.name)}">${esc(m.title || a.name)}</h4>
        ${m.description ? `<p class="ml-pv-desc">${esc(m.description)}</p>` : ''}
        <dl class="ml-pv-kv">${kvHtml}</dl>
        <div class="ml-pv-acts" ${attrs(r, k, false)}><span class="ml-acts">${actionBtns(r)}</span></div>`;
      if(media) side.querySelector('.ml-pv-media').replaceWith(media);
      // Proxy en file ou en cours : l'aperçu suit, puis passe au lecteur.
      if(pxJob(a)) pv.poll = setTimeout(() => { if(pv.aid === aid) pvLoad(aid); }, 3000);
    }
    // ↑ ↓ : média précédent / suivant (la liste suit) ; rendu des lots suivants au besoin.
    function pvMove(d){
      if(!st.order.length) return;
      const i = st.order.indexOf(pvKey()), j = Math.max(0, Math.min(st.order.length - 1, i < 0 ? 0 : i + d));
      const key = st.order[j];
      st.sel.clear(); st.sel.add(key); st.anchor = key; paintSel();
      const find = () => [...$$('.ml-pv-r')].find(x => x.dataset.key === key);
      let el = find(); while(!el && _more && _more()) el = find();
      if(el) el.scrollIntoView({block:'nearest'});
    }
    function pvToggle(){
      const m = $('.ml-pv-side video, .ml-pv-side audio'); if(!m) return false;
      if(m.paused) m.play().catch(() => {}); else m.pause();
      return true;
    }
    // ── Sélection (clic sur la ligne ; Ctrl/Cmd, Maj, Ctrl+A) ──
    function selectedRows(){
      // Index par clé : une sélection de 2000 médias ne doit pas coûter 2000 × 2000 comparaisons.
      const all = new Map(rows().map(x => [keyOf(x), x])), out = [];
      st.sel.forEach(k => {
        const r = all.get(k)
          || (k.startsWith('ext:') ? {ext:k.slice(4), name:baseName(k.slice(4))} : (st.all.find(a => a.id === k) ? {asset:st.all.find(a => a.id === k)} : null));
        if(r) out.push(r);
      });
      return out;
    }
    function paintSel(){
      $$('.ml-main [data-key]').forEach(el => { const on = !!el.dataset.key && st.sel.has(el.dataset.key);
        el.classList.toggle('is-sel', on); if(el.hasAttribute('aria-selected')) el.setAttribute('aria-selected', on); });
      renderBulk(); pvRefresh();
    }
    function selectClick(el, e){
      const key = el.dataset.key; if(!key) return;
      if(e.shiftKey && st.anchor){
        const keys = st.order;            // ordre AFFICHÉ, lots pas encore rendus compris
        const a = keys.indexOf(st.anchor), b = keys.indexOf(key);
        if(a >= 0 && b >= 0){
          if(!(e.ctrlKey || e.metaKey)) st.sel.clear();
          keys.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(k => st.sel.add(k));
        }
      } else if(e.ctrlKey || e.metaKey){
        st.sel.has(key) ? st.sel.delete(key) : st.sel.add(key); st.anchor = key;
      } else { st.sel.clear(); st.sel.add(key); st.anchor = key; }
      paintSel();
    }
    function selectAll(){ st.order.forEach(k => st.sel.add(k)); paintSel(); }
    function renderBulk(){
      const bar = $('.ml-bulk'), n = st.sel.size;
      // Gestionnaire (`selectable`) : la barre dès une ligne ; ailleurs, dès une sélection MULTIPLE.
      bar.hidden = !(n && (o.selectable || n > 1));
      if(bar.hidden) return;
      $('.ml-bulk-n').textContent = L('sel_n','{n} sélectionné(s)').replace('{n}', n);
      const multi = actions.filter(x => x.multi);
      $('.ml-bulk-acts').innerHTML = multi.map(x => `<button type="button" class="btn" data-bulk="act:${esc(x.id)}">${esc(x.label)}</button>`).join('')
        + (o.bulk || []).map((b, i) => `<button type="button" class="btn" data-bulk="${i}"${b.items ? ' aria-haspopup="menu"' : ''}>${esc(b.label)}${b.items ? ' ▾' : ''}</button>`).join('')
        + `<button type="button" class="btn" data-bulk="bin" aria-haspopup="menu">${esc(L('sel_to_bin','Ranger dans un bin'))} ▾</button>`;
    }
    $('.ml-bulk-clear').onclick = () => { st.sel.clear(); paintSel(); };
    $('.ml-bulk-all').onclick = () => selectAll();
    function toBinItems(ids){
      const manual = st.bins.filter(x => !x.smart);
      return manual.length ? manual.map(x => ({label:x.name, run: () => addToBin(x, ids)}))
        : [{header: L('no_manual_bin','Aucun bin manuel : créez-en un avec « + Bin ».')}];
    }
    async function addToBin(b, ids){
      if(!ids.length) return;
      if(await regCall('POST', `/api/media/bins/${b.id}/items`, {asset_ids:ids})){
        toast(L('bin_added','Rangé dans « {b} »').replace('{b}', b.name), 'info'); await loadBins(); binsChanged();
        if(st.bin === b.id){ await loadAssets(); render(); }
      }
    }
    // Sélection multiple et action d'un conteneur : la même action, pour chaque média, dans l'ordre.
    async function actOnRows(id, rs){ for(const r of rs){ if(r.asset || r.ext) await actRow(id, r); } }
    $('.ml-bulk-acts').onclick = e => {
      const b = e.target.closest('[data-bulk]'); if(!b) return;
      const rs = selectedRows();
      if(b.dataset.bulk === 'bin') return openMenu(b, toBinItems(rs.filter(r => r.asset).map(r => r.asset.id)));
      if(b.dataset.bulk.startsWith('act:')) return actOnRows(b.dataset.bulk.slice(4), rs);
      const def = (o.bulk || [])[+b.dataset.bulk]; if(!def) return;
      if(def.items) return openMenu(b, def.items(rs));
      if(def.run) def.run(rs);
    };
    // ── Menu flottant (clic droit, « ⋯ », sous-choix d'une action groupée) ──
    const menuEl = $('.ml-menu');
    // Un élément `check` (vrai/faux) est une case à cocher : le menu RESTE ouvert et `rebuild()` le
    // redessine (choix des colonnes, des champs d'une carte).
    function openMenu(anchor, items, at, rebuild){
      menuEl.innerHTML = items.length ? items.map((it, i) => it.header != null
        ? (it.header ? `<div class="ml-menu-h">${esc(it.header)}</div>` : '<hr class="ml-menu-sep">')
        : it.check != null
        ? `<button type="button" role="menuitemcheckbox" aria-checked="${!!it.check}" class="ml-menu-it ml-menu-chk" data-i="${i}"><span class="ml-chk" aria-hidden="true">${it.check ? '✓' : ''}</span>${esc(it.label)}</button>`
        : `<button type="button" role="menuitem" class="ml-menu-it${it.danger ? ' danger' : ''}" data-i="${i}"${it.disabled ? ' disabled' : ''}>${esc(it.label)}</button>`).join('')
        : `<div class="ml-menu-h">${esc(L('menu_empty','Aucune action'))}</div>`;
      menuEl._items = items; menuEl._rebuild = rebuild || null;
      menuEl.hidden = false;
      // Position voulue dans la FENÊTRE (pointeur, ou sous le bouton), ramenée dans l'écran, puis
      // convertie en position relative à la bibliothèque (menu en absolu dans `.ml`).
      const ra = anchor ? anchor.getBoundingClientRect() : null, rr = root.getBoundingClientRect();
      const mw = menuEl.offsetWidth || 240, mh = menuEl.offsetHeight || 200;
      let x = at ? at.x : ra.left, y = at ? at.y : ra.bottom + 4;
      x = Math.max(4, Math.min(x, window.innerWidth - mw - 8));
      if(y + mh > window.innerHeight - 8) y = Math.max(4, (at ? at.y : ra.top) - mh - 4);
      menuEl.style.left = (x - rr.left) + 'px';
      menuEl.style.top = (y - rr.top) + 'px';
      const first = menuEl.querySelector('.ml-menu-it'); if(first) first.focus({preventScroll:true});
    }
    const closeMenu = () => { menuEl.hidden = true; };
    menuEl.onclick = e => { const b = e.target.closest('.ml-menu-it'); if(!b) return;
      const it = (menuEl._items || [])[+b.dataset.i];
      if(it && it.check != null && menuEl._rebuild){ const i = b.dataset.i, top = menuEl.scrollTop; it.run(); menuEl._rebuild();
        menuEl.scrollTop = top; const nb = menuEl.querySelector(`[data-i="${i}"]`); if(nb) nb.focus({preventScroll:true}); return; }
      closeMenu(); if(it && it.run) it.run(); };
    const _closeMenu = e => { if(!menuEl.hidden && !menuEl.contains(e.target)) closeMenu(); };
    document.addEventListener('mousedown', _closeMenu, true);
    const _closeOnScroll = e => { if(!menuEl.hidden && !menuEl.contains(e.target)) closeMenu(); };
    window.addEventListener('scroll', _closeOnScroll, true);
    window.addEventListener('blur', closeMenu);
    menuEl.addEventListener('keydown', e => {
      if(e.key === 'Escape'){ closeMenu(); return; }
      if(e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const its = [...menuEl.querySelectorAll('.ml-menu-it:not([disabled])')], i = its.indexOf(document.activeElement);
      if(its.length){ e.preventDefault(); its[(i + (e.key === 'ArrowDown' ? 1 : its.length - 1)) % its.length].focus(); }
    });

    // Menu d'UNE ligne : ses actions, puis Infos / Éditer / Renommer / Retirer du bin, puis ce
    // que l'hôte ajoute (`rowMenu` : envoyer, copier, transcoder, supprimer… au Gestionnaire).
    function rowMenuItems(r){
      const a = r.asset, cur = st.bins.find(b => b.id === st.bin), out = [];
      if(r.ext){
        if(!global){ actions.forEach(x => out.push({label:x.label, run:() => actRow(x.id, r)}));
          out.push({label:L('import','Importer'), run:() => importExt(r.ext)}); }
      } else if(a){
        const canFetch = !global && !r.path && (a.available_nodes || []).length > 0;
        actions.forEach(x => { const needs = x.needsLocal !== false && !r.path;
          out.push({label:x.label + (needs && canFetch ? ' ⇣' : ''), disabled: needs && !canFetch, run:() => actRow(x.id, r)}); });
        if(out.length) out.push({header:''});
        out.push({label:L('info','Infos…'), run:() => openInfo(a.id)});
        if(kindOf(a) === 'video' || kindOf(a) === 'audio')
          out.push({label:a.has_proxy ? L('px_recreate','Recréer le proxy') : L('px_create','Créer le proxy'), run:() => createProxies([a.id])});
        if(kindOf(a) === 'image' && window.ImageEditor && typeof window.ImageEditor.open === 'function')
          out.push({label:L('edit','Éditer…'), run:() => { const fmt = typeof o.imageFormat === 'function' ? o.imageFormat() : o.imageFormat;
            window.ImageEditor.open(a.id, Object.assign({onSaved: () => reload()}, fmt && fmt.w ? {format:fmt} : {})); }});
        if(!a.shared){
          out.push({label:L('rename_file','Renommer le fichier…'), run:() => renameFile(a, r)});
          if(canShare()) out.push({label:L('sh_share','Partager…'), run:() => openShare([{kind:'asset', id:a.id, name:a.name}])});
        }
        if(st.tab === 'base' && cur && !cur.smart && (!cur.shared || cur.shared.level === 'edit')) out.push({label:L('bin_remove','Retirer du bin'), run:() => unbin([a.id])});
      }
      const extra = o.rowMenu ? (o.rowMenu(r) || []) : [];
      if(extra.length){ out.push({header:''}); out.push(...extra); }
      return out;
    }
    // Menu d'une SÉLECTION multiple : ce qui a un sens pour plusieurs médias à la fois.
    function selMenuItems(){
      const rs = selectedRows(), ids = rs.filter(r => r.asset).map(r => r.asset.id), out = [{header:L('sel_n','{n} sélectionné(s)').replace('{n}', rs.length)}];
      actions.filter(x => x.multi).forEach(x => out.push({label:x.label, run:() => actOnRows(x.id, rs)}));
      (o.bulk || []).forEach(b => { if(b.items){ const its = b.items(rs) || []; if(its.length){ out.push({header:b.label}, ...its); } }
                                    else if(b.run) out.push({label:b.label, run:() => b.run(rs)}); });
      const cur = st.bins.find(b => b.id === st.bin);
      if(st.tab === 'base' && cur && !cur.smart && (!cur.shared || cur.shared.level === 'edit')) out.push({label:L('bin_remove','Retirer du bin'), run:() => unbin(ids)});
      const own = rs.filter(r => r.asset && !r.asset.shared).map(r => ({kind:'asset', id:r.asset.id, name:r.asset.name}));
      if(own.length && canShare()) out.push({label:L('sh_share_n','Partager ({n})…').replace('{n}', own.length), run:() => openShare(own)});
      const pxIds = rs.filter(r => r.asset && (kindOf(r.asset) === 'video' || kindOf(r.asset) === 'audio')).map(r => r.asset.id);
      if(pxIds.length) out.push({label:L('px_create_n','Créer les proxys ({n})').replace('{n}', pxIds.length), run:() => createProxies(pxIds)});
      out.push({header:L('sel_to_bin','Ranger dans un bin')}, ...toBinItems(ids));
      return out;
    }
    // Choix des colonnes (clic droit sur l'en-tête de la liste) et des champs des cartes : cases à
    // cocher, champs personnalisés de la base compris ; « par défaut » remet la liste d'origine.
    async function openFieldsMenu(kind, anchor, at){
      if(!st.sf) await loadSF();
      const isCols = kind === 'cols', cur = isCols ? colsOf() : cardFieldsOf(), defs = isCols ? DEF_COLS : DEF_CARDF;
      const keys = [...(isCols ? COL_CHOICES : CARD_CHOICES), ...customKeys()];
      const custom = new Set(customKeys());
      const items = [{header:isCols ? L('cols_h','Colonnes affichées') : L('card_fields_h','Champs affichés sur les cartes')}];
      keys.forEach((k, i) => {
        if(custom.has(k) && (i === 0 || !custom.has(keys[i - 1]))) items.push({header:L('sfg.custom','Champs personnalisés')});
        items.push({label:fieldDef(k).label(), check:cur.includes(k), run:() => {
          if(isCols) vs.cols = toggleField(colsOf(), k, defs); else vs.cardF = toggleField(cardFieldsOf(), k, defs);
          saveView(); render(); }});
      });
      items.push({header:''}, {label:L('fields_reset','Revenir aux champs par défaut'), run:() => {
        if(isCols){ vs.cols = null; vs.colW = {}; } else vs.cardF = null; saveView(); render(); }});
      openMenu(anchor, items, at, () => openFieldsMenu(kind, anchor, at));
    }
    $('.ml-cardf-btn').onclick = e => openFieldsMenu('cards', e.currentTarget);

    // Proxys : mis en file sur l'orchestrateur (un à la fois par nœud) ; les refus sont nommés.
    async function createProxies(ids, binId){
      const r = await regCall('POST', '/api/media/proxies', binId != null ? {bin_id:binId, children:true} : {asset_ids:ids});
      if(!r) return null;
      const ref = Object.entries(r.refused || {});
      toast(L('px_queued','{n} proxy(s) en file de génération').replace('{n}', (r.jobs || []).length)
        + (ref.length ? ' — ' + L('px_refused','{n} refusé(s) : {l}').replace('{n}', ref.length).replace('{l}', ref.slice(0, 3).map(([k, v]) => k + ' (' + v + ')').join(', ')) : ''),
        ref.length ? 'warning' : 'info');
      if(_info && infoDlg.open && ids && ids.includes(_info.id)) openInfo(_info.id, 'info');
      return r;
    }
    async function unbin(ids){
      if(await regCall('POST', `/api/media/bins/${st.bin}/items`, {asset_ids:ids, on:false})){
        ids.forEach(i => st.sel.delete(i)); await loadAssets(); render(); loadBins(); binsChanged(); }
    }

    function setTab(t){
      st.tab = t; st.sel.clear(); st.anchor = null; pvStop(true);
      $$('.ml-tab').forEach(b => { const on = b.dataset.tab === t; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); });
      $('.ml-crumbs').hidden = t !== 'net';
      $('.ml-body').classList.toggle('no-bins', t !== 'base');
      $('.ml-filters-btn').hidden = t !== 'base';
      if(t !== 'base') $('.ml-filters').hidden = true;
      if(t === 'net' && !st.net.dirs.length && !st.net.files.length) loadNet(st.net.path);
      else if(t !== 'base' && t !== 'net') reload();
      else render();
    }

    // ── Événements ──
    $$('.ml-tab').forEach(b => b.onclick = () => setTab(b.dataset.tab));
    let _qT = null;
    $('.ml-search').oninput = e => { st.q = e.target.value.trim().toLowerCase();
      if(st.tab === 'base'){ clearTimeout(_qT); _qT = setTimeout(async () => { await loadAssets(); render(); }, 300); } else render(); };
    $('.ml-refresh').onclick = () => reload();
    const main = $('.ml-main');
    // ── Envoi depuis le poste (static/js/media_upload.js, chargé au premier usage) ──
    let _upLoad = null;
    function uploader(){
      if(window.MediaUpload) return Promise.resolve(window.MediaUpload);
      if(!_upLoad) _upLoad = new Promise((res, rej) => {
        // Version de media_upload.js LUI-MÊME : reprendre celle de media_library.js laissait un navigateur
        // servir un ancien envoi depuis son cache quand seul ce fichier-là avait changé.
        const me = document.querySelector('script[src*="media_library.js"]'),
              v = me && (me.dataset.uploadV ? 'v=' + me.dataset.uploadV : (me.src.split('?')[1] || ''));
        const sc = document.createElement('script'); sc.src = '/static/js/media_upload.js' + (v ? '?' + v : '');
        sc.onload = () => res(window.MediaUpload); sc.onerror = () => { _upLoad = null; rej(new Error('media_upload.js')); };
        document.head.appendChild(sc);
      });
      return _upLoad;
    }
    // Bin manuel visé : celui sur lequel on dépose, sinon celui affiché.
    async function sendFiles(files, binId){
      files = Array.from(files || []); if(!files.length) return;
      let up; try{ up = await uploader(); }catch(e){ toast(e.message, 'error'); return; }
      const b = binId != null ? st.bins.find(x => x.id === binId) : curManual();
      const opts = {bin_id: b && !b.smart ? b.id : null, label: b && !b.smart ? '▤ ' + b.name : ''};
      if(!global) return up.enqueue(files, {vmid}, opts);
      // Gestionnaire : nœud + dossier choisis ; projet repris du filtre « projet = … » s'il y en a un.
      const pf = st.tree.items.find(f => f && f.field === 'project' && f.op === 'eq' && f.value);
      const t = await up.chooseTarget({count: files.length}); if(!t) return;
      up.enqueue(files, {node_id:t.node_id, folder:t.folder, project_id: pf ? +pf.value || null : null}, opts);
    }
    const upIn = $('.ml-upload-in');
    $('.ml-upload').onclick = async () => { upIn.value = ''; upIn.click(); try{ (await uploader()).open(); }catch(_){} };
    upIn.onchange = () => sendFiles(upIn.files);
    const isFiles = dt => !!dt && Array.from(dt.types || []).includes('Files') && !Array.from(dt.types).includes('application/x-bobi-src');
    const _onUploaded = async () => { if(!root.isConnected) return; await loadAssets(); render(); loadBins(); };
    document.addEventListener('ml:uploaded', _onUploaded);
    // Attente d'une tâche de fond de la base (import, copie, rapatriement) ; renvoie son résultat.
    // `onProgress({done, total, name})` : avancement, quand la tâche en publie (copie d'un bin).
    async function waitJob(onProgress){
      for(;;){
        await new Promise(res => setTimeout(res, 1500));
        const j = await (await fetch('/api/media/scan')).json().catch(() => ({}));
        if(onProgress && j.progress) onProgress(j.progress);
        if(j.running) continue;
        if(j.error){ toast(j.error, 'error'); return null; }
        return j.result || {};
      }
    }
    // Rapatriement SUIVI (POST /api/media/fetch → tâche ; GET /api/media/fetch/<job>, une fois par
    // seconde au plus, arrêté à la fin ou quand la bibliothèque disparaît) : l'avancement s'affiche
    // dans la bande de tâche, comme la copie d'un bin. Copie déjà là : `path` tout de suite.
    function fetchTxt(name, p){
      const head = L('fetch_run','Rapatriement de « {n} »').replace('{n}', name);
      if(p && p.state === 'verifying') return head + ' — ' + L('fetch_verifying','vérification…');
      if(!p || p.state !== 'copying' || !p.total_bytes) return head + ' — ' + L('fetch_start','Copie du média sur ce nœud…');
      const pc = Math.max(0, Math.min(100, Math.floor(100 * p.done_bytes / p.total_bytes)));
      return head + ' — ' + (p.eta_s != null ? L('fetch_eta','{p} % · {t} restantes').replace('{t}', fmtDur(Math.max(1, p.eta_s))) : '{p} %').replace('{p}', pc);
    }
    async function fetchThen(aid, then){
      const r = await regCall('POST', '/api/media/fetch', {vmid, asset_id:aid}); if(!r) return;
      let res = r;
      if(r.state !== 'done'){
        const a = st.all.find(x => x.id === aid), name = a ? ((a.meta || {}).title || a.name) : aid;
        const job = $('.ml-job'), txt = $('.ml-job-txt'), bar = $('.ml-job-bar');
        const show = p => { txt.textContent = fetchTxt(name, p); bar.max = (p && p.total_bytes) || 1;
          bar.value = p && p.state === 'verifying' ? bar.max : ((p && p.done_bytes) || 0); };
        job.hidden = false; show(r);
        res = null;
        for(let lost = 0; root.isConnected;){
          await new Promise(ok => setTimeout(ok, 1000));
          let rr, j;
          try{ rr = await fetch('/api/media/fetch/' + encodeURIComponent(r.job)); j = await rr.json().catch(() => ({})); }
          catch(_){ if(++lost > 30){ toast(L('fetch_lost','Suivi du rapatriement perdu'), 'error'); break; } continue; }
          if(rr.status === 404){ toast(j.error || L('fetch_lost','Suivi du rapatriement perdu'), 'error'); break; }
          if(!rr.ok){ if(++lost > 30){ toast(j.error || 'HTTP ' + rr.status, 'error'); break; } continue; }
          lost = 0; show(j);
          if(j.state === 'failed'){ toast(L('fetch_failed','Rapatriement en échec : {e}').replace('{e}', j.error || ''), 'error'); break; }
          if(j.state === 'done'){ res = j; break; }
        }
        job.hidden = true;
        if(!res) return;
      }
      await loadAssets(); render();
      const path = res.path || res.local_path;
      if(then && path) then(path);
    }
    async function importExt(ext, then, nodeId){
      const r = await regCall('POST', '/api/media/import_share', nodeId != null ? {node_id:nodeId, path:ext} : {vmid, path:ext});
      if(!r) return null;
      toast(L('imp_start','Import dans la base : vérification puis copie de {n}…').replace('{n}', baseName(ext)), 'info');
      for(;;){
        await new Promise(res => setTimeout(res, 1500));
        const j = await (await fetch('/api/media/scan')).json().catch(() => ({}));
        if(j.running) continue;
        if(j.error){ toast(j.error, 'error'); return null; }
        await loadAssets(); if(st.tab === 'net') await loadNet(st.net.path); render();
        const res = j.result || {}, a = st.all.find(x => x.id === res.asset_id);
        if(o.onImported) o.onImported(res.local_path, a);
        if(then && res.local_path) then(res.local_path, a);
        return res;
      }
    }
    // Action `id` sur la ligne `r` (bouton, menu, double-clic, sélection).
    function actRow(id, r){
      if(id === '__info') return r.asset && openInfo(r.asset.id);
      if(id === '__unbin') return r.asset && unbin([r.asset.id]);
      if(id === '__import') return importExt(r.ext);
      if(r.ext) return importExt(r.ext, (p, a) => o.onAction && o.onAction(id, {asset:a, path:p, name:baseName(p)}));
      const def = actions.find(x => x.id === id);
      if(def && def.needsLocal !== false && !r.path && r.asset && !global)
        return fetchThen(r.asset.id, p => o.onAction && o.onAction(id, {asset:r.asset, path:p, name:r.name}));
      if(o.onAction) return o.onAction(id, {asset:r.asset || null, path:r.path || '', name:r.name});
    }
    const rowOf = el => rows()[+el.dataset.k] || {};
    main.onclick = e => {
      if(e.target.closest('.ml-col-rs') || Date.now() - _rsAt < 400) return;      // poignée de largeur : pas un tri
      const gh = e.target.closest('[data-grp]');
      if(gh) return selectGroup(+gh.dataset.grp, e);
      // Pellicule : une image ouvre la fiche Infos À CET INSTANT (même relais que le survol).
      const fr = e.target.closest('[data-sbt]'), fit = fr && fr.closest('[data-aid]');
      if(fr && fit && fit.dataset.aid && !(e.ctrlKey || e.metaKey || e.shiftKey)){
        _lastScrub = {aid:fit.dataset.aid, t:parseFloat(fr.dataset.sbt) || 0, at:Date.now()}; return openInfo(fit.dataset.aid); }
      if(e.target.closest('.ml-pv-side [data-px-create]') && pv.aid){ const id = pv.aid;
        createProxies([id]).then(r => { if(r && pv.aid === id) pvLoad(id); }); return; }
      const th = e.target.closest('th[data-sort]');
      if(th){ const k = th.dataset.sort; st.sort = {key:k, dir: st.sort.key === k ? -st.sort.dir : (k === 'name' ? 1 : -1)}; render(); return; }
      const d = e.target.closest('.ml-dir');
      if(d){ const n = d.dataset.dir; loadNet(n === '..' ? st.net.path.split('/').slice(0, -1).join('/') : (st.net.path ? st.net.path + '/' : '') + n); return; }
      const b = e.target.closest('[data-act]'), it = e.target.closest('[data-k]');
      if(b && it){
        if(b.disabled) return;
        if(b.dataset.act === '__menu'){ const r = rowOf(it); return openMenu(b, it.dataset.key && st.sel.has(it.dataset.key) && st.sel.size > 1 ? selMenuItems() : rowMenuItems(r)); }
        return actRow(b.dataset.act, rowOf(it));
      }
      if(it){ selectClick(it, e); if(oneTap && !(e.shiftKey || e.ctrlKey || e.metaKey)) runPrimary(it, e); return; }
      if(!e.target.closest('thead') && curMode() !== 'preview' && (e.target === main || e.target.closest('.ml-scroll,.ml-grid,.ml-cards,.ml-empty'))){ st.sel.clear(); paintSel(); }
    };
    // Action principale d'un média (double-clic, ou simple clic en mode « 1 clic »).
    function runPrimary(it, e){
      if(e.target.closest('input,button,video,audio,.ml-pv-side,[data-grp],.ml-col-rs')) return;
      const prim = actions.find(x => x.primary) || actions[0];
      if(!prim){
        // Hôte sans action propre (Gestionnaire, 2026-10-20) : il choisit (o.primary → {label, run}),
        // sinon — ou si son envoi n'a pas pu partir (run rend false) — la fiche Infos. Avant : rien.
        const r = rowOf(it); if(!r.asset) return;
        const hp = o.primary && o.primary();
        if(hp && hp.run(r) !== false) return;
        return openInfo(r.asset.id);
      }
      if(!it.dataset.path && !it.dataset.ext && prim.needsLocal !== false && (global || !it.dataset.aid)) return;
      actRow(prim.id, rowOf(it));
    }
    main.ondblclick = e => {
      const it = e.target.closest('[data-k]'); if(it && !oneTap) runPrimary(it, e);
    };
    // Mode « 1 clic » (2026-10-18) : sur iPad, le double toucher ZOOMAIT la page au lieu de passer le média au
    // preview. Un clic lance alors l'action principale ; la sélection multiple reste au clavier (Maj, Cmd/Ctrl).
    // Préférence par poste ; par défaut ACTIVE sur un appareil tactile, où le double-clic n'est pas un geste.
    // Bouton « Compatibles seulement » : seulement si l'hôte signale des médias (o.flag). Préférence par poste.
    {
      const b = $('.ml-compat');
      if(b && o.flag){
        st.compatOnly = !!pref.get('compatOnly', false);
        b.hidden = false;
        b.textContent = o.flagFilterLabel || L('compat_only', 'Compatibles seulement');
        b.title = o.flagFilterTip || L('compat_only_tip', 'N\'afficher que les médias sans avertissement de format');
        const paint = () => { b.setAttribute('aria-pressed', st.compatOnly ? 'true' : 'false'); b.classList.toggle('on', st.compatOnly); };
        paint();
        b.onclick = () => { st.compatOnly = !st.compatOnly; pref.set('compatOnly', st.compatOnly); paint(); render(); };
      }
    }
    // Gestionnaire (sans action propre) : préférence À PART et désactivée par défaut — son action
    // principale est la fiche Infos ou un ENVOI vers un conteneur, qu'un simple toucher ne doit pas
    // déclencher sans l'avoir demandé.
    const oneTapKey = actions.length ? 'oneTap' : 'oneTap.global';
    let oneTap = !!pref.get(oneTapKey, !!(actions.length && window.matchMedia && matchMedia('(any-pointer: coarse)').matches));
    {
      const b = $('.ml-onetap'), prim = actions.find(x => x.primary) || actions[0];
      const primLabel = () => prim ? prim.label : ((o.primary && o.primary()) || {label:L('info','Infos…')}).label;
      if(b && (prim || global)){
        b.hidden = false;
        const tip = () => { b.title = L('one_click_tip', 'Un seul clic sur un média : « {action} »').replace('{action}', primLabel()); };
        tip(); b.addEventListener('mouseenter', tip); b.addEventListener('focus', tip);
        const paint = () => { b.setAttribute('aria-pressed', oneTap ? 'true' : 'false'); b.classList.toggle('on', oneTap); };
        paint();
        b.onclick = () => { oneTap = !oneTap; pref.set(oneTapKey, oneTap); paint(); };
      }
    }
    // Clic droit : le menu de la ligne (ou de la sélection si la ligne en fait partie), au pointeur.
    // Il n'y en avait PAS : le composant commun n'avait jamais repris le menu contextuel de l'ancien
    // Gestionnaire — le navigateur ouvrait le sien.
    main.addEventListener('contextmenu', e => {
      // En-tête de la liste : le choix des colonnes.
      if(e.target.closest('thead') && st.tab !== 'net'){ e.preventDefault(); openFieldsMenu('cols', null, {x:e.clientX, y:e.clientY}); return; }
      const it = e.target.closest('[data-k]'); if(!it || e.target.closest('input,textarea,video,audio')) return;
      e.preventDefault();
      const key = it.dataset.key;
      if(key && !st.sel.has(key)){ st.sel.clear(); st.sel.add(key); st.anchor = key; paintSel(); }
      openMenu(null, key && st.sel.size > 1 ? selMenuItems() : rowMenuItems(rowOf(it)), {x:e.clientX, y:e.clientY});
    });
    let _rsAt = 0;
    // Largeur d'une colonne : glisser la poignée du bord droit de son en-tête ; double-clic = automatique.
    main.addEventListener('pointerdown', e => {
      const h = e.target.closest('.ml-col-rs'); if(!h || e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      const key = h.dataset.rs, th = h.closest('th'), x0 = e.clientX, w0 = th.getBoundingClientRect().width;
      let w = w0;
      const mv = ev => { w = clampInt(w0 + ev.clientX - x0, COLW_MIN, COLW_MAX); th.style.width = th.style.minWidth = w + 'px';
        const cw = th.querySelector('.ml-cw'); if(cw){ cw.classList.remove('is-auto'); cw.style.width = Math.max(0, w - 16) + 'px'; } };
      const up = () => { document.removeEventListener('pointermove', mv); document.removeEventListener('pointerup', up);
        _rsAt = Date.now();
        if(Math.abs(w - w0) >= 2){ vs.colW = {...vs.colW, [key]:clampInt(w - 16, COLW_MIN, COLW_MAX)}; saveView(); render(); } };
      document.addEventListener('pointermove', mv); document.addEventListener('pointerup', up);
    });
    main.addEventListener('dblclick', e => { const h = e.target.closest('.ml-col-rs'); if(!h) return;
      const w = {...vs.colW}; delete w[h.dataset.rs]; vs.colW = w; saveView(); render(); });
    root.addEventListener('keydown', e => {
      if(e.target.closest('input,textarea,select,dialog')) return;
      // En-tête de groupe : Entrée ou Espace le sélectionne, comme un clic.
      const gh = e.target.closest('[data-grp]');
      if(gh && (e.key === 'Enter' || e.key === ' ')){ e.preventDefault(); selectGroup(+gh.dataset.grp, e); return; }
      // Liste + aperçu : ↑ ↓ passent d'un média à l'autre, Espace lit / met en pause le proxy.
      if(curMode() === 'preview' && main.contains(e.target) && !e.altKey && !e.ctrlKey && !e.metaKey && menuEl.hidden){
        if((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !e.target.closest('video,audio')){ e.preventDefault(); pvMove(e.key === 'ArrowDown' ? 1 : -1); return; }
        if(e.key === ' ' && !e.target.closest('button,video,audio,a,[role="button"]') && pvToggle()){ e.preventDefault(); return; }
      }
      if((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A') && main.contains(document.activeElement)){ e.preventDefault(); selectAll(); }
      else if(e.key === 'Escape' && st.sel.size && menuEl.hidden){ st.sel.clear(); paintSel(); }
    });
    main.addEventListener('dragstart', e => {
      scrubStop();                                   // un glisser qui démarre annule le survol
      const it = e.target.closest('[data-k]'); if(!it) return;
      const key = it.dataset.key;
      const rs = key && st.sel.has(key) && st.sel.size > 1 ? selectedRows() : [rowOf(it)];
      const ids = rs.filter(r => r.asset).map(r => r.asset.id), paths = rs.map(r => r.path).filter(Boolean);
      const dt = e.dataTransfer;
      if(it.dataset.path) dt.setData('application/x-bobi-media', it.dataset.path);
      if(paths.length) dt.setData('application/x-bobi-media-list', JSON.stringify(paths));
      if(ids.length){ dt.setData('application/x-bobi-asset', it.dataset.aid || ids[0]); dt.setData('application/x-bobi-assets', JSON.stringify(ids)); }
      dt.setData('application/x-bobi-src', uid);
      dt.setData('text/plain', it.dataset.path || it.dataset.aid);
      dt.effectAllowed = 'copy';
      if(rs.length > 1){
        // Image de glisser : le nombre emporté, pas la seule ligne sous le pointeur.
        const g = document.createElement('div'); g.className = 'ml-drag-ghost';
        g.textContent = L('drag_n','{n} médias').replace('{n}', rs.length);
        document.body.appendChild(g); try{ dt.setDragImage(g, 12, 12); }catch(_){}
        setTimeout(() => g.remove(), 0);
      }
    });
    const dropIds = dt => { try{ const l = JSON.parse(dt.getData('application/x-bobi-assets') || '[]'); if(l.length) return l; }catch(_){}
      const one = dt.getData('application/x-bobi-asset'); return one ? [one] : []; };
    // Liste d'un panneau qui affiche un bin MANUEL : y déposer des médias les y range (glisser
    // d'un panneau du Gestionnaire à l'autre). Dépôt sur soi-même ignoré.
    const curManual = () => { const b = st.bins.find(x => x.id === st.bin); return st.tab === 'base' && b && !b.smart ? b : null; };
    main.addEventListener('dragover', e => {
      if(isFiles(e.dataTransfer) && o.upload !== false && st.tab === 'base'){ e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; main.classList.add('is-drop-files'); return; }
      if(!curManual() || !e.dataTransfer.types.includes('application/x-bobi-asset')) return;
      e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; main.classList.add('is-drop');
    });
    main.addEventListener('dragleave', e => { if(!main.contains(e.relatedTarget)) main.classList.remove('is-drop', 'is-drop-files'); });
    main.addEventListener('drop', e => {
      main.classList.remove('is-drop', 'is-drop-files');
      if(isFiles(e.dataTransfer)){ if(o.upload === false || st.tab !== 'base') return; e.preventDefault(); sendFiles(e.dataTransfer.files); return; }
      const b = curManual(); if(!b || e.dataTransfer.getData('application/x-bobi-src') === uid) return;
      const ids = dropIds(e.dataTransfer); if(!ids.length) return;
      e.preventDefault(); addToBin(b, ids);
    });

    // ── Survol-défilement (cf. en-tête ; logique pure en haut du fichier) ──
    // Survolable : une vidéo fermée qui a une PLANCHE d'images (instantané, aucune requête vidéo) ou un proxy.
    const canScrub = a => !!(a && (a.has_proxy || a.has_storyboard) && kindOf(a) === 'video' && !a.growing);
    const SCRUB_SETTLE_MS = 300;          // immobile aussi longtemps : l'image EXACTE vient du proxy, la planche cède
    const assetById = id => st.all.find(x => x.id === id) || st.assets.find(x => x.id === id) || null;
    let _arm = null, _sc = null;
    function scrubDisarm(){ if(!_arm) return; clearTimeout(_arm.timer); _arm.img.removeEventListener('pointerleave', _arm.leave);
      _arm.img.removeEventListener('pointermove', _arm.mv); _arm = null; }
    // Agrandissement de l'aperçu pendant le survol : la vignette reste à sa place, l'aperçu flotte
    // PAR-DESSUS, centré sur elle, SCRUB_ZOOM fois plus grand (demande du 2026-10-12 : « très petit »).
    // Il ne capte pas la souris (pointer-events:none) : clic, double-clic, clic droit et glisser
    // atteignent la ligne dessous comme avant ; la position est lue sur le document.
    const SCRUB_ZOOM = 1.8;
    function scrubStop(){
      scrubDisarm();
      if(!_sc) return;
      const s = _sc; _sc = null; if(_scrubActive === scrubStop) _scrubActive = null;
      s.dead = true; clearTimeout(s.idle); s.seeker.stop(); teardownVideo(s.v);
      document.removeEventListener('pointermove', s.move, true);
      window.removeEventListener('blur', scrubStop);
      if(s.wrap.parentNode) s.wrap.remove();
    }
    function scrubStart(img, x){
      const a = assetById((img.closest('[data-aid]') || {dataset:{}}).dataset.aid);
      if(!canScrub(a) || !img.isConnected) return;
      if(_scrubActive) _scrubActive();
      _scrubActive = scrubStop;
      const box = img.getBoundingClientRect();
      const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
      const [W, H] = scrubPopSize(box.width, box.height, SCRUB_ZOOM, vw, vh);
      const left = Math.max(4, Math.min(vw - W - 4, box.left + box.width / 2 - W / 2));
      const top = Math.max(4, Math.min(vh - H - 4, box.top + box.height / 2 - H / 2));
      const wrap = document.createElement('div');
      wrap.className = 'ml-scrub-pop';
      Object.assign(wrap.style, {left:left + 'px', top:top + 'px', width:W + 'px', height:H + 'px'});
      wrap.innerHTML = '<img alt="" src="' + esc(img.currentSrc || img.src) + '">'
        + '<div class="ml-scrub-sb" hidden></div>'
        + '<video muted playsinline preload="metadata" disablepictureinpicture disableremoteplayback tabindex="-1"></video>'
        + '<span class="ml-scrub-bar" aria-hidden="true"><i></i></span><span class="ml-scrub-t" aria-hidden="true"></span>';
      // Dans une fenêtre modale ouverte, l'aperçu doit y entrer (sinon il passe sous son voile).
      (img.closest('dialog[open]') || document.body).appendChild(wrap);
      const sbEl = wrap.querySelector('.ml-scrub-sb');
      const v = wrap.querySelector('video'), bar = wrap.querySelector('.ml-scrub-bar i'), lab = wrap.querySelector('.ml-scrub-t');
      v.muted = true;
      // En mode planche, la vidéo ne se montre qu'une fois le pointeur posé (`settled`) : un `seeked` en retard
      // d'une recherche du mouvement ne doit pas recouvrir la case courante par une image périmée.
      let settled = false;
      v.addEventListener('seeked', () => { if(mode !== 'sb' || settled) wrap.classList.add('is-ready'); });
      const seeker = createSeeker(v);
      const dur = () => (isFinite(v.duration) && v.duration > 0) ? v.duration : ((a.tech || {}).duration || 0);
      // HYBRIDE : planche d'images pendant le mouvement (la case change sans requête), proxy seulement quand
      // le pointeur s'arrête (SCRUB_SETTLE_MS) pour l'image exacte. Sans planche : proxy seul, comme avant.
      // Planche sans proxy : planche seule. `mode` : 'wait' (planche demandée) | 'sb' | 'video'.
      let mode = a.has_storyboard ? 'wait' : 'video', sbData = null, lastT = 0, wired = false;
      const wireVideo = () => { if(wired || !a.has_proxy) return; wired = true; v.src = '/api/media/assets/' + encodeURIComponent(a.id) + '/proxy'; };
      const showCell = t => {
        if(!sbData) return;
        const c = sbCell(sbData, sbIndexAt(sbData, t, dur())), url = sbData.url.replace(/["'()\\\s]/g, ch => '%' + ch.charCodeAt(0).toString(16).padStart(2, '0'));
        sbEl.style.backgroundImage = "url('" + url + "')";
        sbEl.style.backgroundSize = c.size[0] + '% ' + c.size[1] + '%';
        sbEl.style.backgroundPosition = c.pos[0].toFixed(3) + '% ' + c.pos[1].toFixed(3) + '%';
        sbEl.style.aspectRatio = (sbData.w > 0 && sbData.h > 0) ? sbData.w + ' / ' + sbData.h : '16 / 9';
        sbEl.hidden = false;
      };
      if(a.has_storyboard) loadSb(a).then(d => {
        if(!_sc || _sc.wrap !== wrap) return;
        if(d){ sbData = d; mode = 'sb'; showCell(lastT); } else { mode = 'video'; wireVideo(); seeker.want(lastT); }
      });
      const move = ev => {
        const r = wrap.getBoundingClientRect();
        // Sortie de l'aperçu agrandi (ou de la vignette, si elle déborde) : fin du survol.
        const inPop = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
        const inImg = ev.clientX >= box.left && ev.clientX <= box.right && ev.clientY >= box.top && ev.clientY <= box.bottom;
        if(ev.type === 'pointermove' && !inPop && !inImg) return scrubStop();
        const f = scrubFrac(ev.clientX, r.left, r.width), t = scrubTime(f, dur());
        bar.style.width = (f * 100).toFixed(2) + '%'; lab.textContent = fmtClock(t);
        lastT = t; _lastScrub = {aid:a.id, t, at:Date.now()};
        if(mode === 'video'){ seeker.want(t); return; }
        if(mode === 'sb'){
          showCell(t);
          settled = false; wrap.classList.remove('is-ready');
          if(_sc){ clearTimeout(_sc.idle);
            if(a.has_proxy) _sc.idle = setTimeout(() => { if(!_sc || _sc.wrap !== wrap) return; settled = true; wireVideo(); seeker.want(lastT); }, SCRUB_SETTLE_MS); }
        }
      };
      if(mode === 'video') wireVideo();
      document.addEventListener('pointermove', move, true);
      window.addEventListener('blur', scrubStop);
      _sc = {img, wrap, v, seeker, move, idle:null, dead:false};
      move({clientX:x, clientY:box.top + box.height / 2});
    }
    // Souris seulement (pas de survol au toucher), aucun bouton enfoncé (glisser, sélection en cours).
    main.addEventListener('pointerover', e => {
      if(e.pointerType !== 'mouse' || e.buttons) return;
      const img = e.target.closest && e.target.closest('img.ml-thumb[data-scrub]');
      if(!img || (_arm && _arm.img === img)) return;
      scrubDisarm();
      const arm = {img, x:e.clientX};
      arm.mv = ev => { arm.x = ev.clientX; };
      arm.leave = () => { if(_arm === arm) scrubDisarm(); };
      arm.timer = setTimeout(() => { if(_arm !== arm) return; scrubDisarm(); scrubStart(img, arm.x); }, 250);
      img.addEventListener('pointermove', arm.mv); img.addEventListener('pointerleave', arm.leave);
      _arm = arm;
    });
    main.addEventListener('pointerdown', e => { if(_arm && e.target === _arm.img) scrubDisarm(); });
    main.addEventListener('scroll', scrubStop, true);

    // Bins
    function askName(btn, ph, value){
      return new Promise(res => {
        const inp = document.createElement('input'); inp.placeholder = ph; inp.className = 'ml-ask'; inp.value = value || '';
        btn.hidden = true; btn.after(inp); inp.focus(); inp.select();
        let done = v => { done = () => {}; inp.remove(); btn.hidden = false; res(v); };
        inp.oninput = () => { inp.removeAttribute('aria-invalid'); inp.title = ''; };
      inp.onkeydown = e => { if(e.key === 'Enter') done(inp.value.trim() || null); else if(e.key === 'Escape') done(null); };
        inp.onblur = () => done(inp.value.trim() || null);
      });
    }
    const binsBox = $('.ml-bins-list');
    function renderBins(){
      // Bins PARTAGÉS avec l'utilisateur (racine d'un partage : son parent n'est pas à lui) : rangés
      // sous « Partagés avec moi », avec leurs sous-bins ; le nom du partageur en petit.
      const isSharedRoot = b => !!(b.shared && b.shared.root);
      const kids = pid => st.bins.filter(b => (b.parent_id || null) === pid && !isSharedRoot(b));
      const sharedTag = b => b.shared && b.shared.root ? `<span class="ml-hom ml-shared-by" title="${esc(L('sh_by_tip','Partagé avec vous par {u}').replace('{u}', b.shared.by || '?'))}">↗ ${esc(b.shared.by || '')}</span>` : '';
      const item = (b, d) => { const ch = kids(b.id), folded = st.folded.has(b.id);
        return `<button type="button" role="treeitem" class="ml-bin${st.bin === b.id ? ' on' : ''}" style="--d:${d}" data-bin="${b.id}" data-smart="${b.smart ? 1 : ''}" title="${esc(b.name)}"${ch.length ? ` aria-expanded="${!folded}"` : ''}>
          ${ch.length ? `<span class="ml-fold" data-fold="${b.id}" role="button" aria-label="${esc(folded ? L('bin_unfold','Déplier') : L('bin_fold','Replier'))}">${folded ? '▸' : '▾'}</span>` : '<span class="ml-fold-sp"></span>'}
          <span aria-hidden="true">${b.smart ? '⚡' : '▤'}</span><span class="nm">${esc(b.name)}</span>${sharedTag(b)}<span class="n">${b.count == null ? '' : b.count}</span>
          ${b.shared ? '' : `<span class="del" data-del="${b.id}" role="button" aria-label="${esc(L('bin_delete','Supprimer le bin'))}">×</span>`}</button>` + (folded ? '' : ch.map(k => item(k, d + 1)).join('')); };
      const shRoots = st.bins.filter(isSharedRoot), shAssets = (st.sharedMine && st.sharedMine.assets) || [];
      const shNode = shRoots.length || shAssets.length
        ? `<button type="button" role="treeitem" class="ml-bin ml-bin-shared${st.bin === 'shared' ? ' on' : ''}" style="--d:0" data-bin="shared" title="${esc(L('sh_mine_tip','Médias partagés un par un avec vous ; les bins partagés sont dessous'))}"><span class="ml-fold-sp"></span><span aria-hidden="true">↗</span><span class="nm">${esc(L('sh_mine','Partagés avec moi'))}</span><span class="n">${shAssets.length || ''}</span></button>`
          + shRoots.map(b => item(b, 1)).join('') : '';
      binsBox.innerHTML = `<button type="button" role="treeitem" class="ml-bin${st.bin === null ? ' on' : ''}" style="--d:0" data-bin=""><span class="ml-fold-sp"></span><span aria-hidden="true">◎</span><span class="nm">${esc(L('bin_all','Tous les médias'))}</span></button>` + kids(null).map(b => item(b, 0)).join('') + shNode;
      $('.ml-bins-opt').hidden = !st.bins.some(b => b.parent_id);
    }
    async function delBin(b){
      const okc = window.mxlConfirm ? await window.mxlConfirm({message:L('bin_del_confirm','Supprimer le bin « {n} » ? Les médias qu\'il contient ne sont pas supprimés.').replace('{n}', b.name)}) : false;
      if(!okc || !await regCall('DELETE', '/api/media/bins/' + b.id)) return;
      if(st.bin === b.id) st.bin = null;
      await loadBins(); await loadAssets(); render(); binsChanged();
    }
    binsBox.onclick = async e => {
      const fold = e.target.closest('[data-fold]');
      if(fold){
        e.stopPropagation(); const id = +fold.dataset.fold;
        st.folded.has(id) ? st.folded.delete(id) : st.folded.add(id); pref.set('bins.folded', [...st.folded]); renderBins(); return;
      }
      const del = e.target.closest('[data-del]');
      if(del){ e.stopPropagation(); const b = st.bins.find(x => x.id === +del.dataset.del); if(b) delBin(b); return; }
      const it = e.target.closest('.ml-bin'); if(!it) return;
      st.bin = it.dataset.bin === 'shared' ? 'shared' : it.dataset.bin ? +it.dataset.bin : null; st.sel.clear(); st.anchor = null;
      if(st.editBin){ st.editBin = null; if(!$('.ml-filters').hidden) renderFilters(); }   // on quitte l'édition de la recherche d'un bin
      renderBins(); await loadAssets(); render(); armBinTimer();
    };
    binsBox.addEventListener('keydown', e => {
      const it = e.target.closest('.ml-bin'); if(!it || !it.dataset.bin) return;
      const id = +it.dataset.bin, has = st.bins.some(b => b.parent_id === id);
      if(!has || !((e.key === 'ArrowLeft' && !st.folded.has(id)) || (e.key === 'ArrowRight' && st.folded.has(id)))) return;
      e.preventDefault(); e.key === 'ArrowLeft' ? st.folded.add(id) : st.folded.delete(id); pref.set('bins.folded', [...st.folded]); renderBins();
      const again = binsBox.querySelector(`[data-bin="${id}"]`); if(again) again.focus();
    });
    $('.ml-bin-kids').onchange = async e => { st.kids = e.target.checked; pref.set('bins.children', st.kids); if(st.bin != null){ await loadAssets(); render(); } };
    $('.ml-bin-new').onclick = async e => {
      const name = await askName(e.currentTarget, L('bin_new_name','Nom du nouveau bin')); if(!name) return;
      const cur = st.bins.find(b => b.id === st.bin);
      if(await regCall('POST', '/api/media/bins', {name, parent_id: cur && !cur.smart ? cur.id : null})){ await loadBins(); binsChanged(); }
    };
    // Clic droit sur un bin : copier tout son contenu (en local pour un conteneur, vers un nœud au
    // Gestionnaire), renommer, sous-bin, supprimer.
    binsBox.addEventListener('contextmenu', async e => {
      const it = e.target.closest('.ml-bin'); if(!it || !it.dataset.bin) return;
      e.preventDefault();
      const b = st.bins.find(x => x.id === +it.dataset.bin); if(!b) return;
      const items = [{header:b.name}];
      if(global){
        await loadSF();
        items.push({header:L('bin_copy_to','Copier tout le contenu vers le nœud…')},
          ...(st.sf.nodes || []).map(n => ({label:'→ ' + n.name, run:() => copyBin(b, {node_id:n.id}, n.name)})));
      } else items.push({label:L('bin_copy_local','Copier en local tout le contenu'), run:() => copyBin(b, {vmid})});
      // Bin PARTAGÉ avec moi : rien de ce qui appartient à son propriétaire (renommer, sous-bin,
      // supprimer, sa recherche, ses partages) ; proxys en « Modifier » seulement.
      if(b.shared){
        items[0] = {header:b.name + ' — ' + L('sh_by_tip','Partagé avec vous par {u}').replace('{u}', b.shared.by || '?')};
        if(b.shared.level === 'edit') items.push({label:L('px_bin','Créer les proxys du contenu'), run:() => createProxies(null, b.id)});
        openMenu(null, items, {x:e.clientX, y:e.clientY}); return;
      }
      items.push({label:L('px_bin','Créer les proxys du contenu'), run:() => createProxies(null, b.id)});
      if(canShare()) items.push({label:L('sh_share','Partager…'), run:() => openShare([{kind:'bin', id:b.id, name:b.name}])});
      if(b.smart) items.push({header:''}, {label:L('bin_edit_query','Modifier la recherche…'), run:() => editSmartBin(b)});
      items.push({header:''}, {label:L('bin_rename','Renommer…'), run: async () => {
        const name = await askName(it, L('bin_new_name','Nom du nouveau bin'), b.name); if(!name || name === b.name) return;
        if(await regCall('POST', '/api/media/bins', {id:b.id, name, parent_id:b.parent_id, query:b.query || null})){ await loadBins(); binsChanged(); } }});
      if(!b.smart) items.push({label:L('bin_sub','Nouveau sous-bin…'), run: async () => {
        const name = await askName(it, L('bin_new_name','Nom du nouveau bin')); if(!name) return;
        if(await regCall('POST', '/api/media/bins', {name, parent_id:b.id})){ st.folded.delete(b.id); pref.set('bins.folded', [...st.folded]); await loadBins(); binsChanged(); } }});
      items.push({label:L('bin_delete','Supprimer le bin') + '…', danger:true, run:() => delBin(b)});
      openMenu(null, items, {x:e.clientX, y:e.clientY});
    });
    // Double-clic sur un bin intelligent : sa recherche dans l'éditeur.
    binsBox.addEventListener('dblclick', e => { const it = e.target.closest('.ml-bin'); if(!it || !it.dataset.smart) return;
      const b = st.bins.find(x => x.id === +it.dataset.bin); if(b) editSmartBin(b); });
    // Copie de tout un bin (sous-bins compris), en tâche de fond, avancement affiché ici.
    async function copyBin(b, target, label){
      const r = await regCall('POST', `/api/media/bins/${b.id}/copy`, {...target, children:true}); if(!r) return;
      const job = $('.ml-job'), txt = $('.ml-job-txt'), bar = $('.ml-job-bar');
      const head = target.vmid != null ? L('bin_copy_local_run','Copie en local de « {b} »') : L('bin_copy_run','Copie de « {b} » vers {n}');
      const show = p => { txt.textContent = head.replace('{b}', b.name).replace('{n}', label || '') + ' — ' + (p ? `${p.done}/${p.total}${p.name ? ' · ' + p.name : ''}` : '…');
        bar.max = (p && p.total) || 1; bar.value = (p && p.done) || 0; };
      job.hidden = false; show({done:0, total:r.total || 0, name:''});
      const res = await waitJob(show);
      job.hidden = true;
      if(!res) return;
      const nerr = Object.keys(res.errors || {}).length;
      toast(L('bin_copy_done','{c} copié(s) et vérifié(s), {a} déjà présent(s)').replace('{c}', (res.copied || []).length).replace('{a}', (res.already || []).length)
        + (nerr ? ' — ' + L('bin_copy_err','{n} en échec : {l}').replace('{n}', nerr).replace('{l}', Object.keys(res.errors).slice(0, 3).join(', ')) : ''), nerr ? 'warning' : 'info');
      await loadAssets(); render();
    }
    binsBox.addEventListener('dragover', e => {
      const it = e.target.closest('.ml-bin');
      // Fichiers du bureau : sur un bin MANUEL (ou « Tous les médias »), ils y entrent après l'envoi.
      if(it && isFiles(e.dataTransfer) && o.upload !== false && !it.dataset.smart){
        e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; binsBox.querySelectorAll('.is-drop').forEach(x => x.classList.remove('is-drop')); it.classList.add('is-drop'); return; }
      if(!it || !it.dataset.bin || !e.dataTransfer.types.includes('application/x-bobi-asset')) return;
      e.preventDefault(); binsBox.querySelectorAll('.is-drop').forEach(x => x.classList.remove('is-drop')); it.classList.add('is-drop');
    });
    binsBox.addEventListener('dragleave', e => { const it = e.target.closest('.ml-bin'); if(it && !it.contains(e.relatedTarget)) it.classList.remove('is-drop'); });
    binsBox.addEventListener('drop', async e => {
      const it = e.target.closest('.ml-bin'); binsBox.querySelectorAll('.is-drop').forEach(x => x.classList.remove('is-drop'));
      if(it && isFiles(e.dataTransfer)){ e.preventDefault(); if(o.upload === false || it.dataset.smart) return;
        sendFiles(e.dataTransfer.files, it.dataset.bin ? +it.dataset.bin : -1); return; }
      const ids = dropIds(e.dataTransfer); if(!it || !it.dataset.bin || !ids.length) return;
      e.preventDefault();
      if(it.dataset.smart){ toast(L('bin_smart_drop','Bin intelligent : son contenu vient de sa recherche'), 'warning'); return; }
      const b = st.bins.find(x => x.id === +it.dataset.bin); if(b) addToBin(b, ids);
    });
    // Un bin INTELLIGENT affiché se réévalue toutes les 10 s (« créés dans la dernière heure »).
    let _binTimer = null;
    function armBinTimer(){
      clearInterval(_binTimer); _binTimer = null;
      const b = st.bins.find(x => x.id === st.bin);
      if(b && b.smart) _binTimer = setInterval(async () => { if(st.tab !== 'base' || !root.isConnected) return; await loadAssets(); render(); }, 10000);
    }

    // ── Recherche avancée ──
    // Une condition = une ligne [champ] [opérateur] [valeur] ; les valeurs proposées viennent de la
    // BASE (valeurs distinctes présentes, /api/media/search_values), les plus fréquentes d'abord.
    async function loadSF(){
      if(st.sf) return st.sf;
      try{ st.sf = await (await fetch('/api/media/search_fields')).json(); }catch(e){ st.sf = {fields:[], nodes:[], projects:[]}; }
      return st.sf;
    }
    async function loadVals(field){
      if(st.vals[field]) return st.vals[field];
      try{ st.vals[field] = ((await (await fetch('/api/media/search_values?field=' + encodeURIComponent(field))).json()).values) || []; }
      catch(e){ st.vals[field] = []; }
      return st.vals[field];
    }
    const fieldOf = key => ((st.sf && st.sf.fields) || []).find(x => x.key === key);
    const fieldLabel = f => !f ? '' : f.custom ? (f.label || f.key) : L('sf.' + f.key, f.key);
    const suggests = f => !!f && (SUGGEST.has(f.key) || (f.custom && (f.type === 'enum' || f.type === 'text')));
    const isRel = op => op === 'within' || op === 'not_within';
    const noVal = op => ['empty','not_empty','is_true','is_false'].includes(op);
    // Valeur lisible (listes, puces).
    function valLabel(field, v){
      if(v == null || v === '') return '';
      if(typeof v === 'object') return `${v.n} ${L('unit.' + v.unit, v.unit)}`;
      const known = (st.vals[field] || []).find(x => String(x.value) === String(v));
      if(known && known.label) return known.label;
      if(field === 'on_node'){ const n = ((st.sf && st.sf.nodes) || []).find(x => +x.id === +v); if(n) return n.name; }
      if(field === 'project'){ const p = ((st.sf && st.sf.projects) || []).find(x => +x.id === +v); if(p) return p.name;
        if(st.ctx && +st.ctx.project_id === +v && st.ctx.project_name) return st.ctx.project_name; }
      if(field === 'vcodec' || field === 'audio_codec') return codecName(v);
      if(field === 'kind') return L('kind.' + v, v);
      if(field === 'scan') return L('scan.' + v, v);
      if(field === 'fps') return String(Math.round(+v * 100) / 100);
      return String(v);
    }
    function valueInput(f, c, k){
      const v = c.value;
      if(noVal(c.op)) return '<span class="ml-flt-v"></span>';
      if(isRel(c.op)){
        const x = (v && typeof v === 'object') ? v : {n:1, unit:'hours'};
        return `<span class="ml-flt-v ml-flt-rel"><input type="number" min="0" step="1" data-part="n" value="${esc(x.n)}" aria-label="${esc(L('flt_n','Nombre'))}"><select data-part="unit">${['minutes','hours','days'].map(u => `<option value="${u}"${x.unit === u ? ' selected' : ''}>${esc(L('unit.' + u, u))}</option>`).join('')}</select></span>`;
      }
      const opt = (val, label, n) => `<option value="${esc(val)}"${String(val) === String(v) ? ' selected' : ''}>${esc(label)}${n != null ? ' (' + n + ')' : ''}</option>`;
      const sel = (opts) => `<select class="ml-flt-v" data-part="v" aria-label="${esc(L('flt_value','Valeur'))}"><option value=""></option>${opts}</select>`;
      if(f.key === 'kind') return sel(['video','audio','image'].map(x => opt(x, L('kind.' + x, x))).join(''));
      const vals = st.vals[f.key];
      if(f.type === 'node' || f.type === 'project' || f.type === 'share' || f.key === 'scan'){
        if(!vals) return `<span class="ml-flt-v ml-muted">${esc(L('flt_loading','…'))}</span>`;
        return sel(vals.map(x => opt(x.value, x.label || valLabel(f.key, x.value), x.count)).join(''));
      }
      if(f.type === 'enum' && (c.op === 'eq' || c.op === 'ne') && vals && vals.length)
        return sel(vals.map(x => opt(x.value, x.label || valLabel(f.key, x.value), x.count)).join(''));
      const ty = f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : 'text';
      const dl = suggests(f) && vals && vals.length ? `${uid}-dl-${k}` : '';
      return `<span class="ml-flt-v"><input type="${ty}" data-part="v"${ty === 'number' ? ' step="any"' : ''} value="${esc(v == null ? '' : v)}"${dl ? ` list="${dl}"` : ''} aria-label="${esc(L('flt_value','Valeur'))}">${
        dl ? `<datalist id="${dl}">${vals.map(x => `<option value="${esc(x.value)}">${esc((x.label || valLabel(f.key, x.value)) + ' (' + x.count + ')')}</option>`).join('')}</datalist>` : ''}</span>`;
    }
    function fieldSelect(cur){
      const fl = st.sf.fields || [], used = new Set();
      const og = (label, keys) => { const fs = keys.map(fieldOf).filter(Boolean); fs.forEach(f => used.add(f.key));
        return fs.length ? `<optgroup label="${esc(label)}">${fs.map(f => `<option value="${esc(f.key)}"${f.key === cur ? ' selected' : ''}>${esc(fieldLabel(f))}</option>`).join('')}</optgroup>` : ''; };
      let h = SF_GROUPS.map(([g, keys]) => og(L('sfg.' + g, g), keys)).join('');
      h += og(L('sfg.custom','Champs personnalisés'), fl.filter(f => f.custom).map(f => f.key));
      h += og(L('sfg.other','Autres'), fl.filter(f => !used.has(f.key)).map(f => f.key));
      return `<select data-part="field" aria-label="${esc(L('flt_field','Champ'))}">${h}</select>`;
    }
    // Éditeur : un groupe = {items:[critère | groupe], links:['and'|'or', …]} — UNE liaison réglable
    // entre deux éléments consécutifs. ET lie plus fort que OU (comme en SQL) : « A et B ou C et D »
    // = (A ET B) OU (C ET D). Un groupe EXPLICITE (« + Groupe », encadré) sert à ce que la précédence
    // n'exprime pas : A et (B ou C). Le moteur reçoit l'arbre {match, items} (toQuery) ; un arbre
    // relu (bin, setState) redevient des liaisons partout où c'est exprimable (fromTree).
    const MAX_DEPTH = 4, MAX_CONDS = 50;
    const isGroup = x => !!x && Array.isArray(x.items);
    const newGroup = () => ({items:[], links:[]});
    const groupAt = path => path === '' ? st.tree : path.split('.').reduce((g, i) => g.items[+i], st.tree);
    const parentOf = path => { const p = path.split('.'), i = +p.pop(); return {g:groupAt(p.join('.')), i}; };
    const childPath = (path, k) => path === '' ? String(k) : path + '.' + k;
    const countConds = g => g.items.reduce((n, x) => n + (isGroup(x) ? countConds(x) : 1), 0);
    // Retrait de l'élément i : des deux liaisons qui l'entouraient, il reste la plus FAIBLE (un OU
    // l'emporte) — « A ou X et B » sans X donne « A ou B », comme le moteur lit un critère neutre.
    function removeAt(g, i){
      g.items.splice(i, 1);
      if(i === 0) g.links.splice(0, 1);
      else if(i > g.links.length - 1) g.links.splice(i - 1, 1);
      else g.links.splice(i - 1, 2, g.links[i - 1] === 'or' || g.links[i] === 'or' ? 'or' : 'and');
    }
    // Conditions incomplètes et groupes vides retirés (le moteur ignorerait aussi les seconds).
    function cleanGroup(g){
      const out = {items:g.items.slice(), links:(g.links || []).slice(0, Math.max(0, g.items.length - 1))};
      while(out.links.length < out.items.length - 1) out.links.push('and');
      for(let i = out.items.length - 1; i >= 0; i--){
        const x = out.items[i];
        if(isGroup(x)){ const c = cleanGroup(x); if(c.items.length) out.items[i] = c; else removeAt(out, i); }
        else if(!(x && x.field && x.op)) removeAt(out, i);
        else out.items[i] = {field:x.field, op:x.op, value:x.value};
      }
      return out;
    }
    // Liaisons → arbre. Découpe sur les OU ; un tronçon de ≥ 2 éléments devient un groupe « tous ».
    function toQuery(g){
      g = cleanGroup(g);
      const conv = x => isGroup(x) ? unwrap(toQuery(x)) : x;
      const unwrap = n => n.items.length === 1 ? n.items[0] : n;
      const items = g.items.map(conv);
      if(!g.links.includes('or')) return {match:'all', items};
      if(!g.links.includes('and')) return {match:'any', items};
      const runs = [[items[0]]];
      g.links.forEach((l, k) => { if(l === 'or') runs.push([]); runs[runs.length - 1].push(items[k + 1]); });
      return {match:'any', items:runs.map(r => r.length > 1 ? {match:'all', items:r} : r[0])};
    }
    const treeDepth = n => isGroup(n) ? 1 + Math.max(0, ...n.items.map(treeDepth)) : 0;
    // Arbre → liaisons. Dans un « tous » : ses sous-groupes « tous » s'aplatissent, un « au moins un »
    // reste un groupe explicite. Dans un « au moins un » : un « tous » devient un tronçon de liaisons ET,
    // un « au moins un » s'aplatit.
    function fromTree(n){
      const g = newGroup();
      const push = (x, link) => { if(g.items.length) g.links.push(link); g.items.push(x); };
      const andRun = (items, link) => items.forEach((x, k) => {
        const l = k ? 'and' : link;
        if(!isGroup(x)) push({...x}, l);
        else if(x.match === 'any'){ const sub = fromTree(x); if(sub.items.length) push(Object.assign(sub, {grp:true}), l); }
        else { const before = g.items.length; andRun(x.items, l); if(g.items.length === before) return; }
      });
      if(n.match === 'any'){
        const flatAny = items => items.forEach(x => {
          if(!isGroup(x)) push({...x}, 'or');
          else if(x.match === 'any') flatAny(x.items);
          else andRun(x.items, 'or');
        });
        flatAny(n.items || []);
      } else andRun(n.items || [], 'and');
      return g;
    }
    const unwrap = n => n.items.length === 1 ? n.items[0] : n;
    const conjWord = m => L(m === 'any' || m === 'or' ? 'flt_or' : 'flt_and', m === 'any' || m === 'or' ? 'ou' : 'et');
    function condText(c){
      const f = fieldOf(c.field);
      return [f ? fieldLabel(f) : L('sf.' + c.field, c.field), L('op.' + c.op, c.op), noVal(c.op) ? '' : valLabel(c.field, c.value)].filter(Boolean).join(' ');
    }
    // Lecture en clair d'un ARBRE (ce que reçoit le moteur), parenthèses comprises :
    // « (Durée est 0:08 ET Codec vidéo est H.264) OU Projet est Démo ».
    function exprText(g, nested){
      const parts = g.items.map(x => isGroup(x) ? exprText(x, true) : (x.field && x.op ? condText(x) : '')).filter(Boolean);
      const txt = parts.join(' ' + conjWord(g.match).toLocaleUpperCase() + ' ');
      return nested && parts.length > 1 ? '(' + txt + ')' : txt;
    }
    // Ajout possible sans dépasser les bornes du moteur (profondeur de l'arbre PRODUIT, conditions).
    const PLACEHOLDER = {field:'name', op:'not_empty', value:''};
    function canAdd(path, group){
      if(countConds(st.tree) >= MAX_CONDS) return false;
      const c = JSON.parse(JSON.stringify(st.tree));
      const g = path === '' ? c : path.split('.').reduce((x, i) => x.items[+i], c);
      if(g.items.length) g.links.push(g.links.length ? g.links[g.links.length - 1] : 'and');
      g.items.push(group ? {items:[{...PLACEHOLDER}, {...PLACEHOLDER}], links:['or'], grp:true} : {...PLACEHOLDER});
      return treeDepth(toQuery(c)) <= MAX_DEPTH;
    }
    function groupHtml(g, path){
      if(!g.items.length)
        return `<div class="ml-flt-none">${esc(path === '' ? L('flt_none','Ajoutez une condition pour filtrer.') : L('flt_grp_empty','Groupe vide : il ne filtre rien.'))}</div>`;
      return g.items.map((c, k) => {
        const p = childPath(path, k), l = g.links[k - 1];
        // Liaison avec l'élément PRÉCÉDENT : un bouton par liaison, réglable seul.
        const conj = `<span class="ml-flt-conj">${k ? `<button type="button" class="ml-conj${l === 'or' ? ' or' : ''}" data-part="link" data-g="${path}" data-k="${k - 1}" title="${esc(L('flt_link','Basculer et / ou (et lie plus fort que ou)'))}">${esc(conjWord(l))}</button>` : esc(L('flt_if','si'))}</span>`;
        if(isGroup(c)){
          return `<div class="ml-flt-row ml-flt-grprow" data-path="${p}">${conj}<div class="ml-flt-grp">
            <div class="ml-flt-head"><span>${esc(L('flt_grp','Groupe'))}</span><span class="ml-sp"></span>
              <button type="button" class="btn x" data-part="delgrp" aria-label="${esc(L('flt_del_grp','Retirer le groupe'))}" title="${esc(L('flt_del_grp','Retirer le groupe'))}">×</button></div>
            <div class="ml-flt-rows">${groupHtml(c, p)}</div>
            <div class="ml-flt-acts"><button type="button" class="btn" data-part="addc"${canAdd(p, false) ? '' : ' disabled'}>${esc(L('flt_add','+ Condition'))}</button>${
              canAdd(p, true) ? `<button type="button" class="btn" data-part="addg">${esc(L('flt_add_grp','+ Groupe'))}</button>` : ''}</div></div></div>`;
        }
        const f = fieldOf(c.field) || (st.sf.fields || [])[0]; if(!f) return '';
        return `<div class="ml-flt-row" data-path="${p}">${conj}
          ${fieldSelect(f.key)}
          <select data-part="op" aria-label="${esc(L('flt_op','Opérateur'))}">${f.ops.map(op => `<option value="${op}"${op === c.op ? ' selected' : ''}>${esc(L('op.' + op, op))}</option>`).join('')}</select>
          ${valueInput(f, c, p.replace(/\./g, '-'))}<button type="button" class="btn x" data-part="del" aria-label="${esc(L('flt_del','Retirer la condition'))}" title="${esc(L('flt_del','Retirer la condition'))}">×</button></div>`; }).join('');
    }
    function renderFilters(){
      $('.ml-flt-rows').innerHTML = groupHtml(st.tree, '');
      $('.ml-flt-add').disabled = !canAdd('', false); $('.ml-flt-addg').disabled = !canAdd('', true);
      $('.ml-flt-save').textContent = st.editBin ? L('flt_update','Mettre à jour « {n} »').replace('{n}', st.editBin.name) : L('flt_save','Enregistrer comme bin intelligent…');
      renderRead();
      // Valeurs proposées : chargées à la demande, puis l'éditeur est redessiné.
      const walk = g => g.items.forEach(c => { if(isGroup(c)) return walk(c); const f = fieldOf(c.field);
        if(f && !st.vals[f.key] && (suggests(f) || f.type === 'node' || f.type === 'project' || f.type === 'share'))
          loadVals(f.key).then(() => { if(!$('.ml-filters').hidden) renderFilters(); }); });
      walk(st.tree);
    }
    function renderRead(){
      const el = $('.ml-flt-read'), txt = st.sf ? exprText(toQuery(st.tree), false) : '';
      el.hidden = !txt; el.textContent = txt ? L('flt_read','Recherche :') + ' ' + txt : '';
    }
    // Puces des critères de premier niveau, toujours visibles (même panneau fermé) et retirables une à
    // une — dont celles posées d'office (projet du conteneur, producteur du recorder). Un groupe = une puce.
    function renderChips(){
      const box = $('.ml-chips'), n = countConds(st.tree), on = st.tab === 'base' && st.tree.items.length > 0;
      box.hidden = !on;
      $('.ml-filters-n').textContent = n ? ' (' + n + ')' : '';
      $('.ml-filters-btn').classList.toggle('on', n > 0);
      if(!$('.ml-filters').hidden) renderRead();
      if(!on) return;
      box.innerHTML = st.tree.items.map((c, k) => {
        const txt = isGroup(c) ? exprText(unwrap(toQuery(c)), true) || L('flt_grp_empty','Groupe vide : il ne filtre rien.') : condText(c);
        const del = isGroup(c) ? L('flt_del_grp','Retirer le groupe') : L('flt_del','Retirer la condition');
        return (k ? `<span class="ml-chip-conj">${esc(conjWord(st.tree.links[k - 1]))}</span>` : '') +
          `<span class="ml-chip${isGroup(c) ? ' ml-chip-grp' : ''}">${esc(txt)}<button type="button" data-chip="${k}" aria-label="${esc(del)}" title="${esc(del)}">×</button></span>`; }).join('');
    }
    $('.ml-chips').onclick = e => { const b = e.target.closest('[data-chip]'); if(!b) return;
      removeAt(st.tree, +b.dataset.chip); if(!$('.ml-filters').hidden) renderFilters(); filtersChanged(); };
    let _fT = null;
    function filtersChanged(){ clearTimeout(_fT); renderChips(); _fT = setTimeout(async () => { await loadAssets(); render(); }, 350); }
    function showFilters(on){ const box = $('.ml-filters'); box.hidden = !on; $('.ml-filters-btn').setAttribute('aria-expanded', on); }
    $('.ml-filters-btn').onclick = async () => {
      showFilters($('.ml-filters').hidden);
      if(!$('.ml-filters').hidden){ await loadSF(); if(!st.tree.items.length) addCond(''); else renderFilters(); }
    };
    function defaultOp(f){ return suggests(f) && f.ops.includes('eq') ? 'eq' : f.ops[0]; }
    function newCond(){
      const f = fieldOf('created_at') || (st.sf.fields || [])[0]; if(!f) return null;
      const op = defaultOp(f);
      return {field:f.key, op, value: isRel(op) ? {n:1, unit:'hours'} : ''};
    }
    function focusField(path){ const el = $(`.ml-flt-rows [data-path="${path}"] [data-part="field"]`); if(el) el.focus(); }
    // Un élément ajouté reprend la liaison précédente du groupe (ET pour le deuxième).
    function append(g, x){ if(g.items.length) g.links.push(g.links.length ? g.links[g.links.length - 1] : 'and'); g.items.push(x); }
    function addCond(path){
      const g = groupAt(path), c = newCond(); if(!c || !canAdd(path, false)) return;
      append(g, c); renderFilters(); filtersChanged(); focusField(childPath(path, g.items.length - 1));
    }
    function addGroup(path){
      const g = groupAt(path), c = newCond(); if(!c || !canAdd(path, true)) return;
      append(g, {items:[c], links:[], grp:true});
      renderFilters(); filtersChanged(); focusField(childPath(childPath(path, g.items.length - 1), 0));
    }
    $('.ml-flt-add').onclick = async () => { await loadSF(); addCond(''); };
    $('.ml-flt-addg').onclick = async () => { await loadSF(); addGroup(''); };
    const fltRows = $('.ml-flt-rows');
    fltRows.addEventListener('change', e => {
      const row = e.target.closest('[data-path]'); if(!row) return;
      const part = e.target.dataset.part, c = groupAt(row.dataset.path);
      if(part === 'field'){ const f = fieldOf(e.target.value); c.field = f.key; c.op = defaultOp(f); c.value = isRel(c.op) ? {n:1, unit:'hours'} : ''; renderFilters(); }
      else if(part === 'op'){ c.op = e.target.value; if(isRel(c.op) && typeof c.value !== 'object') c.value = {n:1, unit:'hours'};
        else if(!isRel(c.op) && typeof c.value === 'object') c.value = ''; renderFilters(); }
      else if(part === 'n' || part === 'unit') c.value = Object.assign({n:1, unit:'hours'}, c.value || {}, {[part]: part === 'n' ? +e.target.value : e.target.value});
      else if(part === 'v') c.value = e.target.value;
      else return;
      filtersChanged();
    });
    fltRows.addEventListener('input', e => {
      if(e.target.dataset.part !== 'v' || e.target.tagName !== 'INPUT') return;
      groupAt(e.target.closest('[data-path]').dataset.path).value = e.target.value; filtersChanged();
    });
    fltRows.addEventListener('click', e => {
      const p = e.target.closest('[data-part]'); if(!p) return;
      const row = p.closest('[data-path]'), path = row ? row.dataset.path : '';
      if(p.dataset.part === 'del' || p.dataset.part === 'delgrp'){ const {g, i} = parentOf(path); removeAt(g, i); renderFilters(); filtersChanged(); }
      else if(p.dataset.part === 'link'){ const g = groupAt(p.dataset.g), k = +p.dataset.k; g.links[k] = g.links[k] === 'or' ? 'and' : 'or'; renderFilters(); filtersChanged(); }
      else if(p.dataset.part === 'addc') addCond(path);
      else if(p.dataset.part === 'addg') addGroup(path);
    });
    $('.ml-flt-clear').onclick = () => { st.tree = newGroup(); st.editBin = null; renderFilters(); filtersChanged(); };
    // Requête enregistrée (nouveau format {match, items} OU ancien {all, any}) → arbre, même
    // conversion que media_registry.query_tree ; puis liaisons de l'éditeur (fromTree).
    function toTree(q){
      q = q || {};
      const items = [];
      if(q.items || q.match) items.push({match:q.match || 'all', items:q.items || []});
      items.push(...(q.all || []));
      if((q.any || []).length) items.push({match:'any', items:q.any});
      return sanitizeTree(items.length === 1 && isGroup(items[0]) ? items[0] : {match:'all', items}, 1) || {match:'all', items:[]};
    }
    // Recherche d'un bin intelligent rechargée dans l'éditeur ; « Enregistrer » met alors CE bin à jour.
    async function editSmartBin(b){
      await loadSF();
      st.tree = fromTree(toTree(b.query)); st.q = (b.query && b.query.q) || ''; $('.ml-search').value = st.q;
      st.editBin = {id:b.id, name:b.name, parent_id:b.parent_id};
      st.bin = null; st.sel.clear(); renderBins(); armBinTimer();
      showFilters(true); renderFilters(); filtersChanged();
    }
    // État d'un panneau, petit et sérialisable : le Gestionnaire l'enregistre dans ses dispositions
    // (≤ 2 Ko par panneau côté serveur) et le réapplique. setState VALIDE ce qu'il lit (onglet connu,
    // bin entier, texte borné, arbre aux bornes du moteur) et ignore le reste. L'arbre y est au
    // format du moteur ({match, items}), pas celui de l'éditeur.
    const STATE_MAX = 1800;
    function sanitizeTree(t, depth){
      if(!t || typeof t !== 'object' || !Array.isArray(t.items) || depth > MAX_DEPTH) return null;
      const items = [];
      for(const x of t.items){
        if(isGroup(x)){ const g = sanitizeTree(x, depth + 1); if(g) items.push(g); }
        else if(x && typeof x === 'object' && typeof x.field === 'string' && typeof x.op === 'string'){
          const v = x.value;
          if(v == null || ['string','number','boolean'].includes(typeof v)) items.push({field:x.field, op:x.op, value:v});
          else if(typeof v === 'object' && !Array.isArray(v)) items.push({field:x.field, op:x.op, value:{n:+v.n || 0, unit:['minutes','hours','days'].includes(v.unit) ? v.unit : 'hours'}});
        }
      }
      return {match: t.match === 'any' ? 'any' : 'all', items};
    }
    function getState(){
      const t = toQuery(st.tree);
      const out = {tab:st.tab, bin:st.bin, q:st.q || '', tree: t.items.length ? t : null, view, group:vs.group, cell:vs.cell};
      if(vs.cols) out.cols = vs.cols.slice();
      if(vs.cardF) out.cardF = vs.cardF.slice();
      if(Object.keys(vs.colW).length) out.colW = {...vs.colW};
      // Trop gros pour une disposition : on lâche d'abord les largeurs, puis la recherche avancée.
      if(JSON.stringify(out).length > STATE_MAX) delete out.colW;
      if(JSON.stringify(out).length > STATE_MAX) out.tree = null;
      return out;
    }
    async function setState(s){
      if(!s || typeof s !== 'object') return;
      const tabs = ['base', 'net', ...(o.extraTabs || []).map(t => t.id)];
      const vp = sanitizeViewState(s);
      if('cols' in s && s.cols === null) vp.cols = null;
      if('cardF' in s && s.cardF === null) vp.cardF = null;
      if(vp.view && vp.view !== view) pvStop(true);
      applyViewState(vp);
      if(typeof s.q === 'string'){ st.q = s.q.slice(0, 200).trim().toLowerCase(); $('.ml-search').value = st.q; }
      if('bin' in s) st.bin = Number.isInteger(s.bin) && s.bin > 0 ? s.bin : null;
      if('tree' in s){ const t = s.tree == null ? null : sanitizeTree(s.tree, 1);
        if(s.tree == null || t){ st.tree = t ? fromTree(t) : newGroup(); if(countConds(st.tree) > MAX_CONDS) st.tree = newGroup(); } }
      st.editBin = null; st.sel.clear(); st.anchor = null;
      renderBins(); armBinTimer();
      if(!$('.ml-filters').hidden){ await loadSF(); renderFilters(); }
      if(st.tree.items.length) loadSF().then(() => renderChips());
      await loadAssets();
      if(typeof s.tab === 'string' && tabs.includes(s.tab) && s.tab !== st.tab) setTab(s.tab); else render();
    }
    $('.ml-flt-save').onclick = async e => {
      if(!countConds(st.tree) && !st.q){ toast(L('flt_none','Ajoutez une condition pour filtrer.'), 'warning'); return; }
      const eb = st.editBin;
      const name = eb ? eb.name : await askName(e.currentTarget, L('flt_save_name','Nom du bin intelligent')); if(!name) return;
      const q = query(); delete q.kinds;
      const r = await regCall('POST', '/api/media/bins', eb ? {id:eb.id, name, parent_id:eb.parent_id, query:q} : {name, query:q}); if(!r) return;
      st.tree = newGroup(); st.editBin = null; st.q = ''; $('.ml-search').value = ''; st.bin = r.id;
      showFilters(false);
      filtersChanged(); await loadBins(); armBinTimer(); binsChanged();
    };

    // ── Fiche « Infos » + choix de vignette ──
    const dlgHost = document.createElement('div'); root.appendChild(dlgHost);
    dlgHost.innerHTML = `
      <dialog class="ml-dlg ml-info">
        <div class="ml-if-top"><img class="ml-if-thumb" alt="" hidden>
          <div class="ml-if-title"><div class="ml-if-name"></div><div class="ml-if-tabs" role="tablist">
            ${['info','meta','versions','where','proc','thumb','share'].map(t => `<button type="button" role="tab" class="ml-if-tab" data-t="${t}">${esc(L('if_tab_' + t, t))}</button>`).join('')}
          </div></div>
          <button type="button" class="btn ml-if-close">${esc(L('close','Fermer'))}</button></div>
        <div class="ml-if-pane" data-t="info"><div class="ml-if-media"></div><dl class="ml-facts"></dl></div>
        <div class="ml-if-pane" data-t="meta" hidden><div class="ml-if-form"></div>
          <div class="ml-if-acts"><span class="ml-sp"></span><button type="button" class="btn btn-green ml-if-save">${esc(L('save','Enregistrer'))}</button></div></div>
        <div class="ml-if-pane" data-t="versions" hidden><p class="ml-muted ml-if-vhelp">${esc(L('ver_help','Toutes les essences de ce média sous une seule fiche : l\'original et ses versions (transcodages, livraisons). Charger le média charge sa version par défaut.'))}</p>
          <div class="ml-scroll"><table class="ml-if-table ml-if-vtable"><thead><tr><th>${esc(L('ver_col_version','Version'))}</th><th>${esc(L('ver_col_format','Format'))}</th>
            <th>${esc(L('col_size','Taille'))}</th><th>${esc(L('ver_col_nodes','Nœuds'))}</th><th>${esc(L('ver_col_default','Par défaut'))}</th><th></th></tr></thead>
            <tbody class="ml-if-versions"></tbody></table></div></div>
        <div class="ml-if-pane" data-t="where" hidden><div class="ml-if-projs"></div><div class="ml-scroll"><table class="ml-if-table"><tbody class="ml-if-copies"></tbody></table></div>
          <div class="ml-if-pxh">${esc(L('px_title','Proxy de lecture'))}</div><div class="ml-scroll"><table class="ml-if-table"><tbody class="ml-if-proxies"></tbody></table></div>
          <div class="ml-if-acts"><span class="ml-sp"></span><button type="button" class="btn ml-if-delall">${esc(L('del_all','Supprimer le média (toutes les copies)…'))}</button></div></div>
        <div class="ml-if-pane" data-t="proc" hidden><div class="ml-if-lineage"></div><dl class="ml-facts ml-proc-facts"></dl>
          <details class="ml-if-cmd" hidden><summary>${esc(L('proc_cmd','Commande ffmpeg'))}</summary><pre></pre></details></div>
        <div class="ml-if-pane" data-t="thumb" hidden><img class="ml-if-thumbbig" alt="" hidden><p class="ml-if-thumbnote"></p>
          <div class="ml-if-acts"><button type="button" class="btn ml-if-thumbreset">${esc(L('thumb_reset','Par défaut'))}</button><span class="ml-sp"></span>
            <button type="button" class="btn btn-green ml-if-thumbbtn">${esc(L('thumb_pick','Choisir l\'image…'))}</button></div></div>
        <div class="ml-if-pane" data-t="share" hidden><div class="ml-if-shares"></div></div>
      </dialog>
      <dialog class="ml-dlg ml-shdlg">
        <div class="ml-if-top"><div class="ml-if-title"><div class="ml-if-name ml-sh-name"></div><div class="ml-muted ml-sh-sub"></div></div>
          <button type="button" class="btn ml-sh-close">${esc(L('close','Fermer'))}</button></div>
        <div class="ml-sh-body">
          <div class="ml-sh-picked"></div>
          <input type="search" class="ml-sh-q" placeholder="${esc(L('sh_search','Chercher un utilisateur, un groupe, un projet…'))}" aria-label="${esc(L('sh_search','Chercher un utilisateur, un groupe, un projet…'))}">
          <div class="ml-sh-res"></div>
          <div class="ml-sh-row"><label>${esc(L('sh_level','Niveau'))} <select class="ml-sh-level">
              <option value="use">${esc(L('sh_level_use','Consulter et utiliser'))}</option><option value="edit">${esc(L('sh_level_edit','Modifier'))}</option></select></label>
            <label>${esc(L('sh_until','Jusqu\'au (facultatif)'))} <input type="date" class="ml-sh-exp"></label></div>
          <p class="ml-muted ml-sh-help"></p>
          <div class="ml-if-acts"><span class="ml-sp"></span><button type="button" class="btn btn-green ml-sh-go" disabled>${esc(L('sh_go','Partager'))}</button></div>
          <div class="ml-sh-cur"></div>
        </div>
      </dialog>
      <dialog class="ml-dlg ml-scdlg">
        <div class="ml-if-top"><div class="ml-if-title"><div class="ml-if-name ml-sc-name"></div><div class="ml-sc-where ml-muted"></div></div>
          <button type="button" class="btn ml-sc-copy">${esc(L('sc_copy','Copier'))}</button>
          <button type="button" class="btn ml-sc-close">${esc(L('close','Fermer'))}</button></div>
        <pre class="ml-sc-pre" tabindex="0"></pre>
      </dialog>
      <dialog class="ml-dlg ml-thumbdlg">
        <div class="ml-if-top"><div class="ml-if-name ml-th-name"></div></div>
        <div class="ml-th-grid"><figure><img class="ml-th-img" alt=""><figcaption>${esc(L('thumb_new','Nouvelle'))}</figcaption></figure>
          <figure><img class="ml-th-cur" alt=""><figcaption>${esc(L('thumb_current','Actuelle'))}</figcaption></figure></div>
        <input type="range" class="ml-th-range" min="0" value="0" step="0.04">
        <div class="ml-th-ctl"><button type="button" class="btn" data-step="-1" data-unit="s">−1 s</button><button type="button" class="btn" data-step="-1" data-unit="f">◀|</button>
          <span class="ml-th-tc">00:00:00</span><button type="button" class="btn" data-step="1" data-unit="f">|▶</button><button type="button" class="btn" data-step="1" data-unit="s">+1 s</button>
          <button type="button" class="btn ml-th-pos">${esc(L('thumb_from_pos','Image à la position de lecture'))}</button></div>
        <div class="ml-if-acts"><span class="ml-sp"></span><button type="button" class="btn ml-th-cancel">${esc(L('close','Fermer'))}</button>
          <button type="button" class="btn btn-green ml-th-save">${esc(L('thumb_save','Utiliser cette image'))}</button></div>
      </dialog>`;
    const infoDlg = dlgHost.querySelector('.ml-info'), thDlg = dlgHost.querySelector('.ml-thumbdlg'), scDlg = dlgHost.querySelector('.ml-scdlg');
    const shDlg = dlgHost.querySelector('.ml-shdlg');

    // ── Partager un média, une sélection ou un bin (app/media_sharing.py) ──
    // Destinataires : utilisateurs, groupes, membres d'un projet. Deux niveaux ; date de fin
    // facultative. Pour UNE cible, la liste de ses partages (niveau modifiable, retrait immédiat).
    let _sh = {targets:[], rcpt:null, picked:[]};
    const shKind = k => L('sh_kind_' + k, k);
    const shLevel = l => l === 'edit' ? L('sh_level_edit','Modifier') : L('sh_level_use','Consulter et utiliser');
    async function renderShareList(box, kind, id, after){
      let d;
      try{ const r = await fetch(`/api/media/shares?kind=${kind}&id=${encodeURIComponent(id)}`); d = await r.json(); if(!r.ok) throw new Error(d.error || r.status); }
      catch(e){ box.innerHTML = ''; return null; }
      const rows = d.shares || [];
      box.innerHTML = `<div class="ml-if-pxh">${esc(L('sh_current','Partages en cours'))}</div>` + (rows.length ? `<div class="ml-scroll"><table class="ml-if-table"><tbody>${rows.map(x => `<tr class="${x.inert ? 'is-off' : ''}">
          <td>${esc(shKind(x.recipient_kind))}</td><td>${esc(x.recipient_name || L('sh_gone','(supprimé)'))}</td>
          <td><select data-sh-level="${x.id}" ${d.can_manage ? '' : 'disabled'}><option value="use" ${x.level === 'use' ? 'selected' : ''}>${esc(shLevel('use'))}</option><option value="edit" ${x.level === 'edit' ? 'selected' : ''}>${esc(shLevel('edit'))}</option></select></td>
          <td>${esc(x.expires_at ? L('sh_until_d','jusqu\'au {d}').replace('{d}', x.expires_at.slice(0, 10)) : L('sh_no_end','sans fin'))}</td>
          <td class="ml-muted">${esc(L('sh_by','par {u}').replace('{u}', x.sharer_name || '?'))}</td>
          <td>${d.can_manage ? `<button type="button" class="btn" data-sh-del="${x.id}">${esc(L('sh_revoke','Retirer'))}</button>` : ''}</td></tr>`).join('')}</tbody></table></div>`
        : `<p class="ml-muted">${esc(L('sh_none','Partagé avec personne.'))}</p>`)
        + ((d.log || []).length ? `<details class="ml-sh-log"><summary>${esc(L('sh_log','Journal'))}</summary><ul>${d.log.map(e => `<li>${esc((e.at || '').replace('T', ' '))} — ${esc(e.actor || L('sh_system','système'))} · ${esc(L('sh_act_' + e.action, e.action))} · ${esc(e.recipient_name || '')}${e.level ? ' (' + esc(shLevel(e.level)) + ')' : ''}</li>`).join('')}</ul></details>` : '');
      box.onchange = async ev => { const sel = ev.target.closest('[data-sh-level]'); if(!sel) return;
        if(await regCall('PATCH', '/api/media/shares/' + sel.dataset.shLevel, {level:sel.value})){ toast(L('sh_saved','Partage modifié'), 'info'); renderShareList(box, kind, id, after); } };
      box.onclick = async ev => { const b = ev.target.closest('[data-sh-del]'); if(!b) return;
        if(await regCall('DELETE', '/api/media/shares/' + b.dataset.shDel)){ toast(L('sh_revoked','Partage retiré'), 'info'); renderShareList(box, kind, id, after); if(after) after(); } };
      return d;
    }
    function shRender(){
      const q = (shDlg.querySelector('.ml-sh-q').value || '').trim().toLowerCase(), R = _sh.rcpt || {};
      const key = x => x.kind + ':' + x.id, picked = new Set(_sh.picked.map(key));
      const all = [...(R.groups || []), ...(R.users || []), ...(R.projects || [])].filter(x => !picked.has(key(x)) && (!q || x.label.toLowerCase().includes(q)));
      shDlg.querySelector('.ml-sh-res').innerHTML = all.slice(0, 30).map(x => `<button type="button" class="ml-chip ml-sh-cand" data-k="${esc(key(x))}"><span class="ml-muted">${esc(shKind(x.kind))}</span> ${esc(x.label)}${x.members != null ? ` <span class="ml-muted">(${x.members})</span>` : ''}</button>`).join('')
        || (R.users ? `<span class="ml-muted">${esc(L('sh_no_match','Aucun destinataire ne correspond.'))}</span>` : '');
      shDlg.querySelector('.ml-sh-picked').innerHTML = _sh.picked.length ? _sh.picked.map(x => `<span class="ml-chip"><span class="ml-muted">${esc(shKind(x.kind))}</span> ${esc(x.label)}<button type="button" data-unpick="${esc(key(x))}" aria-label="${esc(L('sh_unpick','Retirer ce destinataire'))}">×</button></span>`).join('')
        : `<span class="ml-muted">${esc(L('sh_pick','Choisissez au moins un destinataire.'))}</span>`;
      shDlg.querySelector('.ml-sh-go').disabled = !_sh.picked.length;
      shDlg.querySelector('.ml-sh-help').textContent = shDlg.querySelector('.ml-sh-level').value === 'edit'
        ? L('sh_help_edit','En plus : modifier la fiche (métadonnées, vignette, proxy) et, pour un bin, y ranger ou en retirer des médias. Jamais supprimer le média ni ses fichiers.')
        : L('sh_help_use','Voir, lire, le proxy, charger dans ses conteneurs, rapatrier, mettre en playlist.');
    }
    // « Partager… » n'est proposé qu'à qui détient `media.manage` (le serveur refuse déjà les autres).
    // `hasPerm` vient de layout.html ; absent (page sans layout), on ne masque rien : le serveur tranche.
    function canShare(){ return typeof window.hasPerm !== 'function' || window.hasPerm('media.manage'); }
    async function openShare(targets){
      _sh = {targets, rcpt:_sh.rcpt, picked:[]};
      shDlg.querySelector('.ml-sh-name').textContent = targets.length === 1 ? targets[0].name : L('sh_n_items','{n} médias').replace('{n}', targets.length);
      shDlg.querySelector('.ml-sh-sub').textContent = targets.length === 1 && targets[0].kind === 'bin' ? L('sh_bin_live','Bin partagé vivant : ses sous-bins et ce qui y entrera ensuite, dans la limite de ce que vous voyez.') : '';
      shDlg.querySelector('.ml-sh-q').value = ''; shDlg.querySelector('.ml-sh-exp').value = ''; shDlg.querySelector('.ml-sh-level').value = 'use';
      const cur = shDlg.querySelector('.ml-sh-cur'); cur.innerHTML = '';
      shRender();
      if(!shDlg.open) shDlg.showModal();
      if(!_sh.rcpt){ try{ const r = await fetch('/api/media/shares/recipients'); if(r.ok) _sh.rcpt = await r.json(); else toast(L('forbidden','Droit « gestion des médias » requis'), 'error'); }catch(e){} shRender(); }
      if(targets.length === 1) renderShareList(cur, targets[0].kind, targets[0].id);
    }
    shDlg.querySelector('.ml-sh-close').onclick = () => shDlg.close();
    shDlg.querySelector('.ml-sh-q').oninput = shRender;
    shDlg.querySelector('.ml-sh-level').onchange = shRender;
    shDlg.querySelector('.ml-sh-body').addEventListener('click', ev => {
      const c = ev.target.closest('.ml-sh-cand'), u = ev.target.closest('[data-unpick]'), R = _sh.rcpt || {};
      if(c){ const x = [...(R.groups || []), ...(R.users || []), ...(R.projects || [])].find(y => y.kind + ':' + y.id === c.dataset.k); if(x) _sh.picked.push(x); shRender(); }
      else if(u){ _sh.picked = _sh.picked.filter(y => y.kind + ':' + y.id !== u.dataset.unpick); shRender(); }
    });
    shDlg.querySelector('.ml-sh-go').onclick = async () => {
      const body = {targets:_sh.targets.map(t => ({kind:t.kind, id:t.id})), recipients:_sh.picked.map(x => ({kind:x.kind, id:x.id})),
                    level:shDlg.querySelector('.ml-sh-level').value, expires_at:shDlg.querySelector('.ml-sh-exp').value || null};
      const r = await regCall('POST', '/api/media/shares', body); if(!r) return;
      toast(L('sh_done','{n} partage(s) enregistré(s)').replace('{n}', (r.shares || []).length), 'info');
      _sh.picked = []; shRender();
      if(_sh.targets.length === 1) renderShareList(shDlg.querySelector('.ml-sh-cur'), _sh.targets[0].kind, _sh.targets[0].id);
      if(_info && infoDlg.open) openInfo(_info.id, 'share');
    };
    let _info = null, _th = {aid:'', path:'', t:0, dur:0, timer:null};
    const infoTab = t => { infoDlg.querySelectorAll('.ml-if-tab').forEach(b => b.classList.toggle('on', b.dataset.t === t));
      infoDlg.querySelectorAll('.ml-if-pane').forEach(p => { p.hidden = p.dataset.t !== t; }); };
    infoDlg.querySelectorAll('.ml-if-tab').forEach(b => b.onclick = () => infoTab(b.dataset.t));
    // Filiation : passer d'un média à sa source ou à une version dérivée.
    infoDlg.addEventListener('click', ev => { const g = ev.target.closest('[data-goto]'); if(g){ ev.preventDefault(); openInfo(g.dataset.goto, 'proc'); } });
    infoDlg.querySelector('.ml-if-close').onclick = () => infoDlg.close();
    // Fermer la fiche ARRÊTE la lecture et le téléchargement du proxy (sinon le son continue).
    infoDlg.addEventListener('close', () => { stopPlayer(); clearTimeout(_pxT); });
    function stopPlayer(){ const m = infoDlg.querySelector('.ml-if-media video, .ml-if-media audio'); if(m){ m.pause(); m.removeAttribute('src'); m.load(); } }
    infoDlg.addEventListener('click', ev => { if(ev.target === infoDlg) infoDlg.close(); });
    function fieldInput(f, v){
      const id = 'ml-if-' + f.key, val = v == null ? '' : v;
      if(f.type === 'longtext') return `<textarea id="${id}" data-k="${esc(f.key)}" rows="3">${esc(val)}</textarea>`;
      if(f.type === 'bool') return `<input type="checkbox" id="${id}" data-k="${esc(f.key)}" data-bool="1"${v ? ' checked' : ''}>`;
      if(f.type === 'enum') return `<select id="${id}" data-k="${esc(f.key)}"><option value=""></option>${(f.options || []).map(x => `<option${x === val ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>`;
      return `<input type="${f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : 'text'}" id="${id}" data-k="${esc(f.key)}" value="${esc(val)}">`;
    }
    async function openInfo(aid, tab){
      let a;
      try{ const r = await fetch('/api/media/assets/' + encodeURIComponent(aid)); if(!r.ok) throw new Error('HTTP ' + r.status); a = await r.json(); }
      catch(e){ toast(e.message, 'error'); return; }
      const la = st.all.find(x => x.id === aid) || st.assets.find(x => x.id === aid); a.local_path = la ? la.local_path : null;
      _info = a;
      const t = a.tech || {}, v = t.video || {}, m = a.meta || {}, k = kindOf(a);
      infoDlg.querySelector('.ml-if-name').textContent = m.title || a.name;
      const im = infoDlg.querySelector('.ml-if-thumb'); if(v.width){ im.src = assetThumb(a); im.hidden = false; } else im.hidden = true;
      const rows2 = [[L('if_file','Fichier'), a.name], [L('if_kind','Type'), L('kind.' + k, k || '—')]];
      if(k !== 'image') rows2.push([L('col_dur','Durée'), fmtDur(t.duration)]);
      if(v.width) rows2.push([k === 'image' ? L('if_image','Image') : L('if_video','Vidéo'), k === 'image' ? `${v.width}×${v.height} · ${codecName(v.codec)}` : fmtVideo(v)],
                             [L('if_color','Couleur'), fmtColor(v)]);
      if(k !== 'image') rows2.push([L('col_audio','Audio'), audioGroups(t.audio || [])]);
      if(t.timecode) rows2.push([L('if_tc','TC de départ'), t.timecode]);
      rows2.push([L('if_container','Conteneur'), t.container || '—'], [L('col_size','Taille'), a.size ? fmtSize(a.size) : '—'],
                 [L('if_origin','Origine'), a.origin || '—']);
      if(a.producer) rows2.push([L('if_producer','Producteur'), a.producer]);
      rows2.push([L('if_added','Entré dans la base'), (a.created_at || '').replace('T', ' ')], [L('if_id','Identifiant'), a.id]);
      infoDlg.querySelector('.ml-facts').innerHTML = rows2.map(([kk, vv]) =>
        `<dt>${esc(kk)}</dt><dd>${Array.isArray(vv) ? vv.map(x => `<div>${esc(x)}</div>`).join('') : esc(vv)}</dd>`).join('');
      const base = [{key:'title', label:L('if_title','Titre'), type:'text'}, {key:'description', label:L('if_desc','Description'), type:'longtext'},
                    {key:'keywords', label:L('if_kw','Mots-clés'), type:'text'}];
      infoDlg.querySelector('.ml-if-form').innerHTML = [...base, ...(a.fields || [])].map(f => `<label for="ml-if-${esc(f.key)}">${esc(f.label)}</label>${fieldInput(f, m[f.key])}`).join('');
      const cps = a.copies || [];
      infoDlg.querySelector('.ml-if-copies').innerHTML = cps.length ? cps.map(c => {
        const s2 = c.state === 'present' ? (c.growing ? L('if_growing','en écriture') : L('if_present','présente')) : c.state === 'missing' ? L('if_missing','absente') : c.state;
        const id = `data-node="${esc(c.node_id)}" data-share="${esc(c.share_id || 0)}" data-path="${esc(c.path)}"`;
        // Sidecar : toute copie (nœud ou partage). Renommer : une copie présente sur un nœud.
        let ac = `<button type="button" class="btn" data-sc ${id}>${esc(L('if_sidecar','Sidecar'))}</button>`;
        if(c.location !== 'share') ac += c.state === 'present'
          ? (c.growing ? '' : `<button type="button" class="btn" data-ren ${id}>${esc(L('if_rename','Renommer…'))}</button>`)
            + `<button type="button" class="btn" data-del-copy="${esc(c.node_id)}" data-path="${esc(c.path)}">${esc(L('if_del_copy','Supprimer cette copie'))}</button>`
          : `<button type="button" class="btn" data-forget="${esc(c.node_id)}" data-path="${esc(c.path)}">${esc(L('if_forget','Oublier'))}</button>`;
        return `<tr class="${c.state !== 'present' ? 'is-off' : ''}"><td>${esc(c.location === 'share' ? L('if_share','Partage') : L('if_node','Nœud'))}</td>
          <td>${esc(c.node)}</td><td>${esc(verName({key:c.version || 'original'}))}</td><td class="mono ml-if-path">${esc(c.path)}</td><td>${esc(s2)}${c.fingerprint_ok === false ? ' · ⚠ ' + esc(L('if_fp_ko','contenu différent')) : ''}</td><td class="ml-if-cacts">${ac}</td></tr>`;
      }).join('') : `<tr><td colspan="6">${esc(L('if_no_copy','Aucune copie connue.'))}</td></tr>`;
      renderVersions(a);
      // Projets : un média appartient aux projets qui l'utilisent (il voyage avec eux à l'export).
      if(!st.sf){ try{ st.sf = await (await fetch('/api/media/search_fields')).json(); }catch(e){ st.sf = {fields:[], nodes:[], projects:[]}; } }
      const pn = new Map((st.sf.projects || []).map(x => [x.id, x.name])), mine = a.projects || [];
      const others = (st.sf.projects || []).filter(x => !mine.includes(x.id));
      infoDlg.querySelector('.ml-if-projs').innerHTML = `<span class="ml-if-projs-l">${esc(L('if_projects','Projets'))} :</span>
        ${mine.length ? mine.map(id => `<span class="ml-chip">${esc(pn.get(id) || '#' + id)}<button type="button" data-unproj="${id}" aria-label="${esc(L('if_proj_remove','Retirer du projet'))}">×</button></span>`).join('')
                      : `<span class="ml-muted">${esc(L('if_no_project','aucun'))}</span>`}
        ${others.length ? `<select class="ml-if-projsel"><option value="">${esc(L('if_proj_add','Ajouter au projet…'))}</option>${others.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>` : ''}`;
      const hasPic = !!v.width;
      infoDlg.querySelector('.ml-if-thumbbtn').disabled = !hasPic || k === 'image';
      infoDlg.querySelector('.ml-if-thumbnote').textContent = !hasPic ? L('if_no_video','Média sans image.')
        : k === 'image' ? L('if_thumb_image','La vignette d\'une image, c\'est l\'image.')
        : (a.thumb && a.thumb.custom ? L('if_thumb_custom','Image choisie à {t}.').replace('{t}', tc(a.thumb.t || 0)) : L('if_thumb_auto','Image automatique.'));
      const ti = infoDlg.querySelector('.ml-if-thumbbig'); if(hasPic){ ti.src = assetThumb(a); ti.hidden = false; } else ti.hidden = true;
      // ── Traitement : filiation dans les deux sens + tout ce que le transcodeur a fait ──
      const pr = a.process || null, hasProc = !!(pr || a.source || (a.derived || []).length);
      infoDlg.querySelector('.ml-if-tab[data-t="proc"]').hidden = !hasProc;
      const relL = r => L('rel.' + (r || 'transcode'), r || 'version');
      let lin = '';
      if(a.source) lin += `<p>${esc(L('proc_from','Dérivé de'))} : ${a.source.name ? `<a href="#" data-goto="${esc(a.source.id)}">${esc(a.source.name)}</a>` : esc(L('proc_from_gone','un média qui n\'est plus dans la base'))}${a.relation ? ' (' + esc(relL(a.relation)) + ')' : ''}</p>`;
      if((a.derived || []).length) lin += `<p>${esc(L('proc_derived','Versions dérivées'))} :</p><ul>${a.derived.map(d => `<li><a href="#" data-goto="${esc(d.id)}">${esc(d.name)}</a> — ${esc(relL(d.relation))}${d.preset ? ' · ' + esc(d.preset) : ''}</li>`).join('')}</ul>`;
      infoDlg.querySelector('.ml-if-lineage').innerHTML = lin;
      const pf = [];
      if(pr){
        const lo = pr.loudness, res = pr.result || {}, tcd = pr.transcoder || {};
        pf.push([L('proc_op','Opération'), [pr.operation, pr.preset].filter(Boolean).join(' · ') || '—']);
        if(tcd.hostname) pf.push([L('proc_by','Transcodeur'), tcd.hostname + (tcd.version ? ' v' + tcd.version : '')]);
        if(pr.submitted_by) pf.push([L('proc_user','Demandé par'), pr.submitted_by]);
        if(pr.watchfolder){ const w = pr.watchfolder;
          pf.push([L('proc_wf','Dossier de dépôt'), [w.name || ('#' + w.id), w.input_dir ? w.input_dir + ' → ' + (w.output_dir || '') : ''].filter(Boolean)]);
          if(w.file) pf.push([L('proc_dropped','Fichier déposé'), [w.file, w.size ? fmtSize(w.size) : '', w.mtime ? (w.mtime || '').replace('T', ' ') : '',
            w.detected_at ? L('proc_detected','repéré le {d}').replace('{d}', w.detected_at.replace('T', ' ')) : ''].filter(Boolean).join(' · ')]); }
        if(res.encode_s != null) pf.push([L('proc_time','Encodage'), res.encode_s + ' s' + (res.realtime_factor ? ' · ×' + res.realtime_factor + ' ' + L('proc_rt','temps réel') : '')]);
        if(lo) pf.push([L('proc_loud','Loudness'), [lo.measured_lufs != null ? lo.measured_lufs + ' LUFS' : '', lo.true_peak_dbtp != null ? L('proc_tp','crête') + ' ' + lo.true_peak_dbtp + ' dBTP' : '',
          lo.gain_db ? L('proc_gain','gain') + ' ' + lo.gain_db + ' dB' : '', lo.target_lufs != null ? L('proc_target','cible') + ' ' + lo.target_lufs : ''].filter(Boolean).join(' · ')]);
        const plan = [(pr.plan || {}).video, (pr.plan || {}).audio].filter(Boolean);
        if(plan.length) pf.push([L('proc_plan','Plan'), plan.map(x => typeof x === 'string' ? x : JSON.stringify(x))]);
        if((pr.notes || []).length) pf.push([L('proc_notes','Écarts et remarques'), pr.notes.map(String)]);
        if(pr.source && pr.source.path) pf.push([L('proc_src','Source'), pr.source.path]);
        if(pr.ffmpeg) pf.push(['ffmpeg', pr.ffmpeg]);
        pf.push([L('proc_dates','Créé · démarré · fini'), [pr.created_at, pr.started_at, pr.completed_at].map(x => (x || '—').replace('T', ' ')).join(' · ')]);
      }
      infoDlg.querySelector('.ml-proc-facts').innerHTML = pf.map(([kk, vv]) =>
        `<dt>${esc(kk)}</dt><dd>${Array.isArray(vv) ? vv.map(x => `<div>${esc(x)}</div>`).join('') : esc(vv)}</dd>`).join('');
      const cmdEl = infoDlg.querySelector('.ml-if-cmd'); cmdEl.hidden = !(pr && pr.cmd); if(pr && pr.cmd) cmdEl.querySelector('pre').textContent = pr.cmd;
      renderProxy(a);
      // Partages : la liste (et « Partager… ») pour le propriétaire ; pour un destinataire, de qui il
      // tient ce média et à quel niveau. Ce que son niveau n'autorise pas est masqué.
      const shBox = infoDlg.querySelector('.ml-if-shares'), acc = a.access || 'owner';
      infoDlg.querySelector('.ml-if-delall').hidden = acc !== 'owner';
      infoDlg.querySelector('.ml-if-save').hidden = acc === 'use';
      if(acc !== 'owner'){
        shBox.innerHTML = `<p>${esc(L('sh_by_tip','Partagé avec vous par {u}').replace('{u}', (a.shared || {}).by || '?'))} — ${esc(shLevel(acc))}.</p>
          <p class="ml-muted">${esc(acc === 'edit' ? L('sh_help_edit','En plus : modifier la fiche (métadonnées, vignette, proxy) et, pour un bin, y ranger ou en retirer des médias. Jamais supprimer le média ni ses fichiers.') : L('sh_help_use','Voir, lire, le proxy, charger dans ses conteneurs, rapatrier, mettre en playlist.'))}</p>`;
      } else {
        shBox.innerHTML = `<div class="ml-if-acts"><span class="ml-sp"></span>${canShare() ? `<button type="button" class="btn btn-green ml-if-sharebtn">${esc(L('sh_share','Partager…'))}</button>` : ''}</div><div class="ml-if-shlist"></div>`;
        const shBtn = shBox.querySelector('.ml-if-sharebtn'); if(shBtn) shBtn.onclick = () => openShare([{kind:'asset', id:a.id, name:a.name}]);
        renderShareList(shBox.querySelector('.ml-if-shlist'), 'asset', a.id);
      }
      infoTab(tab || 'info');
      if(!infoDlg.open) infoDlg.showModal();
    }
    // ── Proxy : lecteur (fiche Infos), état et actions (Emplacements), avancement d'une génération ──
    let _pxT = null;
    const pxJob = a => (a.proxy_jobs || []).filter(j => j.state === 'queued' || j.state === 'running').pop() || null;
    // Le proxy est un job de la FERME DE TRANSCODAGE : on dit où il est fait (transcoder, nœud),
    // et pourquoi il attend — « aucun transcoder inscrit » ne doit jamais passer pour une file normale.
    function pxJobTxt(j){
      if(j.state === 'queued') return j.waiting ? L('px_waiting','En attente d\'un transcoder : aucun n\'est inscrit à la ferme (job n° {j})').replace('{j}', j.id)
        : L('px_queued_farm','En file de la ferme de transcodage (job n° {j}{q})').replace('{j}', j.id).replace('{q}', j.queue_pos ? ', ' + L('px_pos','rang {n}').replace('{n}', j.queue_pos) : '');
      if(j.farm_status === 'transfer_in') return L('px_copy_in','Copie de la source vers {w} ({n})…').replace('{w}', j.worker || '?').replace('{n}', j.worker_node || '?');
      if(j.farm_status === 'transfer_out') return L('px_copy_out','Retour du proxy sur {n}…').replace('{n}', j.node || '?');
      return L('px_running','Génération par {w} ({n}) : {p} %').replace('{w}', j.worker || '?').replace('{n}', j.worker_node || '?').replace('{p}', Math.round((j.progress || 0) * 100));
    }
    const pxMade = p => { const m = p.made || {}; return m.worker ? ' · ' + L('px_made','fait par {w} ({n}), job n° {j}').replace('{w}', m.worker).replace('{n}', m.worker_node || '?').replace('{j}', m.farm_job || '?') : ''; };
    // Lecteur du PROXY (vidéo, ou audio seul) — sinon la vignette et « Créer le proxy » / l'avancement.
    // Partagé par la fiche Infos et l'aperçu du mode « Liste + aperçu ». `at` : instant de départ.
    function mediaBlock(a, at){
      const k = kindOf(a), v = (a.tech || {}).video || {};
      const pres = (a.proxies || []).find(p => p.state === 'present'), job = pxJob(a);
      if(pres){
        const audioOnly = /\.m4a$/i.test(pres.path);
        const url = '/api/media/assets/' + encodeURIComponent(a.id) + '/proxy?node_id=' + pres.node_id + (at != null && !audioOnly ? '#t=' + at.toFixed(2) : '');
        return audioOnly
          ? `<audio class="ml-if-player" controls preload="metadata" data-aid="${esc(a.id)}" src="${esc(url)}"></audio>`
          : `<video class="ml-if-player" controls preload="metadata" playsinline data-aid="${esc(a.id)}" src="${esc(url)}"${v.width && at == null ? ` poster="${esc(assetThumb(a))}"` : ''}></video>`;
      }
      const thumb = v.width ? `<img class="ml-if-thumbbig" alt="" src="${esc(assetThumb(a))}">` : '';
      if(k !== 'video' && k !== 'audio') return thumb;
      return `${thumb}<div class="ml-if-pxbar"><span class="ml-muted ml-if-pxtxt">${esc(job ? pxJobTxt(job) : L('px_none','Pas de proxy : le média ne se lit pas ici.'))}</span>
        ${job ? `<progress class="ml-if-pxprog" max="1" value="${job.progress || 0}"></progress>` : `<button type="button" class="btn" data-px-create>${esc(L('px_create','Créer le proxy'))}</button>`}</div>`;
    }
    function renderProxy(a){
      const k = kindOf(a), box = infoDlg.querySelector('.ml-if-media'), v = (a.tech || {}).video || {};
      const pres = (a.proxies || []).find(p => p.state === 'present'), job = pxJob(a);
      const can = k === 'video' || k === 'audio';
      const cur = box.querySelector('video,audio');
      // Le lecteur déjà en place pour ce proxy reste (pas de rechargement à chaque relecture de la fiche).
      if(!(pres && cur && cur.dataset.aid === a.id)){
        stopPlayer();
        // Ouverte dans la minute qui suit un survol-défilement (ou un clic sur une image de la
        // pellicule) : le lecteur part de l'instant survolé.
        const audioOnly = !!pres && /\.m4a$/i.test(pres.path);
        const at = pres && !audioOnly && _lastScrub && _lastScrub.aid === a.id && Date.now() - _lastScrub.at < 60000 ? _lastScrub.t : null;
        if(at != null) _lastScrub = null;
        box.innerHTML = pres || can ? mediaBlock(a, at) : '';
      }
      // Emplacements : le proxy de chaque nœud, et la génération en cours.
      const rowsP = (a.proxies || []).map(p => {
        const s2 = p.state === 'present' ? L('if_present','présente') : p.state === 'creating' ? L('px_creating','en génération')
          : p.state === 'missing' ? L('if_missing','absente') : p.state === 'corrupt' ? L('px_corrupt','taille inattendue') : p.state;
        return `<tr class="${p.state !== 'present' ? 'is-off' : ''}"><td>${esc(L('if_node','Nœud'))}</td><td>${esc(p.node)}</td>
          <td class="mono ml-if-path">${esc(p.path)}</td><td>${esc(s2)}${p.size ? ' · ' + esc(fmtSize(p.size)) : ''}${esc(pxMade(p))}</td>
          <td class="ml-if-cacts">${p.state === 'creating' ? '' : `<button type="button" class="btn" data-px-redo="${esc(p.node_id)}">${esc(L('px_redo','Recréer'))}</button>`}
          <button type="button" class="btn" data-px-del="${esc(p.node_id)}">${esc(L('px_delete','Supprimer le proxy'))}</button></td></tr>`;
      });
      if(job && !(a.proxies || []).some(p => p.state === 'creating' && p.node_id === job.node_id))
        rowsP.push(`<tr><td>${esc(L('if_node','Nœud'))}</td><td>${esc(job.node || '')}</td><td class="mono ml-if-path">${esc(job.out || '')}</td><td>${esc(pxJobTxt(job))}</td><td class="ml-if-cacts"></td></tr>`);
      if(job) rowsP.push(`<tr><td colspan="4"><progress class="ml-if-pxprog" max="1" value="${job.progress || 0}"></progress> <span class="ml-if-pxtxt">${esc(pxJobTxt(job))}</span>
          <a href="/medias#farm" class="ml-muted">${esc(L('px_farm_link','voir la ferme'))}</a></td>
          <td class="ml-if-cacts"><button type="button" class="btn" data-px-cancel="${esc(job.id)}">${esc(L('cancel','Annuler'))}</button></td></tr>`);
      const failed = (a.proxy_jobs || []).filter(j => j.state === 'failed').pop();
      if(failed && !job && !pres) rowsP.push(`<tr class="is-off"><td colspan="5">⚠ ${esc(L('px_failed','Dernière génération en échec : {e}').replace('{e}', (failed.error || '?') + (failed.worker ? ' (' + failed.worker + ')' : '') + ' — job n° ' + failed.id))}</td></tr>`);
      infoDlg.querySelector('.ml-if-proxies').innerHTML = rowsP.length ? rowsP.join('')
        : `<tr><td colspan="4" class="ml-muted">${esc(can ? L('px_none_where','Aucun proxy.') : L('px_na','Pas de proxy pour une image fixe : la vignette suffit.'))}</td>
           <td class="ml-if-cacts">${can ? `<button type="button" class="btn" data-px-create>${esc(L('px_create','Créer le proxy'))}</button>` : ''}</td></tr>`;
      // Génération en file ou en cours : la fiche suit l'avancement, puis s'ouvre sur le lecteur.
      clearTimeout(_pxT);
      if(job) _pxT = setTimeout(async () => {
        if(!infoDlg.open || !_info || _info.id !== a.id) return;
        const j = await (await fetch('/api/media/proxies?asset=' + encodeURIComponent(a.id))).json().catch(() => ({}));
        a.proxy_jobs = j.jobs || [];
        const nj = pxJob(a);
        if(nj){ infoDlg.querySelectorAll('.ml-if-pxprog').forEach(x => { x.value = nj.progress || 0; });
          infoDlg.querySelectorAll('.ml-if-pxtxt').forEach(x => { x.textContent = pxJobTxt(nj); }); renderProxy(a); }
        else { const t = infoDlg.querySelector('.ml-if-tab.on'); await openInfo(a.id, t ? t.dataset.t : 'info'); loadAssets().then(render); }
      }, 2000);
    }
    infoDlg.addEventListener('click', async ev => {
      if(!_info) return;
      const cr = ev.target.closest('[data-px-create]'), redo = ev.target.closest('[data-px-redo]');
      const del = ev.target.closest('[data-px-del]'), cn = ev.target.closest('[data-px-cancel]');
      if(cr || redo){
        const t = infoDlg.querySelector('.ml-if-tab.on');
        const r = await regCall('POST', '/api/media/proxies', {asset_ids:[_info.id], ...(redo ? {node_id:+redo.dataset.pxRedo} : {})});
        if(r){ const ref = Object.values(r.refused || {}); if(ref.length) toast(ref[0], 'warning'); else toast(L('px_queued','{n} proxy(s) en file de génération').replace('{n}', 1), 'info');
          openInfo(_info.id, t ? t.dataset.t : 'info'); }
      } else if(del){
        const okc = window.mxlConfirm ? await window.mxlConfirm({danger:true, message:L('px_del_confirm','Supprimer le proxy de ce nœud ? Le média et ses copies ne sont pas touchés.')}) : false;
        if(!okc) return;
        stopPlayer();
        if(await regCall('DELETE', '/api/media/assets/' + encodeURIComponent(_info.id) + '/proxy', {node_id:+del.dataset.pxDel})) afterChange('where');
      } else if(cn){
        if(await regCall('POST', '/api/media/proxies/' + encodeURIComponent(cn.dataset.pxCancel) + '/cancel', {})){
          toast(L('px_cancelled','Génération annulée'), 'info'); setTimeout(() => openInfo(_info.id, 'where'), 2500); }
      }
    });
    // ── Versions : l'original et ses autres essences, sous la même fiche (app/media_registry.py) ──
    const verName = v => v.key === 'original' ? L('ver_original','Original') : (v.label ? v.key + ' — ' + v.label : v.key);
    function verFormat(v){
      const t = v.tech || {}, vv = t.video || {};
      const parts = [];
      if(vv.width) parts.push(fmtVideo(vv) + (vv.codec ? ' · ' + codecName(vv.codec) : ''));
      const au = (t.audio || []); if(au.length) parts.push(audioGroups(au).join(', '));
      if(t.container) parts.push(t.container);
      return parts.join(' · ') || '—';
    }
    function renderVersions(a){
      const acc = a.access || 'owner', vs = a.versions || [];
      infoDlg.querySelector('.ml-if-versions').innerHTML = vs.map(v => {
        const pr = v.process || {}, by = (pr.transcoder || {}).hostname;
        const sub = v.original ? '' : [v.preset, by ? L('ver_made_by','fait par {w}').replace('{w}', by) : '', (v.created_at || '').replace('T', ' ')].filter(Boolean).join(' · ');
        return `<tr class="${v.present ? '' : 'is-off'}"><td><b>${esc(verName(v))}</b>${sub ? `<div class="ml-muted">${esc(sub)}</div>` : ''}</td>
          <td>${esc(verFormat(v))}</td><td>${esc(v.size ? fmtSize(v.size) : '—')}</td>
          <td>${esc((v.nodes || []).join(', ') || (v.present ? '' : L('ver_offline','hors ligne')))}</td>
          <td><input type="radio" name="${uid}-vdef" data-ver-def="${esc(v.key)}" ${v.default ? 'checked' : ''} ${acc === 'use' ? 'disabled' : ''}
               aria-label="${esc(L('ver_set_default','Version par défaut'))} : ${esc(verName(v))}"></td>
          <td class="ml-if-cacts">${!v.original && acc === 'owner' ? `<button type="button" class="btn" data-ver-del="${esc(v.key)}">${esc(L('ver_delete','Supprimer cette version…'))}</button>` : ''}</td></tr>`;
      }).join('');
    }
    infoDlg.querySelector('.ml-if-versions').addEventListener('change', async ev => {
      const r = ev.target.closest('[data-ver-def]'); if(!r || !_info) return;
      if(await regCall('POST', '/api/media/assets/' + encodeURIComponent(_info.id) + '/default_version', {version:r.dataset.verDef})){
        toast(L('ver_default_set','Version par défaut : {v}').replace('{v}', verName((_info.versions || []).find(v => v.key === r.dataset.verDef) || {key:r.dataset.verDef})), 'info');
        afterChange('versions');
      } else renderVersions(_info);
    });
    infoDlg.querySelector('.ml-if-versions').addEventListener('click', async ev => {
      const d = ev.target.closest('[data-ver-del]'); if(!d || !_info) return;
      const v = (_info.versions || []).find(x => x.key === d.dataset.verDel) || {key:d.dataset.verDel};
      const okc = window.mxlConfirm ? await window.mxlConfirm({danger:true, message:L('ver_del_confirm','Supprimer la version « {v} » : ses copies sur les nœuds sont effacées du disque ({n}). L\'original et les autres versions restent.')
        .replace('{v}', verName(v)).replace('{n}', v.present || 0)}) : false;
      if(!okc) return;
      if(await regCall('DELETE', '/api/media/assets/' + encodeURIComponent(_info.id) + '/versions/' + encodeURIComponent(v.key))){
        toast(L('ver_deleted','Version supprimée'), 'info'); afterChange('versions'); }
    });
    async function afterChange(tab){ _projP = null; await loadAssets(); render(); if(o.onChanged) o.onChanged(); if(_info && infoDlg.open) openInfo(_info.id, tab); }
    infoDlg.querySelector('.ml-if-save').onclick = async () => {
      if(!_info) return;
      const meta = {};
      infoDlg.querySelectorAll('.ml-if-form [data-k]').forEach(el => { meta[el.dataset.k] = el.dataset.bool ? el.checked : el.value; });
      if(!await regCall('PATCH', '/api/media/assets/' + encodeURIComponent(_info.id), {meta})) return;
      toast(L('if_saved','Métadonnées enregistrées'), 'info'); afterChange('meta');
    };
    infoDlg.querySelector('.ml-if-copies').onclick = async ev => {
      const sc = ev.target.closest('[data-sc]'), rn = ev.target.closest('[data-ren]');
      if(sc && _info) return showSidecar(_info, +sc.dataset.node, +sc.dataset.share, sc.dataset.path);
      if(rn && _info) return startRename(rn.closest('tr'), +rn.dataset.node, rn.dataset.path);
      const d = ev.target.closest('[data-del-copy]'), f = ev.target.closest('[data-forget]');
      if(!_info || (!d && !f)) return;
      if(d){
        const okc = window.mxlConfirm ? await window.mxlConfirm({danger:true, message:L('if_del_copy_confirm','Supprimer cette copie du disque ? Le média et ses métadonnées restent dans la base.')}) : false;
        if(!okc || !await regCall('DELETE', '/api/media/assets/' + encodeURIComponent(_info.id), {node_id:+d.dataset.delCopy, path:d.dataset.path})) return;
      } else if(!await regCall('POST', '/api/media/assets/' + encodeURIComponent(_info.id) + '/forget', {node_id:+f.dataset.forget, path:f.dataset.path})) return;
      afterChange('where');
    };
    // Renommer le FICHIER d'une copie (et son sidecar) sur son nœud — en place dans le tableau des
    // emplacements. L'identité et le nom du média (sa fiche) ne changent pas.
    function startRename(tr, node, path){
      if(!tr || tr.querySelector('.ml-ren-in')) return;
      const cell = tr.querySelector('.ml-if-path'), acts = tr.querySelector('.ml-if-cacts');
      const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : '';
      const old = cell.innerHTML, oldActs = acts.innerHTML;
      // Nom codé : seule la BASE est modifiable ; le code du média, sa version et l'extension sont
      // affichés à côté, hors du champ (le serveur les reprend de toute façon — media_naming.rename_stem).
      const fixed = fixedSuffix(baseName(path), _info.id), stem = baseName(path).slice(0, baseName(path).length - fixed.length);
      cell.innerHTML = `<span class="ml-muted">${esc(dir)}</span><input class="ml-ren-in" value="${esc(stem)}" aria-label="${esc(L('if_rename_ph','Nouveau nom du fichier'))}">`
        + (fixed ? `<span class="ml-muted ml-ren-fixed" title="${esc(L('if_rename_fixed','Code du média et version : ajoutés automatiquement'))}">${esc(fixed)}</span>` : '');
      acts.innerHTML = `<button type="button" class="btn btn-green" data-ren-ok>${esc(L('save','Enregistrer'))}</button><button type="button" class="btn" data-ren-no>${esc(L('cancel','Annuler'))}</button>`;
      const inp = cell.querySelector('input'); inp.focus();
      // Raison d'un refus, écrite sous le champ (lue avec lui : aria-describedby), effacée à la frappe.
      const why = document.createElement('span'); why.className = 'ml-ren-why'; why.id = `${uid}-renwhy`; why.hidden = true; cell.appendChild(why);
      inp.setAttribute('aria-describedby', why.id);
      inp.oninput = () => { inp.removeAttribute('aria-invalid'); inp.title = ''; why.hidden = true; why.textContent = ''; };
      const dot = fixed ? -1 : inp.value.lastIndexOf('.'); inp.setSelectionRange(0, dot > 0 ? dot : inp.value.length);
      const undo = () => { cell.innerHTML = old; acts.innerHTML = oldActs; };
      const go = async () => {
        const nn = inp.value.trim() + fixed; if(!inp.value.trim() || nn === baseName(path)) return undo();
        const err = {};
        const r = await regCall('POST', '/api/media/assets/' + encodeURIComponent(_info.id) + '/rename_copy', {node_id:node, path, new_name:nn}, err);
        // Refus (nom déjà pris…) : le champ le DIT, en plus de la notification — sinon il garde le nom
        // refusé et laisse croire que c'est fait (recette du 2026-10-12).
        if(!r){ inp.setAttribute('aria-invalid', 'true'); inp.title = L('if_rename_refused','Renommage refusé : le fichier garde son nom');
          why.textContent = err.message || inp.title; why.hidden = false; return inp.focus(); }
        toast(L('if_renamed','Fichier renommé : {n}').replace('{n}', nn), 'info'); afterChange('where');
      };
      acts.querySelector('[data-ren-ok]').onclick = go;
      acts.querySelector('[data-ren-no]').onclick = undo;
      inp.onkeydown = e => { if(e.key === 'Enter'){ e.preventDefault(); go(); } else if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); undo(); } };
    }
    // Partie AUTOMATIQUE d'un nom codé (`__<code>`, piste Avid / _proxy / version, extension) — même
    // motif que app/media_naming.py:_CODED ; '' si le nom ne porte pas le code de CE média.
    function fixedSuffix(name, aid){
      const m = /^(.+?)__([0-9a-f]{8}|[0-9a-f]{6})(?:_[va]\d{1,3}|_proxy|_[a-z][a-z0-9]{0,15})?(?:\.[^./]{1,8})?$/.exec(name || '');
      if(!m || !String(aid || '').toLowerCase().replace(/-/g, '').startsWith(m[2])) return '';
      return name.slice(m[1].length);
    }
    // Depuis le menu d'une ligne : la copie que voit le conteneur, sinon la seule copie présente ;
    // plusieurs copies au Gestionnaire : la fiche s'ouvre sur les emplacements, on choisit.
    async function renameFile(a){
      await openInfo(a.id, 'where'); if(!_info || _info.id !== a.id) return;
      const nodes = (_info.copies || []).filter(c => c.location !== 'share' && c.state === 'present' && !c.growing);
      let c = null;
      if(!global && st.ctx && a.local_path) c = nodes.find(x => x.node_id === st.ctx.node_id && x.path.endsWith(a.local_path));
      if(!c && nodes.length === 1) c = nodes[0];
      if(!c) return;
      const btn = infoDlg.querySelector(`[data-ren][data-node="${c.node_id}"][data-path="${CSS.escape(c.path)}"]`);
      if(btn) startRename(btn.closest('tr'), c.node_id, c.path);
    }
    // Sidecar brut, en lecture seule, coloré simplement (balises, attributs, valeurs, commentaires).
    function hlXml(x){
      return esc(x).replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="x-c">$1</span>')
        .replace(/(&lt;\/?)([\w:.-]+)/g, '$1<span class="x-t">$2</span>')
        .replace(/([\w:.-]+)=(&quot;[^&]*?(?:&(?!quot;)[^&]*?)*&quot;)/g, '<span class="x-a">$1</span>=<span class="x-v">$2</span>');
    }
    async function showSidecar(a, node, share, path){
      let d;
      try{ const r = await fetch(`/api/media/assets/${encodeURIComponent(a.id)}/sidecar?node_id=${node}&share_id=${share || 0}&path=${encodeURIComponent(path)}`);
        d = await r.json(); if(!r.ok || d.ok === false) throw new Error(d.error || 'HTTP ' + r.status); }
      catch(e){ toast(e.message, 'error'); return; }
      const c = (a.copies || []).find(x => x.node_id === node && (x.share_id || 0) === (share || 0) && x.path === path) || {};
      scDlg.querySelector('.ml-sc-name').textContent = baseName(path) + '.xml';
      scDlg.querySelector('.ml-sc-where').textContent = (c.node || '') + ' : ' + path + '.xml';
      const pre = scDlg.querySelector('.ml-sc-pre');
      pre._raw = d.xml || '';
      pre.innerHTML = d.xml ? hlXml(d.xml) : `<span class="ml-muted">${esc(L('sc_none','Pas de sidecar pour cette copie.'))}</span>`;
      scDlg.querySelector('.ml-sc-copy').disabled = !d.xml;
      if(!scDlg.open) scDlg.showModal();
    }
    scDlg.querySelector('.ml-sc-close').onclick = () => scDlg.close();
    scDlg.addEventListener('click', ev => { if(ev.target === scDlg) scDlg.close(); });
    scDlg.querySelector('.ml-sc-copy').onclick = async () => {
      const pre = scDlg.querySelector('.ml-sc-pre');
      try{ await navigator.clipboard.writeText(pre._raw || ''); toast(L('sc_copied','Sidecar copié'), 'info'); }
      catch(e){ const r = document.createRange(); r.selectNodeContents(pre); const sl = getSelection(); sl.removeAllRanges(); sl.addRange(r);
        toast(L('sc_copy_manual','Copie bloquée par le navigateur : le texte est sélectionné (Ctrl+C).'), 'warning'); }
    };
    const projBox = infoDlg.querySelector('.ml-if-projs');
    projBox.onclick = async ev => {
      const b = ev.target.closest('[data-unproj]');
      if(!b || !_info) return;
      if(await regCall('POST', '/api/media/assets/' + encodeURIComponent(_info.id) + '/projects', {project_id:+b.dataset.unproj, on:false})) afterChange('where');
    };
    projBox.onchange = async ev => {
      const sel = ev.target.closest('.ml-if-projsel');
      if(!sel || !sel.value || !_info) return;
      if(await regCall('POST', '/api/media/assets/' + encodeURIComponent(_info.id) + '/projects', {project_id:+sel.value, on:true})) afterChange('where');
    };
    infoDlg.querySelector('.ml-if-delall').onclick = async () => {
      if(!_info) return;
      const n = (_info.copies || []).filter(c => c.state === 'present' && c.location !== 'share').length;
      const okc = window.mxlConfirm ? await window.mxlConfirm({danger:true, message:L('del_all_confirm','Supprimer le média « {m} » : ses {n} copie(s) sur les nœuds, ses métadonnées et sa vignette ? Les copies sur un partage réseau ne sont pas touchées.').replace('{m}', _info.name).replace('{n}', n)}) : false;
      if(!okc || !await regCall('DELETE', '/api/media/assets/' + encodeURIComponent(_info.id))) return;
      infoDlg.close(); _info = null; afterChange(); toast(L('deleted','Média supprimé'), 'info');
    };
    // Choix de vignette : aperçu par le conteneur (rapide) s'il voit le fichier, sinon par l'orchestrateur.
    function thPreview(){
      clearTimeout(_th.timer);
      _th.timer = setTimeout(() => {
        thDlg.querySelector('.ml-th-img').src = (o.framePreview && _th.path) ? o.framePreview(_th.path, _th.t, 480)
          : `/api/media/assets/${encodeURIComponent(_th.aid)}/frame?t=${_th.t.toFixed(3)}&w=480`;
      }, o.framePreview && _th.path ? 180 : 500);
      thDlg.querySelector('.ml-th-tc').textContent = tc(_th.t);
      thDlg.querySelector('.ml-th-range').value = _th.t;
    }
    function openThumb(a){
      const t = a.tech || {};
      _th.aid = a.id; _th.path = a.local_path || ''; _th.dur = Math.max(0, t.duration || 0); _th.t = Math.min(_th.dur, (a.thumb && a.thumb.t) || 1);
      _th.fps = ((t.video || {}).fps) || 25;
      thDlg.querySelector('.ml-th-name').textContent = a.name;
      const rg = thDlg.querySelector('.ml-th-range'); rg.max = _th.dur || 1; rg.step = 1 / _th.fps;
      thDlg.querySelector('.ml-th-cur').src = assetThumb(a);
      thDlg.querySelector('.ml-th-pos').hidden = !(o.position && o.current && _th.path && o.current() === _th.path);
      thPreview(); if(!thDlg.open) thDlg.showModal();
    }
    thDlg.querySelector('.ml-th-range').oninput = e => { _th.t = parseFloat(e.target.value) || 0; thPreview(); };
    thDlg.querySelectorAll('[data-step]').forEach(b => b.onclick = () => {
      _th.t = Math.max(0, Math.min(_th.dur, _th.t + parseFloat(b.dataset.step) / (b.dataset.unit === 'f' ? _th.fps : 1))); thPreview(); });
    thDlg.querySelector('.ml-th-pos').onclick = () => { _th.t = Math.max(0, o.position() || 0); thPreview(); };
    async function thSave(body, aid){
      if(!await regCall('POST', `/api/media/assets/${encodeURIComponent(aid || _th.aid)}/thumb`, body)) return;
      thDlg.close(); toast(L('thumb_saved','Vignette enregistrée'), 'info'); afterChange('thumb');
    }
    thDlg.querySelector('.ml-th-save').onclick = () => thSave({t:_th.t});
    thDlg.querySelector('.ml-th-cancel').onclick = () => thDlg.close();
    thDlg.addEventListener('click', ev => { if(ev.target === thDlg) thDlg.close(); });
    infoDlg.querySelector('.ml-if-thumbbtn').onclick = () => { if(_info) openThumb(_info); };
    infoDlg.querySelector('.ml-if-thumbreset').onclick = () => { if(_info) thSave({reset:true}, _info.id); };

    // ── Démarrage ──
    // `_dead` : une bibliothèque DÉTRUITE pendant son démarrage (son hôte redessiné entre-temps) ne
    // poursuit pas — ses éléments n'existent plus, chaque `$('…').hidden` levait une exception
    // (vu le 2026-10-20 dans un panneau de workspace redessiné à l'arrivée d'une liste d'options).
    let _dead = false;
    (async () => {
      try{ const r = await fetch(`/api/media/browse_share?vmid=${vmid == null ? '' : vmid}`); const d = await r.json();
        if(_dead) return;
        $('.ml-tab[data-tab="net"]').hidden = o.network === false || !(d.dirs || []).length; }catch(e){}
      if(_dead) return;
      // Conteneur d'un PROJET : la bibliothèque s'ouvre sur les médias de ce projet — filtre posé
      // d'office, visible dans les puces, retirable comme les autres.
      if(!global){
        try{ const r = await fetch('/api/media/context?vmid=' + encodeURIComponent(vmid)); if(r.ok) st.ctx = await r.json(); }catch(e){}
        if(st.ctx && st.ctx.project_id && o.projectFilter !== false && !st.tree.items.some(f => f.field === 'project'))
          { if(st.tree.items.length) st.tree.links.unshift('and'); st.tree.items.unshift({field:'project', op:'eq', value:st.ctx.project_id}); }
      }
      if(_dead) return;
      if(st.tree.items.length) loadSF().then(() => { if(!_dead) renderChips(); });
      await loadAssets(); if(_dead) return; render(); loadBins();
    })();

    return {
      reload, render, markCurrent, importExt, waitJob, regCall, selected: selectedRows,
      clearSelection(){ st.sel.clear(); render(); },
      byPath: p => st.byPath[p] || null,
      assets: () => st.all,
      assetThumb, openInfo, fmtDur, fmtShort, fmtAudioShort, kindOf, fmtSize,
      selectAll, renameFile, openMenu, getState, setState,
      destroy(){ _dead = true; scrubStop(); stopBatches(); sbStop(); pvStop(true); clearInterval(_binTimer); clearTimeout(_qT); clearTimeout(_fT);
        document.removeEventListener('mousedown', _closeMenu, true); document.removeEventListener('ml:bins', _onBins); window.removeEventListener('imageeditor:saved', _onImgSaved);
        document.removeEventListener('ml:uploaded', _onUploaded);
        window.removeEventListener('scroll', _closeOnScroll, true); window.removeEventListener('blur', closeMenu);
        root.innerHTML = ''; root.classList.remove('ml', 'ml-fill'); },
    };
  }

  if(typeof module !== 'undefined') module.exports = {scrubFrac, scrubPopSize, scrubTime, fmtClock, createSeeker, teardownVideo,
    VIEWS, GROUPS, sanitizeViewState, toggleField, groupKeys, buildGroups, pickFrames, sbValid, sbCell, sbTime, sbIndexAt};
  if(typeof window === 'undefined') return;
  window.MediaLibrary = {mount};
})();
