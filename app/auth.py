# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Auth & permissions de l'orchestrateur.

Modèle : rôle par utilisateur. Chaque rôle porte un set fixe de permissions.
Sessions Flask (cookie signé) avec durée par défaut de 30 jours.
"""
from functools import wraps
from flask import session, request, redirect, url_for, jsonify, abort, g
from werkzeug.security import generate_password_hash, check_password_hash

import logging
import secrets
import time

from .database import (db_get_user, db_get_user_by_id, db_get_container,
                       db_project_role)

log = logging.getLogger(__name__)

# ─── Permissions ─────────────────────────────────────────────

PERMISSIONS = [
    "containers.create",
    "containers.delete",
    "containers.deploy",       # start/stop/restart, deploy d'un script
    "plugins.operate",         # contrôle live + réglages « user » des plugins (pages plugin)
    "multiview.edit",          # composer, layouts, tally
    "projects.manage",
    "settings.edit",           # config Proxmox, plages, thème, users
    "backup.manage",
    "files.access",            # gestionnaire de fichiers (Réglages → Fichiers) ; gating fin par racine
    "media.manage",            # gestionnaire de médias (Médias → Gestionnaire de Médias)
]

# ⚠ CES CONSTANTES SONT DES DÉFAUTS DE PREMIER DÉMARRAGE, PLUS LA VÉRITÉ.
# Depuis l'onglet Réglages → Rôles, les habilitations vivent dans la table `habilitations` et
# sont modifiables. `recharger_habilitations()` recopie la table dans `ROLES`/`ROLE_LABELS`
# EN PLACE — les modules qui ont fait `from .auth import ROLES` gardent le bon objet, donc
# voient les changements sans redémarrage. C'est la raison du `.clear()` + `.update()` plutôt
# qu'une réaffectation.
ROLES = {
    "admin": set(PERMISSIONS),
    "operator": {
        "containers.create", "containers.delete", "containers.deploy",
        "plugins.operate", "multiview.edit", "projects.manage",
        "files.access", "media.manage",
    },
    "exploitant": {
        "plugins.operate", "multiview.edit",
    },
    "multiview": {
        "multiview.edit",
    },
    "viewer": set(),   # lecture seule
}

# ⚠ CE CHAMP DÉCRIT, IL NE NOMME PAS. Le nom d'un rôle, c'est son IDENTIFIANT (`exploitant`) :
# c'est lui qu'on lit dans `users.role` et qu'on cherche dans le code. Ces valeurs portaient
# les deux à la fois — « Exploitant (pilotage plugins, sans déploiement) » est un nom ET une
# description agrafés — et c'est ce qui faisait déborder les en-têtes de la matrice. Une
# description, donc : pas de reprise du nom, pas de parenthèses.
ROLE_LABELS = {
    "admin":      "Tous les droits",
    "operator":   "Exploitation complète, sans les réglages",
    "exploitant": "Pilotage des plugins, sans déploiement",
    "multiview":  "Composition des multiviews seulement",
    "viewer":     "Lecture seule",
}

# Ce que ces descriptions valaient AVANT le nettoyage. Sert à la migration : on ne remplace que
# ce qui n'a jamais été modifié — une description réécrite par un administrateur lui appartient.
ROLE_LABELS_ANCIENS = {
    "admin":      "Administrateur",
    "operator":   "Opérateur",
    "exploitant": "Exploitant (pilotage plugins, sans déploiement)",
    "multiview":  "Multiview seul",
    "viewer":     "Lecteur (lecture seule)",
}

# ─── Rôles par projet (chantier 1, cf. docs/reference/PROJETS.md §12) ───────
#
# Le rôle GLOBAL décide du scope d'accès : admin/operator voient tout le cluster
# (rien ne change pour eux) ; tout autre rôle est « scopé projet » et n'atteint
# une ressource que via son appartenance au projet qui la porte. Le rôle global
# reste le plafond des capacités (plugins.operate etc. s'appliquent toujours).

PROJECT_ROLES = ["viewer", "operator", "editor", "owner"]   # ordre croissant

PROJECT_ROLE_LABELS = {
    "owner":    "Propriétaire",
    "editor":   "Éditeur (compose les vues, règle les plugins)",
    "operator": "Opérateur (pilote via les vues)",
    "viewer":   "Lecteur",
}

GLOBAL_ACCESS_ROLES = ("admin", "operator")

# Copies figées des défauts : `ROLES` est muté en place au chargement, donc il ne peut pas
# servir de référence pour « remettre d'origine ».
ROLES_DEFAUT = {r: set(p) for r, p in ROLES.items()}
ROLE_LABELS_DEFAUT = dict(ROLE_LABELS)
GLOBAL_ACCESS_DEFAUT = tuple(GLOBAL_ACCESS_ROLES)

# ⚠ L'ADMINISTRATEUR N'EST PAS MODIFIABLE, ET C'EST STRUCTUREL. Une interface qui laisse retirer
# `settings.edit` au rôle administrateur laisse verrouiller l'installation pour de bon : plus
# personne ne peut rouvrir l'onglet qui rendrait le droit. Il porte donc TOUJOURS toutes les
# autorisations et l'accès global, quoi que dise la table.
ROLE_INTOUCHABLE = "admin"


def recharger_habilitations():
    """Recopie la table `habilitations` dans `ROLES` / `ROLE_LABELS` / `GLOBAL_ACCESS_ROLES`.

    Appelée au démarrage et après chaque modification. Silencieuse si la base n'est pas prête :
    le produit doit pouvoir démarrer sur ses défauts plutôt que refuser de servir."""
    global GLOBAL_ACCESS_ROLES
    try:
        from .database import db_habilitations_semer, db_habilitations_lister
        db_habilitations_semer({r: {"label": ROLE_LABELS_DEFAUT.get(r),
                                    "permissions": sorted(p),
                                    "global_access": r in GLOBAL_ACCESS_DEFAUT}
                                for r, p in ROLES_DEFAUT.items()})
        # Migration des descriptions : uniquement celles restées à leur ANCIENNE valeur par
        # défaut. Un administrateur qui a réécrit la sienne la garde.
        from .database import db_habilitation_get, db_habilitation_upsert
        for rid, ancien in ROLE_LABELS_ANCIENS.items():
            h = db_habilitation_get(rid)
            if h and h.get("label") == ancien and ROLE_LABELS_DEFAUT.get(rid) != ancien:
                db_habilitation_upsert(rid, label=ROLE_LABELS_DEFAUT[rid])
        lignes = db_habilitations_lister(set(PERMISSIONS))
    except Exception as e:                       # base absente, migration en cours…
        log.warning("habilitations : lecture impossible, défauts conservés (%s)", e)
        return
    if not lignes:
        return
    neufs = {l["id"]: set(l["permissions"]) for l in lignes}
    libelles = {l["id"]: (l["label"] or l["id"]) for l in lignes}
    globaux = tuple(l["id"] for l in lignes if l["global_access"])
    # L'administrateur reprend TOUT, quoi qu'il y ait en base (cf. ROLE_INTOUCHABLE).
    if ROLE_INTOUCHABLE in neufs:
        neufs[ROLE_INTOUCHABLE] = set(PERMISSIONS)
        if ROLE_INTOUCHABLE not in globaux:
            globaux = globaux + (ROLE_INTOUCHABLE,)
    else:
        # Quelqu'un a réussi à le faire disparaître : on le remet plutôt que de servir une
        # installation sans administrateur possible.
        neufs[ROLE_INTOUCHABLE] = set(PERMISSIONS)
        libelles[ROLE_INTOUCHABLE] = ROLE_LABELS_DEFAUT[ROLE_INTOUCHABLE]
        globaux = globaux + (ROLE_INTOUCHABLE,)
    ROLES.clear(); ROLES.update(neufs)
    ROLE_LABELS.clear(); ROLE_LABELS.update(libelles)
    GLOBAL_ACCESS_ROLES = globaux


def has_global_access(user=None):
    """True si l'utilisateur voit tout le cluster (UI technique complète)."""
    u = user if user is not None else current_user()
    return bool(u) and u.get("role") in GLOBAL_ACCESS_ROLES

def project_role_at_least(role, min_role):
    """Compare deux rôles projet selon la hiérarchie PROJECT_ROLES."""
    try:
        return PROJECT_ROLES.index(role) >= PROJECT_ROLES.index(min_role)
    except ValueError:
        return False

def project_role_for(pid, user=None):
    """Rôle de l'utilisateur dans le projet (les accès globaux sont owner de fait)."""
    u = user if user is not None else current_user()
    if not u:
        return None
    if has_global_access(u):
        return "owner"
    return db_project_role(pid, u["id"])

# Cache court vmid → (project_id, monitor_user_id) : assert_vmid_access est sur le
# chemin chaud du proxy plugin (T-bar, polls de feedback), on évite un hit SQLite
# par requête. TTL volontairement bref : un rattachement de projet doit se voir vite.
_VMID_PROJECT_TTL = 5.0
_vmid_project_cache = {}

def _vmid_project_info(vmid):
    now = time.monotonic()
    hit = _vmid_project_cache.get(vmid)
    if hit and now - hit[2] < _VMID_PROJECT_TTL:
        return hit[0], hit[1]
    c = db_get_container(vmid)
    pid = c.get("project_id") if c else None
    mon = c.get("monitor_user_id") if c else None
    _vmid_project_cache[vmid] = (pid, mon, now)
    return pid, mon

# Appartenance via SNAPSHOT : tant que le cycle de vie (chantier 3) n'existe pas, un
# projet référence surtout ses containers par snapshot (db_save_project ne pose PAS
# project_id). Un container est donc « dans » un projet si project_id le dit OU si son
# vmid figure dans le snapshot — même sémantique que _attach_projects côté UI technique.
_snap_map_cache = {"ts": 0.0, "map": {}}

def _snapshot_project_map():
    now = time.monotonic()
    if now - _snap_map_cache["ts"] > _VMID_PROJECT_TTL:
        from .database import db_get_projects
        # Types exclus des projets (2110_io lié au nœud, infra partagée) : un VIEUX
        # snapshot peut encore les référencer — ils ne comptent pas comme appartenance.
        try:
            from .projects import PROJECT_EXCLUDED_TYPES as _excl
        except Exception:
            _excl = set()
        m = {}
        for p in db_get_projects():
            for sc in (p.get("snapshot") or []):
                v = sc.get("vmid")
                if v is None or (sc.get("deploy_config") or {}).get("type") in _excl:
                    continue
                try:
                    m.setdefault(int(v), set()).add(p["id"])
                except (TypeError, ValueError):
                    pass
        _snap_map_cache["map"] = m
        _snap_map_cache["ts"] = now
    return _snap_map_cache["map"]

def vmid_project_ids(vmid):
    """Tous les projets qui portent ce container : rattachement direct + snapshots."""
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return set()
    pid, _mon = _vmid_project_info(vmid)
    pids = set(_snapshot_project_map().get(vmid, ()))
    if pid is not None:
        pids.add(pid)
    return pids

def check_vmid_access(vmid, min_role="viewer"):
    """Garde d'accès par ressource pour les utilisateurs scopés projet.

    Renvoie None si l'accès est autorisé, sinon une réponse Flask (401/403/404)
    à retourner telle quelle :  err = check_vmid_access(vmid) ; if err: return err.
    Règles : accès global → toujours OK ; sinon il faut être membre (avec le rôle
    minimal demandé) du projet du container. Un container sans projet est refusé
    aux scopés, sauf leur propre encodeur monitor (monitor_user_id).
    """
    u = current_user()
    if not u:
        return (jsonify({"error": "unauthorized"}), 401)
    if has_global_access(u):
        return None
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return (jsonify({"error": "not_found"}), 404)
    _pid, monitor_uid = _vmid_project_info(vmid)
    if monitor_uid == u["id"]:
        return None
    for pid in vmid_project_ids(vmid):
        role = db_project_role(pid, u["id"])
        if role and project_role_at_least(role, min_role):
            return None
    return (jsonify({"error": "forbidden", "reason": "not_project_member"}), 403)

def scoped_project_ids():
    """None si l'utilisateur voit tout (accès global) ; sinon le set des ids de
    projets dont il est membre (pour filtrer les listes)."""
    u = current_user()
    if not u or has_global_access(u):
        return None
    from .database import db_user_projects
    return {p["id"] for p in db_user_projects(u["id"])}

def require_global_access(view):
    """Réservé aux rôles à accès global (admin/operator) : endpoints d'infrastructure
    (topologie, santé cluster) sans équivalent scopé projet."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("routes.login_page", next=request.path))
        if not has_global_access():
            if _wants_json():
                return jsonify({"error": "forbidden", "reason": "global_access_required"}), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapper

def require_project_role(min_role="viewer", pid_arg="pid"):
    """Décorateur pour les routes portant l'id de projet en argument (<int:pid>)."""
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                if _wants_json():
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("routes.login_page", next=request.path))
            pid = kwargs.get(pid_arg)
            role = project_role_for(pid, u)
            if not role or not project_role_at_least(role, min_role):
                if _wants_json():
                    return jsonify({"error": "forbidden",
                                    "reason": "not_project_member"}), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco

# ─── Helpers password ────────────────────────────────────────

# pbkdf2 plutôt que le scrypt par défaut de werkzeug 3.x : scrypt réclame ~32 Mo
# par vérification et lève « memory limit exceeded » selon le build OpenSSL / les
# limites mémoire d'un LXC, ce qui faisait échouer le login silencieusement.
_HASH_METHOD = "pbkdf2:sha256"

def hash_password(plain):
    return generate_password_hash(plain, method=_HASH_METHOD)

def verify_password(plain, hashed):
    try:
        return check_password_hash(hashed, plain)
    except Exception:
        return False

# ─── Robustesse des mots de passe ────────────────────────────

# Longueur minimale. C'est le SEUL facteur qui compte vraiment : un mot de passe court reste
# court même truffé de symboles, et l'espace de recherche croît avec la longueur bien plus vite
# qu'avec le jeu de caractères.
PWD_LONGUEUR_MIN = 12

# ⚠ PAS DE RÈGLE DE COMPOSITION CLASSIQUE (« une majuscule, un chiffre, un symbole »), ET C'EST
# DÉLIBÉRÉ. Le NIST (SP 800-63B, § 5.1.1.2) la déconseille explicitement : imposée, elle ne
# produit pas de la variété, elle produit `Bobi2026!` sur tous les postes — une majuscule au
# début, un chiffre et un `!` à la fin, exactement là où un attaquant les essaie d'abord. On
# demande donc de la LONGUEUR, et une variété qui DÉCROÎT quand la longueur augmente : une
# phrase de passe de vingt lettres est meilleure qu'un `X7@k!` et doit passer.
PWD_VARIETE = ((PWD_LONGUEUR_MIN, 3), (16, 2), (20, 1))

# ─── Profils d'exigence ──────────────────────────────────────
#
# Un contrôleur de régie sur un réseau isolé et un contrôleur joignable depuis Internet ne
# courent pas le même risque, et imposer le second au premier ne produit pas de la sécurité :
# ça produit des mots de passe sur des post-it. Le profil est donc un RÉGLAGE (`pwd_profil`).
#
# ⚠ Ce qui change d'un profil à l'autre, c'est la LONGUEUR et la VARIÉTÉ. Les trois autres
# contrôles — mot de passe courant, reprise de l'identité, suite de clavier — restent actifs
# PARTOUT, y compris en souple : ils ne coûtent rien à un utilisateur de bonne foi (personne ne
# choisit « azerty » par commodité de frappe, on le choisit parce qu'on n'a pas envie de
# choisir) et ce sont exactement les premiers essais d'une attaque.
PWD_PROFILS = {
    # Réseau de production isolé, pas de route vers l'extérieur, accès physique contrôlé.
    "souple":   {"longueur_min": 8,  "variete": ((8, 1),)},
    # Défaut. Aligné sur ce que le NIST appelle une exigence raisonnable pour un secret mémorisé.
    "standard": {"longueur_min": 12, "variete": ((12, 3), (16, 2), (20, 1))},
    # Contrôleur atteignable depuis un réseau qu'on ne maîtrise pas.
    "stricte":  {"longueur_min": 16, "variete": ((16, 3), (24, 2))},
}
PWD_PROFIL_DEFAUT = "standard"


def pwd_profil():
    """Profil actif. Retombe sur le défaut si le réglage est absent ou inconnu — une valeur
    aberrante en base ne doit pas désactiver le contrôle, seulement être ignorée."""
    try:
        from .database import db_get_setting
        v = (db_get_setting("pwd_profil", None) or "").strip().lower()
    except Exception:
        v = ""
    return v if v in PWD_PROFILS else PWD_PROFIL_DEFAUT


def pwd_exigences(profil=None):
    """Paramètres du profil, sous une forme sérialisable — c'est CE dictionnaire que le
    gabarit passe au miroir navigateur, pour que les deux côtés ne puissent pas diverger sur
    les seuils (ils peuvent encore diverger sur la logique : cf. tools/check_motdepasse.py)."""
    p = PWD_PROFILS.get(profil or pwd_profil(), PWD_PROFILS[PWD_PROFIL_DEFAUT])
    return {"profil": profil or pwd_profil(),
            "longueur_min": p["longueur_min"],
            "variete": [list(x) for x in p["variete"]]}

def pwd_profils_publics():
    """Les TROIS profils, sous forme sérialisable. Sert l'écran de premier démarrage, où le
    profil se choisit en même temps que le mot de passe : sans la table complète, changer de
    profil dans le sélecteur imposerait un aller-retour serveur pour ré-afficher les règles."""
    return {nom: pwd_exigences(nom) for nom in PWD_PROFILS}


# Mots de passe interdits quelle que soit leur longueur. Liste COURTE et assumée : elle n'a pas
# vocation à remplacer une vraie base de fuites, seulement à écarter ce qui est tapé par réflexe
# sur une installation neuve. Comparée en minuscules, sans les chiffres de fin (« bobi2026 » et
# « bobi2027 » sont le même mot de passe).
PWD_INTERDITS = {
    "password", "motdepasse", "azerty", "qwerty", "administrateur", "admin", "root",
    "bobi", "bobistudio", "bobi studio", "changeme", "changermoi", "secret", "letmein",
    "welcome", "bienvenue", "iloveyou", "monkey", "dragon", "soleil", "console", "regie",
}

# Suites de touches et d'alphabet : cherchées DANS le mot de passe, pas comparées à lui.
_SUITES = ("abcdefghijklmnopqrstuvwxyz", "0123456789",
           "azertyuiop", "qwertyuiop", "qsdfghjklm", "asdfghjkl", "wxcvbn", "zxcvbn")


def _classes(pwd):
    """Nombre de familles de caractères présentes (minuscules, majuscules, chiffres, reste)."""
    return sum((any(c.islower() for c in pwd), any(c.isupper() for c in pwd),
                any(c.isdigit() for c in pwd),
                any(not c.isalnum() for c in pwd)))


def _noyau(txt):
    """Forme comparable : minuscules, sans espaces ni chiffres de fin."""
    t = "".join(txt.lower().split())
    return t.rstrip("0123456789") or t


# ORDRE D'AFFICHAGE des règles, et liste de ce qui est ANNONÇABLE avant la frappe. Le miroir
# navigateur porte la même liste (`validerMotDePasse.REGLES`) : une règle ajoutée ici sans y être
# n'apparaîtrait nulle part dans l'interface — l'utilisateur se ferait refuser pour un critère
# qu'on ne lui a jamais montré.
PWD_REGLES = ("court", "variete", "courant", "identite", "repetitif")


def valider_motdepasse(pwd, username=None, extras=(), exigences=None):
    """Rend la LISTE des règles enfreintes (vide = accepté). Jamais un booléen.

    ⚠ Une liste, parce que rendre « invalide » oblige l'appelant à inventer un message, et que
    l'interface doit pouvoir cocher les règles UNE PAR UNE pendant la frappe. Les valeurs sont
    des clés i18n (`compte.pwd_regle_*`), donc affichables telles quelles des deux côtés.

    `username` et `extras` (prénom, nom, email…) servent à refuser un mot de passe qui n'est que
    l'identité de son porteur : c'est la première chose essayée, et elle est publique.

    ⚠ CETTE FONCTION EST LA RÈGLE. Le contrôle côté navigateur est un CONFORT — il évite un
    aller-retour et montre ce qui manque — mais il ne garantit rien : n'importe qui peut poster
    directement sur l'API. Tout chemin qui appelle `hash_password` doit passer par ici d'abord.
    """
    pwd = pwd or ""
    ex = exigences or pwd_exigences()
    lmin = ex["longueur_min"]
    variete = [tuple(x) for x in ex["variete"]]
    fautes = []
    if len(pwd) < lmin:
        fautes.append("court")

    # Variété exigée selon la longueur atteinte (cf. le commentaire de PWD_VARIETE).
    besoin = variete[-1][1]
    for seuil, n in variete:
        if len(pwd) < seuil:
            besoin = n
            break
    if len(pwd) >= lmin and _classes(pwd) < besoin:
        fautes.append("variete")

    noyau = _noyau(pwd)
    if noyau in PWD_INTERDITS:
        fautes.append("courant")

    # Le mot de passe contient l'identité, ou l'identité contient le mot de passe.
    # ⚠ `noyau` doit être testé AUSSI : la chaîne vide est sous-chaîne de tout, donc un mot de
    # passe vide se faisait accuser de contenir l'identité (trouvé au banc).
    for ident in ([username] + list(extras or ())) if len(noyau) >= 3 else []:
        i = _noyau(ident or "")
        if len(i) < 3:
            continue
        if i in noyau or noyau in i:
            fautes.append("identite")
            break

    # Un seul caractère répété, ou une suite de clavier / d'alphabet de 4 signes ou plus.
    if pwd and len(set(pwd)) <= 2:
        fautes.append("repetitif")
    else:
        bas = pwd.lower()
        for suite in _SUITES:
            for i in range(len(suite) - 3):
                bout = suite[i:i + 4]
                if bout in bas or bout[::-1] in bas:
                    fautes.append("repetitif")
                    break
            if "repetitif" in fautes:
                break
    return fautes


# ─── User session ────────────────────────────────────────────

def current_user():
    """Renvoie le user courant (dict) ou None. Caché dans g pour économiser la DB."""
    if hasattr(g, "_user"):
        return g._user
    uid = session.get("user_id")
    u = db_get_user_by_id(uid) if uid else None
    if u is not None and not _session_valide(u):
        session.clear()
        u = None
    g._user = u
    return g._user


def _session_valide(u):
    """Le cookie décrit-il encore une session OUVERTE ? Décide à chaque requête.

    Trois cas, et le troisième est le seul délicat :

    1. `sid` inscrit et non révoqué → valide. On rafraîchit `last_seen`, au plus une fois par
       minute (cf. `SESSION_TOUCH_S`) : le tableau de bord interroge le serveur toutes les
       5 secondes par onglet, écrire à chaque passage ferait du registre une pompe à écritures.
    2. `sid` inscrit et RÉVOQUÉ, ou inconnu du registre → refusé.
    3. Pas de `sid` du tout : cookie émis AVANT l'existence du registre. On l'INSCRIT au vol
       plutôt que de déconnecter tout le monde à la mise à jour — mais seulement si son époque
       est à jour, sinon un cookie dormant survivrait à un « fermer mes autres sessions ».
    """
    from .database import db_session_get, db_session_toucher, db_session_ouvrir
    epoque_compte = u.get("session_epoch") or 0
    if (session.get("session_epoch") or 0) < epoque_compte:
        return False
    sid = session.get("sid")
    ip = request.remote_addr if request else None
    if not sid:
        sid = secrets.token_urlsafe(32)
        session["sid"] = sid
        session["session_epoch"] = epoque_compte
        db_session_ouvrir(sid, u["id"], ip,
                          request.headers.get("User-Agent") if request else None)
        return True
    ligne = db_session_get(sid)
    if not ligne or ligne.get("revoked") or ligne.get("user_id") != u["id"]:
        return False
    db_session_toucher(sid, ip)
    return True

def current_permissions():
    u = current_user()
    if not u:
        return set()
    return ROLES.get(u.get("role"), set())

def has_perm(perm):
    return perm in current_permissions()

def login_user(user):
    from .database import (db_session_ouvrir, db_sessions_purger, db_user_marquer_connexion)
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True   # cookie persistant (~30 jours selon config app)
    # Identifiant OPAQUE tiré au sort : le cookie ne porte plus qu'une référence, et c'est le
    # registre qui décide si elle vaut encore. C'est ce qui rend une session révocable.
    sid = secrets.token_urlsafe(32)
    session["sid"] = sid
    session["session_epoch"] = user.get("session_epoch") or 0
    try:
        db_session_ouvrir(sid, user["id"], request.remote_addr if request else None,
                          request.headers.get("User-Agent") if request else None)
        db_user_marquer_connexion(user["id"])
        db_sessions_purger()
    except Exception:
        # Une connexion ne doit PAS échouer parce que le registre est indisponible. On perd la
        # traçabilité de cette session, pas l'accès au produit.
        log.exception("registre de sessions : ouverture impossible")

def logout_user():
    sid = session.get("sid")
    if sid:
        try:
            from .database import db_session_fermer
            db_session_fermer(sid)
        except Exception:
            log.exception("registre de sessions : fermeture impossible")
    session.clear()

# ─── Décorateurs ─────────────────────────────────────────────

def _wants_json():
    """True si la requête vient d'un fetch / accepte JSON."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept

def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("routes.login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapper

def require_perm(perm):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user():
                if _wants_json():
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("routes.login_page", next=request.path))
            if not has_perm(perm):
                if _wants_json():
                    return jsonify({"error": "forbidden", "missing_permission": perm}), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco
