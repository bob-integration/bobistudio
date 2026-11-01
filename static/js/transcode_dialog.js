// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Fenêtre « Transcoder… » — UNE seule pour tous les points d'entrée (page Ferme, menu d'un média, d'une
// sélection ou d'un bin du gestionnaire). Elle règle le travail, montre le NOM qu'aura chaque sortie
// avant de lancer (/api/transcode-farm/preview : même règle que le transcoder), fait choisir le
// dossier de sortie parmi ceux qui existent (ou un nouveau sous-dossier), puis soumet un job PAR
// source à la ferme (/api/transcode-farm/jobs, `sources`).
//
//   TranscodeDialog.open({
//     sources:  [{kind:'asset', asset_id, label} | {kind:'local', node_id, path, label} | {kind:'share', grant_id, path, label}],
//     projectId: null | n,              // sources de fichiers : le projet dont on parcourt le dossier
//     browse:   {kind, node_id | grant_id} | {asset_id},   // où choisir la destination (sinon pas de choix)
//     title:    'texte',                // facultatif (« Transcoder le contenu de « Bin » »)
//     binId:    n | null,               // bin proposé d'office (celui d'où l'on lance)
//     onDone:   ids => …,               // après un envoi accepté
//   })
//
// La sortie devient une NOUVELLE FICHE reliée à l'original (défaut, décision du 2026-10-29) ou une VERSION
// de la même fiche (`params.as`). Destination = un BIN (on range la fiche produite quand le job est fini
// et le fichier indexé) ; le DOSSIER sur le disque n'est qu'un réglage avancé (à côté de la source).
(function () {
  'use strict';
  const T = (k, fb) => { const v = window.t ? window.t(k) : k; return (v && v !== k) ? v : (fb !== undefined ? fb : k); };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const toast = (m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m));
  const PREVIEW_MAX = 40;

  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch (e) { /* corps vide */ }
    if (!r.ok && !(d && d.ids)) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  let presetsCache = null;
  async function presets() {
    if (!presetsCache) presetsCache = await api('GET', '/api/transcode-farm/presets');
    return presetsCache;
  }

  const PRIOS = () => [['urgent', T('section.farm.prio_urgent', 'Urgente')], ['high', T('section.farm.prio_high', 'Haute')],
    ['normal', T('section.farm.prio_normal', 'Normale')], ['low', T('section.farm.prio_low', 'Basse')]];
  const LAYOUTS = () => ({ auto: T('section.farm.layout_auto', 'Selon le preset'), tracks: T('section.farm.layout_tracks', 'Pistes telles quelles'),
    mono: T('section.farm.layout_mono', 'Une piste mono par canal'), stereo: T('section.farm.layout_stereo', 'Programme stéréo'),
    poly: T('section.farm.layout_poly', 'Une piste polyphonique'), none: T('section.farm.layout_none', 'Sans audio') });

  const CSS = `
.tcd { max-width: 760px; display: flex; flex-direction: column; gap: 12px; }
.tcd h3 { margin: 0; font-size: 1.05em; }
.tcd .tcd-muted { color: var(--text-muted); font-size: 0.86em; }
.tcd .tcd-err { color: var(--status-stopped-fg); }
.tcd-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px 12px; align-items: end; }
.tcd label { display: flex; flex-direction: column; gap: 3px; font-size: 0.85em; color: var(--text-muted); }
.tcd label.tcd-check { flex-direction: row; align-items: center; gap: 6px; color: var(--text); }
.tcd-dest { border: 1px solid var(--border-soft); border-radius: 6px; padding: 8px 12px; display: flex; flex-direction: column; gap: 6px; }
.tcd-dest legend { font-size: 0.85em; color: var(--text-muted); padding: 0 4px; }
.tcd-browser { border: 1px solid var(--border-soft); border-radius: 4px; font-size: 0.88em; }
.tcd-crumbs { padding: 4px 8px; border-bottom: 1px solid var(--border-soft); color: var(--text-muted); }
.tcd-crumbs a { cursor: pointer; }
.tcd-dirs { max-height: 170px; overflow: auto; }
.tcd-dir { padding: 4px 8px; cursor: pointer; min-height: 28px; display: flex; align-items: center; }
.tcd-dir:hover { background: var(--bg-hover, rgba(127,127,127,0.12)); }
.tcd-newdir { display: flex; gap: 6px; padding: 6px 8px; border-top: 1px solid var(--border-soft); }
.tcd-newdir input { flex: 1; min-width: 0; }
.tcd-chosen { padding: 4px 8px; border-top: 1px solid var(--border-soft); font-weight: 500; }
.tcd-prev-head { font-size: 0.85em; color: var(--text-muted); }
.tcd-preview { margin: 0; padding: 6px 10px; list-style: none; border: 1px solid var(--border-soft); border-radius: 4px;
  max-height: 200px; overflow: auto; font-family: var(--font-mono); font-size: 0.84em; }
.tcd-preview li { padding: 2px 0; overflow-wrap: anywhere; }
.tcd-dir-part { color: var(--text-muted); }
.tcd-badge { font-family: var(--font-base); font-size: 0.85em; border: 1px solid var(--border-soft); border-radius: 10px; padding: 0 7px; margin-left: 6px; }
.tcd-warn { color: var(--status-stopped-fg); font-size: 0.88em; }
.tcd-warn ul { margin: 4px 0 0; padding-left: 18px; }
.tcd-actions { display: flex; justify-content: flex-end; gap: 8px; }
.tcd-adv summary { cursor: pointer; font-size: 0.85em; color: var(--text-muted); padding: 4px 0; min-height: 28px; }
.tcd-adv[open] summary { margin-bottom: 6px; }
@media (max-width: 560px) { .tcd { padding: 14px; } .tcd-grid { grid-template-columns: 1fr; } }`;
  function injectCss() {
    if (document.getElementById('tcd-css')) return;
    const st = document.createElement('style'); st.id = 'tcd-css'; st.textContent = CSS;
    document.head.appendChild(st);
  }

  function open(opts) {
    injectCss();
    const sources = (opts.sources || []).filter(Boolean);
    if (!sources.length) { toast(T('section.farm.dlg.none', 'Aucun média à transcoder.'), 'warning'); return; }
    const S = { dest: 'source', path: '', previewSeq: 0, P: null, busy: false };
    const isAsset = sources.every(s => s.kind === 'asset');
    const n = sources.length;
    const ov = document.createElement('div');
    ov.className = 'modal-overlay tcd-overlay';
    ov.innerHTML = `<div class="modal-card tcd" role="dialog" aria-modal="true" aria-labelledby="tcd-title">
      <div class="modal-head"><h3 id="tcd-title">${esc(opts.title || (n === 1
        ? T('section.farm.dlg.title_one', 'Transcoder « {n} »').replace('{n}', sources[0].label || sources[0].path || '')
        : T('section.farm.dlg.title_many', 'Transcoder {n} médias').replace('{n}', n)))}</h3>
        <button type="button" class="btn tcd-x" aria-label="${esc(T('section.farm.dlg.close', 'Fermer'))}">✕</button></div>
      <div class="tcd-grid">
        <label>${esc(T('section.farm.operation', 'Opération'))}<select class="tcd-op">
          <option value="video">${esc(T('section.farm.op_video', 'Vidéo → Vidéo'))}</option>
          <option value="audio">${esc(T('section.farm.op_audio', 'Audio → Audio'))}</option>
          <option value="extract">${esc(T('section.farm.op_extract', 'Extraire audio'))}</option></select></label>
        <label>${esc(T('section.farm.preset', 'Preset'))}<select class="tcd-preset"><option>${esc(T('section.loading', '…'))}</option></select></label>
        <label class="tcd-video">${esc(T('section.farm.cadence', 'Cadence'))}<select class="tcd-cadence"></select></label>
        <label>${esc(T('section.farm.audio', 'Audio'))}<select class="tcd-layout"></select></label>
        <label>${esc(T('section.farm.priority', 'Priorité'))}<select class="tcd-prio">${PRIOS().map(([k, l]) => `<option value="${k}"${k === 'normal' ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
        <label class="tcd-check"><input type="checkbox" class="tcd-loud"> ${esc(T('section.farm.loudness', 'Normaliser EBU R128'))}</label>
      </div>
      <fieldset class="tcd-dest"><legend>${esc(T('section.farm.dlg.as', 'La sortie devient'))}</legend>
        <label class="tcd-check"><input type="radio" name="tcd-as" value="asset" checked> ${esc(T('section.farm.dlg.as_asset', 'Une nouvelle fiche, reliée à l\'original'))}</label>
        <label class="tcd-check"><input type="radio" name="tcd-as" value="version"> ${esc(T('section.farm.dlg.as_version', 'Une version du même média'))}</label>
        <label>${esc(T('section.farm.dlg.bin', 'Ranger dans le bin'))}<select class="tcd-bin"><option value="">${esc(T('section.farm.dlg.bin_none', 'Aucun'))}</option></select></label>
        <div class="tcd-bin-note tcd-muted"></div>
      </fieldset>
      <details class="tcd-adv"><summary>${esc(T('section.farm.dlg.disk', 'Emplacement sur le disque (avancé)'))}</summary>
      <fieldset class="tcd-dest"><legend>${esc(T('section.farm.dlg.dest', 'Dossier de sortie'))}</legend>
        <label class="tcd-check"><input type="radio" name="tcd-dest" value="source" checked> ${esc(isAsset
          ? T('section.farm.dlg.dest_source', 'À côté de la source')
          : T('section.farm.dlg.dest_root', 'Racine du volume (ou du projet)'))}</label>
        <label class="tcd-check"><input type="radio" name="tcd-dest" value="pick"${opts.browse ? '' : ' disabled'}> ${esc(T('section.farm.dlg.dest_pick', 'Choisir un dossier'))}</label>
        <div class="tcd-browser" hidden>
          <div class="tcd-crumbs"></div><div class="tcd-dirs"></div>
          <div class="tcd-newdir"><input type="text" class="tcd-newname" placeholder="${esc(T('section.farm.dlg.new_dir_ph', 'Nouveau sous-dossier'))}">
            <button type="button" class="btn tcd-mk">${esc(T('section.farm.dlg.new_dir', 'Créer ici'))}</button></div>
          <div class="tcd-chosen"></div>
        </div>
        <div class="tcd-multinode tcd-muted" hidden>${esc(T('section.farm.dlg.multinode', 'Ces médias sont sur plusieurs nœuds : le dossier choisi est pris sur le volume de chacun.'))}</div>
      </fieldset></details>
      <div class="tcd-prev-head">${esc(T('section.farm.dlg.preview', 'Fichiers produits'))}</div>
      <ul class="tcd-preview"></ul>
      <div class="tcd-note tcd-muted"></div>
      <div class="tcd-warn" hidden></div>
      <div class="tcd-actions"><button type="button" class="btn tcd-cancel">${esc(T('section.farm.dlg.cancel', 'Annuler'))}</button>
        <button type="button" class="btn btn-blue tcd-go" disabled>${esc(n === 1 ? T('section.farm.dlg.go_one', 'Lancer')
          : T('section.farm.dlg.go_many', 'Lancer les {n} travaux').replace('{n}', n))}</button></div>
    </div>`;
    document.body.appendChild(ov);
    const $ = sel => ov.querySelector(sel);
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    ov.addEventListener('mousedown', e => { if (e.target === ov) close(); });
    $('.tcd-x').onclick = close; $('.tcd-cancel').onclick = close;

    // ── Réglages ──
    function fillPresets() {
      const op = $('.tcd-op').value, sel = $('.tcd-preset'), P = S.P;
      sel.innerHTML = '';
      if (op === 'video') {
        const fams = {};
        (P.video || []).forEach(p => { (fams[p.family] = fams[p.family] || []).push(p); });
        Object.entries(fams).forEach(([f, list]) => {
          const g = document.createElement('optgroup'); g.label = f;
          list.forEach(p => g.appendChild(new Option(p.label, p.id)));
          sel.appendChild(g);
        });
      } else (P.audio || []).forEach(p => sel.appendChild(new Option(p.label, p.id)));
      ov.querySelectorAll('.tcd-video').forEach(x => { x.hidden = op !== 'video'; });
    }
    function preset() {
      const op = $('.tcd-op').value, id = $('.tcd-preset').value;
      return ((op === 'video' ? S.P.video : S.P.audio) || []).find(p => p.id === id) || null;
    }
    function params() {
      const op = $('.tcd-op').value, p = { audio: $('.tcd-layout').value };
      if (op === 'video') { p.preset = $('.tcd-preset').value; if ($('.tcd-cadence').value) p.cadence = $('.tcd-cadence').value; }
      else p.audio_preset = $('.tcd-preset').value;
      if ($('.tcd-loud').checked) p.loudness = true;
      p.as = asMode();
      return p;
    }
    const asMode = () => (ov.querySelector('input[name="tcd-as"]:checked') || {}).value || 'asset';
    ov.querySelectorAll('input[name="tcd-as"]').forEach(r => r.onchange = () => { binNote(); preview(); });
    const binId = () => { const v = $('.tcd-bin').value; return v ? Number(v) : null; };
    function binNote() {
      const b = binId();
      $('.tcd-bin-note').textContent = !b ? '' : asMode() === 'asset'
        ? T('section.farm.dlg.bin_note_asset', 'La nouvelle fiche y sera rangée dès que le fichier sera prêt.')
        : T('section.farm.dlg.bin_note_version', 'La fiche du média (avec sa nouvelle version) y sera rangée dès que le fichier sera prêt.');
    }
    $('.tcd-bin').onchange = binNote;
    async function loadBins() {
      let bins = [];
      try { bins = ((await api('GET', '/api/media/bins')).bins) || []; } catch (e) { /* sans bins : aucun */ }
      // On ne range que dans un bin MANUEL, et, partagé, en « Modifier ».
      const ok = bins.filter(b => !b.smart && (!b.shared || b.shared.level === 'edit'));
      const byId = new Map(ok.map(b => [b.id, b]));
      const path = b => { const out = []; for (let x = b; x; x = byId.get(x.parent_id)) out.unshift(x.name); return out.join(' / '); };
      ok.map(b => [path(b), b.id]).sort((a, b) => a[0].localeCompare(b[0])).forEach(([l, id]) => $('.tcd-bin').appendChild(new Option(l, id)));
      if (opts.binId != null && byId.has(Number(opts.binId))) $('.tcd-bin').value = String(opts.binId);
      binNote();
    }
    const outDir = () => S.dest === 'pick' ? S.path : '';

    // ── Destination ──
    async function browse() {
      const b = opts.browse; if (!b) return;
      const q = new URLSearchParams({ path: S.path, dirs: '1' });
      if (b.asset_id) q.set('asset_id', b.asset_id);
      else { q.set('kind', b.kind); q.set(b.kind === 'share' ? 'grant_id' : 'node_id', b.grant_id || b.node_id);
        if (opts.projectId != null) q.set('project_id', opts.projectId); }
      const parts = S.path ? S.path.split('/') : [];
      $('.tcd-crumbs').innerHTML = `<a data-path="">/</a>` + parts.map((p, i) => ` / <a data-path="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`).join('');
      $('.tcd-chosen').textContent = T('section.farm.dlg.chosen', 'Sortie dans : {p}').replace('{p}', '/' + S.path);
      let d;
      try { d = await api('GET', '/api/transcode-farm/browse?' + q); }
      catch (e) {
        // Dossier pas encore créé (nouveau sous-dossier) : il le sera au rapatriement.
        $('.tcd-dirs').innerHTML = `<div class="tcd-dir tcd-muted">${esc(/absent/.test(e.message)
          ? T('section.farm.dlg.dir_new', 'Nouveau dossier : il sera créé avec la première sortie.') : e.message)}</div>`;
        return;
      }
      $('.tcd-dirs').innerHTML = (d.items || []).map(it => {
        const p = (S.path ? S.path + '/' : '') + it.name;
        return `<div class="tcd-dir" data-path="${esc(p)}">📁 ${esc(it.name)}</div>`;
      }).join('') || `<div class="tcd-dir tcd-muted">${esc(T('section.farm.dlg.no_subdir', 'Aucun sous-dossier'))}</div>`;
    }
    const go = p => { S.path = p; browse(); preview(); };
    $('.tcd-crumbs').onclick = e => { const a = e.target.closest('a[data-path]'); if (a) go(a.dataset.path); };
    $('.tcd-dirs').onclick = e => { const it = e.target.closest('.tcd-dir[data-path]'); if (it) go(it.dataset.path); };
    $('.tcd-mk').onclick = () => {
      const nm = $('.tcd-newname').value.trim().replace(/^\/+|\/+$/g, '');
      if (!nm || nm.split('/').some(x => !x || x === '..' || x.startsWith('.'))) {
        toast(T('section.farm.dlg.bad_dir', 'Nom de dossier invalide.'), 'warning'); return; }
      $('.tcd-newname').value = ''; go((S.path ? S.path + '/' : '') + nm);
    };
    ov.querySelectorAll('input[name="tcd-dest"]').forEach(r => r.onchange = () => {
      S.dest = ov.querySelector('input[name="tcd-dest"]:checked').value;
      $('.tcd-browser').hidden = S.dest !== 'pick';
      if (S.dest === 'pick') browse();
      preview();
    });

    // ── Aperçu des noms ──
    let pt = null;
    function preview() { clearTimeout(pt); pt = setTimeout(doPreview, 150); }
    async function doPreview() {
      const p = preset(); if (!p) return;
      const seq = ++S.previewSeq;
      let d;
      try {
        d = await api('POST', '/api/transcode-farm/preview', { sources: sources.slice(0, PREVIEW_MAX), project_id: opts.projectId ?? null,
          version: p.version, ext: p.ext, output_dir: outDir(), as: asMode() });
      } catch (e) { if (seq === S.previewSeq) { $('.tcd-preview').innerHTML = `<li class="tcd-err">${esc(e.message)}</li>`; } return; }
      if (seq !== S.previewSeq) return;
      const items = d.items || [];
      const nodes = new Set(items.filter(x => x.node_id != null).map(x => x.node_id));
      $('.tcd-multinode').hidden = !(S.dest === 'pick' && nodes.size > 1);
      $('.tcd-preview').innerHTML = items.map((x, i) => x.error
        ? `<li><span class="tcd-err">${esc(sources[i].label || sources[i].path || '')} : ${esc(x.error)}</span></li>`
        : `<li><span class="tcd-dir-part">${esc((x.node ? x.node + ' : ' : '') + (x.dir ? x.dir + '/' : '/'))}</span><strong>${esc(x.name)}</strong>`
          + (x.replaces ? (asMode() === 'asset'
            ? ` <span class="tcd-badge" title="${esc(T('section.farm.dlg.replaces_asset_tip', 'Cette fiche a déjà été produite par le même transcodage : son fichier sera remplacé, jamais doublé.'))}">${esc(T('section.farm.dlg.replaces_asset', 'remplace la fiche existante'))}</span>`
            : ` <span class="tcd-badge" title="${esc(T('section.farm.dlg.replaces_tip', 'Ce média a déjà cette version : elle sera remplacée, jamais doublée.'))}">${esc(T('section.farm.dlg.replaces', 'remplace la version existante'))}</span>`) : '')
          + `</li>`).join('')
        + (n > PREVIEW_MAX ? `<li class="tcd-muted">${esc(T('section.farm.dlg.more', '… et {n} autres, nommés de la même façon').replace('{n}', n - PREVIEW_MAX))}</li>` : '');
      const notes = [asMode() === 'asset'
        ? T('section.farm.dlg.asset_note', 'Nouvelle fiche, reliée à son original : son propre code, la clé « {v} » dans le nom.').replace('{v}', p.version)
        : T('section.farm.dlg.version_note', 'Version « {v} » : le nom garde le code du média et ajoute la clé de version.').replace('{v}', p.version)];
      if (items.some(x => !x.error && !x.code_known))
        notes.push(T('section.farm.dlg.code_note', '{c} : code du média, attribué au lancement.').replace('{c}', d.code_placeholder || '······'));
      $('.tcd-note').textContent = notes.join(' ');
    }

    // ── Envoi ──
    $('.tcd-go').onclick = async () => {
      if (S.busy) return;
      S.busy = true; $('.tcd-go').disabled = true;
      try {
        const r = await api('POST', '/api/transcode-farm/jobs', { sources, project_id: opts.projectId ?? null,
          type: $('.tcd-op').value, params: params(), priority: $('.tcd-prio').value, output_dir: outDir(), bin_id: binId() });
        const ids = r.ids || [], errs = r.errors || [];
        if (ids.length) toast(T('section.farm.dlg.sent', '{n} travail(s) ajouté(s) à la ferme').replace('{n}', ids.length), 'info');
        if (errs.length) {
          $('.tcd-warn').hidden = false;
          $('.tcd-warn').innerHTML = esc(T('section.farm.dlg.some_failed', '{n} non lancé(s) :').replace('{n}', errs.length)) + '<ul>'
            + errs.slice(0, 20).map(e => `<li>${esc(sources[e.index].label || sources[e.index].path || '')} : ${esc(e.reason === 'shared_read_only'
              ? T('section.farm.dlg.read_only', 'partagé en lecture seule') : e.error)}</li>`).join('') + '</ul>';
          S.busy = false;
          if (ids.length && opts.onDone) opts.onDone(ids);
          return;
        }
        if (opts.onDone) opts.onDone(ids);
        close();
      } catch (e) { toast(e.message, 'error'); S.busy = false; $('.tcd-go').disabled = false; }
    };

    // ── Chargement ──
    (async () => {
      try { S.P = await presets(); }
      catch (e) { $('.tcd-preset').innerHTML = `<option>${esc(T('section.farm.no_presets', 'presets indisponibles'))}</option>`;
        $('.tcd-warn').hidden = false; $('.tcd-warn').textContent = e.message; return; }
      const cad = $('.tcd-cadence');
      cad.appendChild(new Option(T('section.farm.as_source', 'Comme la source'), ''));
      (S.P.cadences || []).forEach(c => cad.appendChild(new Option(c, c)));
      const L = LAYOUTS();
      (S.P.audio_layouts || []).forEach(l => $('.tcd-layout').appendChild(new Option(L[l] || l, l)));
      fillPresets();
      $('.tcd-op').onchange = () => { fillPresets(); preview(); };
      ['.tcd-preset'].forEach(s => { $(s).onchange = preview; });
      $('.tcd-go').disabled = false;
      preview();
      loadBins();
      // Aucun transcoder prêt dans la ferme : les travaux attendront — le dire AVANT de lancer.
      try {
        const st = await api('GET', '/api/transcode-farm/state');
        if (st.global && !(st.members || []).some(m => m.member && m.reachable && m.farm_capable)) {
          $('.tcd-warn').hidden = false;
          $('.tcd-warn').textContent = T('section.farm.dlg.no_member', 'Aucun transcoder inscrit et prêt dans la ferme : les travaux attendront qu\'il y en ait un.');
        }
      } catch (e) { /* l'état de la ferme est une aide, pas une condition */ }
    })();
    setTimeout(() => $('.tcd-op').focus(), 0);
  }

  // Médias d'un BIN (sous-bins compris si demandé) → sources de la fenêtre.
  async function binSources(binId, children) {
    const r = await fetch(`/api/media/bins/${binId}/assets`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ children: !!children }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return (d.assets || []).map(a => ({ kind: 'asset', asset_id: a.id, label: a.name || a.title || a.id }));
  }

  window.TranscodeDialog = { open, binSources };
})();
