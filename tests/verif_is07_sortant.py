#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du TALLY SORTANT en IS-07 : d'un écrivain interne jusqu'à la Source qu'un contrôleur lit.
#
# CE QU'IL PROTÈGE. La surface IS-07 est FERMÉE par défaut, donc rien ne l'exerce au quotidien :
# elle peut se rompre sans que personne ne s'en aperçoive avant le jour où on l'ouvre pour un
# tiers. Quatre maillons, et chacun casse en silence :
#   · les Sources sont clées sur (flux, niveau) — une erreur de clé et le contrôleur s'abonne à
#     une Source qui ne changera jamais ;
#   · la définition de type doit DÉCLARER ses valeurs (`values`), sinon personne ne peut lire
#     notre énumération : IS-07 laisse le contenu des enums au constructeur ;
#   · `/state` doit refléter le CUMUL, pas la dernière écriture — c'est là que se voit l'ambre
#     d'une source à la fois au programme et en préparation ;
#   · le réglage doit être restauré, sinon le banc laisse une surface externe ouverte.
#
# ⚠ CE BANC OUVRE `nmos_is07` LE TEMPS DE LA MESURE et le REFERME dans un `finally`. Il n'émet
# rien sur le réseau : tout passe par le client de test Flask.
#
#   $ ./venv/bin/python tools/verif_is07_sortant.py
import importlib
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


from app.database import db_set_setting, db_get_setting, get_db      # noqa: E402

print("IS-07 sortant — d'un écrivain interne jusqu'à la Source publiée\n")

AVANT = db_get_setting("nmos_is07")
try:
    db_set_setting("nmos_is07", "1")
    from services import tsl                                        # noqa: E402
    from services.nmos import is07                                  # noqa: E402
    importlib.reload(is07)
    import main                                                     # noqa: E402
    app = main.app
    app.config["TESTING"] = True
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]

    controle("★ le réglage s'ouvre bien pour la mesure", is07.actif())

    srcs = is07._sources()
    controle("★ des Sources sont publiables, sinon le banc ne prouve rien", bool(srcs),
             "il faut au moins une entrée de correspondance TSL rattachée à un niveau")
    if not srcs:
        raise SystemExit(1)

    shm, idx, niveau, nom = srcs[0]
    sid = is07._sid(shm, niveau)

    # ⚠ CE CONTRÔLE A ÉTÉ AJOUTÉ APRÈS UNE MUTATION MUETTE. Le banc dérivait l'identifiant avec
    # la fonction même qu'il testait : retirer le niveau de la clé le faisait acquiescer. On
    # vérifie donc la PROPRIÉTÉ qui compte — deux niveaux d'un même flux sont deux signaux
    # distincts, et doivent donner deux Sources distinctes. Les confondre ferait voir à un
    # contrôleur une seule lampe pour deux chaînes de destination.
    controle("★★★ deux niveaux d'un même flux donnent deux Sources différentes",
             is07._sid(shm, "niveau-a") != is07._sid(shm, "niveau-b")
             and is07._fid(shm, "niveau-a") != is07._fid(shm, "niveau-b")
             and is07._snd(shm, "niveau-a") != is07._snd(shm, "niveau-b"),
             "une Source qui ne dépend que du flux fait voir une seule lampe pour deux chaînes")
    controle("★★ ...et la même paire donne toujours le même identifiant",
             is07._sid(shm, niveau) == is07._sid(shm, niveau),
             "les UUID doivent être stables : un contrôleur les mémorise")

    liste = cli.get("/x-nmos/events/v1.0/sources/").get_json() or []
    controle("★★ la Source est listée par l'Events API",
             "%s/" % sid in liste,
             "un contrôleur qui ne la voit pas ne s'y abonnera jamais. Obtenu %s" % liste[:2])

    t = cli.get("/x-nmos/events/v1.0/sources/%s/type" % sid).get_json() or {}
    controle("★★★ la définition de type DÉCLARE ses valeurs",
             [v.get("value") for v in (t.get("values") or [])]
             == ["off", "red", "green", "amber"],
             "IS-07 laisse le contenu des enums au constructeur : sans `values`, notre "
             "`string/enum/Tally` est illisible pour qui ne nous connaît pas. Obtenu %r" % t)

    def lu():
        return ((cli.get("/x-nmos/events/v1.0/sources/%s/state" % sid).get_json() or {})
                .get("payload", {}).get("value"))

    st = cli.get("/x-nmos/events/v1.0/sources/%s/state" % sid).get_json() or {}
    controle("★★ l'état porte l'identité de SA Source et de SON Flow",
             (st.get("identity") or {}).get("source_id") == sid
             and (st.get("identity") or {}).get("flow_id") == is07._fid(shm, niveau),
             "un état qui ne dit pas de quoi il parle oblige le contrôleur à le déduire de "
             "l'URL — et le rend faux au premier relais. Obtenu %r" % st.get("identity"))
    controle("★ et un horodatage IS-07 « secondes:nanosecondes »",
             ":" in str((st.get("timing") or {}).get("creation_timestamp") or ""),
             "obtenu %r" % st.get("timing"))

    # ── LE BOUT EN BOUT, avec le cumul de deux écrivains ─────────────────
    tsl.poser_tally("banc:a", {})
    tsl.poser_tally("banc:b", {})
    controle("★ au départ la Source est éteinte", lu() == "off", "obtenu %r" % lu())

    tsl.poser_tally("banc:a", {(idx, niveau): "red"})
    controle("★★★ un écrivain interne allume la Source publiée", lu() == "red",
             "c'est la chaîne entière : état interne → clé (flux, niveau) → Source IS-07. "
             "Obtenu %r" % lu())

    tsl.poser_tally("banc:b", {(idx, niveau): "green"})
    controle("★★★ DEUX écrivains sur le même niveau donnent l'ambre à la Source",
             lu() == "amber",
             "une source à la fois au programme et en préparation est un état réel et courant ; "
             "publier « red » perdrait qu'elle est déjà armée ailleurs. Obtenu %r" % lu())

    tsl.poser_tally("banc:a", {})
    controle("★★★ un écrivain qui se tait ne coupe pas l'autre", lu() == "green",
             "c'est LE piège d'une couche d'état unique : le second écrivain éteint la lampe du "
             "premier. Obtenu %r" % lu())

    tsl.poser_tally("banc:b", {})
    controle("★★ quand tous se taisent, la Source repasse à « off »", lu() == "off",
             "obtenu %r" % lu())
finally:
    try:
        from services import tsl as _t
        _t.poser_tally("banc:a", {})
        _t.poser_tally("banc:b", {})
        residu = _t.get_tally_state()
    except Exception:
        residu = "?"
    db_set_setting("nmos_is07", "0" if AVANT in (None, "", 0, "0", False) else AVANT)
    remis = db_get_setting("nmos_is07")
    print("\n  réglage `nmos_is07` restauré : %r · état de tally résiduel : %r" % (remis, residu))
    if str(remis).strip().lower() in ("1", "true", "on", "yes"):
        echecs.append("réglage laissé OUVERT")
        print("        ⚠ LA SURFACE EST RESTÉE OUVERTE — à refermer à la main")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
