#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Taille des paquets audio sur le bus MXL : un RÉGLAGE DU CŒUR, plus une convention recopiée (2026-11-09).

Le constat : le flowDef audio ne porte aucune granularité — un flux audio est un anneau continu
d'échantillons, `AudioWriter.write` accepte n'importe quel nombre. Les 48 échantillons (1 ms) étaient
recopiés dans chaque script (`SAMPLE_RATE // 1000`), chacun avec un commentaire qui affirmait une contrainte
du bus inexistante. Décision de Cyril : la taille vient d'un réglage du cœur (`mxl_audio_grain_us`,
Réglages → MXL), distinct du ptime 2110-30 ; tous les conteneurs le suivent, 1 ms seule valeur proposée.

GARDE : aucun script de plugin ne recalcule la taille par `<cadence> // 1000`. Une conversion qui n'est
PAS la taille des paquets (une durée en ms convertie en échantillons) porte en fin de ligne le marqueur
`# per-ms, not the bus grain`.

MUTATIONS (le test DOIT échouer) — GRAIN_MUT=<nom> :
    noinject    render_config n'injecte pas la valeur effective
    anyvalue    une valeur non proposée passe telle quelle au script

    ./venv/bin/python tests/verif_grain_audio.py
"""
import glob
import importlib.util
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
MUT = os.environ.get("GRAIN_MUT", "")
if MUT not in ("", "noinject", "anyvalue"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ================================================================ 1. bobimxl
print("── 1. bobimxl.audio_grain ──────────────────────────────────────────────────────")
_src = open(os.path.join(RACINE, "script_templates", "bobimxl.py"), encoding="utf-8").read()
_ns = {"json": json, "flow_id": lambda n: "id-" + n}
exec(compile(_src[_src.index("AUDIO_GRAIN_TAG = "):_src.index("def _audio_planes(")], "bobimxl", "exec"), _ns)
ag = _ns["audio_grain"]
verifie(ag({"mxl_audio_grain_us": 1000}) == 48, "1a. 1 ms à 48 kHz : 48 échantillons")
verifie(ag({}) == 48 and ag(None) == 48, "1b. config sans la clé (orchestrateur antérieur) : 1 ms, l'ancienne convention")
verifie(ag({"mxl_audio_grain_us": 125}) == 6 and ag({"mxl_audio_grain_us": 333}) == 16,
        "1c. la fonction sait exprimer les temps de paquet 2110-30 (125 µs = 6, 333 µs = 16)")
verifie(ag({"mxl_audio_grain_us": 1000}, sample_rate=96000) == 96, "1d. suit la fréquence d'échantillonnage")
verifie(ag({"mxl_audio_grain_us": "x"}) == 48 and ag({"mxl_audio_grain_us": -5}) == 48, "1e. valeur illisible : 1 ms")
# like `slice_height` on a video flow: the producer ANNOUNCES its grain, a reader reads it
ag({"mxl_audio_grain_us": 1000})
fd = json.loads(_ns["build_audio_flow_def"]("x", grain=_ns["_DECLARED_GRAIN"][0]))
verifie(_ns["flow_audio_grain"](fd) == 48 and "urn:x-nmos:tag:grouphint/v1.0" in fd["tags"],
        "1f. ★ le flux annonce sa taille de paquet dans un tag de sa description (comme la vidéo ses tranches)")
verifie(_ns["flow_audio_grain"](json.loads(_ns["build_audio_flow_def"]("x"))) is None
        and _ns["flow_audio_grain"](None) is None, "1g. flux qui n'annonce rien (écrit avant le réglage) : None, lu comme 1 ms")
W = _src[_src.index("class AudioWriter:"):_src.index("class AudioReader")]
verifie("self.grain = int(grain) if grain else _DECLARED_GRAIN[0]" in W and "_old is not None and _old != self.grain" in W,
        "1h. ★ AudioWriter annonce la taille prise par audio_grain(CONFIG) ; recrée le flux SEULEMENT si une autre taille était annoncée")
R = _src[_src.index("class AudioReader"):]
verifie("def grain(self):" in R[:R.index("def index_time_ns")], "1i. AudioReader lit la taille annoncée par le flux auquel il est abonné")

# ================================================================ 2. orchestrateur
print("── 2. réglage du cœur, résolu au rendu ─────────────────────────────────────────")
# La base AVANT le premier import qui y touche : `app.settings` ouvre la base au chargement,
# et la surcharger après ne corrige rien — chaque fil garde sa connexion (cf. app/database.py).
# Sans ça le test ouvre la base de PRODUCTION, en lecture seule mais l'agrégat de ci.yml le
# refuse : la CI publique de la 0.9.15 est tombée là-dessus, tous les tests verts par ailleurs.
import tempfile  # noqa: E402
from app import database as _database  # noqa: E402
_database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_grain_"), "principale.db")
_database.init_db()
from app import plugins, settings  # noqa: E402

reglages = {}
settings.get = lambda k, d=None: reglages.get(k, d)
if MUT == "noinject":
    _rc0 = plugins.render_config
    plugins.render_config = lambda t, p: {k: v for k, v in _rc0(t, p).items() if k != "mxl_audio_grain_us"}
if MUT == "anyvalue":
    plugins.AUDIO_GRAINS_US = (125, 250, 333, 1000)
SET = open(os.path.join(RACINE, "app", "settings.py"), encoding="utf-8").read()
verifie('"mxl_audio_grain_us": 1000,' in SET, "2a. réglage déclaré, 1 ms par défaut")
types = sorted(plugins.REGISTRY)[:5] + ["tone_gen", "audio_console"]
cfgs = {t: plugins.render_config(t, {}) for t in types if t in plugins.REGISTRY}
verifie(cfgs and all(c.get("mxl_audio_grain_us") == 1000 for c in cfgs.values()),
        "2b. ★ TOUT type reçoit la valeur effective dans CONFIG", {t: c.get("mxl_audio_grain_us") for t, c in cfgs.items()})
reglages["mxl_audio_grain_us"] = 125
verifie(plugins.render_config("tone_gen", {}).get("mxl_audio_grain_us") == 1000,
        "2c. ★ une valeur que l'interface ne propose pas ne part pas vers les scripts")
reglages.clear()
p = {"audio_grain_us": 1000}
plugins.render_config("tone_gen", p)
verifie(p == {"audio_grain_us": 1000}, "2d. résolu au rendu : rien n'est figé dans les paramètres du conteneur", p)

# ================================================================ 3. interface, textes, aide
print("── 3. interface et aide ────────────────────────────────────────────────────────")
HTML = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
m = re.search(r'<select id="s_mxl_audio_grain_us".*?</select>', HTML, re.S)
verifie(bool(m) and re.findall(r'<option value="(\d+)"', m.group(0)) == ["1000"],
        "3a. Réglages → MXL : 1 ms est la seule valeur proposée")
verifie("'mxl_audio_grain_us'" in HTML, "3b. relu au chargement de la page")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    verifie(all(cat.get("settings.mxl.grain_" + k) for k in ("title", "help", "lbl", "1ms", "hint")),
            "3c. textes du réglage (%s)" % lang)
AIDE = open(os.path.join(RACINE, "templates", "aide.html"), encoding="utf-8").read()
verifie('id="wiki-art-paquets-audio"' in AIDE and "id: 'paquets-audio'" in AIDE,
        "3d. article d'aide « Paquets audio sur le bus », rangé dans la rubrique")

# ================================================================ 4. garde sur les plugins
print("── 4. aucun script ne recalcule la taille par `// 1000` ────────────────────────")
RX = re.compile(r"\b[A-Za-z_]*(SR|SAMPLE_RATE|sample_rate|RATE)\s*//\s*1000\b")
MARK = "# per-ms, not the bus grain"
fautifs = []
for f in sorted(glob.glob(os.path.join(RACINE, "plugins", "*", "script.py"))
                + glob.glob(os.path.join(RACINE, "plugins", "*", "src", "*.py"))):
    if "/versions/" in f:
        continue
    for i, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
        if RX.search(line) and MARK not in line:
            fautifs.append("%s:%d" % (os.path.relpath(f, RACINE), i))
verifie(not fautifs, "4a. ★ taille des paquets prise par bobimxl.audio_grain(CONFIG)", fautifs)
mxl_io = os.path.join(RACINE, "plugins", "audio_console", "src", "mxl_io.py")
if os.path.exists(mxl_io):
    src = open(mxl_io, encoding="utf-8").read()
    verifie(not re.search(r"gcd\(.*\b48\b", src), "4b. la console découpe selon le réglage, pas selon 48 en dur")

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    for e in echecs:
        print("  - " + e)
    sys.exit(1)
print("OK : la taille des paquets audio vient d'un réglage du cœur")
