#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Pilote Blackmagic d'un nœud : on ne pose QUE l'architecture du nœud, et un échec de dpkg EST un échec.

★ Pourquoi ce test. L'archive officielle `Blackmagic_Desktop_Video_Linux_16.4.tar` porte plusieurs
architectures. `installer_pilote` cherchait `desktopvideo_*.deb` et passait TOUT à `dpkg -i` : un seul
paquet étranger dans la ligne fait refuser dpkg, donc le paquet amd64 présent juste à côté n'était pas
posé non plus. Vécu par le testeur sur la Z8 le 25/10 — « ce n'est pas la bonne version, ce n'est pas de
l'amd64 ».

★ Et la panne était MUETTE pour une seconde raison, indépendante : le verdict se lisait sur
`dpkg -i … | tail -40`, qui rend le code de `tail`, soit 0 quoi qu'il arrive. L'échec passait donc pour
une réussite et l'archive était renommée `.installed` — une reprise ne trouvait plus rien à poser.
Les deux défauts sont gardés ici, parce que corriger le premier sans le second aurait rendu le filtre
invisible : il aurait écarté les paquets, échoué, et annoncé « installé ».

    ./venv/bin/python tests/verif_pilote_blackmagic_arch.py
"""
import ast
import importlib.util
import os
import re
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("BOBI_BMD_SRC") or os.path.join(R, "node_agent", "host_drivers", "blackmagic.py")

spec = importlib.util.spec_from_file_location("blackmagic_under_test", SRC)
bmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bmd)

ECHECS = []


def verifier(nom, condition, sinon=""):
    if condition:
        print("  \033[32mOK\033[0m   %s" % nom)
    else:
        print("  \033[31mNON\033[0m  %s%s" % (nom, ("\n       " + sinon) if sinon else ""))
        ECHECS.append(nom)


def avec_arch(arch, debs):
    """Rejoue le filtre comme si le nœud était en `arch` (None = dpkg muet)."""
    vrai = bmd._architecture_noeud
    bmd._architecture_noeud = lambda: arch
    try:
        return bmd._filtrer_architecture(debs)
    finally:
        bmd._architecture_noeud = vrai


# ── l'archive 16.4 telle que Blackmagic la livre : plusieurs architectures côte à côte ──────────
ARCHIVE = [
    "/tmp/x/deb/x86_64/desktopvideo_16.4a7_amd64.deb",
    "/tmp/x/deb/arm64/desktopvideo_16.4a7_arm64.deb",
    "/tmp/x/deb/armhf/desktopvideo_16.4a7_armhf.deb",
]

print("1. l'architecture du nœud, et elle seule")
gardes, ecartes = avec_arch("amd64", ARCHIVE)
verifier("un nœud amd64 ne garde que le .deb amd64",
         [os.path.basename(g) for g in gardes] == ["desktopvideo_16.4a7_amd64.deb"],
         "gardés : %s" % [os.path.basename(g) for g in gardes])
verifier("les deux autres sont ÉCARTÉS, pas passés à dpkg", len(ecartes) == 2)
gardes, _ = avec_arch("arm64", ARCHIVE)
verifier("un nœud arm64 garde le .deb arm64 (le filtre suit le nœud, pas une constante)",
         [os.path.basename(g) for g in gardes] == ["desktopvideo_16.4a7_arm64.deb"])

print("2. les cas où écarter serait pire que de laisser dpkg trancher")
gardes, ecartes = avec_arch("amd64", ARCHIVE + ["/tmp/x/deb/desktopvideo_16.4a7_all.deb"])
verifier("un paquet `all` est gardé (posable partout)",
         any(g.endswith("_all.deb") for g in gardes))
gardes, ecartes = avec_arch("", ARCHIVE)
verifier("architecture du nœud INCONNUE → on ne jette RIEN",
         len(gardes) == len(ARCHIVE) and not ecartes,
         "un filtre comparé à vide écarterait le bon paquet")

print("3. aucune architecture ne correspond → échec qui NOMME ce qu'il a trouvé")
gardes, ecartes = avec_arch("riscv64", ARCHIVE)
verifier("rien n'est gardé", not gardes)
verifier("et tout est rapporté dans les écartés (pour l'afficher au journal)",
         len(ecartes) == len(ARCHIVE))
src = open(SRC).read()
verifier("le message d'échec cite l'architecture du nœud ET la liste trouvée",
         re.search(r"none of the packages found match this node's architecture", src)
         and re.search(r'say\("  found: %s" % os\.path\.basename', src),
         "sans la liste, la prochaine occurrence est aussi aveugle que celle de la Z8")

# ⚠ ET SURTOUT : QUE `installer_pilote` L'APPELLE. Première version de cette garde : elle
#   éprouvait `_filtrer_architecture` seule. Retirer l'appel dans `installer_pilote` — la
#   régression exacte qu'on corrige, celle qui repasse tout à dpkg — la laissait VERTE. Une
#   garde sur la fonction ne prouve pas le site d'appel.
arbre = ast.parse(open(SRC).read())
fonction = next((n for n in ast.walk(arbre)
                 if isinstance(n, ast.FunctionDef) and n.name == "installer_pilote"), None)
verifier("`installer_pilote` existe", fonction is not None)
appels = [c for c in ast.walk(fonction) if isinstance(c, ast.Call)
          and isinstance(c.func, ast.Name) and c.func.id == "_filtrer_architecture"] if fonction else []
verifier("et il APPELLE _filtrer_architecture avant de poser quoi que ce soit", len(appels) == 1,
         "sans cet appel, le filtre existe et ne sert à rien")
# le résultat du filtre doit être CE QUI EST POSÉ : `debs` réaffecté depuis l'appel.
reaffecte = [a for a in ast.walk(fonction) if isinstance(a, ast.Assign)
             and isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name)
             and a.value.func.id == "_filtrer_architecture"
             and any(isinstance(t, ast.Tuple) and any(isinstance(e, ast.Name) and e.id == "debs"
                                                      for e in t.elts) for t in a.targets)]
verifier("et son résultat REMPLACE la liste posée (`debs`)", len(reaffecte) == 1,
         "filtrer dans une variable qu'on n'utilise pas ne filtre rien")

print("4. le verdict de dpkg n'est pas celui de `tail`")
# ⚠ CHERCHÉ DANS L'ARBRE, PAS DANS LE TEXTE. Première version de cette garde : un
#   `re.search("dpkg -i .*\\|")` sur la source — il attrapait le COMMENTAIRE juste au-dessus du
#   correctif, qui cite le tube fautif pour l'expliquer. La garde était donc rouge sur du code
#   corrigé. Les littéraux de chaîne de l'arbre ne contiennent pas les commentaires.
commandes = [n.value for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Constant) and isinstance(n.value, str) and "dpkg -i" in n.value]
verifier("`dpkg -i` n'est plus branché sur un tube",
         commandes and not any("|" in c for c in commandes),
         "`cmd | tail` rend le code de tail : l'échec passerait pour une réussite ; "
         "commandes vues : %r" % (commandes,))
verifier("la troncature des lignes se fait en Python, après lecture du code",
         "lignes[-40:]" in src)

print("5. l'architecture se lit chez dpkg, pas chez uname")
verifier("`dpkg --print-architecture` est la source",
         "dpkg --print-architecture" in src,
         "`uname -m` dit x86_64 là où dpkg dit amd64 — comparer les deux vocabulaires ne matche jamais")
verifier("et `uname -m` n'est pas utilisé pour cette décision",
         not re.search(r"uname -m[^\n]*arch|arch[^\n]*uname -m", src))

print()
if ECHECS:
    print("\033[31m%d échec(s)\033[0m : %s" % (len(ECHECS), "; ".join(ECHECS)))
    sys.exit(1)
print("\033[32mtout vert\033[0m")
