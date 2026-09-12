# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Users (admin uniquement, via settings.edit) + préférences du compte courant."""

from flask import jsonify, request, session

from . import bp
from ..auth import (require_perm, require_login, current_user, verify_password,
                    hash_password, ROLES, PERMISSIONS,
                    valider_motdepasse)
from ..database import (db_get_containers, db_list_users, db_get_user, db_create_user,
                        db_get_user_by_id, db_update_user, db_delete_user)


def _t(cle, repli):
    """Traduit une clé, en retombant sur `repli` si elle n'est pas au catalogue."""
    from ..i18n import t
    v = t(cle)
    return repli if v == cle else v


@bp.route("/api/users", methods=["GET"])
@require_perm("settings.edit")
def api_list_users():
    users = db_list_users()
    # Enrichit chaque user avec son container de monitoring (lien stable monitor_user_id,
    # repli sur l'ancien hostname monitor-u<id>). Un seul fetch des containers.
    containers = db_get_containers()
    by_uid = {}
    for c in containers:
        mu = c.get("monitor_user_id")
        if mu is not None:
            try: by_uid[int(mu)] = c
            except (TypeError, ValueError): pass
    for c in containers:
        hn = c.get("hostname") or ""
        if hn.startswith("monitor-u"):
            try: by_uid.setdefault(int(hn[len("monitor-u"):]), c)
            except ValueError: pass
    for u in users:
        mc = by_uid.get(u["id"])
        u["monitor_vmid"] = mc["vmid"] if mc else None
        u["monitor_hostname"] = (mc.get("hostname") if mc else None)
    return jsonify({"users": users,
                    # Nom TRADUIT du rôle (cf. `_libelle_role`). Cette liste est la SOURCE
                    # UNIQUE : le formulaire de création la consomme aussi, sinon les deux
                    # divergent — c'était le cas (langue ET contenu : « exploitant » manquait
                    # à la création).
                    "roles": [{"id": r, "label": _libelle_role(r)} for r in ROLES.keys()],
                    "permissions": PERMISSIONS,
                    "role_permissions": {r: sorted(list(perms)) for r, perms in ROLES.items()}})

def _navigateur(ua):
    """Nom LISIBLE du navigateur et du système, depuis l'en-tête User-Agent.

    ⚠ Reconnaissance grossière et ASSUMÉE. Un User-Agent est déclaratif, personne ne peut le
    lire de façon fiable, et le but n'est pas d'inventorier un parc : c'est de permettre à
    quelqu'un de RECONNAÎTRE sa propre session dans une liste de trois. « Chrome sur Windows »
    y suffit ; ce qui l'identifie vraiment, c'est l'adresse et l'heure à côté."""
    ua = ua or ""
    nav = next((n for m, n in (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
        ("Chrome/", "Chrome"), ("Safari/", "Safari")) if m in ua), None)
    sys_ = next((n for m, n in (
        ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
        ("Mac OS X", "macOS"), ("Linux", "Linux")) if m in ua), None)
    if nav and sys_:
        return "%s / %s" % (nav, sys_)
    return nav or sys_ or None


@bp.route("/api/users/me/sessions", methods=["GET"])
@require_login
def api_mes_sessions():
    """Les sessions ouvertes du compte courant. Réservé à l'intéressé : personne d'autre n'a à
    savoir depuis quelles adresses quelqu'un se connecte."""
    from ..database import db_sessions_utilisateur
    u = current_user()
    courante = session.get("sid")
    out = []
    for s in db_sessions_utilisateur(u["id"]):
        out.append({"debut": s.get("created_at"), "vue": s.get("last_seen"),
                    "ip": s.get("ip"), "navigateur": _navigateur(s.get("user_agent")),
                    "courante": s.get("sid") == courante})
    return jsonify({"sessions": out, "derniere_connexion": u.get("last_login")})


@bp.route("/api/users/me/sessions/autres", methods=["DELETE"])
@require_login
def api_fermer_autres_sessions():
    """Ferme toutes les sessions du compte SAUF celle qui appelle."""
    from ..database import db_sessions_fermer_autres
    u = current_user()
    sid = session.get("sid")
    n, epoque = db_sessions_fermer_autres(u["id"], sid or "")
    # ⚠ L'époque vient d'avancer : sans cette ligne, la session qui a demandé la fermeture se
    # fermerait elle-même à la requête suivante.
    session["session_epoch"] = epoque
    return jsonify({"status": "ok", "fermees": n})


def _libelle_role(rid):
    """Nom LISIBLE d'un rôle pour le sélecteur, dans la langue courante.

    Le repli est l'IDENTIFIANT, et pas `ROLE_LABELS[rid]` comme avant : depuis que ce champ
    porte une DESCRIPTION, s'en servir comme nom afficherait « Pilotage des plugins, sans
    déploiement » dans une liste déroulante de rôles. Le cas ne se posait pas tant que les cinq
    rôles étaient figés — ils ont tous leur clé — mais un rôle CRÉÉ par un administrateur n'en
    a aucune, et c'est ce repli qui le nomme."""
    return _t(f"settings.users.role_{rid}", rid)


def _refus_motdepasse(pwd, username=None, extras=()):
    """Rend une réponse Flask si le mot de passe est refusé, sinon None.

    ⚠ Appelée par TOUS les chemins qui posent un mot de passe (création par un administrateur,
    modification par un administrateur, changement par l'intéressé, premier admin de
    l'assistant). Le contrôle du navigateur ne protège rien : l'API est publiquement postable."""
    fautes = valider_motdepasse(pwd, username, extras)
    if not fautes:
        return None
    return jsonify({"error": "mot de passe trop faible", "regles": fautes}), 400


@bp.route("/api/users", methods=["POST"])
@require_perm("settings.edit")
def api_create_user():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role     = data.get("role") or "viewer"
    if not username or not password:
        return jsonify({"error": "username et password requis"}), 400
    if role not in ROLES:
        return jsonify({"error": f"role inconnu: {role}"}), 400
    if db_get_user(username):
        return jsonify({"error": "utilisateur existe déjà"}), 400
    refus = _refus_motdepasse(password, username,
                             (data.get("prenom"), data.get("nom"), data.get("email")))
    if refus:
        return refus
    uid = db_create_user(username, hash_password(password), role,
                         prenom=(data.get("prenom") or "").strip() or None,
                         nom=(data.get("nom") or "").strip() or None,
                         email=(data.get("email") or "").strip() or None,
                         interface=(data.get("interface") or "").strip() or None)
    return jsonify({"status": "ok", "id": uid})

@bp.route("/api/users/<int:uid>", methods=["PATCH"])
@require_perm("settings.edit")
def api_update_user(uid):
    data = request.json or {}
    target = db_get_user_by_id(uid)
    if not target:
        return jsonify({"error": "introuvable"}), 404
    role = data.get("role")
    pwd  = data.get("password")
    if role is not None and role not in ROLES:
        return jsonify({"error": f"role inconnu: {role}"}), 400
    # Empêche de retirer le dernier admin
    if pwd:
        refus = _refus_motdepasse(pwd, target.get("username"),
                                  (target.get("prenom"), target.get("nom"), target.get("email")))
        if refus:
            return refus
    if role and target.get("role") == "admin" and role != "admin":
        admins = [u for u in db_list_users() if u["role"] == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "impossible : c'est le dernier administrateur"}), 400
    # prenom/nom/email : présents dans le body → mis à jour ("" efface, absent = inchangé)
    db_update_user(uid,
                   role=role,
                   password_hash=hash_password(pwd) if pwd else None,
                   prenom=(data.get("prenom").strip() if isinstance(data.get("prenom"), str) else None),
                   nom=(data.get("nom").strip() if isinstance(data.get("nom"), str) else None),
                   email=(data.get("email").strip() if isinstance(data.get("email"), str) else None),
                   interface=(data.get("interface") or "").strip() or None)
    return jsonify({"status": "ok"})

@bp.route("/api/users/<int:uid>", methods=["DELETE"])
@require_perm("settings.edit")
def api_delete_user(uid):
    target = db_get_user_by_id(uid)
    if not target:
        return jsonify({"error": "introuvable"}), 404
    if current_user() and current_user()["id"] == uid:
        return jsonify({"error": "tu ne peux pas te supprimer toi-même"}), 400
    if target.get("role") == "admin":
        admins = [u for u in db_list_users() if u["role"] == "admin"]
        if len(admins) <= 1:
            return jsonify({"error": "impossible : c'est le dernier administrateur"}), 400
    db_delete_user(uid)
    return jsonify({"status": "ok"})

@bp.route("/api/users/me/lang", methods=["POST"])
@require_login
def api_set_own_lang():
    """Préférence de langue d'interface de l'utilisateur courant (i18n)."""
    from .. import i18n
    data = request.json or {}
    lang = (data.get("lang") or "").strip()
    if lang not in i18n.LANG_CODES:
        return jsonify({"error": f"langue inconnue: {lang}"}), 400
    db_update_user(current_user()["id"], lang=lang)
    return jsonify({"status": "ok", "lang": lang})

@bp.route("/api/users/me/theme", methods=["POST"])
@require_login
def api_set_own_theme():
    """Thème d'interface de l'utilisateur courant — PRÉFÉRENCE PERSONNELLE, comme la langue.

    `theme: ""` remet l'utilisateur sur le défaut du système (setting global `theme`), qui
    reste réglé dans Réglages → Personnalisation et sert les comptes qui n'ont rien choisi."""
    from .. import settings as st
    data = request.json or {}
    theme = (data.get("theme") or "").strip()
    if theme and theme not in {t["id"] for t in st.THEMES}:
        return jsonify({"error": f"thème inconnu: {theme}"}), 400
    db_update_user(current_user()["id"], theme=theme)
    return jsonify({"status": "ok", "theme": theme})

@bp.route("/api/users/me", methods=["GET", "PATCH"])
@require_login
def api_moi():
    """Fiche du compte COURANT : lecture, et modification de ce qui lui appartient.

    ⚠ CET ENDPOINT MANQUAIT, ET LE MANQUE ÉTAIT INVISIBLE. Un utilisateur pouvait changer sa
    langue, son thème et son mot de passe, mais PAS son adresse e-mail : seul
    `PATCH /api/users/<uid>`, réservé à `settings.edit`, savait le faire. Il fallait donc
    déranger un administrateur pour corriger une faute de frappe dans sa propre adresse.

    ★ CE QU'IL NE PERMET PAS, ET C'EST L'ESSENTIEL : ni `role`, ni `interface`, ni le
    nom d'utilisateur. On ne se donne pas de droits à soi-même, et une élévation de privilège
    par un endpoint « de confort » est un classique. La liste est BLANCHE, pas noire : un champ
    ajouté à la table demain n'entre pas ici par inadvertance."""
    u = current_user()
    if not u:
        return jsonify({"error": "non authentifié"}), 401
    if request.method == "GET":
        from ..auth import ROLES, PERMISSIONS
        d = {k: u.get(k) for k in
             ("id", "username", "role", "prenom", "nom", "email", "lang", "theme",
              "interface", "telephone", "service", "poste", "photo_url", "created_at")}
        # ★ LES PERMISSIONS SONT RENDUES EN CLAIR, toutes, cochées ou non. Un utilisateur
        # découvrait ses droits en SE HEURTANT à un refus ; les lui montrer répond à
        # « pourquoi ce bouton ne marche pas pour moi ? » avant qu'il ne le demande.
        accordees = ROLES.get(u.get("role")) or set()
        d["permissions"] = [{"cle": p, "accordee": p in accordees} for p in PERMISSIONS]
        return jsonify(d)
    data = request.json or {}
    champs = {}
    # ★ LISTE BLANCHE, PAS NOIRE. Ni `role`, ni `interface`, ni `username` : on ne se donne
    # pas de droits à soi-même, et une colonne ajoutée demain n'entre pas ici par
    # inadvertance. `theme` et `lang` en font partie — ce sont des préférences, pas des droits.
    for k in ("prenom", "nom", "email", "telephone", "service", "poste"):
        if isinstance(data.get(k), str):
            champs[k] = data[k].strip()
    if isinstance(data.get("lang"), str) and data["lang"]:
        from ..i18n import LANG_CODES
        if data["lang"] not in LANG_CODES:
            return jsonify({"error": "langue inconnue"}), 400
        champs["lang"] = data["lang"]
    if isinstance(data.get("theme"), str):
        from .. import settings as _st
        valides = {t["id"] for t in _st.THEMES}
        # "" est LÉGITIME : il remet l'utilisateur sur le défaut du système.
        if data["theme"] and data["theme"] not in valides:
            return jsonify({"error": "thème inconnu"}), 400
        champs["theme"] = data["theme"]
    if not champs:
        return jsonify({"error": "rien à modifier"}), 400
    db_update_user(u["id"], **champs)
    return jsonify({"status": "ok", **champs})


_PHOTO_EXTS = ("png", "jpg", "jpeg", "webp", "gif")


@bp.route("/api/users/me/photo", methods=["POST", "DELETE"])
@require_login
def api_ma_photo():
    """Photo du compte courant → `static/uploads/avatar-<uid>.<ext>`.

    Même mécanique que le logo d'entreprise (`/api/settings/logo`) : un fichier sur disque et
    une URL en base, pas une image en base64 dans la table. Une photo en base64 gonfle chaque
    lecture de la fiche utilisateur — y compris celles qui n'en veulent pas — et se retrouve
    dans toutes les sauvegardes de la base.

    ⚠ LE NOM DE FICHIER PORTE L'UID, PAS LE NOM D'UTILISATEUR. Un compte renommé garderait
    sinon l'ancien fichier, et un compte recréé sous le même nom hériterait de la photo du
    précédent — une tête qui n'est pas la sienne, en haut de l'écran."""
    import glob
    import os
    import time
    from .. import config
    u = current_user()
    if not u:
        return jsonify({"error": "non authentifié"}), 401
    # UNE SEULE DÉFINITION DU DOSSIER D'UPLOADS. Le recalculer à la main ici marchait, et
    # aurait cessé de marcher le jour où `config.UPLOADS_DIR` change — sans que rien ne le
    # signale, puisque l'écriture aurait simplement atterri ailleurs.
    dossier = config.UPLOADS_DIR
    motif = os.path.join(dossier, "avatar-%d.*" % int(u["id"]))

    if request.method == "DELETE":
        for vieux in glob.glob(motif):
            try:
                os.remove(vieux)
            except OSError:
                pass
        db_update_user(u["id"], photo_url="")
        return jsonify({"status": "ok", "photo_url": ""})

    f = request.files.get("photo")
    if not f or not f.filename:
        return jsonify({"error": "fichier 'photo' manquant"}), 400
    ext = os.path.splitext(f.filename)[1].lower().lstrip(".")
    if ext not in _PHOTO_EXTS:
        return jsonify({"error": "format non supporté (%s)" % ", ".join(_PHOTO_EXTS)}), 400
    f.seek(0, os.SEEK_END)
    taille = f.tell()
    f.seek(0)
    if taille > 2 * 1024 * 1024:
        return jsonify({"error": "image trop lourde (max 2 Mo)"}), 400
    os.makedirs(dossier, exist_ok=True)
    for vieux in glob.glob(motif):
        try:
            os.remove(vieux)
        except OSError:
            pass
    f.save(os.path.join(dossier, "avatar-%d.%s" % (int(u["id"]), ext)))
    # Anti-cache : sans lui, le navigateur garde l'ANCIENNE photo à la même URL, et
    # l'utilisateur croit que le téléversement a échoué.
    url = "/static/uploads/avatar-%d.%s?v=%d" % (int(u["id"]), ext, int(time.time()))
    db_update_user(u["id"], photo_url=url)
    return jsonify({"status": "ok", "photo_url": url})


@bp.route("/api/users/me/password", methods=["POST"])
@require_login
def api_change_own_password():
    data = request.json or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    if not new:
        return jsonify({"error": "new_password requis"}), 400
    u = current_user()
    if not verify_password(old, u["password_hash"]):
        return jsonify({"error": "ancien mot de passe incorrect"}), 403
    # ⚠ APRÈS la vérification de l'ancien : sinon la page dit à un inconnu quelles règles
    # s'appliquent avant même de savoir s'il est le titulaire du compte.
    refus = _refus_motdepasse(new, u.get("username"), (u.get("prenom"), u.get("nom"), u.get("email")))
    if refus:
        return refus
    if verify_password(new, u["password_hash"]):
        return jsonify({"error": "le nouveau mot de passe est identique à l'ancien"}), 400
    db_update_user(u["id"], password_hash=hash_password(new))
    return jsonify({"status": "ok"})
