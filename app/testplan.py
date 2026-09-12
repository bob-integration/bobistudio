# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Plan de recette / suivi de tests — page dédiée à l'équipe de test.
# Liste d'items à tester ; chaque testeur coche (OK / défaut / en cours) et laisse
# une remarque. État PARTAGÉ (persisté dans un JSON à côté de la DB) → tous les
# testeurs voient la même liste en temps quasi-réel (la page poll /api/tests).
#
# Volontairement AUTONOME (Blueprint séparé + persistance fichier) : n'écrit dans
# aucun module existant (évite la course avec un agent concurrent sur routes.py).

import fcntl
import json
import os
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, render_template, abort, send_file
import io
from datetime import datetime

from .auth import require_login, current_user, require_perm

testplan_bp = Blueprint("testplan", __name__)

# Persistance : un seul fichier JSON, écriture atomique sous DOUBLE lock :
#  - _lock (threading) : exclut les threads du serveur Flask entre eux ;
#  - flock (fcntl, fichier .lock) : exclut le serveur ET un process externe (l'assistant Claude
#    qui poste une question depuis le CLI via mutate()/append_message()) → pas de write perdu.
STORE_PATH = "/opt/bobistudio/db_testplan.json"
LOCK_PATH = STORE_PATH + ".lock"
_lock = threading.Lock()

STATUSES = ("untested", "ok", "defect", "progress")

# ★ LA GRAVITÉ EST UN CHAMP À PART, PAS TROIS STATUTS DE PLUS. Trois statuts
# `defect_mineur/majeur/bloquant` auraient obligé à migrer les 48 états déjà
# saisis, et à toucher tous les filtres et compteurs. Un champ orthogonal se
# greffe sans rien casser : un défaut d'avant garde son statut et n'a pas de
# gravité — « non qualifié », ce qui est la vérité.
#
# Vocabulaire FERMÉ : une valeur hors liste est ramenée à "" plutôt qu'écrite
# telle quelle. Une gravité inventée ne se compte nulle part et ne se voit pas.
SEVERITES = ("mineur", "majeur", "bloquant")
ROLES = ("dev", "tester")


class _Flock:
    def __enter__(self):
        self._f = open(LOCK_PATH, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._f, fcntl.LOCK_UN)
        finally:
            self._f.close()


def mutate(fn):
    """Read-modify-write atomique du store (in-process + inter-process). `fn(store)` peut
    renvoyer une valeur, retournée à l'appelant. Tous les writes passent par ici."""
    with _lock, _Flock():
        store = _load()
        ret = fn(store)
        _save(store)
        return ret


def append_message(item_id, author, role, text):
    """Ajoute un message au fil de discussion d'un item (utilisable depuis le CLI : l'assistant
    poste ses questions en role='dev'). Cross-process safe via mutate()."""
    role = role if role in ROLES else "tester"
    msg = {"id": uuid.uuid4().hex[:10], "author": (author or "?")[:80],
           "role": role, "text": str(text)[:8000], "ts": time.time()}

    def _do(store):
        if not any(it["id"] == item_id for it in store["items"]):
            return None
        store.setdefault("threads", {}).setdefault(item_id, []).append(msg)
        return msg
    return mutate(_do)

# ─── Checklist par défaut (semée au 1er accès) ────────────────────────────────
# Centrée sur le chantier en cours « entrelacé natif 1 grain = 1 champ » + régressions.
# id = slug STABLE (ne pas renommer : c'est la clé de l'état). area = regroupement.
#
# Tuple = (id, area, title, detail, context). Le champ CONTEXT = notes TECHNIQUES (pour
# l'assistant Claude qui triera les retours) : plugin+version+commit, ce qui a changé,
# fichiers/fonctions touchés, pièges connus, comment reproduire. Affiché sur la page sous
# « ℹ Contexte technique » → un défaut remonté est mappable direct sur le code concerné.
# ★ LA CAMPAGNE VIT DANS UN FICHIER À PART, ET SON ABSENCE EST NORMALE. Les
# éléments de recette de ce site nomment des vmid, des nœuds et des retours de
# testeurs : ils sont retirés à la publication. La page, elle, reste — un
# produit qui livre un suivi de recette est un produit qui assume d'être
# vérifié. Sans le fichier, la campagne démarre vide, ce qui est exactement ce
# qu'il faut pour qui installe le produit.
try:
    from .testplan_seed import SEED_ITEMS
except ImportError:
    SEED_ITEMS = []

def _default_store():
    items = [{"id": i, "area": a, "title": t, "detail": d, "context": c, "builtin": True}
             for (i, a, t, d, c) in SEED_ITEMS]
    return {"version": 1, "items": items, "state": {}}


def _load():
    """Charge le store (sème les items par défaut si absent ; fusionne les nouveaux
    items builtin pour qu'une mise à jour du code ajoute les nouvelles lignes sans
    écraser l'état déjà saisi par l'équipe)."""
    store = None
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            store = None
    if not store:
        store = _default_store()
    # fusion idempotente des items builtin (ajout des nouveaux, MAJ libellé/détail/contexte)
    have = {it["id"]: it for it in store.get("items", [])}
    for (i, a, t, d, c) in SEED_ITEMS:
        if i in have:
            have[i].update({"area": a, "title": t, "detail": d, "context": c, "builtin": True})
        else:
            store.setdefault("items", []).append(
                {"id": i, "area": a, "title": t, "detail": d, "context": c, "builtin": True})
    store.setdefault("state", {})
    store.setdefault("threads", {})
    return store


def _save(store):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE_PATH)


def _summary(store):
    """Compteurs par statut, et par GRAVITÉ pour les défauts.

    ★ « 12 défauts » ne dit pas s'il faut livrer. « 1 bloquant, 3 majeurs, 8
    mineurs » le dit. C'est toute la raison d'être de la gravité : un chiffre
    unique force à ouvrir la liste pour décider."""
    st = store["state"]
    out = {"total": len(store["items"]), "ok": 0, "defect": 0, "progress": 0, "untested": 0,
           "severites": {k: 0 for k in SEVERITES}, "non_qualifies": 0}
    for it in store["items"]:
        e = st.get(it["id"]) or {}
        s = e.get("status", "untested")
        out[s if s in out else "untested"] += 1
        if s == "defect":
            sv = e.get("severite")
            if sv in SEVERITES:
                out["severites"][sv] += 1
            else:
                out["non_qualifies"] += 1
    return out


def _actif():
    """La page de recette est-elle activée ? (réglage `testplan_enabled`)

    ⚠ CONTRÔLÉ DANS LA ROUTE, PAS AU MONTAGE DU BLUEPRINT. Enregistrer ou non le
    blueprint au démarrage marcherait aussi — mais il faudrait redémarrer le
    contrôleur pour changer d'avis, ce qui transforme un interrupteur en
    opération d'exploitation. Ici, la bascule prend effet au prochain clic."""
    from . import settings as _st
    return str(_st.get("testplan_enabled", "0")).strip().lower() not in ("0", "false", "off", "")


@testplan_bp.route("/tests", methods=["GET"])
@require_login
def tests_page():
    # 404, pas 403 : désactivée, cette page n'existe pas. Un refus d'accès
    # laisserait croire à un manque de droits et enverrait chercher une
    # permission qui n'existe pas.
    if not _actif():
        abort(404)
    return render_template("tests.html")


@testplan_bp.route("/api/tests", methods=["GET"])
@require_login
def api_tests():
    store = mutate(lambda s: None)   # persiste la fusion éventuelle des nouveaux builtin
    store = _load()
    return jsonify({"items": store["items"], "state": store["state"],
                    "threads": store.get("threads", {}), "summary": _summary(store)})


@testplan_bp.route("/api/plugins/audit", methods=["GET"])
@require_login
def api_plugins_audit():
    """Audit « déclaré et absent » du parc de plugins (cf. `app/plugin_audit.py`).

    Sur la page Recette parce que c'est un CONSTAT automatique, à côté d'une campagne de tests
    manuelle : les deux répondent à la même question — ce qui est promis tient-il ? — et
    l'exploitant n'a pas à savoir lequel des deux l'a trouvé."""
    from .plugin_audit import auditer
    res = auditer()
    return jsonify({
        "plugins": res,
        "total": len(res),
        "ok": sum(1 for r in res if r["ok"]),
        "manquements": sum(len(r["endpoints_absents"]) + len(r["actions_absentes"])
                           + len(r["reglages_morts"]) for r in res),
    })


@testplan_bp.route("/api/plugins/autotests", methods=["GET"])
@require_login
def api_plugins_autotests():
    """Rejoue les auto-contrôles de tous les conteneurs qui en déclarent.

    GÉNÉRIQUE : tout plugin qui expose `/autotest` dans `control.read_endpoints` est interrogé.
    Ce n'est pas propre au scope — un instrument qui sait prouver son étalonnage est une bonne
    idée partout, et la mécanique ne doit pas être à réécrire au deuxième.

    Un conteneur injoignable est RAPPORTÉ comme tel, jamais omis : une liste où l'absent
    disparaît se lit comme « tout va bien »."""
    from . import plugins as _pl
    from .database import db_get_containers
    from .addressing import get_container_ip
    from .routes.plugin_registry import _load_dc
    import requests as _rq

    types_ok = {m["type"] for m in _pl.all()
                if "/autotest" in ((m.get("control") or {}).get("read_endpoints") or [])}
    out = []
    for c in db_get_containers():
        dc = _load_dc(c)
        t = (dc or {}).get("type")
        if t not in types_ok or c.get("status") != "running":
            continue
        ip = c.get("ip") or get_container_ip(c["vmid"])
        item = {"vmid": c["vmid"], "hostname": c.get("hostname"), "type": t}
        if not ip:
            item.update(etat="injoignable", detail="IP introuvable")
            out.append(item)
            continue
        try:
            r = _rq.get("http://%s:8082/autotest" % ip, timeout=25)
            item.update(r.json() if r.status_code == 200 else
                        {"etat": "injoignable", "detail": "HTTP %s" % r.status_code})
        except Exception as e:                                      # noqa: BLE001
            item.update(etat="injoignable", detail=str(e))
        out.append(item)
    return jsonify({"conteneurs": out,
                    "echecs": sum(x.get("echecs") or 0 for x in out),
                    "injoignables": sum(1 for x in out if x.get("etat") == "injoignable")})


@testplan_bp.route("/api/tests/state", methods=["POST"])
@require_login
def api_tests_state():
    data = request.get_json(silent=True) or {}
    iid = str(data.get("id") or "").strip()
    if not iid:
        return jsonify({"error": "id manquant"}), 400
    status = str(data.get("status") or "untested")
    if status not in STATUSES:
        status = "untested"
    remark = str(data.get("remark") or "")[:4000]
    tester = str(data.get("tester") or "").strip()[:80]
    # ⚠ LA GRAVITÉ NE SURVIT PAS À UN CHANGEMENT DE STATUT. Un point repassé en
    # « OK » qui garderait « bloquant » sous le capot ressortirait bloquant au
    # prochain filtre, sans que rien ne l'affiche. On la vide hors du défaut.
    severite = str(data.get("severite") or "").strip().lower()
    if status != "defect" or severite not in SEVERITES:
        severite = ""

    def _do(store):
        if not any(it["id"] == iid for it in store["items"]):
            return ("err", 404, "item inconnu")
        store["state"][iid] = {"status": status, "remark": remark, "severite": severite,
                               "tester": tester, "updated": time.time()}
        return ("ok", _summary(store))
    res = mutate(_do)
    if res[0] == "err":
        return jsonify({"error": res[2]}), res[1]
    return jsonify({"ok": True, "summary": res[1]})


@testplan_bp.route("/api/tests/thread", methods=["POST"])
@require_login
def api_tests_thread():
    """Ajoute un message au FIL de discussion d'un item (Q/R horodatées, historique conservé).
    L'équipe poste en role='tester' ; l'assistant Claude poste en 'dev' depuis le CLI."""
    data = request.get_json(silent=True) or {}
    iid = str(data.get("id") or "").strip()
    text = str(data.get("text") or "").strip()
    if not iid or not text:
        return jsonify({"error": "id ou texte manquant"}), 400
    author = str(data.get("author") or "").strip() or "anonyme"
    role = "dev" if str(data.get("role")) == "dev" else "tester"
    msg = append_message(iid, author, role, text)
    if msg is None:
        return jsonify({"error": "item inconnu"}), 404
    return jsonify({"ok": True, "message": msg})


@testplan_bp.route("/api/tests/item", methods=["POST"])
@require_login
def api_tests_add_item():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "titre manquant"}), 400
    area = str(data.get("area") or "Divers").strip()[:80] or "Divers"
    detail = str(data.get("detail") or "").strip()[:2000]
    item = {"id": "custom-" + uuid.uuid4().hex[:10], "area": area,
            "title": title[:200], "detail": detail, "builtin": False}
    mutate(lambda s: s["items"].append(item))
    return jsonify({"ok": True, "item": item})


@testplan_bp.route("/api/tests/item/delete", methods=["POST"])
@require_login
def api_tests_del_item():
    data = request.get_json(silent=True) or {}
    iid = str(data.get("id") or "")

    def _do(store):
        it = next((x for x in store["items"] if x["id"] == iid), None)
        if not it:
            return ("err", 404, "item inconnu")
        if it.get("builtin"):
            return ("err", 400, "item intégré non supprimable")
        store["items"] = [x for x in store["items"] if x["id"] != iid]
        store["state"].pop(iid, None)
        store.get("threads", {}).pop(iid, None)
        return ("ok",)
    res = mutate(_do)
    if res[0] == "err":
        return jsonify({"error": res[2]}), res[1]
    return jsonify({"ok": True})


# ─── Export / import d'une recette ──────────────────────────────────────────
# ★ UNE RECETTE EST UN LIVRABLE, PAS UNE DONNÉE PRIVÉE. On la remet à un client,
# on la rejoue sur une autre installation, on la reprend d'un site à l'autre.
# Sans export, elle reste prisonnière d'un fichier sur un serveur — et la seule
# façon de la partager est la capture d'écran.
#
# ⚠ DEUX CHOSES BIEN DISTINCTES, ET LE CHOIX EST EXPLICITE :
#   · le MODÈLE      = les éléments seuls. Ce qu'on veut vérifier. Réutilisable.
#   · la CAMPAGNE    = les éléments PLUS les résultats et les fils de questions.
#     C'est un constat daté, avec des noms de testeurs et des remarques de
#     terrain. Ça ne se diffuse pas par mégarde.
# Le nom du fichier le dit aussi, pour qu'on ne se trompe pas de pièce jointe.

def _recette_export(avec_resultats):
    # ⚠ `mutate` rend ce que RETOURNE le callback, pas le magasin. `mutate(lambda
    # s: None)` sert donc à persister la fusion des nouveaux items intégrés, et
    # rend None — c'est `_load()` qui donne le magasin. Confondre les deux coûte
    # un AttributeError, et c'est ce que faisait la première version.
    mutate(lambda s: None)
    store = _load()
    out = {"format": "bobi.recette", "version": 1,
           "exporte_le": datetime.now().isoformat(timespec="seconds"),
           "items": store.get("items") or []}
    if avec_resultats:
        out["state"] = store.get("state") or {}
        out["threads"] = store.get("threads") or {}
    return out


@testplan_bp.route("/api/tests/export", methods=["GET"])
@require_perm("settings.edit")
def api_tests_export():
    avec = (request.args.get("resultats") or "") in ("1", "true", "yes")
    data = _recette_export(avec)
    nom = "recette-%s-%s.json" % ("campagne" if avec else "modele",
                                  datetime.now().strftime("%Y%m%d"))
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    return send_file(buf, mimetype="application/json",
                     as_attachment=True, download_name=nom)


@testplan_bp.route("/api/tests/import", methods=["POST"])
@require_perm("settings.edit")
def api_tests_import():
    """Importe une recette. Par défaut on FUSIONNE ; `remplacer=1` écrase.

    ⚠ LA FUSION EST LE DÉFAUT, ET CE N'EST PAS UN DÉTAIL. Un import qui écrase
    d'office effacerait des résultats saisis par plusieurs personnes, sans
    retour possible — le magasin n'a pas d'historique. On ajoute donc ce qui
    manque et on laisse le reste ; remplacer est un geste explicite."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "aucun fichier"}), 400
    brut = f.read(8 * 1024 * 1024)
    try:
        data = json.loads(brut.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": "fichier illisible : %s" % e}), 400
    if data.get("format") != "bobi.recette":
        return jsonify({"error": "ce n'est pas un export de recette"}), 400
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "aucun élément dans le fichier"}), 400
    remplacer = (request.form.get("remplacer") or "") in ("1", "true", "yes")
    compte = {"ajoutes": 0, "ignores": 0, "remplaces": 0, "etats": 0, "messages": 0}

    def _do(store):
        if remplacer:
            compte["remplaces"] = len(store.get("items") or [])
            store["items"] = []
            store["state"] = {}
            store["threads"] = {}
        connus = {x["id"] for x in store["items"]}
        for it in items:
            iid = str(it.get("id") or "").strip()
            if not iid:
                continue
            if iid in connus:
                compte["ignores"] += 1
                continue
            # ⚠ `builtin` NE S'IMPORTE PAS. Il marque les éléments qui viennent du
            # code de CETTE installation et que la fusion au démarrage réinjecte ;
            # l'hériter d'un fichier rendrait l'élément indestructible ici.
            store["items"].append({
                "id": iid, "area": str(it.get("area") or ""),
                "title": str(it.get("title") or ""), "detail": str(it.get("detail") or ""),
                "context": str(it.get("context") or ""), "builtin": False,
            })
            connus.add(iid)
            compte["ajoutes"] += 1
        # ★ FUSION VÉRITABLE DES RÉSULTATS ET DES FILS — c'est ce qui fait de
        # l'export/import un ALLER-RETOUR et pas un aller simple. Une première
        # version faisait `setdefault` : un point déjà connu gardait son état et
        # son fil, donc les réponses revenues du site n'entraient JAMAIS. Le
        # testeur aurait exporté ses questions, nous aurions répondu, et il
        # n'aurait rien vu — sans le moindre message d'erreur.
        #
        # Règles, choisies pour qu'aucun des deux côtés ne perde son travail :
        #   · ÉTAT   — le plus RÉCENT gagne, point par point (champ `updated`).
        #     Un statut n'a qu'une valeur ; le dernier qui a regardé fait foi.
        #   · FIL    — on AJOUTE les messages inconnus et on retrie par date.
        #     Une conversation ne s'écrase pas : les deux moitiés se recousent
        #     par l'identifiant de message, stable depuis sa création.
        for cle, val in (data.get("state") or {}).items():
            local = store["state"].get(cle)
            if local is None:
                store["state"][cle] = val
                compte["etats"] += 1
            else:
                try:
                    plus_recent = float(val.get("updated") or 0) > float(local.get("updated") or 0)
                except (TypeError, ValueError, AttributeError):
                    plus_recent = False
                if plus_recent:
                    store["state"][cle] = val
                    compte["etats"] += 1
        for cle, msgs in (data.get("threads") or {}).items():
            fil = store.setdefault("threads", {}).setdefault(cle, [])
            connus = {m.get("id") for m in fil}
            neufs = [m for m in (msgs or []) if m.get("id") not in connus]
            if neufs:
                fil.extend(neufs)
                fil.sort(key=lambda m: float(m.get("ts") or 0))
                compte["messages"] += len(neufs)
        return ("ok",)

    mutate(_do)
    return jsonify({"ok": True, **compte, "avec_resultats": bool(data.get("state"))})
