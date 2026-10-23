# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Journal de SÉCURITÉ — authentification, comptes et droits.

★ CE QUE CE MODULE N'EST PAS : une infrastructure. Le journal existe déjà (`alerts`, qui est
aussi le « qui a fait quoi » depuis le 2026-07-27), il sort déjà vers syslog et SNMP
(`services/alerting/`), il sait déjà porter un acteur et des messages traduisibles. Ce module
RACCORDE les gestes d'authentification et de gestion de comptes à cette tuyauterie, et rien de
plus. Il tient en un helper et une série de verbes nommés.

★ POURQUOI IL EXISTE QUAND MÊME, plutôt que des `db_add_alert` éparpillés dans les routes :
  1. **Aucun de ces appels ne doit pouvoir casser la connexion.** `/login` est la porte d'entrée :
     une exception dans le journal ne doit ni empêcher un légitime d'entrer, ni empêcher un
     imposteur d'être rejeté. Tout passe donc par un `try/except` UNIQUE, ici. C'est le seul
     endroit du produit où l'on préfère perdre une ligne de journal plutôt que de propager —
     ailleurs, un échec silencieux est un défaut.
  2. **Le tri anti-rebond n'est pas le même pour tout le monde**, et se tromper vide l'exigence
     de sa substance (cf. plus bas).
  3. **Aucun secret ne doit fuir.** Les verbes n'acceptent que des identifiants, des adresses et
     des noms de champ — jamais un mot de passe, un hash, un jeton ou un cookie, et il n'y a
     qu'un fichier à relire pour s'en assurer.

★ ANTI-REBOND COUPÉ, POUR TOUTES LES LIGNES SANS EXCEPTION — le point qui décide de la valeur
du résultat.
`db_add_alert` déduplique les répétitions d'un même symptôme (cf. `_antirebond`) SAUF quand la
ligne porte un acteur. Or un ÉCHEC de connexion n'a pas d'acteur — personne n'est authentifié —
donc il tomberait en plein dans le rebond : cinquante tentatives depuis la même adresse
produiraient UNE ligne, c'est-à-dire exactement l'inverse de ce que demande CSG-002 § 2.1.11.
Pour un bruteforce, la répétition EST l'information.

On coupe donc l'anti-rebond partout ici, y compris sur les gestes dont l'acteur les exempterait
déjà. Se reposer sur cette exemption ferait dépendre l'intégrité de la piste d'audit d'une
DÉDUCTION (`database._acteur_courant` retourne None hors contexte de requête, et échoue en
silence par conception) : le jour où l'attribution manque, des lignes disparaîtraient sans que
rien ne le dise. Une piste d'audit n'est pas un détecteur de symptômes — c'est un registre, et un
registre n'a pas le droit d'omettre une écriture parce qu'elle ressemble à la précédente. Le
volume, lui, est borné par la rétention propre (`database.ALERTS_RETENTION_SECURITE_DEFAUT`).

★ ACTEUR. On ne le force jamais : `db_add_alert` le déduit du contexte de requête
(`database._acteur_courant`). Sur `/login`, personne n'est encore connecté → NULL, ce qui est
honnête : l'identifiant *tenté* n'est pas un acteur établi, il voyage en paramètre `tente`.
Après `login_user()`, en revanche, le contexte porte l'utilisateur et la ligne s'attribue seule.
"""
import logging

from .database import db_add_alert

log = logging.getLogger(__name__)


def _ip():
    """Adresse source de la requête, ou None hors contexte HTTP.

    `ProxyFix` (main.py) a déjà réécrit `remote_addr` depuis `X-Forwarded-For` derrière le
    reverse-proxy : c'est donc l'adresse client de confiance, pas celle du proxy."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return None
        return request.remote_addr or None
    except Exception:
        return None


def _emettre(cle, kind, niveau="info", _acteur=None, **params):
    """Écrit UNE ligne de sécurité. Ne lève jamais, n'interrompt jamais l'action journalisée.

    `antirebond=False` EN DUR, pour la raison développée en tête de module : une piste d'audit est
    un registre, pas un détecteur de symptômes.

    `_acteur` force l'attribution au lieu de la laisser DÉDUIRE du contexte de requête. À n'utiliser
    que là où la déduction ne peut pas marcher : la déconnexion, qui vide la session — après
    `logout_user()`, `_acteur_courant()` ne trouve plus personne et la ligne deviendrait « machine ».
    Un `None` explicite = on laisse déduire, comportement de tous les autres verbes.

    ⚠ NE JAMAIS s'en servir pour poser un acteur « par défaut » sur une action de la MACHINE :
    NULL veut dire « automatique », et c'est une information (cf. `database._acteur_courant`)."""
    try:
        p = {k: v for k, v in params.items() if v is not None}
        p.setdefault("ip", _ip() or "?")
        db_add_alert(cle, niveau, kind=kind, params=p, user=_acteur, antirebond=False)
    except Exception:
        # Volontairement non propagé, et volontairement BRUYANT dans le journal fichier : perdre
        # une ligne d'audit est un incident, mais bloquer une connexion en est un plus grave.
        log.exception("journal de sécurité : ligne « %s » non écrite", cle)


# ─── Authentification ────────────────────────────────────────────────────────────────────────

def login_success(username):
    """À appeler APRÈS `login_user()` : le contexte porte alors l'acteur, la ligne s'attribue."""
    _emettre("alert.securite.connexion_ok", "auth", "info", identifiant=username)


def login_failure(username_tente):
    """Échec d'authentification. `username_tente` est l'identifiant SAISI — jamais le mot de
    passe, ni un indice sur lui (CSG-002 § 2.1.11 demande l'identifiant et la source, pas plus).

    Un identifiant vide est rendu « (vide) » plutôt qu'omis : une rafale de POST sans identifiant
    est un fait à voir, et une ligne dont le paramètre manque se rendrait en « {tente} »."""
    _emettre("alert.securite.connexion_echec", "auth", "warning",
             tente=(str(username_tente or "").strip() or "(vide)"))


def login_throttled(username_tente=None):
    """Quota d'échecs dépassé pour cette adresse → 429. C'est un événement de sécurité EN SOI :
    il dit qu'une source insiste, ce qu'aucune ligne d'échec isolée ne dit."""
    _emettre("alert.securite.connexion_bloquee", "auth", "error",
             tente=(str(username_tente or "").strip() or "(vide)"))


def logout(username):
    """⚠ L'acteur est passé EXPLICITEMENT, et c'est le seul verbe du module dans ce cas. La
    déduction par le contexte de requête « marchait » ici par accident — `current_user()` met son
    résultat en cache dans `g`, donc l'appel que fait la route pour connaître le nom peuplait au
    passage ce dont dépendait l'attribution. Une déconnexion journalisée sans cet appel préalable,
    ou après un `session.clear()`, serait retombée en « machine » : un geste humain attribué à
    l'automate. Ici l'attribution ne dépend plus d'un ordre d'appels."""
    _emettre("alert.securite.deconnexion", "auth", "info",
             _acteur=username, identifiant=username)


def premier_admin(username):
    """Création du compte administrateur initial (`/setup`). Sans acteur : à cet instant il n'y a
    encore personne — c'est précisément ce que la ligne raconte."""
    _emettre("alert.securite.premier_admin", "account", "warning",
             identifiant=username)


# ─── Comptes et droits ───────────────────────────────────────────────────────────────────────

def compte_cree(username, role):
    _emettre("alert.securite.compte_cree", "account", "warning",
             identifiant=username, role=role)


def compte_modifie(username, champs):
    """`champs` = NOMS des champs touchés, jamais leurs valeurs. Un « email » suffit à l'auditeur
    pour savoir quoi comparer ; recopier la valeur ferait du journal une seconde base de données
    de données personnelles, qu'il faudrait alors protéger comme la première."""
    _emettre("alert.securite.compte_modifie", "account", "warning",
             identifiant=username, champs=", ".join(champs) if champs else "—")


def compte_supprime(username):
    _emettre("alert.securite.compte_supprime", "account", "error", identifiant=username)


def role_change(username, avant, apres):
    """Changement d'habilitation PORTÉE par un compte. Ligne distincte de `compte_modifie` : c'est
    le seul champ dont la modification change ce que quelqu'un a le DROIT de faire, et un auditeur
    le cherche par lui-même, pas noyé dans une liste de champs."""
    _emettre("alert.securite.role_change", "account", "error",
             identifiant=username, avant=avant or "?", apres=apres or "?")


def motdepasse_change(username, par_admin=False):
    """⚠ Le mot de passe n'apparaît NULLE PART — ni en clair, ni haché, ni tronqué, ni sa
    longueur. On journalise le FAIT et son auteur, ce qui est tout ce qu'un audit demande."""
    _emettre("alert.securite.motdepasse_change_admin" if par_admin
             else "alert.securite.motdepasse_change", "account", "warning",
             identifiant=username)


def sessions_revoquees(username, n):
    _emettre("alert.securite.sessions_revoquees", "account", "warning",
             identifiant=username, n=n)


# ─── Habilitations (les RÔLES eux-mêmes, pas leurs porteurs) ─────────────────────────────────

def habilitation_creee(rid, permissions):
    _emettre("alert.securite.habilitation_creee", "account", "error",
             habilitation=rid, permissions=", ".join(permissions) if permissions else "—")


def habilitation_modifiee(rid, avant, apres):
    """L'ÉCART d'autorisations, pas l'état final : « +containers.deploy » se relit, une liste de
    quarante permissions ne se relit pas, et c'est l'ajout qui intéresse l'auditeur."""
    avant, apres = set(avant or ()), set(apres or ())
    delta = sorted("+" + p for p in apres - avant) + sorted("-" + p for p in avant - apres)
    _emettre("alert.securite.habilitation_modifiee", "account", "error",
             habilitation=rid, delta=", ".join(delta) if delta else "—")


def habilitation_supprimee(rid):
    _emettre("alert.securite.habilitation_supprimee", "account", "error", habilitation=rid)


def habilitation_reinitialisee(rid):
    _emettre("alert.securite.habilitation_reinitialisee", "account", "warning",
             habilitation=rid)
