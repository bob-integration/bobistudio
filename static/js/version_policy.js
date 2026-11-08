/* Éditeur des règles de conservation des versions automatiques de projet (07/11).
 *
 * Réglages → Système → Projets. Le moteur est côté serveur (app/project_versions.py) : cet éditeur
 * compose la règle et MONTRE ce qu'elle fait avant qu'on l'enregistre :
 *   · une frise de l'âge des versions (maintenant → plus ancien), découpée en tranches ;
 *   · un tableau des tranches qui se lit comme une phrase (de quand à quand → ce qu'on garde → au plus) ;
 *   · un résumé en clair de la règle entière ;
 *   · son effet sur les projets actuels (/preview : aujourd'hui → après → supprimées).
 * L'enregistrement l'applique aussitôt à tous les projets, et une version supprimée ne revient pas :
 * une confirmation redit combien. Les versions nommées ne sont jamais concernées.
 * (Première version, même jour : une ligne de champs « Jusqu'à … → … », jugée illisible à l'essai.)
 *
 *   VersionPolicy.mount(hote)
 */
window.VersionPolicy = (function () {
  const T = (k) => (window.t ? window.t(k) : k);
  const E = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const UNITS = [['h', 1], ['d', 24], ['w', 168]];
  const PERIOD_H = {hourly: 1, daily: 24, weekly: 168, monthly: 730};
  // L'exemple de la demande : 5 sur les dernières 24 h, puis une par jour, une par semaine, une par mois.
  const EXAMPLE = {delay_s: 10, max: 0, tiers: [
    {upto_h: 24, keep: 'count', n: 5}, {upto_h: 24 * 7, keep: 'daily'},
    {upto_h: 24 * 7 * 8, keep: 'weekly'}, {upto_h: null, keep: 'monthly'}]};

  function unitOf(h) {
    if (h % 168 === 0) return ['w', h / 168];
    if (h % 24 === 0) return ['d', h / 24];
    return ['h', h];
  }
  function ageText(h) {
    const [u, n] = unitOf(h);
    return T('settings.vpol.age_' + u).replace('{n}', n);
  }
  // Bornes de chaque tranche : [début, fin] en heures (fin null = au-delà).
  function spans(tiers) {
    let a = 0;
    return tiers.map((t) => { const s = [a, t.upto_h]; if (t.upto_h !== null) a = t.upto_h; return s; });
  }
  function spanText([a, b]) {
    if (b === null) return T('settings.vpol.sp_beyond').replace('{a}', ageText(a));
    if (!a) return T('settings.vpol.sp_first').replace('{b}', ageText(b));
    return T('settings.vpol.sp_mid').replace('{a}', ageText(a)).replace('{b}', ageText(b));
  }
  // Au plus combien de versions une tranche garde (Infinity = sans limite).
  function most(t, [a, b]) {
    if (t.keep === 'none') return 0;
    if (t.keep === 'count') return t.n || 0;
    if (t.keep === 'all' || b === null) return Infinity;
    return Math.ceil((b - a) / PERIOD_H[t.keep]);
  }
  function keepText(t) {
    return t.keep === 'count' ? T('settings.vpol.phr_count').replace('{n}', t.n || 0) : T('settings.vpol.phr_' + t.keep);
  }

  function mount(host) {
    let pol = null, def = null, modes = [], timer = null, seq = 0;

    function timeline() {
      const tiers = pol.tiers, sp = spans(tiers);
      const open = tiers.length && tiers[tiers.length - 1].upto_h === null;
      // Échelle logarithmique : une heure et huit semaines tiennent sur la même frise.
      const w = (a, b) => b === null ? 0.9 : Math.max(0.15, Math.log10(1 + b) - Math.log10(1 + a));
      const segs = tiers.length ? tiers.map((t, i) =>
        `<div class="vpol-seg vpol-seg-${t.keep}" style="flex-grow:${w(...sp[i]).toFixed(3)}" title="${E(spanText(sp[i]) + ' : ' + keepText(t))}">
           <span>${E(keepText(t))}</span>${sp[i][1] !== null ? `<em>${E(ageText(sp[i][1]))}</em>` : ''}</div>`).join('')
        : `<div class="vpol-seg vpol-seg-all" style="flex-grow:1"><span>${E(T('settings.vpol.phr_all'))}</span></div>`;
      const tail = tiers.length && !open
        ? `<div class="vpol-seg vpol-seg-drop" style="flex-grow:0.6"><span>${E(T('settings.vpol.dropped'))}</span></div>` : '';
      return `<div class="vpol-line" aria-hidden="true">${segs}${tail}</div>
        <div class="vpol-line-ends meta"><span>${E(T('settings.vpol.now'))}</span><span>${E(T('settings.vpol.older'))} →</span></div>`;
    }

    function summary() {
      const tiers = pol.tiers, sp = spans(tiers);
      let total = tiers.length ? tiers.reduce((s, t, i) => s + most(t, sp[i]), 0) : Infinity;
      if (pol.max) total = Math.min(total, pol.max);
      const parts = tiers.length
        ? tiers.map((t, i) => `${keepText(t)} ${spanText(sp[i])}`).join(' ; ')
        : T('settings.vpol.phr_all');
      const open = tiers.length && tiers[tiers.length - 1].upto_h === null;
      const drop = tiers.length && !open ? ' ' + T('settings.vpol.sum_drop').replace('{a}', ageText(sp[sp.length - 1][1])) : '';
      const cap = total === Infinity ? T('settings.vpol.sum_nolimit') : T('settings.vpol.sum_most').replace('{n}', total);
      return T('settings.vpol.sum_head').replace('{parts}', parts) + '.' + drop + ' ' + cap;
    }

    function rowHtml(t, i, sp) {
      const open = t.upto_h === null;
      const [u, n] = open ? ['d', ''] : unitOf(t.upto_h);
      const m = most(t, sp);
      return `<tr data-i="${i}">
        <td class="vpol-age">${E(spanText(sp).replace(/^./, (c) => c.toUpperCase()))}
          ${open ? '' : `<span class="vpol-upto">${E(T('settings.vpol.upto'))}
            <input type="number" class="vpol-num" min="1" step="1" value="${n}" data-f="n_age" aria-label="${E(T('settings.vpol.upto'))}">
            <select class="ctl-select" data-f="unit" aria-label="${E(T('settings.vpol.unit'))}">${UNITS.map(([k]) =>
              `<option value="${k}"${k === u ? ' selected' : ''}>${E(T('settings.vpol.unit_' + k))}</option>`).join('')}</select></span>`}</td>
        <td class="vpol-keep"><select class="ctl-select" data-f="keep" aria-label="${E(T('settings.vpol.keep'))}">${modes.map((md) =>
            `<option value="${md}"${md === t.keep ? ' selected' : ''}>${E(T('settings.vpol.mode_' + md))}</option>`).join('')}</select>
          ${t.keep === 'count' ? `<input type="number" class="vpol-num" min="1" step="1" value="${t.n || 5}" data-f="n" aria-label="${E(T('settings.vpol.count_n'))}">` : ''}</td>
        <td class="vpol-most">${m === Infinity ? E(T('settings.vpol.most_inf')) : m}</td>
        <td><button type="button" class="btn btn-sm" data-f="del" aria-label="${E(T('settings.vpol.remove_tier'))}">✕</button></td>
      </tr>`;
    }

    function draw() {
      const tiers = pol.tiers, sp = spans(tiers);
      const open = tiers.length && tiers[tiers.length - 1].upto_h === null;
      host.innerHTML = `<div class="vpol">
        <div class="vpol-top">
          <label class="vpol-field">${E(T('settings.vpol.delay'))}
            <input type="number" class="vpol-num" id="vpol-delay" min="2" max="3600" value="${pol.delay_s}"></label>
          <label class="vpol-field">${E(T('settings.vpol.max'))}
            <input type="number" class="vpol-num" id="vpol-max" min="0" max="10000" value="${pol.max}"></label>
        </div>
        <h3 class="vpol-h">${E(T('settings.vpol.rules'))}</h3>
        ${timeline()}
        <p class="vpol-summary">${E(summary())}</p>
        ${tiers.length ? `<table class="vpol-table">
          <thead><tr><th>${E(T('settings.vpol.col_age'))}</th><th>${E(T('settings.vpol.col_keep'))}</th>
            <th>${E(T('settings.vpol.col_most'))}</th><th></th></tr></thead>
          <tbody>${tiers.map((t, i) => rowHtml(t, i, sp[i])).join('')}</tbody></table>`
          : `<p class="meta">${E(T('settings.vpol.no_tier'))}</p>`}
        <div class="vpol-actions">
          <button type="button" class="btn btn-sm" data-a="add">+ ${E(T('settings.vpol.add_tier'))}</button>
          ${open ? '' : `<button type="button" class="btn btn-sm" data-a="open">+ ${E(T('settings.vpol.add_beyond'))}</button>`}
          <span class="vpol-presets"><span class="meta">${E(T('settings.vpol.presets'))}</span>
            <button type="button" class="btn-text-action" data-a="default">${E(T('settings.vpol.preset_default'))}</button>
            <button type="button" class="btn-text-action" data-a="example">${E(T('settings.vpol.preset_example'))}</button></span>
        </div>
        <h3 class="vpol-h">${E(T('settings.vpol.prev_title'))}</h3>
        <div class="vpol-preview" aria-live="polite"><p class="meta">…</p></div>
        <div class="vpol-save"><button type="button" class="btn btn-blue" data-a="save">${E(T('settings.vpol.save'))}</button></div>
      </div>`;
      wire();
      schedulePreview();
    }

    function wire() {
      host.querySelector('#vpol-delay').onchange = (e) => { pol.delay_s = +e.target.value; schedulePreview(); };
      host.querySelector('#vpol-max').onchange = (e) => { pol.max = +e.target.value; draw(); };
      host.querySelectorAll('tr[data-i]').forEach((row) => {
        const t = pol.tiers[+row.dataset.i];
        const f = (k) => row.querySelector(`[data-f="${k}"]`);
        const age = () => { t.upto_h = (+f('n_age').value || 1) * UNITS.find(([k]) => k === f('unit').value)[1]; draw(); };
        if (f('n_age')) { f('n_age').onchange = age; f('unit').onchange = age; }
        f('keep').onchange = (e) => { t.keep = e.target.value; if (t.keep === 'count' && !t.n) t.n = 5; draw(); };
        if (f('n')) f('n').onchange = (e) => { t.n = +e.target.value; draw(); };
        f('del').onclick = () => { pol.tiers.splice(+row.dataset.i, 1); draw(); };
      });
      host.querySelector('[data-a="add"]').onclick = () => {
        const bounded = pol.tiers.filter((t) => t.upto_h !== null);
        const prev = bounded.length ? bounded[bounded.length - 1].upto_h : 0;
        const t = {upto_h: prev ? prev * 4 : 24, keep: prev ? 'daily' : 'count', n: 5};
        const at = pol.tiers.length && pol.tiers[pol.tiers.length - 1].upto_h === null ? pol.tiers.length - 1 : pol.tiers.length;
        pol.tiers.splice(at, 0, t);
        draw();
      };
      const open = host.querySelector('[data-a="open"]');
      if (open) open.onclick = () => { pol.tiers.push({upto_h: null, keep: 'monthly'}); draw(); };
      host.querySelector('[data-a="example"]').onclick = () => { pol = Object.assign(JSON.parse(JSON.stringify(EXAMPLE)), {delay_s: pol.delay_s}); draw(); };
      host.querySelector('[data-a="default"]').onclick = () => { pol = JSON.parse(JSON.stringify(def)); draw(); };
      host.querySelector('[data-a="save"]').onclick = save;
    }

    function schedulePreview() {
      clearTimeout(timer);
      timer = setTimeout(previewNow, 250);
    }
    async function fetchPreview() {
      const r = await fetch('/api/project_versions/preview', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({policy: pol})});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || r.statusText);
      return j;
    }
    async function previewNow() {
      const out = host.querySelector('.vpol-preview');
      const mine = ++seq;
      try {
        const j = await fetchPreview();
        if (mine !== seq) return;
        const rows = j.projects.filter((p) => p.auto);
        out.innerHTML = (j.delete
          ? `<p class="vpol-warn">${E(T('settings.vpol.preview_del').replace('{n}', j.delete))}</p>`
          : `<p class="meta">${E(T('settings.vpol.preview_none'))}</p>`) +
          (rows.length ? `<table class="vpol-table vpol-prev"><thead><tr><th>${E(T('settings.vpol.col_project'))}</th>
              <th>${E(T('settings.vpol.col_now'))}</th><th>${E(T('settings.vpol.col_after'))}</th><th>${E(T('settings.vpol.col_deleted'))}</th></tr></thead>
            <tbody>${rows.map((p) => `<tr><td>${E(p.name)}</td><td>${p.auto}</td><td>${p.auto - p.delete}</td>
              <td class="${p.delete ? 'vpol-warn' : 'meta'}">${p.delete || '—'}</td></tr>`).join('')}</tbody></table>` : '');
      } catch (e) {
        if (mine !== seq) return;
        out.innerHTML = `<p class="vpol-warn">${E(T('settings.vpol.invalid').replace('{e}', e.message))}</p>`;
      }
    }

    async function save() {
      let j;
      try { j = await fetchPreview(); } catch (e) { mxlToast(T('settings.vpol.invalid').replace('{e}', e.message), 'error'); return; }
      // ⚠ CE QUI EST SUPPRIMÉ NE REVIENT PAS : on dit combien, et on demande.
      if (j.delete && !(await mxlConfirm({message: T('settings.vpol.confirm').replace('{n}', j.delete), danger: true}))) return;
      const r = await fetch('/api/project_versions/policy', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({policy: pol})});
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { mxlToast(out.error || r.statusText, 'error'); return; }
      pol = out.policy;
      mxlToast(T('settings.vpol.saved').replace('{n}', out.deleted || 0), 'success');
      draw();
    }

    (async function load() {
      const r = await fetch('/api/project_versions/policy');
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { host.textContent = j.error || r.statusText; return; }
      pol = j.policy; def = j.default; modes = j.modes || [];
      draw();
    })();
  }

  return {mount: mount, EXAMPLE: EXAMPLE, _spans: spans, _most: most};
})();

// Mounts itself on the Settings → System → Projects editor: no inline call in the page. That call
// ran before anything checked this file was there, and a bench that runs the page's inline scripts
// without it crashed on it (verif_maj_coeur, 08/11).
if (typeof document !== 'undefined') {
  const host = document.getElementById('vpol-editor');
  if (host) window.VersionPolicy.mount(host);
}
