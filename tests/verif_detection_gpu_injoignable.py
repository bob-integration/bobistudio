#!/usr/bin/env python3
"""Une sonde GPU qui n'aboutit pas ne doit RIEN écrire en base — et le dire.

LA PANNE (Z8, 2026-10-28, recette custom-216e388666). `_probe_gpu` passait par `_ssh_bin`, un
root-SSH brut, sur un nœud enrôlé sans clé root du contrôleur. Les trois sondes échouaient, les
échecs étaient avalés, et la route concluait « aucun GPU NVIDIA détecté » en écrivant
`gpu_capable=0`. Le testeur voyait « — » dans le champ GPU alors que la page Pilote NVIDIA voyait la
Quadro RTX 4000 et la jugeait conforme.

DEUX défauts distincts, et c'est le second qui fait mal :
  1. le mauvais transport (corrigé : `host_ops.ssh_run`, qui route vers l'agent) ;
  2. **« je n'ai pas pu regarder » rendu comme « il n'y a pas de GPU »**. Celui-là écrit en base :
     il retire un nœud GPU sain du routage, sans trace, et une sonde ultérieure ne le répare que
     si elle aboutit. Un transport réparé n'immunise pas contre lui — la prochaine cause
     d'injoignabilité (agent arrêté, réseau coupé) reproduirait le même effacement.

CE QUI EST MESURÉ ICI : le comportement de la route face à une exécution qui n'aboutit pas
(rc 255, contrat documenté de `node_driver.host_exec` comme de `host_ops.ssh_run`), et face à une
qui aboutit. Pas le transport : c'est `verif_ssh_agent_aware.py` qui le tient, par AST.

Hors ligne : base JETABLE posée avant tout accès, `ssh_run` doublé, aucun réseau.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# Base jetable AVANT tout accès (la connexion SQLite est par THREAD : après, c'est trop tard), et
# les DEUX chemins — `config.DB_PATH` sert à dériver la base des médias, créée à l'import.
_TMP = tempfile.mkdtemp(prefix="verif_gpu_injoignable_")
from app import config                                                        # noqa: E402
config.DB_PATH = os.path.join(_TMP, "t.db")
from app import database                                                      # noqa: E402
database.DB_PATH = config.DB_PATH
database.init_db()

from app import host_ops                                                      # noqa: E402
from app.routes import nodes as routes_nodes                                  # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Doublure de ssh_run ─────────────────────────────────────────────────────────────────────
# `_probe_gpu` importe `ssh_run` À L'INTÉRIEUR de la fonction (`from ..host_ops import ssh_run`),
# donc poser l'attribut sur le module `host_ops` suffit et reste vrai à chaque appel. Si un jour
# l'import remonte en tête de module, le contrôle « la doublure a bien été appelée » plus bas
# échouera — c'est voulu : une doublure qu'on n'a pas prouvée appelée ne mesure rien.
_appels = []


def _faire_ssh_run(reponses):
    def faux(host, cmd, input_data=None, timeout=300):
        _appels.append(cmd)
        for motif, rep in reponses:
            if motif in cmd:
                return rep
        return (1, "", "")
    return faux


def _noeud():
    database.db_add_node(name="z8", host="10.0.0.9")
    n = [x for x in database.db_get_nodes() if x["name"] == "z8"][-1]
    return n["id"]


# ── Cas 1 : hôte injoignable (rc 255 sur tout) ──────────────────────────────────────────────
nid = _noeud()
database.db_update_node(nid, gpu_capable=1, gpu_count=1)      # état sain AVANT la sonde
_appels.clear()
host_ops.ssh_run = _faire_ssh_run([("", (255, "", "agent muet"))])

cap, n, runtime_ok, names, cuda_ok, joignable = routes_nodes._probe_gpu("10.0.0.9")
verifie(len(_appels) >= 1, "la doublure de ssh_run est bien appelée par _probe_gpu",
        "aucun appel intercepté : l'import a peut-être changé de forme, ce test ne mesure plus rien")
verifie(joignable is False, "rc 255 → joignable=False",
        "la sonde ne distingue pas « pas pu regarder » de « pas de GPU »")
verifie(cap is False, "aucune capacité déduite d'une sonde qui n'a pas abouti")

avant = database.db_get_node(nid)

# La route elle-même, sans Flask : on appelle la vue en court-circuitant la permission et le
# contexte de requête — ce qui nous intéresse est l'ÉCRITURE, pas le transport HTTP.
_vue = routes_nodes.api_node_detect_gpu
_brute = getattr(_vue, "__wrapped__", None)
verifie(_brute is not None,
        "la vue expose __wrapped__ (décorateur de permission) — sinon ce test ne peut pas l'appeler")
if _brute is not None:
    from app import i18n as _i18n   # noqa: F401  (db_add_alert passe par les clés)
    import flask
    app = flask.Flask(__name__)
    with app.test_request_context():
        rep = _brute(nid)
    corps, code = (rep if isinstance(rep, tuple) else (rep, 200))
    data = corps.get_json()
    verifie(code == 502, "hôte injoignable → 502", "code rendu : %s" % code)
    verifie(data.get("joignable") is False, "la réponse porte joignable=False (lisible par l'UI)")
    apres = database.db_get_node(nid)
    verifie(apres["gpu_capable"] == avant["gpu_capable"] == 1,
            "gpu_capable NON écrasé par une sonde qui n'a pas abouti",
            "avant=%s après=%s — un nœud GPU sain vient d'être retiré du routage"
            % (avant["gpu_capable"], apres["gpu_capable"]))
    verifie(apres["gpu_count"] == avant["gpu_count"],
            "gpu_count NON écrasé non plus")

    # ── Cas 2 : sonde qui aboutit et trouve un GPU complet ──────────────────────────────────
    host_ops.ssh_run = _faire_ssh_run([
        ("nvidia-smi --query-gpu", (0, "Quadro RTX 4000\n", "")),
        ("docker info", (0, "map[nvidia:{} runc:{}]\n", "")),
        ("cuda version", (0, "    CUDA Version                          : 12.2\n", "")),
    ])
    with app.test_request_context():
        rep2 = _brute(nid)
    corps2, code2 = (rep2 if isinstance(rep2, tuple) else (rep2, 200))
    d2 = corps2.get_json()
    verifie(code2 == 200 and d2.get("gpu_capable") is True,
            "sonde aboutie + GPU + runtime + CUDA → gpu_capable",
            "code=%s gpu_capable=%s msg=%s" % (code2, d2.get("gpu_capable"), d2.get("msg")))
    verifie(d2.get("joignable") is True, "la réponse d'une sonde aboutie porte joignable=True")
    verifie("Quadro RTX 4000" in (d2.get("msg") or ""),
            "le nom de la carte figure dans le message")
    verifie(database.db_get_node(nid)["gpu_capable"] == 1,
            "une sonde aboutie écrit, elle")

    # ── Cas 3 : sonde aboutie, aucun GPU → on écrit 0, et ce n'est PAS un 502 ───────────────
    host_ops.ssh_run = _faire_ssh_run([("", (0, "", ""))])
    with app.test_request_context():
        rep3 = _brute(nid)
    corps3, code3 = (rep3 if isinstance(rep3, tuple) else (rep3, 200))
    d3 = corps3.get_json()
    verifie(code3 == 200 and d3.get("gpu_capable") is False and d3.get("joignable") is True,
            "sonde aboutie sans GPU → 200, gpu_capable=False, joignable=True",
            "code=%s cap=%s joignable=%s" % (code3, d3.get("gpu_capable"), d3.get("joignable")))
    verifie(database.db_get_node(nid)["gpu_capable"] == 0,
            "une absence RÉELLEMENT constatée est écrite, elle")

    # ── Cas 4 : injoignabilité PARTIELLE — une seule sonde sur trois rend 255 ───────────────
    # Pourquoi ce cas : avec une doublure qui rend 255 PARTOUT, retirer le contrôle d'UNE sonde ne
    # change rien, et la garde ne le voyait pas (mutation M2 muette). Or c'est la forme réaliste :
    # un `nvidia-smi` qui dépasse le délai pendant que `docker info` répond. On refuse d'écrire dès
    # qu'une sonde n'a pas abouti — conclure sur un relevé incomplet, c'est reconstruire le défaut.
    for cible, libelle in (("nvidia-smi --query-gpu", "1re sonde (nvidia-smi)"),
                           ("docker info", "2e sonde (docker info)"),
                           ("cuda version", "3e sonde (CUDA)")):
        database.db_update_node(nid, gpu_capable=1, gpu_count=1)
        host_ops.ssh_run = _faire_ssh_run([
            (cible, (255, "", "délai dépassé")),
            ("nvidia-smi --query-gpu", (0, "Quadro RTX 4000\n", "")),
            ("docker info", (0, "map[nvidia:{} runc:{}]\n", "")),
            ("cuda version", (0, "    CUDA Version : 12.2\n", "")),
        ])
        with app.test_request_context():
            r = _brute(nid)
        c, code_p = (r if isinstance(r, tuple) else (r, 200))
        verifie(code_p == 502 and c.get_json().get("joignable") is False,
                "injoignabilité partielle détectée — %s" % libelle,
                "code=%s joignable=%s" % (code_p, c.get_json().get("joignable")))
        verifie(database.db_get_node(nid)["gpu_capable"] == 1,
                "rien écrit malgré un relevé partiel — %s" % libelle,
                "gpu_capable est passé à %s" % database.db_get_node(nid)["gpu_capable"])

print()
if echecs:
    print("ÉCHEC : %d contrôle(s)" % len(echecs))
    for e in echecs:
        print("  - %s" % e)
    sys.exit(1)
print("OK — « pas pu regarder » n'écrase plus « pas de GPU », et une sonde aboutie écrit")
