# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from .config import DB_PATH

log = logging.getLogger(__name__)

# Connexion PAR THREAD (incident 2026-07-11, Errno 24) : l'historique « une connexion fraîche
# par appel, jamais fermée » (219 sites, aucun close) laissait les connexions en attente du GC
# CYCLIQUE (cycles sqlite3 connection↔cursor) → pool transitoire de centaines de fds, débordé
# par une rafale d'opérations → accept() de Waitress en Errno 24, orchestrateur mort. Mesuré :
# ~400 fds .db à 3 h d'uptime ; gc.collect() en récupérait ~100 d'un coup. La connexion par
# thread borne le compte au nombre de threads (~30-40) et supprime le churn open/GC.
# Contrat appelant INCHANGÉ : personne ne ferme (vérifié) ; sémantique transactionnelle
# identique (isolation legacy — un helper qui écrivait sans commit était DÉJÀ bogué : sa
# transaction implicite était perdue au GC ; elle serait maintenant commitée par le prochain
# commit du même thread, comportement au pire « moins pire »).
_tls = threading.local()

def get_db():
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Anti « database is locked » : attendre le verrou jusqu'à 5 s (nombreux threads
    # de fond partagent le même fichier).
    conn.execute("PRAGMA busy_timeout=5000")
    # synchronous=NORMAL : recommandé AVEC le mode WAL (bon compromis durabilité/perf). Réglage
    # PAR CONNEXION (non persistant), donc posé ici pour que tous les threads en bénéficient.
    # Le mode WAL lui-même est persistant (en-tête du fichier DB) : activé une fois dans init_db().
    conn.execute("PRAGMA synchronous=NORMAL")
    _tls.conn = conn
    return conn

def init_db():
    with get_db() as db:
        # WAL : lecteurs et écrivain ne se bloquent plus mutuellement (au lieu du journal `delete`
        # historique qui sérialisait tout via busy_timeout). PERSISTANT (écrit dans l'en-tête du
        # fichier) → une seule activation suffit ; l'auto-checkpoint SQLite (1000 pages par défaut)
        # replie le -wal dans la DB proprement. Exécuté AVANT toute écriture (hors transaction :
        # un PRAGMA journal_mode dans une transaction ouverte est un no-op silencieux).
        # NB : WAL crée des fichiers -wal / -shm à côté de la DB ; l'API sqlite3 `.backup()`
        # (utilisée par app/ha.py) gère WAL correctement — ne JAMAIS copier le seul .db à la main.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute('''CREATE TABLE IF NOT EXISTS containers (
            vmid        INTEGER PRIMARY KEY,
            hostname    TEXT NOT NULL,
            cores       INTEGER DEFAULT 2,
            memory      INTEGER DEFAULT 2048,
            script      TEXT,
            status      TEXT DEFAULT 'unknown',
            restarts    INTEGER DEFAULT 0,
            created_at  TEXT,
            ip          TEXT,
            fps         REAL,
            source      TEXT,
            shm_out     TEXT
        )''')
        cols = [r[1] for r in db.execute("PRAGMA table_info(containers)")]
        if "deploy_config" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN deploy_config TEXT")
        if "nmos_receivers_count" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN nmos_receivers_count INTEGER DEFAULT 0")
        if "cpu_percent" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN cpu_percent REAL")
        if "mem_used" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN mem_used INTEGER")
        # IMAGE RÉELLEMENT POSÉE AU `docker run`. Sans elle, `requires.image_min` ne pouvait
        # protéger que la CRÉATION : au redéploiement, la seule image connaissable était celle
        # du NŒUD, qui peut avoir été promue depuis. Un conteneur créé sur une image ancienne
        # passait donc le contrôle tout en tournant sur l'ancienne — le cas exact qui a coûté
        # deux recréations le 2026-08-25. NULL = conteneur d'avant cette colonne, on ne sait
        # pas, et on le DIT plutôt que de supposer.
        if "image" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN image TEXT")
        if "script_enabled" not in cols:
            # INTENTION d'exploitation du script, distincte de son ÉTAT observé. Écrite par le seul
            # `NcWorker.enabled` du modèle MS-05-02 (services/nmos/plugins_ncp.py) : un contrôleur
            # NMOS tiers peut arrêter un traitement, et il faut que ça TIENNE.
            #
            # ★ Sans elle, le pilotage serait un leurre : le prochain déploiement rallumerait le
            # script (`deploy.py` fait /stop puis /start), et le contrôleur verrait sa consigne
            # acceptée puis silencieusement défaite. Une capacité qui se contredit toute seule est
            # pire que pas de capacité.
            db.execute("ALTER TABLE containers ADD COLUMN script_enabled INTEGER DEFAULT 1")
        if "status_cause" not in cols:
            # CAUSE d'un statut anormal (`script_stopped`, `crash_loop`) : code de sortie, signal,
            # dernières lignes du journal. Sans elle, l'interface affiche « script arrêté » et rien
            # d'autre — le SIGILL des nœuds Sandy Bridge n'était visible que dans `docker logs`, et
            # c'est ce qui a coûté le plus de temps de tout le chantier CPU.
            db.execute("ALTER TABLE containers ADD COLUMN status_cause TEXT")
        if "cpu_count" not in cols:
            # Nombre de CPU vus par le conteneur, au moment où `cpu_percent` a été relevé. Sans lui
            # `cpu_percent` n'a pas d'unité exploitable (cf. db_update_usage) : c'est ce qui permet
            # de repasser au coût ABSOLU en cœurs, seule grandeur comparable entre types.
            db.execute("ALTER TABLE containers ADD COLUMN cpu_count INTEGER")
        if "assigned_vf" not in cols:
            # Nom du VF SR-IOV assigné au container (ex: "enp1s0f0v3"), NULL si aucun
            db.execute("ALTER TABLE containers ADD COLUMN assigned_vf TEXT")
        if "pinned_cores" not in cols:
            # CPUs épinglés (cpuset), format "4,5,6" ou "4-6,8" ; NULL = pas d'épinglage
            db.execute("ALTER TABLE containers ADD COLUMN pinned_cores TEXT")
        if "nmos_audio_count" not in cols:
            # Nombre de receivers audio NMOS exposés (en plus de nmos_receivers_count qui est = vidéo)
            db.execute("ALTER TABLE containers ADD COLUMN nmos_audio_count INTEGER DEFAULT 0")
        if "monitor_user_id" not in cols:
            # Lien stable container monitoring ↔ utilisateur (le hostname peut être renommé)
            db.execute("ALTER TABLE containers ADD COLUMN monitor_user_id INTEGER")
        if "deployed_at" not in cols:
            # Horodatage du dernier déploiement de script (renseigné par db_update_deploy_config)
            db.execute("ALTER TABLE containers ADD COLUMN deployed_at TEXT")
        if "project_id" not in cols:
            db.execute("ALTER TABLE containers ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL")
        if "instance_uuid" not in cols:
            # Identité d'instance PORTABLE (uuid4), découplée du vmid (= handle local). Embarquée dans
            # les snapshots projet → survit recreate/import. Le vmid se réattribue, l'uuid voyage.
            db.execute("ALTER TABLE containers ADD COLUMN instance_uuid TEXT")
        if "config_rev" not in cols:
            # RÉVISION de `deploy_config` : compteur monotone incrémenté à CHAQUE écriture (cf.
            # `db_update_deploy_config`). Sert de garde anti-écrasement quand plusieurs personnes
            # éditent le même conteneur : l'éditeur mémorise la révision qu'il a chargée et la
            # joint à son déploiement ; le serveur refuse (409) si elle a bougé entre-temps.
            # POURQUOI PAS `deployed_at` : sa résolution est la SECONDE — deux écritures dans la
            # même seconde sont indistinguables, et le composer multiview écrit à chaque geste.
            # 0 = jamais écrit depuis la migration ; un éditeur qui ne joint pas de révision n'est
            # pas gardé (rétrocompatible : palette, macros, projets, page Câbles).
            db.execute("ALTER TABLE containers ADD COLUMN config_rev INTEGER DEFAULT 0")
        if "config_rev_by" not in cols:
            # AUTEUR de la dernière écriture (id utilisateur, NULL = écriture machine :
            # surveillance, réconciliation, agent). Sans lui, la garde de révision serait
            # inutilisable : le déploiement s'exécute dans un thread, donc la nouvelle révision
            # n'existe pas encore quand la réponse HTTP part — un éditeur ne peut pas tenir son
            # compteur à jour et se ferait refuser ses PROPRES gestes suivants. Un conflit n'est
            # déclaré que si la dernière écriture vient de QUELQU'UN D'AUTRE.
            db.execute("ALTER TABLE containers ADD COLUMN config_rev_by INTEGER")
        if "agent_token" not in cols:
            # Token d'auth de l'agent PAR-CONTENEUR (:8081, en-tête X-MXL-Agent-Token) : valeur
            # ALÉATOIRE (secrets.token_urlsafe) posée à la création du conteneur par les drivers
            # (docker_compute / docker_driver) qui l'injectent en MXL_AGENT_TOKEN au `docker run`.
            # POURQUOI une colonne plutôt que la dérivation historique (HMAC(flask_secret_key,
            # vmid)) : `flask_secret_key` signe AUSSI les cookies de session — la faire tourner
            # après un incident invaliderait d'un coup TOUS les tokens d'agent et l'orchestrateur
            # ne piloterait plus aucun conteneur existant. Un token par conteneur, indépendant du
            # secret racine, rend cette clé réellement rotable.
            # Pas de backfill : un token n'existe QUE s'il a été injecté dans le conteneur. NULL =
            # conteneur d'avant la migration → `deploy.agent_token` retombe sur le dérivé (cf.
            # `db_agent_token_etat` pour savoir combien il en reste).
            db.execute("ALTER TABLE containers ADD COLUMN agent_token TEXT")
        if "runtime_spec_sig" not in cols:
            # Signature de la spec `docker run` du conteneur EN MARCHE (image/réseau/ip/mounts/
            # ressources/gpu). Permet à docker_compute.deploy_compute d'être IDEMPOTENT : un
            # (re)déploiement de SCRIPT ne recrée plus le conteneur si sa spec n'a pas bougé
            # (l'agent-nœud, lui, fait toujours `rm -f` + `run`). Cf. docker_compute.
            db.execute("ALTER TABLE containers ADD COLUMN runtime_spec_sig TEXT")
        # Backfill idempotent (ne touche que les NULL/'') : un uuid4 par conteneur existant.
        import uuid as _uuid
        for (v,) in db.execute(
                "SELECT vmid FROM containers WHERE instance_uuid IS NULL OR instance_uuid=''").fetchall():
            db.execute("UPDATE containers SET instance_uuid=? WHERE vmid=?", (str(_uuid.uuid4()), v))
        if "desired_state" not in cols:
            # État VOULU par l'opérateur ('running'/'stopped') — distinct de `status` (état OBSERVÉ,
            # écrasé chaque tick par surveillance). Sert à l'auto-recovery au reboot d'un nœud : on ne
            # relève que ce qui devait tourner (un conteneur arrêté volontairement reste arrêté).
            # Écrit par deploy/start/stop/redemarrer (db_set_desired_state) — JAMAIS par surveillance.
            db.execute("ALTER TABLE containers ADD COLUMN desired_state TEXT")
            # Backfill une seule fois (à la création de la colonne) : l'état observé courant est la
            # meilleure approximation de l'intention.
            db.execute("UPDATE containers SET desired_state = CASE WHEN status='running' "
                       "THEN 'running' ELSE 'stopped' END")
        if "role_seeded" not in cols:
            # Marqueur « un emplacement a DÉJÀ été semé pour ce conteneur » (cf. table `production_roles`).
            # Sans lui, supprimer un emplacement le ferait ressusciter au redéploiement suivant :
            # l'opérateur ne pourrait jamais s'en débarrasser.
            db.execute("ALTER TABLE containers ADD COLUMN role_seeded INTEGER DEFAULT 0")
            # Backfill : les conteneurs d'AVANT la migration n'ont pas d'emplacement mais doivent
            # en obtenir un — on les laisse à 0 pour qu'ils soient semés au prochain déploiement.

        # ─── Emplacements (rôles) : l'IDENTITÉ FONCTIONNELLE, stable au remplacement ────────
        # Le vmid est un handle jetable, `instance_uuid` survit au recreate mais PAS au
        # remplacement (nouveau conteneur qui prend la fonction de l'ancien). Un emplacement est
        # la fonction elle-même (« MULTIVIEW RÉGIE 1 ») : c'est LUI que les systèmes de contrôle
        # externes adressent (Ember+ aujourd'hui), et il se réaffecte d'un conteneur à l'autre
        # sans que la config du pupitre en face ne bouge.
        #   num           : numéro Ember+ — AUTOINCREMENT = JAMAIS réattribué (sqlite_sequence).
        #                   Supprimer un emplacement laisse un TROU, c'est voulu : un numéro
        #                   recyclé re-pointerait silencieusement le pupitre sur autre chose.
        #   key           : slug IMMUABLE (identifier Ember+). Le libellé, lui, est renommable.
        #   expect_type   : type de plugin attendu (garde-fou de liaison, informatif).
        #   instance_uuid : conteneur SERVANT — NULL = emplacement hors ligne (la branche reste
        #                   publiée, avec isOnline=false : une branche qui DISPARAÎT laisse le
        #                   pupitre avec des boutons morts sans le savoir).
        db.execute('''CREATE TABLE IF NOT EXISTS production_roles (
            num           INTEGER PRIMARY KEY AUTOINCREMENT,
            key           TEXT NOT NULL UNIQUE,
            label         TEXT,
            expect_type   TEXT,
            instance_uuid TEXT,
            created_at    TEXT
        )''')
        # Un conteneur ne sert qu'UN emplacement (index partiel : les NULL ne collisionnent pas).
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_production_roles_instance "
                   "ON production_roles(instance_uuid) WHERE instance_uuid IS NOT NULL")
        db.execute('''CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            created_at  TEXT,
            snapshot    TEXT
        )''')
        pcols = [r[1] for r in db.execute("PRAGMA table_info(projects)")]
        if "media_path" not in pcols:
            db.execute("ALTER TABLE projects ADD COLUMN media_path TEXT")
        # Chantier 1 (cf. docs/reference/PROJETS.md §12) : propriétaire du projet (NULL = legacy,
        # géré par les admins) + membres avec rôle par projet.
        if "owner_id" not in pcols:
            db.execute("ALTER TABLE projects ADD COLUMN owner_id INTEGER "
                       "REFERENCES users(id) ON DELETE SET NULL")
        # Chantier 3 : cycle de vie du projet (saved|loading|active|error|unloading).
        if "state" not in pcols:
            db.execute("ALTER TABLE projects ADD COLUMN state TEXT DEFAULT 'saved'")
        # Chantier 4 : ports virtuels — la frontière du projet (sources/destinations
        # nommées). L'intérieur du projet se câble sur les ports ; l'admin binde le
        # physique (binding JSON : {"shm":…, "audio_shm":…} pour une source ;
        # {"internal_shm":…} pour une destination = sortie interne publiée).
        db.execute('''CREATE TABLE IF NOT EXISTS project_ports (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind           TEXT NOT NULL DEFAULT 'source',
            media          TEXT NOT NULL DEFAULT 'video',
            name           TEXT NOT NULL,
            ord            INTEGER DEFAULT 0,
            channel_labels TEXT,
            binding        TEXT,
            created_at     TEXT
        )''')
        # Chantier 6 : macros/scénarios (graph = blocs structurés, format blocks/v1)
        # + variables de projet (lues/écrites par les macros, gabarits {{var}}).
        db.execute('''CREATE TABLE IF NOT EXISTS project_macros (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            owner_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            graph        TEXT,
            published_to TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS project_vars (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            value      TEXT,
            PRIMARY KEY (project_id, name)
        )''')
        # Chantier 6 (suite) : déclencheurs permanents « quand <condition> devient vraie
        # → lancer <macro> » (front montant + cooldown_ms, poller dans app/macros.py).
        db.execute('''CREATE TABLE IF NOT EXISTS project_triggers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name        TEXT,
            enabled     INTEGER DEFAULT 0,
            condition   TEXT,
            macro_id    INTEGER,
            cooldown_ms INTEGER DEFAULT 2000,
            created_at  TEXT
        )''')
        # Chantier 3 « projet vivant » : historique de versions du snapshot.
        # label NULL = version automatique (rétention bornée) ; label posé = version
        # nommée (« avant émission »), conservée sans limite.
        db.execute('''CREATE TABLE IF NOT EXISTS project_versions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            created_at TEXT,
            label      TEXT,
            snapshot   TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS project_members (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role       TEXT NOT NULL DEFAULT 'viewer',
            PRIMARY KEY (project_id, user_id)
        )''')
        # Chantier 2 : vues composées d'un projet (interfaces utilisateur sauvegardées).
        # layout = JSON [{id, widget, type, instance_uuid, vmid, x, y, w, h, params}] —
        # les widgets référencent les containers par instance_uuid (stable au recreate).
        db.execute('''CREATE TABLE IF NOT EXISTS project_views (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            owner_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            visibility  TEXT NOT NULL DEFAULT 'private',
            edit_shared INTEGER NOT NULL DEFAULT 0,
            layout      TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS layouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            created_at  TEXT,
            config      TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS pip_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            created_at  TEXT,
            updated_at  TEXT,
            config      TEXT
        )''')
        pptcols = [r[1] for r in db.execute("PRAGMA table_info(pip_templates)")]
        if "tags" not in pptcols:
            # Tags libres (galerie de vignettes, Réglages → PiP) : liste JSON de chaînes,
            # NULL/'' = aucun tag. Uniquement sur les modèles UTILISATEUR (les modèles d'usine,
            # servis depuis app/pip_library.py:BUILTIN_PIP_TEMPLATES, n'ont pas de ligne ici).
            db.execute("ALTER TABLE pip_templates ADD COLUMN tags TEXT")
        # Bibliothèque de POLICES côté orchestrateur (Réglages → Polices). Le FICHIER vit dans
        # static/uploads/fonts/<sha256>.<ext> (préservé par l'updater) ; la ligne ne porte que
        # les métadonnées. La CLÉ D'USAGE d'une police est `lib:<sha256[:16]>` (cf. app/fonts.py)
        # → l'import de layouts/modèles de PiP déduplique par HASH, jamais par nom.
        db.execute('''CREATE TABLE IF NOT EXISTS fonts (
            sha256      TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            family      TEXT,
            style       TEXT,
            ext         TEXT,
            size        INTEGER,
            created_at  TEXT,
            created_by  TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'viewer',
            created_at    TEXT
        )''')
        ucols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
        for _c in ("prenom", "nom", "email"):
            if _c not in ucols:
                db.execute(f"ALTER TABLE users ADD COLUMN {_c} TEXT")
        # Préférence de langue d'interface (i18n) — défaut 'fr'
        if "lang" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'fr'")
        # Thème de l'interface — préférence PAR UTILISATEUR, même modèle que `lang` :
        # NULL = suivre le défaut du système (setting global `theme`). Un thème est un confort
        # de poste de travail (régie sombre, bureau en plein jour) ; il n'a aucune raison
        # d'être imposé à toute la flotte par le dernier qui y a touché.
        if "theme" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN theme TEXT")
        # Interface d'atterrissage au login : 'technique' (UI actuelle) ou 'projets'
        # (accueil /workspaces). Défaut 'technique' pour les comptes existants —
        # personne ne change de comportement sans action de l'admin (docs/reference/PROJETS.md §12).
        if "interface" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN interface TEXT DEFAULT 'technique'")
        # Fiche de la personne : de quoi la joindre et la situer dans l'organisation. Un parc
        # broadcast se pilote à plusieurs, souvent de nuit : savoir QUI a déployé un conteneur
        # et comment le joindre en trois secondes vaut mieux qu'un nom d'utilisateur seul.
        # Toutes facultatives, toutes modifiables par l'intéressé (≠ `role` et `interface`).
        for _c, _t in (("telephone", "TEXT"), ("service", "TEXT"), ("poste", "TEXT"),
                       ("photo_url", "TEXT")):
            if _c not in ucols:
                db.execute("ALTER TABLE users ADD COLUMN %s %s" % (_c, _t))
        # ─── Sessions ouvertes ──────────────────────────────────────────────────────────────
        # Une session Flask est un COOKIE SIGNÉ : le serveur ne sait ni combien sont ouvertes,
        # ni les fermer. « Fermer mes autres sessions » était donc impossible à tenir, et
        # « depuis quel poste suis-je connecté » impossible à répondre. D'où ce registre : le
        # cookie ne porte plus qu'un identifiant opaque, et c'est CETTE table qui décide.
        #
        # `revoked` plutôt qu'un DELETE : une session fermée reste listable un moment, et on
        # distingue « jamais existé » (cookie forgé, ou base réinitialisée) de « fermée ».
        db.execute('''CREATE TABLE IF NOT EXISTS user_sessions (
            sid        TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT,
            last_seen  TEXT,
            ip         TEXT,
            user_agent TEXT,
            revoked    INTEGER NOT NULL DEFAULT 0
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user "
                   "ON user_sessions(user_id, revoked)")
        # Dernière connexion réussie, et ÉPOQUE de session.
        #
        # ⚠ L'ÉPOQUE EXISTE POUR UNE RAISON PRÉCISE. Les cookies émis AVANT ce registre ne
        # portent pas d'identifiant de session : à leur prochain usage ils s'en font attribuer
        # un (personne n'est déconnecté par la mise à jour). Mais un cookie ancien resté
        # DORMANT sur un poste oublié n'existe encore nulle part au moment où l'on clique
        # « fermer mes autres sessions » — il se ferait inscrire tranquillement le lendemain,
        # et aurait survécu à une révocation censée être totale. L'époque ferme ce trou : elle
        # est gravée dans le cookie, comparée à celle du compte, et un cookie plus ancien que
        # l'époque du compte est refusé, qu'il soit inscrit ou non.
        if "last_login" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        if "session_epoch" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
        # Jeton GitHub PERSONNEL, pour la page Mises à jour. Anonyme, GitHub accorde 60 requêtes
        # par heure et par IP : une dizaine par relecture du catalogue, donc six relectures. Un
        # jeton monte à 5 000. Il est par UTILISATEUR et non par site : c'est une identité, celui
        # qui bute sur le plafond fournit la sienne et n'élargit que pour lui.
        # ⚠ Stocké en clair, donc présent dans les sauvegardes de base. L'interface exige
        # explicitement un jeton SANS AUCUNE PORTÉE (lecture publique seule) : il n'ouvre alors
        # rien de plus que ce que n'importe qui lit déjà sans être authentifié.
        if "gh_token" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN gh_token TEXT")

        # ─── Rôles et autorisations ────────────────────────────────────────────────────────
        # Les rôles étaient des CONSTANTES Python : ajouter « le monteur peut piloter les
        # plugins mais pas déployer » demandait de modifier le code et de redéployer. La table
        # les rend éditables ; `auth.py` la lit et retombe sur ses constantes si elle est vide
        # (installation neuve, ou base d'une version antérieure).
        #
        # `permissions` = liste JSON EXPLICITE, jamais un masque de bits ni un « tout sauf » :
        # une permission ajoutée au produit plus tard ne doit être accordée à personne par
        # accident. Elle apparaîtra simplement décochée partout.
        db.execute('''CREATE TABLE IF NOT EXISTS habilitations (
            id            TEXT PRIMARY KEY,
            label         TEXT,
            permissions   TEXT NOT NULL DEFAULT '[]',
            global_access INTEGER NOT NULL DEFAULT 0,
            builtin       INTEGER NOT NULL DEFAULT 0
        )''')

        # Surcouche de traductions éditée via l'UI (i18n) : appliquée PAR-DESSUS les
        # catalogues fichiers, donc persistante à travers les sync git (qui écrasent
        # les *.json versionnés). PK (lang, key) → une valeur par couple.
        db.execute('''CREATE TABLE IF NOT EXISTS i18n_overrides (
            lang  TEXT NOT NULL,
            key   TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (lang, key)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS cable_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            created_at  TEXT,
            payload     TEXT
        )''')
        # Vues de DISPOSITION de la page Câbles (mode « Libre ») : positions des cartes +
        # cartes repliées. Distinct de cable_snapshots (qui sauve le CÂBLAGE/edges) : ici on
        # ne stocke QUE la disposition visuelle, partagée entre utilisateurs.
        db.execute('''CREATE TABLE IF NOT EXISTS cable_layouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            created_at  TEXT,
            payload     TEXT
        )''')
        # Stockage générique par plugin (presets, mémoires…) : remplace les anciennes
        # tables cc_presets / dve_memories (retirées en 2026-07 avec leur migration ;
        # les fichiers DB existants peuvent encore porter ces tables, ignorées).
        # scope='' = global ; scope=str(vmid) = par container. value = JSON.
        db.execute('''CREATE TABLE IF NOT EXISTS plugin_store (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            scope      TEXT NOT NULL DEFAULT '',
            name       TEXT NOT NULL,
            value      TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )''')
        # Liens de partage publics (page client WebRTC) : jeton aléatoire non devinable
        # (secrets.token_urlsafe) → page publique `/w/<token>`. Révocable (delete).
        db.execute('''CREATE TABLE IF NOT EXISTS share_links (
            token      TEXT PRIMARY KEY,
            vmid       INTEGER NOT NULL,
            path       TEXT NOT NULL,
            title      TEXT,
            note       TEXT,
            created_at TEXT
        )''')
        # ⚠ `kind` DISTINGUE LA NATURE DE LA PAGE PUBLIQUE, et ce n'est pas cosmétique : le
        # jeton d'un lecteur WebRTC et celui d'un scope ouvrent des pages différentes et
        # donnent accès à des choses différentes. Sans cette colonne, il faudrait deviner à
        # partir du type du conteneur — donc réinterpréter un droit d'accès à chaque requête,
        # et se tromper le jour où un type change. Migration idempotente ; les liens existants
        # sont des liens WebRTC, d'où le défaut.
        _sl = [r[1] for r in db.execute("PRAGMA table_info(share_links)").fetchall()]
        if _sl and "kind" not in _sl:
            db.execute("ALTER TABLE share_links ADD COLUMN kind TEXT DEFAULT 'webrtc'")
            db.execute("UPDATE share_links SET kind='webrtc' WHERE kind IS NULL")
        # `cidrs` : liste d'adresses ou de réseaux autorisés, séparés par des virgules. VIDE =
        # aucune restriction, ce qui est le comportement des liens existants — un filtre qui
        # s'activerait tout seul à la migration couperait des liens en service sans prévenir.
        if _sl and "cidrs" not in _sl:
            db.execute("ALTER TABLE share_links ADD COLUMN cidrs TEXT")
        # ⚠ UN LIEN PUBLIC DOIT PORTER L'`instance_uuid`, PAS LE `vmid`. Le vmid est un handle
        # LOCAL ET JETABLE — réattribué, il change au recreate (cf. CLAUDE.md, « Identité d'un
        # conteneur : trois barreaux »). Un jeton accroché au vmid a deux défauts, et le second
        # est grave :
        #   1. il MEURT à la recréation du conteneur dans un projet, alors que l'exploitant
        #      attend l'inverse — c'est le même appareil, l'identité d'instance le dit ;
        #   2. si ce vmid est REPRIS par un autre conteneur du même type, le vieux jeton ouvre
        #      la page de CET AUTRE conteneur. Un accès sans identification qui se déplace tout
        #      seul sur une autre machine.
        # L'identité d'instance, elle, survit recreate/restore/import et n'est jamais réattribuée.
        if _sl and "instance_uuid" not in _sl:
            db.execute("ALTER TABLE share_links ADD COLUMN instance_uuid TEXT")
            # Rattrapage : les liens dont le conteneur vit encore reçoivent son identité.
            # Ceux dont il a disparu restent sans — ils sont orphelins, et c'est exact.
            db.execute("UPDATE share_links SET instance_uuid = ("
                       "  SELECT c.instance_uuid FROM containers c WHERE c.vmid = share_links.vmid"
                       ") WHERE instance_uuid IS NULL")
        # Pool SR-IOV nic_pool RETIRÉ (vestige LXC) : le moteur 2110_io tourne sur la PF en
        # AF-XDP, sans VF. Les NIC 2110 se déclarent via node_interfaces (role=media2110).
        # Purge idempotente des tables mortes.
        db.execute("DROP TABLE IF EXISTS nic_vf_alloc")
        db.execute("DROP TABLE IF EXISTS nic_pool")
        # Registre des autres instances Bobi.Studio du réseau (mise à jour pull/push).
        # token = secret d'update DU PAIR (pour le joindre). version/last_seen via ping.
        db.execute('''CREATE TABLE IF NOT EXISTS peers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            url        TEXT NOT NULL UNIQUE,
            token      TEXT,
            version    TEXT,
            last_seen  TEXT,
            created_at TEXT
        )''')
        # deployed_at : date/heure du dernier déploiement appliqué sur le pair (via son ping).
        if "deployed_at" not in [r[1] for r in db.execute("PRAGMA table_info(peers)")]:
            db.execute("ALTER TABLE peers ADD COLUMN deployed_at TEXT")
        # Cluster multi-nœud : un nœud = un hôte d'exécution (Docker ou Proxmox/LXC). Fondation
        # pour le futur cluster avec RAM partagée (MXL). mxl_mount = point de montage du shared
        # memory (défaut /dev/shm local ; pointera la fabric partagée le jour venu).
        db.execute('''CREATE TABLE IF NOT EXISTS nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'docker',
            host        TEXT NOT NULL,
            mtl_iface   TEXT,
            mtl_capable INTEGER NOT NULL DEFAULT 0,
            lcores      TEXT,
            image       TEXT,
            mxl_mount   TEXT DEFAULT '/dev/shm',
            ram_mb      INTEGER,
            status      TEXT DEFAULT 'unknown',
            created_at  TEXT
        )''')
        cols_c = [r[1] for r in db.execute("PRAGMA table_info(containers)")]
        if "node_id" not in cols_c:
            db.execute("ALTER TABLE containers ADD COLUMN node_id INTEGER REFERENCES nodes(id)")
        if "docker_name" not in cols_c:
            db.execute("ALTER TABLE containers ADD COLUMN docker_name TEXT")
        # docker_ip : IP propre du conteneur Docker « compute » (réseau macvlan/ipvlan), lue
        # après `docker run`. Pour ces conteneurs get_container_ip renvoie cette IP (pas l'hôte
        # du nœud comme le chemin MTL --network host). NULL pour LXC / MTL.
        if "docker_ip" not in cols_c:
            db.execute("ALTER TABLE containers ADD COLUMN docker_ip TEXT")
        # Colonne `backend` RETIRÉE (full-Docker) : elle ne pouvait plus valoir que 'docker', et un
        # schéma qui affiche encore « DEFAULT 'lxc' » raconte une architecture qui n'existe plus.
        # Migration en deux temps, dans cet ordre et une seule fois : purge des lignes LXC
        # résiduelles (pure DB — aucun conteneur réel détruit, la prod est 100% docker), puis
        # suppression de la colonne. Idempotent : après le DROP, la condition est fausse à jamais.
        if "backend" in cols_c:
            db.execute("DELETE FROM containers WHERE backend = 'lxc'")
            db.execute("ALTER TABLE containers DROP COLUMN backend")
        # nodes : réseau macvlan + image compute génériques (chemin Docker « compute »).
        cols_n = [r[1] for r in db.execute("PRAGMA table_info(nodes)")]
        if "docker_network" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN docker_network TEXT")
        if "compute_image" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN compute_image TEXT")
        # Pool de cœurs CPU dispo pour le pinning compute (ex. "8-47"). Vide = pas de pinning.
        if "compute_cpuset" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN compute_cpuset TEXT")
        # Variante GPU/NVIDIA de l'image compute (tag bobi-compute-gpu:<ver>, buildée node_only sur
        # le nœud GPU). Renseignée si le nœud porte un GPU NVIDIA + nvidia-container-toolkit ; le
        # déploiement d'un plugin GPU-capable (multiview) choisit cette image + injecte --gpus.
        if "compute_gpu_image" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN compute_gpu_image TEXT")
        # GPU NVIDIA : gpu_capable (0/1, détecté côté orchestrateur via nvidia-smi + runtime nvidia
        # Docker) ; gpu_count = nb de GPU (défaut 1). Un plugin GPU-capable (multiview) déployé sur un
        # nœud gpu_capable prend compute_gpu_image + --gpus (cf. gpu_pool / docker_compute).
        if "gpu_capable" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN gpu_capable INTEGER NOT NULL DEFAULT 0")
        if "gpu_count" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN gpu_count INTEGER")
        # Plugins MÉDIA (player/recorder/transcoder/stills) : image dédiée (GStreamer+ffmpeg) et
        # point de montage LOCAL du stockage média sur l'hôte du nœud (bind → /mnt/media).
        if "media_image" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN media_image TEXT")
        if "media_mount" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN media_mount TEXT")
        # Auto-recovery au reboot d'un nœud (app/node_recovery.py) : dernier boot connu
        # (boot_ts ≈ now − host_uptime_s du /v1/health) et dernier boot pour lequel le recovery a
        # été exécuté. Persistés → détection/one-shot survivent à un restart du contrôleur.
        if "last_boot_ts" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN last_boot_ts REAL")
        if "recovered_boot_ts" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN recovered_boot_ts REAL")
        # Profil CPU du nœud : modèle relevé (clé vers `cpu_profiles`) et surcharge explicite du
        # quota de scheduler. Le quota est une propriété de la MACHINE, pas du site.
        if "cpu_model" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN cpu_model TEXT")
        if "sch_quota_mbs" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN sch_quota_mbs INTEGER")
        # io2110 (Docker) : IP du plan MÉDIA 2110 (CIDR, ex. "198.51.100.60/24") assignée à mtl_iface.
        # Sans elle, le PF n'a pas d'IPv4 → MTL annonce sip=0.0.0.0 (TX cassé, SSM impossible) et la
        # jointure IGMPv3 source-specific échoue → rx_gbps=0 (free-run noir). Le chemin LXC dérivait
        # cette IP du pool VF (vf_sip) ; le chemin Docker la stocke ici. Ré-appliquée au (re)déploiement
        # du conteneur MTL (idempotent), le moteur l'auto-détecte (_detect_iface_ip).
        if "media_ip" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN media_ip TEXT")
        # Identité du DOMAINE MXL du nœud (BCP-007-03 « NMOS With MXL », `domain_def.json:id`).
        # Le problème que ce champ règle : un domaine bind-monté sous DEUX chemins différents
        # selon le conteneur (`/dev/shm/mxl` ici, `/domain_a` là) est le MÊME domaine, et rien
        # dans le chemin ne le dit. La BCP donne donc au domaine une identité propre, portée par
        # un fichier À LA RACINE du domaine — elle voyage avec le montage. On la garde ici parce
        # que le tmpfs, lui, ne survit pas au reboot : la DB est la source de vérité, le fichier
        # n'en est qu'une projection reposée par `mtl.ensure_mxl_domain_def`.
        if "mxl_domain_id" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN mxl_domain_id TEXT")
        # Agent-nœud (bobi-node-agent) : si agent_url renseigné, le contrôleur pilote le nœud via
        # l'API HTTP de l'agent (token) au lieu du root-SSH/Proxmox. capabilities = source de vérité
        # d'éligibilité (JSON array). Cf. NODE_AGENT.md / node_driver.py.
        if "capabilities" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN capabilities TEXT")
        if "agent_url" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN agent_url TEXT")
        if "agent_token" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN agent_token TEXT")
        if "agent_version" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN agent_version TEXT")
        if "last_seen" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN last_seen TEXT")
        # Enrôlement zéro-touch (clé USB préseedée) : un nœud pré-déclaré porte un enroll_token
        # one-time + un profil (JSON : caps, macvlan subnet/gw/vlan, ptp, hugepages, registry) que
        # le nœud vierge récupère au 1er boot via POST /api/nodes/enroll. status: pending→enrolling→up.
        if "enroll_token" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN enroll_token TEXT")
        if "enroll_profile" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN enroll_profile TEXT")
        # Carte de gestion iLO (HPe iLO 5) : montage auto de l'ISO d'enrôlement en CD virtuel via
        # Redfish (cf. app/ilo.py). Mot de passe stocké en clair comme agent_token (réseau interne).
        # NE PAS placer dans enroll_profile (ce blob est renvoyé au nœud à l'enrôlement).
        if "ilo_host" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN ilo_host TEXT")
        if "ilo_user" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN ilo_user TEXT")
        if "ilo_password" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN ilo_password TEXT")
        # BMC vendor-agnostique (Redfish) : 'hpe' (iLO) | 'dell' (iDRAC). Les chemins/IDs Redfish
        # diffèrent par constructeur (cf. app/ilo.py). Les colonnes ilo_* restent le transport des
        # identifiants (host/user/password) quel que soit le vendor. Backfill 'hpe' si iLO renseigné.
        if "bmc_vendor" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN bmc_vendor TEXT DEFAULT 'hpe'")
        db.execute("UPDATE nodes SET bmc_vendor='hpe' "
                   "WHERE (bmc_vendor IS NULL OR bmc_vendor='') AND ilo_host IS NOT NULL AND ilo_host<>''")
        # mTLS du plan de contrôle : tls_ready=1 quand le nœud a reçu un cert signé par la CA interne
        # et que son agent écoute en HTTPS (sur le MÊME port). node_driver dial alors en HTTPS (+ token
        # applicatif en plus). 0 = flotte non migrée → repli HTTP+token (rétro-compatible). node_cert =
        # archive du cert signé (PEM public, diagnostic uniquement — la clé privée ne quitte JAMAIS le nœud).
        if "tls_ready" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN tls_ready INTEGER DEFAULT 0")
        if "node_cert" not in cols_n:
            db.execute("ALTER TABLE nodes ADD COLUMN node_cert TEXT")
        # Modèle « interface → rôle » par nœud (refonte réseau 2026-06) : source de vérité de ce à
        # quoi sert chaque NIC d'un nœud. Subsume mtl_iface/media_ip/parent docker_network qui restent
        # synchronisés (pont de compat — cf. routes api_node_interfaces). Rôles : management |
        # containers (parent macvlan) | media2110 (avec pair_role red/blue + pair_group pour 2022-7
        # ET capacité) | rdma | bmc | unused. ptp_enabled = PTP par-interface (cadre PTP multi-NIC).
        db.execute('''CREATE TABLE IF NOT EXISTS node_interfaces (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id     INTEGER NOT NULL REFERENCES nodes(id),
            ifname      TEXT,
            mac         TEXT,
            pci         TEXT,
            role        TEXT NOT NULL DEFAULT 'unused',
            pair_role   TEXT,
            pair_group  INTEGER,
            ip_cidr     TEXT,
            gateway     TEXT,
            vlan        TEXT,
            ptp_enabled INTEGER DEFAULT 0,
            mtu         INTEGER,
            notes       TEXT,
            created_at  TEXT
        )''')
        # nic_profiles : BIBLIOTHÈQUE DE CARTES (capacités NON auto-découvrables — cf. docs/chantiers/DPDK_NARROW.md §7 :
        # le max de files TX narrow effectif n'est pas lisible du PMD). Un profil MESURÉ par la qualification
        # PRIME sur la biblio statique (app/mtl.py) et le plancher sûr. Keyé (device_id + firmware) — le
        # firmware/DDP peut changer les capacités → une carte peut y figurer plusieurs fois (firmware='' =
        # non renseigné / valeur biblio). `measured=1` = qualifié au banc.
        db.execute('''CREATE TABLE IF NOT EXISTS nic_profiles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    TEXT NOT NULL,
            firmware     TEXT NOT NULL DEFAULT '',
            model        TEXT,
            rl_tx_cap    INTEGER,
            narrow_ok    INTEGER,
            ddp_ok       INTEGER,
            ptp_ok       INTEGER,
            measured     INTEGER DEFAULT 0,
            notes        TEXT,
            qualified_at TEXT,
            UNIQUE(device_id, firmware)
        )''')
        # Profils CPU : quota de scheduler libmtl PAR MODÈLE de processeur. Même modèle que
        # `nic_profiles` (bibliothèque + valeur mesurée qui prime), pour la même raison : la capacité
        # d'un scheduler — combien de Mb/s de ST 2110 un cœur en busy-poll parse et recopie — est une
        # propriété PHYSIQUE de la machine, pas une préférence de site. Elle vivait pourtant dans un
        # réglage GLOBAL (`mtl_sch_quota_mbs`) appliqué à toute la flotte : sur un nœud plus rapide on
        # gaspille des cœurs, sur un nœud plus lent on sous-dimensionne — et sous-dimensionner ne se
        # voit pas tout de suite, ça se paie en sessions refusées ou en wedges sous jitter.
        # `measured=1` exige une campagne de CHARGE RÉELLE ayant atteint le décrochage ; un micro-banc
        # ne prouve rien (cf. la même garde dans app/nic_qualify.py, et la dérive 63→14 qu'elle a
        # coûtée sur les cartes). Sans mesure : la valeur reste déclarative et on le dit.
        db.execute('''CREATE TABLE IF NOT EXISTS cpu_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            model         TEXT NOT NULL,
            quota_mbs     INTEGER,
            cores         INTEGER,
            threads       INTEGER,
            base_mhz      INTEGER,
            memcpy_gbps   REAL,
            pkt_mpps      REAL,
            measured      INTEGER DEFAULT 0,
            notes         TEXT,
            qualified_at  TEXT,
            UNIQUE(model)
        )''')
        # Fenêtre de maintenance TX (docs/reference/TX_LAYOUTS.md étage 2) : bac des changements PERTURBATEURS
        # différés d'un moteur 2110_io. Une action perturbatrice recale l'arbre RL du port (commit TM
        # = stop/start du port ~1 s, cf. app/tx_maintenance.py) : on peut les GROUPER pour ne payer
        # qu'UN SEUL blip. `args` = JSON de l'action, rejouée telle quelle à l'application ;
        # `apply_at` = 'YYYY-MM-DDTHH:MM' (planifié) ou NULL (application manuelle).
        db.execute('''CREATE TABLE IF NOT EXISTS tx_pending_changes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            vmid       INTEGER NOT NULL,
            node_id    INTEGER,
            iface      TEXT,
            op         TEXT NOT NULL,
            args       TEXT NOT NULL DEFAULT '{}',
            label      TEXT,
            status     TEXT NOT NULL DEFAULT 'pending',
            apply_at   TEXT,
            created_at TEXT,
            created_by TEXT,
            result     TEXT
        )''')
        # tx_card_models : BIBLIOTHÈQUE DE MODÈLES DE CARTE 2110 (gabarits RÉUTILISABLES, par TYPE de
        # carte — PAS par interface). Un modèle = une composition nommée de sorties TX déclarées
        # (format annoncé par slot + audio + ANC), rattachée à un type de carte (`nic_model`, la même
        # chaîne que node_interfaces.model / nic_profiles.model). Il ne touche AUCUN matériel : le
        # déclarer ne coûte rien. On l'APPLIQUE ensuite à une carte réelle (page Interfaces) — c'est
        # là, et seulement là, que le coût (recalcul d'arbre RL en DPDK) se paie.
        # ⚠ Un modèle est une SOURCE ; la VÉRITÉ reste le layout appliqué de la carte
        # (settings:tx_layout_<node>_<iface> → deploy_config.tx_slots du moteur). Une carte peut
        # DIVERGER du modèle dont elle est issue : c'est une information, pas une erreur.
        db.execute('''CREATE TABLE IF NOT EXISTS tx_card_models (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            nic_model  TEXT NOT NULL DEFAULT '',
            slots      TEXT NOT NULL DEFAULT '[]',
            notes      TEXT,
            created_at TEXT,
            updated_at TEXT,
            updated_by TEXT
        )''')
        # PTP multi-NIC : domaine PTP par-interface (NULL → repli sur le réglage nœud ptp_domain).
        # Permet de grouper les media2110/ptp_enabled par domainNumber → un ptp4l JBOD par domaine.
        if "ptp_domain" not in [r[1] for r in db.execute("PRAGMA table_info(node_interfaces)")]:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN ptp_domain INTEGER")
        # Modèle + vitesse de lien capturés lors de la config réseau (sonde lspci/ethtool de
        # /api/nodes/<id>/interfaces). Persistés ICI → la page Sources/Destinations 2110 lit le
        # modèle exact (« E810-XXV-4 ») et l'agrégat (somme des vitesses) sans re-sonder en SSH.
        _ni_cols = [r[1] for r in db.execute("PRAGMA table_info(node_interfaces)")]
        if "model" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN model TEXT")
        if "speed_mbps" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN speed_mbps INTEGER")
        # Réserve de files AF-XDP du moteur 2110_io PAR INTERFACE media2110 (capacité « à chaud »
        # choisie par l'opérateur, plafonnée au budget de files de la carte). NULL = auto (le moteur
        # applique son plancher par défaut). Pilote la réserve de files ET le budget de lcores au
        # déploiement (cf. docker_driver._build_docker_run_controlplane / _auto_lcores).
        if "rx_reserve" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN rx_reserve INTEGER")
        if "tx_reserve" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN tx_reserve INTEGER")
        if "queue_margin" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN queue_margin INTEGER")
        # Chemin data du moteur 2110_io PAR INTERFACE media2110 (chantier DPDK/narrow) :
        # NULL/'af_xdp' = comportement actuel (AF_XDP natif sur la PF kernel) ; 'dpdk' = port
        # remis à vfio-pci et passé au moteur en BDF PCI (colonne `pci`) → PMD ice DPDK,
        # pacing RL. Opt-in strict par interface : sans ce flag, rien ne change.
        if "pmd" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN pmd TEXT")
        # SR-IOV (chantier narrow, cf. docs/chantiers/SRIOV_IMPL.md) : pmd='sriov' = PF reste kernel (ptp4l) + VF
        # DPDK-narrow porte le moteur. vf_bdf = BDF de la VF créée au host-prep (ex. 0000:11:11.0) ;
        # vf_ip = IP média (sip) de la VF (le trafic 2110 sort/entre par la VF ; la PF garde son `ip`
        # pour le PTP L4). NULL tant que la VF n'est pas provisionnée.
        if "vf_bdf" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN vf_bdf TEXT")
        if "vf_ip" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN vf_ip TEXT")
        # Profil d'émetteur ST 2110-21 PAR INTERFACE media2110 (chantier narrow) : classe de sender
        # NULL/'' = défaut (auto) | 'narrow' (N) | 'narrow_linear' (NL) | 'wide' (W). Pilote le
        # pacing du moteur (MTL_PACING) : narrow/NL → RL matériel (repli tsc_narrow si >cap),
        # wide → tsc. Device-level dans libmtl → règle « narrow-wins » sur un nœud multi-réseaux.
        if "output_profile" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN output_profile TEXT")
        # Alias lisible de la NIC média (ex. « PGM-Rouge ») affiché sur la page 2110_io et le
        # libellé du profil, au lieu de ens1f0np0/BDF. Purement cosmétique (aucune clé).
        if "alias" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN alias TEXT")
        # Plage IP conteneurs PAR NŒUD (2026-07) : posée sur la carte de rôle containers/
        # mgmt_containers. Si les deux bornes sont renseignées, l'allocation macvlan de CE nœud
        # pioche dans ct_ip_start..ct_ip_end (son subnet) au lieu de la plage cluster ip_start/ip_end.
        # NULL = comportement historique (plage cluster) — zéro régression pour l'existant.
        if "ct_ip_start" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN ct_ip_start TEXT")
        if "ct_ip_end" not in _ni_cols:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN ct_ip_end TEXT")
        # Backfill idempotent : semer node_interfaces depuis les scalaires historiques par nœud
        # (n'écrase rien — ne crée une ligne que si l'ifname est absent pour ce nœud).
        def _seed_iface(_node_id, ifname, role, **extra):
            if not (ifname or "").strip():
                return
            ex = db.execute("SELECT 1 FROM node_interfaces WHERE node_id=? AND ifname=?",
                            (_node_id, ifname)).fetchone()
            if ex:
                return
            cols = ["node_id", "ifname", "role", "created_at"]
            vals = [_node_id, ifname, role, datetime.now().isoformat(timespec="seconds")]
            for k, v in extra.items():
                if v is not None:
                    cols.append(k); vals.append(v)
            db.execute("INSERT INTO node_interfaces (%s) VALUES (%s)"
                       % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        for _nrow in db.execute("SELECT * FROM nodes").fetchall():
            _n = dict(_nrow)
            # ★ PAS de pair_role/pair_group ici. Le semis posait `red`/`0` sur la NIC média
            # primaire, à chaque démarrage, pour tout nœud dont la ligne manquait : un leg ROUGE
            # SOLITAIRE, qui n'apparie rien. Inerte (media_port_pairs exige red ET blue), mais c'était
            # un troisième dialecte pour dire « pas de 2022-7 » — à côté de « tout vide » et de
            # « groupe sans leg » — et le seul visible dans l'UI. Une NIC seule n'est le leg de rien.
            _seed_iface(_n["id"], _n.get("mtl_iface"), "media2110",
                        ip_cidr=_n.get("media_ip"))
        # B1b-cleanup : purge des réglages inertes de l'ère LXC (client API Proxmox + template 299).
        # Plus aucun code vivant ne les lit. `proxmox_host` + net_*/ip_* sont CONSERVÉS.
        db.executemany("DELETE FROM settings WHERE key=?", [
            (k,) for k in ("proxmox_node", "proxmox_user", "proxmox_token_id", "proxmox_token",
                           "storage", "template_vmid", "template_image", "template_disk_gb",
                           "template_memory", "template_cores",
                           "proxmox_host")])   # B1b-2 : host-ops par-nœud, hôte dans la table nodes
        # Allocations de cœurs par conteneur (pinning Docker) : 1 ligne par cœur attribué.
        db.execute('''CREATE TABLE IF NOT EXISTS node_core_alloc (
            node_id INTEGER NOT NULL,
            core    INTEGER NOT NULL,
            vmid    INTEGER NOT NULL,
            PRIMARY KEY (node_id, core)
        )''')
        # Allocations GPU par conteneur (sélecteur --gpus device=<idx>) : 1 ligne par vmid. Le GPU se
        # PARTAGE (time-slicing) → plusieurs vmid peuvent viser le même index (round-robin). Cf. gpu_pool.
        db.execute('''CREATE TABLE IF NOT EXISTS node_gpu_alloc (
            node_id   INTEGER NOT NULL,
            gpu_index INTEGER NOT NULL,
            vmid      INTEGER NOT NULL,
            PRIMARY KEY (node_id, vmid)
        )''')
        # Réglages PAR NŒUD (override du global) — refonte IA Réglages (portée global + override
        # par nœud). Résolution : node_settings > settings (global) > défaut. Cf. settings.setting_for.
        db.execute('''CREATE TABLE IF NOT EXISTS node_settings (
            node_id INTEGER NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (node_id, key)
        )''')
        # « Réseaux 2110 » : table GLOBALE (cluster). Un réseau = une horloge logique PTP (= un
        # ptp4l JBOD) avec un nom + un domaine. Remplace le regroupement par domaine BRUT → deux
        # réseaux peuvent partager un même numéro de domaine et rester indépendants. Les NIC
        # (de n'importe quel nœud) référencent media_network_id. Rouge/bleu = legs d'UN réseau.
        db.execute('''CREATE TABLE IF NOT EXISTS media_networks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            domain     INTEGER NOT NULL,
            created_at TEXT
        )''')
        # ptp_params : surcharges PTP de profil PAR RÉSEAU (JSON : priority1/2, log_*, announce_timeout,
        # delay_thresh, utc_offset, client_only). NULL/clé absente → hérite du réglage nœud. hw_ts reste
        # node-global (capacité carte).
        if "ptp_params" not in [r[1] for r in db.execute("PRAGMA table_info(media_networks)")]:
            db.execute("ALTER TABLE media_networks ADD COLUMN ptp_params TEXT")
        if "media_network_id" not in [r[1] for r in db.execute("PRAGMA table_info(node_interfaces)")]:
            db.execute("ALTER TABLE node_interfaces ADD COLUMN media_network_id INTEGER")
        # Règles de plage multicast STRICTES par port (switch qui contraint les adresses
        # autorisées par port) — scope='network' (média_network_id) ou 'interface' (node_id+ifname,
        # surcharge du réseau). match_json = critères de FORMAT optionnels (scan/résolution/fps
        # vidéo, nb de canaux audio) : absent → règle valable pour tout format de l'essence.
        db.execute('''CREATE TABLE IF NOT EXISTS mcast_ranges (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            scope            TEXT NOT NULL,
            media_network_id INTEGER,
            node_id          INTEGER,
            ifname           TEXT,
            base_ip          TEXT NOT NULL,
            size             INTEGER NOT NULL,
            port_default     INTEGER,
            essence          TEXT,
            leg              INTEGER,
            match_json       TEXT,
            label            TEXT,
            created_at       TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_mcast_ranges_net ON mcast_ranges(media_network_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_mcast_ranges_iface ON mcast_ranges(node_id, ifname)")
        # prefix_len : longueur du préfixe CIDR saisi par l'opérateur (ex. 24 pour un /24), gardée
        # pour ré-afficher la plage en notation CIDR fidèle — base_ip/size restent la source de
        # vérité pour le scan d'allocation (dérivés du CIDR à la saisie). port_default_* : port de
        # base PAR ESSENCE (2110-20 vidéo/2110-30 audio/2110-40 ANC) — remplace l'ancien port_default
        # unique (conservé pour compat, servait de repli générique).
        # ip_offset_* / ip_step_audio : PLAN d'adressage déterministe dans la plage (cf.
        # allocations._plan_offset). L'adresse d'un flux se DÉDUIT de son rang (base + décalage
        # d'essence + n° de sortie) au lieu d'être « la première libre » — la granularité d'un
        # abonnement IGMP est le GROUPE, pas le port : empiler plusieurs flux sur une même adresse
        # avec des ports différents force un récepteur qui s'abonne à l'audio à encaisser AUSSI la
        # vidéo du groupe. NULL = valeurs par défaut du plan (allocations.MCAST_PLAN_DEFAUT).
        for _col, _typ in (("prefix_len", "INTEGER"), ("port_default_video", "INTEGER"),
                          ("port_default_audio", "INTEGER"), ("port_default_anc", "INTEGER"),
                          ("ip_offset_video", "INTEGER"), ("ip_offset_audio", "INTEGER"),
                          ("ip_offset_anc", "INTEGER"), ("ip_step_audio", "INTEGER")):
            if _col not in [r[1] for r in db.execute("PRAGMA table_info(mcast_ranges)")]:
                db.execute(f"ALTER TABLE mcast_ranges ADD COLUMN {_col} {_typ}")
        # Ledger de réservation atomique (voir db_reserve_mcast) : PRIMARY KEY (ip, port) = la garantie
        # d'unicité vient de SQLite (INSERT qui échoue), pas d'une lecture Python suivie d'une décision.
        db.execute('''CREATE TABLE IF NOT EXISTS mcast_allocations (
            ip          TEXT NOT NULL,
            port        INTEGER NOT NULL,
            owner_ref   TEXT NOT NULL,
            reserved_at TEXT,
            PRIMARY KEY (ip, port)
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_mcast_allocations_owner ON mcast_allocations(owner_ref)")
        # Ledgers de RÉSERVATION ATOMIQUE vmid / IP conteneur (même patron que mcast_allocations :
        # la PRIMARY KEY tranche l'INSERT concurrent — le perdant reçoit IntegrityError). Ferme la
        # fenêtre de course lire-max-puis-décider de next_free_vmid()/allocate_container_ip(), où deux
        # créations simultanées obtenaient le même vmid / la même IP macvlan.
        db.execute('''CREATE TABLE IF NOT EXISTS vmid_reservations (
            vmid        INTEGER PRIMARY KEY,
            reserved_at TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS ip_reservations (
            ip          TEXT PRIMARY KEY,
            vmid        INTEGER,
            reserved_at TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_ip_reservations_vmid ON ip_reservations(vmid)")
        # Migration idempotente : rattacher les NIC PTP encore sans réseau. Domaine effectif d'une
        # NIC = ptp_domain de la NIC, sinon réglage nœud ptp_domain, sinon réglage global, sinon 127.
        def _eff_dom(_nid, _pdom):
            if _pdom is not None:
                return int(_pdom)
            for _q, _a in (("SELECT value FROM node_settings WHERE node_id=? AND key='ptp_domain'", (_nid,)),
                           ("SELECT value FROM settings WHERE key='ptp_domain'", ())):
                _r = db.execute(_q, _a).fetchone()
                if _r:
                    try: return int(json.loads(_r["value"]))
                    except Exception: pass
            return 127
        _pending = db.execute("SELECT id, node_id, ptp_domain FROM node_interfaces "
                              "WHERE role='media2110' AND ptp_enabled=1 AND media_network_id IS NULL").fetchall()
        if _pending:
            _net_by_dom = {r["domain"]: r["id"] for r in db.execute("SELECT id, domain FROM media_networks")}
            for r in _pending:
                d = _eff_dom(r["node_id"], r["ptp_domain"])
                if d not in _net_by_dom:
                    _nm = "Principal" if d == 127 else ("Domaine %d" % d)
                    _cur = db.execute("INSERT INTO media_networks (name, domain, created_at) VALUES (?,?,?)",
                                      (_nm, d, datetime.now().isoformat(timespec="seconds")))
                    _net_by_dom[d] = _cur.lastrowid
                db.execute("UPDATE node_interfaces SET media_network_id=? WHERE id=?", (_net_by_dom[d], r["id"]))
            # ptp_primary_domain (par-nœud) → ptp_primary_network
            for _nr in db.execute("SELECT DISTINCT node_id FROM node_interfaces WHERE media_network_id IS NOT NULL").fetchall():
                _nid = _nr["node_id"]
                if db.execute("SELECT 1 FROM node_settings WHERE node_id=? AND key='ptp_primary_network'", (_nid,)).fetchone():
                    continue
                _pd = db.execute("SELECT value FROM node_settings WHERE node_id=? AND key='ptp_primary_domain'", (_nid,)).fetchone()
                _pdv = None
                if _pd:
                    try: _pdv = int(json.loads(_pd["value"]))
                    except Exception: _pdv = None
                if _pdv is not None and _pdv in _net_by_dom:
                    db.execute("INSERT OR REPLACE INTO node_settings (node_id, key, value) VALUES (?,?,?)",
                               (_nid, 'ptp_primary_network', json.dumps(_net_by_dom[_pdv])))
        # Liens RDMA (chantier RDMA) : réplication d'UN flux MXL d'un nœud (initiator) vers un autre
        # (target) via mxl-fabrics (libfabric, provider verbs=RoCEv2). Chaque ligne = un lien actif,
        # piloté par app/rdma.py. src_flow = nom du flux MXL produit sur src_node. Les deux conteneurs
        # mxl-fabrics-demo (target sur dst_node, initiator sur src_node) sont nommés de façon
        # déterministe à partir de l'id (rdma-tgt-<id> / rdma-ini-<id>) — pas de vmid alloué.
        # target_info = descripteur de connexion base64 émis par le target (échangé hors-bande par
        # l'orchestrateur). status : pending | running | error | stopped.
        db.execute('''CREATE TABLE IF NOT EXISTS rdma_links (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            src_node_id   INTEGER NOT NULL,
            src_vmid      INTEGER,
            src_flow      TEXT NOT NULL,
            dst_node_id   INTEGER NOT NULL,
            kind          TEXT DEFAULT 'video',
            provider      TEXT DEFAULT 'verbs',
            service_port  INTEGER,
            status        TEXT DEFAULT 'pending',
            target_info   TEXT,
            notes         TEXT,
            created_at    TEXT
        )''')
        # flow_format (JSON) : format du flux répliqué (w/h/chroma/bit_depth/scan/fps), lu du flowDef
        # source à l'établissement. Permet d'exposer le flux comme source de câblage sur le nœud dst
        # (api_home_summary) sans host_exec à chaque poll. Migration idempotente.
        if "flow_format" not in [r[1] for r in db.execute("PRAGMA table_info(rdma_links)")]:
            db.execute("ALTER TABLE rdma_links ADD COLUMN flow_format TEXT")
        # auto_cable=1 : lien créé AUTOMATIQUEMENT par un câblage inter-nœud (app/rdma.ensure_cable_link)
        # → seul ce type est auto-supprimé au décâblage. Les liens manuels (onglet RDMA) restent.
        if "auto_cable" not in [r[1] for r in db.execute("PRAGMA table_info(rdma_links)")]:
            db.execute("ALTER TABLE rdma_links ADD COLUMN auto_cable INTEGER DEFAULT 0")
        # src_addr / dst_addr : adresses d'endpoint fabric FIGÉES à la création du lien, quand un nœud
        # porte PLUSIEURS interfaces de rôle 'rdma' (agrégation : 2×10G face à 1×25G, cf. dell-1). Un
        # lien = UN chemin = une paire (adresse source, adresse destination) du MÊME sous-réseau — le
        # RDMA ne répartit pas une QP entre deux ports, donc l'agrégat s'obtient en distribuant les
        # LIENS, pas en agrégeant les ports (un bond LACP ne donnerait rien de plus, et le RoCE-over-LAG
        # n'existe pas sur mlx4). NULL = comportement historique : première interface 'rdma' trouvée,
        # ce qui laisse les liens d'avant la migration intacts et sans resave.
        for _c in ("src_addr", "dst_addr"):
            if _c not in [r[1] for r in db.execute("PRAGMA table_info(rdma_links)")]:
                db.execute(f"ALTER TABLE rdma_links ADD COLUMN {_c} TEXT")
        # sync_batch : lot de synchronisation (`maxSyncBatchSizeHint`) RÉELLEMENT posé sur la
        # réplique, au moment où la cible l'a CRÉÉE. Ce n'est pas une copie du réglage : une option
        # de flux se fixe à la création, et une cible qui trouve la réplique déjà là la RÉATTACHE
        # en ignorant `--flow-options`. Sans cette colonne, le réglage est une intention qu'on
        # espère et que rien ne confronte au terrain — c'est-à-dire une trame de latence qui peut
        # revenir en silence. NULL = inconnu (lien antérieur au suivi), et ce n'est PAS 0 :
        # « on n'a pas la valeur » ne se confond pas avec « le lot vaut le défaut du SDK ».
        if "sync_batch" not in [r[1] for r in db.execute("PRAGMA table_info(rdma_links)")]:
            db.execute("ALTER TABLE rdma_links ADD COLUMN sync_batch INTEGER")
        # Tissu de composition : registre des nœuds de fabric MATÉRIALISÉS, keyé par SIGNATURE de
        # contenu (cf. app/compositor_fabric). signature → conteneur (vmid) qui le rend + son shm de
        # sortie. Permet la déduplication (un nœud partagé entre N multiviews = 1 ligne) et le cycle
        # de vie (last_ref : retrait après grâce quand plus aucun output ne le référence).
        db.execute('''CREATE TABLE IF NOT EXISTS fabric_node_alloc (
            signature  TEXT PRIMARY KEY,
            node_id    INTEGER,
            vmid       INTEGER,
            ref        TEXT,
            shm        TEXT,
            kind       TEXT,
            out_w      INTEGER,
            out_h      INTEGER,
            parents    TEXT,
            created_at TEXT,
            last_ref   TEXT,
            tile_x     INTEGER,
            tile_y     INTEGER,
            fmt        TEXT
        )''')
        # Registre NMOS de NIVEAU CLUSTER (C2a) : ressources sender/receiver à UUID + transport
        # STABLES, indépendantes des conteneurs. Un conteneur 2110 « sert » une ressource via son
        # instance_uuid (C1, stable à travers recreate/projet) → l'identité NMOS ne suit plus le vmid.
        db.execute('''CREATE TABLE IF NOT EXISTS nmos_resources (
            id                 TEXT PRIMARY KEY,   -- UUID NMOS STABLE (sender/receiver id)
            kind               TEXT,               -- 'sender' | 'receiver'
            essence            TEXT,               -- 'video' | 'audio' | 'data'
            label              TEXT,
            group_name         TEXT,               -- BCP-002 natural grouping
            role               TEXT,               -- grouphint role ('video' | 'audio N' | 'data')
            transport          TEXT,               -- JSON : multicast/port/legs/format/refclk…
            bind_instance_uuid TEXT,               -- conteneur servant (instance_uuid C1)
            bind_slot          INTEGER,            -- index du slot dans le conteneur (rx idx / tx slot)
            created_at         TEXT
        )''')
        # C2b : label op-owné — une fois relabellée à la main (label_locked=1), la ressource garde
        # son libellé à travers les rebuilds (n'est plus écrasée par le hostname du conteneur servant).
        _ncols = [r[1] for r in db.execute("PRAGMA table_info(nmos_resources)")]
        if "label_locked" not in _ncols:
            db.execute("ALTER TABLE nmos_resources ADD COLUMN label_locked INTEGER DEFAULT 0")
        # Snapshots nommés de la config NMOS (pool + bindings + réglages) rappelables d'un clic.
        # payload = JSON de nmos_config_snapshot() ; les UUID y sont préservés (rappel sans casser le
        # routage du contrôleur).
        db.execute('''CREATE TABLE IF NOT EXISTS nmos_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            payload    TEXT NOT NULL,
            created_at TEXT
        )''')
        _fcols = [r[1] for r in db.execute("PRAGMA table_info(fabric_node_alloc)")]
        if "ref" not in _fcols:
            db.execute("ALTER TABLE fabric_node_alloc ADD COLUMN ref TEXT")
        # parents : JSON list des vmids de multiviews logiques qui consomment ce nœud (pour replier
        # les internes du tissu sous leur multiview dans l'UI). Un nœud partagé a plusieurs parents.
        if "parents" not in _fcols:
            db.execute("ALTER TABLE fabric_node_alloc ADD COLUMN parents TEXT")
        # EMPLACEMENT du nœud dans son mur (tile_x/tile_y) + empreinte de FORMAT de sortie (fmt).
        # Un nœud est adressé par sa signature de CONTENU : le moindre changement de pixel en fait
        # un autre nœud, donc un autre conteneur. Ces trois colonnes permettent de reconnaître
        # qu'un nœud « neuf » occupe le MÊME emplacement, à la MÊME taille et au MÊME format qu'un
        # nœud qui disparaît — auquel cas on mute le conteneur existant à chaud au lieu de le
        # détruire et d'en créer un autre (cf. compositor_fabric.reconcile_fabric, rebind).
        for _c, _t in (("tile_x", "INTEGER"), ("tile_y", "INTEGER"), ("fmt", "TEXT")):
            if _c not in _fcols:
                db.execute(f"ALTER TABLE fabric_node_alloc ADD COLUMN {_c} {_t}")
        # Journal (alertes) : historiquement absente d'init_db (existait dans la DB en
        # place) → une DB recréée cassait silencieusement db_add_alert. Créée ici.
        db.execute('''CREATE TABLE IF NOT EXISTS alerts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            message   TEXT NOT NULL,
            niveau    TEXT DEFAULT 'info',
            timestamp TEXT
        )''')
        # CONTEXTE MACHINE de l'alerte (2026-07) : `vmid`, `node_id`, `kind`. Avant, une alerte
        # n'était QUE du texte — l'UI (« ⤷ journal ») et le service d'alertes DEVINAIENT le vmid en
        # relisant le message (regex + hostnames connus). Ça tombait en panne exactement là où le
        # journal durable est utile : un conteneur DÉTRUIT n'a plus de hostname en base. Ces trois
        # colonnes sont OPTIONNELLES (NULL) : un producteur non migré continue d'écrire comme avant,
        # et les consommateurs retombent sur la déduction textuelle. Pas de reprise rétroactive
        # (décision 2026-07) : la rétention à 1000 lignes renouvelle le parc en ~2 jours.
        # `kind` = VOCABULAIRE FERMÉ, cf. ALERT_KINDS plus bas (une chaîne libre par appel ne
        # serait pas filtrable — c'est tout l'intérêt de la colonne).
        # ACTEUR (2026-07-27) : `alerts` est aussi le journal d'exploitation — « qui a fait quoi ».
        # NULL = action de la MACHINE (boucle de surveillance, réconciliation, watchdog), et c'est
        # une information en soi : il ne faut donc jamais y mettre un acteur « par défaut ».
        # MESSAGE TRADUISIBLE (2026-08-21, signalé par Marine) : `message` était une phrase
        # FRANÇAISE déjà rendue — donc de la donnée, plus un libellé, et rien à traduire à
        # l'affichage. `msg_key` + `msg_params` (JSON) portent désormais la clé i18n et ses
        # paramètres ; le rendu est DIFFÉRÉ à la lecture (`i18n.rendre_alerte`), dans la langue
        # du lecteur — une alerte est écrite une fois et relue par N utilisateurs de langues
        # différentes. `message` reste rempli, en français, comme FORME CANONIQUE : c'est lui que
        # dédoublonne l'anti-rebond, que regroupe la home et que cherche `?q=`, et ces trois
        # usages ont besoin d'une représentation unique INDÉPENDANTE du lecteur.
        # Colonnes optionnelles, pas de reprise rétroactive (même décision que `kind`).
        _alcols = [r[1] for r in db.execute("PRAGMA table_info(alerts)")]
        for _c, _t in (("vmid", "INTEGER"), ("node_id", "INTEGER"), ("kind", "TEXT"),
                       ("user", "TEXT"), ("msg_key", "TEXT"), ("msg_params", "TEXT")):
            if _c not in _alcols:
                db.execute(f"ALTER TABLE alerts ADD COLUMN {_c} {_t}")
        # ÉPISODES D'ALERTE (2026-08-15) — mémoire de l'anti-rebond, cf. `_antirebond` plus bas.
        # Une alerte qui se répète n'apporte plus d'information à partir de la deuxième ligne ; elle
        # en RETIRE, en chassant du journal ce qui, lui, n'est arrivé qu'une fois. Mesuré le
        # 2026-08-15 : 10 068 lignes en base, dont ~80 % faites de quatre messages répétés.
        # La table porte l'état d'un épisode EN COURS (une signature = un symptôme dans un
        # contexte) ; le journal `alerts`, lui, ne reçoit plus que la TRANSITION et un résumé
        # périodique. Persistée à dessein : un redémarrage de l'orchestrateur ne doit pas
        # relibérer le flot d'un incident déjà annoncé.
        db.execute('''CREATE TABLE IF NOT EXISTS alert_episodes (
            signature    TEXT PRIMARY KEY,
            squelette    TEXT,
            kind         TEXT,
            vmid         INTEGER,
            node_id      INTEGER,
            niveau       TEXT,
            first_ts     TEXT,
            last_ts      TEXT,
            last_emit_ts TEXT,
            occurrences  INTEGER DEFAULT 1,
            muettes      INTEGER DEFAULT 0,
            last_message TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_alert_episodes_last ON alert_episodes(last_ts)")
        # Journal d'événements de la sonde 2110 (probe_2110, monitoring longue durée) : timeline
        # d'incidents par signal surveillé — silence, black, freeze, hors-norme vidéo/audio, pertes,
        # sortie du gabarit narrow→wide→failed, perte PTP. Alimenté par le moteur d'événements
        # (seuils sur les métriques :8080 de la sonde). Distinct des alertes (durable, filtrable
        # par vmid/flow/kind). vmid = sonde ; flow = nom/flowId du signal ; kind = type d'événement ;
        # severity info|warning|error ; value = mesure au déclenchement ; ts_start/ts_end = fenêtre.
        db.execute('''CREATE TABLE IF NOT EXISTS probe_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            vmid      INTEGER,
            flow      TEXT,
            kind      TEXT,
            severity  TEXT DEFAULT 'warning',
            message   TEXT,
            value     TEXT,
            ts_start  TEXT,
            ts_end    TEXT
        )''')
        # Profils CPU MESURÉS par étalonnage (app/etalonnage.py) : ce qu'un conteneur a réellement
        # coûté pendant que l'utilisateur exerçait son dispositif. Distinct de `resources.cores` du
        # manifeste, qui est une intention a priori — ici c'est un constat, et lui seul autorise le
        # mot « garanti ».
        #
        # ⚠ La clé est le COUPLE (signature, node_id), jamais le type seul. `signature` = type +
        # version de plugin + condensat des paramètres : changer les réglages PÉRIME la mesure au
        # lieu de la réutiliser en silence. Et une mesure ne voyage pas d'une machine à l'autre —
        # le même `avsync` coûte 40,8 % sur dl360-1 et 79,2 % sur r620-1 (facteur 1,94, mesuré).
        # `mesure` = JSON {n, min, median, p95, p99, max, moyenne} en % d'UN CPU.
        db.execute('''CREATE TABLE IF NOT EXISTS profils_cpu (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT,
            node_id   INTEGER,
            vmid      INTEGER,
            hostname  TEXT,
            mesure    TEXT,
            duree_s   REAL,
            projet    TEXT,
            note      TEXT,
            ts        TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_profils_cpu_cle ON profils_cpu(signature, node_id)")
        # Journal d'événements PTP persisté : trace durable des bascules (port SLAVE↔FAULTY,
        # grandmaster, lock, service) détectées par le sampler (app/ptp.py). Distinct des alertes
        # (filtrable par nœud/réseau) et des métriques (ptp_stats.json). Cf. db_add_ptp_event.
        db.execute('''CREATE TABLE IF NOT EXISTS ptp_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT,
            node_id      INTEGER,
            node_name    TEXT,
            network_id   INTEGER,
            network_name TEXT,
            ifname       TEXT,
            type         TEXT,
            detail       TEXT,
            level        TEXT DEFAULT 'info'
        )''')
        # NB : les migrations de RENOMMAGE DE TYPE (worker_udp→streamer, worker_2110_sender→
        # sender_2110, receiver→receiver_2110, ServeurStream→webrtc_gateway, multiview_free→
        # multiview, dve→split, receiver_2110_mtl→2110_io, + plugin_store dve→split et
        # cc_presets/dve_memories→plugin_store) ont été RETIRÉES en 2026-07 : plus aucune
        # ligne correspondante en DB, et plusieurs types cibles n'existent plus eux-mêmes.
        # Motif de référence si un futur renommage l'exige : voir l'historique git.
        # Migration de données : modèle de FLUX composables 2110 (« Option A »). Les containers
        # `2110_io` sans rx_flows/tx_flows reçoivent les listes DÉRIVÉES de leurs compteurs +
        # ratio audio/vidéo actuels (groupement audio/ANC→vidéo préservé). Idempotente : on ne
        # touche que les params sans la clé → un container déjà migré (ou édité) est laissé tel quel.
        from . import io2110_flows as _io2110_flows
        for vmid, dcraw in db.execute(
                "SELECT vmid, deploy_config FROM containers "
                "WHERE deploy_config LIKE '%2110_io%'").fetchall():
            try:
                dc = json.loads(dcraw) if dcraw else None
            except Exception:
                dc = None
            if not dc or dc.get("type") != "2110_io":
                continue
            p = dc.get("params") or {}
            changed = False
            if "rx_flows" not in p:
                p["rx_flows"] = _io2110_flows.derive_rx_flows(p); changed = True
            if "tx_flows" not in p:
                p["tx_flows"] = _io2110_flows.derive_tx_flows(p); changed = True
            if changed:
                dc["params"] = p
                db.execute("UPDATE containers SET deploy_config=? WHERE vmid=?",
                           (json.dumps(dc), vmid))
        db.commit()
        # Migration de données : FIGER le socle d'octaves des pyramides d'AVANT la clé
        # `base_octaves`. Idempotente (on ne touche que les params SANS la clé).
        #
        # POURQUOI. Le défaut DÉCLARÉ (`plugin.json`) vaut « none » — choix délibéré et argumenté :
        # en câblage à la demande, les octaves restent le plus souvent orphelins et gaspillent de
        # la bande passante mémoire. Mais le SCRIPT retombe sur « full » quand la clé est absente,
        # pour ne pas casser les pyramides antérieures. Les deux se défendent ; c'est leur
        # COEXISTENCE qui est un piège : le comportement dépend alors de si la clé a été
        # MATÉRIALISÉE ou non. Toute action qui matérialise les défauts — palette, réinitialisation,
        # fusion `effective_deploy_defaults` — fait basculer silencieusement une pyramide de
        # « full » à « none », ses octaves disparaissent, et TOUT consommateur qui lit `__p2`
        # tourne à vide.
        #
        # Vécu le 2026-08-11 : le mur de production a composé pendant ~6 minutes à 50 fps SANS
        # CONTENU NEUF (`fps_content` 0,0 pour seul témoin, `fps` nominal — cf. la note de mémoire
        # sur les alarmes qui doivent se comparer à l'INTENTION). Aucune erreur, aucun log côté
        # pyramide : elle faisait exactement ce qu'on venait de lui demander.
        #
        # On fige donc l'implicite : la pyramide garde son comportement, et il ne dépend plus de
        # la façon dont on la redéploie. Les pyramides CRÉÉES ENSUITE reçoivent « none » par le
        # défaut déclaré, comme voulu.
        _fige = 0
        for vmid, dcraw in db.execute(
                "SELECT vmid, deploy_config FROM containers "
                "WHERE deploy_config LIKE '%pyramide%'").fetchall():
            try:
                dc = json.loads(dcraw) if dcraw else None
            except Exception:
                continue
            if not dc or dc.get("type") != "pyramide":
                continue
            p = dc.get("params") or {}
            if "base_octaves" in p or p.get("levels"):
                continue                       # déjà explicite (ou piloté par `levels`) → intact
            p["base_octaves"] = "full"         # LE repli du script, désormais écrit noir sur blanc
            dc["params"] = p
            db.execute("UPDATE containers SET deploy_config=? WHERE vmid=?",
                       (json.dumps(dc), vmid))
            _fige += 1
        if _fige:
            log.info("migration pyramide : socle d'octaves figé à « full » sur %d container(s) "
                     "d'avant la clé base_octaves (le repli du script devient explicite)", _fige)
        db.commit()
        # Migration de données : purge la passerelle WebRTC (infra persistante) des
        # snapshots de projets sauvegardés AVANT son exclusion à la création. Idempotente.
        _EXCLUDED = {"webrtc_gateway", "storage"}
        for pid, snapraw in db.execute("SELECT id, snapshot FROM projects").fetchall():
            try:
                snap = json.loads(snapraw) if snapraw else []
            except Exception:
                continue
            if not isinstance(snap, list):
                continue
            cleaned = [c for c in snap
                       if (c.get("deploy_config") or {}).get("type") not in _EXCLUDED]
            if len(cleaned) != len(snap):
                db.execute("UPDATE projects SET snapshot=? WHERE id=?",
                           (json.dumps(cleaned), pid))
        db.commit()
        # Migration de données : PURGE du base64 des polices (`params.font_library`) des
        # `deploy_config` et des snapshots de projets. Le base64 (jusqu'à 8 Mo/conteneur) n'a
        # jamais eu à être PERSISTÉ : seules les références `lib:<sha16>` comptent en base, la
        # bibliothèque (static/uploads/fonts + table `fonts`) est la source de vérité, et le
        # base64 est ré-injecté à la volée à l'envoi vers le conteneur (deploy._gras).
        # Idempotente : ne touche que les configs qui portent encore la clé.
        _fl_freed = 0
        for vmid, dcraw in db.execute(
                "SELECT vmid, deploy_config FROM containers "
                "WHERE deploy_config LIKE '%font_library%'").fetchall():
            try:
                dc = json.loads(dcraw) if dcraw else None
            except Exception:
                continue
            p = (dc or {}).get("params")
            if not isinstance(p, dict) or "font_library" not in p:
                continue
            p.pop("font_library", None)
            dc["params"] = p
            new = json.dumps(dc)
            _fl_freed += max(0, len(dcraw) - len(new))
            db.execute("UPDATE containers SET deploy_config=? WHERE vmid=?", (new, vmid))
        for pid, snapraw in db.execute(
                "SELECT id, snapshot FROM projects WHERE snapshot LIKE '%font_library%'").fetchall():
            try:
                snap = json.loads(snapraw) if snapraw else None
            except Exception:
                continue
            if not isinstance(snap, list):
                continue
            hit = False
            for c in snap:
                p = ((c.get("deploy_config") or {}) if isinstance(c, dict) else {}).get("params")
                if isinstance(p, dict) and "font_library" in p:
                    p.pop("font_library", None)
                    hit = True
            if hit:
                new = json.dumps(snap)
                _fl_freed += max(0, len(snapraw) - len(new))
                db.execute("UPDATE projects SET snapshot=? WHERE id=?", (new, pid))
        if _fl_freed:
            log.info("migration polices : %d o de base64 purgés des deploy_config/snapshots",
                     _fl_freed)
        db.commit()
        # Migration de données : NUMÉROTATION 1-BASED (« le 0 n'existe pas », 2026-08-13).
        # Décale d'un cran, EN UNE PASSE, tout ce qui porte un numéro de slot ou de flux : clés
        # `tx{n}_shm`/`input_{n}`/`audio_shm_{n}`, noms de flux MXL référencés partout,
        # `rdma_links.src_flow`, `nmos_resources.bind_slot`, `nmos_subscriptions.recv_idx`.
        # Règle et helpers : app/numerotation.py. Idempotente par MARQUEUR en settings — et
        # SEULEMENT par lui : un décalage n'est pas détectable à l'œil sur les données (un `_1`
        # peut être un `_0` déjà migré ou un `_1` d'origine), donc un second passage sans le
        # marqueur re-décalerait tout.
        # ⚠ Indissociable du code : `bind_slot` migré ici est ce qui PRÉSERVE les UUID NMOS
        # (cf. `_registry_id`). Migrer l'un sans l'autre sème des ressources en double.
        try:
            from .migration_numerotation import migrer as _migrer_num
            _rap = _migrer_num(db, simulation=False)
            if not _rap.get("deja_faite") and (_rap.get("cles") or _rap.get("noms")):
                log.info("migration numérotation 1-based : %d clés, %d noms de flux, %d liens "
                         "RDMA, %d ressources NMOS, %d abonnements",
                         _rap["cles"], _rap["noms"], _rap["rdma"],
                         _rap["nmos_resources"], _rap["abonnements"])
        except Exception:
            # Une migration qui échoue à moitié est pire qu'une qui ne part pas : on trace et on
            # laisse la base en l'état (le marqueur n'est posé qu'en fin de `migrer`).
            log.exception("migration numérotation 1-based : ÉCHEC, base laissée inchangée")
        # Migration de données : `video_formats` étendu de 5 champs (Nom;L;H;FPS;Scan) à 8
        # (+Chroma;BitDepth;Colorimétrie) pour la réception ST 2110-20 broadcast. Idempotente :
        # n'ajoute les 3 champs qu'aux lignes qui en ont moins (défauts 422/10 ; UHD≥2160 → 2020).
        row = db.execute("SELECT value FROM settings WHERE key='video_formats'").fetchone()
        try:
            stored = json.loads(row["value"]) if row and row["value"] else None
        except Exception:
            stored = None
        if isinstance(stored, str) and stored:
            lines = stored.split("\n")
            changed = False
            out_lines = []
            for ln in lines:
                s = ln.strip()
                if not s:
                    out_lines.append(ln); continue
                parts = [p.strip() for p in s.split(";")]
                if len(parts) >= 8:
                    out_lines.append(ln); continue
                while len(parts) < 5:
                    parts.append("")
                try: _h = int(parts[2])
                except (TypeError, ValueError): _h = 0
                if len(parts) < 6: parts.append("422")
                if len(parts) < 7: parts.append("10")
                if len(parts) < 8: parts.append("2020" if _h >= 2160 else "709")
                out_lines.append(";".join(parts)); changed = True
            if changed:
                db.execute("UPDATE settings SET value=? WHERE key='video_formats'",
                           (json.dumps("\n".join(out_lines)),))
        db.commit()
        # Ajout idempotent du format SD-SDI PAL (720×576i25, 4:2:2/10/BT.601) à la liste stockée.
        row = db.execute("SELECT value FROM settings WHERE key='video_formats'").fetchone()
        try:
            stored = json.loads(row["value"]) if row and row["value"] else None
        except Exception:
            stored = None
        if isinstance(stored, str):
            labels = [ln.split(";")[0].strip().lower() for ln in stored.split("\n") if ln.strip()]
            if "sd-sdi pal" not in labels:
                sd = "SD-SDI PAL;720;576;50;i;422;10;601"
                stored = (stored + "\n" + sd) if stored.strip() else sd
                db.execute("UPDATE settings SET value=? WHERE key='video_formats'",
                           (json.dumps(stored),))
        db.commit()
        # ─── Correction des cadences ENTRELACÉES mal déclarées (2026-08-15) ────────────────────
        # La colonne FPS compte les CHAMPS pour l'entrelacé (« 1080i50 » → 50), convention établie
        # par `io2110_layouts.py` et `static/tx_models.js`. Deux lignes LIVRÉES la violaient :
        # « SD-SDI PAL » à 25 champs (= 12,5 trames/s, inexistant ; le PAL c'est 25 trames = 50
        # champs) et « HD 1080i59.94 » à 29,97 champs (= 14,985 trames/s au lieu de 29,97).
        # Ces valeurs alimentent le calcul de débit pixel et, depuis BCP-004-01, les capacités
        # annoncées de nos receivers — un contrôleur tiers y lisait une cadence qui n'existe pas.
        #
        # Correction CIBLÉE et idempotente : on ne touche qu'une ligne ENTRELACÉE dont le libellé
        # annonce lui-même une cadence (« …i59.94 ») différente de la colonne — l'intention est
        # alors écrite noir sur blanc, on ne devine rien. Plus le cas nommé « SD-SDI PAL », dont
        # le libellé ne porte pas de cadence, à 25 champs exactement. Un site qui a délibérément
        # saisi autre chose n'est PAS touché.
        row = db.execute("SELECT value FROM settings WHERE key='video_formats'").fetchone()
        try:
            stored = json.loads(row["value"]) if row and row["value"] else None
        except Exception:
            stored = None
        if isinstance(stored, str) and stored.strip():
            import re as _re
            lignes, change = [], False
            for ln in stored.split("\n"):
                p = [c.strip() for c in ln.split(";")]
                if len(p) >= 5 and p[4].lower() == "i":
                    try:
                        fps = float(p[3])
                    except (ValueError, IndexError):
                        fps = 0.0
                    voulu = None
                    m = _re.search(r"[ip](\d+(?:[.,]\d+)?)\s*$", p[0].strip(), _re.IGNORECASE)
                    if m:
                        voulu = float(m.group(1).replace(",", "."))
                    elif p[0].strip().lower() == "sd-sdi pal" and abs(fps - 25.0) < 0.001:
                        voulu = 50.0
                    if voulu is not None and abs(voulu - fps) > 0.01:
                        p[3] = ("%g" % voulu)
                        ln = ";".join(p)
                        change = True
                        log.info("video_formats : cadence entrelacée corrigée — « %s » %g → %g champs/s",
                                 p[0], fps, voulu)
                lignes.append(ln)
            if change:
                db.execute("UPDATE settings SET value=? WHERE key='video_formats'",
                           (json.dumps("\n".join(lignes)),))
        db.commit()
        # ─── TSL : connexions et sources ──────────────────────────────────────────
        # tsl_connections : un serveur TCP par connexion (multi-contrôleurs).
        # label_col  : colonne label (2-9) mise à jour par le texte TSL natif de cette connexion.
        # tally_base : premier niveau tally alloué (LH=base, RH=base+1, TT=base+2).
        db.execute('''CREATE TABLE IF NOT EXISTS tsl_connections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL DEFAULT '',
            port       INTEGER NOT NULL DEFAULT 12345,
            enabled    INTEGER NOT NULL DEFAULT 0,
            label_col  INTEGER NOT NULL DEFAULT 2,
            tally_base INTEGER NOT NULL DEFAULT 0,
            rouge_field TEXT   NOT NULL DEFAULT 'tt',
            vert_field  TEXT   NOT NULL DEFAULT 'lh'
        )''')
        # Migration : champs Rouge/Vert par connexion (= niveau de Tally). Défaut TT/LH.
        cols_tc = {r[1] for r in db.execute("PRAGMA table_info(tsl_connections)").fetchall()}
        if "rouge_field" not in cols_tc:
            db.execute("ALTER TABLE tsl_connections ADD COLUMN rouge_field TEXT NOT NULL DEFAULT 'tt'")
        if "vert_field" not in cols_tc:
            db.execute("ALTER TABLE tsl_connections ADD COLUMN vert_field TEXT NOT NULL DEFAULT 'lh'")
        # Chantier 5 : connexions SORTANTES (client TCP → UMD externe) + rattachement au
        # niveau de tally d'un projet (project_id posé → tally_base effectif = celui du projet).
        if "direction" not in cols_tc:
            db.execute("ALTER TABLE tsl_connections ADD COLUMN direction TEXT NOT NULL DEFAULT 'in'")
        if "dest_host" not in cols_tc:
            db.execute("ALTER TABLE tsl_connections ADD COLUMN dest_host TEXT DEFAULT ''")
        if "project_id" not in cols_tc:
            db.execute("ALTER TABLE tsl_connections ADD COLUMN project_id INTEGER "
                       "REFERENCES projects(id) ON DELETE SET NULL")
        # Chantier 5 : niveau de tally PAR PROJET, auto-alloué (base unique, pas de 3
        # sous-niveaux qui se chevauchent : bases espacées de 3 — LH/RH/TT).
        if "tally_base" not in pcols:
            db.execute("ALTER TABLE projects ADD COLUMN tally_base INTEGER")
        _rows = db.execute("SELECT id FROM projects WHERE tally_base IS NULL ORDER BY id").fetchall()
        for _r in _rows:
            db.execute("UPDATE projects SET tally_base=? WHERE id=?",
                       (_next_tally_base(db), _r[0]))
        # ★ DÉNOUEMENT DES NIVEAUX (2026-08-31). Tout ce qui précède reste pour les bases
        # anciennes ; la migration ci-dessous les aplatit en niveaux nommés numérotés 1..N.
        _migrer_niveaux_tally(db)
        # Puis la BASCULE SUR L'UUID : le numéro cesse d'être une identité pour
        # n'être plus qu'un rang d'affichage. Dans cet ordre — la première sème
        # les niveaux, la seconde leur donne leur identité et réécrit ce qui les cite.
        _migrer_identite_niveaux(db)
        _elaguer_niveaux_projets_dormants(db)
        _migrer_colonnes_libelles(db)
        # tsl_sources : table legacy (conservée, non utilisée pour les nouvelles données).
        db.execute('''CREATE TABLE IF NOT EXISTS tsl_sources (
            tsl_index  INTEGER PRIMARY KEY,
            linked_shm TEXT DEFAULT '',
            projet     TEXT DEFAULT '',
            label_0    TEXT DEFAULT '',
            label_1    TEXT DEFAULT '',
            label_2    TEXT DEFAULT '',
            label_3    TEXT DEFAULT '',
            label_4    TEXT DEFAULT '',
            label_5    TEXT DEFAULT '',
            label_6    TEXT DEFAULT '',
            label_7    TEXT DEFAULT '',
            label_8    TEXT DEFAULT '',
            label_9    TEXT DEFAULT ''
        )''')
        try:
            db.execute("ALTER TABLE tsl_sources ADD COLUMN projet TEXT DEFAULT ''")
        except Exception:
            pass

        # source_labels : métadonnées des sources, keyed par shm.
        # Remplace tsl_sources comme table active pour les labels et projet.
        db.execute('''CREATE TABLE IF NOT EXISTS source_labels (
            shm        TEXT PRIMARY KEY,
            projet     TEXT DEFAULT '',
            label_2    TEXT DEFAULT '',
            label_3    TEXT DEFAULT '',
            label_4    TEXT DEFAULT '',
            label_5    TEXT DEFAULT '',
            label_6    TEXT DEFAULT '',
            label_7    TEXT DEFAULT '',
            label_8    TEXT DEFAULT '',
            label_9    TEXT DEFAULT '',
            parent_shm TEXT DEFAULT NULL
        )''')
        cols_sl = {r[1] for r in db.execute("PRAGMA table_info(source_labels)").fetchall()}
        if "parent_shm" not in cols_sl:
            db.execute("ALTER TABLE source_labels ADD COLUMN parent_shm TEXT DEFAULT NULL")

        # tsl_mapping : mapping (connection_id, tsl_index) → source_shm.
        # Indépendant par connexion : Connection A index 5 ≠ Connection B index 5.
        db.execute('''CREATE TABLE IF NOT EXISTS tsl_mapping (
            connection_id INTEGER NOT NULL,
            tsl_index     INTEGER NOT NULL,
            source_shm    TEXT    DEFAULT '',
            PRIMARY KEY (connection_id, tsl_index)
        )''')

        # ── IS-07 entrant : LE MÊME MODÈLE QUE TSL, et c'est le point ────────────────────────
        # Une connexion = un protocole qui vient écrire des tally dans UN niveau, plus une table
        # qui dit « telle adresse de l'émetteur = tel signal chez nous ». Chez TSL cette adresse
        # est un index de trame ; en IS-07 c'est l'UUID d'une Source. Rien d'autre ne change, et
        # c'est pour ça que l'affectation vit au même endroit : la page Labels.
        #
        # ⚠ UN RECEIVER PAR CONNEXION, PAS PAR SORTIE. On publiait un Receiver par groupe de
        # sortie BCP-002-01 — 99 sur le banc pour 6 utiles. C'est la lecture littérale de la BCP,
        # mais elle répond à la mauvaise question : ce qu'on choisit, ce n'est pas « quelles
        # sorties peuvent recevoir un tally », c'est « quel protocole écrit dans quel niveau ».
        db.execute('''CREATE TABLE IF NOT EXISTS is07_connections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL DEFAULT '',
            enabled    INTEGER NOT NULL DEFAULT 0,
            level_uuid TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS is07_mapping (
            connection_id INTEGER NOT NULL,
            source_id     TEXT    NOT NULL,
            source_shm    TEXT    DEFAULT '',
            PRIMARY KEY (connection_id, source_id)
        )''')

        # Migration : si ancienne config TSL activée → créer une connexion par défaut.
        tsl_enabled = db.execute("SELECT value FROM settings WHERE key='tsl_enabled'").fetchone()
        if tsl_enabled and json.loads(tsl_enabled["value"] or "false"):
            if not db.execute("SELECT 1 FROM tsl_connections LIMIT 1").fetchone():
                port_row = db.execute("SELECT value FROM settings WHERE key='tsl_port'").fetchone()
                old_port = int(json.loads(port_row["value"] or "12345")) if port_row else 12345
                db.execute(
                    "INSERT INTO tsl_connections (name, port, enabled, label_col, tally_base) "
                    "VALUES (?, ?, 1, 2, 0)",
                    ("Connexion par défaut", old_port))

        # Migration tsl_sources → source_labels + tsl_mapping (une seule fois).
        has_old = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tsl_sources'"
        ).fetchone()
        if has_old:
            src_count  = db.execute("SELECT COUNT(*) FROM tsl_sources").fetchone()[0]
            dest_count = db.execute("SELECT COUNT(*) FROM source_labels").fetchone()[0]
            if src_count > 0 and dest_count == 0:
                first_conn = db.execute(
                    "SELECT id FROM tsl_connections ORDER BY id LIMIT 1").fetchone()
                first_cid = first_conn["id"] if first_conn else None
                for row in db.execute("SELECT * FROM tsl_sources").fetchall():
                    shm = row["linked_shm"] or ""
                    if not shm:
                        continue
                    db.execute(
                        '''INSERT OR IGNORE INTO source_labels
                           (shm, projet, label_2, label_3, label_4, label_5,
                            label_6, label_7, label_8, label_9)
                           VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (shm, row["projet"] or "",
                         row["label_2"] or "", row["label_3"] or "",
                         row["label_4"] or "", row["label_5"] or "",
                         row["label_6"] or "", row["label_7"] or "",
                         row["label_8"] or "", row["label_9"] or ""))
                    if first_cid is not None:
                        db.execute(
                            '''INSERT OR IGNORE INTO tsl_mapping
                               (connection_id, tsl_index, source_shm)
                               VALUES (?,?,?)''',
                            (first_cid, row["tsl_index"], shm))

        db.commit()

        # Première installation : AUCUN utilisateur seedé par défaut (plus de couple
        # admin/bobistudio en dur — risque de sécurité). Tant qu'aucun utilisateur
        # n'existe, l'app redirige vers l'assistant de premier démarrage (/setup,
        # cf. routes.py) qui invite à créer le compte administrateur.

        # Migration : params booléens stockés en chaîne ("True"/"False") par un
        # ancien coerce_config (type:"bool" non géré → str()). bool("False")==True
        # côté script → fonctionnalité activée à tort (cf. PROD-011). On recast en
        # vrai booléen les clés connues comme booléennes.
        _BOOL_KEYS = ("smpte_2022_7", "tsl_remote", "hot_input", "overlay_enabled",
                      "overlay_below", "show_brand", "loop", "pl_auto", "pl_loop")
        for vmid, dcraw in db.execute(
                "SELECT vmid, deploy_config FROM containers WHERE deploy_config IS NOT NULL").fetchall():
            try:
                dc = json.loads(dcraw) if dcraw else None
            except Exception:
                continue
            params = (dc or {}).get("params")
            if not isinstance(params, dict):
                continue
            changed = False
            for k in _BOOL_KEYS:
                v = params.get(k)
                if isinstance(v, str):
                    params[k] = v.strip().lower() in ("1", "true", "yes", "on")
                    changed = True
            if changed:
                db.execute("UPDATE containers SET deploy_config=? WHERE vmid=?",
                           (json.dumps(dc), vmid))
        db.commit()


# ─── Stockage générique par plugin (plugin_store) ────────────
# Remplace cc_presets / dve_memories : tout plugin peut persister des entrées JSON
# nommées, scopées globalement (scope='') ou par container (scope=str(vmid)).
def _ps_row(r):
    return {"id": r["id"], "name": r["name"], "value": json.loads(r["value"]),
            "scope": r["scope"], "created_at": r["created_at"], "updated_at": r["updated_at"]}

def plugin_store_list(type_, scope=""):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM plugin_store WHERE type=? AND scope=? ORDER BY name",
            (type_, str(scope or ""))).fetchall()
    return [_ps_row(r) for r in rows]

def plugin_store_get(id_):
    with get_db() as db:
        r = db.execute("SELECT * FROM plugin_store WHERE id=?", (int(id_),)).fetchone()
    return _ps_row(r) | {"type": r["type"]} if r else None

def plugin_store_list_scope(scope):
    """Toutes les entrées (tous types confondus) d'un scope donné — utilisé pour
    embarquer les mémoires par-container (DVE, multiview, presets…) dans un projet."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM plugin_store WHERE scope=? ORDER BY type, name",
            (str(scope or ""),)).fetchall()
    return [_ps_row(r) | {"type": r["type"]} for r in rows]

def plugin_store_create(type_, scope, name, value, unique_name=False):
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    scope = str(scope or "")
    with get_db() as db:
        if unique_name:
            ex = db.execute("SELECT 1 FROM plugin_store WHERE type=? AND scope=? AND name=?",
                            (type_, scope, name)).fetchone()
            if ex:
                return None
        cur = db.execute(
            "INSERT INTO plugin_store (type, scope, name, value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (type_, scope, name, json.dumps(value if value is not None else {}),
             datetime.now().isoformat(timespec="seconds"),
             datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def plugin_store_update(id_, name=None, value=None):
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name.strip())
    if value is not None:
        sets.append("value=?"); vals.append(json.dumps(value))
    if not sets:
        return False
    sets.append("updated_at=?"); vals.append(datetime.now().isoformat(timespec="seconds"))
    vals.append(int(id_))
    with get_db() as db:
        cur = db.execute(f"UPDATE plugin_store SET {', '.join(sets)} WHERE id=?", vals)
        db.commit()
        return cur.rowcount > 0

def plugin_store_delete(id_):
    with get_db() as db:
        cur = db.execute("DELETE FROM plugin_store WHERE id=?", (int(id_),))
        db.commit()
        return cur.rowcount > 0


# ── Token d'agent par-conteneur (:8081) ───────────────────────────────────────
# La colonne `containers.agent_token` porte un SECRET. Les deux getters de conteneur font
# `SELECT *` et leurs lignes finissent en JSON dans /api/containers, dans les snapshots projet et
# dans les sauvegardes exportables : on RETIRE donc la colonne des dicts rendus et on la remplace
# par le seul fait observable dont l'exploitation a besoin — `agent_auth` : "stored" (token propre
# au conteneur) ou "derived" (conteneur d'avant la migration, token encore dérivé du
# `flask_secret_key`). C'est la TRAÇABILITÉ de la migration : tant qu'un "derived" subsiste, le
# secret racine n'est pas rotable. Le secret lui-même ne se lit QUE par `db_get_agent_token`.
def _container_sans_secret(row):
    d = dict(row)
    d["agent_auth"] = "stored" if (d.pop("agent_token", None) or "") else "derived"
    return d



def cache_requete(fn):
    """Mémoïse un helper de LECTURE pour la durée d'UNE requête HTTP GET.

    ★ POURQUOI. `/api/home/summary` appelait `db_get_projects` deux fois,
    `db_get_nodes` et `db_fabric_all` trois fois — dans la MÊME requête. Ce n'est
    pas une négligence : ces helpers sont appelés par des fonctions qui, prises
    isolément, ont raison de les demander. Le doublon n'existe qu'à l'échelle de
    la requête, donc c'est à cette échelle qu'on le supprime, sans toucher aux
    appelants.

    ⚠ SUR LES GET SEULEMENT. Une requête qui ÉCRIT puis relit doit voir son
    écriture : mémoïser sur un POST servirait l'état d'avant, et le bug serait
    invisible — la réponse aurait juste un tour de retard. Un GET n'a pas le
    droit de muter, donc la mémoïsation y est sûre par construction.

    ⚠ ET SEULEMENT SANS ARGUMENT. Une clé de cache bâtie sur des arguments
    arbitraires (dict, listes) est une source de faux partages ; ces helpers-là
    n'en prennent pas, ou pas dans les appels qui font doublon.

    Hors contexte de requête (threads de fond, samplers, services), la fonction
    est appelée normalement : aucun cache, aucun changement de comportement."""
    import functools

    @functools.wraps(fn)
    def _enveloppe(*a, **kw):
        if a or kw:
            return fn(*a, **kw)
        try:
            from flask import g, has_request_context, request
            if not has_request_context() or request.method != "GET":
                return fn()
            cache = getattr(g, "_cache_db", None)
            if cache is None:
                cache = {}
                g._cache_db = cache
            cle = fn.__name__
            if cle not in cache:
                cache[cle] = fn()
            return cache[cle]
        except Exception:
            return fn()

    return _enveloppe


@cache_requete
def db_get_containers():
    with get_db() as db:
        return [_container_sans_secret(r) for r in
                db.execute("SELECT * FROM containers ORDER BY vmid").fetchall()]

def db_get_container(vmid):
    with get_db() as db:
        row = db.execute("SELECT * FROM containers WHERE vmid=?", (vmid,)).fetchone()
        return _container_sans_secret(row) if row else None


def db_get_agent_token(vmid):
    """Token d'agent STOCKÉ de ce conteneur, ou None. Seul point de lecture du secret."""
    if vmid is None:
        return None
    try:
        with get_db() as db:
            row = db.execute("SELECT agent_token FROM containers WHERE vmid=?",
                             (vmid,)).fetchone()
        return (row["agent_token"] or None) if row else None
    except Exception as e:      # colonne absente (DB antérieure à la migration) → dérivé
        log.warning("db_get_agent_token %s: %s", vmid, e)
        return None


def db_ensure_agent_token(vmid, longueur=32):
    """Token d'agent de ce conteneur, GÉNÉRÉ (aléatoire) et persisté s'il n'en a pas encore.
    Appelé par les drivers au moment de construire le `docker run` — c'est-à-dire exactement au
    moment où le token devient effectif DANS le conteneur (injecté en MXL_AGENT_TOKEN). Jamais
    appelé sur le chemin de lecture : un token stocké mais jamais injecté rendrait le conteneur
    injoignable. L'écriture est conditionnelle (`WHERE agent_token IS NULL OR ''`) puis on RELIT :
    deux threads concurrents rendent forcément la même valeur (le perdant lit celle du gagnant)."""
    if vmid is None:
        return None
    import secrets as _secrets
    tok = db_get_agent_token(vmid)
    if tok:
        return tok
    candidat = _secrets.token_urlsafe(longueur)
    with get_db() as db:
        db.execute("UPDATE containers SET agent_token=? WHERE vmid=? "
                   "AND (agent_token IS NULL OR agent_token='')", (candidat, vmid))
        db.commit()
    return db_get_agent_token(vmid)


def db_clear_agent_token(vmid):
    """Oublie le token stocké (retour au token dérivé). Utilisé par l'échappatoire : quand
    l'injection est désactivée, le conteneur recréé n'a plus de MXL_AGENT_TOKEN — garder un token
    stocké ferait envoyer un en-tête que l'agent n'attend plus (inoffensif) mais ferait surtout
    MENTIR l'état de migration (`agent_auth=stored` pour un agent ouvert)."""
    if vmid is None:
        return
    try:
        with get_db() as db:
            db.execute("UPDATE containers SET agent_token=NULL WHERE vmid=?", (vmid,))
            db.commit()
    except Exception as e:
        log.warning("db_clear_agent_token %s: %s", vmid, e)


def db_agent_token_etat():
    """État de la migration des tokens d'agent : {stored, derived, vmids_derives}.
    `derived` > 0 ⇒ des conteneurs s'authentifient encore avec un token dérivé du
    `flask_secret_key` ⇒ cette clé N'EST PAS rotable (la faire tourner les rendrait
    impilotables). La flotte est migrée quand `derived` vaut 0."""
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT vmid, hostname, COALESCE(agent_token,'') AS t FROM containers "
                "ORDER BY vmid").fetchall()
    except Exception as e:
        log.warning("db_agent_token_etat: %s", e)
        return {"stored": 0, "derived": 0, "vmids_derives": [], "erreur": str(e)}
    derives = [{"vmid": r["vmid"], "hostname": r["hostname"]} for r in rows if not r["t"]]
    return {"stored": len(rows) - len(derives), "derived": len(derives),
            "vmids_derives": derives,
            "rotable": not derives}

def db_upsert_container(vmid, hostname, cores=2, memory=2048,
                         script=None, status="unknown", restarts=0, instance_uuid=None):
    import uuid as _uuid
    iu = instance_uuid or str(_uuid.uuid4())   # généré à l'INSERT ; COALESCE → jamais écrasé en UPDATE
    with get_db() as db:
        db.execute('''INSERT INTO containers
            (vmid, hostname, cores, memory, script, status, restarts, created_at, instance_uuid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vmid) DO UPDATE SET
                hostname=excluded.hostname,
                cores=excluded.cores,
                memory=excluded.memory,
                script=excluded.script,
                status=excluded.status,
                instance_uuid=COALESCE(containers.instance_uuid, excluded.instance_uuid)''',
            (vmid, hostname, cores, memory, script, status,
             restarts, datetime.now().isoformat(), iu))
        db.commit()

def db_update_container_image(vmid, image):
    """Tag de l'image RÉELLEMENT posée au `docker run`.

    NULL reste possible : un conteneur créé avant cette colonne n'a rien à déclarer, et
    « je ne sais pas » doit rester distinct de « à jour ». Les consommateurs doivent traiter
    les deux différemment — supposer l'un ou l'autre est exactement la faute qu'on répare."""
    with get_db() as db:
        db.execute("UPDATE containers SET image=? WHERE vmid=?", (image, vmid))
        db.commit()


def db_script_enabled(vmid):
    """INTENTION d'exploitation du script d'un conteneur (défaut True).

    À ne pas confondre avec son état observé (`:8081/status.running`) : l'intention dit ce qu'on
    VEUT, l'état ce qui EST. C'est la distinction qui permet de faire la différence entre « arrêté
    parce qu'on l'a demandé » et « arrêté parce qu'il est tombé »."""
    with get_db() as db:
        r = db.execute("SELECT script_enabled FROM containers WHERE vmid=?", (vmid,)).fetchone()
    return True if r is None or r["script_enabled"] is None else bool(r["script_enabled"])


def db_set_script_enabled(vmid, actif):
    with get_db() as db:
        db.execute("UPDATE containers SET script_enabled=? WHERE vmid=?",
                   (1 if actif else 0, vmid))


def db_update_status(vmid, status, cause=None):
    """Statut du conteneur, et la CAUSE quand elle est connue.

    `cause=None` laisse la valeur existante (un rafraîchissement de statut ne doit pas effacer une
    cause déjà établie) ; `cause=""` l'efface explicitement — c'est ce que fait un retour à la
    normale, où garder l'ancienne cause afficherait une panne résolue comme si elle durait.
    """
    with get_db() as db:
        if cause is None:
            db.execute("UPDATE containers SET status=? WHERE vmid=?", (status, vmid))
        else:
            db.execute("UPDATE containers SET status=?, status_cause=? WHERE vmid=?",
                       (status, (cause or None), vmid))
        db.commit()

def db_set_desired_state(vmid, state):
    """État VOULU ('running'/'stopped') — écrit par deploy/start/stop/redemarrer uniquement
    (jamais par la boucle surveillance, qui n'écrit que `status`, l'état OBSERVÉ)."""
    if state not in ("running", "stopped"):
        return
    with get_db() as db:
        db.execute("UPDATE containers SET desired_state=? WHERE vmid=?", (state, vmid))
        db.commit()

def db_set_node_boot(node_id, last_boot_ts=None, recovered_boot_ts=None):
    """Persiste les jalons de boot d'un nœud (auto-recovery). Ne touche que les champs fournis."""
    sets, args = [], []
    if last_boot_ts is not None:
        sets.append("last_boot_ts=?"); args.append(float(last_boot_ts))
    if recovered_boot_ts is not None:
        sets.append("recovered_boot_ts=?"); args.append(float(recovered_boot_ts))
    if not sets:
        return
    args.append(node_id)
    with get_db() as db:
        db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", args)
        db.commit()

def db_update_spec_sig(vmid, sig):
    """Signature de la spec `docker run` du conteneur actuellement EN MARCHE (cf.
    docker_compute._signature_spec). Écrite APRÈS un run réussi ; remise à None si le run
    échoue (état inconnu → le prochain déploiement doit recréer). Un conteneur simplement
    ARRÊTÉ garde sa signature : c'est son statut docker (≠ running) qui impose la recréation."""
    with get_db() as db:
        db.execute("UPDATE containers SET runtime_spec_sig=? WHERE vmid=?", (sig, vmid))
        db.commit()

def db_update_docker_ip(vmid, ip):
    """IP propre du conteneur Docker « compute » (macvlan), lue après `docker run`."""
    with get_db() as db:
        db.execute("UPDATE containers SET docker_ip=? WHERE vmid=?", (ip, vmid))
        db.commit()
    # containers.docker_ip fait désormais autorité pour cette IP → la réservation transitoire
    # (ip_reservations) est redondante : on la libère (sans elle, une IP de container mort resterait
    # bloquée). NB : on ne libère QUE cette IP-là, pas les autres réservations du vmid.
    if ip:
        db_release_ip_reservation(ip)

def db_update_ip(vmid, ip):
    with get_db() as db:
        db.execute("UPDATE containers SET ip=? WHERE vmid=?", (ip, vmid))
        db.commit()

def db_set_instance_uuid(vmid, instance_uuid):
    """Force l'instance_uuid d'un conteneur (restore en mode DÉPLACEMENT : on conserve l'identité du
    snapshot au lieu du nouvel uuid généré à la création). En COPIE, on n'appelle PAS ceci."""
    with get_db() as db:
        db.execute("UPDATE containers SET instance_uuid=? WHERE vmid=?", (instance_uuid, vmid))
        db.commit()

# ─── Emplacements (rôles) : identité fonctionnelle, stable au remplacement ──────────────
# Cf. le commentaire de la table `production_roles` dans init_db pour le POURQUOI. Ici : le CRUD + la
# résolution emplacement → conteneur servant, et le semage automatique au premier déploiement.

def _role_row(r):
    return dict(r) if r is not None else None

def db_roles_list():
    """Tous les emplacements par numéro croissant (l'ordre de l'arbre Ember+)."""
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM production_roles ORDER BY num").fetchall()]

def db_role_get(num):
    with get_db() as db:
        return _role_row(db.execute("SELECT * FROM production_roles WHERE num=?", (num,)).fetchone())

def db_role_get_by_key(key):
    with get_db() as db:
        return _role_row(db.execute("SELECT * FROM production_roles WHERE key=?", (key,)).fetchone())

def db_role_for_instance(instance_uuid):
    """L'emplacement servi par ce conteneur, ou None."""
    if not instance_uuid:
        return None
    with get_db() as db:
        return _role_row(db.execute("SELECT * FROM production_roles WHERE instance_uuid=?",
                                    (instance_uuid,)).fetchone())

def db_role_slugify(label, taken=None):
    """Slug de clé d'emplacement : minuscules, [a-z0-9_], unique (suffixe _2, _3…).
    La clé est IMMUABLE une fois créée (c'est l'identifier vu par les contrôleurs externes),
    donc on la calcule une seule fois, à la création.
    ASCII strict : accents dépliés (« RÉGIE » → `regie`). Un identifier Ember+ non-ASCII passe
    mal chez plusieurs consommateurs — ce n'est pas la place où prendre ce risque."""
    import unicodedata
    label = unicodedata.normalize("NFKD", label or "")
    label = "".join(c for c in label if not unicodedata.combining(c))
    base = "".join(ch if ch.isalnum() and ch.isascii() else "_" for ch in label.strip().lower())
    base = "_".join(p for p in base.split("_") if p) or "emplacement"
    if base[0].isdigit():          # un identifier Ember+ ne commence pas par un chiffre
        base = f"e_{base}"
    if taken is None:
        with get_db() as db:
            taken = {r[0] for r in db.execute("SELECT key FROM production_roles").fetchall()}
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key

def db_role_create(label, expect_type=None, instance_uuid=None, key=None):
    """Crée un emplacement et retourne sa ligne. `key` figée ici (slug du libellé si absente).
    Lève ValueError si la clé est déjà prise ou si le conteneur sert déjà un emplacement."""
    label = (label or "").strip()
    if not label:
        raise ValueError("libellé vide")
    key = (key or "").strip().lower() or db_role_slugify(label)
    if db_role_get_by_key(key):
        raise ValueError(f"clé « {key} » déjà utilisée")
    if instance_uuid and db_role_for_instance(instance_uuid):
        raise ValueError("ce conteneur sert déjà un emplacement")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO production_roles (key, label, expect_type, instance_uuid, created_at) "
            "VALUES (?,?,?,?,?)",
            (key, label, expect_type or None, instance_uuid or None, datetime.now().isoformat()))
        db.commit()
        num = cur.lastrowid
    return db_role_get(num)

def db_role_set(num, label=None, expect_type=None):
    """Renomme / re-type un emplacement. La CLÉ et le NUMÉRO ne changent jamais."""
    sets, vals = [], []
    if label is not None:
        if not str(label).strip():
            raise ValueError("libellé vide")
        sets.append("label=?"); vals.append(str(label).strip())
    if expect_type is not None:
        sets.append("expect_type=?"); vals.append(expect_type or None)
    if not sets:
        return db_role_get(num)
    with get_db() as db:
        db.execute(f"UPDATE production_roles SET {', '.join(sets)} WHERE num=?", vals + [num])
        db.commit()
    return db_role_get(num)

def db_role_bind(num, instance_uuid):
    """Affecte un conteneur à l'emplacement (None = délier → emplacement hors ligne).
    Un conteneur ne servant qu'un emplacement, on le DÉLIE d'abord de son ancien : la
    réaffectation est le geste normal (remplacement de machine), elle ne doit pas échouer."""
    with get_db() as db:
        if instance_uuid:
            db.execute("UPDATE production_roles SET instance_uuid=NULL WHERE instance_uuid=? AND num<>?",
                       (instance_uuid, num))
        db.execute("UPDATE production_roles SET instance_uuid=? WHERE num=?", (instance_uuid or None, num))
        db.commit()
    return db_role_get(num)

def db_role_delete(num):
    with get_db() as db:
        cur = db.execute("DELETE FROM production_roles WHERE num=?", (num,))
        db.commit()
        return cur.rowcount > 0

def db_role_container(num):
    """Le conteneur servant l'emplacement, ou None (emplacement hors ligne)."""
    r = db_role_get(num)
    if not r or not r.get("instance_uuid"):
        return None
    return db_container_by_instance(r["instance_uuid"])

def db_container_by_instance(instance_uuid):
    if not instance_uuid:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM containers WHERE instance_uuid=?",
                         (instance_uuid,)).fetchone()
    return dict(row) if row else None

def db_roles_with_containers():
    """[(role, container|None)] pour tous les emplacements — une seule passe DB
    (l'arbre Ember+ est reconstruit à chaque GetDirectory, donc ce chemin est chaud)."""
    roles = db_roles_list()
    if not roles:
        return []
    with get_db() as db:
        by_uuid = {r["instance_uuid"]: dict(r)
                   for r in db.execute("SELECT * FROM containers "
                                       "WHERE instance_uuid IS NOT NULL").fetchall()}
    return [(r, by_uuid.get(r.get("instance_uuid"))) for r in roles]

def db_update_fps(vmid, fps):
    with get_db() as db:
        db.execute("UPDATE containers SET fps=? WHERE vmid=?", (fps, vmid))
        db.commit()

def db_update_usage(vmid, cpu_percent, mem_used, cpu_count=None):
    """Met à jour les métriques d'utilisation système. `cpu_percent` en 0..100, `mem_used` en octets.

    ⚠ `cpu_percent` est un pourcentage **des CPU alloués au conteneur**, pas d'un cœur : l'agent
    par-conteneur divise par `n_cpus` puis ÉCRÊTE à 100 (`script_templates/agent.py:/stats`). Deux
    conséquences qu'il faut connaître avant de s'en servir pour dimensionner :
      - il n'est PAS comparable entre deux conteneurs de cpuset différents (57 % sur 16 CPU = 9
        cœurs ; 57 % sur 2 CPU = 1 cœur) ;
      - à 100 % il est un PLANCHER, pas une mesure : l'écrêtage rend invisible un conteneur qui
        réclamerait le double de ce qu'il a.
    D'où `cpu_count` : conservé tel quel pour que le coût ABSOLU (`cpu_percent × cpu_count`) reste
    calculable — cf. `app/cpu_profiles.py`. Sans lui, la seule métrique de coût du projet n'a pas
    d'unité exploitable."""
    with get_db() as db:
        if cpu_count is None:
            db.execute("UPDATE containers SET cpu_percent=?, mem_used=? WHERE vmid=?",
                       (cpu_percent, mem_used, vmid))
        else:
            db.execute("UPDATE containers SET cpu_percent=?, mem_used=?, cpu_count=? WHERE vmid=?",
                       (cpu_percent, mem_used, int(cpu_count), vmid))
        db.commit()

def db_update_resources(vmid, cores=None, memory=None, pinned_cores=None):
    """Met à jour cores/memory/pinned_cores. Champs None ignorés (pas écrasés)."""
    sets, args = [], []
    if cores is not None:
        sets.append("cores=?"); args.append(int(cores))
    if memory is not None:
        sets.append("memory=?"); args.append(int(memory))
    if pinned_cores is not None:  # "" vidé explicitement → NULL
        sets.append("pinned_cores=?"); args.append(pinned_cores or None)
    if not sets:
        return
    args.append(vmid)
    with get_db() as db:
        db.execute(f"UPDATE containers SET {', '.join(sets)} WHERE vmid=?", args)
        db.commit()

def db_update_script(vmid, script):
    with get_db() as db:
        db.execute("UPDATE containers SET script=? WHERE vmid=?", (script, vmid))
        db.commit()

def db_increment_restarts(vmid):
    with get_db() as db:
        db.execute("UPDATE containers SET restarts=restarts+1 WHERE vmid=?", (vmid,))
        db.commit()

def db_delete_container(vmid):
    with get_db() as db:
        db.execute("DELETE FROM containers WHERE vmid=?", (vmid,))
        db.commit()
    # Libère toute réservation d'IP encore détenue par ce vmid (cas : container détruit avant que
    # son docker_ip n'ait été persisté → sinon l'IP resterait bloquée pour toujours). Le vmid, lui,
    # reste réservé (allocation MONOTONE : on ne réutilise jamais un numéro).
    db_release_ip_reservations_for_vmid(vmid)

def db_upsert_container_docker(vmid, hostname, node_id, docker_name,
                              cores=1, memory=2048, status="created", instance_uuid=None,
                              image=None):
    """Insère/maj une ligne container rattachée à un nœud : elle référence un conteneur Docker
    (docker_name) sur node_id.

    `image` = tag RÉELLEMENT posé au `docker run`. C'est la seule façon de savoir plus tard sur
    quoi un conteneur tourne : l'image du NŒUD peut avoir été promue depuis. Préservée en
    UPDATE quand l'appelant ne la donne pas — un simple changement de statut ne doit pas
    l'effacer."""
    import uuid as _uuid
    iu = instance_uuid or str(_uuid.uuid4())   # généré à l'INSERT ; COALESCE → préservé en UPDATE
    with get_db() as db:
        db.execute('''INSERT INTO containers
            (vmid, hostname, cores, memory, status, restarts, created_at,
             node_id, docker_name, instance_uuid, image)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(vmid) DO UPDATE SET
                hostname=excluded.hostname,
                status=excluded.status,
                node_id=excluded.node_id,
                docker_name=excluded.docker_name,
                instance_uuid=COALESCE(containers.instance_uuid, excluded.instance_uuid),
                image=COALESCE(excluded.image, containers.image)''',
            (vmid, hostname, cores, memory, status, datetime.now().isoformat(),
             node_id, docker_name, iu, image))
        db.commit()

# ─── Nœuds (cluster multi-hôte) ──────────────────────────────────────
@cache_requete
def db_get_nodes():
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM nodes ORDER BY id").fetchall()]

def db_get_node(node_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(row) if row else None

def db_node_mxl_domain_id(node_id):
    """Identité du domaine MXL d'un nœud (BCP-007-03, `domain_def.json:id`) — la CRÉE au premier
    appel et la persiste. Renvoie un UUID canonique minuscule, ou None si le nœud n'existe pas.

    STABLE À VIE : ce numéro est ce à quoi un contrôleur tiers (et, demain, le paramètre IS-05
    `mxl_domain_id`) reconnaît le domaine — le renommer reviendrait à prétendre que c'est un autre
    bus. Il ne dépend donc NI du nom du nœud, NI du chemin de montage : uuid4 tiré une fois.

    La création est atomique (UPDATE … WHERE mxl_domain_id IS NULL puis relecture) : deux threads
    qui l'appellent en même temps — sampler de santé et prép hôte, par exemple — obtiennent le
    même UUID, celui du gagnant, jamais deux identités pour un seul domaine."""
    with get_db() as db:
        row = db.execute("SELECT mxl_domain_id FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            return None
        if row["mxl_domain_id"]:
            return row["mxl_domain_id"]
        db.execute("UPDATE nodes SET mxl_domain_id=? WHERE id=? AND "
                   "(mxl_domain_id IS NULL OR mxl_domain_id='')",
                   (str(uuid.uuid4()), node_id))
        row = db.execute("SELECT mxl_domain_id FROM nodes WHERE id=?", (node_id,)).fetchone()
        return row["mxl_domain_id"] if row else None


def db_get_node_by_host(host):
    with get_db() as db:
        row = db.execute("SELECT * FROM nodes WHERE host=?", (host,)).fetchone()
        return dict(row) if row else None

def db_get_node_by_enroll_token(token):
    if not token:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM nodes WHERE enroll_token=?", (token,)).fetchone()
        return dict(row) if row else None

def db_add_node(name, host, kind="docker", mtl_iface=None, mtl_capable=0,
               lcores=None, image=None, mxl_mount="/dev/shm", ram_mb=None,
               docker_network=None, compute_image=None, compute_cpuset=None,
               media_image=None, media_mount=None):
    with get_db() as db:
        cur = db.execute('''INSERT INTO nodes
            (name, kind, host, mtl_iface, mtl_capable, lcores, image, mxl_mount,
             ram_mb, docker_network, compute_image, compute_cpuset, media_image, media_mount,
             status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)''',
            (name, kind, host, mtl_iface, int(bool(mtl_capable)), lcores, image,
             mxl_mount or "/dev/shm", ram_mb, docker_network, compute_image, compute_cpuset,
             media_image, media_mount,
             datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def db_update_node(node_id, **fields):
    allowed = ("name", "kind", "host", "mtl_iface", "mtl_capable", "lcores",
               "image", "mxl_mount", "ram_mb", "status", "docker_network", "compute_image",
               "compute_cpuset", "compute_gpu_image", "gpu_capable", "gpu_count",
               "media_image", "media_mount", "media_ip",
               "capabilities", "agent_url", "agent_token", "agent_version", "last_seen",
               "enroll_token", "enroll_profile", "ilo_host", "ilo_user", "ilo_password",
               "bmc_vendor", "tls_ready", "node_cert",
               # Profil CPU du nœud : modèle relevé (clé vers `cpu_profiles`) + surcharge du quota
               # de scheduler. Absents de cette liste, ils étaient AVALÉS EN SILENCE par l'upsert.
               "cpu_model", "sch_quota_mbs")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); args.append(int(v) if k in ("mtl_capable", "gpu_capable", "tls_ready") else v)
    if not sets:
        return
    args.append(node_id)
    with get_db() as db:
        db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", args)
        db.commit()

def db_delete_node(node_id):
    with get_db() as db:
        db.execute("DELETE FROM node_interfaces WHERE node_id=?", (node_id,))
        # Liens RDMA du nœud, dans LES DEUX SENS : un nœud retiré ne peut plus ni recevoir ni
        # produire. Sans ça, ses liens survivent à sa suppression et le réconciliateur les
        # retente indéfiniment — constaté le 2026-08-07 : SIX liens vers les nœuds 39 et 40,
        # disparus de la table, réessayaient toutes les 63 s depuis des jours. Le bruit qu'ils
        # produisaient dans le journal masquait les DEUX vraies pannes.
        db.execute("DELETE FROM rdma_links WHERE dst_node_id=? OR src_node_id=?",
                   (node_id, node_id))
        db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        db.commit()

# ── Listes blanches de colonnes (motif partagé) ──────────────────────────────
#
# Plusieurs helpers d'écriture acceptent `**fields` et ne retiennent que les colonnes d'une liste
# blanche. Le filtre protège des injections de nom de colonne, mais il est SILENCIEUX par nature :
# un champ hors liste disparaît sans exception ni trace. Ça a déjà coûté — `rdma_links.src_addr` et
# `dst_addr`, ajoutées à la table mais oubliées dans `RDMA_LINK_FIELDS`, faisaient « réussir »
# l'écriture sans rien persister : le rééquilibrage des chemins RDMA annonçait ses déplacements et
# rejouait indéfiniment le même plan, un port restant à zéro sans le moindre message.
#
# D'où ce helper unique : un appelant qui écrit un champ inconnu se trompe, et doit l'apprendre.
# ⚠ AJOUTER UNE COLONNE À L'UNE DE CES TABLES = L'AJOUTER À SA LISTE, sans quoi elle restera NULL.

def _champs_filtres(fields, autorises, nom_liste, appelant, ignorer_none=False):
    """Filtre `fields` sur la liste blanche `autorises` et JOURNALISE les champs écartés.
    `ignorer_none=True` écarte aussi les valeurs None (helpers d'INSERT, où None = « non fourni » ;
    les UPDATE gardent None pour permettre la remise à NULL explicite)."""
    inconnus = [k for k in fields if k not in autorises]
    if inconnus:
        log.warning("%s : champ(s) ignoré(s) car absent(s) de %s : %s — colonne oubliée dans la "
                    "liste blanche ?", appelant, nom_liste, ", ".join(sorted(inconnus)))
    return {k: v for k, v in fields.items()
            if k in autorises and not (ignorer_none and v is None)}


# ── node_interfaces : modèle « interface → rôle » par nœud (refonte réseau) ──

NODE_IFACE_FIELDS = ("ifname", "mac", "pci", "role", "pair_role", "pair_group",
                     "ip_cidr", "gateway", "vlan", "ptp_enabled", "ptp_domain",
                     "media_network_id", "mtu", "notes", "model", "speed_mbps",
                     "rx_reserve", "tx_reserve", "queue_margin", "pmd",
                     "output_profile", "alias", "vf_bdf", "vf_ip",
                     "ct_ip_start", "ct_ip_end")

# Rôle COMBINÉ « Management + Containers » : la carte porte À LA FOIS l'IP de contrôle du nœud
# et le réseau macvlan des conteneurs (cas nœud sur un autre LAN que le cluster, ex. dl360Horace).
# Tout test de rôle doit passer par ces helpers — jamais de comparaison littérale disséminée.
ROLE_MGMT_CONTAINERS = "mgmt_containers"

def role_is_management(role):
    """True si ce rôle porte le plan de contrôle du nœud (management pur ou combiné)."""
    return (role or "") in ("management", ROLE_MGMT_CONTAINERS)

def role_is_containers(role):
    """True si ce rôle porte le réseau macvlan des conteneurs (containers pur ou combiné)."""
    return (role or "") in ("containers", ROLE_MGMT_CONTAINERS)

# ── « Réseaux 2110 » : table globale (cluster). Une horloge logique PTP par réseau ──
_MN_KEEP = object()   # sentinelle « ne pas toucher » pour les champs optionnels de db_update_media_network

def db_get_media_networks():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM media_networks ORDER BY domain, id").fetchall()]

def db_add_media_network(name, domain, ptp_params=None):
    with get_db() as db:
        cur = db.execute("INSERT INTO media_networks (name, domain, ptp_params, created_at) VALUES (?,?,?,?)",
                         ((name or "").strip() or "Réseau", int(domain),
                          (json.dumps(ptp_params) if ptp_params else None),
                          datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def db_update_media_network(net_id, name=None, domain=None, ptp_params=_MN_KEEP):
    """Met à jour un réseau. `ptp_params` : dict (surcharges) → JSON ; None/{} → efface (hérite tout) ;
    sentinelle (défaut) → ne touche pas la colonne."""
    sets, args = [], []
    if name is not None:   sets.append("name=?");   args.append((name or "").strip() or "Réseau")
    if domain is not None: sets.append("domain=?"); args.append(int(domain))
    if ptp_params is not _MN_KEEP:
        sets.append("ptp_params=?"); args.append(json.dumps(ptp_params) if ptp_params else None)
    if not sets:
        return
    with get_db() as db:
        db.execute("UPDATE media_networks SET %s WHERE id=?" % ", ".join(sets), args + [int(net_id)])
        db.commit()

def db_media_network_in_use(net_id):
    """Nombre de NIC (tous nœuds) rattachées à ce réseau — garde-fou avant suppression."""
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM node_interfaces WHERE media_network_id=?",
                          (int(net_id),)).fetchone()[0]

def db_delete_media_network(net_id):
    if db_media_network_in_use(net_id) > 0:
        return False
    with get_db() as db:
        db.execute("DELETE FROM media_networks WHERE id=?", (int(net_id),))
        db.commit()
    return True

def db_get_node_interfaces(node_id):
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM node_interfaces WHERE node_id=? ORDER BY role, pair_group, ifname",
            (node_id,)).fetchall()]

def db_upsert_node_interface(node_id, ifname, clear=(), **fields):
    """Upsert d'une interface (clé node_id+ifname). Ne touche que les champs fournis (non-None).
    `clear` = noms de colonnes à remettre explicitement à NULL (permet de vider un réglage —
    p.ex. retirer l'appariement red/blue — alors que None signifie « ne pas toucher »)."""
    ifname = (ifname or "").strip()
    if not ifname:
        return
    sets = _champs_filtres(fields, NODE_IFACE_FIELDS, "NODE_IFACE_FIELDS",
                           "db_upsert_node_interface", ignorer_none=True)
    for k in clear:
        if k in NODE_IFACE_FIELDS:
            sets[k] = None                       # forcer NULL (vidage explicite)
    with get_db() as db:
        row = db.execute("SELECT id FROM node_interfaces WHERE node_id=? AND ifname=?",
                         (node_id, ifname)).fetchone()
        if row:
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                db.execute(f"UPDATE node_interfaces SET {cols} WHERE id=?",
                           list(sets.values()) + [row["id"]])
        else:
            cols = ["node_id", "ifname", "created_at"] + list(sets.keys())
            vals = [node_id, ifname, datetime.now().isoformat(timespec="seconds")] + list(sets.values())
            db.execute("INSERT INTO node_interfaces (%s) VALUES (%s)"
                       % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        db.commit()

def db_delete_node_interface(node_id, ifname):
    with get_db() as db:
        db.execute("DELETE FROM node_interfaces WHERE node_id=? AND ifname=?", (node_id, ifname))
        db.commit()

# ── nic_profiles : bibliothèque de cartes (capacités mesurées par la qualification) ──────────────
NIC_PROFILE_FIELDS = ("model", "rl_tx_cap", "narrow_ok", "ddp_ok", "ptp_ok", "measured", "notes")

def db_get_nic_profile(device_id, firmware=""):
    """Profil MESURÉ d'une carte (device_id + firmware). Repli firmware='' (profil générique du modèle)
    si le firmware exact n'est pas profilé. None si aucun profil → l'appelant retombe sur la biblio
    statique (app/mtl.py) puis le plancher sûr. Cf. docs/chantiers/DPDK_NARROW.md §7."""
    device_id = (device_id or "").strip().lower()
    if not device_id:
        return None
    with get_db() as db:
        for fw in ((firmware or "").strip(), ""):
            r = db.execute("SELECT * FROM nic_profiles WHERE lower(device_id)=? AND firmware=?",
                           (device_id, fw)).fetchone()
            if r:
                return dict(r)
    return None

def db_upsert_nic_profile(device_id, firmware="", **fields):
    """Upsert d'un profil carte (clé device_id+firmware). Ne touche que les champs fournis (non-None).
    Écrit par la suite de qualification (measured=1). `qualified_at` posé si measured passe à 1."""
    device_id = (device_id or "").strip().lower()
    firmware = (firmware or "").strip()
    if not device_id:
        return
    sets = _champs_filtres(fields, NIC_PROFILE_FIELDS, "NIC_PROFILE_FIELDS",
                           "db_upsert_nic_profile", ignorer_none=True)
    if sets.get("measured"):
        sets["qualified_at"] = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        row = db.execute("SELECT id FROM nic_profiles WHERE lower(device_id)=? AND firmware=?",
                         (device_id, firmware)).fetchone()
        if row:
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                db.execute(f"UPDATE nic_profiles SET {cols} WHERE id=?", list(sets.values()) + [row["id"]])
        else:
            cols = ["device_id", "firmware"] + list(sets.keys())
            vals = [device_id, firmware] + list(sets.values())
            db.execute("INSERT INTO nic_profiles (%s) VALUES (%s)"
                       % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        db.commit()

def db_all_nic_profiles():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM nic_profiles ORDER BY device_id, firmware").fetchall()]

# ── cpu_profiles : quota de scheduler par MODÈLE de CPU (cf. init_db pour le pourquoi) ────────────
CPU_PROFILE_FIELDS = ("quota_mbs", "cores", "threads", "base_mhz",
                      "memcpy_gbps", "pkt_mpps", "measured", "notes")


def db_upsert_cpu_profile(model, **fields):
    """Upsert d'un profil CPU (clé = modèle). Ne touche que les champs fournis (non-None) — poser
    `quota_mbs=None` LAISSE la valeur existante intacte, ce qui permet à une sonde d'enregistrer
    l'identité et le micro-banc SANS écraser un quota mesuré. `qualified_at` posé si measured→1."""
    model = (model or "").strip()
    if not model:
        return
    sets = _champs_filtres(fields, CPU_PROFILE_FIELDS, "CPU_PROFILE_FIELDS",
                           "db_upsert_cpu_profile", ignorer_none=True)
    if sets.get("measured"):
        sets["qualified_at"] = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        row = db.execute("SELECT id FROM cpu_profiles WHERE model=?", (model,)).fetchone()
        if row:
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                db.execute(f"UPDATE cpu_profiles SET {cols} WHERE id=?",
                           list(sets.values()) + [row["id"]])
        else:
            cols = ["model"] + list(sets.keys())
            vals = [model] + list(sets.values())
            db.execute("INSERT INTO cpu_profiles (%s) VALUES (%s)"
                       % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        db.commit()


def _norm_cpu_model(m):
    """Libellé CPU NORMALISÉ pour la comparaison : minuscules, marques déposées et bruit de format
    retirés, espaces réduits. Sans ça « Xeon Gold 6248R » ne matche pas « Intel(R) Xeon(R) Gold
    6248R CPU @ 3.00GHz » — les `(R)` cassent la sous-chaîne, et une bibliothèque qui ne matche
    jamais retombe EN SILENCE sur le défaut global (donc ne sert à rien)."""
    import re as _re
    m = (m or "").lower()
    m = _re.sub(r"\((?:r|tm|c)\)", " ", m)
    m = _re.sub(r"@.*$", " ", m)
    m = _re.sub(r"\b(?:cpu|processor|genuine|intel|amd)\b", " ", m)
    m = _re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:ghz|mhz)\b", " ", m)
    m = _re.sub(r"[^a-z0-9]+", " ", m)
    return " ".join(m.split())


def db_get_cpu_profile(model):
    """Profil du modèle de CPU donné, ou None. Match EXACT puis par sous-chaîne, sur les libellés
    NORMALISÉS (cf. `_norm_cpu_model`) — le plus spécifique gagne."""
    model = (model or "").strip()
    if not model:
        return None
    ml = _norm_cpu_model(model)
    if not ml:
        return None
    with get_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM cpu_profiles").fetchall()]
    for r in rows:
        if _norm_cpu_model(r.get("model")) == ml:
            return r
    best, best_len = None, 0
    for r in rows:
        rm = _norm_cpu_model(r.get("model"))
        if rm and (rm in ml or ml in rm) and len(rm) > best_len:
            best, best_len = r, len(rm)
    return best


def db_all_cpu_profiles():
    with get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM cpu_profiles ORDER BY model").fetchall()]


# ── tx_card_models : bibliothèque de MODÈLES de carte 2110 (gabarits par TYPE de carte) ───────────
# Le blob `slots` est stocké en JSON (même forme que io2110_layouts._normalize_slots). Les helpers
# le (dé)sérialisent pour que les appelants ne manipulent que des listes Python.

def _tx_card_model_row(r):
    d = dict(r)
    try:
        d["slots"] = json.loads(d.get("slots") or "[]")
    except Exception:
        d["slots"] = []
    return d

def db_all_tx_card_models():
    with get_db() as db:
        return [_tx_card_model_row(r) for r in db.execute(
            "SELECT * FROM tx_card_models ORDER BY nic_model, name").fetchall()]

def db_get_tx_card_model(mid):
    with get_db() as db:
        r = db.execute("SELECT * FROM tx_card_models WHERE id=?", (mid,)).fetchone()
    return _tx_card_model_row(r) if r else None

def db_create_tx_card_model(name, nic_model="", slots=None, notes="", actor=""):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO tx_card_models (name, nic_model, slots, notes, created_at, updated_at, "
            "updated_by) VALUES (?,?,?,?,?,?,?)",
            (name, nic_model or "", json.dumps(slots or []), notes or "", now, now, actor or ""))
        db.commit()
        return cur.lastrowid

def db_update_tx_card_model(mid, actor="", **fields):
    """Met à jour un modèle. Seuls name/nic_model/slots/notes sont modifiables (None = ne pas toucher)."""
    sets, args = [], []
    for k in ("name", "nic_model", "notes"):
        if fields.get(k) is not None:
            sets.append(f"{k}=?"); args.append(fields[k])
    if fields.get("slots") is not None:
        sets.append("slots=?"); args.append(json.dumps(fields["slots"]))
    if not sets:
        return
    sets += ["updated_at=?", "updated_by=?"]
    args += [datetime.now().isoformat(timespec="seconds"), actor or ""]
    with get_db() as db:
        db.execute("UPDATE tx_card_models SET %s WHERE id=?" % ", ".join(sets), args + [mid])
        db.commit()

def db_delete_tx_card_model(mid):
    with get_db() as db:
        db.execute("DELETE FROM tx_card_models WHERE id=?", (mid,))
        db.commit()

# ── tx_pending_changes : bac des changements TX différés (fenêtre de maintenance, étage 2) ──

def _tx_pending_row(r):
    d = dict(r)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except Exception:
        d["args"] = {}
    return d

def db_tx_pending_add(vmid, node_id, iface, op, args, label, apply_at=None, created_by=""):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO tx_pending_changes (vmid, node_id, iface, op, args, label, status, "
            "apply_at, created_at, created_by) VALUES (?,?,?,?,?,?,'pending',?,?,?)",
            (vmid, node_id, iface, op, json.dumps(args or {}), label,
             apply_at or None, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), created_by or ""))
        db.commit()
        return cur.lastrowid

def db_tx_pending_list(vmid=None, status="pending"):
    q = "SELECT * FROM tx_pending_changes WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"; args.append(status)
    if vmid is not None:
        q += " AND vmid=?"; args.append(int(vmid))
    q += " ORDER BY id"
    with get_db() as db:
        return [_tx_pending_row(r) for r in db.execute(q, args).fetchall()]

def db_tx_pending_get(pid):
    with get_db() as db:
        r = db.execute("SELECT * FROM tx_pending_changes WHERE id=?", (int(pid),)).fetchone()
        return _tx_pending_row(r) if r else None

def db_tx_pending_set_status(pid, status, result=None):
    with get_db() as db:
        db.execute("UPDATE tx_pending_changes SET status=?, result=? WHERE id=?",
                   (status, result, int(pid)))
        db.commit()

def db_tx_pending_set_apply_at(pid, apply_at):
    with get_db() as db:
        db.execute("UPDATE tx_pending_changes SET apply_at=? WHERE id=?", (apply_at or None, int(pid)))
        db.commit()

def db_tx_pending_due(now_iso):
    """Changements en attente dont l'heure planifiée est passée (comparaison lexicographique ISO)."""
    with get_db() as db:
        return [_tx_pending_row(r) for r in db.execute(
            "SELECT * FROM tx_pending_changes WHERE status='pending' AND apply_at IS NOT NULL "
            "AND apply_at <= ? ORDER BY id", (now_iso,)).fetchall()]

# ── mcast_ranges : règles de plage multicast strictes par réseau logique / interface physique ──

MCAST_RANGE_FIELDS = ("scope", "media_network_id", "node_id", "ifname", "base_ip", "size", "prefix_len",
                      "port_default", "port_default_video", "port_default_audio", "port_default_anc",
                      "ip_offset_video", "ip_offset_audio", "ip_offset_anc", "ip_step_audio",
                      "essence", "leg", "match_json", "label")

def db_get_mcast_ranges(media_network_id=None, node_id=None, ifname=None):
    q = "SELECT * FROM mcast_ranges WHERE 1=1"
    args = []
    if media_network_id is not None:
        q += " AND media_network_id=?"; args.append(int(media_network_id))
    if node_id is not None:
        q += " AND node_id=?"; args.append(int(node_id))
    if ifname is not None:
        q += " AND ifname=?"; args.append(ifname)
    q += " ORDER BY scope, media_network_id, node_id, ifname, id"
    with get_db() as db:
        return [dict(r) for r in db.execute(q, args).fetchall()]

def db_add_mcast_range(**fields):
    """Insère une règle. `scope` doit être 'network' (media_network_id renseigné, node_id/ifname
    NULL) ou 'interface' (node_id+ifname renseignés, media_network_id NULL) — invariant vérifié
    ici (pas en SQL)."""
    scope = fields.get("scope")
    if scope == "network":
        if not fields.get("media_network_id") or fields.get("node_id") or fields.get("ifname"):
            raise ValueError("scope='network' requiert media_network_id seul (pas node_id/ifname)")
    elif scope == "interface":
        if not fields.get("node_id") or not fields.get("ifname") or fields.get("media_network_id"):
            raise ValueError("scope='interface' requiert node_id+ifname (pas media_network_id)")
    else:
        raise ValueError("scope invalide (attendu 'network' ou 'interface')")
    if not fields.get("base_ip") or not fields.get("size"):
        raise ValueError("base_ip/size requis")
    cols = _champs_filtres(fields, MCAST_RANGE_FIELDS, "MCAST_RANGE_FIELDS",
                           "db_add_mcast_range", ignorer_none=True)
    cols["created_at"] = datetime.now().isoformat(timespec="seconds")
    keys = list(cols.keys())
    with get_db() as db:
        cur = db.execute("INSERT INTO mcast_ranges (%s) VALUES (%s)"
                         % (", ".join(keys), ", ".join("?" * len(keys))), list(cols.values()))
        db.commit()
        return cur.lastrowid

def db_update_mcast_range(range_id, **fields):
    sets = _champs_filtres(fields, MCAST_RANGE_FIELDS, "MCAST_RANGE_FIELDS", "db_update_mcast_range")
    if not sets:
        return
    with get_db() as db:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.execute(f"UPDATE mcast_ranges SET {cols} WHERE id=?",
                   list(sets.values()) + [int(range_id)])
        db.commit()

def db_delete_mcast_range(range_id):
    with get_db() as db:
        db.execute("DELETE FROM mcast_ranges WHERE id=?", (int(range_id),))
        db.commit()

# ── mcast_allocations : ledger de RÉSERVATION ATOMIQUE (ferme la fenêtre de course lire-puis-écrire
# entre deux allocateurs concurrents — deux containers/ressources qui calculent une adresse en même
# temps sans que le registre NMOS de l'un ne reflète encore le choix de l'autre). La PRIMARY KEY
# (ip, port) fait que SQLite refuse une 2ᵉ réservation de la même paire (IntegrityError), quel que
# soit l'ordre d'arrivée des threads — l'INSERT lui-même est l'opération atomique, pas une lecture
# suivie d'une décision en Python.
def db_reserve_mcast(ip, port, owner_ref):
    """Tente de réserver atomiquement (ip, port) pour `owner_ref`. True si acquis, False si déjà pris
    (par CET owner_ref ou un autre — idempotent : un owner_ref qui redemande SA PROPRE adresse déjà
    réservée obtient True sans dupliquer la ligne)."""
    with get_db() as db:
        try:
            db.execute("INSERT INTO mcast_allocations (ip, port, owner_ref, reserved_at) VALUES (?,?,?,?)",
                      (ip, int(port), owner_ref, datetime.now().isoformat(timespec="seconds")))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            db.rollback()
            row = db.execute("SELECT owner_ref FROM mcast_allocations WHERE ip=? AND port=?",
                             (ip, int(port))).fetchone()
            return bool(row) and row["owner_ref"] == owner_ref

def db_mcast_for_owner(owner_ref):
    """(ip, port) déjà réservé par cet owner_ref exact, ou None. Permet à allocate_multicast*
    de RÉUTILISER la réservation existante d'un même owner_ref au lieu d'en empiler une nouvelle
    à chaque retry de déploiement (fuite : owner_ref n'est pas PK — seule (ip,port) l'est —
    chaque échec de deploy laissait une adresse orpheline « occupée » à jamais)."""
    with get_db() as db:
        row = db.execute("SELECT ip, port FROM mcast_allocations WHERE owner_ref=? LIMIT 1",
                         (owner_ref,)).fetchone()
        return (row["ip"], row["port"]) if row else None

def db_release_mcast_owner(owner_ref, keep=None):
    """Libère LA réservation exacte de cet owner_ref (ex. suppression d'une ressource NMOS).
    `keep=(ip, port)` : conserve CETTE réservation et ne libère que les autres du même owner —
    utilisé par le plan d'adressage, qui réserve d'abord l'adresse planifiée (pour ne pas se la
    faire souffler) puis rend l'ancienne. Sans `keep`, tout l'owner_ref est libéré."""
    with get_db() as db:
        if keep:
            db.execute("DELETE FROM mcast_allocations WHERE owner_ref=? AND NOT (ip=? AND port=?)",
                       (owner_ref, keep[0], int(keep[1])))
        else:
            db.execute("DELETE FROM mcast_allocations WHERE owner_ref=?", (owner_ref,))
        db.commit()

def db_release_mcast_addr(ip, port, owner_prefix=None):
    """Libère LA réservation de cette adresse (ip, port), optionnellement seulement si son owner_ref
    commence par `owner_prefix` (garde-fou : ne jamais rendre l'adresse d'un AUTRE conteneur).
    Utilisé par la re-planification, qui doit rendre l'ANCIENNE adresse d'un flux dont l'owner_ref
    historique peut différer de celui de l'allocation courante (renommages successifs)."""
    with get_db() as db:
        if owner_prefix:
            db.execute("DELETE FROM mcast_allocations WHERE ip=? AND port=? AND owner_ref LIKE ?",
                       (ip, int(port), owner_prefix + "%"))
        else:
            db.execute("DELETE FROM mcast_allocations WHERE ip=? AND port=?", (ip, int(port)))
        db.commit()

def db_release_mcast_prefix(prefix):
    """Libère TOUTES les réservations dont owner_ref commence par `prefix` (ex. tous les flux TX
    d'un container qu'on détruit : préfixe 'tx:<vmid>:')."""
    with get_db() as db:
        db.execute("DELETE FROM mcast_allocations WHERE owner_ref LIKE ?", (prefix + "%",))
        db.commit()

def db_used_mcast_allocations():
    """Ensemble 'ip:port' de toutes les réservations actives du ledger (complète le registre NMOS,
    qui ne reflète une adresse qu'APRÈS l'enregistrement complet de la ressource — le ledger, lui,
    la connaît dès l'instant de la réservation)."""
    with get_db() as db:
        return {f"{r['ip']}:{r['port']}" for r in db.execute("SELECT ip, port FROM mcast_allocations")}

# ── vmid_reservations / ip_reservations : RÉSERVATION ATOMIQUE d'un handle vmid et d'une IP macvlan.
# L'INSERT (contraint par la PRIMARY KEY) EST l'opération qui tranche — deux allocateurs concurrents
# ne peuvent pas tous deux « gagner » le même vmid/la même IP (le perdant reçoit IntegrityError).
def db_reserve_vmid(vmid):
    """Réserve atomiquement `vmid`. True si acquis, False si déjà réservé (par une allocation
    concurrente). Le vmid étant MONOTONE (jamais réutilisé), la réservation reste un marqueur
    permanent « ce numéro a été distribué » — pas de libération nécessaire (cf. db_prune_consumed_vmid_reservations
    qui ne fait qu'alléger la table une fois le container réellement créé)."""
    with get_db() as db:
        try:
            db.execute("INSERT INTO vmid_reservations (vmid, reserved_at) VALUES (?,?)",
                       (int(vmid), datetime.now().isoformat(timespec="seconds")))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            db.rollback()
            return False

def db_used_vmid_reservations():
    """Ensemble des vmids réservés (en vol ou déjà consommés mais pas encore élagués)."""
    with get_db() as db:
        return {int(r["vmid"]) for r in db.execute("SELECT vmid FROM vmid_reservations")}

def db_prune_consumed_vmid_reservations():
    """Élague les réservations dont le container existe désormais (réservation consommée) : la ligne
    `containers` fait alors autorité. Purement cosmétique (borne la table) — ne réutilise jamais un
    vmid (monotone)."""
    with get_db() as db:
        db.execute("DELETE FROM vmid_reservations WHERE vmid IN (SELECT vmid FROM containers)")
        db.commit()

def db_reserve_ip(ip, vmid):
    """Réserve atomiquement l'IP macvlan `ip` pour `vmid`. True si acquise, False si déjà réservée
    (par CE vmid → idempotent True, ou par un autre → False)."""
    with get_db() as db:
        try:
            db.execute("INSERT INTO ip_reservations (ip, vmid, reserved_at) VALUES (?,?,?)",
                       (ip, int(vmid), datetime.now().isoformat(timespec="seconds")))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            db.rollback()
            row = db.execute("SELECT vmid FROM ip_reservations WHERE ip=?", (ip,)).fetchone()
            return bool(row) and int(row["vmid"]) == int(vmid)

def db_used_ip_reservations():
    """Ensemble des IPs actuellement réservées (avant que le container ne persiste son docker_ip)."""
    with get_db() as db:
        return {r["ip"] for r in db.execute("SELECT ip FROM ip_reservations")}

def db_release_ip_reservation(ip):
    """Libère la réservation d'IP `ip` (appelé quand containers.docker_ip prend le relais)."""
    with get_db() as db:
        db.execute("DELETE FROM ip_reservations WHERE ip=?", (ip,))
        db.commit()

def db_release_ip_reservations_for_vmid(vmid):
    """Libère toutes les réservations d'IP détenues par `vmid` (redeploy du même vmid, ou destruction
    du container avant que l'IP n'ait été persistée en docker_ip → évite la fuite d'IP)."""
    with get_db() as db:
        db.execute("DELETE FROM ip_reservations WHERE vmid=?", (int(vmid),))
        db.commit()

# ── Liens RDMA (réplication de flux MXL inter-nœuds via mxl-fabrics) ──
# ⚠ LISTE BLANCHE : `db_add_rdma_link` / `db_update_rdma_link` IGNORENT en silence tout champ absent
# d'ici. Ajouter une colonne à `rdma_links` sans l'inscrire ici donne un écrivain qui « réussit » et
# une colonne qui reste NULL — sans exception, sans log. Toute nouvelle colonne va DANS cette liste.
RDMA_LINK_FIELDS = ("src_node_id", "src_vmid", "src_flow", "dst_node_id", "kind", "provider",
                    "service_port", "status", "target_info", "notes", "flow_format", "auto_cable",
                    "src_addr", "dst_addr", "sync_batch")

def db_list_rdma_links():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM rdma_links ORDER BY id").fetchall()]

def db_get_rdma_link(link_id):
    with get_db() as db:
        r = db.execute("SELECT * FROM rdma_links WHERE id=?", (int(link_id),)).fetchone()
        return dict(r) if r else None

def db_add_rdma_link(src_node_id, src_flow, dst_node_id, **fields):
    cols = _champs_filtres(fields, RDMA_LINK_FIELDS, "RDMA_LINK_FIELDS",
                           "db_add_rdma_link", ignorer_none=True)
    cols.update(src_node_id=int(src_node_id), src_flow=src_flow, dst_node_id=int(dst_node_id),
                created_at=datetime.now().isoformat(timespec="seconds"))
    keys = list(cols.keys())
    with get_db() as db:
        cur = db.execute("INSERT INTO rdma_links (%s) VALUES (%s)"
                         % (", ".join(keys), ", ".join("?" * len(keys))),
                         [cols[k] for k in keys])
        db.commit()
        return cur.lastrowid

def db_update_rdma_link(link_id, **fields):
    sets = _champs_filtres(fields, RDMA_LINK_FIELDS, "RDMA_LINK_FIELDS", "db_update_rdma_link")
    if not sets:
        return
    with get_db() as db:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.execute(f"UPDATE rdma_links SET {cols} WHERE id=?",
                   list(sets.values()) + [int(link_id)])
        db.commit()

def db_delete_rdma_link(link_id):
    with get_db() as db:
        db.execute("DELETE FROM rdma_links WHERE id=?", (int(link_id),))
        db.commit()

# RÉTENTION du fil. Portée de 1 000 à 10 000 le 2026-07-27, avec la décision que `alerts` est AUSSI
# le JOURNAL D'EXPLOITATION (« qui a fait quoi ») et pas seulement la liste de ce qui va mal. À
# 1 000 lignes, un site actif renouvelait tout son fil en ~2 jours (mesuré : 1 096 lignes en 19 h) —
# l'historique des actions disparaissait avant d'avoir servi, et le bruit ÉVINÇAIT les vraies
# alertes. 10 000 lignes ≈ quelques Mo de SQLite pour des semaines d'historique.
ALERTS_RETENTION_DEFAUT = 10000
ALERTS_PURGE_MARGIN = 200   # purge seulement quand on dépasse RETENTION + marge


def _alerts_retention():
    """Rétention effective (réglage `alerts_retention`, défaut ALERTS_RETENTION_DEFAUT).
    Bornée à 100 minimum : un réglage à 0 ou négatif viderait le journal à chaque insert."""
    try:
        v = int(db_get_setting("alerts_retention", ALERTS_RETENTION_DEFAUT) or ALERTS_RETENTION_DEFAUT)
    except (TypeError, ValueError):
        return ALERTS_RETENTION_DEFAUT
    return max(100, v)


# Compat : d'anciens imports référencent la constante. Elle vaut le défaut ; le chemin d'écriture
# passe par `_alerts_retention()` pour honorer le réglage.
ALERTS_RETENTION = ALERTS_RETENTION_DEFAUT

# ─── Vocabulaire FERMÉ des natures d'incident (colonne `alerts.kind`) ────────────────────────
# UN SEUL endroit. Une chaîne inventée à l'appel serait inexploitable pour filtrer : le but de la
# colonne est justement de pouvoir dire « montre-moi tous les rx_stall » ou « tout ce qui touche le
# nœud 3 ». Ajouter une valeur ICI (et la documenter) avant de l'utiliser ; un `kind` inconnu est
# REFUSÉ (stocké NULL) avec un log d'avertissement — jamais accepté en douce.
ALERT_KINDS = (
    "rx_stall",     # réception 2110 en panne / absente (flux RX)
    "tx_stall",     # émission 2110 en panne / figée (flux TX)
    "signal",       # présence signal : noir, gel, silence, hors-gamut, loudness
    "fps",          # cadence non tenue par un conteneur
    "crash_loop",   # conteneur/script qui redémarre en boucle ou passe en quarantaine
    "agent",        # agent par-conteneur injoignable, script arrêté/repris
    "deploy",       # création / déploiement / arrêt / destruction d'un conteneur
    "prep",         # préparation hôte (MTL, vfio, hugepages, isolation, pinning)
    "net",          # réseau : IP média, lien, port muet, anomalie d'interface
    "node",         # nœud : reboot, auto-recovery, injoignable
    "disk",         # remplissage disque
    "ptp",          # horloge PTP
    "resource",     # CPU/RAM/bande passante mémoire/GPU
    "advisory",     # avis remonté par un plugin (l'exploitant arbitre)
    "webhook",      # sortie d'alerte elle-même en échec (canal mail/webhook injoignable)
    "ui",           # interface elle-même en défaut : un bloc <script> de gabarit dont la syntaxe
                    # est invalide met HORS SERVICE toutes les fonctions de sa page côté navigateur,
                    # sans que le serveur ne s'en aperçoive (il rend la page normalement). Contrôlé
                    # au démarrage par app/template_check.py.
)


def _alert_kind(kind):
    """Normalise/valide un `kind`. Inconnu → None + log bruyant (pas d'échec silencieux)."""
    if kind is None:
        return None
    k = str(kind).strip().lower()
    if k in ALERT_KINDS:
        return k
    log.warning("db_add_alert: kind « %s » hors vocabulaire (ALERT_KINDS) — stocké NULL", kind)
    return None


def _alert_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _acteur_courant():
    """Login de l'utilisateur à l'origine de l'action, ou None pour une action de la MACHINE.

    Rempli UNIQUEMENT quand on écrit depuis le fil d'une requête HTTP (donc de façon synchrone dans
    la route). C'est délibéré : les actions de l'orchestrateur partent ensuite en `threading.Thread`,
    et un thread N'HÉRITE PAS du contexte de requête — prétendre y retrouver l'acteur demanderait de
    le propager à la main dans 64 dispatches. Le « qui a fait quoi » est donc porté par la ligne de
    DEMANDE posée dans la route (cf. `app/audit.py`), pas par les lignes de résultat émises plus
    tard par le traitement de fond, qui restent honnêtement anonymes."""
    try:
        from flask import has_request_context
        if not has_request_context():
            return None
        from .auth import current_user
        u = current_user()
        return (u or {}).get("username") or None
    except Exception:
        return None                 # jamais un chemin d'alerte ne doit échouer sur l'attribution


# ─── Anti-rebond : une alerte qui se répète ne s'écrit qu'à la TRANSITION ────────────────────
#
# Constat du 2026-08-15 : 10 068 alertes en base, dont quatre messages répétés faisaient ~80 % du
# volume (un lien RDMA vers un nœud éteint, 1145 lignes ; un ré-établissement en boucle, 680 par
# lien ; un câblage de pyramide reperdu toutes les 2 min). Conséquence : un « TX #1 instable »
# survenu l'après-midi était déjà noyé sous les répétitions du matin. Une supervision où le volume
# est fait de redites ne supervise plus rien.
#
# Règle, la même que celle d'`app/placement.py` : **on alerte à la transition, on compte ensuite**.
#   • 1ʳᵉ occurrence d'un symptôme  → écrite IMMÉDIATEMENT, telle quelle (c'est la transition).
#   • répétitions dans la fenêtre   → comptées, pas écrites (ni journal, ni e-mail/webhook).
#   • toujours là au bout d'une fenêtre → UNE ligne de résumé « répété N fois depuis <heure> ».
#   • plus rien pendant une fenêtre → l'épisode est CLOS : la prochaine occurrence redevient une
#     transition, donc réécrite tout de suite. C'est ce qui garantit qu'un incident qui repart
#     après accalmie ne se fait jamais étouffer.
#
# Deux garde-fous contre l'étouffement de ce qu'il ne faut PAS étouffer :
#   • le `niveau` fait partie de la signature — une aggravation info→warning→error est une
#     transition, donc écrite sur-le-champ ;
#   • une alerte portant un ACTEUR (`user`) n'est jamais rebondie : `alerts` est aussi le journal
#     d'exploitation, et deux gestes humains identiques sont deux faits distincts à conserver.

ANTIREBOND_DEFAUT_S = 900       # 15 min. 0 (réglage `alerts_antirebond_s`) = désactivé.

# Un nombre qui est une QUANTITÉ est neutralisé (« depuis 93927 s » et « depuis 92479 s » sont le
# même symptôme) ; un nombre qui est un IDENTIFIANT est gardé (`#3800`, `audio_1`, `dl360-1`) —
# sinon deux liens différents se replieraient l'un sur l'autre. D'où la garde arrière : un chiffre
# collé à `#`, à `_`, à `-` ou à un caractère de mot appartient au nom de la chose, pas à sa mesure.
_ANTIREBOND_QUANTITE = re.compile(r"(?<![#\w_-])\d+(?:[.,]\d+)*")

# Les alertes viennent de dizaines de threads de fond : sans ce verrou, deux occurrences
# simultanées du même symptôme liraient le même épisode et écriraient deux lignes.
_antirebond_verrou = threading.Lock()


# Espaces de noms RÉSERVÉS des clés d'alerte. La détection est une correspondance EXACTE sur ce
# motif, jamais une reconnaissance de forme sur le texte : aucune phrase française ne peut
# ressembler à `alert.<…>`, donc il n'y a pas d'ambiguïté à lever, et rien à deviner.
#
# `plugin.<…>` est admis parce que les AVIS de plugins (`advisory`, cf. app/metrics.py) portent
# des clés qui vivent dans le catalogue du plugin, préfixées `plugin.<type>.*` — c'est la
# convention i18n du projet. Sans lui, un avis structuré était stocké en texte brut (la clé
# elle-même), ses paramètres perdus, et SANS erreur au journal : exactement l'échec silencieux
# que ce chantier supprime. Vérifié le 2026-08-21.
_ALERT_CLE = re.compile(r"^(?:alert|plugin)\.[A-Za-z0-9_.]+$")


def _alert_cle(message, params):
    """Sépare une CLÉ i18n d'une phrase déjà rédigée. → (msg_key, msg_params JSON, message FR).

    La phrase française est RENDUE depuis la clé — jamais l'inverse. Les deux formes sortent de
    la même source, donc elles ne peuvent pas diverger, et le texte stocké n'est JAMAIS relu pour
    en déduire un sens (cf. l'avertissement de l'entrée TODO : traduire par motifs marcherait sur
    les phrases d'aujourd'hui et casserait au premier reformulage, en silence).

    Un premier argument qui n'est pas une clé est une phrase du chemin historique : écrite telle
    quelle, sans clé. Les deux formes cohabitent le temps que les sites d'appel soient migrés.

    Ne lève jamais : un défaut de catalogue ne doit pas faire perdre une alerte."""
    if not isinstance(message, str) or not _ALERT_CLE.match(message):
        return None, None, message
    p = dict(params or {})
    try:
        from . import i18n as _i18n
        if not _i18n.existe(message):
            # Clé absente du catalogue : on l'écrit EN CLAIR et on le dit. La replier
            # silencieusement afficherait `alert.deploy.detruit` comme une phrase plausible —
            # exactement le genre d'oubli qui ne se voit jamais.
            log.error("alerte : clé i18n inconnue %r — message écrit en clair", message)
            return None, None, ("%s %s" % (message, p) if p else message)
        # Les sous-clés (listes de paires `[clé, params]`, cf. `i18n._developper_sous_cles`) doivent
        # être développées ICI AUSSI : sans ça, la forme canonique contiendrait la représentation
        # Python de la liste. Ce n'est pas cosmétique — c'est cette colonne qui sert d'INDEX
        # (signature d'anti-rebond, regroupement de l'accueil, recherche `?q=`, et la ligne de
        # journal). Elle serait devenue illisible ET inutilisable pour les trois.
        # On développe sur une COPIE : `msg_params` doit garder les sous-clés brutes, sinon le
        # rendu à la lecture ne pourrait plus les traduire.
        fr = _i18n.t(message, lang=_i18n.DEFAULT_LANG,
                     **_i18n._developper_sous_cles(dict(p), _i18n.DEFAULT_LANG))
        blob = json.dumps(p, ensure_ascii=False, default=str) if p else None
        return message, blob, fr
    except Exception:
        log.exception("alerte : clé %r inexploitable — message écrit en clair", message)
        return None, None, message


def _antirebond_fenetre():
    try:
        return max(0, int(db_get_setting("alerts_antirebond_s", ANTIREBOND_DEFAUT_S)))
    except (TypeError, ValueError):
        return ANTIREBOND_DEFAUT_S


def _ecart_s(ts_iso, maintenant):
    """Secondes écoulées depuis un horodatage ISO, ou None s'il est illisible/absent."""
    if not ts_iso:
        return None
    try:
        return (maintenant - datetime.fromisoformat(ts_iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def _antirebond(message, niveau, vmid, node_id, kind, maintenant):
    """Décide si CETTE occurrence doit être écrite au journal.
    → (écrire: bool, message canonique final, répétition: dict|None).

    Le TROISIÈME membre porte le compte d'un épisode qui dure (`n`, `depuis`). Il est rendu à la
    LECTURE, via `msg_params`, au lieu d'être concaténé ici : concaténé, le « répété N fois
    depuis 14:32 » resterait français sur toutes les alertes en cours — c'est-à-dire justement
    sur celles qu'on regarde. Le message canonique, lui, le porte bien (il sert d'index, pas
    d'affichage).

    Ne lève jamais : un défaut ici doit laisser passer l'alerte (on préfère une redite à un
    silence), jamais la perdre."""
    fenetre = _antirebond_fenetre()
    if fenetre <= 0:
        return True, message, None
    try:
        squelette = _ANTIREBOND_QUANTITE.sub("<n>", message or "")
        cle = "%s|%s|%s|%s|%s" % (kind, vmid, node_id, niveau, squelette)
        sig = hashlib.sha1(cle.encode("utf-8")).hexdigest()
        ts = maintenant.isoformat(timespec="milliseconds")
        with _antirebond_verrou, get_db() as db:
            row = db.execute("SELECT * FROM alert_episodes WHERE signature=?", (sig,)).fetchone()
            depuis = _ecart_s(row["last_ts"], maintenant) if row else None
            if row is None or depuis is None or depuis > fenetre:
                # Transition : épisode neuf, ou épisode rouvert après une accalmie.
                db.execute(
                    "INSERT INTO alert_episodes (signature, squelette, kind, vmid, node_id, niveau,"
                    " first_ts, last_ts, last_emit_ts, occurrences, muettes, last_message)"
                    " VALUES (?,?,?,?,?,?,?,?,?,1,0,?)"
                    " ON CONFLICT(signature) DO UPDATE SET first_ts=excluded.first_ts,"
                    " last_ts=excluded.last_ts, last_emit_ts=excluded.last_emit_ts,"
                    " occurrences=1, muettes=0, last_message=excluded.last_message",
                    (sig, squelette[:400], kind, vmid, node_id, niveau, ts, ts, ts, message))
                db.commit()
                return True, message, None
            n = (row["occurrences"] or 0) + 1
            emis = _ecart_s(row["last_emit_ts"], maintenant)
            if emis is not None and emis < fenetre:
                db.execute("UPDATE alert_episodes SET occurrences=?, muettes=muettes+1, last_ts=?,"
                           " last_message=? WHERE signature=?", (n, ts, message, sig))
                db.commit()
                return False, message, None
            # Toujours en cours au bout d'une fenêtre : une ligne, et le compte réel avec.
            db.execute("UPDATE alert_episodes SET occurrences=?, muettes=0, last_ts=?,"
                       " last_emit_ts=?, last_message=? WHERE signature=?", (n, ts, ts, message, sig))
            db.commit()
            _debut = (row["first_ts"] or "")[11:16] or "?"
            # Le message repris est le plus RÉCENT : sous une même signature, les quantités varient
            # (« depuis 92479 s » → « depuis 93927 s »). On l'annonce en ces termes plutôt que de
            # laisser croire que N lignes identiques ont été vues.
            canonique = "%s — répété %d fois depuis %s" % (message, n, _debut)
            return True, canonique, {"n": n, "depuis": _debut}
    except Exception:
        log.exception("anti-rebond des alertes : défaut — l'alerte est écrite telle quelle")
        return True, message, None


def db_alert_episodes(actifs_seulement=True, limit=200):
    """Épisodes d'alerte, du plus récemment vu au plus ancien. Rend VISIBLE ce que l'anti-rebond
    a tu : `muettes` = occurrences étouffées depuis la dernière ligne écrite."""
    maintenant = datetime.now()
    fenetre = _antirebond_fenetre()
    out = []
    with get_db() as db:
        rows = db.execute("SELECT * FROM alert_episodes ORDER BY last_ts DESC LIMIT ?",
                          (int(limit),)).fetchall()
    for r in rows:
        d = dict(r)
        age = _ecart_s(d.get("last_ts"), maintenant)
        d["age_s"] = round(age, 1) if age is not None else None
        d["actif"] = bool(fenetre and age is not None and age <= fenetre)
        if actifs_seulement and not d["actif"]:
            continue
        out.append(d)
    return out


def db_add_alert(message, niveau="info", vmid=None, node_id=None, kind=None, user=None,
                 antirebond=True, params=None):
    """Journalise une alerte.

    `message` est SOIT une clé i18n (`alert.<…>`, avec ses valeurs dans `params`), SOIT une
    phrase déjà rédigée (chemin historique). Avec une clé, l'appelant n'écrit plus de f-string :
    la phrase française est rendue ici depuis cette même clé et rangée dans `message` comme forme
    canonique, et le rendu destiné à l'écran est différé à la lecture, dans la langue du lecteur
    (`i18n.rendre_alerte`). `params` est un dict EXPLICITE, et non des `**kwargs`, parce que les
    paramètres de message et les colonnes de contexte se recouvrent (`vmid` est très souvent les
    deux) — les mélanger rendrait l'appel ambigu. Le CONTEXTE (`vmid`, `node_id`, `kind`) est OPTIONNEL et
    rétrocompatible : les appels historiques à deux arguments écrivent NULL et restent servis par
    la déduction textuelle côté consommateurs. Un producteur qui ne SAIT pas passe None — il ne
    devine pas.

    `user` : acteur à l'origine de l'action. Laissé à None, il est déduit du contexte de requête
    s'il y en a un (cf. `_acteur_courant`) — les alertes émises dans une route portent donc leur
    acteur SANS modification d'appel. NULL = machine.

    `antirebond` : à False, la ligne est écrite quoi qu'il arrive (cf. `_antirebond`). À réserver
    aux producteurs dont la répétition EST l'information."""
    vmid, node_id, kind = _alert_int(vmid), _alert_int(node_id), _alert_kind(kind)
    msg_key, msg_params, message = _alert_cle(message, params)
    if user is None:
        user = _acteur_courant()
    maintenant = datetime.now()
    # Un geste HUMAIN n'est jamais rebondi : deux actions identiques sont deux faits distincts,
    # et `alerts` sert aussi de journal d'exploitation (« qui a fait quoi »).
    if antirebond and user is None:
        ecrire, message, repetition = _antirebond(message, niveau, vmid, node_id, kind, maintenant)
        if not ecrire:
            return None
        if repetition and msg_key:
            # Le compte voyage dans les paramètres, pas dans la phrase : c'est ce qui permet au
            # lecteur anglophone de voir « repeated 4 times » plutôt qu'un bout de français.
            from .i18n import ALERTE_REP_N, ALERTE_REP_DEPUIS
            _p = json.loads(msg_params) if msg_params else {}
            _p[ALERTE_REP_N] = repetition["n"]
            _p[ALERTE_REP_DEPUIS] = repetition["depuis"]
            msg_params = json.dumps(_p, ensure_ascii=False, default=str)
    with get_db() as db:
        db.execute(
            "INSERT INTO alerts (message, niveau, timestamp, vmid, node_id, kind, user, "
            "msg_key, msg_params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message, niveau, maintenant.isoformat(timespec="milliseconds"),
             vmid, node_id, kind, user, msg_key, msg_params))
        # Purge allégée : au lieu d'un DELETE … NOT IN (scan complet) à CHAQUE insert,
        # on ne purge que lorsque la table dépasse RETENTION + marge (≈ tous les 200
        # inserts). Le COUNT(*) est bien moins coûteux que le DELETE corrélé.
        _ret = _alerts_retention()
        n = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        if n > _ret + ALERTS_PURGE_MARGIN:
            db.execute(
                "DELETE FROM alerts WHERE id NOT IN "
                "(SELECT id FROM alerts ORDER BY id DESC LIMIT ?)",
                (_ret,))
            # Même occasion : les épisodes clos depuis longtemps (une semaine) n'ont plus de rôle,
            # ni pour l'anti-rebond ni pour la lecture. Purgés dans la MÊME branche que les
            # alertes, donc ~1 fois sur 200 inserts : la table reste bornée sans coût par appel.
            db.execute("DELETE FROM alert_episodes WHERE last_ts < ?",
                       ((datetime.now() - timedelta(days=7)).isoformat(timespec="seconds"),))
        db.commit()
    # Sortie PUSH (service d'alertes : e-mail, webhook, …) — APRÈS le commit, et strictement hors
    # chemin critique : notify() ne fait qu'empiler en mémoire et réveiller un thread daemon (aucun
    # I/O réseau, aucune lecture DB ici). Un serveur SMTP ou une URL injoignable ne peut donc ni
    # retarder ni faire échouer l'écriture ci-dessus.
    # Import PARESSEUX : le service lit les settings, qui importent ce module.
    try:
        from services.alerting import notify as _alerting_notify
        try:
            _alerting_notify(message, niveau, vmid=vmid, node_id=node_id, kind=kind,
                             msg_key=msg_key, msg_params=msg_params)
        except TypeError:
            # Sous-module `services/alerting` ANTÉRIEUR au rendu multilingue : parent et
            # sous-modules sont versionnés séparément, et une désynchronisation des deux est un
            # mode de panne déjà vécu ici. On retombe sur l'appel historique — la sortie push
            # part alors en français, ce qui est dégradé mais pas cassé.
            _alerting_notify(message, niveau, vmid=vmid, node_id=node_id, kind=kind)
    except Exception:
        # Pas d'`except: pass` : si la sortie push casse, ça doit se voir dans le journal.
        log.exception("db_add_alert: mise en file du service d'alertes impossible")
    # Rendu : le message CANONIQUE réellement écrit (None si l'anti-rebond l'a tu). L'appelant
    # journalise ça plutôt que la clé — un `log.info("alert.deploy.detruit")` ne dirait rien à
    # qui lit les journaux.
    return message

# ─── Journal d'événements PTP (persisté) ────────────────────
PTP_EVENTS_RETENTION = 5000
PTP_EVENTS_PURGE_MARGIN = 500

def db_add_ptp_event(node_id, node_name, network_id, network_name, ifname, type_, detail, level="info"):
    with get_db() as db:
        db.execute(
            "INSERT INTO ptp_events (ts, node_id, node_name, network_id, network_name, ifname, type, detail, level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="milliseconds"), node_id, node_name,
             network_id, network_name, ifname, type_, detail, level))
        # Purge allégée (cf. db_add_alert) : on ne purge que lorsqu'on dépasse RETENTION + marge.
        n = db.execute("SELECT COUNT(*) FROM ptp_events").fetchone()[0]
        if n > PTP_EVENTS_RETENTION + PTP_EVENTS_PURGE_MARGIN:
            db.execute(
                "DELETE FROM ptp_events WHERE id NOT IN "
                "(SELECT id FROM ptp_events ORDER BY id DESC LIMIT ?)",
                (PTP_EVENTS_RETENTION,))
        db.commit()

def db_last_ptp_event(node_id, network_id, types):
    """Dernier événement PTP (le plus récent) d'un (nœud, réseau) parmi `types`, ou None.

    Sert la REPRISE D'ÉTAT au démarrage de l'orchestrateur (app/ptp.py) : le journal persisté fait
    mémoire à la place du process, sinon un incident d'horloge encore en cours est ré-alerté à
    chaque redémarrage."""
    types = tuple(types or ())
    if not types:
        return None
    sql = ("SELECT * FROM ptp_events WHERE node_id = ? AND network_id = ? AND type IN ({}) "
           "ORDER BY id DESC LIMIT 1".format(",".join("?" * len(types))))
    with get_db() as db:
        r = db.execute(sql, (node_id, network_id) + types).fetchone()
    return dict(r) if r else None


def db_get_ptp_events(node_id=None, network_id=None, q=None, limit=PTP_EVENTS_RETENTION):
    sql = "SELECT * FROM ptp_events"
    where, params = [], []
    if node_id is not None:
        where.append("node_id = ?"); params.append(node_id)
    if network_id is not None:
        where.append("network_id = ?"); params.append(network_id)
    if q:
        where.append("(detail LIKE ? OR ifname LIKE ? OR node_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]

def db_config_rev(vmid):
    """Révision courante de `deploy_config` (cf. la colonne `config_rev`). 0 si inconnue —
    un conteneur jamais écrit depuis la migration, ou un vmid disparu."""
    with get_db() as db:
        r = db.execute("SELECT config_rev FROM containers WHERE vmid=?", (vmid,)).fetchone()
    return int((r[0] if r and r[0] is not None else 0))


def db_config_rev_auteur(vmid):
    """(révision, auteur) de la dernière écriture de `deploy_config`. Auteur None = machine."""
    with get_db() as db:
        r = db.execute("SELECT config_rev, config_rev_by FROM containers WHERE vmid=?",
                       (vmid,)).fetchone()
    if not r:
        return 0, None
    return int(r[0] or 0), r[1]


def db_update_deploy_config(vmid, type_script, params):
    # FILET « params MAIGRES » : `font_library` (jusqu'à 8 Mo de base64 par conteneur) n'est
    # JAMAIS persisté — les références `lib:<sha16>` des params suffisent, la bibliothèque
    # (static/uploads/fonts + table `fonts`) est la source de vérité, et le base64 est ré-injecté
    # à la volée à l'ENVOI vers le conteneur (deploy._gras). Sans ce filet, chaque listing de
    # conteneurs (/api/containers toutes les 5 s, surveillance…) relirait ces mégaoctets.
    if isinstance(params, dict) and "font_library" in params:
        params = {k: v for k, v in params.items() if k != "font_library"}
    with get_db() as db:
        # `config_rev` : +1 à CHAQUE écriture, quelle qu'en soit la source (déploiement, hot-apply
        # persisté, câblage, macro, restauration de projet). C'est ce compteur que la garde
        # anti-écrasement compare — cf. la migration dans `init_db`.
        from .edit_lock import auteur_courant
        db.execute("UPDATE containers SET deploy_config=?, deployed_at=?, "
                   "config_rev=COALESCE(config_rev, 0) + 1, config_rev_by=? WHERE vmid=?",
                   (json.dumps({"type": type_script, "params": params}),
                    datetime.now().isoformat(timespec="seconds"), auteur_courant(), vmid))
        db.commit()
    # « Projet vivant » (chantier 3) : toute modif de config d'un container de projet
    # déclenche un re-snapshot débouncé du projet. Import tardif (pas de cycle au boot),
    # best-effort : ne doit jamais faire échouer l'écriture.
    try:
        from .projects import notify_container_changed
        notify_container_changed(vmid)
    except Exception:
        pass

def db_update_source(vmid, source, shm_out):
    with get_db() as db:
        db.execute("UPDATE containers SET source=?, shm_out=? WHERE vmid=?",
                   (source, shm_out, vmid))
        db.commit()

# ─── Registre des nœuds de fabric (tissu de composition, keyé par signature) ──────────────
@cache_requete
def db_fabric_all(node_id=None):
    """Tous les nœuds de fabric matérialisés (optionnellement filtrés par nœud)."""
    with get_db() as db:
        if node_id is not None:
            rows = db.execute("SELECT * FROM fabric_node_alloc WHERE node_id=?", (node_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM fabric_node_alloc").fetchall()
    return [dict(r) for r in rows]

def db_fabric_get(signature):
    with get_db() as db:
        r = db.execute("SELECT * FROM fabric_node_alloc WHERE signature=?", (signature,)).fetchone()
    return dict(r) if r else None

def db_fabric_upsert(signature, node_id, vmid, shm, kind, out_w, out_h, ref=None, parents=None,
                     tile_x=None, tile_y=None, fmt=None):
    """Enregistre/maj un nœud matérialisé. `ref` = identifiant de teardown du conteneur (nom docker
    ou vmid, selon le chemin). `parents` = liste de vmids des multiviews logiques consommateurs
    (sérialisée JSON). `tile_x`/`tile_y` = emplacement dans le mur et `fmt` = empreinte du format
    de sortie : ensemble avec out_w/out_h, ils identifient l'EMPLACEMENT (par opposition au
    CONTENU, porté par la signature) — cf. le rebind de compositor_fabric.reconcile_fabric.
    Pose created_at à la création et last_ref maintenant."""
    now = datetime.now().isoformat(timespec="seconds")
    pj = json.dumps(parents) if parents is not None else None
    with get_db() as db:
        db.execute("""INSERT INTO fabric_node_alloc
                        (signature, node_id, vmid, ref, shm, kind, out_w, out_h, parents,
                         created_at, last_ref, tile_x, tile_y, fmt)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(signature) DO UPDATE SET
                        node_id=excluded.node_id, vmid=excluded.vmid, ref=excluded.ref,
                        shm=excluded.shm, kind=excluded.kind, out_w=excluded.out_w,
                        out_h=excluded.out_h, parents=excluded.parents, last_ref=excluded.last_ref,
                        tile_x=excluded.tile_x, tile_y=excluded.tile_y, fmt=excluded.fmt""",
                   (signature, node_id, vmid, ref, shm, kind, out_w, out_h, pj, now, now,
                    tile_x, tile_y, fmt))
        db.commit()

def db_fabric_touch(signatures):
    """Rafraîchit last_ref (= référencé à ce cycle) pour un ensemble de signatures encore voulues."""
    if not signatures:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        db.executemany("UPDATE fabric_node_alloc SET last_ref=? WHERE signature=?",
                       [(now, s) for s in signatures])
        db.commit()

def db_fabric_delete(signature):
    with get_db() as db:
        db.execute("DELETE FROM fabric_node_alloc WHERE signature=?", (signature,))
        db.commit()

# ─── Registre NMOS de niveau cluster (C2a) ───────────────────────────
def _nmos_row(r):
    d = dict(r)
    try:
        d["transport"] = json.loads(d["transport"]) if d.get("transport") else {}
    except Exception:
        d["transport"] = {}
    return d

def db_nmos_resources():
    """Toutes les ressources NMOS du registre (transport déjà parsé)."""
    with get_db() as db:
        return [_nmos_row(r) for r in
                db.execute("SELECT * FROM nmos_resources ORDER BY essence, label").fetchall()]

def db_nmos_grouping_collisions(limite=12):
    """Couples `groupe:rôle` en DOUBLE dans le registre — violation du MUST BCP-002-01.

    La spec impose l'unicité du couple dans un même scope de Device. Un banc le vérifiait
    (`services/nmos/bench_bcp002.py`), mais un banc qu'on lance à la main ne surveille rien :
    l'exploitation ne voyait pas la collision. Et comme `db_nmos_resource_upsert` FIGE
    group_name/role à la première écriture, une collision installée est DÉFINITIVE — d'où la
    remontée en continu.

    Renvoie les couples fautifs NOMMÉS, pas un compteur : « 2 » ne dit pas lesquels réparer."""
    with get_db() as db:
        rows = db.execute(
            "SELECT group_name, role, COUNT(*) c FROM nmos_resources "
            "WHERE COALESCE(group_name,'')<>'' AND COALESCE(role,'')<>'' "
            "GROUP BY group_name, role HAVING c > 1 ORDER BY c DESC").fetchall()
    return [{"groupe": r["group_name"], "role": r["role"], "n": r["c"]} for r in rows[:limite]], len(rows)

def db_nmos_resource_get_by_bind(bind_instance_uuid, bind_slot, essence, kind):
    """Ressource bindée à un slot précis d'un conteneur (instance_uuid + slot + essence + kind)."""
    with get_db() as db:
        r = db.execute(
            "SELECT * FROM nmos_resources WHERE bind_instance_uuid=? AND bind_slot=? AND essence=? AND kind=?",
            (bind_instance_uuid, bind_slot, essence, kind)).fetchone()
        return _nmos_row(r) if r else None

def db_nmos_resource_upsert(id, kind, essence, label, group_name, role, transport,
                            bind_instance_uuid, bind_slot):
    """Insère/maj une ressource. transport = dict (sérialisé). À l'UPDATE, le binding et le transport
    sont rafraîchis (le slot servant peut changer de conteneur) ; l'id (UUID NMOS) reste stable.

    ★ `group_name` et `role` sont FIGÉS À LA PREMIÈRE ÉCRITURE (le premier qui écrit gagne). Ce sont
    les deux composantes du tag `grouphint`, dont le registre NMOS dit qu'il est une propriété
    **immuable** de la ressource — et il explique pourquoi : un contrôleur s'en sert au moment du
    paramétrage pour bâtir le nommage de production, et n'aura plus l'occasion de redemander à
    l'exploitant si la valeur change ensuite. Jusqu'ici cet UPDATE les réécrivait à chaque rebuild
    avec la valeur RECALCULÉE, laquelle dérive du préfixe de libellés : changer ce réglage
    d'affichage réécrivait silencieusement le grouping de tout le parc (BCP-002-01)."""
    # ★ REFUSER LA COLLISION AU MOMENT OÙ ELLE SE CRÉE (BCP-002-01, 2026-08-22).
    # Toutes les ressources vivent sous UN SEUL Device cluster, et la base d'un nom de groupe est
    # le préfixe de libellés du nœud — vide par défaut, donc le littéral « 2110 ». Un SECOND moteur
    # sur un autre nœud sans préfixe distinct émettrait le même `2110 01:video` : même scope, même
    # groupe, même rôle. Un contrôleur fusionnerait alors les Rx de deux nœuds dans un même
    # ensemble, et le MUST d'unicité du rôle dans un groupe tomberait.
    # Depuis que les grouphints sont FIGÉS, une telle collision serait DÉFINITIVE : elle ne se
    # corrigerait plus en posant un préfixe après coup. On la dérive donc à l'écriture, et on le
    # DIT — jamais un doublon en silence.
    if group_name and role:
        try:
            with get_db() as _db:
                _pris = _db.execute(
                    "SELECT id FROM nmos_resources WHERE group_name=? AND role=? AND id<>?",
                    (group_name, role, id)).fetchone()
            if _pris:
                _suffixe = (str(bind_instance_uuid or "")[:8] or str(id)[:8])
                _origine = group_name
                group_name = "%s %s" % (group_name, _suffixe)
                db_add_alert("alert.nmos.groupe_collision", "warning", kind="advisory",
                             params={"g": _origine, "r": role, "d": group_name})
        except Exception:
            pass          # un garde-fou ne doit jamais empêcher l'enregistrement lui-même
    with get_db() as db:
        db.execute('''INSERT INTO nmos_resources
            (id, kind, essence, label, group_name, role, transport, bind_instance_uuid, bind_slot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                group_name=CASE WHEN COALESCE(nmos_resources.group_name, '') = ''
                                THEN excluded.group_name ELSE nmos_resources.group_name END,
                role=CASE WHEN COALESCE(nmos_resources.role, '') = ''
                          THEN excluded.role ELSE nmos_resources.role END,
                transport=excluded.transport,
                bind_instance_uuid=excluded.bind_instance_uuid, bind_slot=excluded.bind_slot''',
            (id, kind, essence, label, group_name, role, json.dumps(transport or {}),
             bind_instance_uuid, bind_slot, datetime.now().isoformat()))
        db.commit()

def db_nmos_resource_import(id, kind, essence, label, group_name, role, transport, label_locked=0):
    """Import/rappel de config : insère/remplace une ressource en CONSERVANT son id (UUID NMOS) et
    son label_locked. Le binding (bind_instance_uuid/bind_slot) EXISTANT est préservé à l'UPDATE
    (re-câblage par les bindings/rebuild). Préserver l'UUID = ne pas casser le routage du contrôleur."""
    with get_db() as db:
        db.execute('''INSERT INTO nmos_resources
            (id, kind, essence, label, group_name, role, transport, bind_instance_uuid, bind_slot,
             created_at, label_locked)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind, essence=excluded.essence, label=excluded.label,
                group_name=excluded.group_name, role=excluded.role, transport=excluded.transport,
                label_locked=excluded.label_locked''',
            (id, kind, essence, label, group_name, role, json.dumps(transport or {}),
             datetime.now().isoformat(), int(bool(label_locked))))
        db.commit()

def db_nmos_resource_delete(id):
    with get_db() as db:
        db.execute("DELETE FROM nmos_resources WHERE id=?", (id,))
        db.commit()
    # Libère la réservation multicast associée (voir db_reserve_mcast) — sinon l'adresse reste
    # bloquée indéfiniment pour un owner_ref dont la ressource n'existe plus.
    try:
        db_release_mcast_owner(f"nmos:{id}")
    except Exception:
        pass

# ─── Snapshots nommés de config NMOS ───────────────────────────────────
def db_nmos_snapshot_save(name, payload):
    """Crée un snapshot nommé (payload = dict config). Retourne l'id."""
    with get_db() as db:
        cur = db.execute("INSERT INTO nmos_snapshots (name, payload, created_at) VALUES (?, ?, ?)",
                         (name, json.dumps(payload or {}), datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def db_nmos_snapshots_list():
    """Liste {id, name, created_at, resources} (sans le payload complet — léger pour l'UI)."""
    out = []
    with get_db() as db:
        for r in db.execute("SELECT id, name, payload, created_at FROM nmos_snapshots ORDER BY id DESC"):
            try:
                n = len((json.loads(r["payload"]) or {}).get("resources") or [])
            except Exception:
                n = 0
            out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"], "resources": n})
    return out

def db_nmos_snapshot_get(id):
    with get_db() as db:
        r = db.execute("SELECT id, name, payload, created_at FROM nmos_snapshots WHERE id=?", (id,)).fetchone()
    if not r:
        return None
    try:
        cfg = json.loads(r["payload"])
    except Exception:
        cfg = {}
    return {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "config": cfg}

def db_nmos_snapshot_delete(id):
    with get_db() as db:
        db.execute("DELETE FROM nmos_snapshots WHERE id=?", (id,))
        db.commit()

def db_nmos_resource_get(id):
    with get_db() as db:
        r = db.execute("SELECT * FROM nmos_resources WHERE id=?", (id,)).fetchone()
        return _nmos_row(r) if r else None

def db_nmos_resource_set_label(id, label):
    """C2b : relabel op — fige le libellé (label_locked=1) → préservé aux rebuilds suivants."""
    with get_db() as db:
        db.execute("UPDATE nmos_resources SET label=?, label_locked=1 WHERE id=?", (label, id))
        db.commit()

def db_nmos_resource_set_group(id, group_name, role):
    """Réécrit DÉLIBÉRÉMENT le grouping d'une ressource (BCP-002-01).

    `db_nmos_resource_upsert` fige group_name/role à la première écriture, précisément pour qu'un
    rebuild ne puisse plus les changer. Cette fonction est la porte de sortie assumée : elle sert à
    la normalisation unique des index (zéro de remplissage) au démarrage du service. Ne pas
    l'appeler depuis un chemin qui tourne à chaque rebuild — ce serait rouvrir le trou."""
    with get_db() as db:
        db.execute("UPDATE nmos_resources SET group_name=?, role=? WHERE id=?",
                   (group_name, role, id))
        db.commit()

def db_nmos_resource_create(kind, essence, label, group_name="", role="", transport=None, id=None):
    """C2b : création manuelle d'une ressource registre (réservation cluster, sans conteneur servant).
    UUID aléatoire (pas seedé sur un vmid) sauf si `id` fourni (permet à l'appelant de réserver une
    adresse multicast — owner_ref='nmos:<id>' — AVANT l'insertion, avec le même id), binding NULL →
    reste orpheline tant qu'aucun slot ne s'y bind (le binding auto se fait par instance_uuid+slot ;
    une ressource manuelle reste donc une réservation). label_locked=1 (libellé op). Retourne l'id."""
    import uuid as _uuid
    rid = id or str(_uuid.uuid4())
    with get_db() as db:
        db.execute('''INSERT INTO nmos_resources
            (id, kind, essence, label, group_name, role, transport, bind_instance_uuid, bind_slot,
             created_at, label_locked)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 1)''',
            (rid, kind, essence, label, group_name, role, json.dumps(transport or {}),
             datetime.now().isoformat()))
        db.commit()
    return rid

def db_nmos_resource_set_transport(id, transport):
    with get_db() as db:
        db.execute("UPDATE nmos_resources SET transport=? WHERE id=?",
                   (json.dumps(transport or {}), id))
        db.commit()

def db_nmos_resource_rebind(id, bind_instance_uuid, bind_slot):
    """C2b+ : ré-affecte le slot servant d'une ressource (rebinding explicite). NE touche PAS le
    transport (la ressource fait autorité — push-down). bind_instance_uuid/bind_slot None = délier."""
    with get_db() as db:
        db.execute("UPDATE nmos_resources SET bind_instance_uuid=?, bind_slot=? WHERE id=?",
                   (bind_instance_uuid, bind_slot, id))
        db.commit()

# Limite d'AFFICHAGE par défaut — délibérément DÉCOUPLÉE de la rétention de stockage. Les deux
# étaient la même constante : porter la rétention à 10 000 aurait fait rendre 10 000 lignes à la
# page Conteneurs (`db_get_alerts()` sans limite) et à l'API. On stocke long, on affiche court.
ALERTS_AFFICHAGE_DEFAUT = 1000


def _normaliser_recherche(s):
    """Casse + accents dépliés, pour une comparaison texte tolérante (« é » ~ « e »)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

# Nombre de paramètres liés qu'on s'autorise à ajouter pour le seul terme `msg_key IN (...)` —
# marge sous le plafond SQLite (999 par défaut) : le reste de la requête (message/msg_params/
# autres filtres) consomme aussi des `?`. Un dépassement est journalisé, jamais silencieux.
_MAX_CLES_RECHERCHE = 500

def _cles_alertes_matchant(q, lang):
    """Clés `alert.*` du catalogue `lang` dont la TRADUCTION contient `q` (insensible à la casse
    et aux accents). Coûte un parcours du catalogue — n'est appelé que si `q` est renseigné."""
    from . import i18n
    if not i18n._CATALOGS:
        i18n._load()
    qn = _normaliser_recherche(q)
    cles = [cle for cle in i18n._all_keys()
            if cle.startswith("alert.") and qn in _normaliser_recherche(i18n.t(cle, lang))]
    if len(cles) > _MAX_CLES_RECHERCHE:
        log.warning(
            "db_get_alerts: %d clés i18n matchent %r en %r, tronqué à %d (%d ignorées)",
            len(cles), q, lang, _MAX_CLES_RECHERCHE, len(cles) - _MAX_CLES_RECHERCHE)
        cles = cles[:_MAX_CLES_RECHERCHE]
    return cles

def db_get_alerts(q=None, niveau=None, limit=ALERTS_AFFICHAGE_DEFAUT, vmid=None, node_id=None,
                  kind=None, user=None, lang=None):
    """Filtres CONTEXTE (`vmid`/`node_id`/`kind`) en plus du texte/niveau. Ils ne portent que sur
    les alertes écrites AVEC contexte : les lignes anciennes ont ces colonnes à NULL et sont donc
    exclues d'un filtre — c'est voulu (pas de reprise rétroactive, cf. init_db).

    `q` cherche dans TROIS colonnes : `message` (forme canonique française + lignes non keyées),
    `msg_params` (paramètres bruts non traduits — hostname/vmid/nom de nœud, donc valables dans
    toutes les langues), et `msg_key` via les clés i18n dont la traduction dans `lang` (langue du
    lecteur, résolue via `i18n.current_lang()` si omise) contient `q` — sinon un lecteur en
    anglais qui cherche le mot qu'il voit à l'écran ne trouverait rien (le message stocké est en
    français). Ne coûte rien de plus qu'avant quand `q` est vide : pas de parcours de catalogue."""
    sql = "SELECT * FROM alerts"
    where, params = [], []
    if q:
        or_terms = ["message LIKE ?", "msg_params LIKE ?"]
        params.append(f"%{q}%")
        params.append(f"%{q}%")
        if lang is None:
            try:
                from . import i18n
                lang = i18n.current_lang()
            except Exception:
                lang = None
        if lang:
            # Un catalogue en défaut ne doit pas faire tomber la LISTE D'ALERTES : le tableau de
            # bord la relit toutes les 5 s, et une recherche dégradée (français seul) vaut
            # infiniment mieux qu'un 500 sur la page qui sert à voir ce qui ne va pas.
            try:
                cles = _cles_alertes_matchant(q, lang)
            except Exception:
                log.exception("db_get_alerts: sélection des clés i18n impossible — "
                              "recherche limitée au texte canonique")
                cles = []
            if cles:
                or_terms.append(f"msg_key IN ({','.join('?' * len(cles))})")
                params.extend(cles)
        where.append("(" + " OR ".join(or_terms) + ")")
    if niveau:
        where.append("niveau = ?")
        params.append(niveau)
    if vmid is not None:
        where.append("vmid = ?")
        params.append(int(vmid))
    if node_id is not None:
        where.append("node_id = ?")
        params.append(int(node_id))
    if kind:
        where.append("kind = ?")
        params.append(str(kind))
    if user:
        # « machine » = les actions SANS acteur (surveillance, réconciliation, watchdog) : c'est un
        # filtre légitime et fréquent — « qu'est-ce qui s'est fait tout seul ? ».
        if str(user).lower() == "machine":
            where.append("user IS NULL")
        else:
            where.append("user = ?")
            params.append(str(user))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]

def db_alertes_groupees(fenetre=1000):
    """Alertes de la fenêtre des `fenetre` plus récentes, GROUPÉES par message.

    Rend, par message distinct : la ligne complète de son occurrence LA PLUS RÉCENTE, plus
    `count` (nombre d'occurrences dans la fenêtre), `first_timestamp` (début de l'épisode) et
    `niveaux` (tous les niveaux vus pour ce message — l'appelant tranche lequel est le pire, car
    l'ordre des niveaux ne doit avoir qu'une seule définition, et elle vit côté appelant).

    Existe pour ne plus matérialiser 1000 lignes complètes en Python à chaque appel : la home
    poll cette agrégation toutes les 2 s et c'était devenu le premier poste de CPU du contrôleur
    (mesuré 2026-08-19). Le regroupement se fait là où sont les données. Fenêtre et sémantique
    identiques à l'ancien calcul Python — seul l'endroit du travail change."""
    # ⚠ LA CTE NE PROJETTE QUE CE QU'ELLE GROUPE. La première version faisait
    # `SELECT *` : SQLite matérialisait les 1000 lignes ENTIÈRES — `message` est
    # du texte libre, souvent long — avant d'en agréger 4 colonnes. On ne garde
    # dans la fenêtre que ce dont le GROUP BY a besoin, et on ne va chercher la
    # ligne complète que pour les gagnants (≈770 sur 1000, mais surtout : une
    # jointure par id au lieu d'un tri sur des lignes larges).
    # Mesuré sur 10 033 alertes : 9,1 ms → 4,8 ms, résultats vérifiés IDENTIQUES
    # (mêmes groupes, mêmes id, mêmes comptes, mêmes niveaux).
    sql = """
        WITH f AS (SELECT id, message, niveau, timestamp
                   FROM alerts ORDER BY id DESC LIMIT ?),
             g AS (SELECT message,
                          COUNT(*)                  AS _count,
                          MIN(timestamp)            AS _first_timestamp,
                          MAX(id)                   AS _last_id,
                          GROUP_CONCAT(DISTINCT niveau) AS _niveaux
                   FROM f GROUP BY message)
        SELECT a.*, g._count, g._first_timestamp, g._niveaux
        FROM g JOIN alerts a ON a.id = g._last_id
    """
    out = []
    with get_db() as db:
        for r in db.execute(sql, (int(fenetre),)).fetchall():
            d = dict(r)
            d["count"] = d.pop("_count", 1)
            d["first_timestamp"] = d.pop("_first_timestamp", None)
            d["niveaux"] = [x for x in (d.pop("_niveaux", "") or "").split(",") if x]
            d.pop("_last_id", None)
            out.append(d)
    return out


def db_alerts_count():
    """Nombre TOTAL d'alertes conservées. La home affichait `len()` d'une fenêtre plafonnée à
    1000 : sur une base qui en garde 10 000, elle annonçait « 1000 alerte(s) au total » — un
    plafond d'affichage lu comme un compte."""
    with get_db() as db:
        return int(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])


# ─── Projects ───────────────────────────────────────────────

def _parse_snapshot(row):
    d = dict(row)
    try:
        d["snapshot"] = json.loads(d.get("snapshot") or "[]")
    except Exception:
        d["snapshot"] = []
    return d

# Champs TSL 5.0, dans l'ordre où ils occupaient une base. Cette constante ne sert plus qu'à
# NOMMER les niveaux hérités : le « 3 » appartient désormais à TSL seul (cf. services/tsl).
_CHAMPS_TSL_HERITES = (("lh", "LH"), ("rh", "RH"), ("tt", "TT"))


def db_get_tally_levels():
    """Tous les niveaux, dans l'ordre d'affichage. `[{uuid, nom, num, owner_kind, owner_id}]`.

    ⚠ `id` NE SORT PAS. C'est le rowid interne de la table ; l'identité qu'on expose est `uuid`,
    et le numéro qu'on affiche est `num`. Les rendre tous les trois inviterait un appelant à citer
    le mauvais, et c'est précisément la confusion qu'on vient de défaire."""
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT uuid, nom, num, owner_kind, owner_id FROM tally_levels ORDER BY num, id")]


def db_get_tally_levels_of(owner_kind, owner_id):
    """UUID des niveaux que possède ce porteur, dans l'ordre d'affichage.

    Un porteur hérité en a UN — sa chaîne de destination. Un projet peut s'en voir ajouter
    d'autres (`db_add_tally_level`) : autant de chaînes qu'il en a, sans plafond, et c'est
    précisément ce que le pas de 3 de TSL interdisait."""
    if not owner_id:
        return []
    with get_db() as db:
        return [r[0] for r in db.execute(
            "SELECT uuid FROM tally_levels WHERE owner_kind=? AND owner_id=? ORDER BY num, id",
            (owner_kind, owner_id))]


def db_set_tally_level_nom(level_uuid, nom):
    with get_db() as db:
        db.execute("UPDATE tally_levels SET nom=? WHERE uuid=?", (str(nom or ""), str(level_uuid)))
        db.commit()


def db_set_tally_levels_order(uuids):
    """Réordonne les niveaux : la ligne bouge, ET son numéro avec. Renvoie le nombre replacé.

    ★ C'EST UNE ÉCRITURE D'UNE SEULE COLONNE, et c'est tout l'intérêt du modèle. `num` n'est
    qu'un rang d'affichage — aucune configuration ne le cite, aucun conteneur ne le connaît, car
    tous parlent en `uuid`. Renuméroter ne coûte donc rien et ne casse rien, aujourd'hui comme
    dans deux ans. Une version antérieure faisait porter l'identité par le numéro : il fallait
    alors choisir entre un numéro figé quand la ligne bouge (ce que personne n'appelle
    « réordonner ») et une réécriture de toutes les configurations du site.

    ★ LA LISTE ENTIÈRE, EN UNE FOIS. Un rang par requête laisserait, entre deux appels, deux
    niveaux au même numéro — donc un ordre départagé par le rowid, et un affichage qui saute
    pendant qu'on réorganise.

    Les UUID absents de la liste gardent leur rang relatif, APRÈS ceux qui y sont : un appelant
    qui n'a qu'une vue partielle ne peut pas faire disparaître du classement ce qu'il ne voyait
    pas."""
    voulu, vus = [], set()
    for x in (uuids or []):
        x = str(x or "").strip()
        if x and x not in vus:
            vus.add(x)
            voulu.append(x)
    with get_db() as db:
        connus = [r[0] for r in db.execute("SELECT uuid FROM tally_levels ORDER BY num, id")]
        voulu = [u for u in voulu if u in connus]
        reste = [u for u in connus if u not in vus]
        for rang, u in enumerate(voulu + reste, start=1):
            db.execute("UPDATE tally_levels SET num=? WHERE uuid=?", (rang, u))
        db.commit()
    return len(voulu)


def db_add_tally_level(nom, owner_kind=None, owner_id=None):
    """Ajoute un niveau à la SUITE. Renvoie son UUID — jamais son numéro."""
    import uuid as _uuid
    with get_db() as db:
        u = str(_uuid.uuid4())
        rang = (db.execute("SELECT COALESCE(MAX(num), 0) FROM tally_levels").fetchone()[0] or 0) + 1
        db.execute("INSERT INTO tally_levels (uuid, nom, num, owner_kind, owner_id) "
                   "VALUES (?,?,?,?,?)", (u, str(nom or ""), rang, owner_kind, owner_id))
        db.commit()
        return u


def db_delete_tally_level(level_uuid, con=None):
    """Supprime un niveau et RESSERRE les numéros. Renvoie True s'il existait.

    Les configurations qui le citaient gardent son UUID : elles pointent alors un niveau
    inexistant, ce que les lecteurs traitent déjà comme « aucun niveau ». On ne va PAS les
    nettoyer — un niveau supprimé par erreur se recrée, mais des références effacées ne se
    retrouvent pas.

    ⚠ `con` PERMET DE LA TESTER SANS TOUCHER À LA PRODUCTION — règle posée le 2026-09-01 après
    deux pertes de données : muter une fonction qui SUPPRIME exécute de vraies suppressions, et
    `get_db()` épingle le chemin de la base."""
    u = str(level_uuid or "")
    from contextlib import nullcontext
    with (nullcontext(con) if con is not None else get_db()) as db:
        cur = db.execute("DELETE FROM tally_levels WHERE uuid=?", (u,))
        db.execute("UPDATE tsl_connections SET level_uuid=NULL WHERE level_uuid=?", (u,))
        for rang, (v,) in enumerate(db.execute(
                "SELECT uuid FROM tally_levels ORDER BY num, id").fetchall(), start=1):
            db.execute("UPDATE tally_levels SET num=? WHERE uuid=?", (rang, v))
        db.commit()
        return cur.rowcount > 0


def _migrer_colonnes_libelles(db):
    """Pose le nombre de colonnes de libellé OFFERTES, une seule fois. Deux par défaut.

    ★ MAIS PAS DEUX POUR TOUT LE MONDE. Le produit proposait huit colonnes personnalisées
    d'office ; on passe à deux, extensibles au besoin. Appliquer ce défaut à une installation
    existante ferait disparaître de ses tableaux des colonnes qu'elle REMPLIT — silencieusement,
    et sans que le geste (une mise à jour) ait le moindre rapport apparent avec ses libellés.

    On regarde donc ce qui EXISTE : une colonne comptée est une colonne renommée, ou qui porte au
    moins un libellé. Le nombre initial couvre la plus haute des deux, et jamais moins de deux.

    ⚠ Les huit colonnes physiques restent : réduire l'affichage n'efface rien, et réaugmenter le
    nombre fait réapparaître les libellés intacts."""
    import json as _json
    if db.execute("SELECT COUNT(*) FROM settings WHERE key='label_cols_actives'").fetchone()[0]:
        return                                # déjà posé — l'idempotence tient à cette ligne
    haute = 2
    try:
        r = db.execute("SELECT value FROM settings WHERE key='tsl_label_names'").fetchone()
        noms = _json.loads(r[0]) if r and r[0] else []
        defauts = {2: "Label 2", 3: "Label 3", 4: "Label 4", 5: "Label 5",
                   6: "Label 6", 7: "Label 7", 8: "Label 8", 9: "Label 9"}
        for i in range(2, 10):
            if i < len(noms) and str(noms[i]).strip() not in ("", defauts[i]):
                haute = max(haute, i - 1)     # colonne i = la (i-1)ᵉ personnalisée
    except Exception:
        pass
    try:
        for i in range(2, 10):
            n = db.execute("SELECT COUNT(*) FROM source_labels "
                           "WHERE label_%d IS NOT NULL AND label_%d <> ''" % (i, i)).fetchone()[0]
            if n:
                haute = max(haute, i - 1)
    except Exception:
        pass
    haute = max(2, min(8, haute))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('label_cols_actives', ?)",
               (_json.dumps(haute),))
    log.info("libellés : %d colonne(s) personnalisée(s) offerte(s) — déduit de ce qui est "
             "renommé ou rempli (défaut 2 pour une installation neuve)", haute)


def _elaguer_niveaux_projets_dormants(db):
    """Retire les niveaux semés pour des productions qui ne tournent pas ET que rien ne cite.

    ★ POURQUOI. On semait un niveau à la CRÉATION de chaque projet. Un serveur qui stocke
    cinquante productions et n'en joue qu'une se retrouvait donc avec cinquante lignes dans le
    plan de tally, dont quarante-neuf ne servaient à rien et que rien ne permettait de rattacher à
    quoi que ce soit. Le semis se fait désormais à la RESTAURATION ; ceci nettoie ce qui a déjà
    été semé.

    ⚠ DEUX CONDITIONS, ET LES DEUX SONT NÉCESSAIRES. Un niveau CITÉ quelque part est conservé même
    si son projet dort : sa configuration le retrouvera au réveil. Un niveau d'un projet ACTIF est
    conservé même si rien ne le cite : il vient peut-être d'être créé. Ne retirer que
    l'intersection, c'est ne retirer que ce dont on est sûr.

    ⚠ ET UNE SEULE FOIS, jamais à chaque démarrage. Un élagage récurrent supprimerait le niveau
    qu'un exploitant vient de créer pour une production qu'il n'a pas encore lancée — au prochain
    redémarrage, sans un mot. Le drapeau `tally_elagage_dormants` marque que le ménage est fait."""
    if db.execute("SELECT COUNT(*) FROM settings WHERE key='tally_elagage_dormants'").fetchone()[0]:
        return
    cites = set()

    def _relever(v):
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and "-" in x:
                cites.add(x)
        return None                      # aucune réécriture : on ne fait que lire

    _reecrire_magasins(db, _relever)
    for (u,) in db.execute("SELECT level_uuid FROM tsl_connections "
                           "WHERE level_uuid IS NOT NULL").fetchall():
        cites.add(u)
    dormants = [r[0] for r in db.execute(
        "SELECT t.uuid FROM tally_levels t JOIN projects p ON p.id = t.owner_id "
        "WHERE t.owner_kind='project' AND COALESCE(p.state,'saved') NOT IN ('active','loading')"
    ).fetchall() if r[0] not in cites]
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tally_elagage_dormants', ?)",
               (json.dumps(True),))
    if not dormants:
        return
    db.executemany("DELETE FROM tally_levels WHERE uuid=?", [(u,) for u in dormants])
    for rang, (v,) in enumerate(db.execute(
            "SELECT uuid FROM tally_levels ORDER BY num, id").fetchall(), start=1):
        db.execute("UPDATE tally_levels SET num=? WHERE uuid=?", (rang, v))
    log.info("tally : %d niveau(x) de production dormante retiré(s) — ils étaient semés à la "
             "création du projet, ils le sont maintenant à sa restauration", len(dormants))


def _migrer_identite_niveaux(db):
    """Donne un UUID à chaque niveau, et BASCULE toutes les références dessus. Idempotente.

    ★ POURQUOI. Le modèle précédent faisait porter l'identité par le numéro affiché. Réordonner
    devenait alors impossible sans choisir entre deux mauvaises réponses : garder le numéro quand
    la ligne bouge (personne n'appelle ça réordonner), ou renuméroter et réécrire toutes les
    configurations du site. C'est le motif d'identité que le produit applique déjà aux conteneurs
    — `vmid` jetable d'un côté, `instance_uuid` de l'autre (cf. CLAUDE.md).

    ⚠ ET ELLE RATTRAPE UN OUBLI. La migration du dénouement ne réécrivait que `containers` :
    68 références de niveau dormaient dans les snapshots de projets, les versions et les
    dispositions (relevé le 2026-09-01). Un projet restauré réinjectait donc des numéros périmés,
    et le geste qui déclenche ça n'a aucun rapport apparent avec le tally. `_reecrire_magasins`
    couvre les six magasins."""
    import uuid as _uuid
    # ⚠ LA COLONNE D'ABORD, LA BASCULE ENSUITE — et jamais l'inverse.
    # `_elaguer_niveaux_projets_dormants`, appelée juste après dans `init_db`, LIT
    # `tsl_connections.level_uuid`. Cet ALTER vivait sous le `return` d'idempotence
    # ci-dessous : sur une base NEUVE il n'y a aucun niveau, donc rien à basculer, donc
    # on sortait avant de créer la colonne — et `init_db()` mourait sur
    # « no such column: level_uuid ». Invisible ici, où la base l'a déjà : le défaut ne
    # frappait QUE l'installation neuve, c'est-à-dire le premier geste d'un nouveau venu.
    cols = {r[1] for r in db.execute("PRAGMA table_info(tsl_connections)").fetchall()}
    if "level_uuid" not in cols:
        db.execute("ALTER TABLE tsl_connections ADD COLUMN level_uuid TEXT")

    manquants = db.execute("SELECT id FROM tally_levels WHERE uuid IS NULL OR uuid=''").fetchall()
    if not manquants:
        return                                # déjà basculée — l'idempotence tient à cette ligne
    ancien_vers_uuid = {}
    for (rowid,) in manquants:
        u = str(_uuid.uuid4())
        db.execute("UPDATE tally_levels SET uuid=? WHERE id=?", (u, rowid))
        ancien_vers_uuid[rowid] = u
    # Les références en base citaient le rowid ; elles citent maintenant l'UUID.
    cols = {r[1] for r in db.execute("PRAGMA table_info(tsl_connections)").fetchall()}
    if "level_id" in cols:
        for cid, lid in db.execute("SELECT id, level_id FROM tsl_connections "
                                   "WHERE level_id IS NOT NULL").fetchall():
            db.execute("UPDATE tsl_connections SET level_uuid=? WHERE id=?",
                       (ancien_vers_uuid.get(lid), cid))

    def _vers_uuid(v):
        """Un entier — ou une liste d'entiers — devient une liste d'UUID. Une valeur déjà en
        UUID est laissée telle quelle : la migration doit pouvoir se relire."""
        vals = v if isinstance(v, list) else ([] if v in (None, "", 0, "0") else [v])
        out = []
        for x in vals:
            if isinstance(x, str) and "-" in x:
                out.append(x)                 # déjà un UUID
                continue
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n and ancien_vers_uuid.get(n):
                out.append(ancien_vers_uuid[n])
        return out

    lignes = _reecrire_magasins(db, _vers_uuid)
    log.info("tally : %d niveau(x) ont reçu leur UUID, %d ligne(s) de configuration basculée(s) "
             "(le numéro n'est plus qu'un rang d'affichage)", len(ancien_vers_uuid), lignes)


def _migrer_niveaux_tally(db):
    """Aplatit les bases de tally en NIVEAUX nommés, numérotés 1..N. Idempotente.

    ★ POURQUOI. Le « pas de 3 » de `_next_tally_base` venait du mot de contrôle TSL 5.0, qui
    réserve deux bits pour chacun de ses trois champs (LH/RH/TT). Ce format de trame avait fui
    dans le modèle interne : une production ne pouvait avoir que TROIS chaînes de destination —
    non pas parce que c'est sensé, mais parce que TSL en a trois. IS-07 n'a pas cette contrainte.

    Après migration, un niveau est une entité nommée. Chaque protocole le PROJETTE sur son propre
    format : TSL en consomme trois par index, IS-07 autant qu'il en faut.

    ⚠ Les colonnes `tally_base` sont CONSERVÉES, et c'est délibéré : SQLite ne supprime pas une
    colonne simplement, et elles restent le seul moyen de relire cette migration si elle devait
    l'être. Plus personne ne les lit — un banc le vérifie."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(tsl_connections)").fetchall()}
    if "level_id" not in cols:
        db.execute("ALTER TABLE tsl_connections ADD COLUMN level_id INTEGER "
                   "REFERENCES tally_levels(id) ON DELETE SET NULL")
    # ★ DEUX BARREAUX, comme pour un conteneur (cf. CLAUDE.md « Identité d'un conteneur ») :
    #   · `uuid` — L'IDENTITÉ. C'est elle, et elle seule, que citent les configurations, les
    #     conteneurs et (demain) les Sources IS-07. Elle ne bouge JAMAIS.
    #   · `num`  — le RANG VISUEL, 1..N. Purement d'affichage : réordonner le réécrit librement,
    #     et rien d'autre n'en dépend.
    # C'est ce qui permet de réordonner pour de bon — la ligne ET son numéro bougent — sans que
    # ce soit une migration. La version précédente confondait les deux dans `id`, ce qui laissait
    # le choix entre un numéro figé (inutilisable) et une renumérotation qui réécrit toutes les
    # configurations du site (une migration déguisée en réglage).
    db.execute("""CREATE TABLE IF NOT EXISTS tally_levels (
        id         INTEGER PRIMARY KEY,       -- rowid interne : ne SORT jamais de cette table
        uuid       TEXT    NOT NULL DEFAULT '',
        nom        TEXT    NOT NULL DEFAULT '',
        num        INTEGER NOT NULL DEFAULT 0,
        owner_kind TEXT,                      -- 'project' | 'connection' | NULL
        owner_id   INTEGER
    )""")
    _tcols = {r[1] for r in db.execute("PRAGMA table_info(tally_levels)").fetchall()}
    if "uuid" not in _tcols:
        db.execute("ALTER TABLE tally_levels ADD COLUMN uuid TEXT NOT NULL DEFAULT ''")
    if "num" not in _tcols:
        # `ord` portait déjà le rang : on le reprend tel quel, l'ordre affiché ne bouge pas.
        db.execute("ALTER TABLE tally_levels ADD COLUMN num INTEGER NOT NULL DEFAULT 0")
        if "ord" in _tcols:
            db.execute("UPDATE tally_levels SET num = ord")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tally_levels_uuid "
               "ON tally_levels(uuid) WHERE uuid <> ''")
    # ⚠ RE-SEMIS D'UNE PREMIÈRE VERSION FAUSSE. Elle créait TROIS niveaux par porteur, un par
    # champ TSL — c'était transposer `base+0/1/2` au lieu de s'en défaire, donc rater le
    # dénouement. Un porteur est UNE chaîne de destination : ses trois champs sont trois façons
    # d'exprimer SON état, pas trois chaînes. La preuve était dans le code : `rouge_field` et
    # `vert_field` désignent lequel des trois porte le rouge et lequel le vert POUR UNE MÊME
    # connexion, et le troisième n'est jamais lu.
    # ⚠⚠ L'IDEMPOTENCE TIENT À UN MARQUEUR, PLUS À LA FORME DES DONNÉES. La version précédente
    # déduisait « déjà migrée » de « aucun porteur n'a plus d'un niveau » — et re-semait sinon,
    # ce qui commence par un `DELETE FROM tally_levels`. Or **une production peut légitimement
    # avoir plusieurs niveaux** : c'est la capacité qu'on a ouverte en dénouant (autant de
    # chaînes de destination qu'elle en a). Le jour où quelqu'un s'en sert, la migration se croit
    # à refaire et EFFACE TOUS LES NIVEAUX au démarrage suivant, renommages compris, pour les
    # remplacer par ceux qu'on déduit des vieilles colonnes `tally_base`.
    #
    # Signalé par l'utilisateur le 2026-09-01 (« quand on supprime un niveau, ça supprime tous les
    # niveaux ») : ce n'était pas la suppression, c'était le redémarrage qui suivait.
    #
    # ★ RÈGLE GÉNÉRALE : une garde d'idempotence ne doit jamais INFÉRER l'état d'une forme de
    # données que le modèle autorise. Elle se pose explicitement, et une seule fois.
    _fait = db.execute("SELECT COUNT(*) FROM settings WHERE key='tally_denouement_fait'") \
              .fetchone()[0]
    if _fait:
        return
    if db.execute("SELECT COUNT(*) FROM tally_levels").fetchone()[0]:
        # ⚠⚠ DES NIVEAUX EXISTENT : ON NE TOUCHE À RIEN, ON POSE LE MARQUEUR.
        #
        # Cette fonction commence par un `DELETE FROM tally_levels`. Une version précédente
        # décidait de re-semer d'après la FORME DES DONNÉES — « un porteur a plus d'un niveau,
        # donc la base est restée au modèle à trois par porteur ». Or **une production peut
        # légitimement avoir plusieurs niveaux** : c'est la capacité qu'on a ouverte en dénouant.
        # Le jour où quelqu'un s'en sert, le démarrage suivant efface TOUS les niveaux,
        # renommages compris, et les remplace par ceux déduits des vieilles colonnes `tally_base`.
        # Signalé par l'utilisateur le 2026-09-01 (« quand on supprime un niveau, ça supprime tous
        # les niveaux ») : ce n'était pas la suppression, c'était le redémarrage qui suivait.
        #
        # Le re-semis corrigeait une première version qui n'est jamais sortie de cette machine. Il
        # est RETIRÉ : plus aucun chemin ne peut détruire des niveaux au démarrage.
        #
        # ★ RÈGLE : une garde d'idempotence ne doit jamais INFÉRER l'état d'une forme de données
        # que le modèle autorise. Elle se pose explicitement, et une seule fois.
        db.execute("INSERT OR REPLACE INTO settings (key, value) "
                   "VALUES ('tally_denouement_fait', ?)", (json.dumps(True),))
        return

    # Porteurs historiques, dans l'ordre de leur base. Un même niveau absolu ne peut appartenir
    # qu'à UN porteur : en cas de chevauchement (jamais vu, mais rien ne l'interdisait), le
    # premier par base gagne et on le DIT plutôt que d'en perdre un en silence.
    porteurs = []
    for r in db.execute("SELECT id, name, tally_base FROM tsl_connections "
                        "WHERE tally_base IS NOT NULL").fetchall():
        porteurs.append((int(r[2]), "connection", r[0], r[1] or ("TSL #%s" % r[0])))
    for r in db.execute("SELECT id, name, tally_base FROM projects "
                        "WHERE tally_base IS NOT NULL").fetchall():
        porteurs.append((int(r[2]), "project", r[0], r[1] or ("Projet #%s" % r[0])))
    porteurs.sort()

    # UN niveau par porteur. La table de correspondance associe chacun des trois anciens numéros
    # (base, base+1, base+2) au MÊME nouveau niveau : une référence héritée, quel que soit le
    # champ qu'elle visait, désigne désormais la chaîne entière.
    ancien_vers_neuf = {}
    for neuf, (base, genre, oid, nom) in enumerate(porteurs, start=1):
        db.execute("INSERT INTO tally_levels (id, nom, num, owner_kind, owner_id) "
                   "VALUES (?,?,?,?,?)", (neuf, nom, neuf, genre, oid))
        for i in range(len(_CHAMPS_TSL_HERITES)):
            ancien_vers_neuf.setdefault(base + i, neuf)
        if genre == "connection":
            db.execute("UPDATE tsl_connections SET level_id=? WHERE id=?", (neuf, oid))

    _reecrire_niveaux_plugins(db, ancien_vers_neuf)
    db.execute("INSERT OR REPLACE INTO settings (key, value) "
               "VALUES ('tally_denouement_fait', ?)", (json.dumps(True),))
    log.info("tally : %d porteurs → %d niveaux (1..%d) — un par chaîne de destination, "
             "le pas de 3 de TSL a disparu", len(porteurs), len(porteurs), len(porteurs))


# Paramètres de plugin qui portaient un numéro de niveau absolu. Ils deviennent des LISTES : la
# sélection d'un plugin est un ENSEMBLE de niveaux combinés en OU, dont « un seul » n'est que le
# cas à un élément (cf. TODO.md § TALLY). Les faire en deux temps voudrait dire toucher deux fois
# la même configuration.
_PARAMS_NIVEAU = ("tally_level", "tally_level_a", "tally_level_b", "tally_level_base")

# ★ TOUS LES MAGASINS QUI GARDENT UNE COPIE DE CONFIGURATION, pas seulement les conteneurs vivants.
# Un projet restauré, une version rappelée ou une disposition rechargée réinjectent des paramètres
# écrits AVANT : s'ils portent des numéros de niveau périmés, le tally repart sur le mauvais
# signal, et le geste qui déclenche ça (restaurer un projet) n'a aucun rapport apparent avec le
# tally. Relevé le 2026-09-01 : 68 références dormaient hors de `containers`.
_MAGASINS_NIVEAU = (("containers", "deploy_config", "vmid"),
                    ("projects", "snapshot", "id"),
                    ("project_versions", "snapshot", "id"),
                    ("layouts", "config", "id"),
                    ("dve_memories", "config", "id"),
                    ("pip_templates", "config", "id"))


def _parcourir_niveaux(obj, transforme):
    """Applique `transforme` à toute valeur portée par une clé de `_PARAMS_NIVEAU`, à n'importe
    quelle profondeur. Renvoie True si quelque chose a changé.

    ★ RÉCURSIF, ET C'EST LA SEULE FAÇON. Ces clés vivent à plat dans `params`, mais aussi dans
    `params.flux_config[]` (les tuiles d'un mur) et `params.overlays[]` (ses incrustations) —
    et un snapshot de projet les enfouit d'un niveau de plus. Une conversion à plat paraît finie
    et ne l'est pas : c'est exactement le reste qu'on a payé au dénouement."""
    change = False
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in _PARAMS_NIVEAU:
                neuf = transforme(v)
                if neuf is not None and neuf != v:
                    obj[k] = neuf
                    change = True
            elif _parcourir_niveaux(v, transforme):
                change = True
    elif isinstance(obj, list):
        for v in obj:
            if _parcourir_niveaux(v, transforme):
                change = True
    return change


def _reecrire_magasins(db, transforme):
    """Applique `transforme` à tous les magasins de `_MAGASINS_NIVEAU`. Renvoie le nombre de
    lignes réécrites. Une table absente est ignorée : le schéma varie d'une installation à
    l'autre, et on ne fait pas échouer un démarrage pour une table optionnelle."""
    import json as _json
    touchees = 0
    for table, colonne, cle in _MAGASINS_NIVEAU:
        try:
            lignes = db.execute("SELECT %s, %s FROM %s" % (cle, colonne, table)).fetchall()
        except Exception:
            continue
        for ident, brut in lignes:
            if not brut:
                continue
            try:
                doc = _json.loads(brut)
            except (ValueError, TypeError):
                continue
            if _parcourir_niveaux(doc, transforme):
                db.execute("UPDATE %s SET %s=? WHERE %s=?" % (table, colonne, cle),
                           (_json.dumps(doc), ident))
                touchees += 1
    return touchees


def _reecrire_niveaux_plugins(db, table):
    """Convertit les paramètres de niveau des conteneurs en listes de nouveaux identifiants."""
    if not table:
        return
    import json as _json
    for vmid, dc in db.execute("SELECT vmid, deploy_config FROM containers").fetchall():
        if not dc:
            continue
        try:
            cfg = _json.loads(dc)
        except Exception:
            continue
        params = cfg.get("params") or {}
        change = False
        for cle in _PARAMS_NIVEAU:
            if cle not in params:
                continue
            v = params[cle]
            if isinstance(v, list):          # déjà migré
                continue
            try:
                ancien = int(v)
            except (TypeError, ValueError):
                continue
            neuf = table.get(ancien)
            # Un niveau absent de la table venait d'un porteur disparu : on vide plutôt que de
            # pointer un niveau qui appartient désormais à quelqu'un d'autre.
            params[cle] = [neuf] if neuf else []
            change = True
        # ★ TROISIÈME FUITE DU « 3 », et la plus cachée : `flux_config[].tally_level` du multiview
        # n'est pas un niveau mais un NUMÉRO DE BANDE 1-based, que le distributeur reconvertissait
        # en `(niveau-1)*3`. Elle est imbriquée dans les paramètres, donc invisible d'une
        # conversion à plat — c'est exactement le genre de reste qui fait qu'une migration paraît
        # finie et ne l'est pas.
        for fc in (params.get("flux_config") or []):
            if not isinstance(fc, dict) or "tally_level" not in fc:
                continue
            v = fc["tally_level"]
            if isinstance(v, list):
                continue
            try:
                bande = int(v)
            except (TypeError, ValueError):
                continue
            if bande <= 0:
                fc["tally_level"] = []          # « niveau du projet » : résolu à l'exécution
            else:
                niv = table.get((bande - 1) * 3)
                fc["tally_level"] = [niv] if niv else []
            change = True

        if change:
            cfg["params"] = params
            db.execute("UPDATE containers SET deploy_config=? WHERE vmid=?",
                       (_json.dumps(cfg), vmid))


def _next_tally_base(db):
    """Prochaine base de tally libre (pas de 3 : LH/RH/TT), en évitant les bases des
    autres projets ET celles des connexions TSL configurées à la main.

    ⚠ HÉRITÉ, ET PLUS LU PAR PERSONNE. La colonne `tally_base` est encore alimentée pour qu'une
    base restaurée d'avant le dénouement reste relisable, mais ce n'est plus elle qui décide d'un
    niveau : c'est `tally_levels`, et un projet neuf en reçoit un par `_semer_niveau_projet`."""
    used = {r[0] for r in db.execute(
        "SELECT tally_base FROM projects WHERE tally_base IS NOT NULL").fetchall()}
    try:
        used |= {r[0] for r in db.execute("SELECT tally_base FROM tsl_connections").fetchall()}
    except Exception:
        pass
    nxt = 0
    while nxt in used:
        nxt += 3
    return nxt


def db_assurer_niveau_projet(pid, nom=None):
    """Garantit qu'une production a son niveau de tally. Renvoie son UUID. Idempotente.

    ★ AU MOMENT OÙ ELLE TOURNE, PAS QUAND ON L'ENREGISTRE. Un serveur peut stocker cinquante
    projets et n'en jouer qu'un : leur donner à tous un niveau à la création remplit le plan de
    lignes que rien ne sert et que personne ne peut relier à quoi que ce soit. On sème donc à la
    RESTAURATION — le moment où la production existe pour de bon.

    ★ MAIS ON NE RETIRE JAMAIS. Une fois le niveau créé, il reste : les configurations des
    conteneurs de ce projet citent son UUID, et un projet rangé puis rejoué doit les retrouver
    intactes. Ranger n'est pas supprimer.

    ⚠ SANS ÇA, UNE PRODUCTION EST MUETTE SANS LE DIRE : le mélangeur se replie sur les niveaux de
    sa production, et une liste vide ne produit aucune erreur — seulement l'absence de tally."""
    with get_db() as db:
        deja = db.execute("SELECT uuid FROM tally_levels WHERE owner_kind='project' AND owner_id=?",
                          (pid,)).fetchone()
        if deja:
            return deja[0]
        if nom is None:
            r = db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
            nom = (r[0] if r else None) or ("Production %d" % pid)
        return _semer_niveau_projet(db, pid, nom)


def _semer_niveau_projet(db, pid, nom):
    """Crée le niveau d'une production. Appelée par `db_assurer_niveau_projet` — jamais seule."""
    import uuid as _uuid
    u = str(_uuid.uuid4())
    rang = (db.execute("SELECT COALESCE(MAX(num), 0) FROM tally_levels").fetchone()[0] or 0) + 1
    db.execute("INSERT INTO tally_levels (uuid, nom, num, owner_kind, owner_id) VALUES (?,?,?,?,?)",
               (u, str(nom or "Production %d" % pid), rang, "project", pid))
    return u


def db_snapshot_for_vmids(vmids):
    """Construit un snapshot de projet à partir de containers live (extrait de
    db_save_project — aussi utilisé par le re-snapshot auto « projet vivant »)."""
    if not vmids:
        return []
    placeholders = ",".join("?" * len(vmids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT vmid, hostname, cores, memory, script, deploy_config, monitor_user_id, "
            f"node_id, instance_uuid "
            f"FROM containers WHERE vmid IN ({placeholders})",
            list(vmids)
        ).fetchall()
    snapshot = []
    for r in sorted(rows, key=lambda x: x["vmid"]):
        c = dict(r)
        # Containers de monitoring (un streamer par utilisateur, hostname monitor-u<uid>) :
        # infra par-utilisateur éphémère, jamais clonée dans un projet → exclus.
        if c.get("monitor_user_id"):
            continue
        c.pop("monitor_user_id", None)
        if c.get("deploy_config"):
            try:
                c["deploy_config"] = json.loads(c["deploy_config"])
            except Exception:
                c["deploy_config"] = None
        # Mémoires par-container (plugin_store scopé sur le vmid) : DVE, multiview,
        # presets… embarquées dans le projet pour être restaurées avec le container.
        c["memories"] = plugin_store_list_scope(str(c.get("vmid")))
        snapshot.append(c)
    return snapshot

def db_save_project(name, vmids, media_path=None):
    if not vmids:
        return None
    snapshot = db_snapshot_for_vmids(vmids)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (name, created_at, snapshot, media_path, tally_base) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, datetime.now().isoformat(timespec="seconds"), json.dumps(snapshot),
             media_path, _next_tally_base(db))
        )
        db.commit()
        return cur.lastrowid

# ─── Versions de projet (« projet vivant », chantier 3) ───────

AUTO_VERSIONS_KEEP = 30   # rétention des versions automatiques (label NULL) par projet

def db_update_project_snapshot(pid, snapshot):
    with get_db() as db:
        db.execute("UPDATE projects SET snapshot=? WHERE id=?",
                   (json.dumps(snapshot), pid))
        db.commit()

def db_add_project_version(pid, snapshot, label=None):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO project_versions (project_id, created_at, label, snapshot) "
            "VALUES (?,?,?,?)",
            (pid, datetime.now().isoformat(timespec="seconds"), label,
             json.dumps(snapshot)))
        # Rétention : les versions AUTO au-delà de AUTO_VERSIONS_KEEP sont purgées ;
        # les versions nommées sont conservées sans limite.
        db.execute(
            "DELETE FROM project_versions WHERE project_id=? AND label IS NULL AND id NOT IN "
            "(SELECT id FROM project_versions WHERE project_id=? AND label IS NULL "
            " ORDER BY id DESC LIMIT ?)",
            (pid, pid, AUTO_VERSIONS_KEEP))
        db.commit()
        return cur.lastrowid

def db_project_versions(pid):
    """Liste (sans payload) : id, created_at, label, nb de containers."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, created_at, label, snapshot FROM project_versions "
            "WHERE project_id=? ORDER BY id DESC", (pid,)).fetchall()
    out = []
    for r in rows:
        try:
            n = len(json.loads(r["snapshot"] or "[]"))
        except Exception:
            n = 0
        out.append({"id": r["id"], "created_at": r["created_at"],
                    "label": r["label"], "containers": n})
    return out

def db_get_project_version(vid):
    with get_db() as db:
        r = db.execute("SELECT * FROM project_versions WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    v = dict(r)
    try:
        v["snapshot"] = json.loads(v.get("snapshot") or "[]")
    except Exception:
        v["snapshot"] = []
    return v

def db_delete_project_version(vid):
    with get_db() as db:
        db.execute("DELETE FROM project_versions WHERE id=?", (vid,))
        db.commit()

# ─── Macros + variables de projet (chantier 6) ────────────────

def _parse_macro(r):
    m = dict(r)
    try:
        m["graph"] = json.loads(m.get("graph") or "{}")
    except (TypeError, ValueError):
        m["graph"] = {}
    try:
        pub = json.loads(m.get("published_to") or "[]")
        m["published_to"] = pub if isinstance(pub, list) else []
    except (TypeError, ValueError):
        m["published_to"] = []
    return m

def db_project_macros(pid):
    with get_db() as db:
        return [_parse_macro(r) for r in db.execute(
            "SELECT * FROM project_macros WHERE project_id=? ORDER BY name",
            (pid,)).fetchall()]

def db_system_macros():
    """Macros SYSTÈME (inter-projets, admin) : project_id IS NULL."""
    with get_db() as db:
        return [_parse_macro(r) for r in db.execute(
            "SELECT * FROM project_macros WHERE project_id IS NULL ORDER BY name"
        ).fetchall()]

def db_macros_published_to(pid):
    """Macros système PUBLIÉES vers ce projet (bouton opaque, cf. docs/reference/PROJETS.md §7)."""
    return [m for m in db_system_macros() if pid in (m.get("published_to") or [])]

def db_get_macro(mid):
    with get_db() as db:
        r = db.execute("SELECT * FROM project_macros WHERE id=?", (mid,)).fetchone()
        return _parse_macro(r) if r else None

def db_create_macro(pid, name, owner_id, graph=None):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO project_macros (project_id, name, owner_id, graph, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (pid, name, owner_id, json.dumps(graph or {"format": "blocks/v1", "steps": []}),
             now, now))
        db.commit()
        return cur.lastrowid

def db_update_macro(mid, name=None, graph=None, published_to=None):
    with get_db() as db:
        if name is not None:
            db.execute("UPDATE project_macros SET name=? WHERE id=?", (name, mid))
        if graph is not None:
            db.execute("UPDATE project_macros SET graph=? WHERE id=?",
                       (json.dumps(graph), mid))
        if published_to is not None:
            db.execute("UPDATE project_macros SET published_to=? WHERE id=?",
                       (json.dumps(list(published_to)), mid))
        db.execute("UPDATE project_macros SET updated_at=? WHERE id=?",
                   (datetime.now().isoformat(timespec="seconds"), mid))
        db.commit()

def db_delete_macro(mid):
    with get_db() as db:
        db.execute("DELETE FROM project_macros WHERE id=?", (mid,))
        db.commit()

def db_project_vars(pid):
    with get_db() as db:
        return {r["name"]: r["value"] for r in db.execute(
            "SELECT name, value FROM project_vars WHERE project_id=?", (pid,)).fetchall()}

def db_set_project_var(pid, name, value):
    with get_db() as db:
        db.execute("INSERT INTO project_vars (project_id, name, value) VALUES (?,?,?) "
                   "ON CONFLICT(project_id, name) DO UPDATE SET value=excluded.value",
                   (pid, name, None if value is None else str(value)))
        db.commit()

# ─── Déclencheurs permanents (chantier 6 suite) ───────────────

def _parse_trigger(r):
    t = dict(r)
    try:
        t["condition"] = json.loads(t.get("condition") or "{}")
    except (TypeError, ValueError):
        t["condition"] = {}
    return t

def db_project_triggers(pid):
    with get_db() as db:
        return [_parse_trigger(r) for r in db.execute(
            "SELECT * FROM project_triggers WHERE project_id=? ORDER BY id",
            (pid,)).fetchall()]

def db_all_enabled_triggers():
    """Tous les déclencheurs actifs (tous projets) — lus par le poller (app/macros.py)."""
    with get_db() as db:
        return [_parse_trigger(r) for r in db.execute(
            "SELECT * FROM project_triggers WHERE enabled=1 ORDER BY id").fetchall()]

def db_get_trigger(tid):
    with get_db() as db:
        r = db.execute("SELECT * FROM project_triggers WHERE id=?", (tid,)).fetchone()
        return _parse_trigger(r) if r else None

def db_create_trigger(pid, name=None, condition=None, macro_id=None,
                      cooldown_ms=2000, enabled=0):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO project_triggers (project_id, name, enabled, condition, "
            "macro_id, cooldown_ms, created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, name, 1 if enabled else 0, json.dumps(condition or {}), macro_id,
             int(cooldown_ms or 0), datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def db_update_trigger(tid, name=None, enabled=None, condition=None,
                      macro_id=..., cooldown_ms=None):
    """Mise à jour partielle (None = inchangé ; macro_id accepte None → sentinelle ...)."""
    with get_db() as db:
        if name is not None:
            db.execute("UPDATE project_triggers SET name=? WHERE id=?", (name, tid))
        if enabled is not None:
            db.execute("UPDATE project_triggers SET enabled=? WHERE id=?",
                       (1 if enabled else 0, tid))
        if condition is not None:
            db.execute("UPDATE project_triggers SET condition=? WHERE id=?",
                       (json.dumps(condition), tid))
        if macro_id is not ...:
            db.execute("UPDATE project_triggers SET macro_id=? WHERE id=?", (macro_id, tid))
        if cooldown_ms is not None:
            db.execute("UPDATE project_triggers SET cooldown_ms=? WHERE id=?",
                       (int(cooldown_ms), tid))
        db.commit()

def db_delete_trigger(tid):
    with get_db() as db:
        db.execute("DELETE FROM project_triggers WHERE id=?", (tid,))
        db.commit()

# ─── Ports virtuels de projet (chantier 4) ────────────────────

def _parse_port(r):
    p = dict(r)
    for f in ("channel_labels", "binding"):
        try:
            p[f] = json.loads(p.get(f) or "null")
        except (TypeError, ValueError):
            p[f] = None
    return p

def db_project_ports(pid=None):
    """Ports d'un projet, ou de TOUS les projets (pid=None — vue d'ensemble Câbles/TSL)."""
    with get_db() as db:
        if pid is None:
            rows = db.execute("SELECT * FROM project_ports ORDER BY project_id, kind, ord, id").fetchall()
        else:
            rows = db.execute("SELECT * FROM project_ports WHERE project_id=? "
                              "ORDER BY kind, ord, id", (pid,)).fetchall()
        return [_parse_port(r) for r in rows]

def db_get_port(port_id):
    with get_db() as db:
        r = db.execute("SELECT * FROM project_ports WHERE id=?", (port_id,)).fetchone()
        return _parse_port(r) if r else None

def db_create_port(pid, kind, media, name, ord_=0, channel_labels=None):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO project_ports (project_id, kind, media, name, ord, "
            "channel_labels, created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, kind, media, name, ord_,
             json.dumps(channel_labels) if channel_labels is not None else None,
             datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid

def db_update_port(port_id, name=None, ord_=None, channel_labels=None, binding=None):
    with get_db() as db:
        if name is not None:
            db.execute("UPDATE project_ports SET name=? WHERE id=?", (name, port_id))
        if ord_ is not None:
            db.execute("UPDATE project_ports SET ord=? WHERE id=?", (ord_, port_id))
        if channel_labels is not None:
            db.execute("UPDATE project_ports SET channel_labels=? WHERE id=?",
                       (json.dumps(channel_labels), port_id))
        if binding is not None:
            db.execute("UPDATE project_ports SET binding=? WHERE id=?",
                       (json.dumps(binding), port_id))
        db.commit()

def db_delete_port(port_id):
    with get_db() as db:
        db.execute("DELETE FROM project_ports WHERE id=?", (port_id,))
        db.commit()

def db_import_project(name, snapshot, media_path=None):
    """Crée un projet à partir d'un snapshot déjà constitué (import fichier)."""
    if not name or not isinstance(snapshot, list):
        return None
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (name, created_at, snapshot, media_path, tally_base) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, datetime.now().isoformat(timespec="seconds"), json.dumps(snapshot),
             media_path, _next_tally_base(db))
        )
        db.commit()
        return cur.lastrowid

def db_set_project_state(pid, state):
    with get_db() as db:
        db.execute("UPDATE projects SET state=? WHERE id=?", (state, pid))
        db.commit()

def db_set_project_media_path(pid, media_path):
    with get_db() as db:
        db.execute("UPDATE projects SET media_path=? WHERE id=?", (media_path, pid))
        db.commit()

@cache_requete
def db_get_projects():
    with get_db() as db:
        return [_parse_snapshot(r) for r in
                db.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()]

def db_get_project(pid):
    with get_db() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return _parse_snapshot(row) if row else None

def db_delete_project(pid):
    with get_db() as db:
        db.execute("DELETE FROM projects WHERE id=?", (pid,))
        db.execute("DELETE FROM project_members WHERE project_id=?", (pid,))
        db.commit()

# ─── Membres de projet (rôles par projet : owner|editor|operator|viewer) ──

def db_project_members(pid):
    """Membres d'un projet, enrichis de l'identité utilisateur."""
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT m.user_id, m.role, u.username, u.prenom, u.nom "
            "FROM project_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.project_id=? ORDER BY u.username", (pid,)).fetchall()]

def db_set_project_member(pid, uid, role):
    with get_db() as db:
        db.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES (?,?,?) "
            "ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role",
            (pid, uid, role))
        db.commit()

def db_remove_project_member(pid, uid):
    with get_db() as db:
        db.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?",
                   (pid, uid))
        db.commit()

def db_user_projects(uid):
    """Projets dont l'utilisateur est membre, avec son rôle (snapshot non parsé)."""
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT p.id, p.name, p.created_at, p.media_path, p.owner_id, m.role "
            "FROM project_members m JOIN projects p ON p.id = m.project_id "
            "WHERE m.user_id=? ORDER BY p.name", (uid,)).fetchall()]

def db_project_role(pid, uid):
    """Rôle du membre dans le projet, ou None s'il n'en est pas membre."""
    with get_db() as db:
        r = db.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
            (pid, uid)).fetchone()
        return r["role"] if r else None

# ─── Vues composées d'un projet (chantier 2) ──────────────────

def _parse_view(r):
    v = dict(r)
    try:
        v["layout"] = json.loads(v.get("layout") or "[]")
    except (TypeError, ValueError):
        v["layout"] = []
    v["edit_shared"] = bool(v.get("edit_shared"))
    return v

def db_project_views(pid):
    with get_db() as db:
        return [_parse_view(r) for r in db.execute(
            "SELECT v.*, u.username AS owner_username FROM project_views v "
            "LEFT JOIN users u ON u.id = v.owner_id "
            "WHERE v.project_id=? ORDER BY v.name", (pid,)).fetchall()]

def db_get_view(vid):
    with get_db() as db:
        r = db.execute("SELECT * FROM project_views WHERE id=?", (vid,)).fetchone()
        return _parse_view(r) if r else None

def db_create_view(pid, name, owner_id, layout=None, visibility="private",
                   edit_shared=False):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO project_views (project_id, name, owner_id, visibility, "
            "edit_shared, layout, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, name, owner_id, visibility, 1 if edit_shared else 0,
             json.dumps(layout or []), now, now))
        db.commit()
        return cur.lastrowid

def db_update_view(vid, name=None, layout=None, visibility=None, edit_shared=None):
    with get_db() as db:
        if name is not None:
            db.execute("UPDATE project_views SET name=? WHERE id=?", (name, vid))
        if layout is not None:
            db.execute("UPDATE project_views SET layout=? WHERE id=?",
                       (json.dumps(layout), vid))
        if visibility in ("private", "project"):
            db.execute("UPDATE project_views SET visibility=? WHERE id=?",
                       (visibility, vid))
        if edit_shared is not None:
            db.execute("UPDATE project_views SET edit_shared=? WHERE id=?",
                       (1 if edit_shared else 0, vid))
        db.execute("UPDATE project_views SET updated_at=? WHERE id=?",
                   (datetime.now().isoformat(timespec="seconds"), vid))
        db.commit()

def db_delete_view(vid):
    with get_db() as db:
        db.execute("DELETE FROM project_views WHERE id=?", (vid,))
        db.commit()

# ─── Cable snapshots (configurations de câblage sauvegardées) ──

def db_cable_snapshot_save(name, edges):
    """Stocke un snapshot de câblage. `edges` est une liste de dicts
    {from_vmid, to_vmid, shm, kind, to_slot?}."""
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    payload = json.dumps({"edges": edges or []})
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO cable_snapshots (name, created_at, payload) VALUES (?, ?, ?)",
            (name, datetime.now().isoformat(timespec="seconds"), payload))
        db.commit()
        return cur.lastrowid

def _parse_cable_snapshot(row):
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload") or '{"edges":[]}')
    except Exception:
        d["payload"] = {"edges": []}
    return d

def db_cable_snapshots_list():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at, payload FROM cable_snapshots ORDER BY id DESC"
        ).fetchall()
    return [_parse_cable_snapshot(r) for r in rows]

def db_cable_snapshot_get(sid):
    with get_db() as db:
        row = db.execute(
            "SELECT id, name, created_at, payload FROM cable_snapshots WHERE id=?",
            (sid,)).fetchone()
    return _parse_cable_snapshot(row)

def db_cable_snapshot_delete(sid):
    with get_db() as db:
        cur = db.execute("DELETE FROM cable_snapshots WHERE id=?", (sid,))
        db.commit()
        return cur.rowcount > 0


# ─── Vues de disposition page Câbles (mode « Libre ») ─────────────
# payload = {"positions": {"<vmid>": {"x":…, "y":…}}, "collapsed": [vmid…]}.

# ─── plugin_store : stockage générique par plugin ────────────────────────────────────────────
# La table existait depuis le retrait de `cc_presets` / `dve_memories` (2026-07) mais N'AVAIT
# AUCUN helper ni aucun usage. On l'utilise plutôt que d'ajouter une troisième table
# spécifique — c'est exactement ce que son commentaire de création annonçait.
#
# `scope` : '' = global au TYPE de plugin (un préréglage de disposition vaut pour tous les
# scopes), str(vmid) = propre à un conteneur. `value` est du JSON.

def db_plugin_store_set(type_, name, value, scope=""):
    """Crée ou remplace une entrée. Le couple (type, scope, name) fait l'identité : sauver deux
    fois sous le même nom REMPLACE, ce qui est ce qu'attend quelqu'un qui « met à jour » un
    préréglage."""
    import json as _json
    db = get_db()
    ts = datetime.now().isoformat(timespec="seconds")
    txt = _json.dumps(value, ensure_ascii=False)
    cur = db.execute("UPDATE plugin_store SET value=?, updated_at=? "
                     "WHERE type=? AND scope=? AND name=?", (txt, ts, type_, scope, name))
    if cur.rowcount == 0:
        db.execute("INSERT INTO plugin_store (type, scope, name, value, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?)", (type_, scope, name, txt, ts, ts))
    db.commit()


def db_plugin_store_get(type_, name, scope=""):
    import json as _json
    r = get_db().execute("SELECT value FROM plugin_store WHERE type=? AND scope=? AND name=?",
                         (type_, scope, name)).fetchone()
    if not r:
        return None
    try:
        return _json.loads(r["value"])
    except (ValueError, TypeError):
        # Une valeur illisible n'est pas une absence : on le DIT au journal plutôt que de rendre
        # None, qui se lirait comme « ce préréglage n'existe pas ».
        log.warning("plugin_store %s/%s/%s : JSON illisible", type_, scope, name)
        return None


def db_plugin_store_list(type_, scope=""):
    """[{name, created_at, updated_at}] — SANS les valeurs : une liste de préréglages sert à
    choisir, pas à tout charger."""
    rows = get_db().execute(
        "SELECT name, created_at, updated_at FROM plugin_store WHERE type=? AND scope=? "
        "ORDER BY name", (type_, scope)).fetchall()
    return [dict(r) for r in rows]


def db_plugin_store_delete(type_, name, scope=""):
    db = get_db()
    n = db.execute("DELETE FROM plugin_store WHERE type=? AND scope=? AND name=?",
                   (type_, scope, name)).rowcount
    db.commit()
    return n > 0


def db_cable_layout_save(name, payload):
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    blob = json.dumps(payload or {"positions": {}, "collapsed": []})
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO cable_layouts (name, created_at, payload) VALUES (?, ?, ?)",
            (name, datetime.now().isoformat(timespec="seconds"), blob))
        db.commit()
        return cur.lastrowid

def _parse_cable_layout(row):
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload") or '{"positions":{},"collapsed":[]}')
    except Exception:
        d["payload"] = {"positions": {}, "collapsed": []}
    return d

def db_cable_layouts_list():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at, payload FROM cable_layouts ORDER BY id DESC"
        ).fetchall()
    return [_parse_cable_layout(r) for r in rows]

def db_cable_layout_get(lid):
    with get_db() as db:
        row = db.execute(
            "SELECT id, name, created_at, payload FROM cable_layouts WHERE id=?",
            (lid,)).fetchone()
    return _parse_cable_layout(row)

def db_cable_layout_delete(lid):
    with get_db() as db:
        cur = db.execute("DELETE FROM cable_layouts WHERE id=?", (lid,))
        db.commit()
        return cur.rowcount > 0


# ─── Share links (pages publiques client WebRTC) ─────────────

_SL_COLS = ("token, vmid, path, title, note, created_at, "
            "COALESCE(kind,'webrtc') AS kind, COALESCE(cidrs,'') AS cidrs, "
            "COALESCE(instance_uuid,'') AS instance_uuid")


def db_create_share_link(token, vmid, path, title=None, note=None, kind="webrtc", cidrs="",
                         instance_uuid=None):
    # Le `vmid` est conservé pour mémoire (affichage, historique) mais ce n'est PLUS lui qui
    # désigne le conteneur : c'est `instance_uuid`. Cf. la migration dans `init_db`.
    if instance_uuid is None:
        with get_db() as db:
            r = db.execute("SELECT instance_uuid FROM containers WHERE vmid=?",
                           (int(vmid),)).fetchone()
        instance_uuid = (r["instance_uuid"] if r else None) or ""
    with get_db() as db:
        db.execute(
            "INSERT INTO share_links (token, vmid, path, title, note, created_at, kind, cidrs,"
            " instance_uuid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token, int(vmid), path, title, note,
             datetime.now().isoformat(timespec="seconds"), str(kind or "webrtc"),
             str(cidrs or ""), str(instance_uuid or "")))
        db.commit()
    return token


def db_delete_share_links_orphelins():
    """Supprime les liens dont AUCUN conteneur ne porte plus l'identité d'instance.

    ⚠ ON NE SUPPRIME PAS SUR DESTRUCTION DU CONTENEUR, et c'est délibéré. Un conteneur détruit
    puis RECRÉÉ dans un projet garde son `instance_uuid` : le lien doit alors continuer de
    fonctionner, c'est le même appareil et l'exploitant attend l'inverse d'une révocation. Le
    ménage est donc une action VOULUE, pas un effet de bord d'une destruction.

    Rend le nombre de liens supprimés."""
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM share_links WHERE COALESCE(instance_uuid,'') = '' "
            "   OR instance_uuid NOT IN (SELECT COALESCE(instance_uuid,'') FROM containers)")
        db.commit()
        return cur.rowcount


def db_set_share_link_cidrs(token, cidrs):
    """Change la restriction d'adresses d'un lien. Rend True si le lien existait."""
    with get_db() as db:
        cur = db.execute("UPDATE share_links SET cidrs=? WHERE token=?",
                         (str(cidrs or ""), token))
        db.commit()
        return cur.rowcount > 0


def db_list_all_share_links():
    """TOUS les liens publics, tous conteneurs confondus — pour la page Réglages.

    ⚠ UN ACCÈS SANS IDENTIFICATION QU'ON NE VOIT NULLE PART EST UN ACCÈS QU'ON OUBLIE. Les
    liens n'étaient listés que conteneur par conteneur : celui posé sur une machine détruite
    six mois plus tôt ne s'affichait plus, et restait pourtant valable. D'où cette vue globale,
    qui les montre TOUS, y compris ceux dont le conteneur n'existe plus."""
    with get_db() as db:
        rows = db.execute("SELECT " + _SL_COLS + " FROM share_links "
                          "ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def db_get_share_link(token):
    with get_db() as db:
        r = db.execute(
            "SELECT " + _SL_COLS + " FROM share_links WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None

def db_list_share_links(vmid):
    with get_db() as db:
        rows = db.execute(
            "SELECT " + _SL_COLS + " FROM share_links "
            "WHERE vmid=? ORDER BY created_at DESC", (int(vmid),)).fetchall()
    return [dict(r) for r in rows]

def db_delete_share_link(token):
    with get_db() as db:
        cur = db.execute("DELETE FROM share_links WHERE token=?", (token,))
        db.commit()
        return cur.rowcount > 0


# ─── Layouts (presets de multiview) ─────────────────────────

def db_save_layout(name, config):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO layouts (name, created_at, config) VALUES (?, ?, ?)",
            (name, datetime.now().isoformat(timespec="seconds"), json.dumps(config))
        )
        db.commit()
        return cur.lastrowid

def db_get_layouts():
    with get_db() as db:
        rows = db.execute("SELECT * FROM layouts ORDER BY id DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["config"] = json.loads(d.get("config") or "{}")
            except Exception:
                d["config"] = {}
            result.append(d)
        return result

def db_update_layout(lid, name, config):
    """Écrase un layout existant (nom + config) SANS toucher son id ni created_at.
    Renvoie True si la ligne existait, False sinon (id inconnu → l'appelant renvoie 404)."""
    with get_db() as db:
        cur = db.execute(
            "UPDATE layouts SET name=?, config=? WHERE id=?",
            (name, json.dumps(config), lid)
        )
        db.commit()
        return cur.rowcount > 0

def db_delete_layout(lid):
    with get_db() as db:
        db.execute("DELETE FROM layouts WHERE id=?", (lid,))
        db.commit()

# ─── Modèles de PiP (bibliothèque composable des multiviews) ─
# config = {"components": [{type, x, y, w, h (normalisés 0..1), …}]} — cf. Réglages → PiP.

def db_save_pip_template(name, config, tid=None, tags=None):
    """Crée (tid None) ou met à jour un modèle de PiP. `tags` = liste de chaînes libres
    (None laisse les tags inchangés en update ; absent = aucun tag en création). Renvoie l'id.
    `updated_at` est toujours rafraîchi — sert de tri « modifié récemment » dans la galerie."""
    now = datetime.now().isoformat(timespec="seconds")
    tags_json = json.dumps(tags) if tags is not None else None
    with get_db() as db:
        if tid is not None:
            if tags is not None:
                db.execute("UPDATE pip_templates SET name=?, config=?, updated_at=?, tags=? WHERE id=?",
                           (name, json.dumps(config), now, tags_json, tid))
            else:
                db.execute("UPDATE pip_templates SET name=?, config=?, updated_at=? WHERE id=?",
                           (name, json.dumps(config), now, tid))
            db.commit()
            return tid
        cur = db.execute(
            "INSERT INTO pip_templates (name, created_at, updated_at, config, tags) VALUES (?, ?, ?, ?, ?)",
            (name, now, now, json.dumps(config), tags_json))
        db.commit()
        return cur.lastrowid

def db_get_pip_templates():
    with get_db() as db:
        rows = db.execute("SELECT * FROM pip_templates ORDER BY name COLLATE NOCASE").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["config"] = json.loads(d.get("config") or "{}")
            except Exception:
                d["config"] = {}
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
                if not isinstance(d["tags"], list):
                    d["tags"] = []
            except Exception:
                d["tags"] = []
            result.append(d)
        return result

def db_delete_pip_template(tid):
    with get_db() as db:
        db.execute("DELETE FROM pip_templates WHERE id=?", (tid,))
        db.commit()

# ─── Bibliothèque de polices (métadonnées ; le .ttf vit sur disque) ─
# Logique métier (validation, hash, usage, export/import) : app/fonts.py.

def db_list_fonts():
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM fonts ORDER BY name COLLATE NOCASE").fetchall()]

def db_get_font(sha256):
    with get_db() as db:
        r = db.execute("SELECT * FROM fonts WHERE sha256=?", (sha256,)).fetchone()
        return dict(r) if r else None

def db_add_font(sha256, name, family, style, ext, size, created_by=""):
    """Insère une police. Idempotent : si le sha existe déjà, la ligne est laissée telle
    quelle (dédup par HASH) et l'appelant récupère l'existante."""
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO fonts (sha256, name, family, style, ext, size, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sha256, name, family, style, ext, size,
             datetime.now().isoformat(timespec="seconds"), created_by))
        db.commit()
    return db_get_font(sha256)

def db_delete_font(sha256):
    with get_db() as db:
        cur = db.execute("DELETE FROM fonts WHERE sha256=?", (sha256,))
        db.commit()
        return cur.rowcount > 0

# ─── Settings (clé/valeur typées via JSON) ─────────────────

def db_get_setting(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

def db_set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        db.commit()

# ─── Réglages PAR NŒUD (override du global) ─────────────────

_NODE_SETTING_SENTINEL = object()   # distingue « absent » de « override = null »

def db_get_node_setting(node_id, key, default=_NODE_SETTING_SENTINEL):
    """Override par-nœud d'un réglage, ou `default` (sentinelle) si absent. Distinct d'un override
    explicite à null. Sérialisation JSON comme les settings globaux."""
    with get_db() as db:
        row = db.execute("SELECT value FROM node_settings WHERE node_id=? AND key=?",
                         (node_id, key)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

def db_set_node_setting(node_id, key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO node_settings (node_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(node_id, key) DO UPDATE SET value=excluded.value",
            (node_id, key, json.dumps(value)))
        db.commit()

def db_delete_node_setting(node_id, key):
    """Retire l'override par-nœud (le réglage hérite à nouveau du global)."""
    with get_db() as db:
        db.execute("DELETE FROM node_settings WHERE node_id=? AND key=?", (node_id, key))
        db.commit()

def db_get_node_settings(node_id):
    """Tous les overrides d'un nœud : {key: value}."""
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM node_settings WHERE node_id=?", (node_id,)).fetchall()
    out = {}
    for r in rows:
        try: out[r["key"]] = json.loads(r["value"])
        except Exception: out[r["key"]] = r["value"]
    return out

# ─── Users ──────────────────────────────────────────────────

# ─── Sessions ouvertes ───────────────────────────────────────────────────────

# Au-delà, une session est considérée abandonnée et purgée. Aligné sur la durée de vie du
# cookie (`PERMANENT_SESSION_LIFETIME`, ~30 jours) : garder des lignes plus longtemps que le
# cookie qu'elles décrivent ne servirait qu'à faire grossir la table.
SESSION_RETENTION_JOURS = 30

# Fréquence d'écriture de `last_seen`. ⚠ PAS À CHAQUE REQUÊTE : le tableau de bord interroge
# /api/containers et /api/alerts toutes les 5 s, par onglet ouvert. Une écriture par requête
# transformerait un registre de sessions en générateur d'écritures SQLite permanent.
SESSION_TOUCH_S = 60


def db_session_ouvrir(sid, user_id, ip=None, user_agent=None):
    now = datetime.now().isoformat(timespec="seconds")
    db = get_db()
    db.execute("INSERT OR REPLACE INTO user_sessions "
               "(sid, user_id, created_at, last_seen, ip, user_agent, revoked) "
               "VALUES (?, ?, ?, ?, ?, ?, 0)",
               (sid, user_id, now, now, ip, (user_agent or "")[:400]))
    db.commit()


def db_session_get(sid):
    r = get_db().execute("SELECT * FROM user_sessions WHERE sid = ?", (sid,)).fetchone()
    return dict(r) if r else None


def db_session_toucher(sid, ip=None):
    """Rafraîchit `last_seen` — seulement si elle a plus de SESSION_TOUCH_S. Rend True si une
    écriture a eu lieu, pour que l'appelant puisse la compter."""
    r = get_db().execute("SELECT last_seen FROM user_sessions WHERE sid = ?", (sid,)).fetchone()
    if not r:
        return False
    try:
        vu = datetime.fromisoformat(r["last_seen"])
    except (TypeError, ValueError):
        vu = None
    now = datetime.now()
    if vu and (now - vu).total_seconds() < SESSION_TOUCH_S:
        return False
    db = get_db()
    db.execute("UPDATE user_sessions SET last_seen = ?, ip = COALESCE(?, ip) WHERE sid = ?",
               (now.isoformat(timespec="seconds"), ip, sid))
    db.commit()
    return True


def db_sessions_utilisateur(user_id):
    """Sessions VIVANTES d'un utilisateur, la plus récemment vue d'abord."""
    return [dict(r) for r in get_db().execute(
        "SELECT * FROM user_sessions WHERE user_id = ? AND revoked = 0 "
        "ORDER BY last_seen DESC", (user_id,)).fetchall()]


def db_session_fermer(sid):
    db = get_db()
    db.execute("UPDATE user_sessions SET revoked = 1 WHERE sid = ?", (sid,))
    db.commit()


def db_sessions_fermer_autres(user_id, sauf_sid):
    """Ferme toutes les sessions du compte SAUF une, et fait avancer son époque.

    ⚠ LES DEUX SONT NÉCESSAIRES, et l'époque est la moins évidente : révoquer les lignes ne
    ferme que les sessions DÉJÀ INSCRITES. Un cookie d'avant le registre, resté dormant sur un
    poste oublié, n'est inscrit nulle part au moment du clic — il se ferait enregistrer au
    prochain usage et aurait survécu. L'époque le refuse."""
    db = get_db()
    cur = db.execute("UPDATE user_sessions SET revoked = 1 "
                     "WHERE user_id = ? AND sid != ? AND revoked = 0", (user_id, sauf_sid))
    n = cur.rowcount
    db.execute("UPDATE users SET session_epoch = COALESCE(session_epoch, 0) + 1 WHERE id = ?",
               (user_id,))
    epoque = db.execute("SELECT session_epoch FROM users WHERE id = ?",
                        (user_id,)).fetchone()["session_epoch"]
    db.commit()
    return n, epoque


def db_sessions_purger():
    """Retire les sessions abandonnées. Appelée à la connexion — un moment déjà coûteux, et
    le seul où l'on est sûr que la table va servir."""
    limite = (datetime.now() - timedelta(days=SESSION_RETENTION_JOURS)).isoformat(timespec="seconds")
    db = get_db()
    cur = db.execute("DELETE FROM user_sessions WHERE last_seen < ?", (limite,))
    db.commit()
    return cur.rowcount


def db_user_marquer_connexion(user_id):
    db = get_db()
    db.execute("UPDATE users SET last_login = ? WHERE id = ?",
               (datetime.now().isoformat(timespec="seconds"), user_id))
    db.commit()


# ─── Habilitations (rôles d'AUTORISATION) ───────────────────────────────────────────────────────────────────

def db_habilitations_semer(defauts):
    """Sème la table depuis les constantes de `auth.py` — UNE SEULE FOIS, si elle est vide.

    ⚠ Ne re-sème pas rôle par rôle : un administrateur qui RETIRE une autorisation d'un rôle
    intégré la verrait revenir au prochain démarrage. La table, une fois peuplée, fait foi.

    `defauts` = {id: {"label": …, "permissions": [...], "global_access": bool}}."""
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM habilitations").fetchone()["c"]:
        return False
    for rid, d in defauts.items():
        db.execute("INSERT INTO habilitations (id, label, permissions, global_access, builtin) "
                   "VALUES (?, ?, ?, ?, 1)",
                   (rid, d.get("label"), json.dumps(sorted(d.get("permissions") or [])),
                    1 if d.get("global_access") else 0))
    db.commit()
    return True


def db_habilitations_lister(permissions_connues=None):
    """Toutes les habilitations.

    ⚠ À ne pas confondre avec `db_roles_list()` plus haut, qui liste les EMPLACEMENTS
    (`production_roles`, troisième barreau d'identité). Deux notions, deux tables.
    `permissions_connues` filtre les autorisations qui n'existent plus dans
    le produit : une entrée orpheline restée en base ne doit pas se faire passer pour un droit."""
    out = []
    for r in get_db().execute("SELECT * FROM habilitations ORDER BY builtin DESC, id").fetchall():
        d = dict(r)
        try:
            perms = json.loads(d.get("permissions") or "[]")
        except (TypeError, ValueError):
            perms = []
        if permissions_connues is not None:
            perms = [p for p in perms if p in permissions_connues]
        d["permissions"] = sorted(set(perms))
        d["global_access"] = bool(d.get("global_access"))
        d["builtin"] = bool(d.get("builtin"))
        out.append(d)
    return out


def db_habilitation_get(rid):
    r = get_db().execute("SELECT * FROM habilitations WHERE id = ?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["permissions"] = sorted(set(json.loads(d.get("permissions") or "[]")))
    except (TypeError, ValueError):
        d["permissions"] = []
    d["global_access"] = bool(d.get("global_access"))
    d["builtin"] = bool(d.get("builtin"))
    return d


def db_habilitation_upsert(rid, label=None, permissions=None, global_access=None, builtin=None):
    """Crée ou met à jour un rôle. Les champs à None ne sont PAS touchés."""
    db = get_db()
    existe = db.execute("SELECT id FROM habilitations WHERE id = ?", (rid,)).fetchone()
    if not existe:
        db.execute("INSERT INTO habilitations (id, label, permissions, global_access, builtin) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (rid, label, json.dumps(sorted(set(permissions or []))),
                    1 if global_access else 0, 1 if builtin else 0))
    else:
        sets, args = [], []
        if label is not None:
            sets.append("label = ?"); args.append(label)
        if permissions is not None:
            sets.append("permissions = ?"); args.append(json.dumps(sorted(set(permissions))))
        if global_access is not None:
            sets.append("global_access = ?"); args.append(1 if global_access else 0)
        if builtin is not None:
            sets.append("builtin = ?"); args.append(1 if builtin else 0)
        if sets:
            args.append(rid)
            db.execute("UPDATE habilitations SET %s WHERE id = ?" % ", ".join(sets), args)
    db.commit()


def db_habilitation_supprimer(rid):
    db = get_db()
    db.execute("DELETE FROM habilitations WHERE id = ?", (rid,))
    db.commit()


def db_habilitation_compte_utilisateurs(rid):
    return get_db().execute("SELECT COUNT(*) c FROM users WHERE role = ?", (rid,)).fetchone()["c"]


def db_get_user(username):
    with get_db() as db:
        r = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None

def db_get_user_by_id(uid):
    with get_db() as db:
        r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

def db_list_users():
    with get_db() as db:
        return [dict(r) for r in
                db.execute("SELECT id, username, role, created_at, prenom, nom, email, "
                           "interface FROM users ORDER BY id").fetchall()]

def db_create_user(username, password_hash, role, prenom=None, nom=None, email=None,
                   interface=None):
    # Défaut d'interface à la création : les rôles à accès global atterrissent sur
    # l'UI technique, les autres sur l'accueil projets (/workspaces).
    if interface not in ("technique", "projets"):
        interface = "technique" if role in ("admin", "operator") else "projets"
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, created_at, prenom, nom, email, interface) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (username, password_hash, role, datetime.now().isoformat(timespec='seconds'),
             prenom, nom, email, interface))
        db.commit()
        return cur.lastrowid

def db_set_user_gh_token(uid, token):
    """Pose (ou efface, avec "") le jeton GitHub PERSONNEL d'un utilisateur.

    Fonction dédiée plutôt qu'un paramètre de plus à `db_update_user` : celle-ci est appelée
    depuis les pages d'administration des comptes, où un admin modifie les champs d'AUTRUI.
    Un jeton n'a pas à voyager par ce chemin — il ne se pose que sur soi-même."""
    with get_db() as db:
        db.execute("UPDATE users SET gh_token=? WHERE id=?", (token or "", uid))


def db_update_user(uid, role=None, password_hash=None, prenom=None, nom=None,
                   email=None, lang=None, interface=None, theme=None,
                   telephone=None, service=None, poste=None, photo_url=None):
    with get_db() as db:
        if role is not None:
            db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        if password_hash is not None:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, uid))
        # prenom/nom/email : "" autorisé (efface), None = ne pas toucher
        if prenom is not None:
            db.execute("UPDATE users SET prenom=? WHERE id=?", (prenom, uid))
        if nom is not None:
            db.execute("UPDATE users SET nom=? WHERE id=?", (nom, uid))
        if email is not None:
            db.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
        if lang is not None:
            db.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
        if interface in ("technique", "projets"):
            db.execute("UPDATE users SET interface=? WHERE id=?", (interface, uid))
        # theme : "" remet l'utilisateur sur le défaut du système (colonne NULL)
        if theme is not None:
            db.execute("UPDATE users SET theme=? WHERE id=?", (theme or None, uid))
        # Fiche : même convention que prenom/nom/email — "" efface, None ne touche pas.
        for _c, _v in (("telephone", telephone), ("service", service),
                       ("poste", poste), ("photo_url", photo_url)):
            if _v is not None:
                db.execute("UPDATE users SET %s=? WHERE id=?" % _c, (_v, uid))
        db.commit()

# ─── i18n : surcouche de traductions éditée via l'UI ─────────

def db_i18n_overrides():
    """Toutes les surcharges : { lang: { key: value } }."""
    with get_db() as db:
        rows = db.execute("SELECT lang, key, value FROM i18n_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["lang"], {})[r["key"]] = r["value"]
    return out

def db_i18n_overrides_for_lang(lang):
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM i18n_overrides WHERE lang=?", (lang,)).fetchall()
    return {r["key"]: r["value"] for r in rows}

def db_i18n_set_override(lang, key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO i18n_overrides (lang, key, value) VALUES (?,?,?) "
            "ON CONFLICT(lang, key) DO UPDATE SET value=excluded.value",
            (lang, key, value))
        db.commit()

def db_i18n_delete_override(lang, key):
    with get_db() as db:
        db.execute("DELETE FROM i18n_overrides WHERE lang=? AND key=?", (lang, key))
        db.commit()

def db_i18n_delete_lang(lang):
    """Supprime toutes les surcharges d'une langue (suppression d'une langue custom)."""
    with get_db() as db:
        db.execute("DELETE FROM i18n_overrides WHERE lang=?", (lang,))
        db.commit()


def db_set_project(vmid, project_id):
    with get_db() as db:
        db.execute("UPDATE containers SET project_id=? WHERE vmid=?", (project_id, vmid))
        db.commit()

def db_set_monitor_user(vmid, uid):
    """Lie un container (monitor) à un utilisateur (matching stable, indépendant du hostname)."""
    with get_db() as db:
        db.execute("UPDATE containers SET monitor_user_id=? WHERE vmid=?", (uid, vmid))
        db.commit()

def db_delete_user(uid):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()

def db_count_users():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def db_get_all_settings():
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out


# ─── TSL connections ─────────────────────────────────────────

def db_get_tsl_connections():
    with get_db() as db:
        return [dict(r) for r in
                db.execute("SELECT * FROM tsl_connections ORDER BY id").fetchall()]

def db_upsert_tsl_connection(data):
    """Crée ou met à jour une connexion TSL. `data` dict avec id optionnel."""
    id_   = data.get("id")
    name  = str(data.get("name") or "")
    port  = int(data.get("port") or 12345)
    enabled = int(bool(data.get("enabled")))
    label_col  = int(data.get("label_col") or 2)

    # UN niveau par connexion : la chaîne de destination. Ses trois champs TSL ne sont pas trois
    # chaînes, ce sont trois façons d'exprimer l'état de celle-ci — `rouge_field`/`vert_field`
    # disent lesquels. Sans niveau, la connexion n'écrit RIEN plutôt que d'écrire sur un niveau
    # deviné : ce serait allumer un rouge chez quelqu'un d'autre.
    _v = data.get("level_uuid")
    level_uuid = str(_v).strip() if _v not in (None, "", 0, "0") else None
    rouge_field = str(data.get("rouge_field") or "tt").lower()
    vert_field  = str(data.get("vert_field") or "lh").lower()
    if rouge_field not in ("lh", "rh", "tt"): rouge_field = "tt"
    if vert_field  not in ("lh", "rh", "tt"): vert_field  = "lh"
    direction = str(data.get("direction") or "in").lower()
    if direction not in ("in", "out"): direction = "in"
    dest_host = str(data.get("dest_host") or "")
    project_id = data.get("project_id") or None
    try:
        project_id = int(project_id) if project_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        project_id = None
    # Rattachée à un projet et sans niveau propre → elle prend le premier du projet.
    if project_id and not level_uuid:
        niv = db_get_tally_levels_of("project", project_id)
        level_uuid = niv[0] if niv else None
    with get_db() as db:
        if id_:
            db.execute(
                "UPDATE tsl_connections SET name=?, port=?, enabled=?, label_col=?, "
                "level_uuid=?, "
                "rouge_field=?, vert_field=?, direction=?, dest_host=?, project_id=? WHERE id=?",
                (name, port, enabled, label_col, level_uuid, rouge_field, vert_field,
                 direction, dest_host, project_id, int(id_)))
            lid = int(id_)
        else:
            cur = db.execute(
                "INSERT INTO tsl_connections (name, port, enabled, label_col, "
                "level_uuid, "
                "rouge_field, vert_field, direction, dest_host, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, port, enabled, label_col, level_uuid, rouge_field, vert_field,
                 direction, dest_host, project_id))
            lid = cur.lastrowid
        db.commit()
    return lid

def db_delete_tsl_connection(id_):
    with get_db() as db:
        db.execute("DELETE FROM tsl_mapping WHERE connection_id=?", (int(id_),))
        cur = db.execute("DELETE FROM tsl_connections WHERE id=?", (int(id_),))
        db.commit()
        return cur.rowcount > 0


# ─── Source labels (nouvelle architecture) ────────────────────

_SL_LABEL_COLS  = [f"label_{i}" for i in range(2, 10)]
_SL_FIELDS      = {"projet", "parent_shm"} | set(_SL_LABEL_COLS)

def _resolve_hostname_for_shm(shm):
    """Trouve le hostname du container qui produit ce shm_out."""
    with get_db() as db:
        r = db.execute(
            "SELECT hostname FROM containers WHERE shm_out=? OR shm_out LIKE ? LIMIT 1",
            (shm, f"%{shm}%")).fetchone()
        return r["hostname"] if r else None

def db_get_source_labels():
    with get_db() as db:
        return [dict(r) for r in
                db.execute("SELECT * FROM source_labels ORDER BY shm").fetchall()]

def db_upsert_source_label(shm: str, fields: dict):
    """Crée ou met à jour les métadonnées d'une source. `fields` : projet et/ou label_N."""
    filt = {k: str(v) if v is not None else ""
            for k, v in _champs_filtres(fields, _SL_FIELDS, "_SL_FIELDS",
                                        "db_upsert_source_label").items()}
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM source_labels WHERE shm=?", (shm,)).fetchone()
        if exists:
            if filt:
                sets = [f"{k}=?" for k in filt]
                db.execute(f"UPDATE source_labels SET {', '.join(sets)} WHERE shm=?",
                           list(filt.values()) + [shm])
        else:
            cols = ["shm"] + list(filt.keys())
            vals = [shm] + list(filt.values())
            phs  = ["?"] * len(cols)
            db.execute(f"INSERT INTO source_labels ({', '.join(cols)}) VALUES ({', '.join(phs)})",
                       vals)
        db.commit()

def db_delete_source_label(shm: str):
    with get_db() as db:
        cur = db.execute("DELETE FROM source_labels WHERE shm=?", (shm,))
        db.commit()
        return cur.rowcount > 0

def db_get_source_label_for_shm(shm: str, col: int) -> str:
    """Retourne le label col (0-9) pour un shm.
    Col 0 = hostname auto, col 1 = shm lui-même, col 2-9 = stockés."""
    if col == 0:
        return _resolve_hostname_for_shm(shm) or ""
    if col == 1:
        return shm or ""
    with get_db() as db:
        r = db.execute(f"SELECT label_{col} FROM source_labels WHERE shm=?", (shm,)).fetchone()
        return (r[f"label_{col}"] or "") if r else ""

def db_purger_libelles_orphelins(con=None):
    """Retire les lignes de libellé SANS AUCUNE CONFIGURATION dont plus rien ne produit le flux.

    ★ LA DISTINCTION EST CELLE-CI, et elle décide de tout : une source CONFIGURÉE — un libellé
    écrit à la main, une correspondance TSL ou IS-07 — se garde, même quand son conteneur a
    disparu. Il reviendra peut-être, et son réglage doit l'attendre. Une ligne SANS configuration
    n'est que du résidu : elle ne porte le travail de personne, et il n'y a rien à décider.

    ⚠ « PLUS RIEN NE PRODUIT » SE LIT DANS LA DÉCLARATION, jamais dans l'état d'exécution.
    `deploy_config` ne change pas quand un conteneur s'arrête, ni quand un nœud devient
    injoignable. Se fier au `status` ferait balayer des lignes à chaque redémarrage — et comme
    on ne balaie que les vides, la faute serait invisible jusqu'au jour où elle ne l'est plus.

    ⚠ Les lignes de TEXTE (`__umd:`) n'ont pas de producteur par construction : elles ne sont
    jamais orphelines, sinon on les balaierait toutes à chaque passage.

    ⚠ `con` PERMET DE LA TESTER SANS TOUCHER À LA PRODUCTION. Cette fonction SUPPRIME : un banc
    qui la mute pour vérifier ses gardes exécuterait de vraies suppressions sur la vraie base —
    c'est arrivé, et il a fallu restaurer les libellés depuis une sauvegarde. `get_db()` épinglant
    le chemin de la base, il n'y avait aucun autre moyen de l'isoler. Toute fonction destructrice
    ajoutée ici doit accepter la même injection.

    Renvoie le nombre de lignes retirées."""
    import json as _json
    from . import plugins as _plg
    from contextlib import nullcontext
    with (nullcontext(con) if con is not None else get_db()) as db:
        declares = set()
        for vmid, dc in db.execute("SELECT vmid, deploy_config FROM containers").fetchall():
            if not dc:
                continue
            try:
                cfg = _json.loads(dc) if isinstance(dc, str) else dc
                hn = (cfg.get("params") or {}).get("hostname")
                if not hn:
                    r = db.execute("SELECT hostname FROM containers WHERE vmid=?", (vmid,)).fetchone()
                    hn = r[0] if r else None
                w = _plg.derive_wiring(cfg.get("type"), hn, cfg.get("params") or {}) or {}
                for prod in (w.get("produces") or []):
                    if prod.get("shm"):
                        declares.add(prod["shm"])
            except Exception:
                # Un conteneur illisible ne doit pas rendre ses flux « absents » : on renonce à
                # tout balayer plutôt que de risquer de retirer ce qu'il produisait.
                return 0
        vises = set()
        for t in ("tsl_mapping", "is07_mapping"):
            try:
                vises |= {r[0] for r in db.execute("SELECT source_shm FROM %s" % t) if r[0]}
            except Exception:
                pass
        cols = [c for c in _SL_LABEL_COLS + ["projet"]
                if c in {r[1] for r in db.execute("PRAGMA table_info(source_labels)")}]
        retires = 0
        for ligne in db.execute("SELECT * FROM source_labels").fetchall():
            shm = ligne["shm"] or ""
            if not shm or shm.startswith("__umd:") or shm in declares or shm in vises:
                continue
            if any((ligne[c] or "").strip() for c in cols):
                continue                      # CONFIGURÉE : elle attend son conteneur
            db.execute("DELETE FROM source_labels WHERE shm=?", (shm,))
            retires += 1
        if retires:
            db.commit()
            log.info("libellés : %d ligne(s) sans configuration retirée(s) — leur flux n'est "
                     "plus déclaré par aucun conteneur", retires)
        return retires


def db_get_source_labels_by_shm() -> dict:
    """Retourne {shm: {labels:[]}} pour la page Câbles et le distributor."""
    out = {}
    for s in db_get_source_labels():
        shm = s["shm"]
        labels = [
            _resolve_hostname_for_shm(shm) or "",   # 0 = hostname
            shm,                                      # 1 = MXL
        ] + [s.get(f"label_{i}") or "" for i in range(2, 10)]
        out[shm] = {"labels": labels}
    return out

# ─── IS-07 entrant : connexions et correspondance ─────────────
#
# Volontairement CALQUÉ sur TSL, jusqu'aux noms : une connexion écrit dans UN niveau, et une table
# dit quelle adresse de l'émetteur désigne quel signal chez nous. Chez TSL l'adresse est un index
# de trame, ici l'UUID d'une Source — c'est la seule différence, et elle ne change rien au geste
# de l'exploitant. C'est pour ça que les deux s'éditent dans la MÊME page (Labels), côte à côte :
# un site qui reçoit du tally des deux protocoles y lit une seule table.

def db_get_is07_connections() -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM is07_connections ORDER BY id").fetchall()]


def db_upsert_is07_connection(data: dict):
    """Crée ou met à jour une connexion IS-07 entrante. Renvoie son id."""
    id_ = data.get("id")
    nom = str(data.get("name") or "").strip() or "IS-07"
    actif = 1 if data.get("enabled") else 0
    u = data.get("level_uuid")
    u = str(u).strip() if u not in (None, "", 0, "0") else None
    with get_db() as db:
        if id_:
            db.execute("UPDATE is07_connections SET name=?, enabled=?, level_uuid=? WHERE id=?",
                       (nom, actif, u, int(id_)))
            lid = int(id_)
        else:
            lid = db.execute(
                "INSERT INTO is07_connections (name, enabled, level_uuid) VALUES (?,?,?)",
                (nom, actif, u)).lastrowid
        db.commit()
    return lid


def db_delete_is07_connection(id_) -> bool:
    with get_db() as db:
        db.execute("DELETE FROM is07_mapping WHERE connection_id=?", (int(id_),))
        cur = db.execute("DELETE FROM is07_connections WHERE id=?", (int(id_),))
        db.commit()
        return cur.rowcount > 0


def db_get_is07_mapping(connection_id: int) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM is07_mapping WHERE connection_id=? ORDER BY source_id",
            (int(connection_id),)).fetchall()]


def db_get_is07_mappings_all() -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT connection_id, source_id, source_shm FROM is07_mapping "
            "ORDER BY connection_id, source_id").fetchall()]


def db_set_is07_mapping_for_source(connection_id: int, source_shm: str, source_id):
    """Pose (ou retire, avec None/'') la Source d'émetteur qui désigne ce signal.

    ★ UNE SOURCE PAR SIGNAL ET PAR CONNEXION. On efface d'abord toute autre entrée de cette
    connexion pointant le même signal : sans ça, ré-affecter une source laisserait l'ancienne en
    place, et deux Sources de l'émetteur allumeraient le même signal — dont une périmée, qui ne
    s'éteindrait plus jamais. Même règle que la correspondance TSL."""
    sid = str(source_id or "").strip()
    with get_db() as db:
        db.execute("DELETE FROM is07_mapping WHERE connection_id=? AND source_shm=?",
                   (int(connection_id), source_shm or ""))
        if sid:
            db.execute("INSERT OR REPLACE INTO is07_mapping "
                       "(connection_id, source_id, source_shm) VALUES (?,?,?)",
                       (int(connection_id), sid, source_shm or ""))
        db.commit()


def db_get_source_for_is07(connection_id: int, source_id: str):
    with get_db() as db:
        r = db.execute("SELECT source_shm FROM is07_mapping WHERE connection_id=? AND source_id=?",
                       (int(connection_id), str(source_id))).fetchone()
        return (r["source_shm"] or None) if r else None


# ─── TSL mapping (per-connexion) ──────────────────────────────

def db_get_tsl_mapping(connection_id: int) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM tsl_mapping WHERE connection_id=? ORDER BY tsl_index",
            (int(connection_id),)).fetchall()]

def db_upsert_tsl_mapping(connection_id: int, tsl_index: int, source_shm: str):
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO tsl_mapping (connection_id, tsl_index, source_shm) "
            "VALUES (?, ?, ?)",
            (int(connection_id), int(tsl_index), source_shm or ""))
        db.commit()

def db_delete_tsl_mapping(connection_id: int, tsl_index: int) -> bool:
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM tsl_mapping WHERE connection_id=? AND tsl_index=?",
            (int(connection_id), int(tsl_index)))
        db.commit()
        return cur.rowcount > 0

def db_get_source_for_tsl(connection_id: int, tsl_index: int):
    """Retourne le shm associé à (connection_id, tsl_index) ou None."""
    with get_db() as db:
        r = db.execute(
            "SELECT source_shm FROM tsl_mapping WHERE connection_id=? AND tsl_index=?",
            (int(connection_id), int(tsl_index))).fetchone()
        return (r["source_shm"] or None) if r else None

def db_get_tsl_mappings_all() -> list:
    """Tous les mappings, toutes connexions confondues — pour l'éditeur de labels."""
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT connection_id, tsl_index, source_shm FROM tsl_mapping "
            "ORDER BY connection_id, tsl_index").fetchall()]

def db_set_tsl_mapping_for_source(connection_id: int, source_shm: str, tsl_index):
    """Édition par-source (tableau labels) : (connection_id, source_shm) → tsl_index.

    Réconcilie la table dont la clé primaire est (connection_id, tsl_index) :
    purge l'éventuel index précédent de cette source sur cette connexion, puis pose le
    nouvel index (INSERT OR REPLACE → vole l'index à une autre source le cas échéant).
    `tsl_index` None/"" efface le mapping de cette source.
    """
    cid = int(connection_id)
    shm = source_shm or ""
    with get_db() as db:
        # purge l'index actuel de cette source sur cette connexion
        db.execute("DELETE FROM tsl_mapping WHERE connection_id=? AND source_shm=?",
                   (cid, shm))
        if tsl_index not in (None, ""):
            db.execute(
                "INSERT OR REPLACE INTO tsl_mapping (connection_id, tsl_index, source_shm) "
                "VALUES (?, ?, ?)", (cid, int(tsl_index), shm))
        db.commit()

# ─── Compat legacy tsl_sources (lecture seule, non plus écrit) ────────────────

def db_get_tsl_sources():
    with get_db() as db:
        return [dict(r) for r in
                db.execute("SELECT * FROM tsl_sources ORDER BY tsl_index").fetchall()]

def db_get_tsl_source_label(shm_or_idx, col):
    """Compat distributor : si appelé avec un shm, délègue à db_get_source_label_for_shm."""
    if isinstance(shm_or_idx, str):
        return db_get_source_label_for_shm(shm_or_idx, col)
    return ""

def db_get_tsl_sources_by_shm():
    """Compat câbles : délègue à source_labels."""
    return db_get_source_labels_by_shm()
