/* Choices of a parameter with LIVE options (`options_endpoint` in a plugin manifest), shared by
   every editor that lists them: the ATEM panel key editor (Réglages → Pupitres) and the macro
   editor (workspace). One rule, one place — the two editors used to diverge.

   2026-10-29 — `option_semantics: "position"` (declared on the parameter, e.g. the player's
   « aller au clip » index): what is STORED is a position in a list that changes (the loaded
   playlist), not the item seen today. The list therefore says « Élément 3 (actuellement : Météo) »,
   and it stays usable when the list is empty or shorter: positions 1..POSITION_MIN at least
   (or the list length if longer, or the stored position if further). Before this, an empty
   playlist gave a select with a single « … » entry — nothing to choose (reported by the user on
   2026-10-29, ATEM key editor, player « Atelier-Synthe »).

   window.MXLOptionChoices.normalize(json, p) → [{v, l}] from an endpoint answer (options_path,
                                                 option_value, option_label, label_key) ;
   window.MXLOptionChoices.build(p, items, cur) → [{v, l}] to render, positions expanded. */
(function () {
    'use strict';
    const POSITION_MIN = 20;          // two pages of 10 keys on the panel (ATEM_KEYS_SHOWN)
    const T = (k) => (typeof window.t === 'function' ? window.t(k) : k);

    function normalize(json, p) {
        const items = Array.isArray(json) ? json : ((json && json[p.options_path || 'items']) || []);
        return items.map(it => {
            if (!it || typeof it !== 'object') return {v: it, l: String(it)};
            const v = it[p.option_value || 'value'] ?? '';
            let l = it[p.option_label || p.option_value || 'label'] ?? v;
            // option named by an i18n KEY (plugin text, not data): rendered in the reader's language
            if (it.label_key && window.I18N && window.I18N[it.label_key]) l = window.I18N[it.label_key];
            return {v: v, l: String(l)};
        });
    }

    function isPosition(p) { return !!(p && p.option_semantics === 'position'); }

    function build(p, items, cur) {
        items = items || [];
        if (!isPosition(p)) return items;
        const byPos = {};
        for (const o of items) {
            const k = Number(o.v);
            if (Number.isInteger(k) && k >= 0) byPos[k] = o.l;
        }
        const c = Number(cur);
        const n = Math.max(POSITION_MIN, items.length,
                           (cur !== '' && cur != null && Number.isInteger(c) && c >= 0) ? c + 1 : 0);
        const out = [];
        for (let k = 0; k < n; k++) {
            const tpl = (k in byPos) ? T('js.option_position_now') : T('js.option_position_empty');
            out.push({v: k, l: tpl.replace('{n}', String(k + 1)).replace('{title}', byPos[k] ?? '')});
        }
        return out;
    }

    window.MXLOptionChoices = {normalize, build, isPosition, POSITION_MIN};
})();
