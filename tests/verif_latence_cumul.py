#!/usr/bin/env python3
"""Vérifie la SÉMANTIQUE du cumul de latence de la page Câbles (cf. docs/reference/LATENCE_CHAINE.md).

DÉTERMINISTE : on INJECTE des latences connues dans les caches de `app.metrics`, puis on contrôle
l'arithmétique exacte du cumul rendu par `/api/home/summary`. Aucune E/S réseau, aucun conteneur
interrogé, aucune écriture. ⚠ On ne démarre PAS `surveillance()` : cette boucle REDÉMARRE des
conteneurs — un banc n'a rien à faire là-dedans (cf. mémoire `no-mutating-tests-live-containers`).

Les deux défauts verrouillés ici, constatés le 2026-08-18 :

1. COLLISION DE VMID RX/TX. Les deux moitiés d'un moteur `split_io` sont deux nœuds de topologie
   de MÊME vmid. Le cumul raisonnait par CONTENEUR : depuis le RX il empruntait l'arête d'entrée
   du TX, remontait jusqu'au mur et rapportait le TEMPS DE CALCUL de celui-ci sur les SORTIES du
   RX. Le garde-fou anti-cycle évitait la récursion infinie et laissait passer l'absurdité.
2. SEGMENT A EXCLU. `rx_latency_ms` (capture réseau → écriture shm, ~19 ms ≈ 1 trame) n'était
   qu'un badge isolé, jamais additionné — le total ne pouvait pas approcher le fil-à-fil.

    ./venv/bin/python tools/verif_latence_cumul.py
"""
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
os.chdir(_R)

import main                                     # noqa: E402  (threads seulement sous __main__)
from app import metrics                         # noqa: E402
from app.database import db_list_users          # noqa: E402

# Valeurs injectées, choisies distinctes et non multiples les unes des autres : toute confusion
# entre deux termes se voit dans le total.
SEG_A   = 20.0     # capture 2110 → shm (segment A), par flux du moteur scindé
TRANSIT = 1.0      # arête → mur
OWN_MUR = 4.0      # temps de CALCUL du mur (ne doit JAMAIS remonter en amont)
TR_TX   = 2.0      # arête mur → TX
DE_MUR  = 2.0      # DÉLAI D'ÉTAGE du mur, en TRAMES (axe B — sans rapport avec OWN_MUR)


def _admin():
    for u in db_list_users():
        if u.get("role") == "admin":
            return u["id"]
    return None


def _sommaire(uid):
    app = main.app
    app.config["TESTING"] = True
    with app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user_id"] = uid
        return cl.get("/api/home/summary").get_json()


def run():
    uid = _admin()
    if uid is None:
        print("aucun utilisateur admin — banc inapplicable"); return 0

    # 1er passage À VIDE : uniquement pour découvrir la topologie réelle (qui produit quoi).
    d = _sommaire(uid)
    scindes = [n for n in d["topology"]["nodes"] if n.get("split")]
    if not scindes:
        print("aucun moteur scindé (split_io) dans la topologie — banc NON CONCLUANT"); return 2
    vmid_moteur = scindes[0]["vmid"]
    rx = next((n for n in scindes if n.get("col") == "sources"), None)
    if rx is None:
        print("moteur scindé sans moitié RX — banc NON CONCLUANT"); return 2

    shms_rx = [p["shm"] for p in rx["produces"] if (p.get("kind") or "video") == "video" and p.get("shm")]
    conso = [e["to"] for e in d["topology"]["edges"]
             if e["from"] == vmid_moteur and (e.get("kind") or "video") == "video"
             and e["shm"] in shms_rx and e["to"] != vmid_moteur]
    if not shms_rx or not conso:
        print("aucun consommateur vidéo branché sur le moteur — banc NON CONCLUANT"); return 2
    vmid_mur = conso[0]

    # ── INJECTION (mutation EN PLACE : le routeur importe ces dicts par référence) ───────────
    metrics.rx_latency_cache[vmid_moteur] = {s: SEG_A for s in shms_rx}
    metrics.own_latency_cache[vmid_mur] = OWN_MUR
    metrics.own_latency_cache.pop(vmid_moteur, None)      # le moteur ne déclare pas de calcul
    metrics.latency_cache[vmid_mur] = {"*": TRANSIT}
    metrics.latency_cache[vmid_moteur] = {"*": TR_TX}     # arête mur → TX
    metrics.delai_etage_cache[vmid_mur] = {"trames": DE_MUR, "trames_max": DE_MUR}
    metrics.delai_etage_cache.pop(vmid_moteur, None)      # le moteur n'a pas d'« étage »
    # ⚠ TOUT INTERMÉDIAIRE EN AMONT DU MUR doit mesurer, sinon la chaîne est légitimement
    # INCOMPLÈTE et le contrôle « chaîne complète » échoue pour une raison qui n'a rien à voir
    # avec le code. Le banc supposait que le mur n'avait que le moteur en amont ; le 2026-08-21
    # le parc a été recâblé (mur alimenté via mixer-test) et il est tombé. On neutralise donc
    # tous les autres étages à 0 trame — leur présence ne doit pas changer le total attendu,
    # puisque le cumul retient le MAX des chemins.
    _amont = {e["from"] for e in d["topology"]["edges"]
              if e["to"] == vmid_mur and (e.get("kind") or "video") == "video"}
    for _v in _amont:
        if _v not in (vmid_moteur, vmid_mur):
            metrics.delai_etage_cache[_v] = {"trames": 0.0, "trames_max": 0.0}

    d = _sommaire(uid)
    noeuds = {(n["vmid"], n["col"]): n for n in d["topology"]["nodes"]}
    aretes = [e for e in d["topology"]["edges"] if (e.get("kind") or "video") == "video"]
    ko = []

    def ok(cond, titre, detail=""):
        print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
        if not cond:
            print("         %s" % detail); ko.append(titre)

    def egal(a, b):
        return a is not None and abs(float(a) - float(b)) < 0.05

    print("moteur scindé=%s  mur=%s  flux=%d" % (vmid_moteur, vmid_mur, len(shms_rx)))
    print("injecté : segment A=%.1f  transit=%.1f  calcul du mur=%.1f  transit TX=%.1f\n"
          % (SEG_A, TRANSIT, OWN_MUR, TR_TX))

    # ── 1. Les sorties du RX portent le SEGMENT A — ni 0, ni le calcul d'un nœud aval ────────
    rxn = noeuds.get((vmid_moteur, "sources"))
    for p in rxn["produces"]:
        if p["shm"] not in shms_rx:
            continue
        ok(egal(p.get("delay_total_ms"), SEG_A),
           "sortie RX %s = segment A (%.1f)" % (p["shm"], SEG_A),
           "delay_total_ms=%s ; %s serait la contamination par le calcul du mur"
           % (p.get("delay_total_ms"), SEG_A + OWN_MUR))
        ok(not egal(p.get("delay_total_ms"), OWN_MUR),
           "sortie RX %s ≠ calcul du mur (non-régression collision vmid)" % p["shm"],
           "delay_total_ms=%s == own(mur)" % p.get("delay_total_ms"))

    # ── 2. Cumul à l'arrivée sur le mur = segment A + transit ────────────────────────────────
    for e in aretes:
        if e["from"] == vmid_moteur and e["to"] == vmid_mur and e["shm"] in shms_rx:
            ok(egal(e.get("cum_ms"), SEG_A + TRANSIT),
               "arête RX→mur (%s) : cumul = %.1f" % (e["shm"], SEG_A + TRANSIT),
               "cum_ms=%s" % e.get("cum_ms"))

    # ── 3. Sortie du mur = segment A + transit + son calcul (le télescopage) ─────────────────
    mur = noeuds.get((vmid_mur, "composition")) or noeuds.get((vmid_mur, "sinks")) \
        or next(n for (v, _c), n in noeuds.items() if v == vmid_mur)
    attendu = SEG_A + TRANSIT + OWN_MUR
    for p in mur["produces"]:
        ok(egal(p.get("delay_total_ms"), attendu),
           "sortie du mur %s = %.1f (A + transit + calcul)" % (p["shm"], attendu),
           "delay_total_ms=%s" % p.get("delay_total_ms"))

    # ── 4. Entrée du TX = sortie du mur + transit (le bout aval de la chaîne) ────────────────
    tx = noeuds.get((vmid_moteur, "sinks"))
    if tx:
        shms_mur = {p["shm"] for p in mur["produces"]}
        vus = [p for p in tx["consumes"] if p.get("shm") in shms_mur]
        for p in vus:
            ok(egal(p.get("delay_in_ms"), attendu + TR_TX),
               "entrée TX %s = %.1f (sortie du mur + transit)" % (p["shm"], attendu + TR_TX),
               "delay_in_ms=%s" % p.get("delay_in_ms"))
        if not vus:
            print("  n/a  aucune entrée TX alimentée par le mur")

    # ══ AXE B — le délai du SIGNAL, en trames. Grandeur DISTINCTE de l'axe A ci-dessus. ══════
    print()
    per = None
    f = mur.get("fps_nominal") or (mur.get("cadence") or {}).get("cible")
    try:
        per = 1000.0 / float(f) if float(f) > 0 else None
    except (TypeError, ValueError):
        per = None
    if per is None:
        print("  n/a  cadence nominale du mur inconnue — axe B non contrôlable")
    else:
        de = mur.get("delai_etage")
        ok(de is not None and egal(de.get("trames"), DE_MUR),
           "délai d'étage du mur = %.1f trames" % DE_MUR, "delai_etage=%s" % de)
        ok(de is not None and egal(de.get("ms"), DE_MUR * per),
           "délai d'étage du mur = %.1f ms (trames × période)" % (DE_MUR * per),
           "delai_etage=%s période=%.1f" % (de, per))

        # Le SIGNAL en sortie du mur = segment A + délai d'étage. Le temps de CALCUL (OWN_MUR)
        # ne doit PAS y entrer : c'est tout l'objet de la séparation des deux axes.
        att_ms = SEG_A + DE_MUR * per
        for p_ in mur["produces"]:
            sig = p_.get("delai_signal") or {}
            ok(egal(sig.get("ms"), att_ms),
               "signal en sortie du mur = %.1f ms (A + étage)" % att_ms,
               "delai_signal=%s" % sig)
            ok(not egal(sig.get("ms"), att_ms + OWN_MUR),
               "signal en sortie du mur SANS le temps de calcul (axes séparés)",
               "delai_signal=%s contient OWN_MUR" % sig)
            ok(egal(sig.get("trames"), att_ms / per),
               "signal en sortie du mur = %.2f trames" % (att_ms / per),
               "delai_signal=%s" % sig)
            ok(sig.get("complet") is True and not sig.get("manquants"),
               "chaîne COMPLÈTE quand tous les étages mesurent",
               "complet=%s manquants=%s" % (sig.get("complet"), sig.get("manquants")))

        # Retrait de la mesure : le total doit devenir INCOMPLET et NOMMER l'étage fautif —
        # surtout pas retomber silencieusement sur zéro ou sur le temps de calcul.
        metrics.delai_etage_cache.pop(vmid_mur, None)
        d2 = _sommaire(uid)
        mur2 = next(n for n in d2["topology"]["nodes"] if n["vmid"] == vmid_mur and n["produces"])
        for p_ in mur2["produces"]:
            sig = p_.get("delai_signal") or {}
            ok(sig.get("complet") is False,
               "étage non mesuré → chaîne déclarée INCOMPLÈTE", "complet=%s" % sig.get("complet"))
            ok(bool(sig.get("manquants")),
               "étage non mesuré → il est NOMMÉ", "manquants=%s" % sig.get("manquants"))
            ok(egal(sig.get("ms"), SEG_A),
               "étage non mesuré → on ne compte QUE ce qui est mesuré (%.1f)" % SEG_A,
               "delai_signal=%s" % sig)
        ok((mur2.get("delai_etage") is None),
           "étage non mesuré → delai_etage absent, pas un zéro",
           "delai_etage=%s" % mur2.get("delai_etage"))

        # ── MUR SHARDÉ : le cumul doit suivre la PLUS VIEILLE entrée, pas la première ────────
        # Le mur lit plusieurs flux du moteur. On donne à l'un d'eux un segment A plus grand :
        # le cumul du mur doit adopter CELUI-LÀ. Suivre `edges[0]` rendrait invisible un shard
        # en retard alors que ses tuiles sont dans l'image composée.
        # La cible doit être un flux RÉELLEMENT consommé par le mur — majorer un flux qu'il
        # n'écoute pas ne prouverait rien (première rédaction : elle majorait `_5`, absent de
        # ses entrées, et le banc l'a démentie).
        _in_mur = sorted({e["shm"] for e in aretes
                          if e["to"] == vmid_mur and e["shm"] in shms_rx})
        if not _in_mur:
            print("  n/a  le mur ne consomme aucun flux du moteur — cas shardé non contrôlable")
            _in_mur = None
        if _in_mur:
            _pire = dict(metrics.rx_latency_cache[vmid_moteur])
            _cible = _in_mur[-1]
            _pire[_cible] = SEG_A * 3
            metrics.rx_latency_cache[vmid_moteur] = _pire
            metrics.delai_etage_cache[vmid_mur] = {"trames": DE_MUR, "trames_max": DE_MUR}
            d3 = _sommaire(uid)
            mur3 = next(n for n in d3["topology"]["nodes"] if n["vmid"] == vmid_mur and n["produces"])
            att3 = SEG_A * 3 + DE_MUR * per
            for p_ in mur3["produces"]:
                sig = p_.get("delai_signal") or {}
                ok(egal(sig.get("ms"), att3),
                   "entrées inégales (%s majoré) → le cumul suit la PLUS VIEILLE (%.1f ms)" % (_cible, att3),
                   "delai_signal=%s ; le suivi de edges[0] aurait donné %.1f" % (sig, SEG_A + DE_MUR * per))
            metrics.rx_latency_cache[vmid_moteur] = {s_: SEG_A for s_ in shms_rx}

        # Émission 2110 : constante, mais ÉTIQUETÉE comme non mesurée.
        tx2 = next((n for n in d2["topology"]["nodes"]
                    if n["vmid"] == vmid_moteur and n.get("col") == "sinks"), None)
        if tx2:
            em = tx2.get("delai_emission") or {}
            ok(em.get("estime") is True and em.get("mesure") is False,
               "émission 2110 affichée comme ESTIMÉE, non mesurée", "delai_emission=%s" % em)

    print("\n%s" % ("Tous les invariants tiennent." if not ko else "%d invariant(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    c = run()
    sys.stdout.flush()
    os._exit(c)
