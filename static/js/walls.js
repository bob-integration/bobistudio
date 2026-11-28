// SPDX-License-Identifier: GPL-3.0-or-later
// Murs (fabric rework step 4, 2026-11-25): every wall, what computes it, and the fabric's proposals.
// Data: GET /api/fabric/walls (read only). Actions: apply a proposal, « if the wall saturates ».
(function () {
  'use strict';
  const T = (k, p) => window.tp ? window.tp(k, p) : k;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const $ = id => document.getElementById(id);
  // One colour per container; neighbours differ in lightness too, not only in hue.
  const PALETTE = ['#2F6FD6', '#C7772B', '#1F9C8A', '#8E5BD0', '#B84A3A', '#5AA02C', '#D4A72C', '#3B8FC4'];
  const WALL_GREY = '#8A8A84';
  const S = { data: null, filter: 'all', timer: null };
  const canDeploy = () => typeof PS_CAN_DEPLOY !== 'undefined' && !!PS_CAN_DEPLOY;
  const later = () => { try { return JSON.parse(localStorage.getItem('walls.later') || '{}'); } catch (e) { return {}; } };
  const setLater = v => { try { localStorage.setItem('walls.later', JSON.stringify(v)); } catch (e) { /* per viewer only */ } };

  function wallName(w) { return w.label || w.hostname || ('#' + w.vmid); }

  function byVmid() { const m = {}; for (const w of (S.data && S.data.walls) || []) m[w.vmid] = w; return m; }

  function proposedWalls() {
    const s = new Set();
    for (const p of Object.values((S.data && S.data.proposals) || {})) for (const v of (p.changed || [])) s.add(v);
    return s;
  }

  function map(w) {
    const owners = [...new Set(w.cells.map(c => c.by))];
    const colour = by => by === w.vmid || by == null ? WALL_GREY : PALETTE[owners.filter(o => o !== w.vmid && o != null).indexOf(by) % PALETTE.length];
    const W = 128, H = w.out_w ? Math.round(W * w.out_h / w.out_w) : 72;
    const k = w.out_w ? W / w.out_w : 0;
    const cells = w.cells.map(c => `<span style="left:${c.x * k}px;top:${c.y * k}px;width:${c.w * k}px;height:${c.h * k}px;background:${colour(c.by)}" title="${esc(c.by === w.vmid ? T('section.walls.by_wall') : '#' + c.by)}"></span>`).join('');
    return `<div class="wl-map" style="height:${H}px" role="img" aria-label="${esc(T('section.walls.map_aria', { n: owners.length }))}">${cells}</div>`;
  }

  function compoText(w) {
    if (!w.shards.length) return T('section.walls.one_container');
    const shared = w.shards.filter(s => s.kind === 'shared').length;
    return T('section.walls.n_shards', { n: w.shards.length }) + (shared ? ' · ' + T('section.walls.n_shared', { n: shared }) : '');
  }

  function level(l) { return l ? T('settings.cores.weight.' + l) : '—'; }

  function deadlines(w) {
    const d = w.deadline;
    let out = d == null ? `<span class="wl-warn">${esc(T('section.walls.no_witness'))}</span>`
      : `<span class="${d.missed ? 'wl-bad' : 'wl-ok'}">${esc(T('section.walls.missed', { n: d.missed, s: d.window_s }))}</span>`;
    const blind = w.shards.filter(s => s.deadline == null).length;
    if (blind) out += `<div class="wl-sub wl-warn">${esc(T('section.walls.shards_no_witness', { n: blind }))}</div>`;
    return out;
  }

  function rate(w) {
    const f = w.fps != null ? (Math.round(w.fps * 10) / 10) + ' i/s' : '—';
    return esc(f) + `<div class="wl-sub">${esc(T('section.walls.containers', { n: 1 + w.shards.length }))}</div>`;
  }

  function saturation(w) {
    if (!canDeploy()) return esc(T('section.walls.sat_' + w.saturation));
    return `<label><span class="sr-only" style="position:absolute;left:-9999px">${esc(T('section.walls.col_saturation') + ' — ' + wallName(w))}</span>
      <select data-sat="${w.vmid}">
        <option value="auto"${w.saturation === 'auto' ? ' selected' : ''}>${esc(T('section.walls.sat_auto'))}</option>
        <option value="manual"${w.saturation === 'manual' ? ' selected' : ''}>${esc(T('section.walls.sat_manual'))}</option>
      </select></label>`;
  }

  function rows() {
    const ws = (S.data && S.data.walls) || [];
    const prop = proposedWalls();
    const shown = ws.filter(w => S.filter === 'all' || (S.filter === 'sharded' ? w.shards.length : prop.has(w.vmid)));
    document.querySelectorAll('#wl-embed .wl-chip').forEach(b => {
      const f = b.dataset.f, n = f === 'all' ? ws.length : f === 'sharded' ? ws.filter(w => w.shards.length).length : ws.filter(w => prop.has(w.vmid)).length;
      b.textContent = T('section.walls.f_' + f, { n });
      b.classList.toggle('active', f === S.filter);
    });
    if (!shown.length) { $('wl-rows').innerHTML = `<tr><td colspan="7" class="wl-empty">${esc(T('section.walls.none'))}</td></tr>`; return; }
    $('wl-rows').innerHTML = shown.map(w => `<tr class="${prop.has(w.vmid) ? 'wl-proposed' : ''}">
      <td><strong>${esc(wallName(w))}</strong><div class="wl-sub">#${w.vmid}${w.project ? ' · ' + esc(w.project) : ''}</div>${w.on_air ? `<div class="wl-air">${esc(T('section.walls.on_air'))}</div>` : `<div class="wl-sub">${esc(T('section.walls.starting'))}</div>`}</td>
      <td>${map(w)}<div class="wl-sub">${esc(compoText(w))}</div></td>
      <td>${esc(w.node || '—')}</td>
      <td><span class="wl-badge">${esc(level(w.level))}</span>${w.shards.length ? `<div class="wl-sub">${esc(T('section.walls.shards_level', { l: [...new Set(w.shards.map(s => level(s.level)))].join(', ') }))}</div>` : ''}</td>
      <td>${rate(w)}</td>
      <td>${deadlines(w)}</td>
      <td>${saturation(w)}</td></tr>`).join('');
  }

  function names(vmids) { const m = byVmid(); return vmids.map(v => wallName(m[v] || { vmid: v })).join(', '); }

  function propCard(nid, p) {
    const c = p.cutover || {};
    const head = p.saturated && p.saturated.length
      ? T('section.walls.prop_saturated', { w: names(p.saturated), node: p.node || nid })
      : T('section.walls.prop_regroup', { node: p.node || nid });
    const gain = T('section.walls.prop_gain', { c0: p.containers[0], c1: p.containers[1], a0: p.area_mpx[0], a1: p.area_mpx[1] });
    const air = (p.recut || []).filter(v => (byVmid()[v] || {}).on_air);
    let how;
    if (c.mode === 'restart_in_place') how = `<p class="wl-hole">${esc(T('section.walls.mode_in_place', { w: names(p.changed || []) }))}</p><p>${esc(T('section.walls.mode_in_place_why', { cn: c.cores[0], cf: c.cores[1], mem: (c.memory_mb || [])[1] }))}</p>`;
    else if (c.mode === 'prepare') how = `<p>${esc(T('section.walls.mode_prepare'))}</p>`;
    else how = `<p>${esc(T('section.walls.mode_unknown'))}</p>`;
    const acts = canDeploy() ? `<div class="wl-acts"><button type="button" class="btn btn-blue" data-apply="${nid}">${esc(T('section.walls.validate'))}</button>
      <button type="button" class="btn" data-later="${nid}">${esc(T('section.walls.later'))}</button></div>` : '';
    return `<section class="wl-prop" aria-label="${esc(head)}"><h3>${esc(head)}</h3><p>${esc(gain)}</p>
      ${air.length ? `<p>${esc(T('section.walls.prop_on_air', { w: names(air) }))}</p>` : ''}${how}${acts}</section>`;
  }

  function props() {
    const ps = (S.data && S.data.proposals) || {}, l = later();
    $('wl-props').innerHTML = Object.entries(ps).filter(([nid, p]) => l[nid] !== p.since).map(([nid, p]) => propCard(nid, p)).join('');
  }

  async function apply(nid) {
    const p = ((S.data && S.data.proposals) || {})[nid];
    if (!p) return;
    const c = p.cutover || {}, air = (p.recut || []).filter(v => (byVmid()[v] || {}).on_air);
    const msg = (c.mode === 'restart_in_place' ? T('section.walls.confirm_in_place', { w: names(p.changed || []) }) : T('section.walls.confirm_prepare'))
      + (air.length ? ' ' + T('section.walls.confirm_on_air', { w: names(air) }) : '');
    const ok = await mxlConfirm({ title: T('section.walls.validate'), message: msg, confirmLabel: T('section.walls.validate'), danger: c.mode === 'restart_in_place' });
    if (!ok) return;
    const r = await fetch('/api/fabric/proposals/' + encodeURIComponent(nid) + '/apply', { method: 'POST' });
    if (!r.ok) await mxlConfirm({ alert: true, title: T('section.walls.validate'), message: T('section.walls.apply_failed') });
    load();
  }

  async function setSaturation(vmid, mode) {
    const r = await fetch('/api/fabric/walls/' + vmid + '/saturation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) });
    if (!r.ok) await mxlConfirm({ alert: true, title: T('section.walls.col_saturation'), message: T('section.walls.apply_failed') });
    load();
  }

  async function load() {
    try {
      const r = await fetch('/api/fabric/walls');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      S.data = await r.json();
    } catch (e) {
      $('wl-rows').innerHTML = `<tr><td colspan="7" class="wl-empty">${esc(T('section.walls.load_failed'))}</td></tr>`;
      return;
    }
    props();
    // A select being used is not redrawn under the finger; the next refresh does it.
    if (!(document.activeElement && document.activeElement.closest && document.activeElement.closest('#wl-rows'))) rows();
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('#wl-embed [data-f], #wl-embed [data-apply], #wl-embed [data-later]');
    if (!b) return;
    if (b.dataset.f) { S.filter = b.dataset.f; rows(); }
    else if (b.dataset.apply) apply(b.dataset.apply);
    else if (b.dataset.later) { const l = later(); l[b.dataset.later] = ((S.data.proposals || {})[b.dataset.later] || {}).since; setLater(l); props(); }
  });
  document.addEventListener('change', e => {
    const s = e.target.closest('#wl-embed select[data-sat]');
    if (s) setSaturation(s.dataset.sat, s.value);
  });

  window.wallsLoad = function () { load(); if (!S.timer) S.timer = setInterval(load, 5000); };
  window.wallsStop = function () { if (S.timer) { clearInterval(S.timer); S.timer = null; } };
})();
