#!/usr/bin/env python3
"""Vérifie qu'une entrée FIGÉE dont le producteur AVANCE lève une alerte — et qu'un still,
dont le producteur est figé lui aussi, n'en lève AUCUNE.

Pourquoi (2026-10-24) : la pyramide 1153 a lu pendant HUIT JOURS un flux immobile pendant que
son producteur écrivait à 50,1 grains/s. Conteneur `running`, agent 200, script vivant, journal
vide, aucune alerte. `entrees_ko` ne pouvait pas le voir : il couvre l'échec d'OUVERTURE, et
cette entrée s'était ouverte avec succès avant de se figer. La panne a été trouvée par hasard.

Le critère compare des DELTAS des deux côtés, ce qui annule le faux positif le plus probable :
une source volontairement figée (still, mire, pause) a un producteur qui n'avance pas non plus.

Ce que la garde verrouille :
  (1) entrée figée + producteur qui avance + plus de 10 s → UNE alerte `error` ;
  (2) **still / producteur figé → RIEN** (le cas que la note annonce comme auto-annulé) ;
  (3) rien avant le seuil de persistance ;
  (4) pas de rafale ;
  (5) retour à la normale en `info`, et seulement si on avait alerté ;
  (6) producteur sur un AUTRE nœud (réplique RDMA) → abstention totale ;
  (7) script volontairement arrêté → rien, et l'état est purgé ;
  (8) les deux formats de consommateur : `sources` (pyramide) et `fi_par_tuile` × `proxy_usage`
      (multiview) — sans ce croisement, `fi_par_tuile` ne porte aucun nom de flux ;
  (9) `kind` dans ALERT_KINDS, clés i18n présentes en FR et EN avec les mêmes champs ;
 (10) le pont est appelé depuis `rafraichir_metrics`.

Aucun réseau, aucune base : doublures sur le module. Le temps est manipulé en reculant `t0`,
plutôt qu'en dormant — déterministe et instantané.

    ./venv/bin/python tests/verif_alerte_source_figee.py
"""
import json
import os
import re
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
# `connect_guarded` (2026-10-24) ne donne la base de prod à un test qu'en lecture seule et
# l'avertit sur stderr. Lire la prod reste une dépendance cachée : le verdict dépendrait des
# réglages du site. Base jetable, posée AVANT le premier accès (la connexion est mémorisée
# par thread : surcharger DB_PATH après coup n'isole plus rien).
import tempfile                                          # noqa: E402
from app import config as _cfg, database as _dbm         # noqa: E402
_cfg.DB_PATH = _dbm.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif-"), "principale.db")
_dbm._tls.__dict__.pop("conn", None)
_dbm.init_db()

from app import metrics as M                            # noqa: E402
from app.database import ALERT_KINDS                    # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


alertes = []
CONSO, PROD = 1153, 619
MV = 1248                               # consommateur au format multiview
# ⚠ Tout vmid utilisé par un cas DOIT figurer ici : un `node_id` à None d'un côté fait tomber la
# comparaison dans l'abstention « nœuds différents ». Mon premier jet avait oublié 1248 et le cas
# multiview paraissait non détecté — c'était le harnais, pas le code.
NOEUDS = {CONSO: 34, PROD: 34, MV: 34, 1215: 34}


def pyr(shm, fi):
    return {"sources": {"3": {"shm": shm, "frame_index": fi}}}


def mv(shm, fi):
    """Format multiview : l'index est par tuile, le nom de flux vient de proxy_usage."""
    return {"fi_par_tuile": {"0": fi}, "proxy_usage": {"0": {"src": shm, "cost": "copy"}}}


def vieillir(secondes=30.0):
    """Recule t0 de tous les états : simule la persistance sans dormir."""
    for st in M._fige_etat.values():
        st["t0"] -= secondes


sauve = (M.db_add_alert, M.db_get_container, M._node_de, M._producteur_de)
try:
    M.db_add_alert = lambda msg, niveau="info", **k: alertes.append((msg, niveau, k))
    M.db_get_container = lambda v: {"hostname": "c%s" % v, "node_id": NOEUDS.get(v)}
    M._node_de = lambda v: NOEUDS.get(v)
    M._producteur_de = lambda shm: PROD

    def reset(prod_actif=True):
        M._fige_etat.clear()
        M.shm_active_cache[PROD] = prod_actif
        del alertes[:]

    # (1) le cas réel
    reset(prod_actif=True)
    M._noter_sources_figees(CONSO, pyr("f1", 100))      # premier relevé : on mémorise
    ok(alertes == [], "premier relevé : aucune alerte (rien à comparer)", str(alertes))
    M._noter_sources_figees(CONSO, pyr("f1", 100))      # figé, mais pas encore 10 s
    ok(alertes == [], "(3) figé mais sous le seuil : aucune alerte", str(alertes))
    vieillir(30)
    M._noter_sources_figees(CONSO, pyr("f1", 100))
    ok(len(alertes) == 1, "(1) figé > 10 s + producteur actif → UNE alerte", str(alertes))
    if alertes:
        msg, niveau, kw = alertes[0]
        ok(msg == "alert.signal.source_figee", "message = clé i18n", msg)
        ok(niveau == "error", "niveau error", niveau)
        ok(kw.get("kind") in ALERT_KINDS, "(9) kind dans ALERT_KINDS", str(kw.get("kind")))
        ok(kw.get("vmid") == CONSO, "rattachée au CONSOMMATEUR", str(kw.get("vmid")))
        ok((kw.get("params") or {}).get("prod") == "c619",
           "les params nomment le producteur", str(kw.get("params")))

    # (4) pas de rafale
    del alertes[:]
    for _ in range(4):
        vieillir(30)
        M._noter_sources_figees(CONSO, pyr("f1", 100))
    ok(alertes == [], "(4) pas de rafale sur un gel qui dure", str(alertes))

    # (5) retour à la normale
    del alertes[:]
    M._noter_sources_figees(CONSO, pyr("f1", 101))
    ok(len(alertes) == 1 and alertes[0][0] == "alert.signal.source_repartie",
       "(5) l'index repart → alerte de reprise", str(alertes))
    ok(alertes and alertes[0][1] == "info", "la reprise est en info", str(alertes))
    del alertes[:]
    M._noter_sources_figees(CONSO, pyr("f1", 102))
    ok(alertes == [], "la reprise ne se répète pas", str(alertes))

    # (2) ★ STILL : l'entrée est figée, mais le producteur AUSSI → rien, jamais.
    reset(prod_actif=False)
    for _ in range(6):
        M._noter_sources_figees(CONSO, pyr("still1", 500))
        vieillir(30)
    M._noter_sources_figees(CONSO, pyr("still1", 500))
    ok(alertes == [], "(2) STILL : producteur figé lui aussi → AUCUNE alerte", str(alertes))

    # (6) réplique : producteur sur un autre nœud → abstention
    reset(prod_actif=True)
    NOEUDS[PROD] = 42
    for _ in range(3):
        M._noter_sources_figees(CONSO, pyr("f1", 100))
        vieillir(30)
    ok(alertes == [], "(6) producteur sur un AUTRE nœud (réplique) → abstention", str(alertes))
    NOEUDS[PROD] = 34

    # (7) script volontairement arrêté
    reset(prod_actif=True)
    M._noter_sources_figees(CONSO, pyr("f1", 100))
    vieillir(30)
    import app.database as _db
    _sauve_se = _db.db_script_enabled
    _db.db_script_enabled = lambda v: False
    try:
        M._noter_sources_figees(CONSO, pyr("f1", 100))
    finally:
        _db.db_script_enabled = _sauve_se
    ok(alertes == [], "(7) script arrêté volontairement → aucune alerte", str(alertes))
    ok(not [k for k in M._fige_etat if k[0] == CONSO],
       "(7) et l'état est purgé (pas de fausse reprise au redémarrage)", str(list(M._fige_etat)))

    # (8) format multiview : le croisement fi_par_tuile × proxy_usage
    reset(prod_actif=True)
    M._noter_sources_figees(MV, mv("fab_x", 900))
    vieillir(30)
    M._noter_sources_figees(MV, mv("fab_x", 900))
    ok(len(alertes) == 1, "(8) multiview : fi_par_tuile croisé avec proxy_usage → détecté",
       str(alertes))
    # …et sans proxy_usage, on ne peut PAS rattacher la tuile à un flux : on s'abstient.
    reset(prod_actif=True)
    M._noter_sources_figees(MV, {"fi_par_tuile": {"0": 900}})
    vieillir(30)
    M._noter_sources_figees(MV, {"fi_par_tuile": {"0": 900}})
    ok(alertes == [], "(8) fi_par_tuile SEUL (sans proxy_usage) → abstention", str(alertes))

    # un consommateur qui ne publie aucun index par entrée ne déclenche rien
    reset(prod_actif=True)
    M._noter_sources_figees(1215, {"fps": 50.0})
    ok(alertes == [], "consommateur sans index par entrée → rien", str(alertes))
finally:
    (M.db_add_alert, M.db_get_container, M._node_de, M._producteur_de) = sauve
    M._fige_etat.clear()
    M.shm_active_cache.pop(PROD, None)

# ── (9) i18n ────────────────────────────────────────────────────────────────────────
fr = json.load(open(os.path.join(_R, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(_R, "i18n", "en.json"), encoding="utf-8"))
for cle in ("alert.signal.source_figee", "alert.signal.source_repartie"):
    ok(cle in fr and cle in en, "clé %s présente en FR et EN" % cle)
    if cle in fr and cle in en:
        cf = set(re.findall(r"\{(\w+)", fr[cle]))
        ce = set(re.findall(r"\{(\w+)", en[cle]))
        ok(cf == ce, "mêmes champs interpolés FR/EN pour %s" % cle,
           "FR %s / EN %s" % (sorted(cf), sorted(ce)))

# ── (10) le pont est appelé depuis la boucle ────────────────────────────────────────
src = open(os.path.join(_R, "app", "metrics.py"), encoding="utf-8").read()
corps = src.split("def rafraichir_metrics(")[-1]
ok("_noter_sources_figees(vmid, m)" in corps,
   "(10) le pont est APPELÉ depuis rafraichir_metrics")
# le seuil reste une persistance réelle, pas zéro
ok(M._FIGE_SEUIL_S >= 10.0, "le seuil de persistance est d'au moins 10 s", str(M._FIGE_SEUIL_S))

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
