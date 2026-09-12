# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Settings accessor : DB d'abord (table `settings`), puis fallback sur les
constantes de config.py. Permet d'éditer la config via l'UI sans toucher au code.
"""
import logging
import os   # réglage `timezone` : pose TZ dans l'environnement du process (cf. apply_timezone)

from . import config
from .database import db_get_setting, db_set_setting, db_get_all_settings

log = logging.getLogger(__name__)

# Bornes de `mxl_history_ms` (durée d'historique du bus MXL, PAR NŒUD). grainCount = durée ×
# cadence : à la cadence la plus LENTE de la flotte (25 fps), une durée < 100 ms tombe à ≤ 2
# grains — trop près de « moins d'une case » (le lecteur en retard relit une case déjà recyclée,
# la ligne de temps se corrompt) pour rester un garde-fou plutôt qu'un piège. Plafond 2000 ms
# (10× le défaut 200 ms) : au-delà, aucun cas d'usage connu — seulement plus de latence tolérée
# pour un lecteur en retard, sans bénéfice mesuré (ce n'est PAS un levier RAM, cf. commentaire
# `_BASE_DEFAULTS`).
MXL_HISTORY_MS_MIN = 100
MXL_HISTORY_MS_MAX = 2000


# ─── Defaults de base (hors core plugins) ────────────────────────────
_BASE_DEFAULTS = {
    # B1b-2 : full-Docker, host-ops PAR-NŒUD. Les hôtes vivent dans la table `nodes` (résolus via
    # app.addressing) — `proxmox_host` RETIRÉ. Clés client-API Proxmox + template 299 déjà purgées.
    # `net_*`/`ip_*`/`gateway` restent : réutilisés par le plan réseau macvlan (→ B2).
    # ★ PAGE RECETTE, ÉTEINTE PAR DÉFAUT. Le suivi de campagne sert pendant la
    # mise en service ; passée la livraison, c'est une entrée de menu qui ne mène
    # nulle part. Le défaut vaut donc pour une installation NEUVE — qui n'a pas
    # encore de campagne — et non pour la nôtre, où le réglage est posé en base.
    "testplan_enabled": "0",

    # ── Catalogue des paquets publiés ────────────────────────────────────────
    # ⚠ L'ORGANISATION N'EST PAS ICI, ET C'EST VOULU : elle vit dans
    # `config.CATALOGUE_ORG`, hors de portée de la page Réglages. C'est le seul
    # point de confiance du mécanisme (installer un plugin exécute son `hooks.py`
    # dans l'orchestrateur) ; le rendre modifiable depuis le web revenait à faire
    # de `settings.edit` un droit d'exécution de code arbitraire.
    # Ne restent réglables que l'interrupteur et la durée de cache.
    "catalogue_actif":  "1",
    # Activer un paquet dès sa récupération. Par DÉFAUT oui : récupérer sans activer
    # donne un plugin qui n'apparaît nulle part, et l'exploitant croit que rien ne s'est
    # passé. Décocher sert à préparer une version sans la mettre en service.
    "catalogue_activer": "1",
    "catalogue_ttl_s":  1800,

    "vmid_start":       200,
    "vmid_end":         299,

    # Rotation du journal de l'orchestrateur (anti-saturation disque). Taille PRIORITAIRE : le disque
    # est borné à log_max_mb × (log_backups + 1). log_rotate_days = rotation aussi par temps (0 = off).
    # Pris en compte au (re)démarrage de l'orchestrateur (cf. app/logsetup.py).
    # `log_rotation_active` : interrupteur EXPLICITE. À 0, le journal grossit sans limite — c'est un
    # choix qu'on doit pouvoir faire (capturer un incident long sans perdre le début), pas un état
    # dans lequel on tombe. Les trois valeurs ci-dessous sont appliquées À CHAUD (pas de
    # redémarrage) par `logsetup.appliquer_reglages()`.
    "log_rotation_active": True,
    "log_max_mb":       50,
    "log_backups":      3,
    "log_rotate_days":  7,
    # Espace libre minimal sur la partition qui porte le journal, sous lequel on ALERTE (Go).
    # Un journal non tourné a déjà tué un nœud (2026-06) et failli tuer l'orchestrateur (2,3 Go en
    # 10 min le 2026-07-11) : la place restante est le seul chiffre qui dit si on va dans le mur.
    "log_disk_free_min_gb": 5,

    # Pilote de log Docker posé sur les conteneurs (Réglages → Système → Journalisation).
    # `journald` (recommandé) délègue la rotation à journald (cf. JOURNALD_UNIT_CONF plus bas) ;
    # `json-file` reste borné en dur (50 Mo × 5, cf. app/journal.py:log_opts) — un json-file non
    # borné a déjà saturé un disque (225 Go en 13 h). Lu par app/journal.py:driver(). S'applique
    # aux conteneurs créés APRÈS le changement, pas à ceux déjà lancés.
    "container_log_driver": "journald",  # "journald" | "json-file"

    # Seuils d'alerte « plus assez de CPU » (Réglages → Nœuds & Matériel → CPU).
    # PSI = la seule métrique qui distingue « chargé et sain » de « en train d'étouffer » : un %
    # d'occupation ne le peut pas, et le throttling CFS ne dit RIEN ici (cpuset partagé, pas de
    # quota → nr_throttled reste à 0 pendant qu'un mur rate 98 % de ses trames). Cf. app/cpu_pressure.py.
    "cpu_psi_cont_warn":  10.0,   # conteneur, PSI « full » (% du temps où TOUTES ses tâches attendent)
    "cpu_psi_cont_err":   25.0,
    "cpu_psi_host_warn":  30.0,   # nœud, PSI « some » (% du temps où au moins une tâche attend)
    "cpu_psi_host_err":   60.0,
    # Cadence non tenue (app/metrics._check_cadence) : fps OBSERVÉ vs cadence CIBLE. Les deux étaient
    # collectés sans jamais être confrontés — c'est le signal le plus direct (il mesure le résultat).
    "fps_target_ratio":   0.90,   # fraction de la cible sous laquelle on considère qu'il a décroché
    "fps_low_samples":    12,     # ticks CONSÉCUTIFS avant d'armer (1 tick = 5 s → ~60 s)

    # B3-2 — paire de contrôleurs HA. Rôle de CE contrôleur : "active" pilote (surveillance,
    # services NMOS/Ember+/ATEM/TSL, sampler PTP, backup) ; "standby" boote passif (UI lecture
    # seule, aucun pilotage) en attente d'une bascule manuelle. Défaut "active" = boot actuel.
    "control_role":     "active",          # "active" | "standby"
    # B3-2b — réplication d'état active→standby. L'actif pousse un snapshot SQLite cohérent vers
    # `ha_standby_url` (vide → réplication OFF) toutes les `ha_replicate_interval_min` minutes. Le
    # secret partagé = `update_token` (déjà partagé pour le pull/push code). Le standby stage le
    # snapshot (ne l'applique pas à chaud — l'application = promote, B3-2c).
    "ha_standby_url":            "",
    "ha_replicate_interval_min": 5,
    # Chien de garde du STANDBY : l'actif qui tombe ne produit AUCUN signal (le standby est passif
    # par conception). Il sonde donc l'autre contrôleur (même URL : quand je suis actif il est mon
    # standby, quand je suis en veille il est mon actif) et ARME une alarme après N échecs. Il ne
    # bascule pas tout seul — la promotion reste la décision de l'opérateur (cf. HA.md).
    "ha_watchdog_interval_s":    15,
    "ha_watchdog_fails":         4,     # 4 × 15 s ≈ 60 s avant d'armer
    # VIP de management via keepalived (VRRP) — opt-in. La priorité VRRP est dérivée du RÔLE, donc
    # une bascule planifiée (promote/demote) déplace l'adresse toute seule ; un actif qui MEURT la
    # perd via le track_script (Flask ne répond plus). L'adresse suit la disponibilité, pas le
    # pilotage : arriver sur un standby en lecture seule est voulu — c'est là qu'est « Promouvoir ».
    "vip_enabled":            0,
    "vip_address":            "",       # CIDR, ex. x.x.x.x/24
    "vip_interface":          "",       # interface de management, ex. eth0
    "vip_vrid":               51,       # doit être UNIQUE sur le segment L2
    "vip_auth_pass":          "",       # secret VRRP, identique sur les deux (généré si vide)
    "vip_priority_active":    150,
    "vip_priority_standby":   100,

    # Tissu de composition (auto-sharding des multiviews saturés) — cf. compositor_fabric /
    # deploy.reconcile_fabric_node. Off par défaut (opt-in). budget = latence-trame seuil (ms).
    "fabric_auto":      "off",
    "fabric_budget_ms": 20,
    # Tuiles max par shard (grain du sharding). Petit = beaucoup de process (overhead GPU par-process,
    # cf. banc : 4 tuiles/process sature) ; gros = process plus denses, moins d'overhead (sweet spot
    # ~20-30 tuiles/process, latence < budget). Borné par la latence par-shard (fabric_budget_ms).
    "fabric_max_cells": 4,
    # Filet anti-régression GPU : force le multiview sur le chemin numpy ÉPROUVÉ (xp=np) même sur un
    # nœud GPU. Repli instantané (au redéploiement) si le chemin cupy posait souci. off par défaut.
    "multiview_force_cpu": "off",
    # Mode tranche GLOBAL (Réglages → Vidéo) : les containers compatibles (config_schema porte
    # slice_mode) sont déployés en tranche via plugins.effective_deploy_defaults ; l'explicite
    # (POST/params persistés) prime. Les flux entrelacés restent en image entière (repli côté
    # script). Prise d'effet au (re)déploiement de chaque container. Le tissu suit aussi ce
    # réglage (deploy._reconcile_fabric_node_impl : fabric_slice_mode OU slice_mode_global).
    # Les clés tranche par-plugin sont hidden:true dans les UI — ce switch est LE réglage.
    "slice_mode_global": False,

    # Auto-recovery au reboot d'un nœud (app/node_recovery.py). OFF par défaut (activation
    # consciente par l'opérateur) ; overridable PAR NŒUD via node_settings (setting_for).
    # Même désactivé, la DÉTECTION du reboot reste active (alerte « relever depuis l'UI »).
    "auto_recovery_enabled":      0,
    "auto_recovery_grace_s":      45,   # laisser Docker (unless-stopped) + systemd (PTP) relever
    "auto_recovery_max_attempts": 2,    # tentatives par conteneur (puis alerte error, on n'y revient plus)
    "auto_recovery_backoff_s":    20,   # pause entre deux tentatives sur le même conteneur

    # Alarmes antenne (audit A5/A6/B3) — cf. Réglages → Système → Alarmes.
    "signal_alerts_enabled":           1,    # noir/gel/silence remontés par le moteur 2110_io (metrics.py)
    "ptp_alerts_enabled":              1,    # pont des événements PTP warning/error vers le fil d'alertes
    "ptp_unlock_err_s":                30,   # unlock prolongé (holdover de fait) → alerte error
    # Horloges du cluster (Réglages → Réseau → Horloges). Le seuil est un réglage et non une
    # constante : la durée d'un grain dépend de la cadence de l'exploitant (20 ms à 50 fps,
    # 40 ms à 25, 16,7 ms à 60), et c'est elle qui définit à partir de quand deux nœuds ne
    # désignent plus la même image.
    # 0 = DÉRIVÉ du métier du nœud (demi-grain pour un compute, µs pour un porteur de moteur 2110,
    # cf. app/clocks.py). Une valeur > 0 REMPLACE la dérivation — à ne poser que si l'on sait
    # quelque chose qu'elle ignore.
    "clock_local_offset_us":           0,
    # Source NTP COMMUNE à tous les nœuds (CSV/espaces). Vide = on ne touche à rien, chaque nœud
    # garde ce qu il a. Une source commune rend les nœuds comparables : deux serveurs différents,
    # ce sont deux idées du temps, et leur écart se retrouve entre les nœuds sans qu on sache d où
    # il vient. Sans effet sur un nœud 2110 (son heure vient du grandmaster).
    "ntp_servers":                     "",
    "clock_alerts_enabled":            1,    # signaler un nœud hors grille / un écart excessif
    "script_restart_alert_threshold":  3,    # redémarrages consécutifs avant alerte error
    "script_restart_quarantine_count": 10,   # échecs consécutifs → quarantaine crash_loop (plus d'auto-restart)
    "script_restart_backoff_max_s":    300,  # plafond du backoff exponentiel entre tentatives

    # Canal WEBHOOK du service d'alertes (services/alerting/channels/webhook.py) — cf. Réglages →
    # Système → Alarmes. TOUTE la chaîne d'alerte était en PULL (il fallait ouvrir une page) :
    # c'est la cause directe d'une panne restée 8 jours sans réaction. Désactivé par défaut, aucune
    # valeur de site en dur. Ces clés gardent leur nom historique (elles sont déjà configurées en
    # production) ; les réglages de la DÉCISION et du canal e-mail sont `alerting_*`, déclarés par
    # le manifeste du service.
    "alert_webhook_enabled":   0,
    "alert_webhook_url":       "",         # POST JSON ; vide = jamais d'envoi
    "alert_webhook_min_level": "error",    # info | warning | error (niveau MINIMUM poussé)
    "alert_webhook_timeout_s": 5,          # timeout COURT : l'envoi est hors chemin critique
    # Anti-tempête : les alertes éligibles d'une même fenêtre partent dans UN SEUL POST (le journal
    # peut recevoir des dizaines d'alertes/minute en cas de flap). 0 = un POST par alerte.
    "alert_webhook_window_s":  60,

    # B2 — topologie réseau cluster. simple = management + conteneurs sur UN réseau (= actuel) ;
    # separated = réseau management + réseau conteneurs PRIVÉ dédié (VLAN trunké, /16 hors LAN). Le
    # plan 2110 est toujours physiquement séparé (NIC média dédiée, AF-XDP). En separated, l'orchestrateur est
    # multi-homé (3 réseaux). L'IPAM centralisé + le câblage deploy = B2-2 (B2-1 = modèle + réglages).
    "net_topology":     "simple",          # "simple" | "separated"
    # Plan CONTENEURS (macvlan) — réglages historiques (net_*/ip_*/gateway).
    "net_mode":         "dhcp",            # "dhcp" | "static"
    # ⚠ VIDES À DESSEIN. Ces trois-là portaient des adresses de SITE — la charte
    # dit qu'aucune valeur de site n'est codée en dur (cf. config_local.py), et
    # une plage d'IP par défaut est exactement le genre de valeur qu'on déploie
    # sans la relire. Le mode par défaut est `dhcp`, qui ne s'en sert pas ; en
    # mode `static`, l'exploitant DOIT les poser, et c'est mieux ainsi.
    "ip_start":         "",
    "ip_end":           "",
    "netmask_bits":     24,                # /24
    "gateway":          "",
    # Plan MANAGEMENT (separated seulement, informatif en B2-1 ; hôtes statiques, pas d'IPAM).
    "mgmt_subnet":      "",
    "mgmt_vlan":        "",
    # B2-3 — pool multicast 2110 cluster-unique (alloué par l'orchestrateur dans le registre NMOS).
    "mcast_pool_base":    "239.100.0.0",   # 1ᵉʳ groupe du pool
    "mcast_pool_size":    4096,            # nb de groupes (base .. base+size-1)
    "mcast_port_default": 5000,            # port par défaut d'un flux

    # FUSEAU HORAIRE du système (nom IANA, ex. "Europe/Paris"). Vide = suivre le fuseau de l'OS du
    # contrôleur (comportement historique). Posé dans TZ au démarrage → gouverne d'un seul geste les
    # journaux, les horodatages d'alertes, les dates affichées dans l'UI, et le `tz` injecté aux
    # conteneurs qui affichent l'heure civile (cf. ptp.civil_clock_params → multiview/avsync).
    # ⚠ Ne RÉINTERPRÈTE PAS les horodatages déjà stockés en base : seules les écritures futures
    # suivent le nouveau fuseau.
    "timezone":              "",

    # Mise à jour entre instances (pull/push) — mode serveur + token partagé
    "update_server_enabled": False,
    "update_token":          "",

    # PTP (IEEE 1588 / SMPTE 2059-2) — synchro horloge pour 2110
    "ptp_enabled":   False,
    "ptp_ifname":    "",             # NIC pour PTP (typiquement la PF SR-IOV)
    "ptp_domain":    127,            # SMPTE 2059-2 par défaut
    # PTP multi-NIC : domaine PTP qui discipline l'horloge système (CLOCK_REALTIME). Un seul
    # phc2sys « système » même avec plusieurs domaines. NULL/absent → plus petit domaine actif.
    "ptp_primary_domain": None,
    "ptp_hw_ts":     True,           # hardware timestamping (faux = software, jitter ms)
    # Client uniquement (BMCA) : le nœud ne se proclame JAMAIS grandmaster, même en perte
    # d'Announce (reste en LISTENING). Un nœud média consomme un GM maison externe → défaut True.
    # Évite qu'un faux-GM clignotant (Announce perdus) pollue le domaine PTP des autres nœuds.
    "ptp_client_only": True,
    # Gating broadcast au câblage : refuse une source incompatible (résolution/chroma/cadence) avec
    # un consommateur non-adaptateur, avec une raison. Désactivable (passe en avertissement seul).
    "wire_format_gating": True,

    # Étage 1 docs/reference/TX_LAYOUTS.md : arbre TX statique au boot du moteur 2110_io. Tout slot TX avec une
    # destination déclarée (mcast+port) est poussé `provisioned=True` au contrôleur (session RL
    # silencieuse dès la déclaration, pas d'attente du câblage) — le câblage devient un swap de
    # source (zéro rte_tm_hierarchy_commit). Garde-fou site : False = repli sur le comportement
    # historique (session créée seulement au câblage, un TX vivant peut alors perdre des mbufs).
    "tx_layout_provisioning_enabled": True,
    # Étage 3 docs/reference/TX_LAYOUTS.md : gate de FORMAT sur les slots TX du moteur. Câbler une source dont le
    # format diffère du format déclaré du slot recrée la session (commit TM = gel de TOUTES les
    # sorties de la carte, +9 commits mesurés) et fait ÉMETTRE au TX autre chose que ce qu'il ANNONCE.
    # L'écart doit être résolu (UDC, ou alignement du slot) : il n'y a pas de « forcer ».
    # False = repli sur le comportement historique (câble accepté, session recréée sans avertir).
    "tx_format_gating": True,
    # Changement de format d'une source EN EXPLOITATION (une caméra bascule) : le watcher ALERTE
    # toujours, puis insère automatiquement un UDC (swap de source = zéro commit, la sortie continue
    # d'émettre pendant le démarrage de l'UDC). False = l'alerte reste, l'insertion est manuelle.
    "tx_format_autoudc": True,
    # Role auto (BMCA) — pas de setting explicite : ptp4l décide

    # Formats vidéo prédéfinis (palette de déploiement). Une ligne :
    #   Nom;Largeur;Hauteur;FPS;Scan(i/p);Chroma(420|422|444);BitDepth(8|10|12);Colorimétrie
    # Colorimétrie : 709 | 2020 | 2020pq | 2020hlg | 601. Champs au-delà de Scan optionnels
    # (défauts broadcast ST 2110-20 : 422 / 10 bits / 709). Anciennes lignes 5 champs OK.
    # ⚠ Colonne FPS pour l'entrelacé : elle compte les CHAMPS, jamais les trames (1080i50 → 50).
    # Deux lignes livrées étaient fausses de ce point de vue et ont été corrigées le 2026-08-15 :
    # « SD-SDI PAL » portait 25 (soit 12,5 trames/s) au lieu de 50, et « HD 1080i59.94 » portait
    # 29,97 (soit 14,985 trames/s) au lieu de 59,94. Migration de correction dans `init_db`.
    "video_formats": ("Test 640×360p25;640;360;25;p;422;10;709\n"
                      "SD-SDI PAL;720;576;50;i;422;10;601\n"
                      "HD 1920×1080i50;1920;1080;50;i;422;10;709\n"
                      "3G 1920×1080p50;1920;1080;50;p;422;10;709\n"
                      "UHD 3840×2160p50;3840;2160;50;p;422;10;2020\n"
                      "HD 1920×1080i59.94;1920;1080;59.94;i;422;10;709\n"
                      "3G 1920×1080p59.94;1920;1080;59.94;p;422;10;709\n"
                      "HD 1920×1080p29.97;1920;1080;29.97;p;422;10;709"),
    "video_format_default": "",      # label du format pré-sélectionné dans la palette de déploiement

    # Pipeline MXL — tailles des ring buffers shared memory
    "shm_video_ring": 8,    # grains vidéo en mémoire (ring buffer YUV) — borné 2..8 (MTL st20 : ring ≤ 8)
    "shm_audio_ring": 100,  # grains audio en mémoire (ring buffer L24/48k)
    # Ptime audio ST 2110-30 par DÉFAUT (ms) selon l'installation : "1" (1 ms) | "0.125" (125 µs).
    # Repli uniquement : un SDP avec a=ptime PRIME (auto par entrée, géré par mtl_rx/controller).
    "mtl_audio_ptime": "1",
    # Budget CPU du moteur 2110_io : quota Mb/s par scheduler libmtl (1 lcore) — au-delà, les
    # nouvelles sessions vont sur un autre lcore. C'est LA manette de calibrage CPU : la capacité
    # réelle d'un cœur dépend du CPU/de la bande passante mémoire (memcpy AF_XDP) → défaut PRUDENT
    # 2500 (≈ 2×1080p50), à ajuster par site (Réglages → MXL). L'ancien défaut 5000 tassait 4-5
    # sessions 1080p50 par scheduler (boucle ≈ inter-paquet) → wedge OFO en cascade sous jitter
    # (incident lab Horace 2026-07-06). Le dimensionnement des lcores (_auto_lcores) et le
    # garde-fou d'admission (_mtl_lcore_sessions) dérivent tous deux de ce quota.
    "mtl_sch_quota_mbs": 2500,
    # Budget de temps (s) pour pousser les slots TX au contrôleur après une (re)création du moteur.
    # Une ÉCHÉANCE, pas un compte d'essais : l'agent :8081 répond bien avant que le contrôleur
    # n'accepte /tx (mtl_init met 30-60 s sur E810). L'ancien budget implicite de 5 s par slot
    # laissait un moteur recréé SANS AUCUNE SORTIE, en silence (incident 2026-07-28).
    "mtl_tx_push_timeout_s": 120,
    # Attente MAX de la fin de `mtl_init` avant de configurer un moteur qui vient d'être (re)créé.
    # Distinct de la readiness :8081, qui ne prouve que la vivacité du contrôleur (cf.
    # docker_driver.moteur_initialise). Sur E810 100G, l'entraînement du lien seul prend 60-90 s.
    "mtl_init_wait_s": 90,
    # Pinning cœurs du moteur 2110_io : réserve les lcores dans node_core_alloc (jamais donnés aux
    # containers compute pinnés) et pose --cpuset-cpus sur le conteneur moteur (lcores + 2 cœurs
    # pour le contrôleur Python). Garde-fou nproc côté nœud (pas de cpuset hors machine).
    "mtl_pin_cores": True,
    # Niveau de log du moteur 2110_io (libmtl) — injecté en env MTL_LOG_LEVEL au déploiement, lu par
    # mtl_rx.c. Valeurs : debug|info|notice|warning|err. DÉFAUT "warning" (silencieux) : à INFO,
    # libmtl émet périodiquement un dump de stats volumineux (bloc « END STATE » + SCH/xdp_queue)
    # qui noie les logs du moteur. Les niveaux ≥ INFO ne servent qu'au diagnostic ponctuel. Le
    # niveau EFFECTIF de chaque moteur est tracé dans params.mtl_log_level (cf. docker_driver) pour
    # repérer les moteurs restés verbeux. Valeur inconnue → mtl_rx retombe sur warning.
    "mtl_log_level": "warning",  # debug|info|notice|warning|err
    # NB : `nmos_label_prefix` (préfixe des libellés NMOS, override par nœud) est déclaré dans
    # services/nmos/manifest.json, avec ses clés sœurs — une seule source de vérité.
    # Profondeur de bits du pipeline shm — arbitrage orchestrateur (cf. deploy._apply_pipeline_bit_depth) :
    #   force8 → 8 bits imposé partout (défaut, perfs/mémoire actuelles, zéro régression)
    #   follow → profondeur du format vidéo (8/10/12 ; 10/12 bits = ×2 mémoire shm)
    "mxl_pipeline_bit_depth": "force8",  # "force8" | "follow"

    # Fenêtre d'historique du bus MXL (millisecondes), PAR NŒUD — pas par container. Le SDK MXL
    # (option `urn:x-mxl:option:history_duration/v1.0`, `/dev/shm/mxl/options.json`) exprime la
    # profondeur des ring buffers comme une DURÉE, pas un nombre de trames : le SDK dérive
    # grainCount = durée × cadence (défaut 200 ms → 10 cases à 50 fps, 5 à 25 fps ; l'audio reçoit
    # le double de la durée). Réglage PAR-NŒUD délibérément : le SDK ignore l'option posée au
    # niveau instance. Un changement ne s'applique QU'AUX FLUX CRÉÉS ENSUITE (ring déjà alloué =
    # inchangé). C'est une fenêtre de LATENCE tolérée pour un lecteur en retard, pas un levier
    # d'économie de RAM (mesuré 2026-08-09 : 333 Mo pour 47 Go de tmpfs, soit 0,7 %). Bornes
    # [MXL_HISTORY_MS_MIN, MXL_HISTORY_MS_MAX] validées côté serveur dans `set()` ci-dessous.
    "mxl_history_ms": 200,
    # Lot de synchronisation RDMA (`maxSyncBatchSizeHint`), en TRANCHES. Vide = défaut du SDK
    # (= totalSlices), c'est-à-dire « attendre la trame entière avant de transférer ». Mesuré le
    # 2026-08-09 : 1ʳᵉ bande lisible sur la réplique à 22,63 ms au défaut, 0,54 ms à 2 tranches,
    # pour un débit et un nombre de paquets identiques. Injecté par `docker_compute` en
    # MXL_SYNC_BATCH, lu par `bobimxl._flow_options()`. N'agit que sur les flux CRÉÉS ENSUITE.
    "mxl_sync_batch": "",

    # Hébergement des fichiers d'installation (/install.sh, /install/*) pour le
    # déploiement en one-liner sur un nœud Proxmox. Activable depuis Réglages.
    "install_hosting_enabled": True,
    # Hôte de build des images runtime PARTAGÉES (compute/media/webrtc) : "" (auto) | "local"
    # (docker de l'orchestrateur) | "<node_id>". Cf. routes._build_target. L'image est distribuée
    # à tous les nœuds concernés après le build (auto-push).
    "image_build_node": "",

    # Premier démarrage : faux tant que l'assistant de configuration (/setup/wizard)
    # n'a pas été terminé ou explicitement sauté. Tant qu'il est faux, une connexion
    # réussie redirige vers l'assistant.
    "setup_completed":  False,

    # Exigence sur les mots de passe : `souple` | `standard` | `stricte` (cf. auth.PWD_PROFILS).
    # Ne s'applique qu'aux mots de passe SAISIS ENSUITE — les existants sont stockés en
    # empreinte, donc illisibles : durcir le profil ne peut pas les invalider rétroactivement.
    "pwd_profil":       "standard",

    # Apparence
    "theme":            "classic",   # classic | studio | light
    # Langue d'interface par défaut (i18n) — repli quand l'utilisateur n'a pas de
    # préférence propre (users.lang). Codes : voir app/i18n.LANGUAGES.
    "ui_lang_default":  "fr",
    # Langues personnalisées créées via l'éditeur de traductions (i18n) :
    # liste [{code, label}]. Pas de fichier catalogue → uniquement surcouche DB.
    "ui_custom_languages": [],

    # Personnalisation client (identité du déploiement, EN PLUS de la marque produit) :
    # nom du système, entreprise, logo (chemin /static/uploads/…), emplacement.
    "brand_system_name": "",
    "brand_org_name":    "",
    "brand_logo_url":    "",
    "brand_location":    "",
    # ⚠ N'ACTIVER QUE SI L'ORCHESTRATEUR N'EST JOIGNABLE QU'À TRAVERS UN REVERSE-PROXY DE
    # CONFIANCE. `X-Forwarded-For` est envoyé par le CLIENT : si la machine est joignable en
    # direct, n'importe qui peut s'annoncer à l'adresse de son choix, et le filtre d'adresses
    # des liens publics n'aurait plus que l'apparence d'un filtre. Off par défaut : on prend
    # alors le vrai pair TCP, inforgeable.
    "public_trust_proxy": False,

    # Sauvegarde quotidienne automatisée de la DB (vers backups/)
    "backup_enabled":   False,
    "backup_time":      "02:00",     # heure locale serveur HH:MM
    "backup_retention": 14,          # nombre de sauvegardes conservées
    # État runtime (écrit par le scheduler, lu par l'UI)
    "backup_last_date":   "",        # YYYY-MM-DD du dernier backup réussi (anti double-run)
    "backup_last_status": "",        # message du dernier run
    "backup_last_file":   "",        # nom du dernier fichier produit
}


_CORE_DEFAULTS = None  # chargé paresseusement à la première requête


def _get_core_defaults():
    global _CORE_DEFAULTS
    if _CORE_DEFAULTS is None:
        try:
            from . import core_plugins
            _CORE_DEFAULTS = core_plugins.all_settings_defaults()
        except Exception:
            _CORE_DEFAULTS = {}
    return _CORE_DEFAULTS


DEFAULTS = _BASE_DEFAULTS

# Liste des thèmes connus + leur description (pour le sélecteur UI)
THEMES = [
    {"id": "classic", "label": "Classic — terminal dark (par défaut)"},
    {"id": "studio",  "label": "Studio — broadcast pro, accent amber"},
    {"id": "light",   "label": "Daylight — clean light, accent indigo"},
]

def get(key, default=None):
    """Valeur d'un réglage : DB > DEFAULTS > défauts des services > `default`.
    (`default` accepté depuis 2026-07-04 — des appelants passaient déjà un 2ᵉ argument,
    ce qui levait TypeError : cf. routes._format_gate/wire_format_gating.)"""
    val = db_get_setting(key, None)
    if val is None:
        if key in DEFAULTS:
            return DEFAULTS[key]
        return _get_core_defaults().get(key, default)
    return val

def apply_timezone():
    """Pose le fuseau du réglage `timezone` dans le PROCESS (TZ + tzset). C'est le levier unique :
    tout ce que le contrôleur rend en heure locale en découle — journaux (logging utilise
    time.localtime), horodatages d'alertes, dates de l'UI — ainsi que le `tz` injecté aux
    conteneurs (cf. ptp.civil_clock_params). Réglage vide → on ne touche à RIEN, l'OS fait foi.
    Renvoie le nom appliqué, ou "" si aucun. Un fuseau inconnu est REFUSÉ et journalisé plutôt
    qu'appliqué de travers : `time.tzset()` accepte silencieusement n'importe quoi et retomberait
    sur UTC, ce qui donnerait des journaux faux sans le moindre signal."""
    import time as _time
    tz = (get("timezone") or "").strip()
    if not tz:
        return ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)                      # valide contre la tzdata réellement présente
    except Exception as e:
        log.warning("réglage timezone : fuseau « %s » inconnu (%s) — fuseau de l'OS conservé", tz, e)
        return ""
    os.environ["TZ"] = tz
    try:
        _time.tzset()
    except Exception as e:                # plateformes sans tzset (non-POSIX)
        log.warning("réglage timezone : tzset indisponible (%s)", e)
        return ""
    return tz


def set(key, value):
    # `mxl_history_ms` : une durée nulle ou absurde casserait tous les FLUX CRÉÉS ENSUITE sur le
    # nœud (ring buffer trop court → un lecteur en retard relit une case recyclée). Clamp plutôt
    # que refus silencieux : une saisie hors bornes est ramenée à la borne la plus proche, journalisée.
    if key == "mxl_history_ms":
        try:
            v = int(value)
        except (TypeError, ValueError):
            log.warning("réglage mxl_history_ms : valeur non entière (%r) — ignorée", value)
            return
        clamped = min(MXL_HISTORY_MS_MAX, max(MXL_HISTORY_MS_MIN, v))
        if clamped != v:
            log.warning("réglage mxl_history_ms : %d hors bornes [%d, %d] — ramené à %d",
                        v, MXL_HISTORY_MS_MIN, MXL_HISTORY_MS_MAX, clamped)
        value = clamped
    # `shm_video_ring` : framebuffers DPDK du moteur 2110 (RX ET TX vidéo). Le plancher n'est PAS
    # un optimum choisi, c'est une valeur au-dessus d'un ÉCHEC MESURÉ : le 2026-08-09, un TX 1080p50
    # avec 4 framebuffers (dont 1 immobilisé par la trame de tenue, donc 3 au worker) bloquait le
    # worker 59 % du temps et répétait UNE TRAME SUR QUATRE à l'antenne ; 8 donne 0 % de blocage et
    # 49 trames fraîches/s. Rien entre 5 et 7 n'a été mesuré — 6 est le premier cran laissant une
    # marge réelle au-dessus du défaut connu. Plafond 8 = `ST20_FB_MAX_COUNT`, seule contrainte
    # énoncée par le SDK (`framebuff_cnt ∈ [2, 8]`, aucune recommandation de valeur).
    # Cesse de valoir si quelqu'un mesure 5 ou 6 avec `slot_wait_ms`/`fb_slots` (moteur ≥ 0.85.0).
    if key == "shm_video_ring":
        try:
            v = int(value)
        except (TypeError, ValueError):
            log.warning("réglage shm_video_ring : valeur non entière (%r) — ignorée", value)
            return
        clamped = min(8, max(6, v))
        if clamped != v:
            log.warning("réglage shm_video_ring : %d hors bornes [6, 8] — ramené à %d", v, clamped)
        value = clamped
    db_set_setting(key, value)
    # Le fuseau doit prendre effet SANS redémarrage : sinon les journaux et l'UI continueraient
    # d'afficher l'ancien fuseau jusqu'au prochain restart, et l'utilisateur conclurait que le
    # réglage ne marche pas.
    if key == "timezone":
        apply_timezone()

def setting_for(key, node_id=None):
    """Valeur d'un réglage résolue pour un NŒUD : override par-nœud > global > défaut. Sans node_id
    (ou nœud sans override) → équivalent à get(key). Base de la portée « global + override par
    nœud » de la refonte Réglages."""
    if node_id is not None:
        from .database import db_get_node_setting, _NODE_SETTING_SENTINEL
        ov = db_get_node_setting(node_id, key, _NODE_SETTING_SENTINEL)
        if ov is not _NODE_SETTING_SENTINEL:
            return ov
    return get(key)

def all():
    """Renvoie tous les settings (DB merged sur DEFAULTS + core plugins defaults)."""
    merged = dict(DEFAULTS)
    merged.update(_get_core_defaults())
    merged.update(db_get_all_settings())
    return merged

# Clés à NE JAMAIS exposer sur un chemin de sortie HTTP (fuite de secrets).
# Blacklist explicite (flask_secret_key = clé de signature de session → forge de cookie
# admin ; update_token = secret partagé HA/updater ; pxe_armed_token) + motifs suffixe
# conservateurs (_password/_secret/_token). On NE filtre PAS *_key en générique pour ne
# pas casser des clés fonctionnelles (noms d'interface, etc.).
_SECRET_KEYS = ("flask_secret_key", "update_token", "pxe_armed_token", "vip_auth_pass")
_SECRET_SUFFIXES = ("_password", "_secret", "_token")


def _is_secret_key(key):
    if key in _SECRET_KEYS:
        return True
    return any(key.endswith(sfx) for sfx in _SECRET_SUFFIXES)


def public():
    """Comme all(), mais expurgé des clés sensibles — à utiliser sur TOUT chemin de
    sortie HTTP (l'API settings est lisible par tout compte connecté). NE PAS utiliser
    en interne pour les writes/résolutions qui ont besoin des vraies valeurs (get/set)."""
    return {k: v for k, v in all().items() if not _is_secret_key(k)}


def update_bulk(items):
    """items: dict { key: value }. Ne stocke que les clés connues.

    Retourne (accepted, ignored) : une clé absente de DEFAULTS était JETÉE EN SILENCE, la route
    renvoyant quand même 200/ok — un champ ajouté à l'UI sans sa valeur par défaut ici semblait
    donc s'enregistrer sans jamais rien changer. On remonte désormais les clés ignorées."""
    all_defaults = {**DEFAULTS, **_get_core_defaults()}
    accepted, ignored = 0, []
    for k, v in items.items():
        if k in all_defaults:
            # mxl_history_ms (et tout futur réglage à bornes validées) passe par `set()` : c'est
            # là que vit le clamp serveur — db_set_setting() en direct le contournerait.
            if k == "mxl_history_ms":
                set(k, v)
            else:
                db_set_setting(k, v)
            accepted += 1
        else:
            ignored.append(k)
    if ignored:
        log.warning("settings: clé(s) INCONNUE(S) ignorée(s) — absente(s) de DEFAULTS : %s",
                    ", ".join(sorted(ignored)))
    # Le service d'alertes met ses réglages en cache (il ne peut pas lire la DB sur le chemin
    # critique de db_add_alert) : on l'invalide ici, sinon activer un canal depuis l'UI ne prendrait
    # effet qu'au bout du TTL et les alertes de cet intervalle seraient perdues sans un mot.
    if any(k.startswith("alert_webhook_") or k.startswith("alerting_") for k in items):
        try:
            from services import alerting
            alerting.invalider_cache()
        except Exception:
            log.exception("settings: invalidation du cache du service d'alertes impossible")
    return accepted, ignored

# D Phase 2a : helpers du client API Proxmox (proxmox_token_header / proxmox_url) RETIRÉS
# (proxmox.py supprimé, full-Docker). `proxmox_host` reste un réglage : c'est l'hôte SSH utilisé
# par la couche host-ops (VF/PTP/binds) — sera renommé « hôte » au retargeting par-nœud (B).
