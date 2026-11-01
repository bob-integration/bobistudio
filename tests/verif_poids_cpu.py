#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""POIDS CPU : quatre niveaux nommés, appliqués à chaud, et MESURÉS.

★ CE QUI MANQUAIT (arbitrage de Cyril, 2026-10-30). Il n'y avait que DEUX poids dans tout le
produit : 1024 (le défaut de Docker, hérité sans que personne le pose) et 128, écrit en dur pour
le profil `batch` du transcodeur. Deux conséquences :
  - aucun moyen de faire passer un conteneur d'ANTENNE devant un aperçu sur un nœud saturé ;
  - `priority: batch` confondait la PLACE (pool partagé) et le POIDS (la part sous saturation) :
    « partagé mais prioritaire » n'existait pas, et « dédié mais effaçable » non plus.

Ce que cette garde tient :
1. quatre niveaux NOMMÉS, un barème unique, et un nombre libre REFUSÉ ;
2. le poids est posé au `docker run` pour TOUT conteneur, à la valeur de son niveau ;
3. ★ il est EXCLU de la signature de spec — sinon le déploiement suivant recréerait le conteneur,
   c'est-à-dire couperait l'antenne, pour un geste déjà appliqué à chaud ;
4. ★ et comme rien ne le rattrape, il est MESURÉ sur l'hôte et confronté au voulu : un
   `docker update` manqué se VOIT (`poids_coherent: false`) au lieu de passer inaperçu ;
5. l'ancien `priority: batch` continue de valoir « batch » (manifestes installés, configs
   persistées) ;
6. la surcharge par conteneur prime sur le manifeste, et la réapplication ne touche QUE ce qui
   diverge — rien du tout si le poids n'est pas mesurable ;
7. PLACE et POIDS restent deux colonnes : un dédié ne publie plus « temps réel » (tautologie
   affichée comme une qualité, à côté d'un dédié qui pouvait ne pas tenir sa cadence) ;
8. la bascule du mode d'énergie exige `confirme` CÔTÉ SERVEUR ;
9. le niveau est exposé aux MACROS (sinon la capacité est morte, cf. CLAUDE.md).

Hors ligne : base jetable, hôte et relevé de placement doublés.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_poids_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import core_pool, cpu_weight, docker_compute, node_driver, placement  # noqa: E402
from app import plugins                                                       # noqa: E402

echecs = []
_n = [0]


def verifie(ok, titre, detail=""):
    _n[0] += 1
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Doublures : rien ne sort de ce processus ────────────────────────────────────────────────────
_hote = {"cmds": []}


def _faux_host_exec(node, cmd, timeout=None, **kw):
    _hote["cmds"].append(cmd)
    return (0, "", "")


node_driver.host_exec = _faux_host_exec
docker_compute.ssh_run = lambda host, cmd, timeout=None, **kw: (0, "", "")
core_pool.numa_map_cached = lambda nid: {}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)

# Topologie HT : 8 cœurs logiques, 4 physiques (CORE = CPU mod 4) — comme la Z8 à l'échelle 1/6.
_CARTE = {c: c % 4 for c in range(8)}
core_pool.core_map_cached = lambda nid: dict(_CARTE)

# Ce que l'hôte RENVERRAIT : cpuset et POIDS réellement posés, par nom docker.
_releve = {"ok": True, "cpusets": {}, "poids": {}}


def _faux_releve(node_id, ttl=None, force=False):
    if not _releve["ok"]:
        return None
    return {"isolated": None, "nproc": 8,
            "containers": [{"name": n, "cpuset": _releve["cpusets"].get(n, ""),
                            "cpu_shares": _releve["poids"].get(n), "threads": {}}
                           for n in sorted(set(_releve["cpusets"]) | set(_releve["poids"]))]}


placement.releve_cache = _faux_releve

NID = database.db_add_node("z8", "127.0.0.1")
database.db_update_node(NID, compute_cpuset="0-7", agent_url="http://127.0.0.1:9100",
                        compute_image="bobi-compute:1.0", docker_network="bobi-net")
NODE = database.db_get_node(NID)

# Profils de types, posés dans le registre des plugins (aucun fichier touché).
_PROFILS = {
    "multiview": {"cores": 3, "pin": True},            # dédié, poids normal (rien de déclaré)
    "transcoder": {"cores": 2, "priority": "batch"},   # ancien champ : doit valoir « batch »
    "streamer": {"cores": 2, "cpu_weight": "background"},   # défaut au MANIFESTE
    "mixer": {},                                       # aucun profil du tout
}
plugins.get = lambda t: ({"resources": dict(_PROFILS[t])} if t in _PROFILS else None)
plugins.image_variant = lambda t: "compute"
plugins.wants_media_volume = lambda t: False
plugins.wants_lv2_volume = lambda t: False
plugins.wants_state_volume = lambda t: False
plugins.needs_decklink = lambda t: False
plugins.gpu_capable = lambda t: False


def _ajoute(vmid, nom, typ):
    database.db_upsert_container_docker(vmid, nom, NID, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, typ, {})
    return database.db_get_container(vmid)


print("\n1. ★ Quatre niveaux NOMMÉS, un barème unique, aucun nombre libre")
verifie(cpu_weight.LEVELS == ("onair", "normal", "background", "batch"),
        "1a. les quatre niveaux, du plus au moins prioritaire", str(cpu_weight.LEVELS))
verifie([cpu_weight.WEIGHTS[l] for l in cpu_weight.LEVELS] == [2048, 1024, 256, 128],
        "1b. barème 2048 / 1024 / 256 / 128",
        str([cpu_weight.WEIGHTS[l] for l in cpu_weight.LEVELS]))
verifie(cpu_weight.DEFAULT == "normal" and cpu_weight.value("normal") == 1024,
        "1c. défaut = Normal (1024)")
verifie(not cpu_weight.niveau_valide("1500") and not cpu_weight.niveau_valide("urgent"),
        "1d. ★ un nombre libre, ou un nom inventé, n'est PAS un niveau")
verifie(cpu_weight.value("1500") == 1024,
        "1e. et un niveau inconnu retombe sur le défaut sans lever (un manifeste fautif "
        "n'empêche pas un déploiement, il se journalise)", str(cpu_weight.value("1500")))

print("\n2. Le niveau vient du manifeste, de l'ancien `priority`, ou du conteneur")
c_mv = _ajoute(4101, "mur", "multiview")
c_tr = _ajoute(4102, "transcodeur", "transcoder")
c_st = _ajoute(4103, "monitor-u1", "streamer")
c_mx = _ajoute(4104, "melangeur", "mixer")
verifie(cpu_weight.for_container(c_mv) == {"niveau": "normal", "valeur": 1024,
                                           "source": "default"},
        "2a. type sans rien de déclaré → Normal, source `default`",
        str(cpu_weight.for_container(c_mv)))
verifie(cpu_weight.for_container(c_tr)["niveau"] == "batch"
        and cpu_weight.for_container(c_tr)["source"] == "priority",
        "2b. ★ `priority: batch` vaut toujours « batch » — le retirer relèverait en silence le "
        "transcodeur au poids normal, et il mangerait le CPU des conteneurs d'antenne",
        str(cpu_weight.for_container(c_tr)))
verifie(cpu_weight.for_container(c_st) == {"niveau": "background", "valeur": 256,
                                           "source": "manifest"},
        "2c. `resources.cpu_weight` du manifeste = défaut du TYPE",
        str(cpu_weight.for_container(c_st)))
verifie(cpu_weight.for_container(c_mx)["niveau"] == "normal",
        "2d. un type sans aucun profil de ressources a quand même un poids (c'est lui qu'on "
        "veut pouvoir faire céder)")
database.db_set_resources_override(4103, cpu_weight="onair")
verifie(cpu_weight.for_container(database.db_get_container(4103))
        == {"niveau": "onair", "valeur": 2048, "source": "override"},
        "2e. ★ la surcharge du CONTENEUR prime sur son manifeste (un moniteur relevé en "
        "antenne reste en antenne)",
        str(cpu_weight.for_container(database.db_get_container(4103))))
database.db_set_resources_override(4103, cpu_weight="")
verifie(cpu_weight.for_container(database.db_get_container(4103))["source"] == "manifest",
        "2f. et `cpu_weight=\"\"` le rend à son manifeste")

print("\n3. ★ Le poids est POSÉ au `docker run`, et HORS de la signature de spec")
_spec = {"captee": None}


def _faux_run(node, spec):
    _spec["captee"] = spec
    return True, {"ip": "10.0.0.9"}


node_driver.run_container = _faux_run
node_driver.has_agent = lambda n: True
docker_compute.status_compute = lambda v: "running"
docker_compute._cert_mtls_conteneur = lambda v: None
docker_compute.db_update_docker_ip = lambda v, ip: None
import app.deploy as _deploy                                                  # noqa: E402
_deploy.token_a_injecter = lambda v: None
_deploy.agent_session = lambda: type("S", (), {"get": lambda *a, **k: type(
    "R", (), {"status_code": 200})()})()
_deploy.agent_url = lambda ip, p: "http://x" + p
_deploy.agent_headers = lambda v: {}

docker_compute.deploy_compute(4102, params={}, deploy_type="transcoder", force=True)
_res = ((_spec["captee"] or {}).get("resources") or {})
verifie(_res.get("cpu_shares") == 128,
        "3a. le transcodeur (batch) part avec --cpu-shares 128", str(_res))
docker_compute.deploy_compute(4101, params={}, deploy_type="multiview", force=True)
_res_mv = ((_spec["captee"] or {}).get("resources") or {})
verifie(_res_mv.get("cpu_shares") == 1024,
        "3b. ★ et TOUT conteneur en pose un : avant, seul le batch était servi et le reste "
        "héritait du défaut de Docker — il n'y avait rien à régler", str(_res_mv))
_a = {"name": "x", "resources": {"cpuset": "0-3", "cpu_shares": 1024}}
_b = {"name": "x", "resources": {"cpuset": "0-3", "cpu_shares": 2048}}
verifie(docker_compute._signature_spec(_a) == docker_compute._signature_spec(_b),
        "3c. ★ deux specs qui ne diffèrent QUE par le poids ont la même signature : changer un "
        "poids ne provoque aucune recréation (donc aucune coupure différée)")
_c = {"name": "x", "resources": {"cpuset": "0-2", "cpu_shares": 1024}}
verifie(docker_compute._signature_spec(_a) != docker_compute._signature_spec(_c),
        "3d. mais un cpuset différent, lui, reste une spec différente (la signature n'a pas été "
        "vidée de son sens)")

print("\n4. ★ Appliqué À CHAUD, et le poids POSÉ est MESURÉ")
_hote["cmds"] = []
r = cpu_weight.regler(4101, "onair")
verifie(r["ok"] and r["applique"] and r["niveau"] == "onair", "4a. réglage accepté", str(r))
verifie(any("docker update --cpu-shares=2048" in c and "bobi-4101" in c for c in _hote["cmds"]),
        "4b. ★ `docker update --cpu-shares` sur le conteneur EN MARCHE — recréer serait couper "
        "l'antenne pour un geste d'intendance", str(_hote["cmds"]))
verifie(database.db_get_container(4101).get("cpu_weight_override") == "onair",
        "4c. et le niveau est persisté (il survit à une recréation)")

_releve["cpusets"] = {"bobi-4101": "0-2", "bobi-4102": "3-7",
                      "bobi-4103": "3-7", "bobi-4104": "3-7"}
_releve["poids"] = {"bobi-4101": 2048, "bobi-4102": 128, "bobi-4103": 0, "bobi-4104": 1024}
core_pool.allocate_cores(NID, 4101, 3)
vue = core_pool.vue_coeurs(NID, force=True)
_par = {l["vmid"]: l for l in vue["dedies"] + vue["partages"]}
verifie(_par[4101]["poids_applique"] == 2048 and _par[4101]["poids_coherent"] is True,
        "4d. poids mesuré == voulu → cohérent", str(_par[4101].get("poids_applique")))
verifie(_par[4103]["poids_applique"] == 1024,
        "4e. `CpuShares: 0` est traduit en 1024 — Docker ne dit pas « aucun poids », il dit "
        "« je n'ai rien eu à poser »", str(_par[4103].get("poids_applique")))
verifie(_par[4103]["poids"]["valeur"] == 256 and _par[4103]["poids_coherent"] is False,
        "4f. ★ un poids voulu à 256 et posé à 1024 est SIGNALÉ : comme le poids est hors "
        "signature, rien ne le rattraperait — sans cette mesure, un `docker update` manqué "
        "resterait invisible pour toujours", str(_par[4103]))
_releve["ok"] = False
vue_sans = core_pool.vue_coeurs(NID, force=True)
_p2 = {l["vmid"]: l for l in vue_sans["dedies"] + vue_sans["partages"]}
verifie(all(l["poids_coherent"] is None for l in _p2.values()),
        "4g. nœud injoignable → `poids_coherent` est None, jamais False (ne pas accuser ce "
        "qu'on n'a pas mesuré)")
_releve["ok"] = True

print("\n5. La réapplication ne touche QUE ce qui diverge — et rien sans mesure")
_hote["cmds"] = []
res = cpu_weight.reappliquer(NID, force=True)
verifie([f["vmid"] for f in res["faits"]] == [4103],
        "5a. seul le conteneur divergent est corrigé", str(res))
verifie(set(res["inchanges"]) == {4101, 4102, 4104},
        "5b. les autres sont laissés tranquilles (aucun `docker update` inutile)", str(res))
verifie(sum(1 for c in _hote["cmds"] if "docker update" in c) == 1,
        "5c. une seule commande envoyée", str(_hote["cmds"]))
_releve["ok"] = False
_hote["cmds"] = []
res2 = cpu_weight.reappliquer(NID, force=True)
verifie(not res2["faits"] and not any("docker update" in c for c in _hote["cmds"]),
        "5d. ★ placement non mesurable → on ne corrige RIEN à l'aveugle (même règle que les "
        "cpusets partagés : sans mesure, « inchangé » ne veut rien dire)", str(res2))
verifie(sorted(res2["non_mesures"]) == [4101, 4102, 4103, 4104],
        "5e. et les conteneurs non mesurés sont NOMMÉS", str(res2))
_releve["ok"] = True

print("\n6. PLACE et POIDS : deux colonnes, jamais confondues")
vue = core_pool.vue_coeurs(NID, force=True)
_par = {l["vmid"]: l for l in vue["dedies"] + vue["partages"]}
verifie(_par[4101]["place"] == "dedie" and _par[4101]["plafond"] is None,
        "6a. le multiview épinglé est à la place « dédié »", str(_par[4101].get("place")))
verifie(_par[4102]["place"] == "partage_plafonne" and _par[4102]["plafond"] == 2,
        "6b. le transcodeur est dans le pool, PLAFONNÉ à 2 cœurs (`--cpus`) : une place, pas un "
        "poids", str(_par[4102].get("place")))
verifie("temps_reel" not in _par[4101],
        "6c. ★ un DÉDIÉ ne publie plus « temps réel » : c'était une tautologie (un dédié n'est "
        "jamais batch) affichée comme une qualité, juste à côté d'un dédié qui ne tenait pas sa "
        "cadence", str(sorted(_par[4101])))
verifie("temps_reel" in _par[4104],
        "6d. mais le pool partagé garde la notion : c'est elle qui décide du verdict « trop peu "
        "de cœurs pour les conteneurs temps réel »")

print("\n7. Protection, voisins, grille, synthèse")
verifie(_par[4101]["protection"] == "partie"
        and "freres" in _par[4101]["protection_raison"],
        "7a. le multiview 0-2 a ses frères HT 4-6 dans le pool → « protégé en partie », avec la "
        "RAISON", str(_par[4101].get("protection_raison")))
verifie(_par[4104]["protection"] == "non",
        "7b. un partagé n'est pas protégé, et le dit")
verifie(any(v["via"] == "frere_ht" for v in _par[4101]["partage_avec"]),
        "7c. ★ « partage avec » nomme le voisinage par FRÈRE HT — le seul qu'aucun cpuset ne "
        "montre", str(_par[4101].get("partage_avec")))
verifie(any(v["vmid"] == 4104 and v["via"] == "cpuset" for v in _par[4102]["partage_avec"]),
        "7d. et le voisinage ordinaire du pool, séparément", str(_par[4102].get("partage_avec")))
_g = vue["coeurs"]
verifie(len(_g) == 4 and all(len(x["threads"]) == 2 for x in _g),
        "7e. grille = 4 cœurs PHYSIQUES, deux threads HT chacun", str(len(_g)))
verifie([x["phys"] for x in _g] == [0, 1, 2, 3],
        "7f. un emplacement par cœur physique, dans l'ordre", str([x["phys"] for x in _g]))
_p0 = next(x for x in _g if x["phys"] == 0)
verifie(_p0["frere_partage"] is True,
        "7g. ★ le cœur physique 0 porte un thread dédié (0) et son frère (4) dans le pool : "
        "signalé — « dédié » n'y veut plus dire dédié", str(_p0))
verifie([t["kind"] for t in _p0["threads"]] == ["dedie", "partage"],
        "7h. et chaque thread dit QUI l'occupe", str([t["kind"] for t in _p0["threads"]]))
verifie(_p0["threads"][0]["vmids"] == [4101],
        "7i. avec le vmid du propriétaire (c'est ce qui manquait : « used 49 / free 2 » ne "
        "nomme personne)", str(_p0["threads"][0]))
verifie(vue["synthese"]["partie"] == 1 and vue["synthese"]["non"] == 3,
        "7j. synthèse de protection cliquable : 1 en partie, 3 non protégés",
        str(vue["synthese"]))
verifie(vue["synthese"]["poids_bas"] == 2 and vue["synthese"]["poids_haut"] == 1,
        "7k. et la synthèse des poids (batch + arrière-plan bas, antenne haut)",
        str(vue["synthese"]))
verifie([n["niveau"] for n in vue["poids_niveaux"]] == list(cpu_weight.LEVELS),
        "7l. le barème est PUBLIÉ par le serveur — l'interface ne le recopie pas (deux barèmes "
        "divergeraient en silence)", str(vue["poids_niveaux"]))

print("\n8. Charge MESURÉE, avec son unité et son écrêtage")
database.db_update_usage(4101, 55.0, 1024, cpu_count=4)
_c = database.db_get_container(4101)
verifie(core_pool._charge_coeurs(_c) == (2.2, False),
        "8a. ★ 55 % de 4 CPU = 2,2 cœurs — `cpu_percent` est un pourcentage des CPU ALLOUÉS, "
        "pas d'un cœur (c'est l'unité implicite qui a produit un facteur 30 le 01/08)",
        str(core_pool._charge_coeurs(_c)))
database.db_update_usage(4101, 100.0, 1024, cpu_count=4)
verifie(core_pool._charge_coeurs(database.db_get_container(4101)) == (4.0, True),
        "8b. ★ à 100 % l'agent ÉCRÊTE : la valeur est un MINORANT, et elle est marquée comme "
        "telle (sans quoi « réservé contre consommé » concluerait à tort au sur-dimensionnement)",
        str(core_pool._charge_coeurs(database.db_get_container(4101))))
database.db_update_usage(4102, 42.0, 1024, cpu_count=None)
verifie(core_pool._charge_coeurs(database.db_get_container(4102)) == (None, False),
        "8c. sans `cpu_count`, la charge n'a pas d'unité : on rend None, jamais un chiffre nu",
        str(core_pool._charge_coeurs(database.db_get_container(4102))))

print("\n9. ★ Routes : gate serveur, et refus nommés")
import app.routes.nodes as rn                                                 # noqa: E402
import app.routes.__init__ as _ri                                            # noqa: E402
from flask import Flask                                                       # noqa: E402

_app = Flask(__name__)


def _appel(vue_fn, **kw):
    """Appelle une vue Flask hors requête. Une vue rend soit (réponse, code), soit la réponse."""
    with _app.test_request_context(json=kw.pop("json", None) or {}):
        out = vue_fn.__wrapped__(**kw) if hasattr(vue_fn, "__wrapped__") else vue_fn(**kw)
    if isinstance(out, tuple):
        return out[0].get_json(), out[1]
    return out.get_json(), 200

_j, _code = _appel(rn.api_node_energy_performance, node_id=NID, json={})
verifie(_code == 409 and _j.get("confirmation_requise"),
        "9a. ★ passer un nœud en « performance » SANS `confirme` est refusé par le SERVEUR : "
        "une confirmation qui ne vit que dans l'interface n'en est pas une (cf. la bascule "
        "réseau du 28/10, partie sans confirmation, qui a effacé douze conteneurs)",
        "%s %s" % (_code, _j))
_j, _code = _appel(rn.api_node_cores_weight, node_id=NID, json={"vmid": 4101, "niveau": "turbo"})
verifie(_code == 400, "9b. un niveau inventé est refusé (400)", "%s %s" % (_code, _j))
_autre = database.db_add_node("autre", "127.0.0.2")
database.db_upsert_container_docker(4201, "ailleurs", _autre, "bobi-4201")
_j, _code = _appel(rn.api_node_cores_weight, node_id=NID, json={"vmid": 4201, "niveau": "onair"})
verifie(_code == 409,
        "9c. ★ un conteneur d'un AUTRE nœud est refusé : le vmid est un handle local, et "
        "l'accepter réglerait en silence une machine qu'on ne regarde pas", "%s %s" % (_code, _j))
_j, _code = _appel(rn.api_node_cores_weight, node_id=NID,
                   json={"vmid": 4104, "niveau": "background"})
verifie(_code == 200 and _j.get("niveau") == "background", "9d. réglage nominal accepté",
        "%s %s" % (_code, _j))

print("\n10. Mode d'énergie : mesuré, jamais deviné")
from app import mtl                                                           # noqa: E402
mtl.cpufreq_state = lambda run: None
core_pool._energie_cache.clear()
e = core_pool.etat_energie(NID, force=True)
verifie(e["mesure"] is False and e["gouverneur"] is None,
        "10a. ★ sonde illisible → « je ne sais pas », pas « powersave » : seule la mesure "
        "justifie de proposer un changement", str(e))
mtl.cpufreq_state = lambda run: {"unit": True, "script_ok": True, "dedicated": {0, 1, 2},
                                 "isolated": set(),
                                 "cpus": {c: ("powersave", 800000, 800000, 4600000)
                                          for c in range(8)}}
core_pool._energie_cache.clear()
e = core_pool.etat_energie(NID, force=True)
verifie(e["mesure"] and e["gouverneur"] == "powersave" and e["max_mhz"] == 4600,
        "10b. gouverneur et bornes MESURÉS sur l'hôte", str(e))
verifie(e["conforme"] is False,
        "10c. et le verdict de conformité est rendu (un cœur dédié à 800 MHz n'est pas un cœur "
        "dédié : mélangeur 11,1 ms → 4,4 ms au seul changement de gouverneur, Z8 29/10)", str(e))
mtl.cpufreq_state = lambda run: {"unit": True, "script_ok": True, "dedicated": {0, 1, 2},
                                 "isolated": set(),
                                 "cpus": {c: ("performance", 4600000, 800000, 4600000)
                                          if c in (0, 1, 2) else
                                          ("performance", 800000, 800000, 4600000)
                                          for c in range(8)}}
core_pool._energie_cache.clear()
verifie(core_pool.etat_energie(NID, force=True)["conforme"] is True,
        "10d. nœud conforme → conforme (le bouton n'a rien à proposer)")

print("\n11. ★ Exposé aux MACROS (sinon la capacité est morte)")
from app import core_plugins                                                  # noqa: E402
verifie("app.cpu_weight" in core_plugins.CORE_PROVIDERS,
        "11a. le module est inscrit dans les fournisseurs du cœur")
_ids = {a["id"] for a in cpu_weight.MACRO_PROVIDER["actions"]}
verifie(_ids == {"set", "reset"}, "11b. deux actions : régler, rendre au manifeste", str(_ids))
_opts = cpu_weight.action_options("set", "niveau")
verifie([o["value"] for o in _opts] == list(cpu_weight.LEVELS),
        "11c. le menu de la macro offre les quatre niveaux — et rien d'autre", str(_opts))
_ok, _msg = True, ""
try:
    out = cpu_weight.run_action("set", {"vmid": 4104, "niveau": "onair"}, {})
except Exception as e:                                                        # noqa: BLE001
    _ok, _msg = False, str(e)
verifie(_ok and database.db_get_container(4104)["cpu_weight_override"] == "onair",
        "11d. et l'action FAIT le réglage", _msg)
verifie(cpu_weight.read_state("niveau", {"vmid": 4104}) == "onair",
        "11e. l'état est lisible en condition de macro")
try:
    cpu_weight.read_state("niveau", {})
    _leve = False
except Exception:
    _leve = True
verifie(_leve,
        "11f. ★ sans conteneur désigné, l'état LÈVE au lieu de rendre « normal » : une valeur "
        "par défaut serait une affirmation fausse dans une condition de macro")

print("\n%s — %d contrôle(s), %d échec(s)"
      % ("ÉCHEC" if echecs else "OK", _n[0], len(echecs)))
for e in echecs:
    print("  ✗ " + e)
sys.exit(1 if echecs else 0)
