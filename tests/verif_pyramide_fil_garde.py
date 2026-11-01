#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Pyramide : un échec d'écriture de proxy ne tue plus le fil d'une entrée, un fil mort est relancé,
# et rien ne balaie plus le domaine MXL (`garbage_collect`).
#
# ★ LE CAS (Z8 #220, 28/10, recette custom-d680fb9b49) : réorganisation des entrées → `open_grain`
# d'un proxy lève `MXLError … mxlStatus 8` hors de tout try → worker MORT, processus vivant (les autres
# slots tournent), donc ni script_stopped ni alerte : proxy figé 1 841 s à 0 fps. Cause probable :
# le `garbage_collect()` GLOBAL d'un worker supprimait le proxy pendant l'instant où il n'avait plus
# d'écrivain (l'ancien worker l'avait fermé, le nouveau pas encore rouvert).
#
# On exécute ici le VRAI `worker` du script rendu, sur un faux MXL dont l'écrivain lève UNE fois.
#
#   $ ./venv/bin/python tests/verif_pyramide_fil_garde.py
import ast
import os
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
# ⚠ AVANT tout accès : connexion SQLite tenue PAR THREAD (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database                                     # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_pyr_fil_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()
from app import plugins                                              # noqa: E402
import numpy as np                                                   # noqa: E402

echecs = []


def controle(intitule, condition, explication=""):
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition:
        echecs.append(intitule)
        if explication:
            print("        → %s" % explication)


src = plugins.render_script("pyramide", {}, hostname="banc")
arbre = ast.parse(src)
FN = {n.name: n for n in arbre.body if isinstance(n, ast.FunctionDef)}

# ═══ 1. AUCUN BALAYAGE DU DOMAINE ═══
appels_gc = [ast.unparse(c) for c in ast.walk(arbre)
             if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "garbage_collect"]
controle("★★★ la pyramide n'appelle plus garbage_collect() (global au domaine)", not appels_gc, appels_gc)
cibles = [ast.unparse(k.value) for c in ast.walk(arbre) if isinstance(c, ast.Call)
          and getattr(c.func, "attr", None) == "Thread" for k in c.keywords if k.arg == "target"]
controle("★★ les fils des entrées sont lancés sous leur GARDE", "_worker_garde" in cibles, cibles)


# ═══ 2. LE VRAI WORKER, FAUX MXL ═══
class MXLError(Exception):
    pass


class FauxReader:
    def __init__(self, inst, name):
        self.i = 0
    def format(self):
        return {"width": 64, "height": 36, "chroma": "422", "bit_depth": 8, "fps_num": 50, "fps_den": 1}
    def get_latest(self):
        self.i += 1
        time.sleep(0.004)
        return (self.i, None, np.zeros(64 * 36 * 2, np.uint8))
    def last_write_time(self):
        return 0
    def close(self):
        pass
    def reopen(self):
        pass


class FauxWriter:
    commits = 0
    a_lever = 1          # la 1ʳᵉ ouverture de grain lève, comme le 28/10
    def __init__(self, name):
        self.name = name
    def open_grain(self, src_index=None):
        if FauxWriter.a_lever:
            FauxWriter.a_lever -= 1
            raise MXLError("mxlFlowWriterOpenGrain -> mxlStatus 8")
        return src_index, None, np.zeros(32 * 18 * 2, np.uint8)
    def commit(self, gi, valid_slices=None):
        FauxWriter.commits += 1
    def close(self):
        pass


class FauxBobimxl:
    Reader = FauxReader
    MXL_UNDEFINED_INDEX = -1
    @staticmethod
    def now_tai():
        return 0


class Rolling:
    def push(self, v):
        pass
    def avg(self):
        return 0.0


journal = []
ns = {"time": time, "threading": threading, "np": np, "bobimxl": FauxBobimxl, "inst": None,
      "gc": type("G", (), {"collect": staticmethod(lambda *a: 0)}),
      "log": lambda m, niveau="info": journal.append((niveau, m)),
      "state_lock": threading.Lock(), "metrics_lock": threading.Lock(),
      "state": {"inputs": {0: {"shm": "cam1"}}},
      "metrics": {"sources": {}, "inputs_latency_ms": {}},
      "bus_error": threading.Event(), "SLICE_MODE": False, "_MVK": False, "_extra_gen": [0],
      "_ecart_idx": {}, "_delai_etage": {}, "_sd_new": lambda: None, "RollingMs": Rolling,
      "_desired_specs": lambda s, lyt: [("cam1__s32x18", 32, 18)],
      "_make_proxy": lambda name, lyt, pw, ph: dict(name=name, writer=FauxWriter(name), pw=pw, ph=ph,
                                                    uw=pw // 2, uh=ph),
      "resize_plane": lambda pl, h, w: np.zeros((h, w), pl.dtype),
      "_make_layout": lambda w, h, c, b, fn, fd, il="progressive", ffn=None: dict(
          width=w, height=h, chroma=c, bit_depth=b, fps_num=fn, fps_den=fd, interlace_mode=il,
          fr_sz=w * h * 2, np_dt=np.uint8, uv_w=w // 2, uv_h=h, bps=1, cw=2, ch=1),
      }
for nom in ("_sortie_ko", "_sortie_ok", "_worker_garde", "worker", "_sync_proxies", "_close_proxies",
            "_drop_proxy", "_ouverture_ok", "_ouverture_ko", "_attente_echec"):
    exec(compile(ast.Module([FN[nom]], []), "pyramide/script.py", "exec"), ns)
for n in arbre.body:   # états module des nouveaux mécanismes
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in (
            "_sortie_ko_etat", "_derniere_ecriture", "_fils_etat", "_ko_etat"):
        ns[n.targets[0].id] = {}

# L'échec est publié puis effacé en ~0,1 s (reprise réussie) : on photographie l'état publié
# au moment même où la VRAIE `_sortie_ko` vient de l'écrire.
ko_vu = {}
_vrai_sortie_ko = ns["_sortie_ko"]


def _espion_sortie_ko(slot, motif):
    r = _vrai_sortie_ko(slot, motif)
    ko_vu.update({k: dict(v) for k, v in (ns["metrics"].get("sorties_ko") or {}).items()})
    return r


ns["_sortie_ko"] = _espion_sortie_ko
fil = threading.Thread(target=ns["_worker_garde"], args=(0,), daemon=True)
fil.start()
time.sleep(0.75)
controle("★★★ l'écriture qui lève NE TUE PAS le fil (il est toujours vivant)", fil.is_alive())
controle("★★ l'échec a été PUBLIÉ (sorties_ko) avec son motif",
         "0" in ko_vu and "mxlStatus 8" in ko_vu["0"]["motif"], ko_vu)
controle("★★★ les proxies ont été recréés et l'écriture a REPRIS", FauxWriter.commits > 10, FauxWriter.commits)
controle("★★ une fois rétabli, l'échec disparaît de l'état publié", "0" not in (ns["metrics"].get("sorties_ko") or {}),
         ns["metrics"].get("sorties_ko"))
age = time.time() - ns["_derniere_ecriture"].get(0, 0)
controle("★ l'heure de la dernière écriture est tenue (âge %.2f s)" % age, age < 0.2)
controle("★ le fil n'a jamais été relancé (l'échec a été traité DANS le fil)", not ns["_fils_etat"])
controle("★ journal : l'échec puis le rétablissement sont tracés",
         any("IMPOSSIBLE" in m for _, m in journal) and any("rétablie" in m for _, m in journal),
         [m for _, m in journal][:4])

# ═══ 3. UN FIL QUI MEURT QUAND MÊME EST RELANCÉ ═══
morts = []


def worker_qui_meurt(slot):
    morts.append(slot)
    raise RuntimeError("boum %d" % len(morts))


ns2 = dict(ns, worker=worker_qui_meurt, _fils_etat={})
exec(compile(ast.Module([FN["_worker_garde"]], []), "pyramide/script.py", "exec"), ns2)
ns2["worker"] = worker_qui_meurt
f2 = threading.Thread(target=ns2["_worker_garde"], args=(3,), daemon=True)
f2.start()
time.sleep(0.9)
controle("★★★ un fil mort est RELANCÉ (pas d'entrée arrêtée en silence)", len(morts) >= 2, len(morts))
st = ns2["_fils_etat"].get(3) or {}
controle("★★ ses relances et leur motif sont publiés", st.get("relances", 0) >= 2 and "boum" in str(st.get("dernier_motif")), st)

sorties = []


def worker_qui_sort(slot):
    sorties.append(slot)      # sort SANS exception : un worker qui quitte sa boucle est mort aussi


ns3 = dict(ns, _fils_etat={})
exec(compile(ast.Module([FN["_worker_garde"]], []), "pyramide/script.py", "exec"), ns3)
ns3["worker"] = worker_qui_sort
threading.Thread(target=ns3["_worker_garde"], args=(4,), daemon=True).start()
time.sleep(0.9)
controle("★★ un fil qui SORT de sa boucle sans erreur est relancé aussi", len(sorties) >= 2, len(sorties))

# ═══ 4. L'ORCHESTRATEUR ALERTE (app/metrics.py, vraie fonction) ═══
from app import metrics as M                                         # noqa: E402
alertes = []
M.db_add_alert = lambda cle, niveau="info", **kw: alertes.append((cle, niveau, kw.get("params")))
def m(age, ko=None, relances=0):
    return {"sources": {"0": {"shm": "cam1", "age_ecriture_s": age}},
            "entrees_ko": ko or {}, "sorties_ko": {"0": {"motif": "MXLError: mxlStatus 8"}},
            "fils": {"0": {"vivant": True, "relances": relances, "dernier_motif": "boum"}}}
M._noter_ecritures_figees(900, m(0.1))
controle("orchestrateur : entrée qui écrit → aucune alerte", not alertes, alertes)
M._noter_ecritures_figees(900, m(7.0))
M._noter_ecritures_figees(900, m(9.0))
fig = [a for a in alertes if a[0] == "alert.signal.ecriture_figee"]
controle("★★★ entrée qui n'écrit plus depuis > 5 s → alerte ERREUR, une seule fois, avec le motif",
         len(fig) == 1 and fig[0][1] == "error" and "mxlStatus 8" in fig[0][2]["motif"], alertes)
M._noter_ecritures_figees(900, m(0.2))
controle("★★ retour à la normale → alerte « écrit de nouveau »",
         any(a[0] == "alert.signal.ecriture_repartie" for a in alertes), alertes)
alertes.clear()
M._noter_ecritures_figees(901, m(30.0, ko={"0": {"shm": "cam1"}}))
controle("★ entrée déjà déclarée inexploitable : pas d'alerte en double", not alertes, alertes)
M._noter_ecritures_figees(902, m(0.1, relances=1))
M._noter_ecritures_figees(902, m(0.1, relances=1))
rel = [a for a in alertes if a[0] == "alert.signal.fil_relance"]
controle("★★ fil relancé → alerte, une par relance", len(rel) == 1 and rel[0][2]["n"] == 1, alertes)
import json as _json                                                 # noqa: E402
_fr = _json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
controle("libellés d'alerte présents", all(k in _fr for k in ("alert.signal.ecriture_figee",
         "alert.signal.ecriture_repartie", "alert.signal.fil_relance")))

print("\n%s" % ("ÉCHEC : %d contrôle(s)" % len(echecs) if echecs else "TOUT VERT"))
sys.stdout.flush()
os._exit(1 if echecs else 0)   # fils démons sans fin : sortie franche
