#!/usr/bin/env python3
"""Vérifie qu'`import main` n'installe AUCUN handler sur le journal de production.

Pourquoi cette garde existe (2026-10-24) : le bloc de journalisation de `main.py` était au
niveau module, donc exécuté à l'IMPORT. Or **52 fichiers de `tests/`** font `import main`, plus
les bancs de `tools/bancs/` et des outils de plugin — chacun écrivait ses lignes dans
`/opt/bobistudio/bobistudio.log`. Trouvé par la session Médias : des « planches rapatriées »
revenaient en boucle dans le journal de prod sans qu'aucun job ne tourne ; c'étaient les
fixtures de `tests/verif_media_storyboard.py`, et relancer ce test faisait monter le compteur
du journal de 18 à 20.

Le coût réel n'est pas le bruit, c'est que **le journal cesse d'être une source** : il a fallu
dater un gel de huit jours (pyramide 1153) par l'arithmétique sur l'index MXL, faute de trace
fiable — et la veille de journal prenait ces lignes de test pour la réalité.

Ce que la garde verrouille, des deux côtés :
  (1) `import main` → aucun handler du logger racine ne vise LOG_PATH, et le fichier ne grossit pas ;
  (2) le bloc EXISTE toujours et reste gardé par `if __name__ == "__main__"` — sinon le service,
      qui démarre par `python main.py` (l'ExecStart de bobistudio.service), ne journaliserait plus
      du tout. Une garde qui n'exigerait que (1) serait satisfaite par la suppression du journal.

Aucun réseau. L'import est fait dans un SOUS-PROCESSUS : le configurer ici polluerait ce
processus de test et masquerait le résultat.

    ./venv/bin/python tests/verif_journal_import_main.py
"""
import ast
import json
import os
import subprocess
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

from app.config import LOG_PATH                        # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


# ── (1) ce que fait réellement `import main` ─────────────────────────────────────────
# ⚠ NE PAS comparer la TAILLE du journal avant/après : l'orchestrateur tourne et y écrit en
# permanence (canary, RDMA, sondes…). Mesuré pendant l'écriture de cette garde : +270 octets en
# quelques secondes, sans rapport avec l'import. Une assertion sur la taille serait instable et
# ne passerait que par chance. On cherche donc un MARQUEUR unique : lui seul prouve que c'est
# notre ligne qui a atterri là, et son absence ne dépend pas de ce que fait le service.
MARQUEUR = "sonde-import-main-%d" % os.getpid()
SONDE = r"""
import json, logging, sys
sys.path.insert(0, %r)
import main                                     # ne doit RIEN configurer
root = logging.getLogger()
cibles = []
for h in root.handlers:
    f = getattr(h, "baseFilename", None)
    cibles.append(f or type(h).__name__)
logging.getLogger("sonde").warning(%r)
print(json.dumps({"handlers": cibles}))
""" % (_R, MARQUEUR)

r = subprocess.run([sys.executable, "-c", SONDE], capture_output=True, text=True,
                   cwd=_R, timeout=180)
ok(r.returncode == 0, "`import main` n'échoue pas", (r.stderr or "")[-400:])
data = {}
if r.returncode == 0:
    try:
        data = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        ok(False, "sortie de la sonde lisible", "%s / %r" % (e, (r.stdout or "")[-200:]))

if data:
    vises = [h for h in data["handlers"] if isinstance(h, str) and h and os.path.abspath(h) == os.path.abspath(LOG_PATH)]
    ok(not vises, "aucun handler du logger racine ne vise le journal de production",
       "handlers = %s" % data["handlers"])
    # Et la preuve par le fichier : la ligne WARNING émise après l'import n'y est PAS.
    trouve = False
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            trouve = any(MARQUEUR in ligne for ligne in f)
    ok(not trouve, "la ligne de sonde n'a PAS atterri dans le journal de production",
       "marqueur %r trouvé dans %s" % (MARQUEUR, LOG_PATH))


# ── (2) le bloc existe ET reste gardé — sinon le service ne journalise plus ──────────
src = open(os.path.join(_R, "main.py"), encoding="utf-8").read()
arbre = ast.parse(src)


def _gardes_main(noeud):
    """Les `if __name__ == "__main__":` au niveau module."""
    out = []
    for n in noeud.body:
        if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) and "__name__" in ast.unparse(n.test):
            out.append(n)
    return out


gardes = _gardes_main(arbre)
ok(gardes, "main.py possède au moins un `if __name__ == \"__main__\"`")

# On compte les APPELS (ast.Call), pas les occurrences de texte : chercher la chaîne
# « basicConfig » attraperait aussi un commentaire ou un docstring qui la mentionne.
def _appels(noeud, nom):
    return [n for n in ast.walk(noeud)
            if isinstance(n, ast.Call) and nom in ast.unparse(n.func)]


tous = _appels(arbre, "logging.basicConfig")
sous_garde = [c for g in gardes for c in _appels(g, "logging.basicConfig")]
ok(len(tous) == 1, "un seul appel à logging.basicConfig dans main.py", "trouvés : %d" % len(tous))
ok(len(sous_garde) == 1, "logging.basicConfig est SOUS le garde `__main__`",
   "sous garde : %d / total : %d" % (len(sous_garde), len(tous)))

# Le handler fichier aussi : c'est lui qui écrit dans LOG_PATH.
h_tous = _appels(arbre, "logsetup.make_handler")
h_garde = [c for g in gardes for c in _appels(g, "logsetup.make_handler")]
ok(len(h_tous) == 1 and len(h_garde) == 1,
   "logsetup.make_handler est appelé une fois, SOUS le garde `__main__`",
   "total %d / sous garde %d" % (len(h_tous), len(h_garde)))

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
