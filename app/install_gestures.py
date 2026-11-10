# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""What is left to DO on a fresh install — measured, never guessed.

★ WHY THIS MODULE EXISTS (2026-11-08). The first-run wizard closed on a hard-coded list of
« the two gestures that follow »: enrol a node, then designate the container NIC. On an
all-in-one install the node is enrolled BY the installer — so the wizard asked for something
already done, and stayed silent on the one thing that was actually missing: the runtime
images. Nothing can be deployed without them, and nowhere in the install flow is the build
announced (the installer warns mid-stream, forty lines before its own verdict, and names no
place in the product). Measured on a fresh VM the same day.

A hard-coded list cannot be right twice: the same screen serves a controller alone, a node
alone and an all-in-one. So the list is MEASURED here, once, and both readers use it — the
wizard's last step and anything else that wants to tell the operator where they are.

Three rules this module keeps:

  · AN ABSTENTION IS PUBLISHED. A sub-probe that cannot measure (agent unreachable, image
    inventory stale) lands in `unmeasured`, never in `gestures`. A gesture list that silently
    drops what it could not see is indistinguishable from one that found nothing left to do —
    and it is precisely on a half-installed machine that probes fail.
  · BLOCKING AND MERELY-USEFUL ARE NOT MIXED. `blocking` says « no container can run until
    this is done ». The WebRTC gateway is not blocking: without it the monitoring panel stays
    black, which is worth saying and is not worth numbering next to « you have no node ».
  · NO SECOND SOURCE OF TRUTH. Node network state comes from `node_driver.etat_reseau_conteneurs`
    (tri-state, measured on the host — the DB column only says what was ASKED, cf. the Z8 of
    2026-10-30), and image state from `routes.images.node_images_state`. Re-deriving either
    here would give the operator a verdict that contradicts the page they are sent to.
"""
import json
import logging

log = logging.getLogger(__name__)


def _images_manquantes(node):
    """([tags à obtenir], mesure_incomplète) for this node, from the images policy point."""
    from .routes import images as _img
    etat = _img.node_images_state(node) or {}
    tags, flou = [], False
    for which, v in etat.items():
        if not v.get("expected"):
            continue
        if v.get("unknown"):                 # inventaire agent KO : on ne tranche pas
            flou = True
            continue
        if v.get("present"):
            continue
        # Le tag UTILISÉ est vide sur une installation neuve (la colonne du nœud n'a jamais été
        # écrite) : c'est justement le cas qui nous intéresse, donc on nomme le tag ATTENDU.
        tag = (v.get("tag") or "").strip()
        if not tag:
            try:
                tag = _img._image_tag(which)
            except Exception as e:           # noqa: BLE001
                log.debug("_images_manquantes(%s): tag de %s illisible (%s)", node.get("id"), which, e)
                tag = which
        tags.append(tag)
    return sorted(set(tags)), flou


def remaining_gestures():
    """{"gestures": [...], "unmeasured": [...]} — ordered, measured, ready for the UI.

    A gesture is `{code, blocking, link, params}`; `params` feeds the i18n message, so it
    carries node names and image tags rather than a pre-built sentence (the catalogue owns the
    wording, in both languages).
    """
    from .database import db_get_nodes
    gestures, unmeasured = [], []

    try:
        nodes = db_get_nodes() or []
    except Exception as e:                   # noqa: BLE001
        log.warning("remaining_gestures: nodes illisibles (%s)", e)
        return {"gestures": [], "unmeasured": ["nodes"]}

    # ── 1. Aucun nœud : rien d'autre n'est MESURABLE ────────────────────────
    # Réseau et images sont des propriétés D'UN NŒUD. Les annoncer sans nœud serait deviner, et
    # les taire ferait croire qu'il n'y a qu'un geste. On rend le premier geste, et on dit que la
    # suite se mesurera après.
    if not nodes:
        return {"gestures": [{"code": "node", "blocking": True,
                              "link": "/settings#deploiement/nodes", "params": {}}],
                "unmeasured": []}

    sans_reseau, a_construire, tags, a_redemarrer = [], [], [], []
    for node in nodes:
        nom = node.get("name") or str(node.get("id"))

        # ── 2. Réseau des conteneurs : état RÉEL, tri-état conservé ─────────
        if not (node.get("agent_url") or "").strip():
            unmeasured.append({"code": "network", "node": nom})
        else:
            try:
                from . import node_driver
                present = (node_driver.etat_reseau_conteneurs(node) or {}).get("present")
            except Exception as e:           # noqa: BLE001
                log.debug("remaining_gestures: réseau de %s non mesuré (%s)", nom, e)
                present = None
            if present is None:
                unmeasured.append({"code": "network", "node": nom})
            elif not present:
                sans_reseau.append(nom)

        # ── 3. Images d'exécution ───────────────────────────────────────────
        try:
            manquantes, flou = _images_manquantes(node)
        except Exception as e:               # noqa: BLE001
            log.debug("remaining_gestures: images de %s non mesurées (%s)", nom, e)
            manquantes, flou = [], True
        if flou:
            unmeasured.append({"code": "images", "node": nom})
        if manquantes:
            a_construire.append(nom)
            tags += manquantes

        # ── 3 bis. io2110 : un REDÉMARRAGE en attente ──────────────────────
        # La préparation hôte du moteur 2110 (noyau MTL, hugepages 1G au cmdline, DDP) ne prend
        # effet qu'au reboot — `install-node.sh` le dit en `warn`, au milieu de sa sortie, et
        # personne ne relit la sortie d'une installation. Un nœud io2110 jamais redémarré n'a
        # ni hugepages ni DDP : le moteur ne démarrera pas, et aucun compteur ne dira pourquoi.
        try:
            caps = json.loads(node.get("capabilities") or "[]")
        except Exception:
            caps = []
        if "io2110" in caps:
            try:
                from . import mtl
                v = mtl.verifier(node.get("host")) or {}
                if v.get("error"):
                    unmeasured.append({"code": "io2110_reboot", "node": nom})
                elif v.get("reboot_needed"):
                    a_redemarrer.append(nom)
            except Exception as e:           # noqa: BLE001
                log.debug("remaining_gestures: préparation MTL de %s non mesurée (%s)", nom, e)
                unmeasured.append({"code": "io2110_reboot", "node": nom})

    # Un geste par NATURE, nœuds nommés dedans : une liste d'un geste par nœud serait illisible
    # sur un parc, et c'est le même geste au même endroit.
    if sans_reseau:
        gestures.append({"code": "network", "blocking": True,
                         "link": "/settings#deploiement/nodes",
                         "params": {"nodes": ", ".join(sans_reseau)}})
    if a_construire:
        gestures.append({"code": "images", "blocking": True,
                         "link": "/settings#deploiement/build",
                         "params": {"nodes": ", ".join(a_construire),
                                    "tags": ", ".join(sorted(set(tags)))}})

    if a_redemarrer:
        gestures.append({"code": "io2110_reboot", "blocking": True,
                         "link": "/settings#deploiement/nodes",
                         "params": {"nodes": ", ".join(a_redemarrer)}})

    # ── 4. mTLS du plan de contrôle : important, pas bloquant ───────────────
    # `node_driver` ne parle HTTPS à un agent que si `tls_ready` est posé ET que la CA existe :
    # sinon c'est du HTTP CLAIR. Ça marche, et c'est le premier facteur d'authentification du
    # plan de contrôle qui manque — donc un geste qu'on regrette, pas un geste qui débloque.
    #
    # ⚠ LA MESURE EST `tls_ready`, PAS LA PRÉSENCE DE LA CA. Mon premier jet testait
    # `ca.ca_available()` : sur l'installation d'essai du 2026-11-08 la CA existait déjà (posée à
    # l'installation) alors que le nœud était à `tls_ready=0` — le geste restait donc muet
    # exactement quand il y avait quelque chose à faire. C'est le nœud qui se migre
    # (`routes/enrollment.py:api_node_tls_rotate`), et l'onglet Sécurité fait les deux.
    en_clair = [node.get("name") or str(node.get("id")) for node in nodes
                if (node.get("agent_url") or "").strip() and not node.get("tls_ready")]
    if en_clair:
        gestures.append({"code": "control_plane_tls", "blocking": False,
                         "link": "/settings#securite",
                         "params": {"nodes": ", ".join(en_clair)}})

    # ── 5. Passerelle WebRTC : utile, pas bloquante ─────────────────────────
    # ⚠ LE LIEN VA SUR « SERVICES », PAS SUR « WEBRTC ». Un service dont la clé `*_enabled` est
    # fausse est RETIRÉ des onglets de Réglages (`routes/pages.py`) : sur une installation neuve
    # `webrtc_enabled` est faux, donc `#webrtc` n'existe pas — précisément sur la machine à qui
    # on annonce le geste. C'est l'onglet Services qui liste le service éteint et permet de
    # l'allumer (mesuré sur l'installation d'essai du 2026-11-08 : 36 onglets rendus, aucun
    # « webrtc »).
    try:
        from . import monitor
        if not monitor.gateway_ready():
            gestures.append({"code": "webrtc_gateway", "blocking": False,
                             "link": "/settings#services", "params": {}})
    except Exception as e:                   # noqa: BLE001
        log.debug("remaining_gestures: passerelle WebRTC non mesurée (%s)", e)
        unmeasured.append({"code": "webrtc_gateway", "node": ""})

    return {"gestures": gestures, "unmeasured": unmeasured}
