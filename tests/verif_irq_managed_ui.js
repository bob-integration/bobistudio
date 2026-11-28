#!/usr/bin/env node
// Offline check of the « managed interrupts » button of the node's aptitude sheet (static/node_sheet.js),
// 2026-11-25: the sheet used to only PRINT the boot parameter; the product now sets it through the host
// driver `irq_managed` (NODE_AGENT.md §4.6), never rebooting.
//
// ★ HELD HERE:
//  1. the button shows on the irq line only when the host driver can act (`fixable`): nvme alone → advice only;
//  2. the outcome of the job reads « reboot required, none done » for exit 10 / pending reboot, never « done »;
//     a failed job is a failure, a still-running one is not an outcome;
//  3. the click goes to the generic host-driver relay (run, then job), behind a confirmation;
//  4. every label exists in fr and en, French with a no-break space before : ; ! ?
//
// MUTATIONS, UI_MUT=<name> — each must give rc=1:
//    always   the button shows without `fixable`
//    done     exit 10 reads as a plain success
//
//   node tests/verif_irq_managed_ui.js

const fs = require('fs'), path = require('path');
const ROOT = path.join(__dirname, '..');
let code = fs.readFileSync(path.join(ROOT, 'static', 'node_sheet.js'), 'utf8');
const MUT = process.env.UI_MUT || '';
const MUTS = {
    always: ["l.action === 'irq_managed' && l.fixable", "l.action === 'irq_managed'"],
    done: ["if (job.pending === 'reboot' || job.rc === 10) return 'apt_irq_fix_done';", ''],
};
if (MUT) {
    if (!MUTS[MUT]) { console.log('unknown mutation ' + MUT); process.exit(2); }
    if (code.split(MUTS[MUT][0]).length !== 2) { console.log('mutation ' + MUT + ': pattern stale'); process.exit(1); }
    code = code.replace(MUTS[MUT][0], MUTS[MUT][1]);
    console.log('MUTATION active: ' + MUT);
}
const win = { t: (k) => k, _escp: (s) => String(s) };
const doc = { documentElement: {lang: 'fr'}, addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
new Function('window', 'document', 'fetch', code)(win, doc, () => { throw new Error('no network'); });
const NS = win.NodeSheet;

let ko = 0;
function ok(cond, title, detail) {
    console.log((cond ? '  ok   ' : ' ÉCHEC ') + title);
    if (!cond) { ko++; if (detail !== undefined) console.log('         ' + JSON.stringify(detail)); }
}
const sheet = (line) => ({verdict: 'fit_with_warnings', ts: 0, lines: [line]});
const MEGASAS = {key: 'irq', level: 'warn', value: 25, names: ['megasas0-msix5 (10/s)'], managed: ['megasas0-msix5 (10/s)'],
                 movable: [], cmdline: ['megaraid_sas.smp_affinity_enable=0'], fixable: true, action: 'irq_managed'};
const NVME = Object.assign({}, MEGASAS, {names: ['nvme0q10 (5/s)'], managed: ['nvme0q10 (5/s)'],
                                         cmdline: ['isolcpus=managed_irq,9'], fixable: false});

console.log('\n1. The button');
ok(NS._aptitudeHtml(sheet(MEGASAS), 35).includes('data-ns="apt-irq-fix"'), 'megasas (fixable): button shown');
ok(!NS._aptitudeHtml(sheet(NVME), 35).includes('data-ns="apt-irq-fix"'), '★ nvme alone: no button, advice only');
ok(!NS._aptitudeHtml(sheet(Object.assign({}, MEGASAS, {action: 'irq', fixable: false})), 35).includes('apt-irq-fix'),
   'movable IRQs (action irq): no button');

console.log('\n2. The outcome');
const O = NS._irqFixOutcome;
ok(O({status: 'done', rc: 10, pending: 'reboot'}) === 'apt_irq_fix_done', '★ exit 10: « set, reboot required »');
ok(O({status: 'done', rc: 10, pending: ''}) === 'apt_irq_fix_done', 'exit 10 even once the pending flag is gone');
ok(O({status: 'done', rc: 0, pending: ''}) === 'apt_irq_fix_nothing', 'exit 0: nothing to do');
ok(O({status: 'error', rc: 1, pending: ''}) === 'apt_irq_fix_failed', 'failure stays a failure');
ok(O({status: 'running'}) === 'apt_irq_fix_running' && O(null) === 'apt_irq_fix_running', 'still running: no outcome');

console.log('\n3. The call');
ok(/host-drivers\/irq_managed\/'/.test(code) && code.includes("fetch(base + 'run', {method: 'POST'})")
   && code.includes("fetch(base + 'job?offset=0')"), 'generic relay: run, then job');
const fx = code.slice(code.indexOf('async function fixManagedIrq'), code.indexOf("fetch(base + 'run'"));
ok(fx.includes('mxlConfirm') && fx.includes('if (!go) return;'), 'behind a confirmation');
ok(!/reboot/.test(code.slice(code.indexOf('async function fixManagedIrq'), code.indexOf('loadDrivers(id);\n    }\n', code.indexOf('async function fixManagedIrq')))),
   '★ no reboot call from the button');

console.log('\n4. Labels');
const FR = JSON.parse(fs.readFileSync(path.join(ROOT, 'i18n', 'fr.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'i18n', 'en.json'), 'utf8'));
const keys = ['apt_irq_fix', 'apt_irq_fix_title', 'apt_irq_fix_confirm', 'apt_irq_fix_go', 'apt_irq_fix_running',
              'apt_irq_fix_done', 'apt_irq_fix_nothing', 'apt_irq_fix_failed', 'apt_irq_fix_denied']
    .map(k => 'settings.node_sheet.' + k);
const H = 'settings.hostdrv.irq_managed.';
['open', 'title', 'run', 'row.controller', 'row.others', 'row.bootloader', 'row.configured', 'row.running',
 'status.ok', 'status.install', 'status.pending_reboot', 'status.refused',
 'plan.none', 'plan.nvme_only', 'plan.active', 'plan.apply', 'plan.regenerate', 'plan.pending_reboot',
 'plan.not_honoured', 'plan.no_bootloader', 'plan.param_unreadable', 'step.cmdline', 'step.bootloader']
    .forEach(k => keys.push(H + k));
keys.push('settings.js.node_irq_managed');
const miss = keys.filter(k => !FR[k] || !EN[k]);
ok(!miss.length, 'every label in fr and en (' + keys.length + ')', miss);
const badNb = keys.filter(k => FR[k] && / [:;!?]/.test(FR[k]));
ok(!badNb.length, 'French: no-break space before : ; ! ?', badNb);
ok(FR['settings.node_sheet.apt_irq_fix_confirm'].includes('{cmdline}') && /REQUIS/.test(FR['settings.node_sheet.apt_irq_fix_confirm'])
   && /aucun n'est fait/.test(FR['settings.node_sheet.apt_irq_fix_confirm']),
   '★ the confirmation names the parameter, says a reboot is required and none is done');

console.log(ko ? '\nÉCHEC : ' + ko : '\nOK');
process.exit(ko ? 1 : 0);
