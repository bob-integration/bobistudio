// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// The first-start wizard (templates/setup_wizard.html), as a tester went through it (2026-11-22):
//   1. the language is applied on « Next » and the wizard resumes at step 2 in that language (applied
//      on change, it raced the click: step 2 came in the old language, the reload went back to step 1);
//   2. while a step is being built or saved the buttons are inert, and they only switch with the step
//      they belong to (« Go to Bobi.Studio » showed — clickable — under step 4 while step 5 loaded);
//   3. the logo picker is our own button, in the wizard's language (the native input spoke the
//      browser's: « Choisir un fichier » in an English wizard), and the preview uses Settings' frame.
//   node tests/verif_setup_wizard_ui.js
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'setup_wizard.html'), 'utf8');
const en = JSON.parse(fs.readFileSync(path.join(root, 'i18n', 'en.json'), 'utf8'));
let ko = 0;
const ok = (c, t, d) => { console.log((c ? '  ok  ' : ' ÉCHEC ') + t); if (!c) { console.log('         ' + (d || '')); ko++; } };

// The page script, with Jinja stripped: comments out, expressions as JSON null / [].
let js = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>'));
js = js.replace(/\{#[\s\S]*?#\}/g, '').replace(/\{\{\s*(video_formats|languages|timezones)[^}]*\}\}/g, '[]')
       .replace(/\{\{[^}]*\}\}/g, 'null').replace(/\nwiz\.start\(\);\s*$/, '\n');

function mkEl(id) {
  return { id, innerHTML: '', textContent: '', disabled: false, style: {}, value: '', className: '',
           addEventListener() {}, files: [] };
}
function monde(lang, moi) {
  const els = {}; for (const id of ['wiz-progress', 'wiz-body', 'wiz-prev', 'wiz-skip', 'wiz-next', 'wiz-msg']) els[id] = mkEl(id);
  const store = {}; const appels = []; let rechargee = 0; let attente = null;
  const g = {
    window: { LANG: lang, t: (k) => (lang === 'en' ? en[k] : null) || k },
    document: { getElementById: (id) => els[id] || null },
    sessionStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } },
    location: { reload: () => { rechargee++; }, href: '' },
    fetch: async (url, opt) => {
      appels.push([opt && opt.method, url, opt && opt.body]);
      if (url === '/api/setup/gestures' && attente) await attente;
      const corps = url === '/api/settings' ? {} : url === '/api/users/me' ? (moi || {}) : { gestures: [], unmeasured: [] };
      return { ok: true, json: async () => corps };
    },
  };
  const f = new Function(...Object.keys(g), js + '\nreturn wiz;');
  const wiz = f(...Object.values(g));
  return { wiz, els, store, appels, g, rechargee: () => rechargee, retenir: () => { let r; attente = new Promise((x) => { r = x; }); return r; } };
}
const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  console.log('\n1. language on Next, resume at step 2');
  let m = monde('fr');
  m.wiz.start(); await tick(); await tick();
  m.els['f-lang'] = Object.assign(mkEl('f-lang'), { value: 'en' });
  await m.wiz.next(); await tick();
  ok(m.appels.some(([me, u, b]) => u === '/api/settings' && /ui_lang_default/.test(b || '')), 'Next saves the language as the installation default');
  ok(!m.appels.some(([me, u]) => u === '/api/users/me/lang'),
     'an account without its own language gets none (it would then win over Settings, silently)');
  ok(m.rechargee() === 1 && m.wiz.i === 0, 'then reloads, without moving to step 2 in the old language', [m.rechargee(), m.wiz.i]);
  ok(m.store.wiz_resume === '1', 'and asks to resume at step 2', JSON.stringify(m.store));
  ok(m.els['wiz-next'].disabled === true, 'the buttons stay inert until the reload');
  const perso = monde('fr', { lang: 'fr' }); perso.wiz.start(); await tick(); await tick();
  perso.els['f-lang'] = Object.assign(mkEl('f-lang'), { value: 'en' });
  await perso.wiz.next(); await tick();
  ok(perso.appels.some(([me, u, b]) => u === '/api/users/me/lang' && /"en"/.test(b)), 'an account WITH its own language has it aligned');
  const html_lang = html.slice(html.indexOf('language() {'), html.indexOf('// ── Étape 1'));
  ok(!/onchange=/.test(html_lang), 'choosing in the list applies nothing by itself');
  const apres = monde('en'); Object.assign(apres.store, { wiz_resume: '1' });
  apres.wiz.start(); await tick(); await tick();
  ok(apres.wiz.i === 1 && !('wiz_resume' in apres.store), 'after the reload: step 2, and the resume mark is consumed', apres.wiz.i);
  m = monde('fr'); m.wiz.start(); await tick(); await tick();
  m.els['f-lang'] = Object.assign(mkEl('f-lang'), { value: 'fr' });
  await m.wiz.next(); await tick(); await tick();
  ok(m.rechargee() === 0 && m.wiz.i === 1, 'same language: no reload, straight to step 2');

  console.log('\n2. buttons switch with their step');
  m = monde('en'); m.wiz.start(); await tick(); await tick();
  m.wiz.i = m.wiz.steps.length - 2; await m.wiz.render(); await tick();      // step « updates »
  m.els['f-updchk'] = Object.assign(mkEl('f-updchk'), { checked: true });
  const libere = m.retenir();
  const p = m.wiz.next(); await tick(); await tick(); await tick();
  ok(m.els['wiz-next'].textContent !== en['wiz.finish'], 'while the last step builds, the button is NOT yet « ' + en['wiz.finish'] + ' »', m.els['wiz-next'].textContent);
  ok(m.els['wiz-next'].disabled === true, 'and it is inert');
  const avant = m.g.location.href; await m.wiz.next(); m.wiz.skip(); m.wiz.prev();
  ok(m.g.location.href === avant && m.wiz.i === m.wiz.steps.length - 1, 'clicks during the build do nothing');
  libere(); await p; await tick(); await tick();
  ok(m.els['wiz-next'].textContent === en['wiz.finish'] && m.els['wiz-next'].disabled === false,
     'once built: « ' + en['wiz.finish'] + ' », active', [m.els['wiz-next'].textContent, m.els['wiz-next'].disabled]);

  console.log('\n3. logo picker in the wizard language');
  m = monde('en'); m.wiz.start(); await tick();
  m.els['f-logo'] = mkEl('f-logo');
  m.wiz.i = 2; await m.wiz.render();
  const b = m.els['wiz-body'].innerHTML;
  ok(/<input type="file" id="f-logo"[^>]*\bhidden\b/.test(b), 'the native input is hidden');
  ok(b.includes('>' + en['wiz.identity.logo_choose'] + '<') && b.includes(en['wiz.identity.logo_none']),
     'our button and « no file » text, in English', b.slice(b.indexOf('wiz-file'), b.indexOf('wiz-file') + 300));
  ok(b.includes('identity-logo-frame'), 'the preview uses the Settings frame');
  const fr = JSON.parse(fs.readFileSync(path.join(root, 'i18n', 'fr.json'), 'utf8'));
  ok(['wiz.identity.logo_choose', 'wiz.identity.logo_none'].every((k) => fr[k] && en[k] && fr[k] !== en[k]), 'both keys in fr and en');

  console.log('\n4. Settings: a personal language that wins is said, and can be aligned');
  const st = fs.readFileSync(path.join(root, 'templates', 'settings.html'), 'utf8');
  const d = st.indexOf('async function appliquerLangueDefaut()');
  const src = st.slice(d, st.indexOf('\nasync function ', d + 10));
  for (const [moi, accepte, attendu, quoi] of [
    [{ lang: 'fr' }, true, true, 'own language differs, accepted: the account is aligned'],
    [{ lang: 'fr' }, false, false, 'own language differs, declined: the account keeps it'],
    [{ lang: null }, true, false, 'no own language: nothing asked, the default applies']]) {
    const appels = []; let demande = 0, recharge = 0;
    const g = {
      document: { getElementById: () => ({ value: 'en', selectedOptions: [{ textContent: 'English' }] }) },
      window: { t: (k) => k + ' {lang}' }, alert: () => {},
      confirm: () => { demande++; return accepte; }, location: { reload: () => { recharge++; } },
      fetch: async (url, opt) => { appels.push([url, opt && opt.body]);
        return { ok: true, json: async () => (url === '/api/users/me' ? moi : {}) }; },
    };
    await new Function(...Object.keys(g), src + '\nreturn appliquerLangueDefaut();')(...Object.values(g));
    const aligne = appels.some(([u, b]) => u === '/api/users/me/lang' && /"en"/.test(b));
    ok(aligne === attendu && recharge === 1 && demande === (moi.lang ? 1 : 0), quoi, JSON.stringify({ aligne, demande, recharge }));
  }
  const fr2 = JSON.parse(fs.readFileSync(path.join(root, 'i18n', 'fr.json'), 'utf8'));
  ok(fr2['js.lang.own_differs'] && en['js.lang.own_differs'] && fr2['js.lang.own_differs'].includes('{lang}'), 'the question exists in fr and en');

  process.exit(ko ? 1 : 0);
})().catch((e) => { console.log(' ÉCHEC ' + e.stack); process.exit(1); });
