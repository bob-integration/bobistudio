// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Ferme de transcodage — page Médias, mode « Ferme ». Cf. app/transcode_farm.py.
(function () {
  'use strict';
  const T = (k, fb) => { const v = window.t ? window.t(k) : k; return (v && v !== k) ? v : (fb !== undefined ? fb : k); };
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const toast = (m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m));

  const PRIOS = [['urgent', 20], ['high', 10], ['normal', 0], ['low', -10]];
  const PRIO_LABEL = n => ({ 20: T('section.farm.prio_urgent', 'Urgente'), 10: T('section.farm.prio_high', 'Haute'),
    0: T('section.farm.prio_normal', 'Normale'), '-10': T('section.farm.prio_low', 'Basse') })[n] || n;
  const STATUS = s => ({
    pending: T('section.farm.st_pending', 'En attente'), transfer_in: T('section.farm.st_transfer_in', 'Copie de la source'),
    running: T('section.farm.st_running', 'En cours'), transfer_out: T('section.farm.st_transfer_out', 'Rapatriement'),
    done: T('section.farm.st_done', 'Terminé'), failed: T('section.farm.st_failed', 'Échec'),
    cancelled: T('section.farm.st_cancelled', 'Annulé') })[s] || s;
  const ACCESS = a => ({
    direct: T('section.farm.access_direct', 'lecture directe'),
    stream: T('section.farm.access_stream', 'lecture à distance (sans copie)'),
    copy: T('section.farm.access_copy', 'source copiée vers le transcoder') })[a] || '';
  const OPS = { video: T('section.farm.op_video', 'Vidéo → Vidéo'), audio: T('section.farm.op_audio', 'Audio → Audio'),
    extract: T('section.farm.op_extract', 'Extraire audio'), slate: 'Audio → Vidéo',
    storyboard: T('section.farm.op_storyboard', 'Planche d\'images') };
  const LAYOUTS = { auto: T('section.farm.layout_auto', 'Selon le preset'), tracks: T('section.farm.layout_tracks', 'Pistes telles quelles'),
    mono: T('section.farm.layout_mono', 'Une piste mono par canal'), stereo: T('section.farm.layout_stereo', 'Programme stéréo'),
    poly: T('section.farm.layout_poly', 'Une piste polyphonique'), none: T('section.farm.layout_none', 'Sans audio') };
  const ACTIVE = ['pending', 'transfer_in', 'running', 'transfer_out'];

  const S = { state: null, presets: null, sources: null, src: null, path: '', picked: null,
    filter: 'all', q: '', open: null, timer: null, alive: false, wired: false };

  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch (e) { /* corps vide */ }
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }
  const fmtSize = n => n >= 1e9 ? (n / 1e9).toFixed(2) + ' Go' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' Mo' : n >= 1e3 ? Math.round(n / 1e3) + ' ko' : (n || 0) + ' o';
  const fmtTime = s => s ? s.replace('T', ' ').slice(0, 16) : '';
  function fmtDur(sec) {
    if (sec == null || !isFinite(sec)) return '';
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  }

  // ─── Membres ───────────────────────────────────────────────────────────────
  function projectName(pid) {
    if (pid == null) return T('section.farm.no_project', 'Hors projet');
    const p = (S.state.projects || []).find(x => x.id === pid);
    return p ? p.name : '#' + pid;
  }
  function renderMembers() {
    const ms = S.state.members || [];
    if (!ms.length) {
      $('farm-members').innerHTML = `<tr><td colspan="7" class="farm-muted">${esc(T('section.farm.no_transcoder', 'Aucun transcoder déployé.'))}</td></tr>`;
      return;
    }
    const can = S.state.can_manage_members;
    $('farm-members').innerHTML = ms.map(m => {
      let state;
      if (!m.reachable) state = `<span class="farm-warn">${esc(T('section.farm.unreachable', 'injoignable'))}${m.error ? ' (' + esc(m.error) + ')' : ''}</span>`;
      else if (!m.farm_capable) state = `<span class="farm-warn">${esc(T('section.farm.not_capable', 'à redéployer (transcoder 0.9.2 avec jeton requis)'))}</span>`;
      else state = `<span class="farm-ok">${esc(T('section.farm.ready', 'prêt'))}</span> <span class="farm-muted">${esc(m.version || '')}</span>`;
      const slots = m.reachable ? `${m.running || 0}/${m.max_concurrent || 1}` + (m.pending ? ` +${m.pending}` : '')
        + (m.farm_jobs ? ` · ${esc(T('section.farm.farm_jobs', '{n} de la ferme').replace('{n}', m.farm_jobs))}` : '') : '';
      const cores = m.usable_cpus ? T('section.farm.cores_v', '{c} · {t}/job').replace('{c}', m.usable_cpus).replace('{t}', m.threads_per_job) : '';
      return `<tr>
        <td><input type="checkbox" data-member="${m.vmid}"${m.member ? ' checked' : ''}${can ? '' : ' disabled'}></td>
        <td>${esc(m.hostname)} <span class="farm-muted">#${m.vmid}</span></td>
        <td>${esc(m.node || '')}</td><td>${esc(projectName(m.project_id))}</td>
        <td>${state}</td><td>${slots}</td><td class="farm-muted">${esc(cores)}</td></tr>`;
    }).join('');
  }

  // ─── Nouveau job ───────────────────────────────────────────────────────────
  function fillStatic() {
    const pj = $('farm-project');
    const keep = pj.value;
    pj.innerHTML = '';
    if (S.state.global) pj.appendChild(new Option(T('section.farm.no_project', 'Hors projet'), ''));
    (S.state.projects || []).forEach(p => pj.appendChild(new Option(p.name, p.id)));
    if (keep !== '' && [...pj.options].some(o => o.value === keep)) pj.value = keep;
    const pr = $('farm-priority');
    if (!pr.options.length) {
      PRIOS.forEach(([k, n]) => pr.appendChild(new Option(PRIO_LABEL(n), k)));
      pr.value = 'normal';
    }
  }
  function fillPresets() {
    const op = $('farm-op').value, sel = $('farm-preset'), P = S.presets;
    sel.innerHTML = '';
    if (!P) { sel.appendChild(new Option(T('section.farm.no_presets', 'presets indisponibles'), '')); return; }
    if (op === 'video') {
      const fams = {};
      (P.video || []).forEach(p => { (fams[p.family] = fams[p.family] || []).push(p); });
      Object.entries(fams).forEach(([f, list]) => {
        const g = document.createElement('optgroup'); g.label = f;
        list.forEach(p => g.appendChild(new Option(p.label, p.id)));
        sel.appendChild(g);
      });
    } else {
      (P.audio || []).forEach(p => sel.appendChild(new Option(p.label, p.id)));
    }
    document.querySelectorAll('#farm-embed .farm-video-only').forEach(n => { n.hidden = op !== 'video'; });
    const cad = $('farm-cadence');
    if (!cad.options.length) {
      cad.appendChild(new Option(T('section.farm.as_source', 'Comme la source'), ''));
      (P.cadences || []).forEach(c => cad.appendChild(new Option(c, c)));
    }
    const lay = $('farm-layout');
    if (!lay.options.length) (P.audio_layouts || []).forEach(l => lay.appendChild(new Option(LAYOUTS[l] || l, l)));
  }
  async function loadSources() {
    const pid = $('farm-project').value;
    const src = $('farm-source');
    src.innerHTML = '';
    try {
      S.sources = await api('GET', '/api/transcode-farm/sources?project_id=' + encodeURIComponent(pid));
    } catch (e) { S.sources = { shares: [], nodes: [] }; toast(e.message, 'error'); }
    const g1 = document.createElement('optgroup'); g1.label = T('section.farm.shares', 'Dossiers réseau');
    (S.sources.shares || []).forEach(s => g1.appendChild(new Option(s.name + (s.mounted ? '' : ' — ' + T('section.farm.not_mounted', 'non monté')), 'share:' + s.id)));
    const g2 = document.createElement('optgroup'); g2.label = T('section.farm.node_volumes', 'Volume local d\'un nœud');
    (S.sources.nodes || []).forEach(n => g2.appendChild(new Option(n.name, 'local:' + n.id)));
    if (g1.children.length) src.appendChild(g1);
    if (g2.children.length) src.appendChild(g2);
    S.path = ''; S.picked = null;
    await browse();
  }
  async function browse() {
    const v = $('farm-source').value;
    renderPicked();
    if (!v) { $('farm-items').innerHTML = ''; $('farm-crumbs').textContent = ''; return; }
    const [kind, id] = v.split(':');
    const q = new URLSearchParams({ kind, project_id: $('farm-project').value, path: S.path });
    q.set(kind === 'share' ? 'grant_id' : 'node_id', id);
    let d;
    try { d = await api('GET', '/api/transcode-farm/browse?' + q); }
    catch (e) { $('farm-items').innerHTML = `<div class="farm-item farm-warn">${esc(e.message)}</div>`; return; }
    const parts = S.path ? S.path.split('/') : [];
    $('farm-crumbs').innerHTML = `<a data-path="">/</a>` + parts.map((p, i) => ` / <a data-path="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`).join('');
    $('farm-items').innerHTML = (d.items || []).map(it => {
      const p = (S.path ? S.path + '/' : '') + it.name;
      const sel = S.picked && S.picked.path === p ? ' sel' : '';
      return `<div class="farm-item${sel}" data-${it.type}="${esc(p)}">${it.type === 'dir' ? '📁' : '🎞'} <span>${esc(it.name)}</span>`
        + (it.type === 'file' ? `<span class="farm-size">${fmtSize(it.size)}</span>` : '') + `</div>`;
    }).join('') || `<div class="farm-item farm-muted">${esc(T('section.farm.empty_dir', 'Dossier vide'))}</div>`;
  }
  function renderPicked() {
    $('farm-picked').innerHTML = S.picked
      ? esc(T('section.farm.picked', 'Source : {p}').replace('{p}', S.picked.path))
      : `<span class="farm-muted">${esc(T('section.farm.pick_hint', 'Choisissez un fichier dans la liste.'))}</span>`;
  }
  async function submit() {
    if (!S.picked) { toast(T('section.farm.pick_hint', 'Choisissez un fichier dans la liste.'), 'warning'); return; }
    const op = $('farm-op').value, preset = $('farm-preset').value;
    const params = { audio: $('farm-layout').value };
    if (op === 'video') { params.preset = preset; if ($('farm-cadence').value) params.cadence = $('farm-cadence').value; }
    else params.audio_preset = preset;
    if ($('farm-loudness').checked) params.loudness = true;
    const pid = $('farm-project').value;
    try {
      const r = await api('POST', '/api/transcode-farm/jobs', {
        project_id: pid === '' ? null : Number(pid), source: S.picked.source, type: op, params,
        priority: $('farm-priority').value, output_dir: $('farm-outdir').value.trim() });
      toast(T('section.farm.submitted', 'Job n° {n} ajouté à la ferme').replace('{n}', r.id), 'info');
      loadJobs();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ─── File ──────────────────────────────────────────────────────────────────
  function renderChips(counts) {
    const n = { all: Object.values(counts).reduce((a, b) => a + b, 0),
      active: ACTIVE.reduce((a, s) => a + (counts[s] || 0), 0), pending: counts.pending || 0,
      done: counts.done || 0, failed: counts.failed || 0, cancelled: counts.cancelled || 0 };
    const lab = { all: T('section.farm.f_all', 'Tous'), active: T('section.farm.f_active', 'Actifs'),
      pending: T('section.farm.f_pending', 'En attente'),
      done: T('section.farm.st_done', 'Terminé'), failed: T('section.farm.st_failed', 'Échec'), cancelled: T('section.farm.st_cancelled', 'Annulé') };
    $('farm-chips').innerHTML = Object.keys(lab).map(k =>
      `<button type="button" class="farm-chip${S.filter === k ? ' active' : ''}" data-f="${k}">${esc(lab[k])}${n[k] ? ' ' + n[k] : ''}</button>`).join(' ');
  }
  function where(j) {
    if (j.out_path) return `${esc(j.out_node || '')} : ${esc(j.out_path)}`;
    if (j.worker) return `→ ${esc(j.worker)} <span class="farm-muted">(${esc(j.worker_node || '')})</span>`;
    return '';
  }
  function progress(j) {
    // `no_transcoder` : aucun transcoder inscrit et joignable pour ce projet — le job attendrait sans
    // fin ; cela doit se lire ici, pas passer pour un simple rang dans la file.
    if (j.status === 'pending' && j.phase === 'no_transcoder')
      return `<span class="farm-warn">${esc(T('section.farm.ph_no_transcoder', 'en attente d\'un transcoder (aucun inscrit)'))}</span>`;
    if (j.status === 'pending') return `<span class="farm-muted">${esc(T('section.farm.queue_pos', 'n° {n}').replace('{n}', j.queue_pos || '?'))}</span>`;
    if (j.status === 'transfer_in' || j.status === 'transfer_out' || (j.status === 'running' && j.phase === 'queued'))
      return `<span class="farm-muted">${esc(j.phase === 'queued' ? T('section.farm.ph_queued', 'dans la file du transcoder') : STATUS(j.status))}</span>`;
    if (j.status !== 'running') return '';
    const p = Math.round((j.progress || 0) * 100);
    return `<div class="farm-bar"><span style="width:${p}%"></span></div><span class="farm-muted">${p} %</span>`;
  }
  function actions(j) {
    const b = [];
    if (ACTIVE.includes(j.status)) b.push(`<button class="btn" data-act="cancel" data-id="${j.id}">${esc(T('section.farm.cancel', 'Annuler'))}</button>`);
    else b.push(`<button class="btn" data-act="retry" data-id="${j.id}">${esc(T('section.farm.retry', 'Relancer'))}</button>`);
    return `<div class="farm-actions">${b.join('')}</div>`;
  }
  function prioCell(j) {
    if (j.status !== 'pending') return esc(PRIO_LABEL(j.priority));
    return `<select data-prio="${j.id}">` + PRIOS.map(([k, n]) => `<option value="${k}"${n === j.priority ? ' selected' : ''}>${esc(PRIO_LABEL(n))}</option>`).join('') + `</select>`;
  }
  function jobLabel(j) {
    const p = j.params || {};
    return `${esc(OPS[j.job_type] || j.job_type)} <span class="farm-muted">${esc(p.preset || p.audio_preset || '')}${p.cadence ? ' · ' + esc(p.cadence) : ''}</span>`;
  }
  function renderJobs(d) {
    renderChips(d.counts || {});
    const jobs = d.jobs || [];
    $('farm-empty').hidden = jobs.length > 0;
    $('farm-jobs').innerHTML = jobs.map(j => {
      const row = `<tr class="farm-row" data-row="${j.id}">
        <td>${j.id}</td>
        <td>${esc(j.src_path.split('/').pop())}<div class="farm-muted">${esc(j.src_label)}${j.proxy_asset ? ' · ' + esc(T('section.farm.proxy', 'proxy de lecture')) : ''}</div></td>
        <td>${jobLabel(j)}</td><td>${prioCell(j)}</td><td>${progress(j)}</td>
        <td><span class="farm-st" data-s="${esc(j.status)}">${esc(STATUS(j.status))}</span></td>
        <td>${where(j)}</td><td>${actions(j)}</td></tr>`;
      return row + (S.open === j.id ? `<tr class="farm-detail"><td colspan="8" id="farm-detail">${esc(T('section.loading', '…'))}</td></tr>` : '');
    }).join('');
    if (S.open != null) loadDetail(S.open);
  }
  async function loadDetail(id) {
    let j;
    try { j = await api('GET', '/api/transcode-farm/jobs/' + id); } catch (e) { return; }
    const cell = $('farm-detail');
    if (!cell || S.open !== id) return;
    const info = j.worker_info || j.info || {};
    const lv = j.live || {};
    const kv = [
      [T('section.farm.d_source', 'Source'), `${j.src_label} : ${j.src_path}`],
      [T('section.farm.d_project', 'Projet'), projectName(j.project_id)],
      [T('section.farm.d_worker', 'Transcoder'), j.worker ? `${j.worker} (${j.worker_node || ''})` : ''],
      [T('section.farm.d_access', 'Lecture de la source'), ACCESS(j.access)],
      [T('section.farm.d_output', 'Sortie'), j.out_path ? `${j.out_node || ''} : ${j.out_path}` : ''],
      [T('section.farm.d_plan', 'Ce que fait le job'), [info.video, info.audio].filter(Boolean).join(' · ')],
      [T('section.farm.d_loudness', 'EBU R128'), info.loudness ? `${info.loudness.measured_lufs} LUFS → ${info.loudness.gain_db > 0 ? '+' : ''}${info.loudness.gain_db} dB` : ''],
      [T('section.farm.d_notes', 'Écarts'), (info.notes || []).join(' · ')],
      [T('section.farm.d_speed', 'Vitesse'), lv.speed ? '×' + Number(lv.speed).toFixed(2) : ''],
      [T('section.farm.d_eta', 'Reste'), lv.eta_s != null ? fmtDur(lv.eta_s) : ''],
      [T('section.farm.d_created', 'Créé'), `${fmtTime(j.created_at)}${j.created_by ? ' · ' + j.created_by : ''}`],
      [T('section.farm.d_started', 'Confié'), fmtTime(j.started_at)],
      [T('section.farm.d_finished', 'Terminé'), fmtTime(j.completed_at)],
      [T('section.farm.d_attempts', 'Remises en file'), j.attempts || ''],
      [T('section.farm.d_error', 'Erreur'), j.error || ''],
    ].filter(([, v]) => v !== '' && v != null);
    cell.innerHTML = `<div class="farm-kv">${kv.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join('')}</div>`;
  }
  async function loadJobs() {
    const q = new URLSearchParams({ q: S.q });
    if (S.filter !== 'all') q.set('status', S.filter);
    let d;
    try { d = await api('GET', '/api/transcode-farm/jobs?' + q); }
    catch (e) { $('farm-jobs').innerHTML = `<tr><td colspan="8" class="farm-warn">${esc(e.message)}</td></tr>`; return d; }
    renderJobs(d);
    return d;
  }
  async function refresh() {
    if (!S.alive) return;
    try { S.state = await api('GET', '/api/transcode-farm/state'); renderMembers(); } catch (e) { /* affiché par loadJobs */ }
    const d = await loadJobs();
    const busy = d && ACTIVE.some(s => (d.counts || {})[s]);
    clearTimeout(S.timer);
    S.timer = S.alive ? setTimeout(refresh, busy ? 3000 : 10000) : null;
  }

  function wire() {
    if (S.wired) return;
    S.wired = true;
    $('farm-project').addEventListener('change', loadSources);
    $('farm-source').addEventListener('change', () => { S.path = ''; S.picked = null; browse(); });
    $('farm-op').addEventListener('change', fillPresets);
    $('farm-submit').addEventListener('click', submit);
    $('farm-crumbs').addEventListener('click', e => { const a = e.target.closest('a[data-path]'); if (a) { S.path = a.dataset.path; browse(); } });
    $('farm-items').addEventListener('click', e => {
      const it = e.target.closest('.farm-item'); if (!it) return;
      if (it.dataset.dir != null) { S.path = it.dataset.dir; browse(); return; }
      if (it.dataset.file != null) {
        const [kind, id] = $('farm-source').value.split(':');
        S.picked = { path: it.dataset.file, source: kind === 'share'
          ? { kind, grant_id: Number(id), path: it.dataset.file } : { kind, node_id: Number(id), path: it.dataset.file } };
        $('farm-items').querySelectorAll('.farm-item').forEach(n => n.classList.toggle('sel', n === it));
        renderPicked();
      }
    });
    $('farm-members').addEventListener('change', async e => {
      const cb = e.target.closest('input[data-member]'); if (!cb) return;
      try { await api('POST', '/api/transcode-farm/members/' + cb.dataset.member, { enabled: cb.checked }); refresh(); }
      catch (err) { cb.checked = !cb.checked; toast(err.message, 'error'); }
    });
    $('farm-jobs').addEventListener('click', async e => {
      const b = e.target.closest('button[data-act]');
      if (b) {
        e.stopPropagation();
        try { await api('POST', `/api/transcode-farm/jobs/${b.dataset.id}/${b.dataset.act}`); loadJobs(); }
        catch (err) { toast(err.message, 'error'); }
        return;
      }
      if (e.target.closest('select, .farm-detail')) return;
      const tr = e.target.closest('tr[data-row]'); if (!tr) return;
      const id = Number(tr.dataset.row);
      S.open = S.open === id ? null : id;
      loadJobs();
    });
    $('farm-jobs').addEventListener('change', async e => {
      const s = e.target.closest('select[data-prio]'); if (!s) return;
      try { await api('POST', `/api/transcode-farm/jobs/${s.dataset.prio}/priority`, { priority: s.value }); loadJobs(); }
      catch (err) { toast(err.message, 'error'); }
    });
    $('farm-chips').addEventListener('click', e => { const c = e.target.closest('[data-f]'); if (c) { S.filter = c.dataset.f; loadJobs(); } });
    let qt = null;
    $('farm-q').addEventListener('input', e => { clearTimeout(qt); qt = setTimeout(() => { S.q = e.target.value.trim(); loadJobs(); }, 250); });
    $('farm-purge').addEventListener('click', async () => {
      try { const r = await api('POST', '/api/transcode-farm/purge'); toast(T('section.farm.purged', '{n} job(s) retiré(s)').replace('{n}', r.removed), 'info'); loadJobs(); }
      catch (err) { toast(err.message, 'error'); }
    });
  }

  window.farmLoad = async function () {
    wire();
    S.alive = true;
    try { S.state = await api('GET', '/api/transcode-farm/state'); }
    catch (e) { $('farm-members').innerHTML = `<tr><td colspan="7" class="farm-warn">${esc(e.message)}</td></tr>`; return; }
    renderMembers();
    fillStatic();
    try { S.presets = await api('GET', '/api/transcode-farm/presets'); } catch (e) { S.presets = null; }
    fillPresets();
    await loadSources();
    refresh();
  };
  window.farmStop = function () { S.alive = false; clearTimeout(S.timer); S.timer = null; };
})();
