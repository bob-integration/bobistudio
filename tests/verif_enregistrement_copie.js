#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Registration « Copy the text » (templates/settings.html, regCopier), 2026-11-08.
// Over plain HTTP `navigator.clipboard` does not exist. The first version only SELECTED the text
// and announced « copied »: nothing was copied (reported by Cyril). The function must:
//   1. use the clipboard API when the context is secure;
//   2. otherwise really copy with execCommand('copy') on the dialog's own field;
//   3. say the truth when that fails too, and leave the text selected for Ctrl+C;
//   4. copy the recipient line too, not just the body.
//
// ★ Since 2026-11-08 the fallback is NOT in regCopier: it lives in the shared
// `window.copierTexte` (templates/layout.html), which takes the field to use. This test
// injects THE REAL helper rather than a stub, so what runs here is the whole chain the
// browser runs. A stub would have kept passing while the helper itself broke.
'use strict';
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'templates', 'settings.html'), 'utf8');
const layout = fs.readFileSync(path.join(__dirname, '..', 'templates', 'layout.html'), 'utf8');

// Brace counting, not a regex: the helper nests a function and a catch block, so a
// non-greedy match would stop at the first `};`.
function bloc(src, debut) {
    const i = src.indexOf(debut);
    if (i < 0) throw new Error('bloc introuvable : ' + debut);
    let prof = 0;
    for (let k = src.indexOf('{', i); k < src.length; k++) {
        if (src[k] === '{') prof++;
        else if (src[k] === '}' && --prof === 0) return src.slice(i, k + 1) + ';';
    }
    throw new Error('bloc non termine : ' + debut);
}
const HELPER = bloc(layout, 'window.copierTexte = function(');

let fails = 0;
const ok = (c, label, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + label + (c ? '' : '  [' + JSON.stringify(d) + ']')); if (!c) fails++; };

const a = html.indexOf('async function regCopier()');
const b = html.indexOf('\n}\n', a) + 3;
ok(a > 0 && b > a, 'regCopier found in settings.html');
const src = html.slice(a, b);

function run({ secure, clipboard, execOk }) {
    const champ = { value: 'Objet\n\nCorps', selected: false, focus() {}, select() { this.selected = true; } };
    const msg = { textContent: '' };
    const copied = [];
    const document = {
        getElementById: id => ({ 'reg-copie-msg': msg, 'reg-apercu': champ })[id],
        execCommand: cmd => { if (cmd === 'copy' && execOk) copied.push(champ.value); return !!execOk; },
    };
    const navigator = clipboard ? { clipboard: { writeText: async t => { copied.push(t); } } } : {};
    const window = { isSecureContext: secure, t: k => ({ 'settings.reg.to_line': 'À : {e}',
        'settings.reg.copied': 'COPIED {e}', 'settings.reg.copy_manual': 'MANUAL' })[k] };
    const f = new Function('document', 'navigator', 'window', 'regEmail', 'regMail',
                           HELPER + '\n' + src + '\nreturn regCopier;')(document, navigator, window, 'register@bobi.studio', null);
    return f().then(() => ({ msg: msg.textContent, copied, champ }));
}

(async () => {
    let r = await run({ secure: true, clipboard: true, execOk: false });
    ok(r.copied.length === 1 && r.msg.startsWith('COPIED'), 'secure context: the clipboard API copies', r);
    ok(r.copied[0] === 'À : register@bobi.studio\nObjet\n\nCorps', 'the recipient line is copied with the mail', r.copied[0]);

    r = await run({ secure: false, clipboard: false, execOk: true });
    ok(r.copied.length === 1 && r.msg.startsWith('COPIED'), 'plain HTTP: really copied by execCommand', r);
    ok(r.copied[0].startsWith('À : register@bobi.studio\n'), '… with the recipient line', r.copied[0]);
    ok(r.champ.value === 'Objet\n\nCorps', '… and the shown text is left as it was', r.champ.value);

    r = await run({ secure: false, clipboard: false, execOk: false });
    ok(r.copied.length === 0 && r.msg === 'MANUAL', 'copy refused: it says so, never « copied »', r);
    ok(r.champ.selected, '… and the text is left selected for Ctrl+C');

    // ── « Use my details » (regMesInfos) ──────────────────────────────────────────────────
    // The first version only filled EMPTY fields, silently: on a form already filled by an
    // earlier registration the button looked dead (reported by Cyril, 2026-11-08).
    const a2 = html.indexOf('async function regMesInfos()');
    const b2 = html.indexOf('\n}\n', a2) + 3;
    ok(a2 > 0 && b2 > a2, 'regMesInfos found in settings.html');
    async function mine(user, settings, initial) {
        const fields = {};
        for (const k of ['org', 'name', 'email']) fields['reg_' + k] = { value: initial[k] || '' };
        const msg = { textContent: '' };
        const document = { getElementById: id => id === 'reg-msg' ? msg : fields[id] };
        const fetch = async url => ({ json: async () => url.startsWith('/api/users/me') ? user : settings });
        const window = { t: k => ({ 'settings.reg.mine_done': 'DONE {f}', 'settings.reg.mine_none': 'NONE' })[k] || k };
        await new Function('document', 'fetch', 'window', html.slice(a2, b2) + '\nreturn regMesInfos;')(document, fetch, window)();
        return { v: k => fields['reg_' + k].value, msg: msg.textContent };
    }
    r = await mine({ prenom: 'Cyril', nom: 'Mazouer', email: 'c@x.invalid' }, { brand_org_name: 'BOBI' },
                   { org: 'BOB', name: 'Cyril Ma', email: 'old@x.invalid' });
    ok(r.v('name') === 'Cyril Mazouer' && r.v('email') === 'c@x.invalid' && r.v('org') === 'BOBI',
       'a filled form is REPLACED by the account details', [r.v('name'), r.v('email'), r.v('org')]);
    ok(r.msg.startsWith('DONE'), '… and it says what was taken', r.msg);
    r = await mine({ prenom: '', nom: '', email: '' }, {}, { org: 'BOB', name: 'X', email: 'y@x.invalid' });
    ok(r.v('name') === 'X' && r.v('org') === 'BOB' && r.msg === 'NONE',
       'an empty account empties nothing, and says so', [r.v('name'), r.msg]);

    console.log(fails ? `\n✗ ${fails} failure(s)` : '\n✓ all green');
    process.exit(fails ? 1 : 0);
})();
