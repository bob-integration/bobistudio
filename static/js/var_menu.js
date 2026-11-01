// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
/* Menu « Insérer une variable » : les états d'AUTRES conteneurs, un groupe PAR CONTENEUR.
 *
 * Demandé le 2026-10-29 : avec les états des conteneurs d'un projet, la liste devenait
 * un vrac de plusieurs dizaines d'entrées. Un <select> natif n'a qu'un niveau de groupe — et
 * c'est lui qui donne la roue de sélection de l'iPad — donc « sous-menu par type » se traduit
 * par un <optgroup> par conteneur, intitulé « <portée> · <type> · <conteneur> », triés par type
 * puis par nom. Chaque entrée n'y porte plus que son libellé court.
 *
 * Entrées attendues (cf. app/wall_states.py:_entries) : {name, label, group?, type_label?, short?}.
 * Une entrée sans `group` est rangée dans un groupe unique intitulé `scope` (liste plate). */
(function () {
    'use strict';
    function option(v, text) {
        return new Option(text + '  (%' + v.name + '%)', v.name);
    }
    function appendGroups(sel, scope, list) {
        if (!sel || !list || !list.length) return;
        const groups = new Map();
        list.forEach(v => {
            const key = v.group ? (v.type_label || '') + '\u0000' + v.group : '';
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(v);
        });
        const keys = Array.from(groups.keys()).sort((a, b) =>
            a.localeCompare(b, undefined, {numeric: true, sensitivity: 'base'}));
        keys.forEach(k => {
            const og = document.createElement('optgroup');
            const items = groups.get(k), first = items[0];
            og.label = k ? [scope, first.type_label, first.group].filter(Boolean).join(' · ') : scope;
            items.forEach(v => og.appendChild(option(v, k ? (v.short || v.label) : v.label)));
            sel.appendChild(og);
        });
    }
    window.BobiVarMenu = {appendGroups: appendGroups};
})();
