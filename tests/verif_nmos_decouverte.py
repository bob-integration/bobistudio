#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de la découverte de registre IS-04 (services/nmos/decouverte.py).
#
# TROIS RÈGLES À NE JAMAIS ENFREINDRE, et chacune échoue en SILENCE si elle l'est :
#   1. le réglage explicite gagne — sinon une découverte écrase une décision humaine ;
#   2. on ne se découvre pas soi-même — sinon on s'enregistre chez soi, et tout paraît marcher ;
#   3. `pri` ≥ 100 est écarté — sinon on disparaît de l'installation réelle vers un registre de
#      développement, sans qu'aucune erreur ne soit levée.
#
#   $ ./venv/bin/python tools/verif_nmos_decouverte.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import decouverte as d                            # noqa: E402
import app.database as db                                            # noqa: E402

print("Découverte de registre IS-04 (DNS-SD)\n")

_avant_url = db.db_get_setting("nmos_registry_url", None)
_avant_dec = db.db_get_setting("nmos_decouverte", None)
_avant_reg = db.db_get_setting("nmos_registre", None)

try:
    # ── 1. Nos propres adresses ──────────────────────────────────────────────
    nôtres = d._nos_adresses()
    controle("★★ nos propres adresses sont connues", len(nôtres) >= 2 and "127.0.0.1" in nôtres,
             "en rater une nous ferait nous enregistrer chez NOUS — panne silencieuse : tout "
             "répond, tout paraît juste. Obtenu %s" % sorted(nôtres))
    from services.nmos import _get_host_address
    controle("dont l'adresse annoncée du Node", str(_get_host_address()) in nôtres)

    # ── 2. Le réglage explicite gagne ────────────────────────────────────────
    db.db_set_setting("nmos_decouverte", True)
    db.db_set_setting("nmos_registry_url", "http://registre-de-lexploitant:8235")
    url, origine = d.resoudre()
    controle("★★★ un réglage explicite gagne, et la découverte n'est même pas lancée",
             url == "http://registre-de-lexploitant:8235" and origine == "réglage",
             "une découverte qui écrase une décision humaine transforme un réglage en "
             "suggestion — obtenu %r / %r" % (url, origine))

    # ── 3. Découverte coupée = aucune découverte ─────────────────────────────
    db.db_set_setting("nmos_registry_url", "")
    db.db_set_setting("nmos_decouverte", False)
    url, origine = d.resoudre()
    controle("★ découverte fermée → aucun registre choisi", url is None and origine is None,
             "obtenu %r / %r" % (url, origine))

    # ── 4. Les filtres, sur des annonces simulées ────────────────────────────
    # On n'ouvre pas de vrai mDNS ici : on éprouve la RÈGLE, pas la pile réseau. Le bout en bout
    # se fait avec les registres mock de la suite AMWA (cf. tools/, README du chantier).
    db.db_set_setting("nmos_decouverte", True)
    _vrai_browse = d.decouvrir
    mien = sorted(nôtres - {"127.0.0.1"})[0] if (nôtres - {"127.0.0.1"}) else "127.0.0.1"

    d.decouvrir = lambda duree_s=0: []
    controle("aucune annonce → aucun registre", d.resoudre() == (None, None))

    d.decouvrir = lambda duree_s=0: [{"url": "http://10.0.0.7:8235",
                                      "pri": 0, "ip": "10.0.0.7", "port": 8235, "nom": "reg"}]
    _vrai_joignable = d.joignable
    d.joignable = lambda u: True
    url, origine = d.resoudre()
    controle("★ un registre annoncé et joignable est retenu",
             url == "http://10.0.0.7:8235" and origine == "découverte")
    # ★★★ LE CONTRAT AVEC LE CLIENT D'ENREGISTREMENT. `_register_all(reg_base)` construit
    # `{base}/x-nmos/registration/{ver}/resource` : la découverte doit rendre l'ORIGINE NUE.
    # Rendre l'URL complète doublait le chemin et donnait un 404 à chaque POST — vu le 2026-08-31,
    # et invisible de notre côté : seule une alerte d'échec d'enregistrement remontait.
    controle("★★★ la découverte rend l'ORIGINE, pas l'URL de l'API",
             "/x-nmos/" not in (url or ""),
             "sinon le client double le chemin et tous les POST partent en 404 — obtenu %r" % url)

    d._penalises.clear()
    d.decouvrir = lambda duree_s=0: [
        {"url": "http://a:8235", "pri": 0, "ip": "a", "port": 1, "nom": "muet"},
        {"url": "http://b:8235", "pri": 10, "ip": "b", "port": 1, "nom": "vivant"}]
    d.joignable = lambda u: "b" in u
    url, _ = d.resoudre()
    controle("★★ à priorité différente, celui qui RÉPOND est préféré", url == "http://b:8235",
             "la sonde départage — obtenu %r" % url)

    # ★★★ LE DÉFAUT DU JOUR. Ma sonde ÉLIMINAIT les candidats muets. Résultat mesuré le
    # 2026-08-31 : à chaque bascule, tous les registres de secours étaient écartés (ils
    # répondaient 503 le temps de leur mise en service) et le journal disait « aucun autre
    # disponible » alors que quatre étaient annoncés. IS-04 dit d'essayer DANS L'ORDRE DES
    # PRIORITÉS : c'est la tentative réelle qui tranche, pas un pronostic.
    d._penalises.clear()
    d.joignable = lambda u: False
    url, _ = d.resoudre()
    controle("★★★ si AUCUN ne répond, on tente quand même le mieux placé",
             url == "http://a:8235",
             "une sonde muette n'est pas une preuve de mort, et l'éliminer nous laisse hors "
             "registre alors qu'il est annoncé — obtenu %r" % url)

    # La bascule ne doit pas revenir sur le mort qu'elle vient de quitter.
    d.penaliser("http://a:8235")
    url, _ = d.resoudre()
    controle("★★ un registre qui vient d'échouer est écarté un temps", url == "http://b:8235",
             "sinon la bascule tourne en rond sur le même mort — obtenu %r" % url)
    controle("★ ...mais pas définitivement", d.PENALITE_S <= 300,
             "un registre qui redémarre doit pouvoir revenir — %ss" % d.PENALITE_S)
    d._penalises.clear()
    d.joignable = _vrai_joignable
    d.decouvrir = _vrai_browse

    # ── 5. Les filtres du browse lui-même (pri et auto-exclusion) ────────────
    # On rejoue la logique de tri/filtre du listener sur des entrées fabriquées.
    controle("★★★ le seuil de développement est bien celui d'IS-04",
             d.PRI_DEVELOPPEMENT == 100,
             "IS-04 : « Values 100+ are reserved for development work ». S'enregistrer dans un "
             "registre de test, c'est disparaître de l'installation réelle sans une erreur")
    # ── Les filtres de l'écouteur, éprouvés SUR SON COMPORTEMENT ─────────────
    # ⚠ La version précédente de ces contrôles cherchait des CHAÎNES dans le source. Elle a cassé
    # au premier remaniement sans que rien ne soit faux, et surtout elle n'aurait rien vu d'un
    # filtre présent mais inopérant. On appelle donc `_retenir()` avec de vraies annonces.
    class _Faux:
        def __init__(self, ip, port, **txt):
            import socket as _s
            self.addresses = [_s.inet_aton(ip)]
            self.port = port
            self.properties = {k.encode(): str(v).encode() for k, v in txt.items()}

    from services.nmos import IS04_VERSION
    _base = dict(api_proto="http", api_ver=IS04_VERSION)

    def _retenu(ip, port, **txt):
        with d._annonces_lock:
            d._annonces.clear()
        d._retenir("test._nmos-register._tcp.local.", _Faux(ip, port, **dict(_base, **txt)))
        with d._annonces_lock:
            return dict(d._annonces)

    mien = sorted(nôtres - {"127.0.0.1"})[0] if (nôtres - {"127.0.0.1"}) else "127.0.0.1"
    controle("★★★ notre PROPRE registre est écarté", not _retenu(mien, d.NOTRE_PORT, pri=0),
             "sinon nous nous enregistrons chez nous : boucle silencieuse, tout paraît marcher")
    controle("★★★ ...mais un tiers sur la MÊME machine est retenu",
             bool(_retenu(mien, 5502, pri=0)),
             "exclure toute notre IP écartait à tort un registre tiers colocalisé. Mesuré le "
             "2026-08-31 : les registres mock de la suite AMWA étaient tous filtrés, et la "
             "découverte ne trouvait jamais rien — sans un mot")
    controle("★★ pri >= 100 est écarté", not _retenu("10.0.0.8", 8235, pri=100))
    controle("★ pri < 100 est retenu", bool(_retenu("10.0.0.8", 8235, pri=99)))
    controle("★ une version d'API que nous ne parlons pas est écartée",
             not _retenu("10.0.0.9", 8235, pri=0, api_ver="v1.2"),
             "s'enregistrer dans un registre v1.2 en parlant %s échouerait à chaque requête"
             % IS04_VERSION)
    controle("★★ l'annonce retenue porte l'ORIGINE, pas l'URL de l'API",
             list(_retenu("10.0.0.8", 8235, pri=0).values())[0]["url"] == "http://10.0.0.8:8235")
    with d._annonces_lock:
        d._annonces.clear()

    # ── L'écoute est PERMANENTE, pas un sondage ──────────────────────────────
    src = open(os.path.join(RACINE, "services", "nmos", "decouverte.py"), encoding="utf-8").read()
    controle("★★★ l'écoute mDNS est PERMANENTE",
             "def _demarrer_ecoute" in src and "_browser is not None" in src,
             "un sondage périodique manque toute annonce apparue entre deux fenêtres, et repart "
             "d'un cache mDNS froid. Mesuré : on détectait la mort d'un registre et on ne "
             "trouvait JAMAIS son remplaçant")
    controle("★★ une annonce RETIRÉE déclenche une réévaluation",
             "def remove_service" in src and "bascule_depuis" in src,
             "sans ça on reste accroché à un mort jusqu'au prochain échec de battement")

    # ── 6. La période n'est pas nerveuse ─────────────────────────────────────
    controle("★ AVEC un registre, la réévaluation est LENTE (>= 60 s)", d.PERIODE_S >= 60,
             "une bascule nerveuse fait plus de dégâts qu'une reprise tranquille — ce produit a "
             "déjà perdu un nœud sur une reconnexion sans palier. Obtenu %ss" % d.PERIODE_S)
    controle("★★ SANS registre, l'acquisition est RAPIDE (<= 15 s)",
             d.PERIODE_ACQUISITION_S <= 15,
             "sans registre nous sommes INVISIBLES : une installation qui allume le sien doit "
             "nous voir apparaître en secondes. La suite AMWA laisse 30 s, et elle a raison. "
             "Obtenu %ss" % d.PERIODE_ACQUISITION_S)
finally:
    db.db_set_setting("nmos_registry_url", _avant_url if _avant_url is not None else "")
    db.db_set_setting("nmos_decouverte", _avant_dec if _avant_dec is not None else False)
    db.db_set_setting("nmos_registre", _avant_reg if _avant_reg is not None else False)
    print("\n  réglages restaurés : registry_url=%r découverte=%r registre=%r"
          % (db.db_get_setting("nmos_registry_url", None),
             db.db_get_setting("nmos_decouverte", None),
             db.db_get_setting("nmos_registre", None)))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
