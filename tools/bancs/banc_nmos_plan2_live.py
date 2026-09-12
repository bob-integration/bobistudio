#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc VIVANT du « plan 2 » : le CONTENEUR sert son propre Node API NMOS, et l'orchestrateur
# le lit en CLIENT (`services/nmos/conteneur_node.py` + la surface /x-nmos de l'agent).
#
# CE QU'IL PROUVE
# ---------------
# Que la phrase « nous utilisons NMOS entre nos conteneurs et l'orchestrateur » est VRAIE, et pas
# seulement écrite : un conteneur jetable est déployé, il sert ses ressources sur son :8081, et
# l'orchestrateur les relit et les compare à ce qu'il a lui-même calculé. Le contrôle qui compte
# est le dernier : la comparaison doit savoir dire NON. Une comparaison qui ne détecte jamais
# d'écart est une décoration.
#
# ⚠ AVANT LE REBUILD DE L'IMAGE, l'agent cuit dans `bobi-compute` ne connaît pas /x-nmos. Le banc
# le détecte et installe l'agent du dépôt dans le conteneur JETABLE (copie + restart). Une fois
# l'image reconstruite, cette étape se saute d'elle-même — le banc n'a pas à être modifié.
#
# ⚠ CE BANC MUTE : il crée et détruit un conteneur, et bascule `nmos_conteneur_node`. Tout est
# rendu dans un `finally`.
#
#   $ ./venv/bin/python tools/banc_nmos_plan2_live.py --go [--node <id>]
import argparse
import json
import os
import sys
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

HOTE = "nmosplan2"
echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def principal(node_id):
    from app import docker_compute, node_driver
    from app.containers import detruire_container
    from app.database import db_get_container, db_get_containers, db_get_nodes, \
        db_get_setting, db_set_setting
    from app.deploy import agent_headers, agent_session, agent_url, deployer_script
    from app.metrics import get_container_ip
    from app.vmlocks import verrou_vmid
    from services.nmos import conteneur_node as cn, mxl

    if HOTE in {c["hostname"] for c in db_get_containers()}:
        print("REFUS : %s existe déjà." % HOTE, file=sys.stderr)
        return 2
    noeuds = [n for n in db_get_nodes()
              if n["compute_image"] and n["docker_network"] and n["status"] == "up"]
    if node_id:
        noeuds = [n for n in noeuds if n["id"] == node_id]
    if not noeuds:
        print("REFUS : aucun nœud compute utilisable.", file=sys.stderr)
        return 2
    noeud = noeuds[0]

    avant_vmids = {c["vmid"] for c in db_get_containers()}
    avant_reglage = db_get_setting("nmos_conteneur_node", None)
    vmid = None
    try:
        db_set_setting("nmos_conteneur_node", True)
        params = json.load(open(os.path.join(RACINE, "plugins", "hello_world",
                                             "plugin.json")))["deploy_defaults"]
        params = dict(params, hostname=HOTE)
        vmid = docker_compute.creer_container_compute(noeud["id"], "hello_world", hostname=HOTE)
        if not vmid:
            print("REFUS : création impossible.", file=sys.stderr)
            return 2
        deployer_script(vmid, "hello_world", params)
        ip = get_container_ip(vmid)
        controle("le conteneur jetable est déployé et joignable", bool(ip))
        if not ip:
            return 1

        # ── L'agent sait-il servir /x-nmos ? Sinon, on l'installe dans CE conteneur ──────────
        r = agent_session().get(agent_url(ip, "/x-nmos/"), timeout=5, headers=agent_headers(vmid))
        if r.status_code == 404:
            print("  (agent d'ancienne image : installation de celui du dépôt dans le conteneur)")
            neuf = open(os.path.join(RACINE, "script_templates", "agent.py")).read()
            agent_session().post(agent_url(ip, "/deploy"),
                                 json={"path": "/opt/script/agent_new.py", "content": neuf},
                                 timeout=10, headers=agent_headers(vmid))
            nom = docker_compute._name(vmid)
            node_driver.host_exec(noeud, "docker exec %s cp /opt/script/agent_new.py "
                                         "/usr/local/bin/agent.py && docker restart %s" % (nom, nom),
                                  timeout=90)
            time.sleep(8)
            deployer_script(vmid, "hello_world", params)   # repousse script + document, et relance
            time.sleep(3)

        # ── 1. Le conteneur sert bien un Node API ───────────────────────────────────────────
        code, racine = cn.lire(vmid, "/x-nmos/")
        controle("le CONTENEUR sert /x-nmos/", code == 200 and "node/" in (racine or []),
                 "obtenu %s %s" % (code, racine))
        code, self_ = cn.lire(vmid, "/x-nmos/node/v1.3/self")
        controle("il sert son propre Node, étiqueté à son hostname",
                 code == 200 and isinstance(self_, dict) and self_.get("label") == HOTE,
                 "obtenu %s" % (self_ if code != 200 else self_.get("label")))

        code, senders = cn.lire(vmid, "/x-nmos/node/v1.3/senders")
        controle("il sert ses Senders", code == 200 and len(senders or []) == 3,
                 "hello_world produit 3 flux — obtenu %s" % (len(senders or [])))
        if code == 200 and senders:
            s = senders[0]
            controle("★ le device_id est celui DU CONTENEUR, pas celui du cluster",
                     s.get("device_id") == cn._rid("device", db_get_container(vmid).get(
                         "instance_uuid") or "vmid:%s" % vmid),
                     "les deux plans doivent porter des identités distinctes : une ressource IS-04 "
                     "n'a qu'un device_id, et le plan 1 est keyé sur des barreaux stables")
            controle("les champs BCP-007-03 sont respectés",
                     s.get("transport") == mxl.TRANSPORT and s.get("manifest_href") is None
                     and s.get("interface_bindings") == [])

        # ── 2. Les identités des deux plans sont bien DISJOINTES ────────────────────────────
        p1, _, _, s1, r1, rs1, ss1 = ({"c": {"senders": [], "receivers": []}}, {}, {}, {}, {}, {}, {})
        mxl._build_one(db_get_container(vmid), p1, {}, {}, s1, r1, rs1, ss1, "c", "1:0")
        code, senders = cn.lire(vmid, "/x-nmos/node/v1.3/senders")
        controle("★ aucun identifiant n'est partagé entre le plan 1 et le plan 2",
                 not ({x["id"] for x in (senders or [])} & set(s1)),
                 "le même flux sous deux Devices avec le même id serait invalide en IS-04")

        # ── 3. IS-05 en lecture, et le 404 de transportfile ─────────────────────────────────
        code, liste = cn.lire(vmid, "/x-nmos/connection/v1.1/single/senders")
        controle("il sert la liste IS-05 de ses senders", code == 200 and len(liste or []) == 3)
        sid = (liste or ["/"])[0].rstrip("/")
        code, actif = cn.lire(vmid, "/x-nmos/connection/v1.1/single/senders/%s/active" % sid)
        controle("son /active porte le domaine MXL et l'UUID de flux résolus",
                 code == 200 and (actif or {}).get("transport_params", [{}])[0].get("mxl_flow_id"),
                 "obtenu %s" % actif)
        code, _ = cn.lire(vmid, "/x-nmos/connection/v1.1/single/senders/%s/transportfile" % sid)
        controle("/transportfile rend 404 (BCP-007-03 : MXL n'en a pas)", code == 404,
                 "obtenu %s" % code)

        # ── 4. LE contrôle décisif : la comparaison sait dire NON ────────────────────────────
        v = cn.comparer(vmid)
        controle("orchestrateur et conteneur concordent", v.get("verdict") == "concordant",
                 json.dumps(v, ensure_ascii=False))

        doc = cn.document(db_get_container(vmid))
        perdu = doc["senders"].pop()["id"]
        agent_session().post(agent_url(ip, "/nmos"), json=doc, timeout=5,
                             headers=agent_headers(vmid))
        v = cn.comparer(vmid)
        controle("★ un document PÉRIMÉ est détecté comme divergent, et le manquant est nommé",
                 v.get("verdict") == "DIVERGENT" and perdu in (v.get("manquants") or []),
                 "une comparaison qui ne sait pas dire non est une décoration — obtenu %s"
                 % json.dumps(v, ensure_ascii=False))
        controle("et un re-push rétablit la concordance",
                 cn.pousser(vmid) and cn.comparer(vmid).get("verdict") == "concordant")

        # ── 5. Le réglage ferme bien le push ────────────────────────────────────────────────
        db_set_setting("nmos_conteneur_node", False)
        controle("réglage fermé : l'orchestrateur ne pousse plus", not cn.pousser(vmid),
                 "pousser un document à chaque déploiement est un effet de bord sur le chemin "
                 "critique : il ne doit pas se produire sans qu'on l'ait demandé")
    finally:
        db_set_setting("nmos_conteneur_node",
                       avant_reglage if avant_reglage is not None else False)
        if vmid:
            try:
                with verrou_vmid(vmid, op="banc-plan2-destroy"):
                    detruire_container(vmid)
            except Exception as e:                                   # pragma: no cover
                print("  ⚠ destruction de %s échouée : %s" % (vmid, e), file=sys.stderr)
        time.sleep(3)
        reste = {c["vmid"] for c in db_get_containers()} - avant_vmids
        controle("le parc est rendu à son état initial", not reste,
                 "conteneurs laissés derrière : %s" % sorted(reste))

    print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
    return 1 if echecs else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--go", action="store_true",
                    help="obligatoire : ce banc CRÉE et DÉTRUIT un conteneur")
    ap.add_argument("--node", type=int, default=None)
    a = ap.parse_args()
    if not a.go:
        print("Ce banc déploie un conteneur jetable et le détruit ensuite.\n"
              "Relancez avec --go si c'est bien ce que vous voulez.", file=sys.stderr)
        sys.exit(2)
    sys.exit(principal(a.node))
