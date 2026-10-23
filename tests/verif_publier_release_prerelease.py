#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de `tools/publier_release.sh` : l'étiquetage d'une release, et le paquet qu'elle joint.
#
# ★ DEUX DÉFAUTS RÉELS, CONSTATÉS LE 2026-10-23 EN PUBLIANT LA 0.9.9.
#
# (1) La release est sortie en « Latest » alors que les huit précédentes étaient des
#     pré-versions titrées « Bobi.Studio 0.9.x — bêta ». Celles-là avaient été étiquetées À LA
#     MAIN dans l'interface GitHub : le script ne passait ni `--prerelease`, ni le suffixe. Ce
#     n'est pas un détail d'affichage. `catalogue._derniere_release()` et la mise à jour depuis
#     GitHub interrogent `releases/latest`, qui EXCLUT les pré-versions — une release mal
#     étiquetée se propose donc d'elle-même en mise à jour sur les instances installées.
#
# (2) La garde « paquet incomplet » ne regardait que `plugins/`. Or le sous-module dont
#     l'absence est FATALE est `services/nmos` : `main.py` fait `from services import nmos`
#     sans garde et meurt sur un ImportError avant d'afficher une page. Un paquet au nmos vide
#     passait donc la garde chargée exactement d'empêcher ça.
#
# ⚠ ON EXÉCUTE LE CODE DU SCRIPT, PAS UNE COPIE. Les deux fragments sont EXTRAITS du fichier
# réel puis lancés — une recopie dans ce banc resterait verte le jour où le script change.
# Aucun accès réseau, aucun `gh`, aucun build : on n'exerce que la décision.
#
#   $ ./venv/bin/python tests/verif_publier_release_prerelease.py
import os
import re
import subprocess
import sys
import tempfile
import zipfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(RACINE, "tools", "publier_release.sh")

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


src = open(SCRIPT, encoding="utf-8").read()

# ═══ 1. LA DÉCISION « PRÉ-VERSION » ════════════════════════════════════════════
# Fragment réel, de la pose de TITRE jusqu'au suffixe inclus.
m = re.search(r'^TITRE="Bobi\.Studio.*?^\[ "\$PRE" = 1 \] && TITRE=.*?$',
              src, re.M | re.S)
controle("★★★ le fragment qui décide de la pré-version est présent dans le script",
         m is not None,
         "ni `TITRE=` ni le `case` sur le majeur : le script ne peut pas étiqueter")

if m:
    frag = m.group(0)

    def decide(tag, definitive=0):
        """Lance le VRAI fragment bash avec ce tag, rend (PRE, TITRE)."""
        prog = 'TAG=%s\nDEFINITIVE=%d\n%s\nprintf "%%s|%%s" "$PRE" "$TITRE"\n' % (
            tag, definitive, frag)
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
        if r.returncode != 0:
            return ("rc=%d" % r.returncode, r.stderr.strip())
        return tuple(r.stdout.split("|", 1))

    controle("★★★ un majeur 0 → pré-version, titre suffixé « — bêta »",
             decide("v0.9.10") == ("1", "Bobi.Studio 0.9.10 — bêta"),
             "obtenu : %r" % (decide("v0.9.10"),))
    controle("★★ le « v » du tag ne se retrouve pas dans le titre",
             "v0" not in decide("v0.9.10")[1],
             "les huit précédentes sont titrées « Bobi.Studio 0.9.8 — bêta », sans v")
    controle("★★★ 1.0.0 → release ordinaire, aucun suffixe",
             decide("v1.0.0") == ("0", "Bobi.Studio 1.0.0"),
             "obtenu : %r" % (decide("v1.0.0"),))
    controle("★★ un majeur à deux chiffres reste ordinaire",
             decide("v10.2.0") == ("0", "Bobi.Studio 10.2.0"),
             "un `case 0.*` naïf attraperait aussi 0… mais pas 10 : on vérifie les deux sens")
    controle("★★★ --definitive sort de la bêta même sur un majeur 0",
             decide("v0.9.10", definitive=1) == ("0", "Bobi.Studio 0.9.10"),
             "obtenu : %r" % (decide("v0.9.10", definitive=1),))
    controle("★ un tag sans « v » se comporte pareil",
             decide("0.9.10") == decide("v0.9.10"),
             "get.sh et le catalogue manipulent les deux formes")

# La décision doit être ACHEMINÉE jusqu'à gh, pas seulement calculée.
controle("★★★ `--prerelease` est effectivement ajouté aux arguments de gh",
         re.search(r'\[ "\$PRE" = 1 \]; then _args\+=\(--prerelease\)', src) is not None,
         "le fragment peut décider juste et le script publier quand même en « Latest »")
controle("★★★ le titre passé à gh est $TITRE, plus « Bobi.Studio $TAG »",
         '--title "$TITRE"' in src and '--title "Bobi.Studio $TAG"' not in src,
         "c'est la ligne exacte qui a produit « Bobi.Studio v0.9.9 »")

# ═══ 2. LA GARDE DU PAQUET VIDE ════════════════════════════════════════════════
# ⚠ `<<'PY'` est suivi de `|| die "…"` SUR LA MÊME LIGNE. Une première version exigeait le
# saut de ligne juste après le délimiteur : elle ne trouvait donc pas cette garde-ci et
# attrapait le heredoc SUIVANT (l'alerte « modules privés »), qu'elle exerçait en croyant
# tester celle-ci — trois contrôles verts sur le mauvais code. D'où `[^\n]*`.
m2 = re.search(r"\./venv/bin/python - \"\$ZIP\" <<'PY'[^\n]*\n(.*?)\nPY\n", src, re.S)
controle("★★★ la garde « paquet incomplet » est présente",
         m2 is not None)

if m2:
    garde = m2.group(1)
    INDISPENSABLES = ("main.py", "install/install.py", "install/install_proxmox.py")

    def paquet(modules):
        """Construit un vrai zip et lui soumet la VRAIE garde. Rend (rc, sortie)."""
        d = tempfile.mkdtemp()
        chemin = os.path.join(d, "p.zip")
        with zipfile.ZipFile(chemin, "w") as z:
            for f in INDISPENSABLES:
                z.writestr(f, "x")
            for mod, peuple in modules.items():
                if peuple:
                    z.writestr("%s/plugin.json" % mod, "{}")
                else:
                    z.writestr("%s/" % mod, "")       # dossier seul = sous-module non initialisé
        g = os.path.join(d, "garde.py")
        open(g, "w", encoding="utf-8").write(garde)
        r = subprocess.run([sys.executable, "-I", g, chemin],
                           capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr)

    rc, out = paquet({"plugins/hello_world": True, "services/nmos": True})
    controle("★★ un paquet complet passe", rc == 0, out.strip())
    controle("★ ...et le compte rendu nomme les DEUX familles",
             rc == 0 and "service" in out,
             "un « 33 plugin(s) peuplé(s) » seul laisse croire que services/ a été vu")

    rc, out = paquet({"plugins/hello_world": True, "services/nmos": False})
    controle("★★★ services/nmos VIDE est refusé",
             rc != 0 and "nmos" in out,
             "c'est le cas qui tue l'orchestrateur au démarrage ; obtenu rc=%d %r" % (rc, out.strip()))

    rc, out = paquet({"plugins/hello_world": False, "services/nmos": True})
    controle("★★ un plugin vide reste refusé (pas de régression)",
             rc != 0 and "hello_world" in out,
             "obtenu rc=%d %r" % (rc, out.strip()))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
