#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'onglet Réglages → NMOS : la section « Surfaces avancées ».
#
# CE QU'IL ATTRAPE. Le dépôt documente déjà la panne : « une clé absente de DEFAULTS était JETÉE
# EN SILENCE, la route renvoyant quand même 200/ok — un champ ajouté à l'UI sans sa valeur par
# défaut semblait donc s'enregistrer sans jamais rien changer ». Ce banc relie les trois faces qui
# doivent rester d'accord :
#
#   le GABARIT (les `id` des contrôles) ── le JS (les clés qu'il poste) ── le MANIFESTE (ce que
#   `update_bulk` accepte) ── l'i18n (les libellés, dans les DEUX langues)
#
# Une divergence entre deux de ces quatre ne casse rien visiblement : la case se coche, le toast
# dit « enregistré », et rien ne se passe.
#
#   $ ./venv/bin/python tools/verif_nmos_reglages_ui.py
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

GABARIT = os.path.join(RACINE, "services", "nmos", "settings_tab.html")
echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


print("Réglages → NMOS : surfaces avancées\n")
html = open(GABARIT, encoding="utf-8").read()

# ── 1. Les clés que le JS poste ──────────────────────────────────────────────
def _liste(nom):
    m = re.search(r"const %s = \[(.*?)\];" % nom, html, re.S)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


bools, nums = _liste("_AVANCES_BOOL"), _liste("_AVANCES_NUM")
cles = bools + nums
controle("le JS déclare bien les listes de réglages", bool(bools) and bool(nums),
         "sans elles, appliquerNmosAvance() posterait un objet vide")

# ── 2. Chaque clé a son contrôle dans le gabarit ─────────────────────────────
manquants = [k for k in cles if ('id="s_%s"' % k) not in html]
controle("★ chaque clé postée a son contrôle dans le gabarit", not manquants,
         "le JS lirait `null.checked` et l'application entière planterait — %s" % manquants)

# ── 3. Chaque clé est acceptée par la route générique ────────────────────────
from app import settings as st                                      # noqa: E402

_accepte = {**st.DEFAULTS, **st._get_core_defaults()}
refusees = [k for k in cles if k not in _accepte]
controle("★★ chaque clé est ACCEPTÉE par update_bulk (manifeste)", not refusees,
         "une clé absente est jetée en silence et la route répond quand même ok : la case se "
         "coche, le toast dit « enregistré », et rien ne change — %s" % refusees)

# ── 4. Les booléens sont des toggles, les nombres des steppers ───────────────
mal_typees = [k for k in bools
              if not re.search(r'id="s_%s"[^>]*class="ios-toggle"|class="ios-toggle"[^>]*id="s_%s"'
                               % (k, k), html)]
controle("les booléens utilisent le toggle du catalogue", not mal_typees,
         "on puise dans le catalogue de contrôles, on n'en invente pas — %s" % mal_typees)
mal_num = [k for k in nums if not re.search(r'id="s_%s"[^>]*class="num-stepper"' % k, html)]
controle("les entiers utilisent le stepper numérique", not mal_num, "%s" % mal_num)

# ── 5. Les bornes du gabarit ne contredisent pas le code ─────────────────────
_bornes = dict(re.findall(r'id="s_(\w+)"[^>]*min="(\d+)"', html))
controle("le ramasse-miettes ne peut pas descendre sous le plancher du code",
         int(_bornes.get("nmos_registre_gc_s", 0)) >= 4,
         "le code borne à 4 s (deux battements) : proposer moins dans l'UI ferait saisir une "
         "valeur silencieusement remontée")

# ── 6. i18n : toutes les clés, dans les DEUX langues ─────────────────────────
utilisees = set(re.findall(r"_\('(service\.nmos\.[^']+)'\)", html))
utilisees |= set(re.findall(r"_tnmos\('(service\.nmos\.[^']+)'\)", html))
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "services", "nmos", "i18n", "%s.json" % lang)))
    manque = sorted(utilisees - set(cat))
    controle("i18n %s : aucune clé manquante" % lang, not manque,
             "une clé absente s'affiche BRUTE à l'écran — %s" % manque[:6])

_fr = json.load(open(os.path.join(RACINE, "services", "nmos", "i18n", "fr.json")))
_en = json.load(open(os.path.join(RACINE, "services", "nmos", "i18n", "en.json")))
controle("les deux catalogues portent les mêmes clés", set(_fr) == set(_en),
         "un écart = une langue qui affiche des clés brutes — fr seul : %s ; en seul : %s"
         % (sorted(set(_fr) - set(_en))[:4], sorted(set(_en) - set(_fr))[:4]))

# ── 7. Ce que la section ENGAGE est dit, pas seulement ce qu'elle fait ───────
metas = [k for k in utilisees if ".meta_" in k]
controle("★ chaque groupe porte une ligne `meta` qui dit ce qu'il engage",
         len(metas) >= 5,
         "un libellé dit ce qu'un réglage FAIT ; il faut aussi dire ce qu'il COÛTE, sinon "
         "l'exploitant coche sans savoir — %d trouvée(s)" % len(metas))

# ══════════════════════════════════════════════════════════════════════════════════════════════
# 8. La PROMOTION de NMOS en groupe : cinq sous-onglets, et aucune garde restée sur l'ancien id
# ══════════════════════════════════════════════════════════════════════════════════════════════
# Déplacer un panneau casse en silence tout ce qui le désignait par son ancien id. Ici trois
# gardes de rafraîchissement le faisaient — dont UNE vit dans le gabarit du service, invisible
# depuis settings.html. Et la formule employée, `(el || {}).style?.display !== 'none'`, échoue
# du MAUVAIS côté : un id disparu rend la condition VRAIE, donc l'onglet fermé interroge le
# serveur en boucle au lieu de se taire.
SETTINGS = os.path.join(RACINE, "templates", "settings.html")
reglages = open(SETTINGS, encoding="utf-8").read()
# ★ LA LISTE SE DÉRIVE DU GABARIT, ELLE NE S'ÉCRIT PAS À LA MAIN.
# Elle a été figée à cinq entrées pendant des mois. Quand `tally` est arrivé, personne ne l'a
# ajoutée ici : les contrôles d'i18n, de crochet de rafraîchissement et d'aiguillage
# `switchSubTab` ont donc CESSÉ de le couvrir, en silence, tout en restant verts. Et le
# contrôle d'arbre, lui, comparait à un `== 5` littéral : il est devenu rouge pour la seule
# raison qu'un sixième panneau existait — un échec qui ne désignait aucun défaut.
# En dérivant, un onglet neuf est vérifié le jour où il est écrit, sans rien toucher ici.
SOUS = re.findall(r'id="nmos-tab-([\w-]+)"', html)
assert SOUS, "aucun panneau `nmos-tab-<id>` dans le gabarit — le format a changé"

controle("le panneau est autonome (`set-tab-nmos`), plus un sous-onglet de Protocoles",
         'id="set-tab-nmos"' in html and "protocoles-tab-nmos" not in html.replace(
             "// ", "\n// ").split("// ")[0],
         "le manifeste ne doit plus porter `tab_group`")

manifeste = json.load(open(os.path.join(RACINE, "services", "nmos", "manifest.json")))
controle("le manifeste déclare `nav_tab: nmos` et AUCUN `tab_group`",
         manifeste.get("nav_tab") == "nmos" and "tab_group" not in manifeste,
         "avec `tab_group`, core_plugins.py range le service en sous-onglet et l'id redevient "
         "`<groupe>-tab-<id>` — obtenu %r / %r"
         % (manifeste.get("nav_tab"), manifeste.get("tab_group")))

absents = [s for s in SOUS if 'id="nmos-tab-%s"' % s not in html
           or 'id="subtab-btn-nmos-%s"' % s not in html]
controle("★ les %d sous-onglets suivent la convention `nmos-tab-<id>`" % len(SOUS),
         not absents,
         "c'est CETTE convention que switchSubTab attend, et elle seule qui met `#nmos/<id>` "
         "dans l'adresse : un id qui s'en écarte s'affiche mais aucun lien ne le rouvre — %s"
         % absents)

# ── ★★★ L'ARBRE. Le contrôle qui manquait, et qui aurait attrapé DEUX défauts ───────────────
# Chercher `id="nmos-tab-x"` dans le texte prouve que le panneau EXISTE, pas qu'il est au bon
# endroit. Vécu deux fois : (1) la carte « Surfaces avancées » ajoutée après la fermeture du
# panneau se retrouvait HORS de l'onglet ; (2) au découpage, ce `</div>` orphelin a fermé
# `set-tab-nmos` par erreur, mettant Contrôle, Bus MXL et Installation dehors — trois onglets qui
# s'affichaient vides, parce que switchSubTab ne cherche QUE dans son conteneur. Les treize
# contrôles étaient bien dans la page : tous les contrôles textuels restaient verts.
def _arbre(txt):
    """[(id, écart de profondeur au conteneur)] + profondeur finale (0 = arbre équilibré)."""
    t = re.sub(r"<!--.*?-->", "",
               re.sub(r"<script\b.*?</script>", "", txt, flags=re.S), flags=re.S)
    prof, base, vus = 0, None, []
    for m in re.finditer(r"<div\b[^>]*>|</div>", t):
        if m.group(0).startswith("</"):
            prof -= 1
            continue
        idm = re.search(r'id="([\w-]+)"', m.group(0))
        i = idm.group(1) if idm else None
        if i == "set-tab-nmos":
            base = prof
        elif i and i.startswith("nmos-tab-"):
            vus.append((i, None if base is None else prof - base - 1))
        prof += 1
    return vus, prof


_vus, _prof = _arbre(html)
controle("★★★ l'arbre du gabarit est équilibré", _prof == 0,
         "un <div> de trop ou de moins déplace tout ce qui suit : profondeur finale %d" % _prof)
_mal = [i for i, d in _vus if d != 0]
controle("★★★ les %d panneaux sont ENFANTS DIRECTS de `set-tab-nmos`" % len(SOUS),
         len(_vus) == len(SOUS) and not _mal,
         "switchSubTab ne cherche QUE dans son conteneur : un panneau sorti ou imbriqué "
         "s'affiche VIDE sans qu'aucun contrôle textuel ne bronche — %s (vus : %d)"
         % (_mal, len(_vus)))

controle("le groupe NMOS existe dans la barre du haut",
         "set-group-btn-nmos" in reglages and "{id: 'nmos'," in reglages)
controle("★ et `nmos` a été RETIRÉ de la liste du groupe Protocoles",
         "'alertes', 'rdma', 'nmos'" in reglages,
         "sinon l'onglet apparaît dans les deux groupes, et tabGroupOf() rend le premier trouvé")

# ── Les gardes : plus aucune ne vise l'ancien id, et toutes exigent l'ÉLÉMENT ────────────────
def _gardes(txt):
    """Lignes de code (hors commentaires) citant un id d'onglet NMOS."""
    return [l for l in txt.split("\n")
            if ("protocoles-tab-nmos" in l or "set-tab-nmos" in l or "nmos-tab-" in l)
            and not l.strip().startswith(("//", "<!--", "*", "#"))]

perimees = [l.strip()[:70] for l in _gardes(reglages) + _gardes(html)
            if "protocoles-tab-nmos" in l]
controle("★★ aucune garde ne vise encore `protocoles-tab-nmos`", not perimees,
         "une garde sur un id disparu ne se tait pas : elle interroge le serveur en permanence, "
         "onglet fermé — %s" % perimees)

# ⚠ FENÊTRE, PAS LIGNE. Première version de ce contrôle : elle ne cherchait la formule molle que
# sur les lignes qui nomment un id — et une garde s'écrit sur DEUX lignes, l'id sur la première et
# le test sur la suivante. La mutation « garde qui accepte un élément absent » passait au travers.
# Trouvé en mutant ; sans cette mutation le banc restait vert et ne protégeait rien.
def _molles(txt):
    lignes = txt.split("\n")
    interesse = set()
    for i, l in enumerate(lignes):
        if ("protocoles-tab-nmos" in l or "set-tab-nmos" in l or "nmos-tab-" in l) \
           and not l.strip().startswith(("//", "<!--", "*", "#")):
            interesse.update(range(max(0, i - 2), min(len(lignes), i + 3)))
    return [lignes[i].strip()[:70] for i in sorted(interesse)
            if ("|| {}" in lignes[i] or "?.style?." in lignes[i])
            and not lignes[i].strip().startswith(("//", "<!--", "*", "#"))]

molles = _molles(reglages) + _molles(html)
controle("★★ aucune garde n'accepte un élément ABSENT", not molles,
         "`(el || {}).style?.display !== 'none'` vaut VRAI quand l'id n'existe pas : le jour où "
         "un panneau est renommé, le sondage part en boucle au lieu de s'arrêter — %s" % molles)

controle("★ les liens `#protocoles/nmos` déjà partagés résolvent encore",
         "onglet === 'protocoles' && sous === 'nmos'" in reglages,
         "sans ce repli ils retomberaient sur Ember+ sans un mot")

controle("chaque sous-onglet a son crochet de rafraîchissement",
         all(("name === '%s'" % s) in reglages for s in SOUS),
         "un sous-onglet sans crochet s'ouvre sur des tableaux vides jusqu'au prochain cycle")

# ── i18n des libellés de sous-onglets (catalogue du CŒUR, pas du service) ────────────────────
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lang)))
    manque = [k for k in ["settings.group.nmos"]
              + ["settings.nmos.sub.%s" % s for s in SOUS] if k not in cat]
    controle("i18n %s : le groupe et les %d sous-onglets sont traduits" % (lang, len(SOUS)), not manque,
             "ces clés-là vivent dans le catalogue du CŒUR, pas dans celui du service — %s"
             % manque)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
