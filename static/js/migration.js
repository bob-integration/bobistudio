// SPDX-License-Identifier: GPL-3.0-or-later
// « Migrer vers un autre nœud » — moved out of static/scripts.js on 2026-11-07 so that the plugin
// pages (which do not load scripts.js) can offer it from the container settings panel. Loaded by
// templates/layout.html, with its dialog (templates/migration.html). Same code, same globals
// (`ouvrirMigration`, `fermerMigration`): the Containers page calls them as before.

// Local escaping: scripts.js (and its escapeHtml) is not loaded on every page.
function _migEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ─── Migration d'un conteneur vers un autre nœud ─────────────────────────────
   La modale montre la SIMULATION avant tout : verdict par nœud, vérifications, et
   conséquences pour les voisins (flux qui deviennent distants ou locaux). La bascule
   n'est proposée qu'ensuite, par nœud, et demande une confirmation nommée — c'est une
   opération destructive avec coupure, elle ne doit pas partir au premier clic. */
let _migVmid = null, _migHote = '';

function ouvrirMigration(vmid, hostname) {
    _migVmid = vmid; _migHote = hostname || String(vmid);
    document.getElementById('migration-hote').textContent = _migHote;
    document.getElementById('migration-corps').innerHTML =
        `<p style="color:var(--text-muted)">${window.t('migration.loading')}</p>`;
    document.getElementById('migration-modal').style.display = 'flex';
    fetch(`/api/containers/${vmid}/migration/simuler`)
        .then(r => r.json())
        .then(d => _migRendre(d.candidats || []))
        .catch(e => {
            document.getElementById('migration-corps').innerHTML =
                `<p class="migration-echec">${_migEsc(String(e))}</p>`;
        });
}

function fermerMigration() {
    document.getElementById('migration-modal').style.display = 'none';
    _migVmid = null;
}

function _migRendre(candidats) {
    if (!candidats.length) {
        document.getElementById('migration-corps').innerHTML =
            `<p style="color:var(--text-muted)">—</p>`;
        return;
    }
    const html = candidats.map(p => {
        const nom = _migEsc(p.node_cible_nom || String(p.node_cible));
        // Le nœud OÙ IL EST DÉJÀ : pas une destination, mais la référence de la comparaison —
        // il montre ce que le départ du conteneur lui rendrait. Ni bouton ni mention d'échec :
        // ce n'est pas un refus, c'est le point de départ.
        if (p.est_source) {
            return `<div class="migration-noeud migration-noeud-source">
                <div class="migration-noeud-tete"><strong>${nom}</strong>
                    <span class="migration-ici">${window.t('migration.current_node')}</span></div>
                ${_migJauges(p.ressources, 'libere')}
            </div>`;
        }
        // Un refus de TYPE est définitif : pas de bouton du tout, et le motif en clair —
        // un refus sans motif se lit comme une limitation arbitraire.
        if ((p.refus || []).length) {
            return `<div class="migration-noeud migration-noeud-refus">
                <div class="migration-noeud-tete"><strong>${nom}</strong>
                    <span class="migration-verdict-ko">${window.t('migration.refused')}</span></div>
                ${p.refus.map(r => `<div class="migration-motif">${_migEsc(r)}</div>`).join('')}
            </div>`;
        }
        const verifs = (p.verifications || []).map(v =>
            `<div class="migration-verif ${v.ok ? 'ok' : 'ko'}">
                <span>${v.ok ? '✓' : '✗'}</span>
                <span>${_migEsc(v.nom)}</span>
                <span class="migration-detail">${_migEsc(v.detail || '')}</span>
             </div>`).join('');
        const jauges = _migJauges(p.ressources, 'prend');
        const conseq = (p.consequences || []).length
            ? `<div class="migration-conseq"><strong>${window.t('migration.consequences')}</strong>` +
              p.consequences.map(c =>
                  `<div>${_migEsc(c.hostname || '')} (${c.vmid}) — ${_migEsc((c.flux || []).join(', '))}
                    → ${_migEsc(c.effet || '')}</div>`).join('') + `</div>`
            : '';
        // Vérifications en échec → on n'offre que « passer outre », explicitement nommé.
        const bouton = p.ok
            ? `<button class="btn btn-blue" onclick="_migLancer(${p.node_cible}, '${nom}', false)">${window.t('migration.go')}</button>`
            : `<button class="btn btn-orange" title="${window.t('migration.force_title')}"
                       onclick="_migLancer(${p.node_cible}, '${nom}', true)">${window.t('migration.force')}</button>`;
        return `<div class="migration-noeud">
            <div class="migration-noeud-tete"><strong>${nom}</strong>${bouton}</div>
            ${verifs}${jauges}${conseq}
        </div>`;
    }).join('');
    document.getElementById('migration-corps').innerHTML =
        html + `<p class="migration-conserve">${window.t('migration.keeps')}</p>`;
}

function _migJauge(r, libelle, mode) {
    // Deux barres pour la MÊME ressource du MÊME nœud : son état actuel, puis ce qu'il deviendrait.
    //   mode 'prend'  → la seconde barre ajoute, hachurée, la part que le conteneur occuperait ;
    //   mode 'libere' → elle retranche ce qu'il rendrait en partant (nœud d'origine).
    // La part se dessine à la frontière du plein, donc à sa place réelle sur l'échelle du nœud.
    if (!r || !r.total) return '';
    const total = r.total, libre = (r.libre == null) ? null : r.libre;
    const pris  = (libre == null) ? null : Math.max(0, total - libre);
    const libere = (mode === 'libere') ? Math.max(0, r.libere || 0) : 0;
    const part   = (mode === 'prend')  ? Math.max(0, r.requis || 0) : 0;
    const trop = part > 0 && libre != null && part > libre;
    const fmt = (v) => (r.unite === 'Mo' ? (v / 1024).toFixed(1) + ' Go' : v + ' ' + (r.unite || ''));
    const pct = (v) => Math.max(0, Math.min(100, v / total * 100)).toFixed(1);
    // Après : le plein retranche ce qui part, la hachure porte le delta (pris ou rendu).
    const pleinApres = (pris == null) ? 0 : Math.max(0, pris - libere);
    const libreApres = (libre == null) ? null : Math.max(0, libre - part + libere);
    const delta = libere ? `<span class="mig-gain">−${fmt(libere)}</span>`
                : part   ? `<span class="${trop ? 'mig-trop' : ''}">+${fmt(part)}</span>` : '';
    const barre = (pleinPct, hachPct, cls) => `<div class="mig-barre">
            <div class="mig-barre-pris" style="width:${pleinPct}%"></div>
            ${hachPct > 0 ? `<div class="mig-barre-part ${cls}" style="width:${hachPct}%"></div>` : ''}
        </div>`;
    return `<div class="mig-jauge">
        <div class="mig-jauge-tete"><span>${_migEsc(libelle)}</span>
            <span class="mig-jauge-chiffres">${libre == null ? '—' : fmt(libre)} ${window.t('migration.free')}${delta ? ' · ' + delta : ''}</span></div>
        <div class="mig-duo">
            <div class="mig-duo-col"><span class="mig-duo-lbl">${window.t('migration.now')}</span>
                ${barre(pct(pris == null ? 0 : pris), 0, '')}</div>
            <div class="mig-duo-col"><span class="mig-duo-lbl">${window.t('migration.after')}</span>
                ${barre(pct(pleinApres), Math.min(100 - pct(pleinApres), Number(pct(part + libere))),
                        trop ? 'mig-barre-trop' : (libere ? 'mig-barre-gain' : ''))}</div>
        </div>
        <div class="mig-apres">${libreApres == null ? '' :
            window.t('migration.after_free').replace('{v}', fmt(libreApres))}</div>
    </div>`;
}

function _migProc(cpu) {
    // Le modèle de processeur, sa fréquence et son nombre de cœurs. « Plus de cœurs libres » ne
    // veut pas dire « plus puissant » : deux nœuds peuvent offrir le même nombre de cœurs sans
    // jouer dans la même catégorie. Les cœurs PHYSIQUES sont donnés à côté des threads — un
    // E5-2699 v4 annonce 88 threads pour 44 cœurs, et confondre les deux fait croire à un nœud
    // deux fois plus capable qu'il n'est.
    const p = cpu && cpu.proc;
    if (!p || !p.modele) return '';
    const bouts = [];
    if (p.physiques) bouts.push(window.t('migration.cpu_cores').replace('{n}', p.physiques));
    else if (p.threads) bouts.push(window.t('migration.cpu_threads').replace('{n}', p.threads));
    if (p.physiques && p.threads && p.threads > p.physiques)
        bouts.push(window.t('migration.cpu_threads').replace('{n}', p.threads));
    if (p.ghz) bouts.push(p.ghz.toFixed(2).replace(/0$/, '') + ' GHz');
    return `<div class="mig-proc" title="${_migEsc(p.modele)}">
        <span class="mig-proc-nom">${_migEsc(p.modele)}</span>
        <span class="mig-proc-det">${bouts.join(' · ')}</span>
    </div>`;
}

function _migJauges(res, mode) {
    if (!res) return '';
    return `<div class="mig-jauges-uni">
        ${_migProc(res.cpu)}
        ${_migJauge(res.cpu, window.t('migration.res_cpu'), mode)}
        ${_migJauge(res.ram, window.t('migration.res_ram'), mode)}
        ${_migGpu(res.gpu, mode)}
    </div>`;
}

function _migGpu(g, mode) {
    // Le GPU ne se RÉSERVE pas : aucune part n'y est dessinée. Ce qui change en migrant, c'est le
    // nombre de CLIENTS CUDA — mesuré sur ce parc : un mur seul consomme 16 % du GPU, trois murs
    // 40 %. C'est le compte AVANT → APRÈS qui informe, la VRAM libre n'ayant jamais annoncé une
    // cadence.
    if (!g) return '';
    const nom = g.nom ? _migEsc(g.nom) : window.t('migration.res_gpu_none');
    if (!g.total) {
        return `<div class="mig-jauge"><div class="mig-jauge-tete"><span>${window.t('migration.res_gpu')}</span>
            <span class="mig-jauge-chiffres mig-gpu-absent">${nom}</span></div></div>`;
    }
    const n0 = g.clients;
    const n1 = (n0 == null) ? null : (mode === 'libere' ? Math.max(0, n0 - 1) : n0 + 1);
    const pctVram = Math.min(100, (g.total - (g.libre ?? g.total)) / g.total * 100).toFixed(1);
    return `<div class="mig-jauge">
        <div class="mig-jauge-tete"><span>${window.t('migration.res_gpu')}</span>
            <span class="mig-jauge-chiffres">${nom}</span></div>
        <div class="mig-barre" title="${_migEsc(g.note || '')}">
            <div class="mig-barre-pris" style="width:${pctVram}%"></div>
        </div>
        <div class="mig-gpu-detail" title="${_migEsc(g.note || '')}">
            ${n0 == null ? '—' : `${window.t('migration.res_gpu_clients').replace('{n}', n0)} → <span class="${
                mode === 'libere' ? 'mig-gain' : ''}">${n1}</span>`}${
            g.util_pct != null ? ` · ${Math.round(g.util_pct)} %` : ''}
        </div>
    </div>`;
}

function _migLancer(nodeId, nom, forcer) {
    if (!confirm(window.t('migration.confirm').replace('{h}', _migHote).replace('{n}', nom))) return;
    const corps = document.getElementById('migration-corps');
    corps.innerHTML = `<p>${window.t('migration.running')}</p>`;
    fetch(`/api/containers/${_migVmid}/migration`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node_id: nodeId, forcer: !!forcer})
    }).then(r => r.json().then(d => ({ok: r.ok, d})))
      .then(({ok, d}) => {
        const etapes = (d.etapes || []).map(e =>
            `<div class="migration-verif ${e.ok ? 'ok' : 'ko'}"><span>${e.ok ? '✓' : '✗'}</span>
             <span>${_migEsc(e.etape)}</span>
             <span class="migration-detail">${_migEsc(e.detail || '')}</span></div>`).join('');
        corps.innerHTML =
            `<p class="${d.ok ? 'migration-ok' : 'migration-echec'}">
                ${d.ok ? window.t('migration.done') : window.t('migration.failed')}</p>` +
            (d.erreur ? `<div class="migration-motif">${_migEsc(d.erreur)}</div>` : '') + etapes;
        // La liste des cartes se rafraîchit d'elle-même toutes les 5 s (rafraichirDonnees) ;
        // on la relance tout de suite pour que le conteneur apparaisse sur son nouveau nœud.
        if (d.ok && typeof rafraichirDonnees === 'function') rafraichirDonnees();
      })
      .catch(e => { corps.innerHTML = `<p class="migration-echec">${_migEsc(String(e))}</p>`; });
}
