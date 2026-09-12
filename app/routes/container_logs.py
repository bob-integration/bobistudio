# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Interrogation des journaux de conteneurs depuis le contrôleur — GÉNÉRIQUE (tout conteneur,
pas seulement le moteur 2110) et servie par `journalctl` sur l'hôte du nœud, PAS par `docker logs`.

`docker logs` ne fonctionne que tant que le conteneur existe : l'utiliser reviendrait à payer le
pilote journald sans en tirer le bénéfice. Ici on lit le journal de l'HÔTE, donc l'historique d'un
conteneur DÉTRUIT (moteur 2110 en `--rm`, redéploiement, reboot du nœud) reste accessible — c'est
tout l'intérêt du chantier. Cf. `app/journal.py` pour le contrat de nommage/champs.

Permission `containers.deploy` et NON le simple login : ces journaux portent la topologie du site
(IP média, adresses multicast, SDP, tokens de flux). `lines` est PLAFONNÉ EN DUR
(`journal.MAX_LINES`) — sans borne, un `?lines=1e7` ramènerait des centaines de Mo dans la RAM du
contrôleur."""

import logging

from flask import jsonify, request

from . import bp
from ..auth import require_perm
from .. import journal as _journal

log = logging.getLogger(__name__)


def _resolve(vmid):
    """(container, node, docker_name) d'un vmid. Le nom est DÉTERMINISTE (`bobi-mtl-<vmid>` pour le
    moteur 2110, `bobi-cmp-<vmid>` sinon) : on le reconstruit même si le conteneur n'existe plus, ce
    qui est précisément le cas d'usage. `docker_name` en DB fait foi s'il est renseigné.

    Cas LIMITE traité explicitement : un conteneur SUPPRIMÉ n'a plus de ligne en DB → plus de
    `node_id` → on ne saurait plus quel nœud interroger, et la route rendrait 404 alors que le
    journal, lui, a tout gardé. `?node_id=<id>` (et `?name=` pour un nom non standard) permettent
    donc d'interroger l'historique d'un conteneur totalement oublié de la base."""
    from ..database import db_get_container, db_get_node
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id")) if c.get("node_id") else None
    if node is None:
        try:
            node = db_get_node(int(request.args.get("node_id")))
        except (TypeError, ValueError):
            node = None
    name = request.args.get("name") or c.get("docker_name")
    if not name:
        t = (c.get("type") or "")
        if not t:
            try:
                import json as _json
                t = (_json.loads(c.get("deploy_config") or "{}") or {}).get("type") or ""
            except Exception:
                t = ""
        name = (f"bobi-mtl-{vmid}" if t == "2110_io" else f"bobi-cmp-{vmid}")
    return c, node, name


def _logs_payload(vmid):
    """Corps commun des routes de journal. Retourne (payload, status)."""
    try:
        n = int(request.args.get("lines") or 200)
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(n, _journal.MAX_LINES))           # plafond dur (cf. docstring du module)
    c, node, name = _resolve(vmid)
    if not node:
        return {"ok": False, "error": "nœud introuvable pour ce conteneur — s'il a été SUPPRIMÉ "
                                      "de la base, préciser sur quel nœud chercher : "
                                      f"?node_id=<id> (nom cherché : {name})"}, 404
    def _lire(nom):
        return _journal.lire(
            node, nom, lines=n,
            since=request.args.get("since"), until=request.args.get("until"),
            priority=request.args.get("priority"), grep=request.args.get("grep"),
            boot=request.args.get("boot"))

    data, status = _lire(name)
    # Conteneur totalement OUBLIÉ de la base (ni ligne, ni type) : `_resolve` doit deviner le
    # préfixe et retombe sur `bobi-cmp-`. Pour un ancien MOTEUR, ce nom est faux → journal vide,
    # SANS rien dire, alors que l'historique est bien là sous `bobi-mtl-<vmid>`. On tente donc
    # l'autre préfixe avant de conclure — c'est exactement le cas d'usage post-mortem.
    if (status == 200 and not (data.get("lines") or []) and not request.args.get("name")
            and not (c or {}).get("vmid")):
        autre = (f"bobi-mtl-{vmid}" if name == f"bobi-cmp-{vmid}" else f"bobi-cmp-{vmid}")
        d2, s2 = _lire(autre)
        if s2 == 200 and (d2.get("lines") or []):
            data, status, name = d2, s2, autre
    data["vmid"] = vmid
    data["node"] = {"id": node.get("id"), "name": node.get("name"), "host": node.get("host")}
    data["log_driver"] = _journal.driver()
    # RÉTENTION : le journal est plafonné en TAILLE, donc l'ancien est purgé. On publie
    # occupation / plafond / plus ancienne entrée disponible plutôt que de laisser croire à un
    # historique infini (une fenêtre vide peut n'être qu'un trou de rotation).
    if status == 200:
        data["retention"] = _journal.retention(node)
    return data, status


@bp.route("/api/containers/<int:vmid>/logs", methods=["GET"])
@require_perm("containers.deploy")
def container_logs(vmid):
    """Journal durable d'un conteneur (`journalctl` sur l'hôte du nœud).

    Query : `lines` (≤ 2000), `since`/`until` (formats journalctl : « -2h », « today »,
    « 2026-07-25 10:00:00 »), `priority` (0-7, « err » = stderr du conteneur, « 3..4 »),
    `grep` (motif, insensible à la casse), `boot` (index négatif ou boot-id : `-1` = boot
    précédent du nœud).
    Réponse : `{ok, vmid, name, node, source, lines[], count, truncated, retention, note}`.
    `source` vaut `journald` (durable) ou `docker` (REPLI explicite : conteneur pas encore migré
    au pilote journald — ses lignes disparaîtront à sa prochaine recréation)."""
    data, status = _logs_payload(vmid)
    return jsonify(data), status


@bp.route("/api/nodes/<int:node_id>/journal", methods=["GET"])
@require_perm("containers.deploy")
def node_journal_state(node_id):
    """État du journal d'un nœud : persistance, drop-in BOBI posé, limites effectives, occupation,
    plus ancienne entrée. LECTURE SEULE — n'applique aucune prép."""
    from ..database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    st = _journal.verifier(node.get("host"), run=_journal._runner(node))
    st["retention"] = _journal.retention(node)
    st["log_driver"] = _journal.driver()
    return jsonify(st), (200 if st.get("ok") else 502)


@bp.route("/api/nodes/<int:node_id>/journal/apply", methods=["POST"])
@require_perm("settings.edit")
def node_journal_apply(node_id):
    """Prép nœud : journal persistant (`/var/log/journal`) + limites EXPLICITES (drop-in BOBI :
    SystemMaxUse, limitation de débit désactivée — cf. app/journal.py). Idempotent.

    Ne recrée AUCUN conteneur : le pilote de log est figé à la création, donc la bascule vers
    `journald` ne prend effet qu'au prochain (re)déploiement de CHAQUE conteneur. Le seul
    redémarrage est celui de `systemd-journald` (sans perte : les entrées sont déjà sur disque)."""
    from ..database import db_get_node
    from ..database import db_add_alert
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    ok, msg, clef, params = _journal.ensure_journal_durable(node.get("host"), run=_journal._runner(node))
    alert_params = dict(params or {})
    alert_params["n"] = node.get("name") or node_id
    db_add_alert(clef, "info" if ok else "error", node_id=node_id, kind="prep", params=alert_params)
    return jsonify({"ok": ok, "message": msg,
                    "note": "les conteneurs déjà en cours restent en json-file jusqu'à leur "
                            "prochaine recréation"}), (200 if ok else 502)


@bp.route("/api/nodes/<int:node_id>/journal/containers", methods=["GET"])
@require_perm("containers.deploy")
def node_journal_containers(node_id):
    """Conteneurs dont le journal de ce nœud garde une trace — VIVANTS OU NON.

    Alimente le sélecteur de l'onglet « Journaux » du Monitoring. La liste vient du JOURNAL et non
    de `docker ps -a` ni de la table `containers` : sinon elle raterait exactement les conteneurs
    détruits, qui sont la raison d'être de journald ici."""
    from ..database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    data, status = _journal.conteneurs_connus(node)
    return jsonify(data), status
