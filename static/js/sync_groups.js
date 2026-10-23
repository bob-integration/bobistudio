// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Lecture synchrone — page Médias, mode « Lecture synchrone ». Cf. app/sync_groups.py.
// Tactile d'abord (iPad) : chaque geste est un bouton ou un appui ; une cellule se vise d'un appui, la
// barre de temps se touche (un appui = aller à cet instant), réordonner = ▲ ▼ ; retirer = deux appuis.
(function () {
  'use strict';
  const T = (k, fb) => { const v = window.t ? window.t(k) : k; return (v && v !== k) ? v : (fb !== undefined ? fb : k); };
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const toast = (m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m));
  const fill = (s, o) => String(s).replace(/\{(\w+)\}/g, (m, k) => (o[k] != null ? o[k] : m));
  const SPEEDS = [0.25, 0.5, 1, 2, 4];
  const LOOPS = ['off', 'line', 'playlist'];

  const S = { data: null, gid: null, timer: null, alive: false, wired: false, ml: null, mlVmid: null,
    sigLines: '', sel: null, dlg: null, addBuf: [], addT: null, addMode: null, busy: false };

  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch (e) { /* corps vide */ }
    if (!r.ok) {
      const err = new Error(d.code ? T('section.syncplay.err.' + d.code, d.error || d.code) : (d.error || ('HTTP ' + r.status)));
      err.code = d.code; throw err;
    }
    return d;
  }
  const group = () => ((S.data && S.data.groups) || []).find(g => g.id === S.gid) || null;
  const canOp = () => !!(S.data && S.data.can_operate);
  const fps = g => (g && g.period_ms ? 1000 / g.period_ms : 25);
  function tc(s, g) {
    if (!(s >= 0)) return '--:--:--:--';
    const f = Math.round(fps(g)), tot = Math.round(s * f);
    const fr = tot % f, x = Math.floor(tot / f), h = Math.floor(x / 3600), m = Math.floor(x / 60) % 60, sec = x % 60;
    return [h, m, sec, fr].map(v => String(v).padStart(2, '0')).join(':');
  }
  function parseTc(v, g) {
    v = String(v || '').trim();
    if (!v) return null;
    if (!v.includes(':')) { const n = parseFloat(v.replace(',', '.')); return isNaN(n) ? null : n; }
    const p = v.split(':').map(x => parseFloat(x.replace(',', '.')));
    if (p.some(isNaN)) return null;
    if (p.length === 4) return p[0] * 3600 + p[1] * 60 + p[2] + p[3] / fps(g);
    if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2];
    if (p.length === 2) return p[0] * 60 + p[1];
    return null;
  }
  const ST = s => T('section.syncplay.st_' + s, s);
  const CS = s => T('section.syncplay.cs_' + s, s);

  // ─── Rendu ─────────────────────────────────────────────────────────────────
  function renderChips() {
    const gs = (S.data && S.data.groups) || [];
    if (S.gid == null || !gs.find(g => g.id === S.gid)) S.gid = gs.length ? gs[0].id : null;
    $('sp-chips').innerHTML = gs.map(g => `<button type="button" class="sp-chip${g.id === S.gid ? ' active' : ''}" data-g="${g.id}">${esc(g.name)}</button>`).join(' ');
    $('sp-empty').hidden = !!gs.length;
    $('sp-group').hidden = !gs.length;
    $('sp-settings').hidden = !gs.length || !canOp();
    $('sp-new').hidden = !canOp();
  }

  function renderStatus(g) {
    const n = g.lines.length, i = g.line_index;
    // Tolérance ZÉRO : un écart d'une image (ou une ancre différente, ou des horloges à une image) est un défaut.
    const bad = !!(g.verdict && g.verdict.defect);
    const skew = g.state === 'stopped' ? '—'
      : fill(T('section.syncplay.skew_val', '{ms} ms ({f} image) ± {u} ms'),
        { ms: g.skew_ms.toFixed(1), f: (g.skew_ms / g.period_ms).toFixed(2), u: g.skew_unc_ms.toFixed(1) });
    const nx = g.next ? (g.next.scheduled
      ? fill(T('section.syncplay.next_in', 'ligne suivante dans {s} s'), { s: g.next.in_s != null ? g.next.in_s.toFixed(1) : '?' })
      : T('section.syncplay.next_preparing', 'ligne suivante en préparation')) : '';
    $('sp-status').innerHTML = `<span><b>${esc(ST(g.ended ? 'ended' : g.state))}</b></span>
      <span>${esc(T('section.syncplay.line', 'Ligne'))} <b>${i >= 0 ? i + 1 : '—'}</b> / ${n}</span>
      <span><b>${esc(tc(g.line_time, g))}</b> / ${esc(g.line_duration ? tc(g.line_duration, g) : '--:--:--:--')}</span>
      <span>${esc(T('section.syncplay.speed', 'Vitesse'))} <b>${g.state === 'playing' ? (g.speed || 1) : (g.speed_play || 1)}×</b></span>
      <span class="sp-skew" data-s="${g.state === 'stopped' ? '' : (bad ? 'bad' : 'ok')}">${esc(T('section.syncplay.skew', 'Écart mesuré entre canaux'))} <b>${esc(skew)}</b></span>
      <span class="sp-lead">${esc(nx)}</span>`;
    const p = (g.line_duration && g.line_time != null) ? Math.min(100, 100 * g.line_time / g.line_duration) : 0;
    $('sp-tl-fill').style.width = p.toFixed(2) + '%';
    $('sp-tl-txt').textContent = (g.lines[i] || {}).label || '';
    $('sp-tl').setAttribute('aria-valuenow', String(Math.round(p)));
    $('sp-speeds').innerHTML = `<span>${esc(T('section.syncplay.speed', 'Vitesse'))}</span>` + SPEEDS.map(s =>
      `<button type="button" class="sp-chip${Math.abs((g.speed_play || 1) - s) < 1e-6 ? ' active' : ''}" data-speed="${s}"${canOp() ? '' : ' disabled'}>${String(s).replace('.', ',')}×</button>`).join('');
    $('sp-loops').innerHTML = `<span>${esc(T('section.syncplay.loop', 'Boucle'))}</span>` + LOOPS.map(l =>
      `<button type="button" class="sp-chip${g.loop_mode === l ? ' active' : ''}" data-loop="${l}"${canOp() ? '' : ' disabled'}>${esc(T('section.syncplay.loop_' + l, l))}</button>`).join('');
    $('sp-transport').querySelectorAll('button').forEach(b => { b.disabled = !canOp() || S.busy; });
  }

  function renderFail(g) {
    const f = g.failure, el = $('sp-fail');
    if (!f) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent = fill(T('section.syncplay.fail.' + f.code, '{c} : {d}'), { c: f.channel || '—', d: f.detail || '' });
  }

  function renderIssues(g) {
    const el = $('sp-issues'), xs = [];
    const v = g.verdict;
    if (g.state !== 'stopped' && v && v.frames)
      xs.push(fill(T('section.syncplay.issue_frames', 'Décalage de {n} image(s) entre canaux.'), { n: v.frames }));
    if (g.state !== 'stopped' && v && v.anchors && v.anchors.length)
      xs.push(fill(T('section.syncplay.issue_anchor', 'Temps média différent sur la même image : {c}.'), { c: v.anchors.join(', ') }));
    if (g.state !== 'stopped' && v && v.clock)
      xs.push(fill(T('section.syncplay.issue_skew', 'Écart d\'horloge entre nœuds de {ms} ms : une image ou plus ({p} ms).'), { ms: v.clock_ms.toFixed(1), p: g.period_ms.toFixed(1) }));
    if (g.state !== 'stopped' && g.transient && Date.now() / 1000 - g.transient.at < 30)
      xs.push(fill(T('section.syncplay.issue_transient', 'Désynchro passagère : le canal {c} a répété {n} image(s).'), { c: g.transient.channel, n: g.transient.frames }));
    (g.channels || []).forEach(c => {
      if (g.state !== 'stopped' && !['ok', 'idle', 'preparing'].includes(c.status))
        xs.push(`${c.letter} : ${CS(c.status)}${c.error ? ' — ' + c.error : ''}`);
    });
    (g.pairs || []).forEach(p => { if (p.state === 'defect') xs.push(fill(T('section.syncplay.issue_pair', 'Paire {p} en défaut : {e}.'), { p: p.key, e: p.issues.join(' ; ') })); });
    if (g.blocked) xs.push(fill(T('section.syncplay.issue_blocked', 'Ligne suivante refusée : {e} — la ligne en cours tiendra sa dernière image.'), { e: g.blocked[1] }));
    if (g.recovered === true) xs.push(T('section.syncplay.recovered', 'Lecture reprise d\'après les témoins des players (redémarrage de l\'orchestrateur).'));
    el.hidden = !xs.length;
    el.textContent = xs.join(' · ');
  }

  function renderChannels(g) {
    $('sp-chans').innerHTML = g.channels.map(c => {
      const sub = [];
      if (c.clock_ms != null) sub.push(fill(T('section.syncplay.clock', 'horloge {v} ms ± {u}'), { v: (c.clock_ms >= 0 ? '+' : '') + c.clock_ms.toFixed(2), u: (c.clock_unc_ms || 0).toFixed(2) }));
      if (c.lag) sub.push(fill(T('section.syncplay.lag', 'retard {n} image(s)'), { n: c.lag }));
      if (c.late_recent) sub.push(fill(T('section.syncplay.late_recent', '{n} image(s) en retard (10 s)'), { n: c.late_recent }));
      const pos = c.position != null ? tc(Math.max(0, c.position - ((c.clip || {}).in || 0)), g) : '';
      const bad = !['ok', 'idle', 'preparing'].includes(c.status);
      const pr = c.pair ? (g.pairs || []).find(p => p.a === c.letter || p.b === c.letter) : null;
      const ptag = c.pair ? `<div class="sp-pairtag">⇄ ${esc(fill(T('section.syncplay.paired_with', 'paire remplissage / découpe avec {l}'), { l: c.pair }))}${pr && pr.state === 'defect' ? ' — ' + esc(T('section.syncplay.pair_defect', 'en défaut')) : ''}${pr && pr.proven ? ' · ' + esc(T('section.syncplay.pair_proven', 'ligne déjà jouée sans défaut')) : ''}</div>` : '';
      return `<div class="sp-ch" data-s="${esc(c.status)}"${c.pair ? ' data-pair="' + esc(c.pair) + '"' : ''}${pr && pr.state === 'defect' ? ' data-pairbad="1"' : ''}>${ptag}
        <div class="sp-ch-head"><span class="sp-letter">${esc(c.letter)}</span>
          <span class="sp-host" title="${esc(c.hostname || '')}">${esc(c.hostname || '—')}</span>
          <span class="sp-st">${esc(CS(c.status))}</span></div>
        <div class="sp-title" title="${esc(c.title || '')}">${esc(c.title || (g.state === 'stopped' ? '—' : T('section.syncplay.black', '(noir)')))}</div>
        <div class="sp-sub">${esc(pos)}${c.eos ? ' · ' + esc(T('section.syncplay.eos', 'fin du clip, image tenue')) : ''}</div>
        <div class="sp-sub${bad ? ' bad' : ''}">${esc(sub.join(' · '))}${c.error && bad ? ' — ' + esc(c.error) : ''}</div></div>`;
    }).join('');
  }

  function renderLines(g, force) {
    const sig = JSON.stringify([g.lines, g.members, g.end_policy, canOp(), S.sel]);
    if (!force && sig === S.sigLines) return;
    S.sigLines = sig;
    const L = g.channels.map(c => c.letter);
    const durCol = g.end_policy === 'duration';
    $('sp-lines-head').innerHTML = `<tr><th>#</th><th>${esc(T('section.syncplay.col_title', 'Ligne'))}</th>${L.map(l => `<th>${esc(l)}</th>`).join('')}${durCol ? `<th>${esc(T('section.syncplay.col_duration', 'Durée'))}</th>` : ''}<th></th></tr>`;
    $('sp-noline').hidden = !!g.lines.length;
    const dis = canOp() ? '' : ' disabled';
    $('sp-lines').innerHTML = g.lines.map((ln, i) => {
      const cells = L.map(l => {
        const c = ln.clips[l];
        const sel = S.sel && S.sel.line === ln.id && S.sel.letter === l;
        const name = c ? (c.title || (c.file || '').split('/').pop() || c.asset || '') : T('section.syncplay.black', '(noir)');
        return `<td class="sp-cell${c ? '' : ' empty'}${sel ? ' sel' : ''}" data-cell="${esc(ln.id)}" data-l="${esc(l)}" title="${esc(c ? (c.file || '') : '')}">${esc(name)}${sel && c ? ' <button type="button" class="btn" data-clear="1" aria-label="' + esc(T('section.syncplay.clear_cell', 'Vider la cellule')) + '"' + dis + '>✕</button>' : ''}</td>`;
      }).join('');
      const dur = durCol ? `<td><input class="sp-dur" data-dur="${esc(ln.id)}" value="${ln.duration ? esc(tc(ln.duration, g)) : ''}" placeholder="${esc(T('section.syncplay.dur_auto', 'le plus long'))}"${dis}></td>` : '';
      const prv = (ln.proven || []).length ? ` <span class="sp-lead" title="${esc(T('section.syncplay.proven_tip', 'Déjà jouée en entier sans défaut par la paire : la mesure de décodage au départ ne la refuse plus. Toute modification de la ligne efface ce constat.'))}">✓ ${esc((ln.proven || []).join(', '))}</span>` : '';
      return `<tr data-s="${esc(ln.state)}"><td class="sp-n">${i + 1}</td><td>${esc(ln.title || '')}${prv}</td>${cells}${dur}
        <td class="sp-acts">
          <button type="button" class="btn" data-lop="up" data-id="${esc(ln.id)}" aria-label="${esc(T('section.syncplay.up', 'Monter'))}"${dis}>▲</button>
          <button type="button" class="btn" data-lop="down" data-id="${esc(ln.id)}" aria-label="${esc(T('section.syncplay.down', 'Descendre'))}"${dis}>▼</button>
          <button type="button" class="btn" data-go="${esc(ln.id)}" title="${esc(T('section.syncplay.goto_tip', 'Aller à cette ligne (lecture si le groupe joue, sinon calée sur sa 1re image)'))}"${dis}>${esc(T('section.syncplay.goto', 'Aller'))}</button>
          ${ln.state !== 'current' ? `<button type="button" class="btn" data-lop="drop" data-id="${esc(ln.id)}"${dis}>${esc(T('section.syncplay.drop', 'Retirer'))}</button>` : ''}
        </td></tr>`;
    }).join('');
    const sl = S.sel && g.lines.findIndex(x => x.id === S.sel.line);
    $('sp-sel-hint').textContent = (S.sel && sl >= 0)
      ? fill(T('section.syncplay.sel_hint', 'Cellule visée : ligne {n}, canal {l} — « → Cellule » dans la bibliothèque la remplit.'), { n: sl + 1, l: S.sel.letter })
      : T('section.syncplay.sel_none', 'Touchez une cellule pour la viser, puis « → Cellule » dans la bibliothèque. « + Ligne » crée une ligne avec les médias choisis, un par canal dans l\'ordre de la sélection.');
  }

  async function loadLists(g) {
    const ls = g.saved_lists || [];
    const cur = $('sp-lists').value;
    $('sp-lists').innerHTML = ls.length ? ls.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')
      : `<option value="">${esc(T('section.syncplay.no_list', '— aucune —'))}</option>`;
    if (ls.includes(cur)) $('sp-lists').value = cur;
  }

  function mountLibrary(g) {
    const vm = (g.channels.find(c => c.vmid != null) || {}).vmid;
    if (vm == null || vm === S.mlVmid || !window.MediaLibrary) return;
    if (S.ml) { try { S.ml.destroy(); } catch (e) { /* déjà démonté */ } }
    S.mlVmid = vm;
    S.ml = window.MediaLibrary.mount($('sp-lib'), {
      vmid: vm, T, toast, kinds: ['video', 'audio'], view: 'table', viewKey: 'syncplay',
      actions: [
        { id: 'line', label: T('section.syncplay.lib_line', '+ Ligne'), primary: true, needsLocal: false, multi: true },
        { id: 'cell', label: T('section.syncplay.lib_cell', '→ Cellule'), needsLocal: false, multi: true },
      ],
      onAction: (id, m) => queueAdd(id, m),
    });
  }

  // Une sélection multiple appelle l'action ligne par ligne : on regroupe en un seul geste.
  function queueAdd(id, m) {
    const a = m.asset || null;
    S.addBuf.push({ file: m.path || '', asset: a ? a.id : null, title: a ? ((a.meta || {}).title || a.name || null) : null });
    S.addMode = id;
    clearTimeout(S.addT);
    S.addT = setTimeout(() => run(flushAdd()), 60);
  }

  async function flushAdd() {
    const items = S.addBuf.splice(0), g = group();
    if (!g || !items.length) return;
    const ids = g.channels.map(c => c.uuid), L = g.channels.map(c => c.letter);
    if (S.addMode === 'cell') {
      if (!S.sel) { toast(T('section.syncplay.no_sel', 'Touchez d\'abord une cellule de la playlist.'), 'error'); return; }
      let k = L.indexOf(S.sel.letter);
      for (const it of items) {
        if (k < 0 || k >= ids.length) break;
        await lop('set_cell', { line: S.sel.line, member: ids[k], clip: it });
        k++;
      }
    } else {
      const clips = {};
      items.slice(0, ids.length).forEach((it, k) => { clips[ids[k]] = it; });
      await lop('add', { clips });
      if (items.length > ids.length) toast(fill(T('section.syncplay.too_many', '{n} média(s) de trop : une ligne prend un clip par canal.'), { n: items.length - ids.length }), 'warning');
    }
  }

  function render() {
    renderChips();
    const g = group();
    if (!g) return;
    renderFail(g); renderIssues(g); renderStatus(g); renderChannels(g); renderLines(g);
    loadLists(g); mountLibrary(g);
  }

  // ─── Actions ───────────────────────────────────────────────────────────────
  function apply(d) {
    if (d.group && S.data) {
      const i = S.data.groups.findIndex(x => x.id === d.group.id);
      if (i >= 0) S.data.groups[i] = d.group;
      render();
    }
    return d;
  }
  async function op(name, body) {
    S.busy = true; render();
    try { return apply(await api('POST', `/api/sync_groups/${S.gid}/${name}`, body || {})); }
    finally { S.busy = false; render(); }
  }
  const lop = async (name, body) => apply(await api('POST', `/api/sync_groups/${S.gid}/lines/${name}`, body || {}));
  const run = (p) => p.catch(e => toast(e.message, 'error'));

  function wire() {
    if (S.wired) return;
    S.wired = true;
    $('sp-chips').addEventListener('click', e => {
      const b = e.target.closest('[data-g]'); if (!b) return;
      S.gid = +b.dataset.g; S.sel = null; S.sigLines = ''; render();
    });
    $('sp-transport').addEventListener('click', e => {
      const b = e.target.closest('[data-op]'); if (!b) return;
      const body = b.dataset.frames ? { frames: +b.dataset.frames } : {};
      run(op(b.dataset.op, body));
    });
    $('sp-speeds').addEventListener('click', e => { const b = e.target.closest('[data-speed]'); if (b) run(op('speed', { speed: +b.dataset.speed })); });
    $('sp-loops').addEventListener('click', e => { const b = e.target.closest('[data-loop]'); if (b) run(op('loop', { mode: b.dataset.loop })); });
    $('sp-seek-go').addEventListener('click', () => {
      const g = group(), t = parseTc($('sp-tc').value, g);
      if (t == null) { toast(T('section.syncplay.err.bad_time', 'Timecode illisible'), 'error'); return; }
      run(op('seek', { t }));
    });
    $('sp-tc').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); $('sp-seek-go').click(); } });
    // Barre de temps : un APPUI va à cet instant (pas de glisser : il enverrait une recherche par pixel).
    $('sp-tl').addEventListener('click', e => {
      const g = group(); if (!g || !g.line_duration || !canOp()) return;
      const r = e.currentTarget.getBoundingClientRect();
      const t = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * g.line_duration;
      run(op('seek', { t }));
    });
    $('sp-tl').addEventListener('keydown', e => {
      const g = group(); if (!g || g.line_time == null) return;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); run(op('step', { frames: e.key === 'ArrowLeft' ? -1 : 1 })); }
    });
    $('sp-lines').addEventListener('click', e => {
      const g = group(); if (!g) return;
      const clr = e.target.closest('[data-clear]');
      if (clr && S.sel) { const k = g.channels.findIndex(c => c.letter === S.sel.letter);
        run(lop('set_cell', { line: S.sel.line, member: g.channels[k].uuid, clip: null })); return; }
      const cell = e.target.closest('[data-cell]');
      if (cell) { S.sel = (S.sel && S.sel.line === cell.dataset.cell && S.sel.letter === cell.dataset.l) ? null : { line: cell.dataset.cell, letter: cell.dataset.l };
        renderLines(g, true); return; }
      const go = e.target.closest('[data-go]');
      if (go) { run(op('goto', { line: go.dataset.go })); return; }
      const b = e.target.closest('[data-lop]'); if (!b) return;
      if (b.dataset.lop === 'drop' && b.dataset.arm !== '1') {           // deux appuis : le second confirme
        b.dataset.arm = '1'; b.textContent = T('section.syncplay.drop_confirm', 'Confirmer ?');
        setTimeout(() => { if (b.isConnected) { b.dataset.arm = ''; b.textContent = T('section.syncplay.drop', 'Retirer'); } }, 3000);
        return;
      }
      run(lop(b.dataset.lop, { line: b.dataset.id }));
    });
    $('sp-lines').addEventListener('change', e => {
      const i = e.target.closest('[data-dur]'); if (!i) return;
      const g = group(), v = i.value.trim();
      const d = v ? parseTc(v, g) : 0;
      if (d == null) { toast(T('section.syncplay.err.bad_time', 'Timecode illisible'), 'error'); return; }
      run(lop('duration', { line: i.dataset.dur, duration: d }));
    });
    $('sp-add-empty').addEventListener('click', () => run(lop('add', { clips: {} })));
    $('sp-list-replace').addEventListener('click', () => { const n = $('sp-lists').value; if (n) run(lop('load', { name: n, mode: 'replace' })); });
    $('sp-list-append').addEventListener('click', () => { const n = $('sp-lists').value; if (n) run(lop('load', { name: n, mode: 'append' })); });
    $('sp-list-save').addEventListener('click', () => {
      const n = $('sp-list-name').value.trim();
      if (!n) { toast(T('section.syncplay.err.name_required', 'Nom requis'), 'error'); return; }
      run(lop('save', { name: n }).then(() => toast(fill(T('section.syncplay.saved', 'Playlist « {n} » enregistrée'), { n }), 'info')));
    });
    $('sp-new').addEventListener('click', () => openDlg(null));
    $('sp-settings').addEventListener('click', () => openDlg(group()));
    $('sp-cancel').addEventListener('click', () => $('sp-dlg').close());
    $('sp-form').addEventListener('submit', e => { e.preventDefault(); saveDlg(); });
    $('sp-addmem').addEventListener('change', e => {
      const u = e.target.value; if (!u || !S.dlg) return;
      S.dlg.members.push(u); renderMembers(); renderPairs();
    });
    $('sp-mems').addEventListener('click', e => {
      const b = e.target.closest('[data-m]'); if (!b || !S.dlg) return;
      const i = +b.dataset.i, m = S.dlg.members;
      if (b.dataset.m === 'up' && i > 0) [m[i - 1], m[i]] = [m[i], m[i - 1]];
      if (b.dataset.m === 'down' && i < m.length - 1) [m[i + 1], m[i]] = [m[i], m[i + 1]];
      if (b.dataset.m === 'del') m.splice(i, 1);
      renderMembers(); renderPairs();
    });
    $('sp-pair-add').addEventListener('click', () => {
      const a = $('sp-pair-a').value, b = $('sp-pair-b').value;
      if (!a || !b || a === b || !S.dlg) return;
      S.dlg.pairs.push([a, b]); renderPairs();
    });
    $('sp-pairs').addEventListener('click', e => {
      const b = e.target.closest('[data-pdel]'); if (!b || !S.dlg) return;
      S.dlg.pairs.splice(+b.dataset.pdel, 1); renderPairs();
    });
    $('sp-del').addEventListener('click', async e => {
      const b = e.currentTarget;
      if (b.dataset.arm !== '1') { b.dataset.arm = '1'; b.textContent = T('section.syncplay.delete_confirm', 'Supprimer le groupe ?'); return; }
      try { await api('DELETE', `/api/sync_groups/${S.dlg.gid}`); $('sp-dlg').close(); S.gid = null; await load(); }
      catch (err) { toast(err.message, 'error'); }
    });
  }

  // ─── Réglages ⚙ (création / modification) ──────────────────────────────────
  function renderMembers() {
    const players = (S.data && S.data.players) || [];
    const byU = Object.fromEntries(players.map(p => [p.uuid, p]));
    const m = S.dlg.members;
    $('sp-mems').innerHTML = m.map((u, i) => `<div class="sp-mem"><span class="sp-letter">${String.fromCharCode(65 + i)}</span>
      <span class="sp-host">${esc((byU[u] || {}).hostname || u)}</span>
      <button type="button" class="btn" data-m="up" data-i="${i}" aria-label="${esc(T('section.syncplay.up', 'Monter'))}">▲</button>
      <button type="button" class="btn" data-m="down" data-i="${i}" aria-label="${esc(T('section.syncplay.down', 'Descendre'))}">▼</button>
      <button type="button" class="btn" data-m="del" data-i="${i}" aria-label="${esc(T('section.syncplay.remove_channel', 'Retirer du groupe'))}">✕</button></div>`).join('')
      || `<p class="sp-desc">${esc(T('section.syncplay.no_channel', 'Aucun canal : ajoutez au moins deux players.'))}</p>`;
    const taken = new Set();
    ((S.data && S.data.groups) || []).forEach(g => { if (g.id !== S.dlg.gid) g.members.forEach(u => taken.add(u)); });
    const free = players.filter(p => !m.includes(p.uuid) && !taken.has(p.uuid));
    $('sp-addmem').innerHTML = `<option value="">${esc(T('section.syncplay.pick_player', '— choisir un player —'))}</option>` +
      free.map(p => `<option value="${esc(p.uuid)}">${esc(p.hostname || p.uuid)}${p.status !== 'running' ? ' (' + esc(p.status || '?') + ')' : ''}</option>`).join('');
  }

  function renderPairs() {
    const players = (S.data && S.data.players) || [];
    const byU = Object.fromEntries(players.map(p => [p.uuid, p]));
    const m = S.dlg.members, L = u => String.fromCharCode(65 + m.indexOf(u));
    S.dlg.pairs = S.dlg.pairs.filter(p => m.includes(p[0]) && m.includes(p[1]));
    $('sp-pairs').innerHTML = S.dlg.pairs.map((p, i) => `<div class="sp-mem"><span class="sp-host">⇄ ${esc(L(p[0]))} ${esc((byU[p[0]] || {}).hostname || '')} + ${esc(L(p[1]))} ${esc((byU[p[1]] || {}).hostname || '')}</span>
      <button type="button" class="btn" data-pdel="${i}" aria-label="${esc(T('section.syncplay.pair_del', 'Défaire la paire'))}">✕</button></div>`).join('');
    const used = new Set(S.dlg.pairs.flat());
    const opts = `<option value="">—</option>` + m.filter(u => !used.has(u)).map(u => `<option value="${esc(u)}">${esc(L(u))} · ${esc((byU[u] || {}).hostname || u)}</option>`).join('');
    $('sp-pair-a').innerHTML = opts; $('sp-pair-b').innerHTML = opts;
  }

  function openDlg(g) {
    const lim = (S.data && S.data.limits) || {};
    S.dlg = { gid: g ? g.id : null, members: g ? g.members.slice() : [],
      pairs: g ? (g.pairs || []).map(p => [g.members[p.a.charCodeAt(0) - 65], g.members[p.b.charCodeAt(0) - 65]]) : [] };
    const of = (g && g.open_fail) || 'proceed';
    document.querySelectorAll('#sp-dlg input[name="sp-of"]').forEach(r => { r.checked = r.value === of; });
    $('sp-dlg-t').textContent = g ? T('section.syncplay.settings_t', 'Réglages de la lecture synchrone') : T('section.syncplay.new_t', 'Nouvelle lecture synchrone');
    $('sp-name').value = g ? g.name : '';
    const pol = (g && g.end_policy) || 'longest';
    document.querySelectorAll('#sp-dlg input[name="sp-end"]').forEach(r => { r.checked = r.value === pol; });
    $('sp-lead').value = (g && g.lead_ms) || lim.lead_default || 400;
    const del = $('sp-del'); del.hidden = !g; del.dataset.arm = ''; del.textContent = T('section.syncplay.delete', 'Supprimer le groupe');
    renderMembers(); renderPairs();
    $('sp-dlg').showModal();
  }

  async function saveDlg() {
    const body = { name: $('sp-name').value.trim(), members: S.dlg.members,
      end_policy: (document.querySelector('#sp-dlg input[name="sp-end"]:checked') || {}).value || 'longest',
      lead_ms: parseInt($('sp-lead').value, 10) || 400, pairs: S.dlg.pairs,
      open_fail: (document.querySelector('#sp-dlg input[name="sp-of"]:checked') || {}).value || 'proceed' };
    try {
      const d = S.dlg.gid ? await api('POST', `/api/sync_groups/${S.dlg.gid}`, body) : await api('POST', '/api/sync_groups', body);
      $('sp-dlg').close();
      if (d.id) S.gid = d.id;
      await load();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ─── Cycle ─────────────────────────────────────────────────────────────────
  async function load() {
    try { S.data = await api('GET', '/api/sync_groups'); render(); }
    catch (e) { /* rafraîchissement suivant */ }
  }
  window.syncplayLoad = function () {
    wire();
    S.alive = true;
    load();
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (S.alive && !$('sp-dlg').open && !S.busy) load(); }, 500);
  };
  window.syncplayStop = function () { S.alive = false; clearInterval(S.timer); S.timer = null; };
})();
