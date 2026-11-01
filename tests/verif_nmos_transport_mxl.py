#!/usr/bin/env python3
"""Vérifie qu'un abonnement RX de transport MXL n'est PAS poussé vers `/nmos/subscribe`, et que
les abonnements persistés d'un conteneur disparu sont purgés.

Contrat (mesuré le 2026-10-24, cf. `_transport_is_mxl`) : `/nmos/subscribe` est le contrat de
l'agent du moteur 2110 et ne sait traiter QUE du SDP. Un receiver dont le transport est MXL n'a
pas de SDP — ce n'est pas une donnée manquante, c'est un autre transport, câblé par la page
Câbles. Les pousser produisait 29 alertes `error` par démarrage (« le moteur tourne SANS ces
entrées ») sur des entrées qui fonctionnaient.

Ce que la garde verrouille :
  (1) la détection du transport, y compris sur la 2ᵉ jambe d'un 2022-7 ;
  (2) l'ÉCART : un MXL n'est pas envoyé à l'agent, et ne lève AUCUNE alerte ;
  (3) un RTP reste envoyé (le filtre ne doit pas avaler le cas qui marche) ;
  (4) les ratios journalisés portent sur les abonnements réellement poussés ;
  (5) la purge des orphelins, et son ÉCHEC FERMÉ (containers vide ou illisible → rien n'est purgé).

Aucun réseau, aucun agent, aucune base : doublures posées sur les modules.

    ./venv/bin/python tests/verif_nmos_transport_mxl.py
"""
import json
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

from app import database as D                           # noqa: E402
from services import nmos                               # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def mxl(flow="f1"):
    return {"master_enable": True, "transport_params": [{"mxl_domain_id": "d", "mxl_flow_id": flow}]}


def rtp(sdp="v=0\r\n"):
    return {"master_enable": True, "transport_params": [{"destination_ip": "239.0.0.1"}],
            "transport_file": {"type": "application/sdp", "data": sdp}}


# ── (1) détection du transport ───────────────────────────────────────────────────────
ok(nmos._transport_is_mxl(mxl()), "MXL reconnu (domain + flow)")
ok(nmos._transport_is_mxl({"transport_params": [{"mxl_flow_id": "x"}]}), "MXL reconnu (flow seul)")
ok(nmos._transport_is_mxl({"transport_params": [{"mxl_domain_id": "d"}]}), "MXL reconnu (domain seul)")
ok(not nmos._transport_is_mxl(rtp()), "RTP n'est PAS pris pour du MXL")
ok(not nmos._transport_is_mxl({}), "actif vide : pas de MXL")
ok(not nmos._transport_is_mxl({"transport_params": []}), "params vides : pas de MXL")
# 2022-7 : la 2ᵉ jambe suffit — sinon un abonnement mixte passerait au travers.
ok(nmos._transport_is_mxl({"transport_params": [{"destination_ip": "1"}, {"mxl_flow_id": "y"}]}),
   "MXL reconnu sur la 2ᵉ jambe (2022-7)")
ok(not nmos._transport_is_mxl({"transport_params": [{"mxl_flow": "pas_la_bonne_cle"}]}),
   "clé voisine (`mxl_flow`) NON prise pour du MXL")


# ── (2)(3)(4) ce que `repush_subscriptions` pousse réellement ────────────────────────
envoyes, alertes = [], []


def lancer(entries):
    """Appelle repush_subscriptions avec l'agent, la DB et la readiness doublés. Rend (n, envoyés)."""
    del envoyes[:], alertes[:]
    from app import deploy, addressing
    sauve = (nmos.subscriptions_actives, nmos._notify_agent, nmos._extract_mcast_info,
             addressing.get_container_ip, deploy.attendre_controleur_pret, D.db_add_alert)
    nmos.subscriptions_actives = lambda vmid: entries
    nmos._extract_mcast_info = lambda active, smpte_2022_7=False: None

    def _notify(vmid, idx, ess, enable, sdp, mcast):
        envoyes.append((idx, ess, bool(sdp)))
        return bool(sdp)                     # l'agent n'écrit que si un SDP est présent
    nmos._notify_agent = _notify
    addressing.get_container_ip = lambda vmid: "10.0.0.1"
    deploy.attendre_controleur_pret = lambda ip, vmid=None: True
    D.db_add_alert = lambda *a, **k: alertes.append((a, k))
    try:
        n = nmos.repush_subscriptions(7)
    finally:
        (nmos.subscriptions_actives, nmos._notify_agent, nmos._extract_mcast_info,
         addressing.get_container_ip, deploy.attendre_controleur_pret, D.db_add_alert) = sauve
    return n, list(envoyes), list(alertes)


def entree(idx, active, essence="video"):
    return ("rid%d" % idx, {"vmid": 7, "recv_idx": idx, "essence": essence, "active": active})


# 2 MXL + 1 RTP : seul le RTP part, et AUCUNE alerte n'est levée.
n, env, alt = lancer([entree(0, mxl()), entree(1, mxl(), "audio"), entree(2, rtp())])
ok(len(env) == 1 and env[0][0] == 2, "MXL non envoyés à l'agent, RTP envoyé", str(env))
ok(n == 1, "compte des livraisons = 1 (le RTP)", str(n))
ok(alt == [], "aucune alerte pour des MXL écartés", str(alt))

# Que des MXL : rien n'est envoyé, rien n'est alerté — c'est le cas des 29 du 24/10.
n, env, alt = lancer([entree(0, mxl()), entree(1, mxl(), "data")])
ok(env == [] and n == 0, "que des MXL : aucun envoi", str(env))
ok(alt == [], "que des MXL : aucune alerte", str(alt))

# Le filtre ne doit pas masquer la vraie panne : un RTP SANS SDP alerte toujours.
sans_sdp = {"master_enable": True, "transport_params": [{"destination_ip": "239.0.0.1"}]}
n, env, alt = lancer([entree(0, mxl()), entree(1, sans_sdp)])
ok(len(env) == 1 and env[0][2] is False, "RTP sans SDP : tenté puis compté en échec", str(env))
ok(len(alt) == 1, "RTP sans SDP : l'alerte est TOUJOURS levée", str(alt))
# (4) le dénominateur ignore les MXL écartés : 1/1, pas 1/2.
ok("1/1" in str(alt[0][0][0]), "ratio sur les abonnements POUSSÉS (1/1, pas 1/2)", str(alt[0][0][0]))


# ── (5) purge des orphelins, et échec fermé ──────────────────────────────────────────
def purger(subs, vivants, casse=False):
    """Appelle _purge_orphan_subscriptions avec containers doublé. Rend (restants, écrit)."""
    ecrit = {}
    sauve = (D.db_get_containers, D.db_set_setting)

    def _get():
        if casse:
            raise RuntimeError("base illisible")
        return [{"vmid": v} for v in vivants]
    D.db_get_containers = _get
    D.db_set_setting = lambda k, v: ecrit.update({k: v})
    try:
        return nmos._purge_orphan_subscriptions(dict(subs)), ecrit
    finally:
        (D.db_get_containers, D.db_set_setting) = sauve


SUBS = {"a": {"vmid": 619, "active": mxl()}, "b": {"vmid": 1105, "active": mxl()},
        "c": {"vmid": 1187, "active": mxl()}}

restants, ecrit = purger(SUBS, [619, 1187])
ok(set(restants) == {"a", "c"}, "orphelin (1105) purgé, les vivants gardés", str(sorted(restants)))
ok("nmos_subscriptions" in ecrit, "la purge est PERSISTÉE (réglage réécrit)", str(list(ecrit)))
ok(set(json.loads(ecrit["nmos_subscriptions"])) == {"a", "c"},
   "le réglage réécrit ne contient plus l'orphelin", ecrit.get("nmos_subscriptions", "")[:80])

restants, ecrit = purger(SUBS, [619, 1105, 1187])
ok(set(restants) == {"a", "b", "c"} and not ecrit, "aucun orphelin : rien n'est réécrit", str(list(ecrit)))

# ÉCHEC FERMÉ — les deux formes. Une base vide ou illisible ne doit RIEN effacer.
restants, ecrit = purger(SUBS, [])
ok(set(restants) == {"a", "b", "c"} and not ecrit,
   "containers VIDE : rien n'est purgé (échec fermé)", str(sorted(restants)))
restants, ecrit = purger(SUBS, [619], casse=True)
ok(set(restants) == {"a", "b", "c"} and not ecrit,
   "containers ILLISIBLE : rien n'est purgé (échec fermé)", str(sorted(restants)))

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
