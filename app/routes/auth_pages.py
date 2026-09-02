# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Premier démarrage (setup), login/logout, et widget systemd du topnav (uptime + restart)."""

import subprocess
import threading
import time

from flask import jsonify, request, redirect, url_for, render_template

from . import bp
from ..auth import (require_login, require_perm, login_user, logout_user,
                    verify_password, hash_password, valider_motdepasse,
                    pwd_exigences, PWD_REGLES, PWD_PROFILS,
                    pwd_profil, pwd_profils_publics)
from ..database import db_count_users, db_create_user, db_get_user_by_id, db_get_user

SERVICE_STARTED_AT = time.time()


def _safe_next(raw):
    """N'accepte `next` que s'il s'agit d'un chemin relatif same-origin : commence par
    '/' mais PAS par '//' (protocole-relatif → open redirect). Sinon repli sur home."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("routes.home")


# ─── Rate-limit login (anti-bruteforce) ────────────────────────────────────
# Fenêtre glissante par IP en mémoire : max N échecs / fenêtre, sinon 429. Reset sur
# succès. Simple, sans dépendance externe (mono-processus Waitress → un seul dict).
_LOGIN_MAX_FAILS = 10
_LOGIN_WINDOW_S = 300          # 5 min
_login_fails = {}             # ip -> [timestamps des échecs récents]
_login_lock = threading.Lock()


def _client_ip():
    # ProxyFix (main.py) réécrit déjà remote_addr depuis X-Forwarded-For quand on est
    # derrière le reverse-proxy → remote_addr est l'IP client de confiance.
    return request.remote_addr or "?"


def _login_throttled(ip):
    """True si l'IP a dépassé le quota d'échecs récents (purge la fenêtre au passage)."""
    now = time.time()
    with _login_lock:
        hist = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW_S]
        _login_fails[ip] = hist
        return len(hist) >= _LOGIN_MAX_FAILS


def _login_record_fail(ip):
    now = time.time()
    with _login_lock:
        hist = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW_S]
        hist.append(now)
        _login_fails[ip] = hist


def _login_reset(ip):
    with _login_lock:
        _login_fails.pop(ip, None)


# ─── Premier démarrage / Login / Logout ────────────────────────────────────
@bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    """Assistant de premier démarrage. Affiché tant qu'AUCUN utilisateur n'existe
    (installation neuve, plus de seed admin par défaut). Confirme que l'installation
    est fonctionnelle et invite à créer le compte administrateur."""
    # Dès qu'un compte existe, l'assistant n'a plus lieu d'être → login.
    if db_count_users() > 0:
        return redirect(url_for("routes.login_page"))
    error = None
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        p2 = request.form.get("password2") or ""
        prenom = (request.form.get("prenom") or "").strip() or None
        nom = (request.form.get("nom") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None
        # ★ Le PROFIL d'exigence se choisit ICI, en même temps que le mot de passe. Imposer 12
        # signes à quelqu'un qui monte une instance d'essai sur un réseau isolé, c'est produire
        # un mot de passe sur un post-it — et l'écran d'installation était justement le seul
        # endroit où le réglage n'était pas atteignable (il vit dans Réglages → Sécurité, qui
        # exige d'être connecté, donc d'avoir déjà passé cet écran).
        profil = (request.form.get("pwd_profil") or "").strip().lower()
        if profil not in PWD_PROFILS:
            profil = pwd_profil()
        exigences = pwd_exigences(profil)
        fautes = valider_motdepasse(p, u, (prenom, nom, email), exigences=exigences)
        from ..i18n import t as _t
        if not u or not p:
            error = _t("setup.err_required")
        elif p != p2:
            error = _t("setup.err_mismatch")
        elif fautes:
            # Le tout premier compte est ADMINISTRATEUR : c'est le mot de passe qui compte le
            # plus de l'installation, et c'est celui qu'on tapait le plus vite. Même règle
            # qu'ailleurs, pas un seuil de complaisance à six caractères.
            error = _t("setup.err_weak").format(regles="; ".join(
                _t("compte.pwd_regle_" + f).replace(
                    "{n}", str(exigences["longueur_min"]))
                for f in fautes))
        else:
            # Course possible (double soumission) : re-vérifier qu'aucun compte
            # n'a été créé entre-temps avant d'insérer le tout premier admin.
            if db_count_users() > 0:
                return redirect(url_for("routes.login_page"))
            # Le profil retenu devient celui de l'installation : c'est la même exigence qui
            # s'appliquera aux comptes suivants, et elle reste modifiable (Réglages → Sécurité).
            from .. import settings as _st
            _st.set("pwd_profil", profil)
            uid = db_create_user(u, hash_password(p), "admin", prenom, nom, email)
            login_user(db_get_user_by_id(uid))
            # Compte créé → enchaîner l'assistant de premier démarrage.
            return redirect(url_for("routes.setup_wizard"))
    profil_choisi = (request.form.get("pwd_profil") or "").strip().lower()
    if profil_choisi not in PWD_PROFILS:
        profil_choisi = pwd_profil()
    # ⚠ On REND les champs saisis (sauf les mots de passe, jamais renvoyés au navigateur).
    # Un refus qui vide le formulaire fait retaper identifiant, prénom, nom et courriel pour
    # une faute qui ne portait que sur le mot de passe — et n'apprend toujours pas la règle.
    return render_template("setup.html", error=error,
                           form={"username": request.form.get("username", "").strip(),
                                 "prenom": request.form.get("prenom", "").strip(),
                                 "nom": request.form.get("nom", "").strip(),
                                 "email": request.form.get("email", "").strip()},
                           pwd_exigences=pwd_exigences(profil_choisi),
                           pwd_profils=pwd_profils_publics(),
                           pwd_profil=profil_choisi,
                           pwd_regles=PWD_REGLES)

@bp.route("/api/setup/lang", methods=["POST"])
def api_setup_lang():
    """Sélecteur de langue de l'écran d'installation (page PUBLIQUE). Autorisé UNIQUEMENT
    tant qu'aucun compte n'existe (phase d'install) → fixe le défaut global `ui_lang_default`."""
    if db_count_users() > 0:
        return jsonify({"error": "setup terminé"}), 403
    from .. import i18n, settings as st
    lang = ((request.json or {}).get("lang") or "").strip()
    if lang not in i18n.LANG_CODES:
        return jsonify({"error": f"langue inconnue: {lang}"}), 400
    st.set("ui_lang_default", lang)
    return jsonify({"status": "ok", "lang": lang})

@bp.route("/setup/wizard", methods=["GET"])
@require_login
def setup_wizard():
    """Assistant guidé de premier démarrage (identité, Proxmox, réseau, template,
    NIC 2110, MXL, format vidéo). Réutilise les APIs settings/proxmox/nic existantes.
    Ne s'affiche plus une fois `setup_completed`, sauf `?force=1` (relance depuis Réglages)."""
    from .. import settings as st
    if st.get("setup_completed") and not request.args.get("force"):
        return redirect(url_for("routes.home"))
    # Le fuseau est servi comme sur la page Personnalisation : la liste vient de la tzdata
    # RÉELLEMENT installée, jamais d'une liste en dur — c'est la seule qui garantisse qu'un
    # choix sera applicable par le process.
    from .pages import _timezones_par_region
    return render_template("setup_wizard.html",
                           video_formats=st.get("video_formats") or "",
                           timezones=_timezones_par_region())

@bp.route("/api/setup/complete", methods=["POST"])
@require_perm("settings.edit")
def api_setup_complete():
    from .. import settings as st
    st.set("setup_completed", True)
    return jsonify({"status": "ok"})

@bp.route("/login", methods=["GET", "POST"])
def login_page():
    # Installation neuve sans aucun compte → assistant de premier démarrage.
    if db_count_users() == 0:
        return redirect(url_for("routes.setup_page"))
    error = None
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        from ..i18n import t as _t
        ip = _client_ip()
        if _login_throttled(ip):
            # Trop d'échecs récents depuis cette IP → 429 (défense anti-bruteforce).
            # (message littéral : la clé i18n n'est pas dans le catalogue et les fichiers
            # i18n sont hors périmètre de ce correctif.)
            error = "Trop de tentatives échouées. Réessayez dans quelques minutes."
            return render_template("login.html", error=error, next=next_url), 429
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        user = db_get_user(u)
        if user and verify_password(p, user["password_hash"]):
            _login_reset(ip)
            login_user(user)
            # Premier démarrage non terminé → reprendre l'assistant (sauf si une
            # cible 'next' explicite a été demandée). Réservé aux comptes admin.
            from .. import settings as st
            if (not st.get("setup_completed")
                    and not request.values.get("next")
                    and user.get("role") == "admin"):
                return redirect(url_for("routes.setup_wizard"))
            # Interface projets (chantier 1) : atterrissage sur l'accueil /workspaces
            # (sauf cible explicite — le garde-page redirigera si elle est technique).
            if user.get("interface") == "projets" and not request.values.get("next"):
                return redirect(url_for("routes.workspaces_page"))
            return redirect(next_url)
        _login_record_fail(ip)
        error = _t("auth.invalid_credentials")
    return render_template("login.html", error=error, next=next_url)

@bp.route("/logout", methods=["GET", "POST"])
def logout():
    logout_user()
    return redirect(url_for("routes.login_page"))


# ─── Service systemd (debug) ───────────────────────────────────────────────
# Widget topnav : uptime + bouton restart. Réservé admin.
@bp.route("/api/service/info", methods=["GET"])
@require_login
def api_service_info():
    return jsonify({
        "started_at": SERVICE_STARTED_AT,
        "uptime_s":   max(0, int(time.time() - SERVICE_STARTED_AT)),
    })

@bp.route("/api/service/restart", methods=["POST"])
@require_perm("settings.edit")
def api_service_restart():
    # --no-block : systemctl ne reste pas pendu sur l'arrêt du process appelant
    # (ce process lui-même). La réponse HTTP part avant le SIGTERM.
    try:
        subprocess.Popen(["systemctl", "restart", "--no-block", "bobistudio.service"])
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "systemctl introuvable"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})
