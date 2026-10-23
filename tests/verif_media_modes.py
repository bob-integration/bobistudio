#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde des MODES D'AFFICHAGE de la bibliothèque des médias (static/js/media_library.js) :
liste (colonnes au choix), vignettes, cartes, pellicule, liste + aperçu, et l'option « Grouper par ».

Logique pure exécutée sous node (exportée par `module.exports` quand `window` n'existe pas) :
  1. état relu (disposition du Gestionnaire, préférence du poste) : seul le valide passe, bornes ;
  2. colonnes / champs cochés : retrait, ajout en fin, liste par défaut comme point de départ ;
  3. regroupement : clés par jour / projet / nœud / origine / codec (plusieurs groupes possibles),
     ordre (jours décroissants, '' en dernier), durée totale par groupe (inconnues ignorées) ;
  4. pellicule : 8 cases réparties sur la planche (milieu de chaque huitième), position CSS d'une
     case, planche invalide refusée, instant d'une case (planche, sinon milieu de sa part).
Câblage (source) : getState/setState portent le mode et ses réglages ; sélection Maj / Ctrl+A sur
les DONNÉES affichées (rendu par lots) ; une image de pellicule ouvre Infos à son instant ; la fiche
Infos et l'aperçu partagent le lecteur du proxy ; libellés FR + EN.
"""
import json
import os
import re
import shutil
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(RACINE, "static/js/media_library.js")
ok = fail = 0


def check(nom, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("ok   " + nom)
    else:
        fail += 1
        print("ÉCHEC " + nom + ("  — %s" % (detail,) if detail != "" else ""))


if not shutil.which("node"):
    print("ÉCHEC node absent")
    sys.exit(1)

PROG = r"""
const M = require(process.argv[1]);
const out = {};
// 1. état relu
out.s_ok = M.sanitizeViewState({view:'cards', group:'project', cols:['duration','meta.team','duration','../x', 3], cardF:['keywords'],
                                colW:{duration:12, 'meta.team':5000, 'bad key':100, size:'x'}, cell:9999});
out.s_bad = M.sanitizeViewState({view:'mosaic', group:'week', cols:'duration', cell:'big', colW:[1]});
out.s_null = M.sanitizeViewState(null);
out.s_many = M.sanitizeViewState({cols:Array.from({length:40}, (_, i) => 'c' + String.fromCharCode(97 + i % 26) + 'x'.repeat(i / 26 | 0))}).cols.length;
out.views = M.VIEWS; out.groups = M.GROUPS;
// 2. colonnes cochées
out.t_add = M.toggleField(null, 'keywords', ['duration','format']);
out.t_del = M.toggleField(['duration','format','keywords'], 'format', ['duration']);
out.t_def = M.toggleField(null, 'duration', ['duration','format']);
// 3. regroupement
const a = {id:'x', created_at:'2026-10-12T09:00:00', origin:'recorder', available_nodes:['n1','n2'],
           tech:{video:{codec:'h264'}, audio:[{codec:'aac'}]}};
const au = {id:'y', created_at:'', origin:null, available_nodes:[], tech:{audio:[{codec:'pcm_s24le'}]}};
out.gk = ['day','project','node','origin','codec'].map(b => [M.groupKeys(a, b, {x:[3, 5]}), M.groupKeys(au, b, {})]);
out.gk_none = M.groupKeys(null, 'day');
const items = [{k:0, keys:['2026-10-11'], dur:10}, {k:1, keys:['2026-10-12'], dur:5}, {k:2, keys:[''], dur:3},
               {k:3, keys:['2026-10-12'], dur:NaN}, {k:4, keys:['2026-10-12','2026-10-12'], dur:2}, {k:5, keys:[], dur:1}];
out.g_desc = M.buildGroups(items, true);
out.g_asc = M.buildGroups([{k:0, keys:['b']}, {k:1, keys:['A']}, {k:2, keys:['']}, {k:3, keys:['c', 'A'], dur:4}], false, k => k.toLowerCase());
// 4. pellicule
out.pf = [M.pickFrames(40, 8), M.pickFrames(5, 8), M.pickFrames(0, 8), M.pickFrames(8, 8), M.pickFrames(100)];
out.cell = [M.sbCell({cols:10, rows:4}, 0), M.sbCell({cols:10, rows:4}, 13), M.sbCell({cols:10, rows:4}, 39), M.sbCell({cols:1, rows:1}, 0)];
out.valid = [M.sbValid({n:40, cols:10, rows:4, url:'/api/media/assets/a/storyboard.jpg'}),
             M.sbValid({n:41, cols:10, rows:4, url:'/x.jpg'}), M.sbValid({n:4, cols:2, rows:2, url:'//evil/x.jpg'}),
             M.sbValid({n:4, cols:2, rows:2, url:'https://x/y.jpg'}), M.sbValid(null), M.sbValid({n:0, cols:1, rows:1, url:'/a'})];
out.time = [M.sbTime({n:10, times:[0, 1.5, 3]}, 1, 100), M.sbTime({n:10, times:[]}, 1, 100), M.sbTime({n:10}, 0, 0), M.sbTime({n:4, times:[null, 'x']}, 1, 40)];
console.log(JSON.stringify(out));
"""

r = subprocess.run(["node", "-e", PROG, JS], capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    check("exécution sous node", False, r.stderr[-800:])
    sys.exit(1)
o = json.loads(r.stdout.strip().splitlines()[-1])

# 1. état relu
s = o["s_ok"]
check("état : mode et groupe valides gardés", s.get("view") == "cards" and s.get("group") == "project", s)
check("état : colonnes dédoublonnées, clés invalides retirées", s.get("cols") == ["duration", "meta.team"], s.get("cols"))
check("état : largeurs bornées (40..800), clés et valeurs invalides retirées",
      s.get("colW") == {"duration": 40, "meta.team": 800}, s.get("colW"))
check("état : taille des vignettes bornée", s.get("cell") == 320, s.get("cell"))
check("état : tout invalide → rien", o["s_bad"] == {} and o["s_null"] == {}, (o["s_bad"], o["s_null"]))
check("état : 24 colonnes au plus", o["s_many"] == 24, o["s_many"])
check("modes et groupes proposés", o["views"] == ["table", "grid", "cards", "strip", "preview"]
      and o["groups"] == ["none", "day", "project", "node", "origin", "codec"], (o["views"], o["groups"]))
# 2. colonnes
check("colonne cochée : ajoutée EN FIN de la liste par défaut", o["t_add"] == ["duration", "format", "keywords"], o["t_add"])
check("colonne décochée : retirée", o["t_del"] == ["duration", "keywords"], o["t_del"])
check("colonne par défaut décochée : retirée de la liste par défaut", o["t_def"] == ["format"], o["t_def"])
# 3. regroupement
gk = o["gk"]
check("jour = date d'entrée ; sans date → ''", gk[0] == [["2026-10-12"], [""]], gk[0])
check("projet : TOUS ses projets ; aucun → []", gk[1] == [["3", "5"], []], gk[1])
check("nœud : chaque nœud qui a une copie ; hors ligne → []", gk[2] == [["n1", "n2"], []], gk[2])
check("origine ; inconnue → ''", gk[3] == [["recorder"], [""]], gk[3])
check("codec : vidéo, sinon le premier audio", gk[4] == [["h264"], ["pcm_s24le"]], gk[4])
check("ligne sans média → groupe ''", o["gk_none"] == [""], o["gk_none"])
gd = o["g_desc"]
check("jours : le plus récent d'abord, '' en dernier", [g["key"] for g in gd] == ["2026-10-12", "2026-10-11", ""], [g["key"] for g in gd])
check("ordre d'affichage gardé dans un groupe ; clé répétée = une fois", gd[0]["ks"] == [1, 3, 4], gd[0]["ks"])
check("sans clé → groupe ''", gd[2]["ks"] == [2, 5], gd[2]["ks"])
check("durée totale : somme des durées connues (NaN ignoré)", [g["dur"] for g in gd] == [7, 10, 4], [g["dur"] for g in gd])
ga = o["g_asc"]
check("croissant par clé de tri (casse ignorée), '' en dernier ; un média dans deux groupes",
      [g["key"] for g in ga] == ["A", "b", "c", ""] and ga[0]["ks"] == [1, 3] and ga[2]["ks"] == [3], ga)
# 4. pellicule
pf = o["pf"]
check("8 cases sur 40 : le milieu de chaque huitième", pf[0] == [2, 7, 12, 17, 22, 27, 32, 37], pf[0])
check("planche de moins de 8 : toutes ses cases", pf[1] == [0, 1, 2, 3, 4], pf[1])
check("planche vide : aucune case ; 8 sur 8 : toutes", pf[2] == [] and pf[3] == list(range(8)), (pf[2], pf[3]))
check("8 par défaut, croissantes et distinctes", len(pf[4]) == 8 and pf[4] == sorted(set(pf[4])), pf[4])
c = o["cell"]
check("case 0 : coin haut-gauche ; taille = c×100 % / r×100 %", c[0] == {"size": [1000, 400], "pos": [0, 0]}, c[0])
check("case 13 (ligne 2, colonne 4)", all(abs(x - y) < 1e-9 for x, y in zip(c[1]["pos"], [100 * 3 / 9, 100 / 3])), c[1])
check("dernière case : coin bas-droit", c[2]["pos"] == [100, 100], c[2])
check("planche d'une case : position 0", c[3] == {"size": [100, 100], "pos": [0, 0]}, c[3])
check("planche valide acceptée ; trop de cases, URL externe, //hôte, vide refusées",
      o["valid"] == [True, False, False, False, False, False], o["valid"])
check("instant : celui de la planche, sinon milieu de sa part de durée, jamais négatif",
      o["time"] == [1.5, 15, 0, 15], o["time"])

# ── Câblage (source) ──
src = open(JS, encoding="utf-8").read()
gs = src[src.index("function getState()"):src.index("async function setState(")]
check("getState porte le mode et ses réglages", all(x in gs for x in ("view", "group:vs.group", "cell:vs.cell", "out.cols", "out.colW", "out.cardF")))
check("getState lâche les largeurs avant la recherche avancée quand c'est trop gros",
      gs.index("delete out.colW") < gs.index("out.tree = null"))
ss = src[src.index("async function setState("):src.index("$('.ml-flt-save').onclick")]
check("setState valide par sanitizeViewState", "sanitizeViewState(s)" in ss and "applyViewState(" in ss)
check("Ctrl+A et Maj : sur l'ordre AFFICHÉ (st.order), pas sur le DOM",
      "function selectAll(){ st.order.forEach" in src and "const keys = st.order;" in src)
check("rendu par lots (IntersectionObserver) et plafond = celui du serveur",
      "new IntersectionObserver" in src and re.search(r"const LIB_MAX = 2000;", src) is not None)
mc = src[src.index("main.onclick = e => {"):src.index("main.ondblclick")]
check("image de pellicule : Infos à son instant (_lastScrub puis openInfo)",
      re.search(r"_lastScrub = \{aid:fit\.dataset\.aid, t:parseFloat\(fr\.dataset\.sbt\)", mc) is not None and "openInfo(fit.dataset.aid)" in mc)
check("clic sur un en-tête de groupe = sélection du groupe", "selectGroup(+gh.dataset.grp, e)" in mc)
check("fiche Infos et aperçu : le même lecteur du proxy (mediaBlock)",
      src.count("mediaBlock(a, at)") >= 1 and "mediaBlock(a, null)" in src)
check("aperçu : ↑ ↓ et Espace", "pvMove(e.key === 'ArrowDown' ? 1 : -1)" in src and "pvToggle()" in src)
check("sélecteur : aria-pressed tenu à jour", "setAttribute('aria-pressed', String(on))" in src)
check("en-tête de la liste : clic droit = choix des colonnes", "openFieldsMenu('cols', null" in src)
check("pellicule : planche lue par /storyboard.json, seulement si has_storyboard", "/storyboard.json" in src and "a.has_storyboard" in src)
check("survol-défilement : intact (SCRUB_ZOOM)", "const SCRUB_ZOOM = 1.8;" in src)

# Libellés : chaque mode, groupe et clé nouvelle existe en FR et en EN.
fr = json.load(open(os.path.join(RACINE, "i18n/fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(RACINE, "i18n/en.json"), encoding="utf-8"))
need = ["medialib.view." + v for v in o["views"]] + ["medialib.group." + g for g in o["groups"]]
need += ["medialib.group_empty." + g for g in o["groups"] if g != "none"]
need += ["medialib." + k for k in re.findall(r"(?<![A-Za-z])L\('([a-z_]+(?:\.[a-z_]+)?)'", src) if not k.endswith("_")]
manque = sorted({k for k in need if k not in fr or k not in en})
check("libellés FR + EN présents", not manque, manque)

print("%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
