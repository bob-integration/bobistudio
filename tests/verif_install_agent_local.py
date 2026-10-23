#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""L'enrôlement de l'agent LOCAL (tout-en-un) doit aboutir, ou le DIRE.

★ LA PANNE QUI JUSTIFIE CETTE GARDE — 2026-10-23, station Debian 13 installée par la FLOTTE.
L'installation affichait « prêt », `bobistudio.service` et `bobi-node-agent` tournaient tous les
deux, et la table `nodes` était VIDE : « Aucun nœud déclaré », donc rien de déployable.

`register_local_agent()` lançait un enfant qui fait `from app import …`, SANS fixer le répertoire
courant. Or le script d'installation servi par un orchestrateur (`app/routes/updates.py`) fait
`TMP="$(mktemp -d)"; cd "$TMP"` et n'y télécharge QUE `install.py`, `install_proxmox.py` et le
zip — pas `app/`. L'import échouait donc à 100 % sur ce chemin, et la trace partait à la poubelle
(`run(capture=True)` ne rend que `stdout`). Il restait un `warn` noyé, puis « prêt ».

⚠ `get.sh` y échappait PAR ACCIDENT : il fait `cd "$SRC"`, et cet arbre temporaire contient
`app/`. Il importait donc la copie TÉLÉCHARGÉE au lieu de celle installée. Un même défaut, deux
chemins, dont un seul tombait — c'est pourquoi la garde vise le `cwd`, pas le symptôme.

Et le défaut SYMÉTRIQUE, plus trompeur encore : `node_driver.register()` rend `(False, err)` sans
lever. Juger sur le code de sortie d'un `print(...)` annonçait « Agent local enregistré » alors
que l'agent ne répondait pas. On juge donc sur LA LIGNE EN BASE, qui est le but réel.

Aucun effet de bord : on ne lance jamais `register()`, et on ne touche à aucune base.
"""
import ast
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "install", "install.py")

_echecs = []


def verif(ok, titre, detail=""):
    print(f"  {'✓' if ok else '✗'} {titre}")
    if not ok:
        if detail:
            print(f"      {detail}")
        _echecs.append(titre)


def _fonction(arbre, nom):
    for n in ast.walk(arbre):
        if isinstance(n, ast.FunctionDef) and n.name == nom:
            return n
    return None


arbre = ast.parse(open(SRC, encoding="utf-8").read())
fn = _fonction(arbre, "register_local_agent")
appelant = _fonction(arbre, "path_all_in_one")
src_fn = ast.unparse(fn) if fn else ""
# ⚠ SANS LA DOCSTRING : elle CITE le défaut (« interpolé dans du source ('{token}') »),
# et une garde qui lit la documentation du défaut le trouve toujours présent.
corps_fn = "\n".join(ast.unparse(n) for n in (fn.body[1:] if fn else [])
                     ) if fn else ""

print("\n── la PRÉMISSE du défaut est réelle (sans base, sans réseau)")
# Si cet import marchait depuis n'importe où, toute la garde serait sans objet.
with tempfile.TemporaryDirectory() as ailleurs:
    r_ailleurs = subprocess.run([sys.executable, "-c", "import app"],
                                cwd=ailleurs, capture_output=True, text=True)
r_racine = subprocess.run([sys.executable, "-c", "import app"],
                          cwd=RACINE, capture_output=True, text=True)
verif(r_ailleurs.returncode != 0, "★ `import app` ÉCHOUE depuis un dossier temporaire",
      "il réussit : la prémisse a changé, cette garde ne protège plus rien")
verif("No module named 'app'" in r_ailleurs.stderr, "   et c'est bien un ModuleNotFoundError",
      r_ailleurs.stderr.strip().splitlines()[-1:] or "(aucune trace)")
verif(r_racine.returncode == 0, "`import app` réussit depuis la racine du dépôt",
      r_racine.stderr.strip()[-200:])

print("\n── l'enfant doit être lancé DEPUIS le répertoire de l'application")
appels = [n for n in ast.walk(fn) if isinstance(n, ast.Call)] if fn else []
avec_cwd = [c for c in appels if any(k.arg == "cwd" for k in c.keywords)]
verif(bool(avec_cwd), "★ un appel de sous-processus passe `cwd=`",
      "sans cwd, l'enfant importe `app` depuis le dossier d'où l'installeur a été lancé")
verif(any(any(k.arg == "cwd" and ast.unparse(k.value) == "APP_DIR" for k in c.keywords)
          for c in appels), "   et ce `cwd` vaut APP_DIR")
# `run()` (install_proxmox.py) n'accepte PAS de cwd : s'en servir ici rouvre la panne.
verif(not any(isinstance(c.func, ast.Name) and c.func.id == "run" for c in appels),
      "★ n'utilise PAS le raccourci `run()`, qui ne sait pas fixer le cwd")

print("\n── le jeton ne doit pas être écrit dans du source")
verif("BOBI_AGENT_TOKEN" in src_fn, "★ le jeton passe par l'environnement")
verif("{token}" not in corps_fn and "'+token+'" not in corps_fn.replace(" ", ""),
      "   il n'est plus interpolé dans la chaîne de code",
      "une apostrophe ou un antislash casserait le script, ou y injecterait du code")

print("\n── le verdict doit venir du RÉSULTAT, pas du code de sortie d'un print")
verif("db_get_node_by_host" in src_fn, "★ l'enfant relit la LIGNE EN BASE")
verif("sys.exit" in src_fn, "   et sort non-zéro quand elle manque")
verif(any(isinstance(n, ast.Return) and n.value is not None
          for n in ast.walk(fn)) if fn else False,
      "la fonction REND un verdict à son appelant")

print("\n── une installation sans nœud ne doit pas s'annoncer « prête »")
# On vise le SITE D'APPEL : chercher la chaîne « aio_ready » attraperait sa définition i18n.
def _dit(n, cle):
    return any(isinstance(c, ast.Constant) and c.value == cle for c in ast.walk(n))

nus = [n for n in (appelant.body if appelant else [])
       if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and _dit(n, "aio_ready")]
verif(not nus, "★ « aio_ready » n'est plus annoncé sans condition",
      "un appel nu subsiste dans path_all_in_one : la panne redevient silencieuse")
verif(_dit(appelant, "aio_partial") if appelant else False,
      "   l'état INCOMPLET a son propre message")
verif(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "register_local_agent"
          for n in ast.walk(appelant)) if appelant else False,
      "l'appelant appelle bien register_local_agent")

print("\n── les messages doivent exister dans LES DEUX langues, et viser un écran qui existe")
textes = open(SRC, encoding="utf-8").read()
for cle in ("agent_local_bad", "agent_local_fix", "aio_partial"):
    verif(textes.count(f'"{cle}":') >= 2, f"`{cle}` est dans fr ET en",
          f"{textes.count(chr(34) + cle + chr(34) + ':')} occurrence(s)")
verif("Déploiement → Nœuds" not in textes,
      "★ plus de renvoi à « Réglages → Déploiement → Nœuds », écran qui n'existe plus")

print()
if _echecs:
    print(f"✗ {len(_echecs)} contrôle(s) en échec :")
    for e in _echecs:
        print(f"   · {e}")
    sys.exit(1)
print("✓ tout est vert")
