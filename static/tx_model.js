/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France — Auteur : Cyril Mazouer.
 *
 * « Modèle d'utilisation de la carte » (docs/reference/TX_LAYOUTS.md) — LA vue où l'exploitant COMPREND le modèle
 * sans lire la doc. Une carte E810 porte un arbre de pacing matériel : le modifier =
 * `rte_tm_hierarchy_commit` = arrêt du PORT ENTIER (~1 s) = TOUTES les sorties du port gèlent, y
 * compris celles qui n'ont rien demandé. D'où le modèle, que cette page rend LISIBLE :
 *
 *   1. on DÉCLARE à l'avance les sorties du port et le FORMAT que chacune annonce (contrat SDP) ;
 *   2. déclarer coûte un commit → on le fait dans une fenêtre de maintenance CHOISIE ;
 *   3. exploiter ensuite est GRATUIT (activer/couper/rerouter une sortie déclarée = 0 commit) ;
 *   4. sauf format discordant (gate de l'étage 3 : UDC / aligner la sortie / annuler).
 *
 * ⚠ En AF-XDP il n'y a PAS de rate limiter, donc AUCUN commit : le modèle ne coûte RIEN. La page le
 * DIT au lieu de menacer d'un coût inexistant (règle projet : aucun contrôle muet).
 *
 * Réutilise les briques existantes plutôt que d'en refaire : `window.txMaintConfirm` (tx_maint.js,
 * modale « action perturbatrice » qui NOMME les victimes), le bac de maintenance
 * (`/api/tx-maintenance*`), les états par sortie (`.io2110-state`, base.css) et l'application du
 * layout (`/api/mtl/<vmid>/tx-layout/apply`, gaté étage 2).
 */
(function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const T = (k, fb) => (window.t ? (window.t(k) || fb) : fb);

  // ★ SCISSION EN DEUX OBJETS (demande utilisateur, 2026-07-14) :
  //   · la BIBLIOTHÈQUE de modèles (gabarits réutilisables par TYPE de carte) vit dans l'onglet
  //     « Modèle de carte 2110 » (static/tx_models.js) — un modèle ne coûte RIEN ;
  //   · CE panneau reste sur la CARTE RÉELLE : son layout effectif (sorties, états, sources, formats
  //     annoncés) + l'action « Appliquer un modèle », seul endroit où le coût se paie.
  // L'édition slot-par-slot d'une carte reste possible en mode EXPERT (`expert`), volontairement OFF
  // par défaut : le geste normal est « je choisis un modèle », pas « je bricole une carte ».
  const S = {el: null, node: null, iface: null, ifaces: [], model: null,
             draft: [], dirty: false, verdict: null, check: null, busy: false, spin: false, msg: '',
             expert: false,
             cands: [], binding: {model: null, diverged: false},   // modèles applicables + rattachement
             pickId: null, preview: null, pvBusy: false,
             capture: false, capName: '',        // « Enregistrer comme modèle… » (capture = LECTURE)
             // Progressive disclosure (registre produit dense mais lisible) : la vue par défaut = budget
             // + sorties compactes ; « Appliquer un modèle » et « Maintenance » sont repliés, chaque
             // sortie s'ouvre à la demande. On MÉMORISE l'état d'ouverture (le <details> se re-rend sans
             // l'oublier : sinon éditer une sortie la replierait sous les doigts).
             openApply: false, slotOpen: new Set(),
             mode: 'page', fmts: []};                      // 'card' = fiche d'interface (constate) | 'page' = Modèles (édite/explique)

  // Vidéo OPTIONNELLE (slot audio-seul / ANC-seul) : 1 file si vidéo + 1/audio + 1 si ANC.
  const cost = s => (s.video && s.video.w ? 1 : 0) + (parseInt(s.audio_count) || 0) + (s.anc ? 1 : 0);
  const totalCost = () => S.draft.reduce((n, s) => n + cost(s), 0);

  // Libellé du format : le préréglage de Réglages → Vidéo s'il existe (source unique,
  // static/video_formats.js), sinon la description technique. Aucun parseur recopié ici.
  function _fmtLabel(v) {
    if (!v || !v.w) return '—';
    if (window.videoFormatLabel) return window.videoFormatLabel(v);
    const fps = Number(v.fps || v.f || 0);
    return `${v.w}×${v.h}${(v.scan === 'i') ? 'i' : 'p'}${fps % 1 ? fps.toFixed(2) : fps}${
      v.bd ? ' · ' + v.bd + ' bits' : ''}`;
  }
  const _isOrphan = v => !!(v && v.w) && !(window.videoFormatMatch && window.videoFormatMatch(v));
  function _presetOptions(v) {
    const cur = window.videoFormatMatch ? window.videoFormatMatch(v) : null;
    const opts = (S.fmts || []).map((f, i) =>
      `<option value="${i}"${cur === f ? ' selected' : ''}>${esc(f.label)}</option>`).join('');
    return (cur ? '' : `<option value="-1" selected>${esc(_fmtLabel(v))}</option>`) + opts;
  }

  // ─── Impact PARTAGÉ (dé-duplication) ─────────────────────────────────────────────────────────────
  // Une SEULE zone « coût / victimes nommées », réutilisée par l'aperçu d'un modèle (secModel) ET par
  // le bac de maintenance (secApply) — auparavant deux blocs quasi identiques. Le coût dépend du MODE
  // DU PORT : en AF-XDP, pas de rate limiter → aucun commit → application GRATUITE (on le DIT, jamais un
  // coût inexistant brandi). `verdict` = {created:[{essence}], victims:[{label}]} ; `portsLabel` = le(s)
  // port(s) recalculé(s).
  function impactHtml(verdict, portsLabel) {
    const rl = !!((S.model || {}).port || {}).rl;
    const eng = (S.model || {}).engine;
    const vic = (verdict && verdict.victims) || [];
    const nC = ((verdict && verdict.created) || []).filter(c => c.essence === 'video').length;
    if (!eng) return `<div class="alert alert-info txmo-alert">${esc(T('js.txmodel.no_engine',
      'Aucun moteur 2110 déployé sur cette carte : le modèle est enregistré et sera appliqué au '
      + 'déploiement — rien à geler aujourd’hui.'))}</div>`;
    if (!rl) return `<div class="alert alert-info txmo-alert">${esc(T('js.txmodel.impact_xdp',
      'Port en AF-XDP : appliquer ce modèle ne recalcule aucun arbre et ne gèle aucune sortie.'))}</div>`;
    if (!nC) return `<div class="alert alert-info txmo-alert">${esc(T('js.txmodel.impact_none',
      'Rien à créer : appliquer ce modèle ne coûte aucun recalcul d’arbre.'))}</div>`;
    const names = vic.map(x => `<span class="txm-victim">${esc(x.label)}</span>`).join('');
    return `
      <div class="alert alert-warning txmo-alert">
        <div>${esc(T('js.txmodel.impact_n',
          'Appliquer ce modèle crée {c} session(s) vidéo sur {p} : l’arbre du port est recalculé.')
          .replace('{c}', nC).replace('{p}', portsLabel))}</div>
        <div style="margin-top:6px">${vic.length
          ? esc(T('js.txmodel.impact_victims', 'Ces {n} sortie(s) actuellement en émission figeront ~1 s :')
              .replace('{n}', vic.length))
          : esc(T('js.txmodel.impact_no_victim',
              'Aucune sortie n’émet actuellement sur ce port : le recalage passera inaperçu.'))}</div>
        ${vic.length ? `<div class="txm-victims" style="margin-top:8px">${names}</div>` : ''}
      </div>`;
  }

  // ─── Chargement ────────────────────────────────────────────────────────────────────────────────
  async function load() {
    if (!S.node || !S.iface) return;
    if (window.loadVideoFormats && !(S.fmts || []).length) S.fmts = (await window.loadVideoFormats()) || [];
    try {
      const r = await fetch(`/api/nodes/${S.node}/tx-model?iface=${encodeURIComponent(S.iface)}`);
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      S.model = j.model;
      S.draft = (j.model.layout.slots || []).map(s => ({video: s.video ? {...s.video} : null,
                                                        fmt_label: s.fmt_label || '',
                                                        audio_count: s.audio_count || 0,
                                                        anc: !!s.anc}));
      S.dirty = false; S.verdict = null; S.check = null; S.msg = '';
      S.binding = j.model.binding || {model: null, diverged: false};
      render();
      refreshVerdict();          // coût du modèle ENREGISTRÉ (« qu'est-ce qui reste à appliquer ? »)
      loadCandidates();          // modèles de la bibliothèque applicables à CETTE carte
    } catch (e) {
      if (S.el) S.el.innerHTML = `<div class="alert alert-error">${esc(e.message)}</div>`;
    }
  }

  // Modèles applicables à cette carte (compatibles avec son TYPE) + ceux qui ne le sont pas, avec la
  // RAISON : on ne cache jamais un modèle en silence.
  async function loadCandidates() {
    try {
      const r = await fetch(`/api/nodes/${S.node}/tx-model/candidates?iface=${encodeURIComponent(S.iface)}`);
      const j = await r.json();
      if (!r.ok || !j.ok) return;
      S.cands = j.models || [];
      S.binding = j.binding || S.binding;
      if (S.pickId && !S.cands.some(m => m.id === S.pickId)) S.pickId = null;
      render();
    } catch (_) { /* pas bloquant */ }
  }

  // Diff + coût AVANT le clic (sorties ajoutées/retirées/reformatées, sessions créées, victimes).
  async function loadPreview() {
    if (!S.pickId) { S.preview = null; render(); return; }
    S.pvBusy = true; render();
    try {
      const r = await fetch(`/api/nodes/${S.node}/tx-model/preview`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({iface: S.iface, model_id: S.pickId})});
      const j = await r.json();
      S.preview = (r.ok && j.ok) ? j.preview : null;
    } catch (_) { S.preview = null; }
    S.pvBusy = false; render();
  }

  // Le coût s'affiche AVANT le clic : à chaque édition on redemande le verdict (débounce), qui NOMME
  // les sorties qui figeraient et compte les sessions vidéo à (re)créer.
  let _vt = null;
  function refreshVerdict() {
    clearTimeout(_vt);
    _vt = setTimeout(async () => {
      try {
        const r = await fetch(`/api/nodes/${S.node}/tx-layout/verdict`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({iface: S.iface, slots: S.draft}),
        });
        const j = await r.json();
        S.verdict = (r.ok && j.ok) ? j.verdict : null;
        S.check = (r.ok && j.ok) ? j.check : null;
      } catch (_) { S.verdict = null; }
      render();
    }, 350);
  }

  function touch() { S.dirty = true; render(); refreshVerdict(); }

  // ─── Rendu ─────────────────────────────────────────────────────────────────────────────────────
  function render() {
    if (!S.el || !S.model) return;
    const m = S.model, p = m.port || {};
    const rl = !!p.rl;
    const cap = p.rl_tx_cap || 0;
    const used = totalCost();
    const over = cap && used > cap;
    const col = over ? 'var(--status-stopped-fg)'
              : (cap && used / cap > 0.85) ? 'var(--status-warning-fg)' : 'var(--status-running-fg)';
    const pct = cap ? Math.min(100, Math.round(used / cap * 100)) : 0;
    const ifaceOpts = S.ifaces.map(n => `<option value="${esc(n.ifname)}"${
      n.ifname === S.iface ? ' selected' : ''}>${esc(n.ifname)}${
      n.model ? ' — ' + esc(n.model) : ''}</option>`).join('');

    // ── 1. LA CARTE : le mode du port DÉCIDE de tout le reste (coût, ou absence de coût).
    const mode = rl
      ? `<span class="txmo-mode rl">${esc(T('js.txmodel.mode_rl', 'DPDK · rate limiter'))}</span>
         <span class="meta">${esc(T('js.txmodel.mode_rl_desc',
           "Le pacing est matériel : déclarer une sortie recalcule l'arbre du port (arrêt/redémarrage "
           + '~1 s de TOUTES les sorties de ce port). Exploiter une sortie déjà déclarée est gratuit.'))}</span>`
      : `<span class="txmo-mode free">${esc(T('js.txmodel.mode_xdp', 'AF-XDP · pacing logiciel'))}</span>
         <span class="meta">${esc(T('js.txmodel.mode_xdp_desc',
           "Pas de rate limiter matériel sur ce port : AUCUN recalcul d'arbre, AUCUN gel. Déclarer, "
           + 'retirer ou reformater une sortie ne coûte rien ici.'))}</span>`;

    // Moteur + ce qu'il SERT aujourd'hui : nécessaires dès l'en-tête (sorties portées) et
    // plus bas (marqueur « redéploiement requis »). Une seule définition, en tête.
    const eng0 = m.engine;
    const engServed = eng0 ? (eng0.booted_active_tx != null ? eng0.booted_active_tx
                                                            : (eng0.active_tx_count || 0)) : 0;
    const secCard = `
      <section class="txmo-sec">
        <h4>${esc(T('js.txmodel.sec_card', 'Le port réglé'))}</h4>
        <div class="row txmo-cardhdr">
          <label class="txmo-lbl">${esc(T('js.txmodel.port', 'Port'))}</label>
          <select data-fk="iface" oninput="TxModel.setIface(this.value)">${ifaceOpts}</select>
          <span class="meta">${p.model ? esc(p.model) + ' · ' : ''}${esc(p.pmd || '')}${
            p.speed_mbps ? ' · ' + Math.round(p.speed_mbps / 1000) + ' Gb/s' : ''}</span>
        </div>
        ${eng0 ? `<div class="meta txmo-owned">
          ${eng0.unit_kind === 'pair'
            ? `<span class="io2110-state active" title="${esc(T('js.txmodel.unit_pair_tip',
                'Paire SMPTE 2022-7 : les deux ports transportent le MÊME flux. La paire compte pour UNE capacité, pas deux.'))}">2022-7</span> ` : ''}
          ${esc((eng0.owned_slots && eng0.owned_slots.length)
            ? T('js.txmodel.unit_carries', 'Ce port porte les sorties {list} du moteur ({n} sur {tot}).')
                .replace('{list}', eng0.owned_slots.join(', '))
                .replace('{n}', eng0.owned_slots.length)
                .replace('{tot}', engServed)
            : T('js.txmodel.unit_carries_none', 'Ce port ne porte aucune sortie du moteur pour l’instant.'))}
        </div>` : ''}
        <div class="txmo-modebox">${mode}</div>
        ${rl ? `<div class="txmo-rlbar" title="${esc(T('js.txmodel.budget_tip',
          'Files du rate limiter consommées par le modèle (1 par sortie vidéo + 1 par flux audio + 1 par ANC), sur le plafond mesuré de la carte.'))}">
          <span>${esc(T('js.txmodel.budget', 'Files RL du modèle'))}</span>
          <span class="txmo-rlval" style="color:${col}">${used} / ${cap || '?'}${
            over ? ' — ' + esc(T('js.txmodel.over', 'plafond dépassé')) : ''}</span>
          ${/* Cette barre n'a JAMAIS été visible : `.nic-bar-track` et `.nic-bar-fill` ne sont
                définis que dans le CSS du plugin 2110_io et dans static/io2110.js, et aucun des
                deux n'est chargé sur la page Réglages, où vit ce panneau. La piste avait donc une
                hauteur nulle : il ne restait que le libellé et le chiffre, sans jamais d'erreur.
                `.ctl-gauge` vient du socle, chargé partout — donc la barre existe enfin.
                Le plafond RL est une limite dure : atteindre le cap EST l'alerte (pas de trait
                intermédiaire), le dépasser est la faute. */''}
          <span class="ctl-gauge ctl-gauge--sans-seuil${over ? ' over' : (cap && used / cap > 0.85) ? ' warn' : ''}"
            role="img" style="--ctl-gauge-w:100%;flex:1"
            aria-label="${esc(T('js.txmodel.budget', 'Files RL du modèle'))} : ${used} / ${cap || '?'}"><span
            class="ctl-gauge-fill" style="transform:scaleX(${(pct / 100).toFixed(3)})"></span></span>
        </div>` : ''}
      </section>`;

    // ── 2. LES SORTIES : format ANNONCÉ (contrat SDP), état, source. Une ligne = une sortie.
    const createdSlots = new Set(((S.verdict || {}).created || []).map(c => c.slot));
    const eng = m.engine;
    // ⚠ Le moteur ne crée de session que pour les slots < `active_tx_count` (budget bootté, bouton
    // « + Ajouter un TX » de Destinations 2110) : déclarer au-delà ne ferait RIEN — un modèle qui ne
    // se réalise pas est pire qu'un modèle absent, on le DIT (ligne par ligne, et en bandeau).
    const engCap = eng ? Math.min(engServed, eng.tx_slots_len || 0) : Infinity;
    const rows = S.draft.map((s, i) => {
      const live = (m.slots || []).find(x => x.idx === i) || {};
      const v = s.video || {};
      const willCommit = rl && createdSlots.has(i);
      const inert = i >= engCap;
      const costChip = inert
        ? `<span class="txmo-cost commit" title="${esc(T('js.txmodel.cost_grow_tip',
             'Au-delà du budget bootté du moteur : « Appliquer » RECRÉE le moteur pour réserver les '
             + 'files et provisionner cette sortie (coupure brève de tous les flux, confirmée avant).'))}">${
             esc(T('js.txmodel.cost_grow', 'provisionnée (recréation)'))}</span>`
        : willCommit
        ? `<span class="txmo-cost commit" title="${esc(T('js.txmodel.cost_commit_tip',
             "Cette sortie va (re)créer une session vidéo : un recalcul d'arbre, donc un gel ~1 s de tout le port."))}">${
             esc(T('js.txmodel.cost_commit', '1 recalcul d’arbre'))}</span>`
        : `<span class="txmo-cost free" title="${esc(T('js.txmodel.cost_free_tip',
             'Aucune session à créer : rien à recalculer, aucune sortie ne gèle.'))}">${
             esc(T('js.txmodel.cost_free', 'gratuit'))}</span>`;
      const stKey = live.state || 'declared';
      const stLbl = {
        active: T('js.txmodel.st_active', 'émet'),
        provisioned: T('js.txmodel.st_provisioned', 'déclarée · silencieuse'),
        declared: T('js.txmodel.st_declared', 'pas encore provisionnée'),
        out_of_layout: T('js.txmodel.st_out', 'hors modèle'),
        free: T('js.txmodel.st_free', 'libre'),
      }[stKey] || stKey;
      const stTip = {
        active: T('js.txmodel.st_active_tip', 'La sortie émet : une source est câblée dessus.'),
        provisioned: T('js.txmodel.st_provisioned_tip',
          'La session et sa feuille de pacing existent déjà, au débit nominal du format déclaré : '
          + "l'activer (câbler une source du même format) ne coûte RIEN."),
        declared: T('js.txmodel.st_declared_tip',
          "Déclarée dans le modèle mais pas encore poussée au moteur — « Appliquer » la provisionnera."),
        out_of_layout: T('js.txmodel.st_out_tip',
          "Sortie présente sur le moteur mais absente du modèle de la carte : l'armer recalera l'arbre."),
        free: T('js.txmodel.st_free_tip', 'Aucune sortie déclarée à cet emplacement.'),
      }[stKey] || '';
      const src = live.source
        ? `<code class="txmo-src" title="${esc(T('js.txmodel.src_tip', 'Source MXL câblée sur cette sortie'))}">${esc(live.source)}</code>`
        : `<span class="meta">${esc(T('js.txmodel.src_none', 'aucune source'))}</span>`;
      const dest = live.dest
        ? `<code class="txmo-dest">${esc(live.dest.mcast)}:${esc(live.dest.port)}</code>`
        : `<span class="meta">${esc(T('js.txmodel.dest_none', 'destination non allouée'))}</span>`;
      const announced = live.announced
        ? `<span class="meta" title="${esc(T('js.txmodel.announced_tip',
            "Format actuellement ANNONCÉ par la session (c'est le contrat SDP vu par les récepteurs)."))}">${
            esc(T('js.txmodel.announced', 'annoncé'))} ${esc(_fmtLabel(live.announced))}</span>` : '';
      // Hors mode EXPERT : la carte se LIT (le format vient du modèle appliqué). L'édition manuelle
      // reste disponible d'un clic, mais ce n'est plus le geste par défaut.
      const fields = S.expert ? `
          <label class="txmo-lbl">${esc(T('js.txmodel.format', 'Format annoncé'))}</label>
          <select data-fk="s${i}fmt" oninput="TxModel.setPreset(${i}, this.value)" style="min-width:170px"
            aria-label="${esc(T('js.txmodel.format', 'Format annoncé'))}">${_presetOptions(v)}</select>
          ${_isOrphan(v) ? `<span class="txlib-orphan" title="${esc(T('js.txlib.fmt_orphan_tip',
            'Ce format ne correspond à aucun préréglage de Réglages → Vidéo (renommé ou supprimé depuis). '
            + 'Il reste valide et inchangé ; en choisir un autre le remplacera.'))}">${
            esc(T('js.txlib.fmt_orphan', 'hors liste'))}</span>` : ''}
          <label class="txmo-lbl">${esc(T('js.txmodel.audio', 'Flux audio'))}</label>
          <input type="number" min="0" max="8" value="${s.audio_count || 0}" style="width:64px"
            data-fk="s${i}aud" oninput="TxModel.setAudio(${i}, this.value)">
          <label class="toggle-inline">
            <input type="checkbox" class="ios-toggle" ${s.anc ? 'checked' : ''}
              data-fk="s${i}anc" oninput="TxModel.setAnc(${i}, this.checked)">ANC</label>`
        : '';   // hors mode expert : format/audio/ANC sont déjà dans la ligne compacte (summary)
      // ★ LIGNE COMPACTE (registre produit dense) : une sortie = une ligne — index · format annoncé ·
      //   puces essence (audio/ANC) · état · coût files · source. Le détail (édition format/audio/ANC,
      //   destination, format annoncé live) s'ouvre à la demande via le <details> — état d'ouverture
      //   mémorisé (S.slotOpen) pour survivre au re-rendu non destructif.
      const open = S.slotOpen.has(i);
      const bodyInner = `${fields}<span style="flex:1"></span>${dest} ${announced}`;
      return `
      <details class="txmo-slot${willCommit ? ' commit' : ''}"${open ? ' open' : ''}
        ontoggle="TxModel.slotToggle(${i}, this.open)">
        <summary class="txmo-slot-sum">
          <span class="txmo-idx">Tx #${i + 1}</span>
          ${live.name ? `<span class="txmo-name">${esc(live.name)}</span>` : ''}
          <span class="txmo-fmt">${esc(_fmtLabel(v))}</span>
          ${s.audio_count ? `<span class="txmo-chip" title="${esc(T('js.txmodel.audio', 'Flux audio'))}">${
            s.audio_count}×A</span>` : ''}
          ${s.anc ? `<span class="txmo-chip">ANC</span>` : ''}
          <span class="io2110-state ${esc(stKey)}" title="${esc(stTip)}">${esc(stLbl)}</span>
          ${costChip}
          <span class="meta txmo-qcost">${cost(s)} ${esc(T('js.txmodel.queues', 'files'))}</span>
          <span style="flex:1"></span>
          ${src}
        </summary>
        <div class="txmo-slot-body">
          <div class="txmo-slot-fields">${bodyInner}</div>
          ${S.expert ? `<div class="txmo-slot-actions"><button class="btn btn-sm btn-red"
            title="${esc(T('js.txmodel.remove_tip', 'Retirer cette sortie du modèle (prend effet à l’application).'))}"
            onclick="TxModel.removeSlot(${i})">✕ ${esc(T('js.txmodel.remove', 'Retirer la sortie'))}</button></div>` : ''}
        </div>
      </details>`;
    }).join('');

    // Sorties présentes sur le moteur mais HORS du modèle : à montrer, sinon elles sont invisibles.
    const orphans = (m.slots || []).filter(x => x.state === 'out_of_layout').map(x => `
      <div class="txmo-slot orphan">
        <div class="txmo-slot-head">
          <span class="txmo-idx">Tx #${x.idx + 1}</span>
          <span class="io2110-state out_of_layout">${esc(T('js.txmodel.st_out', 'hors modèle'))}</span>
          <span class="meta">${esc(T('js.txmodel.orphan_hint',
            'Sortie du moteur non déclarée dans le modèle de la carte — la déclarer ci-dessus pour la figer dans l’arbre.'))}</span>
          <span style="flex:1"></span>
          ${x.source ? `<code class="txmo-src">${esc(x.source)}</code>` : ''}
        </div>
      </div>`).join('');

    const short = eng && engCap < S.draft.length;
    const secSlots = `
      <section class="txmo-sec">
        <h4>${esc(T('js.txmodel.sec_slots', 'Les sorties déclarées'))}</h4>
        <div class="meta txmo-lead">${esc(T('js.txmodel.slots_lead',
          "Chaque sortie est déclarée à l'avance avec le format qu'elle ANNONCE (contrat SDP). La session "
          + 'et sa feuille de pacing sont créées dès la déclaration, au débit nominal de ce format : câbler '
          + 'ensuite une source du MÊME format ne coûte rien.'))}</div>
        ${rows || `<div class="meta txmo-empty">${esc(T('js.txmodel.empty',
          'Aucune sortie déclarée — ajouter une sortie, ou charger un exemple.'))}</div>`}
        ${orphans}
        ${short ? `<div class="alert alert-info txmo-alert">${esc(T('js.txmodel.grow_on_apply',
          'Le moteur provisionne aujourd’hui {n} sortie(s). « Appliquer » ce modèle en provisionnera '
          + 'davantage : le moteur sera RECRÉÉ pour réserver les files (coupure brève de tous les flux), '
          + 'après confirmation.')
          .replace('{n}', engCap))}</div>` : ''}
        ${(S.check && (S.check.errors || []).length)
          ? `<div class="alert alert-error txmo-alert">${esc((S.check.errors || []).join(' '))}</div>` : ''}
        <div class="row txmo-slotctrls">
          <label class="toggle-inline" title="${esc(T('js.txmodel.expert_tip',
            'Éditer les sorties de CETTE carte à la main, hors modèle. Le geste normal est d’appliquer '
            + 'un modèle de la bibliothèque ; l’édition manuelle fera DIVERGER la carte de son modèle.'))}">
            <input type="checkbox" class="ios-toggle" ${S.expert ? 'checked' : ''}
              data-fk="expert" oninput="TxModel.setExpert(this.checked)"> ${esc(T('js.txmodel.expert',
              'Édition experte (hors modèle)'))}</label>
          ${S.expert ? `<button class="btn btn-sm" onclick="TxModel.addSlot()">+ ${
            esc(T('js.txmodel.add', 'Ajouter une sortie'))}</button>` : ''}
          ${(S.expert && (m.presets || []).length) ? `<select id="txmo-preset" style="max-width:280px">${
            (m.presets || []).map((p2, k) => `<option value="${k}">${esc(p2.label)}</option>`).join('')
          }</select><button class="btn btn-sm" onclick="TxModel.loadPreset()">${
            esc(T('js.txmodel.load_preset', 'Charger l’exemple'))}</button>` : ''}
        </div>
      </section>`;

    // ── 2bis. APPLIQUER UN MODÈLE — le geste normal. Le modèle vient de la BIBLIOTHÈQUE (onglet
    //     « Modèle de carte 2110 »), il est réglé pour un TYPE de carte ; ici on le pose sur CETTE
    //     carte, avec son DIFF et son COÛT affichés AVANT le clic.
    const bind = S.binding || {};
    const bindLine = bind.model
      ? `<div class="meta txmo-bind">${esc(T('js.txmodel.bind_from', 'Issue du modèle'))}
           <b>${esc(bind.model.name)}</b>${bind.model.deleted
             ? ' — ' + esc(T('js.txmodel.bind_deleted', 'modèle supprimé de la bibliothèque'))
             : ''}${bind.diverged
             ? ` <span class="txmo-diverged" title="${esc(T('js.txmodel.diverged_tip',
                 'Le layout déclaré de cette carte ne correspond plus au modèle dont il est issu '
                 + '(édition experte ici, ou modèle modifié depuis). Ce n’est pas une erreur — '
                 + 'ré-appliquer le modèle réaligne la carte.'))}">${
                 esc(T('js.txmodel.diverged', 'la carte a divergé du modèle'))}</span>` : ''}</div>`
      : `<div class="meta txmo-bind">${esc(T('js.txmodel.bind_none',
          'Cette carte n’est issue d’aucun modèle (layout réglé à la main, ou jamais réglé).'))}</div>`;
    const compat = S.cands.filter(c => c.compatible);
    const incompat = S.cands.filter(c => !c.compatible);
    const pickOpts = `<option value="">—</option>` + compat.map(c =>
      `<option value="${c.id}"${c.id === S.pickId ? ' selected' : ''}>${esc(c.name)} · ${
        c.slot_count} ${esc(T('js.txmodel.outputs', 'sorties'))}</option>`).join('');
    const pv = S.preview;
    let pvHtml = '';
    if (S.pvBusy) {
      pvHtml = `<div class="meta">${esc(T('js.txmodel.pv_loading', 'Calcul du diff et du coût…'))}</div>`;
    } else if (pv) {
      const changed = (pv.diff || []).filter(d => d.op !== 'same');
      const diffHtml = changed.length
        ? `<ul class="txmo-diff">${changed.map(d => `<li class="${esc(d.op)}">Tx #${d.idx + 1} — ${
            d.op === 'add' ? esc(T('js.txmodel.d_add', 'ajoutée')) + ' : ' + esc(_fmtLabel(d.after.video))
            : d.op === 'remove' ? esc(T('js.txmodel.d_rm', 'retirée'))
            : esc(T('js.txmodel.d_chg', 'reformatée')) + ' : ' + esc(_fmtLabel(d.before.video))
              + ' → ' + esc(_fmtLabel(d.after.video))}</li>`).join('')}</ul>`
        : `<div class="meta">${esc(T('js.txmodel.d_none',
            'Aucun changement : cette carte est déjà réglée comme ce modèle.'))}</div>`;
      const pvErr = (pv.check && (pv.check.errors || []).length)
        ? `<div class="alert alert-error txmo-alert">${esc(pv.check.errors.join(' '))}</div>` : '';
      // L'aperçu montre le DIFF seul ; le COÛT (zone d'impact partagée) et l'action unique
      // « Appliquer le modèle » vivent dans la barre d'application ci-dessous — un seul bouton d'apply.
      pvHtml = `${diffHtml}${pvErr}
        <div class="meta txmo-lead" style="margin-top:6px">${esc(T('js.txmodel.pv_apply_hint',
          'Cliquez « Appliquer le modèle » plus bas pour poser ce modèle sur la carte — le coût '
          + '(sorties qui figeront) y est affiché avant le clic.'))}</div>`;
    }
    const secModel = `
      <section class="txmo-sec txmo-model">
        <h4>${esc(T('js.txmodel.sec_pick_model', 'Choisir un modèle (bibliothèque)'))}</h4>
        <div class="meta txmo-lead">${esc(T('js.txmodel.model_lead',
          'Les modèles sont des gabarits RÉUTILISABLES réglés par TYPE de carte (onglet « Modèle de '
          + 'carte 2110 »). Les créer ne coûte rien ; c’est ICI, en les posant sur une carte réelle, '
          + 'que le coût se paie.'))}</div>
        ${bindLine}
        <div class="row txmo-slotctrls">
          <label class="txmo-lbl">${esc(T('js.txmodel.pick', 'Modèle'))}</label>
          <select data-fk="pick" oninput="TxModel.pick(this.value)" style="min-width:260px">${pickOpts}</select>
          <button class="btn btn-sm" onclick="switchReseauTab('txmodel')">${
            esc(T('js.txmodel.lib_link', 'Bibliothèque de modèles'))}</button>
          <span style="flex:1"></span>
          ${S.capture ? `
            <input id="txmo-capname" placeholder="${esc(T('js.txmodel.cap_name', 'Nom du modèle'))}"
              data-fk="capname" value="${esc(S.capName)}" oninput="TxModel.setCapName(this.value)"
              style="min-width:200px">
            <button class="btn btn-sm btn-green" ${S.busy ? 'disabled' : ''}
              onclick="TxModel.captureCard()">${esc(T('js.txmodel.cap_do', 'Créer le modèle'))}</button>
            <button class="btn btn-sm" onclick="TxModel.cancelCapture()">${
              esc(T('js.txmodel.cap_cancel', 'Annuler'))}</button>`
          : `<button class="btn btn-sm" onclick="TxModel.openCapture()"
              title="${esc(T('js.txmodel.cap_tip',
                'Capture les sorties RÉELLES de cette carte dans un nouveau modèle réutilisable, '
                + 'pré-rattaché à son type. C’est une LECTURE : la carte n’est pas touchée (aucun '
                + 'recalcul d’arbre, aucun redéploiement).'))}">📥 ${
              esc(T('js.txmodel.cap_open', 'Enregistrer comme modèle…'))}</button>`}
        </div>
        ${S.capture ? `<div class="meta" style="margin-top:6px">${esc(T('js.txmodel.cap_lead',
          'Capturer, c’est LIRE : la carte ne bouge pas, rien n’est recalculé, rien n’est redéployé. '
          + 'Le modèle créé sera rattaché à cette carte — sa divergence future deviendra mesurable.'))}</div>` : ''}
        ${!compat.length ? `<div class="meta" style="margin-top:6px">⚠ ${esc(T('js.txmodel.no_compat',
          'Aucun modèle réglé pour ce type de carte — en créer un dans la bibliothèque.'))}</div>` : ''}
        ${incompat.length ? `<div class="meta" style="margin-top:6px">${
          esc(T('js.txmodel.incompat', 'Modèles écartés (type de carte différent) :'))} ${
          incompat.map(c => `<span title="${esc(c.why)}">${esc(c.name)}</span>`).join(', ')}</div>` : ''}
        ${pvHtml}
      </section>`;

    // ── (La pédagogie « ce qui est gratuit / ce qui coûte » a été DÉPLACÉE dans la bibliothèque —
    //     static/tx_models.js. Un écran de réglage n'est pas un cours ; rien n'est perdu.)

    // ── 4. APPLIQUER LE MODÈLE — UNE seule action, toujours visible (plus un repli) : déclarer PUIS
    //     provisionner en un clic. La source de la déclaration est le modèle CHOISI (secModel) s'il y en
    //     a un, sinon le brouillon édité (expert). Le COÛT (zone d'impact partagée : victimes nommées)
    //     s'affiche AVANT le clic — celui de l'aperçu du modèle pické, sinon celui du layout courant.
    //     En AF-XDP : direct, sans modale. En DPDK : txMaintConfirm NOMME les victimes et offre le report.
    const picked = !!(S.pickId && S.preview);
    const applyVerdict = picked ? S.preview.verdict : S.verdict;
    const applyPorts = picked ? S.iface : ((S.verdict && S.verdict.ports) || [S.iface]).join(', ');
    const applyImpact = impactHtml(applyVerdict, applyPorts);
    const nCommits = ((applyVerdict && applyVerdict.created) || []).filter(c => c.essence === 'video').length;
    const blockErrs = picked ? ((S.preview.check || {}).errors || []) : ((S.check || {}).errors || []);
    const applyDisabledWhy = !eng
      ? T('js.txmodel.apply_no_engine', 'Aucun moteur 2110 déployé sur cette carte : rien à provisionner.')
      : blockErrs.length
        ? T('js.txmodel.apply_over', 'Le modèle dépasse le budget de la carte : corriger avant d’appliquer.')
        : '';
    const secApplyBar = `
      <section class="txmo-sec txmo-applybar">
        <h4>${esc(T('js.txmodel.sec_apply', 'Appliquer le modèle'))}</h4>
        <div class="meta txmo-lead">${esc(T('js.txmodel.apply_lead',
          'Un seul geste : la carte est réglée sur le modèle choisi (ou le layout édité), PUIS les '
          + 'sorties sont provisionnées sur le moteur. Gratuit en AF-XDP ; en DPDK, un seul recalcul '
          + 'd’arbre — appliqué maintenant, ou reporté en fenêtre de maintenance (choix à la confirmation).'))}</div>
        ${applyImpact}
        <div class="row txmo-actions">
          <button class="btn btn-sm btn-orange" ${(S.busy || applyDisabledWhy) ? 'disabled' : ''}
            title="${esc(applyDisabledWhy || T('js.txmodel.apply_tip',
              'Règle la carte sur le modèle choisi (ou le layout édité), puis provisionne les sorties '
              + 'sur le moteur — événement de maintenance en DPDK (report possible), gratuit en AF-XDP.'))}"
            onclick="TxModel.applyModel()">
            ${S.spin ? '<span class="txmo-spinner txmo-spinner-sm" aria-hidden="true"></span>' : ''}${
              esc(S.spin ? T('js.txmodel.apply_busy', 'Application en cours…')
                         : T('js.txmodel.apply', 'Appliquer le modèle'))}${
              (!S.spin && rl && nCommits) ? ' ⬤' : ''}</button>
          ${applyDisabledWhy ? `<span class="meta">${esc(applyDisabledWhy)}</span>` : ''}
          ${S.expert ? `<button class="btn btn-sm" ${S.busy ? 'disabled' : ''} onclick="TxModel.save()"
            title="${esc(T('js.txmodel.save_tip', 'Enregistre la déclaration seule — gratuit, aucune sortie ne bouge.'))}">
            ${esc(T('js.txmodel.save', 'Enregistrer le layout de la carte'))}</button>` : ''}
          <span style="flex:1"></span>
          ${S.dirty ? `<span class="meta txmo-dirty">${esc(T('js.txmodel.dirty', 'modifications non enregistrées'))}</span>` : ''}
          <span class="meta">${m.layout.updated_at
            ? esc(T('js.txmodel.updated', 'modèle enregistré le')) + ' ' + esc(m.layout.updated_at.replace('T', ' '))
            : esc(T('js.txmodel.never', 'jamais enregistré'))}</span>
        </div>
      </section>`;

    // ── 5. MAINTENANCE PLANIFIÉE — SECONDAIRE, repli fermé par défaut : seulement le SUIVI des reports
    //     (bac de maintenance). Ce n'est plus l'action principale. N'apparaît que s'il y a des changements
    //     en attente. Réutilise S.openApply pour la mémoire d'ouverture (contrat de rendu non destructif).
    const pend = m.pending || [];
    const secMaint = pend.length ? `
      <details class="txmo-fold txmo-maint"${S.openApply ? ' open' : ''} ontoggle="TxModel.toggleApply(this.open)">
        <summary>${esc(T('js.txmodel.sec_maint', 'Maintenance planifiée'))} <span class="txm-bin-count">${pend.length}</span></summary>
        <div class="txmo-fold-body">
        <div class="meta txmo-lead">${esc(T('js.txmodel.maint_lead',
          'Les changements reportés attendent ici. Appliquez-les tout de suite, planifiez-les à une '
          + 'heure, ou retirez-les du bac.'))}</div>
        <div class="txm-bin">
          <span class="txm-bin-count">${pend.length}</span>
          <span>${esc(T('js.txm.bin_title', 'Changements en attente'))}</span>
          <span class="txm-bin-list">${pend.map(pp => `<span class="txm-bin-item">${
            esc(pp.label || pp.op)}${pp.apply_at ? ' · ' + esc(pp.apply_at.slice(11, 16)) : ''}
            <button onclick="TxModel.cancelPending(${pp.id})" title="${esc(T('js.txm.drop', 'Retirer du bac'))}">✕</button></span>`).join('')}</span>
          <span style="flex:1"></span>
          <input type="time" id="txmo-at" style="width:110px" title="${esc(T('js.txm.schedule_tip',
            'Appliquer le bac à cette heure'))}">
          <button class="btn btn-sm" onclick="TxModel.schedulePending()">🕑 ${esc(T('js.txm.schedule', 'Planifier'))}</button>
          <button class="btn btn-sm btn-orange" onclick="TxModel.applyPending()">${esc(T('js.txm.apply_now', 'Appliquer le bac'))}</button>
        </div>
        </div>
      </details>` : '';

    // ★ Les messages (surtout les ÉCHECS) se VOIENT : bandeau `.alert` en tête du panneau — pas une
    // ligne grise en bas d'une section, qu'on ne lit jamais (échec silencieux = anti-patron n°1).
    const isErr = /^⚠/.test(S.msg || '');
    // ★ En cours (S.spin) : bandeau AVEC spinner animé (l'anim CONVOIE l'état « chargement », ≠ déco),
    //   sans croix de fermeture (état transitoire, pas un message qu'on referme). Le libellé (S.msg) POSE
    //   L'ATTENTE — cf. applyModel/_engineApply qui distinguent déclaration (rapide) et provisionnement
    //   (recréation du moteur, ~30 s, coupure brève).
    const banner = S.spin
      ? `<div class="alert alert-info txlib-banner txmo-progress" role="status" aria-live="polite">
           <span class="txmo-spinner" aria-hidden="true"></span><span>${esc(S.msg)}</span></div>`
      : S.msg
      ? `<div class="alert ${isErr ? 'alert-error' : 'alert-info'} txlib-banner"
             role="${isErr ? 'alert' : 'status'}">${esc(S.msg)}
           <button class="txlib-banner-x" onclick="TxModel.clearMsg()" aria-label="fermer">✕</button></div>`
      : '';
    // ★ PARTAGE DES RÔLES (demande utilisateur) — « la fiche d'interface CONSTATE et APPLIQUE ; la
    //   page Modèle ÉDITE et EXPLIQUE » :
    //   · mode 'card' (fiche d'interface) : la carte + l'ÉTAT RÉEL de ses sorties (lecture) + la
    //     capture « Enregistrer comme modèle… ». Pas de cours, pas d'édition. Le CHOIX du modèle et
    //     son application sont un CHAMP de la fiche (section « Modèle » du formulaire d'interface).
    //   · mode 'page' (Modèles de carte 2110) : tout — édition experte, pédagogie gratuit/coûteux,
    //     provisionnement, application d'un modèle.
    // ★ RÔLES (architecture finale, décision utilisateur) : « la fiche d'interface CONSTATE, APPLIQUE
    //   et CAPTURE ; la page Modèles ÉDITE des gabarits et EXPLIQUE ». Ce panneau est le PAR-CARTE :
    //   état réel des sorties, choix + application d'un modèle, capture, édition experte. La PÉDAGOGIE
    //   (gratuit vs coûteux, commit TM, AF-XDP sans commit) a été DÉPLACÉE — pas supprimée — dans la
    //   bibliothèque (static/tx_models.js) : un écran de réglage n'est pas un cours.
    // ★ Rendu NON DESTRUCTIF (cf. tx_models.js) : focus + curseur mémorisés puis restaurés, et les
    //   boutons ne prennent pas le focus → aucun `change` ne s'intercale entre mousedown et mouseup,
    //   donc plus aucun clic mangé par un re-rendu.
    const ae = document.activeElement;
    const fk = (ae && S.el.contains(ae)) ? ae.dataset.fk : null;
    const selS = (fk && ae.selectionStart != null) ? ae.selectionStart : null;
    const selE = (fk && ae.selectionEnd != null) ? ae.selectionEnd : null;

    S.el.innerHTML = banner + secCard + secModel + secApplyBar + secSlots + secMaint;

    if (fk) {
      const back = S.el.querySelector(`[data-fk="${fk}"]`);
      if (back) {
        back.focus();
        if (selS != null && back.setSelectionRange) {
          try { back.setSelectionRange(selS, selE); } catch (_) { /* type sans sélection */ }
        }
      }
    }
    S.el.querySelectorAll('button').forEach(b =>
      b.addEventListener('mousedown', e => e.preventDefault()));
  }

  // ─── Actions ───────────────────────────────────────────────────────────────────────────────────
  window.TxModel = {
    mount(el, node, ifaces, opts) {
      // Anti-écrasement : le panneau est (re)monté à chaque ouverture de l'onglet Réseau ; recharger
      // par-dessus une édition en cours effacerait le brouillon de l'utilisateur.
      const mode = ((opts || {}).mode === 'card') ? 'card' : 'page';
      const same = (S.el === el && S.node === node && S.mode === mode && S.model);
      S.mode = mode;
      S.el = el; S.node = node; S.ifaces = ifaces || [];
      if (!S.ifaces.length) {
        el.innerHTML = `<div class="meta">${esc(T('js.txmodel.no_iface',
          'Aucune carte média 2110 sur ce nœud.'))}</div>`;
        return;
      }
      if (!S.iface || !S.ifaces.some(n => n.ifname === S.iface)) S.iface = S.ifaces[0].ifname;
      if (same && S.dirty) { render(); return; }
      load();
    },
    setIface(i) { S.iface = i; S.pickId = null; S.preview = null; load(); },
    setExpert(b) { S.expert = !!b; render(); },
    clearMsg() { S.msg = ''; render(); },
    // Progressive disclosure : on retient l'état d'ouverture des sections repliées et de chaque sortie
    // (pas de render() ici — le <details> gère l'affichage nativement ; on ne veut pas voler le focus).
    toggleApply(o) { S.openApply = !!o; },
    slotToggle(i, o) { if (o) S.slotOpen.add(i); else S.slotOpen.delete(i); },

    // ── AMORÇAGE : « Enregistrer comme modèle… » — capture les sorties RÉELLES de cette carte dans un
    //    nouveau modèle de la bibliothèque, pré-typé (type déduit de la carte) et rattaché à elle.
    //    ⚠ C'est une LECTURE : aucune écriture sur la carte, aucun commit, aucun redéploiement.
    openCapture() {
      S.capture = true;
      S.capName = S.capName || `${S.iface} — ${new Date().toISOString().slice(0, 10)}`;
      render();
    },
    cancelCapture() { S.capture = false; render(); },
    setCapName(v) { S.capName = v; },
    async captureCard() {
      S.busy = true; S.msg = T('js.txmodel.cap_doing', 'Capture de la carte…'); render();
      try {
        const r = await fetch('/api/tx-card-models/capture', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({node_id: S.node, iface: S.iface, name: S.capName})});
        const j = await r.json().catch(() => ({}));
        S.busy = false;
        if (!r.ok || !j.ok) { S.msg = '⚠ ' + (j.error || `HTTP ${r.status}`); render(); return; }
        S.capture = false;
        S.msg = T('js.txmodel.captured',
          '✓ modèle créé depuis cette carte (la carte n’a pas été touchée)');
        S.pickId = j.id;
        load();                       // recharge le rattachement (la carte est désormais « issue de » ce modèle)
      } catch (e) {
        S.busy = false; S.msg = '⚠ ' + e.message; render();
      }
    },
    // Choix d'un modèle de la bibliothèque → on affiche AUSSITÔT le diff et le coût (avant le clic).
    pick(v) { S.pickId = v ? parseInt(v, 10) : null; S.preview = null; loadPreview(); },

    // ★ « Appliquer le modèle » = L'UNIQUE action (fusion des ex-« Appliquer un modèle » et
    //   « Provisionner »). Un clic = DÉCLARER puis PROVISIONNER, sans étapes séparées :
    //   1) la déclaration du layout est écrite — depuis le modèle CHOISI (tx-model/apply) s'il y en a un,
    //      sinon depuis le brouillon édité en mode expert (tx-layout, via save()). Aucun matériel touché.
    //   2) les sorties sont provisionnées sur le moteur (_engineApply) — le geste qui coûte en DPDK :
    //      200 → fait (cas AF-XDP, sans modale) ; 409 needs_confirm → txMaintConfirm NOMME les victimes
    //      et propose « Appliquer maintenant » ou « Planifier en maintenance » (report au bac).
    async applyModel() {
      if (S.pickId) {
        // PHASE 1 — déclaration (rapide) : écrire le layout depuis le modèle choisi. Spinner + libellé
        // d'étape honnête ; le provisionnement (LENT) prend le relais dans _engineApply.
        S.busy = true; S.spin = true;
        S.msg = T('js.txmodel.declaring', 'Déclaration du modèle…'); render();
        try {
          const r = await fetch(`/api/nodes/${S.node}/tx-model/apply`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({iface: S.iface, model_id: S.pickId})});
          const j = await r.json().catch(() => ({}));
          if (!r.ok || !j.ok) { S.busy = false; S.spin = false; S.msg = '⚠ ' + (j.error || `HTTP ${r.status}`); render(); return; }
        } catch (e) {
          S.busy = false; S.spin = false; S.msg = '⚠ ' + e.message; render(); return;
        }
      } else {
        // Pas de modèle choisi : on provisionne le layout édité/déclaré courant (enregistré d'abord —
        // save() gère busy + bandeau d'erreur et renvoie false si le budget est dépassé).
        if (!(await this.save())) return;
      }
      await this._engineApply();
    },
    // Le format vient d'un PRÉRÉGLAGE (valeurs copiées : w/h/fps/scan/bits + chroma + colorimétrie).
    // '-1' = format hors liste : on ne le réécrit JAMAIS en douce.
    setPreset(i, idx) {
      const f = (S.fmts || [])[parseInt(idx, 10)];
      if (!f) return;
      S.draft[i].video = {w: f.w, h: f.h, fps: f.fps, scan: f.scan, bd: f.bit_depth,
                          chroma: f.chroma, colorimetry: f.colorimetry};
      S.draft[i].fmt_label = f.label;
      touch();
    },
    setAudio(i, n) {
      const v = parseInt(n, 10);
      if (isNaN(v)) return;   // champ vidé en cours de frappe
      S.draft[i].audio_count = Math.max(0, Math.min(8, v));
      touch();
    },
    setAnc(i, b) { S.draft[i].anc = !!b; touch(); },
    addSlot() {
      const last = S.draft[S.draft.length - 1];
      // Défaut = le format par défaut des Réglages (jamais une valeur en dur devinée ici).
      const def = (S.fmts || []).find(f => f.label === window._videoFormatDefault) || (S.fmts || [])[0];
      S.draft.push(last
        ? {video: {...last.video}, fmt_label: last.fmt_label, audio_count: last.audio_count, anc: last.anc}
        : {video: def ? {w: def.w, h: def.h, fps: def.fps, scan: def.scan, bd: def.bit_depth,
                         chroma: def.chroma, colorimetry: def.colorimetry}
                      : {w: 1920, h: 1080, fps: 25, bd: 10, scan: 'p', chroma: '422', colorimetry: '709'},
           fmt_label: def ? def.label : '', audio_count: 1, anc: false});
      touch();
    },
    removeSlot(i) { S.draft.splice(i, 1); touch(); },
    loadPreset() {
      const sel = document.getElementById('txmo-preset');
      const p = ((S.model || {}).presets || [])[sel ? parseInt(sel.value) : -1];
      if (!p) return;
      S.draft = (p.slots || []).map(s => ({video: s.video ? {...s.video} : null,
                                           audio_count: s.audio_count, anc: !!s.anc}));
      touch();
    },

    async save() {
      S.busy = true; S.msg = T('js.txmodel.saving', 'Enregistrement…'); render();
      try {
        const r = await fetch(`/api/nodes/${S.node}/tx-layout`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({iface: S.iface, slots: S.draft}),
        });
        const j = await r.json();
        if (!r.ok || !j.ok) {
          const e = (j.check && j.check.errors) ? j.check.errors.join(' ') : (j.error || `HTTP ${r.status}`);
          S.msg = '⚠ ' + e; S.busy = false; render(); return false;
        }
        S.busy = false; S.dirty = false;
        S.msg = T('js.txmodel.saved', '✓ modèle enregistré (aucune sortie n’a bougé)');
        if (S.model) S.model.layout.updated_at = (j.layout || {}).updated_at;
        render();
        return true;
      } catch (e) {
        S.busy = false; S.msg = '⚠ ' + e.message; render(); return false;
      }
    },

    // Provisionnement des sorties DÉCLARÉES sur le moteur (le geste qui coûte, en DPDK). Un seul
    // ÉVÉNEMENT de maintenance : le serveur classe l'action (étage 2), la modale partagée txMaintConfirm
    // NOMME les sorties qui vont figer, et l'action peut partir au BAC (application différée/planifiée).
    async _engineApply() {
      const vmid = ((S.model || {}).engine || {}).vmid;
      if (!vmid) { S.busy = false; S.spin = false; S.msg = T('js.txmodel.applied_decl',
        '✓ carte réglée — aucun moteur déployé : les sorties seront créées au déploiement.');
        load(); return; }
      const post = body => fetch(`/api/mtl/${vmid}/tx-layout/apply`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      // PHASE 2 — provisionnement (LENT : le moteur 2110 est RECRÉÉ — détruit + recréé, binding vfio,
      // provisionnement — plusieurs dizaines de secondes). Le libellé POSE L'ATTENTE ; le spinner reste
      // animé le temps du POST bloquant (le backend ne streame pas sa progression).
      const provMsg = T('js.txmodel.provisioning',
        'Provisionnement — recréation du moteur en cours (peut prendre ~30 s ; coupure brève des flux)…');
      S.busy = true; S.spin = true; S.msg = provMsg; render();
      let r = await post({});
      if (r.status === 409) {
        const j = await r.json().catch(() => ({}));
        if (j && j.needs_confirm) {
          // ⚠ L'indicateur EN COURS ne doit PAS tourner pendant que la modale attend l'utilisateur :
          // on le coupe, on demande la confirmation, puis on le relance seulement quand ça part vraiment.
          S.busy = false; S.spin = false; S.msg = ''; render();
          const choice = await window.txMaintConfirm(j.verdict || {detail: j.reason},
                                                     {allowDefer: !!(j.verdict && j.verdict.deferrable)});
          if (!choice) { render(); return; }
          S.busy = true; S.spin = true; S.msg = provMsg; render();
          r = await post(choice === 'defer' ? {defer: true} : {confirm: true});
        }
      }
      const j = await r.json().catch(() => ({}));
      S.busy = false; S.spin = false;
      if (!r.ok && !j.deferred) S.msg = '⚠ ' + (j.error || `HTTP ${r.status}`);
      else S.msg = j.deferred ? T('js.txmodel.deferred', 'Changement mis au bac de maintenance.')
                              : T('js.txmodel.applied', '✓ modèle appliqué au moteur');
      load();
    },

    async applyPending() {
      const vmid = ((S.model || {}).engine || {}).vmid;
      if (!vmid) return;
      const r = await fetch(`/api/mtl/${vmid}/tx-maintenance/apply`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      const j = await r.json().catch(() => ({}));
      if (j.errors && j.errors.length) S.msg = '⚠ ' + j.errors.join(' · ');
      load();
    },
    async schedulePending() {
      const vmid = ((S.model || {}).engine || {}).vmid;
      const el = document.getElementById('txmo-at');
      if (!vmid || !el || !el.value) {
        S.msg = T('js.txm.pick_time', 'Choisir une heure d’application.'); render(); return;
      }
      const now = new Date();
      const d = new Date(`${now.toISOString().slice(0, 10)}T${el.value}`);
      if (d <= now) d.setDate(d.getDate() + 1);
      const at = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${
        String(d.getDate()).padStart(2, '0')}T${el.value}`;
      await fetch(`/api/mtl/${vmid}/tx-maintenance/apply`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({at})});
      load();
    },
    async cancelPending(pid) {
      await fetch(`/api/tx-maintenance/${pid}`, {method: 'DELETE'});
      load();
    },
  };
})();
