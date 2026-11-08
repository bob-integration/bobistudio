# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""CONSTAT du placement CPU réel des conteneurs d'un nœud — le pendant de `core_pool`.

`core_pool` CALCULE un placement avant de déployer. Ce module CONSTATE ce que le nœud en a fait
ensuite. C'est une séparation délibérée, et c'est la leçon du 2026-08-01 :

    `git log app/core_pool.py` compte NEUF correctifs, chacun rattrapant une qualité du cœur que
    le modèle ignorait — jumeau HyperThreading, nœud NUMA, dédié/partagé, fréquence épinglée,
    conteneur né sans cpuset… Les neuf CALCULENT en amont. **Aucun ne regarde le résultat.**
    La dixième espèce (« le cœur peut être ISOLÉ ») a donc vécu invisible jusqu'à ce qu'un relevé
    manuel d'une ligne la révèle. La onzième vivra pareil — sauf si quelque chose CONSTATE.

Ce module ne calcule aucun placement et ne déplace aucun conteneur : il lit le nœud, applique
deux invariants, et NOMME ce qui les viole. Lecture seule côté nœud.


═══ LES DEUX INVARIANTS ═══

**I1 — ORDONNANÇABILITÉ.** Tout cœur donné à un conteneur doit être un cœur sur lequel
l'ordonnanceur placera vraiment ses threads :

        cpuset ∩ bande_isolée = ∅

    `isolcpus=domain` ne rend pas un cœur inutilisable : il le retire de l'ÉQUILIBRAGE. Un thread
    qu'on y affine explicitement (un lcore DPDK) y tourne ; un thread ordinaire n'y est jamais
    MIGRÉ. Un cpuset large mais majoritairement isolé entasse donc tous les threads sur le reste,
    sans qu'aucun compteur d'allocation ne bronche. Mesuré sur dl360-1 : moteur pinné `0-15`,
    bande isolée `1-21,25-45` → **un seul** cœur ordonnançable, **253 threads dessus**.

**I2 — EXCLUSIVITÉ DU BUSY-POLL.** Aucun conteneur autre que le moteur 2110 ne s'exécute sur la
bande isolée :

        threads_observés(conteneur ≠ moteur) ∩ bande_isolée = ∅

    La bande isolée EST, par construction (`mtl._isolation_cpus`), l'empreinte du moteur : y poser
    un autre conteneur, c'est disputer le CPU à des boucles busy-poll qui ne cèdent jamais la main.
    Un conteneur SANS cpuset viole I2 par construction — il couvre toute la machine.

**I4 — LE CPUSET EST TENU PAR TOUS LES FILS.** Aucun fil d'un conteneur n'est AUTORISÉ hors du
cpuset effectif de son cgroup :

        Cpus_allowed_list(fil) ⊆ cpuset.cpus.effective(cgroup du conteneur)

    Le cpuset du conteneur peut être juste et ses fils le déborder quand même : un processus lancé
    par `docker exec` hérite des cœurs autorisés de SON LANCEUR, pas de ceux du conteneur. Mesuré
    le 2026-11-08 sur dell-1 : trois conteneurs d'outillage à `Cpus_allowed_list 0-47` alors que
    leur cgroup valait `42-45`, et leurs fils VUS sur les cœurs 15 et 18 — ceux du moteur. Le
    constat ne vaut que sur la bonne source : `HostConfig.CpusetCpus` dit ce qui a été DEMANDÉ à la
    création, `cpuset.cpus.effective` ce que le noyau applique. C'est le second qu'on compare.

    ⚠ L'inclusion est à sens unique : un fil fixé PLUS ÉTROIT que son cgroup est normal et
    recherché (les lcores DPDK du moteur, le fil audio de la console). Ne jamais « réparer » en
    élargissant un tel fil — le 2026-11-08, l'épinglage de `mtl_sch`/`dpdk-worker` du moteur 619
    a été cassé ainsi, et il a fallu redémarrer le moteur.

Les trois premiers sont formulés SANS seuil arbitraire : « ce cœur est-il utilisable » et « qui d'autre est
sur la bande » sont des questions binaires. La gravité, elle, dépend de la conséquence (un
conteneur qui garde 12 cœurs sur 16 n'est pas dans l'état de celui qui n'en garde qu'un).

⚠ Ces invariants ne disent RIEN de la CONTENTION (dix conteneurs sur un cœur ordonnançable le
respectent) : c'est le rôle de `core_pool.capacite` (cœurs physiques) et de `cpu_pressure` (PSI).
Ici on ne répond qu'à une question, mais on y répond sûrement : « ce conteneur a-t-il seulement où
s'exécuter, et n'est-il pas en train de piétiner le moteur ? »

⚠ Le relevé porte sur TOUS les conteneurs Docker du nœud, pas sur les lignes de la table
`containers`. C'est volontaire et c'est le cœur du sujet : sur dl360-1, 16 conteneurs sur 17
(réplication RDMA, bancs) sont créés hors du modèle de l'orchestrateur — donc parfaitement
invisibles à `core_pool`, et pourtant bien réels pour l'ordonnanceur du noyau.
"""
import json
import logging
import re
import threading

from . import core_pool
from .database import db_get_node, db_get_nodes, db_add_alert, db_get_containers
from .episodes import EtatEpisodes as _Episodes

log = logging.getLogger(__name__)

_episodes = _Episodes("placement")

# Le relevé fait un exec sur le nœud : throttle par nœud (la surveillance tourne toutes les 5 s).
RELEVE_TTL_S = 120.0
_dernier = {}          # node_id → (monotone, relevé|None)


# ─── Sonde nœud (lecture seule) ──────────────────────────────────────────────
# Exécutée par `python3 -` via l'agent (stdin) : aucune citation shell à échapper, et python3 est
# garanti présent (l'agent-nœud lui-même est en python). Elle ne fait QUE lire : /sys pour la bande
# isolée, `docker ps/inspect` pour les cpusets posés, /proc pour la localisation RÉELLE des threads.
#
# `psr` (champ 39 de /proc/<pid>/task/<tid>/stat) est le dernier cœur sur lequel le thread a tourné.
# Pour un thread endormi c'est un souvenir, pas une position — d'où le principe : la répartition
# observée sert de PREUVE à l'appui d'un défaut structurel (I1/I2), jamais de défaut à elle seule.
_SONDE = r'''
import json, os, re, subprocess

def sh(c):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""

try:
    iso = open("/sys/devices/system/cpu/isolated").read().strip()
except Exception:
    iso = None

conts, ids = {}, []
for ln in sh("docker ps --no-trunc --format '{{.ID}}\t{{.Names}}'").splitlines():
    p = ln.split("\t")
    if len(p) >= 2 and p[0]:
        conts[p[0]] = {"name": p[1], "cpuset": "", "cpu_shares": None, "threads": {},
                       "cg_cpuset": None, "allowed": {}}
        ids.append(p[0])
if ids:
    # Separator is "|": CpusetCpus may be empty, and a split on spaces would then shift fields.
    for ln in sh("docker inspect -f '{{.Id}}|{{.HostConfig.CpuShares}}|{{.HostConfig.CpusetCpus}}' "
                 + " ".join(ids)).splitlines():
        p = ln.split("|")
        if p[0] in conts:
            conts[p[0]]["cpuset"] = p[2].strip() if len(p) > 2 else ""
            try:
                conts[p[0]]["cpu_shares"] = int(p[1].strip())
            except (IndexError, ValueError):
                pass

rx = re.compile(r"[0-9a-f]{64}")
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        cg = open("/proc/%s/cgroup" % pid).read()
        m = rx.search(cg)
    except Exception:
        continue
    if not m or m.group(0) not in conts:
        continue
    cont = conts[m.group(0)]
    th = cont["threads"]
    if cont["cg_cpuset"] is None:
        # `cpuset.cpus.effective` of the container's OWN cgroup = what the KERNEL allows, whereas
        # HostConfig.CpusetCpus is only what was asked at creation. None stays None when the
        # cgroup cannot be read (v1 hierarchy, path gone): not measured is not "nothing to say".
        for ln in cg.splitlines():
            if ln.startswith("0::"):
                try:
                    with open("/sys/fs/cgroup%s/cpuset.cpus.effective" % ln[3:].strip()) as f:
                        cont["cg_cpuset"] = f.read().strip()
                except Exception:
                    pass
                break
    try:
        tids = os.listdir("/proc/%s/task" % pid)
    except Exception:
        continue
    for tid in tids:
        try:
            with open("/proc/%s/task/%s/stat" % (pid, tid)) as f:
                champs = f.read().rsplit(")", 1)[1].split()
            psr = int(champs[36])
        except Exception:
            continue
        th[str(psr)] = th.get(str(psr), 0) + 1
        # Affinity MASK of this thread, grouped by distinct value (two or three at most on a
        # container: cheaper to send than one entry per thread, and it is the mask that is judged).
        try:
            with open("/proc/%s/task/%s/status" % (pid, tid)) as f:
                for l2 in f:
                    if l2.startswith("Cpus_allowed_list:"):
                        a = l2.split(":", 1)[1].strip()
                        cont["allowed"][a] = cont["allowed"].get(a, 0) + 1
                        break
        except Exception:
            pass

print(json.dumps({"isolated": iso, "nproc": os.cpu_count(),
                  "containers": sorted(conts.values(), key=lambda c: c["name"])}))
'''


def relever(node, timeout=45):
    """Relevé BRUT du placement réel sur le nœud : {isolated, nproc, containers:[{name, cpuset,
    cpu_shares, threads:{cpu: n}, cg_cpuset, allowed:{masque: n_fils}}]}. None si le nœud est
    injoignable ou la sonde illisible (jamais de relevé partiel silencieux : c'est tout ou rien,
    et l'appelant le DIT).

    `cg_cpuset` = `cpuset.cpus.effective` du cgroup du conteneur (ce que le NOYAU applique), et
    `allowed` = les masques d'affinité de ses fils, regroupés par valeur. Les deux sont là pour
    I4, et il faut les deux : comparer un masque de fil à `HostConfig.CpusetCpus` comparerait à
    ce qui a été DEMANDÉ à la création, pas à ce qui est appliqué. None / {} = NON MESURÉ (cgroup
    v1, chemin disparu) — I4 s'abstient alors, il ne conclut pas à l'absence de défaut.

    `cpu_shares` = POIDS CPU RÉELLEMENT posé sur le conteneur (0 = « non précisé », donc le défaut
    de Docker). Mesuré ici, et nulle part ailleurs : le poids s'applique à chaud (`docker update`,
    cf. app/cpu_weight.py) et il est volontairement EXCLU de la signature de spec — rien ne le
    rattraperait, et sans mesure un `docker update` manqué serait invisible."""
    from . import node_driver
    try:
        rc, out, err = node_driver.host_exec(node, "python3 -", input_data=_SONDE, timeout=timeout)
    except Exception as e:
        log.warning("placement.relever(%s): %s", (node or {}).get("host"), e)
        return None
    if rc != 0:
        log.warning("placement.relever(%s): sonde rc=%s %s", (node or {}).get("host"),
                    rc, (err or "")[:200])
        return None
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        log.warning("placement.relever(%s): sortie de sonde illisible (%s)",
                    (node or {}).get("host"), e)
        return None


def releve_cache(node_id, ttl=RELEVE_TTL_S, force=False):
    """`relever` throttlé par nœud (la boucle de surveillance passe toutes les 5 s, la sonde coûte
    un exec et un parcours de /proc). `force=True` pour un rafraîchissement demandé par l'UI."""
    import time as _t
    nid = str(node_id)
    hit = _dernier.get(nid)
    if not force and hit and (_t.monotonic() - hit[0]) < ttl:
        return hit[1]
    r = relever(db_get_node(node_id))
    _dernier[nid] = (_t.monotonic(), r)
    return r


# ─── Application des invariants ──────────────────────────────────────────────

# Variante libmxl réellement chargée, par nœud. Cachée longuement : elle ne change qu'au
# redéploiement d'une image ou au remplacement d'un CPU.
_variante_cache = {}
_VARIANTE_TTL_S = 3600.0

_SONDE_VARIANTE = """import ctypes
ctypes.CDLL("libmxl.so.1")
c = sorted({l.split()[-1] for l in open("/proc/self/maps") if "libmxl.so" in l})
print(c[0] if c else "")
"""


def variante_mxl(node_id, force=False):
    """Quelle variante de `libmxl` ce nœud charge-t-il RÉELLEMENT ?
    `{"variante": "x86-64-v3"|"baseline", "path": …, "avx2": bool}` — ou None si indéterminable.

    ★ POURQUOI CONSTATER PLUTÔT QUE DÉDUIRE. L'image embarque deux variantes et c'est l'éditeur de
    liens qui tranche selon le CPU (`glibc-hwcaps`). Le mécanisme est invisible : un basculement
    silencieux sur la baseline coûterait ~20 % sur un régime consommateur (mesuré) sans qu'aucun
    indicateur ne bouge. Et le risque est concret — `bobimxl` liste des chemins ABSOLUS en repli,
    qui court-circuitent hwcaps ; si la résolution par SONAME échouait, tout continuerait de
    tourner, en baseline, partout.

    On exécute donc le chargeur pour de vrai plutôt que de raisonner sur les drapeaux du CPU."""
    import time as _t
    nid = str(node_id)
    hit = _variante_cache.get(nid)
    if not force and hit and (_t.monotonic() - hit[0]) < _VARIANTE_TTL_S:
        return hit[1]
    from . import node_driver
    node = db_get_node(node_id) or {}
    image = (node.get("compute_image") or "").strip()
    res = None
    if image:
        try:
            node_driver.host_exec(node, "cat > /tmp/_mxlvar.py",
                                  input_data=_SONDE_VARIANTE, timeout=30)
            rc, out, _ = node_driver.host_exec(
                node, "docker run --rm -v /tmp/_mxlvar.py:/tmp/v.py --entrypoint python3 %s "
                      "/tmp/v.py 2>/dev/null | tail -1" % image, timeout=120)
            chemin = (out or "").strip()
            if rc == 0 and chemin.endswith(".so.1.1"):
                rc2, flags, _ = node_driver.host_exec(
                    node, "grep -m1 flags /proc/cpuinfo | grep -qw avx2 && echo oui || echo non",
                    timeout=30)
                res = {"path": chemin,
                       "variante": "x86-64-v3" if "glibc-hwcaps" in chemin else "baseline",
                       "avx2": (flags or "").strip() == "oui"}
        except Exception as e:
            log.debug("variante_mxl(%s): %s", node_id, e)
    _variante_cache[nid] = (_t.monotonic(), res)
    return res


def _noms_moteurs(node_id):
    """Noms Docker des conteneurs MOTEUR 2110 du nœud — les SEULS légitimes sur la bande isolée.
    Convention `bobi-mtl-<vmid>` en repli : le relevé voit des conteneurs que la base ignore, et
    l'inverse (une ligne dont le conteneur a disparu) ne coûte qu'un nom inutile dans le set."""
    from .docker_compute import is_mtl_type, _type_of
    noms = set()
    try:
        for c in db_get_containers() or []:
            if c.get("node_id") == node_id and is_mtl_type(_type_of(c)):
                noms.add(c.get("docker_name") or f"bobi-mtl-{c.get('vmid')}")
    except Exception as e:
        log.debug("_noms_moteurs(%s): %s", node_id, e)
    return noms


def constater(node_id, releve=None, force=False):
    """Applique I1 et I2 au relevé du nœud. Renvoie
    `{"ok": bool, "note": str, "isolated": str, "defauts": [{code, conteneur, niveau, message}]}`.

    `ok=False` + `note` = non jugeable (nœud injoignable, sonde illisible). Un nœud non jugeable
    n'est PAS un nœud sain : on ne produit alors aucun défaut, mais on ne prétend pas non plus qu'il
    n'y en a pas."""
    r = releve if releve is not None else releve_cache(node_id, force=force)
    if not r:
        return {"ok": False, "note": "relevé indisponible (nœud injoignable ou sonde illisible)",
                "isolated": "", "defauts": []}
    if r.get("isolated") is None:
        return {"ok": False, "note": "/sys/devices/system/cpu/isolated illisible",
                "isolated": "", "defauts": []}
    iso = core_pool.parse_cpuset(r.get("isolated"))
    nproc = int(r.get("nproc") or 0)
    moteurs = _noms_moteurs(node_id)
    # I3 : qui tient quels cœurs en DÉDIÉ ici. {cœur: vmid} + le nom du propriétaire, pour pouvoir
    # NOMMER l'autre partie — « ce conteneur peut courir sur un cœur dédié » n'est actionnable que
    # si on dit à QUI, sinon l'exploitant ne sait ni quoi déplacer ni quoi protéger.
    _par_vmid = core_pool.allocations_by_vmid(node_id)
    ded_de = {int(c): int(v) for v, cores in _par_vmid.items() for c in cores}
    _noms = {int(c.get("vmid")): (c.get("hostname") or ("#%s" % c.get("vmid")))
             for c in (db_get_containers() or []) if c.get("vmid") is not None}
    # I4 n'est pas mesurable partout : sans contrôleur `cpuset` sur le cgroup (hiérarchie v1,
    # contrôleur non délégué à ce sous-arbre), il n'y a RIEN à comparer. On le PUBLIE au lieu de
    # s'abstenir en silence : un invariant qui ne s'applique nulle part et ne le dit pas se
    # confond avec un invariant toujours satisfait.
    defauts, sans_cpuset, i4_muets = [], [], []
    for c in r.get("containers") or []:
        nom = c.get("name") or "?"
        # Le vmid lu dans le NOM (bobi-cmp-<vmid> / bobi-mtl-<vmid>) : un conteneur ne se reproche
        # pas de pouvoir courir sur SES propres cœurs dédiés.
        _m = re.match(r"^bobi-(?:cmp|mtl)-(\d+)$", nom)
        _soi = int(_m.group(1)) if _m else None
        ded_autres = {c0 for c0, v0 in ded_de.items() if v0 != _soi}
        cpuset = core_pool.parse_cpuset(c.get("cpuset"))
        threads = {int(k): int(v) for k, v in (c.get("threads") or {}).items()}
        total_th = sum(threads.values())
        est_moteur = nom in moteurs or nom.startswith("bobi-mtl-")

        # ── I4 : le cpuset du conteneur est-il tenu par TOUS ses fils ? ───────
        #
        # Indépendant de I1, I2 et I3, et placé AVANT eux pour cette raison : c'est le cas du
        # conteneur dont le cpuset est JUSTE. Placé après, il aurait été sauté par le `continue`
        # qui clôt I1 pour un moteur 2110 sain — or le moteur est précisément le conteneur dans
        # lequel on lance des `docker exec` à la main.
        # Un `docker exec` hérite des cœurs autorisés de son lanceur (l'agent-nœud, donc toute la
        # machine) ; le cgroup est bon, les fils le débordent. Rien dans `core_pool` ne peut le
        # voir — il n'a pas calculé ces fils-là.
        _cg = core_pool.parse_cpuset(c.get("cg_cpuset")) if c.get("cg_cpuset") else set()
        _allowed = {k: int(v) for k, v in (c.get("allowed") or {}).items()}
        if not _cg or not _allowed:
            i4_muets.append(nom)
        else:
            hors = {}                       # cœur hors cgroup → nombre de fils AUTORISÉS dessus
            for _masque, _n in _allowed.items():
                for x in core_pool.parse_cpuset(_masque) - _cg:
                    hors[x] = hors.get(x, 0) + _n
            if hors:
                # Le masque est le DÉFAUT ; les fils VUS dessus en sont la preuve. Gravité par
                # conséquence : déborder sur la bande isolée ou sur le cœur dédié d'un autre est
                # d'une autre nature que déborder sur du pool partagé.
                _vus = sorted(x for x in hors if threads.get(x))
                _grave = sorted(x for x in hors if x in iso or x in ded_autres)
                _nf = max(hors.values())
                _p4 = {"nom": nom, "n_threads": _nf,
                       "fmt_hors": core_pool.fmt_cpuset(sorted(hors)),
                       "fmt_cgroup": core_pool.fmt_cpuset(sorted(_cg)),
                       "n_hors": len(hors)}
                _m4 = (f"{_nf} fil(s) de {nom} sont AUTORISÉS sur "
                       f"{core_pool.fmt_cpuset(sorted(hors))}, hors du cpuset que le noyau applique "
                       f"à son cgroup ({core_pool.fmt_cpuset(sorted(_cg))}) : un processus lancé "
                       f"par `docker exec` hérite des cœurs autorisés de son LANCEUR, pas de ceux "
                       f"du conteneur. Son cpuset est juste ; ce sont ses fils qui le débordent.")
                if _vus:
                    _p4.update({"fmt_vus": core_pool.fmt_cpuset(_vus),
                                "n_vus": sum(threads[x] for x in _vus)})
                    _m4 += (f" Constaté : {sum(threads[x] for x in _vus)} fil(s) VUS sur "
                            f"{core_pool.fmt_cpuset(_vus)}.")
                if _grave:
                    _p4["fmt_grave"] = core_pool.fmt_cpuset(_grave)
                    _m4 += (f" Dont {core_pool.fmt_cpuset(_grave)}, qui appartiennent au moteur "
                            f"2110 ou à une réservation dédiée.")
                _m4 += (" Relancer ces processus en les bornant (`taskset -c`), ou redémarrer le "
                        "conteneur. ⚠ Ne JAMAIS élargir un fil fixé plus étroit que son cgroup : "
                        "c'est l'épinglage voulu (lcores DPDK, fil audio temps réel).")
                # DEUX CODES, pas un niveau qui change : déborder sur du pool partagé et déborder
                # sur la bande isolée ou le cœur dédié d'un autre n'ont ni la même conséquence ni
                # le même remède, et la clé d'épisode est (nœud, conteneur, code) — un code par
                # nature de défaut, c'est aussi ce qui fait que l'aggravation s'ANNONCE.
                _code = "fils_hors_cpuset_reserve" if _grave else "fils_hors_cpuset"
                defauts.append({
                    "code": _code, "conteneur": nom,
                    "niveau": "error" if _grave else "warning", "message": _m4,
                    "msg_key": "alert.resource.placement_%s%s" % (_code, "_vus" if _vus else ""),
                    "msg_params": _p4})

        # ── I1 : tout cœur donné doit être ordonnançable ──────────────────────
        #
        # ⚠ SAUF pour le moteur 2110, et ce n'est pas une exception de complaisance : ses lcores
        # busy-poll DOIVENT être isolés (c'est leur raison d'être) et DOIVENT figurer dans son
        # cpuset (Docker n'y ferait pas tourner ses threads sinon). L'intersection est donc
        # structurellement non vide, et l'invariant tel qu'énoncé ne pouvait JAMAIS être satisfait :
        # il criait au défaut sur la configuration correcte. Ce qui compte pour un moteur n'est pas
        # que l'intersection soit vide, mais qu'il lui reste assez de cœurs ORDONNANÇABLES pour ses
        # threads de service. On compare donc à l'INTENTION — le nombre de cœurs de service que les
        # réglages lui destinent — au lieu d'un idéal qui ne s'applique pas à lui.
        # (Reformulé le 2026-08-02, après que le correctif de la bande isolée a rendu la
        # configuration correcte sans faire taire l'alarme.)
        if cpuset:
            perdus = cpuset & iso
            restants = cpuset - iso
            if est_moteur:
                try:
                    attendus = len(core_pool.engine_service_cpus(n_cpus=nproc, core_of=None))
                except Exception:
                    attendus = 2
                # +1 : le cœur 0 porte le main_lcore EAL et le housekeeping du noyau ; il ne compte
                # pas comme cœur de service utilisable.
                if len(restants) >= max(2, attendus // 2 + 1):
                    continue
            if perdus:
                # Gravité par CONSÉQUENCE, pas par proportion : ce qui compte est ce qu'il RESTE.
                niveau = "error" if len(restants) <= 1 else "warning"
                sur = sorted(threads.items(), key=lambda kv: -kv[1])[:1]
                preuve = ""
                if sur and total_th:
                    preuve = (f" Constaté : {total_th} thread(s) au total, dont {sur[0][1]} "
                              f"sur le seul cœur {sur[0][0]}.")
                base = (f"{nom} est épinglé sur {core_pool.fmt_cpuset(cpuset)} "
                        f"({len(cpuset)} cœur(s)) mais {len(perdus)} d'entre eux "
                        f"({core_pool.fmt_cpuset(perdus)}) sont ISOLÉS du noyau : l'ordonnanceur n'y "
                        f"migre aucun thread. Il ne lui reste que {len(restants)} cœur(s) "
                        f"réellement ordonnançables "
                        f"({'« ' + core_pool.fmt_cpuset(restants) + ' »' if restants else 'AUCUN'})"
                        f".{preuve}")
                # Le moteur à bout de cœurs de service est LE cas dégénéré : ses threads applicatifs
                # (contrôleur, métriques, drain audio st30p) se retrouvent sur le cœur laissé au
                # housekeeping du noyau, pendant que ses lcores DPDK tournent en boucle active juste
                # à côté. On le nomme À PART — un même constat, mais une cause et un remède propres.
                # Canal i18n en plus de "message" (texte FR figé, consommé tel quel par
                # /api/nodes/<id>/placement → JS, cf. app/routes/monitoring_api.py) : PAS touché.
                _mp = {
                    "nom": nom, "cpuset": core_pool.fmt_cpuset(cpuset), "n_cpuset": len(cpuset),
                    "perdus": len(perdus), "fmt_perdus": core_pool.fmt_cpuset(perdus),
                    "restants": len(restants),
                    "fmt_restants": (core_pool.fmt_cpuset(restants) if restants else "AUCUN"),
                }
                _mesure = bool(sur and total_th)
                if _mesure:
                    _mp.update({"total_th": total_th, "top_core": sur[0][0], "top_n": sur[0][1]})
                if est_moteur:
                    defauts.append({
                        "code": "moteur_sans_coeur_de_service", "conteneur": nom, "niveau": "error",
                        "message": base + (
                            f" C'est le moteur 2110 : ses cœurs de SERVICE sont dans la bande "
                            f"isolée. Bande isolée et cpuset moteur dérivent de la MÊME empreinte "
                            f"(`core_pool.engine_cpu_footprint`), l'une pour la soustraire à "
                            f"l'ordonnanceur, l'autre pour y faire tourner des threads ordinaires : "
                            f"les cœurs de service doivent SORTIR de la bande."),
                        "msg_key": ("alert.resource.placement_moteur_sans_coeur_mesure" if _mesure
                                    else "alert.resource.placement_moteur_sans_coeur"),
                        "msg_params": _mp})
                else:
                    defauts.append({"code": "coeurs_isoles_inutilisables", "conteneur": nom,
                                    "niveau": niveau, "message": base,
                                    "msg_key": ("alert.resource.placement_coeurs_isoles_mesure" if _mesure
                                                else "alert.resource.placement_coeurs_isoles"),
                                    "msg_params": _mp})
        # ── I2 : la bande isolée appartient au moteur ─────────────────────────
        # ── I3 : un cœur DÉDIÉ appartient à son propriétaire ──────────────────
        #
        # ⚠ Cette branche était conditionnée à `iso` seul. Un nœud SANS moteur 2110 n'a pas de
        # bande isolée (la Z8, dl360-1 côté console) : un conteneur sans cpuset n'y était donc
        # JAMAIS signalé, alors qu'il peut parfaitement s'installer sur le cœur dédié d'un
        # conteneur temps réel. C'est le trou mesuré le 2026-11-07 (console audio #1323, 19 blocs
        # de 1 ms manqués en dix minutes sur son cœur 23 « dédié », neuf conteneurs sans cpuset
        # sur le nœud). Les deux risques sont de même nature — un cœur promis à quelqu'un — et se
        # jugent ensemble.
        elif iso or ded_autres:
            occupes_iso = sorted(cpu for cpu in threads if cpu in iso)
            occupes_ded = sorted(cpu for cpu in threads if cpu in ded_autres)
            if occupes_ded:
                # CONSTAT, pas risque : ses threads ont été VUS sur un cœur dédié à un autre.
                _prop = sorted({_noms.get(ded_de[c0], "#%s" % ded_de[c0]) for c0 in occupes_ded})
                defauts.append({
                    "code": "sur_coeur_dedie", "conteneur": nom, "niveau": "error",
                    "message": (
                        f"{nom} n'a AUCUN cpuset et {sum(threads[c0] for c0 in occupes_ded)} de ses "
                        f"thread(s) tournent sur les cœurs "
                        f"{core_pool.fmt_cpuset(occupes_ded)}, DÉDIÉS à "
                        f"{', '.join(_prop)}. « Dédié » ne veut alors plus rien dire : le "
                        f"propriétaire perd sa cadence sans qu'aucun compteur ne bronche. Lui "
                        f"donner un cpuset dans le pool partagé du nœud."),
                    "msg_key": "alert.resource.placement_sur_coeur_dedie",
                    "msg_params": {"nom": nom,
                                   "n_threads": sum(threads[c0] for c0 in occupes_ded),
                                   "fmt_ded": core_pool.fmt_cpuset(occupes_ded),
                                   "proprios": ", ".join(_prop)}})
            if occupes_iso:
                defauts.append({
                    "code": "sur_bande_isolee", "conteneur": nom, "niveau": "error",
                    "message": (
                        f"{nom} n'a AUCUN cpuset et {sum(threads[c] for c in occupes_iso)} de ses "
                        f"thread(s) tournent sur les cœurs ISOLÉS "
                        f"{core_pool.fmt_cpuset(occupes_iso)}, réservés aux boucles busy-poll du "
                        f"moteur 2110 (qui ne rendent jamais la main). Lui donner un cpuset dans le "
                        f"pool de calcul du nœud."),
                    "msg_key": "alert.resource.placement_sur_bande_isolee",
                    "msg_params": {"nom": nom,
                                   "n_threads": sum(threads[c] for c in occupes_iso),
                                   "fmt_iso": core_pool.fmt_cpuset(occupes_iso)}})
            elif not occupes_ded:
                sans_cpuset.append(nom)


    # RISQUE (par opposition aux constats ci-dessus) : agrégé en UN défaut de nœud. Quatorze
    # conteneurs RDMA sans cpuset, c'est UNE chose à réparer, pas quatorze alertes — un fil d'alertes
    # qu'on apprend à ignorer coûte plus cher que l'incident (cf. app/episodes.py).
    if sans_cpuset:
        # DEUX risques de même nature, et le message doit dire CELUI QUI EXISTE : un nœud sans
        # moteur 2110 n'a pas de bande isolée, un nœud sans réservation n'a pas de cœur dédié.
        # Parler des « cœurs isolés » sur un nœud qui n'en a aucun, c'est envoyer l'exploitant
        # chercher un moteur qui n'est pas là.
        _ded = core_pool.fmt_cpuset(sorted(ded_de))
        _prop = sorted({_noms.get(v, "#%s" % v) for v in set(ded_de.values())})
        _liste = ", ".join(sorted(sans_cpuset))
        _p = {"n_conteneurs": len(sans_cpuset), "n_iso": len(iso),
              "fmt_iso": core_pool.fmt_cpuset(iso), "liste": _liste,
              "n_ded": len(ded_de), "fmt_ded": _ded, "proprios": ", ".join(_prop)}
        _queue = (" Rien ne les y a encore menés, rien ne les en empêche. Ces conteneurs sont "
                  "créés HORS du modèle de l'orchestrateur — ils sont donc invisibles à "
                  "l'allocateur de cœurs, qui ne peut pas les borner.")
        if iso and ded_de:
            _cle = "alert.resource.placement_sans_cpuset_iso_dedies"
            _msg = (f"{len(sans_cpuset)} conteneur(s) n'ont AUCUN cpuset et peuvent donc "
                    f"s'exécuter sur les {len(iso)} cœur(s) ISOLÉS {core_pool.fmt_cpuset(iso)} du "
                    f"moteur 2110 COMME sur les {len(ded_de)} cœur(s) DÉDIÉS {_ded} de "
                    f"{', '.join(_prop)} : {_liste}.{_queue}")
        elif ded_de:
            _cle = "alert.resource.placement_sans_cpuset_dedies"
            _msg = (f"{len(sans_cpuset)} conteneur(s) n'ont AUCUN cpuset et peuvent donc "
                    f"s'exécuter sur les {len(ded_de)} cœur(s) DÉDIÉS {_ded} de "
                    f"{', '.join(_prop)} : {_liste}.{_queue}")
        else:
            _cle = "alert.resource.placement_sans_cpuset"
            _msg = (f"{len(sans_cpuset)} conteneur(s) n'ont AUCUN cpuset et peuvent donc "
                    f"s'exécuter sur les {len(iso)} cœur(s) ISOLÉS "
                    f"{core_pool.fmt_cpuset(iso)} du moteur 2110 : {_liste}.{_queue}")
        defauts.append({"code": "sans_cpuset", "conteneur": "*", "niveau": "warning",
                        "detail": sorted(sans_cpuset), "message": _msg,
                        "msg_key": _cle, "msg_params": _p})
    # ── Variante libmxl : un CPU capable qui charge la baseline perd ~20 % EN SILENCE ──────
    v = variante_mxl(node_id, force=force)
    if v and v["avx2"] and v["variante"] == "baseline":
        defauts.append({
            "code": "libmxl_baseline_sur_cpu_capable", "conteneur": "*", "niveau": "warning",
            "message": (
                f"Ce nœud a un CPU compatible AVX2 mais charge la variante BASELINE de libmxl "
                f"({v['path']}). L'image en embarque une optimisée dans `glibc-hwcaps/x86-64-v3/` "
                f"que l'éditeur de liens aurait dû préférer. Coût mesuré : ~20 % de CPU en plus "
                f"sur un régime consommateur. Causes possibles : image sans le sous-dossier "
                f"hwcaps, ou chargement par chemin ABSOLU qui court-circuite la sélection."),
            "msg_key": "alert.resource.placement_libmxl_baseline",
            "msg_params": {"path": v["path"]}})
    return {"ok": True, "note": "", "isolated": core_pool.fmt_cpuset(iso), "nproc": nproc,
            "defauts": defauts, "mxl": v, "i4_non_mesure": sorted(i4_muets)}


def verifier(node_id):
    """Constate le placement du nœud et alerte À LA TRANSITION (état persisté, cf. app/episodes.py :
    une alarme edge-triggered qui repart à zéro au redémarrage de l'orchestrateur ré-annonce un
    incident vieux de trois jours comme s'il naissait).

    La clé d'épisode est (nœud, conteneur, code) : un même défaut sur deux conteneurs s'annonce deux
    fois — ce sont deux conteneurs à réparer — mais un défaut qui persiste ne s'annonce qu'une."""
    node = db_get_node(node_id) or {}
    nom_noeud = node.get("name") or node_id
    res = constater(node_id)
    if not res["ok"]:
        return res
    vus = set()
    for d in res["defauts"]:
        cle = (str(node_id), d["conteneur"], d["code"])
        vus.add(cle)
        # L'état mémorisé n'est pas « déjà annoncé » mais CE QU'ON A ANNONCÉ : pour un défaut agrégé,
        # la liste des conteneurs concernés. Sans ça, un 15ᵉ conteneur sans cpuset resterait muet
        # derrière l'alerte des quatorze premiers.
        etat = d["niveau"] + "|" + ",".join(d.get("detail") or [])
        if _episodes.get(cle) == etat:
            continue                                   # déjà annoncé à l'identique → silence
        _episodes.poser(cle, etat)
        if d.get("msg_key"):
            _params = dict(d.get("msg_params") or {})
            _params["n"] = nom_noeud
            db_add_alert(d["msg_key"], d["niveau"], node_id=node_id, kind="resource", params=_params)
        else:
            # Repli défensif (ne devrait plus arriver : les 7 codes de `constater()` posent tous
            # msg_key/msg_params) — jamais un code d'alerte inconnu ne doit rester muet.
            db_add_alert(f"Nœud {nom_noeud} — placement CPU : {d['message']}", d["niveau"],
                         node_id=node_id, kind="resource")
    # Levée : un défaut disparu doit être DIT, sinon l'exploitant ne sait jamais qu'il a réparé.
    # `cles()` rend des clés TEXTE (tuple aplati par le séparateur) → on les redécoupe.
    from .episodes import _SEP
    for txt in _episodes.cles():
        parts = txt.split(_SEP)
        if len(parts) != 3 or parts[0] != str(node_id):
            continue
        cle = (parts[0], parts[1], parts[2])
        if cle in vus:
            continue
        _episodes.retirer(cle)
        db_add_alert("alert.resource.placement_retabli", "info",
                     node_id=node_id, kind="resource",
                     params={"nom_noeud": nom_noeud, "conteneur": parts[1], "code": parts[2]})
    return res


_passe_en_cours = threading.Lock()


def verifier_tous():
    """Passe sur tous les nœuds. Appelée par la boucle de surveillance ; chaque relevé est throttlé
    par `releve_cache` (TTL 120 s), donc le coût réel est d'un exec par nœud toutes les 2 min.

    ⚠ Verrou NON BLOQUANT : la surveillance tourne toutes les 5 s et lance cette passe en thread. Un
    nœud injoignable fait attendre la sonde jusqu'à son timeout — sans ce verrou, les passes
    s'empileraient en threads jusqu'à saturer le processus (le sujet même de ce module est de ne pas
    fabriquer le problème qu'il diagnostique)."""
    if not _passe_en_cours.acquire(blocking=False):
        return
    try:
        from . import node_driver
        for n in db_get_nodes() or []:
            # Un nœud SANS agent (enrôlement jamais abouti) n'a rien à sonder : sans ce filtre, la
            # sonde partait vers une URL vide — « unknown url type: '/v1/host/exec' » toutes les
            # 2 min, 2 588 fois au journal (relevé le 2026-10-11), pour une ligne en attente.
            if not node_driver.has_agent(n):
                continue
            try:
                verifier(n["id"])
            except Exception as e:
                log.warning("placement.verifier(%s): %s", n.get("id"), e)
            # Guarantees (reservation really applied, consumption still covered by the calibration):
            # same pass, same cached measurement, its own one-minute throttle.
            try:
                from . import guarantee_watch
                guarantee_watch.check_node(n["id"])
            except Exception as e:
                log.warning("guarantee_watch.check_node(%s): %s", n.get("id"), e)
        # ⚠ INDISPENSABLE. `EtatEpisodes.flush` est débouncé (30 s) et n'est appelé QUE depuis
        # `poser`/`retirer` : un défaut annoncé puis stable n'est donc JAMAIS écrit sur disque, et
        # un redémarrage de l'orchestrateur le ré-annonce comme s'il naissait — exactement ce que le
        # module d'épisodes existe pour empêcher. Constaté en branchant ce module le 2026-08-01 :
        # deux alertes émises, `episodes_placement.json` absent. La passe de surveillance donne le
        # battement qui manquait ; l'appel est un no-op tant que rien n'est sale.
        _episodes.flush()
        try:
            from . import guarantee_watch
            guarantee_watch.flush()
        except Exception as e:
            log.warning("guarantee_watch.flush: %s", e)
    finally:
        _passe_en_cours.release()
