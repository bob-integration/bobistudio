#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Les services utiles sont PROPOSÉS à la fin de l'installation — pas allumés d'office.

★ LE CONSTAT (2026-11-08). Les dix services qui portent une clé `*_enabled` sont TOUS éteints par
défaut : emberplus, nmos (×5), atem, control_surface, panel_dyvi, skaarhoj, rdma, sap, snmp,
webrtc. Et `routes/pages.py` RETIRE des onglets de Réglages tout service dont cette clé est
fausse — donc un service éteint est invisible, y compris son onglet. Rien, dans tout le parcours
d'installation, ne dit qu'il existe. La passerelle WebRTC est le cas coûteux : sans elle le
panneau de monitoring reste noir sur toutes les pages, et une destination WebRTC de flux part
dans le vide.

★ ON PROPOSE, ON N'ALLUME PAS D'OFFICE, et la garde tient cette frontière. Plusieurs de ces
services PARLENT sur le réseau dès l'allumage : annonces mDNS (NMOS), annonces SDP (SAP), agent
SNMP en écoute. Les activer sans que personne ne l'ait demandé change le comportement réseau d'un
site — c'est une décision d'exploitation, pas un défaut de confort. D'où une CASE À COCHER à la
fin de l'installation, au seul moment où on est sûr que quelqu'un regarde.

★ ET LA LISTE VIENT DES MANIFESTES. Champ `install_offer` : "on" (proposé, pré-coché), "off"
(proposé, à cocher), absent (non proposé). Une liste tenue dans l'installeur aurait péri au
premier service ajouté — ce dépôt en a déjà vu périmer plusieurs, et c'est écrit dans CLAUDE.md.

Ce qu'elle tient :
1. ★★ l'offre se lit dans les MANIFESTES, et l'installeur n'énumère aucun service ;
2. ★★ un service qui déclare l'offre SANS interrupteur est ignoré et JOURNALISÉ (une case qui ne
   fait rien est pire que pas de case) ;
3. ★★ les intégrations qui exigent du matériel ou des identifiants ne sont PAS proposées ;
4. ★★ l'installeur n'allume que ce qui est COCHÉ, et juge sur la valeur relue ;
5. ★ la proposition passe AVANT l'annonce des gestes restants (sinon elle annonce un état périmé
   d'une seconde) ;
6. ★★ chaque service proposé explique pourquoi, dans les DEUX langues, et depuis SON catalogue —
   le cœur prime sur le service, donc une clé de service laissée au cœur masque la sienne.

Hors ligne : aucun réglage touché, aucun service importé pour ses effets.

MUTATIONS (SERVICES_MUTATION=<nom>) — chacune doit donner rc=1 :
    offre_en_dur            l'installeur énumère les services lui-même
    offre_sans_interrupteur un service sans clé `*_enabled` est quand même proposé
    allume_doffice          l'installeur allume tout sans tenir compte des cases
    propose_apres_gestes    la proposition arrive après l'annonce des gestes
    materiel_propose        une intégration matérielle est proposée
    why_dans_le_coeur       le « pourquoi » est écrit dans le catalogue du cœur

    ./venv/bin/python tests/verif_services_proposes.py
"""
import glob
import json
import logging
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_services_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_plugins                                            # noqa: E402

MUT = os.environ.get("SERVICES_MUTATION", "")
echecs, total = [], [0]


def verifie(nom, cond, detail=""):
    total[0] += 1
    if cond:
        print(f"  \033[32m✓\033[0m {nom}")
    else:
        print(f"  \033[31m✗\033[0m {nom}" + (f"  → {detail}" if detail else ""))
        echecs.append(nom)


# Les manifestes sur DISQUE font foi : c'est eux que l'installeur lira sur la machine installée.
_MANIFESTES = {}
for _p in sorted(glob.glob(os.path.join(RACINE, "services", "*", "manifest.json"))):
    try:
        _m = json.load(open(_p, encoding="utf-8"))
    except Exception:
        continue
    if _m.get("id"):
        _MANIFESTES[_m["id"]] = _m

print("\n── 1. l'offre se lit dans les manifestes ───────────────────────────")
offres = core_plugins.install_offers()
ids = [o["id"] for o in offres]
verifie("1a. ★ au moins un service est proposé (sinon le mécanisme est mort)", bool(offres), ids)
_attendus = sorted(sid for sid, m in _MANIFESTES.items()
                   if (m.get("install_offer") or "").lower() in ("on", "off")
                   and any(k.endswith("_enabled") for k in (m.get("settings_keys") or {})))
verifie("1b. ★★ la liste EST celle des manifestes, ni plus ni moins",
        sorted(ids) == _attendus, f"{sorted(ids)} vs {_attendus}")
for o in offres:
    m = _MANIFESTES[o["id"]]
    verifie(f"1c. ★ {o['id']} : la clé proposée est bien son interrupteur",
            o["key"] in (m.get("settings_keys") or {}) and o["key"].endswith("_enabled"), o["key"])
    verifie(f"1d. ★ {o['id']} : le pré-cochage suit le manifeste",
            o["on"] == ((m.get("install_offer") or "").lower() == "on"),
            f"on={o['on']} offer={m.get('install_offer')}")
    verifie(f"1e. ★★ {o['id']} : son interrupteur est bien ÉTEINT par défaut",
            (m["settings_keys"][o["key"]].get("default") in (False, 0, None)),
            m["settings_keys"][o["key"]].get("default"))

print("\n── 2. une case qui ne ferait rien n'est pas présentée ──────────────")
# On fabrique un service qui déclare l'offre SANS interrupteur : il doit être ÉCARTÉ et DIT.
_faux = {"id": "zz_sans_interrupteur", "label": "Faux", "version": "0.0.1", "nav_tab": "zz",
         "tab_template": "zz/x.html", "settings_keys": {"zz_autre": {"type": "str"}},
         "install_offer": "on"}
if MUT == "offre_sans_interrupteur":
    _faux["settings_keys"]["zz_enabled"] = {"type": "bool", "default": False}
_vrai_scan = core_plugins.scan
core_plugins.scan = lambda: {**_vrai_scan(), "zz_sans_interrupteur": {"manifest": _faux}}
_journal = []


class _Capture(logging.Handler):
    def emit(self, record):
        _journal.append(record.getMessage())


_h = _Capture()
core_plugins.log.addHandler(_h)
try:
    _res = core_plugins.install_offers()
finally:
    core_plugins.log.removeHandler(_h)
    core_plugins.scan = _vrai_scan
verifie("2a. ★★ le service sans interrupteur n'est PAS proposé",
        "zz_sans_interrupteur" not in [o["id"] for o in _res], [o["id"] for o in _res])
verifie("2b. ★★ et le refus est JOURNALISÉ (pas un silence)",
        any("zz_sans_interrupteur" in m for m in _journal), _journal)

print("\n── 3. ce qui exige du matériel ou des identifiants n'est pas proposé ─")
# Décision produit, épinglée ICI pour qu'un ajout futur soit DÉLIBÉRÉ : proposer un pont ATEM à
# qui n'a pas de mélangeur ATEM, ou un agent SNMP sans identifiants, apprend à ignorer la liste.
_JAMAIS = ("emberplus", "atem", "skaarhoj", "panel_dyvi", "control_surface", "rdma", "snmp")
_ids_disque = set(_attendus)
if MUT == "materiel_propose":
    _ids_disque.add("atem")
for sid in _JAMAIS:
    if sid in _MANIFESTES:
        verifie(f"3a. ★★ {sid} n'est pas proposé à l'installation", sid not in _ids_disque)

print("\n── 4. l'installeur ne liste rien et n'allume que le coché ──────────")
_inst = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
if MUT == "offre_en_dur":
    _inst = _inst.replace("from app import core_plugins as cp\\n",
                          "SERVICES = ['webrtc_gateway', 'nmos']\\n")
if MUT == "allume_doffice":
    _inst = _inst.replace('cles = [o["key"] for o in offres if o["id"] in (choix or [])]',
                          'cles = [o["key"] for o in offres]')
if MUT == "propose_apres_gestes":
    _inst = _inst.replace("    proposer_services()\n    dire_gestes_restants()",
                          "    dire_gestes_restants()\n    proposer_services()")
verifie("4a. ★★ la liste vient de `core_plugins.install_offers`",
        "install_offers" in _inst and "core_plugins" in _inst)
# Portée : le CORPS de `proposer_services`. Ailleurs, `install.py` nomme légitimement un service
# — il refuse de poser un contrôleur dont `services/nmos/__init__.py` manque, et il a raison
# (`main.py` l'importe au niveau module). Interdire le mot partout serait une garde fausse.
_corps = re.search(r"def proposer_services\(\):.*?\ndef ", _inst, re.S)
verifie("4b. ★★ aucun identifiant de service n'est écrit DANS la proposition",
        bool(_corps) and not re.search(r"['\"](webrtc_gateway|nmos|sap|emberplus|snmp)['\"]",
                                       _corps.group(0)),
        bool(_corps) and re.findall(r"['\"](webrtc_gateway|nmos|sap|emberplus|snmp)['\"]",
                                    _corps.group(0))[:3])
verifie("4c. ★★ seules les cases COCHÉES sont allumées",
        'if o["id"] in (choix or [])' in _inst)
verifie("4d. ★★ le résultat est RELU, pas déduit du code de sortie",
        "bool(st.get(c))" in _inst and "etat.get(c)" in _inst)
verifie("4e. ★ rien de coché → on le dit, sans rien écrire",
        't("services_none")' in _inst)
verifie("4f. ★ un échec d'écriture est NOMMÉ (clé par clé)",
        't("services_partiel"' in _inst)
verifie("4g. ★ un service déjà allumé n'est pas reproposé",
        "if st.get(o['key']):" in _inst)
verifie("4h. ★★ proposé AVANT l'annonce des gestes (sinon elle est périmée)",
        all(re.search(r"proposer_services\(\)\s*\n(?:\s*#[^\n]*\n)*\s*dire_gestes_restants\(\)", b)
            for b in [re.search(r"def path_controller.*?return None", _inst, re.S).group(0),
                      re.search(r"def path_all_in_one.*?return None", _inst, re.S).group(0)]))
verifie("4i. ★ les deux chemins qui installent un contrôleur le proposent",
        len(re.findall(r"^    proposer_services\(\)", _inst, re.M)) == 2,
        len(re.findall(r"^    proposer_services\(\)", _inst, re.M)))

print("\n── 5. chaque service proposé explique pourquoi, chez LUI ───────────")
_coeur = {lang: json.load(open(os.path.join(RACINE, "i18n", f"{lang}.json"), encoding="utf-8"))
          for lang in ("fr", "en")}
if MUT == "why_dans_le_coeur":
    _coeur["fr"]["service.nmos.install_why"] = "…"
for o in offres:
    for lang in ("fr", "en"):
        p = os.path.join(RACINE, "services", o["id"], "i18n", f"{lang}.json")
        d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
        verifie(f"5a. ★★ {o['id']} ({lang}) : le pourquoi est dans SON catalogue",
                o["why"] in d, p)
        verifie(f"5b. ★★ {o['id']} ({lang}) : et PAS dans celui du cœur (qui primerait)",
                o["why"] not in _coeur[lang])
        if o["why"] in d:
            verifie(f"5c. ★ {o['id']} ({lang}) : le texte dit un usage, pas juste un nom",
                    len(d[o["why"]]) > 40, d[o["why"]][:40])

print()
if echecs:
    print(f"\033[31m{len(echecs)} échec(s)\033[0m sur {total[0]} vérification(s) :")
    for e in echecs:
        print(f"   · {e}")
    sys.exit(1)
print(f"\033[32m{total[0]} vérification(s), 0 échec(s)\033[0m"
      + (f"  (mutation {MUT} NON DÉTECTÉE)" if MUT else ""))
sys.exit(0)
