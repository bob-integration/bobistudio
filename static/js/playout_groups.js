// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Groupes de lecture A/B/C… — page Médias, mode « Lecture A/B ». Cf. app/playout_groups.py.
// Tactile d'abord (iPad) : chaque geste est un bouton ; le réordonnancement passe par ▲ ▼, jamais par
// un glisser seul. Retirer un élément = deux appuis (le second confirme).
(function () {
  'use strict';
  const T = (k, fb) => { const v = window.t ? window.t(k) : k; return (v && v !== k) ? v : (fb !== undefined ? fb : k); };
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const toast = (m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m));
  const fill = (s, o) => String(s).replace(/\{(\w+)\}/g, (m, k) => (o[k] != null ? o[k] : m));

  const S = { data: null, gid: null, timer: null, alive: false, wired: false, ml: null, mlVmid: null,
    sigItems: '', listsFor: null, dlg: null, addBuf: [], addT: null };

  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch (e) { /* corps vide */ }
    if (!r.ok) {
      const err = new Error(d.code ? T('section.playout.err.' + d.code, d.error || d.code) : (d.error || ('HTTP ' + r.status)));
      err.code = d.code; throw err;
    }
    return d;
  }
  const group = () => ((S.data && S.data.groups) || []).find(g => g.id === S.gid) || null;
  const canOp = () => !!(S.data && S.data.can_operate);
  const ST = s => T('section.playout.st_' + s, s);
  const IS = s => T('section.playout.is_' + s, s);
  const tc = s => { if (!(s >= 0)) return '--:--'; s = Math.floor(s); const m = Math.floor(s / 60), x = s % 60;
    return (m >= 60 ? Math.floor(m / 60) + ':' + String(m % 60).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };

  // ─── Rendu ─────────────────────────────────────────────────────────────────
  function renderChips() {
    const gs = (S.data && S.data.groups) || [];
    if (S.gid == null || !gs.find(g => g.id === S.gid)) S.gid = gs.length ? gs[0].id : null;
    $('po-chips').innerHTML = gs.map(g => `<button type="button" class="po-chip${g.id === S.gid ? ' active' : ''}" data-g="${g.id}">${esc(g.name)}</button>`).join(' ');
    $('po-empty').hidden = !!gs.length;
    $('po-group').hidden = !gs.length;
    $('po-settings').hidden = !gs.length || !canOp();
    $('po-new').hidden = !canOp();
  }

  function renderOutputs(g) {
    $('po-outs').innerHTML = g.outputs.map(o => {
      const title = o.role === 'air' ? ((o.loaded && o.loaded.title) || o.item_title) : o.item_title;
      const sub = [];
      if (o.mixer_input) sub.push(fill(T('section.playout.mixer_input', 'entrée {n} du mélangeur'), { n: o.mixer_input }) +
        (o.on_program ? ' · ' + T('section.playout.on_program', 'au programme') : ''));
      if (o.status === 'fetching' && o.fetch && o.fetch.total_bytes)
        sub.push(fill(T('section.playout.fetch_pct', 'rapatriement {p} %'), { p: Math.round(100 * (o.fetch.done_bytes || 0) / o.fetch.total_bytes) }));
      if (o.error && o.status === 'error') sub.push(o.error);
      if (o.status === 'release') sub.push(T('section.playout.block.' + (o.release_block || 'disabled'), o.release_block || ''));
      const pos = o.position || 0, dur = o.duration || 0;
      const bar = (o.role === 'air' && dur > 0)
        ? `<div class="po-bar-p"><span style="width:${Math.min(100, 100 * pos / dur).toFixed(1)}%"></span></div><div class="po-sub">${tc(pos)} / ${tc(dur)} · ${esc(T('section.playout.remaining', 'reste'))} ${tc(dur - pos)}</div>` : '';
      const dis = canOp() ? '' : ' disabled';
      return `<div class="po-out" data-s="${esc(o.status)}">
        <div class="po-out-head"><span class="po-letter">${esc(o.letter)}</span>
          <span class="po-host" title="${esc(o.hostname || '')}">${esc(o.hostname || '—')}</span>
          <span class="po-st" data-s="${esc(o.status)}">${esc(ST(o.status))}</span></div>
        <div class="po-title" title="${esc(title || '')}">${esc(title || '—')}</div>
        ${bar}<div class="po-sub">${esc(sub.join(' · '))}</div>
        <div class="po-out-acts">
          <button type="button" class="btn btn-green" data-play="${esc(o.letter)}"${dis}>▶︎ ${esc(T('section.playout.play', 'Play'))}</button>
          <button type="button" class="btn${o.status === 'release' ? ' btn-blue' : ''}" data-cue="${esc(o.letter)}"${dis}>${esc(o.status === 'release' ? T('section.playout.release_btn', 'Libérer (Cue)') : T('section.playout.cue', 'Cue'))}</button>
        </div></div>`;
    }).join('');
  }

  function renderNext(g) {
    const b = $('po-next');
    if (g.taking) {
      b.innerHTML = `${esc(T('section.playout.taking', 'Mise à l\'antenne…'))}<small>${esc(g.taking.output)}</small>`;
      b.disabled = true; return;
    }
    if (!g.next) {
      b.innerHTML = `${esc(T('section.playout.next', 'SUIVANT'))}<small>${esc(T('section.playout.end', 'fin de la playlist'))}</small>`;
      b.disabled = true; return;
    }
    const it = g.next.item;
    b.innerHTML = `${esc(T('section.playout.next', 'SUIVANT'))} ▶︎<small>${esc(g.next.output)} — ${esc(it.title || (it.file || '').split('/').pop() || it.asset || '')}</small>`;
    b.disabled = !canOp();
  }

  function renderFail(g) {
    const f = g.last_failure, el = $('po-fail');
    if (!f) { el.hidden = true; return; }
    el.hidden = false;
    el.textContent = fill(T('section.playout.fail', 'La sortie {o} n\'a pas démarré « {i} » ({e}) : l\'antenne est restée sur la sortie précédente, rien n\'a été libéré.'),
      { o: f.output, i: f.item || '—', e: T('section.playout.reason.' + f.reason, f.reason) });
  }

  function renderIssues(g) {
    const el = $('po-issues'), xs = g.designation_issues || [];
    el.hidden = !xs.length;
    el.textContent = xs.map(x => (x.output ? x.output + ' : ' : '') + T('section.playout.block.' + x.reason, x.reason)).join(' · ');
  }

  function renderItems(g, force) {
    const sig = JSON.stringify([g.items, g.cursor, canOp()]);
    if (!force && sig === S.sigItems) return;
    S.sigItems = sig;
    const ci = g.items.findIndex(it => it.id === g.cursor);
    $('po-noitem').hidden = !!g.items.length;
    const dis = canOp() ? '' : ' disabled';
    $('po-items').innerHTML = g.items.map((it, i) => {
      const up = i > ci;
      const name = it.title || (it.file || '').split('/').pop() || it.asset || '';
      let acts = '';
      if (up && it.state !== 'skipped') {
        acts = `<button type="button" class="btn" data-op="up" data-id="${esc(it.id)}" title="${esc(T('section.playout.up', 'Monter'))}" aria-label="${esc(T('section.playout.up', 'Monter'))}"${dis}>▲</button>
          <button type="button" class="btn" data-op="down" data-id="${esc(it.id)}" title="${esc(T('section.playout.down', 'Descendre'))}" aria-label="${esc(T('section.playout.down', 'Descendre'))}"${dis}>▼</button>
          <button type="button" class="btn" data-op="goto" data-id="${esc(it.id)}" title="${esc(T('section.playout.goto_tip', 'En faire le suivant (les éléments d\'avant sont sautés)'))}"${dis}>${esc(T('section.playout.goto', 'Aller à'))}</button>
          <button type="button" class="btn" data-op="skip" data-id="${esc(it.id)}"${dis}>${esc(T('section.playout.skip', 'Skip'))}</button>`;
      } else if (up) {
        acts = `<button type="button" class="btn" data-op="unskip" data-id="${esc(it.id)}"${dis}>${esc(T('section.playout.unskip', 'Rétablir'))}</button>`;
      }
      if (it.state !== 'air') acts += ` <button type="button" class="btn" data-op="drop" data-id="${esc(it.id)}"${dis}>${esc(T('section.playout.drop', 'Drop'))}</button>`;
      return `<tr data-s="${esc(it.state)}"><td>${i + 1}</td><td class="po-o">${esc(it.output || '')}</td>
        <td class="po-t" title="${esc(it.file || '')}">${esc(name)}</td><td>${esc(IS(it.state))}</td><td class="po-acts">${acts}</td></tr>`;
    }).join('');
  }

  async function loadLists(g) {
    if (S.listsFor === g.id) return;
    S.listsFor = g.id;
    let ls = [];
    try { ls = (await api('GET', `/api/playout_groups/${g.id}/lists`)).lists || []; } catch (e) { /* aucun membre joignable */ }
    $('po-lists').innerHTML = ls.length ? ls.map(l => `<option value="${esc(l.name)}">${esc(l.name)} (${l.items})</option>`).join('')
      : `<option value="">${esc(T('section.playout.no_list', '— aucune —'))}</option>`;
  }

  function mountLibrary(g) {
    const vm = (g.outputs.find(o => o.vmid != null) || {}).vmid;
    if (vm == null || vm === S.mlVmid || !window.MediaLibrary) return;
    if (S.ml) { try { S.ml.destroy(); } catch (e) { /* déjà démonté */ } }
    S.mlVmid = vm;
    S.ml = window.MediaLibrary.mount($('po-lib'), {
      vmid: vm, T, toast, kinds: ['video', 'audio'], view: 'table', viewKey: 'playout',
      actions: [{ id: 'add', label: T('section.playout.lib_add', '+ Groupe'), primary: true, needsLocal: false, multi: true }],
      onAction: (id, m) => queueAdd(m),
    });
  }

  // Une sélection multiple appelle l'action ligne par ligne : on regroupe en un seul ajout.
  function queueAdd(m) {
    const a = m.asset || null;
    S.addBuf.push({ file: m.path || '', asset: a ? a.id : null, title: a ? ((a.meta || {}).title || a.name || null) : null });
    clearTimeout(S.addT);
    S.addT = setTimeout(async () => {
      const items = S.addBuf.splice(0);
      try { await op('add', { items, mode: 'append' });
        toast(fill(T('section.playout.added', '{n} élément(s) ajouté(s) au groupe'), { n: items.length }), 'info'); }
      catch (e) { toast(e.message, 'error'); }
    }, 60);
  }

  function render() {
    renderChips();
    const g = group();
    if (!g) return;
    renderFail(g); renderIssues(g); renderOutputs(g); renderNext(g); renderItems(g);
    loadLists(g); mountLibrary(g);
  }

  // ─── Actions ───────────────────────────────────────────────────────────────
  async function op(name, body) {
    const d = await api('POST', `/api/playout_groups/${S.gid}/${name}`, body || {});
    if (d.group && S.data) {
      const i = S.data.groups.findIndex(g => g.id === d.group.id);
      if (i >= 0) S.data.groups[i] = d.group;
      render();
    }
    return d;
  }
  const run = (p) => p.catch(e => toast(e.message, 'error'));

  function wire() {
    if (S.wired) return;
    S.wired = true;
    $('po-chips').addEventListener('click', e => {
      const b = e.target.closest('[data-g]'); if (!b) return;
      S.gid = +b.dataset.g; S.sigItems = ''; render();
    });
    $('po-next').addEventListener('click', () => run(op('next')));
    $('po-reset').addEventListener('click', () => run(op('reset')));
    $('po-outs').addEventListener('click', e => {
      const p = e.target.closest('[data-play]'), c = e.target.closest('[data-cue]');
      if (p) run(op('play', { output: p.dataset.play }));
      if (c) run(op('cue', { output: c.dataset.cue }));
    });
    $('po-items').addEventListener('click', e => {
      const b = e.target.closest('[data-op]'); if (!b) return;
      const g = group(); if (!g) return;
      const opn = b.dataset.op, id = b.dataset.id;
      if (opn === 'drop' && b.dataset.arm !== '1') {           // deux appuis : le second confirme
        b.dataset.arm = '1'; b.textContent = T('section.playout.drop_confirm', 'Confirmer ?');
        setTimeout(() => { if (b.isConnected) { b.dataset.arm = ''; b.textContent = T('section.playout.drop', 'Drop'); } }, 3000);
        return;
      }
      run(op('item', { op: opn, item_id: id }));
    });
    $('po-list-replace').addEventListener('click', () => loadList('replace'));
    $('po-list-append').addEventListener('click', () => loadList('append'));
    $('po-new').addEventListener('click', () => openDlg(null));
    $('po-settings').addEventListener('click', () => openDlg(group()));
    $('po-cancel').addEventListener('click', () => $('po-dlg').close());
    $('po-form').addEventListener('submit', e => { e.preventDefault(); saveDlg(); });
    $('po-addmem').addEventListener('change', e => {
      const u = e.target.value; if (!u || !S.dlg) return;
      S.dlg.members.push(u); renderMembers(); renderChans();
    });
    $('po-mems').addEventListener('click', e => {
      const b = e.target.closest('[data-m]'); if (!b || !S.dlg) return;
      const i = +b.dataset.i, m = S.dlg.members;
      if (b.dataset.m === 'up' && i > 0) [m[i - 1], m[i]] = [m[i], m[i - 1]];
      if (b.dataset.m === 'down' && i < m.length - 1) [m[i + 1], m[i]] = [m[i], m[i + 1]];
      if (b.dataset.m === 'del') m.splice(i, 1);
      renderMembers(); renderChans();
    });
    $('po-mixer').addEventListener('change', renderChans);
    $('po-console').addEventListener('change', renderChans);
    $('po-chans').addEventListener('change', e => {
      const sel = e.target.closest('[data-ch]'); if (!sel || !S.dlg) return;
      const u = sel.dataset.u; S.dlg.channels[u] = S.dlg.channels[u] || {};
      if (sel.dataset.ch === 'strip_id') { S.dlg.channels[u].strip_id = sel.value || null; S.dlg.channels[u].strip = null; }
      else S.dlg.channels[u][sel.dataset.ch] = sel.value ? parseInt(sel.value, 10) : null;
    });
    $('po-suggest').addEventListener('click', () => {
      if (!S.dlg) return;
      S.dlg.members.forEach(u => { const sg = suggestFor(u); const ch = S.dlg.channels[u] = S.dlg.channels[u] || {};
        if (sg.mixer_input) ch.mixer_input = sg.mixer_input; if (sg.strip_id) { ch.strip_id = sg.strip_id; ch.strip = null; } });
      renderChans();
    });
    $('po-del').addEventListener('click', async e => {
      const b = e.currentTarget;
      if (b.dataset.arm !== '1') { b.dataset.arm = '1'; b.textContent = T('section.playout.delete_confirm', 'Supprimer le groupe ?'); return; }
      try { await api('DELETE', `/api/playout_groups/${S.dlg.gid}`); $('po-dlg').close(); S.gid = null; await load(); }
      catch (err) { toast(err.message, 'error'); }
    });
  }

  async function loadList(mode) {
    const name = $('po-lists').value;
    if (!name) return;
    try { const d = await op('load_list', { name, mode });
      toast(fill(T('section.playout.added', '{n} élément(s) ajouté(s) au groupe'), { n: d.added || 0 }), 'info'); }
    catch (e) { toast(e.message, 'error'); }
  }

  // ─── Réglages ⚙ (création / modification) ──────────────────────────────────
  function renderMembers() {
    const players = (S.data && S.data.players) || [];
    const byU = Object.fromEntries(players.map(p => [p.uuid, p]));
    const m = S.dlg.members;
    $('po-mems').innerHTML = m.map((u, i) => `<div class="po-mem"><span class="po-letter">${String.fromCharCode(65 + i)}</span>
      <span class="po-host">${esc((byU[u] || {}).hostname || u)}</span>
      <button type="button" class="btn" data-m="up" data-i="${i}" aria-label="${esc(T('section.playout.up', 'Monter'))}">▲</button>
      <button type="button" class="btn" data-m="down" data-i="${i}" aria-label="${esc(T('section.playout.down', 'Descendre'))}">▼</button>
      <button type="button" class="btn" data-m="del" data-i="${i}" aria-label="${esc(T('section.playout.remove_output', 'Retirer du groupe'))}">✕</button></div>`).join('')
      || `<p class="po-desc">${esc(T('section.playout.no_output', 'Aucune sortie : ajoutez au moins deux players.'))}</p>`;
    const taken = new Set();
    ((S.data && S.data.groups) || []).forEach(g => { if (g.id !== S.dlg.gid) g.members.forEach(u => taken.add(u)); });
    const free = players.filter(p => !m.includes(p.uuid) && !taken.has(p.uuid));
    $('po-addmem').innerHTML = `<option value="">${esc(T('section.playout.pick_player', '— choisir un player —'))}</option>` +
      free.map(p => `<option value="${esc(p.uuid)}">${esc(p.hostname || p.uuid)}${p.status !== 'running' ? ' (' + esc(p.status || '?') + ')' : ''}</option>`).join('');
  }

  const mixerOf = uuid => ((S.data && S.data.mixers) || []).find(m => m.uuid === uuid) || null;
  const consoleOf = uuid => ((S.data && S.data.consoles) || []).find(c => c.uuid === uuid) || null;
  const playerOf = uuid => ((S.data && S.data.players) || []).find(p => p.uuid === uuid) || null;

  // SUGGESTION seulement (l'humain valide) : l'entrée du mélangeur qui reçoit le flux vidéo du player, la tranche
  // dont l'entrée A reçoit son audio. Rien n'est enregistré sans « Enregistrer ».
  function suggestFor(u) {
    const h = (playerOf(u) || {}).hostname || '';
    const m = mixerOf($('po-mixer').value), c = consoleOf($('po-console').value);
    const vin = m ? (m.inputs.find(i => [i.shm, i.resolved].some(x => x === h || x === h + '_osd')) || {}).n : null;
    const st = c ? (c.strips.find(x => [x.flow, x.resolved].some(f => f === h + '_audio')) || {}).id : null;
    return { mixer_input: vin || null, strip_id: st || null };
  }

  function renderChans() {
    const m = mixerOf($('po-mixer').value), c = consoleOf($('po-console').value);
    const chans = S.dlg.channels;
    $('po-chans').innerHTML = S.dlg.members.length ? `<div class="po-chan"><span></span><b>${esc(T('section.playout.mixer_input_col', 'Entrée du mélangeur'))}</b><b>${esc(T('section.playout.strip_col', 'Tranche de console'))}</b></div>` +
      S.dlg.members.map((u, i) => {
        const ch = chans[u] || {}, sg = suggestFor(u);
        const optsM = `<option value="">—</option>` + (m ? m.inputs.map(x => `<option value="${x.n}"${ch.mixer_input === x.n ? ' selected' : ''}>${x.n} · ${esc(x.label || x.shm || '')}${sg.mixer_input === x.n ? ' ★' : ''}</option>`).join('') : '');
        // La tranche se désigne par son ID (jamais par son nom : homonymes) ; l'écran dit « n° · nom », et
        // ajoute l'id quand deux tranches portent le même nom. Un id disparu reste affiché, signalé.
        const counts = {}; (c ? c.strips : []).forEach(x => { counts[x.label] = (counts[x.label] || 0) + 1; });
        const cur = ch.strip_id || (c && ch.strip ? ((c.strips.find(x => x.n === ch.strip) || {}).id || '') : '');
        const gone = cur && c && !c.strips.some(x => x.id === cur);
        const optsS = `<option value="">—</option>` + (gone ? `<option value="${esc(cur)}" selected>⚠ ${esc(T('section.playout.strip_gone', 'tranche inexistante'))} (${esc(cur)})</option>` : '') +
          (c ? c.strips.map(x => `<option value="${esc(x.id)}"${cur === x.id ? ' selected' : ''}>${x.n} · ${esc(x.label)}${counts[x.label] > 1 ? ' (' + esc(x.id) + ')' : ''}${sg.strip_id === x.id ? ' ★' : ''}</option>`).join('') : '');
        return `<div class="po-chan"><span class="po-letter">${String.fromCharCode(65 + i)}</span>
          <select data-ch="mixer_input" data-u="${esc(u)}" aria-label="${esc(String.fromCharCode(65 + i) + ' — ' + T('section.playout.mixer_input_col', 'Entrée du mélangeur'))}"${m ? '' : ' disabled'}>${optsM}</select>
          <select data-ch="strip_id" data-u="${esc(u)}" aria-label="${esc(String.fromCharCode(65 + i) + ' — ' + T('section.playout.strip_col', 'Tranche de console'))}"${c ? '' : ' disabled'}>${optsS}</select></div>`;
      }).join('') : '';
  }

  function openDlg(g) {
    S.dlg = { gid: g ? g.id : null, members: g ? g.members.slice() : [], channels: {} };
    if (g) g.members.forEach((u, i) => { S.dlg.channels[u] = Object.assign({}, (g.channels || {})[String.fromCharCode(65 + i)] || {}); });
    $('po-dlg-t').textContent = g ? T('section.playout.settings_t', 'Réglages du groupe de lecture') : T('section.playout.new_t', 'Nouveau groupe de lecture');
    $('po-name').value = g ? g.name : '';
    const trig = g ? g.trigger : 'manual';
    document.querySelectorAll('#po-dlg input[name="po-trig"]').forEach(r => { r.checked = r.value === trig; });
    const none = `<option value="">${esc(T('section.playout.no_mixer', '— aucun —'))}</option>`;
    $('po-mixer').innerHTML = none + ((S.data && S.data.mixers) || []).map(m => `<option value="${esc(m.uuid)}">${esc(m.label)}</option>`).join('');
    $('po-console').innerHTML = none + ((S.data && S.data.consoles) || []).map(c => `<option value="${esc(c.uuid)}">${esc(c.label)}</option>`).join('');
    $('po-mixer').value = (g && g.mixer_uuid) || '';
    $('po-console').value = (g && g.console_uuid) || '';
    $('po-auto').checked = !!(g && g.auto_release);
    $('po-autonext').checked = !!(g && g.auto_next);
    const ooo = (g && g.out_of_order) || 'skip';
    document.querySelectorAll('#po-dlg input[name="po-ooo"]').forEach(r => { r.checked = r.value === ooo; });
    const del = $('po-del'); del.hidden = !g; del.dataset.arm = ''; del.textContent = T('section.playout.delete', 'Supprimer le groupe');
    renderMembers(); renderChans();
    $('po-dlg').showModal();
  }

  async function saveDlg() {
    const trig = (document.querySelector('#po-dlg input[name="po-trig"]:checked') || {}).value || 'manual';
    const channels = {};
    S.dlg.members.forEach(u => { channels[u] = S.dlg.channels[u] || {}; });
    const body = { name: $('po-name').value.trim(), members: S.dlg.members, trigger: trig,
      mixer_uuid: $('po-mixer').value || null, console_uuid: $('po-console').value || null, channels,
      auto_release: $('po-auto').checked, auto_next: $('po-autonext').checked,
      out_of_order: (document.querySelector('#po-dlg input[name="po-ooo"]:checked') || {}).value || 'skip' };
    try {
      const d = S.dlg.gid ? await api('POST', `/api/playout_groups/${S.dlg.gid}`, body)
        : await api('POST', '/api/playout_groups', body);
      $('po-dlg').close();
      if (d.id) S.gid = d.id;
      S.listsFor = null; await load();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ─── Cycle ─────────────────────────────────────────────────────────────────
  async function load() {
    try { S.data = await api('GET', '/api/playout_groups'); render(); }
    catch (e) { /* rafraîchissement suivant */ }
  }
  window.playoutLoad = function () {
    wire();
    S.alive = true;
    load();
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (S.alive && !$('po-dlg').open) load(); }, 1000);
  };
  window.playoutStop = function () { S.alive = false; clearInterval(S.timer); S.timer = null; };
})();
