#!/usr/bin/env python3
"""Le plafond de corps de Waitress ne doit jamais être plus bas qu'une limite de route.

Waitress plafonne PAR DÉFAUT tout corps de requête à 1 Gio et répond 413 (ou coupe la connexion,
« Failed to fetch » côté navigateur) avant que la route ne voie la requête. Le 2026-10-27 sur la Z8,
l'archive Desktop Video 16.4 (2 079 621 523 o) a été refusée ainsi alors que la route de dépôt et
l'agent annoncent 2 Gio : la limite affichée n'était pas celle qui s'appliquait.

Lecture par AST : importer `main` démarre l'application et fabrique la base.

    ./venv/bin/python tests/verif_waitress_corps_max.py
"""
import ast
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def const_int(tree, nom):
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == nom for t in n.targets):
            return eval(compile(ast.Expression(n.value), nom, "eval"))      # littéral entier (ex. 2 << 30)
    return None


main = ast.parse(open(os.path.join(_R, "main.py"), encoding="utf-8").read())
hd = ast.parse(open(os.path.join(_R, "app", "routes", "host_drivers.py"), encoding="utf-8").read())
agent = ast.parse(open(os.path.join(_R, "node_agent", "agent.py"), encoding="utf-8").read())

plafond = const_int(main, "_WAITRESS_BODY_MAX")
route = const_int(hd, "_UPLOAD_MAX")
ag = const_int(agent, "HD_UPLOAD_MAX")
ok(plafond is not None, "main.py définit _WAITRESS_BODY_MAX")
ok(route is not None and ag is not None, "limites de dépôt de pilote lues", (route, ag))
if plafond and route and ag:
    ok(plafond >= max(route, ag), "plafond Waitress ≥ limite de la route et de l'agent",
       "%d < %d" % (plafond, max(route, ag)))

# Le SITE D'APPEL, pas la définition : le serveur Waitress doit recevoir le plafond. Depuis le
# 2026-10-29, main.py construit le serveur par `create_server` (pour en lire les fils occupés,
# cf. app/http_connections.py) au lieu de `serve` — les deux acceptent les mêmes réglages.
appels = [n for n in ast.walk(main) if isinstance(n, ast.Call)
          and getattr(n.func, "id", None) in ("serve", "create_server")]
ok(len(appels) == 1, "un seul appel serve()/create_server() dans main.py", len(appels))
kw = {k.arg: k.value for c in appels for k in c.keywords}
ok(isinstance(kw.get("max_request_body_size"), ast.Name)
   and kw["max_request_body_size"].id == "_WAITRESS_BODY_MAX",
   "serve() reçoit max_request_body_size=_WAITRESS_BODY_MAX", ast.dump(kw.get("max_request_body_size", ast.Constant(None))))

print()
if ko:
    print("%d ÉCHEC(S)" % len(ko))
    sys.exit(1)
print("tout est vert")
