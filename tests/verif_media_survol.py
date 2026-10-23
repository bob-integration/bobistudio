#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde du SURVOL-DÉFILEMENT de la bibliothèque des médias (static/js/media_library.js) :
la vignette d'un média vidéo à proxy devient une <video> que la position de la souris parcourt.

Logique pure exécutée sous node (exportée par `module.exports` quand `window` n'existe pas) :
  1. position → fraction → instant (bornes, largeur nulle, 100 % = dernière image) ; m:ss ;
  2. file de recherche : UNE recherche en vol, la DERNIÈRE position voulue servie au `seeked`,
     rien avant les métadonnées, `fastSeek` pendant le mouvement puis recherche exacte à l'arrêt,
     un `seeked` qui ne vient pas ne bloque pas la file ;
  3. arrêt : écouteurs retirés, plus aucune recherche ; démontage : pause, `src` retiré PUIS load().
Câblage (source) : vignette marquée seulement si `has_proxy` + vidéo, survol coupé au rendu, au
glisser, au démontage ; souris seulement.
"""
import json
import os, re
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
    print("node absent : garde ignorée")
    sys.exit(0)

PROG = r"""
const M = require(process.argv[1]);
const out = {};
// 1. position → instant
out.frac = [M.scrubFrac(100, 100, 200), M.scrubFrac(300, 100, 200), M.scrubFrac(200, 100, 200),
            M.scrubFrac(50, 100, 200), M.scrubFrac(900, 100, 200), M.scrubFrac(150, 100, 0)];
out.time = [M.scrubTime(0, 60), M.scrubTime(1, 60), M.scrubTime(0.5, 60), M.scrubTime(2, 60), M.scrubTime(0.5, 0)];
// Planche d'images : case affichée pour un instant (planche à `times`, sinon proportionnelle).
const SBT = {n:5, cols:3, rows:2, times:[0, 10, 20, 30, 40]}, SBP = {n:4, cols:2, rows:2};
out.sbIdx = [M.sbIndexAt(SBT, 0, 50), M.sbIndexAt(SBT, 9.99, 50), M.sbIndexAt(SBT, 10, 50), M.sbIndexAt(SBT, 35, 50),
             M.sbIndexAt(SBT, 999, 50), M.sbIndexAt(SBP, 0, 40), M.sbIndexAt(SBP, 19.9, 40), M.sbIndexAt(SBP, 39.99, 40),
             M.sbIndexAt(SBP, 5, 0)];
out.clock = [M.fmtClock(0), M.fmtClock(65.9), M.fmtClock(3725)];
// Aperçu agrandi plafonné à 75 % de la fenêtre (largeur ET hauteur, rapport conservé).
out.pop = [M.scrubPopSize(160, 90, 1.8, 1920, 1080), M.scrubPopSize(800, 450, 1.8, 1000, 2000),
           M.scrubPopSize(400, 600, 1.8, 2000, 800), M.scrubPopSize(800, 450, 1.8, 1000, 300)];

// Fausse vidéo : écouteurs comptés, recherches journalisées.
function fakeVideo(withFast){
  const L = {}, v = {readyState:0, log:[], _t:0, paused:false, attrs:{src:'x'},
    addEventListener(n, f){ (L[n] = L[n] || []).push(f); },
    removeEventListener(n, f){ L[n] = (L[n] || []).filter(x => x !== f); },
    fire(n){ (L[n] || []).slice().forEach(f => f()); },
    count(){ return Object.values(L).reduce((s, l) => s + l.length, 0); },
    pause(){ v.log.push('pause'); }, load(){ v.log.push('load:' + ('src' in v.attrs ? 'src' : 'nosrc')); },
    removeAttribute(k){ delete v.attrs[k]; v.log.push('rm:' + k); }};
  Object.defineProperty(v, 'currentTime', {get(){ return v._t; }, set(t){ v._t = t; v.log.push('exact:' + t); }});
  if(withFast) v.fastSeek = t => { v._t = Math.floor(t); v.log.push('fast:' + t); };
  return v;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  // 2a. sans fastSeek : rien avant les métadonnées, une seule en vol, la dernière servie
  let v = fakeVideo(false), s = M.createSeeker(v, {settleMs:30, guardMs:5000});
  s.want(5);
  out.beforeMeta = v.log.slice();
  v.readyState = 1; v.fire('loadedmetadata');
  out.afterMeta = v.log.slice();
  s.want(6); s.want(7); s.want(8);
  out.inFlight = v.log.slice();
  v.fire('seeked');
  out.afterSeeked = v.log.slice();
  v.fire('seeked');
  out.idle = v.log.slice();
  out.seeks = s.state().seeks;
  s.want(8); out.dedupe = v.log.slice();
  // 3. arrêt : écouteurs retirés, plus de recherche
  s.stop();
  out.listenersAfterStop = v.count();
  s.want(9); v.fire('seeked'); out.afterStop = v.log.slice();

  // 2b. fastSeek pendant le mouvement, recherche exacte à l'arrêt
  v = fakeVideo(true); v.readyState = 1; s = M.createSeeker(v, {settleMs:30, guardMs:5000});
  s.want(3.4); v.fire('seeked'); s.want(4.6); v.fire('seeked');
  await sleep(60);
  out.fast = v.log.slice();
  v.fire('seeked'); s.stop();

  // 2c. un seeked qui ne vient jamais ne bloque pas la file
  v = fakeVideo(false); v.readyState = 1; s = M.createSeeker(v, {settleMs:30, guardMs:40});
  s.want(1); s.want(2);
  await sleep(80);
  out.guard = v.log.slice(); s.stop();

  // 3b. démontage
  v = fakeVideo(false); M.teardownVideo(v); out.teardown = v.log.slice();
  console.log(JSON.stringify(out));
})();
"""
r = subprocess.run(["node", "-e", PROG, JS], capture_output=True, text=True)
if r.returncode != 0:
    check("node exécute la logique du survol", False, r.stderr[-600:])
    print("%d ok, %d échec(s)" % (ok, fail))
    sys.exit(1)
o = json.loads(r.stdout)

check("1 fraction : bords, milieu, hors vignette bornée, largeur nulle → 0",
      o["frac"] == [0, 1, 0.5, 0, 1, 0], o["frac"])
t = o["time"]
check("1 instant : 0 % = début, 100 % = dernière image (< durée), milieu, bornes, durée inconnue → 0",
      t[0] == 0 and 59.9 <= t[1] < 60 and abs(t[2] - 29.975) < 1e-9 and t[3] == t[1] and t[4] == 0, t)
check("1 m:ss / h:mm:ss", o["clock"] == ["0:00", "1:05", "1:02:05"], o["clock"])
check("2 rien avant les métadonnées", o["beforeMeta"] == [], o["beforeMeta"])
check("2 métadonnées → la recherche en attente part", o["afterMeta"] == ["exact:5"], o["afterMeta"])
check("2 UNE recherche en vol : les mouvements suivants attendent", o["inFlight"] == ["exact:5"], o["inFlight"])
check("2 au seeked : la DERNIÈRE position voulue, pas les intermédiaires", o["afterSeeked"] == ["exact:5", "exact:8"], o["afterSeeked"])
check("2 file vide : un seeked de plus ne relance rien", o["idle"] == ["exact:5", "exact:8"] and o["seeks"] == 2, (o["idle"], o["seeks"]))
check("2 même position déjà servie : pas de nouvelle requête", o["dedupe"] == ["exact:5", "exact:8"], o["dedupe"])
check("3 arrêt : écouteurs retirés", o["listenersAfterStop"] == 0, o["listenersAfterStop"])
check("3 arrêt : plus aucune recherche", o["afterStop"] == ["exact:5", "exact:8"], o["afterStop"])
check("2 fastSeek pendant le mouvement, recherche EXACTE de la dernière position à l'arrêt",
      o["fast"] == ["fast:3.4", "fast:4.6", "exact:4.6"], o["fast"])
check("2 seeked perdu : la file repart après le délai de garde", o["guard"] == ["exact:1", "exact:2"], o["guard"])
check("3 démontage : pause, src retiré PUIS load() (coupe la connexion)",
      o["teardown"] == ["pause", "rm:src", "load:nosrc"], o["teardown"])

src = open(JS, encoding="utf-8").read()
check("câblage : vignette marquée seulement si (planche OU proxy) + vidéo + pas en cours d'écriture",
      "(a.has_proxy || a.has_storyboard) && kindOf(a) === 'video' && !a.growing" in src and "canScrub(r.asset) ? ` data-scrub" in src)
# Le CORPS de chaque point d'entrée doit appeler scrubStop() en tête : on cherche l'appel dans les
# premières instructions, pas une chaîne alignée (la garde tombait sur un `_dead = true;` inséré ou un
# commentaire rallongé, sans qu'aucun comportement n'ait bougé — relevé du 2026-10-23).
def _calls_first(head_re, n=200):
    m = re.search(head_re, src)
    return bool(m) and re.search(r"\bscrubStop\(\)", src[m.end():m.end() + n]) is not None
check("câblage : survol coupé au rendu, au glisser, au démontage",
      _calls_first(r"function render\(\)\{") and _calls_first(r"addEventListener\('dragstart',[^{]*\{")
      and _calls_first(r"destroy\(\)\{", 40))
check("câblage : souris seulement, aucun bouton enfoncé", "e.pointerType !== 'mouse' || e.buttons" in src)
check("câblage : une seule vidéo de survol dans la page", "if(_scrubActive) _scrubActive();" in src)
css = open(os.path.join(RACINE, "static/css/media_library.css"), encoding="utf-8").read()
check("aperçu AGRANDI ×1,8 (demande du 2026-10-12)", "const SCRUB_ZOOM = 1.8;" in src
      and "scrubPopSize(box.width, box.height, SCRUB_ZOOM, vw, vh)" in src)
# Plafond 75 % de la fenêtre (décision du 2026-10-12) : petite vignette inchangée ; largeur, hauteur,
# ou les deux à la fois → réduites ensemble, rapport conservé.
check("plafond 75 % : petite vignette = ×1,8 exact", o["pop"][0] == [288, 162], o["pop"])
check("plafond 75 % : trop large → 750 px de large, rapport 16:9 conservé", o["pop"][1] == [750, 422], o["pop"])
check("plafond 75 % : trop haute → 600 px de haut, rapport conservé", o["pop"][2] == [400, 600], o["pop"])
check("plafond 75 % : la contrainte la plus forte l'emporte (hauteur 225)", o["pop"][3] == [400, 225], o["pop"])
check("l'aperçu ne capte pas la souris : clic / glisser atteignent la ligne",
      ".ml-scrub-pop {" in css and "pointer-events:none" in css.split(".ml-scrub-pop {", 1)[1].split("}", 1)[0])
check("sortie de l'aperçu → fin du survol", "if(ev.type === 'pointermove' && !inPop && !inImg) return scrubStop();" in src)
check("câblage : la fiche Infos reprend l'instant survolé", "'#t=' + at.toFixed(2)" in src)
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    check("i18n %s : medialib.scrub_tip" % lang, bool(cat.get("medialib.scrub_tip")))
check("planche : case de l'instant (times : dernière <= t ; sinon proportionnelle ; bornée)",
      o["sbIdx"] == [0, 0, 1, 3, 4, 0, 1, 3, 0], o["sbIdx"])
check("planche : survol hybride — case pendant le mouvement, proxy seulement à l'arrêt (300 ms), planche seule sans proxy",
      "SCRUB_SETTLE_MS = 300" in src and "showCell(t);" in src and "if(a.has_proxy) _sc.idle = setTimeout" in src
      and "settled = true; wireVideo(); seeker.want(lastT);" in src and "if(mode !== 'sb' || settled)" in src)
check("planche : repli sur le proxy seul quand la planche est absente ou invalide",
      "mode = 'video'; wireVideo(); seeker.want(lastT);" in src and "a.has_storyboard ? 'wait' : 'video'" in src)
check("planche : le proxy n'est PAS demandé tant que le pointeur bouge (src posé à la 1re immobilité)",
      "const wireVideo = () => { if(wired || !a.has_proxy) return;" in src and "if(mode === 'video') wireVideo();" in src)
check("planche : CSS de la case", ".ml-scrub-sb" in css)
print("%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
