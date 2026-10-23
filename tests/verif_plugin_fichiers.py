#!/usr/bin/env python3
"""Vérifie la livraison de fichiers déclarés par un manifeste (`files` de plugin.json).

Contrat : (1) une déclaration invalide fait SAUTER le plugin au scan (jamais de plugin à moitié
livré) ; (2) au déploiement, les fichiers sont poussés AVANT bobimxl.py puis le script, qui part
EN DERNIER (l'agent lance le dernier /deploy) ; (3) un binaire voyage en base64 (l'agent écrit en
texte) ; (4) l'échec d'un push AVORTE le déploiement — le script n'est jamais poussé.

Aucun réseau ni agent : session HTTP factice. Plugins fabriqués dans un dossier jetable.

    ./venv/bin/python tests/verif_plugin_fichiers.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
from app import plugins, deploy                      # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


TMP = tempfile.mkdtemp(prefix="verif_plugfiles_")
PDIR = os.path.join(TMP, "plugins")
os.makedirs(PDIR)
BIN = bytes(range(256)) * 4            # non-UTF-8 : doit passer en base64


def faire_plugin(nom, files, extra=None):
    d = os.path.join(PDIR, nom)
    os.makedirs(os.path.join(d, "src"), exist_ok=True)
    with open(os.path.join(d, "script.py"), "w") as f:
        f.write("print({config!r}, {hostname!r}, {plugin_version!r})\n")
    with open(os.path.join(d, "src", "mod.py"), "w") as f:
        f.write("X = 'é'\n")
    with open(os.path.join(d, "dsp.so"), "wb") as f:
        f.write(BIN)
    man = {"type": nom, "label": nom, "version": "1.0", "script_template": "script.py",
           "deploy_defaults": {}, "files": files}
    with open(os.path.join(d, "plugin.json"), "w") as f:
        json.dump(man, f)
    if extra:
        extra(d)
    return d


VALIDE = [{"src": "src/mod.py", "dest": "mod.py"},
          {"src": "dsp.so", "dest": "dsp.so.b64", "transport": "base64"}]
faire_plugin("fok", VALIDE)
faire_plugin("ftrav", [{"src": "src/mod.py", "dest": "../evil.py"}])
faire_plugin("fsrctrav", [{"src": "../fok/dsp.so", "dest": "a.so"}])
faire_plugin("fabs", [{"src": "/etc/passwd", "dest": "p"}])
faire_plugin("fabsd", [{"src": "src/mod.py", "dest": "/opt/x.py"}])
faire_plugin("fmanq", [{"src": "src/absent.py", "dest": "a.py"}])
faire_plugin("fcoll", [{"src": "src/mod.py", "dest": "bobimxl.py"}])
faire_plugin("fcoll2", [{"src": "src/mod.py", "dest": "script.py"}])
faire_plugin("fdup", [{"src": "src/mod.py", "dest": "m.py"}, {"src": "dsp.so", "dest": "m.py",
                                                              "transport": "base64"}])
faire_plugin("fsym", [{"src": "lien.py", "dest": "l.py"}],
             lambda d: os.symlink("/etc/passwd", os.path.join(d, "lien.py")))
faire_plugin("ftr", [{"src": "src/mod.py", "dest": "m.py", "transport": "rot13"}])

plugins.PLUGINS_DIR = PDIR
plugins.reload()

ok("fok" in plugins.REGISTRY, "déclaration valide : plugin chargé", str(plugins.SCAN_ERRORS))
for n in ("ftrav", "fsrctrav", "fabs", "fabsd", "fmanq", "fcoll", "fcoll2", "fdup", "fsym", "ftr"):
    ok(n not in plugins.REGISTRY and n in plugins.SCAN_ERRORS, "invalide sauté au scan : " + n,
       str(plugins.SCAN_ERRORS.get(n)))

# validate_package (installation d'un paquet) applique la même règle
_, err = plugins.validate_package(os.path.join(PDIR, "ftrav"))
ok(err and "fichiers" in err, "validate_package refuse la traversée", str(err))
_, err = plugins.validate_package(os.path.join(PDIR, "fok"))
ok(err is None, "validate_package accepte le valide", str(err))

# contenus : texte en clair, binaire en base64 qui fait l'aller-retour
fc = dict(plugins.deploy_files("fok"))
ok(fc["mod.py"] == "X = 'é'\n", "texte transmis tel quel")
ok(base64.b64decode(fc["dsp.so.b64"]) == BIN, "base64 : aller-retour exact")

# version archivée : ses fichiers ne sont pas archivés → rappel refusé, jamais les fichiers courants
_cur = plugins.REGISTRY["fok"].get("version")
ok(dict(plugins.deploy_files("fok", _cur)) == fc, "version courante explicite : fichiers poussés")
try:
    plugins.deploy_files("fok", "0.0.0-archivee")
    ok(False, "version archivée d'un plugin à fichiers : refusée", "aucune exception levée")
except ValueError as e:
    ok("archivée" in str(e), "version archivée d'un plugin à fichiers : refusée", str(e))

# ── déploiement avec agent factice ───────────────────────────────────────────
class Rep:
    def __init__(self, c): self.status_code = c; self.text = ""
    def json(self): return {}


class Session:
    def __init__(self, refuse=None):
        self.posts, self.refuse = [], refuse

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], (json or {}).get("path"),
                           (json or {}).get("content")))
        if self.refuse and json and json.get("path", "").endswith(self.refuse):
            return Rep(500)
        return Rep(200)

    def get(self, *a, **k): return Rep(200)


alertes = []
SPEC = {
    "db_get_container": lambda v: {"vmid": v, "hostname": "h"},
    "db_add_alert": lambda *a, **k: alertes.append((a, k)),
    "get_container_ip": lambda v: "127.0.0.1",
    "_attendre_agent_pret": lambda *a, **k: True,
    "agent_headers": lambda v: {},
    "db_update_script": lambda *a, **k: None,
}
for k, v in SPEC.items():
    setattr(deploy, k, v)
import app.docker_compute as dc                       # noqa: E402
dc.deploy_compute = lambda *a, **k: True
dc.is_mtl_type = lambda t: False


def lancer(refuse=None):
    s = Session(refuse)
    deploy.agent_session = lambda: s
    del alertes[:]
    try:
        r = deploy._deployer_script_locked(1, "fok", {}, script_path="/opt/script/main.py",
                                           script_content="print('s')\n")
    except Exception as e:                            # le garde ne doit pas remonter
        r = "EXC %s" % e
    return r, s.posts


r, posts = lancer()
deploys = [p[1] for p in posts if p[0] == "deploy"]
ok(deploys == ["/opt/script/mod.py", "/opt/script/dsp.so.b64", "/opt/script/bobimxl.py",
               "/opt/script/main.py"], "ordre : fichiers, puis bobimxl, puis script EN DERNIER",
   str(deploys))

r, posts = lancer(refuse="dsp.so.b64")
deploys = [p[1] for p in posts if p[0] == "deploy"]
ok(r is False, "push refusé : déploiement avorté (False)", str(r))
ok("/opt/script/main.py" not in deploys and "/opt/script/bobimxl.py" not in deploys,
   "push refusé : ni bobimxl ni le script poussés", str(deploys))
ok(any("erreur" in str(a[0]) for a, k in alertes), "push refusé : alerte d'erreur levée", str(alertes[-1:]))
ok(not any(p[0] == "start" for p in posts), "push refusé : script jamais démarré")

# ── hooks.py VERSIONNÉ mais NON CHARGÉ ───────────────────────────────────────────────
# `_load_hooks` est la SEULE exception à « aucun code de plugin n'est exécuté in-process » :
# il est importé au scan, à chaque démarrage. Ses trois issues ne se journalisent pas pareil
# (mesuré le 2026-10-23, sans configuration de logging, comme au boot) :
#   chargé          → log.info     → INVISIBLE au boot (le scan précède la config du logging)
#   échec d'import  → log.warning  → visible (gestionnaire de dernier recours → stderr → journald)
#   fichier ABSENT  → `return None` sec, AUCUN message
# C'est donc la DISPARITION du fichier qui passe inaperçue, pas son échec. Un `before_deploy`
# qui ne s'applique plus ne laisse aucune trace, et le symptôme est fonctionnel : un plugin qui
# se déploie sans sa normalisation.
# Le registre ne peut pas le détecter seul — `hooks.py` est optionnel par convention, il n'y a
# aucune déclaration à confronter (avertir pour chaque plugin sans hooks ferait du bruit sur la
# majorité, et un avertissement qu'on apprend à ignorer ne protège de rien). Le DÉPÔT, lui, sait :
# si hooks.py est versionné, il DOIT être chargé.


def hooks_manquants(registry, versionnes):
    """Types dont le dépôt versionne un hooks.py que le registre n'a pas chargé."""
    manque = []
    for type_, m in sorted(registry.items()):
        if type_ in versionnes and m.get("_hooks") is None:
            manque.append(type_)
    return manque


# Le critère d'abord sur des cas FABRIQUÉS : une garde qui ne tourne que sur un dépôt sain
# resterait verte sans rien vérifier.
HDIR = os.path.join(TMP, "hooks_cas")
os.makedirs(HDIR)


def cas_hooks(nom, contenu):
    """Fabrique un plugin minimal, avec ou sans hooks.py (contenu None = pas de fichier)."""
    d = os.path.join(HDIR, nom)
    os.makedirs(d)
    with open(os.path.join(d, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"type": nom, "label": nom, "version": "0.1.0",
                   "script_template": "script.py"}, f)
    with open(os.path.join(d, "script.py"), "w", encoding="utf-8") as f:
        f.write("C = {config}\n")
    if contenu is not None:
        with open(os.path.join(d, "hooks.py"), "w", encoding="utf-8") as f:
            f.write(contenu)
    return d


cas_hooks("hsain", "def before_deploy(vmid, params):\n    return params\n")
cas_hooks("hcasse", "import ce_module_n_existe_pas\n")
cas_hooks("hsans", None)

plugins.PLUGINS_DIR = HDIR
reg = plugins._scan()

ok(reg.get("hsain", {}).get("_hooks") is not None, "cas : hooks.py valide → chargé",
   str(plugins.SCAN_ERRORS))
ok(reg.get("hcasse", {}).get("_hooks") is None, "cas : hooks.py qui lève → non chargé")
ok(reg.get("hsans", {}).get("_hooks") is None, "cas : pas de hooks.py → None")

# Le critère MORD sur les deux formes de « versionné mais pas chargé »…
ok(hooks_manquants(reg, {"hcasse"}) == ["hcasse"], "critère : mord sur un hooks.py qui lève",
   str(hooks_manquants(reg, {"hcasse"})))
ok(hooks_manquants(reg, {"hsans"}) == ["hsans"], "critère : mord sur un hooks.py DISPARU",
   str(hooks_manquants(reg, {"hsans"})))
# …et se tait quand le dépôt n'en versionne pas (sinon il crierait sur la majorité des plugins).
ok(hooks_manquants(reg, set()) == [], "critère : muet sur un plugin sans hooks.py versionné",
   str(hooks_manquants(reg, set())))
ok(hooks_manquants(reg, {"hsain"}) == [], "critère : muet sur un hooks.py versionné ET chargé",
   str(hooks_manquants(reg, {"hsain"})))

# Puis sur le VRAI dossier plugins/.
VRAI = os.path.join(_R, "plugins")
plugins.PLUGINS_DIR = VRAI
reg_vrai = plugins._scan()


def versionne_hooks(pdir):
    """True si le dépôt qui contient `pdir` versionne pdir/hooks.py. (None si git muet.)"""
    r = subprocess.run(["git", "-C", pdir, "ls-files", "--", "hooks.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


vers, muets = set(), []
for nom in sorted(os.listdir(VRAI)):
    d = os.path.join(VRAI, nom)
    if nom.startswith("_") or not os.path.isfile(os.path.join(d, "plugin.json")):
        continue
    v = versionne_hooks(d)
    if v is None:
        muets.append(nom)
    elif v:
        vers.add(json.load(open(os.path.join(d, "plugin.json"), encoding="utf-8"))["type"])

# Une garde dont l'outil manque échoue OUVERTE : le dire plutôt que de rendre un vert creux.
ok(not muets, "dépôt interrogeable pour chaque plugin (git)", "git muet sur : %s" % muets)
ok(len(vers) > 0, "au moins un plugin versionne un hooks.py", "aucun — critère inopérant")
manque = hooks_manquants(reg_vrai, vers)
ok(not manque, "tout hooks.py versionné est chargé au registre (%d plugin(s))" % len(vers),
   "versionné mais NON chargé : %s — cf. plugins.SCAN_ERRORS" % manque)

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
