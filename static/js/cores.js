/* ═══ BobiCores : la fenêtre « Gérer les cœurs » d'un nœud ═══════════════════════════════════
 *
 * UNE source de données (`GET /api/nodes/<id>/cores`, cf. app/core_pool.py:vue_coeurs) et UN
 * rendu, partagés par les deux endroits qui en parlent : la fenêtre ouverte depuis la carte d'un
 * nœud (Monitoring) et la section « Cœurs du nœud » de Réglages → Nœuds. Deux rendus auraient
 * divergé — c'est déjà arrivé pour les cartes de cœurs de Monitoring, dessinées deux fois.
 *
 * ★ DEUX NOTIONS, JAMAIS CONFONDUES (arbitrage de Cyril, 2026-10-30) :
 *     la PLACE  — dédié / partagé / partagé plafonné à N : OÙ le conteneur s'exécute ;
 *     le POIDS  — Antenne / Normal / Arrière-plan / Batch : sa part de CPU SOUS SATURATION.
 *   Et ce que le poids ne fait pas, écrit dans la fenêtre plutôt que su de quelques-uns : il
 *   n'agit QUE sous saturation, entre conteneurs du même pool ; il ne fait rien sur un cœur
 *   dédié, et il ne protège pas du frère HyperThreading.
 *
 * Contrôles : catalogue partagé (`ctl-*`, BobiControls) — jamais redessinés ici.
 * Libellés : i18n `settings.cores.*` (préfixe déjà exporté au JS), jamais de texte en dur.
 */
(function () {
    'use strict';

    const T = (k, d) => {
        try { const v = window.t ? window.t(k) : null; if (v && v !== k) return v; } catch (e) { /* repli */ }
        return d !== undefined ? d : k;
    };
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const fmt = (k, vals) => {
        let s = T(k);
        for (const n in (vals || {})) s = s.split('{' + n + '}').join(vals[n]);
        return s;
    };
    const toast = (m, kind) => {
        if (typeof window.showToast === 'function') window.showToast(m, kind);
        else if (kind === 'error') alert(m);
    };

    // État d'une fenêtre ouverte : filtres, tri, cœur sélectionné. Jamais global — deux nœuds
    // peuvent être ouverts l'un après l'autre sans se contaminer.
    function nouvelEtat(id, nom) {
        return { id, nom, j: null, energie: null, q: '', prot: 'tous', group: 'aucun',
                 core: null, sort: 'nom', dir: 1, onglet: 'a', survol: null, occupe: false,
                 snap: null, snapBusy: false };
    }

    async function _json(url, opts) {
        const r = await fetch(url, opts);
        let j = {};
        try { j = await r.json(); } catch (e) { /* corps vide */ }
        return { ok: r.ok, code: r.status, j };
    }

    const charger = (id, force) => _json('/api/nodes/' + id + '/cores' + (force ? '?force=1' : ''));
    const chargerEnergie = (id, force) => _json('/api/nodes/' + id + '/energy' + (force ? '?force=1' : ''));

    // ─── Vocabulaire ────────────────────────────────────────────────────────────────────────
    const PLACES = { dedie: 'settings.cores.place.dedie', partage: 'settings.cores.place.partage',
                     partage_plafonne: 'settings.cores.place.partage_plafonne' };
    const PROTS = { protege: 'settings.cores.prot.protege', partie: 'settings.cores.prot.partie',
                    non: 'settings.cores.prot.non' };
    const TONS = { protege: 'ok', partie: 'warn', non: 'info' };

    const libellePlace = (l) => (l.place === 'partage_plafonne'
        ? fmt(PLACES.partage_plafonne, { n: l.plafond })
        : T(PLACES[l.place] || PLACES.partage));
    const libellePoids = (n) => T('settings.cores.weight.' + n, n);
    // ★ ONE notion since 2026-11-07: the level says the place (Dédié = reserved cores). Showing
    // « Réservé · Normal » named two settings where there is one. A shared container's CPU cap
    // is the only thing the level does not say: it is added.
    const libelleNiveau = (l) => libellePoids(l.poids.niveau)
        + (l.place === 'partage_plafonne' ? ' · ' + fmt('settings.cores.cap_n', { n: l.plafond }) : '');
    const raison = (l) => {
        const r = l.protection_raison || {};
        return r.cle ? fmt(r.cle, r) : '';
    };
    const lignes = (j) => (j ? (j.dedies || []).concat(j.partages || []) : []);
    // « Poids bas » = sous le niveau NORMAL, tel que le SERVEUR publie le barème. Recopier 1024
    // ici créerait un second barème : le jour où « Normal » change de valeur, la vue classerait
    // les conteneurs selon l'ancien, et personne ne verrait l'écart.
    const seuilNormal = (j) => {
        const n = ((j || {}).poids_niveaux || []).find((x) => x.niveau === 'normal');
        return n ? n.valeur : null;
    };
    const poidsBas = (j, l) => {
        const s = seuilNormal(j);
        return s === null ? false : l.poids.valeur < s;
    };

    // ─── Rendu : la grille des cœurs PHYSIQUES ──────────────────────────────────────────────
    // Une case = un cœur physique, ses deux threads HT côte à côte. Les jumeaux portent des
    // numéros éloignés (i et i+N/2) : dans une bande ordonnée par index, l'appariement est
    // invisible — et c'est précisément lui qui explique un « dédié » qui ne tient pas sa cadence.
    function grille(st) {
        const j = st.j || {};
        const cs = j.coeurs || [];
        if (!cs.length) return '<div class="meta">' + esc(T('settings.cores.sans_grille')) + '</div>';
        const cell = (t) => '<span class="bc-th bc-th-' + t.kind + '" aria-hidden="true"></span>';
        return '<div class="bc-grille" role="group" aria-label="' + esc(T('settings.cores.grille')) + '">'
            + cs.map((c) => {
                const sel = st.core !== null && st.core === c.phys;
                const occ = [].concat.apply([], c.threads.map((t) => t.noms)).filter(Boolean);
                return '<button type="button" class="bc-phys' + (sel ? ' is-sel' : '')
                    + (c.frere_partage ? ' is-frere' : '') + '" data-phys="' + esc(c.phys)
                    + '" aria-pressed="' + (sel ? 'true' : 'false') + '" title="'
                    + esc(fmt('settings.cores.coeur_aria', {
                        p: c.phys === null ? '?' : c.phys,
                        threads: c.threads.map((t) => t.cpu).join(' / '),
                        qui: occ.length ? occ.join(', ') : T('settings.cores.personne'),
                    })) + '">'
                    + '<span class="bc-phys-n">' + esc(c.phys === null ? c.threads[0].cpu : 'P' + c.phys) + '</span>'
                    + '<span class="bc-phys-th">' + c.threads.map(cell).join('') + '</span>'
                    + (c.frere_partage ? '<span class="bc-pastille" aria-hidden="true"></span>' : '')
                    + '</button>';
            }).join('') + '</div>';
    }

    // Infobulle : QUI utilise chaque thread, avec sa place et son poids (demandé nommément par
    // Cyril). Survol ET focus clavier : la grille est faite de vrais boutons.
    function infobulle(st) {
        const j = st.j || {};
        const c = (j.coeurs || []).find((x) => x.phys === st.survol);
        if (!c) return '';
        const par = {};
        lignes(j).forEach((l) => { par[l.vmid] = l; });
        const bloc = (t, i) => {
            const qui = t.vmids.length
                ? t.vmids.map((v) => {
                    const l = par[v];
                    return '<div class="bc-tip-l"><span>' + esc(l ? (l.hostname || ('#' + v)) : ('#' + v))
                        + '</span><span class="meta">' + (l ? esc(libelleNiveau(l)) : '') + '</span></div>';
                }).join('')
                : (t.kind === 'partage'
                    ? (j.partages || []).map((l) => '<div class="bc-tip-l"><span>' + esc(l.hostname || ('#' + l.vmid))
                        + '</span><span class="meta">' + esc(libelleNiveau(l)) + '</span></div>').join('')
                      || '<div class="meta">' + esc(T('settings.cores.personne')) + '</div>'
                    : '<div class="meta">' + esc(T('settings.cores.kind.' + t.kind, t.kind)) + '</div>');
            return '<div class="bc-tip-th"><div class="meta">'
                + esc(fmt('settings.cores.thread', { cpu: t.cpu })) + (i ? ' · ' + esc(T('settings.cores.frere')) : '')
                + '</div>' + qui + '</div>';
        };
        return '<div class="bc-tip" role="tooltip"><div class="bc-tip-t">'
            + esc(fmt('settings.cores.coeur_phys', { p: c.phys })) + '</div>'
            + c.threads.map(bloc).join('') + '</div>';
    }

    // ─── Rendu : le tableau des conteneurs ──────────────────────────────────────────────────
    const COLS = [['nom', 'settings.cores.col.conteneur'], ['prot', 'settings.cores.col.protection'],
                  ['poids', 'settings.cores.col.poids'], ['coeurs', 'settings.cores.col.coeurs'],
                  ['avec', 'settings.cores.col.avec'], ['reserves', 'settings.cores.col.reserves'],
                  ['charge', 'settings.cores.col.charge'], ['famine', 'monitoring.cpu_col_psi']];

    // FAMINE = PSI « full » du conteneur : part du temps où TOUTES ses tâches attendent un cœur.
    // Un conteneur affamé consomme PEU (il attend) — la colonne « charge » ne peut pas le montrer.
    // Seuils et couleurs : ceux de la tuile CPU de Monitoring (≥ 1 % visible, ≥ 25 % grave).
    function famineHtml(v) {
        if (v == null) return '<span class="meta">—</span>';
        if (v < 1) return '<span class="meta">' + esc(T('monitoring.cpu_psi_ok')) + '</span>';
        return '<span class="' + (v >= 25 ? 'bc-famine-bad' : 'bc-famine-warn') + '">'
            + esc(v.toFixed(0)) + ' %</span>';
    }

    function filtrer(st) {
        const j = st.j || {};
        const q = st.q.trim().toLowerCase();
        const n = /^\d+$/.test(q) ? parseInt(q, 10) : null;
        const cpusDe = (l) => parseCpuset(l.cpuset_reserve || l.cpuset_applique || '');
        const physDe = (l) => {
            const g = j.coeurs || [];
            const mine = cpusDe(l);
            return g.filter((c) => c.threads.some((t) => mine.indexOf(t.cpu) >= 0)).map((c) => c.phys);
        };
        return lignes(j).filter((l) => {
            if (st.prot === 'poids_bas') { if (!poidsBas(j, l)) return false; }
            else if (st.prot !== 'tous' && l.protection !== st.prot) return false;
            if (st.core !== null && physDe(l).indexOf(st.core) < 0) return false;
            if (n !== null) return cpusDe(l).indexOf(n) >= 0 || l.vmid === n;
            if (q) return ((l.hostname || '') + ' ' + l.vmid + ' ' + libellePlace(l) + ' '
                           + libellePoids(l.poids.niveau)).toLowerCase().indexOf(q) >= 0;
            return true;
        });
    }

    function parseCpuset(s) {
        const out = [];
        String(s || '').split(',').forEach((part) => {
            if (!part) return;
            const m = part.split('-');
            const a = parseInt(m[0], 10);
            const b = parseInt(m.length > 1 ? m[1] : m[0], 10);
            if (isNaN(a) || isNaN(b)) return;
            for (let i = a; i <= b; i++) out.push(i);
        });
        return out;
    }

    const RANG = { protege: 0, partie: 1, non: 2 };

    function trier(st, list) {
        const cle = {
            nom: (l) => (l.hostname || '').toLowerCase(),
            prot: (l) => (l.protection === null ? 3 : RANG[l.protection]),
            poids: (l) => -l.poids.valeur,
            coeurs: (l) => (parseCpuset(l.cpuset_reserve || l.cpuset_applique)[0] || 0),
            avec: (l) => (l.partage_avec || []).length,
            reserves: (l) => -(l.cores || 0),
            charge: (l) => -(l.charge == null ? -1 : l.charge),
            famine: (l) => -(l.famine == null ? -1 : l.famine),
        }[st.sort] || ((l) => l.vmid);
        return list.slice().sort((a, b) => {
            const A = cle(a), B = cle(b);
            return (A > B ? 1 : A < B ? -1 : 0) * st.dir;
        });
    }

    // Can this viewer change a container? (`window.hasPerm`, set by layout.html.) Without it, the
    // cells stay read-only — the server refuses anyway.
    const peutDeployer = () => !!(window.hasPerm && window.hasPerm('containers.deploy'));

    // A refusal the server explains (409): « Dédié needs N cores on X, which has only M free » —
    // the same sentence as the project view, never a bare code.
    const refus = (j) => (j && j.error === 'no_room'
        ? fmt('projects.res_level_no_room', { need: j.need, name: j.name, available: j.available })
        : (j && j.error) || '');

    // Reserved cores: a number to change for a dedicated container (hot on the server); a DeckLink
    // card's count follows its active connectors (set on its page); a shared one has none — its
    // CPU cap, when set, is said in its level.
    function coeursCellule(l) {
        if (l.place !== 'dedie') return '—';
        if (l.node_io) return esc(l.cores) + '<div class="meta">' + esc(T('settings.cores.node_io_note')) + '</div>';
        if (!peutDeployer()) return esc(l.cores);
        return '<input type="number" class="bc-cores-in" min="1" max="256" step="1" value="' + esc(l.cores)
            + '" data-vmid="' + l.vmid + '" data-avant="' + esc(l.cores) + '" aria-label="'
            + esc(fmt('settings.cores.cores_aria', { nom: l.hostname || l.vmid })) + '">';
    }

    function tableau(st) {
        const j = st.j || {};
        let list = trier(st, filtrer(st));
        const th = COLS.map(([k, lab]) => '<th><button type="button" class="bc-sort" data-sort="' + k + '">'
            + esc(T(lab)) + (st.sort === k ? (st.dir > 0 ? ' ▲' : ' ▼') : '') + '</button></th>').join('');
        const ligne = (l) => {
            const p = l.protection;
            const pr = p === null ? T('settings.cores.prot.inconnu') : T(PROTS[p]);
            const cpus = l.place === 'dedie' ? (l.cpuset_reserve || '—')
                : fmt('settings.cores.pool_n', { n: parseCpuset(l.cpuset_applique).length || (j.partage || {}).cores || 0 });
            const avec = l.partage_avec || [];
            const avecTxt = avec.length
                ? avec.slice(0, 3).map((v) => (v.hostname || ('#' + v.vmid))
                    + (v.via === 'frere_ht' ? ' ' + T('settings.cores.via_ht') : v.via === 'deborde' ? ' ' + T('settings.cores.via_deborde') : '')).join(', ')
                  + (avec.length > 3 ? ' +' + (avec.length - 3) : '')
                : T('settings.cores.personne');
            const charge = l.charge == null ? '<span class="meta">' + esc(T('settings.cores.non_mesure')) + '</span>'
                : esc((l.charge_minorant ? '≥ ' : '') + l.charge.toFixed(2) + ' ' + T('settings.cores.coeurs_unite'));
            const divergent = l.poids_coherent === false;
            return '<tr data-vmid="' + l.vmid + '">'
                + '<td><b>' + esc(l.hostname || ('#' + l.vmid)) + '</b> <span class="meta">#' + l.vmid + '</span>'
                + (window.BobiContainerPanel ? ' <button type="button" class="bc-reglages ctl-push ctl-push--icon" data-vmid="'
                    + l.vmid + '" aria-label="' + esc(fmt('settings.cores.reglages_aria', { nom: l.hostname || l.vmid }))
                    + '">⚙</button>' : '')
                + (l.place === 'dedie' ? '' : '<div class="meta">' + esc(libellePlace(l)) + '</div>') + '</td>'
                + '<td><span class="bc-pill bc-' + (p === null ? 'info' : TONS[p]) + '">' + esc(pr) + '</span>'
                + '<div class="meta">' + esc(raison(l)) + '</div></td>'
                + '<td><div class="bc-poids" data-vmid="' + l.vmid + '"></div>'
                + (divergent ? '<div class="meta bc-warn">' + esc(fmt('settings.cores.poids_divergent',
                    { pose: l.poids_applique, voulu: l.poids.valeur })) + '</div>' : '')
                + '<div class="meta">' + esc(T('settings.cores.weight.src_' + l.poids.source, '')) + '</div></td>'
                + '<td><code>' + esc(cpus) + '</code>'
                + (l.physiques ? '<div class="meta">' + esc(fmt('settings.cores.n_physiques', { n: l.physiques })) + '</div>' : '')
                + (l.coherent === false
                    // Deux défauts, deux phrases : un DÉDIÉ au mauvais cpuset tourne encore sur
                    // son ancienne réservation (il n'a pas été recréé) ; un PARTAGÉ au mauvais
                    // cpuset a un cpuset PÉRIMÉ, calculé à son déploiement, que « Réappliquer »
                    // règle. Les confondre reviendrait à dire « quelque chose ne va pas ».
                    ? '<div class="meta bc-bad">' + esc(T(l.place === 'dedie'
                        ? 'settings.cores.incoherent' : 'settings.cores.perime')) + '</div>'
                      + (l.place === 'dedie' && peutDeployer() ? '<button type="button" class="bc-recreer ctl-push" data-vmid="'
                          + l.vmid + '" data-nom="' + esc(l.hostname || l.vmid) + '">'
                          + esc(T('containers.card.recreate')) + '</button>' : '') : '')
                + (l.deborde ? '<div class="meta bc-bad">' + esc(fmt('settings.cores.deborde', { cores: l.deborde })) + '</div>' : '')
                + '</td>'
                + '<td title="' + esc(avec.map((v) => v.hostname || ('#' + v.vmid)).join(', ')) + '">' + esc(avecTxt) + '</td>'
                + '<td class="bc-num">' + coeursCellule(l) + '</td>'
                + '<td class="bc-num">' + charge + '</td>'
                + '<td class="bc-num" title="' + esc(T('monitoring.cpu_psi_tip')) + '">' + famineHtml(l.famine) + '</td></tr>';
        };
        // Regroupements : par protection, par poids, par cœur physique. Le troisième répond à
        // « qui est sur CE cœur » — la question qu'on se pose en lisant la grille.
        let corps = '';
        const groupe = (lab, sub, g) => '<tr class="bc-grp"><td colspan="' + COLS.length + '"><b>'
            + esc(lab) + '</b> <span class="meta">' + esc(sub) + '</span></td></tr>' + g.map(ligne).join('');
        if (st.group === 'protection') {
            ['protege', 'partie', 'non'].forEach((k) => {
                const g = list.filter((l) => l.protection === k);
                if (g.length) corps += groupe(T(PROTS[k]), fmt('settings.cores.n_conteneurs', { n: g.length }), g);
            });
            const inc = list.filter((l) => l.protection === null);
            if (inc.length) corps += groupe(T('settings.cores.prot.inconnu'), fmt('settings.cores.n_conteneurs', { n: inc.length }), inc);
        } else if (st.group === 'poids') {
            ((j.poids_niveaux) || []).forEach((n) => {
                const g = list.filter((l) => l.poids.niveau === n.niveau);
                if (g.length) corps += groupe(libellePoids(n.niveau) + ' · ' + n.valeur,
                                              fmt('settings.cores.n_conteneurs', { n: g.length }), g);
            });
        } else if (st.group === 'coeur') {
            (j.coeurs || []).forEach((c) => {
                if (st.core !== null && st.core !== c.phys) return;
                const cpus = c.threads.map((t) => t.cpu);
                const g = list.filter((l) => parseCpuset(l.cpuset_reserve || l.cpuset_applique).some((x) => cpus.indexOf(x) >= 0));
                if (g.length) corps += groupe(fmt('settings.cores.coeur_phys', { p: c.phys }),
                                              fmt('settings.cores.threads_x', { threads: cpus.join(' / ') }), g);
            });
        } else corps = list.map(ligne).join('');
        if (!corps) corps = '<tr><td colspan="' + COLS.length + '" class="meta">'
            + esc(T('settings.cores.aucun_resultat')) + '</td></tr>';
        return '<table class="bc-tab"><thead><tr>' + th + '</tr></thead><tbody>' + corps + '</tbody></table>';
    }

    // ─── Rendu : onglet C — une ligne par conteneur, une colonne par cœur physique ───────────
    // La vue « rassurante » voulue par Cyril : deux couleurs dans une même colonne = partage.
    function bandes(st) {
        const j = st.j || {};
        const g = j.coeurs || [];
        if (!g.length) return '<div class="meta">' + esc(T('settings.cores.sans_grille')) + '</div>';
        const tete = '<div class="bc-bd-l bc-bd-head"><div class="bc-bd-nom"></div>'
            + g.map((c) => '<div class="bc-bd-c meta">' + esc(c.phys === null ? c.threads[0].cpu : 'P' + c.phys) + '</div>').join('')
            + '</div>';
        const ligne = (l) => {
            const mine = parseCpuset(l.cpuset_reserve || l.cpuset_applique);
            const ded = l.place === 'dedie';
            return '<div class="bc-bd-l"><div class="bc-bd-nom">' + esc(l.hostname || ('#' + l.vmid))
                + '</div>' + g.map((c) => '<div class="bc-bd-c">' + c.threads.map((t) => {
                    const on = mine.indexOf(t.cpu) >= 0;
                    const frere = ded && on && c.frere_partage;
                    return '<span class="bc-bd-th' + (on ? (ded ? ' is-ded' : ' is-sh') : '')
                        + (poidsBas(j, l) ? ' is-bas' : '') + (frere ? ' is-frere' : '') + '"></span>';
                }).join('') + '</div>').join('') + '</div>';
        };
        let out = tete;
        ((j.poids_niveaux) || []).forEach((n) => {
            [['dedie', T(PLACES.dedie)], ['partage', T(PLACES.partage)]].forEach(([pl, lab]) => {
                const grp = lignes(j).filter((l) => l.poids.niveau === n.niveau
                    && (pl === 'dedie' ? l.place === 'dedie' : l.place !== 'dedie'));
                if (!grp.length) return;
                out += '<div class="bc-bd-grp">' + esc(lab + ' · ' + libellePoids(n.niveau))
                    + ' <span class="meta">' + esc(T('settings.cores.bandes_aide_' + (pl === 'dedie' ? 'dedie' : 'partage'))) + '</span></div>'
                    + grp.map(ligne).join('');
            });
        });
        return '<div class="bc-bandes">' + out + '</div>';
    }

    // ─── Rendu : en-tête, mode d'énergie, synthèse, actions ─────────────────────────────────
    function entete(st) {
        const j = st.j || {};
        const p = j.partage || {};
        const e = st.energie;
        let energie;
        if (!e || !e.mesure) {
            energie = '<div class="bc-energie bc-info"><b>' + esc(T('settings.cores.energie_inconnue')) + '</b>'
                + '<div class="meta">' + esc(T('settings.cores.energie_inconnue_aide')) + '</div></div>';
        } else {
            const ok = e.conforme;
            energie = '<div class="bc-energie ' + (ok ? 'bc-ok' : 'bc-warn') + '"><b>'
                + esc(fmt(ok ? 'settings.cores.energie_ok' : 'settings.cores.energie_ko', { gov: e.gouverneur || '?' }))
                + '</b><div class="meta">' + esc(fmt('settings.cores.energie_detail', {
                    gov: e.gouverneur || '?', min: e.min_mhz || '?', max: e.max_mhz || '?',
                    dedies: e.dedies || '—' }))
                // Veille profonde : seulement quand elle a été MESURÉE. Pas d'interface cpuidle
                // (VM) → on n'écrit rien, plutôt que « coupée sur — » qui se lit comme un manque.
                + (e.veille_mesuree ? ' · ' + esc(fmt('settings.cores.energie_veille',
                    { cores: e.veille_coupee || '—' })) : '')
                // Le budget EN REGARD de l'échelle annoncée par ce CPU. Un « 5 » nu ne dit pas ce
                // qu'il coupe ; « 5 µs — états annoncés : 0, 2, 10, 133 » le dit, et c'est ce qui
                // permet de régler une autre machine sans deviner.
                + (e.veille_mesuree && e.veille_budget_us != null
                    ? '<br>' + esc(fmt('settings.cores.energie_veille_budget', {
                        us: e.veille_budget_us,
                        etats: (e.veille_etats_us || []).join(', ') || '—' })) : '')
                + '</div>'
                + (ok ? '' : '<button type="button" class="btn bc-perf">'
                    + esc(T('settings.cores.passer_performance')) + '</button>') + '</div>';
        }
        const s = j.synthese || {};
        const seg = (k, n, ton) => (n ? '<button type="button" class="bc-seg bc-' + ton
            + '" data-prot="' + k + '">' + esc(fmt('settings.cores.synth_' + k, { n: n })) + '</button>' : '');
        return '<div class="bc-head">'
            + '<div class="bc-head-t"><b>' + esc(fmt('settings.cores.titre_noeud', { n: st.nom })) + '</b>'
            + '<div class="meta">' + esc(fmt('settings.cores.resume', {
                log: j.pool_cores || 0, phys: (p.physiques != null ? p.physiques : '?'),
                ht: T(j.ht_connu ? 'settings.cores.ht_actif' : 'settings.cores.ht_inconnu2'),
            })) + (j.releve ? '' : ' · ' + esc(T('settings.cores.sans_releve'))) + '</div></div>'
            + energie + '</div>'
            + '<div class="bc-synth">' + seg('protege', s.protege, 'ok') + seg('partie', s.partie, 'warn')
            + seg('non', s.non, 'info') + seg('poids_bas', s.poids_bas, 'info')
            + (s.poids_divergents ? '<span class="bc-seg bc-bad">' + esc(fmt('settings.cores.synth_poids_divergents',
                { n: s.poids_divergents })) + '</span>' : '')
            + '</div>';
    }

    function capacite(st) {
        const j = st.j || {};
        const p = j.partage || {};
        const sst = p.si_strict;
        let h = '<div class="bc-cap' + (p.physiques_exclusifs ? '' : ' bc-warn') + '"><div class="meta">'
            + esc(T('settings.cores.cap_titre')) + '</div><b>' + (p.physiques_exclusifs != null ? p.physiques_exclusifs : '?')
            + '</b> <span class="meta">' + esc(T('settings.cores.physiques')) + '</span>'
            + (p.freres_de_dedies ? '<div class="meta">' + esc(fmt('settings.cores.frere_ht', { freres: p.freres_de_dedies })) + '</div>' : '')
            + '</div>';
        if ((j.status || {}).oversub) h += '<div class="bc-cap">'
            + esc(T('settings.cores.sursouscrit')) + '</div>';
        if (p.bas) h += '<div class="bc-cap bc-warn">' + esc(fmt('settings.cores.partage_bas',
            { cores: p.cores, count: p.temps_reel })) + '</div>';
        else if (p.reduit) h += '<div class="bc-cap bc-warn">' + esc(fmt('settings.cores.partage_reduit',
            { cpuset: p.cpuset || '—', cores: p.cores, count: p.conteneurs })) + '</div>';
        if (sst) h += '<div class="bc-cap' + (sst.viable ? '' : ' bc-warn') + '"><div class="meta">'
            + esc(T('settings.cores.si_strict_titre')) + '</div>'
            + esc(sst.viable ? fmt('settings.cores.si_strict', { cpuset: sst.cpuset || '—', cores: sst.cores,
                physiques: sst.physiques, perdus: sst.perdus || '—' })
                : fmt('settings.cores.si_strict_non_viable', { cpuset: sst.cpuset || '—' })) + '</div>';
        return '<div class="bc-caps">' + h + '</div>';
    }

    // ★ LES ORPHELINS SONT NOMMÉS. C'est tout l'enseignement du 28/10 : `cores_status` disait
    // « used 49 / free 2 » — exact et inutilisable, puisqu'il ne désigne personne. Un bouton
    // « Libérer (23) » sans la LISTE referait la même erreur d'un cran plus haut : l'exploitant
    // saurait qu'il y a un problème sans savoir lequel. La grille les montre en rouge, ce
    // tableau-ci les nomme, et chacun a son bouton (libérer un seul, pour voir l'effet).
    function orphelins(st) {
        const o = ((st.j || {}).orphelins || []);
        if (!o.length) return '';
        return '<div class="bc-orph"><div class="meta">' + esc(T('settings.cores.orphelin_aide'))
            + '</div>' + o.map((x) => '<div class="bc-orph-l"><span class="bc-bad">✕ '
                + esc(T('settings.cores.orphelin')) + ' #' + x.vmid + '</span> <code>'
                + esc(x.cpuset || '—') + '</code>'
                + '<button type="button" class="btn bc-lib-un" data-vmid="' + x.vmid + '">'
                + esc(T('settings.cores.liberer')) + '</button></div>').join('') + '</div>';
    }

    // ─── Relevé des cœurs : mesure à la demande (~0,5 s côté agent) ────────────────────────
    // % réel par cœur et les threads qui tournent dessus, conteneur nommé. Ponctuel, horodaté :
    // ce n'est pas une jauge vivante, et l'heure dit de quand date ce qu'on lit.
    function releve(st) {
        if (!st.snap) return '';
        const d = st.snap.data || {};
        const ts = new Date(st.snap.ts).toLocaleTimeString();
        if (!d.ok) return '<div class="meta bc-snap">' + esc(T(d.error === 'agent_too_old'
            ? 'monitoring.core_snapshot_agent_old' : 'monitoring.core_snapshot_err')) + '</div>';
        const rows = (d.cores || []).map((c) => {
            const top = (c.top || []).map((t) => esc(t.hostname ? t.hostname + ' (' + t.vmid + ')' : (t.container || '?'))
                + ' · <span class="meta">' + esc(t.comm || '') + '</span> · ' + esc(t.pct) + ' %').join('<br>')
                || '<span class="meta">' + esc(T('monitoring.core_top_none')) + '</span>';
            return '<tr' + (c.pct >= 80 ? ' class="bc-snap-hot"' : '') + '><td>#' + esc(c.cpu) + '</td><td class="bc-num">'
                + (c.pct != null ? esc(c.pct.toFixed(1)) + ' %' : '—') + '</td><td>' + top + '</td></tr>';
        }).join('');
        return '<div class="bc-snap"><div class="meta">' + esc(T('monitoring.core_snapshot_ts')) + ' ' + esc(ts)
            + ' · ' + esc(d.duration_ms) + ' ms</div><table class="bc-tab"><tbody>' + rows + '</tbody></table></div>';
    }

    async function mesurerReleve(st, redessine) {
        if (st.snapBusy) return;
        st.snapBusy = true; redessine();
        let r;
        try { r = await _json('/api/nodes/' + st.id + '/core-snapshot'); }
        catch (e) { r = { j: { ok: false, error: 'network' } }; }
        st.snap = { ts: Date.now(), data: r.j || { ok: false } };
        st.snapBusy = false; redessine();
    }

    function actions(st) {
        const j = st.j || {};
        const orph = (j.orphelins || []).length;
        return '<div class="bc-actions">'
            + '<button type="button" class="btn bc-reapply">' + esc(T('settings.cores.reappliquer')) + '</button>'
            + '<button type="button" class="btn bc-reapply-poids">' + esc(T('settings.cores.reappliquer_poids')) + '</button>'
            + '<button type="button" class="btn bc-liberer"' + (orph ? '' : ' disabled')
            + '>' + esc(fmt('settings.cores.liberer_n', { n: orph })) + '</button>'
            + '<button type="button" class="btn bc-refresh">' + esc(T('settings.cores.rafraichir')) + '</button>'
            + '<button type="button" class="btn bc-releve"' + (st.snapBusy ? ' disabled' : '') + '>'
            + esc(T(st.snapBusy ? 'monitoring.core_snapshot_busy' : 'monitoring.core_snapshot_btn')) + '</button>'
            + '<span class="meta">' + esc(T('settings.cores.reappliquer_aide')) + '</span>'
            + '<span class="meta">' + esc(T('settings.cores.poids_aide')) + '</span>'
            + '</div>';
    }

    function barre(st) {
        const chips = (k, list) => list.map(([v, lab]) => '<button type="button" class="bc-chip'
            + (st[k] === v ? ' is-cur' : '') + '" data-' + k + '="' + v + '">' + esc(T(lab)) + '</button>').join('');
        return '<div class="bc-barre">'
            + '<label class="bc-search"><span class="meta">' + esc(T('settings.cores.rechercher')) + '</span>'
            + '<input type="search" class="bc-q" value="' + esc(st.q) + '" placeholder="'
            + esc(T('settings.cores.rechercher_ph')) + '"></label>'
            + '<span class="meta">' + esc(T('settings.cores.filtre')) + '</span>'
            + chips('prot', [['tous', 'settings.cores.filtre_tous'], ['protege', 'settings.cores.prot.protege'],
                             ['partie', 'settings.cores.prot.partie'], ['non', 'settings.cores.prot.non'],
                             ['poids_bas', 'settings.cores.filtre_poids_bas']])
            + '<span class="meta">' + esc(T('settings.cores.grouper')) + '</span>'
            + chips('group', [['aucun', 'settings.cores.grp_aucun'], ['protection', 'settings.cores.col.protection'],
                              ['poids', 'settings.cores.col.poids'], ['coeur', 'settings.cores.grp_coeur']])
            + (st.core !== null ? '<button type="button" class="bc-chip is-cur bc-clear-core">'
                + esc(fmt('settings.cores.filtre_coeur', { p: st.core })) + ' ×</button>' : '')
            + '</div>';
    }

    // ─── Assemblage + événements ────────────────────────────────────────────────────────────
    function rendre(st, root) {
        const ong = (k, lab) => '<button type="button" class="bc-onglet' + (st.onglet === k ? ' is-cur' : '')
            + '" data-onglet="' + k + '" role="tab" aria-selected="' + (st.onglet === k ? 'true' : 'false') + '">'
            + esc(T(lab)) + '</button>';
        root.innerHTML = entete(st)
            + '<div class="bc-onglets" role="tablist">' + ong('a', 'settings.cores.onglet_a')
            + ong('c', 'settings.cores.onglet_c') + '</div>'
            + (st.onglet === 'a'
                ? '<div class="bc-haut"><div class="bc-gauche">' + grille(st)
                  + '<div class="bc-legende">' + legende() + '</div>'
                  + '<div class="bc-tipbox">' + infobulle(st) + '</div></div>'
                  + '<div class="bc-droite">' + capacite(st) + orphelins(st) + actions(st) + '</div></div>'
                  + barre(st) + '<div class="bc-tabbox">' + tableau(st) + '</div>' + releve(st)
                : bandes(st));
        brancher(st, root);
    }

    function legende() {
        return [['dedie', 'settings.cores.kind.dedie'], ['partage', 'settings.cores.kind.partage'],
                ['moteur', 'settings.cores.kind.moteur'], ['orphelin', 'settings.cores.kind.orphelin'],
                ['reserve_ht', 'settings.cores.kind.reserve_ht'], ['systeme', 'settings.cores.kind.systeme']]
            .map(([k, lab]) => '<span class="bc-lg"><span class="bc-th bc-th-' + k + '"></span>'
                + esc(T(lab)) + '</span>').join('')
            + '<span class="bc-lg"><span class="bc-pastille-lg"></span>' + esc(T('settings.cores.legende_frere')) + '</span>';
    }

    function brancher(st, root) {
        const redessine = () => rendre(st, root);
        const maj = async (force) => {
            const r = await charger(st.id, force);
            if (r.ok) st.j = r.j;
            redessine();
        };
        root.querySelectorAll('[data-onglet]').forEach((b) => b.onclick = () => {
            st.onglet = b.dataset.onglet; redessine();
        });
        root.querySelectorAll('[data-prot]').forEach((b) => b.onclick = () => {
            st.prot = (st.prot === b.dataset.prot ? 'tous' : b.dataset.prot); redessine();
        });
        root.querySelectorAll('[data-group]').forEach((b) => b.onclick = () => {
            st.group = b.dataset.group; redessine();
        });
        root.querySelectorAll('[data-sort]').forEach((b) => b.onclick = () => {
            st.dir = st.sort === b.dataset.sort ? -st.dir : 1;
            st.sort = b.dataset.sort; redessine();
        });
        const q = root.querySelector('.bc-q');
        if (q) q.oninput = () => { st.q = q.value; const sel = document.activeElement === q; redessine();
                                   if (sel) { const n = root.querySelector('.bc-q'); n.focus();
                                              n.setSelectionRange(n.value.length, n.value.length); } };
        root.querySelectorAll('[data-phys]').forEach((b) => {
            const p = b.dataset.phys === 'null' ? null : parseInt(b.dataset.phys, 10);
            b.onclick = () => { st.core = (st.core === p ? null : p); redessine(); };
            // Survol ET focus clavier : l'infobulle est une information, pas une récompense de souris.
            b.onmouseenter = b.onfocus = () => { st.survol = p; majTip(st, root); };
            b.onmouseleave = b.onblur = () => { st.survol = null; majTip(st, root); };
        });
        root.querySelectorAll('.bc-lib-un').forEach((b) => b.onclick = async () => {
            const r = await _json('/api/nodes/' + st.id + '/cores/release', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ vmid: parseInt(b.dataset.vmid, 10) }),
            });
            toast(r.ok ? fmt('settings.cores.liberer_un_ok', { vmid: r.j.vmid, cores: r.j.cores })
                       : (r.j.error || ''), r.ok ? '' : 'error');
            maj(true);
        });
        const cc = root.querySelector('.bc-clear-core');
        if (cc) cc.onclick = () => { st.core = null; redessine(); };

        // Sélecteur de POIDS par conteneur : contrôle du catalogue partagé, jamais redessiné ici.
        const niveaux = (st.j || {}).poids_niveaux || [];
        root.querySelectorAll('.bc-poids').forEach((el) => {
            const vmid = parseInt(el.dataset.vmid, 10);
            const l = lignes(st.j).find((x) => x.vmid === vmid);
            if (!l || !window.BobiControls) { el.textContent = l ? libellePoids(l.poids.niveau) : ''; return; }
            const c = window.BobiControls.create('buttons', el, {
                options: niveaux.map((n) => ({ value: n.niveau, label: libellePoids(n.niveau) })),
                value: l.poids.niveau, showLabel: false,
            });
            c.on('change', async (v) => {
                if (st.occupe) return;
                st.occupe = true;
                const r = await _json('/api/nodes/' + st.id + '/cores/weight', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ vmid: vmid, niveau: v }),
                });
                st.occupe = false;
                if (!r.ok) { toast(refus(r.j) || T('settings.cores.poids_echec'), 'error'); c.set(l.poids.niveau); return; }
                if (!r.j.applique) toast(fmt('settings.cores.poids_differe', { e: r.j.err || '' }), 'warning');
                else toast(fmt('settings.cores.poids_pose', { nom: l.hostname || vmid, niveau: libellePoids(v) }));
                maj(true);
            });
        });

        // Dedicated cores count: changed hot by the server (docker update); refused with its reason.
        root.querySelectorAll('.bc-cores-in').forEach((el) => {
            el.onchange = async () => {
                const n = parseInt(el.value, 10);
                if (!(n >= 1) || String(n) === el.dataset.avant || st.occupe) return;
                st.occupe = true;
                const r = await _json('/api/containers/' + el.dataset.vmid + '/resources', {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cores: n }),
                });
                st.occupe = false;
                if (!r.ok) { toast(refus(r.j) || T('settings.cores.cores_echec'), 'error'); el.value = el.dataset.avant; return; }
                toast(fmt('settings.cores.cores_pose', { n: n }));
                maj(true);
            };
        });
        root.querySelectorAll('.bc-recreer').forEach((b) => b.onclick = async () => {
            if (!window.confirm(T('containers.card.recreate_confirm').replace('{name}', b.dataset.nom))) return;
            const r = await _json('/api/containers/' + b.dataset.vmid + '/recreate', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirm: true }),
            });
            toast(r.ok ? T('settings.cores.recreer_lance') : (r.j.error || T('containers.card.recreate_failed')),
                  r.ok ? '' : 'error');
        });
        root.querySelectorAll('.bc-reglages').forEach((b) => b.onclick = () => {
            if (window.BobiContainerPanel) window.BobiContainerPanel.open(parseInt(b.dataset.vmid, 10));
        });

        const clic = (sel, fn) => { const b = root.querySelector(sel); if (b) b.onclick = fn; };
        clic('.bc-refresh', () => maj(true));
        clic('.bc-releve', () => mesurerReleve(st, redessine));
        clic('.bc-reapply', async () => {
            const r = await _json('/api/nodes/' + st.id + '/cores/reapply', { method: 'POST' });
            toast(r.ok ? fmt('settings.cores.reapply_ok', { n: (r.j.faits || []).length })
                       : (r.j.error || T('settings.cores.reapply_echec')), r.ok ? '' : 'error');
            maj(true);
        });
        clic('.bc-reapply-poids', async () => {
            const r = await _json('/api/nodes/' + st.id + '/cores/weight/reapply', { method: 'POST' });
            toast(r.ok ? fmt('settings.cores.reapply_poids_ok', { n: (r.j.faits || []).length })
                       : (r.j.error || T('settings.cores.reapply_echec')), r.ok ? '' : 'error');
            maj(true);
        });
        clic('.bc-liberer', async () => {
            if (!confirm(T('settings.cores.liberer_confirme'))) return;
            const r = await _json('/api/nodes/' + st.id + '/cores/release', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tous: true }),
            });
            toast(r.ok ? fmt('settings.cores.liberer_ok', { n: r.j.cores || 0 }) : (r.j.error || ''), r.ok ? '' : 'error');
            maj(true);
        });
        // Mode d'énergie : la confirmation est DEMANDÉE ici et EXIGÉE par le serveur (un
        // `confirme` que seule l'interface tiendrait n'est pas une confirmation).
        clic('.bc-perf', async () => {
            if (!confirm(T('settings.cores.performance_confirme'))) return;
            const r = await _json('/api/nodes/' + st.id + '/energy/performance', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirme: true }),
            });
            if (r.ok) { st.energie = r.j.energie; toast(T('settings.cores.performance_ok')); }
            else toast(r.j.error || T('settings.cores.performance_echec'), 'error');
            maj(true);
        });
    }

    function majTip(st, root) {
        const box = root.querySelector('.bc-tipbox');
        if (box) box.innerHTML = infobulle(st);
    }

    // ─── La fenêtre ─────────────────────────────────────────────────────────────────────────
    async function ouvrir(id, nom) {
        const st = nouvelEtat(id, nom || ('#' + id));
        let dlg = document.getElementById('bobi-cores-dlg');
        if (!dlg) {
            dlg = document.createElement('dialog');
            dlg.id = 'bobi-cores-dlg';
            dlg.className = 'modal-card bc-dlg';
            dlg.setAttribute('aria-label', T('settings.cores.titre'));
            dlg.innerHTML = '<div class="bc-bar"><b class="bc-titre"></b><div style="flex:1"></div>'
                + '<button type="button" class="btn bc-close" aria-label="' + esc(T('layout.close', 'Fermer')) + '">×</button></div>'
                + '<div class="bc-root"></div>';
            document.body.appendChild(dlg);
            dlg.querySelector('.bc-close').onclick = () => dlg.close();
        }
        dlg.querySelector('.bc-titre').textContent = fmt('settings.cores.titre_noeud', { n: st.nom });
        const root = dlg.querySelector('.bc-root');
        root.innerHTML = '<div class="meta">' + esc(T('monitoring.loading', '…')) + '</div>';
        if (typeof dlg.showModal === 'function' && !dlg.open) dlg.showModal();
        const [a, b] = await Promise.all([charger(id, true), chargerEnergie(id, false)]);
        if (!a.ok) { root.innerHTML = '<div class="bc-bad">' + esc(a.j.error || T('settings.cores.echec')) + '</div>'; return; }
        st.j = a.j;
        st.energie = b.ok ? b.j : null;
        rendre(st, root);
    }

    // ─── Section compacte (Réglages → Nœuds) ────────────────────────────────────────────────
    // Le MÊME état et le MÊME tableau, sans la grille ni les bandes : la page des nœuds est une
    // page de configuration, pas un poste de diagnostic. Un seul rendu, donc une seule vérité.
    function rendreCompact(el, j, id, nom) {
        const st = nouvelEtat(id, nom || ('#' + id));
        st.j = j;
        el.innerHTML = '<div class="bc-compact">' + entete(st) + capacite(st) + orphelins(st) + actions(st)
            + barre(st) + '<div class="bc-tabbox">' + tableau(st) + '</div>' + releve(st)
            + '<button type="button" class="btn btn-blue bc-ouvrir">'
            + esc(T('settings.cores.ouvrir_fenetre')) + '</button></div>';
        brancher(st, el);
        const o = el.querySelector('.bc-ouvrir');
        if (o) o.onclick = () => ouvrir(id, nom);
    }

    window.BobiCores = { ouvrir, charger, chargerEnergie, rendreCompact,
                         // exportés pour la garde hors ligne (aucun accès DOM)
                         _interne: { parseCpuset, filtrer, trier, nouvelEtat, libellePlace,
                                     seuilNormal, poidsBas, lignes, famineHtml, releve,
                                     libelleNiveau, coeursCellule, refus, tableau } };
})();
