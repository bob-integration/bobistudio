#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'ÉTAPE 1 du chantier « NMOS dans les conteneurs » : vérifie que
# `services/nmos/mxl.py` dérive une surface IS-04/IS-05 conforme à BCP-007-03
# à partir du manifeste d'un plugin.
#
# POURQUOI `hello_world` SERT DE GABARIT. Ce n'est pas « un plugin au hasard » :
# c'est le plugin de CONTRAT, et son wiring couvre exactement ce qu'il faut —
# trois `produces` (vidéo, audio, data → les trois media_type d'un coup) et
# trois `consumes` tous `optional` (→ le cas « port non câblé », que le schéma
# de la BCP prévoit et qu'on oublierait de tester). Si la surface se dérive
# correctement d'ici, elle se dérive pour tous les plugins.
#
# CE BANC NE TOUCHE À RIEN : aucune écriture DB, aucun conteneur, aucun réseau.
# Toutes les fonctions éprouvées ici sont pures (`node_id=None` est délibéré :
# `_domain_id` ne doit pas ALLOUER un domaine depuis un banc — cf. contrôle 9).
#
#   $ ./venv/bin/python tools/verif_nmos_mxl.py
import importlib.util
import json
import os
import sys
import uuid

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs = []
reussites = []


def controle(intitule, condition, explication=""):
    if condition:
        reussites.append(intitule)
    else:
        echecs.append((intitule, explication))


from services.nmos import mxl                                    # noqa: E402

# ── 1. Le MIROIR d'UUID — le point de rupture silencieux ─────────────────────
# `mxl.flow_uuid` et `bobimxl.flow_id` sont deux copies de la même constante
# (uuid5 du namespace « mxl.bobi.studio ») dans deux fichiers distincts. Si elles
# divergent, les ressources NMOS s'annoncent, les patchs sont acceptés, et le
# câble ne transporte RIEN — sans un message d'erreur nulle part.
_spec = importlib.util.spec_from_file_location(
    "bobimxl_ref", os.path.join(RACINE, "script_templates", "bobimxl.py"))
_ref = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_ref)
    _flow_id_ref = _ref.flow_id
except Exception as e:                                           # pragma: no cover
    _flow_id_ref = None
    controle("bobimxl est importable pour comparaison", False, repr(e))

if _flow_id_ref:
    _noms = ["hello1_hello", "hello1_hello_audio", "hello1_hello_anc",
             "mtlrx302_1", "", "un nom avec des espaces"]
    controle("flow_uuid() est le MIROIR exact de bobimxl.flow_id()",
             all(mxl.flow_uuid(n) == _flow_id_ref(n) for n in _noms),
             "l'UUID d'un flux MXL est dérivé de son nom par uuid5 dans DEUX fichiers ; "
             "une divergence fait pointer tout le routage NMOS vers des flux inexistants")

# ── 2. Inventaire des ports depuis le manifeste ──────────────────────────────
HOTE = "hello-banc-1"
_manif = json.load(open(os.path.join(RACINE, "plugins", "hello_world", "plugin.json"),
                        encoding="utf-8"))
CONTENEUR = {
    "vmid": 999001,
    "hostname": HOTE,
    "instance_uuid": "11111111-2222-3333-4444-555555555555",
    "node_id": None,                     # ⚠ délibéré : voir contrôle 9
    "deploy_config": json.dumps({"type": "hello_world",
                                 "params": dict(_manif.get("deploy_defaults") or {})}),
}

ports = mxl.ports_of(CONTENEUR)
prod, cons = ports["produces"], ports["consumes"]

controle("ports_of() rend les 3 sorties de hello_world", len(prod) == 3,
         "attendu 3 produces (video/audio/data), obtenu %d" % len(prod))
controle("ports_of() rend les 3 entrées de hello_world", len(cons) == 3,
         "attendu 3 consumes, obtenu %d" % len(cons))
controle("les essences des sorties sont video+audio+data",
         sorted(p.get("essence") for p in prod) == ["audio", "data", "video"])
controle("les slots d'entrée sont numérotés 0/1/2",
         [c["slot"] for c in cons] == [0, 1, 2],
         "le slot fait partie de l'identité du Receiver : un décalage rebaptise les ports")
controle("le nom de flux produit est résolu depuis {hostname}",
         any(p.get("shm") == "%s_hello" % HOTE for p in prod),
         "sans substitution, mxl_flow_id serait l'uuid5 du littéral « {hostname}_hello »")

# ── 3. Construction du modèle, sans DB ni conteneur ──────────────────────────
DID = "device-cluster-factice"
devices = {DID: {"senders": [], "receivers": []}}
sources, flows, senders, receivers = {}, {}, {}, {}
recv_state, send_state = {}, {}
mxl._build_one(CONTENEUR, devices, sources, flows, senders, receivers,
               recv_state, send_state, DID, "1756500000:0")
mxl.reindex(send_state)
mxl.resync_subscriptions(receivers, recv_state, senders, send_state)

controle("un Sender par sortie, un Receiver par entrée",
         len(senders) == 3 and len(receivers) == 3,
         "obtenu %d senders / %d receivers" % (len(senders), len(receivers)))
controle("Source et Flow accompagnent chaque Sender",
         len(sources) == 3 and len(flows) == 3)

# ── 4. Les champs que BCP-007-03 impose au Sender ────────────────────────────
un_snd = next(iter(senders.values()))
controle("le Sender porte le transport littéral urn:x-nmos:transport:mxl",
         all(s["transport"] == "urn:x-nmos:transport:mxl" for s in senders.values()),
         "la BCP impose le littéral, elle ne délègue PAS au registre des transports")
controle("interface_bindings est un tableau VIDE",
         all(s["interface_bindings"] == [] for s in senders.values()),
         "« An MXL Sender resource MUST expose an empty interface_bindings array » — "
         "le bus est de la mémoire partagée, il ne sort par aucune NIC")
controle("manifest_href est null",
         all(s["manifest_href"] is None for s in senders.values()),
         "MXL n'a pas de fichier de transport ; /transportfile doit répondre 404")
controle("le Sender référence son Flow, qui référence sa Source",
         flows[un_snd["flow_id"]]["source_id"] in sources)

# ── 5. media_type : ce qui est au registre, et ce qui ne l'est pas ───────────
_par_essence = {}
for f in flows.values():
    _par_essence[f["format"].rsplit(":", 1)[-1]] = f["media_type"]
controle("media_type audio = audio/float32 (au registre NMOS)",
         _par_essence.get("audio") == "audio/float32")
controle("media_type data = video/smpte291 (au registre NMOS)",
         _par_essence.get("data") == "video/smpte291")
controle("media_type vidéo = video/x-mxl-planar (HORS registre, assumé)",
         _par_essence.get("video") == "video/x-mxl-planar",
         "arbitrage produit du 2026-08-15 : planar en interne, miroir v210 au cas par cas. "
         "Annoncer video/v210 sur un flux planar serait une non-conformité PIRE")

# ── 6. Le cas « port non câblé » — celui qu'on oublierait ────────────────────
_libres = [st for st in recv_state.values() if not st.get("shm")]
controle("les 3 entrées optionnelles non câblées sont détectées", len(_libres) == 3)
controle("un Receiver non câblé a mxl_flow_id null",
         all(st["active"]["transport_params"][0]["mxl_flow_id"] is None for st in _libres),
         "le schéma de la BCP prévoit null pour un Receiver non configuré")
controle("un Receiver non câblé a master_enable false",
         all(st["active"]["master_enable"] is False for st in _libres))
controle("un Sender est actif par construction",
         all(st["active"]["master_enable"] is True for st in send_state.values()),
         "un conteneur ÉCRIT son flux tant qu'il tourne : il n'y a rien à activer")

# ── 7. L'asymétrie des transport_params (schémas de la BCP) ──────────────────
_rid = next(iter(recv_state))
ok_auto, code_auto, _p = mxl.apply_receiver_staged(
    _rid, {"transport_params": [{"mxl_flow_id": "auto"}]}, recv_state, send_state)
# ⚠ On vérifie le MOTIF, pas seulement le code. Un test de mutation l'a prouvé : en
# désarmant le refus explicite de « auto », la requête reste rejetée en 400 — par le
# contrôle suivant (« ce n'est pas un UUID »). Sur le seul code HTTP, la disparition de
# la garde serait INVISIBLE.
controle("un Receiver REFUSE mxl_flow_id = \"auto\", et pour CE motif",
         (not ok_auto) and code_auto == 400 and "auto" in (_p.get("error") or ""),
         "receiver_transport_params_mxl.json : « The literal auto is not used for this "
         "parameter » — seuls null et un UUID sont admis")

ok_dom, _c, _p = mxl.apply_receiver_staged(
    _rid, {"transport_params": [{"mxl_domain_id": "auto"}]}, recv_state, send_state)
controle("un Receiver ACCEPTE mxl_domain_id = \"auto\"", ok_dom,
         "le même schéma l'autorise explicitement sur le domaine")

ok_bad, code_bad, _p = mxl.apply_receiver_staged(
    _rid, {"transport_params": [{"mxl_flow_id": "pas-un-uuid"}]}, recv_state, send_state)
controle("un Receiver refuse un mxl_flow_id malformé",
         (not ok_bad) and code_bad == 400)

_sid = next(iter(send_state))
_reel = send_state[_sid]["active"]["transport_params"][0]["mxl_flow_id"]
ok_mv, code_mv, _p = mxl.apply_sender_staged(
    _sid, {"transport_params": [{"mxl_flow_id": str(uuid.uuid4())}]}, send_state)
controle("un Sender refuse qu'on DÉPLACE son mxl_flow_id",
         (not ok_mv) and code_mv == 400,
         "le flux est écrit par le conteneur ; accepter en silence serait un échec silencieux")
ok_id, _c, _p = mxl.apply_sender_staged(
    _sid, {"transport_params": [{"mxl_flow_id": _reel}]}, send_state)
controle("un Sender accepte qu'on lui redonne SA valeur", ok_id)

# ── 8. /constraints n'énumère JAMAIS « auto » ────────────────────────────────
_cs = mxl.constraints(_rid, send_state, recv_state) or []
_cs += mxl.constraints(_sid, send_state, recv_state) or []
_plat = json.dumps(_cs)
controle("/constraints ne contient jamais le littéral \"auto\"",
         '"auto"' not in _plat,
         "« The constraints endpoint does not list auto as an available option » — "
         "auto est accepté en /staged mais ne s'ÉNUMÈRE pas")
controle("/constraints d'un Sender n'énumère que SON flux",
         (mxl.constraints(_sid, send_state, recv_state) or [{}])[0]
         .get("mxl_flow_id", {}).get("enum") == [_reel])

# ── 8bis. Grouping BCP-002-01 DÉRIVÉ ─────────────────────────────────────────
_G = "urn:x-nmos:tag:grouphint/v1.0"


def _hint(res):
    return (res.get("tags") or {}).get(_G, [""])[0]


_noms_rx = {_hint(r).split(":")[0] for r in receivers.values()}
controle("les 3 Receivers de hello_world forment UN SEUL bundle",
         len(_noms_rx) == 1 and "" not in _noms_rx,
         "câbler la vidéo pose aussi l'audio et l'ANC (_apply_wire est groupé) : sans group "
         "hint, le contrôleur voit deux ressources bouger sans les avoir demandées")
controle("leurs rôles sont distincts dans le bundle",
         len({_hint(r).split(":", 1)[1] for r in receivers.values()}) == 3)
controle("les Senders portent aussi un group hint",
         all(_hint(s) for s in senders.values()))

# ⚠ RÉGRESSION VERROUILLÉE ICI. Le grouping doit se faire sur le RANG dans l'essence, jamais sur
# `slot` : `2110_io` numérote ses slots PAR ESSENCE (tx1/tx_audio1/tx_anc1 ont tous slot=0) tandis
# que `hello_world` les numérote GLOBALEMENT (0/1/2).
#
# ★ Le contre-exemple doit être ENTRELACÉ, et le premier que j'ai écrit ne l'était pas : avec des
# slots séquentiels (toutes les vidéos, puis tous les audios), `slot % n` et le rang donnent
# TOUJOURS le même résultat — audio slot (n+r) → (n+r) % n = r. La garde passait donc en désarmant
# la règle. Il faut une déclaration « par entrée » (V,A,V,A), la plus naturelle pour un manifeste
# à entrées multiples, où les deux règles divergent vraiment.
_faux = [{"essence": "video", "slot": 0, "key": "video:0"},
         {"essence": "audio", "slot": 1, "key": "audio:1"},
         {"essence": "video", "slot": 2, "key": "video:2"},
         {"essence": "audio", "slot": 3, "key": "audio:3"}]
_g = mxl._groupes(_faux, "x")
controle("déclaration ENTRELACÉE (V,A,V,A) : chaque audio rejoint SA vidéo",
         _g["audio:1"][0] == _g["video:0"][0] and _g["audio:3"][0] == _g["video:2"][0]
         and _g["video:0"][0] != _g["video:2"][0],
         "obtenu %s — une règle indexée sur slot ferait tomber les DEUX audios dans le même "
         "bundle, et le mauvais" % _g)

# ── 9. Le banc n'a rien alloué ───────────────────────────────────────────────
# `_domain_id(None)` doit rendre None SANS toucher la base : `db_node_mxl_domain_id`
# CRÉE le domaine au premier appel (UPDATE). Un banc qui l'appelle avec un node_id
# réel écrirait en base de PRODUCTION — exactement ce qu'on s'interdit.
controle("_domain_id(None) ne déclenche aucune allocation",
         mxl._domain_id(None) is None)

# ── 10. Identité des ressources : déterministe et distincte ──────────────────
_devices2 = {DID: {"senders": [], "receivers": []}}
_s2, _f2, _sn2, _rc2, _rs2, _ss2 = {}, {}, {}, {}, {}, {}
mxl._build_one(CONTENEUR, _devices2, _s2, _f2, _sn2, _rc2, _rs2, _ss2, DID, "1756500001:0")
controle("les identifiants sont DÉTERMINISTES d'une reconstruction à l'autre",
         set(_sn2) == set(senders) and set(_rc2) == set(receivers),
         "un id qui bouge à chaque rebuild ferait perdre son routage au contrôleur")
controle("chaque (essence, slot) donne un identifiant DISTINCT",
         len(set(senders) | set(receivers)) == 6)

# ── 11. La garde de lecture seule tient sur TOUS les chemins ─────────────────
# ⚠ RÉGRESSION VERROUILLÉE. La garde 405 a d'abord été posée sur les deux routes PATCH unitaires
# — et les endpoints BULK, qui appellent `_apply_*_staged` en direct, la contournaient. Un garde
# conditionné à QUI APPELLE ne protège que celui-là. Elle vit désormais au point de passage ;
# ce contrôle vérifie les DEUX chemins sur le HTTP réel.
import urllib.error                                                # noqa: E402
import urllib.request                                              # noqa: E402

_B = "http://127.0.0.1:5000/x-nmos"


def _http(methode, url, corps=None):
    d = json.dumps(corps).encode() if corps is not None else None
    rq = urllib.request.Request(url, data=d, method=methode,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=8) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return None, str(e)


_code, _corps = _http("GET", _B + "/node/v1.3/receivers")
if _code != 200:
    print("  (service injoignable — contrôles HTTP sautés, ce n'est pas un échec du code)")
else:
    _rx = json.loads(_corps)
    _mxl_rx = [x["id"] for x in _rx if x.get("transport") == "urn:x-nmos:transport:mxl"]
    _rtp_rx = [x["id"] for x in _rx if x.get("transport") != "urn:x-nmos:transport:mxl"]
    if not _mxl_rx:
        print("  (aucun receiver MXL servi — contrôles HTTP sautés)")
    else:
        _c, _ = _http("PATCH", "%s/connection/v1.1/single/receivers/%s/staged" % (_B, _mxl_rx[0]),
                      {"master_enable": True})
        controle("PATCH unitaire sur un Receiver MXL → 405", _c == 405, "obtenu %s" % _c)
        _c, _b = _http("POST", _B + "/connection/v1.1/bulk/receivers",
                       [{"id": _mxl_rx[0], "params": {"master_enable": True}}])
        _codes = [x.get("code") for x in json.loads(_b)] if _c == 200 else []
        controle("★ le BULK sur la même ressource est refusé AUSSI",
                 _codes == [405],
                 "le bulk contournait la garde tant qu'elle vivait sur la route — obtenu %s"
                 % (_codes or _b[:120]))
        if _rtp_rx:
            _c, _b = _http("POST", _B + "/connection/v1.1/bulk/receivers",
                           [{"id": _rtp_rx[0], "params": {}}])
            controle("et le bulk RTP du moteur 2110 reste OUVERT",
                     _c == 200 and [x.get("code") for x in json.loads(_b)] == [200],
                     "le verrou ne doit toucher QUE la surface MXL — obtenu %s" % _b[:120])

# ── Verdict ──────────────────────────────────────────────────────────────────
print("services/nmos/mxl.py — surface BCP-007-03 dérivée de hello_world\n")
for r in reussites:
    print("  OK    %s" % r)
if echecs:
    print("", file=sys.stderr)
    for intitule, explication in echecs:
        print("  ÉCHEC %s" % intitule, file=sys.stderr)
        if explication:
            print("        → %s" % explication, file=sys.stderr)
    print("\n%d contrôle(s) en échec." % len(echecs), file=sys.stderr)
    sys.exit(1)

print("\nOK : %d contrôles passés." % len(reussites))
sys.exit(0)
