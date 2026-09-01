/* Lecture d'un FLUX 2110 — cadence et état, dans les deux sens.
 *
 * Les vues Sources (plugins/2110_io/control.js) et Destinations (static/io2110.js) affichent les
 * mêmes deux informations pour chaque flux : est-ce que ça circule, et à quelle cadence. Chacune
 * les rendait à sa façon, avec ses propres seuils de couleur et ses propres mots — jusqu'à ce
 * qu'une réception affiche « 1000.0 fps » sur une piste audio pendant qu'une émission affichait
 * « — » au même endroit. Le calcul vit donc ici, une fois.
 *
 * ─── Ce que « fps » veut dire selon l'essence ─────────────────────────────────────────────
 * Le moteur publie, pour CHAQUE cible, un compteur générique : Δ trames reçues / Δt. Sur une
 * session vidéo ce sont des images ; sur une session audio 2110-30, ce sont des CHUNKS de 1 ms —
 * soit ~1000/s. Le nombre est juste, son unité ne l'est pas : « 1000 fps » sur de l'audio ne
 * décrit rien qu'un exploitant puisse utiliser, et laisse croire à une mesure d'image.
 *
 * D'où la règle : la CADENCE n'est affichée que pour la vidéo. Pour l'audio et l'ANC, le même
 * compteur ne sert qu'à répondre à « est-ce que ça arrive ? » — une question d'ÉTAT, qui a sa
 * propre colonne. On ne jette donc pas l'information : on la met là où elle veut dire quelque chose.
 */
window.IOFlux = (function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* Un flux circule-t-il ? Vrai dès que le compteur du moteur avance, quelle que soit l'essence. */
  function circule(o) { return Number(o && o.fps) > 0; }

  /* CADENCE — vidéo uniquement, même forme des deux côtés : « 25.0 fps ».
   *
   * `nominal` (optionnel) : la cadence ATTENDUE. Quand on la connaît et que la mesure décroche,
   * le chiffre devient « ⚠ 24.90/25 fps » — l'écart se lit alors sans calcul mental. Elle n'est
   * connue qu'à l'émission (le moteur sait ce qu'il vise) ; en réception on ne dispose que de la
   * mesure, et on n'invente pas une cible pour pouvoir afficher une alerte.
   *
   * Couleur : elle dit une SANTÉ, donc elle ne se déclenche que sur un fait — ça ne circule pas,
   * ou ça circule sous la cible. Un seuil absolu (« au moins 24 ») serait faux : un flux à
   * 12,5 images/s est parfaitement sain si c'est ce qu'il annonce.
   */
  function cadence(essence, fps, nominal, opts) {
    opts = opts || {};
    if (essence !== 'video') {
      return `<span style="color:var(--text-muted)"
        title="${esc(opts.titreVide || "La cadence d'image ne s'applique qu'à la vidéo — l'état de ce flux se lit dans sa colonne.")}">—</span>`;
    }
    if (fps == null) {
      return '<span style="color:var(--text-muted)" title="Aucune cadence mesurée">—</span>';
    }
    const n = Number(fps);
    const nom = Number(nominal) || 0;
    const sous = nom > 0 && n < nom - 0.15;
    /* Zéro l'emporte sur « sous la cible » : rien ne circule, c'est l'arrêt, pas une baisse. La
     * comparaison au nominal aurait affiché « ⚠ 0.00/25 » en ambre — la couleur de ce qui faiblit,
     * pour ce qui est mort. */
    const col = n <= 0 ? 'var(--status-stopped-fg)'
              : sous ? 'var(--status-warning-fg)' : 'var(--status-running-fg)';
    const txt = sous ? `⚠ ${n.toFixed(2)}/${nom}` : n.toFixed(1);
    const tip = opts.titre || (sous
      ? `Cadence mesurée sous la cible (${nom}) : des trames manquent.`
      : 'Cadence réellement ' + (opts.sens === 'tx' ? 'émise' : 'reçue') + ' (mesurée)');
    return `<span style="color:${col}" title="${esc(tip)}">${txt} fps</span>`;
  }

  /* ÉTAT d'un flux — le même vocabulaire des deux côtés, décliné par sens.
   * `etat` : 'inactif' | 'attente' | 'ok' | 'alerte'. Le libellé NOMME toujours la situation :
   * la pastille ne fait que la répéter à l'œil, elle ne la porte jamais seule. */
  const TONS = {
    ok:      { bg: 'var(--status-running-bg)', fg: 'var(--status-running-fg)' },
    attente: { bg: 'var(--border-soft)',       fg: 'var(--text-muted)' },
    inactif: { bg: 'var(--border-soft)',       fg: 'var(--text-muted)' },
    alerte:  { bg: 'var(--status-warning-bg)', fg: 'var(--status-warning-fg)' },
  };
  function badge(etat, libelle, titre) {
    const t = TONS[etat] || TONS.inactif;
    return `<span class="badge" style="background:${t.bg};color:${t.fg}" title="${esc(titre || '')}">${esc(libelle)}</span>`;
  }

  return { circule, cadence, badge };
})();
