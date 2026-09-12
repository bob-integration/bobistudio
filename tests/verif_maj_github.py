***REMOVED***!/usr/bin/env python3
***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED***
***REMOVED*** Banc de la MISE À JOUR DEPUIS GITHUB (détection + application) et du catalogue sur releases.
***REMOVED***
***REMOVED*** ★ CE QUI MANQUAIT. `updater.py` mettait à jour d'instance à INSTANCE sur le réseau local ; le
***REMOVED*** catalogue n'installait que des composants, et ne retenait que les dépôts préfixés. Le dépôt de
***REMOVED*** l'orchestrateur tombait dans le `continue` : un utilisateur extérieur installait une version et
***REMOVED*** n'avait AUCUN moyen d'obtenir la suivante — alors que ses plugins, eux, se mettaient à jour.
***REMOVED***
***REMOVED*** ⚠ AUCUN ACCÈS RÉSEAU ICI. Tout est simulé : ce banc vérifie les DÉCISIONS (quelle release, quel
***REMOVED*** artefact, quel refus), pas la disponibilité de GitHub.
***REMOVED***
***REMOVED***   $ ./venv/bin/python tests/verif_maj_github.py
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


from app import catalogue as C                                       ***REMOVED*** noqa: E402
from app import updater as U                                         ***REMOVED*** noqa: E402
from app import version as V                                         ***REMOVED*** noqa: E402

print("Mise à jour depuis GitHub\n")

_REP = {}


def _faux_http(url):
    for cle, val in _REP.items():
        if cle in url:
            if isinstance(val, Exception):
                raise val
            return val
    raise RuntimeError("URL non simulée : %s" % url)


C._http_json = _faux_http

REL = lambda tag, assets=(), draft=False, pre=True: {          ***REMOVED*** noqa: E731
    "tag_name": tag, "draft": draft, "prerelease": pre, "html_url": "u", "published_at": "2026-09-02",
    "assets": [{"name": n, "browser_download_url": "https://x/" + n} for n in assets]}

***REMOVED*** ═══ 1. QUELLE RELEASE EST RETENUE ══════════════════════
_REP.clear(); _REP["/releases"] = [REL("v0.9.1"), REL("v0.10.0"), REL("v0.2.0")]
t, e = C._derniere_release("org", "depot")
controle("★★★ la PLUS HAUTE est retenue, numériquement", t == "v0.10.0",
         "★ lexicalement « v0.10.0 » précède « v0.2.0 » : un tri de chaînes rendrait la plus "
         "ancienne, et une mise à jour publiée resterait invisible. Obtenu %r" % t)

_REP["/releases"] = [REL("v9.9.9", draft=True), REL("v0.3.0")]
t, _ = C._derniere_release("org", "depot")
controle("★★ un BROUILLON est ignoré", t == "v0.3.0",
         "un brouillon n'est pas publié : le proposer enverrait vers du vide. Obtenu %r" % t)

_REP["/releases"] = [REL("main"), REL("v0.3.0"), REL("nightly")]
t, _ = C._derniere_release("org", "depot")
controle("★★ un tag non numérique est écarté sans faire échouer le reste", t == "v0.3.0",
         "obtenu %r" % t)

_REP["/releases"] = []
t, e = C._derniere_release("org", "depot")
controle("★ aucune release → pas de tag, pas de motif d'erreur", t is None and not e)

***REMOVED*** ★ LA DISTINCTION QUI COMPTE : ne pas avoir pu demander n'est pas ne rien avoir.
_REP["/releases"] = RuntimeError("HTTP Error 403: rate limit exceeded")
t, e = C._derniere_release("org", "depot")
controle("★★★ « je n'ai pas pu demander » ≠ « il n'y en a pas »",
         t is None and "impossible" in e,
         "sans cette distinction, un quota d'API épuisé ferait afficher « aucune release » sur "
         "TOUS les composants — un catalogue qui paraît vide alors que tout est publié, et rien "
         "pour le comprendre. Obtenu tag=%r motif=%r" % (t, e))

***REMOVED*** ═══ 2. QUEL ARTEFACT EST ACCEPTÉ ═══════════════════════
C._reglages = lambda: ("org", 1800, True)
C.DEPOT_CORE = "bobistudio"

_REP.clear(); _REP["/releases/latest"] = REL("v0.9.2", ("bobistudio.zip", "SHA256SUMS"))
z, s, t = U._asset_release()
controle("★★★ une release COMPLÈTE donne ses deux artefacts",
         z and z.endswith("bobistudio.zip") and s and s.endswith("SHA256SUMS") and t == "v0.9.2",
         "obtenu %r / %r / %r" % (z, s, t))

_REP["/releases/latest"] = REL("v0.9.2", ("bobistudio.zip",))
z, s, motif = U._asset_release()
controle("★★★ SANS empreinte, on REFUSE", z is None and "artefact" in motif,
         "★ on installe en ROOT. L'archive de source que GitHub sert d'office n'embarque ni "
         "l'installeur ni d'empreinte ; s'en contenter reviendrait à exécuter du code non "
         "vérifié — exactement ce que get.sh s'interdit. Obtenu %r" % motif)

_REP["/releases/latest"] = REL("v0.9.2", ("SHA256SUMS",))
z, _, motif = U._asset_release()
controle("★★ ...et sans le paquet non plus", z is None and "artefact" in motif)

_REP["/releases/latest"] = REL("v0.9.2", ())
z, _, motif = U._asset_release()
controle("★★ une release NUE est refusée, avec son motif", z is None and motif)

***REMOVED*** `latest` ignore les pré-versions : sur un produit en bêta il rend 404 alors que des releases
***REMOVED*** existent. Le repli par la liste doit rattraper — sinon la détection ne verrait jamais rien.
_REP.clear()
_REP["/releases/latest"] = RuntimeError("HTTP Error 404: Not Found")
_REP["/releases/tags/v0.9.2"] = REL("v0.9.2", ("bobistudio.zip", "SHA256SUMS"))
_REP["/releases"] = [REL("v0.9.2", ("bobistudio.zip", "SHA256SUMS"))]
z, s, t = U._asset_release()
controle("★★★ `latest` en 404 sur une bêta → repli par la LISTE",
         z is not None and t == "v0.9.2",
         "GitHub exclut les pré-versions de `latest` : sans repli, un produit entièrement en "
         "bêta ne verrait JAMAIS ses propres releases. Obtenu %r / %r" % (z, t))

***REMOVED*** ═══ 3. LE COMPARATEUR DE VERSION DU CŒUR ═══════════════
***REMOVED***
***REMOVED*** ⚠ `force=True` À CHAQUE CAS, et ce n'est pas une facilité : la lecture du cœur est MISE EN
***REMOVED*** CACHE depuis le 2026-09-03 (elle était relue à chaque affichage de la page, sur un quota
***REMOVED*** GitHub de 60 requêtes/heure). Ce banc, lui, enchaîne trois états amont DIFFÉRENTS dans le même
***REMOVED*** processus — sans forcer, il recevrait trois fois le premier et validerait n'importe quoi.
***REMOVED*** C'est ce même besoin qui a révélé que le bouton « Relire » ne rafraîchissait pas le cœur.
_REP.clear(); _REP["/releases"] = [REL("v" + V.VERSION)]
dispo, info = C.maj_core_disponible(force=True)
controle("★★ à égalité, aucune mise à jour n'est proposée", not dispo and info)

_REP["/releases"] = [REL("v99.0.0", ("bobistudio.zip", "SHA256SUMS"))]
dispo, info = C.maj_core_disponible(force=True)
controle("★★★ une version SUPÉRIEURE est proposée, et dite applicable",
         dispo and info.get("applicable"), "obtenu %r" % info)

_REP["/releases"] = [REL("v99.0.0")]
dispo, info = C.maj_core_disponible(force=True)
controle("★★★ ...mais pas applicable si la release est nue",
         dispo and not info.get("applicable"),
         "annoncer applicable une mise à jour qui échouerait est pire que se taire. Obtenu %r"
         % info)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
