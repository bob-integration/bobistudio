#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Garde OFFLINE : `force_cpu` descend d'un mur à ses nœuds du tissu compositeur — SANS changer la
# signature des murs qui ne le posent pas.
#
# Incident Horace, 2026-10-06 : deux murs remis au CPU à la main ; leurs quatre shards étaient
# repartis sur l'UNIQUE Tesla T4 du nœud — cinq contextes CUDA en temps partagé (98 % d'occupation
# pour 2 % de trafic mémoire), shards à 16-25 fps pour 50, tuile PGM qui flickait. `force_cpu`
# n'apparaissait NULLE PART dans app/compositor_fabric.py.
#
# Trois familles de vérifications, et la première est la plus importante :
#   A. NON-RÉGRESSION DES SIGNATURES. Un mur sans `force_cpu` doit garder un style — donc des
#      signatures — octet pour octet identiques à l'historique. Sinon la mise à jour elle-même
#      changerait la signature de TOUS les murs et re-shardérait le parc entier, à l'antenne.
#   B. PROPAGATION. Un mur en `force_cpu` le transmet à chaque shard et à son assembleur, et sa
#      signature CHANGE — c'est ce qui déclenche le rebind des shards déjà matérialisés.
#   C. PARTAGE. Deux murs en désaccord sur `force_cpu` ne partagent plus de nœud (contrôle : deux
#      murs identiques, eux, partagent — sinon l'assertion « 0 partagé » ne prouverait rien).
# Plus D : l'alerte « carte réservée mais inutilisée » ne doit pas accuser un CPU DEMANDÉ.
# Plus E : le REBIND d'un shard (envoi à chaud) refuse un changement structurel. Sans ce refus, la
# première version de ce correctif écrivait `force_cpu` en base et laissait le script tourner sur
# GPU — déployée à Horace, elle n'a rien changé.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import compositor_fabric as cf

n = 0


def check(label, cond, got=None):
    global n
    n += 1
    print(("  [OK] " if cond else "  [KO] ") + label + ("" if cond else f"  → {got!r}"))
    assert cond, label


def wall(force_cpu="absent", sources=None):
    """Un mur de 6 cellules 640×360 sur 1920×720 — assez pour que le planificateur sharde."""
    sources = sources or ["src_%d" % i for i in range(6)]
    cells = [{"path": "/dev/shm/" + s, "x": (i % 3) * 640, "y": (i // 3) * 360, "w": 640, "h": 360}
             for i, s in enumerate(sources)]
    params = {"out_width": 1920, "out_height": 720, "flux_config": cells,
              "chroma": "422", "bit_depth": 8}
    if force_cpu != "absent":
        params["force_cpu"] = force_cpu
    return params


def historical_style(params):
    """Le style tel que le calculait le code AVANT ce correctif — la référence à ne pas bouger."""
    return {k: params.get(k) for k in cf._STYLE_FIELDS}


# ── A. Non-régression des signatures ───────────────────────────────────────────────────────────
print("A. un mur SANS force_cpu garde ses signatures historiques")
for label, p in (("clé absente", wall()), ("force_cpu=False", wall(False)),
                 ("force_cpu=None", wall(None))):
    check("style identique à l'historique (%s)" % label,
          cf.style_context(p) == historical_style(p), cf.style_context(p))

ref_sig = cf.decompose_multiview(wall())["output_sig"]
check("signature inchangée avec force_cpu=False",
      cf.decompose_multiview(wall(False))["output_sig"] == ref_sig)
check("signature inchangée avec force_cpu=None",
      cf.decompose_multiview(wall(None))["output_sig"] == ref_sig)

plan = cf.plan_fabric({"1": wall()}, max_cells_per_shard=2)
check("le planificateur sharde bien ce mur (sinon le test ne prouve rien)",
      len(plan["nodes"]) >= 2, len(plan["nodes"]))
node_params = [cf.build_node_params(nd, "shm_%s" % sig) for sig, nd in plan["nodes"].items()]
check("aucun paramètre de shard ne porte la clé force_cpu",
      all("force_cpu" not in np for np in node_params))
asm = cf.build_assembler_params(plan["outputs"]["1"], "mur_1",
                                {sig: "shm_%s" % sig for sig in plan["nodes"]})
check("l'assembleur ne porte pas la clé force_cpu", "force_cpu" not in asm)

# ── B. Propagation ─────────────────────────────────────────────────────────────────────────────
print("B. un mur EN force_cpu le transmet à tous ses nœuds")
plan_cpu = cf.plan_fabric({"1": wall(True)}, max_cells_per_shard=2)
check("chaque nœud du plan porte l'intention",
      all(nd.get("force_cpu") is True for nd in plan_cpu["nodes"].values()),
      [nd.get("force_cpu") for nd in plan_cpu["nodes"].values()])
node_params = [cf.build_node_params(nd, "shm_%s" % sig) for sig, nd in plan_cpu["nodes"].items()]
check("chaque shard reçoit force_cpu=True",
      all(np.get("force_cpu") is True for np in node_params),
      [np.get("force_cpu") for np in node_params])
asm = cf.build_assembler_params(plan_cpu["outputs"]["1"], "mur_1",
                                {sig: "shm_%s" % sig for sig in plan_cpu["nodes"]})
check("l'assembleur reçoit force_cpu=True", asm.get("force_cpu") is True, asm.get("force_cpu"))
check("la signature du mur CHANGE (c'est ce qui déclenche le rebind des shards existants)",
      cf.decompose_multiview(wall(True))["output_sig"] != ref_sig)

# ── C. Partage ─────────────────────────────────────────────────────────────────────────────────
print("C. deux murs en désaccord ne partagent plus de nœud")
both = cf.plan_fabric({"1": wall(), "2": wall()}, max_cells_per_shard=2)
shared_ctrl = sum(1 for nd in both["nodes"].values() if len(nd["shared_by"]) == 2)
check("CONTRÔLE : deux murs identiques partagent des nœuds", shared_ctrl > 0, shared_ctrl)
mixed = cf.plan_fabric({"1": wall(), "2": wall(True)}, max_cells_per_shard=2)
shared_mix = sum(1 for nd in mixed["nodes"].values() if len(nd["shared_by"]) == 2)
check("un seul en CPU → plus aucun nœud partagé", shared_mix == 0, shared_mix)

# ── D. L'alerte GPU n'accuse pas un CPU demandé ────────────────────────────────────────────────
print("D. « carte réservée mais inutilisée » respecte un CPU DEMANDÉ")
import app.gpu_pool as gpu_pool  # noqa: E402
from app import metrics  # noqa: E402

raised = []
metrics.db_add_alert = lambda *a, **k: raised.append(a[0] if a else k)
gpu_pool.gpu_par_vmid = lambda node_id: {42: 0}          # une carte RÉSERVÉE au vmid 42


def container(force_cpu):
    params = {"type": "multiview", "params": {"force_cpu": True} if force_cpu else {}}
    return {"hostname": "shard-test", "node_id": 1, "deploy_config": json.dumps(params)}


raised.clear()
metrics._verifier_gpu_effectif(42, container(False), {"gpu": False})
check("CONTRÔLE : carte réservée, composition CPU, sans force_cpu → alerte", len(raised) == 1, raised)
raised.clear()
metrics._verifier_gpu_effectif(42, container(True), {"gpu": False})
check("carte réservée, composition CPU, AVEC force_cpu → pas d'alerte", len(raised) == 0, raised)

# ── E. Le rebind à chaud refuse un changement structurel ───────────────────────────────────────
print("E. le rebind à chaud ne prétend pas appliquer force_cpu")
import inspect  # noqa: E402
from app import deploy  # noqa: E402


def shard_params(p):
    return [cf.build_node_params(nd, "shm_X")
            for nd in cf.plan_fabric({"1": p}, max_cells_per_shard=2)["nodes"].values()][0]


base_np = shard_params(wall())
other_np = shard_params(wall(sources=["autre_%d" % i for i in range(6)]))
check("CONTRÔLE : autre région, même structure → envoi à chaud admis",
      deploy._multiview_only_hot_changed(base_np, other_np))
check("force_cpu change → envoi à chaud refusé",
      not deploy._multiview_only_hot_changed(base_np, shard_params(wall(True))))
src = inspect.getsource(deploy._reconcile_fabric_node_impl)
rb = src[src.index("def _rebind("):src.index("_apply_mv(", src.index("def _rebind("))]
check("_rebind consulte la liste structurelle AVANT l'envoi à chaud",
      "if not _multiview_only_hot_changed(_old, params):\n            return False" in rb)

print("\n%d vérifications OK" % n)
