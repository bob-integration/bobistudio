#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Le JUMEAU HT dans le cpuset : qui le reçoit, à quelle condition, et qui doit le savoir.

★ LE CONSTAT (dell-1, 2026-11-08, horloge stable). Même console audio de 34 tranches à 48/72,
cœur 13 réservé, vérifié FIL PAR FIL : tous les fils de la console sur le 13 → 13 blocs en retard
en dix minutes ; le fil audio FIFO SEUL sur le 13 et les dix fils Python sur le jumeau 57 → 1. La
protection du cœur n'était pas en cause : le voisin gênant était la console ELLE-MÊME. Le plugin
0.4.173 se sépare tout seul dès que son cpuset porte 2 + n processeurs ; il manquait le côté
orchestrateur, qui réserve bien le jumeau sous `physical_cores` mais ne le DONNAIT pas (#1323
tournait avec `23` quand 47 était fermé à tous et inutilisé).

★ CE QUE LA GARDE TIENT VRAIMENT. Le jumeau n'est pas un cadeau gratuit : il n'est sans coût que
parce que `physical_cores` l'a DÉJÀ retiré du pool partagé. Donné à un type qui n'a pas cette
marque, il rasseoit les treize conteneurs partagés juste à côté du fil temps réel — exactement la
contention mesurée, avec en plus une réservation qui a l'air plus grande. D'où la garde centrale
(1c) : le drapeau seul ne suffit pas, et le refus est DIT.

Et un invariant de symétrie, le plus coûteux à retrouver si on le perd : ce qui est POSÉ et ce à
quoi on COMPARE viennent de la même fonction (`core_pool.cpuset_pose`). Deux définitions auraient
divergé au premier manifeste, et la fenêtre des cœurs aurait annoncé « réservation non appliquée »
à vie sur un conteneur pourtant exactement là où on l'a mis — `guarantee_watch` lit ce champ.

Ce qu'elle tient :
1. ★★ les trois réponses de `cpuset_pose` : sans drapeau, avec drapeau + marque, avec drapeau SEUL ;
2. ★ `allocate_cores` pose le jumeau, mais la RÉSERVATION n'en compte pas deux (capacité intacte) ;
3. ★★ la vue des cœurs compare au cpuset POSÉ, pas à la réservation nue (sinon `guarantee_watch`
   crie à vie), et elle dit quand même « non appliqué » au conteneur resté sur son cœur seul ;
4. ★ le panneau du conteneur compare les deux mêmes notions ;
5. ★ une garantie à chaud ne RETIRE pas la marque « cœurs physiques entiers » en passant ;
6. ★★ aucun manifeste du dépôt ne demande le jumeau sans la marque ;
7. une carte DeckLink garde un processeur logique par cœur physique (son contraire).

Hors ligne : base jetable, aucun nœud joignable, topologie HT fabriquée.

MUTATIONS (JUMEAU_MUTATION=<nom>) — chacune doit donner rc=1 :
    pose_ignore_jumeau      `cpuset_pose` ne donne jamais le jumeau
    pose_sans_garde         le jumeau est donné SANS exiger `physical_cores`
    alloc_sans_pose         `allocate_cores` rend la réservation nue
    vue_compare_reserve     la vue compare à la réservation nue (le faux « non appliqué »)
    panneau_compare_reserve le panneau affiche la réservation nue
    garantie_sans_marque    la garantie à chaud n'honore plus `physical_cores`

    ./venv/bin/python tests/verif_jumeau_ht_dans_cpuset.py
"""
import glob
import json
import logging
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_jumeau_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import container_settings, core_pool, docker_compute, etalonnage, guarantee_watch  # noqa: E402,E501
from app import placement                                               # noqa: E402

MUT = os.environ.get("JUMEAU_MUTATION") or ""
MUTATIONS = {
    "pose_ignore_jumeau": ("app/core_pool.py",
                           '    if not res.get("sibling_in_cpuset"):\n'
                           '        return fmt_cpuset(cores)',
                           '    if True:\n'
                           '        return fmt_cpuset(cores)'),
    # ⚠ Celle-ci REFAIT le danger : le jumeau donné alors qu'il est encore offert au partage.
    "pose_sans_garde": ("app/core_pool.py",
                        '    if not res.get("physical_cores"):\n'
                        '        log.warning("cpuset_pose:',
                        '    if False:\n'
                        '        log.warning("cpuset_pose:'),
    "alloc_sans_pose": ("app/core_pool.py",
                        '        out = cpuset_pose(node_id, vmid, out)',
                        '        pass'),
    "vue_compare_reserve": ("app/core_pool.py",
                            '            ligne["coherent"] = (None if not app\n'
                            '                                 else parse_cpuset(app) == '
                            'parse_cpuset(ligne["cpuset_pose"]))',
                            '            ligne["coherent"] = (None if not app '
                            'else parse_cpuset(app) == set(reserve))'),
    "panneau_compare_reserve": ("app/container_settings.py",
                                '        "reserved": (core_pool.cpuset_pose(\n'
                                '            node["id"], vmid, '
                                'core_pool.allocations_by_vmid(node["id"]).get(vmid) or [], '
                                'res=res)\n            if node else ""),',
                                '        "reserved": (core_pool.fmt_cpuset(sorted('
                                'core_pool.allocations_by_vmid(node["id"]).get(vmid) or []))\n'
                                '                     if node else ""),'),
    "garantie_sans_marque": ("app/etalonnage.py",
                             '    cpuset = core_pool.allocate_cores(c["node_id"], c["vmid"], '
                             'coeurs,\n                                      physical=bool('
                             '_res.get("physical_cores")))',
                             '    cpuset = core_pool.allocate_cores(c["node_id"], c["vmid"], '
                             'coeurs)'),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _f, _old, _new = MUTATIONS[MUT]
    with open(os.path.join(RACINE, _f), encoding="utf-8") as _fh:
        _src = _fh.read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) dans %s — la garde est périmée"
              % (MUT, _src.count(_old), _f))
        sys.exit(1)
    _cible = {"app/core_pool.py": core_pool, "app/container_settings.py": container_settings,
              "app/etalonnage.py": etalonnage}[_f]
    exec(compile(_src.replace(_old, _new), _cible.__file__, "exec"),     # noqa: S102
         _cible.__dict__)
    print("MUTATION active :", MUT)

echecs = []
total = [0]


def verifie(ok, titre, detail=""):
    total[0] += 1
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Montage : dl360-1, pool 18-23,42-47, carte HT 18↔42 … 23↔47 ──────────────────────────────
NID = database.db_add_node("dl360-1", "192.0.2.10")
database.db_update_node(NID, compute_cpuset="18-23,42-47")
_CORE_OF = {c: c for c in range(18, 24)}
_CORE_OF.update({c + 24: c for c in range(18, 24)})
core_pool.core_map_cached = lambda nid: dict(_CORE_OF)
core_pool.numa_map_cached = lambda nid: {}
core_pool.isolated_cached = lambda nid: set()
placement.variante_mxl = lambda nid, force=False: None

# Les trois profils qui comptent, servis à la place du registre des plugins (le manifeste de la
# console ne porte pas encore le drapeau : c'est la session Audio qui le posera).
PROFILS = {
    "console_jumeau": {"pin": True, "cores": 1, "physical_cores": True,
                       "sibling_in_cpuset": True},
    "console_nue": {"pin": True, "cores": 1, "physical_cores": True},
    "jumeau_sans_marque": {"pin": True, "cores": 1, "sibling_in_cpuset": True},
    "decklink": {"pin": True, "cores": 2, "physical_cores": True, "node_io": True},
}
docker_compute.effective_resources = lambda t, c: dict(PROFILS.get(t) or {})

CONSOLE, NUE, SANS_MARQUE, CARTE = 1323, 1324, 1325, 1326
for v, t in ((CONSOLE, "console_jumeau"), (NUE, "console_nue"),
             (SANS_MARQUE, "jumeau_sans_marque"), (CARTE, "decklink")):
    database.db_upsert_container_docker(v, "c-%d" % v, NID, "bobi-cmp-%d" % v)
    database.db_update_deploy_config(v, t, {})

print("\n1. ★★ Les trois réponses de `cpuset_pose`")
_pose_nue = core_pool.cpuset_pose(NID, NUE, [23])
verifie(_pose_nue == "23", "1a. sans drapeau → le cœur réservé, rien de plus", _pose_nue)
_pose = core_pool.cpuset_pose(NID, CONSOLE, [23])
verifie(core_pool.parse_cpuset(_pose) == {23, 47},
        "1b. ★★ drapeau + `physical_cores` → le cœur ET son jumeau HT (23,47)", _pose)
_logs = []
_h = logging.Handler()
_h.emit = lambda r: _logs.append(r.getMessage())
core_pool.log.addHandler(_h)
_pose_sm = core_pool.cpuset_pose(NID, SANS_MARQUE, [23])
core_pool.log.removeHandler(_h)
verifie(core_pool.parse_cpuset(_pose_sm) == {23},
        "1c. ★★ drapeau SANS `physical_cores` → REFUSÉ : le jumeau est encore offert au partage, "
        "le donner rassoit les voisins contre le fil temps réel", _pose_sm)
verifie(any("sibling_in_cpuset" in m for m in _logs),
        "1d. ★ … et le refus est DIT (journal), pas silencieux", _logs)
_sans_carte = core_pool.core_map_cached
core_pool.core_map_cached = lambda nid: {}
verifie(core_pool.cpuset_pose(NID, CONSOLE, [23]) == "23",
        "1e. ★ carte HT illisible → aucun jumeau INVENTÉ")
core_pool.core_map_cached = _sans_carte
verifie(core_pool.cpuset_pose(NID, CONSOLE, []) == "",
        "1f. aucune réservation → \"\" (rien à poser)")

print("\n2. ★ `allocate_cores` pose le jumeau sans le FACTURER")
_libre_avant = core_pool.free_dedicated_capacity(NID, None, physical=True)
_cs = core_pool.allocate_cores(NID, CONSOLE, 1, physical=True)
verifie(core_pool.parse_cpuset(_cs) == {23, 47} or len(core_pool.parse_cpuset(_cs)) == 2,
        "2a. ★★ le cpuset rendu porte le cœur et son jumeau", _cs)
_reserve = core_pool.allocations_by_vmid(NID).get(CONSOLE) or []
verifie(len(_reserve) == 1, "2b. ★★ la RÉSERVATION n'en compte qu'un (le jumeau n'est pas une "
        "seconde réservation : la capacité du nœud mentirait)", _reserve)
verifie(core_pool.allocated_for(NID, CONSOLE) == 1,
        "2c. ★ `allocated_for` dit 1 — sinon un redéploiement libérerait/réallouerait en boucle",
        core_pool.allocated_for(NID, CONSOLE))
verifie(core_pool.free_dedicated_capacity(NID, None, physical=True) == _libre_avant - 1,
        "2d. ★ un seul cœur physique consommé dans la capacité",
        (_libre_avant, core_pool.free_dedicated_capacity(NID, None, physical=True)))
verifie(core_pool.allocate_cores(NID, CONSOLE, 1, physical=True) == _cs,
        "2e. idempotent : le redéploiement rend le même cpuset")
_COEUR = _reserve[0]
_JUMEAU = _COEUR + 24

print("\n3. ★★ La vue des cœurs compare au cpuset POSÉ (et le dit)")


def _releve(cpuset_applique):
    return {"isolated": "", "nproc": 48,
            "containers": [{"name": "bobi-cmp-%d" % CONSOLE, "cpuset": cpuset_applique,
                            "cpu_shares": 0, "threads": {}, "cg_cpuset": None, "allowed": {}}]}


placement.releve_cache = lambda nid, ttl=0, force=False: _releve(
    core_pool.fmt_cpuset([_COEUR, _JUMEAU]))
_lignes = {l["vmid"]: l for l in core_pool.vue_coeurs(NID).get("dedies") or []}
_l = _lignes.get(CONSOLE) or {}
verifie(_l.get("coherent") is True,
        "3a. ★★ cpuset appliqué = cœur + jumeau → COHÉRENT (la réservation nue aurait fait crier "
        "`guarantee_watch` à vie)", {k: _l.get(k) for k in ("cpuset_reserve", "cpuset_applique",
                                                            "cpuset_pose", "coherent")})
verifie(core_pool.parse_cpuset(_l.get("cpuset_pose")) == {_COEUR, _JUMEAU},
        "3b. ★ la vue PUBLIE le cpuset attendu (l'écran peut nommer l'écart)", _l.get("cpuset_pose"))
_def = guarantee_watch.defects(NID) or {}
verifie((CONSOLE, "reservation_not_applied") not in _def,
        "3c. ★★ … donc aucun défaut « réservation non appliquée »", sorted(_def))
placement.releve_cache = lambda nid, ttl=0, force=False: _releve(core_pool.fmt_cpuset([_COEUR]))
_l2 = {l["vmid"]: l for l in core_pool.vue_coeurs(NID).get("dedies") or []}.get(CONSOLE) or {}
verifie(_l2.get("coherent") is False,
        "3d. ★★ resté sur son cœur SEUL → non appliqué : la marque est posée à l'allocation, il "
        "faut le redéployer", {k: _l2.get(k) for k in ("cpuset_applique", "cpuset_pose")})

print("\n4. ★ Le panneau du conteneur compare les deux mêmes notions")
placement.releve_cache = lambda nid, ttl=0, force=False: _releve(
    core_pool.fmt_cpuset([_COEUR, _JUMEAU]))
_pan = container_settings.resources_view(CONSOLE)
verifie(_pan is not None and core_pool.parse_cpuset(_pan.get("reserved")) == {_COEUR, _JUMEAU},
        "4a. ★ « réservé » = ce qui est POSÉ (sinon le panneau s'alarme d'un écart inexistant)",
        (_pan or {}).get("reserved"))
verifie(_pan is not None and _pan.get("coherent") is not False,
        "4b. … et il n'affiche aucune incohérence", (_pan or {}).get("coherent"))

print("\n5. ★ Une garantie à chaud ne retire pas la marque en passant")
_vus = {}
_vrai_alloc = core_pool.allocate_cores


def _alloc_espion(nid, vmid, n, prefer_numa=None, physical=False):
    _vus["physical"] = physical
    return _vrai_alloc(nid, vmid, n, prefer_numa=prefer_numa, physical=physical)


core_pool.allocate_cores = _alloc_espion
core_pool.reapply_shared_after_reservation = lambda *a, **k: None


class _Drv:
    @staticmethod
    def host_exec(node, cmd, **kw):
        return 0, "", ""


etalonnage.node_driver = _Drv
core_pool.release_cores(CONSOLE)
_ok, _detail = etalonnage.garantir(CONSOLE, coeurs=1)
core_pool.allocate_cores = _vrai_alloc
verifie(_ok and _vus.get("physical") is True,
        "5a. ★ `garantir` transmet `physical_cores` du manifeste — ne rien transmettre RETIRAIT la "
        "marque, rendant les frères HT au pool partagé en silence", (_ok, _detail, _vus))
verifie(_ok and core_pool.parse_cpuset((_detail or {}).get("cpuset")) == {_COEUR, _JUMEAU},
        "5b. … et le cpuset posé à chaud porte le jumeau", (_detail or {}).get("cpuset"))

print("\n6. ★★ Le contrat du manifeste, sur tout le dépôt")
_fautifs = []
for _man in sorted(glob.glob(os.path.join(RACINE, "plugins", "*", "plugin.json"))):
    try:
        with open(_man, encoding="utf-8") as _fh:
            _res = (json.load(_fh) or {}).get("resources") or {}
    except Exception as _e:                                             # noqa: BLE001
        continue
    if _res.get("sibling_in_cpuset") and not _res.get("physical_cores"):
        _fautifs.append(os.path.basename(os.path.dirname(_man)))
verifie(not _fautifs,
        "6a. ★★ aucun plugin ne demande le jumeau sans `physical_cores` (le drapeau n'est sans "
        "coût QUE parce que la marque a déjà fermé le jumeau)", _fautifs)

print("\n7. Le contraire : une carte DeckLink")
_cs_carte = core_pool.allocate_cores(NID, CARTE, 2, physical=True)
_phys = {_CORE_OF[c] for c in core_pool.parse_cpuset(_cs_carte)}
verifie(len(core_pool.parse_cpuset(_cs_carte)) == len(_phys) == 2,
        "7a. ★ node I/O : UN processeur logique par cœur physique — ses prises ne doivent pas se "
        "disputer entre elles", _cs_carte)

print("\n%d vérification(s), %d échec(s)" % (total[0], len(echecs)))
if echecs:
    print("ÉCHECS :")
    for e in echecs:
        print("  -", e)
sys.exit(1 if echecs else 0)
