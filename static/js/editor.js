// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
/* ════════════════════════════════════════════════════════════════════════════════════════
 * BASE COMMUNE DES ÉDITEURS — window.BobiEditor (fusion du 2026-10-30, décision de Cyril)
 *
 * Toute fonction d'édition entre ICI, puis dans chaque éditeur (mur multiview, PiP, split,
 * scope, DVE du mélangeur, macros…) — jamais dans un seul. Même règle que les contrôles
 * partagés. Ce fichier réunit les deux bases qui existaient séparément :
 *
 *   BobiEditor.history / .clipboard / .bind / .peers / …  ← ex-BobiEditKit (edit_kit.js) :
 *       annuler/rétablir, presse-papiers, raccourcis, barre d'outils, sélection multiple dans
 *       l'inspecteur. Ne connaît AUCUN rectangle.
 *   BobiEditor.layout                                     ← ex-MXLLayout (layout_engine.js) :
 *       aligner, égaliser, distribuer, aimanter, redimensionner (seul, au format, en groupe),
 *       déplacer un groupe. Ne connaît ni historique, ni clavier, ni DOM.
 *
 * ★ LES DEUX PARTIES RESTENT INDÉPENDANTES : chacune est une fonction fermée, entre ses
 * marqueurs BEGIN/END, qui ne lit rien de l'autre. Un éditeur prend la partie dont il a
 * besoin ; la page publique ne se sert que de la géométrie. Garde :
 * tests/verif_editor_independance.js (exécute chaque partie SEULE).
 * Pas d'alias des anciens noms (consigne) : un appelant oublié doit casser, pas survivre.
 * ════════════════════════════════════════════════════════════════════════════════════════ */
(function () {
'use strict';

// BEGIN:edition ─────────────────────────────────────────────────────────────────────────
/* Noyau d'édition partagé : historique annuler/rétablir, presse-papiers,
 * raccourcis clavier, barre d'outils et aide des raccourcis.
 *
 * Demandé le 2026-10-29 pour les éditeurs de macros (Blocs, Scénario) et l'éditeur de layout du
 * multiview. Trois éditeurs, un seul comportement : Ctrl/⌘+Z ne doit pas vouloir dire trois choses.
 * Ce module ne sait RIEN du contenu édité — l'éditeur lui donne un état (n'importe quel objet
 * sérialisable en JSON) et des fonctions à appeler ; c'est l'éditeur qui sait copier un nœud.
 *
 * Règles communes :
 *  - l'historique garde des INSTANTANÉS de l'état APRÈS chaque modification (100 au plus). Un
 *    instantané identique au précédent n'est pas empilé : un clic qui ne change rien n'occupe pas
 *    un Ctrl+Z ;
 *  - dans un champ de saisie, les raccourcis d'édition sont laissés au navigateur (Ctrl+Z annule
 *    la frappe, Ctrl+C copie le texte). Seuls Ctrl+S (enregistrer) et Échap passent ;
 *  - le presse-papiers vit dans localStorage : on colle d'une macro dans une autre, d'un mur dans
 *    un autre. Il porte un `kind` — un éditeur ne colle que ce qu'il sait lire. Sans stockage
 *    (navigation privée), il reste en mémoire de la page ;
 *  - sélection multiple : une valeur changée dans l'inspecteur va à TOUS les éléments sélectionnés
 *    s'ils sont la même fonction (même clé `keyOf`), sinon au seul élément affiché (`peers`). */
const edition = (function () {
    'use strict';
    const CLIP_KEY = 'bobi.editkit.clipboard';
    const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '');
    let memClip = null;

    function history(max) {
        max = max || 100;
        let stack = [], idx = -1;
        const snap = s => JSON.stringify(s === undefined ? null : s);
        return {
            reset(state) { stack = [snap(state)]; idx = 0; },
            commit(state) {
                const s = snap(state);
                if (idx >= 0 && stack[idx] === s) return false;
                stack = stack.slice(0, idx + 1);
                stack.push(s);
                if (stack.length > max) stack.shift();
                idx = stack.length - 1;
                return true;
            },
            undo() { if (idx <= 0) return undefined; idx--; return JSON.parse(stack[idx]); },
            redo() { if (idx >= stack.length - 1) return undefined; idx++; return JSON.parse(stack[idx]); },
            canUndo() { return idx > 0; },
            canRedo() { return idx >= 0 && idx < stack.length - 1; },
        };
    }

    /* Presse-papiers SYSTÈME, sur demande (`put(kind, data, {system: true})`) : coller dans un AUTRE
     * poste (repris du copier/coller des keyers du mélangeur, 30/10). Un plus, jamais une condition —
     * refusé (contexte non sécurisé, permission), le presse-papiers local reste. `read(kind)` y
     * retombe quand le local n'a rien de ce kind. Un objet du bon kind SANS enveloppe `data` (format
     * d'avant la base commune, ex. `{kind, v, settings}` du mélangeur) est sa propre donnée. */
    function sysWrite(txt) {
        try {
            if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText)
                navigator.clipboard.writeText(txt).catch(() => {});
        } catch (e) { /* local seul */ }
    }
    async function sysRead(kind) {
        try {
            if (typeof navigator === 'undefined' || !navigator.clipboard || !navigator.clipboard.readText) return null;
            const c = JSON.parse(await navigator.clipboard.readText());
            if (!c || typeof c !== 'object' || c.kind !== kind) return null;
            return ('data' in c) ? c : {kind: c.kind, data: c, t: c.at || 0};
        } catch (e) { return null; }
    }
    const clipboard = {
        put(kind, data, opts) {
            const v = {kind, data, t: Date.now()};
            memClip = v;
            try { localStorage.setItem(CLIP_KEY, JSON.stringify(v)); } catch (e) { /* mémoire seule */ }
            if (opts && opts.system) sysWrite(JSON.stringify(v));
        },
        // `kind` (facultatif) : ne rend que ce kind, sinon null.
        get(kind) {
            let c = memClip;
            try {
                const raw = localStorage.getItem(CLIP_KEY);
                if (raw) c = JSON.parse(raw);
            } catch (e) { /* mémoire seule */ }
            return (!kind || (c && c.kind === kind)) ? c : null;
        },
        async read(kind) {
            return clipboard.get(kind) || sysRead(kind);
        },
    };

    function isTyping(t) {
        if (!t) return false;
        const tag = (t.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || !!t.isContentEditable;
    }

    // Touche → action. `null` = pas un raccourci d'édition.
    function shortcut(e) {
        const mod = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
        if (mod && !e.altKey) {
            if (k === 'z') return {action: e.shiftKey ? 'redo' : 'undo'};
            if (k === 'y') return {action: 'redo'};
            if (k === 'c') return {action: 'copy'};
            if (k === 'x') return {action: 'cut'};
            if (k === 'v') return {action: 'paste'};
            if (k === 'd') return {action: 'duplicate'};
            if (k === 'a') return {action: 'selectAll'};
            if (k === 's') return {action: 'save'};
            if (k === 'f') return {action: 'find'};
            if (k === '/' || e.code === 'Slash') return {action: 'toggleDisable'};
            return null;
        }
        if (e.altKey) return null;
        if (k === 'escape') return {action: 'escape'};
        if (k === 'delete' || k === 'backspace') return {action: 'delete'};
        const step = e.shiftKey ? 10 : 1;
        if (k === 'arrowleft') return {action: 'nudge', dx: -step, dy: 0};
        if (k === 'arrowright') return {action: 'nudge', dx: step, dy: 0};
        if (k === 'arrowup') return {action: 'nudge', dx: 0, dy: -step};
        if (k === 'arrowdown') return {action: 'nudge', dx: 0, dy: step};
        if (k === '?') return {action: 'help'};
        return null;
    }
    const PASS_WHILE_TYPING = {save: 1, escape: 1};

    // Raccourcis d'un éditeur : `active()` dit s'il est à l'écran, `on[action](sc, e)` agit. Une
    // action sans gestionnaire est laissée au navigateur. Renvoie la fonction qui détache.
    function bind(opts) {
        const h = e => {
            if (!opts.active || !opts.active()) return;
            const sc = shortcut(e);
            if (!sc) return;
            if (isTyping(e.target) && !PASS_WHILE_TYPING[sc.action]) return;
            const fn = (opts.on || {})[sc.action];
            if (!fn) return;
            if (sc.action === 'escape' && isTyping(e.target)) { e.target.blur(); }
            e.preventDefault();
            fn(sc, e);
        };
        document.addEventListener('keydown', h);
        return () => document.removeEventListener('keydown', h);
    }

    const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    function icon(k) {
        const I = (window.MXLControls && window.MXLControls.ICONS) || {};
        return I[k] || esc(k);
    }
    // Barre d'outils pour le tactile (pas de clavier sur un iPad nu). `defs` = [[icône, titre,
    // onclick (texte JS), id?], …] ; un `null` sépare deux rangées.
    function toolbarHtml(label, defs) {
        const rows = [[]];
        defs.forEach(d => (d ? rows[rows.length - 1].push(d) : rows.push([])));
        return rows.filter(r => r.length).map(r =>
            '<div class="ctl-toolrow" role="group" aria-label="' + esc(label) + '">' + r.map(d =>
                '<button type="button" class="ctl-tool"' + (d[3] ? ' id="' + esc(d[3]) + '"' : '') +
                ' title="' + esc(d[1]) + '" aria-label="' + esc(d[1]) + '" onclick="' + esc(d[2]) + '">' +
                icon(d[0]) + '</button>').join('') + '</div>').join('');
    }
    const MOD = IS_MAC ? '⌘' : 'Ctrl';
    // Libellé lisible d'un raccourci : « mod+z » → « Ctrl + Z » / « ⌘ + Z ».
    function keyLabel(spec) {
        return spec.split('+').map(p => (p === 'mod' ? MOD : p === 'shift' ? (IS_MAC ? '⇧' : 'Maj')
            : p.length === 1 ? p.toUpperCase() : p)).join(' + ');
    }
    // Aide des raccourcis : `rows` = [[spec, libellé], …]. Fenêtre modale générique (base.css).
    function showHelp(title, rows, closeLabel) {
        const old = document.getElementById('bek-help');
        if (old) { old.remove(); return; }
        const ov = document.createElement('div');
        ov.className = 'modal-overlay';
        ov.id = 'bek-help';
        ov.innerHTML = '<div class="modal-card" style="max-width:520px" role="dialog" aria-label="' + esc(title) + '">' +
            '<div class="modal-head"><h3 style="margin:0">' + esc(title) + '</h3>' +
            '<button type="button" class="btn" aria-label="' + esc(closeLabel || '×') + '">✕</button></div>' +
            '<dl class="ctl-keys">' + rows.map(r => '<dt>' + r[0].split(' / ').map(s =>
                '<kbd>' + esc(keyLabel(s)) + '</kbd>').join(' ') + '</dt><dd>' + esc(r[1]) + '</dd>').join('') +
            '</dl></div>';
        const close = () => ov.remove();
        ov.addEventListener('click', e => { if (e.target === ov || e.target.closest('.modal-head button')) close(); });
        ov.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
        document.body.appendChild(ov);
        ov.querySelector('.modal-head button').focus();
    }

    /* Éléments que touche une valeur changée dans l'inspecteur (demande de la recette 29/10 ; base
     * commune décidée le 30/10). `shown` = l'élément affiché, `selection` = la sélection multiple,
     * `keyOf(x)` = la fonction de l'élément (falsy = réglage propre à chacun, jamais partagé).
     * Toute la sélection si `shown` en fait partie et que tous ont la même clé ; sinon `shown`
     * seul — une sélection mêlée ne propage rien. */
    function peers(shown, selection, keyOf) {
        const all = Array.from(selection || []);
        if (!all.includes(shown)) return [shown];
        const k = keyOf(shown);
        return k && all.every(x => keyOf(x) === k) ? all : [shown];
    }

    /* Actions d'ÉDITION communes à tous les éditeurs (30/10) — la barre « Édition » de chacun les
       affiche avec SES boutons ; elles appellent les mêmes actions que le clavier (surface.agir).
       Indispensables au doigt : sur iPad, sans clavier, ce sont les seuls chemins. */
    const tr = (k, d) => { const v = (typeof window !== 'undefined' && window.t) ? window.t(k) : ''; return (v && v !== k) ? v : d; };
    function actionsEdition() {
        return [['undo', 'undo', tr('editor.undo', 'Annuler')], ['redo', 'redo', tr('editor.redo', 'Rétablir')],
                ['cut', 'cut', tr('editor.cut', 'Couper')], ['copy', 'copy_settings', tr('editor.copy', 'Copier')],
                ['paste', 'paste_settings', tr('editor.paste', 'Coller')], ['duplicate', 'duplicate', tr('editor.duplicate', 'Dupliquer')],
                ['delete', 'reset_box', tr('editor.delete', 'Supprimer la sélection')], ['help', 'keyboard', tr('editor.help', 'Raccourcis clavier')]]
            .map(([action, ico, label]) => ({action, ico, label}));
    }
    // Aide STANDARD des raccourcis (un éditeur peut fournir la sienne, plus complète).
    function aideStandard(titre) {
        showHelp(titre || tr('editor.keys_title', "Raccourcis de l'éditeur"), [
            ['mod+z', tr('editor.undo', 'Annuler')], ['mod+shift+z / mod+y', tr('editor.redo', 'Rétablir')],
            ['mod+c', tr('editor.copy', 'Copier')], ['mod+x', tr('editor.cut', 'Couper')], ['mod+v', tr('editor.paste', 'Coller')],
            ['mod+d', tr('editor.duplicate', 'Dupliquer')], ['mod+a', tr('editor.select_all', 'Tout sélectionner')],
            [tr('editor.key_del', 'Suppr'), tr('editor.delete', 'Supprimer la sélection')],
            ['← → ↑ ↓', tr('editor.nudge', 'Déplacer d\'un pixel (Maj : 10)')], [tr('editor.key_esc', 'Échap'), tr('editor.deselect', 'Désélectionner')],
            [tr('editor.key_multi', 'Maj+clic / Ctrl+clic'), tr('editor.multi', 'Ajouter ou retirer de la sélection (la référence est le dernier cliqué)')],
            [tr('editor.key_centre', 'Alt + poignée'), tr('editor.centre', 'Redimensionner autour du centre')],
            ['?', tr('editor.help', 'Raccourcis clavier')]], tr('editor.close', 'Fermer'));
    }

    /* VERROUS AU DOIGT (arbitrage du 30/10, repris du DVE du mélangeur) : sur iPad il n'y a ni
       Maj ni Alt. « Sélection multiple » fait de chaque appui un Maj+clic (et un glisser dans le
       vide trace le rectangle de sélection) ; « Depuis le centre » fait de la poignée un Alt.
       `etat` = l'objet qui porte `multi` / `centre` (la surface d'édition de l'éditeur). Boutons
       à témoin du catalogue (ctl-push--led), mêmes dans tous les éditeurs. */
    function verrousTactiles() {
        return [['multi', tr('editor.touch_multi', 'Sélection multiple'),
                 tr('editor.touch_multi_tip', 'Au doigt : chaque appui ajoute ou retire de la sélection (comme Maj+clic) ; glisser dans le vide trace un rectangle de sélection.')],
                ['centre', tr('editor.touch_centre', 'Depuis le centre'),
                 tr('editor.touch_centre_tip', 'Au doigt : la poignée agrandit autour du centre (comme Alt).')]]
            .map(([cle, label, tip]) => ({cle, label, tip}));
    }
    function verrousHtml(etat) {
        return '<div class="bek-verrous">' + verrousTactiles().map(v =>
            '<button type="button" role="switch" class="ctl-push ctl-push--small ctl-push--led" data-bek-verrou="' + v.cle +
            '" aria-pressed="' + (etat && etat[v.cle] ? 'true' : 'false') + '" title="' + esc(v.tip) + '"><span class="ctl-led"></span>' +
            esc(v.label) + '</button>').join('') + '</div>';
    }
    function lierVerrous(racine, etat) {
        if (!racine || !etat) return;
        racine.querySelectorAll('[data-bek-verrou]').forEach(b => b.addEventListener('click', () => {
            const k = b.dataset.bekVerrou;
            etat[k] = !etat[k];
            racine.querySelectorAll('[data-bek-verrou="' + k + '"]').forEach(x => x.setAttribute('aria-pressed', etat[k] ? 'true' : 'false'));
        }));
    }

    return {history, clipboard, isTyping, shortcut, bind, toolbarHtml, showHelp, keyLabel,
            peers, MOD, IS_MAC, actionsEdition, aideStandard, verrousTactiles, verrousHtml, lierVerrous};
})();
// END:edition

// BEGIN:layout ──────────────────────────────────────────────────────────────────────────
/* Moteur de géométrie d'un éditeur de disposition (ex-window.MXLLayout) — la partie qui était écrite QUATRE fois.
 *
 * ═══════════════════════════════════════════════════════════════════════════════════════
 * POURQUOI CE FICHIER EXISTE
 *
 * Le relevé du 2026-08-27 (TODO.md, « Un seul moteur d'édition de layout au lieu de
 * quatre ») constate que le catalogue de contrôles partage l'APPARENCE — barre d'outils,
 * icônes, styles — mais pas la GÉOMÉTRIE. Aimant, guides, alignement, distribution et
 * égalisation de taille sont réimplémentés dans `multiview.js` (4 331 lignes),
 * `pip_editor.js` (2 220), `split/control.js` et `scope/`. La décision d'alors — copier
 * une quatrième fois pour le scope — était la moins risquée, et l'entrée existe « pour que
 * le prix reste visible et qu'on puisse le payer un jour ».
 *
 * Cette partie paie ce prix. Il n'est PAS une cinquième copie : c'est l'extraction, et son
 * premier consommateur est `plugins/hello_world` — un plugin d'EXEMPLE dont rien ne dépend.
 * Le pilote est là à dessein : si l'extraction est fausse, le rayon d'explosion est nul.
 * Migrer le composer du mur en premier, « le chemin le plus sensible du produit », aurait
 * été l'inverse de la prudence.
 *
 * ═══════════════════════════════════════════════════════════════════════════════════════
 * CE QUE CE MOTEUR NE FAIT PAS, ET POURQUOI
 *
 * Il ne dessine rien, ne connaît ni canvas ni DOM, n'écoute aucun événement. Il prend des
 * rectangles et rend des rectangles. C'est ce qui le rend adoptable par quatre éditeurs qui
 * dessinent de quatre façons différentes — celui du mur peint des tuiles GPU, celui du PiP
 * un canvas 2D. Un moteur qui aurait su dessiner n'aurait servi qu'à l'un d'eux.
 *
 * ═══════════════════════════════════════════════════════════════════════════════════════
 * SÉMANTIQUE — REPRISE À L'IDENTIQUE, PAS RÉINVENTÉE
 *
 * Chaque règle ci-dessous vient de `static/pip_editor.js`. Les migrations futures doivent
 * être des diffs LISIBLES, pas des réconciliations : si ce moteur avait « amélioré » le
 * comportement au passage, plus personne n'aurait pu vérifier une migration.
 *
 *   · Coordonnées NORMALISÉES 0..1, arrondies à 3 décimales (`r3`). Un éditeur qui
 *     travaille en pixels convertit avant/après — c'est lui qui connaît sa taille.
 *   · Alignement : la RÉFÉRENCE est l'élément primaire dès qu'il y a ≥2 sélectionnés, et
 *     il ne bouge pas. À un seul sélectionné, la référence est le cadre.
 *   · Égalisation de taille : ≥2 sélectionnés, référence = le primaire.
 *   · Distribution : ≥3 sélectionnés, et elle répartit les POSITIONS entre la première et
 *     la dernière — pas les espaces. C'est le comportement historique ; le documenter
 *     évite qu'on le « corrige » un jour en croyant à un bug. La distribution des ESPACES
 *     existe à côté (`distribuerEspaces`), en OPÉRATION SÉPARÉE : c'est l'ajout qui était
 *     attendu, pas la modification de celle-ci.
 *   · Aimant : cibles = bords et centres du cadre, plus bords et centres de tous les
 *     éléments NON sélectionnés. Tolérance en pixels, fournie par l'appelant.
 * ═══════════════════════════════════════════════════════════════════════════════════════ */
const layout = (function () {
  "use strict";

  /* Arrondi à 3 décimales — la précision retenue par les éditeurs existants. Au-delà, un
     glisser produit des `0.30000000000000004` qui polluent les diffs de layout persistés. */
  function r3(v) { return Math.round(v * 1000) / 1000; }

  /* Rectangle borné au cadre : un élément ne sort jamais, et sa taille est préservée —
     c'est la position qui cède, pas la largeur. Rogner la taille au bord ferait rétrécir un
     élément qu'on pousse contre un mur, ce que personne n'attend d'un glisser. */
  function borner(r) {
    const w = Math.max(0, Math.min(1, r.w));
    const h = Math.max(0, Math.min(1, r.h));
    return { ...r, w: r3(w), h: r3(h),
             x: r3(Math.max(0, Math.min(1 - w, r.x))),
             y: r3(Math.max(0, Math.min(1 - h, r.y))) };
  }

  /* Référence d'alignement : le primaire dès 2 sélectionnés, sinon `refSeul` s'il est fourni
     (l'éditeur de PiP aligne un composant seul sur la cellule OU sur l'image), sinon le cadre. */
  function _reference(rects, sel, primaire, refSeul) {
    if (sel.length >= 2 && rects[primaire]) {
      const p = rects[primaire];
      return { x: p.x || 0, y: p.y || 0, w: p.w || 0, h: p.h || 0 };
    }
    return refSeul || { x: 0, y: 0, w: 1, h: 1 };
  }

  /* Aligne la sélection. `mode` ∈ left|right|hcenter|top|bottom|vcenter.
     Rend une COPIE : le moteur ne mute jamais ce qu'on lui donne, pour que l'appelant
     puisse comparer avant/après (annulation, marquage « modifié »). */
  /* `opts.libre` : ni bornage au cadre ni arrondi — pour un éditeur dont les éléments peuvent
     sortir du cadre (le split : entrée/sortie de champ) et qui arrondit lui-même. */
  function aligner(rects, sel, primaire, mode, refSeul, opts) {
    const libre = !!(opts && opts.libre);
    const ref = _reference(rects, sel, primaire, refSeul);
    const out = rects.map((r) => ({ ...r }));
    sel.forEach((i) => {
      if (sel.length >= 2 && i === primaire) return;   // la référence ne bouge pas
      const c = out[i];
      if (!c) return;
      switch (mode) {
        case "left":    c.x = ref.x; break;
        case "right":   c.x = ref.x + ref.w - c.w; break;
        case "hcenter": c.x = ref.x + (ref.w - c.w) / 2; break;
        case "top":     c.y = ref.y; break;
        case "bottom":  c.y = ref.y + ref.h - c.h; break;
        case "vcenter": c.y = ref.y + (ref.h - c.h) / 2; break;
        default: return;
      }
      out[i] = libre ? c : borner(c);
    });
    return out;
  }

  /* Égalise la taille sur le primaire. `mode` ∈ w|h|both. Exige ≥2 sélectionnés :
     à un seul, l'opération n'a pas de référence — on rend `null` plutôt que de ne rien
     faire en silence, pour que l'appelant puisse le DIRE à l'exploitant. */
  function egaliser(rects, sel, primaire, mode, opts) {
    const libre = !!(opts && opts.libre);   // ni bornage ni arrondi : l'appelant s'en charge
    if (sel.length < 2) return null;
    const ref = rects[primaire];
    if (!ref) return null;
    const out = rects.map((r) => ({ ...r }));
    sel.forEach((i) => {
      if (i === primaire) return;
      const c = out[i];
      if (!c) return;
      // Plafond = le cadre (1 en normalisé, `opts.cadre` en pixels).
      if (mode === "w" || mode === "both") c.w = Math.min(ref.w, (opts && opts.cadre && opts.cadre.w) || 1);
      if (mode === "h" || mode === "both") c.h = Math.min(ref.h, (opts && opts.cadre && opts.cadre.h) || 1);
      out[i] = libre ? c : borner(c);
    });
    return out;
  }

  /* Distribue la sélection sur un axe. `axe` ∈ h|v. Exige ≥3 sélectionnés (à deux, la
     distribution est l'identité). Répartit les POSITIONS entre la première et la dernière —
     comportement historique, cf. l'en-tête. */
  /* `opts.ancre` = "centre" : répartit les CENTRES (le split), les extrêmes ne bougent pas ;
     `opts.arrondi` remplace `r3` (l'appelant qui arrondit lui-même passe l'identité). */
  function distribuer(rects, sel, axe, opts) {
    if (sel.length < 3) return null;
    const k = axe === "h" ? "x" : "y";
    const out = rects.map((r) => ({ ...r }));
    const arr = (opts && opts.arrondi) || r3;
    if (opts && opts.ancre === "centre") {
      const t = axe === "h" ? "w" : "h";
      const ctr = (i) => (out[i][k] || 0) + (out[i][t] || 0) / 2;
      const idc = sel.slice().sort((a, b) => ctr(a) - ctr(b));
      const c0 = ctr(idc[0]), cN = ctr(idc[idc.length - 1]);
      const pasC = (cN - c0) / (idc.length - 1);
      idc.forEach((i, n) => {
        if (n === 0 || n === idc.length - 1) return;
        out[i][k] = arr(c0 + pasC * n - (out[i][t] || 0) / 2);
      });
      return out;
    }
    const idx = sel.slice().sort((a, b) => (out[a][k] || 0) - (out[b][k] || 0));
    const v0 = out[idx[0]][k] || 0;
    const vN = out[idx[idx.length - 1]][k] || 0;
    const pas = (vN - v0) / (idx.length - 1);
    idx.forEach((i, n) => { out[i][k] = arr(v0 + pas * n); });
    return out;
  }

  /* Distribue les ESPACES entre les éléments, sur un axe. Exige ≥3 sélectionnés.

     ★ CE N'EST PAS `distribuer`, ET C'EST VOULU. `distribuer` répartit les POSITIONS à pas
     constant ; avec des éléments de largeurs inégales, les espaces obtenus ne le sont pas —
     trois éléments de 0,30 / 0,05 / 0,30 finissent à 0 et 0,25 d'écart, soit deux collés et
     un trou. Celle-ci égalise les INTERVALLES entre les bords, ce que fait un outil de
     dessin quand on lui demande « distribuer l'espacement ».
     Aucune des deux n'est fausse : ce sont deux opérations, et les outils sérieux offrent
     les deux. On AJOUTE donc, on ne remplace pas — changer `distribuer` modifierait en
     silence le comportement des quatre éditeurs qui migreront vers ce moteur.

     Les extrêmes ne bougent pas : ils définissent l'étendue. Si les éléments sont plus
     larges que l'étendue disponible, l'espace calculé est NÉGATIF et ils se chevauchent —
     on le laisse arriver plutôt que de l'écrêter, parce qu'un chevauchement visible dit à
     l'exploitant que sa demande était impossible, là où un écrêtage silencieux lui rendrait
     une disposition qui n'est ni la sienne ni régulière. */
  function distribuerEspaces(rects, sel, axe, opts) {
    if (sel.length < 3) return null;
    const arr = (opts && opts.arrondi) || r3;
    const k = axe === "h" ? "x" : "y";
    const t = axe === "h" ? "w" : "h";
    const out = rects.map((r) => ({ ...r }));
    const idx = sel.slice().sort((a, b) => (out[a][k] || 0) - (out[b][k] || 0));
    const prem = out[idx[0]], dern = out[idx[idx.length - 1]];
    const etendue = (dern[k] + dern[t]) - prem[k];
    const occupe = idx.reduce((s, i) => s + (out[i][t] || 0), 0);
    const espace = (etendue - occupe) / (idx.length - 1);
    let pos = prem[k] + (prem[t] + espace);   // même ordre que le curseur du scope : au bit près
    idx.slice(1, -1).forEach((i) => {
      out[i][k] = arr(pos);
      pos += out[i][t] + espace;
    });
    return out;
  }

  /* Cibles d'aimantage : bords et centre du cadre, plus bords et centres des éléments non
     sélectionnés. En NORMALISÉ par défaut ; `opts.cadre` = {w, h} pour travailler en pixels
     (l'éditeur de PiP aimante les rectangles DESSINÉS, ancrages compris, en px canvas). */
  function ciblesAimant(rects, exclus, opts) {
    const c = (opts && opts.cadre) || {};
    const W = c.w || 1, H = c.h || 1, X = c.x || 0, Y = c.y || 0;   // x/y : cadre décalé (marges)
    const xs = [X, X + W, X + W / 2];
    const ys = [Y, Y + H, Y + H / 2];
    rects.forEach((r, i) => {
      if (!r || exclus.includes(i)) return;            // null = objet absent/masqué
      xs.push(r.x, r.x + r.w, r.x + r.w / 2);
      ys.push(r.y, r.y + r.h, r.y + r.h / 2);
    });
    return { xs, ys };
  }

  /* Aimante un rectangle en cours de glisser.
     `tol` est une tolérance NORMALISÉE (l'appelant convertit ses pixels : il est le seul à
     connaître la taille de son canvas — c'est précisément ce que le moteur ne doit pas
     savoir). Rend la position aimantée ET les guides à dessiner : sans eux, l'exploitant
     voit son élément « coller » sans comprendre à quoi. */
  function aimanter(rects, exclus, x, y, w, h, tol, opts) {
    const { xs, ys } = ciblesAimant(rects, exclus, opts);
    const arr = (opts && opts.arrondi) || r3;
    const guides = [];
    let meilleurX = tol + 1, ax = x;
    [["l", x], ["r", x + w], ["c", x + w / 2]].forEach(([bord, v]) => {
      xs.forEach((t) => {
        const d = Math.abs(v - t);
        if (d <= tol && d < meilleurX) {
          meilleurX = d;
          ax = bord === "l" ? t : bord === "r" ? t - w : t - w / 2;
          guides[0] = { axe: "v", pos: t };
        }
      });
    });
    let meilleurY = tol + 1, ay = y;
    [["t", y], ["b", y + h], ["c", y + h / 2]].forEach(([bord, v]) => {
      ys.forEach((t) => {
        const d = Math.abs(v - t);
        if (d <= tol && d < meilleurY) {
          meilleurY = d;
          ay = bord === "t" ? t : bord === "b" ? t - h : t - h / 2;
          guides[1] = { axe: "h", pos: t };
        }
      });
    });
    return { x: arr(ax), y: arr(ay), guides: guides.filter(Boolean) };
  }

  /* Aimante le COIN BAS-DROIT d'un élément qu'on redimensionne (poignée ancrée en haut-gauche) :
     seuls ses bords droit et bas cherchent une cible, et une cible n'est retenue que si elle est
     au-delà du bord opposé (une largeur nulle ou négative n'a pas de sens). Mêmes cibles et
     mêmes unités qu'`aimanter`. Rend {w, h, guides}. */
  function aimanterCoin(rects, exclus, x, y, w, h, tol, opts) {
    const { xs, ys } = ciblesAimant(rects, exclus, opts);
    const guides = [];
    let meilleurX = tol + 1, sw = w;
    xs.forEach((t) => {
      const d = Math.abs((x + w) - t);
      if (d <= tol && d < meilleurX && t > x) { meilleurX = d; sw = t - x; guides[0] = { axe: "v", pos: t }; }
    });
    let meilleurY = tol + 1, sh = h;
    ys.forEach((t) => {
      const d = Math.abs((y + h) - t);
      if (d <= tol && d < meilleurY && t > y) { meilleurY = d; sh = t - y; guides[1] = { axe: "h", pos: t }; }
    });
    return { w: sw, h: sh, guides: guides.filter(Boolean) };
  }

  /* Aimante le coin bas-droit d'un élément redimensionné À FORMAT CONSTANT : le bord droit OU
     le bas, celui qui est le plus près d'une cible, donne le FACTEUR d'échelle (l'autre suit).
     Rend {facteur, guides} — facteur 1 et aucun repère sans cible à portée. */
  function aimanterCoinAuFormat(rects, exclus, x, y, w, h, tol, opts) {
    const { xs, ys } = ciblesAimant(rects, exclus, opts);
    let meilleur = tol + 1, facteur = 1, guides = [];
    xs.forEach((t) => {
      const d = Math.abs((x + w) - t);
      if (d <= tol && d < meilleur && t > x && w > 0) { meilleur = d; facteur = (t - x) / w; guides = [{ axe: "v", pos: t }]; }
    });
    ys.forEach((t) => {
      const d = Math.abs((y + h) - t);
      if (d <= tol && d < meilleur && t > y && h > 0) { meilleur = d; facteur = (t - y) / h; guides = [{ axe: "h", pos: t }]; }
    });
    return { facteur, guides };
  }

  /* Sélection au clic, avec les modificateurs usuels. Rend {sel, primaire}.
     Extrait ici parce que les quatre éditeurs implémentaient la même table de vérité — et
     que trois d'entre eux oubliaient un cas : re-cliquer un élément DÉJÀ primaire dans une
     sélection multiple doit la garder, pas la réduire à un seul. */
  function sélection(sel, primaire, i, mods) {
    const additif = !!(mods && (mods.shiftKey || mods.ctrlKey || mods.metaKey));
    if (i == null) return additif ? { sel, primaire } : { sel: [], primaire: null };
    if (!additif) {
      return sel.length > 1 && sel.includes(i) ? { sel, primaire: i } : { sel: [i], primaire: i };
    }
    if (sel.includes(i)) {
      const reste = sel.filter((k) => k !== i);
      return { sel: reste, primaire: reste.includes(primaire) ? primaire : (reste[0] ?? null) };
    }
    return { sel: sel.concat([i]), primaire: i };
  }

  /* ═══ ÉDITION D'UNE SÉLECTION MULTIPLE ET TENUE DU FORMAT (remontés du composer multiview
     0.116.24, 2026-10-30 — décision de Cyril : toute fonction d'édition entre ICI, puis dans
     chaque éditeur, jamais dans un seul).

     Ces trois opérations sont indépendantes des UNITÉS, contrairement à celles du dessus : le
     composer du mur travaille en pixels pairs (chroma), le PiP en normalisé. L'appelant passe
     son cadre (`{w, h}`, défaut 1×1), sa fonction d'arrondi (défaut `r3`) et son plancher
     (défaut 0). Imposer le normalisé aurait obligé le mur à convertir, puis à re-arrondir au
     pair DERRIÈRE le moteur — et c'est précisément ce double arrondi qui ouvre les interstices
     d'un pixel entre des tuiles collées. ═══ */

  function _opts(o) {
    o = o || {};
    return { W: (o.cadre && o.cadre.w) || 1, H: (o.cadre && o.cadre.h) || 1,
             arr: o.arrondi || r3, min: o.min || 0 };
  }

  /* Redimensionne UN élément à la poignée bas-droite en tenant un FORMAT (L/H) : la largeur
     visée est bornée au cadre et au plancher, la hauteur en découle ; si elle déborde en bas,
     c'est la hauteur qui cède et la largeur suit — jamais de déformation. `ratio` absent ou
     nul = format courant de l'élément. Rend {w, h} (la position ne bouge pas). */
  function redimensionnerAuFormat(r, largeur, ratio, opts) {
    const { W, H, arr, min } = _opts(opts);
    const k = ratio > 0 ? ratio : (r.h > 0 ? r.w / r.h : 1);
    let w = Math.max(min, Math.min(W - r.x, largeur));
    let h = arr(w / k);              // comparée ARRONDIE : c'est la hauteur qui sera posée
    if (h > H - r.y) { h = H - r.y; w = h * k; }
    return { w: arr(w), h: arr(h) };
  }

  /* Redimensionne une SÉLECTION ensemble, à la poignée du primaire : mise à l'échelle depuis
     le coin haut-gauche du groupe, d'un même facteur pour tous.
       · le facteur suit le bord droit du primaire (`largeur` = sa largeur visée) ;
       · chaque élément garde son format ;
       · des éléments jointifs le restent : on arrondit les BORDS (gauche et droit) une seule
         fois, puis la largeur en découle — arrondir largeur et position séparément ouvre des
         interstices dès que le facteur n'est pas entier ;
       · borné pour qu'aucun membre ne sorte du cadre ni ne passe sous le plancher.
     Rend une COPIE de `rects`, ou `null` sous deux sélectionnés (la poignée d'un élément seul
     relève de `redimensionnerAuFormat` : ce sont deux gestes, l'appelant choisit). */
  function redimensionnerGroupe(rects, sel, primaire, largeur, opts) {
    const { W, H, arr, min } = _opts(opts);
    const membres = sel.filter((i) => rects[i]);
    if (membres.length < 2 || !membres.includes(primaire)) return null;
    const ax = Math.min(...membres.map((i) => rects[i].x));
    const ay = Math.min(...membres.map((i) => rects[i].y));
    const p = rects[primaire];
    let s = (p.x + largeur - ax) / (p.x + p.w - ax);
    membres.forEach((i) => {
      const r = rects[i];
      s = Math.min(s, (W - ax) / (r.x + r.w - ax), (H - ay) / (r.y + r.h - ay));
    });
    if (min > 0) s = Math.max(s, min / Math.min(...membres.map((i) => Math.min(rects[i].w, rects[i].h))));
    const out = rects.map((r) => (r ? { ...r } : r));
    membres.forEach((i) => {
      const r = rects[i];
      const x0 = arr(ax + (r.x - ax) * s), x1 = arr(ax + (r.x + r.w - ax) * s);
      const y0 = arr(ay + (r.y - ay) * s), y1 = arr(ay + (r.y + r.h - ay) * s);
      out[i] = { ...r, x: x0, y: y0, w: arr(Math.min(W - x0, x1 - x0)), h: arr(Math.min(H - y0, y1 - y0)) };
    });
    return out;
  }

  /* Déplace une SÉLECTION d'un même delta, borné pour qu'aucun membre ne sorte du cadre.
     `origines` = rectangles AU DÉBUT du geste (on applique toujours depuis l'origine, jamais en
     cumulant : sinon le groupe dérive au fil des événements souris). Groupe plus large que le
     cadre sur un axe : cet axe est figé plutôt que d'appliquer une borne inversée.
     Rend {dx, dy} effectifs — l'appelant les applique à ses objets, qui ne sont pas forcément
     des rectangles de ce moteur (le mur mêle fenêtres, textes, VU, frises). */
  function deplacerGroupe(origines, dx, dy, opts) {
    const { W, H } = _opts(opts);
    let loX = -Infinity, hiX = Infinity, loY = -Infinity, hiY = Infinity;
    origines.forEach((g) => {
      loX = Math.max(loX, -g.x); hiX = Math.min(hiX, W - g.w - g.x);
      loY = Math.max(loY, -g.y); hiY = Math.min(hiY, H - g.h - g.y);
    });
    if (loX > hiX) loX = hiX = 0;
    if (loY > hiY) loY = hiY = 0;
    return { dx: Math.max(loX, Math.min(hiX, dx)), dy: Math.max(loY, Math.min(hiY, dy)) };
  }

  /* ═══ AIMANT DU REDIMENSIONNEMENT — « selon le format » (arbitrage de Cyril, 30/10) ═══
     Poignée bas-droite (redimensionnement ancré en haut-gauche). Cibles = bord lointain et centre
     du cadre + bords des autres éléments (pas leurs centres) : celles du composer du mur, l'éditeur
     de référence.
       · `opts.ratio` > 0 — élément à FORMAT VERROUILLÉ (vidéo, tuile au format de son modèle, box
         du Split) : un rectangle à format fixe ne satisfait qu'UNE cible à la fois. On retient la
         plus proche du coin (bord droit ↔ cibles verticales, bas ↔ horizontales), l'autre dimension
         suit le format. Une cible dont la dimension déduite sortirait du cadre ou passerait sous le
         plancher n'est pas atteignable : écartée. Si l'autre bord tombe à `opts.repere` près d'une
         cible (grille régulière), son repère est tracé aussi. Avant le mur 0.111.0, les deux axes
         étaient aimantés séparément : la hauteur déduite était écrasée, la fenêtre DÉFORMÉE.
       · sinon — élément LIBRE (texte, VU, composant) : chaque axe s'aimante seul.
     opts : {cadre:{x,y,w,h}, ratio, min (plancher, défaut 0), arrondi (dimension déduite, défaut
     identité), repere (tolérance du second repère, défaut 1)}. Rend {w, h, guides}. */
  function aimanterRedim(rects, exclus, x, y, w, h, tol, opts) {
    const o = opts || {};
    const c = o.cadre || {};
    const W = c.w || 1, H = c.h || 1, X = c.x || 0, Y = c.y || 0;
    const min = o.min || 0, arr = o.arrondi || ((v) => v), rep = o.repere == null ? 1 : o.repere;
    const xs = [X + W, X + W / 2], ys = [Y + H, Y + H / 2];
    rects.forEach((r, i) => {
      if (!r || exclus.includes(i)) return;
      xs.push(r.x, r.x + r.w);
      ys.push(r.y, r.y + r.h);
    });
    const guides = [];
    if (o.ratio > 0) {
      const k = o.ratio;
      let best = null;
      xs.forEach((t) => {
        const d = Math.abs((x + w) - t);
        if (d > tol) return;
        const nw = t - x, nh = arr(nw / k);
        if (nw < min || nh < min || y + nh > Y + H) return;
        if (!best || d < best.d) best = { d, w: nw, h: nh, axe: "v" };
      });
      ys.forEach((t) => {
        const d = Math.abs((y + h) - t);
        if (d > tol) return;
        const nh = t - y, nw = arr(nh * k);
        if (nw < min || nh < min || x + nw > X + W) return;
        if (!best || d < best.d) best = { d, w: nw, h: nh, axe: "h" };
      });
      if (!best) return { w: Math.max(min, w), h: Math.max(min, h), guides };
      guides.push(best.axe === "v" ? { axe: "v", pos: x + best.w } : { axe: "h", pos: y + best.h });
      if (best.axe === "v") {
        if (ys.some((t) => Math.abs((y + best.h) - t) <= rep)) guides.push({ axe: "h", pos: y + best.h });
      } else if (xs.some((t) => Math.abs((x + best.w) - t) <= rep)) {
        guides.push({ axe: "v", pos: x + best.w });
      }
      return { w: best.w, h: best.h, guides };
    }
    let bx = tol + 1, sw = w;
    xs.forEach((t) => { const d = Math.abs((x + w) - t); if (d <= tol && d < bx) { bx = d; sw = t - x; } });
    if (bx <= tol) guides.push({ axe: "v", pos: x + sw });
    let by = tol + 1, sh = h;
    ys.forEach((t) => { const d = Math.abs((y + h) - t); if (d <= tol && d < by) { by = d; sh = t - y; } });
    if (by <= tol) guides.push({ axe: "h", pos: y + sh });
    return { w: Math.max(min, sw), h: Math.max(min, sh), guides };
  }

  /* REMPLIR (remonté du composer du mur, 30/10) : la sélection se TUILE le long d'un axe, dans son
     ordre À L'ÉCRAN — parts égales sur l'axe, l'autre dimension suit le FORMAT de chacun (pas de
     déformation), centrée, réduite si elle déborderait. Un élément SEUL et LIBRE prend toute la
     largeur (ou hauteur) du cadre, l'autre dimension inchangée (le geste du bandeau) ; un élément
     seul à format verrouillé suit le tuilage (plein cadre à format constant).
     opts : {cadre:{w,h}, format(i) → ratio (0 = libre ; défaut : forme courante), pas (taille d'une
     part, défaut floor(W/n)), dim(v) et pos(v) d'arrondi (défaut r3)}. Rend une COPIE. */
  function remplir(rects, sel, axe, opts) {
    const o = opts || {};
    const W = (o.cadre && o.cadre.w) || 1, H = (o.cadre && o.cadre.h) || 1;
    const dim = o.dim || r3, pos = o.pos || r3;
    const out = rects.map((r) => (r ? { ...r } : r));
    const membres = sel.filter((i) => out[i]);
    if (!membres.length) return null;
    const format = (i) => {
      const f = o.format ? o.format(i) : 0;
      return f > 0 ? f : 0;
    };
    if (membres.length === 1 && !format(membres[0])) {
      const r = out[membres[0]];
      if (axe === "h") { r.x = 0; r.w = o.plein ? o.plein(W) : W; }
      else { r.y = 0; r.h = o.plein ? o.plein(H) : H; }
      return out;
    }
    const n = membres.length;
    const ratio = (i) => format(i) || (out[i].h > 0 ? out[i].w / out[i].h : 16 / 9);
    if (axe === "h") {
      const idx = membres.slice().sort((a, b) => out[a].x - out[b].x);
      const col = o.pas ? o.pas(W, n) : W / n;
      idx.forEach((i, k) => {
        const f = out[i], r = ratio(i);
        f.x = k * col;
        let w = (k === n - 1) ? (W - k * col) : col;
        let h = w / r;
        if (h > H) { h = H; w = h * r; }
        f.w = dim(w); f.h = dim(h);
        if (f.x + f.w > W) f.x = W - f.w;
        f.y = pos((H - f.h) / 2);
      });
    } else {
      const idx = membres.slice().sort((a, b) => out[a].y - out[b].y);
      const row = o.pas ? o.pas(H, n) : H / n;
      idx.forEach((i, k) => {
        const f = out[i], r = ratio(i);
        f.y = k * row;
        let h = (k === n - 1) ? (H - k * row) : row;
        let w = h * r;
        if (w > W) { w = W; h = w / r; }
        f.w = dim(w); f.h = dim(h);
        if (f.y + f.h > H) f.y = H - f.h;
        f.x = pos((W - f.w) / 2);
      });
    }
    return out;
  }

  return { r3, borner, aligner, egaliser, distribuer, distribuerEspaces, aimanterRedim, remplir,
           aimanter, aimanterCoin, aimanterCoinAuFormat, ciblesAimant, sélection,
           redimensionnerAuFormat, redimensionnerGroupe, deplacerGroupe };
})();
// END:layout

// BEGIN:surface ─────────────────────────────────────────────────────────────────────────
/* ═══ SURFACE D'ÉDITION — le comportement d'un éditeur de disposition, écrit UNE fois ═══
 *
 * Étape 3 du chantier « un seul moteur d'édition » (30/10, arbitrages de Cyril). La géométrie
 * (`layout`) calcule ; la surface ORCHESTRE les gestes : sélection, glisser seul ou en groupe avec
 * l'aimant, poignée seule (selon le format) ou de groupe (depuis le coin), flèches, outils. Elle
 * compose les deux autres parties ; elles ne la connaissent pas.
 *
 * Elle ne dessine rien et n'écoute aucun événement : l'éditeur lui passe les positions DANS SES
 * UNITÉS (pixels de son canevas, fractions…) et lui décrit son monde par un ADAPTATEUR :
 *   objets()        → [{x, y, w, h, format?, min?}] (null = absent/masqué). `format` > 0 = format
 *                     VERROUILLÉ (vidéo, tuile au format de son modèle, box du Split) ;
 *   cibles()        → rectangles qui attirent l'aimant (défaut : objets()) ;
 *   cadre()         → {x, y, w, h} ;
 *   ecrire(i, r)    → pose la géométrie dans le modèle de l'éditeur (il arrondit/borne à sa façon) ;
 *   rendu()         → redessiner (les repères sont dans `S.guides`) ;
 *   valider(idx)    → fin de geste qui a modifié `idx` : persister + empiler l'historique ;
 * et des options : horsCadre (Split : true), tol() (aimant), aimant() (interrupteur), arrondi
 * (positions/dimensions, défaut identité), arrondiGroupe (bords d'un redimensionnement de groupe),
 * min (plancher ; un objet peut porter min, minW, minH), pas {fleche, maj}, refSeul() (référence
 * d'alignement d'un objet SEUL, défaut : le cadre), bornesCentre() (hors-cadre : limites des centres),
 * seuil (distance avant qu'un glisser ne parte), rectangle (sélection au rectangle dans le vide, avec
 * un modificateur ou le verrou « Sélection multiple » ; l'éditeur dessine `S.rectSel`).
 * Un objet peut aussi porter un PLAFOND maxW / maxH (poignée seule : le DVE du mélangeur ne dépasse
 * pas le plein cadre, même hors cadre) — l'aimant n'emmène jamais au-delà.
 *
 * AU DOIGT (iPad, sans clavier — ajouté pour le DVE du mélangeur, 30/10) : deux VERROUS que
 * l'éditeur expose en boutons, équivalents des modificateurs. `S.multi = true` → chaque appui agit
 * comme Maj+clic (ajoute/retire, la référence reste le dernier touché) ; `S.centre = true` → la
 * poignée d'un objet seul agit comme Alt (autour du centre).
 *
 * Règles arbitrées le 30/10 — elles vivent ICI et nulle part ailleurs :
 *   · sélection : référence = DERNIER cliqué ; Maj OU Ctrl/⌘ ajoutent/retirent ;
 *   · groupe contre un bord : TOUT le groupe s'arrête (sauf hors-cadre permis) ;
 *   · poignée seule : format verrouillé → facteur contraint, sinon axes séparés ;
 *   · poignée d'une sélection multiple : mise à l'échelle DEPUIS LE COIN du groupe. */
const surface = (function () {
  return function creer(a, opts) {
    const o = opts || {};
    const arr = o.arrondi || ((v) => v);
    const min = o.min || 0;
    const pas = o.pas || { fleche: 1, maj: 10 };
    const S = { sel: [], primaire: null, guides: [], geste: null };
    const objs = () => a.objets();
    const cadre = () => a.cadre();
    const cibles = () => (a.cibles ? a.cibles() : objs());
    const aimantOn = () => (o.aimant ? o.aimant() : true);
    const tol = () => (o.tol ? o.tol() : 8);
    const optsCadre = () => { const c = cadre(); return { cadre: c, arrondi: arr, min }; };
    const ecrire = (i, r) => a.ecrire(i, r);
    // Les outils de la géométrie travaillent sur un cadre à l'origine : on y translate.
    const versLocal = (r, c) => (r ? { ...r, x: r.x - c.x, y: r.y - c.y } : r);
    const versSurface = (r, c) => (r ? { ...r, x: r.x + c.x, y: r.y + c.y } : r);
    // Delta d'un GROUPE borné : au cadre (cas courant) ; hors-cadre permis, aux bornes de CENTRE
    // de l'éditeur s'il en publie (`bornesCentre()` → {x0, x1, y0, y1} : le Split, caps.pos) —
    // tout le groupe s'arrête quand l'un des siens atteint la sienne (arbitrage du 30/10).
    function deltaGroupe(membres, dx, dy, c) {
      if (!o.horsCadre) return layout.deplacerGroupe(membres.map((z) => versLocal(z, c)), dx, dy, { cadre: c });
      const b = o.bornesCentre && o.bornesCentre();
      if (!b) return { dx, dy };
      let lx = -Infinity, hx = Infinity, ly = -Infinity, hy = Infinity;
      membres.forEach((z) => {
        const cx = z.x + z.w / 2, cy = z.y + z.h / 2;
        lx = Math.max(lx, b.x0 - cx); hx = Math.min(hx, b.x1 - cx);
        ly = Math.max(ly, b.y0 - cy); hy = Math.min(hy, b.y1 - cy);
      });
      if (lx > hx) lx = hx = 0;
      if (ly > hy) ly = hy = 0;
      return { dx: Math.max(lx, Math.min(hx, dx)), dy: Math.max(ly, Math.min(hy, dy)) };
    }

    S.selectionner = function (i, mods) {
      const r = layout.sélection(S.sel, S.primaire, i, mods);
      S.sel = r.sel; S.primaire = r.primaire;
      return S;
    };
    S.vider = function () { S.sel = []; S.primaire = null; return S; };

    /* Début de geste. `t` = {i, poignee} rendu par l'éditeur (son test de toucher), ou null (clic
       dans le vide → la sélection se vide, sauf avec un modificateur). Rend true si un geste démarre. */
    S.multi = false; S.centre = false;
    const avecVerrou = (mods) => (S.multi ? Object.assign({}, mods || {}, { shiftKey: true }) : mods);
    S.appui = function (pos, t, mods) {
      mods = avecVerrou(mods);
      const additif = !!(mods && (mods.shiftKey || mods.ctrlKey || mods.metaKey));
      if (!t || t.i == null) {
        // Dans le vide AVEC un modificateur (ou le verrou « Sélection multiple ») : RECTANGLE de
        // sélection, si l'éditeur sait le dessiner (`rectangle`, `S.rectSel`). Sinon, sans
        // modificateur, le clic dans le vide désélectionne.
        if (additif && o.rectangle) {
          S.geste = { type: "rect", depart: pos, base: S.sel.slice(), orig: [] };
          S.rectSel = { x: pos.x, y: pos.y, w: 0, h: 0 };
          return true;
        }
        if (!additif) S.vider();
        return false;
      }
      const L = objs();
      if (t.poignee && S.sel.includes(t.i) && t.i === S.primaire) {
        const grp = S.sel.filter((k) => L[k]);
        S.geste = { type: "redim", i: t.i, depart: pos, orig: grp.map((k) => ({ k, ...L[k] })) };
        return true;
      }
      S.selectionner(t.i, mods);
      if (!S.sel.includes(t.i)) return false;              // Maj+clic qui RETIRE : pas de glisser
      S.geste = { type: "deplacer", i: t.i, depart: pos,
                  orig: S.sel.filter((k) => L[k]).map((k) => ({ k, ...L[k] })) };
      return true;
    };

    S.glisse = function (pos, mods) {
      const g = S.geste;
      if (!g) return false;
      const dx = pos.x - g.depart.x, dy = pos.y - g.depart.y;
      if (g.type === "rect") {
        const r = { x: Math.min(g.depart.x, pos.x), y: Math.min(g.depart.y, pos.y), w: Math.abs(dx), h: Math.abs(dy) };
        const dedans = [];
        objs().forEach((z, i) => {
          if (z && z.x < r.x + r.w && z.x + z.w > r.x && z.y < r.y + r.h && z.y + z.h > r.y) dedans.push(i);
        });
        S.sel = g.base.concat(dedans.filter((i) => !g.base.includes(i)));
        if (S.primaire == null || !S.sel.includes(S.primaire)) S.primaire = S.sel.length ? S.sel[S.sel.length - 1] : null;
        S.rectSel = r;
        if (a.rendu) a.rendu();
        return true;
      }
      // SEUIL de départ (`seuil`, unités de l'éditeur) : sous lui, un appui reste un CLIC — un doigt
      // qui tremble ne déplace rien (et ne marque rien comme modifié).
      if (!g.parti) {
        const sl = typeof o.seuil === "function" ? o.seuil() : (o.seuil || 0);
        if (Math.hypot(dx, dy) < sl) return false;
        g.parti = true;
      }
      // Alt sur la poignée d'un objet SEUL : il grandit autour de son CENTRE (arbitrage du 30/10,
      // convention des logiciels de dessin) ; sans Alt, le coin opposé reste fixe. L'aimant ne
      // joue pas autour du centre : il raisonne depuis un coin fixe.
      const centre = (!!(mods && mods.altKey) || S.centre) && g.type === "redim" && g.orig.length < 2;
      const c = cadre();
      S.guides = [];
      if (g.type === "deplacer") {
        const p = g.orig.find((z) => z.k === g.i);
        let nx = p.x + dx, ny = p.y + dy;
        if (aimantOn()) {
          const m = layout.aimanter(cibles(), g.orig.map((z) => z.k), nx, ny, p.w, p.h, tol(),
                                    { cadre: c, arrondi: (v) => v });
          nx = m.x; ny = m.y; S.guides = m.guides;
        }
        const d = deltaGroupe(g.orig, nx - p.x, ny - p.y, c);
        g.orig.forEach((z) => ecrire(z.k, { x: arr(z.x + d.dx), y: arr(z.y + d.dy), w: z.w, h: z.h }));
      } else {
        const p = g.orig.find((z) => z.k === g.i);
        if (g.orig.length >= 2) {
          // Poignée d'une sélection multiple : échelle depuis le coin du groupe (layout).
          const rects = [];
          g.orig.forEach((z) => { rects[z.k] = versLocal(z, c); });
          // `arrondiGroupe` : le mur arrondit les BORDS au pixel PAIR (chroma) — un arrondi au
          // pixel suivi d'un pair à l'écriture rouvrirait un interstice entre tuiles collées.
          const out = layout.redimensionnerGroupe(rects, g.orig.map((z) => z.k), g.i, p.w + dx,
                                                  { cadre: c, arrondi: o.arrondiGroupe || arr, min: min || 1 });
          if (out) g.orig.forEach((z) => ecrire(z.k, versSurface(out[z.k], c)));
        } else if (centre) {
          const k = p.format > 0 ? p.format : 0;
          let w = p.w + 2 * (k ? Math.max(dx, dy * k) : dx);
          let h = k ? w / k : p.h + 2 * dy;
          const mn = p.min || min;
          if (w < mn) { w = mn; if (k) h = w / k; }
          if (h < mn) { h = mn; if (k) w = h * k; }
          if (p.maxW && w > p.maxW) { w = p.maxW; if (k) h = w / k; }
          if (p.maxH && h > p.maxH) { h = p.maxH; if (k) w = h * k; }
          const cx = p.x + p.w / 2, cy = p.y + p.h / 2;
          ecrire(g.i, { x: arr(cx - w / 2), y: arr(cy - h / 2), w: arr(w), h: arr(h) });
        } else if (p.format > 0) {
          // Format verrouillé : la poignée suit le plus grand des deux déplacements.
          let larg = p.w + Math.max(dx, dy * p.format);
          if (p.maxW) larg = Math.min(larg, p.maxW);
          if (p.maxH) larg = Math.min(larg, p.maxH * p.format);
          const lim = o.horsCadre ? { w: Infinity, h: Infinity } : { w: c.x + c.w, h: c.y + c.h };
          let r = layout.redimensionnerAuFormat({ x: p.x, y: p.y, w: p.w, h: p.h }, larg, p.format,
                                                { cadre: lim, arrondi: arr, min: p.min || min });
          if (aimantOn()) {
            const m = layout.aimanterRedim(cibles(), [g.i], p.x, p.y, r.w, r.h, tol(),
                                           { cadre: c, ratio: p.format, min: p.min || min, arrondi: arr });
            if (!(p.maxW && m.w > p.maxW) && !(p.maxH && m.h > p.maxH)) { r = { w: m.w, h: m.h }; S.guides = m.guides; }
          }
          ecrire(g.i, { x: p.x, y: p.y, w: r.w, h: r.h });
        } else {
          // Plancher par axe s'il est donné (`minW`/`minH` : une frise est plus large que haute).
          const mw = p.minW || p.min || min, mh = p.minH || p.min || min;
          let w = Math.max(mw, p.w + dx), h = Math.max(mh, p.h + dy);
          if (!o.horsCadre) { w = Math.min(w, c.x + c.w - p.x); h = Math.min(h, c.y + c.h - p.y); }
          if (aimantOn()) {
            const m = layout.aimanterRedim(cibles(), [g.i], p.x, p.y, w, h, tol(), { cadre: c, min: 0 });
            w = Math.max(mw, m.w); h = Math.max(mh, m.h); S.guides = m.guides;
          }
          if (p.maxW) w = Math.min(w, p.maxW);
          if (p.maxH) h = Math.min(h, p.maxH);
          ecrire(g.i, { x: p.x, y: p.y, w: arr(w), h: arr(h) });
        }
      }
      if (a.rendu) a.rendu();
      return true;
    };

    S.relache = function () {
      const g = S.geste;
      S.geste = null; S.guides = [];
      if (!g) return false;
      if (g.type === "rect") { S.rectSel = null; if (a.rendu) a.rendu(); return false; }
      const L = objs();
      const bouge = g.orig.filter((z) => L[z.k] && ["x", "y", "w", "h"].some((q) => L[z.k][q] !== z[q])).map((z) => z.k);
      if (a.rendu) a.rendu();
      if (bouge.length && a.valider) a.valider(bouge);
      return bouge.length > 0;
    };

    /* Flèches : la sélection entière, d'un pas (Maj = grand pas), bornée en GROUPE. */
    S.fleche = function (sx, sy, maj) {
      const L = objs(), c = cadre();
      const membres = S.sel.filter((k) => L[k]);
      if (!membres.length) return false;
      const k = maj ? pas.maj : pas.fleche;
      const d = deltaGroupe(membres.map((m) => L[m]), sx * k, sy * k, c);
      if (!d.dx && !d.dy) return false;
      membres.forEach((m) => ecrire(m, { x: arr(L[m].x + d.dx), y: arr(L[m].y + d.dy), w: L[m].w, h: L[m].h }));
      if (a.rendu) a.rendu();
      if (a.valider) a.valider(membres);
      return true;
    };

    /* Outils de la barre. Rendent la liste des indices modifiés (vide = rien à faire), ou null
       quand la sélection est insuffisante (l'éditeur le DIT à l'exploitant). */
    S.outil = function (nom, arg) {
      const L = objs(), c = cadre();
      const membres = S.sel.filter((k) => L[k]);
      if (!membres.length) return null;
      const loc = L.map((r) => versLocal(r, c));
      const cad = { cadre: { w: c.w, h: c.h } };
      let out = null, cles = ["x", "y"];
      if (nom === "aligner") {
        // Toujours `libre` : le bornage de `layout` est NORMALISÉ (1×1) ; celui de la surface se fait
        // dans le cadre de l'éditeur. Un objet seul s'aligne sur ce cadre (référence fournie).
        // Un objet seul s'aligne sur le cadre — ou sur la référence que l'éditeur fournit
        // (`refSeul()`, en unités de surface : l'image d'un PiP, par exemple).
        const rs = o.refSeul && o.refSeul();
        out = layout.aligner(loc, membres, S.primaire, arg,
                             rs ? versLocal(rs, c) : { x: 0, y: 0, w: c.w, h: c.h }, { libre: true });
        if (!o.horsCadre) out = out.map((r, i) => (membres.includes(i) && r ? _borne(r, c) : r));
      } else if (nom === "egaliser") {
        if (membres.length < 2) return null;
        out = layout.egaliser(loc, membres, S.primaire, arg, { libre: true, cadre: { w: c.w, h: c.h } });
        // Format verrouillé : « même largeur » donne aussi la hauteur, et inversement.
        out = out.map((r, i) => {
          if (!r || !membres.includes(i) || i === S.primaire) return r;
          const f = L[i].format;
          if (f > 0) { if (arg === "h") r.w = r.h * f; else r.h = r.w / f; }
          return o.horsCadre ? r : _borne(r, c);
        });
        cles = ["x", "y", "w", "h"];
      } else if (nom === "distribuer" || nom === "espaces") {
        if (membres.length < 3) return null;
        out = (nom === "distribuer" ? layout.distribuer : layout.distribuerEspaces)(loc, membres, arg, { arrondi: (v) => v });
      } else if (nom === "remplir") {
        out = layout.remplir(loc, membres, arg, Object.assign({}, cad, {
          format: (i) => (L[i] && L[i].format) || 0, dim: (v) => v, pos: (v) => v }));
        cles = ["x", "y", "w", "h"];
      } else return null;
      if (!out) return null;
      const touches = [];
      membres.forEach((i) => {
        const r = versSurface(out[i], c);
        if (!r || cles.every((q) => r[q] === L[i][q])) return;
        const n = { x: L[i].x, y: L[i].y, w: L[i].w, h: L[i].h };
        cles.forEach((q) => { n[q] = arr(r[q]); });
        ecrire(i, n);
        touches.push(i);
      });
      if (a.rendu) a.rendu();
      if (touches.length && a.valider) a.valider(touches);
      return touches;
    };

    /* ═══ HISTORIQUE ET CLAVIER — les mêmes dans tous les éditeurs (arbitrage du 30/10) ═══
       Annuler/rétablir, copier/couper/coller/dupliquer, tout sélectionner, supprimer, flèches,
       Échap, enregistrer, aide : la surface relie les raccourcis de la base (partie édition) à
       l'éditeur, qui ne fournit que CE QUI LUI EST PROPRE :
         etat() / restaurer(e)      → instantané sérialisable du modèle, et sa remise en place ;
         fragment() → donnée | null → ce que « copier » emporte de la sélection ;
         poser(donnee, decalage)    → colle (sélectionne ce qui est posé), rend true si posé ;
         supprimer() → bool, tout() → sélectionne tout, enregistrer(), aide(), dire(msg) ;
         kind                       → type du presse-papiers (on ne colle que ce qu'on sait lire) ;
         lectureSeule()             → bloque les gestes qui écrivent.
       L'historique s'empile par `S.memoriser()`, que l'éditeur appelle à chaque PERSISTANCE (un
       instantané identique au précédent n'est pas empilé). Rend la fonction qui détache. */
    S.historique = null;
    /* Les BOUTONS d'une barre (Annuler, Couper, Coller…) passent par les mêmes actions que le
       clavier : `S.agir("undo")`. Rend false si l'action n'existe pas (clavier pas encore lié). */
    S.agir = function (nom, sc) {
      const f = S._actions && S._actions[nom];
      if (!f) return false;
      f(sc || {}, null);
      return true;
    };
    S.memoriser = function () { if (S.historique && S._etat) S.historique.commit(S._etat()); };
    S.historiqueZero = function () { if (S.historique && S._etat) S.historique.reset(S._etat()); };
    S.clavier = function (k) {
      const E = edition;                                    // partie édition (historique, raccourcis)
      S._etat = k.etat;
      S.historique = E.history(k.profondeur || 100);
      S.historiqueZero();
      const dire = (m) => { if (k.dire) k.dire(m); };
      const ecrit = (f) => (sc, e) => {
        if (k.lectureSeule && k.lectureSeule()) { dire("lecture seule"); return; }
        f(sc, e);
      };
      const coller = (donnee) => {
        if (!donnee || !k.poser) { dire("rien"); return; }
        if (k.poser(donnee, k.decalage || 0)) { if (a.rendu) a.rendu(); S.memoriser(); if (k.apres) k.apres(); }
      };
      const on = {
        undo: ecrit(() => { const st = S.historique.undo(); if (st === undefined) return;
                            k.restaurer(st); S.vider(); if (a.rendu) a.rendu(); if (k.apres) k.apres(); }),
        redo: ecrit(() => { const st = S.historique.redo(); if (st === undefined) return;
                            k.restaurer(st); S.vider(); if (a.rendu) a.rendu(); if (k.apres) k.apres(); }),
        copy: () => { const f = k.fragment && k.fragment(); if (f) { E.clipboard.put(k.kind, f); dire("copie"); } },
        cut: ecrit(() => {
          const f = k.fragment && k.fragment(); if (!f) return;
          E.clipboard.put(k.kind, f);
          if (k.supprimer && k.supprimer()) { S.vider(); if (a.rendu) a.rendu(); S.memoriser(); if (k.apres) k.apres(); }
        }),
        paste: ecrit(() => { const c = E.clipboard.get(k.kind); coller(c && c.data); }),
        duplicate: ecrit(() => { const f = k.fragment && k.fragment(); if (f) coller(f); }),
        selectAll: () => { if (k.tout) { k.tout(); if (a.rendu) a.rendu(); } },
        delete: ecrit(() => { if (k.supprimer && k.supprimer()) { S.vider(); if (a.rendu) a.rendu(); S.memoriser(); if (k.apres) k.apres(); } }),
        nudge: ecrit((sc) => { S.fleche(Math.sign(sc.dx), Math.sign(sc.dy), Math.abs(sc.dx) + Math.abs(sc.dy) > 1); }),
        escape: () => { S.vider(); if (a.rendu) a.rendu(); if (k.apres) k.apres(); },
      };
      if (k.enregistrer) on.save = ecrit(() => k.enregistrer());
      on.help = () => (k.aide ? k.aide() : E.aideStandard(k.titreAide));
      S._actions = on;
      return E.bind({ active: k.actif, on });
    };

    function _borne(r, c) {
      const w = Math.min(r.w, c.w), h = Math.min(r.h, c.h);
      return { ...r, w, h, x: Math.max(0, Math.min(c.w - w, r.x)), y: Math.max(0, Math.min(c.h - h, r.y)) };
    }
    return S;
  };
})();
// END:surface

window.BobiEditor = Object.assign({}, edition, { layout, surface });
})();
