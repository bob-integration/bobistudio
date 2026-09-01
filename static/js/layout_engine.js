/* Moteur de géométrie d'un éditeur de disposition — la partie qui était écrite QUATRE fois.
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
 * Ce fichier paie ce prix. Il n'est PAS une cinquième copie : c'est l'extraction, et son
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
window.MXLLayout = (function () {
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

  /* Référence d'alignement : le primaire dès 2 sélectionnés, sinon le cadre entier. */
  function _reference(rects, sel, primaire) {
    if (sel.length >= 2 && rects[primaire]) {
      const p = rects[primaire];
      return { x: p.x || 0, y: p.y || 0, w: p.w || 0, h: p.h || 0 };
    }
    return { x: 0, y: 0, w: 1, h: 1 };
  }

  /* Aligne la sélection. `mode` ∈ left|right|hcenter|top|bottom|vcenter.
     Rend une COPIE : le moteur ne mute jamais ce qu'on lui donne, pour que l'appelant
     puisse comparer avant/après (annulation, marquage « modifié »). */
  function aligner(rects, sel, primaire, mode) {
    const ref = _reference(rects, sel, primaire);
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
      out[i] = borner(c);
    });
    return out;
  }

  /* Égalise la taille sur le primaire. `mode` ∈ w|h|both. Exige ≥2 sélectionnés :
     à un seul, l'opération n'a pas de référence — on rend `null` plutôt que de ne rien
     faire en silence, pour que l'appelant puisse le DIRE à l'exploitant. */
  function egaliser(rects, sel, primaire, mode) {
    if (sel.length < 2) return null;
    const ref = rects[primaire];
    if (!ref) return null;
    const out = rects.map((r) => ({ ...r }));
    sel.forEach((i) => {
      if (i === primaire) return;
      const c = out[i];
      if (!c) return;
      if (mode === "w" || mode === "both") c.w = Math.min(ref.w, 1);
      if (mode === "h" || mode === "both") c.h = Math.min(ref.h, 1);
      out[i] = borner(c);
    });
    return out;
  }

  /* Distribue la sélection sur un axe. `axe` ∈ h|v. Exige ≥3 sélectionnés (à deux, la
     distribution est l'identité). Répartit les POSITIONS entre la première et la dernière —
     comportement historique, cf. l'en-tête. */
  function distribuer(rects, sel, axe) {
    if (sel.length < 3) return null;
    const k = axe === "h" ? "x" : "y";
    const out = rects.map((r) => ({ ...r }));
    const idx = sel.slice().sort((a, b) => (out[a][k] || 0) - (out[b][k] || 0));
    const v0 = out[idx[0]][k] || 0;
    const vN = out[idx[idx.length - 1]][k] || 0;
    const pas = (vN - v0) / (idx.length - 1);
    idx.forEach((i, n) => { out[i][k] = r3(v0 + pas * n); });
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
  function distribuerEspaces(rects, sel, axe) {
    if (sel.length < 3) return null;
    const k = axe === "h" ? "x" : "y";
    const t = axe === "h" ? "w" : "h";
    const out = rects.map((r) => ({ ...r }));
    const idx = sel.slice().sort((a, b) => (out[a][k] || 0) - (out[b][k] || 0));
    const prem = out[idx[0]], dern = out[idx[idx.length - 1]];
    const etendue = (dern[k] + dern[t]) - prem[k];
    const occupe = idx.reduce((s, i) => s + (out[i][t] || 0), 0);
    const espace = (etendue - occupe) / (idx.length - 1);
    let pos = prem[k] + prem[t] + espace;
    idx.slice(1, -1).forEach((i) => {
      out[i][k] = r3(pos);
      pos += out[i][t] + espace;
    });
    return out;
  }

  /* Cibles d'aimantage, en unités NORMALISÉES : bords et centre du cadre, plus bords et
     centres des éléments non sélectionnés. */
  function ciblesAimant(rects, exclus) {
    const xs = [0, 1, 0.5];
    const ys = [0, 1, 0.5];
    rects.forEach((r, i) => {
      if (exclus.includes(i)) return;
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
  function aimanter(rects, exclus, x, y, w, h, tol) {
    const { xs, ys } = ciblesAimant(rects, exclus);
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
    return { x: r3(ax), y: r3(ay), guides: guides.filter(Boolean) };
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

  return { r3, borner, aligner, egaliser, distribuer, distribuerEspaces,
           aimanter, ciblesAimant, sélection };
})();
