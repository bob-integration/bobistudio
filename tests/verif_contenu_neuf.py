"""Vérif hors-ligne du VERDICT DE CONTENU NEUF (`home_dashboard._contenu_etats`).

Topologie synthétique reproduisant le parc d'Horace : 16 sources 1080i25 → 2 shards → 1
assembleur 1080p50. Fonction PURE : aucune base lue, aucun conteneur touché, exécutable partout.

    ./venv/bin/python tools/verif_contenu_neuf.py

Couvre les deux symptômes du 2026-08-17 (faux positif permanent sur les shards, cécité de
l'assembleur sur un shard gelé) ET le cas d'origine de la métrique (0.69.0), qui doit continuer
de se voir."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import metrics
from app.routes.home_dashboard import _contenu_etats

SRC_FPS, N_SRC = 25.0, 16
SH_A, SH_B, ASM = 540, 541, 362

def topo(src_fps=SRC_FPS):
    srcs = [f"cam{i}" for i in range(N_SRC)]
    nodes, producers = [], {}
    for i, s in enumerate(srcs):                      # 16 sources 2110 (pas de fps_content)
        nodes.append({"vmid": 1000 + i, "produces": [{"shm": s, "kind": "video",
                      "format": {"fps": src_fps} if src_fps else {}}], "consumes": []})
        producers[s] = [{"vmid": 1000 + i, "kind": "video"}]
    for v, part in ((SH_A, srcs[:8]), (SH_B, srcs[8:])):   # 2 shards → sortent à 50
        nodes.append({"vmid": v, "produces": [{"shm": f"fab_{v}", "kind": "video",
                      "format": {"fps": 50}}],
                      "consumes": [{"shm": s, "kind": "video"} for s in part]})
        producers[f"fab_{v}"] = [{"vmid": v, "kind": "video"}]
    nodes.append({"vmid": ASM, "produces": [{"shm": "mur", "kind": "video", "format": {"fps": 50}}],
                  "consumes": [{"shm": f"fab_{SH_A}", "kind": "video"},
                               {"shm": f"fab_{SH_B}", "kind": "video"}]})
    producers["mur"] = [{"vmid": ASM, "kind": "video"}]
    return nodes, producers

def run(contenu, src_fps=SRC_FPS, shards=True):
    metrics.fps_content_cache.clear()
    metrics.fps_content_cache.update({v: c for v, c in contenu.items()})
    metrics.fps_plancher = lambda vmid, canal="fps": contenu.get(vmid)
    nodes, producers = topo(src_fps)
    _contenu_etats(nodes, producers, {ASM: [SH_A, SH_B]} if shards else {},
                   {SH_A: "shard-a", SH_B: "shard-b", ASM: "mur-regie"})
    return {n["vmid"]: n["contenu_etat"] for n in nodes}

ok = True
def check(nom, cond, detail=""):
    global ok
    print(("  OK   " if cond else "  ÉCHEC") + f" {nom}" + (f"   {detail}" if detail else ""))
    ok = ok and cond

# ── 1. Parc SAIN (chiffres réels de la note : shards ~24,7 sur 24,9 dispo, assembleur 50)
e = run({SH_A: 24.7, SH_B: 30.3, ASM: 50.0})
check("shard sain : silencieux", e[SH_A]["tenue"] is True, f"mesure 24,7 / ref {e[SH_A]['ref']}")
check("shard déphasé (30,3) : silencieux", e[SH_B]["tenue"] is True)
check("assembleur sain : silencieux", e[ASM]["tenue"] is True, f"mesure 50 / ref {e[ASM]['ref']}")
check("référence d'un shard = cadence TRAME des sources (25), pas 50", e[SH_A]["ref"] == 25.0)
check("référence de l'assembleur PROPAGÉE depuis les shards (25), pas 50", e[ASM]["ref"] == 25.0)

# ── 2. Un shard GÈLE son contenu tout en continuant d'émettre à 50 fps
e = run({SH_A: 0.0, SH_B: 30.3, ASM: 50.0})
check("shard gelé : décroche", e[SH_A]["tenue"] is False)
check("ASSEMBLEUR : décroche via le maillon (faux négatif d'origine)", e[ASM]["tenue"] is False)
check("le maillon est NOMMÉ", e[ASM]["maillon"] == "shard-a", f"maillon={e[ASM]['maillon']}")
check("l'autre shard reste silencieux", e[SH_B]["tenue"] is True)

# ── 3. Un shard à MOITIÉ de cadence de contenu
e = run({SH_A: 12.4, SH_B: 30.3, ASM: 50.0})
check("shard à demi-cadence : décroche", e[SH_A]["tenue"] is False)
check("assembleur : décroche", e[ASM]["tenue"] is False)

# ── 4. AUCUNE référence (format de source non déclaré) → aucun verdict, jamais d'alerte
e = run({SH_A: 1.0, SH_B: 1.0, ASM: 1.0}, src_fps=None)
check("sans référence : aucun verdict", all(e[v]["tenue"] is None for v in (SH_A, SH_B, ASM)))

# ── 5. Mur NON shardé sur sources 50p : le cas d'origine (0.69.0) doit encore se voir
metrics.fps_content_cache.clear(); metrics.fps_content_cache[ASM] = 38.0
metrics.fps_plancher = lambda vmid, canal="fps": {ASM: 38.0}.get(vmid)
nodes = [{"vmid": 900, "produces": [{"shm": "s", "kind": "video", "format": {"fps": 50}}], "consumes": []},
         {"vmid": ASM, "produces": [{"shm": "mur", "kind": "video", "format": {"fps": 50}}],
          "consumes": [{"shm": "s", "kind": "video"}]}]
_contenu_etats(nodes, {"s": [{"vmid": 900, "kind": "video"}], "mur": [{"vmid": ASM, "kind": "video"}]}, {}, {})
e = {n["vmid"]: n["contenu_etat"] for n in nodes}
check("mur 50p qui ne relaie que 38 : décroche TOUJOURS", e[ASM]["tenue"] is False,
      f"mesure {e[ASM]['mesure']} / ref {e[ASM]['ref']}")

print("\n" + ("TOUT VERT" if ok else "DES CAS ÉCHOUENT"))
sys.exit(0 if ok else 1)
