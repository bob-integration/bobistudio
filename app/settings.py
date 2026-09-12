***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Auteur : Cyril Mazouer, pour le compte de BOBI SAS
***REMOVED*** Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Settings accessor : DB d'abord (table `settings`), puis fallback sur les
constantes de config.py. Permet d'éditer la config via l'UI sans toucher au code.
"""
import logging
import os   ***REMOVED*** réglage `timezone` : pose TZ dans l'environnement du process (cf. apply_timezone)

from . import config
from .database import db_get_setting, db_set_setting, db_get_all_settings

log = logging.getLogger(__name__)

***REMOVED*** Bornes de `mxl_history_ms` (durée d'historique du bus MXL, PAR NŒUD). grainCount = durée ×
***REMOVED*** cadence : à la cadence la plus LENTE de la flotte (25 fps), une durée < 100 ms tombe à ≤ 2
***REMOVED*** grains — trop près de « moins d'une case » (le lecteur en retard relit une case déjà recyclée,
***REMOVED*** la ligne de temps se corrompt) pour rester un garde-fou plutôt qu'un piège. Plafond 2000 ms
***REMOVED*** (10× le défaut 200 ms) : au-delà, aucun cas d'usage connu — seulement plus de latence tolérée
***REMOVED*** pour un lecteur en retard, sans bénéfice mesuré (ce n'est PAS un levier RAM, cf. commentaire
***REMOVED*** `_BASE_DEFAULTS`).
MXL_HISTORY_MS_MIN = 100
MXL_HISTORY_MS_MAX = 2000


***REMOVED*** ─── Defaults de base (hors core plugins) ────────────────────────────
_BASE_DEFAULTS = {
    ***REMOVED*** B1b-2 : full-Docker, host-ops PAR-NŒUD. Les hôtes vivent dans la table `nodes` (résolus via
    ***REMOVED*** app.addressing) — `proxmox_host` RETIRÉ. Clés client-API Proxmox + template 299 déjà purgées.
    ***REMOVED*** `net_*`/`ip_*`/`gateway` restent : réutilisés par le plan réseau macvlan (→ B2).
    ***REMOVED*** ★ PAGE RECETTE, ÉTEINTE PAR DÉFAUT. Le suivi de campagne sert pendant la
    ***REMOVED*** mise en service ; passée la livraison, c'est une entrée de menu qui ne mène
    ***REMOVED*** nulle part. Le défaut vaut donc pour une installation NEUVE — qui n'a pas
    ***REMOVED*** encore de campagne — et non pour la nôtre, où le réglage est posé en base.
    "testplan_enabled": "0",

    ***REMOVED*** ── Catalogue des paquets publiés ────────────────────────────────────────
    ***REMOVED*** ⚠ L'ORGANISATION N'EST PAS ICI, ET C'EST VOULU : elle vit dans
    ***REMOVED*** `config.CATALOGUE_ORG`, hors de portée de la page Réglages. C'est le seul
    ***REMOVED*** point de confiance du mécanisme (installer un plugin exécute son `hooks.py`
    ***REMOVED*** dans l'orchestrateur) ; le rendre modifiable depuis le web revenait à faire
    ***REMOVED*** de `settings.edit` un droit d'exécution de code arbitraire.
    ***REMOVED*** Ne restent réglables que l'interrupteur et la durée de cache.
    "catalogue_actif":  "1",
    ***REMOVED*** Activer un paquet dès sa récupération. Par DÉFAUT oui : récupérer sans activer
    ***REMOVED*** donne un plugin qui n'apparaît nulle part, et l'exploitant croit que rien ne s'est
    ***REMOVED*** passé. Décocher sert à préparer une version sans la mettre en service.
    "catalogue_activer": "1",
    "catalogue_ttl_s":  1800,

    "vmid_start":       200,
    "vmid_end":         299,

    ***REMOVED*** Rotation du journal de l'orchestrateur (anti-saturation disque). Taille PRIORITAIRE : le disque
    ***REMOVED*** est borné à log_max_mb × (log_backups + 1). log_rotate_days = rotation aussi par temps (0 = off).
    ***REMOVED*** Pris en compte au (re)démarrage de l'orchestrateur (cf. app/logsetup.py).
    ***REMOVED*** `log_rotation_active` : interrupteur EXPLICITE. À 0, le journal grossit sans limite — c'est un
    ***REMOVED*** choix qu'on doit pouvoir faire (capturer un incident long sans perdre le début), pas un état
    ***REMOVED*** dans lequel on tombe. Les trois valeurs ci-dessous sont appliquées À CHAUD (pas de
    ***REMOVED*** redémarrage) par `logsetup.appliquer_reglages()`.
    "log_rotation_active": True,
    "log_max_mb":       50,
    "log_backups":      3,
    "log_rotate_days":  7,
    ***REMOVED*** Espace libre minimal sur la partition qui porte le journal, sous lequel on ALERTE (Go).
    ***REMOVED*** Un journal non tourné a déjà tué un nœud (2026-06) et failli tuer l'orchestrateur (2,3 Go en
    ***REMOVED*** 10 min le 2026-07-11) : la place restante est le seul chiffre qui dit si on va dans le mur.
    "log_disk_free_min_gb": 5,

    ***REMOVED*** Pilote de log Docker posé sur les conteneurs (Réglages → Système → Journalisation).
    ***REMOVED*** `journald` (recommandé) délègue la rotation à journald (cf. JOURNALD_UNIT_CONF plus bas) ;
    ***REMOVED*** `json-file` reste borné en dur (50 Mo × 5, cf. app/journal.py:log_opts) — un json-file non
    ***REMOVED*** borné a déjà saturé un disque (225 Go en 13 h). Lu par app/journal.py:driver(). S'applique
    ***REMOVED*** aux conteneurs créés APRÈS le changement, pas à ceux déjà lancés.
    "container_log_driver": "journald",  ***REMOVED*** "journald" | "json-file"

    ***REMOVED*** Seuils d'alerte « plus assez de CPU » (Réglages → Nœuds & Matériel → CPU).
    ***REMOVED*** PSI = la seule métrique qui distingue « chargé et sain » de « en train d'étouffer » : un %
    ***REMOVED*** d'occupation ne le peut pas, et le throttling CFS ne dit RIEN ici (cpuset partagé, pas de
    ***REMOVED*** quota → nr_throttled reste à 0 pendant qu'un mur rate 98 % de ses trames). Cf. app/cpu_pressure.py.
    "cpu_psi_cont_warn":  10.0,   ***REMOVED*** conteneur, PSI « full » (% du temps où TOUTES ses tâches attendent)
    "cpu_psi_cont_err":   25.0,
    "cpu_psi_host_warn":  30.0,   ***REMOVED*** nœud, PSI « some » (% du temps où au moins une tâche attend)
    "cpu_psi_host_err":   60.0,
    ***REMOVED*** Cadence non tenue (app/metrics._check_cadence) : fps OBSERVÉ vs cadence CIBLE. Les deux étaient
    ***REMOVED*** collectés sans jamais être confrontés — c'est le signal le plus direct (il mesure le résultat).
    "fps_target_ratio":   0.90,   ***REMOVED*** fraction de la cible sous laquelle on considère qu'il a décroché
    "fps_low_samples":    12,     ***REMOVED*** ticks CONSÉCUTIFS avant d'armer (1 tick = 5 s → ~60 s)

    ***REMOVED*** B3-2 — paire de contrôleurs HA. Rôle de CE contrôleur : "active" pilote (surveillance,
    ***REMOVED*** services NMOS/Ember+/ATEM/TSL, sampler PTP, backup) ; "standby" boote passif (UI lecture
    ***REMOVED*** seule, aucun pilotage) en attente d'une bascule manuelle. Défaut "active" = boot actuel.
    "control_role":     "active",          ***REMOVED*** "active" | "standby"
    ***REMOVED*** B3-2b — réplication d'état active→standby. L'actif pousse un snapshot SQLite cohérent vers
    ***REMOVED*** `ha_standby_url` (vide → réplication OFF) toutes les `ha_replicate_interval_min` minutes. Le
    ***REMOVED*** secret partagé = `update_token` (déjà partagé pour le pull/push code). Le standby stage le
    ***REMOVED*** snapshot (ne l'applique pas à chaud — l'application = promote, B3-2c).
    "ha_standby_url":            "",
    "ha_replicate_interval_min": 5,
    ***REMOVED*** Chien de garde du STANDBY : l'actif qui tombe ne produit AUCUN signal (le standby est passif
    ***REMOVED*** par conception). Il sonde donc l'autre contrôleur (même URL : quand je suis actif il est mon
    ***REMOVED*** standby, quand je suis en veille il est mon actif) et ARME une alarme après N échecs. Il ne
    ***REMOVED*** bascule pas tout seul — la promotion reste la décision de l'opérateur (cf. HA.md).
    "ha_watchdog_interval_s":    15,
    "ha_watchdog_fails":         4,     ***REMOVED*** 4 × 15 s ≈ 60 s avant d'armer
    ***REMOVED*** VIP de management via keepalived (VRRP) — opt-in. La priorité VRRP est dérivée du RÔLE, donc
    ***REMOVED*** une bascule planifiée (promote/demote) déplace l'adresse toute seule ; un actif qui MEURT la
    ***REMOVED*** perd via le track_script (Flask ne répond plus). L'adresse suit la disponibilité, pas le
    ***REMOVED*** pilotage : arriver sur un standby en lecture seule est voulu — c'est là qu'est « Promouvoir ».
    "vip_enabled":            0,
    "vip_address":            "",       ***REMOVED*** CIDR, ex. x.x.x.x/24
    "vip_interface":          "",       ***REMOVED*** interface de management, ex. eth0
    "vip_vrid":               51,       ***REMOVED*** doit être UNIQUE sur le segment L2
    "vip_auth_pass":          "",       ***REMOVED*** secret VRRP, identique sur les deux (généré si vide)
    "vip_priority_active":    150,
    "vip_priority_standby":   100,

    ***REMOVED*** Tissu de composition (auto-sharding des multiviews saturés) — cf. compositor_fabric /
    ***REMOVED*** deploy.reconcile_fabric_node. Off par défaut (opt-in). budget = latence-trame seuil (ms).
    "fabric_auto":      "off",
    "fabric_budget_ms": 20,
    ***REMOVED*** Tuiles max par shard (grain du sharding). Petit = beaucoup de process (overhead GPU par-process,
    ***REMOVED*** cf. banc : 4 tuiles/process sature) ; gros = process plus denses, moins d'overhead (sweet spot
    ***REMOVED*** ~20-30 tuiles/process, latence < budget). Borné par la latence par-shard (fabric_budget_ms).
    "fabric_max_cells": 4,
    ***REMOVED*** Filet anti-régression GPU : force le multiview sur le chemin numpy ÉPROUVÉ (xp=np) même sur un
    ***REMOVED*** nœud GPU. Repli instantané (au redéploiement) si le chemin cupy posait souci. off par défaut.
    "multiview_force_cpu": "off",
    ***REMOVED*** Mode tranche GLOBAL (Réglages → Vidéo) : les containers compatibles (config_schema porte
    ***REMOVED*** slice_mode) sont déployés en tranche via plugins.effective_deploy_defaults ; l'explicite
    ***REMOVED*** (POST/params persistés) prime. Les flux entrelacés restent en image entière (repli côté
    ***REMOVED*** script). Prise d'effet au (re)déploiement de chaque container. Le tissu suit aussi ce
    ***REMOVED*** réglage (deploy._reconcile_fabric_node_impl : fabric_slice_mode OU slice_mode_global).
    ***REMOVED*** Les clés tranche par-plugin sont hidden:true dans les UI — ce switch est LE réglage.
    "slice_mode_global": False,

    ***REMOVED*** Auto-recovery au reboot d'un nœud (app/node_recovery.py). OFF par défaut (activation
    ***REMOVED*** consciente par l'opérateur) ; overridable PAR NŒUD via node_settings (setting_for).
    ***REMOVED*** Même désactivé, la DÉTECTION du reboot reste active (alerte « relever depuis l'UI »).
    "auto_recovery_enabled":      0,
    "auto_recovery_grace_s":      45,   ***REMOVED*** laisser Docker (unless-stopped) + systemd (PTP) relever
    "auto_recovery_max_attempts": 2,    ***REMOVED*** tentatives par conteneur (puis alerte error, on n'y revient plus)
    "auto_recovery_backoff_s":    20,   ***REMOVED*** pause entre deux tentatives sur le même conteneur

    ***REMOVED*** Alarmes antenne (audit A5/A6/B3) — cf. Réglages → Système → Alarmes.
    "signal_alerts_enabled":           1,    ***REMOVED*** noir/gel/silence remontés par le moteur 2110_io (metrics.py)
    "ptp_alerts_enabled":              1,    ***REMOVED*** pont des événements PTP warning/error vers le fil d'alertes
    "ptp_unlock_err_s":                30,   ***REMOVED*** unlock prolongé (holdover de fait) → alerte error
    ***REMOVED*** Horloges du cluster (Réglages → Réseau → Horloges). Le seuil est un réglage et non une
    ***REMOVED*** constante : la durée d'un grain dépend de la cadence de l'exploitant (20 ms à 50 fps,
    ***REMOVED*** 40 ms à 25, 16,7 ms à 60), et c'est elle qui définit à partir de quand deux nœuds ne
    ***REMOVED*** désignent plus la même image.
    ***REMOVED*** 0 = DÉRIVÉ du métier du nœud (demi-grain pour un compute, µs pour un porteur de moteur 2110,
    ***REMOVED*** cf. app/clocks.py). Une valeur > 0 REMPLACE la dérivation — à ne poser que si l'on sait
    ***REMOVED*** quelque chose qu'elle ignore.
    "clock_local_offset_us":           0,
    ***REMOVED*** Source NTP COMMUNE à tous les nœuds (CSV/espaces). Vide = on ne touche à rien, chaque nœud
    ***REMOVED*** garde ce qu il a. Une source commune rend les nœuds comparables : deux serveurs différents,
    ***REMOVED*** ce sont deux idées du temps, et leur écart se retrouve entre les nœuds sans qu on sache d où
    ***REMOVED*** il vient. Sans effet sur un nœud 2110 (son heure vient du grandmaster).
    "ntp_servers":                     "",
    "clock_alerts_enabled":            1,    ***REMOVED*** signaler un nœud hors grille / un écart excessif
    "script_restart_alert_threshold":  3,    ***REMOVED*** redémarrages consécutifs avant alerte error
    "script_restart_quarantine_count": 10,   ***REMOVED*** échecs consécutifs → quarantaine crash_loop (plus d'auto-restart)
    "script_restart_backoff_max_s":    300,  ***REMOVED*** plafond du backoff exponentiel entre tentatives

    ***REMOVED*** Canal WEBHOOK du service d'alertes (services/alerting/channels/webhook.py) — cf. Réglages →
    ***REMOVED*** Système → Alarmes. TOUTE la chaîne d'alerte était en PULL (il fallait ouvrir une page) :
    ***REMOVED*** c'est la cause directe d'une panne restée 8 jours sans réaction. Désactivé par défaut, aucune
    ***REMOVED*** valeur de site en dur. Ces clés gardent leur nom historique (elles sont déjà configurées en
    ***REMOVED*** production) ; les réglages de la DÉCISION et du canal e-mail sont `alerting_*`, déclarés par
    ***REMOVED*** le manifeste du service.
    "alert_webhook_enabled":   0,
    "alert_webhook_url":       "",         ***REMOVED*** POST JSON ; vide = jamais d'envoi
    "alert_webhook_min_level": "error",    ***REMOVED*** info | warning | error (niveau MINIMUM poussé)
    "alert_webhook_timeout_s": 5,          ***REMOVED*** timeout COURT : l'envoi est hors chemin critique
    ***REMOVED*** Anti-tempête : les alertes éligibles d'une même fenêtre partent dans UN SEUL POST (le journal
    ***REMOVED*** peut recevoir des dizaines d'alertes/minute en cas de flap). 0 = un POST par alerte.
    "alert_webhook_window_s":  60,

    ***REMOVED*** B2 — topologie réseau cluster. simple = management + conteneurs sur UN réseau (= actuel) ;
    ***REMOVED*** separated = réseau management + réseau conteneurs PRIVÉ dédié (VLAN trunké, /16 hors LAN). Le
    ***REMOVED*** plan 2110 est toujours physiquement séparé (NIC média dédiée, AF-XDP). En separated, l'orchestrateur est
    ***REMOVED*** multi-homé (3 réseaux). L'IPAM centralisé + le câblage deploy = B2-2 (B2-1 = modèle + réglages).
    "net_topology":     "simple",          ***REMOVED*** "simple" | "separated"
    ***REMOVED*** Plan CONTENEURS (macvlan) — réglages historiques (net_*/ip_*/gateway).
    "net_mode":         "dhcp",            ***REMOVED*** "dhcp" | "static"
    ***REMOVED*** ⚠ VIDES À DESSEIN. Ces trois-là portaient des adresses de SITE — la charte
    ***REMOVED*** dit qu'aucune valeur de site n'est codée en dur (cf. config_local.py), et
    ***REMOVED*** une plage d'IP par défaut est exactement le genre de valeur qu'on déploie
    ***REMOVED*** sans la relire. Le mode par défaut est `dhcp`, qui ne s'en sert pas ; en
    ***REMOVED*** mode `static`, l'exploitant DOIT les poser, et c'est mieux ainsi.
    "ip_start":         "",
    "ip_end":           "",
    "netmask_bits":     24,                ***REMOVED*** /24
    "gateway":          "",
    ***REMOVED*** Plan MANAGEMENT (separated seulement, informatif en B2-1 ; hôtes statiques, pas d'IPAM).
    "mgmt_subnet":      "",
    "mgmt_vlan":        "",
    ***REMOVED*** B2-3 — pool multicast 2110 cluster-unique (alloué par l'orchestrateur dans le registre NMOS).
    "mcast_pool_base":    "239.100.0.0",   ***REMOVED*** 1ᵉʳ groupe du pool
    "mcast_pool_size":    4096,            ***REMOVED*** nb de groupes (base .. base+size-1)
    "mcast_port_default": 5000,            ***REMOVED*** port par défaut d'un flux

    ***REMOVED*** FUSEAU HORAIRE du système (nom IANA, ex. "Europe/Paris"). Vide = suivre le fuseau de l'OS du
    ***REMOVED*** contrôleur (comportement historique). Posé dans TZ au démarrage → gouverne d'un seul geste les
    ***REMOVED*** journaux, les horodatages d'alertes, les dates affichées dans l'UI, et le `tz` injecté aux
    ***REMOVED*** conteneurs qui affichent l'heure civile (cf. ptp.civil_clock_params → multiview/avsync).
    ***REMOVED*** ⚠ Ne RÉINTERPRÈTE PAS les horodatages déjà stockés en base : seules les écritures futures
    ***REMOVED*** suivent le nouveau fuseau.
    "timezone":              "",

    ***REMOVED*** Mise à jour entre instances (pull/push) — mode serveur + token partagé
    "update_server_enabled": False,
    "update_token":          "",

    ***REMOVED*** PTP (IEEE 1588 / SMPTE 2059-2) — synchro horloge pour 2110
    "ptp_enabled":   False,
    "ptp_ifname":    "",             ***REMOVED*** NIC pour PTP (typiquement la PF SR-IOV)
    "ptp_domain":    127,            ***REMOVED*** SMPTE 2059-2 par défaut
    ***REMOVED*** PTP multi-NIC : domaine PTP qui discipline l'horloge système (CLOCK_REALTIME). Un seul
    ***REMOVED*** phc2sys « système » même avec plusieurs domaines. NULL/absent → plus petit domaine actif.
    "ptp_primary_domain": None,
    "ptp_hw_ts":     True,           ***REMOVED*** hardware timestamping (faux = software, jitter ms)
    ***REMOVED*** Client uniquement (BMCA) : le nœud ne se proclame JAMAIS grandmaster, même en perte
    ***REMOVED*** d'Announce (reste en LISTENING). Un nœud média consomme un GM maison externe → défaut True.
    ***REMOVED*** Évite qu'un faux-GM clignotant (Announce perdus) pollue le domaine PTP des autres nœuds.
    "ptp_client_only": True,
    ***REMOVED*** Gating broadcast au câblage : refuse une source incompatible (résolution/chroma/cadence) avec
    ***REMOVED*** un consommateur non-adaptateur, avec une raison. Désactivable (passe en avertissement seul).
    "wire_format_gating": True,

    ***REMOVED*** Étage 1 docs/reference/TX_LAYOUTS.md : arbre TX statique au boot du moteur 2110_io. Tout slot TX avec une
    ***REMOVED*** destination déclarée (mcast+port) est poussé `provisioned=True` au contrôleur (session RL
    ***REMOVED*** silencieuse dès la déclaration, pas d'attente du câblage) — le câblage devient un swap de
    ***REMOVED*** source (zéro rte_tm_hierarchy_commit). Garde-fou site : False = repli sur le comportement
    ***REMOVED*** historique (session créée seulement au câblage, un TX vivant peut alors perdre des mbufs).
    "tx_layout_provisioning_enabled": True,
    ***REMOVED*** Étage 3 docs/reference/TX_LAYOUTS.md : gate de FORMAT sur les slots TX du moteur. Câbler une source dont le
    ***REMOVED*** format diffère du format déclaré du slot recrée la session (commit TM = gel de TOUTES les
    ***REMOVED*** sorties de la carte, +9 commits mesurés) et fait ÉMETTRE au TX autre chose que ce qu'il ANNONCE.
    ***REMOVED*** L'écart doit être résolu (UDC, ou alignement du slot) : il n'y a pas de « forcer ».
    ***REMOVED*** False = repli sur le comportement historique (câble accepté, session recréée sans avertir).
    "tx_format_gating": True,
    ***REMOVED*** Changement de format d'une source EN EXPLOITATION (une caméra bascule) : le watcher ALERTE
    ***REMOVED*** toujours, puis insère automatiquement un UDC (swap de source = zéro commit, la sortie continue
    ***REMOVED*** d'émettre pendant le démarrage de l'UDC). False = l'alerte reste, l'insertion est manuelle.
    "tx_format_autoudc": True,
    ***REMOVED*** Role auto (BMCA) — pas de setting explicite : ptp4l décide

    ***REMOVED*** Formats vidéo prédéfinis (palette de déploiement). Une ligne :
    ***REMOVED***   Nom;Largeur;Hauteur;FPS;Scan(i/p);Chroma(420|422|444);BitDepth(8|10|12);Colorimétrie
    ***REMOVED*** Colorimétrie : 709 | 2020 | 2020pq | 2020hlg | 601. Champs au-delà de Scan optionnels
    ***REMOVED*** (défauts broadcast ST 2110-20 : 422 / 10 bits / 709). Anciennes lignes 5 champs OK.
    ***REMOVED*** ⚠ Colonne FPS pour l'entrelacé : elle compte les CHAMPS, jamais les trames (1080i50 → 50).
    ***REMOVED*** Deux lignes livrées étaient fausses de ce point de vue et ont été corrigées le 2026-08-15 :
    ***REMOVED*** « SD-SDI PAL » portait 25 (soit 12,5 trames/s) au lieu de 50, et « HD 1080i59.94 » portait
    ***REMOVED*** 29,97 (soit 14,985 trames/s) au lieu de 59,94. Migration de correction dans `init_db`.
    "video_formats": ("Test 640×360p25;640;360;25;p;422;10;709\n"
                      "SD-SDI PAL;720;576;50;i;422;10;601\n"
                      "HD 1920×1080i50;1920;1080;50;i;422;10;709\n"
                      "3G 1920×1080p50;1920;1080;50;p;422;10;709\n"
                      "UHD 3840×2160p50;3840;2160;50;p;422;10;2020\n"
                      "HD 1920×1080i59.94;1920;1080;59.94;i;422;10;709\n"
                      "3G 1920×1080p59.94;1920;1080;59.94;p;422;10;709\n"
                      "HD 1920×1080p29.97;1920;1080;29.97;p;422;10;709"),
    "video_format_default": "",      ***REMOVED*** label du format pré-sélectionné dans la palette de déploiement

    ***REMOVED*** Pipeline MXL — tailles des ring buffers shared memory
    "shm_video_ring": 8,    ***REMOVED*** grains vidéo en mémoire (ring buffer YUV) — borné 2..8 (MTL st20 : ring ≤ 8)
    "shm_audio_ring": 100,  ***REMOVED*** grains audio en mémoire (ring buffer L24/48k)
    ***REMOVED*** Ptime audio ST 2110-30 par DÉFAUT (ms) selon l'installation : "1" (1 ms) | "0.125" (125 µs).
    ***REMOVED*** Repli uniquement : un SDP avec a=ptime PRIME (auto par entrée, géré par mtl_rx/controller).
    "mtl_audio_ptime": "1",
    ***REMOVED*** Budget CPU du moteur 2110_io : quota Mb/s par scheduler libmtl (1 lcore) — au-delà, les
    ***REMOVED*** nouvelles sessions vont sur un autre lcore. C'est LA manette de calibrage CPU : la capacité
    ***REMOVED*** réelle d'un cœur dépend du CPU/de la bande passante mémoire (memcpy AF_XDP) → défaut PRUDENT
    ***REMOVED*** 2500 (≈ 2×1080p50), à ajuster par site (Réglages → MXL). L'ancien défaut 5000 tassait 4-5
    ***REMOVED*** sessions 1080p50 par scheduler (boucle ≈ inter-paquet) → wedge OFO en cascade sous jitter
    ***REMOVED*** (incident lab Horace 2026-07-06). Le dimensionnement des lcores (_auto_lcores) et le
    ***REMOVED*** garde-fou d'admission (_mtl_lcore_sessions) dérivent tous deux de ce quota.
    "mtl_sch_quota_mbs": 2500,
    ***REMOVED*** Budget de temps (s) pour pousser les slots TX au contrôleur après une (re)création du moteur.
    ***REMOVED*** Une ÉCHÉANCE, pas un compte d'essais : l'agent :8081 répond bien avant que le contrôleur
    ***REMOVED*** n'accepte /tx (mtl_init met 30-60 s sur E810). L'ancien budget implicite de 5 s par slot
    ***REMOVED*** laissait un moteur recréé SANS AUCUNE SORTIE, en silence (incident 2026-07-28).
    "mtl_tx_push_timeout_s": 120,
    ***REMOVED*** Attente MAX de la fin de `mtl_init` avant de configurer un moteur qui vient d'être (re)créé.
    ***REMOVED*** Distinct de la readiness :8081, qui ne prouve que la vivacité du contrôleur (cf.
    ***REMOVED*** docker_driver.moteur_initialise). Sur E810 100G, l'entraînement du lien seul prend 60-90 s.
    "mtl_init_wait_s": 90,
    ***REMOVED*** Pinning cœurs du moteur 2110_io : réserve les lcores dans node_core_alloc (jamais donnés aux
    ***REMOVED*** containers compute pinnés) et pose --cpuset-cpus sur le conteneur moteur (lcores + 2 cœurs
    ***REMOVED*** pour le contrôleur Python). Garde-fou nproc côté nœud (pas de cpuset hors machine).
    "mtl_pin_cores": True,
    ***REMOVED*** Niveau de log du moteur 2110_io (libmtl) — injecté en env MTL_LOG_LEVEL au déploiement, lu par
    ***REMOVED*** mtl_rx.c. Valeurs : debug|info|notice|warning|err. DÉFAUT "warning" (silencieux) : à INFO,
    ***REMOVED*** libmtl émet périodiquement un dump de stats volumineux (bloc « END STATE » + SCH/xdp_queue)
    ***REMOVED*** qui noie les logs du moteur. Les niveaux ≥ INFO ne servent qu'au diagnostic ponctuel. Le
    ***REMOVED*** niveau EFFECTIF de chaque moteur est tracé dans params.mtl_log_level (cf. docker_driver) pour
    ***REMOVED*** repérer les moteurs restés verbeux. Valeur inconnue → mtl_rx retombe sur warning.
    "mtl_log_level": "warning",  ***REMOVED*** debug|info|notice|warning|err
    ***REMOVED*** NB : `nmos_label_prefix` (préfixe des libellés NMOS, override par nœud) est déclaré dans
    ***REMOVED*** services/nmos/manifest.json, avec ses clés sœurs — une seule source de vérité.
    ***REMOVED*** Profondeur de bits du pipeline shm — arbitrage orchestrateur (cf. deploy._apply_pipeline_bit_depth) :
    ***REMOVED***   force8 → 8 bits imposé partout (défaut, perfs/mémoire actuelles, zéro régression)
    ***REMOVED***   follow → profondeur du format vidéo (8/10/12 ; 10/12 bits = ×2 mémoire shm)
    "mxl_pipeline_bit_depth": "force8",  ***REMOVED*** "force8" | "follow"

    ***REMOVED*** Fenêtre d'historique du bus MXL (millisecondes), PAR NŒUD — pas par container. Le SDK MXL
    ***REMOVED*** (option `urn:x-mxl:option:history_duration/v1.0`, `/dev/shm/mxl/options.json`) exprime la
    ***REMOVED*** profondeur des ring buffers comme une DURÉE, pas un nombre de trames : le SDK dérive
    ***REMOVED*** grainCount = durée × cadence (défaut 200 ms → 10 cases à 50 fps, 5 à 25 fps ; l'audio reçoit
    ***REMOVED*** le double de la durée). Réglage PAR-NŒUD délibérément : le SDK ignore l'option posée au
    ***REMOVED*** niveau instance. Un changement ne s'applique QU'AUX FLUX CRÉÉS ENSUITE (ring déjà alloué =
    ***REMOVED*** inchangé). C'est une fenêtre de LATENCE tolérée pour un lecteur en retard, pas un levier
    ***REMOVED*** d'économie de RAM (mesuré 2026-08-09 : 333 Mo pour 47 Go de tmpfs, soit 0,7 %). Bornes
    ***REMOVED*** [MXL_HISTORY_MS_MIN, MXL_HISTORY_MS_MAX] validées côté serveur dans `set()` ci-dessous.
    "mxl_history_ms": 200,
    ***REMOVED*** Lot de synchronisation RDMA (`maxSyncBatchSizeHint`), en TRANCHES. Vide = défaut du SDK
    ***REMOVED*** (= totalSlices), c'est-à-dire « attendre la trame entière avant de transférer ». Mesuré le
    ***REMOVED*** 2026-08-09 : 1ʳᵉ bande lisible sur la réplique à 22,63 ms au défaut, 0,54 ms à 2 tranches,
    ***REMOVED*** pour un débit et un nombre de paquets identiques. Injecté par `docker_compute` en
    ***REMOVED*** MXL_SYNC_BATCH, lu par `bobimxl._flow_options()`. N'agit que sur les flux CRÉÉS ENSUITE.
    "mxl_sync_batch": "",

    ***REMOVED*** Hébergement des fichiers d'installation (/install.sh, /install/*) pour le
    ***REMOVED*** déploiement en one-liner sur un nœud Proxmox. Activable depuis Réglages.
    "install_hosting_enabled": True,
    ***REMOVED*** Hôte de build des images runtime PARTAGÉES (compute/media/webrtc) : "" (auto) | "local"
    ***REMOVED*** (docker de l'orchestrateur) | "<node_id>". Cf. routes._build_target. L'image est distribuée
    ***REMOVED*** à tous les nœuds concernés après le build (auto-push).
    "image_build_node": "",

    ***REMOVED*** Premier démarrage : faux tant que l'assistant de configuration (/setup/wizard)
    ***REMOVED*** n'a pas été terminé ou explicitement sauté. Tant qu'il est faux, une connexion
    ***REMOVED*** réussie redirige vers l'assistant.
    "setup_completed":  False,

    ***REMOVED*** Exigence sur les mots de passe : `souple` | `standard` | `stricte` (cf. auth.PWD_PROFILS).
    ***REMOVED*** Ne s'applique qu'aux mots de passe SAISIS ENSUITE — les existants sont stockés en
    ***REMOVED*** empreinte, donc illisibles : durcir le profil ne peut pas les invalider rétroactivement.
    "pwd_profil":       "standard",

    ***REMOVED*** Apparence
    "theme":            "classic",   ***REMOVED*** classic | studio | light
    ***REMOVED*** Langue d'interface par défaut (i18n) — repli quand l'utilisateur n'a pas de
    ***REMOVED*** préférence propre (users.lang). Codes : voir app/i18n.LANGUAGES.
    "ui_lang_default":  "fr",
    ***REMOVED*** Langues personnalisées créées via l'éditeur de traductions (i18n) :
    ***REMOVED*** liste [{code, label}]. Pas de fichier catalogue → uniquement surcouche DB.
    "ui_custom_languages": [],

    ***REMOVED*** Personnalisation client (identité du déploiement, EN PLUS de la marque produit) :
    ***REMOVED*** nom du système, entreprise, logo (chemin /static/uploads/…), emplacement.
    "brand_system_name": "",
    "brand_org_name":    "",
    "brand_logo_url":    "",
    "brand_location":    "",
    ***REMOVED*** ⚠ N'ACTIVER QUE SI L'ORCHESTRATEUR N'EST JOIGNABLE QU'À TRAVERS UN REVERSE-PROXY DE
    ***REMOVED*** CONFIANCE. `X-Forwarded-For` est envoyé par le CLIENT : si la machine est joignable en
    ***REMOVED*** direct, n'importe qui peut s'annoncer à l'adresse de son choix, et le filtre d'adresses
    ***REMOVED*** des liens publics n'aurait plus que l'apparence d'un filtre. Off par défaut : on prend
    ***REMOVED*** alors le vrai pair TCP, inforgeable.
    "public_trust_proxy": False,

    ***REMOVED*** Sauvegarde quotidienne automatisée de la DB (vers backups/)
    "backup_enabled":   False,
    "backup_time":      "02:00",     ***REMOVED*** heure locale serveur HH:MM
    "backup_retention": 14,          ***REMOVED*** nombre de sauvegardes conservées
    ***REMOVED*** État runtime (écrit par le scheduler, lu par l'UI)
    "backup_last_date":   "",        ***REMOVED*** YYYY-MM-DD du dernier backup réussi (anti double-run)
    "backup_last_status": "",        ***REMOVED*** message du dernier run
    "backup_last_file":   "",        ***REMOVED*** nom du dernier fichier produit
}


_CORE_DEFAULTS = None  ***REMOVED*** chargé paresseusement à la première requête


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

***REMOVED*** Liste des thèmes connus + leur description (pour le sélecteur UI)
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
        ZoneInfo(tz)                      ***REMOVED*** valide contre la tzdata réellement présente
    except Exception as e:
        log.warning("réglage timezone : fuseau « %s » inconnu (%s) — fuseau de l'OS conservé", tz, e)
        return ""
    os.environ["TZ"] = tz
    try:
        _time.tzset()
    except Exception as e:                ***REMOVED*** plateformes sans tzset (non-POSIX)
        log.warning("réglage timezone : tzset indisponible (%s)", e)
        return ""
    return tz


def set(key, value):
    ***REMOVED*** `mxl_history_ms` : une durée nulle ou absurde casserait tous les FLUX CRÉÉS ENSUITE sur le
    ***REMOVED*** nœud (ring buffer trop court → un lecteur en retard relit une case recyclée). Clamp plutôt
    ***REMOVED*** que refus silencieux : une saisie hors bornes est ramenée à la borne la plus proche, journalisée.
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
    ***REMOVED*** `shm_video_ring` : framebuffers DPDK du moteur 2110 (RX ET TX vidéo). Le plancher n'est PAS
    ***REMOVED*** un optimum choisi, c'est une valeur au-dessus d'un ÉCHEC MESURÉ : le 2026-08-09, un TX 1080p50
    ***REMOVED*** avec 4 framebuffers (dont 1 immobilisé par la trame de tenue, donc 3 au worker) bloquait le
    ***REMOVED*** worker 59 % du temps et répétait UNE TRAME SUR QUATRE à l'antenne ; 8 donne 0 % de blocage et
    ***REMOVED*** 49 trames fraîches/s. Rien entre 5 et 7 n'a été mesuré — 6 est le premier cran laissant une
    ***REMOVED*** marge réelle au-dessus du défaut connu. Plafond 8 = `ST20_FB_MAX_COUNT`, seule contrainte
    ***REMOVED*** énoncée par le SDK (`framebuff_cnt ∈ [2, 8]`, aucune recommandation de valeur).
    ***REMOVED*** Cesse de valoir si quelqu'un mesure 5 ou 6 avec `slot_wait_ms`/`fb_slots` (moteur ≥ 0.85.0).
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
    ***REMOVED*** Le fuseau doit prendre effet SANS redémarrage : sinon les journaux et l'UI continueraient
    ***REMOVED*** d'afficher l'ancien fuseau jusqu'au prochain restart, et l'utilisateur conclurait que le
    ***REMOVED*** réglage ne marche pas.
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

***REMOVED*** Clés à NE JAMAIS exposer sur un chemin de sortie HTTP (fuite de secrets).
***REMOVED*** Blacklist explicite (flask_secret_key = clé de signature de session → forge de cookie
***REMOVED*** admin ; update_token = secret partagé HA/updater ; pxe_armed_token) + motifs suffixe
***REMOVED*** conservateurs (_password/_secret/_token). On NE filtre PAS *_key en générique pour ne
***REMOVED*** pas casser des clés fonctionnelles (noms d'interface, etc.).
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
            ***REMOVED*** mxl_history_ms (et tout futur réglage à bornes validées) passe par `set()` : c'est
            ***REMOVED*** là que vit le clamp serveur — db_set_setting() en direct le contournerait.
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
    ***REMOVED*** Le service d'alertes met ses réglages en cache (il ne peut pas lire la DB sur le chemin
    ***REMOVED*** critique de db_add_alert) : on l'invalide ici, sinon activer un canal depuis l'UI ne prendrait
    ***REMOVED*** effet qu'au bout du TTL et les alertes de cet intervalle seraient perdues sans un mot.
    if any(k.startswith("alert_webhook_") or k.startswith("alerting_") for k in items):
        try:
            from services import alerting
            alerting.invalider_cache()
        except Exception:
            log.exception("settings: invalidation du cache du service d'alertes impossible")
    return accepted, ignored

***REMOVED*** D Phase 2a : helpers du client API Proxmox (proxmox_token_header / proxmox_url) RETIRÉS
***REMOVED*** (proxmox.py supprimé, full-Docker). `proxmox_host` reste un réglage : c'est l'hôte SSH utilisé
***REMOVED*** par la couche host-ops (VF/PTP/binds) — sera renommé « hôte » au retargeting par-nœud (B).
