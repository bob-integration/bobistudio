#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc d'IS-07 — le tally publié en NMOS (`services/nmos/is07.py`).
#
# Ce module publie une FORME exigée par une spécification : la moindre clé mal nommée dans un
# message STATE rend l'événement inexploitable par un contrôleur, sans erreur nulle part. Le banc
# porte donc surtout sur la conformité de ce qui sort, et sur les deux décisions de modélisation
# qui ne se devinent pas (l'énumération plutôt qu'un booléen, l'absence de Sender).
#
#   $ ./venv/bin/python tools/verif_nmos_is07.py
import json
import os
import sys
import urllib.error
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import is07                                      # noqa: E402

print("IS-07 — tally publié en NMOS\n")

# ── 1. Les deux décisions de modélisation ────────────────────────────────────
controle("★ le type d'événement est une ÉNUMÉRATION, pas un booléen",
         is07.TYPE_EVENEMENT == "string/enum/Tally",
         "notre tally vaut off/red/green/amber ; le réduire à un booléen aurait demandé de "
         "décider quelle couleur signifie « à l'antenne » — une convention de site — et aurait "
         "effacé l'ambre et le vert")
controle("il suit la forme `{base}/enum/{Name}` d'IS-07",
         is07.TYPE_EVENEMENT.count("/") == 2 and "/enum/" in is07.TYPE_EVENEMENT)
controle("les quatre valeurs du tally sont déclarées",
         set(is07.VALEURS) == {"off", "red", "green", "amber"})
controle("les trois niveaux TSL sont publiés tels quels",
         [n for _i, n in is07.NIVEAUX] == ["LH", "RH", "TT"],
         "les traduire en « program »/« preview » serait une convention de site : la présumer "
         "ferait mentir l'étiquette chez qui ne l'applique pas")

# ── 2. Identité : dérivée du FLUX, pas de l'index TSL ────────────────────────
a = is07._sid("flux_a", 0)
controle("l'identité d'une Source est déterministe", a == is07._sid("flux_a", 0))
controle("★ elle dépend du FLUX et du niveau, jamais de l'index TSL",
         a != is07._sid("flux_b", 0) and a != is07._sid("flux_a", 1),
         "un index TSL est une adresse de pupitre : il se réattribue d'une modification de table, "
         "et une identité NMOS qui en dépendrait changerait sous le contrôleur")
controle("Source et Flow ne partagent pas le même identifiant",
         is07._sid("flux_a", 0) != is07._fid("flux_a", 0))

# ── 3. Conformité du message STATE ───────────────────────────────────────────
_par = is07._par_id()
if not _par:
    print("  SAUTÉ  aucun flux tallyé au mapping TSL — le reste du banc n'a rien à décrire")
else:
    sid = next(iter(_par))
    e = is07.etat_source(sid)
    controle("le message STATE porte les cinq champs exigés",
             set(e) == {"identity", "event_type", "timing", "payload", "message_type"},
             "IS-07 § Message types — obtenu %s" % sorted(e))
    controle("son `message_type` vaut « state »", e["message_type"] == "state")
    controle("son `identity` porte source_id ET flow_id",
             set(e["identity"]) == {"source_id", "flow_id"} and e["identity"]["source_id"] == sid)
    controle("son `timing` porte des horodatages « secondes:nanosecondes »",
             all(":" in e["timing"][k] and e["timing"][k].split(":")[1].isdigit()
                 and len(e["timing"][k].split(":")[1]) == 9
                 for k in ("creation_timestamp", "origin_timestamp")),
             "obtenu %s" % e["timing"])
    controle("sa charge utile est une valeur de l'énumération publiée",
             e["payload"]["value"] in is07.VALEURS, "obtenu %r" % e["payload"]["value"])
    controle("une source inconnue rend None, pas un état inventé",
             is07.etat_source("00000000-0000-0000-0000-000000000000") is None)



# ── 4. Un flux mappé sur DEUX pupitres ne compte qu'une fois ─────────────────
# ⚠ Éprouvé sur un JEU D'ESSAI, pas sur la table réelle : celle du site n'a aujourd'hui aucun flux
# mappé deux fois, donc la garde y serait invisible — vérifié par mutation, elle passait même
# désarmée. Un contrôle dont le résultat dépend des données du moment ne prouve rien.
import app.database as _db_is07                                     # noqa: E402

_vrai_map = _db_is07.db_get_tsl_mappings_all
_db_is07.db_get_tsl_mappings_all = lambda: [
    {"connection_id": 1, "tsl_index": 5, "source_shm": "flux_partage"},
    {"connection_id": 2, "tsl_index": 9, "source_shm": "flux_partage"},   # MÊME flux, 2e pupitre
    {"connection_id": 1, "tsl_index": 6, "source_shm": "flux_seul"},
]
try:
    _src = is07._sources()
    _shms = sorted({s for s, _i, _n, _l in _src})
    controle("★ un flux mappé sur DEUX pupitres n'est publié qu'UNE fois",
             len(_src) == 2 * len(is07.NIVEAUX) and _shms == ["flux_partage", "flux_seul"],
             "sinon un contrôleur verrait des doublons changeant ensemble, sans savoir lequel "
             "fait foi — obtenu %d entrées pour %s" % (len(_src), _shms))
finally:
    _db_is07.db_get_tsl_mappings_all = _vrai_map


# ── 5. Sur le HTTP réel : la surface, et son absence de Sender ───────────────
def _http(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


B = "http://127.0.0.1:5000"
code, srcs = _http(B + "/x-nmos/events/v1.0/sources")
if code != 200:
    print("  SAUTÉ  Events API injoignable ou IS-07 désactivé (HTTP %s)" % code)
else:
    controle("l'Events API liste les sources", isinstance(srcs, list) and bool(srcs))
    _s = srcs[0].rstrip("/")
    code, t = _http("%s/x-nmos/events/v1.0/sources/%s/type" % (B, _s))
    controle("/type rend la définition du type d'événement",
             code == 200 and t.get("type") == is07.TYPE_EVENEMENT)
    code, st = _http("%s/x-nmos/events/v1.0/sources/%s/state" % (B, _s))
    controle("/state rend un message STATE conforme",
             code == 200 and st.get("message_type") == "state"
             and st["payload"]["value"] in is07.VALEURS)
    code, _ = _http("%s/x-nmos/events/v1.0/sources/inexistante/state" % B)
    controle("une source inconnue rend 404", code == 404, "obtenu %s" % code)

    code, senders = _http(B + "/x-nmos/node/v1.3/senders")
    if code == 200:
        _ws7 = [x for x in senders if "websocket" in (x.get("transport") or "")]
        # ★ La règle n'est pas « pas de Sender », c'est « un Sender SI ET SEULEMENT SI le
        # transport est servi ». Ce contrôle a été écrit avant que le transport existe, et il
        # affirmait l'absence : il est tombé dès qu'on a livré le serveur, ce qui est exactement
        # ce qu'un banc doit faire quand la vérité change.
        controle("★ un Sender est annoncé SI ET SEULEMENT SI le transport est servi",
                 bool(_ws7) == is07.ws_actif(),
                 "annoncer un Sender sans serveur promettrait un abonnement qui n'arriverait "
                 "jamais — %d sender(s) websocket pour ws_actif()=%s"
                 % (len(_ws7), is07.ws_actif()))
        if _ws7:
            controle("son manifest_href est null (IS-07 n'a pas de fichier de transport)",
                     all(x["manifest_href"] is None for x in _ws7))
    code, sources04 = _http(B + "/x-nmos/node/v1.3/sources")
    if code == 200:
        ev = [x for x in sources04 if x.get("event_type")]
        controle("les Sources IS-07 sont bien dans le modèle IS-04, en format `data`",
                 bool(ev) and all(x["format"] == "urn:x-nmos:format:data" for x in ev))

# ── 6. Transport WebSocket ───────────────────────────────────────────────────
print("\nIS-07 — transport WebSocket\n")

controle("le délai de garde vaut 12 s, comme la spec le CHIFFRE",
         is07.SANTE_TIMEOUT_S == 12,
         "IS-07 : 2 battements manqués (5 s) + 2 s de latence. Garder une session muette ouverte "
         "ferait croire à une supervision en place alors que plus personne n'écoute")

if _par:
    _sid7 = next(iter(_par))
    _tp = is07.transport_params(_sid7)
    controle("les transport_params portent les quatre champs d'IS-07",
             set(_tp) == {"connection_uri", "connection_authorization",
                          "ext_is_07_source_id", "ext_is_07_rest_api_url"},
             "obtenu %s" % sorted(_tp))
    controle("★ toutes les sources partagent la MÊME connection_uri",
             len({is07.transport_params(x)["connection_uri"] for x in list(_par)[:5]}) == 1,
             "« All senders on one NMOS device should offer the same connection_uri to allow the "
             "number of WebSocket connections needed to be reduced »")
    controle("mais chacune a son propre ext_is_07_source_id",
             _tp["ext_is_07_source_id"] == _sid7)

# ★ Le Sender n'existe QUE si le transport est servi.
# ⚠ On bascule LES DEUX réglages : `ressources()` ne publie rien tant qu'IS-07 est globalement
# fermé, donc ne toucher que `nmos_is07_ws` rendait ce contrôle dépendant de l'état du site — il
# passait avec IS-07 ouvert et échouait avec IS-07 fermé, sans rien dire du code.
_avant_ws = _avant_07 = None
try:
    from app.database import db_get_setting as _g7, db_set_setting as _s7
    _avant_ws, _avant_07 = _g7("nmos_is07_ws", None), _g7("nmos_is07", None)
    _s7("nmos_is07", True)
    _s7("nmos_is07_ws", False)
    controle("★ transport fermé → AUCUN Sender n'est publié",
             not is07.ressources("d", "1:0")["senders"],
             "un Sender annonce une URI sur laquelle les états sont POUSSÉS : l'annoncer sans "
             "serveur promettrait un abonnement qui n'arriverait jamais")
    _s7("nmos_is07_ws", True)
    controle("transport ouvert → un Sender par source",
             len(is07.ressources("d", "1:0")["senders"]) == len(_par))
finally:
    if _avant_ws is not None:
        _s7("nmos_is07_ws", _avant_ws)
    if _avant_07 is not None:
        _s7("nmos_is07", _avant_07)

# ── Le push ne va QU'aux abonnés de la source ────────────────────────────────
class _FausseSession:
    def __init__(self, sources):
        self.sources, self.recu = set(sources), []

    def envoyer(self, m):
        self.recu.append(m)


if _par:
    _s_abonne = _FausseSession([_sid7])
    _s_autre = _FausseSession(["une-autre"])
    with is07._sessions_lock:
        is07._sessions.update({_s_abonne, _s_autre})
    try:
        is07._pousser([_sid7])
        controle("★ un changement n'est poussé qu'aux ABONNÉS de cette source",
                 len(_s_abonne.recu) == 1 and not _s_autre.recu,
                 "pousser à tout le monde ferait recevoir à chaque client des états qu'il n'a pas "
                 "demandés, et qu'il ne saurait pas rattacher")
        controle("et ce qui est poussé est un message STATE",
                 _s_abonne.recu and _s_abonne.recu[0].get("message_type") == "state")
    finally:
        with is07._sessions_lock:
            is07._sessions.discard(_s_abonne)
            is07._sessions.discard(_s_autre)

# ── Vivant : poignée de main, abonnement, battement ──────────────────────────
if is07.ws_actif() and _par:
    from services.nmos.client_is12 import Client, ErreurIS12   # couche WebSocket générique
    import time as _t7
    try:
        _c7 = Client("ws://127.0.0.1:%d/" % is07._port(), timeout=6).connecter()
    except ErreurIS12 as _e7:
        _c7 = None
        print("  SAUTÉ  transport WebSocket injoignable (%s)" % str(_e7)[:60])
    if _c7:
        try:
            _c7._envoyer({"command": "subscription", "sources": [_sid7, "inconnue"]})
            _m7 = _c7._lire_message(_t7.monotonic() + 5)
            controle("★ l'abonnement RENVOIE aussitôt l'état courant",
                     _m7 is not None and _m7.get("message_type") == "state"
                     and _m7["identity"]["source_id"] == _sid7,
                     "« the server will resend all the current states » — sans ce renvoi, un "
                     "abonné reste aveugle jusqu'au prochain changement, qui peut ne jamais venir")
            _c7._envoyer({"command": "health", "timestamp": "1788000000:000000000"})
            _m7 = _c7._lire_message(_t7.monotonic() + 5)
            controle("le battement est acquitté par un message `health`",
                     _m7 is not None and _m7.get("message_type") == "health")
            controle("et l'horodatage d'origine du client est renvoyé",
                     _m7["timing"]["origin_timestamp"] == "1788000000:000000000")
        finally:
            _c7.fermer()
else:
    print("  SAUTÉ  transport WebSocket désactivé (`nmos_is07_ws`)")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
