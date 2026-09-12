"""Normalisation et validation du hostname d'un conteneur — source UNIQUE.

Pourquoi un module dédié : le hostname n'est pas un libellé. Il est la racine des noms de flux
MXL (``<hostname>_<idx>``, ``_audio_<idx>``, ``_anc_<idx>`` — cf. ``plugins.derive_wiring``), un
composant de chemin de bind-mount hôte (``/var/lib/bobi/state/<hostname>``, cf.
``docker_compute``), la graine des SSRC RTP annoncés en SDP (``plugins/2110_io/docker/
controller.py``), le libellé semé de l'emplacement de production, et la clé des labels TSL. Il est
donc figé à la création : aucune UI ne le renomme, et le renommer casserait le câblage de tous les
consommateurs.

Ce que le SDK MXL, lui, exige : rien. Un flux y est identifié par son UUID
(``flow_def["id"]`` = ``uuid5(nom)``, cf. ``script_templates/bobimxl.py:319``), et le répertoire
réel est ``/dev/shm/mxl/<uuid>.mxl-flow/``. Le nom lisible est une clé d'annuaire MAISON par-dessus.
On la garde pour deux raisons concrètes : anticollision inter-nœuds à la réplication RDMA
(``docker_driver.nom_moteur_defaut``) et lisibilité dans l'UI (page Câbles, moniteur, labels TSL).

⚠ NE PAS confondre avec les trois autres normalisations du dépôt, qui visent des CIBLES
différentes et divergent volontairement :
  - ``docker_driver._slug``      → composant de chemin, minuscules forcées, non-alnum → tiret
  - ``database.db_role_slugify`` → identifier Ember+, séparateur ``_``, jamais un chiffre en tête
  - ``monitor.dedicated_webrtc_path`` → segment d'URL publique
Les unifier changerait des identités déjà émises vers l'extérieur (clés d'emplacement immuables,
noms de flux de moteurs en service). C'est ICI, et ici seulement, que vit la règle de SAISIE.
"""

import re
import unicodedata

# Docker impose 63 caractères à un hostname réseau ; on s'aligne, même si notre hostname n'est pas
# passé à `docker run` (le nom de conteneur est `bobi-cmp-<vmid>`) — il finit dans des chemins et
# des noms de flux, où une chaîne sans limite ne rend service à personne.
LONGUEUR_MAX = 63

# Préfixes que l'orchestrateur s'attribue : les laisser saisir crée un conteneur que du code
# traitera comme de l'infra. Seuls les préfixes RÉELLEMENT testés ailleurs sont réservés —
#   `bobi-fab-` : tissu compositeur, exclu du reconcile (deploy.py:1829, migration.py:272,
#                 plugin_registry.py:116, cpu_pressure.py:131)
#   `bobi-mtl-` / `bobi-cmp-` : noms de conteneurs Docker générés (docker_driver / docker_compute)
#   `monitor-u` : repli de compat pour retrouver l'encodeur monitor d'un utilisateur (monitor.py:108)
# `mon-` (préfixe des monitors dédiés, monitor.py:631) n'est PAS réservé : il n'est testé nulle
# part — ces conteneurs sont retrouvés par la colonne `monitor_user_id` — et le réserver
# interdirait « mon-mur », « mon-multiview »… pour rien.
PREFIXES_RESERVES = ("bobi-fab-", "bobi-mtl-", "bobi-cmp-", "monitor-u")


def normaliser(v):
    """Forme canonique d'un hostname saisi. Doit rester le MIROIR EXACT de
    ``sanitizeHostname()`` dans ``static/scripts.js`` — le client normalise pour l'affichage,
    le serveur normalise pour faire foi. Une divergence donnerait un nom accepté à l'écran et
    transformé en base.

    Deux règles opposées, assumées (et expliquées à l'utilisateur sous le champ) :
    espace et ``_`` sont CONVERTIS en tiret, tout le reste est SUPPRIMÉ.
    """
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))   # é → e
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^A-Za-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def valider(v, exclure_vmid=None):
    """Valide un hostname SAISI. Retourne ``(valeur_normalisee, erreur_ou_None)``.

    Jamais d'écrêtage muet : trop long → refus, pas troncature (même doctrine que
    ``plugins.validate_config``, qui refuse une valeur hors bornes plutôt que de l'écrêter).
    L'unicité est vérifiée SANS TENIR COMPTE DE LA CASSE : deux conteneurs ``Camera1`` et
    ``camera1`` produiraient deux jeux de flux MXL distincts mais indiscernables à l'œil, et la
    corrélation d'alertes par hostname (``static/scripts.js``) deviendrait indéterministe.
    """
    nom = normaliser(v)
    if not nom:
        return nom, ("Hostname vide ou composé uniquement de caractères non retenus "
                     "(lettres, chiffres et tirets seulement).")
    if len(nom) > LONGUEUR_MAX:
        return nom, f"Hostname trop long ({len(nom)} caractères, maximum {LONGUEUR_MAX})."
    bas = nom.lower()
    for p in PREFIXES_RESERVES:
        if bas.startswith(p):
            return nom, f"Le préfixe « {p} » est réservé à l'orchestrateur — choisis un autre nom."
    from .database import db_get_containers
    for c in (db_get_containers() or []):
        if exclure_vmid is not None and c.get("vmid") == exclure_vmid:
            continue
        if str(c.get("hostname") or "").lower() == bas:
            return nom, (f"Le hostname « {nom} » est déjà utilisé par le conteneur "
                         f"#{c.get('vmid')} — les noms de flux MXL collisionneraient.")
    return nom, None
