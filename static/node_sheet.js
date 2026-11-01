/* Fiche d'un nœud + arrêt / redémarrage confirmés (Réglages → Réseau → Vue d'ensemble).
 *
 * Demande de Cyril, validée sur maquette le 2026-10-30 : arrêter ou redémarrer un nœud était
 * enfoui dans les pilotes hôte et la préparation MTL — introuvable quand on en a besoin. La fiche
 * s'ouvre au clic sur une carte du schéma ; les mêmes boutons sont sur les cartes de Déploiement
 * → Nœuds. Les deux mènent à UNE boîte de confirmation, qui montre ce qui sera coupé (l'antenne
 * d'abord) et ce que les gardes du serveur diraient maintenant (cf. app/node_power.py).
 *
 * ★ LA BOÎTE INFORME, LE SERVEUR DÉCIDE. Les routes de coupure rejouent les gardes : une
 * vérification vieille de trente secondes n'autorise rien. Si le serveur refuse quand même, son
 * refus est affiché, jamais avalé.
 *
 * Données : GET /api/nodes/<id>/sheet (aucune sonde du nœud), /api/alerts?node_id=, et — à
 * l'ouverture seulement, parce qu'elles exécutent un script sur l'hôte — les pilotes hôte.
 */
(function () {
    'use strict';
    const P = 'settings.node_sheet.';
    const T = (k) => window.t(P + k);
    const Tf = (k, vars) => {
        let s = T(k);
        Object.keys(vars || {}).forEach(v => { s = s.split('{' + v + '}').join(String(vars[v])); });
        return s;
    };
    const esc = (s) => window._escp(s == null ? '' : s);
    const AGENT_MACHINE = '0.30.2';          // première version d'agent qui remonte `machine`
    const REFRESH_MS = 10000;
    const CUT_SHOWN = 6;                     // lignes de « ce qui sera coupé » avant « + n autres »

    let current = null;                      // id du nœud dont la fiche est ouverte
    let timer = null;
    let lastSheet = null;

    function dur(s) {
        s = Math.max(0, Math.round(s || 0));
        if (s < 60) return s + ' s';
        if (s < 3600) { const m = Math.floor(s / 60), r = s % 60; return m + ' min' + (r ? ' ' + String(r).padStart(2, '0') : ''); }
        if (s < 86400) { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h + ' h ' + String(m).padStart(2, '0'); }
        return Tf('dur_days', {d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600)});
    }
    function lang() { return document.documentElement.lang || undefined; }
    function dateCourte(ts) {
        return new Date(ts * 1000).toLocaleString(lang(), {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
    }
    function heure(ts, sec) {
        return new Date(ts * 1000).toLocaleTimeString(lang(), sec ? {} : {hour: '2-digit', minute: '2-digit'});
    }
    // Le libellé de l'accès distant suit le constructeur — c'est ce que l'exploitant cherche
    // sur la façade de la machine. Inconnu : le terme générique.
    function bmcLabel(vendor) {
        const v = (vendor || '').toLowerCase();
        if (/\bhp\b|hewlett|hpe/.test(v)) return 'iLO';
        if (/dell/.test(v)) return 'iDRAC';
        if (/lenovo/.test(v)) return 'XClarity';
        if (/supermicro/.test(v)) return 'IPMI';
        return 'BMC';
    }

    // ─── Navigation vers les pages qui règlent ce que la fiche montre ─────────────────────────
    function goHash(h) { if (location.hash === h) window.dispatchEvent(new HashChangeEvent('hashchange')); else location.hash = h; }
    function pickHostNode(id) {
        if (!window.HostNode) return;
        HostNode.current = id;
        try { HostNode._render(); } catch (_) {}
    }
    // Configuration du nœud = le panneau dépliable de sa carte, dans Déploiement → Nœuds. La liste
    // est rendue de façon asynchrone : on attend la carte (borné) avant de la déplier.
    function goConfig(id) {
        goHash('#deploiement/nodes');
        let n = 0;
        const t = setInterval(() => {
            const box = document.getElementById('ndcfg_' + id);
            if (box || ++n > 40) {
                clearInterval(t);
                if (!box) return;
                if (box.style.display === 'none' && typeof window.basculerConfigNode === 'function') window.basculerConfigNode(id);
                box.scrollIntoView({block: 'start', behavior: 'smooth'});
            }
        }, 100);
    }
    function goIfaces(id) { pickHostNode(id); goHash('#reseau/interfaces'); if (typeof window.onHostNodeChange === 'function') window.onHostNodeChange(); }
    function goCpu(id) { pickHostNode(id); goHash('#cpu'); if (typeof window.onHostNodeChange === 'function') window.onHostNodeChange(); }
    function goBuild() { goHash('#deploiement/build'); }

    // ─── Schéma : cartes cliquables et sélection ─────────────────────────────────────────────
    function markSelected() {
        document.querySelectorAll('#cluster_topo .ns-node-card').forEach(g => {
            const on = current != null && String(current) === g.dataset.nodeId;
            g.classList.toggle('is-selected', on);
            g.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }
    function bindTopology() {
        const host = document.getElementById('cluster_topo');
        if (!host || host.dataset.nsBound) return;
        host.dataset.nsBound = '1';
        const pick = (e) => {
            const g = e.target.closest && e.target.closest('.ns-node-card');
            if (!g) return;
            e.preventDefault();
            const id = parseInt(g.dataset.nodeId, 10);
            if (current === id) close(); else open(id);
        };
        host.addEventListener('click', pick);
        host.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') pick(e); });
    }

    // ─── Fiche ─────────────────────────────────────────────────────────────────────────────
    function sheetEl() { return document.getElementById('node_sheet'); }

    function open(id) {
        current = id;
        lastSheet = null;
        const el = sheetEl();
        if (!el) return;
        el.hidden = false;
        el.innerHTML = '<div class="meta ns-pad">' + esc(T('loading')) + '</div>';
        markSelected();
        clearInterval(timer);
        timer = setInterval(() => { if (!document.hidden && current === id) refresh(false); }, REFRESH_MS);
        refresh(true);
    }
    function close() {
        const was = current;
        current = null;
        clearInterval(timer);
        timer = null;
        const el = sheetEl();
        if (el) { el.hidden = true; el.innerHTML = ''; }
        markSelected();
        const g = was != null && document.querySelector('#cluster_topo .ns-node-card[data-node-id="' + was + '"]');
        if (g && g.focus) g.focus();
    }

    async function refresh(first) {
        const id = current;
        let s;
        try {
            const r = await fetch('/api/nodes/' + id + '/sheet');
            s = await r.json();
            if (!r.ok || !s.ok) throw new Error(s.error || ('HTTP ' + r.status));
        } catch (e) {
            if (current === id && first) sheetEl().innerHTML = '<div class="ns-pad" style="color:var(--status-stopped-fg)">' + esc(Tf('load_err', {e: e.message})) + '</div>';
            return;
        }
        if (current !== id) return;
        lastSheet = s;
        render(s, first);
        loadAlerts(id);
        if (first) loadDrivers(id);
    }

    function row(label, value) { return '<dt>' + esc(label) + '</dt><dd>' + value + '</dd>'; }
    function section(key, body, link) {
        return '<section class="ns-sec" data-sec="' + key + '"><div class="ns-sec-head"><h3>' + esc(T('sec_' + key)) + '</h3>'
            + (link || '') + '</div>' + body + '</section>';
    }
    function gauge(label, pct) {
        const na = pct == null;
        const cls = na ? ' na' : (pct >= 95 ? ' over' : (pct >= 80 ? ' warn' : ''));
        const v = na ? T('not_measured') : (Math.round(pct) + ' %');
        return '<div class="ns-load"><span class="ns-load-lab">' + esc(label) + '</span>'
            + '<span class="ctl-gauge' + cls + '" tabindex="0" role="meter" aria-label="' + esc(label) + '"'
            + (na ? '' : ' aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(pct) + '"') + '>'
            + '<span class="ctl-gauge-fill" style="transform:scaleX(' + (na ? 0 : Math.min(1, pct / 100)) + ')"></span>'
            + '<span class="ctl-gauge-val">' + esc(v) + '</span></span>'
            + '<span class="ns-load-val">' + esc(v) + '</span></div>';
    }

    function machineHtml(s) {
        const m = s.machine || {};
        const unknown = m.reported ? '—' : '<span class="meta">' + esc(Tf('agent_old', {v: AGENT_MACHINE})) + '</span>';
        let model = unknown;
        if (m.product) model = esc(m.vendor && !m.product.toLowerCase().startsWith(m.vendor.toLowerCase().split(' ')[0]) ? m.vendor + ' ' + m.product : m.product);
        let cpu = esc(m.cpu_model || '—');
        if (m.cores && m.threads) cpu += ' · ' + esc(Tf('cores_threads', {cores: m.cores, threads: m.threads}));
        else if (m.threads) cpu += ' · ' + esc(Tf('threads_only', {threads: m.threads}));
        if (m.sockets > 1) cpu += ' · ' + esc(Tf('sockets', {n: m.sockets}));
        const ram = m.ram_mb ? esc(Tf('gib', {n: Math.round(m.ram_mb / 1024)})) : '—';
        let gpu;
        if (m.gpus && m.gpus.length) gpu = esc(m.gpus.join(', '));
        else if (m.gpu_count) gpu = esc('NVIDIA ×' + m.gpu_count);
        else gpu = m.gpus ? esc(T('none')) : unknown;
        const bmc = esc(bmcLabel(m.vendor)) + ' · ' + ((s.bmc || {}).configured ? esc(T('bmc_configured'))
            : '<a href="#deploiement/nodes" data-ns="config">' + esc(T('bmc_missing')) + '</a>');
        return '<dl class="ns-grid">' + row(T('model'), model) + row(T('cpu'), cpu) + row(T('memory'), ram)
            + row(T('gpu'), gpu) + row(T('bmc'), bmc) + '</dl>';
    }

    function uptimeHtml(s) {
        const b = s.boot || {};
        const up = b.uptime_s != null ? b.uptime_s : (b.last_boot_ts ? Date.now() / 1000 - b.last_boot_ts : null);
        const bootTs = b.uptime_s != null ? Date.now() / 1000 - b.uptime_s : b.last_boot_ts;
        const booted = up != null ? esc(Tf('booted_val', {date: dateCourte(bootTs), dur: dur(up)})) : '—';
        const r = s.recovery;
        let rec;
        if (!r) rec = esc(T('rec_none'));
        else if (r.state === 'done') {
            rec = esc(Tf('rec_done', {up: r.up != null ? r.up : '?', total: r.total != null ? r.total : '?', dur: dur(r.duration_s)}));
            if (r.failed) rec += ' · <span style="color:var(--status-stopped-fg)">' + esc(Tf('rec_failed', {n: r.failed})) + '</span>';
        } else if (r.state === 'disabled') rec = recDisabled(r);
        else if (r.state === 'running') rec = esc(T('rec_running'));
        else rec = '<span style="color:var(--status-stopped-fg)">' + esc(T('rec_error')) + '</span>';
        return '<dl class="ns-grid">' + row(T('booted'), booted) + row(T('recovery'), rec) + '</dl>';
    }

    // Reprise automatique désactivée : la réparation des certificats est alors la seule reprise du
    // démarrage (cf. node_sheet.CERT_KEYS) — on dit ce qu'elle a fait, puis que le reste ne l'est pas.
    function recDisabled(r) {
        const c = r.certs;
        const off = '<span style="color:var(--status-warning-fg)">' + esc(T(c ? 'rec_disabled_short' : 'rec_disabled')) + '</span>';
        if (!c) return off;
        if (c.error) return '<span style="color:var(--status-stopped-fg)">' + esc(T('rec_certs_error')) + '</span> · ' + off;
        let t = esc(Tf('rec_certs', {n: c.repaired, total: c.broken != null ? c.broken : '?', dur: dur(c.duration_s)}));
        if (c.failed) t += ' · <span style="color:var(--status-stopped-fg)">' + esc(Tf('rec_failed', {n: c.failed})) + '</span>';
        return t + ' · ' + off;
    }
    function airText(c) {
        const a = c.air || {};
        return a.kind === 'output' ? T('dlg_air_output') : Tf('dlg_air_feeds', {name: a.output || '?'});
    }

    function loadHtml(s) {
        const l = s.load || {}, p = s.cores_pool;
        const pool = (p && p.total) ? Tf('pool', {used: p.used, total: p.total}) : T('pool_none');
        return gauge(l.cpu_scope === 'schedulable' ? T('load_cpu_shared') : T('load_cpu'), l.cpu_pct)
            + gauge(T('load_mem'), l.mem_pct) + gauge(T('load_disk'), l.disk_pct)
            + '<div class="meta ns-pool">' + esc(pool) + ' · <a href="#cpu" data-ns="cpu">' + esc(T('link_cpu')) + '</a></div>';
    }

    // Un DÉCOMPTE, pas la liste (demande de Cyril, 30/10) : le détail par image est dans
    // Déploiement → Build, à un clic. Seules comptent les images ATTENDUES sur ce nœud.
    function imagesHtml(s) {
        const imgs = Object.values(s.images || {}).filter(i => i.expected);
        if (!imgs.length) return '<span class="meta">' + esc(T('none')) + '</span>';
        if (imgs.some(i => i.unknown)) return '<span style="color:var(--status-warning-fg)">' + esc(T('img_unknown')) + '</span>';
        const n = imgs.filter(i => i.present).length;
        const col = n === imgs.length ? 'var(--status-running-fg)' : 'var(--status-stopped-fg)';
        return '<span style="color:' + col + '">' + esc(Tf('img_count', {n, total: imgs.length})) + '</span>';
    }

    function powerHtml(s) {
        let warn = '';
        if (s.all_in_one) warn += '<p>' + esc(T('warn_ctrl')) + '</p>';
        if (!(s.bmc || {}).configured) warn += '<p>' + esc(T('warn_no_bmc')) + '</p>';
        return (warn ? '<div class="ns-warn">' + warn + '</div>' : '')
            + '<div class="ns-power-btns">'
            + '<button type="button" class="btn btn-orange" data-ns="reboot">' + esc(T('reboot_btn')) + '</button>'
            + '<button type="button" class="btn btn-red" data-ns="poweroff">' + esc(T('poweroff_btn')) + '</button></div>';
    }

    function render(s, first) {
        const el = sheetEl();
        // Un rafraîchissement ne reconstruit pas la fiche : il ne remplace que les sections qui
        // bougent. Sinon le focus clavier (et la lecture d'un lecteur d'écran) sauterait toutes
        // les 10 s, et les pilotes hôte, lus une fois, s'effaceraient.
        if (!first && el.querySelector('.ns-head')) {
            el.querySelector('.ns-sub').innerHTML = subHtml(s);
            el.querySelector('.ns-head .ctl-led').classList.toggle('on', !!s.online);
            [['uptime', uptimeHtml], ['load', loadHtml], ['images', imagesHtml]].forEach(([k, f]) => {
                const sec = el.querySelector('.ns-sec[data-sec="' + k + '"]');
                if (sec) sec.querySelector('.ns-sec-head').nextElementSibling.outerHTML = '<div>' + f(s) + '</div>';
            });
            const c = el.querySelector('[data-ns="containers"]');
            if (c) c.textContent = Tf('containers_link', {n: s.containers});
            return;
        }
        el.setAttribute('aria-label', Tf('aria_sheet', {name: s.name}));
        el.innerHTML =
            '<div class="ns-head">'
            + '<div class="ns-title"><span class="ctl-led' + (s.online ? ' on' : '') + '" style="--ctl-led-col:var(--status-running-fg)" aria-hidden="true"></span>'
            + '<h2 id="ns-title">' + esc(s.name) + '</h2>'
            + '<span class="badge">' + esc(s.all_in_one ? T('role_all_in_one') : T('role_node')) + '</span>'
            + '<button type="button" class="btn ns-close" data-ns="close" aria-label="' + esc(T('close')) + '">✕</button></div>'
            + '<div class="meta ns-sub">' + subHtml(s) + '</div>'
            + '<div class="ns-links">'
            + '<a class="btn" href="/monitoring#nodes/' + s.id + '">' + esc(T('link_monitoring')) + '</a>'
            + '<a class="btn" href="#deploiement/nodes" data-ns="config">' + esc(T('link_config')) + '</a>'
            + '<a class="btn" href="#reseau/interfaces" data-ns="ifaces">' + esc(T('link_ifaces')) + '</a></div></div>'
            + '<div class="ns-body">'
            + section('machine', '<div>' + machineHtml(s) + '</div>')
            + section('uptime', '<div>' + uptimeHtml(s) + '</div>')
            + section('load', '<div>' + loadHtml(s) + '</div>')
            + section('containers', '', '<a href="/monitoring#nodes/' + s.id + '" data-ns="containers">' + esc(Tf('containers_link', {n: s.containers})) + '</a>')
            + section('images', '<div>' + imagesHtml(s) + '</div>', '<a href="#deploiement/build" data-ns="build">' + esc(T('link_build')) + '</a>')
            + section('drivers', '<div class="ns-drivers meta">' + esc(T('reading')) + '</div>')
            + section('power', powerHtml(s))
            + section('alerts', '<div class="ns-alerts meta">' + esc(T('reading')) + '</div>',
                      '<a href="/monitoring#logs">' + esc(T('link_alerts')) + '</a>')
            + '</div>';
    }
    function subHtml(s) {
        const parts = [s.host].concat(s.capabilities || []);
        let seen = s.online ? T('online') : T('offline');
        if (s.last_seen_s != null) seen += ', ' + Tf('seen', {age: dur(s.last_seen_s)});
        if (s.agent_version) parts.push(Tf('agent', {v: s.agent_version}));
        return esc(parts.filter(Boolean).join(' · ')) + ' · ' + esc(seen);
    }

    async function loadAlerts(id) {
        let rows = [];
        try { rows = await (await fetch('/api/alerts?node_id=' + id + '&limit=5')).json(); } catch (_) { return; }
        const box = current === id && sheetEl().querySelector('.ns-alerts');
        if (!box) return;
        box.classList.toggle('meta', !rows.length);
        box.innerHTML = rows.length ? rows.map(a => {
            const ts = Date.parse(a.timestamp);
            const col = a.niveau === 'error' ? 'var(--status-stopped-fg)' : a.niveau === 'warning' ? 'var(--status-warning-fg)' : 'var(--text-muted)';
            return '<div><span style="color:' + col + ';font-variant-numeric:tabular-nums">' + esc(isNaN(ts) ? '' : heure(ts / 1000)) + '</span> · ' + esc(a.message) + '</div>';
        }).join('') : esc(T('none'));
    }

    // Pilotes hôte : la sonde exécute le script du pilote sur l'hôte (quelques secondes) → lue une
    // fois à l'ouverture. Réservée à `settings.edit` (403 sinon) : la section disparaît alors.
    async function loadDrivers(id) {
        const box = () => current === id && sheetEl().querySelector('.ns-drivers');
        let kinds;
        try {
            const r = await fetch('/api/nodes/' + id + '/host-drivers');
            if (r.status === 403) { const b = box(); if (b) b.closest('.ns-sec').remove(); return; }
            kinds = ((await r.json()).kinds || []);
        } catch (_) { kinds = null; }
        if (!box()) return;
        if (!kinds) { box().textContent = T('drivers_err'); return; }
        if (!kinds.length) { box().textContent = T('none'); return; }
        const H = 'settings.hostdrv.';
        box().innerHTML = '<dl class="ns-grid ns-grid--right">' + kinds.map(k => {
            const kind = typeof k === 'string' ? k : k.kind;
            return '<dt>' + esc(window.t(H + kind + '.title')) + '</dt><dd data-kind="' + esc(kind) + '">' + esc(T('reading')) + '</dd>';
        }).join('') + '</dl>';
        box().classList.remove('meta');
        kinds.forEach(async (k) => {
            const kind = typeof k === 'string' ? k : k.kind;
            let txt = T('drivers_err'), col = 'var(--status-stopped-fg)';
            try {
                const j = await (await fetch('/api/nodes/' + id + '/host-drivers/' + kind + '/state')).json();
                if (j.ok !== false && j.plan) {
                    const v = (j.plan.params || {}).version;
                    txt = (v ? v + ' · ' : '') + window.t(H + kind + '.status.' + j.plan.status);
                    col = j.plan.status === 'ok' ? 'var(--status-running-fg)' : j.plan.status === 'refused' ? 'var(--status-stopped-fg)' : 'var(--status-warning-fg)';
                }
            } catch (_) { /* reste « illisible » */ }
            const b = box();
            const dd = b && b.querySelector('dd[data-kind="' + kind + '"]');
            if (dd) { dd.textContent = txt; dd.style.color = col; }
        });
    }

    // ─── Boîte de confirmation ─────────────────────────────────────────────────────────────
    // Construite ici plutôt qu'avec `mxlConfirm` : celle-ci ne sait pas débloquer son bouton sur
    // une SAISIE (exigée pour l'arrêt), et son piège de focus ne connaît que ses deux boutons.
    // Mêmes classes (`modal-overlay` / `modal-card`) : même apparence que toutes les boîtes.
    function busyText(b) {
        return (b || []).map(x => {
            if (x.kind === 'host_driver') return Tf('busy_host_driver', {d: window.t('settings.hostdrv.' + x.detail + '.title')});
            return Tf('busy_' + x.kind, {d: x.detail || ''});
        }).join(', ');
    }
    // La DÉCISION de la boîte, sans DOM (testée seule : tests/verif_fiche_noeud_ui.js).
    //   · sonde muette → rien n'est permis (on ne coupe pas à l'aveugle) ;
    //   · installation en cours → rien n'est permis, `force` n'y change rien (le serveur non plus) ;
    //   · processus en D → redémarrage FORCÉ seulement ; arrêt impossible ;
    //   · l'arrêt exige en plus la saisie du nom.
    function verdict(c, action) {
        if (!c || !c.probe_ok) return {allowed: false, force: false, needName: false};
        const blocked = (c.blocked || []).length > 0, busy = (c.busy || []).length > 0;
        const allowed = !busy && !(blocked && action === 'poweroff');
        return {allowed, force: allowed && action === 'reboot' && blocked, needName: allowed && action === 'poweroff'};
    }
    function nameMatches(typed, name) { return String(typed || '').trim() === String(name == null ? '' : name); }
    function blockedList(b) { return (b || []).slice(0, 4).map(p => p.comm + (p.wchan ? '/' + p.wchan : '')).join(', '); }

    function confirmPower(id, action) {
        const prevFocus = document.activeElement;
        const ov = document.createElement('div');
        ov.className = 'modal-overlay';
        ov.style.display = 'flex';
        ov.innerHTML = '<div class="modal-card ns-dlg" role="dialog" aria-modal="true" aria-labelledby="ns-dlg-t" aria-describedby="ns-dlg-b">'
            + '<h3 id="ns-dlg-t"></h3><div id="ns-dlg-b" class="ns-dlg-body"><p class="meta">' + esc(T('dlg_checking')) + '</p></div>'
            + '<p class="ns-dlg-result" role="status" aria-live="polite"></p>'
            + '<div class="flex-row ns-dlg-btns"><button type="button" class="btn" data-act="cancel">' + esc(T('cancel')) + '</button>'
            + '<button type="button" class="btn ' + (action === 'poweroff' ? 'btn-red' : 'btn-orange') + '" data-act="ok" disabled>'
            + esc(T(action === 'poweroff' ? 'do_poweroff' : 'do_reboot')) + '</button></div></div>';
        document.body.appendChild(ov);
        const card = ov.querySelector('.modal-card');
        const okBtn = ov.querySelector('[data-act=ok]'), cancelBtn = ov.querySelector('[data-act=cancel]');
        let check = null, sent = false, v = verdict(null, action), nameOk = false;
        const setOk = () => { okBtn.disabled = sent || !v.allowed || (v.needName && !nameOk); };
        const shut = () => { document.removeEventListener('keydown', onKey, true); ov.remove(); if (prevFocus && prevFocus.focus) prevFocus.focus(); };
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); shut(); return; }
            if (e.key !== 'Tab') return;
            const f = Array.from(card.querySelectorAll('button:not([disabled]), input:not([disabled]), a[href]'));
            if (!f.length) return;
            const i = f.indexOf(document.activeElement);
            e.preventDefault();
            f[e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === f.length - 1 ? 0 : i + 1)].focus();
        }
        document.addEventListener('keydown', onKey, true);
        ov.addEventListener('mousedown', e => { if (e.target === ov) shut(); });
        cancelBtn.addEventListener('click', shut);
        cancelBtn.focus();

        okBtn.addEventListener('click', async () => {
            if (okBtn.disabled) return;
            sent = true; setOk();
            const out = ov.querySelector('.ns-dlg-result');
            const force = v.force;
            let j;
            try {
                const r = action === 'poweroff'
                    ? await fetch('/api/nodes/' + id + '/host-poweroff', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})})
                    : await fetch('/api/mtl/reboot?node_id=' + id, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true, force})});
                j = await r.json();
            } catch (e) {
                // Le nœud porteur de l'orchestrateur peut couper la réponse elle-même : ce n'est
                // pas un échec, c'est le geste qui aboutit.
                j = check && check.all_in_one ? {ok: true} : {ok: false, error: String(e)};
            }
            if (j.ok) {
                out.style.color = 'var(--status-running-fg)';
                out.textContent = '✓ ' + T(action === 'poweroff' ? 'sent_poweroff' : (force || j.mode === 'sysrq' ? 'sent_reboot_force' : 'sent_reboot'))
                    + (check.all_in_one ? ' ' + T('sent_ctrl') : '');
            } else {
                out.style.color = 'var(--status-stopped-fg)';
                const e = j.error === 'probe_failed' ? T('chk_probe_failed')
                    : j.error === 'node_busy' ? Tf('chk_busy', {list: busyText(j.busy)})
                    : j.error === 'blocked_processes' ? Tf(action === 'poweroff' ? 'chk_d_poweroff' : 'chk_d_reboot', {n: (j.blocked || []).length, list: blockedList(j.blocked)})
                    : (j.msg || j.detail || j.error || '?');
                out.textContent = '✕ ' + Tf('refused', {e});
            }
            cancelBtn.textContent = T('done_close');
            okBtn.hidden = true;
            cancelBtn.focus();
        });

        (async () => {
            try {
                const r = await fetch('/api/nodes/' + id + '/power-check');
                check = await r.json();
                if (!r.ok || !check.ok) throw new Error(check.error || ('HTTP ' + r.status));
            } catch (e) {
                ov.querySelector('#ns-dlg-b').innerHTML = '<p style="color:var(--status-stopped-fg)">✕ ' + esc(Tf('load_err', {e: e.message})) + '</p>';
                return;
            }
            if (!document.body.contains(ov)) return;
            ov.querySelector('#ns-dlg-t').textContent = Tf(action === 'poweroff' ? 'dlg_poweroff_title' : 'dlg_reboot_title', {name: check.name});
            let html = '';
            if (check.all_in_one || (action === 'poweroff' && !check.bmc_configured)) {
                html += '<div class="ns-danger">';
                if (check.all_in_one) html += '<strong>' + esc(T('dlg_ctrl_head')) + '</strong> ' + esc(T(action === 'poweroff' ? 'dlg_ctrl_poweroff' : 'dlg_ctrl_reboot'));
                if (action === 'poweroff' && !check.bmc_configured) html += (check.all_in_one ? ' ' : '') + esc(T('dlg_no_bmc'));
                html += '</div>';
            }
            const cs = check.containers || [];
            html += '<div class="ns-cut"><div class="ns-cut-head">' + esc(cs.length ? Tf('dlg_cut', {n: cs.length}) : T('dlg_cut_none')) + '</div>'
                + cs.slice(0, CUT_SHOWN).map(c => '<div class="ns-cut-row"><span>' + esc(c.hostname || ('#' + c.vmid)) + '</span>'
                    + (c.onair ? '<span class="ns-onair">' + esc(airText(c)) + '</span>'
                       : '<span class="meta">' + esc(c.status === 'running' ? (c.type || '') : T('dlg_stopped')) + '</span>') + '</div>').join('')
                + (cs.length > CUT_SHOWN ? '<div class="meta">' + esc(Tf('dlg_more', {n: cs.length - CUT_SHOWN})) + '</div>' : '') + '</div>';
            // Les gardes : chacune dit son verdict, en clair — vert, ou la raison exacte.
            const ok = (txt) => '<li><span class="ns-ok" aria-hidden="true">✓</span> ' + esc(txt) + '</li>';
            const ko = (txt) => '<li class="ns-ko"><span aria-hidden="true">✕</span> ' + esc(txt) + '</li>';
            const warn = (txt) => '<li class="ns-warn-li"><span aria-hidden="true">⚠</span> ' + esc(txt) + '</li>';
            v = verdict(check, action);
            let checks = '';
            if (!check.probe_ok) checks += ko(T('chk_probe_failed'));
            else {
                const b = check.blocked || [];
                if (!b.length) checks += ok(Tf('chk_d_ok', {time: heure(check.checked_at, true)}));
                else if (action === 'reboot') checks += warn(Tf('chk_d_reboot', {n: b.length, list: blockedList(b)}));
                else checks += ko(Tf('chk_d_poweroff', {n: b.length, list: blockedList(b)}));
                if ((check.busy || []).length) checks += ko(Tf('chk_busy', {list: busyText(check.busy)}));
                else checks += ok(T('chk_busy_ok'));
            }
            if (v.force) okBtn.textContent = T('do_reboot_force');
            html += '<ul class="ns-checks">' + checks + '</ul>';
            if (v.needName) {
                html += '<label class="ns-type" for="ns-type-in">' + esc(Tf('type_name', {name: check.name})) + '</label>'
                    + '<input id="ns-type-in" type="text" autocomplete="off" spellcheck="false" placeholder="' + esc(check.name) + '">';
            }
            ov.querySelector('#ns-dlg-b').innerHTML = html;
            const inp = ov.querySelector('#ns-type-in');
            if (inp) {
                inp.addEventListener('input', () => { nameOk = nameMatches(inp.value, check.name); setOk(); });
                inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !okBtn.disabled) okBtn.click(); });
                inp.focus();
            }
            setOk();
        })();
    }

    // ─── Clics délégués dans la fiche ──────────────────────────────────────────────────────
    document.addEventListener('click', (e) => {
        const a = e.target.closest && e.target.closest('#node_sheet [data-ns]');
        if (!a || current == null) return;
        const what = a.dataset.ns, id = current;
        if (what === 'close') { e.preventDefault(); close(); }
        else if (what === 'config') { e.preventDefault(); goConfig(id); }
        else if (what === 'ifaces') { e.preventDefault(); goIfaces(id); }
        else if (what === 'cpu') { e.preventDefault(); goCpu(id); }
        else if (what === 'build') { e.preventDefault(); goBuild(); }
        else if (what === 'reboot' || what === 'poweroff') { e.preventDefault(); confirmPower(id, what); }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && current != null && !document.querySelector('.ns-dlg')
            && sheetEl() && sheetEl().contains(document.activeElement)) close();
    });

    window.NodeSheet = {
        open, close, confirmPower,
        // Appelé par dessinerTopologieCluster après chaque rendu du schéma.
        afterTopology() { bindTopology(); markSelected(); },
        _dur: dur, _recDisabled: recDisabled, _airText: airText, _verdict: verdict, _nameMatches: nameMatches, _imagesHtml: imagesHtml,
    };
})();
