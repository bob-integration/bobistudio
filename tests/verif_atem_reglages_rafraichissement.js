// Réglages → Pupitres → ATEM : le rafraîchissement (toutes les 3 s) ne doit ni refermer un menu
// ni faire bouger la page.
//
// ★ CE QUE CETTE GARDE EMPÊCHE DE REVENIR (2026-10-27, rapporté par l'utilisateur) : « le chiffre
// dans la colonne "Vu" change, et ça décale suivant sa largeur le reste de la page. Et certains
// menus se ferment au rafraîchissement ». `rafraichirAtem` réécrivait tout le tableau à chaque
// tic : un <select> ouvert était détruit sous le doigt.
//
// On exécute la VRAIE fonction, extraite de templates/settings.html, contre un DOM minimal.
// Lancer : node tests/verif_atem_reglages_rafraichissement.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(process.env.SETTINGS_HTML || path.join(ROOT, 'templates/settings.html'), 'utf8');
let fails = 0;
function check(ok, msg) { console.log((ok ? '  ok    ' : '  ÉCHEC ') + msg); if (!ok) fails++; }

const debut = html.indexOf('async function rafraichirAtem()');
const fin = html.indexOf('\n}\n', debut);
check(debut > 0 && fin > debut, 'rafraichirAtem trouvée dans settings.html');
const src = html.slice(debut, fin + 2);

// ── DOM minimal : une cellule vivante = <td … data-live="x" data-live-ip="ip">…</td>
const CELL = /<td([^>]*)data-live="([^"]*)" data-live-ip="([^"]*)">([\s\S]*?)<\/td>/g;
function cellsOf(owner) {
    const out = [];
    for (const m of owner._html.matchAll(CELL)) {
        const [all, , live, ip, inner] = m;
        out.push({
            dataset: { live, liveIp: ip },
            get innerHTML() { return inner; },
            set innerHTML(v) { owner._html = owner._html.replace(all, all.replace('>' + inner + '</td>', '>' + v + '</td>')); owner.liveWrites++; },
        });
    }
    return out;
}
function makeTbody() {
    const tb = {
        _html: '', rebuilds: 0, liveWrites: 0, dataset: {}, focused: false,
        get innerHTML() { return this._html; },
        set innerHTML(v) { this._html = v; this.rebuilds++; },
        contains() { return this.focused; },
        querySelectorAll(sel) { return sel === 'td[data-live]' ? cellsOf(this) : []; },
        querySelector(sel) {
            const m = /data-live="([^"]*)"\]\[data-live-ip="([^"]*)"/.exec(sel);
            return m ? cellsOf(this).find(c => c.dataset.live === m[1] && c.dataset.liveIp === m[2]) || null : null;
        },
    };
    return tb;
}
const tb = makeTbody();
const els = {};
const document = {
    activeElement: {},
    getElementById(id) {
        if (id === 'atem_panels_tbody') return tb;
        return els[id] || (els[id] = { style: {}, textContent: '', value: '', checked: false });
    },
    createElement() { return makeTbody(); },
    querySelector() { return null; },     // aucun pli « Sources » ouvert
};
let panneau = { ip: '10.0.0.1', connected: true, me_vmids: [1250], dve_vmids: [], last_seen: 9,
                pgm: 1, pvw: 2, labels: {}, unused_names: 'input', label: 'Régie' };
const ctx = {
    document, console, window: {},
    T: k => k, _editing: () => true, formatUptime: x => String(x), _atemRenderKnown() {},
    _atemOptions: () => '<option>m</option>', _atemDveOptions: () => '<option>d</option>',
    _atemNiveauOptions: () => '<option>n</option>', _atemTouchesLibresOptions: () => '<option>t</option>',
    _atemDskOptions: () => '<option>k</option>',
    _atemChargerNomsColonnes: async () => {},
    _atemSourcesBadge: () => '', _atemSourcesTable: () => '', _atemEchappe: v => String(v),
    _atemPliOuvert: () => null, _atemMajSourcesOuvertes() {}, ATEM_ME_COUNT: 2,
    fetch: async url => ({ json: async () => url.endsWith('/status')
        ? { running: true } : { panels: [Object.assign({}, panneau)], mixers: [], dves: [] } }),
    _atemMixers: [], _atemDves: [],
};
vm.createContext(ctx);
vm.runInContext(src + '\nthis.rafraichirAtem = rafraichirAtem;', ctx);

(async () => {
    await ctx.rafraichirAtem();
    check(tb.rebuilds === 1, 'premier passage : le tableau est construit');
    check(/data-live="vu"[^>]*>[\s\S]*tabular-nums[\s\S]*9s/.test(tb._html), '« Vu » en chiffres tabulaires');
    check(/min-width:5ch/.test(tb._html), '« Vu » a une largeur fixe');

    panneau.last_seen = 10; panneau.pgm = 3;
    await ctx.rafraichirAtem();
    check(tb.rebuilds === 1, 'seul « Vu »/PGM change : PAS de reconstruction (un menu ouvert reste ouvert)');
    check(/10s/.test(tb._html) && /3·2/.test(tb._html), 'les cellules vivantes sont mises à jour en place');

    panneau.label = 'Régie 2';   // un champ de STRUCTURE (le nom du pupitre, rendu dans un <input>)
    tb.focused = true;
    await ctx.rafraichirAtem();
    check(tb.rebuilds === 1, 'structure changée MAIS un champ a le focus : pas de reconstruction');
    tb.focused = false;
    await ctx.rafraichirAtem();
    check(tb.rebuilds === 2, 'focus rendu : la structure changée est reconstruite au tic suivant');

    console.log(fails ? `\nÉCHEC : ${fails}` : '\nOK — le rafraîchissement ne referme plus les menus et ne décale plus la page.');
    process.exit(fails ? 1 : 0);
})();
