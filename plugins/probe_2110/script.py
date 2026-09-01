# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
#
# probe_2110 — PLACEHOLDER de script.
#
# La sonde ST 2110 est un type MATÉRIEL MTL (plugin.json: needs_dpdk=true) : son runtime réel
# est le CONTRÔLEUR baké dans l'image `bobi-mtl` (docker/controller.py + binaire mtl_rx), lancé
# par `app/docker_driver.deploy_docker` — PAS ce fichier. `deploy_docker` construit la ligne
# `docker run` (env RX-only + TIMING_PARSER=1 + PF vfio dédiée) et ne rend JAMAIS ce script.py
# (le chemin `render_script`/agent :8081/deploy est réservé aux types compute/LXC). Le parser de
# conformité 2110-21 vit déjà dans libmtl (mtl_rx.c, cf. docs/reference/PROBE_2110.md) : la sonde le WRAPPE.
#
# Ce fichier n'existe que pour satisfaire le registre de plugins (`plugins._scan` exige un
# `script_template` valide + dry-run `.format`). Il fournit un repli minimal — un `:8080` qui
# annonce le mode placeholder — au cas TRÈS improbable où il serait déployé hors du chemin MTL.
# Toute accolade littérale est DOUBLÉE (contrat str.format des plugins).

import json, time
from http.server import HTTPServer, BaseHTTPRequestHandler

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True




class _Report(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = {{
            "receivers": [],
            "probe": {{
                "mode": "placeholder",
                "hostname": HOSTNAME,
                "plugin_version": PLUGIN_VERSION,
                "note": ("probe_2110 s'exécute normalement via le contrôleur bobi-mtl "
                         "(docker_driver), pas via ce script.py placeholder."),
            }},
        }}
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


log("probe_2110 {{}} placeholder : le runtime réel est le contrôleur bobi-mtl (RX-only + "
    "TIMING_PARSER).".format(PLUGIN_VERSION), "info")
HTTPServer(("0.0.0.0", 8080), _Report).serve_forever()
