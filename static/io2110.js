/* SPDX-License-Identifier: GPL-3.0-or-later
 * Vues I/O 2110 du moteur MTL — onglets custom « Sources 2110 » / « Destinations 2110 » de /io.
 * Gestion du TRANSPORT 2110 (multicasts, 2022-7, audio, ANC) hors page Câbles (recentrée sur le MXL).
 * - Sources 2110 RÉUTILISE la carte de contrôle riche du plugin 2110_io (couleurs + GÉN/IDENT/SDP).
 * - Destinations 2110 : une CARTE par slot TX, une section colorée par ESSENCE (vidéo/audio/ANC),
 *   chacune avec SON multicast (leg0 + leg1 2022-7) éditable. Repli global par moteur.
 * Données : /api/io/mtl.
 */
(function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const RXTYPE = '2110_io';

  /* i18n : `window.t` rend la CLÉ BRUTE quand elle manque du catalogue. On teste le retour et on
   * retombe sur le français passé en 2e argument — un catalogue incomplet doit donner du français,
   * jamais « js.io2110.tx_unwired » à l'écran (vécu le 2026-08-21 : catalogues écrasés). */
  const T = (k, repli) => {
    const v = window.t ? window.t(k) : k;
    return (v && v !== k) ? v : (repli !== undefined ? repli : k);
  };

  /* Le lecteur de flux (cadence, état) est un fichier partagé, chargé par layout.html. S'il
   * manque — gabarit servi depuis le cache après un ajout, fichier non déployé — la vue entière
   * s'arrêtait sur la première cellule. Un point commun ne doit pas pouvoir emporter tout ce qui
   * s'appuie dessus : à défaut, on rend une cellule qui DIT qu'elle est dégradée (jamais un
   * repli discret qui ferait croire à un affichage normal) et le reste de la ligne survit. */
  const FLUX = () => window.IOFlux || {
    circule: o => Number(o && o.fps) > 0,
    cadence: () => '<span style="color:var(--status-warning-fg)" title="' + esc(T('js.io2110.flux_reader_missing_val', 'Lecteur de flux non chargé (io_flux.js) : cette valeur est indisponible, pas nulle.')) + '">?</span>',
    badge: (t, l) => '<span class="badge" title="' + esc(T('js.io2110.flux_reader_missing', 'Lecteur de flux non chargé (io_flux.js).')) + '">' + l + '</span>',
  };

  const STYLE = `<style>
    /* ── Destinations 2110 — styles étendus ──────────────────────────── */
    .io2110-engine{margin:0 0 22px}
    /* Moteur visé par un deep-link « #sources_2110/<vmid> » (raccourci de la page Câbles) :
       les moteurs sont empilés sans sélection, il faut un repère visuel qui survive au refresh 3 s. */
    .io2110-engine.io2110-engine-cible{box-shadow:0 0 0 2px var(--accent,#4a9eff);border-radius:8px;padding:8px 10px;margin-left:-10px;margin-right:-10px}
    .io2110-engine > h3{margin:6px 0 8px;font-size:1em;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
    .io2110-engine > h3 small{color:var(--text-muted);font-weight:normal}

    /* Repli global par moteur */
    .io2110-repli{display:flex;align-items:center;gap:5px;margin-left:auto}
    .io2110-repli-lbl{font-size:0.78em;color:var(--text-muted);white-space:nowrap}
    .io2110-repli-sel{font-size:0.78em;padding:2px 6px;border-radius:4px;
      border:1px solid var(--border);background:var(--bg-input);color:var(--text);cursor:pointer}
    .io2110-repli-sel:focus{outline:none;border-color:var(--accent,#7aa2c8)}

    /* Bandeau de sûreté du moteur (mode du port + budget RL + layout + bac de maintenance) */
    .io2110-safety{margin:2px 0 10px}
    .io2110-safety .nic-bar-wrap{margin:2px 0}

    /* Carte slot TX */
    .io2110-txcard{border:1px solid var(--border-soft);border-radius:8px;padding:8px 12px;
      margin:6px 0;background:var(--surface,rgba(127,127,127,.04))}

    /* En-tête du slot */
    .io2110-slothdr{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap}
    .io2110-slotnum{font-weight:700;font-size:0.85em;min-width:36px}
    .io2110-shmin{font-family:var(--font-mono,monospace);font-size:0.8em;color:var(--text-muted);
      flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .io2110-fpsbadge{font-size:0.75em;font-weight:700;padding:2px 6px;border-radius:4px;
      font-family:var(--font-mono,monospace);white-space:nowrap;flex:none}
    .io2110-fpsbadge.ok  {background:rgba(52,211,153,.13);color:#22c55e}
    .io2110-fpsbadge.warn{background:rgba(234,179,8,.13);color:#eab308}
    .io2110-fpsbadge.off {background:rgba(127,127,127,.1);color:var(--text-muted)}

    /* La ligne d'essence utilise le gabarit PARTAGÉ «.io-flow» (static/css/base.css), commun
       aux réceptions et aux émissions. Ne restent ici que les pièces propres aux sorties. */
    /* Ressource NMOS liée : le TON la distingue, pas la taille — un 0.82em dans une cellule
       déjà en 0.82em se multipliait, et le nom finissait plus petit que l'adresse qu'il qualifie. */
    .io2110-bound{color:#7aa2f7;font-weight:600;white-space:nowrap}
    .io2110-gencell{min-width:86px;display:inline-flex;align-items:center}
    .io2110-identcell{min-width:112px;display:inline-flex;align-items:center;gap:5px}
    .io2110-sdpcell{min-width:64px;display:inline-flex;align-items:center}
    .io2110-flow.video{background:rgba(96,165,250,.08);border-left:3px solid #60a5fa}
    .io2110-flow.audio{background:rgba(52,211,153,.07);border-left:3px solid #34d399}
    .io2110-flow.anc  {background:rgba(209,134,22,.08);border-left:3px solid #d18616}
    /* Les boutons d'édition sont des «.btn .btn-sm» ; il leur reste à ne pas se laisser
       comprimer par le conteneur souple qui les entoure. */
    .io2110-editbtn{flex:none}

    /* ── Contrôles migrés au CATALOGUE le 2026-08-05 ──────────────────────────
     * GEN / IDENT (gestes à accrochage)      → .ctl-push .ctl-push--led
     * TONE / SDP  (ils OUVRENT une fenêtre)  → .btn .btn-sm + .ctl-led
     * taille IDENT (était un prompt() !)     → .ctl-knob .ctl-knob--arc
     * listes format / rythme / repli         → .ctl-select
     * barres de charge                        → .ctl-gauge
     * canaux de tonalité                      → .ctl-strips / .ctl-strip
     * métriques de tableau                    → contexte .ctl-dense
     * Les définitions privées correspondantes sont retirées EN ENTIER : laissées ici, elles
     * seraient injectées APRÈS le socle et le recouvriraient sans bruit — cette feuille est
     * posée à l'exécution, elle gagne toujours à spécificité égale. */
    .io2110-tone-modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;
      display:flex;align-items:center;justify-content:center}
    .io2110-tone-box{background:var(--surface,#1b1b1f);border:1px solid var(--border);border-radius:10px;
      padding:18px 20px;min-width:340px;max-width:92vw;box-shadow:0 10px 40px rgba(0,0,0,.5)}
    .io2110-tone-box h4{margin:0 0 12px;font-size:1.02em}
    .io2110-tone-box .tt-row{display:flex;align-items:center;gap:8px;margin:8px 0;font-size:0.9em}
    .io2110-tone-box .tt-row label{flex:none;width:90px;color:var(--text-muted)}
    .io2110-tone-box input[type=number]{width:90px;padding:3px 6px;border:1px solid var(--border);
      border-radius:4px;background:var(--bg,#111);color:var(--text)}
    /* Canaux de tonalité : «.ctl-strips» / «.ctl-strip» du catalogue (une colonne par canal, le
       filet d'1 px EST la séparation). Ne reste ici que le nombre de colonnes. */
    .io2110-tone-chans{grid-template-columns:repeat(8,1fr);margin:10px 0}
    .io2110-tone-legend{font-size:0.8em;color:var(--text-muted);margin:4px 0 12px}
    .io2110-tone-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}
    /* Le bouton SDP porte sa LED avant son libellé («.btn» n'est pas un conteneur flex). */
    .io2110-sdp{display:inline-flex;align-items:center;gap:5px;flex:none;text-decoration:none}
    .io2110-fmtsel{flex:none;max-width:160px}
    /* Heure de programmation : un champ, pas une liste → il n'emprunte plus l'habillage du
       sélecteur, qui lui dessinait une flèche de menu déroulant sur un champ sans options. */
    .io2110-timein{font-size:0.78em;padding:2px 6px;border-radius:var(--radius-small,4px);
      border:1px solid var(--border);background:var(--bg-input);color:var(--text);flex:none}
    /* Le bouton d'action sur liste est au socle : «.io-addrow» (base.css). */

    /* Socle DPDK (RL) : les +/- TX sont gouvernés par le MODÈLE de carte → note de renvoi. */
    .io2110-txmodel-note{margin:6px 0;padding:6px 8px;font-size:0.8em;line-height:1.4;
      color:var(--text-muted);border:1px dashed var(--border);border-radius:6px;
      background:var(--accent-soft,rgba(122,162,200,.10))}
    .io2110-txmodel-note a{color:var(--accent,#7aa2c8);white-space:nowrap}

    /* « Option A » : flux composables — retrait granulaire + ajout par destination.
       Les boutons ✕ et + sont des COMMANDES du socle («.btn .btn-sm») : il ne reste ici que
       leur placement dans la ligne. */
    .io2110-flowrow{display:flex;align-items:center;gap:4px}
    .io2110-flowrow > :first-child{flex:1 1 auto}
    .io2110-flowctrls{display:flex;align-items:center;gap:6px;margin:5px 0 2px;flex-wrap:wrap}
    .io2110-rmgrp{margin-left:auto}

    /* Barre utilisation NIC — la BARRE vient du catalogue («.ctl-gauge») ; ne restent ici que la
       rangée et ses libellés. */
    .nic-bar-wrap{display:flex;align-items:center;gap:6px;margin:3px 0 6px;font-size:0.82em;line-height:1.3}
    .nic-bar-lbl{color:var(--text-muted);width:72px;flex-shrink:0;font-weight:600;white-space:nowrap}
    .nic-bar-val{min-width:160px;font-variant-numeric:tabular-nums}
    /* Barre « Queues XDP » multi-segments : réellement singulière (live / planifié / réservé /
       au-delà du plafond + repère), aucun équivalent au catalogue → elle reste privée. */
    .nic-xdp-track{flex:1;height:8px;background:var(--border);border-radius:4px;position:relative;overflow:hidden}
    .nic-xdp-fill{height:100%;border-radius:4px;transition:width .6s}
    .nic-xdp-active{position:absolute;top:0;bottom:0;left:0;border-radius:4px 0 0 4px;transition:width .5s}
    .nic-xdp-pending{position:absolute;top:0;bottom:0;opacity:.5;transition:left .5s,width .5s;background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.6) 0 3px,transparent 3px 7px)}
    .nic-xdp-over{position:absolute;top:0;bottom:0;opacity:.7;transition:left .5s,width .5s;background-color:#e8a33d;background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.6) 0 3px,transparent 3px 7px)}
    .nic-xdp-free{position:absolute;top:0;bottom:0;background:var(--text-muted);opacity:.15;transition:left .5s,width .5s}
    .nic-xdp-mark{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 0 1px rgba(0,0,0,.5)}
    .nic-bar-est{font-style:italic;color:var(--text-muted)}
    .nic-model-lbl{font-size:0.76em;color:var(--text-muted);margin:2px 0 1px}
    .nic-shared{color:#e8a33d}
    .io2110-bound{color:#7aa2f7;font-size:0.82em;font-weight:600}

    /* ── Multi-NIC : bande de ports + détail par NIC ─────────────────── */
    .io2110-nicbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 8px}
    .io2110-portstrip{display:flex;gap:8px;flex-wrap:wrap;flex:1;min-width:0}
    .io2110-portchip{display:flex;flex-direction:column;gap:2px;min-width:148px;
      padding:5px 9px;border-radius:7px;border:1px solid var(--border-soft);
      border-left-width:3px;background:var(--surface,rgba(127,127,127,.04));font-size:0.8em}
    .io2110-portchip.down{opacity:.55}
    .pc-top{display:flex;align-items:center;gap:6px}
    .pc-name{font-weight:700;font-family:var(--font-mono,monospace)}
    .pc-net{font-size:0.82em;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .pc-prim{font-size:0.62em;font-weight:700;letter-spacing:.04em;color:#e8a33d;flex:none}
    .pc-down{font-size:0.7em;color:var(--status-stopped-fg,#f87171);flex:none}
    .pc-load{display:flex;align-items:center;gap:5px}
    .pc-loadval{font-variant-numeric:tabular-nums;min-width:78px}
    .pc-loadval.est{font-style:italic;color:var(--text-muted)}
    .pc-meta{color:var(--text-muted);font-size:0.92em}
    .io2110-portdetail{display:flex;flex-direction:column;gap:10px;margin:2px 0 10px}
    .io2110-portcard{border:1px solid var(--border-soft);border-left-width:3px;border-radius:8px;
      padding:8px 11px;background:var(--surface,rgba(127,127,127,.04))}
    .io2110-portcard h5{margin:0 0 6px;font-size:0.85em;display:flex;align-items:center;gap:6px}
    .pc-ptp{font-size:0.64em;font-weight:700;letter-spacing:.03em;padding:1px 5px;border-radius:3px;
      border:1px solid;flex:none}
    .io2110-portbadge{display:inline-flex;align-items:center;gap:3px;font-size:0.68em;font-weight:700;
      font-family:var(--font-mono,monospace);padding:2px 6px;border-radius:4px;flex:none;
      border:1px solid transparent;border-left-width:3px;background:rgba(127,127,127,.1)}
  </style>`;

  // La pagination est pilotée côté serveur (active_tx_count dans deploy_config) :
  // e.tx = slots actifs dans NMOS, e.tx_count = capacité totale déployée.

  async function _fetch() {
    try { return await (await fetch('/api/io/mtl')).json(); }
    catch (e) { return { engines: [] }; }
  }

  function _ensureAsset(kind) {
    const id = 'plugin-' + kind + '-' + RXTYPE;
    if (document.getElementById(id)) return Promise.resolve();
    return new Promise(res => {
      let el;
      if (kind === 'js') { el = document.createElement('script'); el.src = '/api/plugins/' + RXTYPE + '/ui/js'; }
      else { el = document.createElement('link'); el.rel = 'stylesheet'; el.href = '/api/plugins/' + RXTYPE + '/ui/css'; }
      el.id = id; el.onload = () => res(); el.onerror = () => res();
      document.head.appendChild(el);
    });
  }

  const _head = e => '<h3>' + esc(e.hostname) + ' <small>· ' + esc(e.node || '') + ' · #' + e.vmid + '</small></h3>';

  // ── Moteur visé par un deep-link (« /io#sources_2110/<vmid> », raccourci de la page Câbles) ──
  // Les moteurs sont empilés sans notion de sélection : on se contente de surligner + scroller.
  let _cible = null, _cibleScrolled = false;
  function _viser(vmid) {
    const v = (vmid == null ? null : +vmid);
    if (v !== _cible) { _cible = v; _cibleScrolled = false; }
  }
  // À rejouer APRÈS chaque rendu : les Destinations reconstruisent tout l'innerHTML toutes les 3 s,
  // le surlignage disparaîtrait sinon. Le scroll, lui, ne se fait qu'UNE fois (sinon la page
  // re-recentre en boucle sous les yeux de l'utilisateur).
  function _marquerCible(root) {
    if (!root) return;
    root.querySelectorAll('.io2110-engine.io2110-engine-cible')
        .forEach(x => x.classList.remove('io2110-engine-cible'));
    if (_cible == null) return;
    const el = root.querySelector('.io2110-engine[data-vmid="' + _cible + '"]');
    if (!el) return;   // moteur absent de cette vue : pas d'erreur, on ne surligne rien
    el.classList.add('io2110-engine-cible');
    if (!_cibleScrolled) { _cibleScrolled = true; try { el.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch (_) { el.scrollIntoView(); } }
  }

  // ── Sources 2110 : une carte de contrôle plugin (riche) par moteur ──────────
  async function mountSources(el, vmid) {
    _viser(vmid);
    const d = await _fetch();
    const engines = d.engines || [];
    if (!engines.length) { el.innerHTML = '<div class="meta">' + esc(T('js.io2110.no_engine', 'Aucun moteur MTL déployé.')) + '</div>'; return; }
    await _ensureAsset('css'); await _ensureAsset('js');
    let html = '';
    try { html = await (await fetch('/api/plugins/' + RXTYPE + '/ui/html')).text(); } catch (_) {}
    el.innerHTML = STYLE;
    const api = window.MXLPlugins && window.MXLPlugins[RXTYPE];
    for (const e of engines) {
      const wrap = document.createElement('div'); wrap.className = 'io2110-engine';
      wrap.id = 'io2110-eng-' + e.vmid; wrap.dataset.vmid = e.vmid;
      wrap.innerHTML = _head(e);
      const card = document.createElement('div'); card.innerHTML = html;
      wrap.appendChild(card); el.appendChild(wrap);
      if (api && typeof api.mount === 'function') {
        try { api.mount(card, e.vmid, { toast: (window.tpToast || function () {}), type: RXTYPE }); }
        catch (err) { console.error(err); }
      }
    }
    _sEl = el;
    _marquerCible(el);
  }

  // ── Destinations 2110 ────────────────────────────────────────────────────────
  let _dTimer = null, _dEl = null, _sEl = null;
  // Sortie dont le format est en cours d'édition (« vmid:slot »), ou null. Gardé ICI et non dans
  // le DOM : la vue se reconstruit toutes les 3 s, et un état posé sur l'élément disparaîtrait
  // avec lui — la liste se refermerait toute seule pendant qu'on choisit.
  let _fmtEdit = null;

  // « Option A » : enrobe une ligne de flux audio/ANC d'un bouton de retrait granulaire.
  function _txRm(html, vmid, fid) {
    return '<div class="io2110-flowrow">' + html
      + (fid ? `<button type="button" class="btn btn-sm" onclick="IO2110.removeFlow(${vmid},'${esc(fid)}')" title="${esc(T('js.io2110.rm_flow_tip', 'Retirer ce flux'))}">✕</button>` : '')
      + '</div>';
  }

  // Générateur, IDENT et SDP d'une LIGNE D'ESSENCE — alignés sur la carte Sources (2026-08-05).
  // Ils vivaient dans l'en-tête du slot, ce qui était faux de trois façons :
  //   · GEN produit la MIRE VIDÉO, et le générateur audio du même slot (TONE) était, lui, sur sa
  //     ligne : le TX se contredisait tout seul ;
  //   · IDENT est une incrustation dans l'IMAGE — proposé sur un slot audio-seul, il ne veut rien
  //     dire ;
  //   · le SDP « du slot » ne servait que celui de la vidéo (cf. nmos_detail.py).
  // Chaque essence porte donc les siens, et les emplacements vides sont RÉSERVÉS pour que les
  // colonnes restent alignées d'une ligne à l'autre — même méthode qu'en Rx.
  function _txGen(vmid, t, kind, ess, audioIdx, fb) {
    if (kind === 'video') {
      // GEN a trois états, dont « repli » : ce n'est pas un demi-enfoncement, ça dit qu'AUTRE
      // CHOSE émet à sa place → poussoir relâché, et le libellé nomme la situation.
      const cls = _genBadgeClass(t, fb);
      // Pas de pictogramme : l'état allumé/éteint est porté par le poussoir lui-même (LED +
      // fond + aria-pressed), c'est son travail. Le libellé ne change que pour NOMMER une
      // situation que l'état ne dit pas — le repli, où c'est autre chose qui émet.
      // « GÉN » accentué, comme en réception : c'est le même mot.
      const lbl = (cls === 'repli') ? T('js.io2110.gen_fallback_btn', 'Repli')
                                    : T('js.io2110.gen_btn', 'GÉN');
      return `<span class="io-flow-gen"><button type="button" class="ctl-push ctl-push--led"
          role="switch" aria-pressed="${!!t.gen}" onclick="IO2110.toggleGen(${vmid},${t.slot})"
          title="${esc(cls === 'repli' ? T('js.io2110.gen_fallback_tip', 'Aucune source câblée : le repli du moteur émet à la place de cette sortie') : T('js.io2110.gen_bars_tip', 'Générateur de mire de cette sortie'))}"><span
          class="ctl-led"></span>${lbl}</button></span>`;
    }
    if (kind === 'audio' && audioIdx != null) {
      // Le générateur de l'essence AUDIO, à la place du générateur de l'essence VIDÉO. Il ouvre
      // son éditeur (fréquence, niveau, canaux) : c'est une commande, le témoin dit s'il tourne.
      const on = ess.tone && ess.tone.enabled;
      return `<span class="io-flow-gen"><button type="button" class="btn btn-sm io2110-sdp"
          aria-haspopup="dialog" onclick="IO2110.editTone(${vmid},${t.slot},${audioIdx})"
          title="${esc(T('js.io2110.tone_btn_tip', 'Générateur de tonalité (1 kHz/-18 dBFS, choix des canaux + ruptage) — {etat}')
            .replace('{etat}', on ? T('js.io2110.tone_on', 'actif') : T('js.io2110.tone_off', "à l'arrêt")))}"><span class="ctl-led${on ? ' on' : ''}"></span>TONE…</button></span>`;
    }
    return '<span class="io-flow-gen"></span>';   // ANC : rien à générer, place réservée
  }
  function _txIdent(vmid, t, kind) {
    if (kind !== 'video') return '<span class="io-flow-ident"></span>';
    const def = Math.max(10, Math.min(120, Math.max(12, Math.round((t.height || 1080) / 28))));
    const sz = Math.max(10, Math.min(120, t.ident_size || def));
    return `<span class="io-flow-ident"><button type="button" class="ctl-push ctl-push--led"
        role="switch" aria-pressed="${!!t.ident}" onclick="IO2110.toggleIdent(${vmid},${t.slot})"
        title="${esc(T('js.io2110.ident_tip', 'Incruster un IDENT (nom · destination · format) dans le flux 2110 émis'))}"><span
        class="ctl-led"></span>IDENT</button>${t.ident ? `<span class="ctl-knob ctl-knob--arc io2110-identknob"
        data-vmid="${vmid}" data-slot="${t.slot}" data-min="10" data-max="120" data-step="2"
        data-val="${sz}" data-def="${def}" data-unit="px">
        <button type="button" class="ctl-knob-hit" role="slider" aria-label="${esc(T('js.io2110.ident_size_aria', 'Taille du texte IDENT'))}"
          aria-valuemin="10" aria-valuemax="120" aria-valuenow="${sz}" aria-valuetext="${sz}px"
          title="${esc(T('js.io2110.ident_size_tip', 'Taille du texte IDENT — glisser ↕, molette, flèches. Entrée = taille automatique.'))}">${
          window.MXLControls ? window.MXLControls.knobSvg('arc', (sz - 10) / 110, (def - 10) / 110) : ''
        }</button><span class="ctl-knob-val">${sz}px</span></span>` : ''}</span>`;
  }
  // Format d'une essence, à la forme de celui d'une source. DÉCLARÉ, pas mesuré : en réception ces
  // valeurs sont lues dans le SDP reçu ; ici elles disent ce que le moteur crée. L'audio 2110-30
  // est aujourd'hui un modèle UNIQUE (48 kHz / L24 / 8 ch, cf. mtl_rx.c) — il vient du payload et
  // non d'une constante écrite ici, pour que la page suive le jour où il cesse d'être unique.
  // Le ptime, lui, varie réellement d'une sortie à l'autre : il n'est montré que s'il est connu.
  function _txFormat(kind, ess, t, vmid) {
    // VIDÉO : le sélecteur de format vit désormais ICI, sur la ligne de l'essence qu'il concerne,
    // comme le format lu d'une source. L'en-tête ne garde que ce qui vaut pour le slot entier.
    if (kind === 'video') return `<span class="io-flow-fmt">${_formatSelect(vmid, t)}</span>`;
    if (kind === 'anc') return '<span class="io-flow-fmt"></span>';
    if (!ess.sample_rate) return '<span class="io-flow-fmt"></span>';
    const khz = (ess.sample_rate / 1000).toString().replace(/\.0$/, '');
    const txt = `${khz}kHz / L${ess.bit_depth || 24} / ${ess.channels || 1}ch`;
    const pt = (ess.ptime != null) ? ` · ${ess.ptime} ms` : '';
    return `<span class="io-flow-fmt" title="${esc(T('js.io2110.audio_out_fmt_tip', 'Format annoncé pour cette sortie audio')
      + (pt ? T('js.io2110.audio_ptime', ' — paquetisation {ms} ms').replace('{ms}', ess.ptime) : ''))}">${esc(txt + pt)}</span>`;
  }

  // Chemin MXL d'ENTRÉE de l'essence — le pendant du chemin de sortie affiché par source en
  // réception. Sans lui, rien ne permettait de vérifier qu'une sortie audio lit bien le bus qu'on
  // croit : le câblage existe (`tx_audio{i}_shm`), il n'était simplement montré nulle part.
  function _txMxl(ess, kind) {
    const shm = ess.shm_in || '';
    return shm
      ? `<span class="io-flow-mxl" title="${esc(T('js.io2110.mxl_in_tip', 'nom du flux sur le bus MXL (entrée de cette sortie)'))}">${esc(shm)}</span>`
      : `<span class="io-flow-mxl vide" title="${esc(T('js.io2110.mxl_none_tip', 'Aucune entrée MXL câblée pour cette essence'))}">—</span>`;
  }

  // État PAR ESSENCE. Il n'existait qu'au niveau du slot : une sortie dont la vidéo part bien mais
  // dont l'audio est muet affichait « émet », et le silence passait inaperçu. Chaque essence dit
  // donc le sien. `tx_stalled` est mesuré côté vidéo (sender live) ; pour l'audio et l'ANC on ne
  // peut affirmer que ce qu'on sait : une destination posée, ou pas.
  function _txState(t, kind, ess) {
    const mk = (txt, ton, tip) =>
      `<span class="io-flow-state">${FLUX().badge(ton, txt, tip)}</span>`;
    const OK = 'ok', MUET = 'attente', ALER = 'alerte';
    if (kind === 'video') {
      if (t.tx_stalled) return mk('⚠ sans flux', ALER,
        T('js.io2110.tx_noflux_tip', "Activée mais n'émet aucun flux : entrée absente, budget files/lcores, ou files désalignées."));
      if (!t.shm_in && !t.gen) return mk(T('js.io2110.tx_unwired', 'non câblée'), MUET,
        T('js.io2110.tx_unwired_tip', 'Aucune entrée MXL et aucun générateur : cette sortie ne porte rien.'));
      return mk(T('js.io2110.tx_emitting', 'émet'), OK,
        t.gen ? T('js.io2110.tx_emitting_gen_tip', 'Mire générée par le moteur')
              : T('js.io2110.tx_emitting_src_tip', 'Source câblée en cours d\'émission'));
    }
    if (!ess || !ess.mcast) return mk('sans dest.', MUET, "Aucune adresse de destination sur cette essence.");
    if (kind === 'audio' && !ess.shm_in && !(ess.tone && ess.tone.enabled))
      return mk('silence', MUET,
        T('js.io2110.tx_audio_silent_tip', 'Ni entrée MXL câblée ni générateur de tonalité : cette sortie audio émet du silence.'));
    if (kind === 'anc' && !ess.shm_in) return mk('sans source', MUET,
      T('js.io2110.tx_anc_none_tip', "Aucune entrée MXL câblée pour l'ANC."));
    return mk(T('js.io2110.tx_emitting', 'émet'), OK, T('js.io2110.tx_dest_ok_tip', 'Destination posée et source présente'));
  }

  function _txSdp(ess, kind, slot, tagLabel) {
    // Un SDP PAR ESSENCE : c'est ce que NMOS publie, et c'est ce que le Rx propose déjà.
    if (!ess.sdp_href) return '<span class="io-flow-sdp"></span>';
    return `<span class="io-flow-sdp"><button type="button" class="btn btn-sm io2110-sdp"
        aria-haspopup="dialog" onclick="IO2110.viewSdp('${esc(ess.sdp_href)}','Tx #${slot + 1} · ${esc(tagLabel)}')"
        title="${esc(T('js.io2110.sdp_show_tip', 'Afficher le SDP annoncé pour {label}').replace('{label}', tagLabel))}"><span class="ctl-led on"
        style="--ctl-led-col:var(--status-running-fg)"></span>SDP…</button></span>`;
  }

  function _flowRow(vmid, slot, tagClass, tagLabel, kind, ess, audioIdx, boundRes, t, fb) {
    ess = ess || {};
    t = t || { slot: slot };
    const ctrls = _txGen(vmid, t, kind, ess, audioIdx, fb)
                + _txIdent(vmid, t, kind)
                + _txSdp(ess, kind, slot, tagLabel)
                + _txFormat(kind, ess, t, vmid);
    const mc = (m, p) => m ? esc(m + ':' + (p || 0)) : '<span class="meta">—</span>';
    const ai = audioIdx != null ? ',' + audioIdx : '';
    const cur0 = (ess.mcast || '') + ':' + (ess.port || 5000);
    const cur1 = (ess.mcast2 || '') + ':' + (ess.port2 || 5000);
    // slot_key NMOS du flux (rebinding explicite). essence registre : ANC → « data ».
    const slotKey = kind === 'video' ? `tx${slot}:v` : (kind === 'anc' ? `tx${slot}:d` : `tx${slot}:a${audioIdx}`);
    const regEss = kind === 'anc' ? 'data' : kind;
    const essCls = kind === 'anc' ? 'd' : kind === 'audio' ? 'a' : 'v';
    // LIBELLÉ : une sortie porte le nom de ce qu'elle émet — le libellé de la source câblée, résolu
    // par le même mécanisme partagé que les réceptions (window.SourceLabels, niveau réglable dans
    // la barre de navigation). Sans entrée câblée il n'y a rien à nommer, et on n'invente pas.
    let lab = '';
    if (ess.shm_in && window.SourceLabels) {
      const L = window.SourceLabels.labelOf(ess.shm_in);
      if (L && L.value) {
        lab = `<small class="io-flow-lab${L.level !== window.SourceLabels.level ? ' fallback' : ''}"
          title="${esc(L.value)}">${esc(L.value)}</small>`;
      }
    }
    // Cadence MESURÉE : le moteur ne la remonte que pour la vidéo du slot (sender live). Elle
    // porte AUSSI la santé d'émission — sous-cadence ou epochs ratés (`late`), qui signifient des
    // trames perdues et un « RTP alignment failure » côté récepteur. Ce signal vivait dans le
    // badge de l'en-tête ; il suit la cadence sur la ligne plutôt que de rester derrière.
    // Cadence : rendue par le socle partagé, donc identique à celle d'une source — à ceci près
    // qu'en émission on connaît la CIBLE (`fps_nominal`), ce qui permet d'afficher l'écart plutôt
    // qu'un simple chiffre. `late` (epochs ratés) enrichit l'explication sans changer la forme.
    const late = t.late || 0;
    const fpsCell = FLUX().cadence(kind, t.fps, t.fps_nominal, {
      sens: 'tx',
      titre: late > 0 ? window.t('js.io2110.tx_health_tip').replace('{late}', late) : null,
    });
    // Une adresse PAR LIGNE, comme en réception : les deux jambes d'un flux redondant se comparent
    // alors chiffre sous chiffre. Le bouton d'édition suit sa propre adresse.
    const edit = (leg, cur) => `<button type="button" class="btn btn-sm io2110-editbtn"
        title="${esc(T('js.io2110.edit_dest_tip', 'Modifier cette adresse de destination'))}"
        onclick="IO2110.setDest(${vmid},${slot},'${kind}',${leg},'${esc(cur)}'${ai})">✎</button>`;
    let net;
    if (boundRes && boundRes.id) {
      // Flux LIÉ à une ressource du registre NMOS : c'est elle qui fait autorité sur le transport
      // (adresse, format), poussé vers le slot. L'édition locale est donc refusée, et on le DIT
      // plutôt que de laisser un champ qui échouerait.
      net = `<span><span class="io-flow-addr">${mc(ess.mcast, ess.port)}</span>
        <span class="io2110-bound" title="${esc(T('js.io2110.nmos_bound_full_tip', 'Transport piloté par la ressource NMOS « {label} » : adresse et format viennent du registre, pas de cette page.').replace('{label}', boundRes.label || ''))}">${
          esc(boundRes.label || boundRes.id.slice(0, 8))}</span>
        <button type="button" class="btn btn-sm io2110-editbtn"
          onclick="IO2110.unbindFlow(${vmid},'${slotKey}')"
          title="${esc(T('js.io2110.unbind_tip', 'Délier : cette sortie reprend la main sur son adresse'))}">${esc(T('js.io2110.unbind', 'Délier'))}</button></span>`;
    } else {
      net = `<span><span class="io-flow-addr">${mc(ess.mcast, ess.port)}</span>${edit(0, cur0)}
        <button type="button" class="btn btn-sm io2110-editbtn" aria-haspopup="dialog"
          onclick="IO2110.bindFlow(${vmid},'${slotKey}','${regEss}')"
          title="${esc(T('js.io2110.bind_nmos_tip', "Rattacher cette sortie à une ressource du registre NMOS : l'adresse et le format viendront alors du registre, et suivront la ressource d'un nœud à l'autre."))}">NMOS…</button></span>`;
      if (ess.mcast2) {
        net += `<span><span class="io-flow-addr">${mc(ess.mcast2, ess.port2)}</span>${edit(1, cur1)}</span>`;
      }
    }
    return `<div class="io-flow io-flow--${essCls} ctl-dense">
      <span class="io-flow-tag">${tagLabel}</span>
      ${lab}
      ${ctrls}
      ${_txState(t, kind, ess)}
      <span class="io-flow-fps">${fpsCell}</span>
      <span class="io-flow-net" title="${esc(T('js.io2110.addr_emitted', 'adresses 2110 émises'))}">${net}</span>
      ${_txMxl(ess, kind)}
    </div>`;
  }

  function _fpsClass(fps) {
    if (fps == null) return 'off';
    return fps > 1 ? 'ok' : (fps > 0 ? 'warn' : 'off');
  }

  // MODE d'émission du slot — ce qui sort, pas d'où ça vient. Le chemin MXL a rejoint la ligne
  // de l'essence (cf. _txMxl) : le répéter ici afficherait deux fois la même chose, dont une fois
  // loin de l'essence qu'elle concerne.
  function _emitState(t, fallback) {
    // Retourne un texte court décrivant l'état d'émission EFFECTIF du slot TX. Le générateur
    // explicitement activé prime sur le câble attaché (la mire est émise, pas la source) → GEN
    // d'abord. Sinon un câble présent gouverne, puis le repli, puis « non câblée ».
    if (t.gen) return 'GEN · ' + esc(t.gen_pattern || 'bars');
    if (t.shm_in) return T('js.io2110.wired', 'câblée');
    if (fallback && fallback !== 'none') return 'Repli · ' + esc(fallback);
    return T('js.io2110.unwired_paren', '(non câblée)');
  }

  function _genBadgeClass(t, fallback) {
    if (t.gen) return 'on';
    if (!t.shm_in && fallback && fallback !== 'none') return 'repli';
    return 'off';
  }

  // Charge la liste des formats vidéo des Réglages dans window._videoFormats. Réutilise le loader
  // global de scripts.js s'il est présent (page Containers/Projets) ; sinon (page Destinations, où
  // scripts.js n'est PAS chargé) on fait nous-mêmes le fetch /api/settings — même format de sortie
  // que scripts.js.loadVideoFormats() pour rester interchangeable.
  async function _ensureVideoFormats() {
    if (Array.isArray(window._videoFormats)) return window._videoFormats;
    if (window.loadVideoFormats) { try { return await window.loadVideoFormats(); } catch (_) {} }
    try {
      const r = await fetch('/api/settings');
      const s = r.ok ? await r.json() : {};
      window._videoFormatDefault = s.video_format_default || '';
      window._videoFormats = String(s.video_formats || '').split('\n')
        .map(l => l.trim()).filter(Boolean)
        .map(l => {
          const p = l.split(';').map(x => x.trim());
          return { label: p[0] || '', w: parseInt(p[1]) || 0, h: parseInt(p[2]) || 0,
                   fps: parseFloat(p[3]) || 25,
                   scan: (p[4] || 'p').toLowerCase() === 'i' ? 'i' : 'p',
                   chroma: ['420', '422', '444'].includes(p[5]) ? p[5] : '422',
                   bit_depth: [8, 10, 12].includes(parseInt(p[6])) ? parseInt(p[6]) : 10,
                   colorimetry: (p[7] || '709').toLowerCase() };
        })
        .filter(f => f.label && f.w && f.h);
    } catch (_) { window._videoFormats = []; }
    return window._videoFormats;
  }

  // Sélecteur de résolution du GÉN (mire) d'une sortie. Un slot CÂBLÉ qui SUIT sa source verrouille
  // le format (affichage informatif). Mais un slot dont le générateur est explicitement activé
  // (bouton GEN) émet la mire EN PRIORITÉ sur le câble attaché → son format redevient choisissable.
  // Sinon : presets des Réglages (window._videoFormats).
  // ── État d'une sortie (étage 1) ──────────────────────────────────────────────────────────────
  // C'est CE chip qui explique pourquoi allumer une sortie est gratuit : elle EXISTAIT DÉJÀ.
  //   ACTIVE        : session vivante + source → elle émet du contenu
  //   PROVISIONNÉE  : session + feuille RL créées, SANS source → silencieuse (l'allumer = swap)
  //   DÉCLARÉE      : au layout mais pas encore provisionnée (→ « Appliquer le layout »)
  //   HORS LAYOUT   : slot absent du layout de la carte → l'armer recalera l'arbre du port
  function _stateChip(e, t) {
    const st = (e._layout && e._layout.slot_states) ? e._layout.slot_states[t.slot] : null;
    if (!st) return '';
    const lbl = window.t('js.io2110.slot_state_' + st);
    const tip = window.t('js.io2110.slot_state_' + st + '_tip');
    return `<span class="io2110-state ${st}" title="${esc(tip)}">${esc(lbl)}</span>`;
  }

  // Pastille de CLASSEMENT d'une action (étage 2). Sur un port en rate limiter, les actions qui
  // touchent la SIGNATURE d'une session (format, TROFF, destination) recalent l'arbre → ambre.
  // Les autres (source, mire, IDENT, tonalité, repli) sont des swaps → vert. Sur un port AF-XDP il
  // n'y a pas d'arbre : tout est vert, et le bandeau du moteur le dit.
  function _rlOn(e) { return !(e._layout && e._layout.budget && e._layout.budget.dpdk_active === false); }

  // Mode PMD des ports TX du moteur (socle narrow) : DPDK (RL actif) vs AF-XDP.
  // Sous DPDK, le NOMBRE et le FORMAT des sorties TX sont gouvernés par le MODÈLE de carte
  // (Réglages → Réseau → Sorties 2110) — les +/- TX y sont redondants (et peuvent contredire le
  // modèle) → on les masque. Détection : `rl_active` (flag moteur agrégé, avec repli orchestrateur
  // pour un nœud PF dpdk moteur muet), OU au moins UN port média en `pmd=dpdk` (cas mixte).
  function _txDpdk(e) {
    if (!e) return false;
    if (e.rl_active) return true;
    return (e.nic_ports || []).some(p => p && p.pmd === 'dpdk');
  }
  function _dot(e, disrupt) {
    if (!_rlOn(e)) return '<span class="txm-dot safe" title="' + esc(window.t('js.txm.dot_safe_xdp')) + '"></span>';
    return disrupt
      ? '<span class="txm-dot disrupt" title="' + esc(window.t('js.txm.dot_disrupt')) + '"></span>'
      : '<span class="txm-dot safe" title="' + esc(window.t('js.txm.dot_safe')) + '"></span>';
  }

  // Format d'une sortie, à la FORME de celui d'une source (carte Sources) : « 1920×1080p25 »
  // fusionné, puis ce que la config ajoute (chroma, profondeur).
  //
  // Deux différences de FOND que la forme commune ne doit pas gommer :
  //  · en Rx le format est MESURÉ (lu dans le SDP reçu) ; ici il est DÉCLARÉ. On prend donc
  //    `fps_cfg` (le réglage) et jamais `fps` (le live), et on ne colore PAS la cadence comme le
  //    Rx colore la sienne : là-bas la couleur dit la santé d'une mesure, ici elle ferait passer
  //    une intention pour un constat. La cadence réellement émise a déjà son badge à côté.
  //  · pas de COLORIMÉTRIE : le moteur ne la connaît pas (cf. nmos_detail.py). Un champ absent
  //    est absent — on ne comble pas avec une valeur vraisemblable.
  function _fmtExtras(t) {
    const chroma = t.chroma ? String(t.chroma).replace(/^(\d)(\d)(\d)$/, '$1:$2:$3') : '';
    const extra = [chroma, t.bit_depth ? t.bit_depth + 'b' : ''].filter(Boolean).join(' · ');
    return extra ? ' · ' + extra : '';
  }
  function _formatSelect(vmid, t) {
    const fpsTxt = (t.fps_cfg != null) ? String(Number(t.fps_cfg)).replace(/\.0$/, '') : '';
    const cur = `${t.width}×${t.height}${t.scan === 'i' ? 'i' : 'p'}${fpsTxt}`;
    if (t.shm_in && !t.gen) {
      return `${cur}${_fmtExtras(t)} <span class="io-flow-fmt-note"
        title="${esc(T('js.io2110.follows_source_tip', "Cette sortie suit le format de la source qui lui est câblée : il n'y a rien à choisir."))}">${esc(T('js.io2110.follows_source', 'suit la source'))}</span>`;
    }
    const fmts = window._videoFormats || [];
    if (!fmts.length) return '';
    const curKey = `${t.width}x${t.height}@${t.fps_cfg || 25}${t.scan || 'p'}`;
    const hasCur = fmts.some(f => `${f.w}x${f.h}@${f.fps}${f.scan}` === curKey);
    // `data-depth` : la PROFONDEUR déclarée par le format des Réglages. Elle manquait, et la
    // conséquence n'était pas visible : choisir « 1080p50 10 bits » changeait la résolution et
    // laissait la sortie en 8 bits, sans rien dire. L'opérateur croyait avoir choisi 10.
    const curOpt = hasCur ? '' :
      `<option value="${curKey}" data-w="${t.width}" data-h="${t.height}" data-fps="${t.fps_cfg || 25}" data-scan="${t.scan || 'p'}" data-depth="${t.bit_depth || ''}" selected>${cur} (actuel)</option>`;
    const opts = fmts.map(f => {
      const key = `${f.w}x${f.h}@${f.fps}${f.scan}`;
      // Le libellé du format porte sa profondeur quand elle diffère de celle en service : c'est
      // ce que ce choix va CHANGER, et ça doit se lire avant de cliquer, pas après.
      const suff = (f.bit_depth && t.bit_depth && f.bit_depth !== t.bit_depth) ? ` → ${f.bit_depth}b` : '';
      return `<option value="${key}" data-w="${f.w}" data-h="${f.h}" data-fps="${f.fps}" data-scan="${f.scan}" data-depth="${f.bit_depth || ''}"${key === curKey ? ' selected' : ''}>${esc(f.label)}${suff}</option>`;
    }).join('');
    // AU REPOS : du texte, exactement comme le format lu d'une source. La liste ne remplace ce
    // texte que si CETTE sortie est en cours d'édition (`_fmtEdit`) — un état gardé en module et
    // non dans le DOM, sans quoi le rafraîchissement de 3 s refermerait la liste sous les doigts.
    const cle = `${vmid}:${t.slot}`;
    if (_fmtEdit !== cle) {
      return `<button type="button" class="io-fmt-text" title="${esc(T('js.io2110.fmt_click', "Format d'émission — cliquer pour changer"))}"
                onclick="IO2110.editFormat(${vmid},${t.slot})">${cur}${_fmtExtras(t)}</button>`;
    }
    // Chroma et profondeur ne se choisissent pas ici (réglages du moteur) : ils suivent la liste
    // en texte, pour que la ligne se lise d'un bloc comme un format de source.
    return `<select class="ctl-select io2110-fmtsel io-fmt-sel" title="${esc(T('js.io2110.fmt_gen_res_tip', 'Résolution du générateur (mire) de cette sortie'))}"
              onchange="IO2110.setFormat(${vmid},${t.slot},this)"
              onblur="IO2110.closeFormat()">${curOpt}${opts}</select>${
      _fmtExtras(t) ? `<span title="${esc(T('js.io2110.fmt_extras_tip', 'Chroma et profondeur : réglages du moteur, communs à toutes ses sorties'))}">${_fmtExtras(t)}</span>` : ''}`;
  }

  // Rythme d'émission (mode tranche) : « image suivante » (défaut, émission alignée epoch — le
  // récepteur reçoit chaque ligne à l'instant nominal) vs « décalée » (l'image part N µs après
  // l'epoch, dès que les premières tranches sont prêtes ; TROFF déclaré dans le SDP, timestamp
  // RTP inchangé → sync A/V préservée ; gain ≈ 1 trame de latence, coût = marge tampon récepteur).
  function _pacingSelect(vmid, t) {
    const cur = t.epoch_shift_us || 0;
    const curOpt = (cur > 0 && cur !== 6000)
      ? `<option value="${cur}" selected>${esc(T('js.io2110.pacing_offset_current', 'Décalée +{ms} ms (actuel)').replace('{ms}', (cur / 1000).toFixed(1)))}</option>` : '';
    return `<select class="ctl-select io2110-fmtsel" title="${esc(T('js.io2110.pacing_tip', "Rythme d'émission : « image suivante » = émission alignée sur l'epoch nominal (interop stricte) ; « décalée » = l'image part dès que ses premières tranches sont prêtes (TROFF déclaré au SDP, timestamp inchangé) — gain ≈ 1 trame de latence, exige une marge tampon côté récepteur"))}"
              onchange="IO2110.setPacing(${vmid},${t.slot},this.value)">
        <option value="0"${cur === 0 ? ' selected' : ''}>${esc(T('js.io2110.pacing_next', '⏱ Image suivante'))}</option>
        ${curOpt}
        <option value="6000"${cur === 6000 ? ' selected' : ''}>${esc(T('js.io2110.pacing_offset6', '⚡ Décalée +6 ms (TROFF)'))}</option>
      </select>`;
  }

  // Choix de la trame émise (mode tranche). Le moteur garde quelques trames prêtes d'avance ;
  // servir la plus RÉCENTE au lieu de la plus ancienne rend une image entière de latence, parce
  // qu'une trame fraîche est disponible avant que le transport ne vienne la chercher. Mesuré le
  // 2026-08-12 : âge du contenu au récepteur 62,4 → 42,4 ms, sans perte de cadence.
  function _fraicheurSelect(vmid, t) {
    const cur = (t.serve_newest === undefined || t.serve_newest === null) ? 1
              : (t.serve_newest ? 1 : 0);
    return `<select class="ctl-select io2110-fmtsel" title="${esc(T('js.io2110.fresh_tip', 'Choix de la trame émise : « la plus récente » libère les trames périmées en attente et rend environ une image de latence ; « la plus ancienne » est le comportement historique, à ne reprendre que si une source irrégulière provoque des répétitions'))}"
              onchange="IO2110.setFraicheur(${vmid},${t.slot},this.value)">
        <option value="1"${cur === 1 ? ' selected' : ''}>${esc(T('js.io2110.fresh_newest', '⚡ Trame la plus récente'))}</option>
        <option value="0"${cur === 0 ? ' selected' : ''}>${esc(T('js.io2110.fresh_oldest', '⏱ Trame la plus ancienne'))}</option>
      </select>`;
  }

  function _nicBar(gbps, estGbps, cap, label) {
    const val = gbps != null ? gbps : estGbps;
    const isEst = gbps == null && estGbps != null;
    if (val == null) return '';
    const pct = Math.min(100, Math.round(val / cap * 100));
    const etat = _nicEtat(pct);
    const txt = `${isEst ? '~' : ''}${val.toFixed(1)} / ${cap} Gbps (${pct}%)${isEst ? ' (estimation)' : ''}`;
    return `<div class="nic-bar-wrap">
      <span class="nic-bar-lbl">${label}</span>
      <span class="nic-bar-val${isEst ? ' nic-bar-est' : ''}" style="color:${_nicCouleur(etat)}">${txt}</span>
      ${_gauge(pct, etat, `${label} : ${txt}`)}
    </div>`;
  }

  // ─── Jauges de charge (catalogue) ──────────────────────────────────────────────────────────
  // Identique à la carte Sources du plugin 2110_io : mêmes seuils d'exploitation qu'avant
  // (alerte 60 %, critique 80 %), mais portés par les états du composant et non par des couleurs
  // écrites à la main — et le seuil est MATÉRIALISÉ, donc on le voit approcher. Une mesure
  // absente prend l'état `na` : la barre à zéro d'avant se lisait « ce port ne reçoit rien »
  // alors qu'elle voulait dire « je n'ai pas la mesure ».
  const NIC_SEUIL = 60, NIC_CRIT = 80;
  function _nicEtat(pct){ return pct == null ? 'na' : pct >= NIC_CRIT ? 'over' : pct >= NIC_SEUIL ? 'warn' : ''; }
  function _nicCouleur(etat){
    return etat === 'over' ? 'var(--status-stopped-fg)'
         : etat === 'warn' ? 'var(--status-warning-fg)'
         : etat === 'na'   ? 'var(--text-muted)' : 'var(--status-running-fg)';
  }
  // Rendue sans `.ctl-gauge-val` : la valeur est déjà affichée en clair et en permanence à côté,
  // ce qui satisfait la règle du composant (le chiffre ne doit jamais exiger un survol).
  function _gauge(pct, etat, titre, seuil){
    const p = Math.max(0, Math.min(1, (pct || 0) / 100));
    const cls = 'ctl-gauge' + (etat ? ' ' + etat : '') + (seuil === false ? ' ctl-gauge--sans-seuil' : '');
    return `<span class="${cls}" role="img" aria-label="${esc(titre)}"
      style="--ctl-gauge-w:100%;flex:1${seuil === false ? '' : `;--ctl-gauge-seuil:${NIC_SEUIL}%`}"><span
      class="ctl-gauge-fill" style="transform:scaleX(${p.toFixed(3)})"></span></span>`;
  }

  function _nicHeader(model, aggregateGbps) {
    if (!model) return '';
    // Agrégat = somme des vitesses de lien réelles des ports de la carte (ex. 4×10 = 40G).
    const agg = (aggregateGbps > 0)
      ? ` · <span class="nic-shared">${esc(T('js.io2110.nic_shared', 'agrégé {n}G').replace('{n}', aggregateGbps))}</span>` : '';
    return `<div class="nic-model-lbl">${esc(model)}${agg}</div>`;
  }

  // Couleur stable d'un réseau média (pastille red/blue de la bande de ports + badges de slot).
  // Hash du nom → teinte ; on garde une saturation/luminosité fixe pour rester lisible en thème clair/sombre.
  function _netColor(network) {
    const s = String(network || '·');
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffffff;
    return `hsl(${h % 360},62%,55%)`;
  }

  // Chip de port (toujours visible) : nom + réseau + barre de charge (mesurée ou estimée) + files/flux.
  function _portChip(p, role) {
    const col   = _netColor(p.network);
    const cap   = p.port_capacity_gbps || 100;
    const meas  = role === 'tx' ? p.tx_gbps : p.rx_gbps;
    const est   = role === 'tx' ? null      : p.rx_estimated_gbps;
    const val   = meas != null ? meas : est;
    const isEst = meas == null && est != null;
    const pct   = val != null ? Math.min(100, Math.round(val / cap * 100)) : null;
    const etat  = _nicEtat(pct);
    const q     = role === 'tx' ? p.tx_queues : p.rx_queues;
    const flows = role === 'tx' ? p.tx_flow_count : p.rx_flow_count;
    const down  = p.link_up === false;
    return `<div class="io2110-portchip${down ? ' down' : ''}" style="border-left-color:${col}">
      <div class="pc-top">
        <span class="pc-name" style="color:${col}" title="${esc(p.iface)}">${esc(p.alias || p.iface)}</span>
        ${p.primary ? '<span class="pc-prim">PRIM</span>' : ''}
        <span class="pc-net">${esc(p.network || '')}</span>
        ${down ? `<span class="pc-down" title="${esc(window.t('js.io2110.link_down'))}">⚠ lien</span>` : ''}
      </div>
      <div class="pc-load">
        <span class="pc-loadval${isEst ? ' est' : ''}"${val == null ? ' style="color:var(--text-muted)"' : ''}>${
          val != null ? (isEst ? '~' : '') + val.toFixed(1) + ' / ' + cap + ' G' : '—'}</span>
        ${_gauge(pct, etat, val != null ? T('js.io2110.port_rate', '{iface} : {val} sur {cap} Gbps').replace('{iface}', p.iface).replace('{val}', val.toFixed(1)).replace('{cap}', cap)
                                        : T('js.io2110.port_no_rate', '{iface} : débit non mesuré').replace('{iface}', p.iface))}
      </div>
      <div class="pc-meta">${flows != null ? esc(T('js.io2110.pc_flows', '{n} flux').replace('{n}', flows)) : ''}${q != null ? ' · ' + esc(T('js.io2110.pc_queues', '{n} files').replace('{n}', q)) : ''}</div>
    </div>`;
  }

  // Badge état PTP d'un port (SLAVE/MASTER/PASSIVE/LISTENING/FAULTY…) — couleur par état.
  function _ptpBadge(state){
    if (!state) return '';
    const M = {SLAVE:['SLAVE','var(--status-running-fg,#22c55e)'], MASTER:['MASTER','#60a5fa'],
      GRAND_MASTER:['GRAND MASTER','#60a5fa'], PRE_MASTER:['PRE-MASTER','#60a5fa'],
      PASSIVE:['PASSIVE','var(--text-muted)'], LISTENING:['LISTENING','#e8a33d'],
      UNCALIBRATED:['UNCAL','#e8a33d'], FAULTY:['FAULTY','var(--status-stopped-fg,#f87171)'],
      DISABLED:['DISABLED','var(--text-muted)'], INITIALIZING:['INIT','var(--text-muted)']};
    const [lbl,c] = M[state] || [state, 'var(--text-muted)'];
    return `<span class="pc-ptp" style="color:${c};border-color:${c}" title="${esc(T('js.io2110.ptp_state_tip', 'État PTP du port : {state}').replace('{state}', state))}">⏱ ${lbl}</span>`;
  }

  // Bande de ports + bouton « Par NIC ». Mono-port (nic_ports vide) → '' (UI agrégée inchangée).
  // Le détail déplié montre, PAR PORT, des barres PLEINE LARGEUR : débit RX (et TX côté Destinations)
  // + barre Queues XDP multi-segments (live/planifié/réservé/libre + repère plafond, comme la globale)
  // sur le budget de files du PORT — plus l'état PTP du port.
  function _nicPortStrip(e, role) {
    const ports = e.nic_ports || [];
    if (ports.length < 2) return '';
    const open   = IO2110._nicExpanded.has(e.vmid);
    const strip  = ports.map(p => _portChip(p, role)).join('');
    const detail = open ? `<div class="io2110-portdetail">${
      ports.map(p => {
        const col = _netColor(p.network);
        const cap = p.port_capacity_gbps || 100;
        const rxBar = _nicBar(p.rx_gbps, p.rx_estimated_gbps, cap, 'RX');
        const txBar = (role === 'tx') ? _nicBar(p.tx_gbps, null, cap, 'TX') : '';
        // Port DPDK (PF vfio, socle narrow) : plus de plafond AF-XDP (xdp_hw=null) — la métrique
        // pertinente = sessions RL TX / cap RL du port + files RSS RX. Sinon barre XDP historique.
        const xdpBar = (p.pmd === 'dpdk')
          ? ((p.rl_tx_cap ? _rlBar(p.tx_sessions_active || 0, p.rl_tx_cap, 0) : '') + _rssRow(p.rx_queues))
          : ((p.xdp_hw && p.xdp_reserved != null)
             ? _xdpBar(p.xdp_active || 0, p.xdp_planned, p.xdp_reserved, p.xdp_hw)
             : '');
        const flows = (p.rx_flow_count != null || p.tx_flow_count != null)
          ? `<span class="pc-meta">Flux : ${(p.rx_flow_count || 0)} RX · ${(p.tx_flow_count || 0)} TX</span>` : '';
        return `<div class="io2110-portcard" style="border-left-color:${col}">
          <h5><span style="color:${col}" title="${esc(p.iface)}">${esc(p.alias || p.iface)}</span>${p.primary ? '<span class="pc-prim">PRIM</span>' : ''}
            ${p.network ? `<span class="pc-net">${esc(p.network)}</span>` : ''}${_ptpBadge(p.ptp_state)}
            <span class="pc-meta" style="margin-left:auto">${p.link_up === false ? '⚠ lien down' : (p.link_up ? 'lien up' : '')}</span></h5>
          ${rxBar}${txBar}${xdpBar}${flows}
        </div>`;
      }).join('')}</div>` : '';
    return `<div class="io2110-nicbar">
        <div class="io2110-portstrip">${strip}</div>
        <button type="button" class="btn btn-sm" aria-expanded="${open}" onclick="IO2110.toggleNic(${e.vmid})"
          title="${esc(window.t('js.io2110.pernic_tip'))}">${open ? '▾' : '▸'} ${esc(window.t('js.io2110.pernic'))}</button>
      </div>${detail}`;
  }

  // Petit badge de port sur un slot RX/TX (multi-NIC) : iface coloré + 📌 si épinglé.
  // En 2022-7, le badge montre LA PAIRE (« a⇄b ») — la session écoute/émet sur les deux legs.
  function _portBadge(port, ifNet, ifPair) {
    if (!port || !port.iface) return '';
    const col = _netColor(ifNet ? ifNet[port.iface] : '');
    const base = port.alias || port.iface;
    const lbl = (ifPair && ifPair[port.iface])
      ? `${esc(base)}⇄${esc(ifPair[port.iface])}` : esc(base);
    const tip = (ifPair && ifPair[port.iface]) ? 'js.io2110.pair_227'
      : (port.pinned ? 'js.io2110.port_pinned' : 'js.io2110.port_auto');
    return `<span class="io2110-portbadge" style="border-left-color:${col};color:${col}"
      title="${esc(window.t(tip))} · ${esc(port.iface)}">${port.pinned ? '📌' : ''}${lbl}</span>`;
  }

  // Sélecteur de PORT (NIC) d'un slot TX — le pendant exact de celui des sources. Il manquait ici
  // seulement à l'écran : `/api/mtl/<vmid>/pin` accepte `role:"tx"` et persiste `tx_pins` depuis le
  // début, et le payload porte déjà `ports` / `smpte_2022_7` / `port_pairs`. On ne pouvait donc
  // qu'y LIRE le port choisi automatiquement, pas l'épingler — sur un nœud multi-cartes, aucune
  // façon de dire « cette sortie part par là », alors que la même page le permet en réception.
  //
  // SMPTE 2022-7 : une session dual-leg occupe LES DEUX ports d'une paire → on propose la PAIRE
  // (valeur = leg rouge), jamais ses membres séparément ; les ports non appariés restent offerts
  // individuellement. Proposer un demi-lien serait proposer une redondance qui n'en est pas une.
  function _portSelect(e, slot, port) {
    const ports = e.ports || [];
    if (ports.length < 2) return '';                     // mono-port : rien à choisir
    const pairs = (e.smpte_2022_7 && e.port_pairs) ? e.port_pairs : [];
    const pairOf = ifn => pairs.find(pr => pr[0] === ifn || pr[1] === ifn);
    const cur = (port && port.pinned) ? port.iface : '';   // '' = Auto
    const eff = (port && port.iface) || '';
    const curPair = pairOf(cur), effPair = pairOf(eff);
    const effLbl = effPair ? `${effPair[0]} ⇄ ${effPair[1]}` : eff;
    const opts = [`<option value=""${cur === '' ? ' selected' : ''}>Auto${eff ? ` (${esc(effLbl)})` : ''}</option>`];
    const vus = new Set();
    for (const p of ports) {
      const pr = pairOf(p.ifname);
      if (pr) {
        const cle = pr.join(':');
        if (vus.has(cle)) continue;
        vus.add(cle);
        opts.push(`<option value="${esc(pr[0])}"${curPair && curPair.join(':') === cle ? ' selected' : ''
          }>${esc(pr[0])} ⇄ ${esc(pr[1])}${p.network ? ` · ${esc(p.network)}` : ''}</option>`);
      } else {
        opts.push(`<option value="${esc(p.ifname)}"${cur === p.ifname ? ' selected' : ''
          }>${esc(p.ifname)}${p.network ? ` · ${esc(p.network)}` : ''}</option>`);
      }
    }
    return `<select class="ctl-select io2110-fmtsel" onchange="IO2110.setPort(${e.vmid},${slot},this)"
      title="${esc(pairs.length ? T('js.io2110.port_pair_tip', 'Paire 2022-7 (les deux legs) ou port de cette sortie')
                            : T('js.io2110.port_auto_tip', 'Port (NIC) de cette sortie — Auto = répartition automatique'))}">${opts.join('')}</select>`;
  }

  // Barre « Queues XDP » multi-segments (B2+) — IDENTIQUE au helper de control.js (page Sources).
  // active=live · planned=provisionné (≥active) · reserved=plafond mtl_init · hw=files NIC.
  function _xdpBar(active, planned, reserved, hw, scope){
    active   = Math.max(0, active || 0);
    planned  = Math.max(active, planned || active);
    reserved = Math.max(0, reserved || 0);
    const pend  = Math.max(0, planned - active);
    const hot   = Math.max(0, reserved - active);
    const freeQ = Math.max(0, reserved - planned);
    const overQ = Math.max(0, planned - reserved);
    const pct   = v => Math.min(100, Math.max(0, v / hw * 100));
    const col   = (active >= reserved) ? 'var(--status-stopped-fg,#f87171)'
                : (hot <= 1 ? '#e8a33d' : 'var(--status-running-fg,#22c55e)');
    const aPct = pct(active), rPct = pct(reserved), planPct = pct(planned);
    const hotL = aPct, hotW = Math.max(0, Math.min(planPct, rPct) - aPct);
    const ovrL = Math.max(aPct, rPct), ovrW = Math.max(0, planPct - ovrL);
    const freeL = pct(Math.min(Math.max(active, planned), reserved));
    const freeW = Math.max(0, rPct - freeL);
    const txt = (overQ
      ? T('js.io2110.q_over', '{active} live · +{pend} planifié dont {over} > réservé ({reserved}) → redéploiement').replace('{active}', active).replace('{pend}', pend).replace('{over}', overQ).replace('{reserved}', reserved)
      : T('js.io2110.q_ok', '{active} live · +{pend} planifié · {free} libre / {hw} files').replace('{active}', active).replace('{pend}', pend).replace('{free}', freeQ).replace('{hw}', hw)) + (scope || '');
    return `<div class="nic-bar-wrap">
      <span class="nic-bar-lbl">Queues XDP</span>
      <span class="nic-bar-val" style="color:${overQ ? '#e8a33d' : col}">${txt}</span>
      <div class="nic-xdp-track">
        <div class="nic-xdp-free"    style="left:${freeL}%;width:${freeW}%"></div>
        <div class="nic-xdp-pending" style="left:${hotL}%;width:${hotW}%;background-color:${col}"></div>
        <div class="nic-xdp-over"    style="left:${ovrL}%;width:${ovrW}%"></div>
        <div class="nic-xdp-active"  style="width:${aPct}%;background:${col}"></div>
        <div class="nic-xdp-mark"    style="left:${rPct}%" title="${esc(T('js.io2110.q_cap_tip', 'Plafond à chaud : {reserved} files réservées à mtl_init — au-delà, redéploiement requis').replace('{reserved}', reserved))}"></div>
      </div>
    </div>`;
  }

  // Barre « Sessions TX (RL) » — socle DPDK narrow : le budget TX pertinent = sessions sur le
  // rate-limiter matériel (cap RL par port, la limite dure de la carte — docs/chantiers/DPDK_NARROW.md §7).
  // `dropped` = sessions demandées au-delà du cap et IGNORÉES par le moteur → badge SUR-CAPACITÉ.
  function _rlBar(active, cap, dropped, scope){
    active  = Math.max(0, active || 0);
    cap     = Math.max(1, cap || 1);
    dropped = Math.max(0, dropped || 0);
    const pct  = Math.min(100, Math.round(active / cap * 100));
    const over = dropped > 0 || active > cap;
    const col  = over ? 'var(--status-stopped-fg,#f87171)'
               : (active >= cap ? '#e8a33d' : 'var(--status-running-fg,#22c55e)');
    // Le plafond RL est une limite DURE de la carte : l'atteindre EST l'alerte, le dépasser est
    // la faute. Rien d'intermédiaire à matérialiser → jauge sans trait de seuil.
    const etat = over ? 'over' : (active >= cap ? 'warn' : '');
    let txt = window.t('js.io2110.rl_val').replace('{act}', active).replace('{cap}', cap)
              + ` (${pct}%)`;
    if (over) txt = window.t('js.io2110.rl_overcap').replace('{n}', dropped || (active - cap))
                    + ' — ' + txt;
    return `<div class="nic-bar-wrap">
      <span class="nic-bar-lbl" title="${esc(window.t('js.io2110.rl_tip'))}">${esc(window.t('js.io2110.rl_sessions'))}</span>
      <span class="nic-bar-val" style="color:${_nicCouleur(etat)}">${esc(txt + (scope || ''))}</span>
      ${_gauge(pct, etat, esc(txt), false)}
    </div>`;
  }

  // Ligne « Files RX (RSS) » (socle DPDK) : files de réception réservées, dimensionnées à la
  // demande (pas de plafond AF-XDP sous vfio) — informatif, sans barre de saturation.
  function _rssRow(n, scope){
    if (n == null) return '';
    return `<div class="nic-bar-wrap">
      <span class="nic-bar-lbl" title="${esc(window.t('js.io2110.rx_rss_tip'))}">${esc(window.t('js.io2110.rx_rss'))}</span>
      <span class="nic-bar-val">${esc(window.t('js.io2110.rx_rss_val').replace('{n}', n) + (scope || ''))}</span>
    </div>`;
  }

  // ── Bandeau de sûreté du moteur (docs/reference/TX_LAYOUTS.md étages 1-2) ───────────────────────────────────
  // Répond, en une bande, aux trois questions que l'opérateur se pose avant de toucher une sortie :
  //   1. « ce port peut-il blipper ? »  → mode du port (narrow/RL vs AF-XDP, où RIEN n'est perturbateur)
  //   2. « mon layout tient-il ? »      → jauge de files RL consommées / plafond de la carte
  //   3. « où en est le provisioning ? »→ état du layout + bouton Appliquer (événement de maintenance)
  // `e._layout` est peuplé par `_dRefresh` (GET /api/mtl/<vmid>/tx-layout/status).
  function _budgetGauge(used, cap) {
    if (!cap) return '';
    const pct = Math.min(100, Math.round(used / cap * 100));
    const over = used > cap;
    // Budget de files du layout : l'alerte est à 85 % des files, pas à 60 % comme un débit.
    const etat = over ? 'over' : pct > 85 ? 'warn' : '';
    return `<div class="nic-bar-wrap" title="${esc(window.t('js.io2110.layout_budget_tip'))}">
      <span class="nic-bar-lbl">${esc(window.t('js.io2110.layout_budget'))}</span>
      <span class="nic-bar-val" style="color:${_nicCouleur(etat)}">${used} / ${cap}${
        over ? ' — ' + esc(window.t('js.io2110.layout_over')) : ''}</span>
      <span class="ctl-gauge${etat ? ' ' + etat : ''}" role="img" style="--ctl-gauge-w:100%;flex:1;--ctl-gauge-seuil:85%"
        aria-label="${esc(window.t('js.io2110.layout_budget'))} : ${used} / ${cap}"><span
        class="ctl-gauge-fill" style="transform:scaleX(${(pct / 100).toFixed(3)})"></span></span>
    </div>`;
  }

  // ★ MULTI-PORT : un moteur peut émettre sur PLUSIEURS cartes (ou sur une paire 2022-7, qui compte
  // pour UNE unité de capacité). On rend une ligne d'état + une jauge de files PAR unité — l'ancien
  // panneau ne montrait que la carte primaire, donc la moitié des sorties d'un nœud bi-port était
  // invisible ici, et son modèle déclaré paraissait ignoré.
  // Une carte (ou une paire 2022-7) du moteur : état de son modèle + jauge de ses files RL. Le
  // budget est PAR PORT — chaque carte a son propre arbre RL et son propre plafond ; agréger les
  // deux sur une seule jauge doublait la consommation affichée.
  function _layoutCardRow(e, c) {
    const b = c.budget || {};
    const rl = b.dpdk_active !== false;
    const applied = c.state === 'applied';
    const lbl = c.state === 'none' ? window.t('js.io2110.layout_none')
              : applied ? window.t('js.io2110.layout_applied') : window.t('js.io2110.layout_pending');
    const count = c.state === 'none' ? '' : ` (${c.provisioned || 0}/${c.declared || 0})`;
    // « porte N sortie(s) » = ce que cette carte transporte RÉELLEMENT (répartition du moteur),
    // à distinguer du nombre DÉCLARÉ tant que le modèle n'est pas appliqué.
    const carries = (c.capacity_slots != null)
      ? `<span class="meta">${esc(window.t('js.io2110.card_carries')
          .replace('{n}', c.capacity_slots))}</span>` : '';
    const pairChip = c.kind === 'pair'
      ? `<span class="io2110-state active" title="${esc(window.t('js.io2110.card_pair_tip'))}">2022-7</span>` : '';
    const applyBtn = (!applied && c.state !== 'none' && typeof PS_CAN_DEPLOY !== 'undefined' && PS_CAN_DEPLOY)
      ? `<button class="io-addrow" onclick="IO2110.applyLayout(${e.vmid}, '${esc(c.key)}')"
           title="${esc(window.t('js.io2110.layout_apply_tip'))}">⚙ ${
           esc(window.t('js.io2110.layout_apply'))}${rl ? ' ⬤' : ''}</button>` : '';
    return `<div style="margin-bottom:6px">
      <div class="meta" style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
        <strong>${esc(c.label || c.key)}</strong>${pairChip}
        <span>${esc(lbl)}${count}</span>${carries}${applyBtn}
      </div>
      ${rl ? _budgetGauge(c.used_queues || 0, (b.rl_tx_cap || 0)) : ''}
    </div>`;
  }

  // Actions à l'échelle du MOTEUR (pas d'une carte) : édition des modèles, redéploiement, plan mcast.
  function _layoutEngineActions(e) {
    const can = (typeof PS_CAN_DEPLOY !== 'undefined' && PS_CAN_DEPLOY);
    const editLink = `<a href="/settings" class="meta">${esc(window.t('js.io2110.layout_edit_link'))}</a>`;
    const redeployBtn = can
      ? `<button class="io-addrow" onclick="IO2110.realign(${e.vmid})"
           title="${esc(window.t('js.io2110.engine_redeploy_tip'))}">⟳ ${
           esc(window.t('js.io2110.engine_redeploy'))}</button>` : '';
    const planBtn = can
      ? `<button class="io-addrow" onclick="IO2110.mcastPlan(${e.vmid})"
           title="${esc(window.t('js.io2110.mcast_plan_tip'))}">⊞ ${
           esc(window.t('js.io2110.mcast_plan'))}</button>` : '';
    return `<div class="meta" style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:4px 0">
      ${editLink}${redeployBtn}${planBtn}</div>`;
  }

  function _layoutPanel(e) {
    const cards = (e._layout && e._layout.cards) || [];
    if (cards.length > 1) {
      const rows = cards.map(c => _layoutCardRow(e, c)).join('');
      return `<div class="io2110-safety">${rows}${_layoutEngineActions(e)}${_binBar(e.vmid)}</div>`;
    }
    const st = (e._layout && e._layout.status) || e._layout;
    if (!st || st.state === 'no_iface') return '';
    const b = st.budget || {};
    const rl = b.dpdk_active !== false;
    const editLink = `<a href="/settings" class="meta">${esc(window.t('js.io2110.layout_edit_link'))}</a>`;
    // ★ AUCUN CONTRÔLE MUET : sur un port AF-XDP il n'y a pas de rate limiter — le profil « narrow »
    // déclaré dans Réglages n'a AUCUN effet, et aucune action n'est perturbatrice. On le DIT.
    const mode = rl
      ? `<span class="io2110-state out_of_layout" title="${esc(window.t('js.io2110.mode_rl_tip'))}">${
          esc(window.t('js.io2110.mode_rl'))}</span>`
      : `<span class="io2110-state active" title="${esc(window.t('js.io2110.mode_xdp_tip'))}">${
          esc(window.t('js.io2110.mode_xdp'))}</span>`;
    const applied = st.state === 'applied';
    const lbl = st.state === 'none' ? window.t('js.io2110.layout_none')
              : applied ? window.t('js.io2110.layout_applied') : window.t('js.io2110.layout_pending');
    const count = st.state === 'none' ? '' : ` (${st.provisioned || 0}/${st.declared || 0})`;
    const applyBtn = (!applied && st.state !== 'none')
      ? `<button class="io-addrow" onclick="IO2110.applyLayout(${e.vmid})"
           title="${esc(window.t('js.io2110.layout_apply_tip'))}">⚙ ${
           esc(window.t('js.io2110.layout_apply'))}${rl ? ' ⬤' : ''}</button>` : '';
    // Redéploiement du moteur TOUJOURS disponible (≠ « réaligner » qui n'apparaît que sur famine) :
    // recrée le moteur avec sa config COURANTE → prend en compte un nouveau port média, un réglage
    // changé, etc. DISRUPTIF (coupe tous les flux) → passe par IO2110.realign (modale de confirmation).
    // Gaté sur containers.deploy : réservé aux administrateurs/déployeurs (un exploitant ne le voit pas).
    const redeployBtn = (typeof PS_CAN_DEPLOY !== 'undefined' && PS_CAN_DEPLOY)
      ? `<button class="io-addrow" onclick="IO2110.realign(${e.vmid})"
           title="${esc(window.t('js.io2110.engine_redeploy_tip'))}">⟳ ${
           esc(window.t('js.io2110.engine_redeploy'))}</button>` : '';
    // Plan multicast : une adresse de GROUPE par flux (vidéo/ANC/audios séparés). Simulation d'abord
    // — l'opérateur voit le diff AVANT d'accepter le blip (la destination entre dans la signature).
    const planBtn = (typeof PS_CAN_DEPLOY !== 'undefined' && PS_CAN_DEPLOY)
      ? `<button class="io-addrow" onclick="IO2110.mcastPlan(${e.vmid})"
           title="${esc(window.t('js.io2110.mcast_plan_tip'))}">⊞ ${
           esc(window.t('js.io2110.mcast_plan'))}</button>` : '';
    return `<div class="io2110-safety">
      <div class="meta" style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px">
        ${mode}
        <span>${esc(lbl)}${count}</span>
        ${editLink}${applyBtn}${redeployBtn}${planBtn}
      </div>
      ${rl ? _budgetGauge(st.used_queues || 0, (b.rl_tx_cap || 0)) : ''}
      ${rl ? `<div class="meta" style="display:flex; align-items:center; gap:12px; margin:2px 0 6px">
        <span>${_dot(e, false)}${esc(window.t('js.txm.legend_safe'))}</span>
        <span>${_dot(e, true)}${esc(window.t('js.txm.legend_disrupt'))}</span>
      </div>` : ''}
      ${_binBar(e.vmid)}
    </div>`;
  }

  // ── Bac de changements en attente (fenêtre de maintenance) ───────────────────────────────────
  // N changements perturbateurs appliqués ENSEMBLE = UN SEUL recalcul d'arbre = un seul blip.
  const _pending = {};   // vmid → [changements]
  function _binBar(vmid) {
    const list = _pending[vmid] || [];
    if (!list.length) return '';
    const items = list.map(p => `<span class="txm-bin-item">${esc(p.label || p.op)}${
      p.apply_at ? ' · ' + esc(p.apply_at.slice(11, 16)) : ''}
      <button onclick="IO2110.cancelPending(${p.id})" title="${esc(window.t('js.txm.drop'))}">✕</button></span>`).join('');
    const at = list.find(p => p.apply_at);
    return `<div class="txm-bin">
      <span class="txm-bin-count">${list.length}</span>
      <span>${esc(window.t('js.txm.bin_title'))}</span>
      <span class="txm-bin-list">${items}</span>
      <span style="flex:1"></span>
      ${at ? `<span class="meta">${esc(window.t('js.txm.scheduled_at'))} ${esc(at.apply_at.replace('T', ' '))}</span>` : ''}
      <input type="time" id="txm-at-${vmid}" class="io2110-timein" style="width:104px"
             title="${esc(window.t('js.txm.schedule_tip'))}">
      <button class="io-addrow" onclick="IO2110.schedulePending(${vmid})">🕑 ${
        esc(window.t('js.txm.schedule'))}</button>
      <button class="io-addrow" onclick="IO2110.applyPending(${vmid})"
              title="${esc(window.t('js.txm.apply_tip'))}">⬤ ${esc(window.t('js.txm.apply_now'))}</button>
    </div>`;
  }

  function renderDest(d) {
    if (!d.engines || !d.engines.length)
      return '<div class="meta">' + esc(T('js.io2110.no_engine', 'Aucun moteur MTL déployé.')) + '</div>';
    return STYLE + d.engines.map(e => {
      const fb = e.fallback || 'black';
      const sel = v => v === fb ? ' selected' : '';
      const aggGbps = e.nic_aggregate_gbps || 100;
      const nicH = _nicHeader(e.nic_model, aggGbps);
      // Barre TX AGRÉGÉE : nic_tx_gbps = somme de tous les ports → dénominateur = agrégat (40G),
      // pas un seul port (10G). Le détail par port est dans la bande « Par NIC ».
      const nicTxBar = _nicBar(e.nic_tx_gbps, null, aggGbps, 'TX');
      const nicStrip = _nicPortStrip(e, 'tx');   // multi-NIC : bande de ports + toggle (sinon '')
      const ifNet = {}; (e.ports || []).forEach(p => { ifNet[p.ifname] = p.network; });
      const ifPair = {}; if (e.smpte_2022_7) (e.port_pairs || []).forEach(([a, b]) => { ifPair[a] = b; ifPair[b] = a; });
      // Barre Queues XDP multi-segments (B2+), IDENTIQUE à la page Sources (helper _xdpBar) :
      // PLEIN=live · HACHURÉ=planifié · PÂLE=réservé libre (ancré au marqueur) · TRAIT=plafond mtl_init.
      // Repli sur l'ancien rendu allocated/HW si reserved absent (image moteur pré-0.25).
      const xdpAlloc    = e.xdp_allocated;
      const xdpAct      = e.xdp_active ?? 0;
      const xdpReserved = e.xdp_reserved;
      const xdpPlanned  = e.xdp_planned ?? xdpAct;
      // xdp_hw_max_combined est désormais l'agrégat 4 ports fourni par le backend (comme
      // active/reserved/planned) → plus de rustine × _nPorts. Détail par port dans la bande « Par NIC ».
      const _nPorts     = (e.nic_ports || []).length;
      const xdpHwMax    = e.xdp_hw_max_combined;
      const _xdpScope   = _nPorts > 1 ? ' · ' + window.t('js.io2110.all_nics') : '';
      const hasB2 = (xdpReserved != null) && (xdpHwMax != null) && xdpHwMax > 0;
      // Socle DPDK narrow (rl_active) : la barre « Queues XDP » n'a plus de sens (PF vfio, pas de
      // plafond AF-XDP) → « Sessions TX (RL) » sur le cap RL agrégé + files RX (RSS) réservées.
      const hasRL = !!(e.rl_active && e.rl_tx_cap_total);
      let nicXdpBar = '';
      if (hasRL) {
        nicXdpBar = _rlBar(e.rl_tx_sessions, e.rl_tx_cap_total, e.rl_tx_dropped, _xdpScope)
                  + _rssRow(e.rl_rx_queues, _xdpScope);
      } else if (hasB2) {
        nicXdpBar = _xdpBar(xdpAct, xdpPlanned, xdpReserved, xdpHwMax, _xdpScope);
      } else if (xdpAlloc != null) {
        const xdpDen     = xdpHwMax || xdpAlloc;
        const xdpUsedPct = xdpDen ? Math.min(100, Math.round(xdpAlloc / xdpDen * 100)) : 0;
        const xdpOver    = (xdpHwMax != null) && (xdpAlloc > xdpHwMax);
        const xdpEtat = (xdpOver || xdpUsedPct >= 100) ? 'over' : xdpUsedPct > 85 ? 'warn' : '';
        const xdpTxt  = (xdpHwMax != null)
          ? (xdpOver ? T('js.io2110.xdp_over', '{alloc} / {max} files — SUR-CAPACITÉ ({act} actives)').replace('{alloc}', xdpAlloc).replace('{max}', xdpHwMax).replace('{act}', xdpAct)
                     : T('js.io2110.xdp_ok', '{alloc} / {max} files ({pct} %)').replace('{alloc}', xdpAlloc).replace('{max}', xdpHwMax).replace('{pct}', xdpUsedPct))
          : `${xdpAct} / ${xdpAlloc} sessions`;
        nicXdpBar = `<div class="nic-bar-wrap">
          <span class="nic-bar-lbl">Queues XDP</span>
          <span class="nic-bar-val" style="color:${_nicCouleur(xdpEtat)}">${xdpTxt}</span>
          <span class="ctl-gauge${xdpEtat ? ' ' + xdpEtat : ''}" role="img" aria-label="${esc(xdpTxt)}"
            style="--ctl-gauge-w:100%;flex:1;--ctl-gauge-seuil:85%"><span class="ctl-gauge-fill"
            style="transform:scaleX(${(Math.min(100, xdpUsedPct) / 100).toFixed(3)})"></span></span>
        </div>`;
      }
      return `
      <div class="io2110-engine" id="io2110-eng-${e.vmid}" data-vmid="${e.vmid}">
        <h3>${esc(e.hostname)} <small>· ${esc(e.node || '')} · #${e.vmid}</small>
          <span class="io2110-repli">
            <span class="io2110-repli-lbl">${esc(T('js.io2110.fallback_lbl', 'Repli'))}</span>
            <select class="ctl-select io2110-repli-sel" onchange="IO2110.setFallback(${e.vmid},this.value)">
              <option value="none"${sel('none')}>${esc(T('js.io2110.fallback_none', 'Coupé'))}</option>
              <option value="black"${sel('black')}>${esc(T('js.io2110.fallback_black', 'Noir + silence'))}</option>
              <option value="bars"${sel('bars')}>${esc(T('js.io2110.fallback_bars', 'Mire + 1000 Hz'))}</option>
            </select>
          </span>
        </h3>
        ${_layoutPanel(e)}
        ${nicH}${nicTxBar}${nicXdpBar}${nicStrip}
        ${(() => {
          const allTx = e.tx || [];
          const txFull = e.tx_count || 0;
          const remaining = txFull - allTx.length;
          const cards = allTx.map(t => {
            const fps    = t.fps;
            // Santé TX (sous-cadence, epochs ratés) : calculée avec la cadence, sur la ligne
            // VIDÉO — cf. _flowRow. Ne reste ici que la comparaison source/fil ci-dessous.
            // Source vs fil : le fil TIENT la cadence nominale (horloge de sortie) même quand la
            // source est déficitaire — il rejoue alors la trame précédente. ⚠ `fps` ne mesure PAS
            // le fil : il compte les trames NEUVES prises par le worker et sous-estime donc le fil
            // (cf. mtl_rx.c:write_stats, et le bloc de nmos_detail.py). Le seul signal probant
            // d'un déficit RÉEL est `repeats` qui monte — pas un `fps` bas.
            const fpsSrc = t.fps_source;
            const srcDeficit = (fpsSrc != null && fps != null && fpsSrc < fps - 0.15);
            const srcLbl = srcDeficit
              ? window.t('js.io2110.tx_source_deficit').replace('{fps}', fpsSrc.toFixed(1)) : '';
            const srcTip = srcDeficit
              ? window.t('js.io2110.tx_source_deficit_tip')
                  .replace('{fps}', fpsSrc.toFixed(1)).replace('{repeats}', t.repeats || 0) : '';
            return `
          <div class="io2110-txcard">
            <div class="io2110-slothdr ctl-dense">
              <span class="io2110-slotnum">Tx #${t.slot + 1}</span>
              ${_stateChip(e, t)}
              ${/* L'un OU l'autre, jamais les deux : le sélecteur porte déjà le port effectif dans
                    son « Auto (…) », et l'afficher à côté du badge dirait deux fois la même chose
                    — dont une fois sous une forme qui a l'air d'un état alors que c'est un choix.
                    Mono-port : rien à choisir, le badge suffit. Même règle que la carte Sources. */''}
              ${(e.ports || []).length >= 2 ? _portSelect(e, t.slot, t.port)
                                            : _portBadge(t.port, ifNet, ifPair)}
              <span class="io2110-shmin">${_emitState(t, fb)}</span>
              ${/* La cadence et l'état d'émission sont descendus sur la ligne VIDÉO, à côté du
                    format qu'ils qualifient. Ne reste ici que le DÉFICIT DE SOURCE, qui n'est pas
                    un état de sortie : il dit que l'amont fournit moins d'images que le fil n'en
                    émet (trames rejouées) — une information sur le producteur, pas sur le slot. */''}
              ${srcDeficit ? `<span class="io2110-fpsbadge warn" title="${esc(srcTip)}">${esc(srcLbl)}</span>` : ''}
              ${/* Ne restent dans l'en-tête que les réglages qui gouvernent le SLOT ENTIER :
                    le format de la sortie et le rythme d'émission (grille d'epochs de la session).
                    Le générateur, l'IDENT et le SDP sont descendus sur la ligne de l'essence
                    qu'ils concernent — cf. _txGen / _txIdent / _txSdp. */''}
              ${_dot(e, true)}${_pacingSelect(e.vmid, t)}${_fraicheurSelect(e.vmid, t)}
            </div>
            ${_flowRow(e.vmid, t.slot, 'v', T('js.io2110.tag_video', 'VIDÉO'),  'video', t.video, null, (t.bind || {}).video, t, fb)}
            ${(t.audios || []).map((a, ai) => {
                const _row = _flowRow(e.vmid, t.slot, 'a', 'AUD ' + (ai + 1), 'audio', a, ai, ((t.bind || {}).audios || [])[ai], t, fb);
                return _txDpdk(e) ? _row : _txRm(_row, e.vmid, a.flow_id);
              }).join('')}
            ${t.anc_flow_id ? (_txDpdk(e) ? _flowRow(e.vmid, t.slot, 'd', 'ANC', 'anc', t.anc, null, (t.bind || {}).anc, t, fb) : _txRm(_flowRow(e.vmid, t.slot, 'd', 'ANC', 'anc', t.anc, null, (t.bind || {}).anc, t, fb), e.vmid, t.anc_flow_id)) : ''}
            ${_txDpdk(e) ? '' : `<div class="io2110-flowctrls">
              <button type="button" class="btn btn-sm" onclick="IO2110.addFlow(${e.vmid},'audio','${esc(t.flow_id || '')}')">+ Audio</button>
              ${!t.anc_flow_id ? `<button type="button" class="btn btn-sm" onclick="IO2110.addFlow(${e.vmid},'anc','${esc(t.flow_id || '')}')">+ ANC</button>` : ''}
              <button type="button" class="btn btn-sm io2110-rmgrp" onclick="IO2110.removeFlow(${e.vmid},'${esc(t.flow_id || '')}')" title="${esc(T('js.io2110.rm_dest_tip', 'Retirer cette destination'))}">✕ destination</button>
            </div>`}
          </div>`;
          }).join('') || '<div class="meta">' + esc(T('js.io2110.no_tx_slot', 'Aucun slot TX actif.')) + '</div>';
          // Socle DPDK (RL) : le modèle de carte gouverne active_tx_count de façon autoritaire →
          // masquer les +/- TX (redondants/contradictoires) et renvoyer vers Réglages → Réseau →
          // Sorties 2110. Mode AF-XDP : budget de files partagé RX/TX → on GARDE les +/- (balance).
          const dpdkTx = _txDpdk(e);
          const addBtn = (!dpdkTx && remaining > 0)
            ? `<button class="io-addrow" onclick="IO2110.activateTx(${e.vmid})">${esc(T('js.io2110.add_tx', '+ Ajouter un TX'))}</button>`
            : '';
          const delBtn = (!dpdkTx && allTx.length > 0)
            ? `<button class="io-addrow" onclick="IO2110.removeTx(${e.vmid})">− Retirer le dernier TX</button>`
            : '';
          const modelNote = dpdkTx
            ? `<div class="io2110-txmodel-note">${esc(window.t('js.io2110.tx_model_note'))} <a href="/settings#reseau">${esc(window.t('js.io2110.tx_model_link'))}</a></div>`
            : '';
          // Remède famine : ≥1 destination activée mais sans flux (tx_stalled) → réalignement des files.
          const realignBtn = allTx.some(t => t.tx_stalled)
            ? `<button class="io-addrow" onclick="IO2110.realign(${e.vmid})" title="${esc(T('js.io2110.realign_tx_tip', "Une ou plusieurs destinations sont activées mais n'émettent aucun flux. Redéployer le moteur réaligne les files (coupure brève de TOUS les flux)."))}">⟳ Redéployer pour réaligner les files</button>`
            : '';
          return cards + modelNote + addBtn + delBtn + realignBtn;
        })()}
      </div>`;
    }).join('');
  }

  async function _dRefresh() {
    if (!_dEl) return;
    const d = await _fetch();
    if (!_dEl) return;
    // Layout TX déclaré (lecture seule ici, cf. _layoutPanel) : un fetch par moteur, en parallèle,
    // best-effort (un moteur sans NIC média résolue répond juste {state:'no_iface'}).
    const engines = d.engines || [];
    await Promise.all(engines.map(async e => {
      try {
        const r = await fetch(`/api/mtl/${e.vmid}/tx-layout/status`);
        const j = await r.json();
        e._layout = (r.ok && j.ok) ? j.status : null;
      } catch (_) { e._layout = null; }
      await _loadPending(e.vmid);
    }));
    if (!_dEl) return;
    _dEl.innerHTML = renderDest(d);
    _armerRotatifs();
    _marquerCible(_dEl);
  }

  // Rotatifs de taille IDENT : le GESTE (glisser, molette, clavier, remise au défaut) vient du
  // catalogue ; ici on ne branche que ce que la valeur commande. Le POST est étranglé — on tourne
  // en continu, le moteur n'a pas à recevoir un appel par cran.
  const _identThrottle = {};
  function _armerRotatifs() {
    if (!_dEl || !window.MXLControls) return;
    window.MXLControls.attachKnobGestures(_dEl, T('js.io2110.knob_reset', 'Remettre à la taille automatique'));
    if (_dEl._knobBound) return;
    _dEl._knobBound = true;
    _dEl.addEventListener('ctl-knob-input', (e) => {
      const k = e.target.closest('.io2110-identknob');
      if (!k) return;
      const cle = k.dataset.vmid + ':' + k.dataset.slot;
      clearTimeout(_identThrottle[cle]);
      _identThrottle[cle] = setTimeout(
        () => window.IO2110.setIdentSize(+k.dataset.vmid, +k.dataset.slot, e.detail.value), 150);
    });
  }

  // Action moteur DISRUPTIVE (relance mtl_init / recréation → coupure de TOUS les flux) : le serveur
  // la bloque (HTTP 409 + needs_confirm + reason). On confirme explicitement puis on ré-émet avec
  // confirm:true. Les ops à chaud passent normalement. Retourne la Response (null si annulé).
  async function ioMutate(url, payload, opts) {
    payload = payload || {};
    opts = opts || {};
    const post = (body) => fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify(body)});
    let r = await post(payload);
    if (r.status === 409) {
      const j = await r.json().catch(() => ({}));
      if (j && j.needs_confirm) {
        // Étage 2 : le serveur a CALCULÉ que l'action recale l'arbre TX. La modale nomme les sorties
        // qui vont figer et propose de DIFFÉRER (bac de maintenance) quand l'action est différable.
        const choice = await window.txMaintConfirm(j.verdict || {detail: j.reason},
                                                   {allowDefer: !!(j.verdict && j.verdict.deferrable)});
        if (!choice) return null;
        r = await post(Object.assign({}, payload,
                                     choice === 'defer' ? {defer: true} : {confirm: true}));
        if (choice === 'defer' && opts.vmid) await _loadPending(opts.vmid);
      }
    }
    return r;
  }

  // Bac de maintenance : état + actions (appliquer / planifier / retirer un changement).
  async function _loadPending(vmid) {
    try {
      const r = await fetch(`/api/tx-maintenance?vmid=${vmid}`);
      const j = await r.json();
      _pending[vmid] = (j.ok && j.pending) ? j.pending : [];
    } catch (_) { _pending[vmid] = []; }
  }

  window.MXLPlugins = window.MXLPlugins || {};
  window.MXLPlugins.sources_2110 = {
    mount(el, vmid) { mountSources(el, vmid); },
    // Deep-link vers un autre moteur alors que l'onglet est DÉJÀ monté (plugin_section.html).
    focus(vmid) { _viser(vmid); _marquerCible(_sEl); },
    unmount() { _sEl = null; try { window.MXLPlugins[RXTYPE] && window.MXLPlugins[RXTYPE].unmount(); } catch (_) {} },
  };
  window.MXLPlugins.destinations_2110 = {
    focus(vmid) { _viser(vmid); _marquerCible(_dEl); },
    async mount(el, vmid) {
      _dEl = el; _viser(vmid); if (_dTimer) { _dTimer.stop(); _dTimer = null; }
      await _ensureAsset('css');   // partage le CSS du plugin (classes flow-row, gen-badge, etc.)
      await _ensureVideoFormats();   // presets pour le sélecteur de résolution (scripts.js absent ici)
      _dTimer = window.MXLPoll(_dRefresh, 3000);   // poll sans recouvrement (cf. layout.html)
    },
    unmount() { if (_dTimer) _dTimer.stop(); _dTimer = null; _dEl = null; },
  };

  window.IO2110 = {
    // Multi-NIC : moteurs dont le détail « Par NIC » est déplié (persisté entre les refresh 3s).
    _nicExpanded: new Set(),
    toggleNic(vmid) {
      if (this._nicExpanded.has(vmid)) this._nicExpanded.delete(vmid);
      else this._nicExpanded.add(vmid);
      _dRefresh();
    },
    // C2b+ : lier un flux d'un slot TX à une ressource NMOS du registre (rebinding explicite).
    // La ressource fait autorité (push-down mcast/format) → l'édition directe du slot est ensuite
    // refusée (409) tant que lié.
    async bindFlow(vmid, slotKey, essence) {
      let resources = [];
      try {
        const j = await (await fetch('/api/nmos/registry')).json();
        resources = (j.resources || []).filter(r => r.kind === 'sender' && r.essence === essence);
      } catch (_) {}
      if (!resources.length) { (window.tpToast || alert)(T('js.io2110.no_nmos_sender', 'Aucune ressource NMOS sender compatible (créez-en une dans Réglages → NMOS)')); return; }
      const modal = document.createElement('div');
      modal.className = 'io2110-tone-modal';
      const opts = resources.map(r => `<option value="${r.id}">${esc(r.label || r.id.slice(0, 8))}${r.active ? ' · actif' : ' · orphelin'}</option>`).join('');
      modal.innerHTML = `<div class="io2110-tone-box" style="width:440px;max-width:92vw">
        <div style="margin-bottom:10px"><strong>Lier ${esc(slotKey)} à une ressource NMOS</strong></div>
        <select id="io2110-bindsel" style="width:100%;margin-bottom:12px">${opts}</select>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="io-addrow" id="io2110-bindcancel">Annuler</button>
          <button class="io-addrow" id="io2110-bindok">Lier</button>
        </div></div>`;
      document.body.appendChild(modal);
      const close = () => modal.remove();
      modal.addEventListener('click', e => { if (e.target === modal) close(); });
      modal.querySelector('#io2110-bindcancel').onclick = close;
      modal.querySelector('#io2110-bindok').onclick = async () => {
        const rid = modal.querySelector('#io2110-bindsel').value;
        close();
        const r = await fetch('/api/nmos/bind', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resource_id: rid, vmid, slot_key: slotKey }),
        });
        const j = await r.json().catch(() => ({}));
        if (!j.ok) (window.tpToast || alert)(j.error || T('js.io2110.bind_failed', 'échec de la liaison'));
        _dRefresh();
      };
    },
    async unbindFlow(vmid, slotKey) {
      const r = await fetch('/api/nmos/unbind', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vmid, slot_key: slotKey }),
      });
      const j = await r.json().catch(() => ({}));
      if (!j.ok) (window.tpToast || alert)(j.error || T('js.io2110.failed', 'échec'));
      _dRefresh();
    },
    // Affiche le SDP (transportfile) dans une modale plutôt que de le télécharger
    // (parité avec la page Sources 2110, qui ouvre une modale).
    async viewSdp(href, label) {
      let txt = '';
      try {
        const r = await fetch(href);
        txt = await r.text();
        if (!r.ok) txt = `(HTTP ${r.status})\n` + txt;
      } catch (e) { txt = T('js.io2110.sdp_fetch_err', 'Erreur de récupération du SDP : ') + e; }
      const modal = document.createElement('div');
      modal.className = 'io2110-tone-modal';
      const dl = String(label || 'sender').replace(/[^\w.-]+/g, '_');
      modal.innerHTML = `<div class="io2110-tone-box" style="width:680px;max-width:92vw">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px">
          <strong style="font-size:1.02em">SDP — ${esc(label || '')}</strong>
          <span style="display:flex;gap:8px;flex:none">
            <button class="io-addrow" id="sdp-copy">Copier</button>
            <a class="btn btn-sm" href="${esc(href)}" download="${esc(dl)}.sdp">${esc(T('js.io2110.sdp_download', 'Télécharger'))}</a>
            <button class="io-addrow" id="sdp-close">Fermer</button>
          </span>
        </div>
        <textarea id="sdp-ta" readonly spellcheck="false" rows="14"
          style="width:100%;box-sizing:border-box;font-family:var(--font-mono,monospace);font-size:0.8em;
                 padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg,#111);
                 color:var(--text);resize:vertical">${esc(txt)}</textarea>`;
      document.body.appendChild(modal);
      const onKey = ev => { if (ev.key === 'Escape') close(); };
      const close = () => { modal.remove(); document.removeEventListener('keydown', onKey); };
      document.addEventListener('keydown', onKey);
      modal.addEventListener('click', ev => { if (ev.target === modal) close(); });
      modal.querySelector('#sdp-close').addEventListener('click', close);
      modal.querySelector('#sdp-copy').addEventListener('click', () => {
        window.copierTexte(txt);
        const b = modal.querySelector('#sdp-copy'), o = b.textContent;
        b.textContent = T('js.io2110.copied', 'Copié ✓'); setTimeout(() => { b.textContent = o; }, 1200);
      });
    },

    async setDest(vmid, slot, essence, leg, cur, audioIdx) {
      const aiSuffix = (essence === 'audio' && audioIdx != null) ? ` #${audioIdx + 1}` : '';
      const lbl = { video: T('js.io2110.ess_video', 'Vidéo'), audio: 'Audio', anc: 'ANC' }[essence] || essence;
      const v = prompt(`Destination 2110 — ${lbl}${aiSuffix}${leg ? ' (leg 2022-7)' : ''} (multicast:port) :`, cur || '239.0.0.1:5000');
      if (!v) return;
      const m = v.trim().match(/^(\d+\.\d+\.\d+\.\d+):(\d+)$/);
      if (!m) { alert('Format attendu : multicast:port (ex. 239.95.17.50:2120)'); return; }
      const body = { essence, leg, mcast: m[1], port: parseInt(m[2]) };
      if (essence === 'audio' && audioIdx != null) body.audio_idx = audioIdx;
      const r = await ioMutate(`/api/mtl/${vmid}/tx/${slot}/dest`, body, {vmid});
      if (!r) { _dRefresh(); return; }
      const j = await r.json().catch(() => ({}));
      if (!j.ok && j.error && !j.deferred) alert(j.error);
      _dRefresh();
    },

    // Épinglage du port d'une sortie (ou retour à la répartition auto si iface vide). Même
    // endpoint et même sémantique qu'en réception ; on laisse au moteur le temps de déplacer la
    // session avant de relire, comme le fait la carte Sources.
    // Ouvre la liste de format d'une sortie. Le rendu suivant la pose à la place du texte, et on
    // lui donne le focus : au clavier comme à la souris, elle est alors prête à être déroulée.
    editFormat(vmid, slot) {
      _fmtEdit = `${vmid}:${slot}`;
      _dRefresh();
      setTimeout(() => {
        const el = _dEl && _dEl.querySelector('.io-fmt-sel');
        if (el) el.focus();
      }, 60);
    },
    // Refermer sans choisir : on revient au texte. Le `setTimeout` laisse le `change` éventuel
    // passer avant le `blur` — sinon quitter la liste au clavier annulerait la sélection.
    closeFormat() {
      setTimeout(() => { if (_fmtEdit) { _fmtEdit = null; _dRefresh(); } }, 120);
    },

    async setPort(vmid, slot, sel) {
      const iface = sel.value;
      sel.disabled = true;
      try {
        const r = await ioMutate(`/api/mtl/${vmid}/pin`, { role: 'tx', idx: slot, iface }, { vmid });
        if (r && !r.ok) {
          const j = await r.json().catch(() => ({}));
          (window.tpToast || alert)(j.error || T('js.io2110.port_pin_failed', "Échec de l'épinglage de port"));
        }
      } catch (_) { (window.tpToast || alert)(T('js.io2110.net_error', 'Erreur réseau')); }
      finally { sel.disabled = false; }
      setTimeout(_dRefresh, 700);
    },

    async toggleGen(vmid, slot) {
      const d = await _fetch();
      const eng = (d.engines || []).find(e => e.vmid === vmid);
      const tx  = eng && (eng.tx || []).find(t => t.slot === slot);
      const enabled = tx ? !tx.gen : true;
      const r = await fetch(`/api/containers/${vmid}/control/gen_tx`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idx: slot, enabled }),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    async toggleIdent(vmid, slot) {
      const d = await _fetch();
      const eng = (d.engines || []).find(e => e.vmid === vmid);
      const tx  = eng && (eng.tx || []).find(t => t.slot === slot);
      const enabled = tx ? !tx.ident : true;
      const r = await fetch(`/api/containers/${vmid}/control/ident_tx`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idx: slot, enabled }),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    // Taille IDENT. Le contrôle est désormais un rotatif du catalogue ; cette méthode ne fait plus
    // que POSTER, et elle est appelée en RAFALE pendant qu'on tourne — d'où l'étranglement, et
    // surtout l'absence de `_dRefresh()` : re-rendre la carte à chaque cran arracherait le
    // rotatif des doigts de l'exploitant. Le rendu suivant (3 s) reprend la valeur du moteur.
    async setIdentSize(vmid, slot, n) {
      n = parseInt(n, 10);
      if (!(n >= 10 && n <= 120)) return;
      const r = await fetch(`/api/containers/${vmid}/control/ident_tx`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idx: slot, size: n }),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) (window.tpToast || alert)(j.error);
    },

    async editTone(vmid, slot, ai) {
      // Récupère la config courante de tonalité du flux audio (slot, ai).
      const d = await _fetch();
      const eng = (d.engines || []).find(e => e.vmid === vmid);
      const tx  = eng && (eng.tx || []).find(t => t.slot === slot);
      const a   = tx && (tx.audios || [])[ai];
      const tn  = (a && a.tone) || { enabled: false, freq: 1000, level_db: -18,
                                     active: Array(8).fill(true), rupted: Array(8).fill(false) };
      // État local par canal : 0=muet, 1=tonalité, 2=tonalité+ruptures.
      const st = [];
      for (let c = 0; c < 8; c++) {
        const on = (tn.active || [])[c], rup = (tn.rupted || [])[c];
        st[c] = on ? (rup ? 2 : 1) : 0;
      }
      const CLS = ['mute', 'on', 'rup'], LBL = ['muet', 'actif', 'ruptures'];
      const modal = document.createElement('div');
      modal.className = 'io2110-tone-modal';
      modal.innerHTML = `<div class="io2110-tone-box">
        <h4>${esc(T('js.io2110.tone_title', '♪ Générateur de tonalité — Tx #{slot} · audio {ai}')
              .replace('{slot}', slot + 1).replace('{ai}', ai + 1))}</h4>
        <div class="tt-row"><label>${esc(T('js.io2110.tone_enabled', 'Activé'))}</label>
          <input type="checkbox" class="ctl-switch" id="tt-en" ${tn.enabled ? 'checked' : ''}></div>
        <div class="tt-row"><label>${esc(T('js.io2110.tone_freq', 'Fréquence'))}</label>
          <input type="number" id="tt-freq" min="20" max="20000" step="1" value="${tn.freq || 1000}"> Hz</div>
        <div class="tt-row"><label>${esc(T('js.io2110.tone_level', 'Niveau'))}</label>
          <input type="number" id="tt-lvl" min="-60" max="0" step="1" value="${tn.level_db != null ? tn.level_db : -18}"> dBFS</div>
        <div class="tt-row"><label>${esc(T('js.io2110.tone_chans', 'Canaux'))}</label><span class="meta">${esc(T('js.io2110.tone_chans_hint', 'cliquer pour cycler'))}</span></div>
        ${/* Tranches du catalogue : une colonne par canal, l'état par `aria-pressed` et le
              ruptage par l'étiquette d'alerte — donc lisible sans la couleur, et annoncé. Les
              pavés d'avant étaient des <div> cliquables : ni tabulables, ni actionnables au
              clavier, et leurs trois états ne tenaient QUE dans la teinte. */''}
        <div class="ctl-strips io2110-tone-chans">
          ${st.map((s, c) => `<button type="button" class="ctl-strip" data-ch="${c}"
            aria-pressed="${s >= 1}" title="canal ${c + 1} — ${LBL[s]}"><span
            class="ctl-strip-num">${c + 1}</span><span class="ctl-strip-tag">${s === 2 ? 'rupt' : ''}</span></button>`).join('')}
        </div>
        <div class="io2110-tone-legend">Actif · « rupt » = actif + ruptures · éteint = muet — cliquer pour cycler</div>
        <div class="io2110-tone-btns">
          <button class="btn" id="tt-cancel">Annuler</button>
          <button class="btn btn-primary" id="tt-apply">Appliquer</button>
        </div>
      </div>`;
      document.body.appendChild(modal);
      const close = () => modal.remove();
      modal.addEventListener('click', ev => { if (ev.target === modal) close(); });
      modal.querySelectorAll('.io2110-tone-chans .ctl-strip').forEach(el => {
        el.addEventListener('click', () => {
          const c = +el.dataset.ch;
          st[c] = (st[c] + 1) % 3;
          el.setAttribute('aria-pressed', String(st[c] >= 1));
          el.querySelector('.ctl-strip-tag').textContent = st[c] === 2 ? 'rupt' : '';
          el.title = `canal ${c + 1} — ${LBL[st[c]]}`;
        });
      });
      modal.querySelector('#tt-cancel').addEventListener('click', close);
      modal.querySelector('#tt-apply').addEventListener('click', async () => {
        const enabled = modal.querySelector('#tt-en').checked;
        const freq = parseInt(modal.querySelector('#tt-freq').value, 10) || 1000;
        const level_db = parseFloat(modal.querySelector('#tt-lvl').value);
        const active = st.map(s => s >= 1);
        const rupted = st.map(s => s === 2);
        const r = await fetch(`/api/containers/${vmid}/control/tone_tx`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ idx: slot, ai, enabled, freq,
                                 level_db: isNaN(level_db) ? -18 : level_db, active, rupted }),
        });
        const j = await r.json().catch(() => ({}));
        if (j.error) alert(j.error);
        close(); _dRefresh();
      });
    },

    async activateTx(vmid) {
      const r = await ioMutate(`/api/mtl/${vmid}/activate`, {kind: 'tx'});
      if (!r) return;
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    // Layout TX déclaré (docs/reference/TX_LAYOUTS.md étage 1) : provisionne SILENCIEUSEMENT tous les slots du
    // layout de la NIC de ce moteur (auto-alloc destinations manquantes + provisioned=True poussé
    // au contrôleur). Événement de maintenance (recalcule l'arbre RL du port) — pas de câble touché.
    async applyLayout(vmid, iface) {
      // Provisionne SILENCIEUSEMENT les sorties déclarées (sessions + feuilles RL, sans câble).
      // Événement de maintenance : le serveur calcule le verdict et la modale nomme les victimes ;
      // l'action est différable (bac) → un seul blip pour tout un lot de changements.
      const r = await ioMutate(`/api/mtl/${vmid}/tx-layout/apply`,
                               iface ? {iface} : {}, {vmid});
      if (!r) return;
      const j = await r.json().catch(() => ({}));
      if (!r.ok && !j.deferred) { alert(j.error || `HTTP ${r.status}`); return; }
      _dRefresh();
    },

    // Re-planifie les adresses multicast des sorties (une adresse de groupe par flux). Deux temps :
    // SIMULATION (diff affiché, rien de touché) puis application gatée étage 2 (un seul commit TM).
    async mcastPlan(vmid) {
      const r0 = await fetch(`/api/mtl/${vmid}/tx/mcast-plan`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
      const j0 = await r0.json().catch(() => ({}));
      if (!r0.ok) { alert(j0.error || `HTTP ${r0.status}`); return; }
      const chg = (j0.diff || []).filter(d => d.etat === 'a_changer');
      if (!chg.length) { alert(window.t('js.io2110.mcast_plan_none')); return; }
      const lignes = chg.map(d => `TX #${d.slot + 1} ${d.essence}${
        d.essence === 'audio' ? ' #' + (d.audio_idx + 1) : ''} : ${d.de} → ${d.vers}:${d.port}`);
      if (!confirm(window.t('js.io2110.mcast_plan_confirm').replace('{n}', chg.length)
                   + '\n\n' + lignes.join('\n'))) return;
      const r = await ioMutate(`/api/mtl/${vmid}/tx/mcast-plan`, {apply: true}, {vmid});
      if (!r) return;
      const j = await r.json().catch(() => ({}));
      if (!r.ok && !j.deferred) { alert(j.error || `HTTP ${r.status}`); return; }
      if ((j.conflicts || []).length) {
        alert(window.t('js.io2110.mcast_plan_conflict').replace('{n}', j.conflicts.length));
      }
      _dRefresh();
    },

    async applyPending(vmid) {
      const r = await fetch(`/api/mtl/${vmid}/tx-maintenance/apply`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
      const j = await r.json().catch(() => ({}));
      if (j.errors && j.errors.length) alert(j.errors.join('\n'));
      await _loadPending(vmid);
      _dRefresh();
    },

    async schedulePending(vmid) {
      const el = document.getElementById(`txm-at-${vmid}`);
      const hhmm = el && el.value;
      if (!hhmm) { alert(window.t('js.txm.pick_time')); return; }
      // « à HH:MM » = aujourd'hui si l'heure est à venir, sinon demain.
      const now = new Date();
      const d = new Date(`${now.toISOString().slice(0, 10)}T${hhmm}`);
      if (d <= now) d.setDate(d.getDate() + 1);
      const at = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${
        String(d.getDate()).padStart(2, '0')}T${hhmm}`;
      await fetch(`/api/mtl/${vmid}/tx-maintenance/apply`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({at})});
      await _loadPending(vmid);
      _dRefresh();
    },

    async cancelPending(pid) {
      await fetch(`/api/tx-maintenance/${pid}`, {method: 'DELETE'});
      _dRefresh();
    },

    // Remède famine : redéploie le moteur (réaligne les files). DISRUPTIF → ioMutate confirme.
    async realign(vmid) {
      const r = await ioMutate(`/api/mtl/${vmid}/realign`, {});
      if (!r) return;
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    async removeTx(vmid) {
      const r = await fetch(`/api/mtl/${vmid}/deactivate`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: 'tx'}),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    // « Option A » : ajoute un flux audio/ANC à une destination (attaché à sa vidéo si attachedTo,
    // sinon indépendant). Dans la limite du pool TX pré-provisionné (sinon redéploiement requis).
    async addFlow(vmid, essence, attachedTo) {
      const r = await ioMutate(`/api/mtl/${vmid}/flows/add`,
        {role: 'tx', essence, attached_to: attachedTo || null});
      if (!r) return;
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    // Retire un flux par id (une vidéo retire aussi ses audios/ANC attachés).
    async removeFlow(vmid, fid) {
      if (!fid) return;
      const r = await fetch(`/api/mtl/${vmid}/flows/remove`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: fid}),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    async setFallback(vmid, mode) {
      const r = await fetch(`/api/mtl/${vmid}/tx/fallback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const j = await r.json().catch(() => ({}));
      if (j.error) alert(j.error);
      _dRefresh();
    },

    // Rythme d'émission (mode tranche) : 0 = image suivante (défaut), >0 = grille décalée de N µs.
    async setPacing(vmid, slot, val) {
      const r = await ioMutate(`/api/mtl/${vmid}/tx/${slot}/pacing`,
        { epoch_shift_us: parseInt(val, 10) || 0 }, {vmid});
      if (!r) { _dRefresh(); return; }
      const j = await r.json().catch(() => ({}));
      if (!j.ok && j.error && !j.deferred) alert(j.error);
      _dRefresh();
    },

    // Choix de la trame émise : 1 = la plus récente prête (défaut), 0 = la plus ancienne.
    async setFraicheur(vmid, slot, val) {
      const r = await ioMutate(`/api/mtl/${vmid}/tx/${slot}/serve_newest`,
        { serve_newest: parseInt(val, 10) || 0 }, {vmid});
      if (!r) { _dRefresh(); return; }
      const j = await r.json().catch(() => ({}));
      if (!j.ok && j.error && !j.deferred) alert(j.error);
      _dRefresh();
    },

    // Résolution du générateur (mire) d'une sortie GÉN. Lit le preset choisi (data-*) → POST format.
    async setFormat(vmid, slot, sel) {
      const o = sel.selectedOptions[0];
      _fmtEdit = null;                 // choix fait : on retourne au texte, quoi qu'il advienne
      if (!o || !o.dataset.w) { _dRefresh(); return; }
      // Le format entre dans la signature de session → recréation = recalage de l'arbre TX du port
      // (sauf slot câblé qui suit sa source : le serveur le calcule et laisse passer sans modale).
      const corps = { width: parseInt(o.dataset.w), height: parseInt(o.dataset.h),
                      fps: parseFloat(o.dataset.fps), scan: o.dataset.scan };
      // La profondeur ne part QUE si le format en déclare une : sans ça on écraserait le réglage
      // en service par un 0 silencieux. Elle entre dans la signature de session, donc le serveur
      // demandera confirmation si la sortie doit être recréée — c'est le but.
      if (o.dataset.depth) corps.bit_depth = parseInt(o.dataset.depth);
      const r = await ioMutate(`/api/mtl/${vmid}/tx/${slot}/format`, corps, {vmid});
      if (!r) { _dRefresh(); return; }
      const j = await r.json().catch(() => ({}));
      if (!j.ok && j.error && !j.deferred) alert(j.error);
      _dRefresh();
    },
  };
})();
