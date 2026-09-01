#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Sonde MS-05-02 : lit le modèle de contrôle d'un appareil TIERS par IS-14, et sait le piloter.
#
# À QUOI ÇA SERT. En salle d'interopérabilité, la première question est toujours « qu'est-ce que
# ton appareil expose, exactement ? ». Cet outil répond sans rien savoir de l'implémentation du
# pair : il part de son Node IS-04, y trouve le point de contrôle ANNONCÉ dans `controls[]`, et
# parcourt le modèle. Aucune URL n'est devinée — un appareil qui n'annonce pas son point de
# contrôle n'en a pas, et fabriquer l'adresse à sa place produirait des 404 pris pour des pannes.
#
#   # inventaire du modèle d'un pair (lecture seule)
#   $ ./venv/bin/python tools/sonde_ncp.py http://10.0.0.9:5000
#
#   # un objet en particulier
#   $ ./venv/bin/python tools/sonde_ncp.py http://10.0.0.9:5000 --objet root.plugins.plugin_42
#
#   # écrire une propriété / invoquer une méthode (⚠ MUTE le pair)
#   $ ./venv/bin/python tools/sonde_ncp.py <base> --objet <chemin> --ecrire 3p3=37
#   $ ./venv/bin/python tools/sonde_ncp.py <base> --objet <chemin> --invoquer 3m1 --args '{}'
import argparse
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from services.nmos import client_ncp as cl                          # noqa: E402


def _base(cible):
    """`cible` est soit un Node IS-04 (on y découvre le point de contrôle), soit directement une
    Configuration API. On distingue sur le chemin plutôt que de sonder à l'aveugle."""
    cible = cible.rstrip("/")
    if "/x-nmos/configuration/" in cible:
        return cible
    pts = cl.points_de_controle_du_node(cible)
    if not pts:
        raise cl.ErreurTiers(
            "aucun Device de ce Node n'annonce %s dans son tableau `controls` — "
            "l'appareil n'expose pas de Configuration API" % cl.TYPE_IS14)
    if len(pts) > 1:
        print("  (%d Devices annoncent un point de contrôle ; on prend le premier)" % len(pts),
              file=sys.stderr)
    return pts[0][1]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cible", help="URL d'un Node IS-04, ou directement une Configuration API")
    ap.add_argument("--objet", help="chemin de rôle à détailler (sinon : inventaire complet)")
    ap.add_argument("--ecrire", metavar="ID=VALEUR", help="⚠ écrit une propriété sur le pair")
    ap.add_argument("--invoquer", metavar="ID", help="⚠ invoque une méthode sur le pair")
    ap.add_argument("--args", default="{}", help="arguments JSON de --invoquer")
    ap.add_argument("--max", type=int, default=200, help="objets max à l'inventaire")
    a = ap.parse_args()

    try:
        base = _base(a.cible)
    except cl.ErreurTiers as e:
        print("ÉCHEC : %s" % e, file=sys.stderr)
        return 2
    print("point de contrôle : %s\n" % base)

    if not a.objet:
        objets = cl.inventaire(base, max_objets=a.max)
        print("%d objet(s) :\n" % len(objets))
        for o in objets:
            if "erreur" in o:
                print("  %-46s ⚠ %s" % (o["rolePath"], o["erreur"]))
                continue
            print("  %-46s %-18s %s" % (o["rolePath"], o.get("name") or "?",
                                        o.get("classId")))
        return 0

    d = cl.descripteur(base, a.objet) or {}
    d = d.get("value", d)
    print("classe      : %s %s" % (d.get("name"), d.get("classId")))
    print("description : %s" % d.get("description"))
    print("propriétés  :")
    for pid in cl.proprietes(base, a.objet):
        try:
            print("   %-6s = %r" % (pid, cl.lire(base, a.objet, pid)))
        except cl.ErreurTiers as e:
            print("   %-6s ⚠ %s" % (pid, e))
    meths = _http_liste(base, a.objet)
    if meths:
        print("méthodes    : %s" % ", ".join(meths))

    if a.ecrire:
        pid, _, val = a.ecrire.partition("=")
        try:
            cl.ecrire(base, a.objet, pid.strip(), val)
            print("\nécriture %s = %r : ACCEPTÉE" % (pid.strip(), val))
        except cl.ErreurTiers as e:
            print("\nécriture %s : REFUSÉE — %s" % (pid.strip(), e))
            return 1
    if a.invoquer:
        try:
            r = cl.invoquer(base, a.objet, a.invoquer, json.loads(a.args))
            print("\ninvocation %s : ACCEPTÉE — %s" % (a.invoquer,
                                                       json.dumps(r, ensure_ascii=False)[:160]))
        except cl.ErreurTiers as e:
            print("\ninvocation %s : REFUSÉE — %s" % (a.invoquer, e))
            return 1
    return 0


def _http_liste(base, chemin):
    try:
        return [str(x).rstrip("/") for x in
                (cl._http("GET", "%s/rolePaths/%s/methods/" % (base, chemin)) or [])]
    except cl.ErreurTiers:
        return []


if __name__ == "__main__":
    sys.exit(main())
