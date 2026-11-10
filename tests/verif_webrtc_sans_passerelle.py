#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Une destination WebRTC sans passerelle : l'échec doit être DIT, pas avalé.

★ LE CONSTAT (2026-11-08). `plugins/streamer/hooks.py` résout `ingest_url` depuis les réglages
`webrtc_*`. Sans passerelle déployée il se contente de RETIRER la clé
(`d.pop("ingest_url", None)`) et le déploiement continue : le conteneur part, ffmpeg encode, les
branches UDP/SRT marchent, et la branche WebRTC n'existe pas. Aucune alerte, aucun compteur, et
la carte du flux affiche un chemin WebRTC qui ne mène nulle part. C'est l'échec silencieux
fonctionnel typique : la capacité est annoncée, elle n'existe pas.

★ ET LE MESSAGE QUI EXISTAIT ENVOYAIT AU MAUVAIS ENDROIT. Le panneau de monitoring, lui, DIT que
la passerelle manque — mais son lien visait `Réglages → WebRTC`, onglet que `routes/pages.py`
RETIRE tant que la clé `webrtc_enabled` est fausse. Donc, sur une installation neuve — la seule
où ce message s'affiche — le lien ne menait nulle part (mesuré : 36 onglets rendus, aucun
« webrtc »). Il faut d'abord activer le service dans Services.

Ce qu'elle tient :
1. ★★ destination WebRTC active sans `ingest_url` → alerte, nommée, avec le chemin ;
2. ★★ destination résolue (passerelle présente) → AUCUNE alerte (sinon on apprend à l'ignorer) ;
3. ★ une destination WebRTC désactivée ne déclenche rien, un flux UDP/SRT seul non plus ;
4. ★★ le contrôle est posé dans `deploy.py` après le hook — le point par lequel passent TOUS les
   déploiements — et juge sur le RÉSULTAT du hook, pas sur les réglages ;
5. ★★ on n'empêche RIEN : le déploiement continue (les autres destinations servent) ;
6. ★★ aucun message de passerelle n'envoie plus vers l'onglet absent d'une installation neuve ;
7. ★ l'éditeur de flux le dit AVANT de déployer : `/api/streams/<vmid>` publie
   `gateway_ready`, et la carte du flux pose la note SOUS la destination — pas dans le bloc de
   prévisualisation, qui est replié par défaut sur certains postes et ne reflète que l'ENREGISTRÉ.

Hors ligne : base jetable, aucun conteneur, aucun réglage WebRTC.

MUTATIONS (WEBRTC_MUTATION=<nom>) — chacune doit donner rc=1 :
    pop_silencieux      le contrôle ne dit rien (retour anticipé)
    alerte_toujours     il alerte même quand la destination est résolue
    desactivee_comptee  une destination désactivée déclenche l'alerte
    controle_deplace    le contrôle n'est plus appelé dans `deploy.py`
    lien_onglet_absent  les messages repointent sur l'onglet qui n'existe pas
    api_muette          `/api/streams` ne publie plus `gateway_ready`
    editeur_muet        l'éditeur n'affiche jamais la note
    tri_etat_ecrase_js  « pas encore su » devient « pas de passerelle » (faux au montage)
    note_dans_preview_seule  la note n'est plus rafraîchie, seule la preview parle

    ./venv/bin/python tests/verif_webrtc_sans_passerelle.py
"""
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_webrtc_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

MUT = os.environ.get("WEBRTC_MUTATION", "")
echecs, total = [], [0]


def verifie(nom, cond, detail=""):
    total[0] += 1
    if cond:
        print(f"  \033[32m✓\033[0m {nom}")
    else:
        print(f"  \033[31m✗\033[0m {nom}" + (f"  → {detail}" if detail else ""))
        echecs.append(nom)


# ─── Le contrôle, chargé depuis la SOURCE de deploy.py pour pouvoir la muter ──
# On n'importe pas `app.deploy` (il tire la moitié du produit) : on extrait la fonction et on
# l'exécute avec un `db_add_alert` espionné. La MUTATION porte donc sur le code réel.
_SRC_DEPLOY = os.path.join(RACINE, "app", "deploy.py")
_src_deploy = open(_SRC_DEPLOY, encoding="utf-8").read()

_MUTATIONS = {
    "pop_silencieux": ("    chemins = []\n", "    chemins = []\n    return\n"),
    "alerte_toujours": ('        if (d.get("ingest_url") or "").strip():\n'
                        '            continue                       # résolue : la passerelle est là\n',
                        '        if False:\n            continue\n'),
    "desactivee_comptee": ('or not d.get("enabled"):', 'or False:'),
}

_bloc = re.search(r"\ndef _avertir_webrtc_sans_passerelle\(.*?\n\n\ndef ", _src_deploy, re.S)
assert _bloc, "fonction `_avertir_webrtc_sans_passerelle` introuvable dans app/deploy.py"
_code = _bloc.group(0)[: -len("\n\ndef ")]
if MUT in _MUTATIONS:
    avant, apres = _MUTATIONS[MUT]
    assert avant in _code, f"mutation {MUT} : motif introuvable (la source a bougé)"
    _code = _code.replace(avant, apres, 1)

_alertes = []


class _Log:
    @staticmethod
    def warning(*a, **k):
        pass


_ns = {"log": _Log,
       "db_add_alert": lambda *a, **k: _alertes.append((a, k))}
exec(compile("import re\n" + _code, _SRC_DEPLOY, "exec"), _ns)
avertir = _ns["_avertir_webrtc_sans_passerelle"]


def alerte_pour(params):
    _alertes.clear()
    avertir(42, params)
    return list(_alertes)


print("\n── 1. destination WebRTC non résolue : c'est DIT ───────────────────")
a = alerte_pour({"destinations": [{"type": "webrtc", "enabled": True, "path": "regie1"}]})
verifie("1a. ★★ une alerte est journalisée", len(a) == 1, a)
verifie("1b. ★★ elle porte la clé i18n dédiée",
        bool(a) and a[0][0][0] == "alert.webrtc.destination_sans_passerelle",
        bool(a) and a[0][0][:1])
verifie("1c. ★★ elle NOMME le chemin (sinon on cherche lequel)",
        bool(a) and a[0][1]["params"]["paths"] == "regie1", bool(a) and a[0][1].get("params"))
verifie("1d. ★ elle est attachée au conteneur et au bon domaine",
        bool(a) and a[0][1]["vmid"] == 42 and a[0][1]["kind"] == "deploy",
        bool(a) and {k: a[0][1].get(k) for k in ("vmid", "kind")})
verifie("1e. ★ `kind` appartient au vocabulaire fermé",
        bool(a) and a[0][1]["kind"] in database.ALERT_KINDS)
verifie("1f. ★ niveau avertissement, pas erreur (le reste du flux fonctionne)",
        bool(a) and a[0][0][1] == "warning", bool(a) and a[0][0][1:2])

a = alerte_pour({"destinations": [{"type": "webrtc", "enabled": True, "path": "a"},
                                  {"type": "webrtc", "enabled": True, "path": "b"},
                                  {"type": "udp", "host": "198.51.100.9", "port": 9000}]})
verifie("2a. ★ deux destinations muettes → UNE alerte qui les nomme toutes",
        len(a) == 1 and a[0][1]["params"]["paths"] == "a, b" and a[0][1]["params"]["n"] == 2,
        a and a[0][1].get("params"))

print("\n── 3. ce qui ne doit RIEN déclencher ───────────────────────────────")
verifie("3a. ★★ destination résolue (passerelle là) → aucune alerte",
        alerte_pour({"destinations": [{"type": "webrtc", "enabled": True, "path": "p",
                                       "ingest_url": "rtsp://192.0.2.50:8554/p"}]}) == [])
verifie("3b. ★ destination WebRTC DÉSACTIVÉE → aucune alerte",
        alerte_pour({"destinations": [{"type": "webrtc", "enabled": False, "path": "p"}]}) == [])
verifie("3c. ★ flux UDP/SRT seul → aucune alerte",
        alerte_pour({"destinations": [{"type": "udp", "host": "198.51.100.9", "port": 9000},
                                      {"type": "srt", "host": "198.51.100.9", "port": 9001}]}) == [])
verifie("3d. ★ params sans destinations (tout autre type de conteneur) → aucune alerte",
        alerte_pour({}) == [] and alerte_pour({"destinations": None}) == [])
verifie("3e. ★ une destination mal formée ne fait pas tomber un déploiement",
        alerte_pour({"destinations": ["n'importe quoi", None, 3]}) == [])

print("\n── 4. posé sur le chemin que TOUT déploiement traverse ─────────────")
_d = _src_deploy
if MUT == "controle_deplace":
    _d = _d.replace("    _avertir_webrtc_sans_passerelle(vmid, params)\n", "")
verifie("4a. ★★ appelé dans `deployer_script`, APRÈS le hook du plugin",
        re.search(r"hook before_deploy %s: %s.*?_avertir_webrtc_sans_passerelle\(vmid, params\)",
                  _d, re.S) is not None)
verifie("4b. ★★ le jugement porte sur `ingest_url` (résultat du hook), pas sur les réglages",
        "ingest_url" in _code and "webrtc_gateway_ip" not in _code and "webrtc_enabled" not in _code)
verifie("4c. ★★ rien n'est EMPÊCHÉ (aucune levée, aucun retour bloquant)",
        "raise" not in _code and "return False" not in _code)
verifie("4d. ★ un défaut de journalisation ne casse pas le déploiement",
        "except Exception" in _code)

print("\n── 5. plus aucun message vers l'onglet absent ──────────────────────")
fr = json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(RACINE, "i18n", "en.json"), encoding="utf-8"))
if MUT == "lien_onglet_absent":
    fr = dict(fr, **{"js.monitor.no_gateway": 'Allez dans <a href="/settings#webrtc">Réglages</a>.'})
# ⚠ Un service dont `*_enabled` est faux est retiré des onglets : `#webrtc` n'existe donc PAS
# sur une installation neuve, la seule où ces messages s'affichent.
_suspects = {k: v for cat in (fr, en) for k, v in cat.items()
             if "/settings#webrtc" in v}
verifie("5a. ★★ aucun libellé ne LIE vers `#webrtc`", not _suspects, sorted(_suspects))
verifie("5b. ★★ le message du monitoring envoie bien activer le service",
        "#services" in fr["js.monitor.no_gateway"] and "#services" in en["js.monitor.no_gateway"],
        fr["js.monitor.no_gateway"][:60])
verifie("5c. ★ la raison est toujours vraie dans le code",
        'endswith("_enabled")' in open(os.path.join(RACINE, "app", "routes", "pages.py"),
                                       encoding="utf-8").read())
verifie("5d. ★★ la clé de l'alerte neuve est traduite des deux côtés",
        "alert.webrtc.destination_sans_passerelle" in fr
        and "alert.webrtc.destination_sans_passerelle" in en)
for _cle in ("alert.webrtc.destination_sans_passerelle",):
    _p = set(re.findall(r"\{(\w+)\}", fr[_cle])) | set(re.findall(r"\{(\w+)\}", en[_cle]))
    verifie("5e. ★★ tout {param} du libellé est fourni par l'appelant",
            _p <= {"n", "paths"}, sorted(_p))

print("\n── 6. l'éditeur peut le dire AVANT de déployer ─────────────────────")
_api = open(os.path.join(RACINE, "app", "routes", "streams_api.py"), encoding="utf-8").read()
if MUT == "api_muette":
    _api = _api.replace('"gateway_ready": bool(gw_enabled and gw_ip)', '"vmid": vmid')
verifie("6a. ★ `/api/streams/<vmid>` publie `gateway_ready`", '"gateway_ready"' in _api)
verifie("6b. ★ mesuré sur le service ACTIVÉ et une IP connue, pas sur l'un des deux",
        "bool(gw_enabled and gw_ip)" in _api)

print("\n── 7. et l'éditeur le DIT, là où l'on coche ────────────────────────")
# ⚠ Le message qui existait vivait dans le bloc de PRÉVISUALISATION : replié par défaut sur
# certains postes (localStorage `mxl.streams.preview.<vmid>`), et calculé sur l'état ENREGISTRÉ —
# donc muet au moment où l'on coche « diffuser ». La note doit être sous la destination.
_js = os.path.join(RACINE, "plugins", "streamer", "control.js")
if not os.path.exists(_js):
    verifie("7-. (sous-module streamer absent de cette copie : section sautée)", True)
else:
    _c = open(_js, encoding="utf-8").read()
    if MUT == "editeur_muet":
        _c = _c.replace("champs.insertAdjacentHTML('beforeend', gwNoteHtml())", "void 0")
    if MUT == "tri_etat_ecrase_js":
        _c = _c.replace("_gwReady = (c.gateway_ready === undefined) ? null : !!c.gateway_ready;",
                        "_gwReady = !c.gateway_ready;")
    if MUT == "note_dans_preview_seule":
        _c = _c.replace("            refreshGwNotes();\n", "")
    verifie("7a. ★★ l'éditeur lit `gateway_ready` du serveur", "c.gateway_ready" in _c)
    verifie("7b. ★★ TRI-ÉTAT conservé : `undefined` n'est pas « pas de passerelle »",
            "(c.gateway_ready === undefined) ? null" in _c)
    verifie("7c. ★★ la note n'est affichée que sur une ABSENCE mesurée",
            "_gwReady === false" in _c)
    verifie("7d. ★★ elle est posée sous la destination, pas dans la preview",
            "insertAdjacentHTML('beforeend', gwNoteHtml())" in _c
            and "data-gw-note" in _c)
    verifie("7e. ★★ elle apparaît dès qu'on choisit « WebRTC » (type changé)",
            re.search(r"pathInput\.value = _autoWebrtcPath\(\);\s*\}\s*\n\s*refreshGwNotes\(\);",
                      _c) is not None)
    verifie("7f. ★ et elle disparaît seule si la passerelle est déployée ailleurs (poll)",
            len(re.findall(r"refreshGwNotes\(\);", _c)) >= 4,
            len(re.findall(r"refreshGwNotes\(\);", _c)))
    verifie("7g. ★ le rafraîchissement est idempotent (pas de note en double)",
            "if(!note) champs.insertAdjacentHTML" in _c)
    _pfr = json.load(open(os.path.join(RACINE, "plugins", "streamer", "i18n", "fr.json"),
                          encoding="utf-8"))
    _pen = json.load(open(os.path.join(RACINE, "plugins", "streamer", "i18n", "en.json"),
                          encoding="utf-8"))
    verifie("7h. ★★ `plugin.streamer.dest_no_gw` est dans les deux langues DU PLUGIN",
            "plugin.streamer.dest_no_gw" in _pfr and "plugin.streamer.dest_no_gw" in _pen)
    verifie("7i. ★★ et pas dans le catalogue du cœur (qui primerait)",
            "plugin.streamer.dest_no_gw" not in fr)
    verifie("7j. ★★ la note dit que les sorties UDP/SRT, elles, marchent",
            "UDP" in _pfr.get("plugin.streamer.dest_no_gw", "")
            and "UDP" in _pen.get("plugin.streamer.dest_no_gw", ""))
    for _k in ("plugin.streamer.dest_no_gw", "plugin.streamer.preview_no_gw"):
        verifie(f"7k. ★★ `{_k}` passe d'abord par Services (l'onglet WebRTC n'existe pas encore)",
                "Services" in _pfr.get(_k, "") and "Services" in _pen.get(_k, ""),
                _pfr.get(_k, "")[:60])

print()
if echecs:
    print(f"\033[31m{len(echecs)} échec(s)\033[0m sur {total[0]} vérification(s) :")
    for e in echecs:
        print(f"   · {e}")
    sys.exit(1)
print(f"\033[32m{total[0]} vérification(s), 0 échec(s)\033[0m"
      + (f"  (mutation {MUT} NON DÉTECTÉE)" if MUT else ""))
sys.exit(0)
