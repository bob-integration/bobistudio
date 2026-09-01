# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Niveaux de tally — l'affectation, et elle vit ICI et nulle part ailleurs.
#
# ★ POURQUOI PAS DANS L'ONGLET TSL. Le tally n'était réglable que depuis la page du protocole TSL,
# ce qui le cachait derrière un TRANSPORT — alors qu'il est consommé par TSL, par IS-07, par les
# mélangeurs et par les multiviews, et qu'il est découpé par production. L'allocation est la seule
# chose vraiment INTER-PROTOCOLE : c'est là qu'un chevauchement se voit, et nulle part ailleurs.
# Le détail propre à chaque protocole (hôte et port d'une connexion, activation d'un Receiver
# IS-07) reste dans sa page : le déplacer forcerait à tout dupliquer au troisième protocole.

import logging

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm

log = logging.getLogger(__name__)
from ..database import (db_get_tally_levels, db_add_tally_level, db_set_tally_level_nom,
                        db_set_tally_levels_order, db_delete_tally_level, db_get_projects,
                        db_get_tsl_connections)


def lien_de_config(type_, vmid=None):
    """Où va-t-on RÉGLER ce consommateur de tally.

    ★ RÉSOLU CÔTÉ SERVEUR, et il le faut : la rubrique qui héberge un type est déclarée dans son
    manifeste (`nav.section`) et quatre types ont déjà déménagé. Un lien câblé en dur dans une
    page enverrait sur une rubrique où l'onglet n'existe plus — une redirection qui ment est pire
    qu'un lien absent, elle affiche une page plausible.

    ⚠ Le tally n'étant PAS réglable depuis le plan (décision : l'affectation reste dans la page du
    protocole ou du plugin), ce lien est le seul chemin entre « je vois que ce mur consomme mon
    niveau » et « je vais changer ça ». Sans lui, le plan est un constat sans issue."""
    from .. import plugins
    m = plugins.get(type_) or {}
    sec = ((m.get("nav") or {}).get("section")) or "traitements"
    return "/%s#%s%s" % (sec, type_, ("/%s" % vmid) if vmid else "")


def _porteurs():
    """Nom lisible de chaque porteur, pour que la page ne montre pas des `project#7`."""
    noms = {}
    try:
        for p in db_get_projects():
            noms[("project", p["id"])] = p.get("name") or ("Projet #%s" % p["id"])
    except Exception:
        pass
    try:
        for c in db_get_tsl_connections():
            noms[("connection", c["id"])] = c.get("name") or ("TSL #%s" % c["id"])
    except Exception:
        pass
    return noms


@bp.route("/api/tally/levels", methods=["GET"])
@require_login
def tally_levels():
    noms = _porteurs()
    out = []
    for n in db_get_tally_levels():
        out.append(dict(n, owner_nom=noms.get((n.get("owner_kind"), n.get("owner_id")))))
    return jsonify(out)


@bp.route("/api/tally/levels", methods=["POST"])
@require_perm("settings.edit")
def tally_level_add():
    d = request.get_json(silent=True) or {}
    genre = d.get("owner_kind")
    if genre not in (None, "", "project", "connection"):
        return jsonify({"error": "owner_kind inconnu"}), 400
    return jsonify({"uuid": db_add_tally_level(d.get("nom") or "", genre or None,
                                               d.get("owner_id"))})


@bp.route("/api/tally/levels/<uuid>", methods=["DELETE"])
@require_perm("settings.edit")
def tally_level_del(uuid):
    """Supprime un niveau et resserre les numéros. Les configurations qui le citaient gardent son
    UUID — elles pointent alors un niveau inexistant, ce que les lecteurs traitent déjà comme
    « aucun niveau ». On ne les nettoie PAS : un niveau supprimé par erreur se recrée, des
    références effacées ne se retrouvent pas."""
    if not db_delete_tally_level(uuid):
        return jsonify({"error": "niveau inconnu"}), 404
    return jsonify({"ok": True})


@bp.route("/api/tally/levels/order", methods=["POST"])
@require_perm("settings.edit")
def tally_levels_order():
    """Réordonne les niveaux. `{"uuids": [...]}` — la liste ENTIÈRE, dans l'ordre voulu.

    La ligne bouge ET son numéro avec : `num` n'est qu'un rang d'affichage, personne ne le cite.
    Les configurations et les conteneurs parlent en `uuid`, qui ne bouge jamais — c'est ce qui
    rend le réordonnancement gratuit, ici comme dans deux ans."""
    d = request.get_json(silent=True) or {}
    ids = d.get("uuids")
    if not isinstance(ids, list):
        return jsonify({"error": "`uuids` : liste attendue"}), 400
    return jsonify({"ok": True, "places": db_set_tally_levels_order(ids)})


@bp.route("/api/tally/levels/<uuid>", methods=["PUT"])
@require_perm("settings.edit")
def tally_level_set(uuid):
    """Renomme un niveau. Son UUID ne change jamais — c'est ce que citent les configurations ;
    son numéro se change en réordonnant, pas ici."""
    d = request.get_json(silent=True) or {}
    if "nom" not in d:
        return jsonify({"error": "seul `nom` est modifiable"}), 400
    db_set_tally_level_nom(uuid, d.get("nom"))
    return jsonify({"ok": True})


@bp.route("/api/tally/consumers", methods=["GET"])
@require_login
def tally_consumers():
    """Qui LIT chaque niveau, côté conteneurs : {level_id: [{vmid, hostname, type, detail}]}.

    ★ POURQUOI CÔTÉ SERVEUR. Un consommateur ne se lit pas dans une colonne : c'est un multiview
    dont une tuile déclare un niveau, un overlay de texte, ou n'importe quel plugin qui publie le
    hook `tally_targets` — et ce hook, seul l'orchestrateur sait l'exécuter. Le déduire dans le
    navigateur aurait voulu dire recopier là-bas la connaissance de chaque modèle de données, et
    en oublier un au plugin suivant : le mur d'un exploitant restait invisible dans le plan alors
    qu'il consommait bel et bien un niveau.

    ★ LE REPLI SUR LA PRODUCTION COMPTE AUSSI. Une tuile sans niveau explicite suit ceux de son
    projet (c'est le distributeur qui le résout). Ne montrer que les niveaux ÉCRITS EN DUR ferait
    dire « personne » sur les niveaux les plus utilisés du site.
    """
    import json as _json
    from ..database import db_get_containers, db_get_tally_levels_of
    from .. import plugins as _plg

    par_niveau, niv_projet = {}, {}

    def _ajouter(niveaux, ct, type_, detail, agit):
        """`agit` : le consommateur ALLUME-t-il vraiment quelque chose sur ce niveau ?

        ★ DÉCLARER N'EST PAS CONSOMMER, ET LES DEUX DOIVENT SE VOIR. Un mur peut affecter un
        niveau à toutes ses tuiles et laisser « Rouge » et « Vert » décochés : il ne s'allumera
        jamais. Le masquer, c'est répondre « personne » à l'exploitant qui vient justement de
        l'affecter ; le compter comme actif, c'est lui cacher pourquoi rien ne se passe. On le
        montre, et on dit lequel des deux cas c'est."""
        for n in niveaux or ():
            n = str(n or "").strip()
            if not n:
                continue
            liste = par_niveau.setdefault(n, [])
            for x in liste:
                if x["vmid"] == ct.get("vmid") and x["detail"] == detail:
                    x["agit"] = x["agit"] or agit
                    x["n"] = x.get("n", 1) + 1
                    break
            else:
                liste.append({"vmid": ct.get("vmid"), "hostname": ct.get("hostname") or "",
                              "type": type_, "detail": detail, "agit": agit, "n": 1,
                              "lien": lien_de_config(type_, ct.get("vmid"))})

    def _du_projet(pid):
        if pid not in niv_projet:
            try:
                niv_projet[pid] = db_get_tally_levels_of("project", pid) if pid else []
            except Exception:
                niv_projet[pid] = []
        return niv_projet[pid]

    def _liste(v, pid):
        """Un champ `tally_level` : liste d'UUID, scalaire hérité, ou vide = ceux de la
        production."""
        if not isinstance(v, list):
            v = [v] if v not in (None, "", 0, "0") else []
        if not v:
            return _du_projet(pid)
        return v

    for ct in db_get_containers():
        try:
            dc = ct.get("deploy_config")
            dc = _json.loads(dc) if isinstance(dc, str) else (dc or {})
        except (ValueError, TypeError):
            continue
        if not dc:
            continue
        type_ = dc.get("type") or ""
        params = dc.get("params") or {}
        pid = ct.get("project_id")
        if type_ == "multiview":
            # ⚠ LE MUR RESTE SUR SON CHEMIN — même raison que dans le distributeur : c'est le
            # composant le plus sensible du produit, il ne publie pas `tally_targets`, et on ne
            # le fait pas passer sur du code neuf pour l'élégance.
            for fc in (params.get("flux_config") or []):
                if not isinstance(fc, dict):
                    continue
                niv = _liste(fc.get("tally_level"), pid)
                if not niv:
                    continue
                agit = bool(fc.get("tally_red") or fc.get("tally_green")
                            or fc.get("label_source") == "protocol")
                _ajouter(niv, ct, type_, "tuiles", agit)
            for ov in (params.get("overlays") or []):
                if not isinstance(ov, dict) or (ov.get("kind") or "") != "text":
                    continue
                if (ov.get("text_source") or "local") != "tsl":
                    continue
                niv = _liste(ov.get("tally_level"), pid)
                if niv:
                    _ajouter(niv, ct, type_, "incrustations",
                             bool(ov.get("tally_red") or ov.get("tally_green")))
            continue
        try:
            hook = _plg.get_hook(type_, "tally_targets")
        except Exception:
            hook = None
        if not hook:
            continue
        try:
            cibles = hook(params, {"vmid": ct.get("vmid"), "project_id": pid}) or []
        except Exception:
            continue
        for cible in cibles:
            if not isinstance(cible, dict):
                continue
            # Un plugin qui publie une cible la VEUT : le hook n'est appelé que pour ce qu'il
            # déclare, il n'y a pas ici d'équivalent des cases décochées du mur.
            _ajouter(_liste(cible.get("niveau"), pid), ct, type_,
                     str(cible.get("cle") or ""), True)
    return jsonify(par_niveau)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# IS-07 ENTRANT — le MÊME objet qu'une connexion TSL
# ══════════════════════════════════════════════════════════════════════════════════════════════
# ★ Ce qu'un exploitant choisit n'est pas « quelles sorties peuvent recevoir un tally » mais
# « quel protocole écrit dans quel niveau ». On a d'abord publié un Receiver par groupe de sortie
# BCP-002-01 — 99 sur le banc pour 6 utiles : la lecture littérale de la BCP, qui répondait à
# côté de la question. Une connexion = un niveau, plus une correspondance qui dit quelle Source
# de l'émetteur désigne quel signal chez nous. Cette correspondance s'édite dans la page Labels,
# à côté des colonnes TSL, parce que c'est le même geste.

def _relancer_is07():
    """Republie le modèle et coupe les écoutes devenues sans objet.

    ⚠ ON ARRÊTE AVANT DE RECONSTRUIRE. Une connexion supprimée ou désactivée garderait sinon son
    client en vie, qui continuerait d'écrire dans un niveau au nom d'une connexion qui n'existe
    plus — un rouge sans propriétaire, que rien dans l'interface ne permet plus d'éteindre."""
    try:
        from services.nmos import is07_entrant as _i7e
        from services import nmos as _n
        _i7e.arreter_tout()
        _n.rebuild_model()
    except Exception as e:
        log.warning("IS-07 : relance après changement de connexion (%s)", e)


@bp.route("/api/tally/is07/connections", methods=["GET"])
@require_login
def tally_is07_connections():
    """Les connexions IS-07 entrantes : quel protocole écrit dans quel niveau, et son état."""
    from services.nmos import is07 as _i7, is07_entrant as _i7e
    from services import nmos as _n
    from ..database import db_get_is07_connections
    vivants = _i7e.etat()
    out = []
    for c in db_get_is07_connections():
        rid = _i7._rid_conn(c["id"])
        act = ((_n._recv_state.get(rid) or {}).get("active") or {})
        tp = (act.get("transport_params") or [{}])[0]
        out.append(dict(c, receiver_id=rid,
                        master_enable=bool(act.get("master_enable")),
                        connection_uri=tp.get("connection_uri"),
                        client=vivants.get(rid)))
    return jsonify({"actif": _i7.entrant_actif(), "connections": out})


@bp.route("/api/tally/is07/connections", methods=["POST"])
@bp.route("/api/tally/is07/connections/<int:cid>", methods=["PUT"])
@require_perm("settings.edit")
def tally_is07_connection_set(cid=None):
    from ..database import db_upsert_is07_connection
    d = dict(request.get_json(silent=True) or {})
    if cid:
        d["id"] = cid
    u = d.get("level_uuid")
    if u and not any(n["uuid"] == u for n in db_get_tally_levels()):
        return jsonify({"error": "niveau inconnu"}), 400
    lid = db_upsert_is07_connection(d)
    _relancer_is07()
    return jsonify({"id": lid, "ok": True})


@bp.route("/api/tally/is07/connections/<int:cid>", methods=["DELETE"])
@require_perm("settings.edit")
def tally_is07_connection_del(cid):
    from ..database import db_delete_is07_connection
    if not db_delete_is07_connection(cid):
        return jsonify({"error": "connexion inconnue"}), 404
    _relancer_is07()
    return jsonify({"ok": True})


@bp.route("/api/tally/is07/mapping_all", methods=["GET"])
@require_login
def tally_is07_mapping_all():
    """Toute la correspondance, pour l'éditeur de labels — pendant de /api/tsl/mapping_all."""
    from ..database import db_get_is07_mappings_all
    return jsonify(db_get_is07_mappings_all())


@bp.route("/api/tally/is07/mapping/by_source/batch", methods=["POST"])
@require_perm("settings.edit")
def tally_is07_mapping_batch():
    """`[{connection_id, shm, source_id}]` — pose ou retire, par lot.

    Même contrat que le lot TSL : la page Labels enregistre toute une colonne d'un coup, et une
    requête par cellule ferait des dizaines d'allers-retours pour un seul geste."""
    from ..database import db_set_is07_mapping_for_source
    rows = request.get_json(silent=True) or []
    if not isinstance(rows, list):
        return jsonify({"error": "liste attendue"}), 400
    n = 0
    for r in rows:
        shm = (r or {}).get("shm")
        if not shm or (r or {}).get("connection_id") is None:
            continue
        db_set_is07_mapping_for_source(int(r["connection_id"]), shm, r.get("source_id"))
        n += 1
    _relancer_is07()
    return jsonify({"ok": True, "saved": n})
