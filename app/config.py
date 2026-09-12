***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Auteur : Cyril Mazouer, pour le compte de BOBI SAS
***REMOVED*** Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import os

***REMOVED*** ── Valeurs par défaut ────────────────────────────────────────────────────────
***REMOVED*** Surcharger dans config_local.py à la racine du projet (non versionné).

***REMOVED*** B1b-2 : full-Docker, host-ops par-nœud — les hôtes vivent dans la table `nodes`. PROXMOX_* RETIRÉS.
***REMOVED*** Plan conteneurs : subnet/passerelle/pool d'IP = cluster ; carte parent + VLAN (si trunk) = PAR-NŒUD.
***REMOVED*** ─── Catalogue des paquets (plugins et services) ─────────────────────────────
***REMOVED*** Organisation GitHub d'où le catalogue lit les paquets publiés, ET d'où l'on
***REMOVED*** apprend qu'une nouvelle version de Bobi.Studio est sortie
***REMOVED*** (`catalogue.maj_core_disponible`). La surcharge dans `config_local.py`
***REMOVED*** n'existe que pour un fork ou un banc d'essai.
***REMOVED***
***REMOVED*** ⚠ Ce commentaire a longtemps affirmé que « les mises à jour de Bobi.Studio
***REMOVED*** viennent de là, par définition » — c'était une INTENTION que le code n'avait
***REMOVED*** jamais réalisée : `_construire()` ne retient que les dépôts préfixés
***REMOVED*** `bobistudio-plugin-` / `bobistudio-service-`, et celui de l'orchestrateur
***REMOVED*** tombait dans le `continue`. La détection existe depuis le 2026-09-02 ;
***REMOVED*** l'application, elle, attend qu'une release porte l'artefact du builder.
***REMOVED***
***REMOVED*** ⚠⚠ SI VOUS CHANGEZ CETTE VALEUR, SOYEZ SÛR DE VOTRE SOURCE. Installer un plugin
***REMOVED*** depuis le catalogue dépose son code sur le contrôleur, et son fichier `hooks.py`
***REMOVED*** est IMPORTÉ ET EXÉCUTÉ DANS L'ORCHESTRATEUR — c'est l'unique exception à la règle
***REMOVED*** « aucun code de plugin in-process », et elle donne à ce code les droits du
***REMOVED*** contrôleur : la base, les jetons d'agent, le réseau de contrôle. Pointer une
***REMOVED*** organisation dont vous ne maîtrisez pas les dépôts revient à laisser un tiers
***REMOVED*** exécuter ce qu'il veut sur votre régie.
***REMOVED***
***REMOVED*** C'est aussi pourquoi cette valeur n'est PAS un réglage d'interface : l'exposer
***REMOVED*** dans la page Réglages ferait de la permission `settings.edit` un droit
***REMOVED*** d'exécution de code arbitraire. La changer exige un accès au serveur — c'est
***REMOVED*** volontaire, et c'est le seul garde-fou du mécanisme.
CATALOGUE_ORG   = "bob-integration"

CHECK_INTERVAL  = 5
DB_PATH         = "/opt/bobistudio/db_bobistudio.db"
LOG_PATH        = "/opt/bobistudio/bobistudio.log"
***REMOVED*** Rotation du journal (anti-saturation disque). La TAILLE prime : disque borné à
***REMOVED*** LOG_MAX_MB × (LOG_BACKUPS + 1). LOG_ROTATE_DAYS = rotation aussi par temps (0 = off). Réglables
***REMOVED*** en base (Réglages → Système → Base / Journaux) ; ci-dessous = simples défauts.
LOG_MAX_MB      = 50
LOG_BACKUPS     = 3
LOG_ROTATE_DAYS = 7

***REMOVED*** ── mTLS du plan de contrôle ──────────────────────────────────────────────────
***REMOVED*** Matériel de CA interne (racine + cert contrôleur) : fichiers à droits 600, hors DB, hors git.
***REMOVED*** Généré une fois par `tools/ca-init.py`. Répliquer ce dossier (au moins ca.key) sur le
***REMOVED*** contrôleur standby (HA) pour qu'il puisse re-signer après un failover — jamais dans le
***REMOVED*** snapshot SQLite. Surchargeable dans config_local.py (ex. montage chiffré dédié).
TLS_DIR         = "/opt/bobistudio/tls"

***REMOVED*** ── Arbre servi par Flask ─────────────────────────────────────────────────────
***REMOVED*** Racine du projet, dérivée de l'emplacement de CE fichier (qui vit dans app/). À dériver ICI et
***REMOVED*** nulle part ailleurs : l'expression `dirname(dirname(__file__))` recopiée telle quelle dans un
***REMOVED*** module de `app/routes/` compte un niveau de trop et désigne `app/static/uploads/`, où Flask ne
***REMOVED*** sert rien. C'est ce qui a fait disparaître le logo d'entreprise — l'upload répondait « ok », le
***REMOVED*** fichier était bien écrit, et l'URL enregistrée pointait sur un 404 (constaté le 2026-08-12).
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR     = os.path.join(BASE_DIR, "static", "uploads")

***REMOVED*** ── Surcharge locale ──────────────────────────────────────────────────────────
try:
    ***REMOVED*** config_local.py est à la racine du projet (dans sys.path au démarrage).
    ***REMOVED*** (Les clés Proxmox éventuelles d'un config_local.py de site sont importées mais inertes.)
    from config_local import *          ***REMOVED*** noqa: F401,F403
except ImportError:
    pass
