# Cartouchier — décisions de l'exploitant (2026-10-17)

Un plugin INDÉPENDANT de la console audio (gestion des médias, isolé du fil audio temps réel, réutilisable par
plusieurs consoles ou sans console), intégré à la page de la console par des mécanismes génériques.

## Le plugin

- **Deux modes** : une **grille** de touches de taille réglable, avec des **pages** ; ou une **playlist**.
- **Lecteurs partagés** : le son sort par des lecteurs (un flux MXL audio stéréo chacun), **nombre réglable, 2
  par défaut**. Une touche lancée prend un lecteur libre, ou celui qui lui est affecté. En playlist, un **mode
  A/B** alterne les lecteurs : A joue, B tient l'élément suivant prêt, et ainsi de suite.
- **Comportement d'une touche** : **boucle**, **enchaînement** (à la fin, lance une autre touche),
  **exclusif** (lancer une touche arrête les autres de son groupe), **fondu d'arrêt** de durée réglable.
- **Deuxième appui en cours de lecture : réglable par touche** (arrêt avec son fondu, pause / reprise, ou relance
  depuis le début).
- **Médias** : **les deux** sources — la base des médias existante (registre, import, partages, comme le player)
  ET un dépôt rapide de fichier à la volée.
- **Niveaux** : **normalisation EBU R128** au chargement vers une cible commune, plus un **gain par touche**.
- **Affichage** : **décompte restant** (couleur d'alerte dans les dernières secondes, seuil réglable),
  **titre**, **forme d'onde** avec tête de lecture, **intro / fin** (marqueurs, avec leur propre décompte).
- **Un cartouchier commun** : les pages sont celles du cartouchier, partagées par tous. Pour séparer deux postes,
  on déploie deux cartouchiers.

## Intégration dans la console

- **Panneau cartouchier** dans la page de la console (ouvert depuis la barre, comme Scènes) : les touches et l'état
  des lecteurs, sans changer de page. Il réutilise l'interface du plugin (contrat `mount(el, vmid, ctx)`).
- **Décompte sur la tranche** : une tranche alimentée par un lecteur affiche automatiquement titre, décompte et
  intro / fin. Mécanisme générique : la console sait quel flux alimente la tranche, l'orchestrateur sait qui le
  produit, le producteur publie par flux `{title, remaining_ms, intro_ms, outro_ms, state}`. Le player et le
  recorder pourront publier la même chose.
- **Fader start** : la mécanique existe (`app/fader_triggers.py`, ≈ 200 ms). En plus : un **lien en une étape**
  dans la configuration de la tranche, « fader start → lecteur N ». **Ce que fait la fermeture du fader est
  réglable par lien** : arrêt (et élément suivant prêt en playlist), pause, ou rien.

## Pilotage externe

- **Macros (obligatoire)** : lancer / arrêter une touche, changer de page, lecteur suivant, A/B ; états lisibles :
  lecteurs, temps restant, élément en cours.
- **Ember+** : touches et lecteurs exposés dans l'arbre (emplacements).
- **Surfaces matérielles** (pupitres à touches, surfaces MCU, etc.) : PAS dans le plugin. Ce sont les SERVICES de
  surface qui s'appuient sur les actions et les états publiés par le plugin, comme pour tout autre plugin.

## Ordre proposé

1. Le plugin seul : lecteurs, grille et playlist, comportements, médias, R128, affichage, macros, Ember+.
2. L'intégration console : publication par flux, décompte sur la tranche, lien fader start, panneau.

## Demandes suivantes (2026-10-17)

### Édition rapide et réglages globaux (en cours)
- Mode édition : le gestionnaire de médias s'affiche à côté de la grille ; glisser (souris, doigt) ou toucher-toucher
  un média sur une touche ou dans la playlist = affectation immédiate et enregistrée, annulable.
- Défauts globaux des touches (fondu d'arrêt, deuxième appui, boucle, enchaînement, groupe), hérités sauf
  surcharge explicite, « revenir au défaut ».

### Monitoring audio (décidé)
Un bouton d'écoute comme dans la console, avec les trois usages :
- **écouter un lecteur au choix** (ce qui part réellement vers la console) ;
- **pré-écoute hors antenne** : écouter un son AVANT de le lancer, sans qu'il sorte sur un lecteur (un PFL du
  cartouchier, propre au navigateur) ;
- **pré-écoute dans l'éditeur** : le clip, une sélection, autour d'un point In / Out.
Mécanisme existant à réutiliser : les flux déclarés `control.streams` (app/plugin_streams.py), comme l'écoute de la
console.

### Éditeur de clip et montage (décidé)
- **Dans le cartouchier**, ouvert depuis la touche ou l'élément de playlist, en grand : forme d'onde zoomable,
  écoute, cibles de 44 px (iPad).
- **Non destructif par défaut** (In, Out, fondus, gain, coupes = réglages de la touche ; le fichier n'est jamais
  modifié, le même son sert à plusieurs touches avec des découpes différentes), **plus « Enregistrer comme nouveau
  média »** dans la base des médias pour le réutiliser ailleurs (player, autres cartouchiers).
- Fonctions : **In / Out**, **fondus** d'entrée et de sortie, **couper un passage** (avec micro-fondu aux
  raccords), **assembler plusieurs sons** bout à bout avec fondus enchaînés réglables, **enveloppe de volume**
  (points sur la forme d'onde), **gain et normalisation**.

### Sauvegarder, exporter, importer, rappeler (décidé)
- **Par mode, séparément** : une GRILLE (ses pages et touches) ou une PLAYLIST se sauvegarde et se rappelle
  indépendamment — rappeler la playlist d'une émission ne touche pas aux jingles de la grille.
- **Une page seule** de la grille aussi : sauvegardée, exportée, rappelée dans n'importe quelle page, y compris d'un
  autre cartouchier.
- **Bibliothèque commune à l'installation** (rappelable depuis tout cartouchier), export / import en fichier pour une
  autre installation — comme les shows de la console.
- **Rappel sans coupure** : ce qui joue continue jusqu'au bout (ou jusqu'à son arrêt) ; seules les touches / la
  playlist changent.
- Réutiliser l'existant plutôt que d'inventer : préréglages de plugin (plugin_store, export / import, rappel depuis un
  pupitre — commit 4da0595) ou mécanisme générique des shows (app/plugin_shows.py) ; exposer le rappel aux macros.
