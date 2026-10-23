# Surfaces de contrôle pour la console audio — cahier des charges (2026-10-17)

Chantier confié à une session DÉDIÉE (décision de l'exploitant, 2026-10-17) : c'est un service d'orchestrateur,
indépendant du code de la console. La session « Audio » (console) reste l'interlocutrice pour tout ce qui doit
changer DANS la console (nouvel endpoint, flux de mesures) : la prévenir plutôt que modifier
`plugins/audio_console/` en parallèle.

## Décisions de l'exploitant

- **Plusieurs surfaces dès la v1.** Elles peuvent servir plusieurs opérateurs OU le même opérateur (deux surfaces
  côte à côte = 16 tranches). Le **monitoring doit suivre** : une surface est rattachée à un opérateur, donc à SON
  monitoring (bus d'écoute de l'opérateur) — PFL de la surface = PFL dans ce monitoring-là.
- **Une surface est liée à UNE console et à UN opérateur** (pas « la console ouverte dans le navigateur »).
- **Modes d'encodeurs v1 : Pan, Trim, Départs aux.**
- Matériel visé : une surface à 8 faders motorisés tactiles + 1 master, encodeurs à couronne de LED, écrans
  par tranche, vu-mètres LED, Ethernet (MCU / HUI et mode réseau natif), extensions possibles. L'exploitant
  l'aura « bientôt » : **mesurer le protocole sur l'appareil réel** (réseau de banc) avant de se fier à une
  documentation.
- **Aucune marque protégée dans le produit** (règle du projet) : pilote générique « MCU / HUI » + profils
  (« surface 8 faders motorisés », « extension 8 faders »), jamais le nom du fabricant dans le code ou l'UI.

## Architecture proposée

- Un service `services/control_surface/` sur le modèle de `services/skaarhoj/` (une connexion par surface, un
  fil par surface, état dans la base, onglet Réglages). Servi par le contrôleur ACTIF seul (HA).
- Configuration par surface : adresse, profil, **console (vmid → plutôt l'emplacement / instance_uuid, cf.
  « Identité d'un conteneur » du CLAUDE.md)**, **opérateur**, page de départ, ordre si plusieurs surfaces du
  même opérateur (surface 1 = tranches 1-8, surface 2 = 9-16).
- Le service agit avec les DROITS de l'opérateur rattaché : zones de la console (`/api/console-zones/<vmid>/…`,
  « canOperate ») — une surface ne touche que les tranches de son opérateur.
- Exposer aux macros (règle OBLIGATOIRE du projet) : actions (changer de page, de mode d'encodeur, rattacher à
  un opérateur) et états (surface connectée, page courante).

## Interface de la console (à utiliser, ne pas réinventer)

Tous ces appels passent par le proxy générique `/api/containers/<vmid>/plugin/<path>` (ou en direct sur le
conteneur, port de contrôle 8082, depuis le contrôleur).

| Besoin | Endpoint de la console | Remarque |
|---|---|---|
| Liste des tranches / bus, niveaux de fader, mutes, VCA | `GET /doc?faders=1` | vue légère (~3 Ko), conçue pour être interrogée à ~10 Hz (déclencheurs « fader start ») : `options` (id, libellé, genre), `faders` {id: gain_db, mute, vca, mute_safe}, `vcas` |
| Document complet (noms, largeur, pan, trim, départs, couleurs de bus) | `GET /doc` | lourd : à la connexion et sur changement de `version`, pas en boucle |
| Écrire un paramètre | `POST /set` `{path, value}` | chemins par ID : `strips.<id>.chain.<eid>.params.gain_db` (fader), `…params.pos` (pan), `…params.gain_db` du trim, `strips.<id>.sends.<sid>.level_db` / `.on` |
| Mute | `POST /mute` `{id, on}` | |
| PFL dans un monitoring | `POST /set` `{path: "buses.<listen>.solo.<id>", value}` | **PFL UN PAR UN par défaut** (activer en éteint les autres du même monitoring) — reproduire la règle de l'interface (`soloApply` dans `control.js`) ; mode du monitoring `pfl` / `afl` / `auto` (auto = PFL tranches, AFL bus) |
| Monitorings (bus d'écoute) | `kind: "listen"` dans `/doc` | chaque opérateur peut avoir le sien ; la surface utilise CELUI de son opérateur |
| Vu-mètres | `GET /state` → `meters.strips/buses[id].peak_db_ch / rms_db_ch` | ⚠ `/state` pèse ~15 Ko : trop pour 25-30 Hz × N surfaces. **Demander à la session Audio un flux de mesures léger** (endpoint ou flux poussé) plutôt que d'interroger `/state` en boucle |
| Valeurs : unités | fader en dB, `-Infinity` = -∞ (voir `SP_FADER` / `fromWire` dans `control.js` pour la courbe d'affichage) | |

Les chemins exacts (ids d'éléments de chaîne fader / pan / trim) se lisent dans `/doc` (`chain[].kind`).
Le contrat des paramètres pilotables est aussi publié en arbre pour les macros (`/api/containers/<vmid>/param_tree`).

## Points d'attention

- **Retour moteur et toucher** : pendant qu'un fader est touché, ne pas le renvoyer en position (sinon la
  main et le moteur se battent) ; reprendre le suivi au relâcher.
- **Pages de 8** alignées sur l'ordre des tranches de la console ; deux surfaces du même opérateur = pages
  consécutives.
- **iPad = référence** pour toute UI de configuration (cibles 44 px) ; libellés par `T(clé, repli)` / i18n.
- **Journaux en anglais.**
- Tester contre la console de banc (vmid 1167, dl360-1) avant la 1160.
