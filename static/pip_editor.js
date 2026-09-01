// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Éditeur de modèles de PiP (Réglages → PiP) : compose librement les composants d'une cellule
// de multiview (vidéo, UMD, tally, VU-mètres, ANC, horloge, texte, format) et sauvegarde le
// résultat en bibliothèque (/api/pip_templates). Géométrie NORMALISÉE 0..1 relative à la cellule.
// PRÉSENTATION : réutilise la feuille de style et les classes du composer multiview
// (/api/plugins/multiview/ui/extra_css : .mw-editor, .mw-toolbar, .tool-btn, steppers…) pour
// une parité visuelle totale — mêmes outils (aligner/taille/distribuer), même snap magnétique.
// Le rendu réel vit dans plugins/multiview/script.py ; l'affectation par tuile dans le composer.
(function () {
'use strict';

const _t = (k, fb) => {
    const v = (typeof window.t === 'function') ? window.t(k) : null;
    return (v && v !== k) ? v : fb;
};
const $ = id => document.getElementById(id);
const esc = s => String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Réplique JS de _meter_fit_dims (plugins/multiview/script.py) : en mode width_mode='fit',
// SEULES les barres de canaux s'élargissent pour remplir rw — la zone de graduations (tick,
// dB/PPM + repères) et l'espacement inter-canaux (gap) restent FIXES (16 px / 1 px), sinon
// l'échelle se déforme. Repli sur la largeur intrinsèque (bar=5) si rw est trop étroit pour une
// barre lisible (≥ 2 px) — jamais de barre à 0 px. Renvoie aussi `mw` (largeur réellement
// dessinée, ≤ rw : reste éventuel non couvert à droite, comme l'arrondi entier des barres).
function meterFitDims(nChannels, rw) {
    const n = Math.max(1, nChannels);
    const MIN_BAR = 2;
    const tick = 16, gap = 1;
    const availBars = rw - tick - (n - 1) * gap;
    let bar = Math.floor(availBars / n);
    if (bar < MIN_BAR) bar = 5;   // repli intrinsèque : mêmes dims que le mode 'auto'
    const mw = tick + n * bar + (n - 1) * gap;
    return { tick, bar, gap, mw };
}

const PIP_TYPES = ['video', 'umd', 'tally', 'meters', 'anc', 'clock', 'text', 'format',
                   'video_history', 'audio_history'];
const TYPE_LABELS = () => ({
    video:  _t('settings.pip.c_video', 'Vidéo'),
    umd:    _t('settings.pip.c_umd', 'UMD'),
    tally:  _t('settings.pip.c_tally', 'Tally'),
    meters: _t('settings.pip.c_meters', 'Audio'),
    anc:    _t('settings.pip.c_anc', 'Ancillary'),
    clock:  _t('settings.pip.c_clock', 'Horloge'),
    text:   _t('settings.pip.c_text', 'Texte'),
    format: _t('settings.pip.c_format', 'Format'),
    video_history: _t('settings.pip.c_vhist', 'Historique vidéo'),
    audio_history: _t('settings.pip.c_ahist', 'Historique audio'),
});

const NEW_COMP = {
    // Vidéo : TOUJOURS 16:9 dans l'éditeur — en coordonnées normalisées sur une cellule 16:9,
    // 16:9 ⇔ w == h. La contrainte est maintenue par _enforceVideoRatio (drag, champs, outils).
    video:  { x: 0, y: 0, w: 1, h: 1, fit: 'fill', border: 'none', border_w: 3,
              border_color: '#ffffff' },
    umd:    { x: 0.15, y: 0.86, w: 0.7, h: 0.12, text_source: 'name', text: '',
              tally_bg: true, tally_text: false, font_size: 0, align: 'center',
              color: '#ffffff', bg_color: '#000000', bg_opacity: 75 },
    tally:  { x: 0.02, y: 0.87, w: 0.06, h: 0.1, shape: 'lamp', slot: 'dominant', thickness: 4 },
    meters: { x: 0.9, y: 0, w: 0.1, h: 0.86, channels: 2, ch_start: 1, scale: 'dbfs',
              opacity: 100, align: 'right', width_mode: 'auto',
              graduations: 'auto', grad_side: 'left' },
    anc:    { x: 0, y: 0.79, w: 1, h: 0.07, anc_types: true, anc_tc: true, anc_cc: false,
              anc_afd: false, anc_st352: false, anc_scte: false, anc_crc: true,
              anc_opacity: 60, font_size: 0, align: 'left' },
    clock:  { x: 0.3, y: 0.05, w: 0.4, h: 0.12, clock_source: 'ptp', tz: '',
              show_hh: true, show_mm: true, show_ss: true, show_ff: false, offset_ms: 0,
              font_size: 0, align: 'center', color: '#ffffff', bg_color: '#000000', bg_opacity: 60 },
    text:   { x: 0.25, y: 0.4, w: 0.5, h: 0.15, text: 'TEXTE', font_size: 0, align: 'center',
              color: '#ffffff', bg_color: '', bg_opacity: 100 },
    format: { x: 0.55, y: 0.02, w: 0.42, h: 0.08, font_size: 0, align: 'center',
              color: '#d2d4da', bg_color: '#000000', bg_opacity: 65 },
    // Frises d'historique (multiview 0.37.0) : la source est celle de la FENÊTRE (vidéo pour
    // video_history, audio pour audio_history) — rien à câbler dans le modèle.
    video_history: { x: 0.02, y: 0.66, w: 0.96, h: 0.16, duration: 30, opacity: 100, events: true },
    audio_history: { x: 0.02, y: 0.84, w: 0.96, h: 0.14, duration: 30, opacity: 100,
                     channels: 2, ch_start: 1 },
};

// Spécification des champs du panneau propriétés, par type.

// ─── Sélecteur de FUSEAU HORAIRE ───────────────────────────────────────────────────────────
// Liste servie par /api/timezones = la tzdata RÉELLEMENT installée, groupée par région. Jamais
// une liste codée en dur : proposer un fuseau absent du système ferait afficher l'heure du mur
// sans aucun signal. Chargée une seule fois puis mémorisée.
let _tzGroups = null, _tzLoading = null;
function _tzLoad() {
    if (_tzGroups) return Promise.resolve(_tzGroups);
    if (_tzLoading) return _tzLoading;
    _tzLoading = fetch('/api/timezones')
        .then(r => (r.ok ? r.json() : { groups: [] }))
        .then(j => { _tzGroups = j.groups || []; return _tzGroups; })
        .catch(() => []);
    return _tzLoading;
}
function fillTzSelect(el, val) {
    if (!el) return;
    _tzLoad().then(groups => {
        if (el.dataset.filled !== '1') {
            el.innerHTML = '';
            el.appendChild(new Option(_t('settings.pip.clk_tz_wall', 'Fuseau du mur'), ''));
            groups.forEach(g => {
                const og = document.createElement('optgroup');
                og.label = g.region;
                (g.zones || []).forEach(z => og.appendChild(new Option(z, z)));
                el.appendChild(og);
            });
            el.dataset.filled = '1';
        }
        el.value = val || '';
    });
}


// Variables insérables : servies par /api/text-variables — DÉFINIES UNE SEULE FOIS côté
// orchestrateur (app/routes/pages.py) et partagées avec le composeur de mur. Dupliquer la liste
// dans chaque éditeur l'aurait fait diverger au premier ajout.
let _varCat = null, _varLoading = null;
function loadTextVars() {
    if (_varCat) return Promise.resolve(_varCat);
    if (_varLoading) return _varLoading;
    _varLoading = fetch('/api/text-variables')
        .then(r => (r.ok ? r.json() : { system: [], source: [] }))
        .then(j => { _varCat = j; return _varCat; })
        .catch(() => ({ system: [], source: [] }));
    return _varLoading;
}

const F = (k, type, label, extra) => Object.assign({ k, type, label }, extra || {});

// Champ POLICE (composants qui rendent du texte) : le <select> est rempli à la volée par
// window.BobiFonts (catalogue /api/fonts = polices de l'image runtime + bibliothèque
// téléversée, clés `lib:<sha16>`) — cf. refreshProps. La clé est stockée telle quelle dans le
// modèle ; app/fonts.py l'embarque en base64 au déploiement et le script du mur la
// matérialise. `options` reste vide : le remplissage (avec optgroups) est fait par fillSelect.
const F_FONT = () => F('font', 'select', _t('settings.pip.f_font', 'Police'),
                       { font: true, options: [] });
const FIELD_SPECS = () => ({
    video: [
        F('fit', 'select', _t('settings.pip.f_fit', 'Ajustement'), { options: [
            ['fill', _t('settings.pip.fit_fill', 'Remplir (étirer)')],
            ['contain', _t('settings.pip.fit_contain', 'Contenir (ratio source)')]] }),
        // Styles de CADRE (ex-frame_style global de mur, migré dans le modèle 0.33.0) : le
        // cadre épouse le rectangle IMAGE réel (letterbox compris), dessiné vers l'intérieur.
        F('border', 'select', _t('settings.pip.f_border', 'Cadre'), { options: [
            ['none', _t('settings.pip.border_none', 'Aucun')],
            ['fixed', _t('settings.pip.border_fixed', 'Fixe (couleur)')],
            ['tally', _t('settings.pip.border_tally', 'Piloté par le tally')],
            ['classic', _t('settings.pip.border_classic', 'Cadre fin neutre')],
            ['stylized', _t('settings.pip.border_stylized', 'Moniteur (bezel)')],
            ['viewfinder', _t('settings.pip.border_viewfinder', 'Viseur (équerres)')],
            ['flat', _t('settings.pip.border_flat', 'Soulignement bas')]] }),
        F('border_w', 'number', _t('settings.pip.f_border_w', 'Épaisseur (px)'), { min: 1, max: 24 }),
        F('border_color', 'color', _t('settings.pip.f_border_color', 'Couleur bordure')),
    ],
    umd: [
        F('text_source', 'select', _t('settings.pip.f_text_source', 'Texte'), { options: [
            ['name', _t('settings.pip.ts_name', 'Nom de la source')],
            ['tsl', _t('settings.pip.ts_tsl', 'Protocole (TSL)')],
            ['fixed', _t('settings.pip.ts_fixed', 'Texte fixe')]] }),
        F('_vars', 'vars', _t('settings.pip.f_vars', 'Variable')),
        F('text', 'textarea', _t('settings.pip.f_fixed_text', 'Texte fixe')),
        F('tally_bg', 'check', _t('settings.pip.f_tally_bg', 'Fond teinté par le tally')),
        F('tally_text', 'check', _t('settings.pip.f_tally_text', 'Texte coloré par le tally')),
        F_FONT(),
        F('font_size', 'number', _t('settings.pip.f_font_size', 'Taille texte (0 = auto)'), { min: 0, max: 200 }),
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        F('color', 'color', _t('settings.pip.f_color', 'Couleur texte')),
        F('bg_color', 'color', _t('settings.pip.f_bg_color', 'Couleur fond'), { empty: true }),
        F('bg_opacity', 'number', _t('settings.pip.f_bg_opacity', 'Opacité fond'), { min: 0, max: 100 }),
        F('anchor_x', 'select', _t('settings.pip.f_anchor_x', 'Ancrage horizontal'), { options: [
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")],
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')]] }),
        F('anchor_y', 'select', _t('settings.pip.f_anchor_y', 'Ancrage vertical'), { options: [
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')],
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")]] }),
    ],
    tally: [
        F('shape', 'select', _t('settings.pip.f_shape', 'Forme'), { options: [
            ['lamp', _t('settings.pip.shape_lamp', 'Lampe (pastille)')],
            ['bar', _t('settings.pip.shape_bar', 'Pavé plein')],
            ['border', _t('settings.pip.shape_border', 'Cadre')]] }),
        F('slot', 'select', _t('settings.pip.f_slot', 'Signal'), { options: [
            ['dominant', _t('settings.pip.slot_dominant', 'Dominant (R+V)')],
            ['L', _t('settings.pip.slot_l', 'Gauche (rouge)')],
            ['R', _t('settings.pip.slot_r', 'Droite (vert)')]] }),
        F('thickness', 'number', _t('settings.pip.f_thickness', 'Épaisseur cadre (px)'), { min: 2, max: 24 }),
        F('anchor_x', 'select', _t('settings.pip.f_anchor_x', 'Ancrage horizontal'), { options: [
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")],
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')]] }),
        F('anchor_y', 'select', _t('settings.pip.f_anchor_y', 'Ancrage vertical'), { options: [
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')],
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")]] }),
    ],
    meters: [
        F('channels', 'select', _t('settings.pip.f_channels', 'Canaux'), { options: [
            ['1', '1'], ['2', '2'], ['4', '4'], ['6', '6'], ['8', '8']], num: true }),
        // Affectation : premier canal dans un espace de 16 (2 flux de 8) — ex. « 1-2 à gauche,
        // 3-4 à droite » = deux composants meters, ch_start 1 et 3.
        F('ch_start', 'select', _t('settings.pip.f_ch_start', 'Premier canal'), {
            options: Array.from({ length: 16 }, (_, i) => [String(i + 1), String(i + 1)]),
            num: true, help: _t('settings.pip.f_ch_start_help',
                'Canaux 1-8 = flux audio de la source ; 9-16 = 2ᵉ flux (2 flux de 8).') }),
        F('scale', 'select', _t('settings.pip.f_scale', 'Graduation'), { options: [
            ['dbfs', 'dBFS'], ['ppm', 'EBU PPM']] }),
        F('opacity', 'number', _t('settings.pip.f_opacity', 'Opacité'), { min: 10, max: 100 }),
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        // Graduations : « Auto » dégrade selon la place réellement allouée (chiffres → traits
        // seuls → rien), les trois autres crans s'imposent quelle que soit la taille.
        F('graduations', 'select', _t('settings.pip.f_grad', 'Graduations'), { options: [
            ['auto',  _t('settings.pip.grad_auto',  'Auto (selon la place)')],
            ['full',  _t('settings.pip.grad_full',  'Chiffres + traits')],
            ['marks', _t('settings.pip.grad_marks', 'Traits seuls')],
            ['none',  _t('settings.pip.grad_none',  'Aucune')]] }),
        F('grad_side', 'select', _t('settings.pip.f_grad_side', 'Position des graduations'), { options: [
            ['left',   _t('settings.pip.left',  'Gauche')],
            ['right',  _t('settings.pip.right', 'Droite')],
            ['inside', _t('settings.pip.grad_inside', 'Sur les barres (aucune colonne)')]] }),
        F('width_mode', 'select', _t('settings.pip.f_width_mode', 'Largeur'), { options: [
            ['auto', _t('settings.pip.width_mode_auto', 'Fixe')],
            ['fit', _t('settings.pip.width_mode_fit', 'Suit la largeur du composant')]],
            help: _t('settings.pip.f_width_mode_help',
                "« Suit la largeur » élargit les barres de canaux pour remplir le composant "
                + "(la zone de graduations garde sa largeur fixe) ; l'alignement devient alors "
                + "sans effet.") }),
    ],
    anc: [
        F('anc_types', 'check', _t('settings.pip.f_anc_types', 'Types présents')),
        F('anc_tc', 'check', _t('settings.pip.f_anc_tc', 'Timecode')),
        F('anc_cc', 'check', _t('settings.pip.f_anc_cc', 'Sous-titres')),
        F('anc_afd', 'check', 'AFD'),
        F('anc_st352', 'check', _t('settings.pip.f_anc_st352', 'Format signalé (ST 352)')),
        F('anc_scte', 'check', 'SCTE-104'),
        F('anc_crc', 'check', _t('settings.pip.f_anc_crc', 'Erreurs checksum')),
        F('anc_opacity', 'number', _t('settings.pip.f_bg_opacity', 'Opacité fond'), { min: 0, max: 100 }),
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        F_FONT(),
        F('font_size', 'number', _t('settings.pip.f_font_size', 'Taille texte (0 = auto)'), { min: 0, max: 200 }),
    ],
    clock: [
        F('clock_source', 'select', _t('settings.pip.f_clock_source', 'Source'), { options: [
            ['ptp', _t('settings.pip.clk_ptp', 'PTP (heure du jour)')],
            ['anc', _t('settings.pip.clk_anc', 'ANC (timecode de la source)')]] }),
        // Fuseau PROPRE à cette horloge — vide = fuseau du mur (réglage système). Les options
        // sont posées à la volée depuis /api/timezones (tzdata réelle), cf. fillTzSelect.
        F('tz', 'tz', _t('settings.pip.f_clock_tz', 'Fuseau horaire')),
        F('show_hh', 'check', 'HH'), F('show_mm', 'check', 'MM'),
        F('show_ss', 'check', 'SS'), F('show_ff', 'check', _t('settings.pip.f_ff', 'Images (II)')),
        F('offset_ms', 'number', _t('settings.pip.f_offset', 'Offset (ms)'), { min: -86400000, max: 86400000 }),
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        F_FONT(),
        F('font_size', 'number', _t('settings.pip.f_font_size', 'Taille texte (0 = auto)'), { min: 0, max: 200 }),
        F('color', 'color', _t('settings.pip.f_color', 'Couleur texte')),
        F('bg_color', 'color', _t('settings.pip.f_bg_color', 'Couleur fond'), { empty: true }),
        F('bg_opacity', 'number', _t('settings.pip.f_bg_opacity', 'Opacité fond'), { min: 0, max: 100 }),
        F('anchor_x', 'select', _t('settings.pip.f_anchor_x', 'Ancrage horizontal'), { options: [
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")],
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')]] }),
        F('anchor_y', 'select', _t('settings.pip.f_anchor_y', 'Ancrage vertical'), { options: [
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')],
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")]] }),
    ],
    text: [
        F('_vars', 'vars', _t('settings.pip.f_vars', 'Variable')),
        F('text', 'textarea', _t('settings.pip.f_text', 'Texte')),
        F_FONT(),
        F('font_size', 'number', _t('settings.pip.f_font_size', 'Taille texte (0 = auto)'), { min: 0, max: 200 }),
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        F('color', 'color', _t('settings.pip.f_color', 'Couleur texte')),
        F('bg_color', 'color', _t('settings.pip.f_bg_color', 'Couleur fond'), { empty: true }),
        F('bg_opacity', 'number', _t('settings.pip.f_bg_opacity', 'Opacité fond'), { min: 0, max: 100 }),
        F('anchor_x', 'select', _t('settings.pip.f_anchor_x', 'Ancrage horizontal'), { options: [
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")],
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')]] }),
        F('anchor_y', 'select', _t('settings.pip.f_anchor_y', 'Ancrage vertical'), { options: [
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')],
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")]] }),
    ],
    format: [
        F('align', 'select', _t('settings.pip.f_align', 'Alignement'), { options: [
            ['left', _t('settings.pip.left', 'Gauche')], ['center', _t('settings.pip.center', 'Centre')],
            ['right', _t('settings.pip.right', 'Droite')]] }),
        F_FONT(),
        F('font_size', 'number', _t('settings.pip.f_font_size', 'Taille texte (0 = auto)'), { min: 0, max: 200 }),
        F('color', 'color', _t('settings.pip.f_color', 'Couleur texte')),
        F('bg_color', 'color', _t('settings.pip.f_bg_color', 'Couleur fond'), { empty: true }),
        F('bg_opacity', 'number', _t('settings.pip.f_bg_opacity', 'Opacité fond'), { min: 0, max: 100 }),
        F('anchor_x', 'select', _t('settings.pip.f_anchor_x', 'Ancrage horizontal'), { options: [
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")],
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')]] }),
        F('anchor_y', 'select', _t('settings.pip.f_anchor_y', 'Ancrage vertical'), { options: [
            ['cell',  _t('settings.pip.anchor_cell',  'Bords de la cellule')],
            ['image', _t('settings.pip.anchor_image', "Bords de l'image")]] }),
    ],
    video_history: [
        F('duration', 'select', _t('settings.pip.f_duration', 'Durée'), { options: [
            ['10', '10 s'], ['30', '30 s'], ['60', '60 s'], ['120', '120 s']], num: true }),
        F('events', 'check', _t('settings.pip.f_events', "Ruban d'événements"), {
            help: _t('settings.pip.f_events_help',
                "Gel (ambre), noir (indigo), perte de signal (rouge) sous la bande. La vignette "
                + "capturée À L'INSTANT de l'événement est épinglée dans sa case.") }),
        F('opacity', 'number', _t('settings.pip.f_opacity', 'Opacité'), { min: 10, max: 100 }),
    ],
    audio_history: [
        F('duration', 'select', _t('settings.pip.f_duration', 'Durée'), { options: [
            ['10', '10 s'], ['30', '30 s'], ['60', '60 s'], ['120', '120 s']], num: true }),
        F('channels', 'select', _t('settings.pip.f_channels', 'Canaux'), { options: [
            ['1', '1'], ['2', '2'], ['4', '4'], ['8', '8']], num: true }),
        F('ch_start', 'select', _t('settings.pip.f_ch_start', 'Premier canal'), {
            options: Array.from({ length: 16 }, (_, i) => [String(i + 1), String(i + 1)]),
            num: true, help: _t('settings.pip.f_ch_start_help',
                'Canaux 1-8 = flux audio de la source ; 9-16 = 2ᵉ flux (2 flux de 8).') }),
        F('opacity', 'number', _t('settings.pip.f_opacity', 'Opacité'), { min: 10, max: 100 }),
    ],
});
const WHEN_OPTIONS = () => [
    ['always', _t('settings.pip.when_always', 'Toujours')],
    ['tally_red', _t('settings.pip.when_red', 'Tally rouge')],
    ['tally_green', _t('settings.pip.when_green', 'Tally vert')],
    ['tally_any', _t('settings.pip.when_any', 'Tally actif')],
    ['tally_off', _t('settings.pip.when_off', 'Tally éteint')],
    ['no_signal', _t('settings.pip.when_nosignal', 'Pas de signal')],
    ['freeze', _t('settings.pip.when_freeze', 'Image figée')],
    ['signal_ok', _t('settings.pip.when_ok', 'Signal présent')],
];

// ── état ──
let lib = [];                 // bibliothèque (/api/pip_templates)
let cur = null;               // modèle en édition {id?, name, builtin?, config:{components:[]}}
let selIdxs = [];             // multi-sélection (Maj+clic) ; référence = DERNIÈRE sélectionnée
let selIdx = -1;              // composant primaire (= dernier de selIdxs, -1 si vide)
let cellW = 640;              // largeur de cellule SIMULÉE (aperçu min_w)
let simTally = 'off';         // simulation tally de l'aperçu : off | red | green
let simSignal = '';           // simulation signal : '' | nosignal | freeze
let drag = null;              // {mode:'move'|'resize', idx, start, orig:[{gi,x,y}], ow, oh}
let snapEnabled = true;
let snapGuides = [];          // [{type:'v'|'h', pos}] en px canvas, dessinées pendant le drag
// Seuil de snap ∝ résolution du canvas (la tuile simulée change la résolution interne, pas la
// taille d'affichage) → distance d'accroche CONSTANTE à l'écran quelle que soit la simulation.
const SNAP_PX = () => Math.max(4, Math.round(cvs().width / 80));

const comps = () => (cur && cur.config && cur.config.components) || [];

// ── Format LIBRE du modèle (config.aspect = ratio L/H de la cellule cible) ──
// Absent/invalide = 16:9 (comportement historique). L'aspect pilote la RÉSOLUTION du canvas
// (applySimCell) et le couplage W/H du composant vidéo (_enforceVideoRatio) : la vidéo reste
// 16:9 en PIXELS quelle que soit la cellule → en normalisé, h = w × aspect / (16/9).
function tplAspect() {
    const a = cur && cur.config && parseFloat(cur.config.aspect);
    return (a && a > 0.1 && a < 10) ? a : 16 / 9;
}
const _videoK = () => tplAspect() / (16 / 9);   // h_norm = w_norm × k pour une vidéo 16:9

function parseAspect(str) {
    // "16:9" / "16/9" / "4:3" / "1.85" / "1920x1080" → ratio L/H, ou null si invalide.
    const s = String(str || '').trim().replace(',', '.');
    let m = s.match(/^(\d+(?:\.\d+)?)\s*[:\/xX×]\s*(\d+(?:\.\d+)?)$/);
    if (m) {
        const w = parseFloat(m[1]), h = parseFloat(m[2]);
        return (w > 0 && h > 0) ? w / h : null;
    }
    const v = parseFloat(s);
    return (v > 0.1 && v < 10) ? v : null;
}

function fmtAspect(a) {
    // Affiche « L:H » pour les ratios usuels, sinon la valeur décimale.
    const KNOWN = [[16, 9], [4, 3], [21, 9], [1, 1], [9, 16], [3, 4], [32, 9], [5, 4], [16, 10]];
    for (const [w, h] of KNOWN) {
        if (Math.abs(a - w / h) < 0.002) return w + ':' + h;
    }
    return String(Math.round(a * 1000) / 1000);
}

function _setSel(arr) {
    selIdxs = arr;
    selIdx = arr.length ? arr[arr.length - 1] : -1;
}

// Vidéo TOUJOURS 16:9 en PIXELS : sur un canevas d'aspect A, cela impose h = w × A/(16/9)
// en normalisé (A = 16:9 ⇒ w == h, l'historique). w est la dimension pilote.
function _enforceVideoRatio(c) {
    if (!c || c.type !== 'video') return;
    const k = _videoK();
    const wMax = Math.min(1 - (c.x || 0), (1 - (c.y || 0)) / k);
    const s = Math.max(0.02, Math.min(c.w || 1, wMax, 1));
    c.w = Math.round(s * 1000) / 1000;
    c.h = Math.round(s * k * 1000) / 1000;
}

function newTemplate() {
    cur = { id: null, name: '', builtin: false, tags: [],
            config: { components: [Object.assign({ id: 'video', type: 'video' }, NEW_COMP.video)] } };
    _setSel([0]);
    refreshAll();
}

function addComp(type) {
    if (!cur) newTemplate();
    if (type === 'video' && comps().some(c => c.type === 'video')) {
        flash(_t('settings.pip.flash_one_video', 'Un seul composant vidéo par modèle.'), 'warn');
        return;
    }
    const c = Object.assign({ id: type + Date.now().toString(36), type }, NEW_COMP[type]);
    _enforceVideoRatio(c);   // sur un modèle non-16:9, la vidéo par défaut suit le couplage W/H
    comps().push(c);
    _setSel([comps().length - 1]);
    refreshAll();
}

function removeComp() {
    if (!selIdxs.length) return;
    selIdxs.slice().sort((a, b) => b - a).forEach(i => comps().splice(i, 1));
    _setSel([]);
    refreshAll();
}

// ── Copier / coller les réglages d'un composant ──
// Repris À L'IDENTIQUE du composer multiview (plugins/multiview/multiview.js :
// copierReglagesFenetre/collerReglagesFenetre, control.html lignes ~100-106) : même geste, mêmes
// icônes, même sémantique — presse-papier interne (variable JS, pas le clipboard système),
// primaire = source, colle dans TOUTE la sélection courante, position (x/y) et identité (id)
// TOUJOURS exclues. Ici la liste blanche des clés copiées = les réglages du TYPE (FIELD_SPECS)
// + w/h (la taille), au lieu du COPY_FIELDS figé du composer (un seul type de fenêtre là-bas,
// plusieurs types de composants ici). Ne colle que sur des composants du MÊME TYPE que la
// source — sinon les clés n'ont aucun sens (un tally n'a pas de bg_color) ; jamais silencieux si
// des cibles sont d'un autre type (règle « pas de contrôle muet »).
let reglagesClipboard = null;   // {type, ...réglages copiés}

function copyCompSettings() {
    const c = comps()[selIdx];
    if (!cur || !c) {
        flash(_t('settings.pip.flash_select_copy', 'Sélectionnez un composant à copier.'), 'warn');
        return;
    }
    const keys = (FIELD_SPECS()[c.type] || []).map(f => f.k).concat(['w', 'h']);
    reglagesClipboard = { type: c.type };
    keys.forEach(k => { if (c[k] !== undefined) reglagesClipboard[k] = c[k]; });
    updateClipButtons();
    flash(_t('settings.pip.flash_copied',
        'Réglages copiés (hors position). Sélectionnez les composants cibles puis Coller.'), 'ok');
}

function pasteCompSettings() {
    if (!cur || !reglagesClipboard) {
        flash(_t('settings.pip.flash_nothing_to_paste', "Rien à coller : copiez d'abord les réglages d'un composant."), 'warn');
        return;
    }
    if (!selIdxs.length) {
        flash(_t('settings.pip.flash_select_targets', 'Sélectionnez un ou plusieurs composants cibles.'), 'warn');
        return;
    }
    const r3 = v => Math.round(v * 1000) / 1000;
    let applied = 0, mismatched = 0;
    selIdxs.forEach(i => {
        const c = comps()[i];
        if (!c) return;
        if (c.type !== reglagesClipboard.type) { mismatched++; return; }
        Object.keys(reglagesClipboard).forEach(k => {
            if (k === 'type') return;
            if (k === 'w' || k === 'h') {
                // Taille collée, position (x/y) INCHANGÉE : bornée pour ne pas sortir de la cellule.
                const posKey = k === 'w' ? 'x' : 'y';
                c[k] = r3(Math.min(reglagesClipboard[k], 1 - (c[posKey] || 0)));
            } else {
                c[k] = reglagesClipboard[k];
            }
        });
        _enforceVideoRatio(c);
        applied++;
    });
    refreshProps(); draw();
    if (applied === 0) {
        flash(_t('settings.pip.flash_paste_type_mismatch',
            'Aucun réglage appliqué : le composant copié est de type différent de la sélection.'), 'warn');
        return;
    }
    let msg = _t('settings.pip.flash_pasted', 'Réglages collés dans {n} composant(s).').replace('{n}', applied);
    if (mismatched) {
        msg += ' ' + _t('settings.pip.flash_pasted_skipped', '{n} ignoré(s) (type différent).')
            .replace('{n}', mismatched);
    }
    flash(msg, mismatched ? 'warn' : 'ok');
}

// Bouton Coller DÉSACTIVÉ tant qu'il n'y a rien dans le presse-papier ou aucune sélection cible
// (même règle que le composer, cf. multiview.js:updateToolbar ~1593 — « pas de contrôle muet »).
// Appelé depuis refreshProps() : point d'entrée unique après toute mutation de sélection/modèle,
// comme updateToolbar() est appelé depuis dessiner() côté composer.
function updateClipButtons() {
    const copyBtn = $('pip_copy_settings');
    if (copyBtn) copyBtn.disabled = selIdx < 0;
    const pasteBtn = $('pip_paste_settings');
    if (pasteBtn) pasteBtn.disabled = !reglagesClipboard || selIdxs.length === 0;
}

// ── Outils de disposition (mêmes sémantiques que le composer, en normalisé) ──
// Référence d'alignement quand UN SEUL composant est sélectionné : la cellule entière (défaut,
// comportement historique) ou l'IMAGE, c'est-à-dire le rectangle du composant `video`. Centrer un
// UMD sur la cellule ≠ le centrer sur l'image dès que la vidéo ne remplit pas la cellule — et
// c'est presque toujours sur l'image qu'on veut se caler.
let alignRef = 'cell';

function _alignRefRect() {
    if (alignRef === 'video') {
        const v = (comps() || []).find(c => c && c.type === 'video');
        if (v && v.w > 0 && v.h > 0) {
            return { x: v.x || 0, y: v.y || 0, w: v.w, h: v.h };
        }
        // Modèle sans composant vidéo : on retombe sur la cellule plutôt que de ne rien faire.
    }
    return { x: 0, y: 0, w: 1, h: 1 };
}

function alignComps(mode) {
    if (!cur || !selIdxs.length) return;
    // Référence : le primaire si ≥2 sélectionnés, sinon la cellule OU l'image (cf. alignRef).
    const p = comps()[selIdx];
    const ref = (selIdxs.length >= 2 && p) ? { x: p.x || 0, y: p.y || 0, w: p.w || 0, h: p.h || 0 }
                                           : _alignRefRect();
    const r3 = v => Math.round(v * 1000) / 1000;
    selIdxs.forEach(i => {
        if (selIdxs.length >= 2 && i === selIdx) return;
        const c = comps()[i];
        if (!c) return;
        switch (mode) {
            case 'left':    c.x = ref.x; break;
            case 'right':   c.x = ref.x + ref.w - c.w; break;
            case 'hcenter': c.x = ref.x + (ref.w - c.w) / 2; break;
            case 'top':     c.y = ref.y; break;
            case 'bottom':  c.y = ref.y + ref.h - c.h; break;
            case 'vcenter': c.y = ref.y + (ref.h - c.h) / 2; break;
        }
        c.x = r3(Math.max(0, Math.min(1 - c.w, c.x)));
        c.y = r3(Math.max(0, Math.min(1 - c.h, c.y)));
    });
    refreshProps(); draw();
}

function matchSizeComps(mode) {
    if (!cur || selIdxs.length < 2) {
        flash(_t('settings.pip.flash_select2', 'Sélectionnez au moins 2 composants (Maj+clic).'), 'warn');
        return;
    }
    const ref = comps()[selIdx];
    if (!ref) return;
    const r3 = v => Math.round(v * 1000) / 1000;
    selIdxs.forEach(i => {
        if (i === selIdx) return;
        const c = comps()[i];
        if (!c) return;
        if (mode === 'w' || mode === 'both') {
            c.w = r3(Math.min(ref.w, 1));
            if (c.x + c.w > 1) c.x = r3(1 - c.w);
        }
        if (mode === 'h' || mode === 'both') {
            c.h = r3(Math.min(ref.h, 1));
            if (c.y + c.h > 1) c.y = r3(1 - c.h);
        }
        _enforceVideoRatio(c);
    });
    refreshProps(); draw();
}

function distributeComps(axis) {
    if (!cur || selIdxs.length < 3) {
        flash(_t('settings.pip.flash_select3', 'Sélectionnez au moins 3 composants (Maj+clic).'), 'warn');
        return;
    }
    const items = selIdxs.map(i => comps()[i]).filter(Boolean);
    const k = axis === 'h' ? 'x' : 'y';
    items.sort((a, b) => (a[k] || 0) - (b[k] || 0));
    const v0 = items[0][k] || 0;
    const vN = items[items.length - 1][k] || 0;
    const step = (vN - v0) / (items.length - 1);
    items.forEach((c, n) => { c[k] = Math.round((v0 + step * n) * 1000) / 1000; });
    refreshProps(); draw();
}

// ── Format libre : rogner l'espace inutilisé sur les 4 côtés ──
// Bbox de TOUS les composants → renormalisation 0..1 + nouvel aspect (A × bw/bh). La vidéo
// reste 16:9 en pixels par construction (h/w se renormalisent comme l'aspect) ; le
// _enforceVideoRatio final n'absorbe que les arrondis.
function trimTemplate() {
    if (!cur || !comps().length) return;
    let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
    comps().forEach(c => {
        x0 = Math.min(x0, c.x || 0); y0 = Math.min(y0, c.y || 0);
        x1 = Math.max(x1, (c.x || 0) + (c.w || 0)); y1 = Math.max(y1, (c.y || 0) + (c.h || 0));
    });
    const bw = x1 - x0, bh = y1 - y0;
    if (bw < 0.05 || bh < 0.05) return;
    if (bw > 0.998 && bh > 0.998) {
        flash(_t('settings.pip.flash_trim_nothing', 'Rien à rogner : les composants occupent déjà toute la cellule.'), 'warn');
        return;
    }
    const r3 = v => Math.round(Math.max(0, Math.min(1, v)) * 1000) / 1000;
    comps().forEach(c => {
        c.x = r3(((c.x || 0) - x0) / bw);
        c.y = r3(((c.y || 0) - y0) / bh);
        c.w = Math.max(0.02, Math.round(((c.w || 0) / bw) * 1000) / 1000);
        c.h = Math.max(0.02, Math.round(((c.h || 0) / bh) * 1000) / 1000);
        if (c.x + c.w > 1) c.x = r3(1 - c.w);
        if (c.y + c.h > 1) c.y = r3(1 - c.h);
    });
    cur.config.aspect = Math.round(tplAspect() * (bw / bh) * 10000) / 10000;
    comps().forEach(_enforceVideoRatio);
    syncAspectField();
    applySimCell();
    flash(_t('settings.pip.flash_trimmed', 'Espace inutilisé rogné — nouveau format : ')
          + fmtAspect(tplAspect()), 'ok');
}

function syncAspectField() {
    const el = $('pip_aspect');
    if (el && document.activeElement !== el) el.value = cur ? fmtAspect(tplAspect()) : '';
}

// ── visibilité simulée (miroir de _comp_visible du script) ──
function compVisible(c) {
    if ((c.min_w || 0) > cellW) return false;
    const when = c.when || 'always';
    if (when === 'always') return true;
    if (when === 'no_signal') return simSignal === 'nosignal';
    if (when === 'freeze') return simSignal === 'freeze';
    if (when === 'signal_ok') return simSignal === '';
    if (when === 'tally_red') return simTally === 'red';
    if (when === 'tally_green') return simTally === 'green';
    if (when === 'tally_any') return simTally !== 'off';
    if (when === 'tally_off') return simTally === 'off';
    return true;
}

// ── canvas ──
function cvs() { return $('pip_canvas'); }

// SIMULATION de taille de tuile : la RÉSOLUTION interne du canvas = la tuile simulée (640×360,
// 320×180…), la taille d'AFFICHAGE reste constante (fixée en px, comme resizeCanvas du
// composer). Les éléments à taille ABSOLUE (VU-mètres, polices fixes, épaisseurs de bordure)
// grossissent donc relativement sur une petite tuile — exactement comme dans le moteur.
function applySimCell() {
    const cv = cvs();
    if (!cv) return;
    const h = Math.max(2, Math.round(cellW / tplAspect() / 2) * 2);   // aspect du modèle, hauteur paire
    cv.width = cellW;
    cv.height = h;
    _resizeCanvasDisplay();
    refreshProps();
    draw();
}

function _resizeCanvasDisplay() {
    const cv = cvs();
    if (!cv) return;
    const zone = $('pip_edit_zone');
    const avail = (zone && zone.clientWidth) || 900;
    cv.style.width = Math.min(900, avail) + 'px';
    cv.style.height = 'auto';
}
// ── Rectangle de l'IMAGE, tel que le MOTEUR le calcule ──────────────────────────────────────
// L'éditeur dessinait le rectangle du composant `video` comme si c'était l'image. Au rendu,
// l'image est en réalité (1) rétrécie par la réserve du style « viseur » et (2) recadrée en
// `contain` au ratio de la source. D'où un habillage collé aux bords à l'écran et débordant de
// 8-10 px sur un petit PiP. On reproduit ici les deux étapes, à l'identique de _video_rect.
function imgPxRect() {
    const cv = cvs();
    const v = (comps() || []).find(c => c && c.type === 'video');
    if (!v) return { x: 0, y: 0, w: cv.width, h: cv.height, out: 0 };
    let x = Math.round((v.x || 0) * cv.width), y = Math.round((v.y || 0) * cv.height);
    let w = Math.max(4, Math.round((v.w || 0) * cv.width));
    let h = Math.max(4, Math.round((v.h || 0) * cv.height));
    let fo = 0;                            // débord de la bordure hors de l'image
    if ((v.border || 'none') === 'viewfinder') {
        // Même formule que le moteur : épaisseur clampée, puis marge = épaisseur + 1.
        const bw = Math.max(1, Math.min(24, parseInt(v.border_w, 10) || 3));
        const m = Math.max(2, bw) + 1;
        fo = Math.max(2, bw);
        const mx = Math.min(m, Math.max(0, Math.floor(w / 4)));
        const my = Math.min(m, Math.max(0, Math.floor(h / 4)));
        x += mx; y += my; w = Math.max(2, w - 2 * mx); h = Math.max(2, h - 2 * my);
    }
    if ((v.fit || 'fill') === 'contain') {
        const SA = 16 / 9;                 // ratio SOURCE simulé par le composer
        let nw = Math.min(w, Math.round(h * SA));
        let nh = Math.max(2, Math.round(nw / SA));
        if (nh > h) { nh = h; nw = Math.max(2, Math.round(nh * SA)); }
        x += Math.floor((w - nw) / 2); y += Math.floor((h - nh) / 2);
        w = nw; h = nh;
    }
    return { x, y, w, h, out: fo };        // `out` = débord de la bordure, cf. framePxRect
}

// Rectangle du CADRE = image + ce que la bordure dessine À L'EXTÉRIEUR (équerres du viseur).
// C'est le bord VISUEL du bloc vidéo, et donc la référence d'ancrage de l'habillage : mesuré sur
// le mur, l'équerre occupe les colonnes 4-5 et l'image commence à la 6 — s'aligner sur l'image
// laisserait les tallies en retrait des équerres.
function framePxRect() {
    const im = imgPxRect();
    const o = im.out || 0;
    if (!o) return im;
    return { x: Math.max(0, im.x - o), y: Math.max(0, im.y - o),
             w: Math.max(2, im.w + 2 * o), h: Math.max(2, im.h + 2 * o) };
}

// Ancrage par AXE — mêmes règles et mêmes défauts que le moteur (_comp_anchor).
const _ANCHOR_X_IMAGE_TYPES = ['umd', 'tally', 'format', 'clock', 'text'];
function compAnchor(c, axis) {
    if (!c || c.type === 'video') return 'cell';
    const v = String(c['anchor_' + axis] || '').toLowerCase();
    if (v === 'cell' || v === 'image') return v;
    return (axis === 'x' && _ANCHOR_X_IMAGE_TYPES.includes(c.type)) ? 'image' : 'cell';
}

function pxRect(c) {
    const cv = cvs();
    let bx = 0, by = 0, bw = cv.width, bh = cv.height;
    const ax = compAnchor(c, 'x'), ay = compAnchor(c, 'y');
    if (ax === 'image' || ay === 'image') {
        const im = framePxRect();
        if (ax === 'image') { bx = im.x; bw = im.w; }
        if (ay === 'image') { by = im.y; bh = im.h; }
    }
    const x = bx + Math.round((c.x || 0) * bw), y = by + Math.round((c.y || 0) * bh);
    return { x, y, w: Math.max(4, Math.round((c.w || 0) * bw)),
             h: Math.max(4, Math.round((c.h || 0) * bh)) };
}

function hexA(hex, a) {
    hex = (hex || '#000000').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
    const n = parseInt(hex, 16) || 0;
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
}

function drawMock(ctx, c, r) {
    // La VIDÉO se dessine à son rectangle d'IMAGE (réserve viseur + fit contain appliqués), pas
    // au rectangle brut du composant : c'est cette image-là que l'habillage doit border, et c'est
    // elle que le moteur produira. La zone réservée reste visible en filigrane ci-dessous.
    if (c.type === 'video') {
        const im = imgPxRect();
        if (im.w > 4 && im.h > 4 && (im.x !== r.x || im.y !== r.y || im.w !== r.w || im.h !== r.h)) {
            ctx.save();
            ctx.setLineDash([3, 3]);
            ctx.strokeStyle = 'rgba(150,150,160,0.45)'; ctx.lineWidth = 1;
            ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);   // place RÉSERVÉE
            ctx.restore();
            r = im;
        }
    }
    const dim = compVisible(c) ? 1 : 0.25;
    ctx.save();
    ctx.globalAlpha = dim;
    // Taille de police FIDÈLE au moteur : fixe (font_size px) si définie, sinon auto ∝ hauteur
    // du rectangle — sur une petite tuile simulée, une police fixe devient donc grosse.
    const fsAuto = Math.max(8, r.h * 0.6);
    const fs = (parseInt(c.font_size) > 0) ? parseInt(c.font_size) : fsAuto;
    // POLICE RÉELLE du composant (bibliothèque comprise : @font-face posé par BobiFonts) — un
    // aperçu qui garde system-ui mentirait sur le rendu du conteneur.
    const fam = window.BobiFonts ? window.BobiFonts.cssFamily(c.font) : 'system-ui';
    ctx.font = 'bold ' + fs + 'px ' + fam;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
    const tallyCol = simTally === 'red' ? '#dc2828' : (simTally === 'green' ? '#28c850' : '#3a3a42');
    if (c.type === 'video') {
        // Filigrane DISCRET (pas la taille auto des composants texte — le composant vidéo
        // remplit souvent toute la cellule, l'auto donnait un « VIDÉO » géant).
        ctx.font = 'bold ' + Math.max(10, Math.min(r.h * 0.12, 24)) + 'px system-ui';
        const g = ctx.createLinearGradient(r.x, r.y, r.x + r.w, r.y + r.h);
        g.addColorStop(0, '#28303c'); g.addColorStop(1, '#101418');
        ctx.fillStyle = g; ctx.fillRect(r.x, r.y, r.w, r.h);
        if (simSignal === 'nosignal') {
            ctx.fillStyle = '#16181c'; ctx.fillRect(r.x, r.y, r.w, r.h);
            ctx.fillStyle = '#8b929e'; ctx.fillText('NO SIGNAL', cx, cy);
        } else {
            ctx.strokeStyle = 'rgba(255,255,255,0.12)';
            ctx.beginPath(); ctx.moveTo(r.x, r.y); ctx.lineTo(r.x + r.w, r.y + r.h);
            ctx.moveTo(r.x + r.w, r.y); ctx.lineTo(r.x, r.y + r.h); ctx.stroke();
            ctx.fillStyle = 'rgba(255,255,255,0.35)';
            ctx.fillText(simSignal === 'freeze' ? 'FREEZE' : TYPE_LABELS().video.toUpperCase(), cx, cy);
        }
        // Cadre du composant vidéo — aperçu des styles migrés (0.33.0), miroir approché du
        // moteur (_tpl_draw_video_border) : dessiné vers l'intérieur du rect image.
        const bmode = c.border || 'none';
        if (bmode !== 'none') {
            const bw = Math.max(1, parseInt(c.border_w) || 3);
            const tallyOr = (neutral) => simTally !== 'off' ? tallyCol : neutral;
            if (bmode === 'stylized') {
                const t = Math.max(4, Math.round(bw * 2.2));
                ctx.strokeStyle = '#2e2e35'; ctx.lineWidth = t;
                ctx.strokeRect(r.x + t / 2, r.y + t / 2, r.w - t, r.h - t);
                ctx.strokeStyle = '#62626c'; ctx.lineWidth = 1;
                ctx.strokeRect(r.x + t, r.y + t, r.w - 2 * t, r.h - 2 * t);
            } else if (bmode === 'viewfinder') {
                const col = tallyOr('#e1e1e8');
                const arm = Math.max(8, Math.round(Math.min(r.w, r.h) * 0.14));
                const bt = Math.max(2, bw);
                ctx.fillStyle = col;
                for (const [px, py, sx, sy] of [[r.x, r.y, 1, 1], [r.x + r.w, r.y, -1, 1],
                                                [r.x, r.y + r.h, 1, -1], [r.x + r.w, r.y + r.h, -1, -1]]) {
                    ctx.fillRect(Math.min(px, px + sx * arm), Math.min(py, py + sy * bt),
                                 arm, bt);
                    ctx.fillRect(Math.min(px, px + sx * bt), Math.min(py, py + sy * arm),
                                 bt, arm);
                }
            } else if (bmode === 'flat') {
                const t = Math.max(2, bw);
                ctx.fillStyle = tallyOr('#5a5a62');
                ctx.fillRect(r.x, r.y + r.h - t, r.w, t);
            } else {
                ctx.strokeStyle = bmode === 'tally' ? tallyOr('#46464e')
                                : bmode === 'classic' ? '#82828a'
                                : (c.border_color || '#ffffff');
                ctx.lineWidth = bmode === 'classic' ? Math.max(2, bw) : bw;
                ctx.strokeRect(r.x + bw / 2, r.y + bw / 2, r.w - bw, r.h - bw);
            }
        }
    } else if (c.type === 'umd') {
        ctx.fillStyle = (c.tally_bg && simTally !== 'off')
            ? (simTally === 'red' ? 'rgba(120,20,20,0.92)' : 'rgba(20,90,35,0.92)')
            : hexA(c.bg_color || '#000000', (c.bg_opacity ?? 75) / 100);
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.fillStyle = (c.tally_text && simTally === 'red') ? '#ff5a5a'
                      : (c.tally_text && simTally === 'green') ? '#78ff8c' : (c.color || '#ffffff');
        const txt = c.text_source === 'fixed' ? (c.text || '—')
                  : c.text_source === 'tsl' ? 'UMD TSL' : 'CAM 1';
        ctx.fillText(txt, cx, cy);
    } else if (c.type === 'tally') {
        if ((c.shape || 'lamp') === 'border') {
            ctx.strokeStyle = tallyCol; ctx.lineWidth = Math.max(2, c.thickness || 4);
            ctx.strokeRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
        } else if (c.shape === 'bar') {
            ctx.fillStyle = tallyCol; ctx.fillRect(r.x, r.y, r.w, r.h);
        } else {
            const rad = Math.max(3, Math.min(r.w, r.h) / 2 - 1);
            ctx.fillStyle = tallyCol;
            ctx.beginPath(); ctx.arc(cx, cy, rad, 0, 7); ctx.fill();
            ctx.strokeStyle = 'rgba(255,255,255,0.5)'; ctx.stroke();
        }
    } else if (c.type === 'meters') {
        // Largeur du meter : miroir EXACT du moteur (plugins/multiview/script.py).
        // Mode "auto" (défaut, « Fixe ») : dimensions INTRINSÈQUES en dur (METER_TICK_W 16 +
        // n×5 + gaps de 1 px), ancrées left/center/right dans le rectangle — un meter est PLUS
        // LARGE relativement sur une petite tuile (tailles absolues). Mode "fit" (« Suit la
        // largeur ») : SEULES les barres s'élargissent pour remplir r.w (tick/gap fixes) via
        // meterFitDims (réplique _meter_fit_dims), align sans effet.
        const n = parseInt(c.channels) || 2;
        const s1 = Math.max(1, Math.min(16, parseInt(c.ch_start) || 1));
        const widthMode = c.width_mode || 'auto';
        let TICK, BW, GAP, mw, mx;
        if (widthMode === 'fit') {
            ({ tick: TICK, bar: BW, gap: GAP, mw } = meterFitDims(n, r.w));
            mx = r.x;
        } else {
            TICK = 16; BW = 5; GAP = 1;
            mw = TICK + n * BW + (n - 1) * GAP;
            const al = c.align || 'left';
            mx = r.x + (al === 'center' ? (r.w - mw) / 2 : (al === 'right' ? r.w - mw : 0));
        }
        ctx.fillStyle = 'rgba(0,0,0,' + (0.7 * (c.opacity ?? 100) / 100) + ')';
        ctx.fillRect(mx, r.y, mw + 1, r.h);
        ctx.fillStyle = '#b4b4b4';
        ctx.font = '8px monospace'; ctx.textAlign = 'left';
        ['0', '-12', '-30'].forEach((lbl, k) => {
            ctx.fillText(lbl, mx + 1, r.y + 8 + k * (r.h - 20) / 2.4);
        });
        const barsH = Math.max(10, r.h - 12);
        for (let i = 0; i < n; i++) {
            const lvl = 0.55 + 0.3 * Math.sin((s1 + i) * 1.7);
            const bx = mx + TICK + i * (BW + GAP);
            ctx.fillStyle = '#3cc83c'; ctx.fillRect(bx, r.y + barsH * (1 - lvl * 0.7), BW, barsH * lvl * 0.7);
            ctx.fillStyle = '#dcb428'; ctx.fillRect(bx, r.y + barsH * (1 - lvl), BW, barsH * (lvl - lvl * 0.7));
        }
        // Numéros de canaux réels (affectation ch_start) sous les barres
        ctx.fillStyle = '#c8c8d0';
        ctx.font = '8px monospace'; ctx.textAlign = 'center';
        for (let i = 0; i < n; i++) {
            ctx.fillText(String(s1 + i), mx + TICK + i * (BW + GAP) + BW / 2, r.y + r.h - 3);
        }
    } else if (c.type === 'anc') {
        ctx.fillStyle = hexA('#000000', (c.anc_opacity ?? 60) / 100);
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.fillStyle = '#ebebeb'; ctx.textAlign = 'left';
        const parts = [];
        if (c.anc_types) parts.push('ATC RP188 AFD');
        if (c.anc_tc) parts.push('10:00:00:00');
        if (c.anc_st352) parts.push('1080i50');
        if (c.anc_afd) parts.push('16:9 FULL');
        if (c.anc_scte) parts.push('SPLICE 1');
        if (c.anc_cc) parts.push('CC ●');
        ctx.font = Math.max(8, Math.min(r.h * 0.6, 18)) + 'px ' + fam;
        ctx.fillText(parts.join('  ') || 'ANC --', r.x + 4, cy);
    } else if (c.type === 'clock') {
        ctx.fillStyle = hexA(c.bg_color || '#000000', (c.bg_opacity ?? 60) / 100);
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.fillStyle = c.color || '#ffffff';
        const seg = [];
        if (c.show_hh !== false) seg.push('12'); if (c.show_mm !== false) seg.push('34');
        if (c.show_ss !== false) seg.push('56');
        let txt = seg.join(':'); if (c.show_ff) txt += ':12';
        ctx.fillText(txt || '12:34:56', cx, cy);
    } else if (c.type === 'text') {
        if (c.bg_color) { ctx.fillStyle = hexA(c.bg_color, (c.bg_opacity ?? 100) / 100); ctx.fillRect(r.x, r.y, r.w, r.h); }
        ctx.fillStyle = c.color || '#ffffff';
        ctx.fillText(c.text || 'TEXTE', cx, cy);
    } else if (c.type === 'format') {
        ctx.fillStyle = hexA(c.bg_color || '#000000', (c.bg_opacity ?? 65) / 100);
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.fillStyle = c.color || '#d2d4da';
        ctx.font = Math.max(8, Math.min(r.h * 0.6, 16)) + 'px ' + fam;
        ctx.fillText('1920×1080p25', cx, cy);
    } else if (c.type === 'video_history') {
        // Aperçu SCHÉMATIQUE (l'éditeur n'a pas de flux live) : cases de vignettes + ruban
        // d'événements — miroir de _vh_render (plugins/multiview/script.py).
        const a = (c.opacity ?? 85) / 100;
        ctx.fillStyle = `rgba(14,16,20,${a})`;
        ctx.fillRect(r.x, r.y, r.w, r.h);
        const rb = c.events === false ? 0 : Math.max(3, Math.min(10, r.h / 8));
        const sh = Math.max(3, r.h - rb - 2);
        const n = Math.max(1, Math.min(120, parseInt(c.duration) || 30));
        const cw = r.w / n;
        for (let k = 0; k < n; k++) {
            ctx.fillStyle = (k % 2) ? 'rgba(52,56,66,0.9)' : 'rgba(40,44,52,0.9)';
            ctx.fillRect(r.x + k * cw, r.y, Math.max(1, cw - 1), sh);
        }
        if (rb) {
            ctx.fillStyle = 'rgba(44,48,56,0.95)';
            ctx.fillRect(r.x, r.y + sh + 1, r.w, rb);
            ctx.fillStyle = 'rgba(240,184,44,0.95)';
            ctx.fillRect(r.x + r.w * 0.55, r.y + sh + 1, Math.max(2, r.w * 0.08), rb);
            ctx.fillStyle = 'rgba(236,72,60,0.95)';
            ctx.fillRect(r.x + r.w * 0.8, r.y + sh + 1, Math.max(2, r.w * 0.05), rb);
        }
    } else if (c.type === 'audio_history') {
        // Aperçu SCHÉMATIQUE : enveloppe des crêtes + colonne de saturation — miroir de _ah_render.
        const a = (c.opacity ?? 85) / 100;
        ctx.fillStyle = `rgba(14,16,20,${a})`;
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.strokeStyle = 'rgba(96,210,140,0.95)';
        ctx.beginPath();
        for (let x = 0; x < r.w; x += 2) {
            const h = (Math.abs(Math.sin(x / 7)) * 0.35 + Math.abs(Math.sin(x / 23)) * 0.5) * (r.h / 2 - 2);
            ctx.moveTo(r.x + x, cy - h); ctx.lineTo(r.x + x, cy + h);
        }
        ctx.stroke();
        ctx.fillStyle = 'rgba(236,72,60,0.95)';
        ctx.fillRect(r.x + r.w * 0.62, r.y, 2, r.h);
    }
    ctx.restore();
}

function draw() {
    const cv = cvs();
    if (!cv) return;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cv.width, cv.height);
    const list = comps();
    // vidéo d'abord (fond), puis le reste dans l'ordre
    const order = list.map((c, i) => i).sort((a, b) =>
        (list[a].type === 'video' ? 0 : 1) - (list[b].type === 'video' ? 0 : 1));
    order.forEach(i => drawMock(ctx, list[i], pxRect(list[i])));
    // cadres de sélection : tous les sélectionnés en bleu, le PRIMAIRE (référence des outils)
    // en blanc pointillé — même convention que le composer. Poignée sur le primaire.
    selIdxs.forEach(i => {
        if (!list[i]) return;
        const r = pxRect(list[i]);
        const primary = (i === selIdx);
        ctx.strokeStyle = primary ? '#ffffff' : '#58a6ff';
        ctx.lineWidth = 2;
        ctx.setLineDash(primary ? [6, 3] : [3, 3]);
        ctx.strokeRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
        ctx.setLineDash([]);
        if (primary) {
            ctx.fillStyle = '#58a6ff';
            ctx.fillRect(r.x + r.w - 8, r.y + r.h - 8, 8, 8);
        }
    });
    // Lignes guides de snap (pendant le drag) — même rendu que le composer.
    if (drag && snapGuides.length) {
        ctx.strokeStyle = '#e3b341';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        snapGuides.forEach(g => {
            ctx.beginPath();
            if (g.type === 'v') { ctx.moveTo(g.pos, 0); ctx.lineTo(g.pos, cv.height); }
            else                { ctx.moveTo(0, g.pos); ctx.lineTo(cv.width, g.pos); }
            ctx.stroke();
        });
        ctx.setLineDash([]);
    }
}

// ── Snap magnétique (miroir de computeSnap/computeSnapResize du composer, en px canvas) ──
function _snapTargets(excludeIdxs) {
    const cv = cvs();
    const xT = [0, cv.width, cv.width / 2];
    const yT = [0, cv.height, cv.height / 2];
    comps().forEach((o, i) => {
        if (excludeIdxs.includes(i)) return;
        const r = pxRect(o);
        xT.push(r.x, r.x + r.w, r.x + r.w / 2);
        yT.push(r.y, r.y + r.h, r.y + r.h / 2);
    });
    return { xT, yT };
}

function computeSnap(excludeIdxs, x, y, w, h) {
    const { xT, yT } = _snapTargets(excludeIdxs);
    const guides = [];
    const SP = SNAP_PX();
    let bestDx = SP + 1, snapX = x;
    [{ e: 'l', v: x }, { e: 'r', v: x + w }, { e: 'c', v: x + w / 2 }].forEach(({ e, v }) => {
        xT.forEach(t => {
            const d = Math.abs(v - t);
            if (d <= SP && d < bestDx) {
                bestDx = d;
                snapX = e === 'l' ? t : e === 'r' ? t - w : t - w / 2;
                guides[0] = { type: 'v', pos: t };
            }
        });
    });
    let bestDy = SP + 1, snapY = y;
    [{ e: 't', v: y }, { e: 'b', v: y + h }, { e: 'c', v: y + h / 2 }].forEach(({ e, v }) => {
        yT.forEach(t => {
            const d = Math.abs(v - t);
            if (d <= SP && d < bestDy) {
                bestDy = d;
                snapY = e === 't' ? t : e === 'b' ? t - h : t - h / 2;
                guides[1] = { type: 'h', pos: t };
            }
        });
    });
    return { x: snapX, y: snapY, guides: guides.filter(Boolean) };
}

function computeSnapResize(excludeIdxs, x, y, w, h) {
    // Snap du coin bas-droit uniquement (resize ancré haut-gauche).
    const { xT, yT } = _snapTargets(excludeIdxs);
    const guides = [];
    const SP = SNAP_PX();
    let bestDx = SP + 1, sw = w;
    xT.forEach(t => {
        const d = Math.abs((x + w) - t);
        if (d <= SP && d < bestDx && t > x) { bestDx = d; sw = t - x; guides[0] = { type: 'v', pos: t }; }
    });
    let bestDy = SP + 1, sh = h;
    yT.forEach(t => {
        const d = Math.abs((y + h) - t);
        if (d <= SP && d < bestDy && t > y) { bestDy = d; sh = t - y; guides[1] = { type: 'h', pos: t }; }
    });
    return { w: sw, h: sh, guides: guides.filter(Boolean) };
}

function canvasPos(e) {
    const cv = cvs(), b = cv.getBoundingClientRect();
    return { x: (e.clientX - b.left) * cv.width / b.width,
             y: (e.clientY - b.top) * cv.height / b.height };
}

function hit(pos) {
    const list = comps();
    for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].type === 'video') continue;   // vidéo sélectionnable en dernier (fond)
        const r = pxRect(list[i]);
        if (pos.x >= r.x && pos.x <= r.x + r.w && pos.y >= r.y && pos.y <= r.y + r.h) return i;
    }
    for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].type !== 'video') continue;
        const r = pxRect(list[i]);
        if (pos.x >= r.x && pos.x <= r.x + r.w && pos.y >= r.y && pos.y <= r.y + r.h) return i;
    }
    return -1;
}

function onDown(e) {
    const pos = canvasPos(e);
    const cv = cvs();
    if (selIdx >= 0 && comps()[selIdx]) {
        const c = comps()[selIdx];
        const r = pxRect(c);
        if (pos.x >= r.x + r.w - 10 && pos.x <= r.x + r.w + 4 &&
            pos.y >= r.y + r.h - 10 && pos.y <= r.y + r.h + 4) {
            drag = { mode: 'resize', idx: selIdx, start: pos, ow: c.w || 0, oh: c.h || 0 };
            cv.setPointerCapture(e.pointerId);
            return;
        }
    }
    const i = hit(pos);
    if (e.shiftKey && i >= 0) {
        // Maj+clic : toggle dans la multi-sélection ; le dernier cliqué devient la référence.
        const rest = selIdxs.filter(x => x !== i);
        if (rest.length === selIdxs.length) rest.push(i);
        _setSel(rest);
    } else if (i >= 0) {
        if (!selIdxs.includes(i)) _setSel([i]);
        else _setSel(selIdxs.filter(x => x !== i).concat([i]));   // devient la référence
    } else {
        _setSel([]);
    }
    if (i >= 0) {
        const grp = selIdxs.includes(i) ? selIdxs : [i];
        drag = { mode: 'move', idx: i, start: pos,
                 orig: grp.map(gi => ({ gi, x: comps()[gi].x || 0, y: comps()[gi].y || 0 })) };
        cv.setPointerCapture(e.pointerId);
    }
    refreshProps(); draw();
}

function onMove(e) {
    if (!drag) return;
    const cv = cvs();
    const pos = canvasPos(e);
    const r3 = v => Math.round(v * 1000) / 1000;
    if (drag.mode === 'move') {
        // Delta CUMULÉ depuis l'origine (avec snap sur le composant saisi), appliqué au GROUPE.
        const o = drag.orig.find(g => g.gi === drag.idx);
        const c = comps()[drag.idx];
        if (!o || !c) return;
        let nx = (o.x * cv.width) + (pos.x - drag.start.x);
        let ny = (o.y * cv.height) + (pos.y - drag.start.y);
        if (snapEnabled) {
            const s = computeSnap(drag.orig.map(g => g.gi), nx, ny,
                                  (c.w || 0) * cv.width, (c.h || 0) * cv.height);
            nx = s.x; ny = s.y; snapGuides = s.guides;
        } else snapGuides = [];
        let ddx = nx / cv.width - o.x, ddy = ny / cv.height - o.y;
        // Clamp du delta : AUCUN composant du groupe ne sort de la cellule.
        let loX = -Infinity, hiX = Infinity, loY = -Infinity, hiY = Infinity;
        drag.orig.forEach(g => {
            const gc = comps()[g.gi];
            if (!gc) return;
            loX = Math.max(loX, -g.x); hiX = Math.min(hiX, 1 - (gc.w || 0) - g.x);
            loY = Math.max(loY, -g.y); hiY = Math.min(hiY, 1 - (gc.h || 0) - g.y);
        });
        ddx = Math.max(loX, Math.min(hiX, ddx));
        ddy = Math.max(loY, Math.min(hiY, ddy));
        drag.orig.forEach(g => {
            const gc = comps()[g.gi];
            if (!gc) return;
            gc.x = r3(g.x + ddx);
            gc.y = r3(g.y + ddy);
        });
    } else {
        const c = comps()[drag.idx];
        if (!c) return;
        const dx = (pos.x - drag.start.x) / cv.width, dy = (pos.y - drag.start.y) / cv.height;
        if (c.type === 'video') {
            // Vidéo verrouillée 16:9 en pixels (h = w × k en normalisé) : la poignée suit le
            // plus grand delta, exprimé en unités de largeur.
            const k = _videoK();
            const wMax = () => Math.min(1 - (c.x || 0), (1 - (c.y || 0)) / k);
            let s = Math.max(0.02, Math.min(wMax(), drag.ow + Math.max(dx, dy / k)));
            if (snapEnabled) {
                const sn = computeSnapResize([drag.idx], (c.x || 0) * cv.width, (c.y || 0) * cv.height,
                                             s * cv.width, s * k * cv.height);
                snapGuides = sn.guides.slice(0, 1);
                if (sn.guides[0]) s = Math.max(0.02, Math.min(sn.w / cv.width, wMax()));
            } else snapGuides = [];
            c.w = r3(s); c.h = r3(s * k);
        } else {
            let nw = Math.max(0.02, Math.min(1 - (c.x || 0), drag.ow + dx));
            let nh = Math.max(0.02, Math.min(1 - (c.y || 0), drag.oh + dy));
            if (snapEnabled) {
                const sn = computeSnapResize([drag.idx], (c.x || 0) * cv.width, (c.y || 0) * cv.height,
                                             nw * cv.width, nh * cv.height);
                nw = Math.max(0.02, Math.min(sn.w / cv.width, 1 - (c.x || 0)));
                nh = Math.max(0.02, Math.min(sn.h / cv.height, 1 - (c.y || 0)));
                snapGuides = sn.guides;
            } else snapGuides = [];
            c.w = r3(nw);
            c.h = r3(nh);
        }
    }
    syncGeomFields(); draw();
}

function onUp() {
    drag = null;
    snapGuides = [];
    draw();
}

// ── Déplacement au clavier (flèches) ──
// Le drag à la souris passe par le snap magnétique : impossible de poser un composant à quelques
// pixels d'une arête sans qu'il s'y colle. Les flèches déplacent la sélection PIXEL PAR PIXEL (de
// la tuile SIMULÉE, cf. applySimCell : cv.width = largeur de tuile), en contournant le snap.
// Maj = pas de 10 px. Les coordonnées étant stockées en fractions à 3 décimales, le pas ne peut
// pas descendre sous 0,001 — plancher appliqué (sinon l'arrondi mangerait le déplacement).
// Toute la MULTI-sélection bouge d'un bloc, et le déplacement est borné pour qu'AUCUN composant
// ne sorte de la cellule (même règle que le drag).
const NUDGE_KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

function nudgeSelection(e) {
    const dir = NUDGE_KEYS[e.key];
    if (!dir) return;
    const cv = cvs();
    if (!cv) return;
    e.preventDefault();                       // sinon la page défile

    const r3 = v => Math.round(v * 1000) / 1000;
    const mult = e.shiftKey ? 10 : 1;
    let dx = Math.max(1 / cv.width, 0.001) * dir[0] * mult;
    let dy = Math.max(1 / cv.height, 0.001) * dir[1] * mult;

    const items = selIdxs.map(i => comps()[i]).filter(Boolean);
    if (!items.length) return;
    items.forEach(c => {                      // borne collective : la sélection reste dans la cellule
        dx = Math.max(-(c.x || 0), Math.min(1 - (c.w || 0) - (c.x || 0), dx));
        dy = Math.max(-(c.y || 0), Math.min(1 - (c.h || 0) - (c.y || 0), dy));
    });
    if (!dx && !dy) return;                   // déjà contre le bord

    items.forEach(c => {
        c.x = r3((c.x || 0) + dx);
        c.y = r3((c.y || 0) + dy);
    });
    snapGuides = [];
    syncGeomFields();
    draw();
}

// ── Steppers numériques −/+ (repris du composer : mwEnhanceSteppers) ──
function pipStep(input, dir) {
    const step = parseFloat(input.step) || 1;
    const min = input.min !== '' ? parseFloat(input.min) : -Infinity;
    const max = input.max !== '' ? parseFloat(input.max) : Infinity;
    let v = parseFloat(input.value); if (isNaN(v)) v = 0;
    v = Math.min(max, Math.max(min, v + dir * step));
    v = parseFloat(v.toFixed(6));
    input.value = v;
    input.dispatchEvent(new Event('change', { bubbles: true }));
}
function _pipBindHold(btn, fn) {
    let to = null, iv = null;
    const stop = () => { clearTimeout(to); clearInterval(iv); to = iv = null; };
    btn.addEventListener('pointerdown', e => {
        e.preventDefault(); fn();
        to = setTimeout(() => { iv = setInterval(fn, 60); }, 350);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => btn.addEventListener(ev, stop));
}
function pipEnhanceSteppers(root) {
    (root || document).querySelectorAll('input[type="number"]').forEach(inp => {
        if (inp.closest('.num-stepper')) return;
        inp.classList.add('mw-num');
        const wrap = document.createElement('div');
        wrap.className = 'num-stepper';
        inp.parentNode.insertBefore(wrap, inp);
        const mk = (cls, txt) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'num-btn ' + cls; b.tabIndex = -1;
            b.textContent = txt; b.setAttribute('aria-hidden', 'true');
            return b;
        };
        const dec = mk('num-dec', '−'), inc = mk('num-inc', '+');
        wrap.append(dec, inp, inc);
        _pipBindHold(dec, () => pipStep(inp, -1));
        _pipBindHold(inc, () => pipStep(inp, +1));
    });
}

// ── panneau propriétés ──
function syncGeomFields() {
    const c = comps()[selIdx];
    if (!c) return;
    [['pip_gx', c.x], ['pip_gy', c.y], ['pip_gw', c.w], ['pip_gh', c.h]].forEach(([id, v]) => {
        const el = $(id);
        if (el && document.activeElement !== el) el.value = Math.round((v || 0) * 1000) / 10;
    });
}

function refreshProps() {
    updateClipButtons();
    const box = $('pip_props');
    if (!box) return;
    const c = comps()[selIdx];
    if (!c) {
        box.hidden = true;
        return;
    }
    box.hidden = false;
    const specs = FIELD_SPECS()[c.type] || [];
    let html = '<div class="mw-editor-row">' +
        '<div class="field"><label>' + esc(_t('settings.pip.component', 'Composant')) + '</label>' +
        '<b style="align-self:flex-start">' + esc(TYPE_LABELS()[c.type] || c.type) + '</b></div>' +
        ['x', 'y', 'w', 'h'].map(k => '<div class="field field-narrow"><label for="pip_g' + k + '">' +
            k.toUpperCase() + ' (%)</label><input type="number" id="pip_g' + k +
            '" step="0.1" min="0" max="100"></div>').join('') +
        '<button type="button" class="btn btn-red" id="pip_del_comp">' +
        esc(_t('settings.pip.remove_comp', 'Retirer')) + '</button></div>';
    html += '<div class="mw-editor-row mw-editor-row-divider">';
    specs.forEach((f, fi) => {
        const id = 'pip_f_' + fi;
        const title = f.help ? ' title="' + esc(f.help) + '"' : '';
        if (f.type === 'check') {
            html += '<label class="field field-inline" for="' + id + '"' + title +
                '><input type="checkbox" class="ios-toggle" id="' + id + '"' +
                (c[f.k] ? ' checked' : '') + '> <span>' + esc(f.label) + '</span></label>';
        } else if (f.type === 'textarea') {
            // Multiligne, comme le champ texte du composeur de mur : un libellé sur deux lignes
            // est courant, et un input d'une ligne rendait la saisie pénible.
            html += '<div class="field field-grow"' + title + '><label for="' + id + '">' +
                esc(f.label) + '</label><textarea id="' + id + '" rows="2" ' +
                'style="resize:vertical;font-family:inherit;min-height:2.2em" ' +
                'title="' + esc(_t('settings.pip.textarea_tip', 'Entrée = saut de ligne')) + '">' + esc(c[f.k] ?? '') + '</textarea></div>';
        } else if (f.type === 'vars') {
            // Insertion assistée : le <select> n'est pas un réglage, c'est un bouton d'insertion.
            // Il retombe sur sa première option après chaque usage pour rester réutilisable.
            html += '<div class="field"' + title + '><label for="' + id + '">' + esc(f.label) +
                '</label><select id="' + id + '" data-vars="1"></select></div>';
        } else if (f.type === 'tz') {
            // Volontairement VIDE ici : les ~490 fuseaux sont posés après coup par fillTzSelect
            // (liste serveur = tzdata réelle). Les inliner dans la chaîne HTML rendrait ce
            // template illisible et re-sérialiserait la liste à chaque changement de sélection.
            html += '<div class="field"' + title + '><label for="' + id + '">' + esc(f.label) +
                '</label><select id="' + id + '" data-tz="1"></select></div>';
        } else if (f.type === 'select') {
            html += '<div class="field"' + title + '><label for="' + id + '">' + esc(f.label) +
                '</label><select id="' + id + '">' +
                f.options.map(o => '<option value="' + esc(o[0]) + '"' +
                    (String(c[f.k] ?? '') === String(o[0]) ? ' selected' : '') + '>' + esc(o[1]) +
                    '</option>').join('') + '</select></div>';
        } else if (f.type === 'color') {
            html += '<div class="field field-color"><label for="' + id + '">' + esc(f.label) +
                '</label><input type="color" id="' + id + '" value="' +
                esc(c[f.k] || '#000000') + '"></div>' +
                (f.empty ? '<label class="field field-inline" for="' + id + '_off">' +
                    '<input type="checkbox" class="ios-toggle" id="' + id + '_off"' +
                    (!c[f.k] ? ' checked' : '') + '> <span>' +
                    esc(_t('settings.pip.no_bg', 'aucun')) + '</span></label>' : '');
        } else {
            html += '<div class="field field-narrow"><label for="' + id + '">' + esc(f.label) +
                '</label><input type="' + (f.type === 'number' ? 'number' : 'text') +
                '" id="' + id + '" value="' + esc(c[f.k] ?? '') + '"' +
                (f.min !== undefined ? ' min="' + f.min + '"' : '') +
                (f.max !== undefined ? ' max="' + f.max + '"' : '') + '></div>';
        }
    });
    html += '</div><div class="mw-editor-row mw-editor-row-divider">' +
        '<div class="field"><label for="pip_when">' + esc(_t('settings.pip.f_when', 'Visible')) +
        '</label><select id="pip_when">' + WHEN_OPTIONS().map(o => '<option value="' + o[0] + '"' +
            ((c.when || 'always') === o[0] ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('') +
        '</select></div>' +
        '<div class="field field-narrow"><label for="pip_min_w">' +
        esc(_t('settings.pip.f_min_w', 'Masquer sous (px de large)')) +
        '</label><input type="number" id="pip_min_w" min="0" max="4000" value="' +
        (c.min_w || 0) + '"></div></div>';
    box.innerHTML = html;
    // Champs POLICE : options (optgroups « Polices système » / « Bibliothèque ») posées par le
    // catalogue partagé — le <select> rendu ci-dessus est volontairement vide.
    specs.forEach((f, fi) => {
        if (!f.font || !window.BobiFonts) return;
        window.BobiFonts.fillSelect($('pip_f_' + fi), c.font || window.BobiFonts.defaultKey());
    });
    // Champs FUSEAU : même principe que les polices — le <select> rendu est vide, la liste vient
    // du serveur (tzdata réellement installée) et n'est chargée qu'une fois par session.
    specs.forEach((f, fi) => {
        if (f.type === 'tz') fillTzSelect($('pip_f_' + fi), c[f.k] || '');
    });
    // Champ « insérer une variable » : écrit %nom% à la position du curseur dans le champ texte
    // du même composant, puis déclenche son onchange pour que le modèle et l'aperçu suivent.
    specs.forEach((f, fi) => {
        if (f.type !== 'vars') return;
        const sel = $('pip_f_' + fi);
        if (!sel) return;
        const ti = specs.findIndex(x => x.k === 'text');
        loadTextVars().then(cat => {
            sel.innerHTML = '';
            sel.appendChild(new Option(_t('settings.pip.vars_pick', 'Insérer une variable…'), ''));
            const grp = (label, list) => {
                if (!list || !list.length) return;
                const og = document.createElement('optgroup');
                og.label = label;
                list.forEach(v => og.appendChild(new Option(v.label + '  (%' + v.name + '%)', v.name)));
                sel.appendChild(og);
            };
            grp(_t('settings.pip.vars_grp_src', 'Source de la fenêtre'), cat.source);
            grp(_t('settings.pip.vars_grp_sys', 'Ce conteneur'), cat.system);
            // Machine, liens, contrôleur : trois questions distinctes, trois groupes. Poussés par
            // l'orchestrateur (un conteneur ne voit que son propre cgroup).
            grp(_t('settings.pip.vars_grp_node', 'Nœud'), cat.noeud);
            grp(_t('settings.pip.vars_grp_rdma', 'RDMA'), cat.rdma);
            grp(_t('settings.pip.vars_grp_orch', 'Orchestrateur'), cat.orchestrateur);
        });
        sel.onchange = () => {
            const v = sel.value; sel.value = '';
            const inp = ti >= 0 ? $('pip_f_' + ti) : null;
            if (!v || !inp) return;
            const tok = '%' + v + '%';
            const p0 = inp.selectionStart, p1 = inp.selectionEnd;
            inp.value = (p0 === null) ? (inp.value + tok)
                                      : inp.value.slice(0, p0) + tok + inp.value.slice(p1);
            if (p0 !== null) { const np = p0 + tok.length; inp.setSelectionRange(np, np); }
            inp.focus();
            if (inp.onchange) inp.onchange();
        };
    });
    syncGeomFields();
    // bind
    $('pip_del_comp').onclick = removeComp;
    ['x', 'y', 'w', 'h'].forEach(k => {
        $('pip_g' + k).onchange = () => {
            const v = Math.max(0, Math.min(100, parseFloat($('pip_g' + k).value) || 0)) / 100;
            c[k] = Math.round(v * 1000) / 1000;
            // Vidéo verrouillée 16:9 en pixels : W et H couplés (modifier l'un ajuste l'autre).
            if (c.type === 'video' && (k === 'w' || k === 'h')) {
                c.w = (k === 'h') ? c.h / _videoK() : c[k];
                _enforceVideoRatio(c);
            }
            syncGeomFields();
            draw();
        };
    });
    specs.forEach((f, fi) => {
        const el = $('pip_f_' + fi);
        if (!el) return;
        // `vars` n'est pas un réglage : c'est un bouton d'insertion, son gestionnaire est posé
        // plus haut. Le lier ici l'écraserait ET persisterait une clé `_vars` parasite.
        if (f.type === 'vars') return;
        el.onchange = () => {
            if (f.type === 'check') c[f.k] = el.checked;
            else if (f.type === 'number' || f.num) c[f.k] = parseFloat(el.value) || 0;
            else c[f.k] = el.value;
            draw();
        };
        if (f.type === 'color' && f.empty) {
            const off = $('pip_f_' + fi + '_off');
            if (off) off.onchange = () => { c[f.k] = off.checked ? '' : el.value; draw(); };
            el.onchange = () => {
                const off2 = $('pip_f_' + fi + '_off');
                if (off2) off2.checked = false;
                c[f.k] = el.value; draw();
            };
        }
    });
    $('pip_when').onchange = () => { c.when = $('pip_when').value; draw(); };
    $('pip_min_w').onchange = () => { c.min_w = parseInt($('pip_min_w').value) || 0; draw(); };
    pipEnhanceSteppers(box);
}

// ── bibliothèque : galerie de vignettes ──
// Un modèle est un objet VISUEL — la liste texte historique forçait à chercher à l'œil ce qu'on
// ne voyait pas. La galerie DESSINE chaque modèle en miniature (vraie composition, pas un bloc de
// couleur) pour qu'il se reconnaisse d'un coup d'œil. Recherche (nom + tag), filtre par tag, tri
// (nom / modifié récemment — DÉFAUT « récemment » : on rouvre le plus souvent le modèle sur
// lequel on vient de travailler, comme un sélecteur de fichiers récents).
let libSearch = '';
let libSort = 'recent';      // 'recent' | 'name'
let libTag = 'all';

async function loadLib() {
    try {
        const r = await fetch('/api/pip_templates');
        lib = r.ok ? await r.json() : [];
    } catch (e) { lib = []; }
    renderLib();
}

// requestIdleCallback (repli setTimeout) : les vignettes se dessinent hors du chemin critique,
// jamais synchrone avec une frappe clavier — cf. renderLib/applyLibFilters plus bas : le filtrage
// texte ne touche QUE l'affichage (hidden + order CSS), jamais le canvas déjà rendu (cache sur
// `canvas.dataset.rendered`).
function _pipIdle(fn) {
    if (typeof window.requestIdleCallback === 'function') window.requestIdleCallback(fn, { timeout: 500 });
    else setTimeout(fn, 0);
}

// Vignette = réutilisation STRICTE du moteur de rendu de l'éditeur (drawMock, ~ligne 524) : même
// letterbox centré au format libre du modèle que l'ancien aperçu, mais la composition RÉELLE
// (couleurs, texte, cadres, VU-mètres…) au lieu de blocs de couleur par type. drawMock lit trois
// globales de simulation (simTally/simSignal/cellW) : neutralisées le temps du rendu puis
// restaurées — sans risque d'interférence avec l'aperçu d'édition en cours, l'appel est
// synchrone (JS mono-thread, rien ne s'exécute entre la neutralisation et la restauration).
function renderTemplateThumb(cv, config) {
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cv.width, cv.height);
    const a = (config && parseFloat(config.aspect) > 0.1 && parseFloat(config.aspect) < 10)
        ? parseFloat(config.aspect) : 16 / 9;
    let rw = cv.width, rh = rw / a;
    if (rh > cv.height) { rh = cv.height; rw = rh * a; }
    const ox = (cv.width - rw) / 2, oy = (cv.height - rh) / 2;
    const list = (config && config.components) || [];
    const order = list.map((c, i) => i).sort((x, y) =>
        (list[x].type === 'video' ? 0 : 1) - (list[y].type === 'video' ? 0 : 1));
    const savedTally = simTally, savedSignal = simSignal, savedCellW = cellW;
    simTally = 'off'; simSignal = ''; cellW = 100000;   // neutre : jamais masqué par min_w
    order.forEach(i => {
        const c = list[i];
        const r = { x: ox + (c.x || 0) * rw, y: oy + (c.y || 0) * rh,
                    w: Math.max(2, (c.w || 0) * rw), h: Math.max(2, (c.h || 0) * rh) };
        drawMock(ctx, c, r);
    });
    simTally = savedTally; simSignal = savedSignal; cellW = savedCellW;
    ctx.strokeStyle = 'rgba(255,255,255,0.18)';
    ctx.strokeRect(ox + 0.5, oy + 0.5, rw - 1, rh - 1);
}

function _isBuiltinId(id) { return String(id).startsWith('builtin:'); }

// Une carte par modèle. Construite UNE FOIS par cycle de chargement (loadLib/save/delete/import) ;
// la recherche/tri/tag ne reconstruit jamais le DOM ni les canvases (cf. applyLibFilters).
function _pipLibCard(l, builtin) {
    const card = document.createElement('div');
    card.className = 'pip-lib-card';
    card.dataset.id = l.id;
    card.dataset.name = (l.name || '').toLowerCase();
    card.dataset.tags = (l.tags || []).join('|').toLowerCase();
    card.dataset.updated = l.updated_at || '';
    card.setAttribute('role', 'listitem');
    card.tabIndex = 0;
    card.title = _t('settings.pip.open', 'Ouvrir') + ' — ' + (l.name || '');

    const cv = document.createElement('canvas');
    cv.width = 168; cv.height = 96;
    cv.className = 'pip-lib-thumb';
    cv.setAttribute('aria-hidden', 'true');
    card.appendChild(cv);

    const meta = document.createElement('div');
    meta.className = 'pip-lib-meta';
    const nameEl = document.createElement('b');
    nameEl.className = 'pip-lib-name';
    nameEl.title = l.name || '';
    nameEl.textContent = l.name || '';
    meta.appendChild(nameEl);
    if (builtin) {
        const badge = document.createElement('span');
        badge.className = 'pip-lib-badge-sys';
        badge.textContent = _t('settings.pip.builtin', 'Système');
        badge.title = _t('settings.pip.builtin_title',
            "Modèle d'usine : pas de tags, pas de date de modification (référence fixe du produit).");
        meta.appendChild(badge);
    }
    card.appendChild(meta);

    if (!builtin && (l.tags || []).length) {
        const tagsRow = document.createElement('div');
        tagsRow.className = 'pip-lib-tagrow';
        (l.tags || []).forEach(tg => {
            const s = document.createElement('span');
            s.className = 'pip-lib-tagchip';
            s.textContent = tg;
            tagsRow.appendChild(s);
        });
        card.appendChild(tagsRow);
    }

    const actions = document.createElement('div');
    actions.className = 'pip-lib-actions';
    const mk = (act, label, cls) => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn ' + (cls || ''); b.dataset.act = act;
        b.textContent = label; b.title = label;
        return b;
    };
    actions.appendChild(mk('dup', _t('settings.pip.duplicate', 'Dupliquer')));
    actions.appendChild(mk('export', _t('settings.pip.export', 'Exporter')));
    if (!builtin) actions.appendChild(mk('del', _t('settings.pip.delete', 'Supprimer'), 'btn-red'));
    card.appendChild(actions);

    card.addEventListener('click', e => { if (!e.target.closest('.pip-lib-actions')) libAction(l.id, 'open'); });
    card.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); libAction(l.id, 'open'); }
    });
    actions.querySelectorAll('button[data-act]').forEach(b => {
        b.onclick = e => { e.stopPropagation(); libAction(l.id, b.dataset.act); };
    });

    _pipIdle(() => {
        if (cv.dataset.rendered) return;
        renderTemplateThumb(cv, l.config);
        cv.dataset.rendered = '1';
    });
    return card;
}

function renderLib() {
    const grid = $('pip_lib_grid');
    if (!grid) return;
    grid.innerHTML = '';
    if (!lib.length) {
        grid.innerHTML = '<p class="meta pip-lib-empty">' + esc(_t('settings.pip.lib_empty', 'Aucun modèle.')) + '</p>';
        renderLibTags();
        return;
    }
    const users = lib.filter(l => !_isBuiltinId(l.id));
    const builtins = lib.filter(l => _isBuiltinId(l.id));
    const frag = document.createDocumentFragment();
    users.forEach(l => frag.appendChild(_pipLibCard(l, false)));
    if (builtins.length) {
        const h = document.createElement('h3');
        h.className = 'pip-lib-group-title';
        h.textContent = _t('settings.pip.builtin_group', "Modèles d'usine");
        frag.appendChild(h);
        builtins.forEach(l => frag.appendChild(_pipLibCard(l, true)));
    }
    const empty = document.createElement('p');
    empty.className = 'meta pip-lib-empty';
    empty.id = 'pip_lib_grid_empty';
    empty.hidden = true;
    empty.textContent = _t('settings.pip.lib_no_match', 'Aucun modèle ne correspond à la recherche.');
    frag.appendChild(empty);
    grid.appendChild(frag);
    renderLibTags();
    applyLibFilters();
}

// Chips de tags (« Tous » + tags existants, modèles utilisateur uniquement — les modèles
// d'usine n'ont pas de tags). Masqué si aucun tag n'existe encore : pas de rangée de contrôles
// qui ne filtrerait rien.
function renderLibTags() {
    const box = $('pip_lib_tags');
    if (!box) return;
    const tagSet = new Set();
    lib.forEach(l => { if (!_isBuiltinId(l.id)) (l.tags || []).forEach(tg => tagSet.add(tg)); });
    const tags = Array.from(tagSet).sort((a, b) => a.localeCompare(b, 'fr'));
    if (!tags.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    const mk = (val, label) => '<button type="button" class="filter-chip' + (libTag === val ? ' active' : '') +
        '" data-tag="' + esc(val) + '">' + esc(label) + '</button>';
    box.innerHTML = mk('all', _t('settings.pip.tag_all', 'Tous')) + tags.map(tg => mk(tg, tg)).join('');
    box.querySelectorAll('button[data-tag]').forEach(b => {
        b.onclick = () => { libTag = b.dataset.tag; renderLibTags(); applyLibFilters(); };
    });
}

// Filtrage/tri PUR AFFICHAGE : jamais de reconstruction DOM ni de redessin de canvas ici — sinon
// chaque frappe dans le champ de recherche redessinerait toute la galerie (coût perf inutile, les
// vignettes ne changent pas). Les modèles d'usine restent un groupe à part, toujours trié par nom
// (pas de date à leur appliquer).
function applyLibFilters() {
    const grid = $('pip_lib_grid');
    if (!grid) return;
    const q = libSearch.trim().toLowerCase();
    const cards = Array.from(grid.querySelectorAll('.pip-lib-card'));
    let visibleUsers = 0, visibleBuiltins = 0;
    cards.forEach(card => {
        const builtin = _isBuiltinId(card.dataset.id);
        const matchesQ = !q || card.dataset.name.includes(q) ||
            card.dataset.tags.split('|').some(tg => tg.includes(q));
        const matchesTag = libTag === 'all' || card.dataset.tags.split('|').includes(libTag);
        const show = matchesQ && matchesTag;
        card.hidden = !show;
        if (show) { if (builtin) visibleBuiltins++; else visibleUsers++; }
    });
    const users = cards.filter(c => !_isBuiltinId(c.dataset.id));
    const cmp = libSort === 'name'
        ? (a, b) => a.dataset.name.localeCompare(b.dataset.name, 'fr')
        : (a, b) => b.dataset.updated.localeCompare(a.dataset.updated);
    users.sort(cmp).forEach((card, i) => { card.style.order = i; });
    const builtins = cards.filter(c => _isBuiltinId(c.dataset.id))
        .sort((a, b) => a.dataset.name.localeCompare(b.dataset.name, 'fr'));
    const title = grid.querySelector('.pip-lib-group-title');
    if (title) { title.style.order = users.length + 1; title.hidden = visibleBuiltins === 0; }
    builtins.forEach((card, i) => { card.style.order = users.length + 2 + i; });
    const noMatch = $('pip_lib_grid_empty');
    if (noMatch) noMatch.hidden = (visibleUsers + visibleBuiltins) > 0 || !lib.length;
}

// ── éditeur de tags du modèle en cours ──
function renderTagEditor() {
    const box = $('pip_tag_chips');
    if (!box) return;
    const tags = (cur && cur.tags) || [];
    box.innerHTML = tags.map((tg, i) => '<span class="pip-tag-chip">' + esc(tg) +
        '<button type="button" class="pip-tag-chip-del" data-i="' + i + '" aria-label="' +
        esc(_t('settings.pip.tag_remove', 'Retirer le tag')) + ' ' + esc(tg) + '">×</button></span>').join('');
    box.querySelectorAll('button[data-i]').forEach(b => {
        b.onclick = () => {
            cur.tags.splice(parseInt(b.dataset.i, 10), 1);
            renderTagEditor();
        };
    });
}

function addTagFromInput() {
    const inp = $('pip_tag_input');
    if (!inp || !cur) return;
    const v = inp.value.trim();
    if (!v) return;
    cur.tags = cur.tags || [];
    if (!cur.tags.includes(v)) cur.tags.push(v);
    inp.value = '';
    renderTagEditor();
}

function libAction(id, act) {
    const l = lib.find(x => String(x.id) === String(id));
    if (!l) return;
    if (act === 'open' || act === 'dup') {
        cur = { id: act === 'dup' ? null : l.id,
                name: act === 'dup' ? (l.name + ' (copie)') : l.name,
                builtin: act === 'open' && _isBuiltinId(l.id),
                tags: _isBuiltinId(l.id) ? [] : (l.tags || []).slice(),
                config: JSON.parse(JSON.stringify(l.config || { components: [] })) };
        (cur.config.components || []).forEach(_enforceVideoRatio);   // vidéo toujours 16:9
        _setSel([]);
        refreshAll();
    } else if (act === 'export') {
        // Export par l'API (/api/pip_templates/<id>/export) : le fichier EMBARQUE les polices
        // utilisées ({name, family, ext, sha256, ttf_b64}) — un modèle exporté reste fidèle sur
        // une autre instance, où l'import les dédupliquera par HASH. Les modèles d'usine
        // (id « builtin:… ») n'ont pas de route d'export : repli sur la config seule (ils
        // n'utilisent que les polices de l'image runtime).
        if (_isBuiltinId(l.id)) {
            _download(l.name, { name: l.name, tags: l.tags || [], config: l.config, fonts: [] });
            return;
        }
        fetch('/api/pip_templates/' + l.id + '/export')
            .then(r => r.ok ? r.json() : Promise.reject(r.status))
            .then(j => {
                _download(l.name, j);
                const n = (j.fonts || []).length;
                flash(_t('settings.pip.flash_exported', 'Modèle exporté (polices embarquées).')
                      + (n ? ' (' + n + ')' : ''), 'ok');
            })
            .catch(() => flash(_t('settings.pip.flash_export_failed', "Échec de l'export du modèle."), 'error'));
    } else if (act === 'del') {
        if (!confirm(_t('settings.pip.confirm_delete', 'Supprimer ce modèle ?') + ' (' + l.name + ')')) return;
        fetch('/api/pip_templates/' + l.id, { method: 'DELETE' }).then(() => {
            if (cur && String(cur.id) === String(l.id)) { cur = null; _setSel([]); refreshAll(); }
            loadLib();
        });
    }
}

function _download(name, obj) {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (name || 'pip').replace(/[^\w.à-üÀ-Ü-]+/g, '_') + '.pip.json';
    a.click();
    URL.revokeObjectURL(a.href);
}

// Avertissements de police renvoyés par le POST (import d'un modèle d'une autre instance) :
// `missing_font` = police référencée, ni en bibliothèque ni embarquée → le rendu retombera sur
// DejaVu. JAMAIS silencieux (règle projet) : toast d'avertissement + rechargement du catalogue
// (des polices ont pu être ajoutées à la bibliothèque par l'import).
function _reportFontWarnings(warnings) {
    const w = warnings || [];
    if (!w.length) return;
    const keys = w.map(x => x.key || x.name || x.code).filter(Boolean).join(', ');
    flash(_t('settings.pip.font_warn', "Police(s) manquante(s) à l'import — repli DejaVu : {n}")
          .replace('{n}', keys || w.length), 'warn');
}

async function saveTemplate() {
    if (!cur) return;
    const name = ($('pip_name').value || '').trim();
    if (!name) { flash(_t('settings.pip.flash_name', 'Donnez un nom au modèle.'), 'warn'); return; }
    if (cur.builtin) {   // un modèle d'usine s'enregistre comme copie
        cur = Object.assign({}, cur, { id: null, builtin: false });
    }
    cur.name = name;
    cur.tags = cur.tags || [];
    let r = null;
    try {
        r = await fetch('/api/pip_templates', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            // `fonts` = polices embarquées d'un modèle IMPORTÉ (repassées telles quelles au POST) :
            // le serveur les absorbe en bibliothèque (dédup par HASH) et réécrit les clés `lib:*`
            // de la config vers les clés locales, puis renvoie `font_warnings`.
            body: JSON.stringify({ id: cur.id, name, config: cur.config, tags: cur.tags,
                                   fonts: cur.fonts || [] }) });
    } catch (e) {}
    if (r && r.ok) {
        const j = await r.json();
        const hadFonts = !!(cur.fonts && cur.fonts.length);
        cur.id = j.id;
        cur.fonts = [];               // absorbées : elles ne doivent pas être re-postées
        flash(_t('settings.pip.flash_saved', 'Modèle enregistré.'), 'ok');
        _reportFontWarnings(j.font_warnings);
        if (window.BobiFonts) window.BobiFonts.load(true).then(() => { refreshProps(); draw(); });
        refreshAll();
        await loadLib();
        if (hadFonts) {
            // Import : le serveur a RÉÉCRIT les clés `lib:*` de la config vers les clés locales
            // (dédup par hash). On relit la config persistée, sinon l'éditeur continuerait à
            // travailler sur les clés de l'instance d'origine et les réécrirait au prochain save.
            const saved = lib.find(x => String(x.id) === String(j.id));
            if (saved && saved.config) {
                cur.config = JSON.parse(JSON.stringify(saved.config));
                refreshAll();
            }
        }
    } else {
        flash(_t('settings.pip.flash_save_failed', 'Échec de l’enregistrement.'), 'error');
    }
}

function importTemplate(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
        input.value = '';
        let data = null;
        try { data = JSON.parse(reader.result); } catch (e) {}
        if (!data || !data.config || !Array.isArray(data.config.components)) {
            flash(_t('settings.pip.flash_invalid_file', 'Fichier de modèle invalide.'), 'error');
            return;
        }
        cur = { id: null, name: data.name || '', builtin: false,
                tags: Array.isArray(data.tags) ? data.tags.slice() : [], config: data.config,
                // Polices embarquées du fichier : conservées jusqu'à l'ENREGISTREMENT, qui les
                // repasse au POST (absorption + dédup par hash côté serveur, cf. saveTemplate).
                fonts: Array.isArray(data.fonts) ? data.fonts : [] };
        (cur.config.components || []).forEach(_enforceVideoRatio);   // vidéo toujours 16:9
        _setSel([]);
        refreshAll();
        // Le modèle référence une police que le fichier n'embarque pas et que la bibliothèque
        // locale ne connaît pas → prévenir TOUT DE SUITE (le rendu retombera sur DejaVu), sans
        // attendre l'enregistrement.
        if (window.BobiFonts) {
            window.BobiFonts.load().then(({ fonts }) => {
                const known = new Set(fonts.map(f => f.key));
                (cur.fonts || []).forEach(f => f.sha256 && known.add('lib:' + f.sha256.slice(0, 16)));
                const missing = (cur.config.components || [])
                    .map(c => c.font)
                    .filter(k => window.BobiFonts.isLib(k) && !known.has(k));
                _reportFontWarnings([...new Set(missing)].map(k => ({ code: 'missing_font', key: k })));
            });
        }
    };
    reader.readAsText(file);
}

// Toast — réutilise le composant global .wire-toast (base.css / static/scripts.js
// showWireToast), en haut à droite, plutôt qu'un toast maison bas-centré sans max-width qui
// s'étalait en barre pour un message un peu long. kind : 'ok' (défaut, confirmation) |
// 'warn' (garde-fou, sélection manquante…) | 'error' (échec).
function flash(msg, kind) {
    if (typeof window.showWireToast === 'function') { window.showWireToast(msg, kind || 'ok'); return; }
    // Repli si scripts.js n'est pas chargé (page hors contexte) : même classe CSS globale.
    const t = document.createElement('div');
    t.className = 'wire-toast wire-toast-' + (kind || 'ok');
    t.setAttribute('role', 'status');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.classList.add('wire-toast-out'); setTimeout(() => t.remove(), 400); }, 4200);
}

function refreshAll() {
    const ed = $('pip_edit_zone');
    const empty = $('pip_editor_empty');
    if (ed) ed.hidden = !cur;
    if (empty) empty.hidden = !!cur;
    const sec = $('pip_editor_section');
    if (sec) sec.classList.toggle('empty', !cur);
    if (cur) {
        $('pip_name').value = cur.name || '';
        const note = $('pip_builtin_note');
        if (note) note.hidden = !cur.builtin;
        cur.tags = cur.tags || [];
        renderTagEditor();
        syncAspectField();
        applySimCell();           // la résolution du canvas suit l'aspect du modèle ouvert
        _resizeCanvasDisplay();   // le clientWidth n'existe qu'une fois la zone affichée
    } else {
        const chips = $('pip_tag_chips');
        if (chips) chips.innerHTML = '';
    }
    const tagInput = $('pip_tag_input');
    if (tagInput) {
        tagInput.disabled = !cur;
        tagInput.title = cur ? '' : _t('settings.pip.tag_needs_template',
            'Ouvrez ou créez un modèle pour lui donner des tags.');
    }
    refreshProps();
    draw();
    renderLib();
}

// SVG des outils — mêmes pictos que le composer multiview (control.html).
const _SVG = {
    align_left: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="4" y="3" width="9.5" height="3.6" rx="1"/><rect x="4" y="9.4" width="6" height="3.6" rx="1"/></svg>',
    align_hcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="12" height="3.6" rx="1"/><rect x="4" y="9.4" width="8" height="3.6" rx="1"/><rect x="7.2" y="1" width="1.6" height="14" rx="0.8"/></svg>',
    align_right: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="2.5" y="3" width="9.5" height="3.6" rx="1"/><rect x="6" y="9.4" width="6" height="3.6" rx="1"/></svg>',
    align_top: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="3" y="4" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="4" width="3.6" height="6" rx="1"/></svg>',
    align_vcenter: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="3.6" height="12" rx="1"/><rect x="9.4" y="4" width="3.6" height="8" rx="1"/><rect x="1" y="7.2" width="14" height="1.6" rx="0.8"/></svg>',
    align_bottom: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="3" y="2.5" width="3.6" height="9.5" rx="1"/><rect x="9.4" y="6" width="3.6" height="6" rx="1"/></svg>',
    size_w: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="2" width="10" height="4.5" rx="1"/><rect x="3" y="9.5" width="10" height="4.5" rx="1" fill-opacity="0.55"/></svg>',
    size_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2" y="3" width="4.5" height="10" rx="1"/><rect x="9.5" y="3" width="4.5" height="10" rx="1" fill-opacity="0.55"/></svg>',
    size_both: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1.5" y="1.5" width="8" height="8" rx="1" fill-opacity="0.55"/><rect x="6.5" y="6.5" width="8" height="8" rx="1"/></svg>',
    distribute_h: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="1.6" height="14" rx="0.8"/><rect x="13.4" y="1" width="1.6" height="14" rx="0.8"/><rect x="6" y="4" width="4" height="8" rx="1"/></svg>',
    distribute_v: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="14" height="1.6" rx="0.8"/><rect x="1" y="13.4" width="14" height="1.6" rx="0.8"/><rect x="4" y="6" width="8" height="4" rx="1"/></svg>',
    // Copier/coller réglages — icônes IDENTIQUES au composer multiview (control.html ~100-106).
    copy_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="5.5" y="1.5" width="9" height="9" rx="1.5" fill-opacity="0.45"/><rect x="1.5" y="5.5" width="9" height="9" rx="1.5"/></svg>',
    paste_settings: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="12" rx="1.5" fill-opacity="0.45"/><rect x="5.5" y="1" width="5" height="3" rx="1"/><rect x="5" y="6.5" width="6" height="6" rx="1"/></svg>',
};

function _toolGroup(label, defs) {
    const grp = document.createElement('div');
    grp.className = 'tool-group';
    grp.innerHTML = '<span class="tool-group-label" aria-hidden="true">' + esc(label) + '</span>';
    const row = document.createElement('div');
    row.className = 'tool-row';
    row.setAttribute('role', 'group');
    row.setAttribute('aria-label', label);
    defs.forEach(([svg, title, fn]) => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'tool-btn';
        b.innerHTML = svg;
        b.title = title; b.setAttribute('aria-label', title);
        b.onclick = fn;
        row.appendChild(b);
    });
    grp.appendChild(row);
    return grp;
}

function initUI() {
    const root = $('pip-editor-root');
    if (!root || root.dataset.ready) return;
    root.dataset.ready = '1';
    // Feuille de style du composer multiview (parité visuelle : .mw-editor, toolbar, steppers…).
    if (!document.querySelector('link[data-pip-mwcss]')) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = '/api/plugins/multiview/ui/extra_css';
        link.dataset.pipMwcss = '1';
        document.head.appendChild(link);
    }
    const L = (k, fb) => esc(_t(k, fb));
    root.innerHTML = `
<style>
/* La galerie a besoin d'assez de largeur pour QUE 3 colonnes de vignettes restent lisibles
   (voir « Le fond du problème » du chantier : un modèle est un objet visuel, pas une ligne de
   texte) — colonne élargie à 400px (vs 300px historique). Sous 1100px l'aside repasse en pleine
   largeur (empilé sous l'éditeur) : la galerie y gagne d'ailleurs en confort. */
.pip-compose { display:grid; grid-template-columns: minmax(0, 1fr) 300px; gap: var(--space-4); align-items:start; }
@media (max-width: 1100px) { .pip-compose { grid-template-columns: 1fr; } }
/* Espace de composition CENTRÉ dans la colonne : le wrap du composer est en width:fit-content
   (il colle au canvas) → le centrage se fait par marges auto SUR LE WRAP, et la taille
   d'affichage du canvas est fixée en px par _resizeCanvasDisplay (comme resizeCanvas du
   composer) — la RÉSOLUTION interne, elle, suit la taille de tuile simulée. */
.pip-compose .mw-canvas-wrap { margin: 0 auto; }
.pip-compose .mw-canvas-wrap canvas { background:#000; }
.pip-compose .mw-hint, .pip-compose .mw-editor-row { justify-content:center; }
.pip-compose .mw-hint { text-align:center; }
/* Sélecteur de REFERENCE d'alignement. La classe .tool-select est definie dans multiview.css,
   qui n'est PAS chargee par la page Reglages : sans cette regle locale le selecteur serait au
   style brut du navigateur au milieu d'une barre d'outils stylee.
   ATTENTION : ce bloc vit DANS le template literal assigne a root.innerHTML. N'y mettre AUCUN
   accent grave ni interpolation : ils terminent la chaine, l'editeur reste vide, et ni node
   --check ni le chargement du module ne signalent quoi que ce soit. */
.pip-compose .tool-select, #pip_toolbar .tool-select {
  height: 26px; padding: 0 4px; margin-left: 6px; font-size: 0.8em;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--bg-soft); color: var(--text); cursor: pointer; }
#pip_toolbar .tool-select:hover { background: var(--bg-hover); color: var(--text-strong); }
/* Éditeur de tags du modèle en cours (section Nom du modèle). */
.pip-tag-editor { flex-direction:column; align-items:stretch; gap:6px; }
.pip-tag-chips { display:flex; flex-wrap:wrap; gap:4px; min-height:22px; }
.pip-tag-chip { display:inline-flex; align-items:center; gap:4px; font-size:0.78em;
  padding:2px 4px 2px 9px; border-radius:999px; background: var(--accent-soft); color: var(--accent); }
.pip-tag-chip-del { border:none; background:transparent; color:inherit; cursor:pointer;
  font-size:1.1em; line-height:1; padding:0 3px; opacity:0.75; }
.pip-tag-chip-del:hover { opacity:1; }
/* Galerie de vignettes (bibliothèque). */
.pip-lib-toolbar { display:flex; gap: var(--space-2); margin-bottom: var(--space-2); }
.pip-lib-toolbar .filter-search { flex:1 1 auto; min-width:0; }
.pip-lib-toolbar .filter-select { flex:none; }
.pip-lib-tags { display:flex; flex-wrap:wrap; gap:4px; margin-bottom: var(--space-2); }
/* La bibliothèque vit SOUS l'éditeur, en PLEINE LARGEUR (elle était à l'étroit dans l'aside :
   3 colonnes de vignettes dans 400 px = illisible). Le nombre de colonnes s'adapte tout seul à
   la place disponible — sur un écran large on voit une vingtaine de modèles sans défiler. */
.pip-lib-full { margin-top: var(--space-4); }
.pip-lib-grid { display:grid; gap: var(--space-2);
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); }
.pip-lib-group-title { grid-column: 1 / -1; margin: var(--space-2) 0 0; padding-top: var(--space-2);
  border-top: 1px solid var(--border-soft); font-size:0.78em; font-weight:600; letter-spacing:0.5px;
  text-transform:uppercase; color: var(--text-muted); }
.pip-lib-card { border:1px solid var(--border); border-radius: var(--radius-small);
  padding:6px; background: var(--bg-input); cursor:pointer; display:flex; flex-direction:column;
  gap:4px; transition: border-color 0.12s; }
.pip-lib-card:hover { border-color: var(--accent); }
.pip-lib-card:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.pip-lib-thumb { width:100%; height:auto; aspect-ratio:16/9; display:block; border-radius:3px; background:#000; }
.pip-lib-meta { display:flex; align-items:center; justify-content:space-between; gap:4px; }
.pip-lib-name { font-size:0.78em; font-weight:500; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; min-width:0; }
.pip-lib-badge-sys { flex:none; font-size:0.6em; padding:1px 5px; border-radius:3px;
  letter-spacing:0.4px; text-transform:uppercase; font-weight:600; color: var(--text-muted);
  border:1px dashed var(--border); }
.pip-lib-tagrow { display:flex; flex-wrap:wrap; gap:3px; }
.pip-lib-tagchip { font-size:0.66em; padding:1px 6px; border-radius:999px; background: var(--bg-elev);
  border:1px solid var(--border-soft); color: var(--text-muted); }
.pip-lib-actions { display:flex; gap:3px; }
.pip-lib-actions .btn { flex:1; padding:3px 4px; font-size:0.7em; }
.pip-lib-empty { grid-column: 1 / -1; color: var(--text-muted); font-size:0.85em; }
</style>
<div class="pip-compose">
  <section class="mw-editor empty" id="pip_editor_section">
    <div id="pip_editor_empty">
      <h2 class="empty-state-title">${L('settings.pip.no_template_title', 'Aucun modèle ouvert')}</h2>
      <p class="empty-state-msg">${L('settings.pip.hint',
        'Ouvrez un modèle de la bibliothèque ou créez-en un nouveau. Les modèles s’affectent ensuite aux fenêtres dans le composer multiview.')}</p>
    </div>
    <div id="pip_edit_zone" hidden>
      <div class="mw-insert" role="group">
        <span class="lbl">${L('settings.pip.add_label', 'Ajouter :')}</span>
        <span id="pip_palette" style="display:contents"></span>
      </div>
      <div class="mw-toolbar" id="pip_toolbar" role="toolbar"></div>
      <div class="mw-canvas-wrap">
        <canvas id="pip_canvas" width="640" height="360" aria-label="${L('settings.pip.title', 'Modèles de PiP')}"></canvas>
      </div>
      <p class="mw-hint">${L('settings.pip.select_hint',
        'Maj + clic pour multi-sélectionner. Référence des outils = dernière sélectionnée (cadre blanc). Suppr retire la sélection. La fenêtre vidéo reste toujours en 16:9.')}</p>
      <div class="mw-editor-row">
        <div class="field field-narrow"><label for="pip_aspect" title="${L('settings.pip.aspect_help',
            'Ratio L:H de la cellule cible (ex. 16:9, 4:3, 1.85). Les modèles historiques sont en 16:9.')}">${
            L('settings.pip.aspect', 'Format du modèle')}</label>
          <input type="text" id="pip_aspect" autocomplete="off" style="max-width:90px"></div>
        <div class="field"><label>&nbsp;</label>
          <button type="button" class="btn" id="pip_trim" title="${L('settings.pip.trim_help',
            'Supprime l’espace vide sur les 4 côtés (le format du modèle devient la boîte englobante des composants).')}">${
            L('settings.pip.trim', 'Rogner l’espace inutilisé')}</button></div>
        <div class="field"><label for="pip_sim_cell">${L('settings.pip.sim_cell', 'Taille de tuile simulée')}</label>
          <select id="pip_sim_cell">
            <option value="1920">1920 px</option>
            <option value="960">960 px (2×2)</option>
            <option value="640" selected>640 px (3×3)</option>
            <option value="480">480 px (4×4)</option>
            <option value="320">320 px</option>
          </select></div>
        <div class="field"><label for="pip_sim_tally">${L('settings.pip.sim_tally', 'Simulation tally')}</label>
          <select id="pip_sim_tally">
            <option value="off">${L('settings.pip.tally_off', 'Éteint')}</option>
            <option value="red">${L('settings.pip.tally_red', 'Rouge')}</option>
            <option value="green">${L('settings.pip.tally_green', 'Vert')}</option>
          </select></div>
        <div class="field"><label for="pip_sim_signal">${L('settings.pip.sim_signal', 'Simulation signal')}</label>
          <select id="pip_sim_signal">
            <option value="">${L('settings.pip.signal_ok', 'Présent')}</option>
            <option value="nosignal">${L('settings.pip.signal_lost', 'Absent')}</option>
            <option value="freeze">${L('settings.pip.signal_freeze', 'Figé')}</option>
          </select></div>
      </div>
      <div id="pip_props" class="mw-entry-panel" hidden></div>
    </div>
  </section>
  <aside class="mw-side">
    <section class="mw-settings" aria-label="${L('settings.pip.name', 'Nom du modèle')}">
      <h2>${L('settings.pip.title', 'Modèles de PiP')}</h2>
      <div class="row">
        <div class="field"><label for="pip_name">${L('settings.pip.name', 'Nom du modèle')}</label>
          <input type="text" id="pip_name" autocomplete="off"></div>
      </div>
      <div class="row pip-tag-editor">
        <label for="pip_tag_input">${L('settings.pip.tags', 'Tags')}</label>
        <div class="pip-tag-chips" id="pip_tag_chips"></div>
        <input type="text" id="pip_tag_input" autocomplete="off"
               placeholder="${L('settings.pip.tag_add_placeholder', 'Ajouter un tag… (Entrée)')}">
      </div>
      <div class="row">
        <button type="button" class="btn btn-green" id="pip_save">${L('settings.pip.save', 'Enregistrer')}</button>
        <button type="button" class="btn" id="pip_new">${L('settings.pip.new', '+ Nouveau modèle')}</button>
      </div>
      <div class="row"><span class="meta" id="pip_builtin_note" hidden>${L('settings.pip.builtin_note',
        "Modèle d'usine : l'enregistrement crée une copie modifiable.")}</span></div>
    </section>
  </aside>
</div>
<section class="layout-list pip-lib-full">
      <h2>${L('settings.pip.library', 'Bibliothèque')}</h2>
      <div class="pip-lib-toolbar">
        <input type="search" id="pip_lib_search" class="filter-search" autocomplete="off"
               placeholder="${L('settings.pip.search_placeholder', 'Rechercher un modèle ou un tag…')}"
               aria-label="${L('settings.pip.search_placeholder', 'Rechercher un modèle ou un tag…')}">
        <select id="pip_lib_sort" class="filter-select" aria-label="${L('settings.pip.sort_aria', 'Trier la bibliothèque')}">
          <option value="recent">${L('settings.pip.sort_recent', 'Modifié récemment')}</option>
          <option value="name">${L('settings.pip.sort_name', 'Nom (A→Z)')}</option>
        </select>
      </div>
      <div class="pip-lib-tags" id="pip_lib_tags" role="group"
           aria-label="${L('settings.pip.filter_tags', 'Filtrer par tag')}"></div>
      <div class="save-layout-form">
        <button type="button" class="btn" id="pip_import_btn">${L('settings.pip.import', 'Importer…')}</button>
        <input type="file" id="pip_import_file" accept=".json" hidden>
      </div>
      <div id="pip_lib_grid" class="pip-lib-grid" role="list"><p class="meta">…</p></div>
</section>`;
    // Palette « Ajouter : » (mêmes boutons .btn que le composer)
    const pal = $('pip_palette');
    const labels = TYPE_LABELS();
    PIP_TYPES.forEach(ty => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn';
        b.textContent = '+ ' + labels[ty];
        b.onclick = () => addComp(ty);
        pal.appendChild(b);
    });
    // Toolbar : mêmes groupes/pictos que le composer + snap magnétique.
    const tb = $('pip_toolbar');
    tb.appendChild(_toolGroup(_t('plugin.multiview.group_align', 'Aligner'), [
        [_SVG.align_left, _t('plugin.multiview.align_left', 'Aligner à gauche'), () => alignComps('left')],
        [_SVG.align_hcenter, _t('plugin.multiview.align_hcenter', 'Centrer horizontalement'), () => alignComps('hcenter')],
        [_SVG.align_right, _t('plugin.multiview.align_right', 'Aligner à droite'), () => alignComps('right')],
        [_SVG.align_top, _t('plugin.multiview.align_top', 'Aligner en haut'), () => alignComps('top')],
        [_SVG.align_vcenter, _t('plugin.multiview.align_vcenter', 'Centrer verticalement'), () => alignComps('vcenter')],
        [_SVG.align_bottom, _t('plugin.multiview.align_bottom', 'Aligner en bas'), () => alignComps('bottom')],
    ]));
    // Sélecteur de RÉFÉRENCE (sélection unique) : cellule ou image. Placé dans le groupe
    // « Aligner » parce qu'il ne gouverne que ces six boutons.
    {
        const grp = tb.lastChild;
        const row = grp.querySelector('.tool-row') || grp;
        const sel = document.createElement('select');
        sel.className = 'tool-select';
        sel.title = _t('settings.pip.align_ref_title',
                       "Référence quand un seul composant est sélectionné");
        [['cell', _t('settings.pip.align_ref_cell', 'sur la cellule')],
         ['video', _t('settings.pip.align_ref_video', "sur l'image")]].forEach(([v, l]) => {
            sel.appendChild(new Option(l, v));
        });
        sel.value = alignRef;
        sel.onchange = () => { alignRef = sel.value; };
        row.appendChild(sel);
    }
    tb.appendChild(_toolGroup(_t('plugin.multiview.group_size', 'Taille'), [
        [_SVG.size_w, _t('plugin.multiview.size_w', 'Même largeur que la référence'), () => matchSizeComps('w')],
        [_SVG.size_h, _t('plugin.multiview.size_h', 'Même hauteur que la référence'), () => matchSizeComps('h')],
        [_SVG.size_both, _t('plugin.multiview.size_both', 'Même taille que la référence'), () => matchSizeComps('both')],
    ]));
    tb.appendChild(_toolGroup(_t('plugin.multiview.group_distribute', 'Distribuer'), [
        [_SVG.distribute_h, _t('plugin.multiview.distribute_h', 'Distribuer horizontalement'), () => distributeComps('h')],
        [_SVG.distribute_v, _t('plugin.multiview.distribute_v', 'Distribuer verticalement'), () => distributeComps('v')],
    ]));
    // Copier / coller les réglages — mêmes icônes que le composer multiview (control.html ~100-106).
    const clipGrp = _toolGroup(_t('settings.pip.group_settings', 'Réglages'), [
        [_SVG.copy_settings, _t('settings.pip.copy_settings_title', 'Copier les réglages du composant (hors position)'), copyCompSettings],
        [_SVG.paste_settings, _t('settings.pip.paste_settings_title', 'Coller les réglages dans les composants sélectionnés'), pasteCompSettings],
    ]);
    clipGrp.querySelector('.tool-row').setAttribute('aria-label',
        _t('settings.pip.group_settings_aria', 'Copier / coller les réglages de composant'));
    const [copyBtn, pasteBtn] = clipGrp.querySelectorAll('.tool-btn');
    copyBtn.id = 'pip_copy_settings';
    pasteBtn.id = 'pip_paste_settings';
    pasteBtn.disabled = true;   // rien dans le presse-papier au départ
    tb.appendChild(clipGrp);
    const snapLbl = document.createElement('label');
    snapLbl.className = 'mw-snap';
    snapLbl.innerHTML = '<input type="checkbox" class="ios-toggle" id="pip_snap" checked> <span>' +
        esc(_t('plugin.multiview.snap', 'Snap magnétique')) + '</span>';
    tb.appendChild(snapLbl);
    $('pip_snap').onchange = function () { snapEnabled = this.checked; };

    $('pip_new').onclick = newTemplate;
    $('pip_save').onclick = saveTemplate;
    $('pip_name').addEventListener('change', () => { if (cur) cur.name = $('pip_name').value; });
    $('pip_tag_input').addEventListener('keydown', e => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        addTagFromInput();
    });
    $('pip_import_btn').onclick = () => $('pip_import_file').click();
    $('pip_import_file').onchange = function () { importTemplate(this); };
    $('pip_lib_search').addEventListener('input', () => {
        libSearch = $('pip_lib_search').value;
        applyLibFilters();
    });
    $('pip_lib_sort').value = libSort;
    $('pip_lib_sort').addEventListener('change', () => {
        libSort = $('pip_lib_sort').value;
        applyLibFilters();
    });
    $('pip_sim_cell').onchange = () => { cellW = parseInt($('pip_sim_cell').value) || 640; applySimCell(); };
    $('pip_trim').onclick = trimTemplate;
    $('pip_aspect').onchange = () => {
        if (!cur) return;
        const a = parseAspect($('pip_aspect').value);
        if (a) {
            cur.config.aspect = Math.round(a * 10000) / 10000;
            comps().forEach(_enforceVideoRatio);   // la vidéo suit le nouveau couplage W/H
        }
        syncAspectField();
        applySimCell();
    };
    window.addEventListener('resize', _resizeCanvasDisplay);
    $('pip_sim_tally').onchange = () => { simTally = $('pip_sim_tally').value; draw(); };
    $('pip_sim_signal').onchange = () => { simSignal = $('pip_sim_signal').value; draw(); };
    const cv = cvs();
    cv.addEventListener('pointerdown', onDown);
    cv.addEventListener('pointermove', onMove);
    cv.addEventListener('pointerup', onUp);
    cv.addEventListener('pointercancel', onUp);
    document.addEventListener('keydown', e => {
        if (!cur || !selIdxs.length) return;
        if (document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
        if (e.key === 'Delete') { removeComp(); return; }
        nudgeSelection(e);
    });
    loadLib();
    // Catalogue de polices + @font-face de la bibliothèque : dès que c'est chargé, on redessine
    // (l'aperçu doit rendre la police du modèle, pas system-ui) et on re-remplit le panneau.
    if (window.BobiFonts) window.BobiFonts.load().then(() => { refreshProps(); draw(); });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUI);
} else {
    initUI();
}
})();
