---
name: Bobi.Studio
description: Console d'orchestration de containers LXC pour pipeline vidéo ST 2110 simulé, trois lumières (dark, light, studio).
colors:
  slate-signal: "#7aa2c8"
  slate-signal-hover: "#95b7d6"
  daylight-indigo: "#4f46e5"
  daylight-indigo-hover: "#6366f1"
  studio-amber: "#f59e0b"
  studio-amber-hover: "#fbbf24"
  default-bg: "#14161a"
  default-bg-elev: "#1b1e23"
  default-bg-input: "#181a1f"
  default-bg-hover: "#23262c"
  default-bg-selected: "#262a31"
  default-border: "#2a2d34"
  default-border-soft: "#23262c"
  default-text: "#d4d6da"
  default-text-muted: "#8a8d94"
  default-text-strong: "#f0f1f3"
  light-bg: "#f6f7f9"
  light-bg-elev: "#ffffff"
  light-border: "#e5e7eb"
  light-text: "#1f2937"
  light-text-muted: "#6b7280"
  light-text-strong: "#111827"
  studio-bg: "#18181b"
  studio-bg-elev: "#23232a"
  studio-text-strong: "#fafafa"
  status-running-fg: "#7ab98a"
  status-running-bg: "#1d2e23"
  status-stopped-fg: "#d07a82"
  status-stopped-bg: "#322023"
  status-warning-fg: "#c4a667"
  status-warning-bg: "#2e2a1e"
  status-unknown-fg: "#8a8d94"
  status-unknown-bg: "#25272c"
  # Les couleurs de mode (badges de type de container) ne sont plus des tokens figés :
  # elles sont déclarées par chaque plugin (plugin.json:badge.oklch) et générées au
  # rendu par templates/layout.html. Voir « Modes » plus bas.
typography:
  display:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "1.35em"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "0.95em"
    fontWeight: 600
    lineHeight: 1.4
  label:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "0.78em"
    fontWeight: 500
    letterSpacing: "1px"
  body:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "0.88em"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "12px"
  lg: "16px"
  xl: "20px"
components:
  button-primary:
    backgroundColor: "{colors.slate-signal}"
    textColor: "{colors.default-text-strong}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  button-primary-hover:
    backgroundColor: "{colors.slate-signal-hover}"
    textColor: "{colors.default-text-strong}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.default-text}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  card:
    backgroundColor: "{colors.default-bg-elev}"
    textColor: "{colors.default-text}"
    rounded: "{rounded.md}"
    padding: "16px"
  input:
    backgroundColor: "{colors.default-bg-input}"
    textColor: "{colors.default-text}"
    rounded: "{rounded.sm}"
    padding: "6px 9px"
  badge-running:
    backgroundColor: "{colors.status-running-bg}"
    textColor: "{colors.status-running-fg}"
    rounded: "3px"
    padding: "2px 8px"
  badge-stopped:
    backgroundColor: "{colors.status-stopped-bg}"
    textColor: "{colors.status-stopped-fg}"
    rounded: "3px"
    padding: "2px 8px"
  badge-warning:
    backgroundColor: "{colors.status-warning-bg}"
    textColor: "{colors.status-warning-fg}"
    rounded: "3px"
    padding: "2px 8px"
  filter-chip:
    backgroundColor: "transparent"
    textColor: "{colors.default-text}"
    rounded: "{rounded.pill}"
    padding: "3px 10px"
  filter-chip-active:
    backgroundColor: "rgba(122, 162, 200, 0.10)"
    textColor: "{colors.slate-signal}"
---

# Design System: Bobi.Studio

> Statut : vérifié contre le code (base.css, layout.html, plugins) le **2026-07-04**.

## 1. Overview

**Creative North Star: "Le tableau de bord vidéo IP"**

Une console technique où la topologie du pipeline broadcast reste lisible en permanence : chaque container est un nœud, chaque fps un signal vivant, chaque statut une LED de baie. Le système design SERT la lecture du pipeline, il ne se met jamais devant. L'utilisateur principal est un opérateur MXL, le spectateur est un visiteur en démo ou un étudiant : tous deux doivent comprendre l'état du système en quelques secondes.

Trois lumières, un seul outil. Le thème **default** (dark neutre, accent Slate Signal) est le poste de travail standard. **Daylight** habille le système pour le bureau et la lumière du jour. **Studio** réchauffe le dark pour la régie sombre, avec un accent amber lisible à distance qui évoque la tally lamp broadcast. Les tokens structurels (rayons, espacements, typo, statuts) sont partagés ; seules les couleurs de surface et l'accent varient. C'est la même console dans trois éclairages, pas trois produits.

Le système rejette le **corporate broadcast lourd** (grilles serrées Windows-XP, boutons 3D, gris terne), le **gamer/cyberpunk neon** (noir + cyan/magenta, glow, esthétique hacker movie), le **SaaS générique** cream/violet et les **Bootstrap admin templates** des années 2015. Famille de référence : Linear, Grafana, Tailscale, Raycast. Sobre, typographie soignée, l'information dense respire grâce au rythme d'espacement.

**Key Characteristics:**
- Trois thèmes, un seul jeu de tokens structurels.
- Statuts sémantiques robustes (`running`, `stopped`, `warning`, `unknown`, `script_stopped`), jamais codés par la couleur seule.
- Densité d'information assumée, aérée par les espacements et la hiérarchie typographique.
- Monospace réservée aux valeurs techniques (fps, timestamps, badges, champs numériques), jamais pour la prose.
- Interface **i18n FR/EN** : tout texte UI passe par une clé de traduction — `_('cle')` côté Jinja, `window.t('cle')` côté JS (catalogue injecté `window.I18N = js_catalog` dans layout.html ; admin des traductions : templates/i18n.html). Le français reste la langue de référence des clés et des valeurs par défaut. Jamais de texte codé en dur.

## 2. Colors

Le système distingue trois rôles d'accent (un par thème), un vocabulaire de statuts partagé, et quatre couleurs de mode pour typer les containers par rôle dans le pipeline.

### Primary (varie selon le thème)

- **Slate Signal** (`#7aa2c8`) : accent du thème par défaut. Bleu ardoise désaturé, lisible sur dark neutre, jamais agressif. Utilisé pour les liens, le focus de champ, l'état actif des onglets et des chips de filtre, le titre actif dans la nav.
- **Daylight Indigo** (`#4f46e5`) : accent du thème Daylight. Indigo franc qui ressort sur fond blanc cassé.
- **Studio Amber** (`#f59e0b`) : accent du thème Studio. Référence directe à la tally lamp broadcast, lisible à distance en régie sombre.

### Neutral (varie selon le thème, mêmes rôles)

Trois couches de fond par thème : **bg** (surface), **bg-elev** (cards, formulaires, nav, palette), **bg-input** (champs, lignes de liste). Trois niveaux de texte : **text** (lecture courante), **text-muted** (labels, meta, timestamps), **text-strong** (titres, valeurs actives). Les borders sont toujours présentes, jamais imitées par des shadows.

- Default (dark neutre) : `bg #14161a`, `bg-elev #1b1e23`, `border #2a2d34`, `text #d4d6da`, `text-strong #f0f1f3`.
- Daylight (clean light) : `bg #f6f7f9`, `bg-elev #ffffff`, `border #e5e7eb`, `text #1f2937`, `text-strong #111827`.
- Studio (dark warm) : `bg #18181b`, `bg-elev #23232a`, `border #36363f`, `text #e4e4e7`, `text-strong #fafafa`.

### Statuts (partagés tous thèmes, rôles fixes)

Couples fond+texte calibrés pour chaque thème ; les rôles sémantiques ne bougent jamais.

- **Running** (vert, ex `#7ab98a` sur `#1d2e23`) : container up + script actif.
- **Stopped** (rouge, ex `#d07a82` sur `#322023`) : container down ou erreur grave.
- **Warning / script_stopped** (jaune amber, ex `#c4a667` sur `#2e2a1e`) : container up mais script arrêté côté agent.
- **Unknown** (gris, ex `#8a8d94` sur `#25272c`) : état indéterminé, timeout agent.

### Modes (rôle du container dans le pipeline)

Teintes basse-saturation qui typent les containers sans hiérarchie de gravité (ce n'est pas un statut, c'est une catégorie). **Système ouvert, piloté par les plugins** : il n'existe plus de liste figée de classes `.mode-*` à couleurs hex dans le CSS.

- Chaque plugin déclare sa couleur dans `plugins/<type>/plugin.json` → `badge: {label, class, oklch}` (ex. mixer : `"class": "mode-mixer", "oklch": "0.78 0.05 20"`).
- `app/plugins.py:badge_css_vars()` collecte `{classe: oklch}` dédupliqué ; `templates/layout.html` génère un bloc `<style>` inline : `background: oklch(L C H / 14%)`, `color: oklch(L C H)`, `border-color: oklch(L C H / 32%)`.
- En thème **light**, la variante est dérivée automatiquement par `calc(L - 0.33)` sur la luminosité (mêmes chroma/hue, alphas 10 % / 28 %).
- `static/css/base.css` ne porte que la **structure** (`.mode-badge`) et l'état vide (`.mode-none`).

Ajouter un type = déclarer un `badge.oklch` dans son manifeste (chroma mesuré ≈ 0.05, L ≈ 0.78 pour rester dans la famille), pas toucher au CSS.

### Familles de tokens spécialisées (base.css)

- **`--space-1..7`** : échelle d'espacement explicite (4 / 8 / 12 / 16 / 24 / 32 / 48 px). Le nouveau code l'utilise ; `--gap` / `--card-padding` restent pour la compat.
- **`--topo-flow-video|audio|data`** (+ variantes `-active`) : couleurs des arêtes de la topologie pipeline (page Câbles) — vidéo sur l'accent du thème, audio ocre, data violet.
- **`--bus-pgm` / `--bus-pvw` / `--bus-dissolve` / `--bus-keyer`** (+ `-text`) : bus du mélangeur — PGM rouge antenne, PVW vert, DISSOLVE cyan, KEYER ambre. Convention universelle des consoles broadcast : **non thématisées**, identiques sur les trois lumières.
- **`--overlay-accent` / `--overlay-accent-soft`** : accent OKLCH (`0.74 0.11 315`) des overlays du composer multiview (texte/horloge/image), décliné dans les trois thèmes.
- **`--canvas-bg`** : fond des surfaces de prévisualisation canvas (multiview, mini-previews), overridé par thème.

### Named Rules

**La règle Statut > Couleur.** Un statut critique (running, stopped) n'est jamais identifiable par la couleur seule. Toujours coupler avec le label texte du badge. Lisibilité conservée en N&B (captures, projection dégradée).

**La règle Trois Lumières.** Toute nouvelle couleur structurelle doit exister dans les trois thèmes avec le même rôle. Pas de couleur qui n'apparaît que dans un thème, sauf l'accent.

**La règle Saturation Mesurée.** Aucun token n'utilise la pleine saturation. Les statuts et les modes sont volontairement désaturés (chroma réduit). Le neon, c'est l'anti-référence.

**La règle OKLCH pour les ajouts.** Le système actuel est en hex pour raisons historiques. Tout nouveau token doit être défini en OKLCH avec chroma réduit aux extrêmes de luminosité.

## 3. Typography

**Display Font:** Inter (avec fallback `'Segoe UI', system-ui, -apple-system, sans-serif`).
**Body Font:** Inter (même famille, une seule famille pour l'UI).
**Mono Font:** `ui-monospace` (avec fallback `'SF Mono', Menlo, Consolas`).

**Character:** Sans-serif neutre, lisible à toutes les tailles, qui disparaît au profit de l'information. La monospace est réservée aux valeurs techniques (fps, timestamps, IP, paths, badges de statut, champs numériques). Aucune typo d'affichage décorative.

### Hierarchy

- **Display** (h1, weight 600–700, `1.35em`, letter-spacing `-0.01em`) : titre de page unique en haut de chaque vue.
- **Headline** (h3, weight 600, `0.95em`) : titres de cards et de sections d'éditeur.
- **Title de section** (h2, weight 600, `0.78em`, `UPPERCASE`, letter-spacing `1–1.5px`) : sous-section, sépare des blocs sans peser visuellement.
- **Body** (weight 400, `13.5–14px`, line-height `1.5`) : taille de base. Cap à 65–75ch sur la prose (aide, documentation).
- **Label** (weight 500, `0.78em`) : labels de formulaire, meta, timestamps. `text-muted` par défaut.
- **Mono** (weight 400, `0.88em`) : valeurs techniques. Jamais pour la prose courante, jamais pour les labels d'interface.

### Named Rules

**La règle Monospace Confinée.** La monospace ne sert qu'aux *valeurs* techniques (fps, timestamps, IP, chemins shm, badges). Jamais pour les labels, jamais pour les boutons, jamais pour la prose. Si un humain le lit comme une phrase, c'est Inter.

**La règle Échelle Serrée.** Ratio ~1.15 entre les pas de l'échelle. Beaucoup d'éléments cohabitent sur une vue (cards × N containers + nav + sidebar + alertes) ; un contraste typographique exagéré crée du bruit.

## 4. Elevation

Le système est **majoritairement flat**. La hiérarchie de profondeur passe par le contraste de fond entre `bg`, `bg-elev` et `bg-input` (couches tonales), pas par les ombres.

### Shadow Vocabulary

- **Default** (`box-shadow: none`) : aucune ombre. La séparation est portée par les borders 1px et les changements de surface.
- **Daylight** (`0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.06)`) : ombre tactile très discrète, posée sur les cards, formulaires et nav pour compenser l'absence de contraste de fond sur surface blanche.
- **Studio** (`0 1px 3px rgba(0, 0, 0, 0.35), 0 1px 2px rgba(0, 0, 0, 0.25)`) : ombre fine sur dark warm, équivalente à Daylight pour matérialiser les surfaces sans relief excessif.

### Named Rules

**La règle Flat par Défaut.** Les surfaces sont plates au repos. Pas de glow décoratif, pas de drop-shadow épaisse, pas de relief 3D sur les boutons. La profondeur, c'est le contraste tonal.

**La règle Pas de Glassmorphism.** Pas de `backdrop-filter: blur`. Pas de gradient violet en arrière-plan. Le système a déjà rejeté un thème glass (Aurora) pour cette raison. Si une surface a besoin d'attirer l'œil, c'est par sa hiérarchie ou son accent, pas par un effet matériau.

## 5. Components

### Buttons
- **Shape:** rayon `var(--radius-small)` = 4–6px. Pas pill, pas square.
- **Primary (`.btn` + couleur):** `.btn-blue`, `.btn-green`, `.btn-red`, `.btn-orange`, `.btn-purple` portent une couleur d'intention pleine désaturée (`#3b5673`, `#355c43`, `#6e3138`…). Texte `#f0f1f3`, padding `6px 12px`, font-size `0.85em`, weight 500.
- **Hover / Active:** `filter: brightness(1.10)` au hover, `0.95` au press. Pas de translation, pas de scale.
- **Transition:** 120ms sur background, border-color, color. Aucune choreography.
- **Don't:** ne jamais composer un bouton avec gradient text, glow, ou border-radius pill par défaut.

### Status Badges (`.badge.running` / `.stopped` / `.script_stopped` / `.unknown`)
- **Shape:** rayon `3px`, padding `2px 8px`, font-size `0.75em`, weight 600, `lowercase`, letter-spacing `0.3px`.
- **Family:** monospace.
- **Rule:** background et texte du même rôle sémantique. Le texte du label EST le statut (jamais juste la couleur).

### Mode Badges (`.mode-badge` + classe couleur générée / `.mode-none`)
- **Shape (`.mode-badge`, base.css):** rayon `3px`, padding `2px 8px`, font-size `0.72em`, weight 600, `UPPERCASE`, letter-spacing `0.5px`.
- **Couleur:** générée depuis `plugin.json:badge.oklch` par layout.html (voir « Modes » en section Colors) : fond `oklch(L C H / 14%)`, texte `oklch(L C H)`, border `oklch(L C H / 32%)` ; dérivation auto `L - 0.33` en thème light. Aucune classe couleur codée en dur dans le CSS.
- **mode-none:** transparent, border dashed, `text-muted`. État "rien déployé".

### Cards (`.card`)
- **Corner:** `var(--radius)` = 6px (default/studio), 8px (light).
- **Background:** `var(--bg-elev)`.
- **Border:** 1px solid `var(--border)`. Toujours présente.
- **Shadow:** selon thème (voir Elevation).
- **Padding:** `var(--card-padding)` = 16–18px.
- **Grid:** `grid-template-columns: repeat(auto-fill, minmax(280px, 1fr))`, gap 12–14px.

### Forms (`.form`, `.palette-field`)
- **Container:** même style que card (`bg-elev` + border + radius).
- **Input / Select:** fond `var(--bg-input)`, border 1px `var(--border)`, padding `6px 9px` (compact) ou `8px 11px` (palette).
- **Focus:** border-color `var(--accent)`, background passe à `var(--bg)`. En Daylight, un glow accent `box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12)` complète.
- **Family:** monospace pour `input[type=number|text]` et `select` (valeurs techniques).
- **Width:** par défaut 90px ; `.wide` 200px ; `.xwide` 360px. Palette : `width: 100%`.

### Switch iOS (`input[type=checkbox].ios-toggle`, base.css)
- **Usage — règle projet:** TOUT booléen d'interface (feature-toggle « Activer … », capacités, options) utilise `.ios-toggle`. **Jamais de checkbox nue.**
- **Anatomie:** classe posée directement sur l'`<input type="checkbox">` (`appearance: none`) : track pill `36×20px` (`bg-input` + border), thumb rond `14px` en `::before`.
- **États:** off = thumb `text-muted` ; checked = track `var(--accent-soft)` + border accent, thumb accent translaté de `16px` ; `:focus-visible` = outline accent 2px.
- **Motion:** track 180ms ease-out, thumb 220ms `cubic-bezier(0.22, 1, 0.36, 1)`.
- **Groupes horizontaux:** envelopper dans `label.toggle-inline` (inline-flex, gap `7px`, weight 500, texte `var(--text)`).

### Nav Réglages à 2 niveaux (templates/settings.html)
- **Niveau 0 — groupes:** rangée `.tabs.set-groups` de 7 groupes de haut niveau (Général, Cluster & réseau, Nœuds & matériel, Signal & format, Médias, Protocoles & pupitres, Système).
- **Niveau 1 — onglets:** rangée `.tabs.set-subnav` des onglets existants, **filtrée** au groupe actif par `switchSetGroup()` (mapping `SET_GROUPS` → `SET_TABS`, permissions respectées). Les sous-onglets internes des pages restent inchangés.
- **Style:** les deux niveaux réutilisent le composant Tabs standard ; aucun style bespoke.

### Filter Chips (`.filter-chip`)
- **Shape:** pill, border 1px, padding `3px 10px`, font-size `0.82em`.
- **Default:** fond transparent, texte `var(--text)`, border `var(--border)`.
- **Active:** fond `var(--accent-soft)` (10% accent), border et texte `var(--accent)`. Toujours associé à un label texte.

### Tabs (`.tabs` / `.tab`)
- **Style:** onglets plats sous une bordure inférieure. Pas d'onglet 3D.
- **Default:** fond transparent, `text-muted`.
- **Active:** fond `bg-elev`, border 1px `var(--border)` sans bordure basse, texte `text-strong`. Coins supérieurs arrondis seulement.

### Top Navigation (`.topnav`)
- **Container:** `bg-elev` + border + radius + shadow du thème, padding `10px 16px`.
- **Brand:** weight 700, letter-spacing `2px` (le seul endroit avec un letter-spacing aussi marqué), `text-strong`.
- **Liens:** padding `7px 14px`, radius small, weight 500.
- **Hover:** fond `var(--bg-hover)`.
- **Active:** fond `var(--bg-selected)`, couleur accent.
- **Settings (icone-action):** lien encadré (border 1px), hover passe border + texte à l'accent.

### Alerts (`.alert.alert-info` / `alert-warning` / `alert-error`)
- **Shape:** rayon small, padding `8px 12px`, font-size `0.85em`.
- **Default:** `bg-elev` + border 1px `var(--border)`.
- **Variant:** full 1px border + fond teinté pris sur les tokens de statut. `info` = `--accent` + `--accent-soft`. `warning` = `--status-warning-fg` + `--status-warning-bg`. `error` = `--status-stopped-fg` + `--status-stopped-bg`. Pas de side-stripe.

### Multiview / Layout List Items (`.mw-list li` / `.layout-list li`)
- **Style:** carte mini avec preview canvas, padding `8px 10px`, radius small, border soft, fond `bg-input`.
- **Selected:** fond `bg-selected`, border accent.

## 6. Do's and Don'ts

### Do:
- **Do** utiliser **Inter** pour toute l'UI et la **monospace** uniquement pour les valeurs techniques (fps, IP, timestamps, paths shm, champs numériques).
- **Do** définir tout nouveau token couleur en **OKLCH** avec chroma réduit (`<0.1`) aux extrêmes de luminosité, même si l'historique du fichier est en hex.
- **Do** rendre tout statut identifiable sans la couleur : badge avec **label texte** (`running`, `stopped`), pas seulement une pastille colorée.
- **Do** passer tout texte UI par l'**i18n** : `_('cle')` en Jinja, `window.t('cle')` en JS (catalogue `window.I18N`), langues FR/EN — le français reste la référence des clés/valeurs par défaut. Jamais de libellé codé en dur.
- **Do** utiliser le switch **`.ios-toggle`** pour tout booléen d'interface — jamais de checkbox nue.
- **Do** séparer les surfaces par **contraste de fond** (`bg` → `bg-elev` → `bg-input`) et bordures 1px, pas par drop-shadow.
- **Do** propager toute nouvelle couleur structurelle dans **les trois thèmes** (default, light, studio) avec le même rôle sémantique.
- **Do** garder l'**accent ≤10% de la surface** : liens, focus, état actif, état sélectionné. Jamais en décoration.

### Don't:
- **Don't** introduire de **glassmorphism** (`backdrop-filter: blur`, surfaces translucides, gradients radiaux violets). Le thème Aurora a été retiré du système pour cette raison.
- **Don't** utiliser de **border-left/right > 1px comme bande colorée** sur cards, alertes, list items. La règle s'applique aussi aux alertes actuelles (`alert-info|warning|error`), à corriger lors d'une passe `polish`.
- **Don't** utiliser de **gradient text** (`background-clip: text` + gradient). Aucun titre, aucune valeur ne doit être colorée par gradient.
- **Don't** dériver vers le **corporate broadcast lourd** : pas de boutons 3D, pas de palette grise terne style EVS/Studer historique, pas de grilles serrées style Windows-XP.
- **Don't** dériver vers le **gamer/cyberpunk neon** : pas de noir profond + cyan/magenta, pas de glow décoratif, pas d'esthétique "hacker movie".
- **Don't** appliquer la **monospace** aux labels, boutons, titres, prose. La monospace est confinée aux valeurs techniques.
- **Don't** utiliser de **display font** ou de typo décorative. Une seule famille (Inter) porte tout l'UI.
- **Don't** dépendre de la **couleur seule** pour signaler un statut critique. Toujours coupler avec un label texte.
- **Don't** introduire de **modale** quand un panel inline ou une palette latérale suffit. Le pattern dominant est la palette de déploiement sticky à droite.
- **Don't** créer une couleur visible uniquement dans un seul thème (sauf l'accent).
- **Don't** utiliser `#000` ou `#fff` purs pour de nouveaux tokens : tinter légèrement vers le hue du thème.
