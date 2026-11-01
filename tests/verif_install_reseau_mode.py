#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""L'installeur tout-en-un demande le MODE du réseau des conteneurs, et l'applique vraiment.

Pourquoi ce banc, et pourquoi il reprend les gardes de `verif_install_agent_local` :

`appliquer_reseau_conteneurs()` lance un processus enfant qui importe `app`. C'est EXACTEMENT
la famille de défauts du 2026-10-23 — du code qui suppose son répertoire courant. Lancé depuis
le dossier temporaire où `get.sh` déroule, l'enfant meurt en `ModuleNotFoundError`, et comme on
ne juge que sur un code de sortie, l'installation continue en annonçant autre chose que ce qui
s'est passé. Les deux gardes qui comptent sont donc : `cwd=APP_DIR`, et le mode transmis par
l'ENVIRONNEMENT (un mode interpolé dans du source est la porte ouverte du défaut (d) du jeton).

Et la question elle-même : elle ne doit être posée QUE si le nœud local est enrôlé — sans ligne
en base, la réponse n'a nulle part où aller, et on aurait demandé pour rien à quelqu'un dont
l'installation est déjà incomplète.

Hors ligne : aucun sous-processus n'est réellement lancé, `subprocess.run` est doublé.
"""
import ast
import importlib.util
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "install"))

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


spec = importlib.util.spec_from_file_location(
    "bobi_install_sous_test", os.path.join(RACINE, "install", "install.py"))
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


class FauxRun:
    """Doublure de subprocess.run : note l'appel, rend ce qu'on lui dit."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.appels = []

    def __call__(self, argv, **kw):
        self.appels.append({"argv": argv, "cwd": kw.get("cwd"), "env": kw.get("env") or {}})
        return self


print("\n1. L'enfant tourne là où `app` s'importe, et reçoit le mode par l'environnement")
faux = FauxRun(0, stdout=json.dumps({"name": "bobi-interne", "subnet": "172.30.0.0/24"}))
inst.subprocess.run = faux
ok_r, detail = inst.appliquer_reseau_conteneurs("interne")

verifie(bool(faux.appels), "1a. un processus enfant est bien lancé")
a = faux.appels[0] if faux.appels else {}
verifie(a.get("cwd") == inst.APP_DIR,
        "1a bis. ★ `cwd=APP_DIR` — sans lui l'enfant meurt en ModuleNotFoundError depuis le "
        "dossier temporaire de get.sh (la panne du 23/10, même famille)", str(a.get("cwd")))
verifie((a.get("env") or {}).get("BOBI_NET_MODE") == "interne",
        "1b. ★ le mode passe par l'ENVIRONNEMENT", str((a.get("env") or {}).get("BOBI_NET_MODE")))
source_enfant = a.get("argv", ["", "", ""])[2] if len(a.get("argv") or []) > 2 else ""
verifie("'interne'" not in source_enfant and '"interne"' not in source_enfant,
        "1b bis. ★ et il n'est PAS interpolé dans le source de l'enfant (défaut (d) du jeton)",
        source_enfant[:200])
verifie(ok_r is True and detail.get("subnet") == "172.30.0.0/24",
        "1c. le détail rendu par l'enfant remonte à l'installeur (pour l'afficher)", str(detail))


print("\n2. L'enfant juge sur le RÉSULTAT, pas sur « le script s'est exécuté »")
arbre = ast.parse(source_enfant)
texte = source_enfant
verifie("code == 200" in texte,
        "2a. ★ il ne sort 0 que si l'application du mode a répondu 200", texte[-200:])
verifie("db_get_node_by_host" in texte and "sys.exit(1)" in texte,
        "2b. il refuse quand aucun nœud local n'est en base, au lieu d'écrire dans le vide")
verifie(isinstance(arbre, ast.Module) and len(arbre.body) > 3,
        "2c. le source de l'enfant est du Python valide (il est bâti à la main)")


print("\n3. Un échec est rapporté avec SA raison")
faux2 = FauxRun(1, stdout=json.dumps({"error": "subnet interne « zz » invalide"}))
inst.subprocess.run = faux2
ok_r, detail = inst.appliquer_reseau_conteneurs("interne")
verifie(ok_r is False, "3a. un enfant en échec n'est pas pris pour un succès")
verifie("zz" in str(detail),
        "3b. ★ et c'est LA raison de l'enfant qui remonte, pas un message générique — sinon "
        "l'exploitant n'a rien à corriger", str(detail))

faux3 = FauxRun(1, stderr="Traceback...\nRuntimeError: agent injoignable")
inst.subprocess.run = faux3
ok_r, detail = inst.appliquer_reseau_conteneurs("interne")
verifie(ok_r is False and "agent injoignable" in str(detail),
        "3c. sans charge JSON, la dernière ligne d'erreur sert de raison", str(detail))

faux4 = FauxRun(0, stdout="")
inst.subprocess.run = faux4
ok_r, _ = inst.appliquer_reseau_conteneurs("site")
verifie(ok_r is True and faux4.appels[0]["env"].get("BOBI_NET_MODE") == "site",
        "3d. le mode site passe par le même chemin (il pose la colonne, sans créer de réseau)")


print("\n4. La question : posée au bon moment, avec le bon défaut")
src_aio = ast.get_source_segment(
    open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read(),
    next(n for n in ast.parse(open(os.path.join(RACINE, "install", "install.py"),
                                   encoding="utf-8").read()).body
         if isinstance(n, ast.FunctionDef) and n.name == "path_all_in_one")) or ""
verifie("net_title" in src_aio and "appliquer_reseau_conteneurs" in src_aio,
        "4a. le chemin tout-en-un demande le mode et l'applique")
verifie(src_aio.index("register_local_agent") < src_aio.index("net_title"),
        "4b. la question vient APRÈS l'enrôlement (sans ligne de nœud, elle n'a aucun support)")
verifie("if noeud_ok:" in src_aio,
        "4c. ★ et PAS DU TOUT si l'enrôlement a échoué — on ne demande pas pour rien à "
        "quelqu'un dont l'installation est déjà incomplète")
i_int = src_aio.find('("interne"')
i_site = src_aio.find('("site"')
verifie(0 <= i_int < i_site and "default=0" in src_aio,
        "4d. « interne » est proposé EN PREMIER et par défaut (recommandé sur un tout-en-un)",
        "%d / %d" % (i_int, i_site))
verifie("allow_back=False" in src_aio,
        "4e. pas de retour en arrière à cette étape : le nœud est déjà enrôlé derrière")


print("\n5. Les deux langues, sinon l'anglais affiche des clés")
cles = ["net_title", "net_interne_l", "net_interne_d", "net_site_l", "net_site_d",
        "net_interne_ok", "net_interne_bad", "net_site_todo"]
for lang in ("fr", "en"):
    manquantes = [k for k in cles if k not in inst.STR.get(lang, {})]
    verifie(not manquantes, "5%s. toutes les clés existent en « %s »"
            % ("a" if lang == "fr" else "b", lang), str(manquantes))
verifie(all("{name}" in inst.STR[l]["net_interne_ok"] and "{subnet}" in inst.STR[l]["net_interne_ok"]
            for l in ("fr", "en")),
        "5c. le message de succès nomme le réseau ET son subnet dans les deux langues")
verifie(all("{err}" in inst.STR[l]["net_interne_bad"] for l in ("fr", "en")),
        "5d. le message d'échec porte la raison dans les deux langues")


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ installeur : le mode réseau est demandé, transmis proprement, et appliqué pour de vrai")
sys.exit(0)
