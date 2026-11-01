#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""La profondeur des ring buffers MXL est posée sur TOUT nœud qui porte le bus — 2110 ou non.

★ LE CONSTAT (Z8, 2026-10-29). `/dev/shm/mxl/options.json` (réglage `mxl_history_ms`, reposé au
boot par tmpfiles.d) n'était posé QUE par la prép MTL d'un nœud 2110. La Z8 (DeckLink) ne l'avait
jamais reçu : `node_health` levait « profondeur MXL absente » à chaque redémarrage de
l'orchestrateur, et rien ne la réparait. Le défaut SDK (200 ms) masquait le défaut ; toute autre
valeur réglée ne se serait appliquée sur aucun nœud non-2110.

Ce que cette garde tient, sur les VRAIES fonctions (app/mxl_history.py, mtl, node_health,
settings), les commandes hôte RÉELLES jouées dans un bac à sable par nœud :
1. ★ dérive constatée sur un nœud NON 2110 → ALERTÉE, jamais réparée en silence ; le geste manuel
   (bouton de rattrapage) la pose, puis l'alerte passe à « rétablie » ;
2. ★ jamais d'unité systemd ni de daemon-reload (révoque le GPU des conteneurs lancés) ;
3. ★ on MESURE avant d'agir : rien n'est réécrit sur un nœud conforme ;
4. ★ un changement du réglage se propage aux nœuds qui portent le bus (pas aux autres) ;
5. ★ les flux déjà ouverts sous une autre profondeur sont SIGNALÉS (rien n'est redéployé) ;
6. ★ l'INSTALLATION (le vrai `_enroll_finalize`) pose la profondeur sur un nœud vierge, et met le
   nœud en performance ; le déploiement ne la pose plus (décision du 2026-10-30) ;
7. ★ un échec n'est jamais muet (alerte) ;
8. la route manuelle (rattrapage des nœuds déjà installés) existe.

Hors ligne : base jetable (jamais la prod) ; /dev/shm/mxl, /etc/tmpfiles.d, /etc/systemd/system
réécrits vers des dossiers temporaires ; `systemctl` et `docker` factices qui journalisent.

MUTATIONS (MXL_HISTORY_MUTATION=<nom>) — chacune doit donner rc=1 :
    seulement_2110        le point d'entrée ignore un nœud non-2110
    toujours_reinstalle   on réécrit sans mesurer
    unite_systemd         la pose passe par un daemon-reload
    reglage_non_propage   un changement de `mxl_history_ms` n'est pas propagé
    echec_muet            un échec n'est plus alerté
    derive_reparee_en_silence  node_health répare au lieu de laisser lire l'alerte
    flux_anciens_muets    les flux vivants à l'ancienne profondeur ne sont plus signalés
    installation_sans_pose     l'enrôlement ne pose plus la profondeur
    installation_sans_performance  l'enrôlement ne met plus le nœud en performance
    deploiement_repose    `deploy_compute` repose la profondeur à chaque déploiement

    ./venv/bin/python tests/verif_mxl_history_tout_noeud.py
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

# Base jetable, posée AVANT le premier accès (connexion mémorisée par thread). On ne touche PAS
# PRODUCTION_DB_PATH (cf. verif_alerte_domaine_mxl.py).
_TMPDIR = tempfile.mkdtemp(prefix="verif_mxl_history_")
_TMP = os.path.join(_TMPDIR, "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import mtl, mxl_history, node_driver, node_health, settings          # noqa: E402
from app import core_pool                                                       # noqa: E402
from app.routes import enrollment                                               # noqa: E402

MUT = os.environ.get("MXL_HISTORY_MUTATION") or ""
_DC_PATH = os.path.join(RACINE, "app", "docker_compute.py")
MUTATIONS = {
    "seulement_2110": (mxl_history,
                       '    if not node:\n        return False, False, "node not found"\n',
                       '    if not node:\n        return False, False, "node not found"\n'
                       '    if "io2110" not in (node.get("capabilities") or ""):\n'
                       '        return False, True, "not a 2110 node"\n'),
    "toujours_reinstalle": (mtl, "    if conf:\n        return False, True, why, st",
                            "    if False:\n        return False, True, why, st"),
    "unite_systemd": (mtl, "echo '@@UNIT_RETIREE'; fi; \"\n        \"echo '@@OK'\"",
                      "echo '@@UNIT_RETIREE'; fi; \"\n        \"systemctl daemon-reload; echo '@@OK'\""),
    "reglage_non_propage": (settings, '            mxl_history.propagate_async(reason="setting")',
                            "            pass"),
    "echec_muet": (mxl_history, '    log.warning("MXL depth on node %s (%d ms, %s) failed: %s"',
                   '    return acted, ok, msg\n    log.warning("MXL depth on node %s (%d ms, %s) failed: %s"'),
    "derive_reparee_en_silence": (node_health,
                                  "        # Rattrapage : le bouton du Monitoring → Nœuds (`POST /api/mtl/mxl_history`).\n",
                                  "        mxl_history.sync_node(node_id, \"drift\", want=want_ms)\n"),
    "flux_anciens_muets": (mxl_history, "                    _alert_stale_flows(node_id, name, w, sf, vmid)",
                           "                    pass"),
    "installation_sans_pose": (enrollment, '        mxl_history.sync_node(node_id, "install", create=True)\n', ""),
    "installation_sans_performance": (enrollment, "        _cp.sync_dedicated_cpufreq(node_id)\n", ""),
    "deploiement_repose": (None, "    db_update_docker_ip(vmid, ip)\n",
                           "    db_update_docker_ip(vmid, ip)\n    mxl_history.sync_node(node['id'], 'deploy', create=True)\n"),
}
with open(_DC_PATH, encoding="utf-8") as _f:
    _DC_SRC = _f.read()
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _mod, _old, _new = MUTATIONS[MUT]
    _src = _DC_SRC if _mod is None else open(_mod.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) — la garde est périmée"
              % (MUT, _src.count(_old)))
        sys.exit(1)
    if _mod is None:
        _DC_SRC = _src.replace(_old, _new)
    elif _mod is enrollment:
        # Module de routes : on ne ré-exécute que la fonction (re-décorer les routes d'un
        # blueprint déjà construit n'a pas de sens).
        _f = next(n for n in ast.parse(_src).body if isinstance(n, ast.FunctionDef)
                  and n.name == "_enroll_finalize")
        _seg = ast.get_source_segment(_src, _f)
        exec(compile(_seg.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    else:
        exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    print("MUTATION active :", MUT)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Bacs à sable : un « hôte » par nœud ─────────────────────────────────────────────────────────
_hotes = {}          # node_id → dossier
_joignable = {}
_cmds = []
_lock = threading.Lock()


def _remap(h):
    return [("/dev/shm/mxl", os.path.join(h, "shm_mxl")),
            ("/etc/tmpfiles.d", os.path.join(h, "tmpfiles")),
            ("/etc/systemd/system", os.path.join(h, "systemd")),
            ("/tmp/.mxl-tmpf.new", os.path.join(h, "tmpf.new"))]


def _hote_neuf(nid, domaine=False, flux=0):
    h = os.path.join(_TMPDIR, "hote%d" % nid)
    shutil.rmtree(h, ignore_errors=True)
    for d in ("tmpfiles", "systemd", "bin"):
        os.makedirs(os.path.join(h, d))
    if domaine:
        os.makedirs(os.path.join(h, "shm_mxl"))
        for i in range(flux):
            os.makedirs(os.path.join(h, "shm_mxl", "%08d-0000-0000-0000-000000000000.mxl-flow" % i))
    for outil in ("systemctl", "docker"):
        p = os.path.join(h, "bin", outil)
        with open(p, "w") as f:
            f.write('#!/bin/sh\necho "%s $*" >> "%s/outils.log"\nexit 0\n' % (outil, h))
        os.chmod(p, 0o755)
    _hotes[nid] = h
    _joignable[nid] = True
    return h


def _faux_host_exec(node, cmd, input_data=None, timeout=300, **kw):
    nid = (node or {}).get("id")
    with _lock:
        _cmds.append((nid, cmd))
    if not _joignable.get(nid, False):
        return (255, "", "agent unreachable")
    h = _hotes[nid]
    for a, b in _remap(h):
        cmd = cmd.replace(a, b)
    env = dict(os.environ, PATH=os.path.join(h, "bin") + ":" + os.environ.get("PATH", ""))
    p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60, env=env, cwd=h)
    return p.returncode, p.stdout, p.stderr


node_driver.host_exec = _faux_host_exec


def _options(nid):
    p = os.path.join(_hotes[nid], "shm_mxl", "options.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p)).get(mtl.MXL_HISTORY_OPTION_KEY)


def _tmpf(nid):
    p = os.path.join(_hotes[nid], "tmpfiles", "bobi-mxl.conf")
    return open(p).read() if os.path.exists(p) else ""


def _ecritures(nid=None):
    return [c for n, c in _cmds if (nid is None or n == nid) and "MXLEOF" in c]


def _alertes(nid, motif):
    with database.get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM alerts WHERE node_id=? AND msg_key LIKE ? ORDER BY id", (nid, motif))]


def _attendre_propagation():
    for t in list(threading.enumerate()):
        if t.name == "mxl-history-propagate" and t is not threading.current_thread():
            t.join(30)


def _sonde_derive(nid, name, node):
    node_health._check_mxl_history_drift(nid, name, node)


Z8 = database.db_add_node("z8", "127.0.0.1")              # DeckLink, aucune capacité io2110
database.db_update_node(Z8, capabilities=json.dumps(["decklink"]), agent_url="http://127.0.0.1:9100")
VIERGE = database.db_add_node("vierge", "127.0.0.2")       # enrôlé, n'a jamais porté de MXL
database.db_update_node(VIERGE, capabilities=json.dumps([]), agent_url="http://127.0.0.2:9100")
_hote_neuf(Z8, domaine=True, flux=2)
_hote_neuf(VIERGE, domaine=False)
NODE_Z8 = database.db_get_node(Z8)

# ════════════════════════════════════════════════════════════════════════════════════════════
print("\n1. ★ Dérive sur un nœud NON 2110 : alertée, jamais réparée en silence")
_sonde_derive(Z8, "z8", NODE_Z8)                     # 1er passage : arme seulement
node_health._mxl_history_drift_at[Z8] = 0            # échoit tout de suite
_cmds.clear()
_sonde_derive(Z8, "z8", NODE_Z8)
verifie(len(_alertes(Z8, "%mxl_profondeur_derive_inconnue_unitko_seul%")) == 1,
        "1a. l'alerte de dérive existante est levée (fichier absent, non persistant)",
        _alertes(Z8, "%mxl%"))
verifie(_options(Z8) is None and not _ecritures(Z8),
        "1b. ★★ la dérive n'est PAS réparée en silence : elle se lit (Z8 : 8 alertes non lues "
        "cachaient une installation incomplète)", (_options(Z8), _ecritures(Z8)))
acted, ok, msg = mxl_history.sync_node(Z8, "manual", create=True)   # le bouton de rattrapage
verifie(acted and ok and _options(Z8) == 200_000_000 and "200000000" in _tmpf(Z8),
        "1c. ★ bouton de rattrapage : options.json ET tmpfiles.d posés à 200 ms",
        (acted, ok, msg, _options(Z8), _tmpf(Z8)))
verifie(not _alertes(Z8, "%mxl_profondeur_flux_anciens%"),
        "1d. flux vivants créés sans fichier = défaut SDK 200 ms = voulu → aucun avertissement")
node_health._mxl_history_drift_at[Z8] = 0
_sonde_derive(Z8, "z8", NODE_Z8)
verifie(len(_alertes(Z8, "%mxl_profondeur_retablie%")) == 1,
        "1e. ★★ à la sonde suivante, l'alerte passe à « rétablie »",
        _alertes(Z8, "%mxl%"))

print("\n2. ★ Jamais d'unité systemd ni de daemon-reload")
_outils = open(os.path.join(_hotes[Z8], "outils.log")).read() \
    if os.path.exists(os.path.join(_hotes[Z8], "outils.log")) else ""
verifie("daemon-reload" not in _outils and not any("daemon-reload" in c for _, c in _cmds),
        "2a. ★★ aucun daemon-reload (il révoque le GPU des conteneurs déjà lancés)", _outils)
verifie(os.listdir(os.path.join(_hotes[Z8], "systemd")) == [],
        "2b. aucune unité déposée dans /etc/systemd/system",
        os.listdir(os.path.join(_hotes[Z8], "systemd")))

print("\n3. ★ Mesure avant d'agir")
_cmds.clear()
acted, ok, msg = mxl_history.sync_node(Z8, "manual")
verifie(ok and not acted and not _ecritures(),
        "3a. ★★ nœud conforme → RIEN n'est réécrit (une sonde, zéro écriture)", (acted, ok, msg))
with open(os.path.join(_hotes[Z8], "tmpfiles", "bobi-mxl.conf"), "w") as f:
    f.write(_tmpf(Z8).replace("200000000", "300000000"))
acted, ok, msg = mxl_history.sync_node(Z8, "manual")
verifie(acted and ok and "200000000" in _tmpf(Z8),
        "3b. valeur PERSISTÉE fausse (vivante juste) → vue et corrigée (le boot reposerait l'ancienne)",
        (acted, ok, msg, _tmpf(Z8)))

print("\n4. ★ Un changement du réglage se propage")
_cmds.clear()
settings.update_bulk({"mxl_history_ms": 400})
_attendre_propagation()
verifie(_options(Z8) == 400_000_000 and "400000000" in _tmpf(Z8),
        "4a. ★★ Z8 : 400 ms posés (vivant ET persistant) dès l'enregistrement du réglage",
        (_options(Z8), _tmpf(Z8)))
verifie(not os.path.exists(os.path.join(_hotes[VIERGE], "shm_mxl")) and not _ecritures(VIERGE),
        "4b. nœud sans domaine : mesuré, pas de domaine fabriqué hors installation et geste manuel")

print("\n5. ★ Flux déjà ouverts : signalés, jamais redéployés")
_fa = _alertes(Z8, "%mxl_profondeur_flux_anciens%")
_fp = json.loads(_fa[0].get("msg_params") or "{}") if _fa else {}
verifie(len(_fa) == 1 and _fp.get("flows") == 2 and _fp.get("old_ms") == 200 and _fp.get("want_ms") == 400,
        "5a. ★★ alerte : 2 flux ouverts gardent 200 ms jusqu'au redéploiement de leurs producteurs", _fa)
_st = mxl_history.status(Z8) or {}
verifie((_st.get("stale_flows") or {}).get("flows") == 2 and _st.get("cur_ms") == 400,
        "5b. état publié pour la vue nœud (flux anciens, profondeur posée)", _st)
verifie(not any("docker" in c and ("restart" in c or " rm " in c or "run " in c) for _, c in _cmds),
        "5c. aucun conteneur relancé")

print("\n6. ★ L'installation pose la profondeur et le mode performance (vrai _enroll_finalize)")
_perf = []
node_driver.ping = lambda url: True
node_driver.register = lambda ip, port, token, name=None: (True, {})
enrollment._provision_shared_images = lambda node: ([], [])
core_pool.read_cpu_core_map = lambda node: {}
core_pool.sync_dedicated_cpufreq = lambda node_id, vmid=None: _perf.append(node_id) or (True, True, "")
from app import ca as _ca                                                       # noqa: E402
_ca.ca_available = lambda: False
_cmds.clear()
enrollment._enroll_finalize(VIERGE, "127.0.0.2")
verifie(_options(VIERGE) == 400_000_000 and "400000000" in _tmpf(VIERGE),
        "6a. ★★ nœud vierge : domaine créé, profondeur posée (vivante ET persistante) dès l'installation",
        (_options(VIERGE), _tmpf(VIERGE)))
verifie(_perf == [VIERGE], "6b. ★★ et mis en mode performance (unité + gouverneur, cf. "
        "verif_cpufreq_coeurs_dedies)", _perf)
_arbre = ast.parse(_DC_SRC)
_fn = next(n for n in ast.walk(_arbre) if isinstance(n, ast.FunctionDef) and n.name == "deploy_compute")
_appels = {ast.unparse(n.func) for n in ast.walk(_fn) if isinstance(n, ast.Call)}
verifie(not any("mxl_history" in a for a in _appels),
        "6c. ★ `deploy_compute` ne repose plus la profondeur (posée à l'installation)", _appels)
verifie(not hasattr(mxl_history, "ensure_for_deploy"), "6d. le point d'entrée de déploiement a disparu")

print("\n7. ★ Un échec n'est jamais muet")
_joignable[Z8] = False
settings.update_bulk({"mxl_history_ms": 500})
_attendre_propagation()
_joignable[Z8] = True
_ec = _alertes(Z8, "%mxl_profondeur_echec%")
verifie(len(_ec) >= 1 and _ec[-1].get("kind") == "prep",
        "7a. ★★ nœud injoignable → alerte (sinon les flux suivants prendraient l'ancienne profondeur)", _ec)

print("\n8. Route manuelle")
with open(os.path.join(RACINE, "app", "routes", "mtl_engine.py"), encoding="utf-8") as f:
    _src_r = f.read()
_i = _src_r.find('"/api/mtl/mxl_history"')
verifie(_i > 0 and "mxl_history.sync_node(nid, \"manual\", create=True)" in _src_r[_i:_i + 1500],
        "8a. POST /api/mtl/mxl_history pose (create=True) sur n'importe quel nœud")

print("\n" + ("─" * 70))
shutil.rmtree(_TMPDIR, ignore_errors=True)
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ profondeur MXL posée sur tout nœud qui porte le bus ; mesure avant d'agir ; tmpfiles.d sans "
      "reload ; réglage propagé ; flux anciens signalés ; échec alerté")
