# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Inventaire Docker par nœud : ce que l'agent voit RÉELLEMENT, confronté à la base.

Toutes les pages du produit listent la table `containers`. Un conteneur présent sur un nœud
mais absent de cette table n'apparaît donc NULLE PART, et aucun bouton ne peut le détruire :
l'alerte « conteneur orphelin » nommait jusqu'ici un objet inatteignable. Cet écran ferme le
cul-de-sac.

Le tri ne se fait pas au préfixe. Un `rdma-ini-<id>` / `rdma-tgt-<id>` est légitime tant que
le lien `<id>` existe dans `rdma_links` — et devient une fuite dès qu'il n'existe plus. Exclure
les `rdma-*` en bloc, comme le suggère l'intuition, masquerait précisément les orphelins les
plus coûteux (ils continuent de répliquer).

L'appariement est fait sur le COUPLE (nœud, nom), jamais sur le nom seul : un vmid est
réattribué, donc deux nœuds peuvent porter un `bobi-cmp-936` sans rapport l'un avec l'autre.
"""

import logging
import re
import shlex

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm
from ..database import db_add_alert, db_get_node, db_get_nodes, db_get_containers, get_db
from .. import node_driver
from ..vmlocks import verrou_vmid

log = logging.getLogger(__name__)

# Noms produits par l'orchestrateur lui-même (docker_compute._name / docker_driver._name).
_RE_ORCH = re.compile(r"^bobi-(?:cmp|mtl)-(\d+)$")
# Noms produits par le service RDMA (services/rdma/__init__.py:_nom_cible/_nom_initiateur).
_RE_RDMA = re.compile(r"^rdma-(?:ini|tgt)-(\d+)$")
# Garde-fou d'injection : le nom part dans l'URL appelée sur l'agent.
_RE_NOM_SUR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Signature Docker d'un conteneur dont le processus ne peut PAS être tué. Ce n'est pas un refus de
# l'agent ni une panne réseau : c'est le noyau qui ne rendra jamais la main.
_RE_INTUABLE = re.compile(r"did not receive an exit event|could not kill", re.I)


def _diagnostic_intuable(node, nom):
    """Un `docker rm -f` qui échoue sur « did not receive an exit event » n'a qu'UNE cause connue
    ici : le processus du conteneur est en état **D** (sommeil non interruptible) dans le noyau, et
    un état D ne répond ni à SIGKILL ni à quoi que ce soit d'autre.

    On ne le DEVINE pas — on va le CONSTATER sur le nœud (état + pile noyau du PID). Sans ça
    l'exploitant reçoit « refusée par l'agent : could not kill … », c'est-à-dire le vocabulaire de
    Docker pour une cause qui n'est pas la sienne : il cherche du côté de l'agent, des droits ou du
    réseau, alors qu'il n'y a rien à y trouver et que seul un redémarrage du NŒUD libérera le nom.

    Retourne (diagnostic|None, détails) — `None` si l'hypothèse n'est PAS vérifiée : mieux vaut
    rendre l'erreur brute qu'un diagnostic inventé."""
    try:
        rc, out, _ = node_driver.host_exec(
            node,
            "P=$(docker inspect -f '{{.State.Pid}}' %s 2>/dev/null); "
            "[ -n \"$P\" ] && [ \"$P\" != 0 ] || exit 1; "
            "echo \"pid=$P\"; "
            "awk '{print \"state=\" $3}' /proc/$P/stat 2>/dev/null; "
            "head -3 /proc/$P/stack 2>/dev/null" % shlex.quote(nom),
            timeout=15)
    except Exception as e:
        log.warning("inventaire : diagnostic « intuable » de %s impossible : %s", nom, e)
        return None, ""
    if rc != 0 or "state=D" not in (out or ""):
        return None, (out or "")
    pile = [l.strip() for l in (out or "").splitlines() if l.strip().startswith("[<")]
    fonction = ""
    if pile:
        # « [<0>] cm_destroy_id+0x1d6/0x5c0 [ib_cm] » → « cm_destroy_id [ib_cm] »
        m = re.search(r"\]\s+([A-Za-z0-9_]+)\+.*?(\[[a-z0-9_]+\])?\s*$", pile[0])
        if m:
            fonction = " ".join(x for x in m.groups() if x)
    return ("intuable", fonction or "état D, pile noyau indisponible")


# Classes d'un conteneur vu sur un nœud.
#   managed : la base le connaît SUR CE NŒUD → géré, destruction par la page Conteneurs
#   rdma    : lien RDMA vivant → destruction en supprimant le lien, pas le conteneur
#   orphan  : nommé par nous ou par le service RDMA, mais plus rien ne le référence
#   foreign : étranger à l'orchestrateur (lancement manuel, banc, image sans étiquette)
_DESTRUCTIBLE = ("orphan", "foreign")


def _liens_rdma():
    """Identifiants des liens RDMA connus de la base."""
    try:
        with get_db() as db:
            return {int(r[0]) for r in db.execute("SELECT id FROM rdma_links")}
    except Exception as e:                                   # table absente = aucun lien
        log.warning("inventaire : lecture rdma_links impossible (%s)", e)
        return set()


def _index_base():
    """(node_id, docker_name) → ligne de conteneur, pour l'appariement."""
    idx = {}
    for c in db_get_containers():
        nom = c.get("docker_name")
        if nom:
            idx[(c.get("node_id"), nom)] = c
    return idx


def _classer(nom, node_id, idx, liens):
    """→ (classe, detail) pour un conteneur vu sur un nœud."""
    gere = idx.get((node_id, nom))
    if gere:
        return "managed", {"vmid": gere.get("vmid"), "hostname": gere.get("hostname"),
                           "db_status": gere.get("status")}
    m = _RE_RDMA.match(nom)
    if m:
        lid = int(m.group(1))
        if lid in liens:
            return "rdma", {"link_id": lid}
        return "orphan", {"link_id": lid, "cause": "rdma_link_absent"}
    m = _RE_ORCH.match(nom)
    if m:
        return "orphan", {"vmid": int(m.group(1)), "cause": "db_row_absent"}
    return "foreign", {}


@bp.route("/api/inventaire")
@require_login
def api_inventaire():
    """Inventaire Docker de chaque nœud, confronté à la base.

    `?node_id=` limite à un nœud. Un nœud injoignable est rendu AVEC son erreur : une liste
    vide et un agent muet ne veulent pas dire la même chose, et confondre les deux ferait
    croire à un nœud propre.
    """
    nid = request.args.get("node_id", type=int)
    noeuds = [db_get_node(nid)] if nid else db_get_nodes()
    noeuds = [n for n in noeuds if n]

    idx, liens = _index_base(), _liens_rdma()
    out, totaux = [], {"managed": 0, "rdma": 0, "orphan": 0, "foreign": 0}

    for n in noeuds:
        node = dict(n)
        bloc = {"id": node.get("id"), "name": node.get("name"),
                "status": node.get("status"), "items": [], "error": None}
        try:
            lst = node_driver.list_containers(node)
        except Exception as e:
            lst, bloc["error"] = None, str(e)[:200]
        if lst is None:
            bloc["reachable"] = False
            out.append(bloc)
            continue
        bloc["reachable"] = True
        for it in lst:
            nom = (it or {}).get("name") or ""
            if not nom:
                continue
            classe, detail = _classer(nom, node.get("id"), idx, liens)
            totaux[classe] = totaux.get(classe, 0) + 1
            bloc["items"].append({"name": nom, "status": it.get("status"),
                                  "image": it.get("image"), "classe": classe,
                                  "detail": detail,
                                  "destructible": classe in _DESTRUCTIBLE})
        bloc["items"].sort(key=lambda x: ({"orphan": 0, "foreign": 1, "rdma": 2,
                                           "managed": 3}.get(x["classe"], 9), x["name"]))
        out.append(bloc)

    return jsonify({"nodes": out, "totals": totaux})


# `<nom>` et non `<path:nom>` : un nom de conteneur Docker ne contient jamais de « / », et le
# convertisseur `path` rendrait la fin de règle ambiguë.
@bp.route("/api/inventaire/<int:node_id>/<nom>/destroy", methods=["POST"])
@require_perm("containers.delete")
def api_inventaire_destroy(node_id, nom):
    """Détruit un conteneur que l'orchestrateur ne gère plus.

    Refuse tout ce qui est encore référencé : un `managed` se détruit par la page Conteneurs
    (sinon la base resterait en avance sur la réalité, l'inverse exact du problème traité ici),
    et un `rdma` vivant se détruit en supprimant son lien.

    Appel SYNCHRONE, contrairement aux opérations de cycle de vie habituelles : c'est un seul
    appel d'agent borné à 45 s, et un bouton de ménage qui ne dit pas s'il a réussi ne vaut pas
    mieux que l'alerte sans geste qu'il remplace.
    """
    if not _RE_NOM_SUR.match(nom or ""):
        return jsonify({"error": "nom de conteneur invalide"}), 400
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud inconnu"}), 404
    node = dict(node)

    classe, detail = _classer(nom, node_id, _index_base(), _liens_rdma())
    if classe == "managed":
        return jsonify({"error": "managed", "classe": classe, "detail": detail}), 409
    if classe == "rdma":
        return jsonify({"error": "rdma_link_alive", "classe": classe, "detail": detail}), 409

    # Le nom porte parfois un vmid, et les vmid sont RÉATTRIBUÉS : on se sérialise avec les
    # opérations de cycle de vie de ce vmid pour ne pas détruire un conteneur en cours de
    # création qui viendrait de reprendre le numéro.
    m = _RE_ORCH.match(nom)
    vmid = int(m.group(1)) if m else None

    def _detruire():
        return node_driver.container_action(node, nom, "destroy")

    if vmid is not None:
        with verrou_vmid(vmid):
            ok, err = _detruire()
    else:
        ok, err = _detruire()

    if not ok:
        # Avant de rendre le vocabulaire de Docker à l'exploitant : est-ce le cas connu du processus
        # bloqué en état D ? Si oui, on nomme la cause ET la seule sortie — sinon on cherche pendant
        # des jours du côté de l'agent (vécu le 2026-08-30 : le message a remonté jusqu'à moi).
        if _RE_INTUABLE.search(str(err) or ""):
            diag, ou = _diagnostic_intuable(node, nom)
            if diag:
                log.warning("inventaire : %s sur %s est INTUABLE (état D dans %s) — "
                            "seul un redémarrage du nœud libérera le nom",
                            nom, node.get("name"), ou)
                db_add_alert("alert.inventaire.intuable", "warning", node_id=node_id,
                             kind="node", params={"nom": nom, "n": node.get("name"), "ou": ou})
                return jsonify({"error": "intuable", "ou": ou,
                                "detail": str(err)[:300]}), 409
        log.warning("inventaire : destruction de %s sur %s refusée par l'agent : %s",
                    nom, node.get("name"), err)
        return jsonify({"error": "agent", "detail": str(err)[:300]}), 502

    db_add_alert("Conteneur %s détruit sur %s depuis l'inventaire (%s)"
                 % (nom, node.get("name"), classe),
                 "info", vmid=vmid, node_id=node_id, kind="deploy")
    log.info("inventaire : %s détruit sur %s (%s)", nom, node.get("name"), classe)
    return jsonify({"ok": True, "name": nom, "classe": classe})
