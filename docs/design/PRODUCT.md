## Register

product

## Users

Opérateurs, ingénieurs et techniciens broadcast pilotant un pipeline vidéo ST 2110 en production sur un cluster de nœuds Docker (conteneurs répartis sur des nœuds enrôlés, pilotés par agent-nœud). Trois profils distincts :

- **Opérateur de régie** : surveille en permanence l'état du pipeline pendant l'antenne. Réagit vite, lit les statuts d'un coup d'œil, ne configure pas — il opère. Sa fenêtre est souvent en fond de régie, visible à distance.
- **Ingénieur système** : déploie, configure, diagnostique. Travaille en bureau ou en régie technique. Sessions longues, fenêtres multiples, besoin de granularité.
- **Utilisateur projet** : technicien ou client avec accès restreint à un projet donné (ses containers, ses médias). Accède via le portail projet, pas l'orchestrateur complet.

Production live. Disponibilité 24/7. Plusieurs opérateurs peuvent être connectés simultanément. Un incident = risque d'antenne.

## Product Purpose

Orchestrer et surveiller un parc de conteneurs Docker, répartis sur les nœuds enrôlés d'un cluster multi-nœuds, qui implémentent un pipeline broadcast IP réel (receivers ST 2110, mixer, multiview, encodeurs, enregistreurs). L'interface répond en permanence à trois questions, par ordre de priorité décroissante :

1. **Qu'est-ce qui tourne — et est-ce nominal ?** (surveillance temps réel, première priorité)
2. **Pourquoi ça ne tourne plus — et comment corriger vite ?** (diagnostic et remédiation)
3. **Comment configurer ou déployer ?** (configuration, usage moins fréquent en production)

Succès = un opérateur de régie lit l'état complet du pipeline en moins de 3 secondes sans action. Un incident visible est identifiable et actionnable sans quitter l'écran principal.

Le périmètre s'est élargi au-delà du seul pipeline : services protocolaires broadcast (NMOS IS-04/05, Ember+, TSL, émulation ATEM, panneaux Skaarhoj), édition des flux sortants (page /streams), monitoring multi-nœuds (santé des serveurs du cluster), haute disponibilité du contrôleur (warm-standby) et interface bilingue FR/EN. La doctrine UX ci-dessous s'applique à l'ensemble.

## Brand Personality

Console de production broadcast moderne. La famille de référence : **Grafana, Linear, Resolume, disguise** — dense mais lisible, typographie soignée, statuts qui ressortent immédiatement, aucun élément décoratif qui distrait de l'état.

Trois mots : **fiable, précis, réactif**.

Voix UX française cohérente avec le code. Le vocabulaire technique broadcast (ST 2110, multicast, SHM, timecode, tally) est assumé sans glossaire — l'utilisateur est professionnel. Les messages d'erreur sont actionnables, pas génériques.

## Anti-references

- **Corporate broadcast lourd** : pas de grilles Windows-XP, pas de boutons 3D, pas de palette grise terne style EVS/Studer historique.
- **Gamer / cyberpunk neon** : pas de noir + cyan/magenta néon, pas de glow, pas d'esthétique "hacker movie".
- **SaaS générique** cream + violet, big rounded cards, hero metrics gradient.
- **Bootstrap admin template** : pas de sidebar bleu corporate, pas de badges multicolores partout.
- **Dashboard "demo-friendly"** : l'interface n'est plus optimisée pour être comprise par un visiteur occasionnel — elle est optimisée pour l'opérateur sous pression.

## Design Principles

1. **L'état prime sur tout.** Un container dégradé, un script arrêté, une alerte critique — ces informations doivent sauter aux yeux sans chercher. Les statuts (`running`, `script_stopped`, alertes `info|warning|error`) sont la colonne vertébrale visuelle. Aucun élément décoratif ne concurrence leur visibilité.
2. **Densité lisible.** Production = beaucoup d'information à l'écran simultanément. La densité est assumée, mais structurée par la hiérarchie typographique et le rythme d'espacement. Jamais un écran qui suffoque, jamais un écran qui cache.
3. **Réactivité opérateur.** Les actions critiques (start/stop, redémarrer, basculer une source) sont accessibles en 1 clic depuis n'importe quelle vue. Pas de navigation profonde pour les gestes fréquents.
4. **Trois lumières, un seul outil.** Les thèmes `default` (dark neutre), `light` (Daylight) et `studio` (broadcast warm) sont le même produit dans trois éclairages de régie. Tokens structurels partagés, seuls les couleurs de surface et l'accent changent.
5. **Cohérence française.** Labels, statuts, niveaux d'alerte, messages d'erreur — une seule langue, alignée sur la convention du code.
6. **Zéro ambiguïté sur les statuts critiques.** Un container down n'est jamais identifiable par la couleur seule — toujours couplé avec un label texte. Lisible sur projecteur dégradé, capture N&B, ou écran de contrôle à distance.

## Accessibility & Inclusion

- WCAG AA visé sur le contraste texte et les états de focus.
- Statuts critiques toujours doublés d'un label texte (couleur seule insuffisante).
- Aucune animation décorative qui distrait du monitoring en cours.
- Pas de contrainte `prefers-reduced-motion` formelle, mais le réflexe s'applique.
