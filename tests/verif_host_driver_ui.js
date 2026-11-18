// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Le panneau « pilotes hôte » de templates/settings.html : il S'EXÉCUTE (un nom de variable oublié dans
// un gabarit ne casse aucune compilation et tue le panneau en silence — cf. les locales orphelines).
//   node tests/verif_host_driver_ui.js
// On extrait le bloc de la page, on lui fournit un DOM minimal, et on le fait rendre les états réels
// que l'agent renvoie (plan à installer, redémarrage en attente, arrêt complet en attente, refus).
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates', 'settings.html'), 'utf8');
const a = html.indexOf("// ─── Pilotes hôte d'un nœud : panneau GÉNÉRIQUE");
const b = html.indexOf('// GPU NVIDIA : détection');
if (a < 0 || b < a) { console.log('ÉCHEC bloc introuvable'); process.exit(1); }
const fr = JSON.parse(fs.readFileSync(path.join(root, 'i18n', 'fr.json'), 'utf8'));

const els = {};
const mk = (id) => (els[id] = { id, innerHTML: '', style: {}, textContent: '', querySelector: () => null });
global.document = { getElementById: (id) => els[id] || null };
global.window = { t: (k) => fr[k] || k };
global.T = (k) => window.t(k);
global.escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.fetch = async () => ({ json: async () => ({ ok: true }) });
global.confirm = () => true;
global.setTimeout = () => 0;
(0, eval)(html.slice(a, b) + '\nglobal.__hd = { hostDrvRender, _hdFmt, _hdErr, _hdLogFolded };');
const { hostDrvRender: render, _hdFmt: fmt, _hdErr: herr, _hdLogFolded: logFolded } = global.__hd;

let ko = 0;
const ok = (c, t, d) => { console.log((c ? '  ok  ' : ' ÉCHEC ') + t); if (!c) { console.log('         ' + (d || '')); ko++; } };
const base = { ok: true, kind: 'nvidia', can_run: true, upload_dir: '/x',
  rows: [{ key: 'gpu', value: 'Tesla T4 (turing)' }, { key: 'driver', value: '550.163.01 (debian)' }],
  plan: { status: 'upgrade', code: 'driver_too_old', params: { current: '550.163.01', gpus: 'Tesla T4' },
          steps: ['apt_components', 'prereqs', 'purge', 'install_repo', 'verify_module'] }, job: { status: 'none', pending: '' } };

mk('hostdrv_nvidia_7');
render(7, 'nvidia', base);
let h = els['hostdrv_nvidia_7'].innerHTML;
ok(h.includes('Tesla T4 (turing)') && h.includes('Installer / mettre à jour le pilote'), 'état « à mettre à jour » : lignes et bouton', h.slice(0, 200));
ok(h.includes('570 ou plus') || h.includes('trop ancien'), 'le code de plan est traduit (phrase, pas la clé)', h);
ok(!/settings\.hostdrv\.[a-z_.]+/.test(h), 'aucune clé i18n brute affichée', (h.match(/settings\.hostdrv\.[a-z_.]+/g) || []).join(','));
ok(h.includes('<li>') && h.split('<li>').length === 6, 'les 5 étapes prévues sont listées', '');
ok(h.includes('min-height:44px'), 'boutons à hauteur tactile (44 px)', '');
ok(!h.includes('reboot'), 'pas de bandeau de redémarrage sans action en attente', '');

render(7, 'nvidia', Object.assign({}, base, { job: { status: 'none', pending: 'reboot' } }));
h = els['hostdrv_nvidia_7'].innerHTML;
ok(h.includes('hostDrvReboot(7') && h.includes('Redémarrage requis'), 'redémarrage en attente → bandeau + bouton EXPLICITE', '');
render(7, 'nvidia', Object.assign({}, base, { job: { status: 'none', pending: 'shutdown' } }));
h = els['hostdrv_nvidia_7'].innerHTML;
ok(h.includes('hostDrvPoweroff(7') && h.includes('Arrêt COMPLET') && !h.includes('hostDrvReboot('), 'arrêt complet en attente → bouton d\'arrêt (et pas de simple redémarrage)', '');
render(7, 'nvidia', Object.assign({}, base, { can_run: false, plan: { status: 'refused', code: 'secure_boot', params: {}, steps: [] } }));
h = els['hostdrv_nvidia_7'].innerHTML;
ok(h.includes('Secure Boot est ACTIVÉ') && !h.includes('hostDrvRun('), 'refus (Secure Boot) → message, aucun bouton d\'installation', '');
render(7, 'nvidia', Object.assign({}, base, { plan: Object.assign({}, base.plan, { status: 'install', code: 'no_driver', params: { fallback: 'repo_unavailable', gpus: 'X' } }) }));
h = els['hostdrv_nvidia_7'].innerHTML;
ok(h.includes('repli sur l\'installeur'), 'repli hors-ligne annoncé dans le message', '');
ok(fmt('a {x} b {y}', { x: 1 }) === 'a 1 b ', 'substitution des paramètres', '');
ok(herr({ error: 'agent_too_old' }).includes('trop ancien') && herr({ error: 'zzz', detail: 'd' }).includes('Erreur'), 'erreurs : code connu traduit, inconnu → libellé par défaut', '');
// ── DeckLink, 2026-11-14 : cartes sur le bus, aucun module. Le bouton nomme CE geste (il disait
//    « Mettre à jour le micrologiciel » dans tous les états), et le journal d'une opération finie
//    est titré, daté et replié — sinon il se lit comme l'état actuel.
mk('hostdrv_blackmagic_7');
render(7, 'blackmagic', { ok: true, kind: 'blackmagic', can_run: true, upload_dir: '/x',
  rows: [{ key: 'kernel', value: '6.12.117+deb13-amd64 - 0 module(s), 0 device node(s), 2 PCI device(s)' }],
  plan: { status: 'module_missing', code: 'module_not_built', params: { kernel: '6.12.117+deb13-amd64', pci: 2 },
          steps: [{ id: 'build', params: { kernel: '6.12.117+deb13-amd64' } }] }, job: { status: 'none' } });
h = els['hostdrv_blackmagic_7'].innerHTML;
ok(h.includes('Compiler et charger le module') && !h.includes('Mettre à jour le micrologiciel'),
   'module absent : le bouton nomme le geste (compiler), pas le micrologiciel', h.slice(0, 300));
ok(h.includes('Module noyau absent') && h.includes('6.12.117+deb13-amd64') && !h.includes('Aucune carte'),
   'module absent : statut et message vrais, jamais « Aucune carte »', '');
ok(!/settings\.hostdrv\.[a-z_.]+/.test(h), 'module absent : aucune clé i18n brute', (h.match(/settings\.hostdrv\.[a-z_.]+/g) || []).join(','));
const lf = logFolded({ status: 'done', started: 1763138000, ended: 1763138312, elapsed: 312 }, 'Installing desktopvideo_16.5a1_amd64.deb <x>');
ok(lf.includes('Journal de la dernière opération') && lf.includes('terminé') && lf.includes('312 s') && /— [^<]*\d/.test(lf),
   'journal fini : titré « dernière opération », statut, date et durée', lf.slice(0, 200));
ok(lf.startsWith('<details') && !/<details[^>]*\bopen\b/.test(lf), 'journal fini : REPLIÉ par défaut', lf.slice(0, 80));
ok(lf.includes('&lt;x&gt;') && !lf.includes('<x>'), 'journal fini : contenu échappé', '');
process.exit(ko ? 1 : 0);
