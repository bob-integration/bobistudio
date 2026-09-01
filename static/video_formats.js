/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 BOBI SAS, France — Auteur : Cyril Mazouer.
 *
 * LISTE DES FORMATS VIDÉO — source unique (Réglages → Vidéo, setting `video_formats`).
 *
 * ⚠ Ce helper vivait dans static/scripts.js, qui n'est chargé QUE par containers.html et
 * projects.html. Toute autre page (Réglages, pages de plugin…) qui voulait un format vidéo devait
 * donc soit s'en passer, soit RECOPIER le parseur — c.-à-d. créer une seconde vérité qui dérive en
 * silence. Il est donc extrait ici et chargé par layout.html pour TOUTES les pages (même motif que
 * bobi_fonts.js, num_stepper.js, tx_maint.js).
 *
 * Une ligne de réglage = `label ; width ; height ; fps ; scan ; chroma ; bit_depth ; colorimetry`
 * (ex. « 3G 1080p50 ;1920;1080;50;p;422;10;709 »). Le préréglage porte donc AUSSI la chroma et la
 * colorimétrie : aucune de ces valeurs n'est saisie à la main ni devinée ailleurs.
 */
window._videoFormats = null;
window._videoFormatDefault = '';

async function loadVideoFormats() {
    if (window._videoFormats !== null) return window._videoFormats;
    try {
        const r = await fetch('/api/settings');
        if (!r.ok) return [];
        const s = await r.json();
        const raw = s.video_formats || '';
        window._videoFormatDefault = s.video_format_default || '';
        window._videoFormats = raw.split('\n')
            .map(l => l.trim()).filter(Boolean)
            .map(l => {
                const parts = l.split(';').map(p => p.trim());
                const chroma = ['420', '422', '444'].includes(parts[5]) ? parts[5] : '422';
                const bd = [8, 10, 12].includes(parseInt(parts[6])) ? parseInt(parts[6]) : 10;
                return {
                    label: parts[0] || '',
                    w:     parseInt(parts[1]) || 0,
                    h:     parseInt(parts[2]) || 0,
                    fps:   parseFloat(parts[3]) || 25,
                    scan:  (parts[4] || 'p').toLowerCase() === 'i' ? 'i' : 'p',
                    chroma:      chroma,
                    bit_depth:   bd,
                    colorimetry: (parts[7] || '709').toLowerCase(),
                };
            })
            .filter(f => f.label && f.w && f.h);
    } catch (e) {
        window._videoFormats = [];
    }
    return window._videoFormats;
}
window.loadVideoFormats = loadVideoFormats;

/* Préréglage correspondant à des VALEURS stockées (w/h/fps/scan[/bd]), ou null.
   ★ Les consommateurs (modèles de carte 2110…) stockent les VALEURS, pas le label : renommer ou
   supprimer un format dans les Réglages ne doit pas casser en silence ce qui a été déclaré. Le label
   n'est qu'un affichage — et quand plus aucun préréglage ne correspond, on le DIT (info, pas erreur). */
window.videoFormatMatch = function (v) {
    if (!v || !window._videoFormats) return null;
    return window._videoFormats.find(f =>
        f.w === parseInt(v.w) && f.h === parseInt(v.h) &&
        Math.abs(f.fps - parseFloat(v.fps || 0)) < 0.01 &&
        f.scan === (v.scan === 'i' ? 'i' : 'p') &&
        (v.bd == null || f.bit_depth === parseInt(v.bd))) || null;
};

/* Libellé lisible d'un format stocké : le label du préréglage s'il existe, sinon la description
   technique (le format reste parfaitement utilisable — il n'est simplement plus dans la liste). */
window.videoFormatLabel = function (v) {
    if (!v || !v.w) return '—';
    const m = window.videoFormatMatch(v);
    if (m) return m.label;
    const fps = Number(v.fps || 0);
    return `${v.w}×${v.h}${v.scan === 'i' ? 'i' : 'p'}${fps % 1 ? fps.toFixed(2) : fps}`
         + (v.bd ? ` · ${v.bd} bits` : '');
};
