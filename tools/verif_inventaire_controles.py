#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Équivalence STRICTE entre l'ancien balayage (une recherche par classe et par fichier) et le
nouveau (une passe). Un gain de vitesse qui change le résultat n'est pas un gain."""
import sys, glob, os, re, time
sys.path.insert(0, '/opt/bobistudio')
from app.routes import pages

racine = '/opt/bobistudio'
src = re.sub(r"/\*.*?\*/", "", open(os.path.join(racine,'static/css/controls.css'),encoding='utf-8').read(), flags=re.S)
classes = sorted(set(re.findall(r"\.(ctl-[a-z0-9-]+)", src)))

def ancien(classes):
    adoption = {c: [] for c in classes}
    for css in sorted(glob.glob(os.path.join(racine, "plugins", "*", "control.css"))):
        plug = os.path.basename(os.path.dirname(css))
        usage = ""
        for autre in ("control.html", "control.js"):
            chem = os.path.join(os.path.dirname(css), autre)
            if os.path.exists(chem):
                usage += open(chem, encoding="utf-8", errors="ignore").read()
        for c in classes:
            if re.search(r"[\"'\s]%s[\"'\s]" % re.escape(c), usage):
                adoption[c].append(plug)
    for js in sorted(glob.glob(os.path.join(racine, "static", "*.js"))):
        nom = os.path.basename(js)
        s = open(js, encoding="utf-8", errors="ignore").read()
        for c in classes:
            if re.search(r"[\"'\s]%s[\"'\s]" % re.escape(c), s):
                adoption[c].append(nom)
    for c in adoption: adoption[c].sort()
    return adoption

t0=time.time(); a = ancien(classes); t_ancien=time.time()-t0
t0=time.time(); n, _orph = pages._adoption_controles(classes); t_neuf=time.time()-t0
for c in n: n[c].sort()

print('classes : %d | ancien %.2f s | nouveau %.3f s | gain ×%.0f' % (len(classes), t_ancien, t_neuf, t_ancien/max(t_neuf,1e-6)))
ecarts = {c: (a[c], n[c]) for c in classes if a[c] != n[c]}
print('classes dont l\'adoption DIFFÈRE :', len(ecarts))
for c,(x,y) in list(ecarts.items())[:5]: print('   %-22s ancien=%s neuf=%s' % (c,x,y))
assert not ecarts, "le résultat a changé"
print('\n✓ résultat identique, à la classe et au consommateur près')
