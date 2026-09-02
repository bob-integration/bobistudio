# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""API des LIBELLÉS DE SOURCE — `/api/labels/*`.

★ POURQUOI CE FICHIER EXISTE. Ces routes vivaient dans `services/tsl` et portaient le préfixe
`/api/tsl/…`. Elles n'ont pourtant rien de protocolaire : elles servent les libellés d'une
source et la configuration de leurs colonnes, que le tally vienne de TSL, d'IS-07 ou d'un
mélangeur. Le dépôt étant public, une adresse mal nommée s'y fige — c'est le moment de la
corriger, ou jamais.

Les anciennes adresses répondent encore, en redirection 308 (cf. `_compat` plus bas).

★ LA CLÉ EST LE `shm`, PAS UN UUID, et c'est réfléchi. Le SDK MXL identifie bien un flux par
un UUID — mais il le DÉRIVE du nom (`uuid5(nom)`), donc il n'apporte aucune stabilité
supplémentaire : si le nom change, l'UUID change avec. Le nom lisible, lui, est FIGÉ à la
création du conteneur et sert déjà de clé à six choses (bind-mount d'état, graine des SSRC RTP
annoncés en SDP, libellé d'emplacement, câblage, page Câbles, libellés). Passer à l'UUID
coûterait la lisibilité partout et une jointure, sans rien gagner.

Ce qui ne survit pas au REMPLACEMENT d'un conteneur par un autre relèverait de l'EMPLACEMENT
(`production_roles`, le 3ᵉ barreau d'identité) — et changerait le sens du produit : le libellé
suivrait la fonction plutôt que le flux. Autre chantier, autre décision.
"""
from flask import jsonify, redirect, request

from . import bp
from ..auth import require_login, require_perm
from ..database import (db_delete_source_label, db_get_setting,
                        db_get_source_labels, db_get_source_labels_by_shm, db_set_setting,
                        db_upsert_source_label)
from ..tally import noms_colonnes, nb_colonnes_actives

log = __import__("logging").getLogger(__name__)


_DEFAULT_SUFFIX_MAP = {"_audio_0": "_A1", "_audio_1": "_A2", "_anc_0": "_Anc"}


# ── Source labels ─────────────────────────────────────────────────────────

@bp.route("/api/labels", methods=["GET"])
@require_login
def source_labels_get():
    return jsonify(db_get_source_labels())


@bp.route("/api/labels/batch", methods=["POST"])
@require_perm("settings.edit")
def source_labels_batch():
    rows = request.json or []
    if not isinstance(rows, list):
        return jsonify({"error": "liste attendue"}), 400
    saved = 0
    for row in rows:
        shm = (row.get("shm") or "").strip()
        if not shm:
            continue
        fields = {k: v for k, v in row.items() if k != "shm"}
        db_upsert_source_label(shm, fields)
        from app.tally import invalider_libelles; invalider_libelles()
        saved += 1
    return jsonify({"ok": True, "saved": saved})


@bp.route("/api/labels/<path:shm>", methods=["DELETE"])
@require_perm("settings.edit")
def source_label_delete(shm):
    db_delete_source_label(shm)
    from app.tally import invalider_libelles; invalider_libelles()
    return jsonify({"ok": True})


@bp.route("/api/labels/by_shm", methods=["GET"])
@require_login
def tsl_sources_by_shm():
    return jsonify(db_get_source_labels_by_shm())


@bp.route("/api/labels/orphelins", methods=["GET"])
@require_login
def source_labels_orphelins():
    """Les lignes de libellé dont PLUS AUCUN conteneur ne produit le flux.

    ★ « ABSENT » VEUT DIRE ABSENT DE LA DÉCLARATION, PAS ÉTEINT. `/api/sources` dérive de
    `deploy_config` : un conteneur arrêté, un nœud injoignable, un flux qui ne coule pas —
    tout cela reste DÉCLARÉ. Ne sont orphelines que les lignes dont le producteur a été
    détruit ou renommé. Sans cette propriété, arrêter un conteneur ferait basculer tous ses
    libellés en « à nettoyer », et quelqu'un les supprimerait.

    ⚠ ON NE SUPPRIME RIEN ICI. Un libellé est du TRAVAIL — quelqu'un l'a écrit — et une ligne
    vide ne coûte qu'une ligne. On les CLASSE pour que l'exploitant tranche :
      · `rempli`  : au moins un libellé écrit. Le perdre, c'est perdre ce travail.
      · `mappe`   : une correspondance TSL ou IS-07 la vise encore. La retirer casserait le
                    tally de quelque chose que quelqu'un adresse — même si le flux a disparu,
                    c'est le signe qu'on n'a pas fini de ranger."""
    from app.database import (db_get_source_labels, db_get_tsl_mappings_all,
                              db_get_is07_mappings_all, db_get_containers)
    from app import plugins as _plg
    import json as _json
    declares = set()
    for c in db_get_containers():
        try:
            dc = c.get("deploy_config")
            dc = _json.loads(dc) if isinstance(dc, str) else (dc or {})
            if not dc:
                continue
            w = _plg.derive_wiring(dc.get("type"), c.get("hostname"),
                                   dc.get("params") or {}) or {}
            for prod in (w.get("produces") or []):
                if prod.get("shm"):
                    declares.add(prod["shm"])
        except Exception:
            continue
    vises = set()
    for m in (db_get_tsl_mappings_all() or []):
        if m.get("source_shm"):
            vises.add(m["source_shm"])
    try:
        for m in (db_get_is07_mappings_all() or []):
            if m.get("source_shm"):
                vises.add(m["source_shm"])
    except Exception:
        pass
    out = []
    for l in db_get_source_labels():
        shm = l.get("shm") or ""
        # Les lignes de TEXTE (`__umd:`) n'ont pas de producteur par construction : les
        # compter orphelines les proposerait au nettoyage à chaque passage.
        if not shm or shm.startswith("__umd:") or shm in declares:
            continue
        out.append({"shm": shm,
                    "rempli": [k for k in l if k.startswith("label_") and l[k]],
                    "mappe": shm in vises})
    return jsonify(sorted(out, key=lambda x: x["shm"]))


@bp.route("/api/labels/orphelins", methods=["POST"])
@require_perm("settings.edit")
def source_labels_orphelins_purge():
    """`{"shms": [...]}` — retire ces lignes de libellé. Refuse celles qu'une correspondance
    vise encore : le tally les adresse, et les effacer serait casser en silence."""
    from app.database import (db_delete_source_label, db_get_tsl_mappings_all,
                              db_get_is07_mappings_all)
    d = request.json or {}
    shms = d.get("shms")
    if not isinstance(shms, list):
        return jsonify({"error": "`shms` : liste attendue"}), 400
    vises = {m.get("source_shm") for m in (db_get_tsl_mappings_all() or [])}
    try:
        vises |= {m.get("source_shm") for m in (db_get_is07_mappings_all() or [])}
    except Exception:
        pass
    retires, refuses = 0, []
    for shm in shms:
        shm = str(shm or "").strip()
        if not shm:
            continue
        if shm in vises:
            refuses.append(shm)
            continue
        db_delete_source_label(shm)
        from app.tally import invalider_libelles; invalider_libelles()
        retires += 1
    return jsonify({"ok": True, "retires": retires, "refuses": refuses})


@bp.route("/api/labels/suffix_map", methods=["GET"])
@require_login
def source_labels_suffix_map_get():
    stored = db_get_setting("source_label_suffix_map", None)
    return jsonify(stored if isinstance(stored, dict) else _DEFAULT_SUFFIX_MAP)


@bp.route("/api/labels/suffix_map", methods=["POST"])
@require_perm("settings.edit")
def source_labels_suffix_map_set():
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"error": "dict attendu"}), 400
    db_set_setting("source_label_suffix_map", {str(k): str(v) for k, v in data.items()})
    return jsonify({"ok": True})


# ── Noms des colonnes ──────────────────────────────────────────────────────

@bp.route("/api/labels/names", methods=["GET"])
@require_login
def tsl_label_names_get():
    """Les colonnes de libellé OFFERTES : hostname, MXL, puis les personnalisées actives.

    ★ ON EN REND MOINS QU'IL N'EN EXISTE, et c'est le point. Huit colonnes personnalisées
    étaient proposées d'office ; un site en utilise deux ou trois, et les cinq autres
    allongeaient chaque menu, chaque tableau et chaque sélecteur du produit sans rien porter.
    Le nombre actif est un réglage (`label_cols_actives`, 2 par défaut) et les colonnes
    s'ajoutent au besoin.

    ⚠ LES HUIT COLONNES PHYSIQUES RESTENT. Réduire l'affichage n'efface aucune donnée : un
    libellé écrit en colonne 7 reste lisible par son index (`db_get_source_label_for_shm`),
    et réaugmenter le nombre le fait réapparaître intact. C'est ce qui rend le réglage
    réversible sans risque."""
    return jsonify(noms_colonnes()[:2 + nb_colonnes_actives()])


@bp.route("/api/labels/names", methods=["POST"])
@require_perm("settings.edit")
def tsl_label_names_set():
    data = request.json
    if not isinstance(data, list) or not (3 <= len(data) <= 10):
        return jsonify({"error": "liste de 3 à 10 noms attendue"}), 400
    # On COMPLÈTE jusqu'à dix avec les noms déjà en base : enregistrer une liste tronquée
    # écraserait le nom des colonnes masquées, qu'on retrouverait anonymes en les rouvrant.
    anciens = noms_colonnes()
    noms = [str(n) for n in data] + anciens[len(data):]
    db_set_setting("tsl_label_names", noms[:10])
    return jsonify({"ok": True})


@bp.route("/api/labels/cols", methods=["GET"])
@require_login
def tsl_label_cols_get():
    return jsonify({"actives": nb_colonnes_actives(), "max": 8,
                    "noms": noms_colonnes()})


@bp.route("/api/labels/cols", methods=["POST"])
@require_perm("settings.edit")
def tsl_label_cols_set():
    """`{"actives": n}` — combien de colonnes personnalisées sont offertes (1 à 8)."""
    d = request.json or {}
    try:
        n = int(d.get("actives"))
    except (TypeError, ValueError):
        return jsonify({"error": "`actives` : entier attendu"}), 400
    if not (1 <= n <= 8):
        return jsonify({"error": "entre 1 et 8"}), 400
    db_set_setting("label_cols_actives", n)
    return jsonify({"ok": True, "actives": n})

# ─── Compatibilité : les anciennes adresses ─────────────────────────────────────────
# ⚠ 308 ET NON 301. Un 301 autorise le client à retomber en GET — un POST de libellés y
# perdrait son corps, donc l'écriture, sans erreur visible. Le 308 conserve méthode et
# corps. Ces redirections sont éprouvées par `tests/verif_labels_routes.py` : une
# compatibilité qu'on n'éprouve pas n'est qu'une intention.
_ANCIENNES = [
    ("/api/source_labels",             "/api/labels"),
    ("/api/source_labels/batch",       "/api/labels/batch"),
    ("/api/source_labels/suffix_map",  "/api/labels/suffix_map"),
    ("/api/source_labels/orphelins",   "/api/labels/orphelins"),
    ("/api/tsl/sources/by_shm",        "/api/labels/by_shm"),
    ("/api/tsl/label_names",           "/api/labels/names"),
    ("/api/tsl/label_cols",            "/api/labels/cols"),
]
for _i, (_vieux, _neuf) in enumerate(_ANCIENNES):
    def _mk(neuf=_neuf):
        def _redir(**kw):
            return redirect(neuf, code=308)
        return _redir
    bp.add_url_rule(_vieux, endpoint="labels_compat_%d" % _i,
                    view_func=_mk(), methods=["GET", "POST", "PUT", "DELETE"])

def _redir_shm(shm):
    return redirect("/api/labels/%s" % shm, code=308)
bp.add_url_rule("/api/source_labels/<path:shm>", endpoint="labels_compat_shm",
                view_func=_redir_shm, methods=["DELETE"])
