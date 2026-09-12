# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Contrôle de syntaxe du JavaScript embarqué dans les gabarits HTML.

**Pourquoi.** Une page de réglages entière est devenue inutilisable — tous les sous-onglets
affichés d'un coup, plus aucune navigation — à cause de DEUX caractères : des backticks encadrant
un nom de champ dans un commentaire HTML, à l'intérieur d'une fonction qui construit son HTML par
littéral de gabarit. Un backtick y FERME le littéral, même dans ce qui ressemble à un commentaire,
et le mot suivant devient du code. Le bloc de script entier cesse alors d'être valide, et avec lui
toutes les fonctions qu'il définit.

Rien ne le signalait : le serveur rend la page sans broncher, c'est le navigateur qui refuse le
script — en silence, sauf à ouvrir la console. On cherche du côté du CSS, du cache, d'un onglet
cassé, jamais d'un commentaire.

**Ce que ce module fait.** Il extrait les blocs `<script>` de chaque gabarit, neutralise le Jinja,
et confie le résultat à `node --check` — le seul juge qui fasse autorité sur du JavaScript. Aucun
analyseur maison : c'est exactement l'outil qui a fini par trouver le défaut.

**Ce qu'il ne fait pas.** Il n'exécute rien et ne juge que la SYNTAXE. Node absent → contrôle sauté
(journalisé), jamais bloquant : un serveur de production ne doit pas dépendre d'un outil de
développement pour démarrer.
"""

import glob
import logging
import os
import re
import subprocess
import tempfile

log = logging.getLogger(__name__)

_RE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", re.S)


def _sans_jinja(js):
    """Remplace les expressions Jinja par un IDENTIFIANT, pour que le JS redevienne analysable.

    Conscient de l'IMBRICATION : `{{ '{{autre_var}}' }}` (du Jinja qui produit littéralement des
    accolades) existe dans les gabarits, et un remplacement non gourmand s'arrêterait au premier
    `}}` en laissant la queue derrière lui — un faux positif qui décrédibiliserait tout le contrôle.

    ⚠ Le substitut est `x` NU, sans guillemets. Il a longtemps été `"x"`, et cette paire de
    guillemets a fait crier ce contrôle à tort pendant des heures, en `error`, sur une page
    parfaitement saine : `mxlToast("{{ _('settings.public.saved') }}", 'success')` devenait
    `mxlToast(""x"", 'success')` — deux chaînes vides collées à un identifiant, donc
    « missing ) after argument list ». Le gabarit était juste, le rendu aussi ; seul le
    neutraliseur était faux.

    Un identifiant nu est valide dans TOUTES les positions où un `{{ }}` peut légitimement se
    trouver, et c'est ce qui le rend sûr : dans une chaîne (`"x"`), en interpolation partielle
    (`"début x fin"`), en expression (`const v = x;`), en argument (`f(x)`), en clé d'objet, après
    un point (`obj.x` — que `obj."x"` cassait), dans un commentaire ou une expression régulière.
    Aucune de ces positions n'exige une CHAÎNE : en JavaScript une chaîne est une expression parmi
    d'autres, jamais une exigence de syntaxe.

    Reste la limite de fond, à connaître : ce contrôle juge le GABARIT, pas la page RENDUE. Il
    attrape ce qui est cassé en toutes circonstances (un backtick égaré) et il est aveugle à ce que
    la VALEUR TRADUITE injectée casse — qui, lui, se voit en rendant la page dans chaque langue.
    Les deux contrôles sont complémentaires ; celui-ci est le moins cher, pas le plus fort.
    """
    out, i, n = [], 0, len(js)
    while i < n:
        if js.startswith("{{", i):
            prof, j = 1, i + 2
            while j < n and prof:
                if js.startswith("{{", j):
                    prof += 1; j += 2
                elif js.startswith("}}", j):
                    prof -= 1; j += 2
                else:
                    j += 1
            out.append("x"); i = j
        elif js.startswith("{%", i) or js.startswith("{#", i):
            fin = "%}" if js.startswith("{%", i) else "#}"
            j = js.find(fin, i)
            i = (j + 2) if j >= 0 else n
        else:
            out.append(js[i]); i += 1
    return "".join(out)


def _node_dispo():
    try:
        return subprocess.run(["node", "--version"], capture_output=True,
                              timeout=5).returncode == 0
    except Exception:
        return False


def _gabarits(dossier):
    """Gabarits à contrôler : ceux du dossier, PLUS les onglets de réglages des services.

    Ces derniers sont `{% include %}`és dans settings.html : leur JS s'exécute donc sur la page
    Réglages exactement comme s'il y était écrit, et un bloc invalide y met la page entière hors
    service — le défaut même que ce module existe pour attraper. Ne balayer que `templates/`
    laissait une bonne moitié du JS de cette page hors contrôle."""
    fichiers = sorted(glob.glob(os.path.join(dossier, "*.html")))
    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.abspath(dossier) == os.path.join(racine, "templates"):
        fichiers += sorted(glob.glob(os.path.join(racine, "services", "*", "*.html")))
    return fichiers


def verifier(dossier=None):
    """Renvoie la liste des problèmes : [(gabarit, ligne_approx, message)]. Vide = tout va bien."""
    dossier = dossier or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "templates")
    if not _node_dispo():
        log.info("contrôle JS des gabarits sauté : node introuvable")
        return []
    soucis = []
    for chemin in _gabarits(dossier):
        src = open(chemin, encoding="utf-8").read()
        for m in _RE_SCRIPT.finditer(src):
            ligne = src[:m.start()].count("\n") + 1
            tmp = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as f:
                    f.write(_sans_jinja(m.group(1))); tmp = f.name
                r = subprocess.run(["node", "--check", tmp], capture_output=True,
                                   text=True, timeout=20)
                if r.returncode:
                    msg = next((l.strip() for l in r.stderr.splitlines() if "Error" in l),
                               "syntaxe invalide")
                    # Nom AVEC son dossier parent : tous les onglets de services s'appellent
                    # `settings_tab.html`, un basename seul ne dirait pas lequel est cassé.
                    soucis.append((os.path.join(os.path.basename(os.path.dirname(chemin)),
                                                os.path.basename(chemin)), ligne, msg))
            except Exception as e:
                log.debug("contrôle JS %s: %s", chemin, e)
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except OSError: pass
    return soucis


def verifier_au_demarrage():
    """Contrôle au boot. Un bloc invalide casse TOUTES les fonctions de son gabarit côté navigateur
    — c'est une panne d'interface complète, donc une alerte `error`, pas une ligne de journal."""
    try:
        soucis = verifier()
    except Exception as e:
        log.debug("contrôle JS des gabarits: %s", e)
        return
    for gabarit, ligne, msg in soucis:
        log.error("JS invalide dans %s (bloc <script> vers la ligne %d) : %s — "
                  "toutes les fonctions de ce gabarit sont HORS SERVICE dans le navigateur",
                  gabarit, ligne, msg)
        try:
            from .database import db_add_alert
            db_add_alert("alert.ui.js_invalide", "error", kind="ui",
                         params={"gabarit": gabarit, "ligne": ligne, "msg": msg})
        except Exception:
            pass
    if not soucis:
        log.info("contrôle JS des gabarits : tout est valide")
