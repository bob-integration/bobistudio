/* ═══ BobiContainerPanel: the « container settings » side panel (2026-11-07) ════════════════════
 *
 * ONE panel, opened from two places with the same code:
 *   · the ⚙ of the active instance on a plugin page (templates/plugin_section.html);
 *   · « Container settings… » in a card's Actions menu on the Containers page (static/scripts.js).
 * Until then the plugin page had a 300px popover (config fields + snapshots) and the Containers page
 * reopened the deploy palette, which still offered a « CPU mode » the server had stopped honouring.
 * The deploy palette stays for the FIRST deployment of a container.
 *
 * Four blocks:
 *   1. Configuration — the plugin's SYSTEM fields (`scope:user` ones belong to the plugin console,
 *      `hidden` ones are never rendered), Apply, config snapshots, and the mounted plugin's
 *      `settingsActions` (only the plugin page has a mounted plugin, so only it passes them);
 *   2. Resources — `GET /api/containers/<vmid>/resources_view` (app/container_settings.py): level,
 *      dedicated cores (hot) or CPU cap (recreates), memory (recreates), coherence, links;
 *   3. Network and project — egress, project of a media-volume container;
 *   4. Actions — the card's shortcuts, Destroy apart.
 * Without `containers.deploy` the panel is a read-only view.
 *
 * Buttons and the level selector come from the shared catalogue (`ctl-*`, BobiControls). Labels go
 * through i18n (`containers.panel.*`, plus the existing keys reused as they are).
 *
 *   BobiContainerPanel.open(vmid, {hostname, type, schema, settingsPanel, settingsActions, inst,
 *                                  engineList, toast, canDeploy, canDestroy, onClose})
 *   BobiContainerPanel.close()
 *   BobiContainerPanel.fields — the config-field helpers (rendered with the palette's conventions).
 */
(function () {
    'use strict';

    const T = (k, d) => {
        try { const v = window.t ? window.t(k) : null; if (v && v !== k) return v; } catch (e) { /* fallback */ }
        return d !== undefined ? d : k;
    };
    const fmt = (k, vals) => {
        let s = T(k);
        for (const n in (vals || {})) s = s.split('{' + n + '}').join(vals[n] == null ? '' : String(vals[n]));
        return s;
    };
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

    // ─── Config fields (moved from plugin_section.html, same data-cf/data-cft conventions as the
    // Containers palette) ───────────────────────────────────────────────────────────────────────
    function fieldHtml(f) {
        const k = esc(f.key);
        // Setting applied HOT by the container (`live` in config_schema): marked, so that « Apply
        // redeploys » does not announce a cut that will not happen.
        const lbl = esc(f.label || f.key) + (f.live ? ' <span class="badge">' + esc(T('section.settings_live_badge')) + '</span>' : '');
        const ph = f.placeholder != null ? ' placeholder="' + esc(f.placeholder) + '"' : '';
        const val = (f.default != null) ? ' value="' + esc(f.default) + '"' : '';
        const help = f.help ? '<small class="meta">' + esc(f.help) + '</small>' : '';
        if (f.type === 'checkbox' || f.type === 'bool')
            return '<div class="palette-field-check" data-cf-wrap="' + k + '"><label>'
                + '<input type="checkbox" class="ios-toggle" data-cf="' + k + '" data-cft="checkbox"' + (f.default ? ' checked' : '') + '> ' + lbl + '</label>' + help + '</div>';
        let input;
        if (f.type === 'number') {
            const a = [f.min != null ? 'min="' + esc(f.min) + '"' : '', f.max != null ? 'max="' + esc(f.max) + '"' : '',
                       f.step != null ? 'step="' + esc(f.step) + '"' : ''].join(' ');
            input = '<input type="number" data-cf="' + k + '" data-cft="number" ' + a + ph + val + '>';
        } else if (f.type === 'multiselect') {
            // Same contract and same catalogue control as the palette (scripts.js): mounted after
            // the HTML is inserted (cf. mountFields).
            input = '<div data-cf="' + k + '" data-cft="multiselect" data-cf-opts="'
                + esc(JSON.stringify({ o: f.options || [], v: f.default || [] })) + '"></div>';
        } else if (f.type === 'select') {
            const opts = (f.options || []).map((o) => '<option value="' + esc(o.value) + '"'
                + ((f.default != null && String(o.value) === String(f.default)) ? ' selected' : '') + '>'
                + esc(o.label || o.value) + '</option>').join('');
            input = '<select data-cf="' + k + '" data-cft="select">' + opts + '</select>';
        } else {
            input = '<input type="text" data-cf="' + k + '" data-cft="text"' + ph + val + '>';
        }
        return '<div class="palette-field" data-cf-wrap="' + k + '"><label>' + lbl + '</label>' + input + help + '</div>';
    }
    function mountFields(box) {
        if (!box || !window.MXLControls) return;
        box.querySelectorAll('[data-cft="multiselect"]').forEach((el) => {
            if (el._ctl) return;
            let d = { o: [], v: [] };
            try { d = JSON.parse(el.dataset.cfOpts || '{}'); } catch (e) { /* field without options */ }
            el._ctl = window.MXLControls.chooseList(el, {
                options: (d.o || []).map((o) => ({ value: o.value, label: o.label || o.value })),
                valeurs: Array.isArray(d.v) ? d.v : (d.v ? [d.v] : []),
                vide: '— aucun —', ajouter: '+ Ajouter…', tout: 'tout est choisi' });
        });
    }
    function readFields(box) {
        const out = {};
        box.querySelectorAll('[data-cf]').forEach((el) => {
            const k = el.dataset.cf, t = el.dataset.cft;
            if (t === 'checkbox') out[k] = el.checked;
            else if (t === 'number') { const v = parseFloat(el.value); if (!isNaN(v)) out[k] = v; }
            else if (t === 'multiselect') out[k] = el._ctl ? el._ctl.value() : [];
            else out[k] = el.value;
        });
        return out;
    }
    function applyVisibility(box, schema) {
        const vals = readFields(box);
        (schema || []).forEach((f) => {
            if (!f.visible_if) return;
            const wrap = box.querySelector('[data-cf-wrap="' + CSS.escape(f.key) + '"]');
            if (wrap) wrap.hidden = !Object.entries(f.visible_if).every(([k, v]) =>
                Array.isArray(v) ? v.some((x) => String(vals[k]) === String(x)) : String(vals[k]) === String(v));
        });
    }
    // Bounds of the config_schema (min/max/step already set as HTML attributes): refused BEFORE
    // sending, with the browser's message. The server revalidates (plugins.validate_config).
    function validateFields(box) {
        for (const el of box.querySelectorAll('[data-cf]')) {
            if (!el.checkValidity || el.checkValidity()) continue;
            const w = el.closest('[data-cf-wrap]');
            const lbl = w ? ((w.querySelector('label') || {}).textContent || '') : '';
            el.reportValidity();
            return ((lbl || el.dataset.cf).trim()) + ' : ' + el.validationMessage;
        }
        return null;
    }
    // System fields only: `scope:user` ones are the plugin console's (live), `hidden: true` ones stay
    // in the schema (coerce_config, deploy) but are never rendered (global settings such as the slice
    // mode). A plugin whose UI owns its structure opts out with manifest `settings_panel: false`.
    function systemFields(schema, settingsPanel) {
        if (settingsPanel === false) return [];
        return (schema || []).filter((f) => !f.hidden && (f.scope || 'system') !== 'user');
    }

    // ─── DOM helpers ────────────────────────────────────────────────────────────────────────
    function h(tag, props, kids) {
        const el = document.createElement(tag);
        for (const k in (props || {})) {
            const v = props[k];
            if (v == null || v === false) continue;
            if (k === 'class') el.className = v;
            else if (k === 'text') el.textContent = v;
            else if (k === 'onclick') el.onclick = v;
            else if (k === 'value') el.value = v;
            else if (k === 'hidden') el.hidden = true;
            else if (k === 'disabled') el.disabled = true;
            else el.setAttribute(k, v === true ? '' : String(v));
        }
        (kids || []).forEach((c) => {
            if (c == null || c === false) return;
            el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return el;
    }
    const push = (label, onclick, extra) => h('button', Object.assign(
        { type: 'button', class: 'ctl-push ctl-push--small', text: label, onclick }, extra || {}));
    const line = (text, cp, cls) => h('p', { class: cls || 'meta', 'data-cp': cp, text });
    function say(el, msg, kind) {
        if (!el) return;
        el.textContent = msg || '';
        el.className = 'cpanel-status' + (kind ? ' ' + kind : '');
    }
    async function call(url, method, body) {
        const init = { method: method || 'GET' };
        if (body !== undefined) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(body); }
        try {
            const r = await fetch(url, init);
            let j = {};
            try { j = await r.json(); } catch (e) { /* empty body */ }
            return { ok: r.ok, code: r.status, j: j || {} };
        } catch (e) {
            return { ok: false, code: 0, j: { error: String(e && e.message || e) } };
        }
    }
    const parseDc = (c) => {
        try { return typeof c.deploy_config === 'string' ? JSON.parse(c.deploy_config || '{}') : (c.deploy_config || {}); }
        catch (e) { return {}; }
    };
    const perm = (p, fallback) => (typeof window.hasPerm === 'function' ? !!window.hasPerm(p) : fallback);

    // ─── State ──────────────────────────────────────────────────────────────────────────────
    let root = null, st = null, seq = 0;

    function ensureRoot() {
        if (root) return root;
        root = h('aside', { class: 'cpanel', id: 'bobi-cpanel', role: 'dialog', 'aria-modal': 'false',
                            'aria-labelledby': 'cpanel-title', hidden: true });
        document.body.appendChild(root);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && root && !root.hidden) close();
        });
        return root;
    }

    function close() {
        seq++;
        if (root) { root.hidden = true; root.replaceChildren(); }
        const prev = st;
        st = null;
        if (prev && typeof prev.opts.onClose === 'function') { try { prev.opts.onClose(); } catch (e) { console.error(e); } }
    }

    function block(id, title) {
        const body = h('div', { class: 'cpanel-block-body' });
        const el = h('section', { class: 'cpanel-block', 'data-cp-block': id },
                     [h('h3', { class: 'cpanel-block-title', text: title }), body]);
        return { el, body };
    }

    async function open(vmid, opts) {
        opts = opts || {};
        vmid = Number(vmid);
        const my = ++seq;
        const r = ensureRoot();
        const perms = {
            deploy: opts.canDeploy != null ? !!opts.canDeploy : perm('containers.deploy', false),
            destroy: opts.canDestroy != null ? !!opts.canDestroy : perm('containers.delete', false),
        };
        const blocks = {
            config: block('config', T('containers.panel.block_config')),
            resources: block('resources', T('containers.panel.block_resources')),
            network: block('network', T('containers.panel.block_network')),
            actions: block('actions', T('containers.panel.block_actions')),
        };
        st = { vmid, opts, perms, my, c: null, rv: null, blocks,
               name: opts.hostname || ('#' + vmid) };
        const host = h('span', { class: 'cpanel-host', 'data-cp': 'host',
                                 text: (opts.hostname ? opts.hostname + ' ' : '') + '#' + vmid });
        const closeBtn = push('✕', close, { class: 'ctl-push ctl-push--small cpanel-close', 'data-cp': 'close',
                                            'aria-label': T('containers.panel.close'), title: T('containers.panel.close') });
        r.replaceChildren(
            h('header', { class: 'cpanel-head' }, [
                h('h2', { class: 'cpanel-title', id: 'cpanel-title' }, [T('containers.panel.title'), ' ', host]),
                closeBtn]),
            perms.deploy ? null : line(T('containers.panel.read_only'), 'read-only', 'meta cpanel-ro'),
            h('div', { class: 'cpanel-body' }, [blocks.config.el, blocks.resources.el, blocks.network.el, blocks.actions.el]));
        r.hidden = false;
        try { closeBtn.focus(); } catch (e) { /* not focusable yet */ }
        Object.values(blocks).forEach((b) => b.body.replaceChildren(line('…', 'loading')));

        const rc = await call('/api/containers/' + vmid + '/config');
        if (my !== seq) return;
        if (!rc.ok) {
            blocks.config.body.replaceChildren(line(T('section.settings_load_error'), 'load-error', 'cpanel-status err'));
            ['resources', 'network', 'actions'].forEach((k) => blocks[k].body.replaceChildren());
            return;
        }
        st.c = rc.j;
        st.dc = parseDc(rc.j);
        if (rc.j.hostname) { st.name = rc.j.hostname; host.textContent = rc.j.hostname + ' #' + vmid; }
        renderConfig();
        renderActions();
        blocks.network.body.replaceChildren(
            st.egressBox = h('div', { class: 'cpanel-sub', 'data-cp': 'egress-box' }),
            st.projectBox = h('div', { class: 'cpanel-sub', 'data-cp': 'project-box' }),
            st.netNone = line('—', 'net-none'));
        await Promise.all([loadResources(), loadEgress()]);
    }

    // ─── 1. Configuration ───────────────────────────────────────────────────────────────────
    function renderConfig() {
        const { opts, perms, blocks, vmid } = st;
        const B = blocks.config.body;
        const type = opts.type || st.dc.type || '';
        const params = st.dc.params || {};
        const schema = opts.schema || (window.PLUGIN_CONFIG_SCHEMAS || {})[type] || [];
        const sysF = systemFields(schema, opts.settingsPanel);
        const kids = [];
        // Engine picker (custom 2110 tabs, several nodes): reopens the panel on the chosen engine.
        if (opts.engineList && opts.engineList.length > 1) {
            const sel = h('select', { 'data-cp': 'engine' }, opts.engineList.map((e) =>
                h('option', { value: String(e.vmid), selected: e.vmid === vmid, text: e.hostname || ('#' + e.vmid) })));
            sel.value = String(vmid);
            sel.onchange = () => {
                const e = opts.engineList.find((x) => String(x.vmid) === sel.value);
                if (e) open(e.vmid, Object.assign({}, opts, { hostname: e.hostname,
                    inst: { vmid: e.vmid, version: e.version, hostname: e.hostname } }));
            };
            kids.push(h('div', { class: 'palette-field' }, [h('label', { text: T('section.engine_pick') }), sel]));
        }
        // The MOUNTED plugin's own actions (manifest ⇒ UI: `settingsActions(inst)` → [{label, run}]),
        // first — e.g. the mixer's setup assistant. Only with the deploy right; the plugin filters too.
        if (perms.deploy && typeof opts.settingsActions === 'function') {
            let acts = [];
            try { acts = opts.settingsActions(opts.inst || { vmid }) || []; } catch (e) { acts = []; }
            if (acts.length) kids.push(h('div', { class: 'cpanel-plugin-actions', 'data-cp': 'plugin-actions' },
                acts.map((a) => h('button', { type: 'button', class: 'ctl-push ctl-push--wide', text: a.label,
                    onclick: () => { close(); try { a.run(); } catch (e) { console.error(e); } } }))));
        }
        const status = h('div', { class: 'cpanel-status', role: 'status', 'data-cp': 'config-status' });
        if (sysF.length) {
            const box = h('div', { class: 'cpanel-fields', 'data-cp': 'fields' });
            box.innerHTML = '<fieldset class="palette-group"><legend class="palette-group-title">'
                + esc(T('section.settings_system')) + '</legend>' + sysF.map(fieldHtml).join('') + '</fieldset>';
            mountFields(box);
            box.querySelectorAll('[data-cf]').forEach((el) => {
                const k = el.dataset.cf;
                if (k in params) {
                    if (el.dataset.cft === 'checkbox') el.checked = !!params[k];
                    else if (el.dataset.cft === 'multiselect') { if (el._ctl && el._ctl.set) el._ctl.set(params[k]); }
                    else el.value = params[k];
                }
                el.addEventListener('change', () => applyVisibility(box, sysF));
                if (!perms.deploy) el.disabled = true;
            });
            applyVisibility(box, sysF);
            kids.push(box);
            if (perms.deploy) {
                kids.push(line(T(sysF.every((f) => f.live) ? 'section.settings_live_hint'
                    : (sysF.some((f) => f.live) ? 'section.settings_mixed_hint' : 'section.settings_redeploy_hint')),
                    'config-hint', 'meta cpanel-hint'));
                const apply = h('button', { type: 'button', class: 'ctl-push', 'data-cp': 'config-apply',
                                            text: T('section.settings_apply') });
                apply.onclick = () => applyConfig(box, status, apply);
                kids.push(apply);
            }
        } else {
            kids.push(line(T('containers.panel.no_fields'), 'no-fields'));
        }
        kids.push(status);
        // Config snapshots (every type, decided 2026-07-05).
        const list = h('div', { class: 'cpanel-snaps', 'data-cp': 'snaps' }, [line('…')]);
        const snapKids = [h('legend', { class: 'palette-group-title', text: T('section.cfgsnap_title') }), list];
        if (perms.deploy) {
            const name = h('input', { type: 'text', 'data-cp': 'snap-name', placeholder: T('section.cfgsnap_name_ph') });
            snapKids.push(h('div', { class: 'cpanel-row' }, [name,
                push(T('section.cfgsnap_save'), () => snapSave(name, list), { 'data-cp': 'snap-save' })]));
        }
        kids.push(h('fieldset', { class: 'palette-group' }, snapKids));
        B.replaceChildren(...kids);
        snapsLoad(list);
    }

    async function applyConfig(box, status, btn, confirmed) {
        const s = st;
        const bad = validateFields(box);
        if (bad) { say(status, '✕ ' + bad, 'err'); toast(bad, 'error'); return; }
        btn.disabled = true;   // no double click / double redeploy
        say(status, T('section.settings_applying'));
        const r = await call('/api/containers/' + s.vmid + '/plugin_config', 'POST',
                             { params: readFields(box), confirm: !!confirmed });
        btn.disabled = false;
        if (r.code === 409 && r.j.needs_confirm) {
            say(status, '');
            if (window.confirm(r.j.reason || '?')) return applyConfig(box, status, btn, true);
            return;
        }
        if (r.ok) { say(status, T('section.settings_applied'), 'ok'); toast(T('section.settings_applied'), 'info'); }
        else {
            // 409 {error, pending:true} = a redeploy already running: shown as a normal error.
            const m = r.j.error || T('section.settings_error');
            say(status, '✕ ' + m, 'err'); toast(m, 'error');
        }
    }

    function toast(msg, kind) {
        const f = st && st.opts.toast;
        if (typeof f === 'function') { try { f(msg, kind); } catch (e) { /* toast host absent */ } }
        else if (typeof window.showToast === 'function') window.showToast(msg, kind);
    }

    async function snapsLoad(list) {
        const s = st;
        const r = await call('/api/containers/' + s.vmid + '/config_snapshots');
        if (s !== st) return;
        const snaps = r.ok ? (r.j.snapshots || []) : [];
        if (!snaps.length) { list.replaceChildren(line('—')); return; }
        list.replaceChildren(...snaps.map((sn) => h('div', { class: 'cpanel-row', 'data-cp': 'snap' }, [
            h('span', { class: 'cpanel-snap-name' }, [sn.name || '', ' ', h('small', { class: 'meta', text: sn.created_at || '' })]),
            s.perms.deploy ? push(T('section.cfgsnap_restore'), () => snapRestore(sn.id), { 'data-cp': 'snap-restore' }) : null,
            s.perms.deploy ? push('✕', async () => {
                await call('/api/containers/' + s.vmid + '/config_snapshots/' + sn.id, 'DELETE');
                snapsLoad(list);
            }, { 'data-cp': 'snap-delete', 'aria-label': T('containers.panel.snap_delete'), title: T('containers.panel.snap_delete') }) : null,
        ])));
    }
    async function snapSave(input, list) {
        const name = (input.value || '').trim();
        if (!name) return;
        const r = await call('/api/containers/' + st.vmid + '/config_snapshots', 'POST', { name });
        if (!r.ok) { toast(r.j.error || ('HTTP ' + r.code), 'error'); return; }
        input.value = '';
        toast(T('section.cfgsnap_saved'));
        snapsLoad(list);
    }
    async function snapRestore(sid, confirmed) {
        const r = await call('/api/containers/' + st.vmid + '/config_snapshots/' + sid + '/restore', 'POST',
                             confirmed ? { confirm: true } : {});
        if (r.code === 409 && r.j.needs_confirm) {
            if (window.confirm((r.j.reason || '') + '\n\n' + T('section.cfgsnap_confirm'))) return snapRestore(sid, true);
            return;
        }
        if (!r.ok) { toast(r.j.error || ('HTTP ' + r.code), 'error'); return; }
        toast(T('section.cfgsnap_restoring'));
    }

    // ─── 2. Resources ───────────────────────────────────────────────────────────────────────
    async function loadResources() {
        const s = st;
        const r = await call('/api/containers/' + s.vmid + '/resources_view');
        if (s !== st) return;
        s.rv = r.ok ? r.j : null;
        renderResources();
        renderProject();
    }

    function renderResources() {
        const s = st, rv = s.rv, P = s.perms;
        const B = s.blocks.resources.body;
        if (!rv) { B.replaceChildren(line(T('containers.panel.res_unavailable'), 'res-unavailable')); return; }
        const status = h('div', { class: 'cpanel-status', role: 'status', 'data-cp': 'res-status' });
        s.resStatus = status;
        const kids = [];
        if (rv.engine) {
            kids.push(line(T('containers.panel.res_engine'), 'res-engine'));
        } else {
            if (rv.node_io) {
                kids.push(line(T('containers.panel.res_node_io'), 'res-node-io'));
            } else {
                // Level: what passes first under saturation, and for Dedicated the place itself.
                const lv = h('div', { class: 'cpanel-level', 'data-cp': 'level' });
                kids.push(h('div', { class: 'palette-field' }, [h('label', { text: T('projects.res_level') }), lv,
                    rv.level_source ? h('small', { class: 'meta', 'data-cp': 'level-source',
                        text: T('settings.cores.weight.src_' + rv.level_source, '') }) : null]));
                let ctl = null;
                if (window.BobiControls) {
                    ctl = window.BobiControls.create('buttons', lv, {
                        options: (rv.levels || []).map((l) => ({ value: l.id, label: T('settings.cores.weight.' + l.id, l.id) })),
                        value: rv.level, showLabel: false, label: T('projects.res_level'), disabled: !P.deploy });
                    ctl.on('change', (v) => setLevel(v, ctl));
                } else lv.textContent = T('settings.cores.weight.' + rv.level, rv.level);
                if (rv.dedicated) kids.push(...dedicatedCores(rv, P));
                else kids.push(...cpuCap(rv, P));
            }
            kids.push(...memory(rv, P));
            if (rv.coherent === false) {
                kids.push(h('div', { class: 'cpanel-warn', role: 'alert', 'data-cp': 'incoherent' }, [
                    h('span', { text: fmt('containers.panel.res_incoherent', { applied: rv.applied || '—', reserved: rv.reserved || '—' }) }),
                    P.deploy ? push(T('containers.card.recreate'), () => recreate(), { 'data-cp': 'recreate' }) : null]));
            }
        }
        // Links: the node's cores window, and each project's Resources view.
        const links = h('div', { class: 'cpanel-links' });
        if (rv.node && window.BobiCores) {
            links.appendChild(h('a', { href: '#', class: 'cpanel-link', 'data-cp': 'node-link',
                text: fmt('containers.panel.res_node_link', { node: rv.node.name || ('#' + rv.node.id) }),
                onclick: (e) => { if (e && e.preventDefault) e.preventDefault();
                                  window.BobiCores.ouvrir(rv.node.id, rv.node.name || ''); } }));
        }
        (rv.projects || []).forEach((p) => links.appendChild(h('a', {
            href: '/projects#resources-' + p.id, class: 'cpanel-link', 'data-cp': 'project-link',
            text: fmt('containers.panel.res_project_link', { name: p.name || ('#' + p.id) }) })));
        kids.push(links, status);
        B.replaceChildren(...kids);
    }

    function dedicatedCores(rv, P) {
        const n = rv.cores || 0;
        const out = [h('div', { class: 'palette-field' }, [
            h('label', { text: T('containers.panel.res_dedicated_cores') }),
            h('div', { class: 'cpanel-row' }, [
                push('−', () => setCores(n - 1), { 'data-cp': 'cores-minus', 'aria-label': T('containers.panel.res_cores_less'),
                                                   disabled: !P.deploy || n <= 1 }),
                h('span', { class: 'ctl-readout' }, [h('b', { 'data-cp': 'cores', text: String(n) })]),
                push('+', () => setCores(n + 1), { 'data-cp': 'cores-plus', 'aria-label': T('containers.panel.res_cores_more'),
                                                   disabled: !P.deploy }),
                (rv.node && rv.free_dedicated != null) ? h('span', { class: 'meta', 'data-cp': 'free',
                    text: fmt('containers.panel.res_free', { free: rv.free_dedicated, node: rv.node.name || '' }) }) : null]),
        ])];
        out.push(line(fmt('containers.panel.res_reserved', { cores: rv.reserved || '—' }), 'reserved'));
        out.push(line(T('containers.panel.res_cores_hot'), 'cores-hint', 'meta cpanel-hint'));
        return out;
    }

    function cpuCap(rv, P) {
        // A CPU cap on the shared pool (`--cpus`): a number, or « none » — `cores: 0` is the
        // explicit no-cap override (the manifest may set a cap itself, so « follow the manifest »
        // is not « no cap »). Both recreate the container: same confirm.
        const out = [h('div', { class: 'palette-field' }, [
            h('label', { text: T('containers.panel.res_cap') }),
            h('span', { 'data-cp': 'cap', text: rv.cap ? fmt('containers.panel.res_cap_value', { n: rv.cap })
                                                         : T('containers.panel.res_cap_none') })])];
        if (P.deploy) {
            const inp = h('input', { type: 'number', min: '1', max: '256', step: '1', 'data-cp': 'cap-input',
                                     value: rv.cap ? String(rv.cap) : '', 'aria-label': T('containers.panel.res_cap') });
            out.push(h('div', { class: 'cpanel-row' }, [inp, push(T('containers.panel.apply'), () => {
                const v = parseInt(inp.value, 10);
                if (!(v >= 1 && v <= 256)) { say(st.resStatus, '✕ ' + T('containers.panel.res_cap'), 'err'); return; }
                setRecreating({ cores: v });
            }, { 'data-cp': 'cap-apply' }),
            rv.cap ? push(T('containers.panel.res_cap_remove'), () => setRecreating({ cores: 0 }),
                          { 'data-cp': 'cap-remove' }) : null]));
            out.push(line(T('containers.panel.res_recreate_hint'), 'cap-hint', 'meta cpanel-hint'));
        }
        return out;
    }

    function memory(rv, P) {
        const out = [h('div', { class: 'palette-field' }, [
            h('label', { text: T('containers.panel.res_memory') }),
            P.deploy ? null : h('span', { 'data-cp': 'memory', text: rv.memory ? String(rv.memory) : '—' })])];
        if (P.deploy) {
            const inp = h('input', { type: 'number', min: '64', max: '1048576', step: '64', 'data-cp': 'mem-input',
                                     value: rv.memory ? String(rv.memory) : '', 'aria-label': T('containers.panel.res_memory') });
            out[0].appendChild(h('div', { class: 'cpanel-row' }, [inp, push(T('containers.panel.apply'), () => {
                const v = parseInt(inp.value, 10);
                if (!(v >= 64)) { say(st.resStatus, '✕ ' + T('containers.panel.res_memory'), 'err'); return; }
                if (v === rv.memory) return;
                setRecreating({ memory: v });
            }, { 'data-cp': 'mem-apply' })]));
        }
        return out;
    }

    const patchResources = (body) => call('/api/containers/' + st.vmid + '/resources', 'PATCH', body);
    function refusal(r, noRoomKey) {
        if (r.j && r.j.error === 'no_room') return fmt(noRoomKey, r.j);
        return fmt('containers.panel.res_failed', { error: (r.j && (r.j.error || r.j.detail)) || ('HTTP ' + r.code) });
    }

    async function setLevel(v, ctl) {
        const s = st, prev = s.rv.level;
        if (v === prev) return;
        const r = await patchResources({ cpu_weight: v });
        if (s !== st) return;
        if (!r.ok) {
            // The level stays as it was (the server changed nothing): the control says so too.
            ctl.set(prev);
            say(s.resStatus, '✕ ' + refusal(r, 'projects.res_level_no_room'), 'err');
            return;
        }
        await loadResources();
        say(st && st.resStatus, T('containers.panel.res_hot_ok'), 'ok');
    }

    async function setCores(n) {
        const s = st;
        if (n < 1) return;
        const r = await patchResources({ cores: n });
        if (s !== st) return;
        if (!r.ok) { say(s.resStatus, '✕ ' + refusal(r, 'containers.panel.res_cores_no_room'), 'err'); return; }
        const hot = !(r.j.cores && r.j.cores.hot === false);
        await loadResources();
        say(st && st.resStatus, T(hot ? 'containers.panel.res_hot_ok' : 'containers.panel.res_cores_stopped'), 'ok');
    }

    // CPU cap and memory are part of the container's spec: they apply by RECREATING it. Said before
    // (confirm) and after (the status the server answers).
    async function setRecreating(body) {
        const s = st;
        if (!window.confirm(fmt('containers.panel.res_confirm_recreate', { name: s.name }))) return;
        const r = await patchResources(body);
        if (s !== st) return;
        if (!r.ok) { say(s.resStatus, '✕ ' + refusal(r, 'containers.panel.res_cores_no_room'), 'err'); return; }
        say(s.resStatus, T(r.j.status === 'differe_prochain_demarrage' ? 'containers.panel.res_deferred'
                           : (r.j.status === 'inchange' ? 'containers.panel.res_unchanged' : 'containers.panel.res_recreating')), 'ok');
    }

    // ─── 3. Network and project ─────────────────────────────────────────────────────────────
    function netShown() { if (st && st.netNone) st.netNone.hidden = !!(st.egressBox.firstChild || st.projectBox.firstChild); }

    async function loadEgress() {
        const s = st;
        const r = await call('/api/containers/' + s.vmid + '/egress');
        if (s !== st) return;
        if (!r.ok) { netShown(); return; }   // no egress to show (or no right to read it)
        const j = r.j;
        const sel = h('select', { 'data-cp': 'egress', disabled: !s.perms.deploy }, [
            h('option', { value: 'node', text: T('containers.egress.node') }),
            h('option', { value: 'internet', text: T('containers.egress.internet'), disabled: j.internet_possible === false })]);
        sel.value = j.egress || 'node';
        let current = sel.value;
        // Shown even when « Internet » is impossible: the option is then disabled WITH its reason.
        const hint = h('div', { class: 'meta', 'data-cp': 'egress-hint', text: j.internet_possible === false
            ? T('containers.egress.indispo') + ' — ' + (j.internet_refus || '')
            : T('containers.egress.note') + (j.iface ? ' (' + j.iface + ')' : '') });
        sel.onchange = async () => {
            const want = sel.value;
            if (want === current) return;
            hint.textContent = '…';
            const rr = await call('/api/containers/' + s.vmid + '/egress', 'PATCH', { egress: want });
            if (rr.ok && rr.j.ok) {
                current = rr.j.egress || want;
                hint.textContent = '✓ ' + (rr.j.message || '') + ' — ' + T('containers.egress.note');
            } else {
                // Back to the REAL value: leaving the refused choice would show an egress the
                // container does not have.
                sel.value = current;
                hint.textContent = '✕ ' + (rr.j.error || ('HTTP ' + rr.code));
            }
        };
        s.egressBox.replaceChildren(h('div', { class: 'palette-field' }, [
            h('label', { text: T('containers.egress.label') }), sel, hint]));
        netShown();
    }

    async function renderProject() {
        const s = st;
        if (!s.rv || !s.rv.media_volume) { netShown(); return; }
        const r = await call('/api/projects');
        if (s !== st) return;
        const projs = (r.ok && Array.isArray(r.j)) ? r.j : [];
        const sel = h('select', { 'data-cp': 'project', disabled: !s.perms.deploy },
            [h('option', { value: '', text: T('containers.palette.project_all') })].concat(
                projs.map((p) => h('option', { value: String(p.id), text: p.name || ('#' + p.id) }))));
        sel.value = s.c.project_id ? String(s.c.project_id) : '';
        const status = h('div', { class: 'cpanel-status', role: 'status', 'data-cp': 'project-status' });
        const kids = [h('label', { text: T('containers.filter.project') }), sel,
                      h('small', { class: 'meta', text: T('containers.palette.project_desc') })];
        if (s.perms.deploy) kids.push(push(T('containers.palette.save'), () => saveProject(sel, status), { 'data-cp': 'project-save' }), status);
        s.projectBox.replaceChildren(h('div', { class: 'palette-field' }, kids));
        netShown();
    }

    async function saveProject(sel, status) {
        const s = st;
        const projName = (sel.options && sel.options[sel.selectedIndex] || {}).text || '—';
        if (sel.value && window.ProjectEnv && !(await window.ProjectEnv.confirmVmid(s.vmid, projName))) return;
        if (!window.confirm(T('js.project.media_restart') + '\n\n' + T('js.label.project') + ' : ' + projName
                            + '\n\n' + T('js.confirm.continue'))) return;
        say(status, T('js.status.restarting'));
        const r = await call('/api/containers/' + s.vmid + '/project', 'PATCH',
                             { project_id: sel.value ? parseInt(sel.value, 10) : null });
        say(status, r.ok ? T('js.restart_launched') : T('js.request_error'), r.ok ? 'ok' : 'err');
    }

    // ─── 4. Actions ─────────────────────────────────────────────────────────────────────────
    // The Containers page's global functions (static/scripts.js) when they are loaded; the plugin
    // pages do not load scripts.js, so the same request is sent from here there.
    const glob = (n) => (typeof window[n] === 'function' ? window[n] : null);
    async function recreate() {
        const s = st;
        if (glob('recreer')) { await glob('recreer')(s.vmid, s.name); return; }
        if (!window.confirm(T('containers.card.recreate_confirm').replace('{name}', s.name))) return;
        const r = await call('/api/containers/' + s.vmid + '/recreate', 'POST', { confirm: true });
        if (!r.ok) toast(T('containers.card.recreate_failed') + ' ' + (r.j.error || ('HTTP ' + r.code)), 'error');
        else toast(T('containers.panel.act_sent'));
    }

    function renderActions() {
        const s = st, P = s.perms, c = s.c, vmid = s.vmid;
        const B = s.blocks.actions;
        if (!P.deploy && !P.destroy) { B.el.hidden = true; B.body.replaceChildren(); return; }
        const type = s.dc.type || '';
        const engine = glob('_isRx2110') ? glob('_isRx2110')(type) : type === '2110_io';
        const done = (p) => Promise.resolve(p).then(() => toast(T('containers.panel.act_sent')));
        const kids = [];
        if (P.deploy) {
            kids.push(push(T('containers.panel.act_restart'), () => done(glob('restart') ? glob('restart')(vmid)
                : call('/api/containers/' + vmid + '/restart', 'POST')), { 'data-cp': 'act-restart' }));
            kids.push(c.status === 'running'
                ? push(T('containers.panel.act_stop'), () => done(glob('stopScript') ? glob('stopScript')(vmid)
                    : call('/api/containers/' + vmid + '/stop_script', 'POST')), { 'data-cp': 'act-stop' })
                : push(T('containers.panel.act_start'), () => done(glob('startScript') ? glob('startScript')(vmid)
                    : call('/api/containers/' + vmid + '/start_script', 'POST')), { 'data-cp': 'act-start' }));
            // An engine does not need Recreate (it runs --rm, Restart re-runs it); the route refuses it.
            if (!engine) kids.push(push(T('containers.card.recreate'), () => recreate(),
                { 'data-cp': 'act-recreate', title: T('containers.card.recreate_title') }));
            // The migration dialog is shared (static/js/migration.js, loaded by layout.html).
            if (glob('ouvrirMigration')) kids.push(push(T('migration.button'), () => {
                const name = s.name; close(); glob('ouvrirMigration')(vmid, name);
            }, { 'data-cp': 'act-migrate', title: T('migration.button_title') }));
            if (window.BobiLogs && typeof window.BobiLogs.open === 'function') kids.push(push(T('js.logs.card_btn', 'Journal'),
                () => window.BobiLogs.open(vmid, { nom: s.name }), { 'data-cp': 'act-logs' }));
        }
        const row = h('div', { class: 'cpanel-actions' }, kids);
        const out = [row];
        if (P.destroy) {
            out.push(h('div', { class: 'cpanel-danger' }, [push(T('containers.card.destroy'), async () => {
                if (glob('detruire')) { await glob('detruire')(vmid); return; }
                if (!window.confirm(T('js.destroy.confirm') + ' ' + vmid + ' ?')) return;
                await call('/api/containers/' + vmid, 'DELETE');
                close();
            }, { class: 'ctl-push ctl-push--small ctl-push--danger cpanel-destroy', 'data-cp': 'act-destroy',
                 'aria-label': T('containers.card.destroy') + ' ' + s.name })]));
        }
        B.body.replaceChildren(...out);
    }

    window.BobiContainerPanel = {
        open, close,
        isOpen: (vmid) => !!(st && root && !root.hidden && (vmid == null || st.vmid === Number(vmid))),
        fields: { html: fieldHtml, mount: mountFields, read: readFields, applyVisibility,
                  validate: validateFields, system: systemFields },
    };
})();
