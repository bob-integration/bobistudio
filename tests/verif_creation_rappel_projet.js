#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Container creation: the project reminder must never leave « Add to list » silently dead.
//
// WHY. Reported by the tester on 2026-11-27 (Chrome): creating a pyramide / transcoder /
// plugin_gui with a project selected, « Add to list » did nothing at all — nothing reached the list,
// no message, so nothing could be launched. The reminder (static/js/project_env.js) used the
// browser's confirm(), and Chrome returns false WITHOUT showing anything once « prevent this page
// from creating additional dialogs » has been ticked (until reload), or in a background tab. And
// addToCart (static/scripts.js) answered a refusal with a bare `return`.
//
// Checked, each by its own case (every rule has data that tells it from its absence):
//   1. the reminder goes through window.mxlConfirm, never the native confirm() when it exists;
//   2. no notice (type without environment) → no dialog at all, the gesture goes through;
//   3. a notice request that hangs does not freeze the gesture (timeout → goes through);
//   4. declining in addToCart SAYS it (warning shown) and adds nothing;
//   5. accepting adds the lines;
//   6. a second click while the first waits does not add the lines twice.
// The code is taken from the files themselves; the DOM is a minimal stand-in (no jsdom here).
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

const envSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'project_env.js'), 'utf8');
const scripts = fs.readFileSync(path.join(ROOT, 'static', 'scripts.js'), 'utf8');

// ── project_env.js, run as is in a fake window ────────────────────────────────────────────────
function loadEnv({ fetchImpl, mxl, timeoutMs }) {
    const calls = { native: 0, mxl: 0, mxlOpts: null };
    const win = {
        t: k => k,
        mxlConfirm: mxl === undefined ? undefined : async (o) => { calls.mxl++; calls.mxlOpts = o; return mxl; },
    };
    const src = timeoutMs ? envSrc.replace(/NOTICE_TIMEOUT_MS = \d+/, 'NOTICE_TIMEOUT_MS = ' + timeoutMs) : envSrc;
    new Function('window', 'fetch', 'confirm', 'AbortController', 'setTimeout', 'clearTimeout', src)(
        win, fetchImpl, () => { calls.native++; return true; }, AbortController, setTimeout, clearTimeout);
    return { env: win.ProjectEnv, calls };
}
const noticeFetch = notice => async () => ({ ok: true, json: async () => ({ notice }) });
const NOTICE = { type: 'pyramide', scope: 'shared', label: 'Pyramide' };

async function envCases() {
    {   // 1 — mxlConfirm, not confirm()
        const { env, calls } = loadEnv({ fetchImpl: noticeFetch(NOTICE), mxl: false });
        const r = await env.confirmType('pyramide', 'Atelier');
        ok(calls.mxl === 1 && calls.native === 0, 'the reminder uses mxlConfirm, never confirm()', calls);
        ok(r === false, 'its answer is passed back (declined → false)', r);
        const h = (calls.mxlOpts && calls.mxlOpts.html) || '';
        ok(h.includes('projects.env_notice_shared') && h.includes('projects.env_notice_question'),
           'the dialog carries the reminder and the question', h);
    }
    {   // no mxlConfirm on the page → native fallback (a page without layout.html)
        const { env, calls } = loadEnv({ fetchImpl: noticeFetch(NOTICE), mxl: undefined });
        await env.confirmType('pyramide', 'Atelier');
        ok(calls.native === 1, 'without mxlConfirm, the native box is the fallback', calls);
    }
    {   // 2 — no notice → no dialog
        const { env, calls } = loadEnv({ fetchImpl: noticeFetch(null), mxl: false });
        let r;
        try { r = await env.confirmType('multiview', 'Atelier'); } catch (e) { r = 'THREW: ' + e.message; }
        ok(r === true && calls.mxl === 0 && calls.native === 0, 'no notice → no dialog, the gesture goes through', { r, calls });
    }
    {   // 3 — hanging request → timeout, goes through
        const hang = (url, opts) => new Promise((_, rej) => {
            if (opts && opts.signal) opts.signal.addEventListener('abort', () => rej(new Error('aborted')));
        });
        const { env, calls } = loadEnv({ fetchImpl: hang, mxl: false, timeoutMs: 50 });
        const t0 = Date.now();
        const r = await Promise.race([env.confirmType('pyramide', 'Atelier'),
                                      new Promise(res => setTimeout(() => res('HUNG'), 1000))]);
        ok(r === true && calls.mxl === 0, 'a hanging notice request does not freeze the gesture', { r, ms: Date.now() - t0 });
    }
}

// ── addToCart (static/scripts.js), the slice between its guard and _resetStep2 ────────────────
const a = scripts.indexOf('let _addToCartBusy'), b = scripts.indexOf('// Réinitialise l\'étape 2', a);
ok(a > 0 && b > a, 'addToCart found in scripts.js');
const cartSrc = scripts.slice(a, b);

function loadCart(confirmAnswer, confirmDelayMs) {
    const st = { warn: [], rendered: 0, reset: 0 };
    const row = {
        querySelector: sel => ({
            '.cc-hostname': { value: 'pyr-a' },
            '.cc-node-sel': { value: '55' },
            '.cc-project-sel': { value: '7', selectedIndex: 0, options: [{ text: 'Atelier' }] },
        })[sel] || null,
    };
    const doc = {
        querySelectorAll: sel => sel === '#create-step2-rows .create-row' ? [row] : [],
        querySelector: sel => sel === '.cc-project-sel' ? row.querySelector(sel) : null,
    };
    const win = {
        t: k => k,
        ProjectEnv: { confirmType: () => new Promise(res => setTimeout(() => res(confirmAnswer), confirmDelayMs || 0)) },
    };
    const api = new Function('window', 'document', 'ProjectEnv', 'hooks', `
        let _curType = {type: 'pyramide', label: 'Pyramide'};
        let _createCart = [];
        const sanitizeHostname = s => s;
        const defaultDeployParams = () => ({});
        const _ccRowFormatOverride = () => null;
        const _createSetWarn = m => hooks.warn.push(m);
        const renderCart = () => hooks.rendered++;
        const _resetStep2 = () => hooks.reset++;
        ${cartSrc}
        return { addToCart, cart: () => _createCart };`)(win, doc, win.ProjectEnv, st);
    return { api, st };
}

async function cartCases() {
    {   // 4 — declined → said, nothing added
        const { api, st } = loadCart(false);
        await api.addToCart();
        ok(api.cart().length === 0, 'declined → nothing added', api.cart());
        ok(st.warn.includes('js.create.project_declined'), 'declined → the refusal is SAID', st.warn);
    }
    {   // 5 — accepted → added
        const { api, st } = loadCart(true);
        await api.addToCart();
        ok(api.cart().length === 1 && st.rendered === 1, 'accepted → the line is added', api.cart());
    }
    {   // 6 — double click while waiting → once
        const { api } = loadCart(true, 30);
        await Promise.all([api.addToCart(), api.addToCart()]);
        ok(api.cart().length === 1, 'a second click while waiting does not add twice', api.cart().length);
    }
}

// A case that THROWS is a failure, not a silent pass: without this, a mutation that makes the code
// crash (instead of answering wrongly) left every check unprinted and nothing counted red.
(async () => {
    for (const c of [envCases, cartCases]) {
        try { await c(); } catch (e) { ok(false, c.name + ' threw', e && e.message); }
    }
    process.exit(fails ? 1 : 0);
})();
