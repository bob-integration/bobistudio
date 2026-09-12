// ─── Moniteur de ressources CPU (étalonnage) ──────────────────────────────────────────────────
// Brique PARTAGÉE par l'onglet Ressources (templates/containers.html, chargé via scripts.js) et
// le widget compact des pages plugin (templates/plugin_section.html, où scripts.js n'est PAS
// chargé — d'où ce fichier autonome, sans dépendance à scripts.js). Chargé globalement par
// templates/layout.html comme num_stepper.js/bobi_fonts.js.
//
// Trois quantités sur UN SEUL axe (% d'un CPU, 100 = 1 cœur) : fond de barre = reserve_pct
// (réservé), remplissage = consomme_pct (live), repère = mesure.max (pic étalonné). La couleur
// compare le consommé à L'INTENTION mesurée, jamais à un idéal absolu — cf. app/etalonnage.py.
(function (global) {
    'use strict';

    const REFRESH_MS = 5000;   // les métriques sous-jacentes ne bougent pas plus vite (cf. spec)

    // window.t est toujours défini (layout.html) ; garde-fou minimal si jamais absent.
    function tr(key) { return (typeof window !== 'undefined' && window.t) ? window.t(key) : key; }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function fmtPct(v) { return (v == null) ? '—' : (Math.round(v * 10) / 10) + ' %'; }
    // Unité affichée : des CŒURS CPU, pas un pourcentage. « 152 % » ne dit pas de quoi — de la
    // machine ? d'un cœur ? du GPU ? — alors que « 1,5 cœur CPU » est sans ambiguïté, et que c'est
    // l'unité dans laquelle on réserve. Le pourcentage brut reste dans l'infobulle.
    function fmtCores(v) {
        if (v == null) return '—';
        const n = v / 100;
        const v2 = n >= 10 ? Math.round(n) : Math.round(n * 10) / 10;
        // Séparateur décimal selon la locale : une interface française affichant « 8.3 » détonne.
        return v2.toLocaleString(document.documentElement.lang || undefined)
            + ' ' + tr('containers.ressources.unit_cpu');
    }
    function fmtChrono(sSec) {
        sSec = Math.max(0, Math.floor(sSec));
        const h = Math.floor(sSec / 3600), m = Math.floor((sSec % 3600) / 60), s = sSec % 60;
        const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
        return h > 0 ? (h + ':' + mm + ':' + ss) : (mm + ':' + ss);
    }

    function canDeploy() {
        if (typeof PS_CAN_DEPLOY !== 'undefined') return !!PS_CAN_DEPLOY;
        if (typeof RES_CAN_DEPLOY !== 'undefined') return !!RES_CAN_DEPLOY;
        return false;
    }

    // ── Couleur : compare CONSOMMÉ à L'INTENTION (le pic mesuré), pas à un idéal ────────────────
    function fillClass(c) {
        const reserve = c.reserve_pct, consomme = c.consomme_pct;
        const peak = c.mesure && c.mesure.max;
        if (reserve != null && consomme != null && consomme >= 0.9 * reserve) return 'res-fill-danger';
        if (c.etat === 'perime') return 'res-fill-warn';   // la garantie serait FAUSSE : jamais vert
        if ((c.etat === 'etalonne' || c.etat === 'autre_noeud') && peak != null && consomme != null) {
            return consomme > peak ? 'res-fill-warn' : 'res-fill-ok';
        }
        return 'res-fill-neutral';   // non_etalonne sans réserve à l'approche : rien à comparer
    }

    function axisMax(c) {
        const peak = (c.mesure && c.mesure.max) || 0;
        return Math.max(c.reserve_pct || 0, c.consomme_pct || 0, peak, 100) * 1.08;
    }

    function peakLabel(c) {
        if (c.etat === 'perime') return tr('containers.ressources.peak_stale');
        if (c.etat === 'autre_noeud') return tr('containers.ressources.peak_estimate');
        return tr('containers.ressources.peak');
    }

    function guaranteeHtml(c) {
        if (!canDeploy()) {
            return '<div class="res-brick-hint">' + esc(tr('containers.ressources.calibrate_need_perm')) + '</div>';
        }
        const peak = c.mesure && c.mesure.max;
        const marge = 1.25;
        const defCores = peak != null ? Math.max(1, Math.ceil((peak * marge) / 100)) : 1;
        let hint = '';
        if (c.etat === 'non_etalonne') hint = tr('containers.ressources.guarantee_hint_none');
        else if (c.etat === 'perime') hint = tr('containers.ressources.guarantee_hint_stale');
        else if (c.etat === 'autre_noeud') hint = tr('containers.ressources.guarantee_hint_estimate');
        // Rouge « proche de la réserve » sur un conteneur qui tient son régime ÉTALONNÉ : ce n'est
        // pas une dérive, c'est une réserve sous-dimensionnée. Sans ce mot, l'alerte accuse le
        // conteneur — et une alarme qui désigne le mauvais coupable est pire qu'aucune alarme.
        else if (c.etat === 'etalonne' && c.reserve_pct != null && c.consomme_pct != null
                 && c.mesure && c.mesure.max != null
                 && c.consomme_pct >= 0.9 * c.reserve_pct && c.consomme_pct <= c.mesure.max)
            hint = tr('containers.ressources.guarantee_hint_tight');
        return '<div class="res-brick-actions">'
            + '<input type="number" class="res-cores-input" min="1" step="1" value="' + defCores + '" '
            + 'aria-label="' + esc(tr('containers.ressources.guarantee_cores')) + '" '
            + 'title="' + esc(tr('containers.ressources.guarantee_cores')) + '">'
            + '<button type="button" class="btn btn-purple" data-res-guarantee="' + c.vmid + '">'
            + esc(tr('containers.ressources.guarantee')) + '</button>'
            + '</div>'
            + (hint ? '<div class="res-brick-hint">' + esc(hint) + '</div>' : '')
            + pressionsHtml(c)
            + '<div class="res-brick-status" data-res-status="' + c.vmid + '"></div>';
    }

    // GPU : la barre principale ne parle QUE du CPU. Le GPU a sa propre pastille — avec sa mesure
    // quand la carte travaille, et le mot « non mesuré » quand elle est seulement attribuée. Ne
    // jamais afficher 0 % pour « pas de mesure » : ce serait affirmer que la carte est au repos.
    function gpuChip(c) {
        if (c.gpu_index == null) return '';
        const g = c.gpu_live;
        const nom = tr('containers.ressources.gpu_assigned').replace('{i}', c.gpu_index);
        if (!g) return '<span class="res-gpu-chip" title="' + esc(tr('containers.ressources.gpu_assigned_tip'))
            + '">' + esc(nom) + ' · ' + esc(tr('containers.ressources.gpu_idle')) + '</span>';
        const bouts = [tr('containers.ressources.gpu_sm') + ' ' + Math.round(g.sm) + ' %',
                       tr('containers.ressources.gpu_mem') + ' ' + Math.round(g.mem) + ' %'];
        if (g.enc > 0) bouts.push(tr('containers.ressources.gpu_enc') + ' ' + Math.round(g.enc) + ' %');
        if (g.dec > 0) bouts.push(tr('containers.ressources.gpu_dec') + ' ' + Math.round(g.dec) + ' %');
        return '<span class="res-gpu-chip res-gpu-actif" title="'
            + esc(tr('containers.ressources.gpu_live_tip')) + '">'
            + esc(nom) + ' · ' + esc(bouts.join(' · ')) + '</span>';
    }

    // Ressources de MACHINE sous tension. On ne les mesure pas ici — la page Monitoring le fait
    // déjà, par nœud et dans la durée. On SIGNALE, et on renvoie. Sans ça, un widget qui n'affiche
    // que le CPU dirait « tout va bien » pendant un effondrement de bande passante mémoire.
    function pressionsHtml(c) {
        const p = c.pressions;
        if (!p || !p.length) return '';
        const txt = p.map(x => x.kind === 'membw'
            ? tr('containers.ressources.press_membw').replace('{v}', Math.round(x.valeur || 0))
            : tr('containers.ressources.press_disk').replace('{v}', Math.round(x.valeur || 0))
                                                     .replace('{n}', x.ref || '')).join(' · ');
        const grave = p.some(x => x.niveau === 'error');
        return '<div class="res-brick-press' + (grave ? ' res-press-grave' : '') + '">⚠ ' + esc(txt)
            + ' <a href="/monitoring" class="res-press-lien">'
            + esc(tr('containers.ressources.press_link')) + '</a></div>';
    }

    function brickHtml(c, opts) {
        opts = opts || {};
        const max = axisMax(c);
        const reserveW = c.reserve_pct != null ? Math.min(100, c.reserve_pct / max * 100) : 0;
        const consommeW = c.consomme_pct != null ? Math.min(100, c.consomme_pct / max * 100) : 0;
        const peak = c.mesure && c.mesure.max;
        const markW = peak != null ? Math.min(100, peak / max * 100) : null;
        const ghost = (c.etat === 'perime' || c.etat === 'autre_noeud');
        const fc = fillClass(c);
        const noeud = c.noeud || {};
        const tight = (noeud.libres != null && noeud.physiques
            && noeud.libres <= Math.max(1, Math.round(noeud.physiques * 0.15)));
        const pLabel = peakLabel(c);
        const reserveTxt = c.reserve_pct != null ? fmtCores(c.reserve_pct) : tr('containers.ressources.unreserved');
        const titleParts = [
            tr('containers.ressources.reserved') + ' : ' + reserveTxt,
            tr('containers.ressources.consumed') + ' : ' + fmtCores(c.consomme_pct)
                + ' (' + fmtPct(c.consomme_pct) + ' ' + (window.t ? window.t('js.of_one_core') : 'of one core') + ')',
        ];
        if (peak != null) titleParts.push(pLabel + ' : ' + fmtCores(peak));
        if (c.gpu_index != null) titleParts.push(tr('containers.ressources.gpu_assigned_tip'));
        return '<div class="res-brick" data-vmid="' + c.vmid + '" data-type="' + esc(c.type || '') + '">'
            + '<div class="res-brick-head">'
            + '<span class="res-brick-name">' + esc(c.hostname || ('#' + c.vmid)) + ' <span class="meta">#' + c.vmid + '</span></span>'
            + '<span class="res-state res-state-' + esc(c.etat) + '">' + esc(tr('containers.ressources.state.' + c.etat) || c.etat) + '</span>'
            + '</div>'
            + '<div class="res-bar-wrap' + (c.reserve_pct == null ? ' res-bar-unreserved' : '') + '" title="' + esc(titleParts.join(' · ')) + '">'
            + (c.reserve_pct != null ? '<div class="res-bar-reserve" style="width:' + reserveW + '%"></div>' : '')
            + '<div class="res-bar-fill ' + fc + '" style="width:' + consommeW + '%"></div>'
            + (markW != null ? '<div class="res-bar-mark' + (ghost ? ' res-mark-ghost' : '') + '" style="left:' + markW + '%"></div>' : '')
            + '</div>'
            + '<div class="res-brick-meta">'
            + '<span>' + esc(tr('containers.ressources.reserved')) + ' : <b>' + esc(reserveTxt) + '</b></span>'
            + '<span>' + esc(tr('containers.ressources.consumed')) + ' : <b>' + fmtCores(c.consomme_pct) + '</b></span>'
            + '<span>' + esc(pLabel) + ' : <b>' + (peak != null ? fmtCores(peak) : tr('containers.ressources.peak_none')) + '</b></span>'
            // Un GPU ATTRIBUÉ n'est pas un GPU mesuré : la barre ne parle que du CPU. Le dire ici,
            // sinon on laisse croire qu'elle couvre les deux ressources.
            + gpuChip(c)
            + '</div>'
            + (noeud.physiques != null
                ? '<div class="res-brick-node' + (tight ? ' res-node-tight' : '') + '">'
                + esc(tr('containers.ressources.node_capacity')
                    .replace('{libres}', noeud.libres).replace('{physiques}', noeud.physiques))
                + '</div>' : '')
            + (opts.showActions ? guaranteeHtml(c) : '')
            + '</div>';
    }

    // Câble les boutons « Garantir » d'un conteneur de bricks fraîchement injecté.
    function wireGuarantees(root) {
        root.querySelectorAll('[data-res-guarantee]').forEach(btn => {
            btn.onclick = async () => {
                const vmid = btn.dataset.resGuarantee;
                const brick = btn.closest('.res-brick');
                const input = brick.querySelector('.res-cores-input');
                const status = brick.querySelector('[data-res-status="' + vmid + '"]');
                const coeurs = parseInt(input.value, 10) || 1;
                btn.disabled = true;
                if (status) { status.className = 'res-brick-status'; status.textContent = tr('containers.ressources.guaranteeing'); }
                try {
                    const r = await fetch('/api/etalonnage/garantir', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ vmid: parseInt(vmid, 10), coeurs }),
                    });
                    const j = await r.json().catch(() => ({}));
                    if (!r.ok || !j.ok) {
                        if (status) { status.className = 'res-brick-status err'; status.textContent = '✕ ' + (j.error || r.statusText); }
                    } else if (status) {
                        status.className = 'res-brick-status ok';
                        status.textContent = tr('containers.ressources.guarantee_ok').replace('{coeurs}', j.coeurs);
                    }
                } catch (e) {
                    if (status) { status.className = 'res-brick-status err'; status.textContent = tr('containers.ressources.guarantee_error'); }
                } finally {
                    btn.disabled = false;
                }
            };
        });
    }

    async function fetchRessources(params) {
        const qs = new URLSearchParams();
        if (params && params.type) qs.set('type', params.type);
        if (params && params.node_id) qs.set('node_id', params.node_id);
        if (params && params.vmid) qs.set('vmid', params.vmid);
        const url = '/api/ressources' + (qs.toString() ? '?' + qs.toString() : '');
        try {
            const r = await fetch(url);
            if (!r.ok) return [];
            const j = await r.json();
            return j.conteneurs || [];
        } catch (e) { return []; }
    }

    async function fetchState() {
        try {
            const r = await fetch('/api/etalonnage/state');
            return await r.json();
        } catch (e) { return { etat: 'aucune' }; }
    }

    global.MXLRessources = {
        REFRESH_MS, esc, fmtPct, fmtCores, fmtChrono, gpuChip, canDeploy, brickHtml, wireGuarantees,
        fetchRessources, fetchState,
    };
})(window);
