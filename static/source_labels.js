/* ═══════════════════════════════════════════════════════════════════════════════════════════
   NIVEAU DE LIBELLÉ DES SOURCES — service GLOBAL (chargé par layout.html sur TOUTES les pages)

   Le parc nomme un même flux à plusieurs niveaux simultanés (table `source_labels` : `label_2`
   … `label_9`, éditée par /labels) : nom technique, nom de production, nom d'antenne, UMD reçu
   par TSL… Consigne : toute liste de sources doit laisser CHOISIR le niveau affiché, jamais en
   imposer un — sinon la liste est inutilisable pour les métiers qui n'emploient pas ce niveau-là,
   et la saisie des autres niveaux devient du travail mort.

   Ce module porte donc UNE fois :
     - le niveau courant, choisi dans la barre de navigation (à côté de l'utilisateur), mémorisé
       par navigateur — c'est un réglage de VUE, qu'on change à la volée selon ce qu'on cherche ;
     - la RÈGLE de résolution (héritage parent + suffixe, repli), qui vivait uniquement dans le JS
       de la page /labels : elle est ici, et /labels l'appelle plutôt que de la redéfinir ;
     - un événement `source-labels:change` pour que chaque page se re-rende à la volée.

   ⚠ Ce fichier est chargé par layout.html, PAS par scripts.js : ce dernier n'est inclus que par
   les pages containers/projects, donc un helper qui y vivrait serait ABSENT — en silence — de
   toutes les pages de plugin (piège déjà payé plusieurs fois).
   ═════════════════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var STORE_KEY = 'bobi.source_label_level';
  var DEFAULT_LEVEL = 2;          // colonne 2 = convention TSL du parc (UMD reçu)
  // Le parc compte DIX niveaux, pas huit : 0 = hostname du conteneur producteur et 1 = nom du flux
  // MXL (le shm) sont calculés, pas stockés — ils n'ont pas de colonne en base, mais ce sont des
  // niveaux à part entière côté exploitation (cf. db_get_source_label_for_shm, même convention).
  var MIN_LEVEL = 0, MAX_LEVEL = 9;
  var FIRST_STORED = 2;           // 2..9 = colonnes réellement stockées dans source_labels

  var _level  = DEFAULT_LEVEL;
  var _rows   = [];               // [{shm, label_2..label_9, parent_shm}]
  var _byShm  = {};
  var _names  = [];               // noms des colonnes (réglage tsl_label_names)
  var _host   = {};               // shm → hostname du producteur (niveau 0, calculé)
  var _suffix = {};               // {"_audio_0": "_A1", …} — suffixe ajouté au label hérité
  var _loaded = null;             // promesse de premier chargement
  var _sig    = null;             // signature du dernier chargement (anti-re-rendu inutile)

  try {
    var v = parseInt(window.localStorage.getItem(STORE_KEY), 10);
    if (v >= MIN_LEVEL && v <= MAX_LEVEL) _level = v;
  } catch (e) { /* localStorage indisponible → défaut */ }

  function _colKey(col) { return 'label_' + col; }

  function _index(rows) {
    var m = {};
    (rows || []).forEach(function (r) { if (r && r.shm) m[r.shm] = r; });
    return m;
  }

  /* Valeur d'une cellule, héritage compris. `rows` permet à l'éditeur /labels de résoudre sur SES
     lignes en cours d'édition (non enregistrées) au lieu du cache serveur.
     Règle (inchangée, déplacée ici) : valeur propre → sinon celle du parent + suffixe de la queue
     de nom (`_audio_0` → `_A1`), le suffixe étant un réglage. Retourne {value, inherited}. */
  function resolve(shm, col, rows) {
    var idx = rows ? _index(rows) : _byShm;
    var row = idx[shm];
    if (!row) return { value: '', inherited: false };
    var key = _colKey(col);
    var own = row[key] || '';
    if (own) return { value: own, inherited: false };
    var parent = row.parent_shm;
    if (!parent) return { value: '', inherited: false };
    var prow = idx[parent];
    if (!prow) return { value: '', inherited: false };
    var pval = prow[key] || '';
    if (!pval) return { value: '', inherited: false };
    var tail = String(shm).slice(String(parent).length);
    return { value: pval + (_suffix[tail] !== undefined ? _suffix[tail] : tail), inherited: true };
  }

  /* Valeur d'un niveau donné, calculés compris. 0 = hostname du producteur, 1 = nom du flux MXL. */
  function valueAt(shm, col) {
    if (col === 0) return { value: _host[shm] || '', inherited: false };
    if (col === 1) return { value: shm || '', inherited: false };
    return resolve(shm, col);
  }

  /* Libellé AFFICHABLE d'un shm : niveau courant (ou `col`), puis repli sur le premier autre
     niveau non vide, puis rien. On ne retombe PAS sur le shm ici : l'appelant affiche déjà le nom
     technique par ailleurs (colonne MXL, tag du flux) et une redite n'apprend rien.
     Retourne {value, inherited, level} — `level` ≠ demandé signale un repli. */
  function labelOf(shm, col) {
    if (!shm) return { value: '', inherited: false, level: null };
    shm = String(shm).replace(/^\/dev\/shm\//, '');
    var want = (col == null) ? _level : col;
    var got = valueAt(shm, want);
    if (got.value) return { value: got.value, inherited: got.inherited, level: want };
    // Repli sur les seuls niveaux SAISIS (2..9). On ne retombe pas sur le hostname ni sur le shm :
    // le nom technique est déjà affiché ailleurs par les pages, et le faire remonter ici ferait
    // passer un repli pour un libellé. Ces deux niveaux ne s'affichent que si on les DEMANDE.
    for (var c = FIRST_STORED; c <= MAX_LEVEL; c++) {
      if (c === want) continue;
      var alt = resolve(shm, c);
      if (alt.value) return { value: alt.value, inherited: alt.inherited, level: c };
    }
    return { value: '', inherited: false, level: null };
  }

  /* Texte à AFFICHER dans une liste de sources (menu déroulant, tableau…).
     `technical` = ce que la page affichait avant (hostname · shm, « hôte → flux »…). On préfixe
     le libellé du niveau courant quand il en existe un, et on GARDE le nom technique derrière :
     dans un sélecteur, deux sources peuvent porter le même nom d'antenne, et l'opérateur doit
     pouvoir les départager. Aux niveaux 0/1 (hostname, MXL) on ne préfixe rien : c'est déjà ce
     que le texte technique dit. */
  function display(shm, technical) {
    technical = technical == null ? '' : String(technical);
    if (!shm || _level < FIRST_STORED) return technical;
    var lab = labelOf(shm);
    if (!lab.value) return technical;
    return technical ? (lab.value + ' · ' + technical) : lab.value;
  }

  function levelName(col) {
    var i = col == null ? _level : col;
    return _names[i] || ('Label ' + i);
  }

  function _emit() {
    document.dispatchEvent(new CustomEvent('source-labels:change', { detail: { level: _level } }));
  }

  function setLevel(col) {
    col = parseInt(col, 10);
    if (!(col >= MIN_LEVEL && col <= MAX_LEVEL) || col === _level) return;
    _level = col;
    try { window.localStorage.setItem(STORE_KEY, String(col)); } catch (e) { /* ignore */ }
    _sync();
    _emit();
  }

  function load(force) {
    if (_loaded && !force) return _loaded;
    _loaded = Promise.all([
      fetch('/api/labels').then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; }),
      fetch('/api/labels/names').then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; }),
      fetch('/api/labels/suffix_map').then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }),
      fetch('/api/sources').then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; })
    ]).then(function (res) {
      _rows = res[0] || []; _byShm = _index(_rows);
      _names = res[1] || []; _suffix = res[2] || {};
      _host = {};
      (res[3] || []).forEach(function (s) { if (s && s.shm) _host[s.shm] = s.hostname || ''; });
      _sync();
      // N'émettre QUE si le contenu a bougé : une page peut appeler refresh() en boucle (la page
      // Câbles rafraîchit toutes les 3 s pour suivre les saisies TSL), et re-rendre un tableau
      // identique le ferait clignoter sous les doigts de l'opérateur.
      var sig = JSON.stringify([_rows, _names, _suffix, _host]);
      if (sig !== _sig) { _sig = sig; _emit(); }
      return true;
    });
    return _loaded;
  }

  /* Sélecteur de la barre de navigation (présent sur toutes les pages via layout.html). */
  function _sync() {
    var sel = document.getElementById('nav-label-level');
    if (!sel) return;
    var opts = '';
    for (var c = MIN_LEVEL; c <= MAX_LEVEL; c++) {
      opts += '<option value="' + c + '"' + (c === _level ? ' selected' : '') + '>' +
              String(levelName(c)).replace(/[<>&]/g, '') + '</option>';
    }
    sel.innerHTML = opts;
    sel.value = String(_level);
  }

  window.SourceLabels = {
    get level() { return _level; },
    setLevel: setLevel,
    labelOf: labelOf,
    display: display,
    resolve: resolve,
    valueAt: valueAt,
    levelName: levelName,
    load: load,
    refresh: function () { return load(true); },
    rows: function () { return _rows; }
  };

  document.addEventListener('DOMContentLoaded', function () {
    var sel = document.getElementById('nav-label-level');
    if (sel) sel.addEventListener('change', function () { setLevel(sel.value); });
    load();
  });
})();


/* ═══════════════════════════════════════════════════════════════════════════════════════════
   TALLY DES SOURCES (TSL) — service GLOBAL, même esprit que SourceLabels ci-dessus.

   Le tally se résout en DEUX temps et aucune UI ne devrait refaire ce chemin à la main :
     1. mapping INVERSE shm → [{tsl_index, levels}]  (/api/tsl/mapping/by_shm) — le mapping n'est
        stocké que dans l'autre sens en base ;
     2. état des lampes par « <index>_<niveau> »      (/api/tally/state).
   Une lampe ne concerne une source que si l'index ET le niveau tombent dans la MÊME entrée de
   mapping : deux connexions peuvent employer le même index pour des sources différentes.

   NIVEAU affiché : un niveau est une ENTITÉ NOMMÉE (`tally_levels`), et son identifiant est
   celui-là même qui apparaît dans les clés d'état. Plus de bande de 3 à recouper : c'était la
   trame de TSL 5.0 (deux bits par champ, trois champs) recopiée dans le filtre d'une page qui
   n'émet pas de TSL. 0 = tous les niveaux, -1 = tally éteint.
   Le choix se fait dans la barre de navigation, à côté du niveau de libellé.

   Le sondage ne tourne QUE si quelqu'un l'a demandé (start/stop avec compteur) : inutile de
   réveiller le serveur toutes les 2 s sur une page qui n'affiche pas de tally.
   ═════════════════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var STORE_KEY = 'bobi.tally_level';
  /* Cadence du sondage de tally.
     ★ 500 ms, ET SEULEMENT SI L'ONGLET EST VISIBLE. Le tally est un état
     d'ANTENNE : 2 s de retard sur une pastille rouge, ce sont 2 s pendant
     lesquelles l'exploitant lit une information fausse. Mesuré AVANT de changer
     quoi que ce soit : `/api/tally/state` coûte 4,0 ms pour 146 octets, quand
     `/api/home/summary` — que la même page appelle toutes les 2 s — coûte
     114 ms. Passer ce sondage-ci à 500 ms ajoute ~12 ms/s par onglet, soit
     +19 % sur les 64 ms/s qu'un onglet coûte déjà. Meilleur rapport de la page.

     ⚠ LA GARDE DE VISIBILITÉ N'EST PAS UN LUXE. Ce module est chargé sur TOUTES
     les pages : sans elle, dix onglets oubliés en arrière-plan paient le rythme
     rapide en permanence, pour un affichage que personne ne regarde. C'est
     exactement la forme de sondage sans contre-pression qui a déjà saturé
     l'orchestrateur une fois. */
  var POLL_MS = 500;          // onglet au premier plan
  var POLL_MS_CACHE = 5000;   // onglet en arrière-plan : un filet, pas une cadence
  var COLORS = { red: '#cc2222', green: '#22aa44', amber: '#cc8800' };

  var _level = 0;                 // -1 = aucun (tally éteint) · 0 = tous · sinon l'UUID d'un niveau
  var _maps  = null;              // {shm: [{tsl_index, levels, connection_id, name}]}
  var _state = {};                // {"<index>_<niveau>": couleur}
  var _sig   = null;
  var _timer = null, _subs = 0, _visLie = false;

  try {
    // ⚠ PLUS UN ENTIER. Un niveau est identifié par un UUID depuis le 2026-09-01 ; `parseInt`
    // sur « 5074cc86-… » rend 5074, donc un filtre qui ne correspond à rien et une page sans
    // aucune pastille, sans erreur. Seuls -1 et 0 restent numériques (aucun / tous).
    var v = window.localStorage.getItem(STORE_KEY);
    if (v === '-1' || v === '0') _level = parseInt(v, 10);
    else if (v) _level = v;
  } catch (e) { /* localStorage indisponible → tous */ }


  function _emit() {
    document.dispatchEvent(new CustomEvent('source-tally:change', { detail: { level: _level } }));
  }

  /* Lampes ALLUMÉES d'une source : [[niveau, couleur], …]. Filtrées par le niveau choisi. */
  /* Lampes d'une source : l'état est indexé PAR SOURCE, on lit donc directement.

     ★ Le mapping TSL a disparu d'ici, et c'est le point. Cette fonction retrouvait l'index TSL
     de la source, puis balayait l'état à la recherche des clés portant cet index — si bien
     qu'une source sans correspondance TSL n'avait AUCUNE lampe, quel que soit son tally réel.
     Le filtre par `levels` du mapping ne servait qu'à départager deux porteurs employant le
     même index ; la collision n'existe plus, la clé nomme la source. */
  function lampsFor(shm) {
    if (_level < 0) return [];    // « Aucun » : tally éteint partout (aucune lampe, aucune couleur)
    if (!shm) return [];
    shm = String(shm).replace(/^\/dev\/shm\//, '');
    var seul = (_level && _level !== -1) ? String(_level) : null;
    var out = [];
    Object.keys(_state).forEach(function (k) {
      var c = _state[k];
      if (!c || c === 'off') return;
      /* Coupure au DERNIER souligné : le niveau est un UUID, donc sans souligné, alors qu'une
         référence de source peut parfaitement en contenir. */
      var cut = k.lastIndexOf('_');
      if (cut < 0) return;
      var ref = k.slice(0, cut), lvl = k.slice(cut + 1);
      if (String(ref) !== shm) return;
      if (seul && String(lvl) !== seul) return;
      out.push([lvl, c]);
    });
    return out;
  }

  /* Couleur DOMINANTE d'une source, ou null.
     ★ ROUGE **ET** VERT DONNENT ORANGE. Une source à la fois à l'antenne et en
     préparation est un état RÉEL et courant — c'est même souvent celui qu'on veut
     repérer le plus vite. Rendre « rouge » écrasait l'information : l'exploitant
     voyait l'antenne mais pas qu'elle était déjà armée sur une autre destination.
     L'ambre reste aussi une couleur que l'émetteur TSL peut envoyer directement
     (code 3) — les deux chemins convergent sur la même teinte, ce qui est voulu.
     Ordre du reste : ambre > rouge > vert. */
  function colorFor(shm) {
    var cols = lampsFor(shm).map(function (l) { return l[1]; });
    if (!cols.length) return null;
    var r = cols.indexOf('red') >= 0, v = cols.indexOf('green') >= 0;
    if (cols.indexOf('amber') >= 0 || (r && v)) return 'amber';
    if (r) return 'red';
    return v ? 'green' : null;
  }

  function color(name) { return COLORS[name] || name; }

  /* Niveaux proposables = ceux réellement présents dans le mapping. On les prend LÀ et pas dans
     /api/tally/levels : un niveau que rien n'adresse ne filtrerait rien, et allongerait le menu
     d'entrées qui ne changent jamais la page. */
  function levels() {
    var seen = {}, out = [];
    Object.keys(_maps || {}).forEach(function (shm) {
      (_maps[shm] || []).forEach(function (m) {
        (m.levels || []).forEach(function (n) {
          if (!n || seen[n]) return;
          seen[n] = 1;
          // Le LIBELLÉ est le nom de la connexion : c'est ce que l'exploitant reconnaît. La
          // valeur est l'UUID, qui ne s'affiche nulle part.
          out.push({ value: n, label: m.name || 'Niveau' });
        });
      });
    });
    out.sort(function (a, b) { return String(a.label).localeCompare(String(b.label)); });
    return out;
  }

  function setLevel(n) {
    // -1 (aucun) et 0 (tous) sont numériques ; tout le reste est un UUID de niveau.
    if (n === '-1' || n === -1) n = -1;
    else if (n === '0' || n === 0 || n === '' || n == null) n = 0;
    else n = String(n);
    if (n === _level) return;
    _level = n;
    try { window.localStorage.setItem(STORE_KEY, String(n)); } catch (e) { /* ignore */ }
    _sync();
    _emit();
  }

  function refresh() {
    var jobs = [fetch('/api/tally/state').then(function (r) { return r.ok ? r.json() : {}; })
                 .catch(function () { return {}; })];
    if (_maps === null) {
      jobs.push(fetch('/api/tsl/mapping/by_shm').then(function (r) { return r.ok ? r.json() : {}; })
                  .catch(function () { return {}; }));
    }
    return Promise.all(jobs).then(function (res) {
      _state = res[0] || {};
      if (res.length > 1) { _maps = res[1] || {}; _sync(); }
      var sig = JSON.stringify([_state, _level]);
      if (sig !== _sig) { _sig = sig; _emit(); }   // n'émettre que sur changement RÉEL
    });
  }

  function _periode() {
    return (document.visibilityState === 'hidden') ? POLL_MS_CACHE : POLL_MS;
  }

  function _relancer() {
    // Le rythme d'un MXLPoll est figé à sa création : pour en changer, on le
    // reconstruit. Appelé au seul changement de visibilité, jamais sur le
    // chemin chaud.
    if (!_timer) return;
    _timer.stop();
    _timer = window.MXLPoll(refresh, _periode());
  }

  function start() {
    _subs++;
    if (_timer) return;
    // Poll SANS RECOUVREMENT (window.MXLPoll, cf. layout.html) : ce module est chargé sur TOUTES
    // les pages, donc son rythme se paie partout. MXLPoll lance lui-même la 1re passe.
    _timer = window.MXLPoll(refresh, _periode());
    if (!_visLie) {
      // Un seul écouteur pour la vie de la page : start/stop s'enchaînent
      // (compteur d'abonnés), en poser un par appel les cumulerait.
      document.addEventListener('visibilitychange', _relancer);
      _visLie = true;
    }
  }
  function stop() {
    _subs = Math.max(0, _subs - 1);
    if (_subs === 0 && _timer) { _timer.stop(); _timer = null; }
  }

  function _sync() {
    var sel = document.getElementById('nav-tally-level');
    if (!sel) return;
    // « Aucun » d'abord : couper le tally est un choix d'exploitation à part entière (une régie
    // qui ne veut pas de couleurs à l'écran), pas l'absence de niveau — d'où une valeur dédiée.
    var opts = '<option value="-1">' + (window.t ? window.t('js.tally.none') : 'Aucun') + '</option>' +
               '<option value="0">' + (window.t ? window.t('js.tally.all_levels') : 'Tous') + '</option>';
    levels().forEach(function (l) {
      opts += '<option value="' + l.value + '"' + (l.value === _level ? ' selected' : '') + '>' +
              String(l.label).replace(/[<>&]/g, '') + '</option>';
    });
    sel.innerHTML = opts;
    sel.value = String(_level);
  }

  window.SourceTally = {
    get level() { return _level; },
    setLevel: setLevel,
    lampsFor: lampsFor,
    colorFor: colorFor,
    color: color,
    levels: levels,
    refresh: refresh,
    start: start,
    stop: stop
  };

  document.addEventListener('DOMContentLoaded', function () {
    var sel = document.getElementById('nav-tally-level');
    if (sel) sel.addEventListener('change', function () { setLevel(sel.value); });
    // Le sélecteur doit être peuplé même si aucune page n'a démarré le sondage.
    fetch('/api/tsl/mapping/by_shm').then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (j) { _maps = j || {}; _sync(); }).catch(function () { _maps = {}; });
  });
})();
