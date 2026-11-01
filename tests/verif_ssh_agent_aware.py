#!/usr/bin/env python3
"""Garde : un appel hôte SANS stdin binaire doit passer par `host_ops.ssh_run` (agent-aware).

POURQUOI. `host_ops.ssh_run` est le choke-point : il résout l'hôte en ligne de `nodes`, et si ce
nœud a un `agent_url` il exécute via l'agent (jeton HTTP, /v1/host/exec) au lieu d'un root-SSH.
`_ssh_bin` / `_ssh_stream` (routes/images.py) sont des root-SSH BRUTS. Ils existent pour une seule
raison, écrite dans leur docstring : pousser un tar sur stdin, ce que `ssh_run` (text=True) ne sait
pas faire. Employés SANS stdin binaire, ils n'ont aucun avantage et un défaut grave : sur un nœud
enrôlé sans clé root SSH — le cas normal depuis que l'agent existe — ils échouent, et comme les
appelants avalent l'échec, le verdict rendu est « la chose n'existe pas » au lieu de « je n'ai pas
pu regarder ».

Vécu (Z8, 2026-10-28, recette custom-216e388666) : « Détecter GPU » laissait le champ à « — » alors
que la page Pilote NVIDIA voyait la Quadro RTX 4000 et la jugeait conforme. Même motif sur la
présence d'image et sur deux routes du réseau des conteneurs.

L'INVARIANT testé est la raison d'être de ces helpers, pas une liste de fichiers : tout appel à
`_ssh_bin`/`_ssh_stream` doit passer un stdin binaire (3e positionnel ou `input_bytes=`). Un
nouveau site qui ne le fait pas échoue ici, qu'il soit dans un fichier déjà connu ou non.

DETTE_CONNUE recense les sites non corrigés au moment où cette garde est posée, pour qu'elle soit
verte sans masquer les nouveaux. Chaque ligne retirée de cette liste est un défaut réparé ; la
liste doit finir vide. Elle ne dispense PAS de corriger : elle empêche la régression pendant que
d'autres sessions corrigent les leurs.
"""
import ast
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
HELPERS = ("_ssh_bin", "_ssh_stream")

# Sites root-SSH sans stdin binaire connus au 2026-10-28, répartis entre deux autres sessions.
# (fichier, fonction englobante) — PAS le numéro de ligne : il bouge à chaque édition du fichier,
# et une garde qui se périme au premier reformatage ne garde rien.
DETTE_CONNUE = {
    ("app/routes/images.py", "_image_present"),     # présence d'image — session Tests globaux
}


def _fonction_englobante(arbre, lineno):
    """Nom de la fonction (ou méthode) qui contient cette ligne. '<module>' si aucune."""
    best, best_lineno = "<module>", -1
    for n in ast.walk(arbre):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fin = max((getattr(d, "end_lineno", n.end_lineno) or n.end_lineno)
                      for d in [n])
            if n.lineno <= lineno <= (fin or n.lineno) and n.lineno > best_lineno:
                best, best_lineno = n.name, n.lineno
    return best


def _nom_appele(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def main():
    echecs, dette_vue, total = [], set(), 0
    for chemin in sorted((RACINE / "app").rglob("*.py")):
        rel = chemin.relative_to(RACINE).as_posix()
        try:
            arbre = ast.parse(chemin.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as e:
            echecs.append("%s : illisible (%s)" % (rel, e))
            continue
        for n in ast.walk(arbre):
            if not isinstance(n, ast.Call):
                continue
            nom = _nom_appele(n)
            if nom not in HELPERS:
                continue
            total += 1
            kw = {k.arg for k in n.keywords}
            # Signature : (host, cmd, input_bytes=None, timeout=...) → stdin binaire = 3e
            # positionnel ou mot-clé explicite.
            if len(n.args) >= 3 or "input_bytes" in kw:
                continue
            fn = _fonction_englobante(arbre, n.lineno)
            if (rel, fn) in DETTE_CONNUE:
                dette_vue.add((rel, fn))
                continue
            echecs.append(
                "%s:%d (%s) : %s() sans stdin binaire — employer host_ops.ssh_run, qui route vers "
                "l'agent du nœud ; sinon un nœud enrôlé sans clé root SSH rend « absent » au lieu "
                "de « pas pu regarder »" % (rel, n.lineno, fn, nom))

    # Une dette RÉPARÉE doit sortir de la liste, sinon celle-ci devient un cimetière qui autorise
    # silencieusement une régression au même endroit.
    for ref in sorted(DETTE_CONNUE - dette_vue):
        echecs.append(
            "%s (%s) : plus aucun appel root-SSH sans stdin binaire ici — retirer cette entrée de "
            "DETTE_CONNUE (sinon elle réautoriserait le défaut)" % ref)

    print("%d appel(s) root-SSH examiné(s) ; %d dette(s) connue(s) encore présente(s) sur %d"
          % (total, len(dette_vue), len(DETTE_CONNUE)))
    if echecs:
        print("ÉCHEC :")
        for e in echecs:
            print("  - %s" % e)
        return 1
    print("OK — tout appel hôte sans stdin binaire passe par ssh_run (agent-aware)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
