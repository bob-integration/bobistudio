# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Catalogue des plugins et services PUBLIÉS : lecture d'une organisation GitHub,
# comparaison avec l'installé, téléchargement du paquet.
#
# ★ POURQUOI CE FICHIER EXISTE. Un exploitant n'a pas à cloner un dépôt git pour
# installer un traitement vidéo. Les sous-modules sont l'outil de DÉVELOPPEMENT ;
# la distribution passe par ici. La moitié du chemin existait déjà — `/api/plugins/
# import` accepte un `.mxlplugin` (zip du dossier) et `_extract_validated_package`
# tolère un dossier racine englobant, donc une archive GitHub s'y branche telle
# quelle. Il ne manquait que la LISTE.
#
# ⚠ CE QU'ON INSTALLE, C'EST DU CODE QUI TOURNERA DANS LE CONTRÔLEUR. Le corps
# d'un plugin s'exécute dans un conteneur, jamais ici — mais `hooks.py` est
# l'exception documentée : il est importé et exécuté DANS l'orchestrateur
# (`plugins._load_hooks`). Installer depuis Internet, c'est donc exécuter du code
# tiers avec les droits du contrôleur. D'où trois garde-fous qui ne sont pas
# négociables :
#   1. une ORGANISATION de confiance, réglable, jamais une URL libre ;
#   2. la permission `settings.edit` sur les routes ;
#   3. l'interface qui le DIT, au lieu de le taire derrière un bouton « installer ».
#
# ⚠ ET UN SERVICE N'EST PAS UN PLUGIN — mais il a SON registre. `app/core_plugins.py`
# gère les services exactement comme `app/plugins.py` gère les plugins : validation
# de paquet, versions archivées, activation. Le catalogue s'y branche au lieu de
# réécrire un chemin d'installation à lui : deux chemins finissent par diverger, et
# celui qui sert le moins est celui qui dérive.
#
# Ce qui reste vrai de la différence : `main.py` importe les services par leur nom
# au démarrage, donc un service installé à chaud ne devient effectif qu'au
# REDÉMARRAGE du contrôleur. Le catalogue le dit ; il ne le laisse pas deviner.
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from . import core_plugins as _cp
from . import plugins as _pl
from . import settings as _st

log = logging.getLogger(__name__)

# Convention de nommage des dépôts publiés. Ce sont ces préfixes qui distinguent
# un paquet d'un dépôt quelconque de l'organisation : le catalogue ne propose
# QUE ce qui se nomme comme un paquet.
PREFIXE_PLUGIN = "bobistudio-plugin-"
PREFIXE_SERVICE = "bobistudio-service-"

# Le dépôt de l'ORCHESTRATEUR lui-même. Il ne porte aucun des deux préfixes, donc `_construire`
# l'ignore — c'est voulu : ce n'est pas un composant installable, on ne l'installe pas, on le MET
# À JOUR. Il est nommé ici pour que `derniere_version_core()` sache où regarder.
DEPOT_CORE = "bobistudio"

# ⚠ L'API GitHub ANONYME est limitée à 60 requêtes par heure et par adresse. Le
# listing coûte UNE requête ; les manifestes passent par raw.githubusercontent.com,
# qui est un CDN et ne compte pas dans ce quota. C'est ce découpage qui rend le
# catalogue utilisable sans jeton — donc sans demander un secret à l'exploitant
# pour lire des dépôts publics.
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CODELOAD = "https://codeload.github.com"

_TIMEOUT = 8
_TAILLE_MAX = 20 * 1024 * 1024          # même plafond que l'import manuel

_verrou = threading.Lock()
_cache = {"t": 0.0, "entrees": [], "erreur": None, "org": None}


def _reglages():
    """(organisation, TTL du cache, catalogue actif).

    ⚠ L'ORGANISATION NE VIENT PAS DES RÉGLAGES. Elle a d'abord été un champ de la
    page Réglages — et c'était une erreur de conception : c'est le seul point de
    confiance de tout le mécanisme, puisque installer un plugin exécute son
    `hooks.py` dans l'orchestrateur. Modifiable depuis le web, elle transformait
    `settings.edit` en « exécuter du code arbitraire sur le contrôleur ».

    Elle vient donc du code (`config.CATALOGUE_ORG`), surchargeable par
    `config_local.py` seul — ce qui exige un accès au serveur. Les mises à jour du
    produit viennent de cette organisation par définition ; la surcharge n'existe
    que pour un fork ou un banc d'essai.

    Reste réglable en base : l'INTERRUPTEUR (un site peut interdire toute sortie
    vers Internet) et la durée de cache. Ni l'un ni l'autre n'ouvre de porte."""
    from . import config as _cfg
    org = str(getattr(_cfg, "CATALOGUE_ORG", "bob-integration") or "").strip()
    ttl = int(_st.get("catalogue_ttl_s", 1800) or 1800)
    actif = str(_st.get("catalogue_actif", "1")).strip().lower() not in ("0", "false", "off", "")
    return org, ttl, actif


def _http_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "bobistudio-catalogue",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_texte(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bobistudio-catalogue"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8")


def _version_service_installee(nom):
    """Version d'un service installé, d'après SON registre. None s'il est absent."""
    try:
        if not _cp.is_service(nom):
            return None
        return (_cp._entry(nom) or {}).get("manifest", {}).get("version") or ""
    except Exception:
        return None


def _derniere_release(org, depot):
    """Tag de la dernière release publiée de ce dépôt, ou None.

    ⚠ COÛT EN QUOTA. Le listing de l'organisation coûte UNE requête d'API ; ceci en coûte une
    DE PLUS PAR DÉPÔT, et l'API anonyme est plafonnée à 60 par heure et par adresse. C'est le
    prix de la reproductibilité — une branche dit « l'état du dépôt à cet instant », ce qui n'est
    pas une version. Le cache de 30 minutes (`catalogue_ttl_s`) tient l'ensemble largement sous
    le plafond ; un rafraîchissement FORCÉ, lui, le paie plein.

    Les pré-versions comptent : le produit est en bêta, les ignorer ne montrerait rien.
    """
    try:
        rels = _http_json("%s/repos/%s/%s/releases?per_page=10" % (API, org, depot))
    except Exception as e:
        # ★ « JE N'AI PAS PU DEMANDER » N'EST PAS « IL N'Y EN A PAS ». Rendre None dans les deux
        # cas ferait afficher « aucune release publiée » sur TOUS les composants dès que le quota
        # d'API est épuisé — un catalogue qui paraît vide alors que tout est publié, et rien pour
        # le comprendre. On distingue, et l'appelant le dit.
        return None, "interrogation impossible (%s)" % e
    from .version import analyser
    meilleure = None
    for r in rels or []:
        if r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")
        v = analyser(tag)
        if v is not None and (meilleure is None or v > meilleure[0]):
            meilleure = (v, tag)
    return (meilleure[1] if meilleure else None), ""


def _manifeste_distant(org, depot, ref, fichier):
    """Manifeste d'un dépôt, lu au fil de l'eau. None si absent ou illisible.

    `ref` est un TAG de release, plus une branche. On lit quand même le MANIFESTE et non le nom
    du tag : c'est la même source que celle qui fera foi après installation, et une étiquette
    peut exister sans que le manifeste ait été bumpé — on annoncerait alors une mise à jour qui
    n'en est pas, et l'exploitant réinstallerait la même chose.

    Passe par le CDN raw.githubusercontent : ne compte PAS dans le quota d'API."""
    url = "%s/%s/%s/%s/%s" % (RAW, org, depot, ref or "HEAD", fichier)
    try:
        return json.loads(_http_texte(url))
    except Exception:
        return None


def _comparer(dispo, installee):
    """« absent », « a_jour », « maj » ou « locale_plus_recente »."""
    if installee is None:
        return "absent"
    if not dispo:
        return "inconnu"
    if str(dispo) == str(installee):
        return "a_jour"
    return "maj" if _pl._ver_key(dispo) > _pl._ver_key(installee) else "locale_plus_recente"


def _construire(org):
    """Interroge l'organisation et rend la liste des paquets publiés."""
    depots = _http_json("%s/orgs/%s/repos?per_page=100&type=public" % (API, org))
    entrees = []
    for d in depots:
        nom = d.get("name") or ""
        if nom.startswith(PREFIXE_PLUGIN):
            genre, ident, fichier = "plugin", nom[len(PREFIXE_PLUGIN):], "plugin.json"
        elif nom.startswith(PREFIXE_SERVICE):
            genre, ident, fichier = "service", nom[len(PREFIXE_SERVICE):], "manifest.json"
        else:
            continue
        # ★ ON SE BASE SUR LES RELEASES, PLUS SUR LA BRANCHE (décidé le 2026-09-02). Une branche
        # dit « l'état du dépôt à cet instant » : deux clients installant le même composant à un
        # jour d'intervalle pouvaient recevoir deux codes différents, tous deux annonçant la même
        # version — le numéro venant du manifeste sur la branche, que rien n'oblige à bumper. Le
        # catalogue distribuait donc sans versions, ce qui privait d'objet l'épinglage par
        # conteneur et rendait « mettre à jour vers 0.115.2 » ambigu.
        tag, err_rel = _derniere_release(org, nom)
        man = (_manifeste_distant(org, nom, tag, fichier) or {}) if tag else {}
        # ★ L'IDENTITÉ VIENT DU MANIFESTE, PAS DU NOM DU DÉPÔT. Un dépôt se RENOMME —
        # `bobistudio-plugin-helloworld` est devenu `...-hello_world` le 2026-09-01, et
        # GitHub a simplement posé une redirection. Dériver le type du nom du dépôt aurait
        # donc changé l'identité d'un plugin déjà installé : la mise à jour se serait posée
        # À CÔTÉ de l'existant au lieu de le remplacer, sans que rien ne le signale.
        type_ = (man.get("type") or man.get("id") or ident) if man else ident
        if genre == "plugin":
            installee = (_pl.get(type_) or {}).get("version") if _pl.is_plugin(type_) else None
        else:
            installee = _version_service_installee(type_)
        dispo = man.get("version") or ""
        entrees.append({
            "genre":      genre,
            "type":       type_,
            "depot":      nom,
            "tag":        tag or "",
            "label":      man.get("label") or man.get("name") or type_,
            "description": (d.get("description") or "").strip(),
            "version_dispo":    dispo,
            "version_installee": installee,
            "etat":       _comparer(dispo, installee),
            "url":        d.get("html_url") or "",
            # Un manifeste illisible n'est pas une erreur fatale : le dépôt existe,
            # on l'affiche, mais on refuse de l'installer sans savoir ce qu'il est.
            "manifeste_lu": bool(man),
            # ★ UN DÉPÔT SANS RELEASE RESTE LISTÉ, avec son motif. Le faire DISPARAÎTRE serait la
            # faute qu'on passe la journée à traquer : l'exploitant chercherait un composant qu'il
            # sait publié, ne le trouverait pas, et n'aurait rien pour comprendre. On le montre,
            # on refuse de l'installer, et on dit pourquoi.
            "installable": bool(tag) and bool(man),
            "indisponible": ("" if tag else (err_rel or "aucune release publiée sur ce dépôt")),
        })
    entrees.sort(key=lambda e: (e["genre"], e["label"].lower()))
    return entrees


def lister(force=False):
    """Catalogue, depuis le cache si possible. Ne lève jamais.

    ★ L'ERREUR EST UNE DONNÉE, PAS UNE EXCEPTION. Un contrôleur sans accès Internet
    est un cas NORMAL (site isolé, coupure) : le catalogue doit alors dire qu'il
    n'a pas pu lire, pas disparaître. Et on garde la dernière liste connue —
    périmée et annoncée comme telle vaut mieux que vide et muette."""
    org, ttl, actif = _reglages()
    if not actif:
        return {"actif": False, "entrees": [], "erreur": None, "org": org, "age_s": None}
    with _verrou:
        frais = (time.time() - _cache["t"]) < ttl and _cache["org"] == org
        if _cache["entrees"] and frais and not force:
            return {"actif": True, "entrees": list(_cache["entrees"]), "erreur": _cache["erreur"],
                    "org": org, "age_s": round(time.time() - _cache["t"], 1)}
    try:
        entrees = _construire(org)
        erreur = None
    except urllib.error.HTTPError as e:
        # 403 sur l'API GitHub = quota anonyme épuisé, pas un refus d'accès. Le
        # dire précisément évite de faire chercher un problème de droits.
        entrees, erreur = None, ("quota GitHub anonyme épuisé (60 requêtes/heure) — réessayez plus tard"
                                 if e.code == 403 else "GitHub a répondu HTTP %s" % e.code)
    except urllib.error.URLError as e:
        entrees, erreur = None, "pas d'accès à GitHub : %s" % (getattr(e, "reason", e),)
    except Exception as e:
        entrees, erreur = None, "catalogue illisible : %r" % (e,)
    with _verrou:
        if entrees is not None:
            _cache.update({"t": time.time(), "entrees": entrees, "erreur": None, "org": org})
        else:
            _cache["erreur"] = erreur
        return {"actif": True, "entrees": list(_cache["entrees"]), "erreur": _cache["erreur"],
                "org": org,
                "age_s": None if not _cache["t"] else round(time.time() - _cache["t"], 1)}


def entree(depot):
    """L'entrée du catalogue pour ce dépôt, ou None. Sert de LISTE BLANCHE."""
    for e in lister().get("entrees") or []:
        if e["depot"] == depot:
            return e
    return None


def derniere_version_core():
    """La dernière version publiée de Bobi.Studio, ou None.

    ★ POURQUOI ÇA N'EXISTAIT PAS. Le catalogue ne retient que les dépôts préfixés
    `bobistudio-plugin-` / `bobistudio-service-` ; celui de l'orchestrateur tombe dans le
    `continue` et n'était donc JAMAIS vu. `app/config.py` affirmait pourtant que « les mises à
    jour de Bobi.Studio viennent de là, par définition » — le commentaire décrivait une intention
    que le code n'avait jamais réalisée. Conséquence pour un utilisateur extérieur : il installe
    une version et n'a AUCUN chemin vers la suivante, alors que ses plugins, eux, se mettent à
    jour depuis le catalogue.

    On lit les RELEASES, pas la branche : une branche dit « l'état du dépôt à cet instant », ce
    qui n'est pas une version et ne se compare à rien. Les pré-versions sont retenues — le produit
    est en bêta, les ignorer ne montrerait rien.

    Renvoie `{"version", "tag", "url", "publiee_le", "prerelease", "artefacts": [...]}` ou None
    si l'organisation ne répond pas ou n'a aucune release.
    """
    org, _ttl, actif = _reglages()
    if not actif:
        return None
    try:
        rels = _http_json("%s/repos/%s/%s/releases?per_page=10" % (API, org, DEPOT_CORE))
    except Exception as e:
        log.info("catalogue : releases du coeur illisibles (%s)", e)
        return None
    from .version import analyser
    meilleure = None
    for r in rels or []:
        if r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")
        v = analyser(tag)
        if v is None:
            continue
        if meilleure is None or v > meilleure[0]:
            meilleure = (v, r, tag)
    if not meilleure:
        return None
    _v, r, tag = meilleure
    return {
        "version":    tag.lstrip("vV"),
        "tag":        tag,
        "url":        r.get("html_url"),
        "publiee_le": (r.get("published_at") or "")[:10],
        "prerelease": bool(r.get("prerelease")),
        # ⚠ Une release SANS artefact n'est pas applicable : l'archive de source que sert GitHub
        # n'embarque ni l'installeur ni son empreinte, et `get.sh` vérifie un SHA256SUMS avant
        # d'exécuter quoi que ce soit en root. L'appelant doit le dire plutôt que de proposer un
        # bouton qui échouerait.
        "artefacts":  [a.get("name") for a in (r.get("assets") or [])],
    }


def maj_core_disponible():
    """(disponible, info) — compare la dernière version publiée à celle de CETTE instance."""
    from .version import VERSION, au_moins, comparable
    info = derniere_version_core()
    if not info:
        return False, None
    info["version_installee"] = VERSION
    if not comparable(info["version"], VERSION):
        return False, info
    dispo = not au_moins(VERSION, info["version"])
    info["applicable"] = bool(info["artefacts"])
    return dispo, info


def telecharger(depot, tag=None):
    """Archive zip du dépôt, en octets. Lève ValueError si le dépôt n'est pas au
    catalogue, ou si l'archive dépasse le plafond de l'import.

    ⚠ LE NOM DU DÉPÔT VIENT D'UNE REQUÊTE et sert de fragment d'URL. On ne le
    nettoie pas : on exige qu'il figure DÉJÀ dans le catalogue, donc qu'il vienne
    de la liste renvoyée par l'organisation configurée. Une liste blanche ne se
    contourne pas avec un caractère bien choisi."""
    e = entree(depot)
    if not e:
        raise ValueError("dépôt absent du catalogue : %s" % depot)
    org, _ttl, _actif = _reglages()
    tag = tag or e.get("tag") or ""
    if not tag:
        # Cohérent avec le listing : un dépôt sans release n'est pas installable, et on le DIT.
        raise ValueError("aucune release publiée sur %s" % depot)
    url = "%s/%s/%s/zip/refs/tags/%s" % (CODELOAD, org, depot, tag)
    req = urllib.request.Request(url, headers={"User-Agent": "bobistudio-catalogue"})
    with urllib.request.urlopen(req, timeout=30) as r:
        # Lecture BORNÉE : `read()` sur une réponse HTTP est une confiance qu'on
        # n'a pas à accorder à un serveur distant. Un octet de plus que le plafond
        # et on refuse, plutôt que de remplir la mémoire du contrôleur.
        brut = r.read(_TAILLE_MAX + 1)
    if len(brut) > _TAILLE_MAX:
        raise ValueError("archive trop volumineuse (> %d Mo)" % (_TAILLE_MAX // (1024 * 1024)))
    return brut
