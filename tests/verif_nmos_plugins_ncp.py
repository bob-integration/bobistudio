#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de `services/nmos/plugins_ncp.py` — les paramètres de plugin exposés en MS-05-02.
#
# CE QUE CE BANC PROTÈGE, ET POURQUOI C'EST CELUI-LÀ QUI COMPTE
# --------------------------------------------------------------
# Ce module publie un CONTRAT : des `classId` et des noms de propriétés qu'un contrôleur tiers
# mémorise. Une régression ici ne casse rien chez nous — elle casse chez le client, silencieusement,
# le jour où il met à jour. D'où des contrôles qui portent surtout sur ce qui est PUBLIÉ et sur ce
# qui ne doit jamais bouger.
#
# Le chemin d'écriture est éprouvé en détournant `macros.exec_post` : on vérifie ce qui SERAIT
# envoyé au conteneur, sans en avoir besoin. Le trajet réel a été vérifié à la main le 2026-08-31
# sur un hello_world vivant (Set(coefficient_omega=42) et Set(fond="nuit") retrouvés dans /state).
#
#   $ ./venv/bin/python tools/verif_nmos_plugins_ncp.py
import json
import os
import re
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(
        intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import ncp, plugins_ncp as P                    # noqa: E402

print("plugins_ncp — contrat publié\n")

# ── 1. Le contrat : des classId qui ne doivent JAMAIS bouger ─────────────────
controle("les deux classId sont ceux publiés",
         P.CLASSE_PLUGIN == [1, 1, P.CLE_AUTORITE, 1]
         and P.CLASSE_PARAMETRE == [1, 2, P.CLE_AUTORITE, 1],
         "un classId publié est mémorisé par les contrôleurs : le changer sans migration les "
         "fait piloter autre chose, sans erreur nulle part")
controle("NcBobiPlugin dérive NcBlock, NcBobiParametre dérive NcWorker",
         P.CLASSE_PLUGIN[:2] == [1, 1] and P.CLASSE_PARAMETRE[:2] == [1, 2])

P.enregistrer_classes()
reg = ncp.registre()
controle("les classes sont déclarées au registre MS-05-02",
         reg.descripteur_classe(P.CLASSE_PARAMETRE) is not None
         and reg.descripteur_classe(P.CLASSE_PLUGIN) is not None,
         "sans descripteur, GetControlClass répond « classe inconnue » et le contrôleur voit nos "
         "objets sans pouvoir lire leurs propriétés")
controle("★ l'héritage se résout MALGRÉ la clé d'autorité 0",
         reg.ancetres(P.CLASSE_PARAMETRE) == [(1,), (1, 2), (1, 2, P.CLE_AUTORITE, 1)],
         "obtenu %s — le commentaire de `ancetres` ne parle que des clés NÉGATIVES ; avec 0 le "
         "saut ne s'applique pas, et seule l'absence de (1,2,0) du registre sauve le résultat"
         % (reg.ancetres(P.CLASSE_PARAMETRE),))

_props = {p["name"]: p for p in reg.descripteur_classe(P.CLASSE_PARAMETRE,
                                                       inclure_herite=False)["properties"]}
controle("les 8 propriétés du paramètre sont publiées",
         set(_props) == {"key", "groupLabel", "value", "valueType",
                         "minimum", "maximum", "step", "defaultValue"},
         "obtenu %s" % sorted(_props))
controle("★ `value` est la SEULE propriété inscriptible",
         [n for n, p in _props.items() if not p["isReadOnly"]] == ["value"],
         "tout le pilotage passe par elle ; en rendre une autre inscriptible ouvrirait un "
         "chemin d'écriture non prévu")
controle("les propriétés sont au niveau 3 (nos classes dérivent d'une profondeur 2)",
         all(p["id"]["level"] == 3 for p in _props.values()))
controle("leurs index sont uniques et contigus",
         sorted(p["id"]["index"] for p in _props.values()) == list(range(1, 9)))

# ── 2. La clé d'autorité : UN SEUL littéral dans tout le produit ─────────────
# Même leçon que le PEN IANA pour SNMP : la clé est EMBARQUÉE dans chaque classId publié. La
# disséminer, c'est se garantir un parc à moitié migré le jour où le CID IEEE est attribué.
_hits = subprocess.run(
    ["grep", "-rn", "-E", r"CLE_AUTORITE\s*=", "--include=*.py", RACINE],
    capture_output=True, text=True).stdout.strip().splitlines()
_hits = [h for h in _hits if "/venv/" not in h and "/tools/" not in h]
controle("★ la clé d'autorité n'est définie qu'à UN endroit",
         len(_hits) == 1 and "plugins_ncp.py" in _hits[0],
         "trouvée %d fois : %s" % (len(_hits), _hits))
controle("aucun classId n'écrit la clé en dur ailleurs",
         not [h for h in subprocess.run(
             ["grep", "-rn", "-E", r"\[1,\s*[12],\s*0,\s*1\]", "--include=*.py", RACINE],
             capture_output=True, text=True).stdout.strip().splitlines()
             if "/venv/" not in h and "plugins_ncp.py" not in h and "/tools/" not in h])

# ── 3. Forme canonique et rôles ──────────────────────────────────────────────
controle("les booléens sont canonisés en true/false",
         P._canon(True) == "true" and P._canon(False) == "false",
         "un contrôleur qui lit « True » (Python) au lieu de « true » ne saura pas le coercer")
controle("None reste None (et ne devient pas la chaîne « None »)", P._canon(None) is None)

# ⚠ Le rôle est l'ADRESSE d'un objet (GetMemberDescriptors, chemins IS-14) : il doit dépendre de
# l'identité du paramètre, jamais de son rang. Un paramètre ajouté au manifeste décalerait sinon
# toutes les adresses déjà mémorisées par un contrôleur.
controle("le rôle dépend de l'identité, pas du rang",
         P._role("el", "Groupe A", "gain") == P._role("el", "Groupe A", "gain"))
controle("deux paramètres distincts ont des rôles distincts",
         P._role("el", "Groupe A", "gain") != P._role("el", "Groupe B", "gain"))
# ⚠ LE CONTRE-EXEMPLE DOIT RÉSISTER À NFKD, et le premier que j'ai écrit n'y résistait pas :
# « À » se décompose en « A » + diacritique, que la normalisation retire déjà — le filtre ASCII
# n'était donc pas sollicité, et la garde passait même désarmée. Il faut des caractères que NFKD
# ne décompose PAS : « ø », « œ », du cyrillique. C'est le cas réel d'un site non francophone.
_cas = P._role("el", "Grøupe œuf Ω", "gain %")
controle("le rôle ne contient que des caractères sûrs, même hors décomposition NFKD",
         re.fullmatch(r"[a-z0-9_]+", _cas) is not None,
         "un rôle est une ADRESSE dans les chemins d'URL IS-14 — obtenu %r" % _cas)
controle("et la translittération préserve la distinction des libellés accentués",
         P._role("el", "Réglages", "g") != P._role("el", "Reglage", "g")
         and P._role("el", "Réglages", "g") == P._role("el", "Reglages", "g"),
         "on translittère (é→e) plutôt que de remplacer par « _ » : un rôle illisible ne "
         "s'associe plus à rien pour l'humain")

# ── 4. Bornes publiées = bornes appliquées ───────────────────────────────────
APPELS = []


class _FauxMacros:
    """Capture ce qui SERAIT envoyé au conteneur, sans conteneur."""
    @staticmethod
    def exec_post(vmid, endpoint, corps):
        APPELS.append((vmid, endpoint, corps))
        return True


_app = ncp.Appareil({"name": "T", "key": "t", "revisionLevel": "0", "brandName": None,
                     "uuid": "u", "description": "d"},
                    {"name": "B", "organizationId": None, "website": None},
                    serial="s", device_name="banc")

SPEC_NUM = {"key": "gain", "label": "Gain", "group_label": "G", "element": "el",
            "kind": "number", "min": 0, "max": 100, "step": 1, "default": 0,
            "endpoint": "/params", "wrap": None}
SPEC_ENUM = {"key": "fond", "label": "Fond", "group_label": "G", "element": "el",
             "kind": "enum", "options": ["a", "b"], "default": "a",
             "endpoint": "/params", "wrap": "styles"}

_num = P.Parametre(_app, _app.oid_libre(), "gain", 1, 42, SPEC_NUM)
_enum = P.Parametre(_app, _app.oid_libre(), "fond", 1, 42, SPEC_ENUM)

_vrai = P._appliquer
P._appliquer = lambda vmid, spec, val: _FauxMacros.exec_post(
    vmid, spec.get("endpoint"),
    ({spec["wrap"]: {spec["key"]: val}} if spec.get("wrap") else {spec["key"]: val}))
try:
    r = _num._ecrire("value", 150)
    controle("★ une valeur au-dessus du maximum PUBLIÉ est refusée",
             r.get("status") != 200 and "maximum" in (r.get("errorMessage") or ""),
             "publier des bornes puis accepter au-delà, c'est se contredire — obtenu %s" % r)
    controle("et rien n'a été envoyé au conteneur", not APPELS)

    r = _num._ecrire("value", -1)
    controle("une valeur sous le minimum est refusée", r.get("status") != 200)
    r = _num._ecrire("value", "abc")
    controle("une valeur non numérique est refusée sur un paramètre number",
             r.get("status") != 200)
    controle("aucun de ces refus n'a touché le conteneur", not APPELS)

    r = _enum._ecrire("value", "z")
    controle("une valeur hors énumération est refusée",
             r.get("status") != 200 and "énumération" in (r.get("errorMessage") or ""))

    r = _num._ecrire("value", 42)
    controle("une valeur valide est acceptée", r.get("status") == 200)
    controle("et part au conteneur sur l'endpoint du manifeste",
             bool(APPELS) and APPELS[-1] == (42, "/params", {"gain": 42}),
             "obtenu %r" % (APPELS[-1:] or None,))
    controle("la valeur publiée est mise à jour en forme canonique",
             _num._vals["value"] == "42")

    APPELS.clear()
    r = _enum._ecrire("value", "b")
    controle("★ le `wrap` du manifeste est respecté dans le corps envoyé",
             bool(APPELS) and APPELS[-1][2] == {"styles": {"fond": "b"}},
             "un wrap ignoré POSTerait au mauvais niveau et le plugin ne verrait rien — "
             "obtenu %r" % ([a[2] for a in APPELS[-1:]] or None,))
finally:
    P._appliquer = _vrai

# ── 5. Un refus du conteneur ne doit JAMAIS passer pour un succès ────────────
P._appliquer = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
try:
    r = _num._ecrire("value", 10)
    controle("★ un refus du conteneur remonte en ERREUR",
             r.get("status") != 200 and "refus" in (r.get("errorMessage") or "").lower(),
             "rendre « ok » sur une consigne que le conteneur n'a pas reçue est le pilotage "
             "fantôme qu'on refuse — obtenu %s" % r)
    controle("et la valeur publiée n'est pas mise à jour", _num._vals["value"] == "42",
             "annoncer une valeur qu'on n'a pas réussi à poser mentirait au contrôleur")
finally:
    P._appliquer = _vrai

# ── 5bis. Actions : une méthode générique, et des refus qui NOMMENT le problème ──
_act = P.Action(_app, _app.oid_libre(), "action_fond", 1, 42,
                {"id": "fond", "label": "Changer le fond", "endpoint": "/fond",
                 "params": [{"key": "couleur", "label": "Couleur",
                             "options_endpoint": "/couleurs"}]})
_act_fixe = P.Action(_app, _app.oid_libre(), "action_pip_off", 1, 42,
                     {"id": "pip_off", "label": "Masquer", "endpoint": "/pip",
                      "body": {"on": False}})

controle("la classe Action déclare UNE méthode générique Invoke",
         [m["name"] for m in reg.descripteur_classe(P.CLASSE_ACTION,
                                                    inclure_herite=False)["methods"]] == ["Invoke"],
         "des méthodes typées par action exigeraient une classe par action, donc un classId "
         "encodant un index sans source stable — le piège déjà évité pour les paramètres")
controle("les champs attendus sont DÉCRITS, avec leur endpoint d'options vivantes",
         json.loads(_act._vals["argumentFields"])[0]["optionsEndpoint"] == "/couleurs",
         "figer les options ici les périmerait aussitôt (fichiers, presets, sources)")
controle("les champs FIXES d'une action sont publiés à part",
         json.loads(_act_fixe._vals["fixedBody"]) == {"on": False})

_INVOQUE = []
_vrai_exec = P._executer
P._executer = lambda vmid, aid, params: _INVOQUE.append((vmid, aid, params))
try:
    r = _act._m_invoke({"argumentsJson": '{"coleur":"nuit"}'})
    controle("★ un argument INCONNU est refusé, et les clés attendues sont nommées",
             r.get("status") != 200 and "couleur" in (r.get("errorMessage") or ""),
             "une faute de frappe avalée ferait croire à l'opérateur que sa consigne est partie, "
             "alors que l'action s'exécuterait avec ses valeurs par défaut — obtenu %s" % r)
    controle("et rien n'a été déclenché", not _INVOQUE)

    r = _act._m_invoke({"argumentsJson": "pas du json"})
    controle("un argumentsJson illisible est refusé", r.get("status") != 200)
    # ⚠ Vérifier le MOTIF, pas le statut. Avec le contrôle de type désarmé, « [1,2] » est encore
    # rejeté — mais comme « arguments inconnus », par le contrôle suivant. Sur le seul statut, la
    # disparition de cette garde-ci serait invisible. (Deuxième fois cette nuit pour ce motif.)
    r = _act._m_invoke({"argumentsJson": "[1,2]"})
    controle("un argumentsJson qui n'est pas un OBJET est refusé, et pour CE motif",
             r.get("status") != 200 and "OBJET" in (r.get("errorMessage") or ""),
             "obtenu %s" % r)
    controle("aucun de ces refus n'a déclenché l'action", not _INVOQUE)

    r = _act._m_invoke({"argumentsJson": '{"couleur":"nuit"}'})
    controle("une invocation valide passe", r.get("status") == 200)
    controle("et part sur l'identifiant d'action du manifeste",
             _INVOQUE and _INVOQUE[-1] == (42, "fond", {"couleur": "nuit"}),
             "obtenu %r" % (_INVOQUE[-1:] or None,))

    _INVOQUE.clear()
    r = _act_fixe._m_invoke({"argumentsJson": None})
    controle("une action sans argument s'invoque avec argumentsJson null",
             r.get("status") == 200 and bool(_INVOQUE) and _INVOQUE[-1][2] == {})
finally:
    P._executer = _vrai_exec

P._executer = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
try:
    r = _act_fixe._m_invoke({"argumentsJson": None})
    controle("★ l'échec d'une action remonte en ERREUR",
             r.get("status") != 200 and "échou" in (r.get("errorMessage") or ""),
             "un « ok » sur une action qui n'a pas eu lieu est du pilotage fantôme — obtenu %s" % r)
finally:
    P._executer = _vrai_exec

# ── 6. Le réglage ferme bien la surface ──────────────────────────────────────
from app.database import db_get_setting, db_set_setting                      # noqa: E402
_avant = db_get_setting("nmos_plugins_ncp", "0")
try:
    db_set_setting("nmos_plugins_ncp", "0")
    controle("fermé par défaut, la surface n'est pas publiée", not P.actif(),
             "publier ce modèle, c'est publier un contrat que d'autres mémoriseront")
    db_set_setting("nmos_plugins_ncp", "1")
    controle("le réglage ouvre bien la surface", P.actif())
    # ⚠ Les réglages booléens sont stockés en JSON : `db_get_setting` rend `True`, pas « 1 ».
    # Une comparaison sensible à la casse (`str(True)` vaut « True ») ne correspond à aucune des
    # formes attendues — le réglage activé depuis l'interface serait resté SANS EFFET. Panne
    # muette parfaite : la case est cochée, et rien ne se passe.
    db_set_setting("nmos_plugins_ncp", True)
    controle("★ un booléen JSON active la surface autant que la chaîne « 1 »", P.actif(),
             "db_get_setting rend un VRAI booléen pour les réglages de type bool")
    db_set_setting("nmos_plugins_ncp", False)
    controle("et un booléen False la referme", not P.actif())
finally:
    db_set_setting("nmos_plugins_ncp", _avant)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
