#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc VIVANT de l'étape 2 du chantier « NMOS dans les conteneurs » : déploie deux
# `hello_world` jetables, les câble, et vérifie que la surface BCP-007-03 dérivée
# par `services/nmos/mxl.py` dit la VÉRITÉ sur un parc réel — dans les deux sens.
#
# CE QU'IL PROUVE, ET QUE LE BANC PUR NE PEUT PAS PROUVER
# -------------------------------------------------------
# `tools/verif_nmos_mxl.py` éprouve la DÉRIVATION (fonctions pures, aucun conteneur).
# Il ne peut rien dire du seul risque qui compte vraiment : **deux vérités de routage**.
# Ici on décâble par le chemin de la page Câbles, SANS toucher à NMOS, et on exige que
# `/active` retombe à `null` tout seul. Si un jour la surface NMOS se met à mémoriser
# son propre état au lieu de constater celui du graphe, c'est CE contrôle qui tombe —
# et lui seul.
#
# ⚠ CE BANC MUTE. Il crée et détruit des conteneurs. Garde-fous :
#   - il REFUSE de tourner sans `--go` ;
#   - il ne touche QUE les deux conteneurs qu'il crée, jamais un existant ;
#   - il refuse de démarrer si ses hostnames sont déjà pris ;
#   - il détruit ses conteneurs dans un `finally` — y compris si un contrôle échoue ;
#   - il compare le parc AVANT/APRÈS et le signale s'il a laissé quoi que ce soit.
#
#   $ ./venv/bin/python tools/banc_nmos_mxl_live.py --go [--node <id>]
import argparse
import json
import os
import sys
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

HOTE_A = "nmosbanc-a"
HOTE_B = "nmosbanc-b"

echecs = []
reussites = []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(
        intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def _params(hostname):
    manif = json.load(open(os.path.join(RACINE, "plugins", "hello_world", "plugin.json"),
                           encoding="utf-8"))
    p = dict(manif.get("deploy_defaults") or {})
    p["hostname"] = hostname
    return p


# ⚠ ÉTAT PERSISTANT ENTRE LES RECONSTRUCTIONS — et ce n'est pas un détail de banc.
# `rebuild_model` garde `_recv_state`/`_send_state` en globals de module : ils SURVIVENT à
# chaque reconstruction. Si le banc repartait de dictionnaires neufs à chaque `_vue()`, le
# contrôle décisif (« /active retombe à null tout seul ») passerait TRIVIALEMENT — il n'y
# aurait rien à mémoriser. On reproduit donc la persistance réelle, sinon on teste un modèle
# que le produit n'exécute jamais.
_RS, _SS = {}, {}


def _vue():
    """Modèle MXL reconstruit depuis la DB — aucun effet de bord, aucune écriture."""
    from services.nmos import mxl
    did = "banc-cluster"
    dev = {did: {"senders": [], "receivers": []}}
    src, flw, snd, rcv = {}, {}, {}, {}
    mxl.build(dev, src, flw, snd, rcv, _RS, _SS, did, "%d:0" % int(time.time()))
    mxl.reindex(_SS)
    mxl.resync_subscriptions(rcv, _RS, snd, _SS)
    # Purge des ressources disparues, comme le fait `rebuild_model` sous verrou.
    for orphelin in [k for k in _RS if k not in rcv]:
        del _RS[orphelin]
    for orphelin in [k for k in _SS if k not in snd]:
        del _SS[orphelin]
    return {"senders": snd, "receivers": rcv, "send_state": _SS, "recv_state": _RS}


def _rx_de(vue, vmid):
    """{essence: état} des Receivers d'un conteneur."""
    return {st["essence"]: st for st in vue["recv_state"].values() if st["vmid"] == vmid}


def _tx_de(vue, vmid):
    return {st["essence"]: (sid, st) for sid, st in vue["send_state"].items()
            if st["vmid"] == vmid}


def principal(node_id):
    from app import docker_compute, node_driver
    from app.containers import detruire_container
    from app.database import db_get_containers, db_get_nodes, db_get_container
    from app.deploy import deployer_script
    from app.routes.cabling import _apply_wire, _apply_unwire
    from app.vmlocks import verrou_vmid
    from services.nmos import mxl

    avant = {c["vmid"] for c in db_get_containers()}
    pris = {c["hostname"] for c in db_get_containers()}
    if HOTE_A in pris or HOTE_B in pris:
        print("REFUS : %s ou %s existe déjà — ce banc ne réutilise jamais un conteneur qu'il "
              "n'a pas créé." % (HOTE_A, HOTE_B), file=sys.stderr)
        return 2

    # ⚠ MÊME NŒUD OBLIGATOIRE : le bus MXL est LOCAL à un nœud. Deux hello_world sur des
    # nœuds différents ne pourraient pas se câbler (sauf réplication RDMA), et le banc
    # échouerait pour une raison qui n'a rien à voir avec ce qu'il teste.
    noeuds = [n for n in db_get_nodes()
              if n["compute_image"] and n["docker_network"] and n["status"] == "up"]
    if node_id:
        noeuds = [n for n in noeuds if n["id"] == node_id]
    if not noeuds:
        print("REFUS : aucun nœud compute utilisable.", file=sys.stderr)
        return 2
    noeud = noeuds[0]
    print("nœud de banc : %s (id=%s, domaine MXL %s)\n"
          % (noeud["name"], noeud["id"], (noeud["mxl_domain_id"] or "—")[:8]))

    crees = []
    try:
        # ── Déploiement ──────────────────────────────────────────────────────
        for hote in (HOTE_A, HOTE_B):
            v = docker_compute.creer_container_compute(noeud["id"], "hello_world", hostname=hote)
            if not v:
                print("REFUS : création de %s impossible." % hote, file=sys.stderr)
                return 2
            crees.append(v)
            deployer_script(v, "hello_world", _params(hote))
        vmid_a, vmid_b = crees
        print("déployés : %s=%s  %s=%s" % (HOTE_A, vmid_a, HOTE_B, vmid_b))

        # Les flux MXL n'existent qu'une fois le script démarré : on attend l'OBSERVABLE
        # (le dossier de flux), jamais une durée fixe qui serait fausse sur un nœud chargé.
        cible = mxl.flow_uuid("%s_hello" % HOTE_A)
        vus = False
        for _ in range(30):
            time.sleep(2)
            r = node_driver.host_exec(noeud, "ls -1 /dev/shm/mxl 2>/dev/null", timeout=15)
            out = r[1] if isinstance(r, tuple) and len(r) > 1 else str(r)
            if cible in (out or ""):
                vus = True
                break
        controle("les deux conteneurs publient leurs flux sur le bus", vus,
                 "sans flux vivant, tout ce qui suit testerait du vide")
        if not vus:
            return 1

        # ── 1. Le modèle dérivé décrit le parc réel ──────────────────────────
        vue = _vue()
        tx_a, rx_b = _tx_de(vue, vmid_a), _rx_de(vue, vmid_b)
        controle("A expose 3 Senders (video/audio/data)",
                 set(tx_a) == {"video", "audio", "data"})
        controle("B expose 3 Receivers (video/audio/data)",
                 set(rx_b) == {"video", "audio", "data"})
        controle("les Senders portent le domaine MXL du nœud",
                 all(s[1]["active"]["transport_params"][0]["mxl_domain_id"]
                     == noeud["mxl_domain_id"] for s in tx_a.values()),
                 "le domaine est l'identité INDÉPENDANTE DU CHEMIN DE MONTAGE exigée par la BCP")
        controle("les Receivers non câblés sont à null / master_enable false",
                 all((not st["shm"]) and st["active"]["master_enable"] is False
                     and st["active"]["transport_params"][0]["mxl_flow_id"] is None
                     for st in rx_b.values()))

        # ── 1bis. Localité : /constraints ne doit annoncer QUE le nœud de la ressource ──
        # Le bus MXL est LOCAL au nœud. Énumérer les domaines des autres nœuds du cluster, c'est
        # publier des routes impossibles — le contrôleur les propose, et l'échec n'arrive qu'à
        # l'activation. Vu en vrai sur le HTTP le 2026-08-31 : les 4 domaines étaient annoncés.
        _c = mxl.constraints(
            [r for r, st in vue["recv_state"].items() if st["vmid"] == vmid_b][0],
            vue["send_state"], vue["recv_state"]) or [{}]
        controle("/constraints n'annonce QUE le domaine du nœud de la ressource",
                 _c[0].get("mxl_domain_id", {}).get("enum") == [noeud["mxl_domain_id"]],
                 "obtenu %s" % _c[0].get("mxl_domain_id"))
        controle("/constraints ne liste que des flux du MÊME nœud",
                 all(f in [s["active"]["transport_params"][0]["mxl_flow_id"]
                           for s in vue["send_state"].values() if s.get("mxl")]
                     for f in (_c[0].get("mxl_flow_id", {}).get("enum") or [])))

        # ── 2. Câblage par le chemin de la page Câbles ───────────────────────
        shm_a = "%s_hello" % HOTE_A
        with verrou_vmid(vmid_b, op="banc-nmos-wire"):
            ok, _s, _p = _apply_wire(vmid_a, vmid_b, shm_a, "video")
        controle("le câblage A→B aboutit", ok)
        time.sleep(4)

        vue = _vue()
        rx_b, tx_a = _rx_de(vue, vmid_b), _tx_de(vue, vmid_a)
        controle("le Receiver vidéo de B pointe le flux de A",
                 rx_b["video"]["active"]["transport_params"][0]["mxl_flow_id"]
                 == mxl.flow_uuid(shm_a))
        controle("son sender_id résout vers le Sender vidéo de A",
                 rx_b["video"]["active"]["sender_id"] == tx_a["video"][0])
        controle("le Sender de A annonce l'abonnement en retour",
                 vue["senders"][tx_a["video"][0]]["subscription"]["active"] is True)
        # Le câblage est GROUPÉ : poser la vidéo pose aussi l'audio et l'ANC. Un contrôleur
        # tiers verra donc trois Receivers bouger pour un seul PATCH — c'est ce que le
        # grouping BCP-002-01 sert à exprimer, et nos ressources MXL n'en portent pas encore.
        controle("le câblage groupé entraîne AUSSI l'audio et l'ANC",
                 all(rx_b[e]["shm"] for e in ("audio", "data")),
                 "si ce contrôle tombe, c'est le comportement de _apply_wire qui a changé")

        # ── 3. LE contrôle décisif : décâbler HORS de NMOS ───────────────────
        with verrou_vmid(vmid_b, op="banc-nmos-unwire"):
            ok, _s, _p = _apply_unwire(vmid_b, shm_a, "video")
        controle("le décâblage par la page Câbles aboutit", ok)
        time.sleep(4)
        rx_b = _rx_de(_vue(), vmid_b)
        controle("★ /active retombe à null SANS qu'on ait touché NMOS",
                 all((not st["shm"]) and st["active"]["master_enable"] is False
                     and st["active"]["transport_params"][0]["mxl_flow_id"] is None
                     and st["active"]["sender_id"] is None for st in rx_b.values()),
                 "c'est LE contrôle qui interdit une seconde vérité de routage : la surface "
                 "NMOS doit CONSTATER le graphe, jamais mémoriser un état parallèle")

        # ── 4. Sens inverse : un PATCH IS-05 pose vraiment le câble ──────────
        vue = _vue()
        rid_id = [r for r, st in vue["recv_state"].items()
                  if st["vmid"] == vmid_b and st["essence"] == "video"][0]
        sid_a, st_a = _tx_de(vue, vmid_a)["video"]
        flux = st_a["active"]["transport_params"][0]["mxl_flow_id"]
        ok, code, payload = mxl.apply_receiver_staged(
            rid_id, {"master_enable": True,
                     "transport_params": [{"mxl_domain_id": "auto", "mxl_flow_id": flux}],
                     "activation": {"mode": "activate_immediate"}},
            vue["recv_state"], vue["send_state"])
        controle("un PATCH IS-05 activate_immediate est accepté", ok and code == 200,
                 json.dumps(payload, ensure_ascii=False)[:200])
        time.sleep(4)
        c = db_get_container(vmid_b)
        dc = json.loads(c["deploy_config"]) if isinstance(c["deploy_config"], str) else c["deploy_config"]
        controle("le PATCH a réellement posé le câble dans les params du conteneur",
                 ((dc or {}).get("params") or {}).get("input_shm") == shm_a,
                 "un 200 sans câble posé serait l'échec silencieux type")
        tp = (payload.get("transport_params") or [{}])[0]
        controle("/active porte le domaine RÉSOLU, pas le littéral « auto »",
                 tp.get("mxl_domain_id") == noeud["mxl_domain_id"],
                 "« auto » est une instruction de résolution, valable en /staged ; /active "
                 "doit montrer ce qui s'applique")
        controle("/active porte le sender_id résolu",
                 payload.get("sender_id") == sid_a,
                 "sinon le contrôleur affiche un abonnement actif « connecté à rien »")

    finally:
        # ── Remise en état, quoi qu'il arrive ────────────────────────────────
        for v in reversed(crees):
            try:
                with verrou_vmid(v, op="banc-nmos-destroy"):
                    detruire_container(v)
            except Exception as e:                                   # pragma: no cover
                print("  ⚠ destruction de %s échouée : %s" % (v, e), file=sys.stderr)
        time.sleep(3)
        reste = {c["vmid"] for c in db_get_containers()} - avant
        controle("le parc est rendu à son état initial", not reste,
                 "conteneurs laissés derrière : %s" % sorted(reste))

    print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
    return 1 if echecs else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--go", action="store_true",
                    help="obligatoire : ce banc CRÉE et DÉTRUIT des conteneurs")
    ap.add_argument("--node", type=int, default=None, help="id du nœud de banc")
    a = ap.parse_args()
    if not a.go:
        print("Ce banc déploie deux conteneurs jetables et les détruit ensuite.\n"
              "Relancez avec --go si c'est bien ce que vous voulez.", file=sys.stderr)
        sys.exit(2)
    sys.exit(principal(a.node))
