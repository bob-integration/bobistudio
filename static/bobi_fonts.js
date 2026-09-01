// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Fichier AUTONOME, chargé par layout.html → disponible sur TOUTES les pages.
// Il vivait dans scripts.js, qui n'est chargé QUE par containers.html et projects.html : sur la
// page du composer (plugin_section.html, « scripts.js n'est pas chargé ici ») et sur Réglages,
// window.BobiFonts était donc INDÉFINI → les sélecteurs de police retombaient en silence sur la
// liste figée des polices d'image, et une police de la BIBLIOTHÈQUE n'apparaissait jamais.

// ─── Bibliothèque de polices (window.BobiFonts) ────────────────────────────────
// Catalogue COMMUN aux sélecteurs de police (composer multiview, éditeur de modèles de PiP) :
// polices de l'image runtime (`builtin`) + bibliothèque téléversée (Réglages → Polices,
// clé `lib:<sha16>`). Une seule requête /api/fonts par page (promesse mémoïsée).
// APERÇU FIDÈLE : les polices de la bibliothèque sont déclarées en @font-face côté navigateur
// (famille « bobi-<sha16> », même convention que Réglages → Polices) → un canvas qui écrit avec
// `ctx.font = ... BobiFonts.cssFamily(key)` rend la VRAIE police, comme le conteneur. Sans ça
// l'éditeur mentirait sur le rendu final.
window.BobiFonts = (function () {
    let promise = null;
    let cat = [];                       // catalogue tel que renvoyé par l'API
    let defKey = 'dejavu-sans-bold';
    // Familles CSS des polices d'image (approximation navigateur : ces polices ne sont pas
    // servies par l'orchestrateur ; le rendu final, lui, se fait dans le conteneur).
    const BUILTIN_CSS = {
        'dejavu-sans':          '"DejaVu Sans", sans-serif',
        'dejavu-sans-bold':     '"DejaVu Sans", sans-serif',
        'dejavu-serif':         '"DejaVu Serif", serif',
        'dejavu-mono':          '"DejaVu Sans Mono", monospace',
        'liberation-sans':      '"Liberation Sans", Arial, sans-serif',
        'liberation-sans-bold': '"Liberation Sans", Arial, sans-serif',
        'liberation-mono':      '"Liberation Mono", monospace',
        'inter':                'Inter, system-ui, sans-serif',
        'roboto':               'Roboto, system-ui, sans-serif',
        'firacode':             '"Fira Code", monospace',
    };

    function _injectFaces(fonts) {
        const id = 'bobi-fonts-faces';
        let st = document.getElementById(id);
        if (!st) { st = document.createElement('style'); st.id = id; document.head.appendChild(st); }
        st.textContent = fonts.filter(f => !f.builtin && f.url && f.sha256)
            .map(f => `@font-face{font-family:"bobi-${f.sha256.slice(0, 16)}";src:url("${f.url}");}`)
            .join('\n');
    }

    function load(force) {
        if (!promise || force) {
            promise = fetch('/api/fonts').then(r => r.json()).then(j => {
                cat = (j && j.fonts) || [];
                defKey = (j && j.default) || defKey;
                _injectFaces(cat);
                return { fonts: cat, default: defKey };
            }).catch(() => ({ fonts: [], default: defKey }));
        }
        return promise;
    }

    const defaultKey = () => defKey;
    const catalog = () => cat;
    const isLib = k => /^lib:[0-9a-f]{16}$/.test(String(k || ''));

    // Famille CSS d'une clé de police (pour un aperçu canvas/DOM fidèle).
    function cssFamily(key) {
        key = String(key || defKey);
        if (isLib(key)) return `"bobi-${key.slice(4)}", sans-serif`;
        return BUILTIN_CSS[key] || 'sans-serif';
    }

    // Libellé lisible d'une clé (repli : la clé elle-même — jamais de libellé vide).
    function label(key) {
        const f = cat.find(x => x.key === key);
        return (f && f.name) || String(key || '');
    }

    // Remplit un <select> (2 groupes : polices système / bibliothèque) et pose la valeur.
    // `emptyLabel` (optionnel) = libellé d'un choix vide en tête (héritage).
    function fillSelect(sel, value) {
        if (!sel) return;
        const b = cat.filter(f => f.builtin), l = cat.filter(f => !f.builtin);
        const T = (k, fb) => {
            const v = (typeof window.t === 'function') ? window.t(k) : null;
            return (v && v !== k) ? v : fb;
        };
        const opt = f => `<option value="${f.key}">${(f.name || f.key).replace(/[<>&]/g, '')}</option>`;
        let html = `<optgroup label="${T('settings.fonts.grp_builtin', 'Polices système')}">`
                 + b.map(opt).join('') + '</optgroup>';
        if (l.length) {
            html += `<optgroup label="${T('settings.fonts.grp_library', 'Bibliothèque')}">`
                  + l.map(opt).join('') + '</optgroup>';
        }
        sel.innerHTML = html;
        // Clé stockée absente du catalogue (police de bibliothèque supprimée depuis) : on l'affiche
        // explicitement plutôt que de basculer en silence sur une autre police — le rendu, lui,
        // retombera sur DejaVu côté conteneur, et l'utilisateur doit le savoir.
        if (value && !cat.some(f => f.key === value)) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = value + ' — ' + T('settings.fonts.missing', 'police absente');
            sel.appendChild(opt);
        }
        if (value) sel.value = value;
        if (!sel.value) sel.value = defKey;
    }

    return { load, catalog, defaultKey, cssFamily, label, fillSelect, isLib };
})();
