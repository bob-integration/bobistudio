# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Convention de NUMÉROTATION — source UNIQUE. « Le 0 ne doit pas exister. »

Décision utilisateur du 2026-08-11, exécutée le 2026-08-13, élargie le même jour à TOUS les
producteurs indexés (pas seulement le moteur 2110) puis au contrat générique d'entrée.

## La règle, unique et sans exception

`idx` reste l'indice de TABLEAU, 0-based : il indexe `tx_slots[]`, `rx_flows[]`, les pools du
moteur, les listes de `params`. Un indice de tableau qui commencerait à 1 ferait de chaque
boucle une occasion de décalage d'un cran, pour un gain nul.

**Tout ce qui SORT de l'indice sous forme de chaîne est 1-based**, et passe par ce module :

| ce qui est nommé | avant | après |
|---|---|---|
| flux MXL | `hn_0`, `hn_audio_0`, `hn_anc_0` | `hn_1`, `hn_audio_1`, `hn_anc_1` |
| clé de sortie TX | `tx0_shm` | `tx1_shm` |
| clé d'entrée (câblage) | `input_0`, `input_v_0` | `input_1`, `input_v_1` |
| slot du registre NMOS | `tx0:v`, `v:0` | `tx1:v`, `v:1` |
| libellé SDP | `s=<hn> TX0` | `s=<hn> TX1` |

Les graines d'UUID NMOS et de SSRC RTP, elles, restent sur l'indice BRUT — cf. la section
suivante : c'est ce qui préserve l'identité au lieu de la renouveler.

## L'identité, elle, ne bouge PAS — et ce n'est pas un hasard

L'arbitrage du 2026-08-13 acceptait de perdre les UUID NMOS. Il s'avère que ce n'est pas
nécessaire, et la raison mérite d'être écrite : `_registry_id()` (services/nmos) retrouve une
ressource par `(instance_uuid, bind_slot, essence, kind)` et rend l'`id` DÉJÀ enregistré ; la
graine `_stable_uuid(seed)` n'est qu'un repli pour un slot INCONNU du registre. Or la migration
décale `nmos_resources.bind_slot` en même temps que le code (`tx0:v` → `tx1:v`) : le lookup
tombe juste, l'ancien UUID est rendu tel quel.

Donc : **les UUID NMOS des slots existants sont PRÉSERVÉS, et les abonnements IS-05 des
récepteurs tiers survivent.** Même raisonnement pour les SSRC RTP, dont la graine reste sur
l'indice brut.

⚠ Le corollaire est un piège : migrer le code SANS migrer `bind_slot` ne casserait rien
visiblement — le registre sèmerait simplement une ressource NEUVE à côté de l'ancienne, qui
resterait annoncée. On se retrouverait avec deux jeux de senders NMOS pour le même signal.
Les deux migrations sont indissociables.

## Les deux miroirs à tenir

1. `plugins/2110_io/docker/controller.py` porte sa propre copie de `numero()` (il tourne dans
   l'image du moteur, il ne peut pas importer `app`). Le moteur NOMME les flux, l'orchestrateur
   les RETROUVE par ce nom : une divergence casse tout le câblage en silence.
2. Les `plugins/<type>/script.py` construisent leurs clés d'entrée inline (même raison). Le
   gabarit est `"input_%d" % (i + 1)`.

## Vérification

`tools/verif_numerotation.py` échoue s'il reste, hors de ce module, une construction manuelle de
clé indexée (`"input_%d" % i`, `"tx%d_shm" % i`, …). C'est ce test qui empêche la convention de
se faire grignoter au prochain ajout.
"""


def numero(idx):
    """Indice de tableau (0-based) → numéro PUBLIC (1-based). Passage OBLIGATOIRE de toute mise
    en chaîne d'un indice de slot, de flux ou d'entrée."""
    return int(idx) + 1


def indice(n):
    """Réciproque de `numero()` : numéro public lu (tag NMOS, clé, nom de flux) → indice de
    tableau. Toute relecture d'un numéro DOIT passer par ici."""
    return int(n) - 1


# ── Clés d'ENTRÉE (contrat générique du câblage : `state_field` des manifestes) ───────────────
# Écrites par `app/routes/cabling._apply_wire`, relues par les scripts de plugins. Trois formes :
# générique (`input_{n}`), et les variantes vidéo/audio du plugin `delay`.

def cle_input(idx, fmt=False):
    return "input_%d%s" % (numero(idx), "_fmt" if fmt else "")


def cle_input_v(idx, fmt=False):
    return "input_v_%d%s" % (numero(idx), "_fmt" if fmt else "")


def cle_input_a(idx, fmt=False):
    return "input_a_%d%s" % (numero(idx), "_fmt" if fmt else "")


# ── Clés de SORTIE TX du moteur 2110 ─────────────────────────────────────────────────────────

def cle_tx_shm(idx, fmt=False):
    return "tx%d_shm%s" % (numero(idx), "_fmt" if fmt else "")


def cle_tx_audio_shm(idx, fmt=False):
    return "tx_audio%d_shm%s" % (numero(idx), "_fmt" if fmt else "")


def cle_tx_anc_shm(idx, fmt=False):
    return "tx_anc%d_shm%s" % (numero(idx), "_fmt" if fmt else "")


# ── Noms de FLUX MXL ─────────────────────────────────────────────────────────────────────────
# ⚠ MIROIR de `plugins/2110_io/docker/controller.py`. L'UUID MXL d'un flux est dérivé de son NOM
# (uuid5) : ces trois fonctions décident donc de l'identité des flux.

def flux_video(hostname, idx):
    return "%s_%d" % (hostname, numero(idx))


def flux_audio(hostname, idx):
    return "%s_audio_%d" % (hostname, numero(idx))


def flux_anc(hostname, idx):
    return "%s_anc_%d" % (hostname, numero(idx))


# ── Slot du registre NMOS (`nmos_resources.bind_slot`) ───────────────────────────────────────

def slot_tx(idx, suffixe):
    """`slot_tx(0, "v")` → `tx1:v`. `suffixe` : `v`, `d`, ou `a<n>`."""
    return "tx%d:%s" % (numero(idx), suffixe)


def slot_rx(essence_courte, idx):
    """`slot_rx("v", 0)` → `v:1`. `essence_courte` : `v`, `a` ou `d`."""
    return "%s:%d" % (essence_courte, numero(idx))
