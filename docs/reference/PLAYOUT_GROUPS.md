# Groupes de lecture A/B/C…

*Fait foi pour `app/playout_groups.py` (mesuré et décidé le 2026-10-17). Garde : `tests/verif_playout_groups.py`.*

## Le modèle

Des players **ordinaires** (un conteneur = une sortie) réunis en un groupe de N ≥ 2 sorties. Lettre = position
dans le groupe. Identité des membres, du mélangeur et de la console : `instance_uuid`. Aucune option dans le
player, aucun second plugin : la coordination vit dans l'orchestrateur et parle aux players par leurs endpoints
existants (`/file` + `cue`, `/play`, `/pause`, `/queue`).

Persistance : table `playout_groups` (créée par `init_db`). Ce qui est en mémoire (prise en attente, dernier
échec, vues) se reconstitue : la boucle compare ce qu'elle **voit** (`/state` de chaque player) à ce que le
plan veut ; un redémarrage ne recharge rien qui soit déjà en place.

## La répartition est une fonction

`plan(g)` : le k-ième élément à venir (après le curseur, non sauté) va à la sortie `(base + 1 + k) mod N`,
`base` = la sortie de la dernière prise. Avec une sortie à l'antenne, N−1 éléments attendent ; sans, N.
Drop / Skip / ▲▼ / Aller à ne font que modifier la playlist : la répartition change, seules les sorties **en
cue** rechargent. Jamais rechargées d'office : la sortie à l'antenne, une sortie en attente de libération,
une sortie dont le mélangeur désigné utilise l'entrée.

## Play, Cue, Suivant

| Commande | Effet |
|---|---|
| **Play** (sortie) | lance l'élément **déjà cué** (constaté au `/state`), sinon refus signalé. Ouvre une PRISE en attente. |
| **Cue** (sortie) | arrête la sortie et la recue sur l'élément qui lui revient (à l'antenne : elle la quitte). |
| **Suivant** | Play de la sortie qui attend l'élément suivant. |
| mélangeur (mode `mixer`) | le programme qui passe sur l'**entrée désignée** de cette sortie vaut son Play. |

**Play hors ordre** (une sortie qui n'attend pas le suivant), réglage du groupe `out_of_order` : `skip` (défaut —
les éléments dépassés sont marqués sautés, rétablissables), `requeue` (remis juste après l'élément lancé, dans
leur ordre), `refuse` (Play refusé, l'antenne ne bouge pas).

**Enchaînement automatique** (`auto_next`, désactivé par défaut) : la fin CONSTATÉE du clip à l'antenne — `eof`
du player, ou position qui repart du début depuis ses 1,5 dernières secondes (clip en boucle) — vaut Suivant,
une fois par élément. Même règle que tout Play : la sortie qui attend doit être cuée (sinon échec signalé), et
la précédente n'est libérée qu'après constat.

La prise n'est **acquise** que quand le `/state` montre l'élément en lecture (non en pause) : alors seulement
le curseur avance et la sortie précédente passe « à libérer ». Non constatée en 3 s (`TAKE_TIMEOUT_S`) ou
Play refusé : l'antenne reste où elle est, rien n'est libéré, alerte `alert.playout.take_failed` + bandeau.

## Libération

Par défaut **manuelle** (Cue de la sortie). Option **libération automatique** (par groupe, désactivée par
défaut, fenêtre ⚙) : seulement si deux faits sont LUS —

1. **mélangeur désigné** : l'entrée désignée n'est pas *utilisée*. Utilisée = programme ; transition en cours,
   entrante **et** sortante (`afv.on_air` met la sortante d'un fondu AUTO dans ses fermetures — elle reste à
   l'image) ; T-bar (les deux) ; keyer actif ou en mouvement. **L'aperçu ne compte pas** (pas à l'antenne).
   Le DSK du mélangeur est une page HTML : aucun player ne peut y être.
2. **console désignée** : la tranche désignée est **fermée** — fader à −∞ ou coupée (mute de la tranche ou d'un
   VCA qui la commande hors Mute Safe), lecture de `GET /doc?faders=1` partagée avec le fader start
   (`services/audio/fader_triggers.py`) ; **ou fermée par l'audio suit la vidéo** (décision 2026-10-17, quel que soit son
   niveau fermé `closed_db`) : AFV général en service, la tranche suit (`afv.on`) et n'est pas ouverte
   (`afv.open`, momentané, lu dans `GET /doc?live=1` — document lourd, relu au plus 1 fois/s et seulement
   quand le fader ne suffit pas).

Une confirmation manque (conteneur supprimé ou injoignable, entrée/tranche inexistante, désignation
incomplète) : pas de libération, la raison est affichée (`release_block`) et exposée aux macros.

## Désignation

Déclarée par l'humain (⚙), jamais déduite : mélangeur, console, et par sortie l'entrée du mélangeur et la
tranche. La tranche est stockée par son **id** (`strips[].id` du document, unique — jamais le libellé :
homonymes) avec son rang à titre indicatif ; l'écran affiche « n° · nom » (et l'id si deux tranches portent le
même nom). Un id disparu est signalé (« inexistante ») et la libération reste manuelle. Une désignation
enregistrée par rang seul (avant les ids) est résolue en id au prochain enregistrement. Le formulaire **suggère** (★) l'entrée qui reçoit le flux vidéo du player et la tranche dont
l'entrée A reçoit `<hostname>_audio`. Vérifiée à l'enregistrement (contre le déployé) et à l'exécution
(`designation_issues`).

## Mélangeur

Les vues viennent de l'observateur AFV (`afv.subscribe`) : un seul interrogateur par mélangeur, 0,25 s. Pas à
l'image près, par décision.

## Macros

Fournisseur du cœur `playout_group` : actions `next`, `play`, `cue`, `skip`, `drop`, `goto`, `load_playlist`,
`reset`, `trigger`, `auto_release`, `auto_next`, `out_of_order` ; états par groupe (`on_air_output`,
`next_title`, `next_ready`, `remaining`, `last_failure`, `auto_release`, `auto_next`, `out_of_order`,
`designation`…) et par sortie (`output_status`, `output_item`,
`output_release_block`).
