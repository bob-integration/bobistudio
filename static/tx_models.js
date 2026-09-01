/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France — Auteur : Cyril Mazouer.
 *
 * BIBLIOTHÈQUE DE MODÈLES DE CARTE 2110 (Réglages → Réseau → « Modèles de carte 2110 »).
 *
 * Deux temps, deux objets (cf. app/tx_card_models.py) :
 *   1. ICI on RÈGLE des modèles par TYPE DE CARTE. Un modèle est une DÉCLARATION : il ne touche aucun
 *      matériel, il ne coûte RIEN.
 *   2. On les APPLIQUE à une carte réelle (Réseau → Interfaces) : c'est LÀ que le coût se paie (DPDK :
 *      recalcul de l'arbre de pacing = arrêt du port ~2,2 s ; AF-XDP : aucun rate limiter → gratuit).
 *
 * ── AGENCEMENT (refonte, compétence `impeccable`, registre PRODUIT) ───────────────────────────────
 * Le geste courant N'EST PAS de saisir 16 sorties une par une : dans la vraie vie une carte porte
 * 8 ou 16 sorties du MÊME format. L'éditeur est donc construit autour de « PRÉRÉGLAGE × QUANTITÉ » :
 *   · COMPOSER (en tête, le geste principal) : un préréglage de Réglages → Vidéo + une quantité
 *     (steppers globaux : Maj = ×10) + « Ajouter » → N sorties identiques d'un coup. Le budget de
 *     files se met à jour AVANT le clic, et si les N ne tiennent pas, le bouton est désactivé et DIT
 *     pourquoi (jamais de refus après coup) ;
 *   · GROUPES (le corps) : les sorties identiques sont REGROUPÉES en une ligne « 8 × 3G 1080p50 ».
 *     Éditer le groupe (préréglage, audio, ANC, quantité) édite ses N sorties d'un coup — c'est aussi
 *     la réponse au « changer le format de plusieurs sorties » : pas de sélection multiple à inventer,
 *     le groupe EST la sélection. Rien n'est caché : « déplier » liste chaque TX, éditable une à une
 *     (le cas hétérogène reste possible, il n'est simplement plus le chemin par défaut) ;
 *   · BUDGET DE FILES en en-tête COLLANT : c'est l'information vitale (elle décide de la validité),
 *     elle reste à l'écran pendant qu'on ajoute/retire ;
 *   · PÉDAGOGIE repliée (<details>) : elle informe, elle ne mange pas l'écran.
 *
 * Format vidéo : PRÉRÉGLAGE issu de Réglages → Vidéo (window.loadVideoFormats, static/video_formats.js
 * — source unique, jamais recopiée). On stocke les VALEURS (w/h/fps/scan/bits + chroma + colorimétrie),
 * pas le nom : renommer un format dans les Réglages ne doit pas casser un modèle en silence. Le label
 * est un affichage, et un format devenu orphelin de la liste est SIGNALÉ (info, pas erreur).
 */
(function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const T = (k, fb) => (window.t ? (window.t(k) || fb) : fb);

  const S = {el: null, models: [], types: [], sel: null, draft: null, dirty: false,
             q: '', filterType: '', msg: '', err: '', busy: false,
             fmts: [], ptime: 1,          // préréglages vidéo + ptime audio GLOBAL du moteur (lecture)
             addKind: 'video',            // essence de la sortie à composer : video | audio | anc
             addFmt: 0, addQty: 1, addAudio: 1, addAnc: false,   // le COMPOSER (geste principal)
             open: {}};                   // groupes dépliés (clé de groupe → true)

  // ★ VIDÉO OPTIONNELLE : une sortie est vidéo (+ audio/ANC), audio-seule ou ANC-seule. `video` vaut
  // null pour une sortie sans vidéo. Le coût en files RL suit : 1 si vidéo + 1/audio + 1 si ANC.
  const cost = s => (s.video ? 1 : 0) + (parseInt(s.audio_count) || 0) + (s.anc ? 1 : 0);
  // Essence dominante d'une sortie (libellé + rendu).
  const kindOf = s => s.video ? 'video' : ((parseInt(s.audio_count) || 0) > 0 ? 'audio' : 'anc');
  const kindLabel = k => k === 'audio' ? T('js.txlib.kind_audio', 'Audio seul')
                       : k === 'anc'   ? T('js.txlib.kind_anc', 'ANC seul')
                       : T('js.txlib.kind_video', 'Vidéo');
  // Sortie décrite par l'état COURANT du composer (selon l'essence choisie).
  function _composerSlot() {
    if (S.addKind === 'audio')
      return {video: null, fmt_label: '', audio_count: Math.max(1, parseInt(S.addAudio) || 1), anc: false};
    if (S.addKind === 'anc')
      return {video: null, fmt_label: '', audio_count: 0, anc: true};
    const f = S.fmts[S.addFmt];
    return {...(f ? slotOf(f) : {video: {}, fmt_label: ''}),
            audio_count: parseInt(S.addAudio) || 0, anc: !!S.addAnc};
  }
  const composerUnit = () => cost(_composerSlot());
  const totalCost = d => (d.slots || []).reduce((n, s) => n + cost(s), 0);
  const audioFlows = d => (d.slots || []).reduce((n, s) => n + (parseInt(s.audio_count) || 0), 0);
  const capOf = key => {
    const t = S.types.find(t => t.key === (key || '').toLowerCase());
    return t ? t.rl_tx_cap : 0;
  };
  const fmtLabel = v => (window.videoFormatLabel ? window.videoFormatLabel(v) : `${v.w}×${v.h}`);
  const isOrphan = v => !!(v && v.w) && !(window.videoFormatMatch && window.videoFormatMatch(v));
  const slotOf = f => ({video: {w: f.w, h: f.h, fps: f.fps, scan: f.scan, bd: f.bit_depth,
                                chroma: f.chroma, colorimetry: f.colorimetry},
                        fmt_label: f.label});

  // ── DÉBIT VIDÉO (garde-fou de LIEN) — MIROIR EXACT du backend io2110_layouts._slot_bw_mbps :
  //    W×H×fps×bpp, bpp = 20 bits/pixel si ≥10 bits, sinon 16. Sortie SANS vidéo (audio/ANC seul) → 0
  //    (négligeable pour ce garde-fou). ★ ENTRELACÉ : un champ ne transmet que la MOITIÉ des lignes, et
  //    la convention fps de video_formats.js compte les CHAMPS (1080i50 ⇒ fps=50). Diviser par 2 rend
  //    donc le débit RÉEL : 1080i50 ≈ 1080p25 ≈ moitié de 1080p50.
  function _slotBw(s) {
    let bw = 0;
    const v = s && s.video;
    if (v) {
      const bpp = (parseInt(v.bd) || 10) >= 10 ? 20 : 16;
      bw = ((parseInt(v.w) || 0) * (parseInt(v.h) || 0) * (parseFloat(v.fps) || 0) * bpp) / 1e6;
      if (v.scan === 'i') bw /= 2;
    }
    // Audio 2110-30 : ~9,2 Mb/s/flux (L24/48k, 8ch — estimation conservatrice) — MIROIR du backend.
    // Compté même sans vidéo (audio-seul). ANC négligeable → ignoré.
    bw += (parseInt(s && s.audio_count) || 0) * (48000 * 24 * 8 / 1e6);
    return bw;
  }
  const totalBw = d => (d.slots || []).reduce((n, s) => n + _slotBw(s), 0);
  // Débit du LIEN de la carte (Mb/s), depuis le type sélectionné. 0 = type inconnu (aucune carte connue
  // de ce modèle) → on affiche le débit agrégé SANS comparaison ni alarme (jamais d'échec silencieux).
  const speedOf = key => {
    const t = S.types.find(t => t.key === (key || '').toLowerCase());
    return t ? (parseInt(t.speed_mbps) || 0) : 0;
  };
  const gbps = mbps => (mbps / 1000).toFixed(1);
  // Couleur du débit via TOKENS de thème (jamais de couleur en dur). Lien inconnu → neutre (running).
  const bwColor = (bw, speed) => (speed && bw > speed) ? 'var(--status-stopped-fg)'
                : (speed && bw / speed > 0.85) ? 'var(--status-warning-fg)'
                : 'var(--status-running-fg)';
  // Ligne « débit après ajout » du composer. Lien connu → comparé (t / c) ; inconnu → agrégé seul, et
  // on le DIT (pas de comparaison muette).
  const _composerBwText = (afterBw, speed) => speed
    ? T('js.txlib.bw_add', '→ {t} / {c} Gbps de débit après ajout')
        .replace('{t}', gbps(afterBw)).replace('{c}', gbps(speed))
    : T('js.txlib.bw_add_unknown', '→ {t} Gbps de débit après ajout (lien inconnu)')
        .replace('{t}', gbps(afterBw));

  // ─── Chargement ────────────────────────────────────────────────────────────────────────────────
  async function load(keepSel) {
    if (window.loadVideoFormats) S.fmts = (await window.loadVideoFormats()) || [];
    try {
      const st = await (await fetch('/api/settings')).json();
      S.ptime = parseFloat(st.mtl_audio_ptime || 1) || 1;      // ptime GLOBAL (cf. bandeau audio)
    } catch (_) { S.ptime = 1; }
    const j = await call('/api/tx-card-models');
    if (!j) { render(); return; }
    S.models = j.models || [];
    S.types = j.types || [];
    if (keepSel === undefined) keepSel = S.sel;
    S.sel = S.models.some(m => m.id === keepSel) ? keepSel : (S.models[0] ? S.models[0].id : null);
    S.draft = _draftOf(S.sel);
    S.dirty = false;
    _resetComposer();
    render();
  }
  function _draftOf(id) {
    const m = S.models.find(x => x.id === id);
    if (!m) return null;
    return {id: m.id, name: m.name, nic_model: m.nic_model || '', notes: m.notes || '',
            slots: (m.slots || []).map(s => ({video: s.video ? {...s.video} : null,  // null = sans vidéo
                                              fmt_label: s.fmt_label || '',
                                              audio_count: s.audio_count || 0, anc: !!s.anc}))};
  }
  // Pré-remplissage du composer : préréglage = celui des Réglages (video_format_default) ; quantité =
  // ce qui RESTE de place dans la carte (bornée à 8, la taille de mur courante), au minimum 1.
  function _resetComposer() {
    const di = S.fmts.findIndex(f => f.label === window._videoFormatDefault);
    S.addFmt = di >= 0 ? di : 0;
    S.addKind = 'video'; S.addAudio = 1; S.addAnc = false;
    S.addQty = Math.max(1, Math.min(8, _fits()));
  }
  // Combien de sorties du gabarit COURANT du composer tiennent encore dans la carte ?
  function _fits() {
    const d = S.draft;
    const cap = d ? capOf(d.nic_model) : 0;
    if (!d || !cap) return 1;
    const unit = composerUnit() || 1;
    return Math.max(0, Math.floor((cap - totalCost(d)) / unit));
  }

  // Tout appel réseau passe par ici : un échec remonte SON message et s'AFFICHE (bandeau de tête).
  async function call(url, opts) {
    S.err = ''; S.msg = '';
    try {
      const r = await fetch(url, opts);
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { S.err = j.error || `HTTP ${r.status}`; return null; }
      return j;
    } catch (e) {
      S.err = e.message || String(e);
      return null;
    }
  }

  // ─── Rendu ─────────────────────────────────────────────────────────────────────────────────────
  // Options de préréglage. Un format hors liste (renommé/supprimé dans les Réglages) reste EN TÊTE,
  // sélectionné, marqué : il n'est jamais réécrit en douce.
  function _presetOptions(v, sel) {
    const cur = (v && window.videoFormatMatch) ? window.videoFormatMatch(v) : null;
    const opts = S.fmts.map((f, i) =>
      `<option value="${i}"${(sel != null ? i === sel : cur === f) ? ' selected' : ''}>${
        esc(f.label)}</option>`).join('');
    const orphan = (v && !cur && v.w)
      ? `<option value="-1" selected>${esc(fmtLabel(v))} — ${esc(T('js.txlib.fmt_orphan', 'hors liste'))}</option>`
      : '';
    return orphan + opts;
  }

  function _rail(list) {
    const typeOpts = `<option value="">${esc(T('js.txlib.all_types', 'Tous les types'))}</option>` +
      S.types.map(t => `<option value="${esc(t.key)}"${t.key === S.filterType ? ' selected' : ''}>${
        esc(t.model)}</option>`).join('');
    const items = list.map(m => `
      <li class="txlib-item${m.id === S.sel ? ' sel' : ''}" role="option" tabindex="0"
          aria-selected="${m.id === S.sel}" onclick="TxModels.select(${m.id})"
          onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();TxModels.select(${m.id})}">
        <span class="txlib-item-name">${esc(m.name)}</span>
        <span class="txlib-item-meta">${m.slot_count} ${esc(T('js.txlib.outputs', 'sorties'))} ·
          <span class="${m.valid ? '' : 'txlib-bad'}" title="${esc((m.errors || []).join(' '))}">${
            m.used_queues}/${m.rl_tx_cap}</span>${m.bw_mbps
            ? ' · ' + esc(gbps(m.bw_mbps)) + ' ' + esc(T('js.txlib.gbps', 'Gbps')) : ''}</span>
      </li>`).join('');
    return `
      <aside class="txlib-rail">
        <div class="txlib-railtools">
          <input type="search" placeholder="${esc(T('js.txlib.search', 'Rechercher…'))}" value="${esc(S.q)}"
            data-fk="q" oninput="TxModels.setQuery(this.value)"
            aria-label="${esc(T('js.txlib.search', 'Rechercher…'))}">
          <select data-fk="qtype" oninput="TxModels.setType(this.value)"
            aria-label="${esc(T('js.txlib.type', 'Type de carte'))}">${typeOpts}</select>
        </div>
        <ul class="txlib-list" role="listbox">${items || `<li class="txlib-none">${
          esc(T('js.txlib.empty', 'Aucun modèle pour l’instant.'))}</li>`}</ul>
        <button class="btn btn-sm" onclick="TxModels.create()">+ ${
          esc(T('js.txlib.new', 'Modèle vierge'))}</button>
        <p class="txlib-railhint">${esc(T('js.txlib.from_card_hint',
          'Pour partir d’une carte déjà configurée : Réseau → Interfaces → sélectionner la carte → '
          + '« Enregistrer comme modèle… ». Le type est alors déduit de la carte.'))}</p>
      </aside>`;
  }

  function _head(d, cap, used, over, noType) {
    const pct = cap ? Math.min(100, Math.round(used / cap * 100)) : 0;
    const col = over ? 'var(--status-stopped-fg)'
              : (cap && used / cap > 0.85) ? 'var(--status-warning-fg)' : 'var(--status-running-fg)';
    const tOpts = `<option value="">— ${esc(T('js.txlib.type', 'Type de carte'))} —</option>` +
      S.types.map(t => `<option value="${esc(t.model)}"${
        (d.nic_model || '').toLowerCase() === t.key ? ' selected' : ''}>${esc(t.model)} · ${
        t.rl_tx_cap} ${esc(T('js.txlib.queues', 'files'))}${
        t.measured ? '' : ' · ' + esc(T('js.txlib.unmeasured', 'non mesuré'))}</option>`).join('');
    const blockWhy = _saveWhy(d, over, noType, !(d.name || '').trim());
    const speed = speedOf(d.nic_model);
    const bw = totalBw(d);
    const bwCol = bwColor(bw, speed);
    const bwPct = speed ? Math.min(100, Math.round(bw / speed * 100)) : 0;
    const bwNote = !speed
      ? T('js.txlib.bw_unknown', 'Type de carte inconnu : débit affiché sans comparaison au lien.')
      : (bw > speed) ? T('js.txlib.bw_over', 'débit estimé supérieur au lien de la carte')
      : T('js.txlib.bw_note', 'estimation ST 2110-20 vidéo + 2110-30 audio (blanking/RTP ignorés)');
    return `
      <header class="txlib-head">
        <div class="txlib-headrow">
          <input class="txlib-title" data-fk="name" value="${esc(d.name)}"
            oninput="TxModels.setName(this.value)" aria-label="${esc(T('js.txlib.name', 'Nom'))}"
            placeholder="${esc(T('js.txlib.name_ph', 'Nom du modèle (obligatoire)'))}">
          <select class="txlib-type" data-fk="type" oninput="TxModels.setType2(this.value)"
            aria-label="${esc(T('js.txlib.type', 'Type de carte'))}">${tOpts}</select>
          <span class="txlib-spacer"></span>
          <span class="txlib-dirty"${(S.dirty || !d.id) ? '' : ' hidden'}>${esc(d.id
            ? T('js.txmodel.dirty', 'modifications non enregistrées')
            : T('js.txlib.not_saved', 'modèle pas encore créé'))}</span>
          <button class="btn btn-sm btn-green txlib-save" ${(S.busy || blockWhy) ? 'disabled' : ''}
            title="${esc(blockWhy || T('js.txlib.save_tip',
              'Enregistre le modèle. Aucune carte, aucun moteur n’est touché.'))}"
            onclick="TxModels.save()">${esc(d.id ? T('js.txlib.save', 'Enregistrer')
                                                 : T('js.txlib.create_save', 'Créer le modèle'))}</button>
          ${d.id ? `<button class="btn btn-sm" onclick="TxModels.duplicate()">${
              esc(T('js.txlib.duplicate', 'Dupliquer'))}</button>
            <button class="btn btn-sm btn-red" onclick="TxModels.remove()" title="${
              esc(T('js.txlib.delete_tip', 'Supprime le modèle. Les cartes déjà réglées avec lui GARDENT '
                + 'leur layout (le modèle est une source, pas la vérité).'))}">${
              esc(T('js.txlib.delete', 'Supprimer'))}</button>`
            : `<button class="btn btn-sm" onclick="TxModels.cancelNew()">${
              esc(T('js.txlib.cancel_new', 'Abandonner'))}</button>`}
        </div>
        <div class="txlib-budget">
          <span class="meta txlib-savewhy">${esc(blockWhy)}</span>
          <span class="txlib-budget-lbl">${esc(T('js.txlib.budget', 'Files RL du modèle'))}</span>
          <span class="txlib-budget-val" style="color:${col}">${used} / ${cap || '—'}</span>
          <div class="txlib-budget-track" role="progressbar" aria-valuenow="${used}"
               aria-valuemin="0" aria-valuemax="${cap || 0}">
            <div class="txlib-budget-fill" style="width:${pct}%;background:${col}"></div>
          </div>
          <span class="meta txlib-budget-note">${noType
            ? esc(T('js.txlib.need_type_short', 'Choisir d’abord le type de carte.'))
            : over ? esc(T('js.txmodel.over', 'plafond dépassé'))
            : esc(T('js.txlib.budget_note', '1 file par sortie vidéo + 1 par flux audio + 1 par ANC'))}</span>
        </div>
        <div class="txlib-budget txlib-bw">
          <span class="txlib-budget-lbl">${esc(T('js.txlib.bw', 'Débit'))}</span>
          <span class="txlib-bw-val" style="color:${bwCol}">${esc(gbps(bw))}${
            speed ? ' / ' + esc(gbps(speed)) : ''} ${esc(T('js.txlib.gbps', 'Gbps'))}</span>
          <div class="txlib-budget-track" role="progressbar" aria-valuenow="${Math.round(bw)}"
               aria-valuemin="0" aria-valuemax="${speed || 0}">
            <div class="txlib-bw-fill" style="width:${bwPct}%;background:${bwCol}"></div>
          </div>
          <span class="meta txlib-bw-note">${esc(bwNote)}</span>
        </div>
      </header>`;
  }

  // ★ LE COMPOSER — le geste principal : « ce préréglage, N fois ». Coût simulé AVANT le clic.
  function _composer(d, cap, used) {
    const f = S.fmts[S.addFmt];
    const isV = S.addKind === 'video', isA = S.addKind === 'audio';
    const unit = composerUnit();
    const qty = Math.max(1, parseInt(S.addQty) || 1);
    const after = used + unit * qty;
    const room = _fits();
    const speed = speedOf(d.nic_model);
    const bwUnit = _slotBw(_composerSlot());
    const afterBw = totalBw(d) + bwUnit * qty;
    const noFmt = isV && !f;               // le format n'est requis QUE pour une sortie vidéo
    const noType = !(d.nic_model || '').trim();
    const tooBig = !!(cap && after > cap);
    const why = noType ? T('js.txlib.need_type_short', 'Choisir d’abord le type de carte.')
              : noFmt ? T('js.txlib.no_fmt', 'Aucun format vidéo dans les Réglages → Vidéo.')
              : tooBig ? T('js.txlib.add_nofit',
                  '{n} sortie(s) de ce gabarit ne tiennent pas : il reste {r} place(s) dans la carte.')
                  .replace('{n}', qty).replace('{r}', room)
              : '';
    // Sélecteur d'ESSENCE : vidéo (+ audio/ANC) · audio seul · ANC seul.
    const kindSel = `<select data-fk="addkind" oninput="TxModels.setAddKind(this.value)"
        aria-label="${esc(T('js.txlib.kind', 'Type de sortie'))}" style="min-width:130px">
        <option value="video"${isV ? ' selected' : ''}>${esc(kindLabel('video'))}</option>
        <option value="audio"${isA ? ' selected' : ''}>${esc(kindLabel('audio'))}</option>
        <option value="anc"${S.addKind === 'anc' ? ' selected' : ''}>${esc(kindLabel('anc'))}</option>
      </select>`;
    // Contrôles selon l'essence : format (vidéo), nb de flux audio (vidéo/audio), ANC (vidéo seul —
    // en essence « ANC seul » l'ANC est implicite).
    const fmtCtl = isV ? `<select data-fk="addfmt" oninput="TxModels.setAddFmt(this.value)"
        aria-label="${esc(T('js.txlib.format', 'Format'))}">${_presetOptions(null, S.addFmt)}</select>` : '';
    const audCtl = (isV || isA) ? `<label class="txlib-inline">${esc(T('js.txlib.audio', 'Audio'))}
        <input type="number" min="${isA ? 1 : 0}" max="8" value="${S.addAudio}" style="width:64px"
          data-fk="addaud" oninput="TxModels.setAddAudio(this.value)"></label>` : '';
    const ancCtl = isV ? `<label class="toggle-inline"><input type="checkbox" class="ios-toggle" ${
        S.addAnc ? 'checked' : ''} data-fk="addanc" oninput="TxModels.setAddAnc(this.checked)">ANC</label>` : '';
    return `
      <div class="txlib-composer">
        <span class="txlib-composer-lbl">${esc(T('js.txlib.add_title', 'Ajouter des sorties'))}</span>
        ${kindSel}${fmtCtl}${audCtl}${ancCtl}
        <label class="txlib-inline">${esc(T('js.txlib.qty', 'Quantité'))}
          <input type="number" min="1" max="64" value="${qty}" style="width:76px"
            data-fk="addqty" oninput="TxModels.setAddQty(this.value)"></label>
        <button class="btn btn-sm btn-green txlib-add" ${(noFmt || noType || tooBig) ? 'disabled' : ''}
          title="${esc(why || T('js.txlib.add_tip',
            'Crée N sorties identiques d’un coup. Aucune carte n’est touchée : un modèle ne coûte rien.'))}"
          onclick="TxModels.addBatch()">+ ${esc(T('js.txlib.add_n', 'Ajouter {n}').replace('{n}', qty))}</button>
        <span class="meta txlib-composer-cost ${tooBig ? 'bad' : ''}">${
          esc(T('js.txlib.add_cost', '{u} file(s) par sortie → {t} / {c} après ajout')
            .replace('{u}', unit).replace('{t}', after).replace('{c}', cap || '—'))}</span>
        <span class="meta txlib-composer-bw ${speed && afterBw > speed ? 'bad' : ''}">${
          esc(_composerBwText(afterBw, speed))}</span>
        <span class="meta txlib-composer-why"${why ? '' : ' hidden'}>${esc(why)}</span>
      </div>`;
  }

  // Clé de regroupement : deux sorties IDENTIQUES (format + audio + ANC) forment un groupe.
  const gkey = s => JSON.stringify([s.video, s.audio_count, !!s.anc]);
  function _groups(d) {
    const out = [];
    (d.slots || []).forEach((s, i) => {
      const k = gkey(s);
      const last = out[out.length - 1];
      if (last && last.key === k) last.idx.push(i);
      else out.push({key: k, slot: s, idx: [i]});
    });
    return out;
  }

  function _rows(d) {
    const groups = _groups(d);
    if (!groups.length) {
      return `<p class="txlib-none">${esc(T('js.txlib.no_slot',
        'Aucune sortie — utiliser « Ajouter des sorties » ci-dessus.'))}</p>`;
    }
    return `<ul class="txlib-groups">` + groups.map((g, gi) => {
      const s = g.slot, v = s.video, n = g.idx.length;
      const open = !!S.open[g.key + '#' + gi];
      const orphan = isOrphan(v);
      const first = g.idx[0] + 1, last = g.idx[g.idx.length - 1] + 1;
      // Éditer le GROUPE édite ses N sorties d'un coup : le groupe EST la sélection multiple (pas
      // besoin d'inventer un mécanisme de sélection, ni un « appliquer à toutes » ambigu).
      const detail = open ? `<ul class="txlib-sub">${g.idx.map(i => `
        <li class="txlib-slot">
          <span class="txlib-slot-idx">TX ${i + 1}</span>
          ${d.slots[i].video
            ? `<select class="txlib-slot-fmt" data-fk="s${i}fmt" oninput="TxModels.setPreset(${i}, this.value)"
                 aria-label="${esc(T('js.txlib.format', 'Format'))}">${_presetOptions(d.slots[i].video)}</select>`
            : `<span class="txlib-slot-fmt txlib-essence">${esc(kindLabel(kindOf(d.slots[i])))}</span>`}
          <label class="txlib-inline">${esc(T('js.txlib.audio', 'Audio'))}
            <input type="number" min="${d.slots[i].video || d.slots[i].anc ? 0 : 1}" max="8"
              value="${d.slots[i].audio_count || 0}" style="width:64px"
              data-fk="s${i}aud" oninput="TxModels.setAudio(${i}, this.value)"></label>
          <label class="toggle-inline"><input type="checkbox" class="ios-toggle" ${
            d.slots[i].anc ? 'checked' : ''} data-fk="s${i}anc"
            oninput="TxModels.setAnc(${i}, this.checked)">ANC</label>
          <span class="txlib-slot-cost">${cost(d.slots[i])} ${esc(T('js.txlib.queues', 'files'))}</span>
          <button class="btn btn-sm btn-red" title="${esc(T('js.txlib.remove', 'Retirer cette sortie'))}"
            aria-label="${esc(T('js.txlib.remove', 'Retirer cette sortie'))}"
            onclick="TxModels.removeSlot(${i})">✕</button>
        </li>`).join('')}</ul>` : '';
      return `
      <li class="txlib-group${open ? ' open' : ''}">
        <div class="txlib-group-row">
          <button class="txlib-group-x" aria-expanded="${open}"
            title="${esc(open ? T('js.txlib.fold', 'Replier') : T('js.txlib.unfold',
              'Déplier : voir et éditer chaque sortie'))}"
            onclick="TxModels.toggleGroup('${esc(g.key + '#' + gi)}')">${open ? '▾' : '▸'}</button>
          <span class="txlib-group-n">${n} ×</span>
          ${v
            ? `<select class="txlib-group-fmt" data-fk="g${gi}fmt" oninput="TxModels.setGroupPreset(${gi}, this.value)"
                 aria-label="${esc(T('js.txlib.format', 'Format'))}">${_presetOptions(v)}</select>`
            : `<span class="txlib-group-fmt txlib-essence">${esc(kindLabel(kindOf(s)))}</span>`}
          ${orphan ? `<span class="txlib-orphan" title="${esc(T('js.txlib.fmt_orphan_tip',
            'Ce format ne correspond à aucun préréglage de Réglages → Vidéo (renommé ou supprimé depuis). '
            + 'Il reste valide et inchangé ; en choisir un autre le remplacera.'))}">${
            esc(T('js.txlib.fmt_orphan', 'hors liste'))}</span>` : ''}
          <label class="txlib-inline">${esc(T('js.txlib.audio', 'Audio'))}
            <input type="number" min="${v || s.anc ? 0 : 1}" max="8" value="${s.audio_count || 0}" style="width:64px"
              data-fk="g${gi}aud" oninput="TxModels.setGroupAudio(${gi}, this.value)"></label>
          <label class="toggle-inline"><input type="checkbox" class="ios-toggle" ${s.anc ? 'checked' : ''}
            data-fk="g${gi}anc" oninput="TxModels.setGroupAnc(${gi}, this.checked)">ANC</label>
          <label class="txlib-inline">${esc(T('js.txlib.qty', 'Quantité'))}
            <input type="number" min="0" max="64" value="${n}" style="width:76px"
              data-fk="g${gi}qty" oninput="TxModels.setGroupQty(${gi}, this.value)"></label>
          <span class="txlib-slot-cost">${cost(s) * n} ${esc(T('js.txlib.queues', 'files'))}</span>
          <span class="meta txlib-group-range">TX ${first}${n > 1 ? '–' + last : ''}</span>
          <button class="btn btn-sm btn-red" title="${esc(T('js.txlib.remove_group',
            'Retirer ces sorties'))}" aria-label="${esc(T('js.txlib.remove_group', 'Retirer ces sorties'))}"
            onclick="TxModels.removeGroup(${gi})">✕</button>
        </div>
        ${detail}
      </li>`;
    }).join('') + `</ul>`;
  }

  // ── Ptime audio : GLOBAL AU MOTEUR (env AUDIO_PTIME → controller.py:54 ; appliqué tel quel à CHAQUE
  //    session TX audio, controller.py:1749, et au SDP émis, controller.py:2225). Un modèle ne peut donc
  //    PAS le porter par sortie : offrir un choix ici serait un CONTRÔLE MUET (interdit). On AFFICHE la
  //    valeur effective + son coût en paquets, et on renvoie au réglage qui, lui, a un effet.
  function _ptimeBar(d) {
    const flows = audioFlows(d);
    const pps = Math.round(1000 / (S.ptime || 1));
    return `
      <p class="txlib-ptime">
        <span class="txlib-ptime-val">ptime ${S.ptime} ms</span>
        <span>${esc(T('js.txlib.ptime_lead',
          'Réglage GLOBAL du moteur : le même pour toutes les sorties audio — un modèle ne peut pas le '
          + 'porter par sortie.'))}</span>
        <span class="meta">${esc(T('js.txlib.ptime_rate',
          '{p} paquets/s par flux audio · {n} flux dans ce modèle')
          .replace('{p}', pps).replace('{n}', flows))}</span>
        <span class="txlib-spacer"></span>
        <button class="btn btn-sm" onclick="switchSetTab('audio')">${
          esc(T('js.txlib.ptime_link', 'Réglages → Audio'))}</button>
      </p>`;
  }

  function _pedagogy() {
    return `
      <details class="txlib-ped">
        <summary>${esc(T('js.txmodel.sec_cost', 'Ce qui est gratuit, ce qui coûte'))}</summary>
        <p class="meta txlib-ped-lead">${esc(T('js.txlib.cost_lead',
          'Un modèle ne coûte rien : ce sont les SORTIES d’une carte qui coûtent, au moment où on les '
          + 'provisionne (fiche de l’interface). Sur un port DPDK, créer une session recalcule l’arbre de '
          + 'pacing matériel : le port entier s’arrête ~2,2 s. Sur un port AF-XDP il n’y a pas de rate '
          + 'limiter — aucun commit n’est possible, rien ne gèle, jamais.'))}</p>
        <div class="txmo-legend">
          <div class="txmo-legend-col free">
            <div class="txmo-legend-hdr">${esc(T('js.txmodel.free_title',
              'Gratuit — en direct, sans effet sur les voisines'))}</div>
            <ul>
              <li>${esc(T('js.txmodel.free_1', 'Activer une sortie déclarée (câbler une source du format annoncé)'))}</li>
              <li>${esc(T('js.txmodel.free_2', 'Couper une sortie (elle redevient silencieuse, sa feuille de pacing reste)'))}</li>
              <li>${esc(T('js.txmodel.free_3', 'Changer la source d’une sortie (simple bascule d’entrée)'))}</li>
              <li>${esc(T('js.txlib.free_5', 'Créer, modifier ou supprimer un modèle ici (aucune carte n’est touchée)'))}</li>
            </ul>
          </div>
          <div class="txmo-legend-col commit">
            <div class="txmo-legend-hdr">${esc(T('js.txmodel.commit_title',
              'Coûte un recalcul d’arbre — tout le port gèle (~2,2 s), en DPDK seulement'))}</div>
            <ul>
              <li>${esc(T('js.txmodel.commit_1', 'Déclarer une nouvelle sortie'))}</li>
              <li>${esc(T('js.txmodel.commit_2', 'Changer le format annoncé d’une sortie'))}</li>
              <li>${esc(T('js.txmodel.commit_3', 'Câbler une source dont le format DIFFÈRE du format annoncé (refusé par défaut : insérer un UDC, ou aligner la sortie en maintenance)'))}</li>
              <li>${esc(T('js.txmodel.commit_4', 'Redéployer le moteur'))}</li>
            </ul>
          </div>
        </div>
      </details>`;
  }

  function render() {
    if (!S.el) return;
    const q = S.q.toLowerCase();
    const list = S.models.filter(m =>
      (!S.filterType || (m.nic_model || '').toLowerCase() === S.filterType) &&
      (!q || (m.name || '').toLowerCase().includes(q) || (m.nic_model || '').toLowerCase().includes(q)));

    const d = S.draft;
    let main;
    if (!d) {
      main = `<main class="txlib-main">
        <div class="txlib-empty">
          <p>${esc(T('js.txlib.pick', 'Sélectionner un modèle, ou en créer un nouveau.'))}</p>
          <button class="btn btn-sm" onclick="TxModels.create()">+ ${
            esc(T('js.txlib.new', 'Modèle vierge'))}</button>
        </div>${_pedagogy()}</main>`;
    } else {
      const cap = capOf(d.nic_model);
      const used = totalCost(d);
      const over = !!(cap && used > cap);
      const noType = !(d.nic_model || '').trim();
      main = `<main class="txlib-main">
        ${_head(d, cap, used, over, noType)}
        <section class="txlib-outputs">
          ${_composer(d, cap, used)}
          ${_rows(d)}
        </section>
        ${_ptimeBar(d)}
        ${_pedagogy()}
      </main>`;
    }
    const banner = S.err
      ? `<div class="alert alert-error txlib-banner" role="alert">⚠ ${esc(S.err)}
           <button class="txlib-banner-x" onclick="TxModels.clearMsg()" aria-label="fermer">✕</button></div>`
      : S.msg
      ? `<div class="alert alert-info txlib-banner" role="status">${esc(S.msg)}
           <button class="txlib-banner-x" onclick="TxModels.clearMsg()" aria-label="fermer">✕</button></div>`
      : '';
    // ★ Rendu NON DESTRUCTIF : on mémorise le champ actif (data-fk) + la position du curseur, on
    //   reconstruit, puis on rend le focus et le curseur. Sans ça, tout rendu pendant une saisie
    //   éjectait l'utilisateur du champ.
    const ae = document.activeElement;
    const fk = (ae && S.el.contains(ae)) ? ae.dataset.fk : null;
    const selS = (fk && ae.selectionStart != null) ? ae.selectionStart : null;
    const selE = (fk && ae.selectionEnd != null) ? ae.selectionEnd : null;

    S.el.innerHTML = banner + `<div class="txlib">${_rail(list)}${main}</div>`;
    // ★ Steppers −/+ enrobés SYNCHRONIQUEMENT ici (pas via le seul MutationObserver global, qui est
    //   asynchrone/rAF) : sinon, à chaque re-rendu, les champs numériques étaient brièvement « nus »
    //   avant que l'observateur ne repose les boutons → CLIGNOTEMENT du « − ». Idempotent (skip si
    //   déjà enrobé). L'observateur reste le filet pour les injections hors de ce panneau.
    if (window.enhanceSteppers) window.enhanceSteppers(S.el);

    if (fk) {
      const back = S.el.querySelector(`[data-fk="${fk}"]`);
      if (back) {
        back.focus();
        if (selS != null && back.setSelectionRange) {
          try { back.setSelectionRange(selS, selE); } catch (_) { /* type sans sélection */ }
        }
      }
    }
    // ★ Les boutons ne PRENNENT PAS le focus : sans blur, aucun `change` ne peut s'intercaler entre
    //   le mousedown et le mouseup — donc plus aucun clic mangé par un re-rendu. (C'était LE bug :
    //   taper un nom puis cliquer « Créer le modèle » détruisait le bouton avant le mouseup.)
    S.el.querySelectorAll('button').forEach(b =>
      b.addEventListener('mousedown', e => e.preventDefault()));
  }

  // Mise à jour CIBLÉE (aucun innerHTML) : ce qui dépend de l'état mais ne change pas la structure —
  // budget, badge « modifié », coût du composer, état (et raison) des boutons. Appelée à chaque frappe.
  function _sync() {
    const d = S.draft;
    if (!S.el || !d) return;
    const cap = capOf(d.nic_model);
    const used = totalCost(d);
    const over = !!(cap && used > cap);
    const noType = !(d.nic_model || '').trim();
    const noName = !(d.name || '').trim();
    const col = over ? 'var(--status-stopped-fg)'
              : (cap && used / cap > 0.85) ? 'var(--status-warning-fg)' : 'var(--status-running-fg)';
    const q = sel => S.el.querySelector(sel);

    const val = q('.txlib-budget-val');
    if (val) { val.textContent = `${used} / ${cap || '—'}`; val.style.color = col; }
    const fill = q('.txlib-budget-fill');
    if (fill) {
      fill.style.width = (cap ? Math.min(100, Math.round(used / cap * 100)) : 0) + '%';
      fill.style.background = col;
    }
    const note = q('.txlib-budget-note');
    if (note) {
      note.textContent = noType ? T('js.txlib.need_type_short', 'Choisir d’abord le type de carte.')
                       : over ? T('js.txmodel.over', 'plafond dépassé')
                       : T('js.txlib.budget_note', '1 file par sortie vidéo + 1 par flux audio + 1 par ANC');
    }
    // Segment « Débit » : même logique ciblée que le budget de files (reflète la frappe sans re-render).
    const speed = speedOf(d.nic_model);
    const bw = totalBw(d);
    const bwCol = bwColor(bw, speed);
    const bwVal = q('.txlib-bw-val');
    if (bwVal) {
      bwVal.textContent = `${gbps(bw)}${speed ? ' / ' + gbps(speed) : ''} ` + T('js.txlib.gbps', 'Gbps');
      bwVal.style.color = bwCol;
    }
    const bwFill = q('.txlib-bw-fill');
    if (bwFill) {
      bwFill.style.width = (speed ? Math.min(100, Math.round(bw / speed * 100)) : 0) + '%';
      bwFill.style.background = bwCol;
    }
    const bwNote = q('.txlib-bw-note');
    if (bwNote) {
      bwNote.textContent = !speed
        ? T('js.txlib.bw_unknown', 'Type de carte inconnu : débit affiché sans comparaison au lien.')
        : (bw > speed) ? T('js.txlib.bw_over', 'débit estimé supérieur au lien de la carte')
        : T('js.txlib.bw_note', 'estimation ST 2110-20 vidéo + 2110-30 audio (blanking/RTP ignorés)');
    }
    // Bouton « Enregistrer / Créer » : jamais muet — il DIT pourquoi il est désactivé.
    const why = _saveWhy(d, over, noType, noName);
    const save = q('.txlib-save');
    if (save) {
      save.disabled = !!(S.busy || why);
      save.title = why || T('js.txlib.save_tip',
        'Enregistre le modèle. Aucune carte, aucun moteur n’est touché.');
    }
    const saveWhy = q('.txlib-savewhy');
    if (saveWhy) saveWhy.textContent = why || '';
    const dirty = q('.txlib-dirty');
    if (dirty) {
      dirty.textContent = d.id ? T('js.txmodel.dirty', 'modifications non enregistrées')
                               : T('js.txlib.not_saved', 'modèle pas encore créé');
      dirty.hidden = !(S.dirty || !d.id);
    }
    _syncComposer(d, cap, used);
  }
  function _syncComposer(d, cap, used) {
    const q = sel => S.el.querySelector(sel);
    const f = S.fmts[S.addFmt];
    const unit = composerUnit();
    const qty = Math.max(1, parseInt(S.addQty) || 1);
    const after = used + unit * qty;
    const tooBig = !!(cap && after > cap);
    const noFmt = S.addKind === 'video' && !f;   // format requis seulement en essence vidéo
    const why = !(d.nic_model || '').trim()
                  ? T('js.txlib.need_type_short', 'Choisir d’abord le type de carte.')
              : noFmt ? T('js.txlib.no_fmt', 'Aucun format vidéo dans les Réglages → Vidéo.')
              : tooBig ? T('js.txlib.add_nofit',
                  '{n} sortie(s) de ce gabarit ne tiennent pas : il reste {r} place(s) dans la carte.')
                  .replace('{n}', qty).replace('{r}', _fits())
              : '';
    const cost = q('.txlib-composer-cost');
    if (cost) {
      cost.textContent = T('js.txlib.add_cost', '{u} file(s) par sortie → {t} / {c} après ajout')
        .replace('{u}', unit).replace('{t}', after).replace('{c}', cap || '—');
      cost.classList.toggle('bad', tooBig);
    }
    const speed = speedOf(d.nic_model);
    const afterBw = totalBw(d) + _slotBw(_composerSlot()) * qty;
    const bw = q('.txlib-composer-bw');
    if (bw) {
      bw.textContent = _composerBwText(afterBw, speed);
      bw.classList.toggle('bad', !!(speed && afterBw > speed));
    }
    const add = q('.txlib-add');
    if (add) {
      add.disabled = !!why;
      add.title = why || T('js.txlib.add_tip',
        'Crée N sorties identiques d’un coup. Aucune carte n’est touchée : un modèle ne coûte rien.');
      add.textContent = '+ ' + T('js.txlib.add_n', 'Ajouter {n}').replace('{n}', qty);
    }
    const cwhy = q('.txlib-composer-why');
    if (cwhy) { cwhy.textContent = why || ''; cwhy.hidden = !why; }
  }
  // Raison (affichée + infobulle) d'un enregistrement impossible. Aucun contrôle muet.
  function _saveWhy(d, over, noType, noName) {
    if (noName) return T('js.txlib.need_name', 'Donner un nom au modèle.');
    if (noType) return T('js.txlib.need_type_short', 'Choisir d’abord le type de carte.');
    if (over) return T('js.txlib.over_msg', '');
    return '';
  }

  // Édition SANS conséquence structurelle (nom, type, composer) : l'état suit la frappe, le DOM est
  // mis à jour de façon CIBLÉE — le champ en cours de saisie n'est jamais détruit.
  function edit() { S.dirty = true; _sync(); }
  // Édition STRUCTURELLE (sorties ajoutées/retirées/regroupées) : rendu complet, focus restauré.
  function touch() { S.dirty = true; render(); }

  // ─── Actions ───────────────────────────────────────────────────────────────────────────────────
  window.TxModels = {
    mount(el) {
      if (!el) return;
      S.el = el;
      load();                       // rechargé à CHAQUE ouverture d'onglet (jamais de données périmées)
    },
    setQuery(v) { S.q = v; render(); },
    setType(v) { S.filterType = v; render(); },
    async select(id) {
      if ((S.dirty || (S.draft && !S.draft.id)) && !(await window.mxlConfirm({
            message: T('js.txlib.discard', 'Abandonner les modifications non enregistrées ?'),
            title: T('js.txlib.discard_title', 'Modifications non enregistrées'), danger: true}))) return;
      S.sel = id; S.draft = _draftOf(id); S.dirty = false; S.msg = ''; S.err = '';
      S.open = {}; _resetComposer(); render();
    },
    // ⚠ Ces trois-là ne re-rendent PAS : ils mettent l'état à jour et synchronisent le DOM de façon
    //   ciblée. Re-rendre pendant la frappe détruisait l'input (et mangeait le clic suivant).
    setName(v) { S.draft.name = v; edit(); },
    setType2(v) {
      S.draft.nic_model = v;
      S.addQty = Math.max(1, Math.min(8, _fits()));   // la quantité proposée suit le nouveau plafond
      const qtyEl = S.el.querySelector('[data-fk="addqty"]');
      if (qtyEl && document.activeElement !== qtyEl) qtyEl.value = S.addQty;
      edit();
    },

    // ── Composer (geste principal) — sync ciblé, jamais de rebuild ──
    // Changer d'ESSENCE change la STRUCTURE du composer (le format apparaît/disparaît) → rebuild.
    setAddKind(v) {
      S.addKind = (v === 'audio' || v === 'anc') ? v : 'video';
      if (S.addKind === 'audio' && (parseInt(S.addAudio) || 0) < 1) S.addAudio = 1;
      S.addQty = Math.max(1, Math.min(8, _fits()));
      render();
    },
    setAddFmt(v) { S.addFmt = parseInt(v, 10) || 0; _sync(); },
    setAddAudio(n) {
      const lo = S.addKind === 'audio' ? 1 : 0;
      S.addAudio = Math.max(lo, Math.min(8, parseInt(n) || 0));
      _sync();
    },
    setAddAnc(b) { S.addAnc = !!b; _sync(); },
    setAddQty(n) {
      const v = parseInt(n, 10);
      S.addQty = isNaN(v) ? 1 : Math.max(1, Math.min(64, v));
      _sync();
    },
    addBatch() {
      if (!S.draft) return;
      if (S.addKind === 'video' && !S.fmts[S.addFmt]) return;
      const qty = Math.max(1, parseInt(S.addQty) || 1);
      for (let i = 0; i < qty; i++) S.draft.slots.push(_composerSlot());
      S.dirty = true;
      S.addQty = Math.max(1, Math.min(8, _fits()));      // la quantité proposée suit la place restante
      render();
    },

    // ── Groupe = la sélection multiple (éditer le groupe édite ses N sorties) ──
    toggleGroup(k) { S.open[k] = !S.open[k]; render(); },
    setGroupPreset(gi, idx) {
      const f = S.fmts[parseInt(idx, 10)];
      const g = _groups(S.draft)[gi];
      if (!f || !g) return;          // '-1' = format hors liste : jamais réécrit en douce
      g.idx.forEach(i => { S.draft.slots[i].video = slotOf(f).video;
                           S.draft.slots[i].fmt_label = f.label; });
      touch();
    },
    setGroupAudio(gi, n) {
      const g = _groups(S.draft)[gi];
      const v = parseInt(n, 10);
      if (!g || isNaN(v)) return;
      g.idx.forEach(i => { S.draft.slots[i].audio_count = Math.max(0, Math.min(8, v)); });
      touch();
    },
    setGroupAnc(gi, b) {
      const g = _groups(S.draft)[gi];
      if (!g) return;
      g.idx.forEach(i => { S.draft.slots[i].anc = !!b; });
      touch();
    },
    // Quantité d'un groupe : ajoute des copies à la fin du groupe, ou retire les dernières.
    setGroupQty(gi, n) {
      const g = _groups(S.draft)[gi];
      const v = parseInt(n, 10);
      if (!g || isNaN(v)) return;      // champ vidé en cours de frappe : on ne supprime RIEN
      n = Math.max(0, Math.min(64, v));
      const cur = g.idx.length;
      if (n === cur) return;
      if (n < cur) {
        const drop = new Set(g.idx.slice(n));
        S.draft.slots = S.draft.slots.filter((_, i) => !drop.has(i));
      } else {
        const at = g.idx[g.idx.length - 1] + 1;
        const copy = () => ({video: {...g.slot.video}, fmt_label: g.slot.fmt_label,
                             audio_count: g.slot.audio_count, anc: !!g.slot.anc});
        S.draft.slots.splice(at, 0, ...Array.from({length: n - cur}, copy));
      }
      touch();
    },
    removeGroup(gi) {
      const g = _groups(S.draft)[gi];
      if (!g) return;
      const drop = new Set(g.idx);
      S.draft.slots = S.draft.slots.filter((_, i) => !drop.has(i));
      touch();
    },

    // ── Édition d'UNE sortie (cas hétérogène : possible, mais plus le chemin par défaut) ──
    setPreset(i, idx) {
      const f = S.fmts[parseInt(idx, 10)];
      if (!f) return;
      S.draft.slots[i].video = slotOf(f).video;
      S.draft.slots[i].fmt_label = f.label;
      touch();
    },
    setAudio(i, n) {
      const v = parseInt(n, 10);
      if (isNaN(v)) return;
      S.draft.slots[i].audio_count = Math.max(0, Math.min(8, v));
      touch();
    },
    setAnc(i, b) { S.draft.slots[i].anc = !!b; touch(); },
    removeSlot(i) { S.draft.slots.splice(i, 1); touch(); },

    create() {
      const pre = S.filterType ? _modelOfKey(S.filterType)
                : (S.types.length === 1 ? S.types[0].model : '');
      S.sel = null;
      S.draft = {id: null, name: T('js.txlib.new_name', 'Nouveau modèle'), nic_model: pre,
                 notes: '', slots: []};
      S.dirty = false; S.err = ''; S.msg = ''; S.open = {};
      _resetComposer();
      render();
    },
    cancelNew() { S.draft = _draftOf(S.sel); S.dirty = false; S.err = ''; S.msg = ''; render(); },

    async save() {
      const d = S.draft;
      if (!d) return;
      if (!(d.name || '').trim()) {
        S.err = T('js.txlib.need_name', 'Donner un nom au modèle.');
        render(); return;
      }
      if (!(d.nic_model || '').trim()) {
        S.err = T('js.txlib.need_type', 'Choisir le TYPE de carte : c’est lui qui borne le modèle.');
        render(); return;
      }
      S.busy = true; render();
      const j = await call(d.id ? `/api/tx-card-models/${d.id}` : '/api/tx-card-models',
        {method: 'POST', headers: {'Content-Type': 'application/json'},
         body: JSON.stringify({name: d.name, nic_model: d.nic_model, slots: d.slots,
                               notes: d.notes || ''})});
      S.busy = false;
      if (!j) { render(); return; }
      S.dirty = false;
      const msg = d.id ? T('js.txlib.saved', '✓ modèle enregistré (aucune carte n’a bougé)')
                       : T('js.txlib.created', '✓ modèle créé (aucune carte n’a bougé)');
      await load(j.id || d.id);
      S.msg = msg; render();
    },
    async duplicate() {
      if (!S.draft || !S.draft.id) return;
      const j = await call(`/api/tx-card-models/${S.draft.id}/duplicate`, {method: 'POST'});
      if (j) { await load(j.id); S.msg = T('js.txlib.duplicated', '✓ modèle dupliqué'); }
      render();
    },
    async remove() {
      if (!S.draft || !S.draft.id) return;
      if (!(await window.mxlConfirm({
            message: T('js.txlib.delete_confirm',
              'Supprimer ce modèle ? Les cartes déjà réglées avec lui gardent leur layout.'),
            title: T('js.txlib.delete', 'Supprimer'), danger: true}))) return;
      const j = await call(`/api/tx-card-models/${S.draft.id}`, {method: 'DELETE'});
      if (!j) { render(); return; }
      S.sel = null;
      await load(null);
      S.msg = T('js.txlib.deleted', '✓ modèle supprimé (les cartes gardent leur layout)');
      render();
    },
    clearMsg() { S.err = ''; S.msg = ''; render(); },
  };
  function _modelOfKey(key) {
    const t = S.types.find(t => t.key === key);
    return t ? t.model : '';
  }
})();
