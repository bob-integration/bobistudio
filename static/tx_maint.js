/* SPDX-License-Identifier: GPL-3.0-or-later
 * Fenêtre de maintenance TX (docs/reference/TX_LAYOUTS.md étage 2) — modale partagée.
 * Chargé par layout.html sur TOUTES les pages : Destinations 2110 (plugin_section.html) et Câbles
 * (cables.html) NE chargent PAS scripts.js (réservé à containers.html/projects.html) — y définir
 * cette modale l'aurait rendue `undefined` précisément là où le garde-fou sert.
 * N'utilise AUCUN helper global (escape local) pour la même raison.
 */
(function () {
  const escapeHtml = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

window.txMaintConfirm = function (verdict, opts) {
    opts = opts || {};
    const t = (k, fb) => (window.t ? (window.t(k) || fb) : fb);
    return new Promise((resolve) => {
        const v = verdict || {};
        const vic = v.victims || [];
        const done = (r) => { ov.remove(); document.removeEventListener('keydown', onKey); resolve(r); };
        const onKey = (e) => { if (e.key === 'Escape') done(null); };
        const names = vic.map(x => `<span class="txm-victim">${escapeHtml(x.label)}</span>`).join('');
        // Le décompte des victimes est LA phrase qui compte : « N sorties actives figeront ~1 s ».
        const impact = vic.length
            ? t('js.txm.impact', 'Ses {n} sortie(s) active(s) figeront environ 1 seconde :')
                .replace('{n}', vic.length)
            : t('js.txm.impact_none', 'Aucune sortie n\'émet actuellement sur ce port : le recalage passera inaperçu.');
        const scope = v.scope === 'engine'
            ? t('js.txm.scope_engine', 'Cette action REDÉMARRE le moteur : tous les flux (RX et TX) seront coupés.')
            : t('js.txm.scope_port', 'Cette action recale l\'arbre TX du port {p} (nœud {n}).')
                .replace('{p}', (v.ports || []).join(', ') || v.iface || '?')
                .replace('{n}', v.node || '?');
        const ov = document.createElement('div');
        ov.className = 'modal-overlay txm-overlay';
        ov.innerHTML = `
          <div class="modal-card txm-card" role="alertdialog" aria-modal="true">
            <h2 class="txm-title">${t('js.txm.title', 'Action perturbatrice')}</h2>
            <p class="txm-scope">${escapeHtml(scope)}</p>
            <p class="txm-impact">${escapeHtml(impact)}</p>
            ${vic.length ? `<div class="txm-victims">${names}</div>` : ''}
            ${v.detail ? `<p class="meta txm-detail">${escapeHtml(v.detail)}</p>` : ''}
            <div class="txm-actions">
              ${opts.allowDefer ? `<button type="button" class="btn" id="txm-defer">${
                  t('js.txm.defer', 'Différer (bac de maintenance)')}</button>` : ''}
              <span style="flex:1"></span>
              <button type="button" class="btn" id="txm-cancel">${t('js.txm.cancel', 'Annuler')}</button>
              <button type="button" class="btn btn-danger" id="txm-now">${
                  t('js.txm.now', 'Appliquer maintenant')}</button>
            </div>
          </div>`;
        ov.addEventListener('click', (e) => { if (e.target === ov) done(null); });
        document.body.appendChild(ov);
        document.addEventListener('keydown', onKey);
        ov.querySelector('#txm-cancel').onclick = () => done(null);
        ov.querySelector('#txm-now').onclick = () => done('now');
        const d = ov.querySelector('#txm-defer');
        if (d) d.onclick = () => done('defer');
        ov.querySelector('#txm-now').focus();
    });
};
})();
