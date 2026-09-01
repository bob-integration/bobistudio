#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Le bus MXL et le 2110 sont DEUX Devices IS-04 distincts.
#
# Pourquoi ce banc existe : ils ont longtemps partagé un seul Device, nommé « Bobi.Studio
# 2110 I/O », qui portait 228 ressources MXL pour 18 ressources 2110. Un contrôleur n'avait
# alors aucun moyen de dire ce qu'il pouvait connecter — le 2026-09-01 un PATCH IS-05 a été
# tenté sur un receiver MXL, qui l'a refusé en 405 après coup. Le transport était pourtant
# écrit sur chaque ressource ; c'est le RANGEMENT qui manquait.
#
# Le banc tourne HORS LIGNE — aucun réseau, aucune écriture. Il LIT en revanche la base
# réelle (`mxl.build` appelle `db_get_containers`) : c'est voulu, le rangement se vérifie sur
# les conteneurs qui existent vraiment. Sur une base sans conteneur MXL, le dernier bloc bascule
# sur « le Device est correctement absent » plutôt que de passer en silence.
import json
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


print("Surfaces NMOS séparées : bus MXL ≠ 2110\n")

from services.nmos import mxl                                        # noqa: E402
import services.nmos as nmos                                         # noqa: E402

# ── 1. Le module MXL sait faire son propre Device ────────────────────────────
controle("★★★ la surface MXL a son propre constructeur de Device",
         hasattr(mxl, "device_resource"),
         "sans lui, elle ne peut que se greffer sur le Device du 2110 — c'est l'état qui a "
         "produit le 405 incompréhensible")

src_mxl = open(os.path.join(RACINE, "services", "nmos", "mxl.py"), encoding="utf-8").read()
src_ini = open(os.path.join(RACINE, "services", "nmos", "__init__.py"), encoding="utf-8").read()

controle("★★★ le module MXL ne reçoit plus le Device du cluster 2110",
         "cluster_did" not in src_mxl and "mxl_did" in src_mxl,
         "le paramètre s'appelait `cluster_did` : tant qu'il désigne le Device du 2110, toutes "
         "les ressources MXL y retournent, quel que soit le reste")
controle("★★★ le point d'appel passe un identifiant DISTINCT",
         '_stable_uuid("device:mxl")' in src_ini,
         "passer `cluster_did` ici annulerait tout le reste sans qu'aucun contrôle de forme ne "
         "le voie")
controle("★★ l'identifiant du Device MXL est STABLE",
         "device:mxl" in src_ini and "uuid5" in src_ini or "_stable_uuid" in src_ini,
         "un identifiant tiré au hasard à chaque boot ferait disparaître puis réapparaître un "
         "Device entier chez tous les contrôleurs abonnés")
controle("★★ un Device MXL VIDE n'est pas annoncé",
         'if not d["senders"] and not d["receivers"]:' in src_mxl
         and "del new_devices[mxl_did]" in src_mxl,
         "une installation sans conteneur MXL annoncerait un Device creux, que le contrôleur "
         "afficherait sans jamais rien pouvoir en faire")
controle("★★ le Device MXL annonce quand même IS-05",
         '"controls": _controls(),' in src_mxl,
         "décision du 2026-09-01 : le MXL RESTE dans l'API de connexion (lecture de l'état "
         "d'abonnement, et l'écriture s'ouvrira par `nmos_mxl_ecriture`). Sans `controls`, un "
         "contrôleur ne trouverait plus la Connection API pour ces ressources")
controle("★ son libellé est réglable",
         '"nmos_mxl_label"' in src_ini and "nmos_mxl_label" in src_mxl,
         "un site qui renomme son I/O 2110 doit pouvoir renommer son bus aussi")

# ── 2. LE RANGEMENT, sur un modèle réellement construit ──────────────────────
# On ne relit pas la source : on regarde où les ressources ATTERRISSENT. C'est la seule
# vérification qui aurait attrapé le défaut d'origine.
print("\n── Le modèle construit ─────────────────────────────────────────────────")
devices, sources, flows, senders, receivers = {}, {}, {}, {}, {}
did_2110 = nmos._stable_uuid("device:cluster")
did_mxl = nmos._stable_uuid("device:mxl")
controle("★★★ les deux Devices ont des identifiants différents", did_2110 != did_mxl,
         "même graine = un seul Device, et tout le reste est cosmétique")

try:
    devices[did_2110] = nmos._build_cluster_device_resource(did_2110, 1)
    mxl.build(devices, sources, flows, senders, receivers, {}, {}, did_mxl, "1:0")
    monte = True
except Exception as e:
    monte = False
    print("        (construction impossible : %s)" % e)
controle("★★ la surface MXL se construit hors ligne", monte,
         "sans ça, le rangement n'est vérifiable que sur un système vivant")

if monte and did_mxl in devices:
    mal = []
    for rid in devices[did_mxl]["receivers"]:
        if (receivers.get(rid) or {}).get("transport") != "urn:x-nmos:transport:mxl":
            mal.append(rid)
    for sid in devices[did_mxl]["senders"]:
        if (senders.get(sid) or {}).get("transport") != "urn:x-nmos:transport:mxl":
            mal.append(sid)
    controle("★★★ tout ce qui est sur le Device MXL a le transport MXL", not mal,
             "une ressource RTP rangée là serait invisible pour qui cherche du 2110. "
             "Obtenu %d intruse(s)" % len(mal))
    fuite = [r for r in devices[did_2110]["receivers"]
             if (receivers.get(r) or {}).get("transport") == "urn:x-nmos:transport:mxl"]
    controle("★★★ aucune ressource MXL n'est restée sur le Device 2110", not fuite,
             "c'est EXACTEMENT le défaut d'origine : 228 receivers MXL annoncés sous le nom "
             "« 2110 I/O ». Obtenu %d" % len(fuite))
    controle("★★★ les deux Devices ne portent pas le MÊME nom",
             (devices[did_mxl].get("label") or "") != (devices[did_2110].get("label") or "")
             and "MXL" in (devices[did_mxl].get("label") or ""),
             "deux Devices identiquement nommés « Bobi.Studio 2110 I/O » seraient PIRE qu'un "
             "seul : le contrôleur en afficherait deux, indiscernables. Obtenu %r et %r"
             % (devices[did_2110].get("label"), devices[did_mxl].get("label")))
    controle("★★ les références du Device pointent des ressources qui existent",
             all(r in receivers for r in devices[did_mxl]["receivers"])
             and all(s in senders for s in devices[did_mxl]["senders"]),
             "une référence pendante fait échouer la validation IS-04 d'un contrôleur")
else:
    controle("★★ un Device MXL est produit quand il y a des ressources",
             monte and not devices.get(did_mxl),
             "aucun conteneur MXL sur cette installation : le Device est correctement absent")

# ── 3. L'ÉCRAN, dans les deux langues ────────────────────────────────────────
# Le gabarit seul ne prouve rien : un champ peut être écrit et ne pas atteindre l'arbre rendu,
# et une clé i18n manquante s'affiche telle quelle sans qu'aucune erreur ne soit levée.
# ⚠ La langue vient de `users.lang`, PAS d'un réglage global — un banc qui écrirait un réglage
# pour la forcer laisserait sa valeur derrière lui (payé le 2026-09-01 : une ligne `langue`
# parasite créée en base, que rien ne lisait).
print("\n── L'écran rendu ───────────────────────────────────────────────────────")
import re                                                            # noqa: E402
try:
    import main                                                      # noqa: E402
    from app.database import get_db                                  # noqa: E402
    from app import i18n                                             # noqa: E402
    main.app.config["TESTING"] = True
    with get_db() as _db:
        _u = _db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    pages = {}
    for _lg in ("fr", "en"):
        _cli = main.app.test_client()
        with _cli.session_transaction() as _s:
            _s["user_id"] = _u["id"]
            _s["username"] = _u["username"]
        _orig = i18n.current_lang
        i18n.current_lang = (lambda l: (lambda: l))(_lg)
        try:
            pages[_lg] = _cli.get("/settings").data.decode("utf-8", "replace")
        finally:
            i18n.current_lang = _orig
except Exception as e:
    pages = {}
    print("        (page non rendue : %s)" % e)

controle("★★ la page Réglages se rend dans les deux langues",
         len(pages) == 2 and all(len(v) > 100000 for v in pages.values())
         and pages.get("fr") != pages.get("en"),
         "des pages IDENTIQUES voudraient dire que la langue n'a pas été prise en compte — le "
         "contrôle des libellés ne prouverait alors rien")
if len(pages) == 2:
    controle("★★ le champ de libellé MXL atteint l'arbre rendu",
             all('id="s_nmos_mxl_label"' in v for v in pages.values()),
             "sans lui, le Device MXL s'appelle pour toujours comme son défaut")
    # ⚠ NE PAS se contenter de chercher « nmos_mxl_label » dans la page : la chaîne y est déjà,
    # dans l'attribut `id` du champ. Il faut lire le CONTENU de la liste — sinon vider
    # `_AVANCES_STR` laisse le contrôle vert (vérifié par mutation, il était muet).
    _m = re.search(r"const\s+_AVANCES_STR\s*=\s*\[(.*?)\]", pages["fr"], re.S)
    _liste = re.findall(r"'([^']+)'", _m.group(1)) if _m else []
    controle("★★★ le champ appartient à la famille des chaînes ENVOYÉES",
             "nmos_mxl_label" in _liste,
             "les réglages avancés n'avaient que des booléens et des nombres. Un champ texte "
             "hors famille s'affiche, s'édite, et n'est JAMAIS enregistré — aucune erreur. "
             "Obtenu %r" % (_liste,))
    controle("★★★ la liste est aussi RELUE au chargement",
             pages["fr"].count("_AVANCES_STR.forEach") >= 2,
             "envoyée mais jamais relue, la valeur enregistrée n'apparaîtrait plus dans le champ "
             "après un rechargement — on croirait l'avoir perdue")
    _man = json.load(open(os.path.join(RACINE, "services", "nmos", "manifest.json"),
                          encoding="utf-8"))
    controle("★★★ la clé est déclarée au manifeste du service",
             "nmos_mxl_label" in (_man.get("settings_keys") or {}),
             "`/api/settings` n'accepte que les clés déclarées : sans elle, l'écriture repart "
             "en `ignored` et le réglage ne change rien")
    controle("★★ le libellé est traduit dans les deux langues",
             "Label du Device « bus MXL »" in pages["fr"]
             and "“MXL bus” Device label" in pages["en"],
             "une clé i18n manquante s'affiche telle quelle, sans erreur nulle part")
    _brutes = {lg: sorted(set(re.findall(r">\s*(service\.nmos\.[a-z0-9_.]+)\s*<", v)))
               for lg, v in pages.items()}
    controle("★ aucune clé i18n brute à l'écran",
             not _brutes["fr"] and not _brutes["en"],
             "obtenu %r" % _brutes)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
