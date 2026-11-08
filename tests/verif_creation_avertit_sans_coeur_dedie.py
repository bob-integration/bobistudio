#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Créer un conteneur quand AUCUN cœur ne peut lui être dédié : on avertit, l'humain choisit.

★ LE CONSTAT (banc du 2026-11-07, dl360-1). Une console audio créée alors que plus un seul cœur
n'était dédiable a été placée sur le pool PARTAGÉ (43-45,47) **sans aucun signal**. Le repli
partagé est délibéré et reste le bon défaut — un conteneur sans cpuset flotterait sur les lcores
busy-poll du moteur 2110 — mais il était MUET : `effective_cpuset` n'alerte que si le pool
partagé est lui-même entièrement dédié, ce qui n'était pas le cas ici. Résultat : un conteneur
temps réel qui croit tenir des cœurs dédiés, n'en a aucun, et dont aucun compteur de charge ne
dit rien. Mesuré le même jour sur la machine réelle : `mixer` demande 3 cœurs dédiés, la capacité
stricte de dl360-1 valait 0, et sept conteneurs tenaient tout.

Arbitrage de Cyril (2026-11-07) : avertir À LA CRÉATION, et laisser le choix — créer néanmoins,
ou libérer des ressources d'abord.

Ce que cette garde tient, sur les VRAIES fonctions et la VRAIE route :
1. ★ `preflight_dedication` MESURE sous la règle stricte d'`allocate_cores`, ne réserve rien, et
   NOMME qui tient les cœurs ;
2. ★ un type qui ne demande aucun cœur dédié n'est JAMAIS averti (une porte qui crie à tort
   s'apprend à cliquer sans lire) ;
3. ★ la porte est SUR LE SERVEUR : 409 + `error_code` + `confirmation_requise`, et AUCUNE ligne
   en base — une confirmation qui ne vit que dans l'interface n'en est pas une (bascule de mode
   réseau du 28/10 : douze conteneurs effacés) ;
4. ★ `confirme_partage` passe outre et crée — c'est un avertissement, pas un refus ;
5. un nœud qui a des cœurs libres — ou pas de pool du tout — ne déclenche RIEN ;
6. ★ côté interface : la fournée renvoie la confirmation, le drapeau est bien envoyé, et fermer
   la fenêtre ne vaut JAMAIS « créer » ;
7. les clés i18n existent dans les DEUX catalogues.

Hors ligne : base jetable, aucun nœud, aucun docker (la création réelle est remplacée).

MUTATIONS (CREATION_DEDIE_MUTATION=<nom>) — chacune doit donner rc=1 :
    porte_absente            la route ne mesure plus rien avant de créer
    porte_sans_mesure        le préflight se déclare toujours satisfait
    capacite_logique         il compte les cœurs du pool au lieu de la capacité STRICTE
    avertit_les_partages     il avertit aussi un type qui n'a jamais demandé de dédié
    confirmation_ignoree     `confirme_partage` n'est plus honoré (on ne peut plus créer)
    pool_absent_averti       l'abstention « nœud sans pool de calcul » retirée
    proprietaires_muets      il ne dit plus qui tient les cœurs
    fermeture_vaut_creer     (interface) fermer la fenêtre crée quand même

    ./venv/bin/python tests/verif_creation_avertit_sans_coeur_dedie.py
"""
import json
import os
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_creation_dedie_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool, docker_compute                               # noqa: E402

MUT = os.environ.get("CREATION_DEDIE_MUTATION") or ""
MUTATIONS = {
    "porte_absente": ("app/routes/__init__.py",
                      '        if not data.get("confirme_partage"):\n'
                      '            _pf = docker_compute.preflight_dedication(int(node_id), d_type)',
                      '        if False:\n'
                      '            _pf = docker_compute.preflight_dedication(int(node_id), d_type)'),
    "porte_sans_mesure": ("app/docker_compute.py",
                          '    out["suffisant"] = out["capacite"] >= want',
                          '    out["suffisant"] = True'),
    "capacite_logique": ("app/docker_compute.py",
                         '    out["capacite"] = core_pool.free_dedicated_capacity('
                         'node_id, None, physical=physique)',
                         '    out["capacite"] = len(core_pool.parse_cpuset(out["pool"]))'),
    # ⚠ NE PAS muter `if want <= 0: return out` dans le préflight : ce serait INERTE (want=0
    # donne capacite=0, donc `0 >= 0`, donc `suffisant`). C'est une économie de requête, pas la
    # barrière. L'erreur réaliste est dans la CONDITION de la route : avertir dès que le nœud
    # n'a plus un cœur libre, sans regarder si le type en demandait.
    "avertit_les_partages": ("app/routes/__init__.py",
                             '            if _pf["demande"] > 0 and not _pf["suffisant"]:',
                             '            if not _pf["capacite"]:'),
    "pool_absent_averti": ("app/docker_compute.py",
                           '    if not out["pool"]:\n'
                           '        out["raison"] = "no compute pool on this node"\n'
                           "        return out",
                           '    if not out["pool"]:\n'
                           '        out["raison"] = "no compute pool on this node"'),
    "confirmation_ignoree": ("app/routes/__init__.py",
                             '        if not data.get("confirme_partage"):',
                             "        if True:"),
    "proprietaires_muets": ("app/docker_compute.py",
                            '    if not out["suffisant"]:\n'
                            '        # Qui les tient, nommément',
                            '    if False:\n'
                            '        # Qui les tient, nommément'),
    # Mutation d'INTERFACE : elle porte sur le texte de scripts.js, relu par la section 6.
    "fermeture_vaut_creer": ("static/scripts.js",
                             "        dlg.addEventListener('close', () => rendre('liberer'), "
                             "{ once: true });",
                             "        dlg.addEventListener('close', () => rendre('creer'), "
                             "{ once: true });"),
}
_MUT_JS = ""
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _f, _old, _new = MUTATIONS[MUT]
    with open(os.path.join(RACINE, _f), encoding="utf-8") as _fh:
        _src = _fh.read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) dans %s — la garde est périmée"
              % (MUT, _src.count(_old), _f))
        sys.exit(1)
    if _f.endswith(".js"):
        _MUT_JS = _src.replace(_old, _new)          # la section 7 lit CE texte
    elif _f == "app/docker_compute.py":
        exec(compile(_src.replace(_old, _new), docker_compute.__file__, "exec"),   # noqa: S102
             docker_compute.__dict__)
    # app/routes/__init__.py : appliqué APRÈS la création de l'application (cf. _muter_la_route),
    # sinon il n'y a pas encore de table de points d'entrée à corriger.
    print("MUTATION active :", MUT)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


def js_source():
    """Le texte de `static/scripts.js` — muté en mémoire si la campagne le demande."""
    if _MUT_JS:
        return _MUT_JS
    with open(os.path.join(RACINE, "static/scripts.js"), encoding="utf-8") as f:
        return f.read()


# ── Deux nœuds : l'un saturé (dl360-1 du banc), l'autre avec des cœurs libres ────────────────
SATURE = database.db_add_node("sature", "192.0.2.10")
LIBRE = database.db_add_node("libre", "192.0.2.11")
database.db_update_node(SATURE, compute_cpuset="18-23", agent_url="http://192.0.2.10:9100")
database.db_update_node(LIBRE, compute_cpuset="18-23", agent_url="http://192.0.2.11:9100")
core_pool.core_map_cached = lambda nid: {}          # modèle plat : pas de carte HT à inventer
core_pool.numa_map_cached = lambda nid: {}
core_pool.isolated_cached = lambda nid: set()
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)

# Le nœud saturé : six conteneurs tiennent ses six cœurs (comme dl360-1 au banc).
for i, c in enumerate(range(18, 24)):
    v = 7000 + i
    database.db_upsert_container_docker(v, "preneur-%d" % i, SATURE, "bobi-%d" % v)
    database.db_update_deploy_config(v, "mixer", {})
    core_pool.allocate_cores(SATURE, v, 1)

# ── Un type DÉDIÉ et un type PARTAGÉ, pris dans le registre réel ─────────────────────────────
from app import plugins                                                 # noqa: E402

# Pris dans le registre RÉEL (`plugins.all()`), pas écrits en dur : un type dont le manifeste
# changerait de niveau ferait alors mentir la garde en silence.
_TYPES = sorted(m["type"] for m in (plugins.all() or []) if m.get("type"))
_DEDIE = next(t for t in _TYPES if docker_compute.effective_resources(t, {}).get("pin"))
_PARTAGE = next(t for t in _TYPES if not docker_compute.effective_resources(t, {}).get("pin")
                and not (plugins.get(t) or {}).get("auto_provision")
                and not (plugins.get(t) or {}).get("one_per_node")
                and plugins.image_variant(t) not in ("media", "gui")
                and not plugins.wants_media_volume(t))

print("\n1. ★ `preflight_dedication` mesure, ne réserve rien, et nomme les propriétaires")
print("   (type dédié « %s », type partagé « %s »)" % (_DEDIE, _PARTAGE))
_avant = dict(core_pool.allocations_by_vmid(SATURE))
pf = docker_compute.preflight_dedication(SATURE, _DEDIE)
verifie(pf["demande"] > 0, "1a. le type dédié demande des cœurs", pf)
verifie(pf["capacite"] == 0 and pf["suffisant"] is False,
        "1b. ★★ nœud saturé : capacité STRICTE à 0, donc insuffisant", pf)
verifie(dict(core_pool.allocations_by_vmid(SATURE)) == _avant,
        "1c. ★ et RIEN n'a été réservé au passage (c'est une mesure, pas une allocation)")
verifie([o["hostname"] for o in pf["proprietaires"]] == ["preneur-%d" % i for i in range(6)],
        "1d. ★★ les propriétaires sont NOMMÉS : « libérer des cœurs » n'est actionnable que si "
        "l'exploitant sait lesquels", pf["proprietaires"])
verifie(all(o["coeurs"] for o in pf["proprietaires"]),
        "1e. … avec les cœurs que chacun tient", pf["proprietaires"])

print("\n2. ★ Un type qui ne demande aucun cœur dédié n'est JAMAIS averti")
pf0 = docker_compute.preflight_dedication(SATURE, _PARTAGE)
verifie(pf0["demande"] == 0 and pf0["suffisant"] is True,
        "2a. ★★ même sur le nœud saturé : rien à promettre, donc rien à avertir (une porte qui "
        "crie à tort s'apprend à cliquer sans lire)", pf0)

print("\n3. ★ Un nœud qui a des cœurs libres ne déclenche rien")
pfl = docker_compute.preflight_dedication(LIBRE, _DEDIE)
verifie(pfl["capacite"] >= pfl["demande"] and pfl["suffisant"] is True,
        "3a. capacité suffisante → aucun avertissement", pfl)

# ── La VRAIE route, avec la création docker remplacée ────────────────────────────────────────
import main as app_main                                                 # noqa: E402
from app import deploy as _deploy                                       # noqa: E402

app = app_main.app
app.config["TESTING"] = True
CREES = []
_pose = threading.Event()


def _faux_creer(node_id, d_type, hostname=None, **kw):
    CREES.append((node_id, d_type, hostname))
    _pose.set()
    return 9001


docker_compute.creer_container_compute = _faux_creer
_deploy.deployer_script = lambda *a, **k: True
database.db_create_user("admin_t", "x", "admin")
cli = app.test_client()
with cli.session_transaction() as s:
    u = database.db_get_user("admin_t")
    s["user_id"], s["username"], s["role"] = u["id"], u["username"], u["role"]

def _muter_la_route():
    """Remplace la vue `creer` ENREGISTRÉE par sa version mutée.

    ⚠ LE PIÈGE PAYÉ ICI (2026-11-07) : la première version écrivait
    `app.view_functions["main.creer"]`. Le point d'entrée s'appelle `routes.creer` — l'affectation
    créait donc une clé MORTE, la vraie route continuait de tourner, et les trois mutations de
    route rendaient `rc=0`… que j'ai d'abord lu comme « la garde ne les attrape pas » alors que
    rien n'était muté. D'où l'assertion : la clé doit DÉJÀ exister.
    """
    import ast
    with open(os.path.join(RACINE, "app/routes/__init__.py"), encoding="utf-8") as fh:
        src = fh.read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "creer")
    seg = ast.get_source_segment(src, fn)
    # Le décorateur `@bp.route` est RETIRÉ : le Blueprint est déjà enregistré, et Flask refuse
    # qu'on y ajoute une règle après coup. `@require_perm` reste (la porte d'habilitation fait
    # partie de ce qu'on mesure).
    seg = "\n".join(l for l in seg.splitlines() if not l.startswith("@bp.route"))
    import app.routes as _rt
    exec(compile(seg.replace(_old, _new), "creer<mute>", "exec"), _rt.__dict__)    # noqa: S102
    cle = "routes.creer"
    assert cle in app.view_functions, "point d'entree %s introuvable : la garde est perimee" % cle
    app.view_functions[cle] = _rt.creer


if MUT and MUTATIONS[MUT][0] == "app/routes/__init__.py":
    _muter_la_route()


def creer(node_id, d_type, hostname, **extra):
    CREES.clear()
    _pose.clear()
    rv = cli.post("/api/containers", json=dict({"node_id": node_id, "deploy_type": d_type,
                                                "hostname": hostname}, **extra))
    if rv.status_code == 200:
        _pose.wait(10)
        time.sleep(0.05)
    return rv.status_code, (rv.get_json() or {})


print("\n3bis. ★ Un nœud SANS pool de calcul : on s'abstient, on ne crie pas")
# « Le pool est plein » et « il n'y a pas de pool » donnent la même capacité — zéro — et sont
# opposés : le second est un nœud jamais préparé, qui a déjà son propre avertissement dans
# `effective_cpuset`. Sans cette distinction la porte se déclenchait sur toute installation neuve
# (constaté le 07/11 sur le nœud de banc de verif_mixer_assistant).
SANSPOOL = database.db_add_node("sans-pool", "192.0.2.12")
pfn = docker_compute.preflight_dedication(SANSPOOL, _DEDIE)
verifie(pfn["suffisant"] is True and pfn["raison"] == "no compute pool on this node",
        "3bis-a. ★★ abstention nommée, pas un avertissement", pfn)

print("\n4. ★ La porte est SUR LE SERVEUR, et aucune ligne n'est créée")
code, j = creer(SATURE, _DEDIE, "neuf-1")
verifie(code == 409, "4a. ★★ 409 : la création est suspendue, pas exécutée", (code, j))
verifie(j.get("error_code") == "no_dedicated_core" and j.get("confirmation_requise") is True,
        "4b. ★ avec de quoi l'interface construire un CHOIX (error_code + confirmation_requise)", j)
verifie(not CREES, "4c. ★★ et RIEN n'a été créé — après, la ligne existe et il faut nettoyer un "
                   "conteneur qui n'aurait jamais dû naître", CREES)
verifie(str(j.get("error") or "").strip() != ""
        and "{" not in str(j.get("error") or ""),
        "4d. le message est rendu (clé i18n résolue, pas de placeholder nu)", j.get("error"))
verifie(j.get("node_id") == SATURE and (j.get("dedication") or {}).get("proprietaires"),
        "4e. ★ le nœud et les propriétaires voyagent avec le refus (l'interface ouvre la fenêtre "
        "des cœurs du BON nœud)", j)

print("\n5. ★ `confirme_partage` passe outre : c'est un avertissement, pas un refus")
code, j = creer(SATURE, _DEDIE, "neuf-2", confirme_partage=True)
verifie(code == 200 and CREES, "5a. ★★ création effectuée après confirmation", (code, j, CREES))

print("\n6. Un type partagé, et un nœud libre, passent sans rien demander")
code, j = creer(SATURE, _PARTAGE, "neuf-3")
verifie(code == 200 and CREES, "6a. type partagé sur le nœud saturé : aucune porte", (code, j))
code, j = creer(LIBRE, _DEDIE, "neuf-4")
verifie(code == 200 and CREES, "6b. type dédié sur un nœud qui a des cœurs : aucune porte", (code, j))
code, j = creer(SANSPOOL, _DEDIE, "neuf-5")
verifie(code == 200 and CREES,
        "6c. ★★ nœud sans pool de calcul : aucune porte (sinon elle crierait sur toute "
        "installation neuve, et on s'apprendrait à cliquer sans lire)", (code, j))

print("\n7. ★ Côté interface : le drapeau est envoyé, et fermer ne vaut jamais « créer »")
_js = js_source()
verifie("confirme_partage: !!confirmePartage" in _js,
        "7a. le POST porte la réponse à la porte serveur")
verifie("no_dedicated_core" in _js and "_createDedieDialog" in _js,
        "7b. le 409 est reconnu et ouvre la fenêtre de choix")
verifie("rendre('creer')" in _js and "rendre('liberer')" in _js,
        "7c. les deux issues existent")
verifie("dlg.addEventListener('close', () => rendre('liberer')" in _js,
        "7d. ★★ Échap / clic hors de la fenêtre = « libérer », JAMAIS « créer » : une fenêtre "
        "fermée par réflexe ne doit pas créer un conteneur temps réel sans cœurs")
verifie("window.BobiCores" in _js,
        "7e. « libérer des cœurs » ouvre la fenêtre des cœurs du nœud, pas un message qui "
        "laisse l'exploitant chercher l'écran")
verifie("_createCart = garde;" in _js,
        "7f. ★ le panier GARDE les lignes abandonnées (l'humain vient de dire qu'il y revient)")

print("\n8. Les clés i18n existent dans les deux catalogues")
for f in ("i18n/fr.json", "i18n/en.json"):
    with open(os.path.join(RACINE, f), encoding="utf-8") as fh:
        cat = json.load(fh)
    manquantes = [k for k in ("api.containers.aucun_coeur_dediable",
                              "api.containers.coeurs_dediables_insuffisants",
                              "js.create.dedie_titre", "js.create.dedie_aide",
                              "js.create.dedie_tenus", "js.create.dedie_quand_meme",
                              "js.create.dedie_liberer", "js.create.dedie_annule")
                  if k not in cat]
    verifie(not manquantes, "8. %s complet" % f, manquantes)

print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ aucun cœur dédiable → avertissement à la création, porte serveur, choix de l'humain, "
      "rien créé sans son mot")
