# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Journal d'exploitation — « qui a demandé quoi », posé DANS la route.

Le fil `alerts` n'est pas seulement la liste de ce qui va mal : c'est aussi le journal de ce que
les exploitants ont fait (décision 2026-07-27). Encore faut-il pouvoir attribuer l'action.

**Pourquoi la ligne est posée dans la route, et pas au moment du résultat.** Les routes de cet
orchestrateur retournent immédiatement et dispatchent le travail dans un `threading.Thread` (cf.
CLAUDE.md). Or un thread **n'hérite pas** du contexte de requête Flask : à l'endroit où le
déploiement réussit ou échoue, l'identité du demandeur a déjà disparu. La retrouver imposerait de
propager un contexte à la main dans 64 dispatches — beaucoup de surface pour une information qu'on
peut capturer une fois, au bon endroit : **à la demande**.

Le contrat est donc explicite, et il faut le lire comme tel :
  - `journal(...)` écrit UNE ligne « <acteur> a demandé : <action> », au moment du clic ;
  - les lignes ÉMISES ENSUITE par le traitement de fond (« déployé et redémarré », « échec ») n'ont
    pas d'acteur et n'en inventent pas. Un `user` NULL veut dire « la machine », et c'est une
    information : la boucle de surveillance qui redémarre un conteneur ne doit surtout pas être
    attribuée au dernier humain passé par là.
On corrèle les deux par le `vmid` et l'horodatage, qui suffisent à reconstituer l'enchaînement.

Les alertes écrites de façon SYNCHRONE dans une route (86 sites) portent déjà l'acteur sans rien
changer : `db_add_alert` le déduit du contexte de requête (cf. `database._acteur_courant`).
"""
import logging

from .database import db_add_alert

log = logging.getLogger(__name__)


def acteur():
    """Login du demandeur, ou None hors contexte de requête (action machine)."""
    try:
        from flask import has_request_context
        if not has_request_context():
            return None
        from .auth import current_user
        return (current_user() or {}).get("username") or None
    except Exception:
        return None


def journal(action, cible=None, vmid=None, node_id=None, kind=None, niveau="info", params=None):
    """Pose la ligne de DEMANDE dans le fil. À appeler dans la route, AVANT le dispatch en thread.

    `action` : soit une CLÉ i18n `alert.audit.<…>` (forme à utiliser), soit — chemin historique —
    un verbe à l'infinitif ou un groupe nominal court en français. `cible` : libellé lisible de
    l'objet (hostname, nom de projet) — le vmid seul ne dit rien à la relecture, et il ne dira
    plus rien du tout quand le conteneur aura été détruit.

    Le journal d'exploitation se relit APRÈS COUP, souvent par quelqu'un d'autre que celui qui a
    agi : il doit donc se rendre dans la langue de son LECTEUR, pas dans celle de l'auteur du
    geste. D'où les clés, malgré un vocabulaire d'actions restreint et connu.

    Ne lève jamais : un journal ne doit pas faire échouer l'action qu'il décrit."""
    try:
        qui = acteur()
        if isinstance(action, str) and action.startswith("alert."):
            # Forme KEYÉE. La présence d'une cible change la PHRASE, pas un paramètre : « — {cible} »
            # collé en suffixe ne se traduirait pas, et une cible absente laisserait un tiret
            # orphelin. Le helper choisit donc la variante, une fois ici, plutôt que de faire porter
            # ce détail par chacun des six sites d'appel.
            cle = action if cible else action + "_sans_cible"
            p = {"qui": qui or "machine"}
            if cible:
                p["cible"] = cible
            p.update(params or {})
            db_add_alert(cle, niveau, vmid=vmid, node_id=node_id, kind=kind, user=qui, params=p)
        else:
            libelle = f"{action}" + (f" — {cible}" if cible else "")
            db_add_alert(f"{qui or 'machine'} a demandé : {libelle}", niveau,
                         vmid=vmid, node_id=node_id, kind=kind, user=qui)
    except Exception as e:                      # jamais bloquant
        log.debug("journal d'exploitation (%s): %s", action, e)
