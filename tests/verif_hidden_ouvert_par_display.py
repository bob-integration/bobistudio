#!/usr/bin/env python3
"""Un élément né `hidden` ne s'ouvre pas par `style.display` (2026-10-28).

Le testeur (Recette, Z8) : le bouton « Configurer » d'une carte conteneur n'ouvrait plus rien, sur toutes
les instances. L'aside `#deploy-palette` naît `hidden`, et renderDeployPalette ne posait que
`style.display = 'block'` : or `[hidden]{display:none !important}` (static/css/base.css, a50ea4a,
30/08) l'emporte sur tout style en ligne. La palette était invisible depuis deux mois.

Pour chaque élément à id né avec l'attribut `hidden` (gabarits, JS, plugins, services), on cherche
le code qui lui pose un `style.display` autre que `none` — directement ou par une variable qui le
porte. Si aucun site ne lève l'attribut (`.hidden =`, `removeAttribute('hidden')`,
`toggleAttribute('hidden')`), l'élément ne peut pas s'afficher : échec.
"""
import glob, os, re, sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATS = ("templates/**/*.html", "static/**/*.js", "plugins/*/*.html", "plugins/*/*.js",
        "services/*/*.html", "services/*/*.js")
ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


src = {}
for p in PATS:
    for f in glob.glob(os.path.join(_R, p), recursive=True):
        src[os.path.relpath(f, _R)] = open(f, encoding="utf-8", errors="replace").read()

TAG = re.compile(r'<[a-zA-Z][^<>]*?\sid=["\']([A-Za-z][\w-]*)["\'][^<>]*>', re.S)
born = {}
for f, s in src.items():
    for m in TAG.finditer(s):
        if re.search(r'\shidden(\s|>|/|=)', m.group(0)):
            born.setdefault(m.group(1), f)

GET = r"(?:document\.getElementById|\$|byId|el|qs|document\.querySelector)\(\s*['\"]#?%s['\"]\s*\)"
SHOW = r"\.style\.display\s*=\s*(?!\s|['\"]none['\"])"
UNHIDE = r"\.(?:hidden\s*=|removeAttribute\(\s*['\"]hidden|toggleAttribute\(\s*['\"]hidden)"


def scan(i):
    shows, unhides = [], False
    g = GET % re.escape(i)
    for f, s in src.items():
        for m in re.finditer(g + r"\s*" + SHOW, s):
            shows.append("%s:%d" % (f, s.count("\n", 0, m.start()) + 1))
        unhides |= bool(re.search(g + r"\s*" + UNHIDE, s))
        for vm in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*" + g, s):
            v, body = re.escape(vm.group(1)), s[vm.end(): vm.end() + 4000]
            for m in re.finditer(r"\b%s%s" % (v, SHOW), body):
                shows.append("%s:%d" % (f, s.count("\n", 0, vm.end() + m.start()) + 1))
            unhides |= bool(re.search(r"\b%s%s" % (v, UNHIDE), body))
    return shows, unhides


ok(len(born) > 20, "inventaire : des éléments nés `hidden` sont trouvés (%d)" % len(born))
ok("deploy-palette" in born, "la palette de déploiement fait partie de l'inventaire")
bad = []
for i, f in sorted(born.items()):
    shows, unhides = scan(i)
    if shows and not unhides:
        bad.append("#%s (né dans %s) ouvert par style.display en %s" % (i, f, ", ".join(shows[:3])))
ok(not bad, "aucun élément né `hidden` ouvert seulement par style.display", "\n         ".join(bad))
print("\nÉCHECS : %d" % len(ko) if ko else "\nOK")
sys.exit(1 if ko else 0)
