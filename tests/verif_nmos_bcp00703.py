#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Conformité AMWA BCP-007-03 « NMOS Support for MXL » v1.0.0 (publiée le 2026-08-18).
#
# Le banc ne relit pas la spec à l'œil : il VALIDE nos ressources réellement servies contre les
# schémas JSON de la spec, vendorisés dans `services/nmos/nc_models/schemas/` (provenance et
# commit amont dans le NOTICE du dossier). Hors ligne — client de test Flask, aucun réseau.
#
# ⚠ POURQUOI IS-05 v1.2. BCP-007-03 l'exige (« Nodes compliant with this specification MUST
# implement IS-05 v1.2 or higher ») et ce n'est PAS une formalité de numéro : en v1.1,
# `sender_transport_params.json` est un `anyOf` FERMÉ sur rtp/dash/websocket/mqtt — nos
# paramètres MXL y sont INVALIDES, donc un contrôleur strict rejetait nos ressources. Le banc
# reproduit cette validation croisée pour que la raison ne se perde pas.
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


try:
    from jsonschema import Draft4Validator, RefResolver
except ImportError:
    print("  (jsonschema absent : `./venv/bin/python -m pip install jsonschema`)")
    sys.exit(1)

SCH = os.path.join(RACINE, "services", "nmos", "nc_models", "schemas")


def valider(dossier, fichier, instance):
    d = os.path.join(SCH, dossier) + os.sep
    sch = json.load(open(d + fichier, encoding="utf-8"))
    r = RefResolver(base_uri="file://" + d, referrer=sch)
    return sorted(e.message for e in Draft4Validator(sch, resolver=r).iter_errors(instance))


print("Conformité BCP-007-03 v1.0.0 — validation par schéma\n")

import main                                                          # noqa: E402
from app.database import get_db                                      # noqa: E402
from services import nmos                                            # noqa: E402

main.app.config["TESTING"] = True

# ★ IL FAUT UN UTILISATEUR RÉEL, et une base neuve n'en a aucun. `current_user()` relit
# l'utilisateur en base : forger une session sur un identifiant inventé ne passe pas
# `require_login`. Sur une base peuplée on prend le premier ; sur une base VIERGE — le cas de
# l'intégration continue — on en crée un jetable.
#
# ⚠ Ce banc passait en local et échouait en CI, sur `_u["id"]` avec `_u` à None. Un banc qui
# dépend de l'état de la base de son auteur ne dit rien de ce qu'il prétend vérifier.
def _utilisateur():
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
        if u is not None:
            return u["id"], u["username"]
        db.execute("INSERT INTO users (username, password_hash, role, session_epoch) "
                   "VALUES ('banc-bcp00703', 'x', 'admin', 0)")
        db.commit()
        u = db.execute("SELECT id, username FROM users WHERE username='banc-bcp00703'").fetchone()
        return u["id"], u["username"]


_UID, _UNAME = _utilisateur()
CLI = main.app.test_client()
with CLI.session_transaction() as _s:
    _s["user_id"] = _UID
    _s["username"] = _UNAME


def g(chemin):
    r = CLI.get(chemin)
    try:
        return r.status_code, r.get_json()
    except Exception:
        return r.status_code, None


# ⚠ LE MODÈLE NE SE CONSTRUIT PAS TOUT SEUL dans un processus de test. Il est bâti par un fil
# de fond du serveur ; un client de test neuf voit des listes VIDES. Sans cet appel, la moitié
# des contrôles ci-dessous passent À VIDE (`all()` d'une liste vide est vrai) — c'est ce qui
# s'est produit au premier jet, et seul le contrôle « il y a des ressources à vérifier » l'a
# révélé. Ne jamais le retirer.
nmos.rebuild_model()

MXL = "urn:x-nmos:transport:mxl"
_, senders = g("/x-nmos/node/v1.3/senders")
_, receivers = g("/x-nmos/node/v1.3/receivers")
_, flows = g("/x-nmos/node/v1.3/flows")
_, devices = g("/x-nmos/node/v1.3/devices")
tx = [s for s in (senders or []) if s.get("transport") == MXL]
rx = [r for r in (receivers or []) if r.get("transport") == MXL]
rtp = [s for s in (senders or []) if "rtp" in (s.get("transport") or "")]
V = nmos.IS05_VERSION

print("── Versions exigées ────────────────────────────────────────────────────")
controle("★★★ IS-05 v1.2 au moins (§ Connection Management)",
         nmos.IS05_VERSION >= "v1.2",
         "en v1.1 nos paramètres MXL sont INVALIDES contre le schéma de cette version-là : le "
         "contrôleur rejette la ressource. Obtenu %r" % nmos.IS05_VERSION)
controle("★★ IS-04 v1.3 au moins", nmos.IS04_VERSION >= "v1.3",
         "obtenu %r" % nmos.IS04_VERSION)
_, vers = g("/x-nmos/connection/")
controle("★★ la v1.2 est annoncée EN PREMIER",
         (vers or [None])[0] == "v1.2/", "obtenu %r" % (vers,))
controle("★★ la v1.1 reste servie (pas de régression pour un contrôleur 2110)",
         "v1.1/" in (vers or []) and g("/x-nmos/connection/v1.1/single/senders")[0] == 200,
         "couper la v1.1 casserait un contrôleur épinglé dessus, c'est-à-dire le chemin de "
         "PRODUCTION, pour un gain nul")
# ★★★ L'INVARIANT QUI COMPTE, et il a failli être enfreint. L'alias des versions secondaires
# dépendait d'un appel dans `main.py`. Or le service NMOS se met à jour SEUL depuis la page
# Catalogue : sur un cœur resté en arrière, `is05_root` annonçait `v1.1/` que plus aucune route
# ne servait → 404. Mesuré. Un contrôleur 2110 épinglé sur v1.1 aurait été coupé par une mise à
# jour censée ne rien casser. Le service pose désormais son alias lui-même (`bp.record_once`).
_muettes = [v for v in (vers or [])
            if g("/x-nmos/connection/%ssingle/senders" % v)[0] != 200]
controle("★★★ toute version ANNONCÉE répond vraiment", not _muettes,
         "annoncer une version qu'on ne sert pas est pire que ne pas l'annoncer : le contrôleur "
         "la choisit et tombe en 404. Obtenu %r" % (_muettes,))
controle("★★★ le cœur n'a rien à appeler pour ça",
         "installer_alias_is05" not in open(os.path.join(RACINE, "main.py"),
                                            encoding="utf-8").read(),
         "toute dépendance du service envers `main.py` est un piège de mise à jour : les deux "
         "ne se mettent pas à jour ensemble")

print("\n── Ressources IS-04 (§ Senders / Receivers) ────────────────────────────")
controle("★★ il y a des ressources MXL à vérifier", bool(tx) and bool(rx),
         "sans conteneur MXL le banc ne prouve rien — il ne doit pas passer pour autant")
controle("★★★ tout Sender MXL a un `interface_bindings` VIDE",
         all(s.get("interface_bindings") == [] for s in tx),
         "MUST de la spec : le MXL ne passe par aucune interface réseau")
controle("★★★ tout Sender MXL a `manifest_href` à null",
         all(s.get("manifest_href") is None for s in tx),
         "MUST : il n'y a pas de fichier de transport en MXL")
controle("★★★ tout Receiver MXL a un `interface_bindings` VIDE",
         all(r.get("interface_bindings") == [] for r in rx))
controle("★★ tout Receiver MXL déclare au moins un `media_types`",
         all(r.get("caps", {}).get("media_types") or r.get("format") for r in rx),
         "MUST : `media_types` avec au moins une valeur du registre")
controle("★★ tout Receiver MXL déclare des `constraint_sets` (BCP-004-01)",
         all((r.get("caps") or {}).get("constraint_sets") for r in rx),
         "MUST : sans quoi un contrôleur ne peut pas juger la compatibilité d'un Flow")

print("\n── IS-05 : transport_params (§ Transport Parameters) ───────────────────")
for quoi, liste, fichier in (("sender", tx, "sender_transport_params_mxl.json"),
                             ("receiver", rx, "receiver_transport_params_mxl.json")):
    ko, n = [], 0
    for r in liste[:30]:
        for ep in ("active", "staged"):
            st, d = g("/x-nmos/connection/%s/single/%ss/%s/%s" % (V, quoi, r["id"], ep))
            tps = (d or {}).get("transport_params") or []
            # MUST : un SEUL jeu de paramètres dans le tableau, staged comme active.
            if len(tps) != 1:
                ko.append((r["id"][:8], ep, "%d jeux" % len(tps)))
            for tp in tps:
                n += 1
                e = valider("bcp-007-03", fichier, tp)
                if e:
                    ko.append((r["id"][:8], ep, e[0][:90]))
    controle("★★★ %s : %d jeu(x) valides contre le schéma de la spec" % (quoi, n),
             n > 0 and not ko, "obtenu %r" % (ko[:3],))

ko = []
for quoi, liste in (("sender", tx), ("receiver", rx)):
    for r in liste[:30]:
        st, d = g("/x-nmos/connection/%s/single/%ss/%s/constraints" % (V, quoi, r["id"]))
        cs = d if isinstance(d, list) else []
        if len(cs) != 1:
            ko.append((r["id"][:8], "%d jeux de contraintes" % len(cs)))
        # MUST NOT : `auto` ne doit JAMAIS être listé comme option des contraintes.
        if "auto" in json.dumps(cs):
            ko.append((r["id"][:8], "`auto` listé dans les contraintes"))
controle("★★★ un seul jeu de contraintes, et `auto` n'y est jamais listé", not ko,
         "MUST NOT explicite de la spec — `auto` est une valeur de STAGING, pas une option "
         "que le contrôleur peut choisir. Obtenu %r" % (ko[:3],))

print("\n── IS-05 : endpoints (§ Sender / Receiver behaviour) ───────────────────")
controle("★★ /transportfile d'un Sender MXL rend 404",
         all(g("/x-nmos/connection/%s/single/senders/%s/transportfile" % (V, s["id"]))[0] == 404
             for s in tx[:20]),
         "MUST : « MUST always return a 404 »")
# ⚠ CE CONTRÔLE SEUL NE DISTINGUE RIEN — vérifié par mutation : en retirant la garde
# BCP-007-03, l'endpoint tombe dans le chemin RTP, interroge le conteneur, n'y trouve pas de
# SDP et rend 404 **quand même**. Même code, autre chemin, et 20 allers-retours HTTP de 2 s.
# Ce qui sépare les deux, c'est que la garde répond SANS toucher au conteneur. On rend donc le
# conteneur injoignable : avec la garde c'est toujours 404, sans elle ce serait 503.
import services.nmos as _n                                           # noqa: E402
_vrai = _n.requests.get


class _Injoignable(Exception):
    pass


def _ko(*a, **k):
    raise _Injoignable("conteneur injoignable (simulé par le banc)")


_n.requests.get = _ko
try:
    mxl_ko = [g("/x-nmos/connection/%s/single/senders/%s/transportfile" % (V, s["id"]))[0]
              for s in tx[:5]]
    rtp_ko = [g("/x-nmos/connection/%s/single/senders/%s/transportfile" % (V, s["id"]))[0]
              for s in rtp[:5]]
finally:
    _n.requests.get = _vrai
controle("★★★ le 404 MXL ne dépend PAS du conteneur",
         mxl_ko == [404] * len(mxl_ko) and len(mxl_ko) == 5,
         "« MUST always return a 404 » : toujours, y compris conteneur éteint. Sans la garde, "
         "la réponse dépendrait de ce que le conteneur raconte. Obtenu %r" % (mxl_ko,))
controle("★★★ …et le contre-exemple distingue vraiment",
         bool(rtp_ko) and all(c == 503 for c in rtp_ko),
         "si un Sender RTP rendait 404 lui aussi dans les mêmes conditions, le contrôle "
         "ci-dessus ne prouverait rien. Obtenu %r" % (rtp_ko,))
controle("★★ /transportfile d'un Sender RTP n'est PAS cassé",
         not rtp or g("/x-nmos/connection/%s/single/senders/%s/transportfile"
                      % (V, rtp[0]["id"]))[0] in (200, 503),
         "le SDP du 2110 est le chemin de production : un 404 ici serait une régression grave "
         "introduite en voulant satisfaire la BCP")

st, tt = g("/x-nmos/connection/%s/single/senders/%s/transporttype" % (V, tx[0]["id"])) if tx else (0, None)
controle("★★★ /transporttype existe et rend l'URN MXL", st == 200 and tt == MXL,
         "endpoint défini par IS-05 et jamais implémenté chez nous (404 sur une ressource qui "
         "existe). Obtenu %s %r" % (st, tt))
if rtp:
    st2, tt2 = g("/x-nmos/connection/%s/single/senders/%s/transporttype" % (V, rtp[0]["id"]))
    controle("★★★ /transporttype retire la sous-classification (.mcast)",
             tt2 == "urn:x-nmos:transport:rtp",
             "IS-05 : « with any subclassifications or versions removed ». Rendre "
             "`rtp.mcast` ferait échouer la validation du contrôleur. Obtenu %r "
             "(transport = %r)" % (tt2, rtp[0].get("transport")))
    controle("★★ la réponse valide le schéma IS-05 v1.2",
             not valider("is-05-v1.2", "transporttype-response-schema.json", tt2)
             and not valider("is-05-v1.2", "transporttype-response-schema.json", tt),
             "le schéma v1.1 était un `oneOf` d'énumération : `mxl` n'y passait pas")
st, eps = g("/x-nmos/connection/%s/single/senders/%s" % (V, tx[0]["id"])) if tx else (0, [])
controle("★★ /transporttype est ANNONCÉ dans la liste des endpoints",
         "transporttype/" in (eps or []),
         "un contrôleur qui suit l'arborescence ne le trouverait jamais. Obtenu %r" % (eps,))

print("\n── media_type (§ Flows) ────────────────────────────────────────────────")
# ⚠ Le registre AMWA est un SUPPLÉMENT à l'IANA : `video/raw` et `audio/L24` sont légitimes
# (« implementers MUST consult the applicable IS-04 schemas in addition to the entries in this
# register »). Ne pas les compter comme des écarts — c'est l'erreur facile.
REGISTRE_AMWA = {"video/v210", "video/v210a", "video/smpte291", "audio/float32"}
IANA_IS04 = {"video/raw", "video/jxsv", "audio/L16", "audio/L24", "audio/L32",
             "application/json", "video/H264", "video/H265"}
ids_mxl = {s.get("flow_id") for s in tx}
hors = {}
for f in (flows or []):
    if f["id"] not in ids_mxl:
        continue
    mt = f.get("media_type")
    if mt not in REGISTRE_AMWA and mt not in IANA_IS04:
        hors[mt] = hors.get(mt, 0) + 1
# ★ ÉCART CONNU ET ASSUMÉ, PAS UN DÉFAUT. `video/x-mxl-planar` n'est enregistré nulle part —
# c'est l'arbitrage planar contre v210, une DÉCISION PRODUIT en attente (cf. TODO § BCP-007-03).
# Verdir ce contrôle en retirant la vérification serait mentir ; le laisser rouge rendrait la CI
# rouge en permanence, donc invisible. On BORNE donc l'écart : il doit être exactement celui
# qu'on croit. Tout AUTRE media_type hors registre fait échouer — c'est ce qui compte, parce
# qu'un nouveau plugin peut en introduire un sans que personne ne le remarque.
ECART_ASSUME = {"video/x-mxl-planar"}
inattendus = {k: v for k, v in hors.items() if k not in ECART_ASSUME}
controle("★★★ aucun media_type hors registre AUTRE que l'écart assumé", not inattendus,
         "MUST de la spec. Un nouveau type non enregistré rend nos Flows non conformes sans que "
         "rien ne le signale. Obtenu %r" % (inattendus,))
controle("★ l'écart assumé est toujours le même, et il est DOCUMENTÉ",
         set(hors) <= ECART_ASSUME,
         "obtenu %r" % (hors,))
if hors:
    print("        ⚠ écart assumé, en attente d'arbitrage : %s"
          % ", ".join("%s (%d flows)" % (k, v) for k, v in sorted(hors.items())))

print("\n── Devices (rangement des surfaces) ────────────────────────────────────")
mxl_dev = [d for d in (devices or []) if "MXL" in (d.get("label") or "")]
controle("★★ le Device MXL n'annonce QUE la v1.2 d'IS-05",
         bool(mxl_dev) and sorted(c["type"] for c in mxl_dev[0]["controls"]
                                  if "sr-ctrl" in c["type"]) == ["urn:x-nmos:control:sr-ctrl/v1.2"],
         "annoncer la v1.1 inviterait un contrôleur à lire des paramètres que le schéma de SA "
         "version rejette — on lui tendrait un piège")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
