#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Vérifie le plugin `cartouchier` HORS LIGNE : ni MXL, ni ffmpeg, ni conteneur.
#
# Ce qui est vérifié, et pourquoi chaque point est ici (chacun est une panne SILENCIEUSE) :
#   1. le gabarit se rend ET compile, le registre l'accepte, les noms de flux du script sont
#      ceux de `wiring.produces` (sinon la console lit /flows sous des noms qu'aucun câble ne porte) ;
#   2. la logique des lecteurs, pilotée avec des tableaux numpy : lecteur libre, lecteur affecté,
#      exclusivité, boucle, enchaînement SANS TROU, deuxième appui (arrêt / pause / relance),
#      fondu, coupure sans clic, A/B, enchaînement automatique ;
#   3. la R128 sur des signaux de sonie CONNUE (sinus de référence BS.1770), et la normalisation
#      appliquée de bout en bout (ce qui sort du lecteur est à la cible) ;
#   4. /flows et les actions de macro, par le script RENDU lui-même (chaque action déclarée est
#      servie, chaque liste vivante répond, chaque état déclaré se lit), et la persistance :
#      le document relu par `control.persist` redonne le même cartouchier après redéploiement ;
#   2 bis. (0.1.2) les DÉFAUTS des touches : héritage cartouchier → page → touche, surcharge,
#      retour au défaut, défaut changé qui ne touche que les héritiers (le fondu même en cours de
#      lecture), migration d'un document de version 1, affectation directe (remplacement qui garde
#      les réglages, annulation, insertion dans la playlist à une position) ;
#   5. la console : contrat de page publique, arrêt du sondage, aucune clé i18n manquante, les trois
#      gestes d'affectation (souris, doigt, toucher-toucher), la résolution des défauts IDENTIQUE à
#      celle du moteur (exécutée sous node), le rattachement d'un envoi à SA touche.
#   4 bis. (0.1.3) l'éditeur de clip et la pré-écoute : la LISTE DE MONTAGE rendue par le moteur,
#      échantillon par échantillon contre les lois recalculées ici (In / Out, fondus et leurs formes,
#      coupe avec micro-fondu, enveloppe, assemblage en fondu enchaîné, normalisation par son, gain),
#      la durée montée dans /flows et /state, la pré-écoute qui ne touche aucun lecteur, l'écoute
#      (/listen.pcm : morceaux de 10 ms, arrêt de la pré-écoute au départ du dernier auditeur), les
#      fenêtres de forme d'onde (niveau grossier, échantillons), « nouveau média » (fichier, durée,
#      contenu, sidecar lu par le relevé de la base, original intact) ; côté console, la projection
#      temps source <-> temps monté et l'enveloppe identiques au moteur.
#   2 ter. (0.1.7) le PASSAGE A/B : ce que devient l'élément de l'autre lecteur au lancement (laisser
#      jouer par défaut, couper, fondu de la durée réglée), la suite A/B quand l'ancien joue encore
#      (lecteur repris au GO quand les deux jouent, enchaînement auto), réglage sauvegardé et pilotable.
#
# Chaque garde a été MUTÉE une fois (le défaut correspondant introduit dans une copie du
# plugin) pour vérifier qu'elle mord — une garde verte du premier coup est suspecte. Pour
# rejouer une mutation : copier plugins/cartouchier ailleurs (le dossier doit garder ce nom),
# la modifier, puis
#     CART_PLUGIN_DIR=/chemin/vers/copie/cartouchier ./venv/bin/python tests/verif_plugin_cartouchier.py
#
#   $ ./venv/bin/python tests/verif_plugin_cartouchier.py
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
DOSSIER = os.environ.get("CART_PLUGIN_DIR") or os.path.join(RACINE, "plugins", "cartouchier")

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    if condition:
        reussites.append(intitule)
    else:
        echecs.append((intitule, explication))


def lire(nom):
    with open(os.path.join(DOSSIER, nom), encoding="utf-8") as f:
        return f.read()


# ── 0. Présence ──────────────────────────────────────────────────────────────────────────────
if not os.path.isfile(os.path.join(DOSSIER, "plugin.json")):
    # Deux cas que le même dossier vide confond. Plugin ENREGISTRÉ comme sous-module mais non
    # initialisé : échec (le clone est incomplet). Pas encore enregistré du tout (son dépôt
    # distant n'existe pas encore) : rien à vérifier, on le DIT au lieu de passer en silence.
    gm = os.path.join(RACINE, ".gitmodules")
    inscrit = os.path.isfile(gm) and "plugins/cartouchier" in open(gm, encoding="utf-8").read()
    if inscrit:
        print("ÉCHEC : plugins/cartouchier est vide — sous-module non initialisé.\n"
              "        git submodule update --init plugins/cartouchier", file=sys.stderr)
        sys.exit(1)
    print("SAUTÉ : plugins/cartouchier absent et non inscrit dans .gitmodules — rien à vérifier.")
    sys.exit(0)

import numpy as np  # noqa: E402

manifeste = json.loads(lire("plugin.json"))
script = lire("script.py")
SR = 48000
HOTE = "studio-cart"

# ── 1. Gabarit, manifeste, registre ──────────────────────────────────────────────────────────
params = dict(manifeste.get("deploy_defaults") or {})
try:
    rendu = script.format(config=repr(params), hostname=HOTE,
                          plugin_version=manifeste.get("version", "0"))
    controle("le gabarit str.format se rend (accolades doublées)", True)
except Exception as e:
    rendu = ""
    controle("le gabarit str.format se rend (accolades doublées)", False, repr(e))
if rendu:
    try:
        compile(rendu, "<cartouchier>", "exec")
        controle("le script rendu compile", True)
    except SyntaxError as e:
        controle("le script rendu compile", False, "ligne %s : %s" % (e.lineno, e.msg))

from app import plugins as _pl  # noqa: E402
if os.environ.get("CART_PLUGIN_DIR"):
    _pl.PLUGINS_DIR = os.path.dirname(DOSSIER)
_pl.reload()
controle("le registre accepte le plugin", "cartouchier" in _pl.REGISTRY,
         "écarté au scan : %s" % _pl.scan_errors().get("cartouchier"))

ctrl = manifeste.get("control") or {}
eps = set(ctrl.get("endpoints") or [])
reads = set(ctrl.get("read_endpoints") or [])
controle("/flows est lisible avec la seule connexion (read_endpoint)", "/flows" in reads,
         "la console de la phase 2 lit /flows sans droit d'exploitation")
controle("/state est lisible en condition de macro", "/state" in reads)
manquants = sorted({a.get("endpoint") for a in manifeste.get("actions") or [] if not a.get("core")} - eps)
controle("chaque action vise un chemin de control.endpoints", not manquants,
         "le proxy refuse un chemin non déclaré : %s" % manquants)
opts = sorted({p.get("options_endpoint") for a in manifeste.get("actions") or []
               for p in a.get("params") or [] if p.get("options_endpoint")} - reads)
controle("chaque liste vivante est un read_endpoint", not opts, "options non lisibles : %s" % opts)
ids_actions = {a.get("id") for a in manifeste.get("actions") or []}
for requis in ("fire", "stop", "stop_all", "page", "page_next", "pl_go", "pl_next", "pl_prev",
               "ab_on", "ab_off", "player_play", "player_stop", "preview_key", "preview_stop"):
    controle("action de macro « %s » déclarée" % requis, requis in ids_actions,
             "une capacité non exposée aux macros est morte (CLAUDE.md)")
from app.plugin_persist import validate_spec  # noqa: E402
controle("control.persist est valide", not validate_spec(manifeste), str(validate_spec(manifeste)))
controle("control.persist retient le document du cartouchier (param cart, instantané /doc)",
         (ctrl.get("persist") or {}).get("param") == "cart"
         and (ctrl.get("persist") or {}).get("snapshot") == "/doc")
controle("deux lecteurs par défaut", params.get("players") == 2)
for f in manifeste.get("files") or []:
    controle("fichier livré présent : %s" % f["src"], os.path.isfile(os.path.join(DOSSIER, f["src"])))
controle("image média (ffmpeg + numpy + libmxl)", manifeste.get("image") == "media")
controle("rubrique de navigation « medias »", (manifeste.get("nav") or {}).get("section") == "medias")

wiring = _pl.derive_wiring("cartouchier", HOTE, {"players": 3}) if "cartouchier" in _pl.REGISTRY \
    else {"produces": []}
flux_cables = [p["shm"] for p in wiring["produces"]]
controle("wiring.produces déplie un flux audio par lecteur",
         flux_cables == ["%s_player_%d" % (HOTE, i) for i in (1, 2, 3)]
         and all(p.get("essence") == "audio" and (p.get("format") or {}).get("channels") == 2
                 for p in wiring["produces"]), str(wiring["produces"]))

# ── Modules du plugin (importés tels qu'ils seront poussés : paquet `cart`) ──────────────────
_tmp = tempfile.mkdtemp(prefix="verif_cart_")
os.symlink(os.path.join(DOSSIER, "src"), os.path.join(_tmp, "cart"))
sys.path.insert(0, _tmp)
from cart import engine as E  # noqa: E402
from cart import loudness as L  # noqa: E402

BLK = 48


def Z0(d):
    """La première zone d'un document (0.1.8 : un document migré n'en a qu'une)."""
    return d["zones"][0]


def dc(v, n):
    return E.ArraySource(np.full((n, 2), v, dtype=np.float32))


def sine(db, secs, f=997.0, left_only=False):
    t = np.arange(int(SR * secs)) / SR
    x = (10 ** (db / 20.0) * np.sin(2 * np.pi * f * t)).astype(np.float32)
    return np.stack([x, np.zeros_like(x) if left_only else x], axis=1)


def moteur(keys, n_players=2, items=None, mode="grid", ab=True, auto=False, norm=None, handover=None, handover_ms=None):
    srcs = {}
    doc_keys = []
    for i, (kid, src, extra) in enumerate(keys):
        srcs[kid] = src
        doc_keys.append(dict({"id": kid, "slot": i, "title": kid.upper(),
                              "media": {"asset": kid, "file": ""}}, **extra))
    doc_items = []
    for iid, src, extra in items or []:
        srcs[iid] = src
        doc_items.append(dict({"id": iid, "title": iid.upper(), "media": {"asset": iid, "file": ""}}, **extra))
    doc = {"mode": mode, "pages": [{"id": "p1", "keys": doc_keys}],
           "playlist": {"items": doc_items, "ab": ab, "auto": auto}}
    if handover is not None:
        doc["playlist"]["ab_handover"] = handover
    if handover_ms is not None:
        doc["playlist"]["ab_handover_ms"] = handover_ms
    return E.CartEngine(n_players, SR, doc, source_for=lambda m: srcs.get(m.get("asset")),
                        norm_db_for=norm or (lambda m: 0.0))


def rendre(e, ms):
    out = [[] for _ in e.players]
    for _ in range(ms):
        for i, b in enumerate(e.render(BLK)):
            out[i].append(b)
    return [np.concatenate(o) for o in out]


def etat(e, n):
    return e.players[n - 1].state


def refus(fn, code):
    try:
        fn()
    except E.EngineError as ex:
        return ex.code == code
    return False


class scenario:
    """Une exception dans un scénario est un ÉCHEC nommé, pas un banc qui s'arrête : un moteur
    muté qui lève au lieu de se tromper doit être compté, et les scénarios suivants tourner."""

    def __init__(self, nom):
        self.nom = nom

    def __enter__(self):
        return self

    def __exit__(self, typ, ex, tb):
        if ex is not None and isinstance(ex, Exception):
            controle(self.nom + " (exception)", False, "%s: %s" % (typ.__name__, ex))
            return True
        return False


# ── 2. Lecteurs ──────────────────────────────────────────────────────────────────────────────
long_ = SR * 5
with scenario('lecteur libre : deux touches prennent les lecteurs 1 puis 2'):
    e = moteur([("a", dc(.5, long_), {}), ("b", dc(.5, long_), {}), ("c", dc(.5, long_), {})])
    e.fire("a")
    e.fire("b")
    controle("lecteur libre : deux touches prennent les lecteurs 1 puis 2",
             e.holder("a").n == 1 and e.holder("b").n == 2)
    controle("lecteur libre : sans lecteur libre la touche est REFUSÉE (pas jouée en silence)",
             refus(lambda: e.fire("c"), "no_free_player"))
    out = rendre(e, 2)
    controle("un lecteur en lecture produit le son, gain unité", abs(float(out[0][-1, 0]) - .5) < 1e-6)

with scenario('lecteur affecté occupé / SON lecteur même si le 1 est libre'):
    e = moteur([("a", dc(.5, long_), {}), ("b", dc(.3, long_), {"player": 2})])
    r = e.fire("b")
    controle("lecteur affecté : la touche va sur SON lecteur même si le 1 est libre", r["player"] == 2)
    e.fire("a")
    e2 = moteur([("a", dc(.5, long_), {"player": 1}), ("b", dc(.3, long_), {"player": 1})])
    e2.fire("a")
    e2.fire("b")
    controle("lecteur affecté occupé : la nouvelle touche le reprend",
             e2.players[0].item["id"] == "b" and etat(e2, 2) == "idle")

with scenario('exclusivité : lancer B arrête A (même groupe)'):
    e = moteur([("a", dc(.5, long_), {"group": "g"}), ("b", dc(.3, long_), {"group": "g"}),
                ("c", dc(.2, long_), {})])
    e.fire("a")
    e.fire("c")
    e.fire("b")
    controle("exclusivité : lancer B arrête A (même groupe)", e.holder("a") is None and e.holder("b") is not None)
    controle("exclusivité : une touche hors groupe continue", e.holder("c") is not None)

with scenario('boucle : un son de 1000 échantillons joue encore après 4800'):
    e = moteur([("a", dc(.5, 1000), {"loop": True})])
    e.fire("a")
    out = rendre(e, 100)[0]
    controle("boucle : un son de 1000 échantillons joue encore après 4800",
             etat(e, 1) == "playing" and e.counters["looped"] >= 4)
    controle("boucle : aucun trou au point de bouclage", float(np.min(np.abs(out[:, 0]))) > .49)

with scenario('enchaînement : B part sur le MÊME lecteur à la fin de A'):
    e = moteur([("a", dc(.5, 1000), {"next": "b"}), ("b", dc(.25, long_), {})])
    e.fire("a")
    out = rendre(e, 30)[0][:, 0]
    controle("enchaînement : B part sur le MÊME lecteur à la fin de A",
             e.holder("b") is not None and e.holder("b").n == 1 and e.counters["chained"] == 1)
    controle("enchaînement : sans trou (échantillon 999 = A, 1000 = B, aucun zéro)",
             abs(out[999] - .5) < 1e-6 and abs(out[1000] - .25) < 1e-6 and float(np.min(np.abs(out[:1440]))) > .2)

with scenario("fin d'un son sans boucle ni enchaînement : le lecteur se libère"):
    e = moteur([("a", dc(.5, 1000), {})])
    e.fire("a")
    rendre(e, 30)
    controle("fin d'un son sans boucle ni enchaînement : le lecteur se libère", etat(e, 1) == "idle")

with scenario('deuxième appui « arrêt » sans fondu : lecteur libéré'):
    e = moteur([("a", dc(.5, long_), {"second_press": "stop"})])
    e.fire("a")
    rendre(e, 5)
    e.fire("a")
    out = rendre(e, 10)[0][:, 0]
    controle("deuxième appui « arrêt » sans fondu : lecteur libéré", etat(e, 1) == "idle")
    controle("coupure SANS CLIC : la queue descend en rampe au lieu de tomber à zéro",
             .45 < out[0] <= .5 and 0 < out[100] < out[0] and out[300] == 0.0,
             "premiers échantillons après la coupure : %s / %s / %s" % (out[0], out[100], out[300]))

with scenario('deuxième appui « pause » : lecteur en pause, position tenue'):
    e = moteur([("a", E.ArraySource(np.repeat(np.linspace(0, 1, long_, dtype=np.float32)[:, None], 2, 1)),
                 {"second_press": "pause"})])
    e.fire("a")
    rendre(e, 10)
    pos = e.players[0].pos
    e.fire("a")
    rendre(e, 10)
    controle("deuxième appui « pause » : lecteur en pause, position tenue",
             etat(e, 1) == "paused" and e.players[0].pos == pos)
    e.fire("a")
    rendre(e, 1)
    controle("appui sur une touche en pause : reprise où elle était",
             etat(e, 1) == "playing" and e.players[0].pos == pos + BLK)

with scenario('deuxième appui « relance » : repart du début'):
    e = moteur([("a", dc(.5, long_), {"second_press": "restart"})])
    e.fire("a")
    rendre(e, 50)
    e.fire("a")
    controle("deuxième appui « relance » : repart du début", etat(e, 1) == "playing" and e.players[0].pos == 0)

with scenario("fondu d'arrêt : état fading pendant le fondu"):
    e = moteur([("a", dc(.5, long_), {"fade_out_ms": 100})])
    e.fire("a")
    rendre(e, 5)
    e.stop_key("a")
    controle("fondu d'arrêt : état fading pendant le fondu", etat(e, 1) == "fading")
    out = rendre(e, 101)[0][:, 0]
    controle("fondu d'arrêt : niveau décroissant, nul à la fin, lecteur libéré",
             bool(np.all(np.diff(out[:4800]) <= 1e-7)) and out[2400] < .5 and out[-1] == 0.0
             and etat(e, 1) == "idle")
    e = moteur([("a", dc(.5, long_), {"fade_out_ms": 1000})])
    e.fire("a")
    e.stop_key("a")
    e.fire("a")
    controle("appui pendant le fondu : coupe aussitôt", etat(e, 1) == "idle")

with scenario('lecteur libre : il écrit du SILENCE'):
    e = moteur([("a", dc(.5, long_), {})])
    out = rendre(e, 3)
    controle("lecteur libre : il écrit du SILENCE (la sortie ne dépend pas d'un son chargé)",
             all(o.shape == (3 * BLK, 2) and not o.any() for o in out))

with scenario("fader start : arrêt seulement si la lecture a eu lieu (if_played)"):
    items = [("i1", dc(.1, SR), {}), ("i2", dc(.2, SR), {})]
    e = moteur([], items=items, mode="playlist", ab=True)
    r = e.player_stop(1, if_played=True)
    controle("if_played : un élément chargé, pas encore lu, reste chargé (fermer le fader pour le lancer)",
             r.get("action") == "none" and etat(e, 1) == "cued" and e.players[0].item["id"] == "i1")
    e.pl_go(); rendre(e, 2)
    r = e.player_stop(1, if_played=True)
    controle("if_played : en cours de lecture, l'arrêt s'applique", r.get("action") == "stop")
    e2 = moteur([], items=items, mode="playlist", ab=True)
    e2.player_stop(1)
    controle("sans if_played : comportement inchangé (l'élément chargé est retiré)", etat(e2, 1) != "cued")

with scenario("fader start : jouer un lecteur lance CE qu'il tient, pas l'élément de l'autre lecteur"):
    items = [("i1", dc(.1, SR), {}), ("i2", dc(.2, SR), {}), ("i3", dc(.3, SR), {})]
    e = moteur([], items=items, mode="playlist", ab=True)
    # A tient i1 prêt ; on prépare i2 sur B (l'élément « suivant » au sens de la playlist pointe ailleurs)
    e.players[1].pending_cue = "i2"; e._apply_pending_cue(e.players[1])
    e.pl_ready = "i2"
    r = e.player_play(1)
    controle("player_play(A) : A joue SON élément (i1), B ne part pas",
             etat(e, 1) == "playing" and e.players[0].item["id"] == "i1" and etat(e, 2) != "playing")
    controle("après coup : B tient l'élément qui suit i1 (i2)", e.pl_on_air == "i1" and e.pl_ready == "i2"
             and (etat(e, 2) == "cued" and e.players[1].item["id"] == "i2"))

with scenario("A/B : à l'ouverture, A tient le premier élément prêt"):
    items = [("i1", dc(.1, SR), {}), ("i2", dc(.2, SR), {}), ("i3", dc(.3, SR), {})]
    e = moteur([], items=items, mode="playlist", ab=True, handover="cut")
    controle("A/B : à l'ouverture, A tient le premier élément prêt",
             etat(e, 1) == "cued" and e.players[0].item["id"] == "i1")
    e.pl_go()
    controle("A/B : GO → A joue, B tient l'élément suivant prêt",
             etat(e, 1) == "playing" and etat(e, 2) == "cued" and e.players[1].item["id"] == "i2")
    e.pl_go()
    controle("A/B (passage « couper ») : second GO → B joue, A se libère et tient le troisième",
             etat(e, 2) == "playing" and etat(e, 1) == "cued" and e.players[0].item["id"] == "i3")
    e.pl_step(-1)
    controle("playlist « précédent » : l'élément prêt recule, l'antenne ne bouge pas",
             etat(e, 2) == "playing" and e.pl_ready == "i2" and e.players[0].item["id"] == "i2")
    e = moteur([], items=items, mode="playlist", ab=True, auto=True)
    e.pl_go()
    rendre(e, 1005)
    controle("enchaînement automatique : à la fin de A, B part seul",
             etat(e, 2) == "playing" and e.pl_on_air == "i2")
    e = moteur([], items=items, mode="playlist", ab=False)
    e.pl_go()
    e.pl_go()
    controle("playlist sans A/B : GO lance l'élément suivant, rien ne reste prêt sur un lecteur",
             e.pl_on_air == "i2" and not any(p.state == "cued" for p in e.players))

# ── Passage A/B (0.1.7) : sort de l'élément qui joue sur l'AUTRE lecteur quand on lance le suivant ──
with scenario("passage A/B : défaut « laisser jouer », bornes, sauvegarde"):
    items = [("i1", dc(.1, SR * 4), {}), ("i2", dc(.2, SR * 4), {}), ("i3", dc(.3, SR * 4), {})]
    e = moteur([], items=items, mode="playlist", ab=True)
    pl_ = Z0(e.doc)["playlist"]
    controle("passage A/B : le défaut est « laisser jouer » (document sans réglage, et document neuf)",
             pl_.get("ab_handover") == "keep" and pl_.get("ab_handover_ms") == 1000
             and Z0(E.default_doc())["playlist"].get("ab_handover") == "keep", str(pl_))
    e.pl_options({"ab_handover": "n_importe", "ab_handover_ms": 99999})
    controle("passage A/B : mode inconnu → défaut, durée bornée à 30 s",
             Z0(e.doc)["playlist"]["ab_handover"] == "keep" and Z0(e.doc)["playlist"]["ab_handover_ms"] == 30000,
             str(Z0(e.doc)["playlist"]))
    e.pl_options({"ab_handover": "fade", "ab_handover_ms": 450})
    cap = e.capture("playlist")
    e.pl_options({"ab_handover": "cut", "ab_handover_ms": 0})
    e.recall("playlist", cap["payload"])
    controle("passage A/B : la sauvegarde de playlist porte le réglage, le rappel le restitue",
             cap["payload"].get("ab_handover") == "fade" and Z0(e.doc)["playlist"]["ab_handover"] == "fade"
             and Z0(e.doc)["playlist"]["ab_handover_ms"] == 450, str(cap["payload"].get("ab_handover")))

with scenario("passage A/B « laisser jouer » : les deux jouent, la suite A/B reste juste"):
    items = [("i1", dc(.1, SR * 4), {"fade_out_ms": 0}), ("i2", dc(.2, SR * 4), {"fade_out_ms": 0}),
             ("i3", dc(.3, SR * 4), {"fade_out_ms": 200}), ("i4", dc(.4, SR * 4), {})]
    e = moteur([], items=items, mode="playlist", ab=True)
    e.pl_go(); rendre(e, 20)
    e.pl_go(); o = rendre(e, 20)
    controle("laisser jouer : au GO, A (i1) continue de jouer pendant que B (i2) part",
             etat(e, 1) == "playing" and e.players[0].item["id"] == "i1" and etat(e, 2) == "playing"
             and e.players[1].item["id"] == "i2" and float(np.min(o[0][:, 0])) > .099,
             str([(p.state, (p.item or {}).get("id")) for p in e.players]))
    controle("laisser jouer : l'élément suivant attend que A se libère (pending_cue), rien n'est perdu",
             e.players[0].pending_cue == "i3" and e.pl_ready == "i3")
    e.player_stop(1, if_played=True)       # fader start : fermeture du fader de A
    rendre(e, 2)
    controle("laisser jouer : A arrêté par l'opérateur → il tient aussitôt l'élément suivant prêt",
             etat(e, 1) == "cued" and e.players[0].item["id"] == "i3" and etat(e, 2) == "playing")
    e.pl_go(); rendre(e, 20)
    controle("laisser jouer : GO suivant → A lance i3, B (i2) joue toujours, B attendra i4",
             etat(e, 1) == "playing" and e.players[0].item["id"] == "i3" and etat(e, 2) == "playing"
             and e.players[1].item["id"] == "i2" and e.players[1].pending_cue == "i4")
    # Les deux lecteurs jouent encore : le GO prend celui qui n'est PAS à l'antenne (B, i2) ; son son
    # s'efface en queue avec son propre fondu (0 ms → anti-clic), le lecteur est libre aussitôt.
    r = e.pl_go()
    o = rendre(e, 30)
    controle("deux lecteurs occupés : le GO n'est pas refusé, il prend le lecteur qui n'est pas à l'antenne",
             r.get("player") == 2 and e.players[1].item["id"] == "i4" and etat(e, 2) == "playing"
             and etat(e, 1) == "playing" and e.players[0].item["id"] == "i3", str(r))
    controle("deux lecteurs occupés : l'ancien son s'efface sans clic (queue), le nouveau sort au bon niveau",
             abs(float(o[1][0, 0]) - .6) < .21 and abs(float(o[1][-1, 0]) - .4) < 1e-6,
             "%r %r" % (float(o[1][0, 0]), float(o[1][-1, 0])))
    # Puis A (i3, fondu propre 200 ms) n'est plus à l'antenne : un GO de plus le reprend, sa queue dure 200 ms.
    e.pl_options({"loop": True})
    e.pl_go()
    o = rendre(e, 300)
    queue = o[0][:, 0] - .1                    # i1 repart sur A (liste bouclée) ; ce qui dépasse = la queue de i3
    fin = int(np.nonzero(queue > 1e-6)[0][-1]) if (queue > 1e-6).any() else 0
    controle("deux lecteurs occupés : la queue de l'ancien son dure SON fondu (200 ms ± 1 bloc)",
             abs(fin - SR * 200 // 1000) <= BLK, "fin à %d échantillons" % fin)

with scenario("passage A/B « laisser jouer » : choisir un autre élément pendant que les deux jouent"):
    items = [("i1", dc(.1, SR * 4), {"fade_out_ms": 0}), ("i2", dc(.2, SR * 4), {"fade_out_ms": 0}),
             ("i3", dc(.3, SR * 4), {})]
    e = moteur([], items=items, mode="playlist", ab=True)
    e.pl_go(); e.pl_go(); rendre(e, 5)
    e.pl_goto(0)                           # toucher la ligne 1 : elle devient prête
    e.player_stop(1, if_played=True); rendre(e, 2)
    controle("laisser jouer : l'élément choisi pendant que les deux jouent est celui que A prépare en se libérant",
             etat(e, 1) == "cued" and e.players[0].item["id"] == "i1" and e.pl_ready == "i1",
             str([(p.state, (p.item or {}).get("id"), p.pending_cue) for p in e.players]))

with scenario("passage A/B « laisser jouer » + enchaînement auto : la fin de l'ancien ne lance rien"):
    items = [("i1", dc(.1, SR // 2), {}), ("i2", dc(.2, SR * 4), {}), ("i3", dc(.3, SR * 4), {})]
    e = moteur([], items=items, mode="playlist", ab=True, auto=True)
    e.pl_go(); rendre(e, 100)
    e.pl_go(); rendre(e, 500)               # i1 (0,5 s) finit sur A pendant que i2 est à l'antenne sur B
    controle("auto + laisser jouer : la fin de l'élément laissé jouer ne déclenche pas de GO ; A prépare i3",
             e.pl_on_air == "i2" and etat(e, 2) == "playing" and etat(e, 1) == "cued"
             and e.players[0].item["id"] == "i3",
             str([(p.state, (p.item or {}).get("id")) for p in e.players]))

with scenario("passage A/B « couper » et « fondu de X ms »"):
    items = [("i1", dc(.5, SR * 4), {"fade_out_ms": 2000}), ("i2", dc(.2, SR * 4), {"fade_out_ms": 2000}),
             ("i3", dc(.3, SR * 4), {})]
    e = moteur([], items=items, mode="playlist", ab=True, handover="cut")
    e.pl_go(); rendre(e, 20)
    e.pl_go(); o = rendre(e, 20)
    controle("couper : au GO, A est coupé aussitôt (anti-clic 5 ms, pas son fondu de 2 s) et tient i3",
             etat(e, 1) == "cued" and e.players[0].item["id"] == "i3" and etat(e, 2) == "playing"
             and not o[0][SR * 6 // 1000:].any(), str([(p.state, (p.item or {}).get("id")) for p in e.players]))
    e = moteur([], items=items, mode="playlist", ab=True, handover="fade", handover_ms=300)
    e.pl_go(); rendre(e, 20)
    e.pl_go()
    o = rendre(e, 400)
    a = o[0][:, 0]
    fin = int(np.nonzero(a > 1e-6)[0][-1]) if (a > 1e-6).any() else 0
    controle("fondu : A s'efface sur la durée RÉGLÉE (300 ms ± 1 bloc), pas sur son fondu propre (2 s)",
             abs(fin - SR * 300 // 1000) <= BLK and float(a[SR * 150 // 1000]) < .5 and float(a[SR * 150 // 1000]) > .01,
             "fin à %d échantillons" % fin)
    controle("fondu : fini, A tient l'élément suivant prêt ; B joue",
             etat(e, 1) == "cued" and e.players[0].item["id"] == "i3" and etat(e, 2) == "playing")
    e = moteur([], items=items, mode="playlist", ab=False, handover="keep")
    e.pl_go(); rendre(e, 20); e.pl_go(); rendre(e, 20)
    controle("hors A/B : le réglage ne s'applique pas, l'ancien part avec son fondu propre",
             any(p.state == "fading" and p.item["id"] == "i1" for p in e.players))

# ── 2 bis. Défauts des touches (0.1.2) : héritage, surcharge, retour au défaut, migration ─────
def moteur2(keys, n_players=2, items=None, defaults=None, page_defaults=None):
    """Document de version 2 : ce que porte la touche est pris tel quel (None = hérité)."""
    srcs, dk, di = {}, [], []
    for i, (kid, src, extra) in enumerate(keys):
        srcs[kid] = src
        dk.append(dict({"id": kid, "slot": i, "title": kid.upper(), "media": {"asset": kid, "file": ""}}, **extra))
    for iid, src, extra in items or []:
        srcs[iid] = src
        di.append(dict({"id": iid, "title": iid.upper(), "media": {"asset": iid, "file": ""}}, **extra))
    doc = {"version": 2, "pages": [{"id": "p1", "keys": dk, "defaults": page_defaults or {}}],
           "playlist": {"items": di, "ab": False}, "defaults": defaults or {}}
    return E.CartEngine(n_players, SR, doc, source_for=lambda m: srcs.get(m.get("asset")))


with scenario("défauts : une touche sans valeur propre hérite du cartouchier"):
    e = moteur2([("a", dc(.5, long_), {}), ("b", dc(.5, long_), {"fade_out_ms": 200, "second_press": "pause"}),
                 ("c", dc(.5, long_), {})])
    e.set_defaults({"fade_out_ms": 300, "second_press": "restart"})
    ea, eb = e.effective(e.find_key("a")), e.effective(e.find_key("b"))
    controle("défauts : une touche sans valeur propre hérite du cartouchier",
             ea["fade_out_ms"] == 300 and ea["second_press"] == "restart"
             and e.find_key("a")["fade_out_ms"] is None, json.dumps(ea))
    controle("défauts : un défaut changé ne touche PAS une touche qui le surcharge",
             eb["fade_out_ms"] == 200 and eb["second_press"] == "pause", json.dumps(eb))
    e.fire("a")
    rendre(e, 20)
    e.fire("a")
    controle("défauts : le comportement hérité est celui qui JOUE (deuxième appui « relance » par défaut)",
             etat(e, 1) == "playing" and e.players[0].pos == 0)
    e.set_key({"page": "p1", "key": {"id": "b", "fade_out_ms": None}})
    controle("défauts : « revenir au défaut » (valeur null) rend la touche héritière",
             e.find_key("b")["fade_out_ms"] is None and e.effective(e.find_key("b"))["fade_out_ms"] == 300
             and e.find_key("b")["second_press"] == "pause")
    e.set_defaults({"fade_out_ms": None})
    controle("défauts : null au niveau du cartouchier = défaut d'usine",
             e.doc["defaults"]["fade_out_ms"] == E.DEFAULT_FIELDS["fade_out_ms"])

with scenario("défauts : le fondu d'arrêt changé s'applique à un son DÉJÀ lancé"):
    e = moteur2([("a", dc(.5, long_), {}), ("b", dc(.5, long_), {"fade_out_ms": 100})])
    e.fire("a")
    e.fire("b")
    rendre(e, 5)
    e.set_defaults({"fade_out_ms": 1000, "loop": True})
    e.stop_key("a")
    e.stop_key("b")
    fa = e.player_view(e.players[0])["fade_remaining_ms"]
    fb = e.player_view(e.players[1])["fade_remaining_ms"]
    controle("défauts : le fondu d'arrêt changé s'applique à un son DÉJÀ lancé",
             etat(e, 1) == "fading" and abs(fa - 1000) <= 2, "fondu restant : %s ms" % fa)
    controle("défauts : … mais pas à une touche qui a son propre fondu", abs(fb - 100) <= 2, "fondu restant : %s ms" % fb)
    e2 = moteur2([("a", dc(.5, 1000), {})])
    e2.fire("a")
    e2.set_defaults({"loop": True})
    rendre(e2, 30)
    controle("défauts : la boucle d'un son lancé est figée au lancement (un réglage ne déplace pas un son qui joue)",
             etat(e2, 1) == "idle")
    e2.fire("a")
    rendre(e2, 30)
    controle("défauts : la boucle par défaut s'applique au lancement suivant", etat(e2, 1) == "playing")

with scenario("défauts de page : la page passe avant le cartouchier, la touche avant la page"):
    e = moteur2([("a", dc(.5, long_), {}), ("b", dc(.5, long_), {"group": ""}), ("c", dc(.5, long_), {})],
                defaults={"group": "g", "player": 0})
    e.fire("a")
    e.fire("c")
    controle("défauts : le groupe d'exclusivité par défaut rend exclusives les touches qui en héritent",
             e.holder("a") is None and e.holder("c") is not None)
    e.fire("b")
    controle("défauts : une touche au groupe VIDE explicite (surcharge) n'est pas exclusive",
             e.holder("b") is not None and e.holder("c") is not None)
    e.stop_all(cut=True)
    e.set_defaults({"page": "p1", "player": 2, "fade_out_ms": 50})
    r = e.fire("a")
    controle("défauts de page : la page passe avant le cartouchier (lecteur 2)", r["player"] == 2)
    controle("défauts de page : fondu de la page (50 ms), le cartouchier inchangé",
             e.effective(e.find_key("a"))["fade_out_ms"] == 50 and e.doc["defaults"]["fade_out_ms"] == 0)
    e.set_defaults({"page": "p1", "player": None})
    controle("défauts de page : null rend le champ au cartouchier",
             Z0(e.doc)["pages"][0]["defaults"] == {"fade_out_ms": 50}
             and e.effective(e.find_key("a"))["player"] == 0)
    ei = moteur2([], items=[("i1", dc(.5, SR), {})], defaults={"fade_out_ms": 400})
    controle("playlist : un élément hérite du fondu d'arrêt du cartouchier",
             ei.effective(ei._items(Z0(ei.doc))[0], "playlist")["fade_out_ms"] == 400)

with scenario("migration : document de version 1"):
    v1 = {"version": 1, "pages": [{"id": "p1", "keys": [
        {"id": "x", "slot": 0, "loop": False, "fade_out_ms": 0, "second_press": "stop", "group": "", "player": None},
        {"id": "y", "slot": 1, "loop": True, "fade_out_ms": 400, "second_press": "pause", "group": "fx", "player": 2}]}],
        "playlist": {"items": [{"id": "i1", "fade_out_ms": 0}, {"id": "i2", "fade_out_ms": 700}]}}
    d = E.normalize_doc(v1, 2)
    kx, ky = Z0(d)["pages"][0]["keys"]
    controle("migration : une valeur ÉGALE à l'ancien défaut implicite redevient héritée",
             all(kx[f] is None for f in E.KEY_INHERITED), json.dumps(kx))
    controle("migration : une valeur différente reste une surcharge",
             ky["loop"] is True and ky["fade_out_ms"] == 400 and ky["second_press"] == "pause"
             and ky["group"] == "fx" and ky["player"] == 2, json.dumps(ky))
    controle("migration : éléments de playlist (fondu 0 → hérité, 700 gardé)",
             [it["fade_out_ms"] for it in Z0(d)["playlist"]["items"]] == [None, 700])
    controle("migration : le document passe en version courante avec les défauts d'usine",
             d["version"] == E.DOC_VERSION and d["defaults"] == E.DEFAULT_FIELDS and Z0(d)["pages"][0]["defaults"] == {})
    d2 = copy.deepcopy(d)
    Z0(d2)["pages"][0]["keys"][0]["second_press"] = "stop"
    d2 = E.normalize_doc(d2, 2)
    controle("migration : après migration, une valeur explicite égale au défaut RESTE une surcharge (pas de re-migration)",
             Z0(d2)["pages"][0]["keys"][0]["second_press"] == "stop")
    controle("migration : idempotente", E.normalize_doc(d, 2) == d)

with scenario("affectation directe : remplacer le son garde les réglages de la touche"):
    e = moteur2([("old", dc(.5, long_), {"fade_out_ms": 500, "group": "g", "loop": True, "player": 2,
                                          "gain_db": 3.0, "color": "red", "intro_ms": 1200})])
    avant = json.loads(json.dumps(e.find_key("old")))
    r = e.assign_media({"page": "p1", "slot": 0, "media": {"asset": "neuf", "file": ""}, "title": "Neuf"})
    k = e.find_key(r["key_id"])
    garde = {f: k[f] for f in ("fade_out_ms", "group", "loop", "player", "gain_db", "color", "intro_ms", "slot")}
    controle("affectation directe : remplacer le son garde les réglages de la touche (même id)",
             r["key_id"] == "old" and k["media"]["asset"] == "neuf" and k["title"] == "Neuf"
             and garde == {f: avant[f] for f in garde}, json.dumps(k))
    controle("affectation directe : la réponse rend la touche d'avant (pour « Annuler »)",
             (r.get("previous") or {}).get("media", {}).get("asset") == "old")
    e.set_key({"page": "p1", "slot": r["previous"]["slot"], "key": r["previous"]})
    controle("affectation directe : « Annuler » rend le son et le titre d'avant",
             e.find_key("old")["media"]["asset"] == "old" and e.find_key("old")["title"] == "OLD")
    r2 = e.assign_media({"page": "p1", "slot": 7, "media": {"asset": "neuf"}, "title": "Neuf"})
    k2 = e.find_key(r2["key_id"])
    controle("affectation directe : un emplacement vide reçoit une NOUVELLE touche, tout hérité",
             k2 is not None and k2["slot"] == 7 and r2["previous"] is None
             and all(k2[f] is None for f in E.KEY_INHERITED), json.dumps(k2))
    controle("affectation directe : sans média, refusée (code)",
             refus(lambda: e.assign_media({"page": "p1", "slot": 2, "media": {}}), "no_media"))
    ep = moteur2([], items=[("i1", dc(.5, SR), {}), ("i2", dc(.5, SR), {})])
    rp = ep.playlist_op({"op": "add", "at": 1, "items": [{"media": {"asset": "neuf"}, "title": "Neuf"}]})
    controle("affectation directe : insérer dans la playlist à une POSITION (ids des ajouts rendus)",
             [it["id"] for it in ep._items(Z0(ep.doc))] == ["i1", (rp.get("added") or [None])[0], "i2"])

# ── 3. R128 ──────────────────────────────────────────────────────────────────────────────────
with scenario('R128 : sinus stéréo 997 Hz à −23 dBFS crête = −23,0 LUFS (±0,05)'):
    ref = sine(-23.0, 10)
    m = L.measure(lambda p, n: ref[p:p + n], len(ref))
    controle("R128 : sinus stéréo 997 Hz à −23 dBFS crête = −23,0 LUFS (±0,05)",
             m["lufs"] is not None and abs(m["lufs"] + 23.0) <= .05, str(m))
    g = sine(0.0, 10, left_only=True)
    m0 = L.measure(lambda p, n: g[p:p + n], len(g))
    controle("R128 : sinus 0 dBFS sur la gauche seule = −3,01 LUFS (point d'étalonnage BS.1770)",
             m0["lufs"] is not None and abs(m0["lufs"] + 3.01) <= .05, str(m0))
    sil = np.concatenate([ref, np.zeros((SR * 20, 2), np.float32), ref])
    ms = L.measure(lambda p, n: sil[p:p + n], len(sil))
    controle("R128 : le silence est écarté par les portes (même sonie avec 20 s de silence)",
             ms["lufs"] is not None and abs(ms["lufs"] - m["lufs"]) <= .1, str(ms))
    controle("R128 : silence complet = non mesurable (None), jamais −inf",
             L.measure(lambda p, n: np.zeros((n, 2), np.float32), SR * 2)["lufs"] is None)
    controle("normalisation : −30 LUFS vers −23 = +7 dB", L.normalization_gain({"lufs": -30.0, "peak_dbfs": -20.0})[0] == 7.0)
    controle("normalisation : une amplification est plafonnée par le gain maximal",
             L.normalization_gain({"lufs": -50.0, "peak_dbfs": -40.0}, max_gain_db=12) == (12.0, "max_gain"))
    controle("normalisation : une amplification ne fait pas écrêter",
             L.normalization_gain({"lufs": -30.0, "peak_dbfs": -3.0}) == (2.0, "peak"))
    controle("normalisation : une atténuation n'est jamais plafonnée",
             L.normalization_gain({"lufs": -10.0, "peak_dbfs": 0.0})[0] == -13.0)

with scenario('normalisation de bout en bout : ce qui sort du lecteur est à la cible (−23 LUFS)'):
    src30 = sine(-30.0, 6)
    meas30 = L.measure(lambda p, n: src30[p:p + n], len(src30))
    e = moteur([("a", E.ArraySource(src30), {}), ("b", E.ArraySource(src30), {"gain_db": 3.0})],
               norm=lambda md: L.normalization_gain(meas30)[0])
    e.fire("a")
    e.fire("b")
    o = rendre(e, 5000)
    la = L.measure(lambda p, n: o[0][p:p + n], len(o[0]))["lufs"]
    lb = L.measure(lambda p, n: o[1][p:p + n], len(o[1]))["lufs"]
    controle("normalisation de bout en bout : ce qui sort du lecteur est à la cible (−23 LUFS)",
             la is not None and abs(la + 23.0) <= .1, "mesuré en sortie : %s" % la)
    controle("gain de touche AJOUTÉ à la normalisation (+3 dB → −20 LUFS)",
             lb is not None and abs(lb + 20.0) <= .1, "mesuré en sortie : %s" % lb)

# ── 4 bis. Éditeur de clip et pré-écoute (0.1.3) ──────────────────────────────────────────────
# La liste de montage est rendue par le MOTEUR en lecture : chaque loi de gain (montage.py) est
# recalculée ici échantillon par échantillon, indépendamment du code, et comparée à ce que sort
# un LECTEUR (blocs de 1 ms, comme le fil audio) — donc indépendante de la taille de bloc.
from cart import montage as MT  # noqa: E402
from cart import listen as LI  # noqa: E402

TOL = 2e-7                                   # < pas de la rampe (1e-6) / 2 : un échantillon décalé se voit


def rampe(n, pas=1e-6):
    return E.ArraySource(np.repeat((np.arange(n) * pas).astype(np.float32)[:, None], 2, axis=1))


def forme(nom, x):
    import math
    return {"lin": x, "eq": math.sin(x * math.pi / 2), "s": 0.5 * (1 - math.cos(x * math.pi)), "exp": x * x}[nom]


def joue(srcs, edit, gain_db=0.0, norm=None, ms=None):
    """Sortie du lecteur 1 pour une touche portant `edit` (rendue en blocs de 1 ms)."""
    doc = {"pages": [{"id": "p1", "keys": [{"id": "k", "slot": 0, "media": {"asset": "a"}, "edit": edit,
                                            "gain_db": gain_db}]}]}
    e = E.CartEngine(2, SR, doc, source_for=lambda m: srcs.get(m.get("asset")),
                     norm_db_for=norm or (lambda m: 0.0))
    n = e.source_of(e.find_key("k")).frames
    e.fire("k")
    out = rendre(e, ms or (n // BLK + 3))[0][:, 0]
    return e, n, out


with scenario("montage : In / Out à l'échantillon"):
    A = rampe(SR)
    e, n, o = joue({"a": A}, {"segments": [{"in_ms": 100, "out_ms": 500}]})
    ref = A.pcm[4800:24000, 0]
    controle("montage : In / Out — durée montée exacte (400 ms = 19200 échantillons)", n == 19200, str(n))
    controle("montage : In / Out — premier échantillon = celui de In, dernier = celui d'avant Out",
             np.allclose(o[:n], ref, atol=TOL, rtol=0), "écart max %.3g" % float(np.abs(o[:n] - ref).max()))
    controle("montage : silence après Out (le lecteur est libéré)", not o[n:].any() and etat(e, 1) == "idle")

with scenario("montage : fondus d'entrée et de sortie (durée, forme)"):
    A = E.ArraySource(np.full((SR, 2), 0.5, np.float32))
    e, n, o = joue({"a": A}, {"segments": [{}], "fade_in_ms": 10, "fade_in_shape": "eq",
                              "fade_out_ms": 20, "fade_out_shape": "s"})
    fi, fo = 480, 960
    ref = np.array([0.5 * (forme("eq", k / fi) if k < fi else 1.0)
                    * (forme("s", 1 - (k - (n - fo) + 1) / fo) if k >= n - fo else 1.0) for k in range(n)])
    controle("montage : fondus — loi exacte à chaque échantillon (entrée eq, sortie s)",
             np.allclose(o[:n], ref, atol=TOL, rtol=0), "écart max %.3g" % float(np.abs(o[:n] - ref).max()))
    controle("montage : fondus — premier et dernier échantillons muets", o[0] == 0 and abs(o[n - 1]) < 1e-7)

with scenario("montage : coupe avec micro-fondu au raccord"):
    A = rampe(SR)
    e, n, o = joue({"a": A}, {"segments": [{"in_ms": 0, "out_ms": 1000, "cuts": [[200, 300]]}]})
    xf = 480                                 # 10 ms : la valeur décidée, pas relue dans le module testé
    ref = np.concatenate([A.pcm[:9600 - xf, 0],
                          np.array([A.pcm[9600 - xf + j, 0] * (1 - j / xf) + A.pcm[14400 + j, 0] * (j / xf)
                                    for j in range(xf)]),
                          A.pcm[14400 + xf:48000, 0]])
    controle("montage : coupe — durée = 1 s − 100 ms − micro-fondu (%d éch.)" % xf, n == 48000 - 4800 - xf, str(n))
    controle("montage : coupe — passage retiré, raccord en fondu enchaîné linéaire de 10 ms",
             n == ref.shape[0] and np.allclose(o[:n], ref, atol=TOL, rtol=0),
             "écart max %.3g" % (float(np.abs(o[:n] - ref).max()) if n == ref.shape[0] else -1))

with scenario("montage : enveloppe de volume"):
    A = E.ArraySource(np.full((SR, 2), 0.5, np.float32))
    e, n, o = joue({"a": A}, {"segments": [{"out_ms": 300}], "envelope": [[50, 0], [150, -12], [250, -12]]})

    def db_at(k):
        t = [2400, 7200, 12000]
        d = [0.0, -12.0, -12.0]
        if k <= t[0]:
            return d[0]
        if k >= t[-1]:
            return d[-1]
        i = 1 if k < t[1] else 2
        return d[i - 1] + (d[i] - d[i - 1]) * (k - t[i - 1]) / (t[i] - t[i - 1])
    ref = np.array([0.5 * 10 ** (db_at(k) / 20) for k in range(n)])
    controle("montage : enveloppe — linéaire en dB entre les points, plate avant / après",
             np.allclose(o[:n], ref, atol=TOL, rtol=1e-5), "écart max %.3g" % float(np.abs(o[:n] - ref).max()))

with scenario("montage : assemblage avec fondu enchaîné, normalisation par son, gain du clip"):
    A, B = rampe(SR), E.ArraySource(np.full((SR // 2, 2), 0.25, np.float32))
    norm = {"a": 2.0, "b": 6.0}
    e, n, o = joue({"a": A, "b": B}, {"segments": [{"out_ms": 500},
                                                   {"media": {"asset": "b"}, "xfade_ms": 50, "xfade_shape": "eq"}]},
                   gain_db=-6.0, norm=lambda m: norm[m.get("asset")])
    xf, g, ga, gb = 2400, 10 ** (-6 / 20), 10 ** (2 / 20), 10 ** (6 / 20)
    ref = np.concatenate([A.pcm[:24000 - xf, 0] * ga,
                          np.array([A.pcm[24000 - xf + j, 0] * ga * forme("eq", 1 - j / xf) + 0.25 * gb * forme("eq", j / xf)
                                    for j in range(xf)]),
                          np.full(24000 - xf, 0.25 * gb)]) * g
    controle("montage : assemblage — durée = somme − fondu enchaîné", n == 24000 + 24000 - xf, str(n))
    controle("montage : assemblage — fondu enchaîné eq, chaque son à SA normalisation (+2 / +6 dB), gain du clip (−6 dB) sur tout",
             n == ref.shape[0] and np.allclose(o[:n], ref, atol=TOL, rtol=1e-5),
             "écart max %.3g" % (float(np.abs(o[:n] - ref).max()) if n == ref.shape[0] else -1))
    e.fire("k")
    controle("montage : le gain du lecteur est celui du clip (la normalisation est par son)",
             e.players_view()[0]["gain_db"] == -6.0, str(e.players_view()[0]["gain_db"]))
    m1 = e.source_of(e.find_key("k"))
    tout = np.concatenate([m1.read(p, 4800)[:, 0].copy() for p in range(0, m1.frames, 4800)]) * g
    controle("montage : rendu indépendant de la taille de bloc (1 ms dans le lecteur = 100 ms d'un bloc)",
             np.allclose(tout, o[:n], atol=TOL, rtol=1e-5))
    controle("montage : un son du montage pas encore décodé est refusé (pas de silence)",
             refus(lambda: E.CartEngine(1, SR, {"pages": [{"id": "p", "keys": [{"id": "k", "slot": 0, "media": {"asset": "a"},
                   "edit": {"segments": [{}, {"media": {"asset": "x"}}]}}]}]},
                   source_for=lambda m: A if m.get("asset") == "a" else None).fire("k"), "media_not_ready"))
    controle("montage : une liste qui ne change rien est rangée à None (lecture directe)",
             E.normalize_item({"edit": {"segments": [{}]}}, 2)["edit"] is None)

with scenario("pré-écoute : hors antenne, aucun lecteur touché"):
    A = rampe(SR)
    doc = {"pages": [{"id": "p1", "keys": [{"id": "k", "slot": 0, "media": {"asset": "a"}}]}]}
    e = E.CartEngine(2, SR, doc, source_for=lambda m: {"a": A}.get(m.get("asset")))
    e.preview({"key": "k", "start_ms": 100, "end_ms": 200})
    sortie = rendre(e, 50)
    pfl = []
    e2 = E.CartEngine(2, SR, doc, source_for=lambda m: {"a": A}.get(m.get("asset")))
    e2.preview({"key": "k", "start_ms": 100, "end_ms": 200})
    for _ in range(150):
        e2.render(BLK)
        pfl.append(e2.pfl_out[:, 0].copy() if e2.pfl_out is not None else np.zeros(BLK, np.float32))
    pfl = np.concatenate(pfl)
    controle("pré-écoute : les lecteurs restent libres et muets (aucun flux MXL ne la porte)",
             all(p.state == "idle" for p in e.players) and not any(s.any() for s in sortie))
    controle("pré-écoute : sa sortie est le son, de start_ms à end_ms, puis plus rien",
             np.allclose(pfl[240:4800], A.pcm[4800 + 240:9600, 0], atol=TOL, rtol=0) and not pfl[4800 + 240:].any()
             and e2.pfl.state == "idle", "fin %s" % e2.pfl.state)
    controle("pré-écoute : n'a pas compté de lancement ni d'événement de lecteur",
             e.counters["fired"] == 0 and not e.events)
    e.preview({"key": "k"})
    e.render(BLK)
    hub = LI.ListenHub(2, on_last_pfl=e.preview_stop)
    cp, c1 = hub.open("pfl"), hub.open(1)
    for _ in range(20):
        hub.push(e.render(BLK), e.pfl_out)
    dp, d1 = cp.get(0), c1.get(0)
    controle("écoute : 10 ms par morceau (int16 stéréo), la pré-écoute entendue, le lecteur 1 muet",
             dp is not None and len(dp) == 1920 and any(dp) and d1 is not None and not any(d1),
             "pfl=%s l1=%s" % (dp and len(dp), d1 and any(d1)))
    cp.close()
    controle("écoute : le dernier navigateur en pré-écoute parti, la pré-écoute s'arrête", e.pfl.state == "idle")
    controle("écoute : source inconnue refusée", LI.ListenHub.parse_src("9", 2) is None
             and LI.ListenHub.parse_src("pfl", 2) == "pfl" and LI.ListenHub.parse_src("2", 2) == 2)

with scenario("forme d'onde : fenêtre à la demande"):
    n = SR * 4
    x = np.zeros((n, 2), np.float32)
    x[100000] = [0.0, -0.8]
    lv = MT.peaks_level(lambda p, k: x[p:p + k], n)
    large = MT.window(lambda p, k: x[p:p + k], n, 0, n, 400, lv)
    etroit = MT.window(lambda p, k: x[p:p + k], n, 99950, 100050, 100, lv)
    controle("forme d'onde : vue large par le niveau grossier — un seul seau porte la crête, au bon endroit",
             len(large) == 400 and large.count(80) == 1 and large.index(80) == 100000 * 400 // n
             and sum(1 for v in large if v) == 1, str([i for i, v in enumerate(large) if v]))
    controle("forme d'onde : vue étroite sur les échantillons — la crête dans le seau 50 sur 100",
             etroit.index(80) == 50 and sum(1 for v in etroit if v) == 1, str([i for i, v in enumerate(etroit) if v]))


# ── 4. /flows, actions de macro, persistance — par le script RENDU ──────────────────────────
_media = tempfile.mkdtemp(prefix="verif_cart_media_")


def charger_script(cart=None, players=None):
    cfg = dict(params, cache_dir=os.path.join(_tmp, "cache"), media_root=_media)
    if players:
        cfg["players"] = players
    if cart is not None:
        cfg["cart"] = cart
    src = script.format(config=repr(cfg), hostname=HOTE, plugin_version=manifeste["version"])
    mod = types.ModuleType("cart_script")
    mod.__file__ = os.path.join(_tmp, "main.py")
    exec(compile(src, "<cartouchier>", "exec"), mod.__dict__)
    srcs = {"j1": dc(.5, SR * 2), "j2": dc(.4, SR * 3)}
    mod.engine.source_for = lambda md: srcs.get(md.get("asset"))
    return mod


ok_charge = True
try:
    S = charger_script()
except Exception as ex:
    ok_charge = False
    controle("le script rendu se charge sans MXL (bobimxl importé dans le fil audio seul)", False, repr(ex))
if ok_charge:
    with scenario("/flows : une entrée par flux produit, lecteurs libres compris"):
        controle("les noms de flux du script = ceux de wiring.produces",
                 S.FLOWS == ["%s_player_%d" % (HOTE, i) for i in (1, 2)], str(S.FLOWS))
        r1 = S._post("/key", {"page": "p1", "slot": 0, "key": {"title": "Jingle", "media": {"asset": "j1"},
                                                              "intro_ms": 500, "outro_ms": 1500}})
        r2 = S._post("/key", {"page": "p1", "slot": 1, "key": {"title": "Tapis", "media": {"asset": "j2"}}})
        k1 = r1["key_id"]
        k2 = r2["key_id"]
        S._post("/fire", {"key": k1})
        for _ in range(250):
            S.engine.render(BLK)
        code, flows = S._get("/flows", {})
        f1 = flows.get(S.FLOWS[0]) or {}
        champs = {"title", "remaining_ms", "elapsed_ms", "duration_ms", "intro_ms", "outro_ms", "state", "player"}
        controle("/flows : une entrée par flux produit, lecteurs libres compris",
                 code == 200 and sorted(flows) == sorted(S.FLOWS), str(sorted(flows)))
        controle("/flows : les champs du contrat sont tous là", champs <= set(f1), str(sorted(f1)))
        controle("/flows : titre, état, lecteur, temps cohérents",
                 f1.get("title") == "Jingle" and f1.get("state") == "playing" and f1.get("player") == 1
                 and f1.get("elapsed_ms") == 250 and f1.get("duration_ms") == 2000
                 and f1.get("remaining_ms") == 1750 and f1.get("intro_ms") == 500
                 and f1.get("outro_ms") == 1500, json.dumps(f1))
        controle("/flows : décompte vers l'intro et vers la fin",
                 f1.get("intro_remaining_ms") == 250 and f1.get("outro_remaining_ms") == 1250, json.dumps(f1))
        controle("/flows : un lecteur libre est publié « idle »",
                 (flows.get(S.FLOWS[1]) or {}).get("state") == "idle")

        # Chaque action déclarée est SERVIE par le script (aucune ne tombe dans « inconnu »).
        exemples = {"key": k2, "page": "p1", "index": 0, "player": 1, "play": False}
        non_servies = []
        for a in manifeste.get("actions") or []:
            if a.get("core"):
                continue                         # action CŒUR (rappel) : servie par l'orchestrateur, cf. 6.
            corps = dict(a.get("body") or {})
            for p in a.get("params") or []:
                corps.setdefault(p["key"], exemples.get(p["key"]))
            if a["endpoint"].startswith("/pl/") and a["id"] != "ab_on" and a["id"] != "ab_off":
                S._post("/playlist", {"op": "add", "items": [{"title": "x", "media": {"asset": "j1"}}]})
            try:
                r = S._post(a["endpoint"], corps)
            except S.E.EngineError:
                r = {"refused": True}            # servi, refus métier lisible : c'est une réponse
            if r is None:
                non_servies.append(a["id"])
        controle("chaque action de macro est servie par le conteneur", not non_servies, str(non_servies))
        S._post("/mode", {"mode": "grid"})
        vides = []
        for oe in sorted({p.get("options_endpoint") for a in manifeste["actions"] for p in a.get("params") or []
                          if p.get("options_endpoint")}):
            c, d = S._get(oe, {})
            if c != 200 or not d.get("items") or not all("value" in x and "label" in x for x in d["items"]):
                vides.append(oe)
        controle("chaque liste vivante rend {items: [{value, label}]}", not vides, str(vides))
        etat_ = S.state_view()
        illisibles = []
        for st in manifeste.get("state") or []:
            v = etat_
            for part in st["path"].split("."):
                if isinstance(v, dict) and part in v:
                    v = v[part]
                elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
                    v = v[int(part)]
                else:
                    v = KeyError
                    break
            if v is KeyError:
                illisibles.append(st["id"])
        controle("chaque état déclaré se lit dans /state", not illisibles, str(illisibles))
        illisibles = [g.get("path_prefix") + "." + f
                      for g in (manifeste.get("param_tree") or {}).get("global_groups") or []
                      for f in g.get("fields") or {}
                      if f not in (etat_.get(g.get("path_prefix")) or {})]
        controle("chaque paramètre du param_tree se lit dans /state (sous son path_prefix)",
                 not illisibles, str(illisibles))
        groupes = {g.get("endpoint") for g in (manifeste.get("param_tree") or {}).get("global_groups") or []}
        controle("les défauts des touches sont pilotables par les macros (param_tree /defaults)",
                 "/defaults" in groupes, str(groupes))
        g_ab = next((g for g in (manifeste.get("param_tree") or {}).get("global_groups") or []
                     if g.get("endpoint") == "/pl/options"), {})
        f_ab = g_ab.get("fields") or {}
        _r = S._post("/pl/options", {"ab_handover": "fade", "ab_handover_ms": 700})
        pl_st = S.state_view()["playlist"]
        controle("passage A/B pilotable par les macros (param_tree /pl/options : mode keep|cut|fade, durée bornée) et lisible dans /state",
                 (f_ab.get("ab_handover") or {}).get("options") == ["keep", "cut", "fade"]
                 and (f_ab.get("ab_handover_ms") or {}).get("max") == E.AB_HANDOVER_MS_MAX
                 and pl_st.get("ab_handover") == "fade" and pl_st.get("ab_handover_ms") == 700
                 and {"ab_handover", "ab_handover_ms"} <= {st["id"] for st in manifeste.get("state") or []},
                 str(pl_st))
        S._post("/pl/options", {"ab_handover": "keep"})

        # Affectation directe (glisser / toucher-toucher) : persistée tout de suite, comme tout
        # réglage — le chemin est déclaré dans control.persist.settings, sinon l'orchestrateur ne
        # relit pas l'instantané et la touche est perdue au premier redémarrage.
        persist_ = ctrl.get("persist") or {}
        for ep_ in ("/assign", "/defaults"):
            controle("%s est un chemin déclaré ET persisté (control.persist.settings)" % ep_,
                     ep_ in eps and ep_ in (persist_.get("settings") or []))
        ra = S._post("/assign", {"page": "p1", "slot": 5, "media": {"asset": "j2"}, "title": "Direct"})
        S._post("/defaults", {"fade_out_ms": 750, "second_press": "restart"})
        S._post("/defaults", {"page": "p1", "loop": True})
        rp = S._post("/playlist", {"op": "add", "at": 0, "items": [{"media": {"asset": "j1"}, "title": "Tête"}]})

        # Persistance : ce que lit control.persist (GET /doc) redonne le même cartouchier.
        S._post("/settings", {"target_lufs": -18})
        code, doc = S._get("/doc", {})
        S2 = charger_script(doc)
        controle("persistance : le document relu redonne touches et réglages après redéploiement",
                 S2.engine.find_key(k1) is not None and S2.engine.find_key(k1)["title"] == "Jingle"
                 and S2.engine.doc["settings"]["target_lufs"] == -18.0)
        kd = S2.engine.find_key((ra.get("doc") or ra).get("key_id", "") if isinstance(ra, dict) else "")
        controle("persistance : une affectation directe survit au redéploiement",
                 kd is not None and kd["media"]["asset"] == "j2" and kd["title"] == "Direct" and kd["slot"] == 5,
                 json.dumps(kd))
        p1 = next(p for p in Z0(S2.engine.doc)["pages"] if p["id"] == "p1")
        controle("persistance : défauts du cartouchier et de la page survivent au redéploiement",
                 S2.engine.doc["defaults"]["fade_out_ms"] == 750
                 and S2.engine.doc["defaults"]["second_press"] == "restart"
                 and p1["defaults"] == {"loop": True}, json.dumps([S2.engine.doc["defaults"], p1["defaults"]]))
        ajoutes = rp.get("added") if isinstance(rp, dict) else None
        controle("persistance : un élément inséré en tête de playlist y reste, son id est rendu (annulation)",
                 bool(ajoutes) and Z0(S2.engine.doc)["playlist"]["items"][0]["id"] == ajoutes[0]
                 and Z0(S2.engine.doc)["playlist"]["items"][0]["title"] == "Tête", str(ajoutes))
        controle("/state publie les défauts (lus par les conditions de macro)",
                 (S2.state_view().get("defaults") or {}).get("fade_out_ms") == 750
                 and S2.state_view().get("page_defaults") == {"loop": True})
        controle("l'instantané /doc est le document seul (rien d'éphémère n'est persisté)",
                 set(doc) == set(E.default_doc()), str(sorted(doc)))


    with scenario("éditeur de clip par le script rendu : /flows, /wave, /montage/wave, nouveau média"):
        S3 = charger_script()
        r = S3._post("/key", {"page": "p1", "slot": 0, "key": {"title": "Monté", "media": {"asset": "j1"},
                                                               "edit": {"segments": [{"in_ms": 500, "out_ms": 1500}]}}})
        km = r["key_id"]
        S3._post("/fire", {"key": km})
        for _ in range(100):
            S3.engine.render(BLK)
        f = S3._get("/flows", {})[1][S3.FLOWS[0]]
        controle("/flows : durée MONTÉE (1 s sur un fichier de 2 s), restant et écoulé cohérents",
                 f["duration_ms"] == 1000 and f["elapsed_ms"] == 100 and f["remaining_ms"] == 900, json.dumps(f))
        controle("/state : la touche montée publie sa durée montée",
                 (S3.state_view()["key_media"].get(km) or {}).get("duration_ms") == 1000)
        c, w = S3._get("/waveform", {"key": [km]})
        controle("/waveform d'une touche montée : la forme d'onde du montage (durée montée)",
                 c == 200 and w.get("edited") and w.get("duration_ms") == 1000 and len(w.get("points") or []) == 400, str(c))
        mw = S3._post("/montage/wave", {"key": km, "edit": {"segments": [{"in_ms": 0, "out_ms": 2000, "cuts": [[100, 600]]}]},
                                        "points": 50})
        controle("/montage/wave : fenêtre du brouillon non enregistré, avec sa disposition (pièces en ms)",
                 len(mw["points"]) == 50 and abs(mw["duration_ms"] - (1500 - MT.CUT_XFADE_MS)) < 1
                 and [round(p["at_ms"]) for p in mw["layout"]] == [0, 100 - MT.CUT_XFADE_MS], json.dumps(mw.get("layout")))
        # une vraie source décodée dans le cache du conteneur : /wave lit le niveau grossier (mis en cache)
        xs = np.zeros((SR * 2, 2), np.float32)
        xs[SR] = 0.5
        kk = S3.library.cache_key({"asset": "pic"})
        xs.astype("<f4").tofile(os.path.join(S3.library.cache, kk + ".f32"))
        S3.library.sources[kk] = S3.M.MemmapSource(os.path.join(S3.library.cache, kk + ".f32"))
        c, wv = S3._get("/wave", {"asset": ["pic"], "points": ["200"]})
        c2, wz = S3._get("/wave", {"asset": ["pic"], "from_ms": ["999"], "to_ms": ["1001"], "points": ["96"]})
        controle("/wave : vue entière (niveau grossier, gardé en cache) et zoom à 2 ms (échantillons)",
                 c == 200 and wv["points"].index(50) == 100 and sum(1 for v in wv["points"] if v) == 1
                 and os.path.isfile(os.path.join(S3.library.cache, kk + ".pk"))
                 and c2 == 200 and wz["points"].index(50) == 48 and sum(1 for v in wz["points"] if v) == 1,
                 "%s %s" % (c, c2))
        # « Enregistrer comme nouveau média » : un original réel sur le volume, avec son identité
        orig = os.path.join(_media, "jingles", "j1__abcdef.wav")
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        with open(orig, "wb") as fo_:
            fo_.write(b"RIFF-original-ne-doit-pas-bouger")
        with open(orig + ".xml", "w") as fo_:
            fo_.write('<media id="j1"/>')
        avant = open(orig, "rb").read()
        S3.library.asset_scan_t = 0.0
        jb = S3._post("/montage/render", {"key": km, "name": "Jingle court", "gain_db": -6})
        import time as _t
        for _ in range(200):
            c, j = S3._get("/montage/job", {"id": [jb["job"]]})
            if j.get("state") != "running":
                break
            _t.sleep(0.02)
        import wave as _wave
        ok_fichier = j.get("state") == "done"
        chemin = os.path.join(_media, j.get("file") or "x")
        if ok_fichier:
            with _wave.open(chemin) as wf:
                nb, larg, voies, cad = wf.getnframes(), wf.getsampwidth(), wf.getnchannels(), wf.getframerate()
                brut = wf.readframes(10)
            v0 = int.from_bytes(brut[0:3], "little", signed=True) / 8388607.0
        controle("nouveau média : fichier WAV 24 bits stéréo 48 kHz de la durée MONTÉE (1 s)",
                 ok_fichier and (nb, larg, voies, cad) == (SR, 3, 2, SR) and j.get("duration_ms") == 1000,
                 json.dumps(j))
        controle("nouveau média : contenu = le montage, gain du brouillon compris (0,5 × −6 dB)",
                 ok_fichier and abs(v0 - 0.5 * 10 ** (-6 / 20)) < 1e-5, str(ok_fichier and v0))
        side = open(chemin + ".xml").read() if ok_fichier else ""
        controle("nouveau média : à côté de l'original, nommé avec le code de sa NOUVELLE identité, sidecar origin=cartouchier + filiation",
                 ok_fichier and os.path.dirname(chemin) == os.path.dirname(orig)
                 and re.search(r"Jingle_court__%s\.wav$" % j["asset"].replace("-", "")[:6], chemin) is not None
                 and ('id="%s"' % j["asset"]) in side and 'origin="cartouchier"' in side and 'derived_from="j1"' in side,
                 chemin + " " + side[:200])
        controle("nouveau média : l'original n'est pas touché", open(orig, "rb").read() == avant
                 and sorted(os.listdir(os.path.dirname(orig))) == sorted(["j1__abcdef.wav", "j1__abcdef.wav.xml",
                                                                         os.path.basename(chemin), os.path.basename(chemin) + ".xml"]))
        # La base des médias l'importe d'elle-même : le relevé du nœud (script_templates/media_scan.py)
        # lit dans ce sidecar l'identité, l'origine, le producteur et la filiation, et l'origine est
        # celle d'un producteur du système (AUTO_ORIGINS) — sinon le fichier resterait « non référencé ».
        import importlib.util as _ilu
        _sp = _ilu.spec_from_file_location("media_scan", os.path.join(RACINE, "script_templates", "media_scan.py"))
        _ms = _ilu.module_from_spec(_sp)
        _sp.loader.exec_module(_ms)
        from app import media_registry as _mr
        controle("nouveau média : la base l'importe d'elle-même (relevé du nœud : identité, origine AUTO_ORIGINS, producteur, filiation)",
                 ok_fichier and _ms.ID_RE.search(side).group(1) == j["asset"]
                 and _ms.ORIGIN_RE.search(side).group(1) in _mr.AUTO_ORIGINS
                 and _ms.PRODUCER_RE.search(side).group(1) == HOTE and _ms.DERIVED_RE.search(side).group(1) == "j1",
                 str(_mr.AUTO_ORIGINS))
        controle("nouveau média : le cartouchier le retrouve aussitôt par son identité",
                 ok_fichier and S3.library.resolve({"asset": j["asset"]}) == chemin)
        # pré-écoute par le script : action de macro servie, aucun lecteur pris
        S3._post("/stop_all", {"cut": True})
        S3.engine.render(BLK)
        S3._post("/preview", {"key": km})
        S3.engine.render(BLK)
        controle("/preview (macro « pré-écouter une touche ») : la pré-écoute joue, aucun lecteur pris",
                 S3.state_view()["preview"]["state"] == "playing"
                 and all(p["state"] == "idle" for p in S3.state_view()["players"]))
        S3._post("/preview/stop", {})
        controle("/preview/stop : la pré-écoute s'arrête", S3.engine.pfl.state == "idle")
        # le volume média est partagé par les scénarios suivants : l'original de « j1 » posé ici n'y reste pas
        shutil.rmtree(os.path.dirname(orig), ignore_errors=True)


# ── 6. Sauvegardes (0.1.3) : capture et rappel par genre, sans coupure ──────────────────────
# Le moteur capture et applique ; la bibliothèque vit dans l'orchestrateur
# (tests/verif_bibliotheque_cartouchier.py). Ce qui est vérifié ici : chaque genre ne touche
# QUE sa partie ; une page se pose dans une autre page, d'une autre grille ; un rappel pendant une
# lecture ne coupe rien et ne donne jamais à une nouvelle touche l'id d'un son qui joue.
def moteur_sv(pages, items=None, cols=4, rows=3, srcs=None):
    srcs = srcs or {}
    doc = {"grid": {"cols": cols, "rows": rows}, "pages": pages,
           "playlist": {"items": items or [], "ab": True}}
    return E.CartEngine(2, SR, doc, source_for=lambda m: srcs.get(m.get("asset")))


def touche(kid, slot, asset, **kw):
    return dict({"id": kid, "slot": slot, "title": kid.upper(), "media": {"asset": asset, "file": ""}}, **kw)


with scenario("sauvegardes : grille, playlist, page"):
    SRC = {"a": dc(.5, SR * 2), "b": dc(.3, SR * 2), "c": dc(.2, SR * 2), "z": dc(.7, SR * 4)}
    e = moteur_sv([{"id": "p1", "name": "Jingles", "keys": [touche("k1", 0, "a"), touche("k2", 5, "b", next="k1")],
                    "defaults": {"loop": True}},
                   {"id": "p2", "name": "Tapis", "keys": [touche("k3", 1, "c")]}],
                  items=[{"id": "i1", "title": "Ouverture", "media": {"asset": "a"}}], srcs=SRC)
    cg = e.capture("grid")
    cp = e.capture("playlist")
    cpg = e.capture("page", "p1")
    controle("capture : chaque genre a son résumé",
             cg["summary"]["pages"] == 2 and cg["summary"]["keys"] == 3 and cp["summary"]["items"] == 1
             and cpg["summary"]["keys"] == 2 and cpg["summary"]["page_name"] == "Jingles",
             json.dumps([cg["summary"], cp["summary"], cpg["summary"]]))
    controle("capture : une page désignée par son numéro (base 1) ou par défaut la page affichée",
             e.capture("page", "2")["payload"]["page"]["name"] == "Tapis"
             and e.capture("page")["payload"]["page"]["name"] == "Jingles")
    controle("capture : genre inconnu refusé par un code", refus(lambda: e.capture("tout"), "bad_kind"))
    # La grille et la playlist se défont, puis se rappellent chacune SANS toucher l'autre.
    e.set_grid({"cols": 2, "rows": 2})
    e.pages_op({"op": "delete", "page": "p2"})
    e.playlist_op({"op": "clear"})
    e.playlist_op({"op": "add", "items": [{"title": "Autre", "media": {"asset": "c"}}]})
    pl_avant = copy.deepcopy(Z0(e.doc)["playlist"]["items"])
    r = e.recall("grid", cg["payload"])
    controle("rappel de grille : taille, pages, touches, défauts de page et chaînage restitués",
             Z0(r)["grid"] == {"cols": 4, "rows": 3} and [p["name"] for p in Z0(r)["pages"]] == ["Jingles", "Tapis"]
             and Z0(r)["pages"][0]["defaults"] == {"loop": True} and Z0(r)["pages"][0]["keys"][1]["next"] == "k1"
             and [k["id"] for k in Z0(r)["pages"][0]["keys"]] == ["k1", "k2"], json.dumps(Z0(r)["pages"])[:300])
    controle("rappel de grille : la playlist n'est PAS touchée", Z0(e.doc)["playlist"]["items"] == pl_avant)
    pages_avant = copy.deepcopy(Z0(e.doc)["pages"])
    r = e.recall("playlist", cp["payload"])
    controle("rappel de playlist : éléments restitués, la grille n'est PAS touchée",
             [it["title"] for it in Z0(r)["playlist"]["items"]] == ["Ouverture"] and Z0(e.doc)["pages"] == pages_avant)
    controle("rappel : genre inconnu, sauvegarde vide refusés par un code",
             refus(lambda: e.recall("tout", {}), "bad_kind") and refus(lambda: e.recall("grid", {}), "empty_save")
             and refus(lambda: e.recall("page", {}), "empty_save"))

    # Une page posée dans une AUTRE page (remplacer), ajoutée, et posée dans un AUTRE cartouchier
    # dont la grille est plus étroite : ligne et colonne gardées, ce qui ne tient pas se replace.
    r = e.recall("page", cpg["payload"], page="p2")
    p2 = next(p for p in Z0(r)["pages"] if p["id"] == "p2")
    controle("rappel de page dans une autre page : touches, nom et défauts posés, l'autre page intacte",
             p2["name"] == "Jingles" and len(p2["keys"]) == 2 and p2["defaults"] == {"loop": True}
             and len(next(p for p in Z0(r)["pages"] if p["id"] == "p1")["keys"]) == 2, json.dumps(p2)[:300])
    controle("rappel de page dans une autre page : ids neufs (aucun doublon avec la page d'origine), chaînage suivi",
             not ({k["id"] for k in p2["keys"]} & {"k1", "k2"})
             and p2["keys"][1]["next"] == p2["keys"][0]["id"], json.dumps(p2["keys"])[:300])
    n_pages = len(Z0(e.doc)["pages"])
    r = e.recall("page", cpg["payload"], add=True)
    controle("rappel de page en AJOUT : une page de plus, affichée",
             len(Z0(r)["pages"]) == n_pages + 1 and Z0(r)["page"] == Z0(r)["pages"][-1]["id"] and r["recall"]["page"] == Z0(r)["page"])
    autre = moteur_sv([{"id": "p1", "keys": [touche("x1", 0, "z")]}], cols=3, rows=2, srcs=SRC)
    pg_large = {"grid": {"cols": 4, "rows": 3}, "page": {"name": "Large", "keys": [
        touche("k1", 1, "a"), touche("k2", 5, "b"), touche("k3", 3, "c")]}}
    r = autre.recall("page", pg_large, page="1")
    pk = {k["title"]: k["slot"] for k in Z0(r)["pages"][0]["keys"]}
    controle("autre cartouchier, grille plus étroite : ligne et colonne gardées, la touche hors colonne se replace",
             pk.get("K1") == 1 and pk.get("K2") == 4 and pk.get("K3") not in (1, 4) and pk.get("K3", 99) < 6
             and r["recall"]["relocated"] == 1 and r["recall"]["hidden"] == 0, json.dumps([pk, r["recall"]]))
    controle("autre cartouchier : page inconnue refusée par un code",
             refus(lambda: autre.recall("page", pg_large, page="9"), "unknown_page"))

with scenario("sauvegardes : rappel pendant une lecture, sans coupure"):
    SRC = {"a": dc(.5, SR * 2), "b": dc(.25, SR * 2), "c": dc(.2, SR * 2)}
    e = moteur_sv([{"id": "p1", "keys": [touche("k1", 0, "a"), touche("k2", 1, "b")]}],
                  items=[{"id": "i1", "title": "Un", "media": {"asset": "a"}},
                         {"id": "i2", "title": "Deux", "media": {"asset": "b"}}], srcs=SRC)
    e.fire("k1")
    rendre(e, 100)
    flows_avant = e.flows_view(["f1", "f2"])
    # Une grille dont la touche « k1 » est un AUTRE son, et dont « k2 » est le même.
    sv = {"grid": {"cols": 4, "rows": 3}, "pages": [{"id": "p1", "keys": [touche("k1", 0, "c"), touche("k9", 1, "a")]}]}
    r = e.recall("grid", sv)
    o = rendre(e, 100)
    flows_apres = e.flows_view(["f1", "f2"])
    controle("rappel pendant une lecture : le lecteur JOUE toujours, sans un échantillon de silence",
             etat(e, 1) == "playing" and float(np.min(np.abs(o[0][:, 0]))) > .49, str(float(np.min(np.abs(o[0][:, 0])))))
    controle("rappel pendant une lecture : /flows garde le titre et avance le décompte",
             flows_apres["f1"]["title"] == flows_avant["f1"]["title"] == "K1"
             and flows_apres["f1"]["elapsed_ms"] == 200 and flows_apres["f1"]["remaining_ms"] == 1800,
             json.dumps(flows_apres["f1"]))
    nk = Z0(r)["pages"][0]["keys"][0]
    controle("rappel pendant une lecture : une touche d'un AUTRE son ne reprend pas l'id du son qui joue",
             nk["id"] != "k1" and r["recall"]["renamed"] == 1, json.dumps(nk))
    e.fire(nk["id"])
    controle("rappel pendant une lecture : appuyer la nouvelle touche lance SON son, l'ancien continue",
             etat(e, 1) == "playing" and etat(e, 2) == "playing" and e.players[1].item["media"]["asset"] == "c")
    # Même son, même id : la touche reste celle qui joue (l'appui agit sur ce son).
    e2 = moteur_sv([{"id": "p1", "keys": [touche("k1", 0, "a")]}], srcs=SRC)
    e2.fire("k1")
    r2 = e2.recall("grid", {"pages": [{"id": "p1", "keys": [touche("k1", 3, "a")]}]})
    controle("rappel : la touche du MÊME son garde son id (elle reste allumée, l'appui agit sur ce son)",
             Z0(r2)["pages"][0]["keys"][0]["id"] == "k1" and e2.holder("k1") is not None and r2["recall"]["renamed"] == 0)
    # Playlist rappelée pendant qu'un élément est à l'antenne.
    e.set_mode("playlist")
    e.stop_all(cut=True)
    rendre(e, 10)
    e.pl_go()
    rendre(e, 50)
    joue = [p for p in e.players if p.state == "playing" and p.origin == "playlist"]
    r = e.recall("playlist", {"items": [{"id": "i1", "title": "Neuf", "media": {"asset": "c"}},
                                        {"id": "i5", "title": "Cinq", "media": {"asset": "b"}}],
                              "ab_handover": "cut"})
    o = rendre(e, 50)
    pn = joue[0].n if joue else 1
    controle("playlist rappelée à l'antenne : l'élément en cours continue (aucune coupure)",
             bool(joue) and joue[0].state == "playing" and joue[0].item["title"] == "Un"
             and float(np.min(np.abs(o[pn - 1][:, 0]))) > .49)
    controle("playlist rappelée : le prêt est le premier élément NOUVEAU (id renommé, pas l'ancien « i1 »)",
             e.pl_ready == Z0(r)["playlist"]["items"][0]["id"] and e.pl_ready != "i1"
             and Z0(r)["playlist"]["items"][0]["title"] == "Neuf", str(e.pl_ready))
    e.pl_go()
    rendre(e, 5)
    controle("playlist rappelée (passage « couper » porté par la sauvegarde) : le GO suivant lance le nouvel élément et libère l'ancien",
             any(p.state == "playing" and p.item and p.item["title"] == "Neuf" for p in e.players)
             and not any(p.state == "playing" and p.item and p.item["title"] == "Un" for p in e.players), str([(p.state, (p.item or {}).get("title")) for p in e.players]))

if ok_charge:
    with scenario("sauvegardes : /capture et /recall du script RENDU"):
        controle("/capture déclaré en lecture, /recall déclaré ET persisté",
                 "/capture" in reads and "/capture" in eps and "/recall" in eps
                 and "/recall" in ((ctrl.get("persist") or {}).get("settings") or []))
        rc_ = ctrl.get("recall") or {}
        controle("control.recall : bibliothèque à genres, application générique",
                 rc_.get("source") == "plugin_store" and rc_.get("apply") == "post" and rc_.get("endpoint") == "/recall"
                 and rc_.get("capture") == "/capture" and rc_.get("kinds") == ["grid", "playlist", "page"], json.dumps(rc_))
        acts_ = {a["id"]: a for a in manifeste.get("actions") or []}
        controle("macros : rappeler une grille, une playlist, une page dans une page (listes vivantes)",
                 all(acts_.get("recall_" + k, {}).get("core") == "recall" and acts_["recall_" + k].get("recall_kind") == k
                     and any(p.get("options_from") == "recall" for p in acts_["recall_" + k]["params"])
                     for k in ("grid", "playlist", "page"))
                 and any(p["key"] == "page" and p.get("options_endpoint") == "/pages" for p in acts_["recall_page"]["params"])
                 and any(p["key"] == "add" for p in acts_["recall_page"]["params"]))
        S3 = charger_script()
        S3._post("/key", {"page": "p1", "slot": 0, "key": {"title": "Ici", "media": {"asset": "j1"}}})
        S3._post("/key", {"page": "p1", "slot": 1, "key": {"title": "Ailleurs", "media": {"asset": "absent-ici"}}})
        c_, cap = S3._get("/capture", {"kind": ["page"]})
        c2_, bad = S3._get("/capture", {"kind": ["nope"]})
        controle("/capture rend {kind, payload, summary} ; un genre inconnu est un code 400",
                 c_ == 200 and cap["kind"] == "page" and cap["summary"]["keys"] == 2 and c2_ == 400
                 and bad.get("error") == "bad_kind", json.dumps([c_, c2_, bad]))
        S4 = charger_script()
        out = S4._post("/recall", {"kind": "page", "payload": cap["payload"], "name": "Démo", "add": True})
        rep = (out or {}).get("recall") or {}
        controle("/recall : la page est ajoutée et le document rendu (persisté par l'orchestrateur)",
                 len(Z0(out["doc"])["pages"]) == 2 and rep.get("page") == Z0(out["doc"])["page"] and rep.get("name") == "Démo",
                 json.dumps(rep)[:300])
        controle("/recall : un son absent du nœud est SIGNALÉ (touche gardée), jamais un refus",
                 rep.get("missing_count") == 2 and len(Z0(out["doc"])["pages"][1]["keys"]) == 2
                 and {m["title"] for m in rep.get("missing") or []} == {"Ici", "Ailleurs"}, json.dumps(rep)[:300])

# ── 7. Onglets et zones (0.1.8) ──────────────────────────────────────────────────────────────
# Un onglet a une disposition (1, 2 côte à côte, 2 empilées, 1 + 2) ; chaque emplacement porte une
# ZONE (grille, playlist, playlist A/B) qui possède ses lecteurs. Ce qui est vérifié : un document
# d'avant les onglets se lit à l'identique (une zone qui a tout) ; deux zones sont deux cartouchiers
# indépendants (lecteurs, pages, exclusivité, enchaînement auto) ; un lecteur n'appartient qu'à une
# zone ; les lecteurs d'un onglet caché continuent ; un ordre SANS zone (macro, lien de console
# d'avant) va à la zone par défaut de son genre ; l'éditeur de disposition refuse plutôt que de
# voler un lecteur ; la page publique d'un onglet ne joue que dans cet onglet.
def tz_key(kid, slot, **kw):
    return dict({"id": kid, "slot": slot, "title": kid.upper(), "media": {"asset": kid, "file": ""}}, **kw)


def tz_zone(zid, mode, players, keys=(), items=(), page="", **pl):
    pid = "p_" + zid
    return {"id": zid, "name": zid.upper(), "mode": mode, "players": list(players),
            "pages": [{"id": pid, "keys": list(keys)}], "page": page or pid,
            "playlist": dict({"items": [{"id": i, "title": i.upper(), "media": {"asset": i, "file": ""}} for i in items]},
                             **pl)}


def tz_engine(tabs, zones, n_players=4, tab=None, srcs=None):
    doc = {"version": 3, "tabs": tabs, "zones": zones, "tab": tab or tabs[0]["id"]}
    srcs = srcs or {}
    return E.CartEngine(n_players, SR, doc, source_for=lambda m: srcs.get(m.get("asset"), dc(.5, long_)))


with scenario("onglets : migration d'un document d'avant les onglets"):
    old = {"version": 2, "mode": "playlist", "grid": {"cols": 5, "rows": 2}, "page": "p2",
           "pages": [{"id": "p1", "name": "Jingles", "keys": [tz_key("k1", 0), tz_key("k2", 3, next="k1")]},
                     {"id": "p2", "name": "Tapis", "keys": [tz_key("k3", 1, group="g")], "defaults": {"loop": True}}],
           "playlist": {"items": [{"id": "i1", "media": {"asset": "i1"}}, {"id": "i2", "media": {"asset": "i2"}}],
                        "ab": True, "ab_players": [2, 3], "auto": True, "loop": True,
                        "ab_handover": "fade", "ab_handover_ms": 700},
           "defaults": {"fade_out_ms": 200}, "seq": 9}
    d = E.normalize_doc(old, 3)
    z = Z0(d)
    controle("onglets : migration — UN onglet, UNE zone qui possède TOUS les lecteurs",
             len(d["tabs"]) == 1 and len(d["zones"]) == 1 and d["tabs"][0]["zones"] == [z["id"]]
             and d["tabs"][0]["layout"] == "1" and z["players"] == [1, 2, 3] and d["tab"] == d["tabs"][0]["id"],
             json.dumps({"tabs": d["tabs"], "players": z["players"]}))
    controle("onglets : migration — grille, pages, page affichée, playlist et options repris à l'identique",
             z["mode"] == "playlist" and z["grid"] == {"cols": 5, "rows": 2} and z["page"] == "p2"
             and [p["name"] for p in z["pages"]] == ["Jingles", "Tapis"]
             and [k["id"] for k in z["pages"][0]["keys"]] == ["k1", "k2"] and z["pages"][0]["keys"][1]["next"] == "k1"
             and z["pages"][1]["defaults"] == {"loop": True}
             and [it["id"] for it in z["playlist"]["items"]] == ["i1", "i2"]
             and {k: z["playlist"][k] for k in ("ab", "ab_players", "auto", "loop", "ab_handover", "ab_handover_ms")}
             == {"ab": True, "ab_players": [2, 3], "auto": True, "loop": True, "ab_handover": "fade", "ab_handover_ms": 700}
             and d["defaults"]["fade_out_ms"] == 200, json.dumps(z)[:400])
    controle("onglets : normalize_doc idempotent (migré, puis relu)", E.normalize_doc(d, 3) == d)
    controle("onglets : migration d'un document v1 à 1 lecteur — A/B impossible, comme avant",
             Z0(E.normalize_doc({"playlist": {"ab": True}}, 1))["playlist"]["ab"] is False)
    junk = E.normalize_doc({"version": 3, "tabs": [{"id": "t1", "layout": "zz", "zones": ["nope", 7]}],
                            "zones": "pas une liste", "tab": "t9"}, 2)
    controle("onglets : valeurs inconnues → défauts sûrs (disposition 1, zone grille créée, onglet existant)",
             len(junk["tabs"]) == 1 and junk["tabs"][0]["layout"] == "1" and len(junk["zones"]) == 1
             and Z0(junk)["mode"] == "grid" and junk["tab"] == junk["tabs"][0]["id"], json.dumps(junk)[:300])
    e = E.CartEngine(3, SR, old, source_for=lambda m: dc(.5, long_))
    controle("onglets : migré — la playlist A/B tient l'élément prêt sur SON lecteur A (2), sans réenregistrement",
             etat(e, 2) == "cued" and e.players[1].item["id"] == "i1", str([etat(e, n) for n in (1, 2, 3)]))
    r = e.pl_go()
    controle("onglets : migré — GO sans zone joue sur 2, prépare la suite sur 3 (comportement d'avant)",
             r["player"] == 2 and etat(e, 3) == "cued" and e.players[2].item["id"] == "i2")
    e.set_mode("grid")
    e.fire("k1")
    controle("onglets : migré — une touche prend un lecteur libre de la zone (1)", e.holder("k1").n == 1)

TZ_TABS = [{"id": "t1", "name": "Plateau", "layout": "2h", "zones": ["za", "zb"]},
           {"id": "t2", "name": "Antenne", "layout": "1+2", "zones": ["zj", "zp", "zx"]}]


def tz_plateau(n_players=6, ab_auto=False):
    return tz_engine(TZ_TABS, [
        tz_zone("za", "grid", [1], [tz_key("a1", 0, group="g"), tz_key("a2", 1)]),
        tz_zone("zb", "grid", [2], [tz_key("b1", 0, group="g")]),
        tz_zone("zj", "grid", [5], [tz_key("j1", 0)]),
        tz_zone("zp", "playlist", [3, 4], items=["p1", "p2", "p3"], ab=True, ab_players=[3, 4], auto=ab_auto),
        tz_zone("zx", "grid", [], []),
    ], n_players=n_players)


with scenario("onglets : deux grilles côte à côte, deux cartouchiers indépendants"):
    e = tz_plateau()
    e.fire("a1")
    e.fire("b1")
    ha, hb = e.holder("a1"), e.holder("b1")
    controle("zones : chaque grille joue sur SON lecteur (Invité A → 1, Invité B → 2)",
             hb is not None and hb.n == 2 and (ha is None or ha.n == 1))
    controle("zones : même nom de groupe d'exclusivité dans deux zones → aucune interaction",
             etat(e, 1) == "playing" and etat(e, 2) == "playing")
    controle("zones : lecteur de la zone occupé → REFUS, jamais le lecteur libre d'une autre zone",
             refus(lambda: e.fire("a2"), "no_free_player") and etat(e, 6) == "idle" and etat(e, 5) == "idle")
    e.stop_all(cut=True, zone="za")
    controle("zones : « tout arrêter » d'une zone ne touche pas l'autre", etat(e, 1) == "idle" and etat(e, 2) == "playing")
    e.pages_op({"op": "add", "zone": "zb"})
    e.set_page("prev", zone="zb")
    pa, pb = e.zone("za")["page"], e.zone("zb")["page"]
    controle("zones : pages propres à chaque zone (une page ajoutée et changée dans B, A n'a pas bougé)",
             pa == "p_za" and pb == "p_zb" and len(e.zone("zb")["pages"]) == 2 and len(e.zone("za")["pages"]) == 1, str([pa, pb]))
    e2 = tz_engine([{"id": "t1", "layout": "1", "zones": ["za"]}],
                   [tz_zone("za", "grid", [1], [tz_key("a1", 0, player=3)])], n_players=4)
    controle("zones : une touche affectée à un lecteur HORS de sa zone est refusée (pas jouée ailleurs)",
             refus(lambda: e2.fire("a1"), "player_outside_zone") and all(p.state == "idle" for p in e2.players))

with scenario("onglets : playlist A/B à côté d'une grille"):
    e = tz_plateau()
    controle("A/B : la zone playlist tient son premier élément sur son lecteur A (3)",
             etat(e, 3) == "cued" and e.players[2].item["id"] == "p1")
    r = e.pl_go(zone="zp")
    controle("A/B : GO de la zone joue sur 3, prépare la suite sur 4", r["player"] == 3 and etat(e, 4) == "cued"
             and e.players[3].item["id"] == "p2")
    e.fire("j1")
    controle("A/B : la grille des jingles (zone voisine) joue sur 5 pendant ce temps", e.holder("j1").n == 5)
    controle("fader start : lecteur 4 (cued) — if_played le laisse chargé",
             e.player_stop(4, if_played=True)["action"] == "none" and etat(e, 4) == "cued")
    r = e.player_play(4)
    controle("fader start : jouer le lecteur 4 lance CE qu'il tient, dans SA zone",
             r.get("player") == 4 and r.get("zone") == "zp" and e.players[3].item["id"] == "p2" and etat(e, 4) == "playing")
    controle("fader start : la suite revient sur l'autre lecteur A/B de la zone (3), jamais sur la grille",
             e.players[2].pending_cue == "p3" or (etat(e, 3) == "cued" and e.players[2].item["id"] == "p3"),
             str([etat(e, n) for n in range(1, 7)]))
    controle("A/B : les lecteurs de la grille ne servent jamais la playlist",
             e.players[4].item["id"] == "j1" and etat(e, 1) == "idle" and etat(e, 2) == "idle")

with scenario("onglets : enchaînement auto par zone"):
    e = tz_engine([{"id": "t1", "layout": "2v", "zones": ["za", "zb"]}],
                  [tz_zone("za", "playlist", [1], items=["a1", "a2"], ab=False, auto=True),
                   tz_zone("zb", "playlist", [2], items=["b1", "b2"], ab=False, auto=False)],
                  n_players=2, srcs={"a1": dc(.5, SR // 10), "b1": dc(.5, SR // 10)})
    e.pl_go(zone="za")
    e.pl_go(zone="zb")
    rendre(e, 150)
    controle("auto : la fin d'un élément enchaîne dans SA zone (A, auto) et pas dans l'autre (B, manuel)",
             etat(e, 1) == "playing" and e.players[0].item["id"] == "a2" and etat(e, 2) == "idle",
             str([(p.state, (p.item or {}).get("id")) for p in e.players]))

with scenario("onglets : un lecteur n'appartient qu'à une zone"):
    d = E.normalize_doc({"version": 3, "tabs": [{"id": "t1", "layout": "2h", "zones": ["za", "zb"]}],
                         "zones": [tz_zone("za", "grid", [1, 2]), tz_zone("zb", "playlist", [2, 3], ab=True)]}, 3)
    za, zb = d["zones"]
    controle("exclusivité : un lecteur revendiqué par deux zones reste à la PREMIÈRE (ordre des onglets)",
             za["players"] == [1, 2] and zb["players"] == [3], json.dumps([za["players"], zb["players"]]))
    controle("exclusivité : une zone A/B privée d'un de ses deux lecteurs n'est plus A/B (jamais un lecteur volé)",
             zb["playlist"]["ab"] is False, json.dumps(zb["playlist"]))
    e = tz_plateau()
    controle("éditeur : un lecteur d'une zone d'un AUTRE onglet est refusé (player_taken)",
             refus(lambda: e.set_layout({"tab": "t1", "layout": "2h", "slots": [
                 {"zone": "za", "kind": "grid", "players": [1, 3]}, {"zone": "zb", "kind": "grid", "players": [2]}]}),
                 "player_taken"))
    controle("éditeur : le même lecteur dans deux emplacements est refusé (player_twice)",
             refus(lambda: e.set_layout({"tab": "t1", "layout": "2h", "slots": [
                 {"zone": "za", "kind": "grid", "players": [1]}, {"zone": "zb", "kind": "grid", "players": [1]}]}),
                 "player_twice"))
    controle("éditeur : une playlist A/B à un seul lecteur est refusée",
             refus(lambda: e.set_layout({"tab": "t1", "layout": "1", "slots": [
                 {"zone": "za", "kind": "playlist_ab", "players": [1]}]}), "ab_needs_two_players"))
    controle("éditeur : nombre d'emplacements ≠ disposition → refusé",
             refus(lambda: e.set_layout({"tab": "t1", "layout": "1+2", "slots": [
                 {"zone": "za", "kind": "grid", "players": [1]}]}), "bad_layout"))
    controle("éditeur : un refus ne change RIEN (document intact)",
             e.zone("za")["players"] == [1] and e.zone("zb")["players"] == [2] and e.doc["tabs"][0]["layout"] == "2h")
    e.fire("b1")
    e.set_layout({"tab": "t1", "layout": "1+2", "slots": [
        {"zone": "zb", "kind": "grid", "players": [2, 6], "name": "Invité B"},
        {"kind": "playlist", "players": [1], "name": "Liste"},
        {"kind": "grid", "players": []}]})
    t1 = e.doc["tabs"][0]
    zs = [e.zone(z) for z in t1["zones"]]
    controle("éditeur : disposition 1 + 2 appliquée ; la zone gardée garde ses touches ; la zone non gardée est supprimée",
             t1["layout"] == "1+2" and zs[0]["id"] == "zb" and zs[0]["name"] == "Invité B" and zs[0]["players"] == [2, 6]
             and e.find_key("b1") is not None and e.find_key("a1") is None and E.zone_kind(zs[1]) == "playlist"
             and zs[1]["players"] == [1] and zs[2]["players"] == [] and "za" not in [z["id"] for z in e.doc["zones"]],
             json.dumps(t1))
    controle("éditeur : ce qui joue continue quand la disposition change", e.holder("b1") is not None and etat(e, 2) == "playing")
    controle("onglets : supprimer le dernier onglet est refusé",
             refus(lambda: tz_engine([{"id": "t1", "layout": "1", "zones": ["za"]}],
                                     [tz_zone("za", "grid", [1])]).tabs_op({"op": "delete", "tab": "t1"}), "last_tab"))

with scenario("onglets : un onglet caché continue de jouer"):
    e = tz_plateau()
    e.fire("a1")
    e.pl_go(zone="zp")
    e.set_tab("t2")
    rendre(e, 20)
    v = e.zones_view()
    controle("onglet caché : ses lecteurs continuent (changer d'onglet ne touche à rien)",
             e.doc["tab"] == "t2" and etat(e, 1) == "playing" and etat(e, 3) == "playing" and e.players[0].pos > 0)
    controle("onglet caché : le point de l'onglet s'allume quand un de ses lecteurs joue",
             [t["playing"] for t in v["tabs"]] == [True, True], json.dumps(v["tabs"]))
    e.stop_all(cut=True)
    rendre(e, 2)
    controle("onglet : point éteint quand plus rien ne joue",
             [t["playing"] for t in e.zones_view()["tabs"]] == [False, False])
    e.set_tab("next")
    controle("onglets : « suivant » boucle (t2 → t1), un numéro désigne un onglet",
             e.doc["tab"] == "t1" and e.set_tab(2)["tab"] == "t2")
    e.tabs_op({"op": "add", "name": "Nouveau"})
    nt = e.doc["tabs"][-1]
    controle("onglets : « + Onglet » crée un onglet nommé, une zone grille sans lecteur, et l'affiche",
             nt["name"] == "Nouveau" and e.doc["tab"] == nt["id"] and e.zone(nt["zones"][0])["players"] == []
             and e.zone(nt["zones"][0])["mode"] == "grid")
    e.tabs_op({"op": "move", "tab": nt["id"], "delta": -5})
    controle("onglets : réordonner", e.doc["tabs"][0]["id"] == nt["id"])
    e.tabs_op({"op": "delete", "tab": nt["id"]})
    controle("onglets : supprimer un onglet supprime ses zones", len(e.doc["tabs"]) == 2
             and nt["zones"][0] not in [z["id"] for z in e.doc["zones"]])

with scenario("onglets : un ordre sans zone va à la zone par défaut de son genre"):
    e = tz_plateau()
    controle("défaut : zone grille = la première grille dans l'ordre des onglets (za) ; playlist = la première playlist (zp)",
             e.default_zone("grid")["id"] == "za" and e.default_zone("playlist")["id"] == "zp"
             and e.default_zone()["id"] == "za")
    r = e.pl_go()
    controle("défaut : « Playlist : GO » d'avant les onglets (sans zone) lance la playlist de l'onglet Antenne",
             r["zone"] == "zp" and r["player"] == 3)
    e.set_page("next")
    controle("défaut : « page suivante » sans zone agit sur la première grille", e.zone("za")["page"] == "p_za")
    controle("défaut : une zone inconnue est REFUSÉE (pas la zone par défaut à la place)",
             refus(lambda: e.pl_go(zone="zz"), "unknown_zone"))
    e.set_path("zones.zp.playlist.ab_handover", "cut")
    controle("macros : arbre par id — zones.<id>.playlist.ab_handover règle CETTE zone",
             e.zone("zp")["playlist"]["ab_handover"] == "cut" and refus(lambda: e.set_path("zones.zp.name", "x"), "bad_path"))
    cap = e.capture("playlist", zone="zp")
    e.recall("grid", e.capture("grid", zone="zb")["payload"], zone="za")
    controle("sauvegardes : capture/rappel PAR ZONE (la grille de B rappelée dans A ; B et la playlist intactes)",
             [k["title"] for k in e.zone("za")["pages"][0]["keys"]] == ["B1"]
             and e.find_key("b1") is not None and cap["summary"]["zone"] == "zp"
             and [it["id"] for it in e.zone("zp")["playlist"]["items"]] == ["p1", "p2", "p3"],
             json.dumps(e.zone("za")["pages"])[:300])

if ok_charge:
    with scenario("onglets : script rendu — /state, listes, ordres sans zone, onglet public"):
        cart = tz_plateau().doc
        S5 = charger_script(cart, players=6)
        st = S5.state_view()
        controle("/state : onglets et zones publiés (genre, lecteurs, onglet), lecteurs avec leur zone",
                 [t["id"] for t in st["tabs"]] == ["t1", "t2"] and st["tab"] == "t1"
                 and {z["id"]: z["kind"] for z in st["zones"]} == {"za": "grid", "zb": "grid", "zj": "grid",
                                                                   "zp": "playlist_ab", "zx": "grid"}
                 and [p["zone"] for p in st["players"]] == ["za", "zb", "zp", "zp", "zj", None], json.dumps(st["tabs"]))
        controle("/state : les champs d'avant (mode, page, playlist) = zones par défaut",
                 st["mode"] == "grid" and st["page"] == "p_za" and st["playlist"]["ab_players"] == [3, 4] and st["mode"] == "grid",
                 json.dumps([st["mode"], st["page"], st["playlist"]]))
        c_, zl = S5._get("/zones", {})
        c2_, tl = S5._get("/tabs", {})
        c3_, il = S5._get("/items", {"zone": ["zp"]})
        controle("listes vivantes : /zones, /tabs, /items?zone=",
                 c_ == 200 and [x["value"] for x in zl["items"]] == ["za", "zb", "zj", "zp", "zx"]
                 and [x["value"] for x in tl["items"]] == ["t1", "t2"] and len(il["items"]) == 3)
        r = S5._post("/tab", {"tab": "t2"})
        controle("macro « afficher un onglet » : /tab", r == {"tab": "t2"} and S5.engine.doc["tab"] == "t2")
        acts_ = {a["id"]: a for a in manifeste.get("actions") or []}
        zoned = [a for a in ("pl_go", "pl_next", "pl_prev", "pl_goto", "page", "page_next", "page_prev", "stop_all",
                             "mode_grid", "mode_playlist", "recall_grid", "recall_playlist", "recall_page")
                 if not any(p["key"] == "zone" and p.get("optional") and p.get("options_endpoint") == "/zones"
                            for p in acts_.get(a, {}).get("params") or [])]
        controle("macros : chaque action de playlist / grille prend une zone FACULTATIVE (liste vivante /zones)",
                 not zoned, str(zoned))
        controle("macros : afficher un onglet, onglet suivant / précédent, changer le genre d'une zone",
                 all(a in acts_ for a in ("tab", "tab_next", "tab_prev", "zone_grid", "zone_playlist", "zone_playlist_ab")))
        bi = ((manifeste.get("param_tree") or {}).get("by_id") or {})
        controle("macros : arbre de paramètres par id sur les zones (/set, catalogue zones.json)",
                 bi.get("document") == "cart" and (bi.get("set") or {}).get("endpoint") == "/set"
                 and os.path.isfile(os.path.join(DOSSIER, bi.get("catalog") or "-"))
                 and all(ep in eps for ep in ("/set", "/tab", "/tabs", "/layout", "/zone")))
        pers = (ctrl.get("persist") or {}).get("settings") or []
        controle("persistance : /tab, /tabs, /layout, /zone, /set déclarés ET persistés",
                 all(ep in pers for ep in ("/tab", "/tabs", "/layout", "/zone", "/set")), str(pers))
        controle("/zones et /tabs lisibles par le relais public (read_endpoints)", {"/zones", "/tabs"} <= set(reads))
        # Onglet public jouable (lien avec scope {tab, play}) : la serrure du CONTENEUR.
        controle("public : PUBLIC_ACTIONS du conteneur = control.public_actions du manifeste (ni plus large, ni plus étroit)",
                 list(S5.PUBLIC_ACTIONS) == list(ctrl.get("public_actions") or []), str(S5.PUBLIC_ACTIONS))
        sc = json.dumps({"tab": "t1", "play": True})
        ref = S5.public_scope_refusal
        controle("public : une touche de l'onglet partagé passe ; une touche d'un AUTRE onglet est refusée",
                 ref("/fire", {"key": "a1"}, sc) is None and ref("/fire", {"key": "j1"}, sc) == "out_of_scope")
        controle("public : GO d'une zone d'un autre onglet, lecteur d'une autre zone → refusés ; lecteur de l'onglet → admis",
                 ref("/pl/go", {"zone": "zp"}, sc) == "out_of_scope" and ref("/pl/go", {}, sc) == "out_of_scope"
                 and ref("/player/play", {"player": 3}, sc) == "out_of_scope" and ref("/player/stop", {"player": 2}, sc) is None)
        controle("public : hors public_actions (édition) → refusé ; lien sans « jouer » → lecture seule ; sans onglet → refusé",
                 ref("/key", {"page": "p_za"}, sc) == "not_public" and ref("/layout", {}, sc) == "not_public"
                 and ref("/fire", {"key": "a1"}, json.dumps({"tab": "t1", "play": False})) == "read_only"
                 and ref("/fire", {"key": "a1"}, json.dumps({"play": True})) == "no_tab"
                 and ref("/fire", {"key": "a1"}, "{{pas du json") == "bad_scope")

# ── 5. Console ───────────────────────────────────────────────────────────────────────────────
js = lire("control.js")
controle("console : ctx.base honoré par une fonction unique",
         re.search(r"BASE = \(ctx && ctx\.base\)", js) is not None
         and re.search(r"const api = p => BASE \?", js) is not None,
         "toutes les URL passent par api(), qui suit ctx.base (page publique)")
controle("console : le sondage s'arrête au démontage", "clearInterval" in js)
controle("console : aucun glisser HTML5 indispensable (iPad)", "draggable" not in js,
         "déplacer une touche ou un élément passe par des boutons")
controle("manifeste : ui.public_page déclarée", (manifeste.get("ui") or {}).get("public_page") is True)
cles = set(re.findall(r"\bT\('([a-z0-9_]+)'", js))
cles -= {"err_", "media_", "sp_", "st_", "color_", "limited_", "shape_"}
fr = json.loads(lire("i18n/fr.json"))
en = json.loads(lire("i18n/en.json"))
manq = sorted(k for k in cles if "plugin.cartouchier." + k not in fr or "plugin.cartouchier." + k not in en)
controle("console : chaque clé T() existe en français ET en anglais", not manq, str(manq))
controle("i18n : mêmes clés en français et en anglais", set(fr) == set(en),
         str(sorted(set(fr) ^ set(en))[:8]))
for pref, valeurs in (("st_", E.STATES), ("sp_", E.SECOND_PRESS), ("shape_", MT.SHAPES)):
    manq = [v for v in valeurs if "plugin.cartouchier." + pref + v not in fr]
    controle("i18n : %s* couvre le vocabulaire du moteur" % pref, not manq, str(manq))
codes = set(re.findall(r'EngineError\("([a-z_]+)"', lire("src/engine.py")))
codes |= set(re.findall(r'MontageError\("([a-z_]+)"', lire("src/montage.py")))
codes |= set(re.findall(r'"error": "([a-z_]+)"', lire("script.py"))) - {"unknown"}
manq = sorted(c for c in codes if "plugin.cartouchier.err_" + c not in fr)
controle("i18n : chaque code de refus du moteur est traduit", not manq, str(manq))
# Affecter un son (0.1.2) : trois gestes, dont deux au doigt. Le glisser HTML5 reçu des lignes de
# la bibliothèque ne suffit pas (il ne part pas au doigt sur iPad, mesuré le 2026-10-14).
controle("console : glisser au DOIGT (pointer events, cible trouvée sous le doigt)",
         "pointerdown" in js and "elementFromPoint" in js and "pointercancel" in js)
controle("console : toucher le média PUIS la touche l'affecte (sans glisser)",
         re.search(r"if \(EDIT && armed\) \{ dropMedia\(\{kind: 'key'", js) is not None
         and re.search(r"if \(EDIT && armed\) \{ dropMedia\(\{kind: 'pl'", js) is not None)
controle("console : le gestionnaire de médias est le composant commun, monté une seule fois (pas recopié)",
         js.count("MediaLibrary.mount(") == 1 and "class=\"ml-" not in js)
controle("console : l'affectation directe passe par /assign et s'annule (bandeau, sans confirm())",
         "post('/assign'" in js and "showUndo(" in js and re.search(r"(?<![.\w])confirm\(", js) is None)
controle("console : nombre de lecteurs par le chemin d'écriture de l'orchestrateur, droit de déployer exigé",
         "/plugin_config" in js and "containers.deploy" in js and "players: n" in js)
# Écoute (0.1.3) : le flux PCM est DÉCLARÉ (control.streams, relayé tel quel par l'orchestrateur),
# hors du proxy JSON, fermé au démontage, absent de la page publique (le relais public ne sert aucun flux).
from app import plugin_streams as _ps  # noqa: E402
flux = ctrl.get("streams") or []
controle("écoute : /listen.pcm déclaré en flux GET L16 (control.streams valide), hors des chemins JSON",
         not _ps.validate_spec(manifeste) and any(f.get("path") == "/listen.pcm" and f.get("method") == "GET"
                                                  and "L16" in f.get("content_type", "") for f in flux)
         and "/listen.pcm" not in eps and "/listen.pcm" not in reads, str(_ps.validate_spec(manifeste)))
controle("écoute : le flux est lu par api() (ctx.base), fermé au démontage, caché en page publique",
         "api('/listen.pcm')" in js and re.search(r"function unmount\(\) \{\s*listenStop\(\)", js) is not None
         and 'class="cart-listen"${PUBLIC ? \' hidden\' : \'\'}' in js and "if (LST.on || PUBLIC" in js)
controle("éditeur de clip : confirmation intégrée à la page, glisser au doigt et pincer (pointer events), cibles boutons",
         "clipClose(false)" in js and "CE.pinch" in js and "setPointerCapture" in js and "data-ca=\"zoom-in\"" in js)
controle("éditeur de clip : la forme d'onde vient du conteneur par fenêtres (/wave, /montage/wave)",
         "'/wave?asset='" in js and "'/montage/wave'" in js)
controle("console : un menu Sauvegardes par mode, rappel confirmé DANS la page, médias manquants rapportés",
         "openSaves()" in js and "cart-sv-confirm" in js and "missingHtml(" in js
         and "'/api/containers/' + VMID + '/library'" in js and "/presets-import" in js and "/presets-export" in js)
controle("console : les sauvegardes (hors proxy) sont sautées en mode public, droit plugins.operate pour agir",
         re.search(r"function openSaves\(\) \{\n    if \(PUBLIC\) return;", js) is not None
         and "hasPerm('plugins.operate')" in js)
controle("console : passage A/B en trois boutons (laisser jouer, couper, fondu) et durée en ms seulement en fondu",
         all(k in js for k in ("'ho_keep'", "'ho_cut'", "'ho_fade'", "b('keep'", "b('cut'", "b('fade'"))
         and "opt({ab_handover: b.dataset.ho})" in js
         and "const opt = o => post('/pl/options', Object.assign({zone: zid}, o));" in js
         and re.search(r"mode === 'fade' \? `<label class=\"cart-ho-msl\"><input type=\"number\" class=\"cart-ho-ms\"", js) is not None
         and "${pl.ab ? handoverHtml(pl) : ''}" in js)
controle("console : cibles de 44 px pour le passage A/B (boutons cart-btn, champ de durée)",
         re.search(r"\.cart-ho-ms \{[^}]*min-height: 44px", lire("control.css")) is not None)
controle("console : boutons d'enregistrement qui disent ce qu'ils font (grille, playlist, page)",
         all(k in js for k in ("'sv_save_grid'", "'sv_save_playlist'", "'sv_save_page'")))
if shutil.which("node"):
    r = subprocess.run(["node", "--check", os.path.join(DOSSIER, "control.js")], capture_output=True, text=True)
    controle("console : node --check", r.returncode == 0, r.stderr[-300:])
    # La résolution des défauts de la CONSOLE (ce que l'éditeur affiche) = celle du MOTEUR (ce qui
    # joue), sur toutes les combinaisons ; et le rapprochement d'un envoi avec SA touche.
    cas = []
    for propre in (None, 120):
        for page in ({}, {"fade_out_ms": 60}):
            for cart in ({"fade_out_ms": 0}, {"fade_out_ms": 900}):
                cas.append([{"fade_out_ms": propre, "second_press": None, "loop": None, "group": "" if propre else None,
                             "player": 0 if propre else None}, page, dict(E.DEFAULT_FIELDS, **cart)])
    attendu = [E.resolve(it, pg, ct) for it, pg, ct in cas]
    # disposition RENDUE par le moteur (deux sons, une coupe dans le premier) : la console doit en
    # déduire les mêmes temps montés que le moteur pour pré-écouter un point de la vue Source.
    _mj = E.CartEngine(1, SR, {"pages": [{"id": "p", "keys": [{"id": "k", "slot": 0, "media": {"asset": "a"},
          "edit": {"segments": [{"out_ms": 1000, "cuts": [[100, 600]]}, {"media": {"asset": "b"}, "xfade_ms": 200}]}}]}]},
          source_for=lambda m: dc(.1, SR * 2))
    _layout_js = _mj.source_of(_mj.find_key("k")).layout(SR)
    prog = """
globalThis.window = {};
require(%s);
const P = window.MXLPlugins.cartouchier._pure;
const cas = %s;
const lst = [{size: 100, mtime: 1000.5, slot: 1}, {size: 200, mtime: 2000, slot: 2}];
const LAY = %s;
process.stdout.write(JSON.stringify({
  eff: cas.map(c => P.effectiveOf(c[0], c[1], c[2])),
  mine: P.matchUpload(lst, {asset_id: 'a', session: {size: 200, mtime: 2000.3}}),
  other: P.matchUpload(lst, {asset_id: 'a', session: {size: 999, mtime: 1}}),
  nosess_one: P.matchUpload([lst[0]], {asset_id: 'a', session: null}),
  nosess_many: P.matchUpload(lst, {asset_id: 'a', session: null}),
  drop: [P.parseDrop('key:7'), P.parseDrop('pl:0'), P.parseDrop('x:1')],
  map: P.srcToMix ? [P.srcToMix(LAY, 0, 50), P.srcToMix(LAY, 0, 150), P.srcToMix(LAY, 0, 700), P.srcToMix(LAY, 1, 30),
        P.srcToMix(LAY, 0, 0)] : null,
  back: P.mixToSrc ? [P.mixToSrc(LAY, 120), P.mixToSrc(LAY, 1600)] : null,
  env: P.envDbAt ? [P.envDbAt([[100, 0], [200, -12]], 50), P.envDbAt([[100, 0], [200, -12]], 150),
        P.envDbAt([[100, 0], [200, -12]], 900), P.envDbAt([], 5)] : null,
}));
""" % (json.dumps(os.path.join(DOSSIER, "control.js")), json.dumps(cas), json.dumps(_layout_js))
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    try:
        res = json.loads(r.stdout)
    except ValueError:
        res = {}
    controle("console : la résolution des défauts est celle du moteur (toutes combinaisons)",
             res.get("eff") == attendu, (r.stderr[-300:] or json.dumps(res.get("eff"))[:300]))
    controle("console : un envoi est rattaché à SA touche (taille + date du fichier), pas au premier en attente",
             res.get("mine") == 1, str(res.get("mine")))
    controle("console : un import sans rapport ne vole pas un dépôt en attente", res.get("other") == -1)
    controle("console : sans session (média déjà dans la base), rattaché seulement s'il est seul en attente",
             res.get("nosess_one") == 0 and res.get("nosess_many") == -1)
    xf_ = MT.CUT_XFADE_MS
    controle("console : temps source -> temps monté, comme le moteur (dans une pièce, dans une coupe = le raccord, son suivant, In)",
             res.get("map") == [50, 100 - xf_, 100 - xf_ + 100, 100 - xf_ + 400 - 200 + 30, 0],
             str(res.get("map")))
    controle("console : temps monté -> temps source (tête de lecture dans la vue Source)",
             res.get("back") == [{"seg": 0, "t": 600 + 120 - (100 - xf_)}, {"seg": 1, "t": 1600 - (100 - xf_ + 400 - 200)}],
             str(res.get("back")))
    controle("console : valeur de l'enveloppe = loi du moteur (plate avant, linéaire en dB, plate après)",
             res.get("env") == [0, -6, -12, 0], str(res.get("env")))
    controle("console : cibles de dépôt (touche, position de playlist)",
             res.get("drop") == [{"kind": "key", "slot": 7}, {"kind": "pl", "index": 0}, None], str(res.get("drop")))

# Onglets et page publique d'un onglet (0.1.8), côté console, sous node : règles pures (garde des
# POST publics, onglet affiché, règles de l'éditeur de disposition identiques au moteur), puis un
# MONTAGE réel de la console sur un DOM factice, en page publique : chaque URL passe par ctx.base,
# seul l'onglet du lien est rendu, aucun POST n'est tenté en lecture seule.
if shutil.which("node") and ok_charge:
    _S6 = charger_script(tz_plateau().doc, players=6)
    _doc6 = _S6.engine.doc_copy()
    _st6 = _S6.state_view()
    _st6["doc_rev"] = 0
    _slots_cas = [
        ("t1", [{"zone": "za", "kind": "grid", "players": [1, 3]}, {"zone": "zb", "kind": "grid", "players": [2]}]),
        ("t1", [{"zone": "za", "kind": "grid", "players": [1]}, {"zone": "zb", "kind": "grid", "players": [1]}]),
        ("t1", [{"zone": "za", "kind": "playlist_ab", "players": [1]}]),
        ("t1", [{"zone": "za", "kind": "playlist_ab", "players": [1, 6]}, {"zone": None, "kind": "grid", "players": [2]}]),
        ("t1", [{"zone": "zp", "kind": "playlist_ab", "players": [3, 4]}, {"zone": "zb", "kind": "grid", "players": [1, 2]}]),
    ]
    _attendu_le = []
    for _tab, _sl in _slots_cas:
        _e = tz_plateau()
        try:
            _e.set_layout({"tab": _tab, "layout": "2h" if len(_sl) == 2 else "1", "slots": _sl})
            _attendu_le.append(None)
        except E.EngineError as _ex:
            _attendu_le.append(_ex.code)
    prog_pub = r"""
const htmls = [], calls = [];
function mkEl(sel) {
  const el = {_sel: sel, dataset: {}, style: {}, hidden: false, childElementCount: 0, isConnected: true, textContent: '',
    clientWidth: 300, width: 300, height: 44, children: [], value: '',
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    set innerHTML(v) { this._html = v; htmls.push(String(v)); this._c = {}; },
    get innerHTML() { return this._html || ''; },
    querySelector(s) { this._c = this._c || {}; return this._c[s] || (this._c[s] = mkEl(s)); },
    querySelectorAll() { return []; },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, insertBefore() {}, scrollIntoView() {},
    getContext() { return {clearRect() {}, fillRect() {}}; }, closest() { return null; }, contains() { return false; },
    matches() { return false; }, remove() {}};
  return el;
}
globalThis.window = {isSecureContext: false};
globalThis.document = {addEventListener() {}, removeEventListener() {}, activeElement: null,
  createElement: mkEl, head: mkEl('head'), body: mkEl('body')};
globalThis.performance = {now: () => 0};
globalThis.requestAnimationFrame = () => 1; globalThis.cancelAnimationFrame = () => {};
globalThis.getComputedStyle = () => ({getPropertyValue: () => ''});
globalThis.localStorage = {getItem: () => null, setItem() {}};
const DOC = %(doc)s, ST = %(st)s;
globalThis.fetch = async (url, opts) => {
  calls.push({url: String(url), method: (opts && opts.method) || 'GET'});
  const body = /\/state$/.test(url) ? ST : /\/doc$/.test(url) ? DOC : {};
  return {ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body))};
};
require(%(js)s);
const C = window.MXLPlugins.cartouchier, P = C._pure;
let tick = null;
globalThis.setInterval = fn => { tick = fn; return 0; };
async function run(ctx) {
  htmls.length = 0; calls.length = 0;
  const el = mkEl('root');
  C.mount(el, 42, ctx);
  await new Promise(r => setTimeout(r, 30));
  const html = htmls.join('\n');
  // a second poll with NOTHING changed must rebuild nothing (zones, players strip, tab strip)
  const n0 = htmls.length;
  if (tick) { tick(); await new Promise(r => setTimeout(r, 30)); }
  const rebuilt = htmls.slice(n0).filter(h => /cart-zone-head|cart-grid|cart-pl-bar|class="cart-player"|cart-tab/.test(h)).length;
  const out = {calls: calls.slice(), html_zones: Array.from(new Set((html.match(/data-zone="([^"]+)"/g) || []))),
               keys: Array.from(new Set((html.match(/data-key="([^"]+)"/g) || []))),
               players: Array.from(new Set((html.match(/class="cart-player" data-player="(\d+)"/g) || []))),
               go_disabled: /cart-go" disabled/.test(html), tab_buttons: /data-tab="/.test(html), rebuilt,
               polled: calls.filter(c => /\/state$/.test(c.url)).length};
  C.unmount();
  return out;
}
(async () => {
  const ro = await run({base: '/p/TOK', tab: 't2'});
  const play = await run({base: '/p/TOK', tab: 't2', play: true});
  const gone = await run({base: '/p/TOK', tab: 'nope', play: true});
  process.stdout.write(JSON.stringify({ro, play, gone,
    allowed: [P.postAllowed('/fire', null), P.postAllowed('/key', null), P.postAllowed('/fire', {tab: 't', play: false}),
              P.postAllowed('/fire', {tab: 't', play: true}), P.postAllowed('/key', {tab: 't', play: true}),
              P.postAllowed('/player/pause', {tab: 't', play: true})],
    paths: P.PUBLIC_PATHS,
    shown: [(P.tabToShow(DOC, null) || {}).id, (P.tabToShow(DOC, {tab: 't2'}) || {}).id, P.tabToShow(DOC, {tab: 'zz'})],
    kinds: DOC.zones.map(P.zoneKind),
    resize: [P.resizeSlots([{zone: 'a'}], '1+2').length, P.resizeSlots([{zone: 'a'}, {zone: 'b'}, {zone: 'c'}], '2v').map(s => s.zone)],
    le: %(cases)s.map(c => { const r = P.layoutCheck(DOC, c[0], c[1]); return r ? r.code : null; }),
  }));
})();
""" % {"doc": json.dumps(_doc6), "st": json.dumps(_st6), "js": json.dumps(os.path.join(DOSSIER, "control.js")),
       "cases": json.dumps(_slots_cas)}
    r = subprocess.run(["node", "-e", prog_pub], capture_output=True, text=True, timeout=60)
    try:
        jr = json.loads(r.stdout)
    except ValueError:
        jr = {}
    ro, pl_, gone = jr.get("ro") or {}, jr.get("play") or {}, jr.get("gone") or {}
    urls = [c["url"] for c in ro.get("calls") or []] + [c["url"] for c in pl_.get("calls") or []]
    controle("page publique d'un onglet : CHAQUE appel passe par ctx.base (relais du jeton), aucun hors relais",
             bool(urls) and all(u.startswith("/p/TOK/plugin/") for u in urls), (r.stderr[-400:] or str(urls[:6])))
    controle("page publique en lecture seule : aucun POST tenté au montage ni au sondage",
             bool(ro.get("calls")) and all(c["method"] == "GET" for c in ro.get("calls") or []), str(ro.get("calls")))
    controle("page publique : SEULES les zones de l'onglet du lien sont rendues (Antenne : zj, zp, zx)",
             sorted(ro.get("html_zones") or []) == sorted(['data-zone="zj"', 'data-zone="zp"', 'data-zone="zx"']),
             str(ro.get("html_zones")))
    controle("page publique : aucune touche d'un autre onglet, aucun lecteur d'un autre onglet, pas de barre d'onglets",
             'data-key="j1"' in (ro.get("keys") or []) and 'data-key="a1"' not in (ro.get("keys") or [])
             and 'data-key="b1"' not in (ro.get("keys") or []) and not ro.get("tab_buttons")
             and sorted(ro.get("players") or []) == sorted(['class="cart-player" data-player="%d"' % n for n in (3, 4, 5)]),
             json.dumps([ro.get("keys"), ro.get("players")]))
    controle("page publique : GO désactivé en lecture seule, actif quand le lien autorise à jouer",
             ro.get("go_disabled") is True and pl_.get("go_disabled") is False)
    controle("console : un sondage sans changement ne reconstruit RIEN (zones, bandeau des lecteurs, onglets)",
             ro.get("polled", 0) >= 2 and ro.get("rebuilt") == 0 and pl_.get("rebuilt") == 0,
             json.dumps([ro.get("polled"), ro.get("rebuilt"), pl_.get("rebuilt")]))
    controle("page publique : un lien vers un onglet supprimé ne montre AUCUN autre onglet",
             not (gone.get("html_zones") or []) and not (gone.get("keys") or []), str(gone.get("html_zones")))
    controle("console : garde des POST publics (session : tout ; lien : rien sans « jouer », sinon public_actions seules)",
             jr.get("allowed") == [True, True, False, True, False, False], str(jr.get("allowed")))
    controle("console : PUBLIC_PATHS = control.public_actions du manifeste",
             jr.get("paths") == (ctrl.get("public_actions") or []), str(jr.get("paths")))
    controle("console : onglet affiché (le partagé, celui du lien, rien si le lien est périmé)",
             jr.get("shown") == ["t1", "t2", None], str(jr.get("shown")))
    controle("console : genre d'une zone = celui du moteur",
             jr.get("kinds") == [E.zone_kind(z) for z in _doc6["zones"]], str(jr.get("kinds")))
    controle("console : redimensionner une disposition garde les zones dans l'ordre, ajoute des grilles vides",
             jr.get("resize") == [3, ["a", "b"]], str(jr.get("resize")))
    controle("éditeur de disposition : la console applique les MÊMES règles que le moteur (lecteur pris, en double, A/B à un lecteur)",
             jr.get("le") == _attendu_le and _attendu_le[:3] == ["player_taken", "player_twice", "ab_needs_two_players"],
             json.dumps([jr.get("le"), _attendu_le]))

shutil.rmtree(_tmp, ignore_errors=True)
shutil.rmtree(_media, ignore_errors=True)

# ── Verdict ──────────────────────────────────────────────────────────────────────────────────
print("cartouchier — vérification hors ligne\n")
for r in reussites:
    print("  OK    %s" % r)
# ── Petits défauts du 2026-10-18 ─────────────────────────────────────────────────────────────
_cjs, _ccss = lire("control.js"), lire("control.css")
_ls = _ccss.split(".cart-listen select {")[1].split("}")[0]
controle("pré-écoute : le menu suit le thème (fond et texte des jetons, pas le blanc du navigateur)",
         "background: var(--bg-input)" in _ls and "color: var(--text)" in _ls)
controle("lecteur sans zone : il le dit et dit où l'attribuer",
         "T('zone_none_hint'" in _cjs and "cart-player-zone--none" in _cjs and ".cart-player-zone--none {" in _ccss)
_tpl = open(os.path.join(RACINE, "templates/plugin_section.html"), encoding="utf-8").read()
controle("lien public : le badge nomme l'onglet (son nom, pas son identifiant)",
         'data-ps-tab="' in _tpl and "querySelectorAll('[data-ps-tab]')" in _tpl and "n.textContent = t.label" in _tpl)

# ── Sorties régulières (0.1.11, 2026-10-19) ──────────────────────────────────────────────────
# Mesuré sur 1176 : 121 ms de retard sur la grille TAI après 10 h (cadence sur time.monotonic) et des
# pointes de 302 échantillons (GIL à 5 ms, fil audio en CFS) — une console lisant 144 derrière passait à sec.
_at = script.split("def audio_thread():")[1].split("\ndef ")[0]
controle("fil audio : cadencé sur l'index TAI des échantillons, jamais sur l'horloge monotone",
         "bobimxl.current_index(SR)" in _at and "next_idx + BLOCK <= now_idx + LEAD" in _at
         and "time.monotonic() - t0" not in _at,
         "une cadence monotone dérive de la grille TAI (~4 ppm mesurés)")
controle("fil audio : temps réel demandé dès son démarrage (SCHED_FIFO, refus publié)",
         "_apply_rt()" in _at.split("while True")[0] and "os.SCHED_FIFO" in script and 'metrics["rt"] = rt' in script)
_main = script.split("def main():")[1]
controle("GIL rendu en 0,5 ms au fil audio", "sys.setswitchinterval(0.0005)" in _main.split("Thread(")[0])
_mf = json.loads(lire("plugin.json"))
_rtmax = [f for f in _mf["config_schema"] if f.get("key") == "rt_priority"]
controle("manifeste : SYS_NICE et rtprio couvrent la priorité demandée",
         "SYS_NICE" in (_mf["resources"].get("cap_add") or []) and _rtmax
         and (_mf["resources"].get("ulimits") or {}).get("rtprio", 0) >= _rtmax[0]["max"])
_lead = re.search(r"^LEAD = ([^#\n]+)", script, re.M)
_lead_v = eval(_lead.group(1), {"BLOCK": 48}) if _lead else 0
controle("écriture en avance de 1 à 5 ms sur l'instant TAI (absorbe une requête qui tient le GIL)",
         48 <= _lead_v <= 240, "LEAD = %r échantillons" % _lead_v)

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
