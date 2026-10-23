/* Catalogue de contrôles — partie JS.
 *
 * Le CSS ne suffit pas : un rotatif est un TRACÉ. Laisser chaque plugin dessiner le sien, ce serait
 * déplacer la duplication au lieu de la supprimer — cinq rotatifs privés, c'est précisément ce
 * qu'on corrige. Le dessin vit donc ici, à côté du style, et les plugins l'appellent.
 *
 * Chargé par layout.html sur toutes les pages, comme controls.css, APRÈS le moteur
 * (`bobi-controls.js`, objet `BobiControls`) auquel les rotatifs délèguent.
 * Inventaire vivant : Réglages → Contrôles.
 */
window.MXLControls = (function () {
  /* ROTATIF — le tracé, la variante, le bouton ↺ et le geste vivent dans le MOTEUR du catalogue
   * (`static/js/bobi-controls.js`, objet `BobiControls`), chargé juste avant ce fichier. Jusqu'au
   * 2026-10-13 il en existait deux implémentations — celle-ci et celle de BobiControls — qui
   * divergeaient à chaque amélioration (le cran magnétique avait été codé deux fois). Il n'en
   * reste qu'une ; les fonctions ci-dessous gardent leur NOM et leur CONTRAT pour les plugins.
   *
   * Contrat déclaratif (inchangé) :
   *     <span class="ctl-knob ctl-knob--arc"
   *           data-min data-max data-step data-val [data-def] [data-unit]>
   *       <… class="ctl-knob-hit" role="slider">…</…>
   *       <span class="ctl-knob-val">…</span>
   *     </span>
   * → `ctl-knob-input` (detail.value) à chaque valeur, `knob._ctlPose(v, mode)`, `data-val` tenu
   * à jour, `ctl-knob-reset` pour la remise au défaut. Options AJOUTÉES, toutes facultatives :
   * `data-scale="log|db"`, `data-fine` (pas du réglage fin ; sans lui, Maj ralentit sans affiner),
   * `data-zero`, `data-detent="0"`, `data-push` (+ `data-gate`) pour le poussoir intégré
   * (`ctl-knob-gate`, detail.on). Cran magnétique au défaut (`data-def`) actif d'office.
   * Clavier : flèches, PgUp/PgDn, Début/Fin, Entrée = défaut, Espace = défaut ou poussoir,
   * Maj = réglage fin (au clavier, Maj faisait des pas ×5 : inversé pour s'aligner sur le glisser). */
  const E = () => window.BobiControls;
  function knobSvg(kind, v01, def01, ouvert, zero01) { return E().knobSvg(kind, v01, def01, ouvert, zero01); }
  /* Variante déclarée sur l'élément, 'arc' à défaut (contrat historique). */
  function knobKind(el) { return E().knobKind(el) || 'arc'; }
  /* Pose le ↺ sur tout rotatif qui n'en a pas, sous `racine`. Idempotent. */
  function attachKnobs(racine, libelle) {
    (racine || document).querySelectorAll('.ctl-knob').forEach(function (knob) { E().ensureKnobReset(knob, libelle); });
  }
  /* Le geste du rotatif — pointeur, molette, clavier, poussoir, cran. Idempotent. */
  function attachKnobGestures(racine, libelleReset) {
    (racine || document).querySelectorAll('.ctl-knob').forEach(function (knob) { E().adoptKnob(knob, libelleReset); });
  }

  /* Pictos de disposition (16×16, `currentColor`) — aligner, dimensionner, distribuer, copier.
   * Ils vivaient dans split ; le composer multiview et l'éditeur de PiP font le même geste et
   * auraient redessiné les mêmes tracés. Un picto redessiné, c'est un picto qui DIVERGE. */
  const ICONS = {
        align_left: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="4" y="3" width="9.5" height="3.6" rx="1"/><rect x="4" y="9.4" width="6" height="3.6" rx="1"/></svg>',
        align_hcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="12" height="3.6" rx="1"/><rect x="4" y="9.4" width="8" height="3.6" rx="1"/><rect x="7.2" y="1" width="1.6" height="14" rx="0.8"/></svg>',
        align_right: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="2.5" y="3" width="9.5" height="3.6" rx="1"/><rect x="6" y="9.4" width="6" height="3.6" rx="1"/></svg>',
        align_top: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="3" y="4" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="4" width="3.6" height="6" rx="1"/></svg>',
        align_vcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="3.6" height="12" rx="1"/><rect x="9.4" y="4" width="3.6" height="8" rx="1"/><rect x="1" y="7.2" width="14" height="1.6" rx="0.8"/></svg>',
        align_bottom: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="3" y="2.5" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="6" width="3.6" height="6" rx="1"/></svg>',
        size_w: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="10" height="4.5" rx="1"/><rect x="3" y="9.5" width="10" height="4.5" rx="1" fill-opacity="0.55"/></svg>',
        size_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="4.5" height="10" rx="1"/><rect x="9.5" y="3" width="4.5" height="10" rx="1" fill-opacity="0.55"/></svg>',
        size_both: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1.5" y="1.5" width="8" height="8" rx="1" fill-opacity="0.55"/><rect x="6.5" y="6.5" width="8" height="8" rx="1"/></svg>',
        distribute_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="6" y="4" width="4" height="8" rx="1"/></svg>',
        distribute_v: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="4" y="6" width="8" height="4" rx="1"/></svg>',
        copy_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="5.5" y="1.5" width="9" height="9" rx="1.5" fill-opacity="0.45"/><rect x="1.5" y="5.5" width="9" height="9" rx="1.5"/></svg>',
        reset_box: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1.2" y="2.2" width="13.6" height="11.6" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2.6 1.8"/><rect x="4.6" y="5.4" width="6.8" height="5.2" rx="1"/></svg>',
        paste_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="12" rx="1.5" fill-opacity="0.45"/><rect x="5.5" y="1" width="5" height="3" rx="1"/><rect x="5" y="6.5" width="6" height="6" rx="1"/></svg>',
  };

  /* Un groupe d'outils : intitulé + rangée de boutons-pictos.
   * `defs` = [[svg, titre, onclick, id?], …]. Le titre n'est pas facultatif : un bouton
   * purement graphique sans libellé accessible est muet pour qui ne voit pas l'icône. */
  function toolGroup(label, defs) {
    const grp = document.createElement('div');
    grp.className = 'ctl-toolgroup';
    const lab = document.createElement('span');
    lab.className = 'ctl-toolgroup-label';
    lab.setAttribute('aria-hidden', 'true');
    lab.textContent = label;
    grp.appendChild(lab);
    const row = document.createElement('div');
    row.className = 'ctl-toolrow';
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', label);
    defs.forEach(function (d) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ctl-tool';
      b.innerHTML = d[0];
      b.title = d[1];
      b.setAttribute('aria-label', d[1]);
      if (d[3]) b.id = d[3];
      b.onclick = d[2];
      row.appendChild(b);
    });
    grp.appendChild(row);
    return grp;
  }

  // ── Liste d'ADRESSES / RÉSEAUX ────────────────────────────────────────────────────────
  // Saisir « x.x.x.x/24, 192.168.1.5 » à la main dans un champ texte, c'est demander à
  // l'exploitant de connaître la notation CIDR ET de ne pas se tromper de virgule. Ici :
  // une adresse, un préfixe choisi dans une liste, un bouton — et chaque règle devient une
  // puce qu'on retire d'un clic.
  //
  // ★ LE PRÉFIXE EST UN CHAMP NUMÉRIQUE, pas une liste déroulante. Une liste de vingt-cinq
  // entrées pour un nombre qu'on connaît déjà oblige à chercher au lieu de taper, et 32 —
  // le cas de très loin le plus fréquent — s'y trouve noyé. Le champ arrive prérempli à 32 :
  // ajouter une machine seule ne demande alors qu'une adresse et un clic.
  //
  // Le « / » est écrit DEVANT, en dur : c'est de la notation, pas une valeur à saisir.
  //
  // ⚠ VALIDATION LOCALE, MAIS PAS D'AUTORITÉ. Le serveur revalide et NORMALISE : c'est lui
  // qui fait foi, et l'appelant doit reposer ce qu'il rend. Contrôler ici évite un
  // aller-retour pour une faute de frappe, rien de plus.
  //
  // Bornes par défaut 8..32, et ce n'est pas de la timidité : au-delà de /8 une liste
  // d'autorisation cesse d'autoriser quoi que ce soit, et /0 la vide entièrement en ayant
  // l'air d'une règle. Qui veut tout ouvrir efface la liste, il n'écrit pas /0.

  function _ipValide(txt) {
    const p = String(txt || '').trim().split('.');
    if (p.length !== 4) return false;
    return p.every(function (x) {
      return /^\d{1,3}$/.test(x) && Number(x) >= 0 && Number(x) <= 255;
    });
  }

  /** Préfixe SUGGÉRÉ par une adresse qui se termine par des zéros, ou 0 si rien à déduire.
   *
   *  « x.x.x.x » veut dire un /24 dans la tête de qui l'écrit, « x.x.x.x » un /16 : c'est
   *  la lecture qu'on en fait depuis toujours. On la propose donc au lieu de la faire taper.
   *
   *  ⚠ CE N'EST QU'UNE SUGGESTION, ET ELLE NE DOIT JAMAIS ÉCRASER UN CHOIX. Un /25 sur
   *  « x.x.x.x » est parfaitement légitime, et se le voir réécrire en /24 pendant qu'on
   *  finit de taper l'adresse serait insupportable — c'est l'appelant qui garde la main via
   *  le drapeau « l'utilisateur a touché au préfixe ».
   *
   *  ⚠ ON NE DÉDUIT RIEN D'UNE ADRESSE COMMENÇANT PAR 0. « 0.0.0.0 » n'est pas un réseau
   *  qu'on autorise, c'est l'absence de règle — le déduire en /8 donnerait une règle qui a
   *  l'air d'en être une. */
  function _prefixeDeduit(txt) {
    const p = String(txt || '').trim().split('.');
    if (p.length !== 4 || !p.every(function (x) {
      return /^\d{1,3}$/.test(x) && Number(x) <= 255;
    })) return 0;
    if (Number(p[0]) === 0) return 0;
    let zeros = 0;
    for (let i = 3; i >= 1 && Number(p[i]) === 0; i--) zeros++;
    return [0, 24, 16, 8][zeros] || 0;   // 0 zéro final → on laisse le champ tel quel (32)
  }

  function _regles(valeur) {
    return String(valeur || '').replace(/;/g, ',').split(',')
      .map(function (x) { return x.trim(); }).filter(Boolean);
  }

  /** Éditeur de liste d'adresses/réseaux.
   *  `hote` : élément d'accueil (vidé). `opts.value` : chaîne « a/b, c ». `opts.onchange(v)`
   *  reçoit la NOUVELLE chaîne. `opts.vide` : texte affiché quand la liste est vide. */
  function cidrList(hote, opts) {
    opts = opts || {};
    let regles = _regles(opts.value);
    const emettre = function () {
      if (typeof opts.onchange === 'function') opts.onchange(regles.join(', '));
    };
    const dessiner = function () {
      hote.innerHTML = '';
      hote.className = (hote.className || '') .replace(/\bctl-cidr\b/g, '').trim() + ' ctl-cidr';
      const puces = document.createElement('div');
      puces.className = 'ctl-cidr-puces';
      if (!regles.length) {
        const v = document.createElement('span');
        v.className = 'ctl-cidr-vide';
        v.textContent = opts.vide || 'sans restriction';
        puces.appendChild(v);
      }
      regles.forEach(function (r, i) {
        const p = document.createElement('span');
        p.className = 'ctl-cidr-puce';
        p.appendChild(document.createTextNode(r));
        const x = document.createElement('button');
        x.type = 'button'; x.className = 'ctl-cidr-x';
        x.textContent = '\u00d7';
        x.title = 'Retirer ' + r;
        x.setAttribute('aria-label', 'Retirer ' + r);
        x.onclick = function () { regles.splice(i, 1); dessiner(); emettre(); };
        p.appendChild(x);
        puces.appendChild(p);
      });
      hote.appendChild(puces);

      const ligne = document.createElement('div');
      ligne.className = 'ctl-cidr-ajout';
      const ip = document.createElement('input');
      ip.type = 'text'; ip.className = 'ctl-cidr-ip';
      // GABARIT, PAS EXEMPLE. Une adresse plausible en filigrane se lit comme une valeur
      // déjà là — le placeholder du produit est en italique atténué pour cette raison, mais
      // sur un champ d'adresse le doute reste. `x.x.x.x` ne peut être confondu avec rien.
      ip.placeholder = opts.placeholder || 'x.x.x.x';
      ip.inputMode = 'numeric';
      // Un champ sans étiquette n'est qu'une boîte pour un lecteur d'écran. Il n'y a pas la
      // place d'un `<label>` visible sur une ligne de tableau : `aria-label` est la réponse.
      ip.setAttribute('aria-label', opts.labelIp || 'Adresse à autoriser');
      const souci = document.createElement('p');
      souci.className = 'ctl-cidr-souci';
      souci.hidden = true;
      souci.setAttribute('role', 'alert');
      const slash = document.createElement('span');
      slash.className = 'ctl-cidr-slash';
      slash.textContent = '/';
      slash.setAttribute('aria-hidden', 'true');
      const pref = document.createElement('input');
      // ⚠ `no-stepper` — ET C'EST UNE DÉROGATION ASSUMÉE À LA RÈGLE DU PRODUIT. Le stepper
      // global enrobe tout `<input type="number">` de deux boutons de 30 px : pour un nombre
      // à DEUX chiffres, dans une cellule de tableau déjà serrée, ça faisait 106 px pour
      // afficher « 32 ». Et un préfixe réseau ne se règle pas par incréments — on le SAIT et
      // on le tape, contrairement à un gain ou un nombre d'entrées, pour lesquels le stepper
      // a été créé. Demandé par l'utilisateur, et la raison tient.
      pref.type = 'number'; pref.className = 'ctl-cidr-pref no-stepper';
      pref.min = String(opts.prefixeMin == null ? 8 : opts.prefixeMin);
      pref.max = String(opts.prefixeMax == null ? 32 : opts.prefixeMax);
      pref.step = '1';
      pref.value = String(opts.prefixeDefaut == null ? 32 : opts.prefixeDefaut);
      // Étiquette EXPLICITE : « 32 » seul, hors contexte visuel, ne dit rien à un lecteur
      // d'écran — et le « / » qui le précède est décoratif, donc masqué.
      pref.setAttribute('aria-label', opts.labelPrefixe || 'Longueur du préfixe réseau');
      const add = document.createElement('button');
      add.type = 'button'; add.className = 'btn btn-sm';
      add.textContent = opts.ajouter || 'Ajouter';
      const refuser = function (msg) {
        ip.setAttribute('aria-invalid', 'true');
        souci.textContent = msg;
        souci.hidden = false;
        ip.focus();
      };
      const accepter = function () {
        ip.removeAttribute('aria-invalid');
        souci.hidden = true;
        souci.textContent = '';
      };
      const poser = function () {
        const v = ip.value.trim();
        if (!v) {
          refuser(opts.souciVide || 'Saisissez une adresse.');
          return;
        }
        if (!_ipValide(v)) {
          // Le message dit CE QUI EST ATTENDU, pas « invalide » : on corrige une saisie, on
          // ne devine pas une règle.
          refuser(opts.souciFormat || 'Adresse attendue sous la forme x.x.x.x');
          return;
        }
        accepter();
        const nP = Number(pref.value);
        const minP = Number(pref.min), maxP = Number(pref.max);
        if (!Number.isInteger(nP) || nP < minP || nP > maxP) {
          refuser((opts.souciPrefixe || 'Préfixe attendu entre {min} et {max}')
                    .replace('{min}', minP).replace('{max}', maxP));
          pref.focus();
          return;
        }
        // Une machine seule s'écrit sans préfixe : « x.x.x.x » se lit, « x.x.x.x/32 »
        // se déchiffre. Le serveur accepte les deux.
        const r = nP === 32 ? v : (v + '/' + nP);
        if (regles.indexOf(r) < 0) regles.push(r);
        ip.value = '';
        dessiner(); emettre();
      };
      add.onclick = poser;
      const surEntree = function (e) {
        if (e.key === 'Enter') { e.preventDefault(); poser(); }
      };
      ip.onkeydown = surEntree;
      pref.onkeydown = surEntree;
      // ★ UN CIDR COLLÉ SE RÉPARTIT TOUT SEUL. On copie « 10.0.0.0/8 » depuis une doc ou un
      // ticket, on le colle, et le champ le refusait au motif que ce n'est pas une adresse.
      // Refuser ce que l'utilisateur a EXACTEMENT voulu dire est la pire forme de validation :
      // on découpe et on remplit les deux champs.
      // ★ « L'UTILISATEUR A CHOISI » EST UN ÉTAT, et c'est lui qui rend la déduction
      // supportable. Sans ce drapeau, taper « x.x.x.x » puis corriger le préfixe en /25
      // verrait le /25 réécrit en /24 à la frappe suivante dans le champ d'adresse.
      // Il retombe à faux à chaque règle ajoutée : la ligne repart neuve.
      let prefChoisi = false;
      pref.oninput = function () { prefChoisi = true; };
      ip.oninput = function () {
        const v = ip.value;
        const i = v.indexOf('/');
        if (i > 0) {
          const n = parseInt(v.slice(i + 1), 10);
          ip.value = v.slice(0, i).trim();
          if (Number.isInteger(n)) { pref.value = String(n); prefChoisi = true; }
        } else if (!prefChoisi) {
          const d = _prefixeDeduit(ip.value);
          pref.value = String(d || (opts.prefixeDefaut == null ? 32 : opts.prefixeDefaut));
        }
        accepter();
      };
      ligne.appendChild(ip); ligne.appendChild(slash); ligne.appendChild(pref);
      ligne.appendChild(add);
      ligne.appendChild(souci);
      hote.appendChild(ligne);
    };
    dessiner();
    return {
      value: function () { return regles.join(', '); },
      set: function (v) { regles = _regles(v); dessiner(); }
    };
  }

  // ── CHOIX MULTIPLE dans une longue liste ──────────────────────────────────────────────
  // Une liste déroulante pour AJOUTER, et ce qui est choisi s'affiche dessous en puces qu'on
  // retire d'un clic.
  //
  // ★ POURQUOI PAS UN `<select multiple>`. Il tient tant qu'il y a quatre entrées ; à vingt il
  //   devient inutilisable — il faut faire défiler une boîte de quatre lignes, et le
  //   Ctrl+clic pour désélectionner ne s'invente pas. Surtout, on ne voit pas ce qu'on a
  //   choisi sans parcourir toute la liste : la sélection est DANS la liste au lieu d'être à
  //   côté. Ici les deux sont séparés — la liste sert à ajouter, les puces disent l'état.
  //
  // ★ CE QUI EST DÉJÀ CHOISI SORT DE LA LISTE. Le proposer à nouveau invite à un doublon que
  //   le contrôle refuserait en silence, et allonge le menu de ce qu'on a déjà réglé.
  //
  // ★ L'ORDRE DES PUCES EST CELUI DES CHOIX, pas celui de la liste. C'est l'ordre dans lequel
  //   on a travaillé, et le rétablir en ordre de liste ferait sauter la dernière puce ajoutée
  //   ailleurs qu'à l'endroit où on regarde.
  //
  // `hote` : l'élément d'accueil. `opts` :
  //   options   [{value, label}] — la liste complète
  //   valeurs   [value]          — la sélection de départ
  //   onChange  fn(valeurs)      — appelée à chaque ajout ou retrait
  //   vide      texte affiché quand rien n'est choisi
  //   ajouter   libellé de l'entrée neutre du menu
  function chooseList(hote, opts) {
    opts = opts || {};
    var choix = (opts.valeurs || []).map(String);
    var api;

    function libelle(v) {
      for (var i = 0; i < (opts.options || []).length; i++) {
        if (String(opts.options[i].value) === String(v)) return opts.options[i].label;
      }
      // ⚠ UNE VALEUR QUI N'EST PLUS DANS LA LISTE RESTE AFFICHÉE, et signalée. La masquer la
      // ferait disparaître au premier enregistrement sans que personne l'ait demandé — or
      // c'est justement le cas qui mérite un regard : le niveau a été supprimé ailleurs.
      return String(v) + ' ?';
    }
    function connue(v) {
      return (opts.options || []).some(function (o) { return String(o.value) === String(v); });
    }

    function dessiner() {
      hote.textContent = '';
      hote.className = (hote.className || '').replace(/\bctl-choix\b/g, '').trim() + ' ctl-choix';

      var puces = document.createElement('div');
      puces.className = 'ctl-choix-puces';
      if (!choix.length) {
        var vide = document.createElement('span');
        vide.className = 'ctl-choix-vide';
        vide.textContent = opts.vide || '— aucun —';
        puces.appendChild(vide);
      }
      choix.forEach(function (v) {
        var p = document.createElement('span');
        p.className = 'ctl-choix-puce' + (connue(v) ? '' : ' ctl-choix-inconnue');
        p.appendChild(document.createTextNode(libelle(v)));
        var x = document.createElement('button');
        x.type = 'button';
        x.className = 'ctl-choix-x';
        x.setAttribute('aria-label', (opts.retirer || 'Retirer') + ' ' + libelle(v));
        x.textContent = '×';
        x.onclick = function (ev) {
          // ⚠ ON REFUSE UN CLIC QU'ON N'A PAS REÇU. Déposé dans un <label> — ce qui arrive, et
          // c'est arrivé — le contrôle voit TOUT clic de l'étiquette renvoyé vers son premier
          // bouton, donc vers la croix de la première puce : cliquer à côté d'une puce en
          // supprimait une autre. Un contrôle de catalogue doit tenir partout où on le dépose,
          // sans supposer le balisage d'accueil.
          //
          // ⚠ ET `ev.detail === 0` NE SUFFIT PAS À DISTINGUER : une activation au CLAVIER
          // (Entrée, Espace) vaut aussi 0, et la refuser rendrait le contrôle inutilisable sans
          // souris. Ce qui sépare les deux, c'est le FOCUS : au clavier, le bouton activé est
          // celui qui a le focus ; le clic renvoyé par un <label> ne l'a pas.
          if (ev && ev.detail === 0 && typeof document !== "undefined"
              && document.activeElement !== x) return;
          if (ev) { ev.preventDefault(); ev.stopPropagation(); }
          choix = choix.filter(function (c) { return c !== v; });
          dessiner();
          if (opts.onChange) opts.onChange(choix.slice());
        };
        p.appendChild(x);
        puces.appendChild(p);
      });
      hote.appendChild(puces);

      var restants = (opts.options || []).filter(function (o) {
        return choix.indexOf(String(o.value)) < 0;
      });
      var sel = document.createElement('select');
      sel.className = 'ctl-choix-select';
      var neutre = document.createElement('option');
      neutre.value = '';
      neutre.textContent = restants.length ? (opts.ajouter || '+ Ajouter…')
                                           : (opts.tout || 'tout est choisi');
      sel.appendChild(neutre);
      restants.forEach(function (o) {
        var e = document.createElement('option');
        e.value = String(o.value);
        e.textContent = o.label;
        sel.appendChild(e);
      });
      sel.disabled = !restants.length;
      // `change` et non `click` : le menu doit rester utilisable au clavier, et un ajout ne se
      // déclenche qu'une fois la valeur choisie.
      sel.onchange = function () {
        if (!sel.value) return;
        choix.push(String(sel.value));
        dessiner();
        if (opts.onChange) opts.onChange(choix.slice());
      };
      hote.appendChild(sel);
    }

    dessiner();
    api = {
      value: function () { return choix.slice(); },
      set: function (v) { choix = (v || []).map(String); dessiner(); },
      options: function (liste) { opts.options = liste || []; dessiner(); }
    };
    return api;
  }

  return { knobSvg: knobSvg, knobKind: knobKind, attachKnobs: attachKnobs,
           attachKnobGestures: attachKnobGestures,
           ICONS: ICONS, toolGroup: toolGroup, cidrList: cidrList,
           chooseList: chooseList };
})();
