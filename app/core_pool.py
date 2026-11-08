# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Allocateur de cœurs CPU par nœud (pinning Docker « compute »).

Modèle calqué sur l'ex-nic_pool (retiré) : un POOL de cœurs déclaré par nœud (`nodes.compute_cpuset`, ex. "8-47") et une
table d'allocation par conteneur (`node_core_alloc`). `allocate_cores` attribue N cœurs LIBRES à un
vmid (IDEMPOTENT : un redéploiement réutilise l'allocation existante) et renvoie la chaîne pour
`docker run --cpuset-cpus` ; `release_cores` libère à la suppression. Permet un vrai pinning Docker
(cœurs dédiés NON chevauchants) — opt-in par type via `resources.pin` au manifeste. Sans pool ou si
cœurs libres insuffisants → None (l'appelant retombe sur le quota `--cpus`).
"""
import logging
import sqlite3
import threading
from .database import get_db, db_get_node, db_add_alert
from . import cpu_weight
from .episodes import EtatEpisodes as _Episodes

log = logging.getLogger(__name__)

# Sérialise lire-les-libres → insérer : sans verrou, deux déploiements concurrents lisaient le
# même ensemble libre et l'INSERT OR REPLACE (PK node_id,core) écrasait silencieusement la ligne
# du premier → deux conteneurs épinglés sur les MÊMES cœurs. Un seul processus orchestrateur
# écrit cette table → un lock in-process suffit ; l'INSERT strict (plus de OR REPLACE) sert de
# ceinture si un autre écrivain apparaissait (échec bruyant plutôt que clobber).
_alloc_lock = threading.Lock()


def parse_cpuset(s):
    """'8-11,20-23' / '8,9,10' / '8-47' → set d'ints. '' / None → set vide."""
    out = set()
    for part in str(s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return out


def fmt_cpuset(cores):
    """set/list d'ints → chaîne compacte '8-11,20' (plages fusionnées)."""
    cs = sorted(set(int(c) for c in cores))
    if not cs:
        return ""
    parts, start, prev = [], cs[0], cs[0]
    for c in cs[1:]:
        if c == prev + 1:
            prev = c
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = c
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _allocated(db, node_id):
    """{core: vmid} déjà alloués sur ce nœud."""
    rows = db.execute("SELECT core, vmid FROM node_core_alloc WHERE node_id=?", (node_id,)).fetchall()
    return {r["core"]: r["vmid"] for r in rows}


def read_cpu_core_map(node):
    """{cpu_logique: cœur_physique} du nœud via `lscpu -p=CPU,CORE` (host_exec) — pour rendre la
    dérivation du compute_cpuset HT-AWARE (deux threads HyperThreading partagent un cœur physique et
    ses unités d'exécution : un compute sur le sibling HT d'un lcore moteur contend le busy-poll DPDK
    malgré des numéros de CPU disjoints). Renvoie {} si indisponible → la dérivation retombe sur le
    modèle plat (contigu)."""
    from . import node_driver
    try:
        rc, out, _ = node_driver.host_exec(node, "lscpu -p=CPU,CORE", timeout=10)
        if rc != 0:
            return {}
    except Exception as e:
        log.warning("read_cpu_core_map(%s): %s", (node or {}).get("host"), e)
        return {}
    m = {}
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = ln.split(",")
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            m[int(p[0])] = int(p[1])
    return m


def read_cpu_numa_map(node):
    """{cpu_logique: nœud_NUMA} du nœud via `lscpu -p=CPU,NODE` (host_exec).

    Sur un bi-socket, la RAM et les périphériques PCIe (dont le GPU) appartiennent à UN socket :
    un conteneur épinglé du mauvais côté paie chaque lecture de shm et chaque upload H2D au prix
    du lien inter-socket. Constaté en prod (Horace, 2026-07-27) : le mur `multiview-vision` était
    épinglé 19-21 (nœud NUMA 0) alors que la T4 et ses 7 shards vivaient sur le nœud 1 — segment
    `inputs` du compositing à 21,9 ms pour 8,4 ms au mur voisin (MOINS de pixels lus), soit 25-37 fps
    au lieu de 50. Même cpuset déplacé sur 40-42 (nœud 1) : `inputs` 11,1 ms, 50 fps, sans rien
    changer d'autre. La topologie NUMA n'est donc PAS un détail d'optimisation, c'est un facteur 2.

    Renvoie {} si indisponible (nœud injoignable / mono-socket sans lscpu) → l'allocateur retombe
    sur le modèle plat historique, en le SIGNALANT (jamais de repli muet)."""
    from . import node_driver
    try:
        rc, out, _ = node_driver.host_exec(node, "lscpu -p=CPU,NODE", timeout=10)
        if rc != 0:
            return {}
    except Exception as e:
        log.warning("read_cpu_numa_map(%s): %s", (node or {}).get("host"), e)
        return {}
    m = {}
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        p = ln.split(",")
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            m[int(p[0])] = int(p[1])
    return m


def read_gpu_numa_map(node):
    """{index_gpu: nœud_NUMA} des GPU NVIDIA du nœud.

    `nvidia-smi` donne le BDF PCI de chaque GPU, le noyau donne son NUMA dans
    `/sys/bus/pci/devices/<bdf>/numa_node`. C'est CE nœud que doit viser le cpuset d'un conteneur
    GPU : le buffer hôte épinglé du multiview (staging H2D) et le canvas sont alloués là où tourne
    le thread qui les touche en premier.
    Renvoie {} si pas de GPU / nvidia-smi absent. Un `numa_node` à -1 (machine mono-socket ou
    firmware muet) est EXCLU : il ne contraint rien."""
    from . import node_driver
    cmd = ("nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null | tr -d ' ' | "
           "while IFS=, read -r i b; do b=$(printf '%s' \"$b\" | tr 'A-Z' 'a-z'); b=${b#????}; "
           "echo \"$i:$(cat /sys/bus/pci/devices/$b/numa_node 2>/dev/null || echo -1)\"; done")
    try:
        rc, out, _ = node_driver.host_exec(node, cmd, timeout=15)
        if rc != 0:
            return {}
    except Exception as e:
        log.warning("read_gpu_numa_map(%s): %s", (node or {}).get("host"), e)
        return {}
    m = {}
    for ln in (out or "").splitlines():
        idx, _, nd = ln.strip().partition(":")
        try:
            i, n = int(idx), int(nd)
        except ValueError:
            continue
        if n >= 0:
            m[i] = n
    return m


def read_isolated_cpus(node):
    """CPU logiques RETIRÉS DE L'ORDONNANCEUR par le cmdline noyau, lus dans
    `/sys/devices/system/cpu/isolated` — la bande **ACTIVE**, pas celle qu'un réglage prétend avoir
    demandée (le cmdline ne prend effet qu'au reboot, cf. `mtl.verifier` / `reboot_needed`).

    ★ POURQUOI core_pool doit la connaître. `isolcpus=domain` ne rend PAS un cœur inutilisable : il
    le retire de l'ÉQUILIBRAGE de charge. Un thread qu'on y affine explicitement (un lcore DPDK)
    y tourne très bien ; un thread ordinaire, lui, n'y est JAMAIS migré — il reste là où il est né.
    Un conteneur dont le cpuset ne contient qu'un seul cœur non isolé voit donc TOUS ses threads
    s'entasser sur ce cœur, sans qu'aucun compteur d'allocation ne s'en aperçoive : le cpuset est
    « large », les cœurs sont « libres », et la machine est à genoux.

    Mesuré sur dl360-1 (2026-08-01) : `isolated = 1-21,25-45`, moteur `bobi-mtl-619` pinné sur
    `0-15` → intersection avec les cœurs ordonnançables = **{0}**, et `ps -L -o psr` sur le moteur
    rend **253 threads sur le seul cœur 0**. Coût mesuré : 38-43 fps par sortie en DPDK contre
    49,7 en AF-XDP, et impossibilité de monter en charge.

    Renvoie un set d'ints ; set VIDE = nœud sans isolation (cas normal), ce qui rend
    `ordonnancables()` équivalent au cpuset — le modèle dégénère proprement. **None = illisible**
    (nœud injoignable) : un marqueur `ISO=` préfixe la valeur pour qu'une sortie vide due à un exec
    en échec ne se lise pas « rien n'est isolé »."""
    from . import node_driver
    try:
        rc, out, _ = node_driver.host_exec(
            node, "printf 'ISO='; cat /sys/devices/system/cpu/isolated 2>/dev/null; echo", timeout=10)
        if rc != 0:
            return None
    except Exception as e:
        log.warning("read_isolated_cpus(%s): %s", (node or {}).get("host"), e)
        return None
    for ln in (out or "").splitlines():
        if ln.startswith("ISO="):
            return parse_cpuset(ln[4:].strip())
    return None


def ordonnancables(node_id, cpuset):
    """Cœurs de `cpuset` sur lesquels l'ordonnanceur du nœud placera VRAIMENT des threads
    ordinaires = cpuset − bande isolée (cf. `read_isolated_cpus` pour le mécanisme exact).

    C'est LE compte qui manquait aux neuf correctifs de ce module : ils comptent des cœurs
    ATTRIBUÉS, celui-ci compte des cœurs UTILISABLES. Sur un nœud sans isolation les deux sont
    égaux — la distinction ne coûte rien là où elle ne s'applique pas.

    ⚠ Ne dit RIEN de la contention : un cœur ordonnançable peut être disputé par dix conteneurs.
    C'est le rôle de `capacite` (cœurs physiques) et de `cpu_pressure` (PSI). Ici on ne répond qu'à
    une question, mais on y répond sûrement : « ce conteneur a-t-il seulement où s'exécuter ? »"""
    cores = parse_cpuset(cpuset) if not isinstance(cpuset, (set, frozenset)) else set(cpuset)
    iso = isolated_cached(node_id)
    return cores if iso is None else (cores - iso)


def _reglages_moteur():
    """(base, cap, svc) des réglages MTL. Lecture UNIQUE : `engine_cpu_footprint` et
    `engine_service_cpus` doivent partir des mêmes valeurs, sinon la bande isolée et les cœurs de
    service se calculent sur des hypothèses différentes — c'est exactement le défaut corrigé ici."""
    from . import settings as st
    try:
        base = max(1, int(st.get("mtl_lcore_base") or 1))
        cap  = max(2, int(st.get("mtl_lcore_max")  or 16))
    except (TypeError, ValueError):
        base, cap = 1, 16
    try:
        svc = max(1, int(st.get("mtl_service_cores") or 1))
    except (TypeError, ValueError):
        svc = 1
    return base, cap, svc


def engine_service_cpus(n_cpus=None, core_of=None):
    """CPU de SERVICE du moteur : la queue de l'empreinte, celle où tournent des threads ORDINAIRES.

    `docker_driver` les pose en `ctrl = [max(lcore) + i for i in 1..svc+1]` — soit les (svc+1)
    derniers CPU de l'empreinte. Ils sont dans le cpuset du moteur pour accueillir ses threads de
    contrôle, de métriques et d'écriture MXL.

    ⚠ Ils ne doivent SURTOUT PAS être isolés. Constaté sur dl360-1 le 2026-08-02 : la bande isolée
    étant dérivée de l'empreinte ENTIÈRE, les cœurs de service s'y retrouvaient — l'ordonnanceur n'y
    migre alors aucun thread, et les 277 threads de service du moteur se repliaient TOUS sur le seul
    CPU ordonnançable de son cpuset, le cœur 0, partagé avec le housekeeping du noyau (54,8 %). Le
    mécanisme censé décharger le cœur 0 le remplissait.
    """
    base, cap, svc = _reglages_moteur()
    hi = base + cap + 1 + (svc - 1)
    cpus = set(range(base + cap, hi + 1))                        # les (svc+1) derniers
    if core_of:
        phys = {core_of[c] for c in cpus if c in core_of}
        cpus |= {c for c, k in core_of.items() if k in phys}     # + jumeaux HT
    if n_cpus:
        cpus = {c for c in cpus if 0 <= c < int(n_cpus)}
    return cpus


def engine_cpu_footprint(n_cpus=None, core_of=None):
    """**SOURCE DE VÉRITÉ UNIQUE** de l'empreinte CPU du moteur 2110 sur un nœud.

    Tout ce qui doit « éviter le moteur » DOIT passer par ici — le pool de calcul
    (`derive_compute_cpuset`, qui en prend le complément) ET l'isolation noyau
    (`mtl._isolation_cpus`, qui en prend la bande à isoler). Deux calculs séparés DIVERGENT :
    l'implémentation initiale de l'isolation rendait une bande PLATE (`1-18`) en la déclarant
    « identique à derive_compute_cpuset » alors que celui-ci était déjà HT-aware → les siblings
    HT des lcores (49-66 sur un nœud 96 CPU) restaient hors bande, donc en housekeeping, et
    l'unité IRQ leur ENVOYAIT les interruptions : le noyau tournait sur les jumeaux physiques
    des cœurs busy-poll (contention HT documentée comme piste morte).

    Empreinte = CPU logiques `0 .. base+cap+1+extra` (docker_driver `_auto_lcores` + pinning) :
    base=`mtl_lcore_base` (défaut 1), cap=`mtl_lcore_max` (défaut 16), `extra` = cœurs de service
    SUPPLÉMENTAIRES (`mtl_service_cores` − 1, cf. docker_driver._build_run_cmd). Bornée par `cap`
    → stable même quand le moteur redimensionne. `core_of` {cpu logique: cœur physique} (cf.
    `read_cpu_core_map`) → HT-AWARE : ajoute TOUS les threads siblings des cœurs physiques touchés.
    `n_cpus` (si connu) borne l'empreinte aux CPU RÉELLEMENT présents.

    Renvoie `(cpus:set[int], ht_aware:bool)`. `ht_aware=False` = carte de topologie absente →
    modèle plat : l'appelant DOIT le signaler (repli muet = échec silencieux)."""
    base, cap, svc = _reglages_moteur()
    extra = svc - 1                                              # 0 si réglage par défaut (1)
    hi = base + cap + 1 + extra
    # 0 inclus : MTL préfixe TOUJOURS son main_lcore (=0) à la liste EAL, et le cpuset Docker du
    # moteur le contient (docker_driver). En dessous de `base` (réglage inhabituel) les cœurs sont
    # comptés dans l'empreinte comme le faisait déjà le modèle plat historique.
    cpus = set(range(0, hi + 1))
    ht_aware = False
    if core_of:
        phys = {core_of[c] for c in cpus if c in core_of}
        cpus |= {c for c, k in core_of.items() if k in phys}
        ht_aware = True
    if n_cpus:
        cpus = {c for c in cpus if 0 <= c < int(n_cpus)}
    return cpus, ht_aware


def derive_compute_cpuset(n_cpus, core_of=None):
    """CPU logiques COMPUTE d'un nœud = (tous) − (empreinte MAX du moteur 2110, cf.
    `engine_cpu_footprint` = LA source de vérité). `core_of` {cpu:cœur} (via read_cpu_core_map) →
    HT-AWARE : exclut AUSSI les threads siblings des cœurs moteur (sinon contention HT).
    None → modèle plat (contigu, ancien).
    Renvoie une chaîne --cpuset-cpus (ex. "19-23,43-47"), ou "" si aucun cœur libre. Pure (pas de DB)."""
    if not n_cpus:
        return ""
    n = int(n_cpus)
    moteur, _ht = engine_cpu_footprint(n_cpus=n, core_of=core_of)
    pool = [c for c in range(n) if c not in moteur]
    return fmt_cpuset(sorted(pool)) if pool else ""


def pool_par_defaut(node, n_cpus, core_of=None, isoles=None):
    """Pool de cœurs à poser sur un nœud qui n'a AUCUN réglage opérateur.

    Deux natures de nœud, deux dérivations — les confondre gaspille la moitié d'une machine :

    · nœud portant la capacité **io2110** : le pool est le COMPLÉMENT de l'empreinte du moteur
      (`derive_compute_cpuset`), pour qu'aucun conteneur de calcul ne vienne flotter sur les lcores
      busy-poll ;
    · nœud de calcul PUR (pas d'io2110) : aucun moteur n'y tournera jamais, donc rien à éviter — le
      pool est la machine ENTIÈRE, moins la bande isolée du noyau (des CPU isolés ne sont pas
      ordonnançables : les distribuer reviendrait à promettre des cœurs qui n'exécuteront rien).

    Constaté le 2026-08-02 sur dl-380 (56 CPU, capacités compute/media/gpu) : la dérivation moteur
    rendait `18-27,46-55`, soit 20 CPU sur 56 — 36 CPU réservés à un moteur qui n'existera pas.
    """
    caps = node.get("capabilities") or "[]"
    try:
        import json as _json
        caps = _json.loads(caps) if isinstance(caps, str) else list(caps)
    except (TypeError, ValueError):
        caps = []
    if "io2110" in caps:
        return derive_compute_cpuset(n_cpus, core_of=core_of)
    if not n_cpus:
        return ""
    iso = set(isoles if isoles is not None else (read_isolated_cpus(node) or set()))
    # CPU 0 EXCLU : c'est le cœur de service du noyau (timers, IRQ non dirigées, tâches
    # housekeeping). Y clouer un conteneur média, c'est le mettre en concurrence avec le système
    # sur le seul cœur qu'on ne peut pas décharger. Le premier essai sur dl-380 a attribué
    # exactement celui-là, faute de l'avoir exclu (2026-08-02).
    iso.add(0)
    pool = [c for c in range(int(n_cpus)) if c not in iso]
    return fmt_cpuset(sorted(pool)) if pool else ""


def ensure_compute_cpuset(node_id, n_cpus, core_of=None):
    """Pose `nodes.compute_cpuset` auto-dérivé (derive_compute_cpuset) SI absent — pour que les
    conteneurs compute soient pinnés HORS des lcores busy-poll du moteur 2110 (anti-collision :
    sans pool, un compute non-pinné flotte sur les cœurs du moteur). `core_of` → dérivation HT-aware.
    IDEMPOTENT : ne touche JAMAIS une valeur déjà définie (réglage opérateur respecté)."""
    node = db_get_node(node_id)
    if not node:
        return ""
    cur = (node.get("compute_cpuset") or "").strip()
    if cur:
        return cur                                 # déjà défini (auto ou manuel) → ne pas écraser
    cpuset = pool_par_defaut(node, n_cpus, core_of=core_of)
    if not cpuset:
        return ""
    from .database import db_update_node, db_set_node_setting
    db_update_node(node_id, compute_cpuset=cpuset)
    # Mémorise la valeur AUTO-dérivée. Elle permet plus tard de savoir si le pool courant est encore
    # celui qu'on a calculé (donc re-dérivable) ou s'il a été retouché par un opérateur (intouchable).
    try:
        db_set_node_setting(node_id, "compute_cpuset_auto", cpuset)
    except Exception as e:
        log.debug("ensure_compute_cpuset: mémo auto %s: %s", node_id, e)
    _cle = "alert.prep.isolation_derivee_ht" if core_of else "alert.prep.isolation_derivee"
    db_add_alert(_cle, "info", node_id=node_id, kind="prep",
                 params={"n": (node.get("name") or node_id), "cpuset": cpuset, "n_cpus": int(n_cpus)})
    return cpuset


def rederiver_si_auto(node_id, n_cpus, core_of=None):
    """Re-dérive le pool quand la NATURE du nœud a changé — et seulement s'il est resté auto.

    Le piège que ça ferme : `ensure_compute_cpuset` est idempotent et n'écrase JAMAIS. Un nœud
    enrôlé en calcul pur reçoit donc la machine entière ; si on lui ajoute ensuite la capacité
    `io2110`, son pool continue de couvrir les lcores busy-poll du moteur — et des conteneurs de
    calcul viendront s'y poser. Silencieusement.

    Ne touche rien si l'opérateur a modifié le pool à la main (la valeur en base diffère du mémo
    `compute_cpuset_auto`) : un réglage humain prime toujours sur une dérivation.
    """
    from .database import db_update_node, db_get_node_setting, db_set_node_setting
    node = db_get_node(node_id)
    if not node:
        return ""
    cur = (node.get("compute_cpuset") or "").strip()
    memo = (db_get_node_setting(node_id, "compute_cpuset_auto", "") or "").strip()
    if cur and cur != memo:
        return cur                                  # réglage opérateur → intouchable
    neuf = pool_par_defaut(node, n_cpus, core_of=core_of)
    if not neuf or neuf == cur:
        return cur
    db_update_node(node_id, compute_cpuset=neuf)
    db_set_node_setting(node_id, "compute_cpuset_auto", neuf)
    db_add_alert("alert.prep.pool_coeurs_rederive", "info",
                 node_id=node_id, kind="prep",
                 params={"n": node.get("name") or node_id, "cur": cur or "—", "neuf": neuf})
    return neuf


def _choisir_numa(free, n, numa_of, prefer_numa, prio=None, poids=None):
    """Choisit `n` cœurs libres DANS UN SEUL nœud NUMA (cf. `read_cpu_numa_map` pour le pourquoi).

    Ordre de préférence : (1) `prefer_numa` (le socket du GPU du conteneur) s'il a la place ;
    (2) à défaut, le nœud NUMA qui a le PLUS de cœurs libres — un conteneur cohérent sur le mauvais
    socket vaut toujours mieux qu'un conteneur à cheval sur les deux ; (3) en dernier recours, le
    premier-servi historique à cheval, avec ALERTE (le repli muet est l'anti-patron à ne pas
    reproduire : personne ne relierait jamais un mur à 30 fps à un cpuset réparti sur deux sockets).
    `prio` : sous-ensemble de `free` à servir EN PREMIER — les cœurs dont le sibling HyperThreading
    est lui aussi libre. Sans ça, viser le bon socket ferait basculer un conteneur de cœurs
    PHYSIQUES libres (mauvais socket) vers des siblings HT de cœurs déjà pris (bon socket) : on
    échangerait une pénalité inter-socket contre une contention HT, et « dédié » cesserait de vouloir
    dire dédié. Un nœud NUMA n'est donc candidat de plein droit que s'il offre `n` cœurs
    PHYSIQUEMENT libres ; à défaut on retombe sur ses cœurs libres tout court.
    `poids(core)` : COÛT de prendre ce cœur quand il n'est pas physiquement libre — la charge
    mesurée du conteneur qui en tient le sibling. Sert d'ORDRE DE REPLI. Sans lui, le repli suivait
    l'ordre NUMÉRIQUE, ce qui est un tirage au sort : sur dl360-1 (6 cœurs physiques, un mur et
    deux shards à 3 cœurs), le second shard se collait sur les jumeaux du PREMIER SHARD (42-44 <
    45-47) au lieu de ceux de l'assembleur, pourtant deux fois plus léger. Les deux shards
    tombaient à 30 fps pour 50 — le mur, lui, affichait 50 fps nominal, si bien que rien ne le
    montrait : ses tuiles ne se rafraîchissaient qu'une trame sur trois (stuttering constaté en
    production le 2026-08-07). Quand la collision est INÉVITABLE, on choisit donc avec QUI.
    Renvoie (cœurs, motif) — `motif` (dict `{"cle": ..., "params": {...}}`, ou None) = repli à
    signaler par l'appelant. Clé i18n choisie ICI (3 cas fixes) plutôt qu'une phrase toute faite :
    l'appelant n'a que le vmid/nœud à ajouter aux params."""
    if not numa_of:
        return free[:n], {"cle": "alert.resource.pinning_topologie_plate", "params": {"ncores": n}}
    prio = set(prio or ())
    par_numa, par_numa_prio = {}, {}
    for c in free:
        nd = numa_of.get(c)
        if nd is None:
            continue
        par_numa.setdefault(nd, []).append(c)
        if c in prio:
            par_numa_prio.setdefault(nd, []).append(c)

    _p = poids or (lambda c: 0)

    def _prendre(nd):
        """`n` cœurs du nœud NUMA `nd` : physiquement libres d'abord, puis — à défaut — ceux dont
        le voisin HT est le MOINS chargé (cf. `poids`), et non les plus petits numéros."""
        cs = sorted(par_numa_prio.get(nd, ()))
        cs += sorted(set(par_numa[nd]) - prio, key=lambda c: (_p(c), c))
        return cs[:n]

    if prefer_numa is not None and len(par_numa_prio.get(prefer_numa, ())) >= n:
        return _prendre(prefer_numa), ""
    # Le socket visé n'a plus de cœur PHYSIQUE libre : un autre socket qui, lui, en a, est
    # préférable — sinon on paie la contention HT en plus de rien gagner.
    candidats = sorted((nd for nd, cs in par_numa_prio.items() if len(cs) >= n),
                       key=lambda nd: (-len(par_numa_prio[nd]), nd))
    if not candidats and prefer_numa is not None and len(par_numa.get(prefer_numa, ())) >= n:
        return _prendre(prefer_numa), ""      # plus rien de physique nulle part → au moins local
    if not candidats:
        candidats = sorted((nd for nd, cs in par_numa.items() if len(cs) >= n),
                           key=lambda nd: (-len(par_numa[nd]), nd))
    if candidats:
        nd = candidats[0]
        if nd == prefer_numa:
            return _prendre(nd), None
        motif = None
        if prefer_numa is not None:
            motif = {"cle": "alert.resource.pinning_repli_numa",
                     "params": {"prefer_numa": prefer_numa, "ncores": n, "nd": nd}}
        return _prendre(nd), motif
    return free[:n], {"cle": "alert.resource.pinning_a_cheval", "params": {"ncores": n}}


def _score_placement(cores, numa_of, prefer_numa):
    """Qualité d'un placement, du meilleur au pire :
      0 = d'un seul tenant SUR le socket visé (celui du GPU / des entrées) ;
      1 = à cheval, MAIS touchant le socket visé ;
      2 = d'un seul tenant, entièrement À CÔTÉ du socket visé ;
      3 = à cheval ET entièrement à côté.

    **TOUCHER LE BON SOCKET PRIME SUR LA COHÉRENCE DU CPUSET** — c'est contre-intuitif et c'est
    mesuré. Horace, 2026-07-27 : `multiview-eqts` sur 22-24 (à cheval : 22-23 sur le node 0,
    24 sur le node 1 où vivent la T4 et les shards) tenait `inputs` à **8,4 ms** ; `multiview-vision`
    sur 19-21 (node 0 d'un seul tenant, entièrement du mauvais côté) était à **21,9 ms** et 25-37 fps.
    Le mécanisme est l'allocation *first-touch* : le thread principal de 361 tournant sur le cpu 24,
    54 030 de ses pages ont atterri sur le node 1 contre 26 828 sur le node 0 — son working set est
    du bon côté du bus malgré un cpuset incohérent.

    La version précédente notait « à cheval » PIRE que « d'un seul tenant, mauvais socket ». Elle a
    donc déplacé 361 de 22-24 vers 19-21 en le croyant corrigé : `inputs` 8,4 → 16,5 ms. Le barème
    validait le placement exact qui bridait 362.

    Réserve honnête : le gain d'un cpuset à cheval REPOSE sur le first-touch, il n'est pas garanti
    (rien n'oblige le thread chaud à se réveiller du bon côté). C'est pourquoi le score 1 ne sert
    qu'à NE PAS DÉGRADER un placement existant ; l'allocation neuve, elle, ne vise jamais le score 1
    (cf. `_choisir_numa`) : elle cherche le score 0, et 1 n'est qu'un état de fait toléré.

    Sert à ne RE-placer un conteneur que si c'est STRICTEMENT mieux — un cpuset qui change sans
    gain est du churn : il recrée le conteneur pour rien."""
    if not numa_of or not cores:
        return 0                     # topologie inconnue : aucun jugement possible → ne rien bouger
    nds = {numa_of.get(c) for c in cores}
    entier = len(nds) == 1 and None not in nds
    if prefer_numa is None:
        # Sans socket visé, seule la cohérence se juge (mêmes rangs relatifs qu'avant).
        return 0 if entier else 3
    touche = prefer_numa in nds
    if entier:
        return 0 if touche else 2
    return 1 if touche else 3


def _libelle_placement(cores, numa_of, prefer_numa):
    """Décrit un placement EN DISANT OÙ IL EST RÉELLEMENT, jamais où on aurait voulu qu'il soit.

    L'ancienne alerte annonçait « cœurs d'un seul nœud NUMA, celui du GPU » sans regarder le
    résultat : quand le correctif retombait sur le socket opposé, elle affirmait le contraire de
    ce qu'elle venait de faire. Une alerte qui ment est pire que pas d'alerte — l'exploitant clôt
    l'incident sur sa foi."""
    if not numa_of or not cores:
        return "topologie NUMA illisible"
    nds = sorted({numa_of.get(c) for c in cores if numa_of.get(c) is not None})
    inconnus = any(numa_of.get(c) is None for c in cores)
    if not nds:
        return "nœud NUMA inconnu"
    ou = f"nœud NUMA {nds[0]}" if len(nds) == 1 else \
         "à cheval sur les nœuds NUMA " + " et ".join(str(x) for x in nds)
    if inconnus:
        ou += " (+ cœurs de nœud inconnu)"
    if prefer_numa is None:
        return ou
    if nds == [prefer_numa]:
        return f"{ou} — celui visé"
    if prefer_numa in nds:
        return f"{ou} — dont celui visé ({prefer_numa})"
    return f"{ou} — SANS le socket visé ({prefer_numa})"


def _resources_of(vmid):
    """Effective `resources` profile of this container (manifest + its own settings), or {}.

    Lazy import: `docker_compute` imports this module, and `vue_coeurs` already reads the profile
    the same way. {} on any failure — a type whose profile cannot be read must behave like a type
    that asks for nothing, never like one that asks for everything."""
    try:
        from .database import db_get_container
        from .docker_compute import effective_resources, _type_of
        c = db_get_container(vmid) or {}
        return effective_resources(_type_of(c), c) or {}
    except Exception as e:                      # noqa: BLE001
        log.debug("_resources_of(%s): %s", vmid, e)
        return {}


def cpuset_pose(node_id, vmid, cores, res=None):
    """The cpuset to SET on this container for its reservation `cores`: those cores, plus their HT
    SIBLING when the type asks for it (`resources.sibling_in_cpuset`). → the `--cpuset-cpus` string.

    ★ ONE POLICY POINT, used by BOTH SIDES: `allocate_cores` (what is set) and `vue_coeurs` (what
    the node is measured against). Two definitions would diverge at the first manifest, and the
    cores window would then announce `reservation_not_applied` forever on a container that is in
    fact exactly where it was put — `guarantee_watch` reads that very field.

    ★ WHY A TYPE WOULD ASK FOR THE SIBLING (audio console, measured on dell-1 the 2026-11-08, on a
    node whose clock is steady). The console's engine holds a real-time block of 0.67 to 1 ms on
    its dedicated core; its ten Python threads (plugin hosts, control) are ordinary. Same console
    of 34 slices at 48/72, dedicated core 13, verified THREAD BY THREAD: every thread on core 13 →
    13 late blocks / 10 min; the FIFO audio thread ALONE on 13 and the Python threads on sibling 57
    → 1. The protection of the core was never in question — the neighbour was the console ITSELF.
    The plugin splits on its own as soon as its cpuset holds 2 + n CPUs
    (`plugin_host.setup_plugin_cores`: first = audio thread alone, second = console, rest = hosts);
    the orchestrator only has to hand it the sibling, which `physical_cores` has ALREADY taken out
    of the shared pool — so this costs the node nothing it had not already paid.

    ⚠ The sibling is given only to a type that ALSO carries `physical_cores`. Without it the
    sibling is still offered to the shared pool, and handing it over would seat the thirteen shared
    containers right beside the real-time thread — the very contention measured above, with the
    reservation LOOKING bigger. Refused and NAMED, never silently granted.

    ⚠ NOT for a DeckLink card (cf. `allocate_cores`): node I/O takes ONE logical CPU per physical
    core, so that its own capture threads never contend with each other."""
    cores = sorted(parse_cpuset(cores) if isinstance(cores, str) else {int(c) for c in (cores or [])})
    if not cores:
        return ""
    res = _resources_of(vmid) if res is None else (res or {})
    if not res.get("sibling_in_cpuset"):
        return fmt_cpuset(cores)
    if not res.get("physical_cores"):
        log.warning("cpuset_pose: vmid %s asks for its HT sibling (`sibling_in_cpuset`) WITHOUT "
                    "`physical_cores` — refused: the sibling is still in the shared pool, giving "
                    "it would put the neighbours back beside the real-time thread", vmid)
        return fmt_cpuset(cores)
    core_of = core_map_cached(node_id)
    if not core_of:
        return fmt_cpuset(cores)         # HT map unreadable: no sibling is invented
    phys = {core_of[c] for c in cores if c in core_of}
    return fmt_cpuset(sorted(set(cores) | {c for c, ph in core_of.items() if ph in phys}))


def allocate_cores(node_id, vmid, n, prefer_numa=None, physical=False):
    """`_allocate_cores`, plus the WHOLE-PHYSICAL-CORE mark of the reservation.

    `physical=True` (node I/O, `resources.physical_cores` of the manifest — a DeckLink card): the
    HT siblings of the cores are kept out of the shared pool as well (`partage_disponible`), so
    nothing at all runs beside the card's threads. Dedicated containers never got them anyway (the
    strict HT rule of 30/10); this closes the shared pool, whatever `pin_reserve_physical` says.
    The mark is written on whatever the allocation returns, existing or new, so switching a
    container to or from node I/O takes effect at its next deployment. A physical reservation also
    takes ONE logical CPU per physical core: two siblings of the same core would make the card
    contend with itself.

    ⚠ The RESERVATION (`node_core_alloc`) holds one logical CPU per physical core; the cpuset
    RETURNED can be wider, because a type may ask for its HT sibling (`cpuset_pose`). The two must
    not be confused: the reservation is what capacity is counted on, the cpuset is what is set."""
    out = _allocate_cores(node_id, vmid, n, prefer_numa=prefer_numa, physical=physical)
    if out:
        try:
            with get_db() as db:
                db.execute("UPDATE node_core_alloc SET physical=? WHERE node_id=? AND vmid=?",
                           (1 if physical else 0, node_id, vmid))
        except Exception as e:
            log.warning("allocate_cores: physical mark for vmid %s on node %s: %s", vmid, node_id, e)
        out = cpuset_pose(node_id, vmid, out)
    return out


def physical_siblings(node_id, exclude_vmid=None, core_of=None):
    """Physical cores held WHOLE by a `physical` reservation (other than `exclude_vmid`): no shared
    container may run on any of their logical CPUs. Empty without an HT map (flat model)."""
    core_of = core_map_cached(node_id) if core_of is None else core_of
    if not core_of:
        return set()
    try:
        with get_db() as db:
            rows = db.execute("SELECT core, vmid FROM node_core_alloc WHERE node_id=? AND physical=1",
                              (node_id,)).fetchall()
    except Exception as e:
        log.warning("physical_siblings node=%s: %s", node_id, e)
        return set()
    return {core_of[r["core"]] for r in rows if r["vmid"] != exclude_vmid and r["core"] in core_of}


def physical_allocations(node_id):
    """{vmid: [cores]} of the WHOLE-physical-core reservations of this node (node I/O)."""
    out = {}
    try:
        with get_db() as db:
            for r in db.execute("SELECT core, vmid FROM node_core_alloc WHERE node_id=? AND physical=1",
                                (node_id,)).fetchall():
                out.setdefault(r["vmid"], []).append(r["core"])
    except Exception as e:
        log.warning("physical_allocations node=%s: %s", node_id, e)
    return {v: sorted(c) for v, c in out.items()}


def _one_per_phys(cands, core_of, keep=()):
    """One logical CPU per physical core among `cands` (sorted), preferring those in `keep` (the
    container's current cores, so a redeployment does not shuffle them)."""
    keep = set(keep)
    chosen = {}
    for c in sorted(cands, key=lambda c: (c not in keep, c)):
        chosen.setdefault(core_of.get(c, ("flat", c)), c)
    return sorted(chosen.values())


def _allocate_cores(node_id, vmid, n, prefer_numa=None, physical=False):
    """Attribue `n` cœurs LIBRES du pool du nœud au `vmid`. IDEMPOTENT : si le vmid a déjà des cœurs
    sur ce nœud → les renvoie tels quels (pas de ré-allocation au redéploiement). Renvoie la chaîne
    `--cpuset-cpus` (ex. "8-11"), ou None si pas de pool / cœurs libres insuffisants (+ alerte).

    `prefer_numa` : nœud NUMA visé (typiquement celui du GPU alloué, cf. `numa_of_gpu`). Les cœurs
    rendus sont TOUJOURS d'un seul nœud NUMA quand la topologie le permet — cf. `_choisir_numa`.

    ★ RÈGLE STRICTE (30/10) : jamais un cœur dont le frère HT est tenu par une AUTRE réservation
    (dédié ou moteur). Pas assez de cœurs conformes → None + `alert.resource.pinning_repli_ht`
    (l'appelant retombe sur le pool partagé). Une allocation existante qui viole la règle est
    replacée (`pinning_ht_corrected`) ou rendue (`pinning_ht_released`) au (re)déploiement."""
    n = int(n or 0)
    if n <= 0 or not node_id:
        return None
    node = db_get_node(node_id)
    pool = parse_cpuset((node or {}).get("compute_cpuset"))
    if not pool:
        return None
    # ★ Avant de chercher des cœurs libres, rendre ceux que plus aucun conteneur ne détient. C'est
    # le seul moment où la fuite a une conséquence (un refus, ou un repli en partage), donc le seul
    # endroit où la réparer est à la fois utile et observable — plutôt qu'un balayage périodique de
    # plus, dont personne ne lirait le journal.
    try:
        purger_orphelines(node_id)
    except Exception as _e:
        log.warning("allocate_cores: purge des réservations orphelines (nœud %s) : %s", node_id, _e)
    with _alloc_lock:
        with get_db() as db:
            alloc = _allocated(db, node_id)
            numa_of = numa_map_cached(node_id)
            core_of = core_map_cached(node_id)

            def _poids(c):
                """Coût de prendre le cœur `c` : 0 s'il est physiquement libre, sinon la charge
                MESURÉE du conteneur qui tient son sibling (own_latency_ms, publiée sur :8080 et
                mise en cache par la boucle de métriques). Un conteneur inconnu du cache compte
                pour une charge moyenne — on ne le privilégie ni ne le pénalise."""
                if not core_of:
                    return 0
                phys = core_of.get(c)
                if phys is None:
                    return 0
                for cc, vv in alloc.items():
                    if vv != vmid and core_of.get(cc) == phys:
                        try:
                            from .metrics import own_latency_cache as _olc
                            return float(_olc.get(vv) or 10.0)
                        except Exception:                                  # noqa: BLE001
                            return 10.0
                return 0

            def _phys_libres(libres):
                """Cœurs de `libres` dont le sibling HT n'est alloué à AUCUN AUTRE conteneur — cf.
                `prio` dans `_choisir_numa`. Les cœurs du vmid courant ne se comptent pas comme pris
                (il peut être en train de les rendre). Sans carte HT lisible : tous (modèle plat)."""
                if not core_of:
                    return set(libres)
                pris = {core_of[c] for c, v in alloc.items() if c in core_of and v != vmid}
                return {c for c in libres if core_of.get(c) not in pris}

            def _tenants(cores):
                """vmid (autres que le nôtre) qui tiennent un frère HT de `cores` — pour NOMMER."""
                if not core_of:
                    return []
                phys = {core_of[c] for c in cores if c in core_of}
                return sorted({v for c, v in alloc.items()
                               if v != vmid and core_of.get(c) in phys})

            def _strict_pick(cands):
                """`n` cœurs parmi `cands` (déjà filtrés par la règle stricte), NUMA respecté →
                (cœurs, motif). `prio` = tous : le repli sur des frères pris n'existe plus."""
                if not numa_of:
                    return cands[:n], None
                return _choisir_numa(cands, n, numa_of, prefer_numa, prio=set(cands), poids=_poids)

            def _alerter_motif(motif):
                if motif:
                    _p = dict(motif["params"])
                    _p["noms"] = _noms_vmids([vmid])
                    _p["n"] = (node or {}).get("name", node_id)
                    db_add_alert(motif["cle"], "warning", node_id=node_id, vmid=vmid,
                                 kind="resource", params=_p)

            # ★★ RÈGLE STRICTE (décision de Cyril, 30/10) : un dédié ne prend JAMAIS le frère HT
            # d'un autre dédié. Un cœur n'est candidat que si son frère est libre, ou dans le pool
            # partagé (ce que `reserve_physique` laisse à l'exploitant) — jamais tenu par une
            # réservation de `node_core_alloc` (conteneur dédié OU lcore de moteur 2110, dont
            # `_exclure_du_pool` sort déjà les frères du pool). Z8, 30/10 : decklink-io #231
            # « dédié » 27-31 = les frères de 1-5 tenus par deux pyramides dédiées ; 265 % demandés,
            # jamais 5 cœurs, images répétées/noires en SDI, et AUCUN compteur ne le disait.
            # L'ancienne préférence (`prio` + `poids`) retombait sur ces frères quand le socket
            # n'avait plus de cœur physique entier : c'est ce repli-là qui est supprimé. À défaut
            # de cœurs physiques entiers → REPLI PARTAGÉ (None), comme un pool plein, et dit.
            mine = sorted(c for c, v in alloc.items() if v == vmid)
            if mine and physical and core_of and len({core_of.get(c) for c in mine}) < len(mine):
                # A whole-core reservation holding two siblings of one core (made before it was
                # whole): start again from scratch rather than keep the card contending with itself.
                db.execute("DELETE FROM node_core_alloc WHERE node_id=? AND vmid=?", (node_id, vmid))
                alloc = _allocated(db, node_id)
                mine = []
            if mine:
                # IDEMPOTENCE, mais PAS AVEUGLE. Recopier l'allocation existante sans la regarder
                # FIGE une erreur pour toujours : un conteneur épinglé du mauvais côté du bus par une
                # version antérieure (ou avant que son GPU ne change de socket) la garde à TOUS ses
                # redéploiements — c'est ce qui a maintenu le mur `multiview-vision` d'Horace à 30 fps
                # pour 50 (cf. mémoire numa-blind-core-pool-halves-gpu-walls). Le (re)déploiement est
                # le SEUL moment où le placement peut être revu (le conteneur est recréé de toute
                # façon) ; on ne déplace donc JAMAIS un conteneur en cours de route, mais on ne
                # reconduit pas non plus un placement incohérent sans l'examiner.
                score_actuel = _score_placement(mine, numa_of, prefer_numa)
                cand = sorted((pool - set(alloc)) | set(mine))
                if core_of:
                    cand = sorted(_phys_libres(cand))
                    if physical:
                        cand = _one_per_phys(cand, core_of, keep=mine)
                # Allocation existante qui VIOLE la règle stricte (posée avant elle) : replacée si
                # un placement conforme existe, sinon rendue → repli partagé, avec l'alerte.
                partage_ht = (set(mine) - _phys_libres(mine)) if core_of else set()
                if partage_ht:
                    voisins = _tenants(partage_ht)
                    if len(cand) >= n:
                        sans, motif = _strict_pick(cand)
                        db.execute("DELETE FROM node_core_alloc WHERE vmid=?", (vmid,))
                        db.executemany(
                            "INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                            [(node_id, c, vmid) for c in sans])
                        _alerter_motif(motif)
                        db_add_alert("alert.resource.pinning_ht_corrected", "info",
                                     node_id=node_id, vmid=vmid, kind="resource",
                                     params={"noms": _noms_vmids([vmid]),
                                             "n": (node or {}).get('name', node_id),
                                             "avant": fmt_cpuset(mine), "apres": fmt_cpuset(sans),
                                             "voisins": _noms_vmids(voisins)})
                        return fmt_cpuset(sans)
                    db.execute("DELETE FROM node_core_alloc WHERE vmid=?", (vmid,))
                    db_add_alert("alert.resource.pinning_ht_released", "warning",
                                 node_id=node_id, vmid=vmid, kind="resource",
                                 params={"noms": _noms_vmids([vmid]),
                                         "n": (node or {}).get('name', node_id),
                                         "avant": fmt_cpuset(mine), "demandes": n,
                                         "possibles": len(cand), "manquants": n - len(cand),
                                         "voisins": _noms_vmids(voisins)})
                    return None
                if score_actuel == 0 or len(mine) != n or len(cand) < n:
                    return fmt_cpuset(mine)      # déjà optimal, ou resize géré en amont → statu quo
                # Les cœurs qu'on rendrait redeviennent candidats : le meilleur placement possible
                # s'évalue sur (libres + les miens), règle stricte comprise.
                mieux, _m = _strict_pick(cand)
                score_mieux = _score_placement(mieux, numa_of, prefer_numa)
                if score_mieux >= score_actuel:
                    return fmt_cpuset(mine)      # rien de strictement mieux → aucun churn
                db.execute("DELETE FROM node_core_alloc WHERE vmid=?", (vmid,))
                db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                               [(node_id, c, vmid) for c in mieux])
                db_add_alert("alert.resource.pinning_corrige", "info",
                             node_id=node_id, vmid=vmid, kind="resource",
                             params={"noms": _noms_vmids([vmid]),
                                     "n": (node or {}).get('name', node_id),
                                     "avant": fmt_cpuset(mine), "apres": fmt_cpuset(mieux),
                                     "lib_apres": _libelle_placement(mieux, numa_of, prefer_numa),
                                     "lib_avant": _libelle_placement(mine, numa_of, prefer_numa)})
                return fmt_cpuset(mieux)
            free = sorted(pool - set(alloc))
            if len(free) < n:
                db_add_alert("alert.resource.pinning_repli_quota", "warning",
                             node_id=node_id, vmid=vmid, kind="resource",
                             params={"n": (node or {}).get('name', node_id), "libres": len(free),
                                     "pool": len(pool), "demandes": n, "vmid": vmid})
                return None
            if core_of:
                conformes = sorted(_phys_libres(free))
                if physical:
                    conformes = _one_per_phys(conformes, core_of)
                if len(conformes) < n:
                    bloques = sorted(set(free) - set(conformes))
                    db_add_alert("alert.resource.pinning_repli_ht", "warning",
                                 node_id=node_id, vmid=vmid, kind="resource",
                                 params={"noms": _noms_vmids([vmid]),
                                         "n": (node or {}).get('name', node_id),
                                         "demandes": n, "libres": len(free),
                                         "possibles": len(conformes),
                                         "manquants": n - len(conformes),
                                         "freres": fmt_cpuset(bloques),
                                         "voisins": _noms_vmids(_tenants(bloques))})
                    return None
                free = conformes
            pick, motif = _strict_pick(free)
            _alerter_motif(motif)
            try:
                db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                               [(node_id, c, vmid) for c in pick])
            except sqlite3.IntegrityError:
                # Écrivain concurrent inattendu (hors process) : ne JAMAIS écraser une allocation
                # existante — repli quota CPU, l'opérateur voit l'alerte.
                db_add_alert("alert.resource.pinning_collision", "warning",
                             node_id=node_id, vmid=vmid, kind="resource",
                             params={"node_id": node_id, "vmid": vmid})
                return None
    return fmt_cpuset(pick)


def free_dedicated_capacity(node_id, vmid=None, physical=False):
    """Cœurs qu'une réservation dédiée de `vmid` pourrait obtenir AUJOURD'HUI sous la règle
    stricte de `allocate_cores` : cœurs ordonnançables du pool, non réservés à un autre, dont le
    frère HT n'est tenu par AUCUNE autre réservation. Les cœurs de `vmid` comptent (un resize les
    rend d'abord). Sans carte HT : modèle plat (libres + les siens).

    Sert au dimensionnement dynamique (pyramide) : lui donner le compte LOGIQUE `cores_status.free`
    lui ferait demander des cœurs que la règle stricte refuse — et un refus, c'est le repli
    PARTAGÉ du conteneur entier, pire qu'une réservation plus petite mais tenue."""
    node = db_get_node(node_id) or {}
    pool = ordonnancables(node_id, node.get("compute_cpuset"))
    with get_db() as db:
        alloc = _allocated(db, node_id)
    dispo = {c for c in pool if alloc.get(c) in (None, vmid)}
    core_of = core_map_cached(node_id)
    if not core_of:
        return len(dispo)
    pris = {core_of[c] for c, v in alloc.items() if c in core_of and v != vmid}
    ok = {c for c in dispo if core_of.get(c) not in pris}
    if physical:
        return len({core_of.get(c, ("flat", c)) for c in ok})    # one logical CPU per physical core
    return len(ok)


def reserve_physique(node_id=None):
    """Réglage : une réservation dédiée prend-elle le CŒUR PHYSIQUE (frères HT exclus du partagé) ?

    OFF par défaut, et ce n'est pas de la timidité : l'activer retire du pool partagé tous les
    frères HT des cœurs dédiés. Sur un nœud déjà dense (la Z8 : 26 cœurs physiques tous touchés par
    une réservation), le pool partagé tombe à zéro et les conteneurs partagés basculent sur le
    chemin « pool entièrement dédié » — alerte forte, ou refus si `compute_refuse_oversubscribed`.
    C'est la VÉRITÉ de la machine, mais ce n'est pas un changement à imposer sous une antenne :
    l'exploitant le décide, par nœud, en sachant ce qu'il perd. Ce qui est inconditionnel, c'est
    de le DIRE (cf. `vue_coeurs.dedies[].exclusif` et `alerter_dedies_non_exclusifs`)."""
    from . import settings as _st
    try:
        return bool(int(_st.setting_for("pin_reserve_physical", node_id) or 0))
    except (TypeError, ValueError):
        return False


def partage_disponible(node_id, exclure_vmid=None, pool=None, core_of=None, strict=None):
    """Les cœurs du pool réellement OFFERTS AU PARTAGE → (cœurs triés, cœurs physiques dédiés).

    `strict` : None = suivre le réglage du nœud (le cas normal) ; True/False = forcer la règle
    pour SIMULER sans rien changer — c'est ce qui permet de chiffrer ce qu'une activation coûterait
    AVANT de la décider, au lieu de le calculer à la main sur un relevé.

    ★ UNE SEULE DÉFINITION POUR TROIS USAGES : le déploiement (`effective_cpuset`), la vue
    (`vue_coeurs`) et la réapplication (`reappliquer_partages`). Deux définitions auraient
    divergé au premier réglage — et « réappliquer » aurait rendu aux conteneurs partagés des
    cœurs que le déploiement, lui, leur refusait."""
    node = db_get_node(node_id) or {}
    pool = parse_cpuset(node.get("compute_cpuset")) if pool is None else set(pool)
    with get_db() as db:
        alloc = _allocated(db, node_id)
    pris = {c for c, owner in alloc.items() if owner != exclure_vmid}
    core_of = core_map_cached(node_id) if core_of is None else core_of
    phys_dedies = {core_of[c] for c in pris if c in core_of} if core_of else set()
    libres = [c for c in sorted(pool) if c not in pris]
    if phys_dedies and (reserve_physique(node_id) if strict is None else strict):
        libres = [c for c in libres if core_of.get(c) not in phys_dedies]
    # Node I/O (DeckLink): its physical cores are closed to the shared pool unconditionally — the
    # Z8 black flashes of 30/10 were a card sharing its execution units with busy neighbours.
    entiers = physical_siblings(node_id, exclude_vmid=exclure_vmid, core_of=core_of)
    if entiers:
        libres = [c for c in libres if core_of.get(c) not in entiers]
    return libres, phys_dedies


def cpuset_auxiliaire(node_id):
    """Le cpuset à poser sur un conteneur AUXILIAIRE : ni compute, ni porteur d'une réservation.

    Un conteneur de réplication RDMA, un banc, un outil : rien de tout cela n'a de ligne dans
    `containers`, donc rien de tout cela ne passe par `effective_cpuset`. Lancé tel quel, il naît
    SANS cpuset — c'est-à-dire libre de s'exécuter partout, y compris sur un cœur dédié à un
    conteneur temps réel et sur les lcores busy-poll du moteur 2110. Mesuré sur dl360-1 le
    2026-11-07 : neuf conteneurs dans ce cas (réplication RDMA, un banc), pendant que la console
    audio #1323 manquait 19 blocs de 1 ms en dix minutes sur son cœur « dédié » 23.

    On leur donne donc la MÊME frontière qu'au repli partagé d'un conteneur compute
    (`partage_disponible`) : hors des cœurs dédiés, hors de l'empreinte du moteur. Pas de
    réservation — un auxiliaire ne RÉSERVE rien, il est simplement BORNÉ.

    Pool entièrement dédié → le pool entier, comme `effective_cpuset` : partager le pool est
    dégradé, mais flotter sur toute la machine est pire. Aucun pool déclaré → "" (rien à poser,
    l'appelant garde son comportement d'avant)."""
    node = db_get_node(node_id) or {}
    pool = (node.get("compute_cpuset") or "").strip()
    if not pool:
        return ""
    try:
        libres, _phys = partage_disponible(node_id, pool=parse_cpuset(pool))
    except Exception as e:                      # relevé HT illisible, nœud injoignable
        log.warning("cpuset_auxiliaire: nœud %s non mesurable (%s) — pool entier", node_id, e)
        return pool
    return fmt_cpuset(libres) or pool


def effective_cpuset(node_id, vmid, n=0, prefer_numa=None, physical=False):
    """Résout le cpuset Docker à poser pour UN conteneur compute, en garantissant qu'il n'en manque
    JAMAIS un si le nœud en expose un (cause racine du 2026-07-13, nœud 30 : 4 conteneurs sans
    aucun cpuset flottaient sur les lcores busy-poll du moteur 2110 → contention TX, cf. mémoire
    mtl-tx-frozen-uint64-race). Deux étages :
      1. Pinning DÉDIÉ (`n` cœurs exclusifs non chevauchants, via `allocate_cores`) si `n>0`.
      2. À DÉFAUT (n<=0 : type sans profil `resources.pin`/`cores` ; OU pool plein ; OU tout autre
         échec d'allocation dédiée) → REPLI sur le POOL PARTAGÉ ENTIER du nœud
         (`nodes.compute_cpuset`, non-exclusif : plusieurs conteneurs s'y partagent le temps CPU).
         Toujours DANS la zone hors-moteur (cf. `derive_compute_cpuset`) — jamais sur les lcores
         DPDK, contrairement à un conteneur sans cpuset qui peut flotter n'importe où.
    Renvoie (cpuset:str, dedicated:bool). cpuset == "" seulement si le nœud n'a AUCUN
    compute_cpuset déclaré (rien à répliquer dessus — cas résiduel : nœud jamais préparé par
    ensure_compute_cpuset, ex. aucun moteur 2110 encore déployé dessus)."""
    node = db_get_node(node_id)
    if n and int(n) > 0:
        dedicated = allocate_cores(node_id, vmid, int(n), prefer_numa=prefer_numa,
                                   physical=physical)
        if dedicated:
            return dedicated, True
    pool = ((node or {}).get("compute_cpuset") or "").strip()
    if not pool:
        log.warning("core_pool: AUCUN compute_cpuset sur le nœud %s (vmid=%s) — conteneur créé SANS "
                    "cpuset (risque de contention avec un moteur 2110 co-localisé)", node_id, vmid)
        return "", False

    # ★ Le pool de repli EXCLUT les cœurs DÉDIÉS à d'AUTRES conteneurs (node_core_alloc). Sinon le
    # repli « partagé » s'installe sur des cœurs réservés en EXCLUSIVITÉ et les dispute à leur
    # propriétaire — constaté sur Horace le 2026-07-14 : le streamer 171 (sans allocation dédiée,
    # donc replié sur TOUT le pool 19-47,67-95) brûlait 107 % de CPU sur les cœurs 19-21 dédiés au
    # mur 333, qui chutait alors sous 50 fps ~20 % du temps. « Dédié » doit vouloir dire dédié.
    with get_db() as db:
        proprios = {owner for c, owner in _allocated(db, node_id).items()
                    if owner != vmid and c in parse_cpuset(pool)}
    # Définition UNIQUE du pool partagé (frères HT exclus si l'exploitant a choisi que « dédié »
    # veuille dire « cœur physique » — cf. `reserve_physique`).
    libres, _phys = partage_disponible(node_id, exclure_vmid=vmid, pool=parse_cpuset(pool))
    if not libres:
        # Tous les cœurs du pool sont réservés : mieux vaut partager le pool entier (dégradé, mais
        # borné hors des lcores du moteur) que de créer un conteneur SANS cpuset, libre de flotter
        # sur les cœurs busy-poll du moteur 2110. ⚠ Ce n'est PAS un détail de log : on installe un
        # conteneur PAR-DESSUS des cœurs réservés en exclusivité à d'autres → alerte NOMMANT les
        # propriétaires (anti-patron de l'échec silencieux), et REFUS pur et simple si l'exploitant
        # a activé `compute_refuse_oversubscribed`.
        log.warning("core_pool: pool partagé du nœud %s ENTIÈREMENT dédié (vmid=%s) — repli sur le "
                    "pool complet %s : contention avec les propriétaires", node_id, vmid, pool)
        # `msg` reste le texte FR historique : repris tel quel par `raise PoolSature(msg)`, dont
        # `docker_compute.py` relaie ensuite `str(e)` en paramètre `e` d'une alerte propre (piège des
        # données réutilisées ailleurs — cf. alert.deploy.compute.pool_sature). Les DEUX alertes émises
        # ICI, elles, ont chacune leur clé i18n : seul `msg` (la donnée qui fuit vers l'exception) ne bouge pas.
        msg = (f"Nœud {(node or {}).get('name', node_id)} : le pool de calcul {pool} est "
               f"ENTIÈREMENT dédié à {_noms_vmids(proprios)} — {_noms_vmids([vmid])} y est déployé "
               f"PAR-DESSUS (cœurs partagés de force). Aucun des deux n'a la garantie de tenir sa "
               f"cadence : libérer des cœurs (détruire/redimensionner un conteneur), réduire "
               f"l'empreinte du moteur 2110, ou déployer sur un autre nœud.")
        _p_sature = {"n": (node or {}).get("name", node_id), "pool": pool,
                     "proprios": _noms_vmids(proprios), "noms": _noms_vmids([vmid])}
        if _strict_pool():
            db_add_alert("alert.resource.pinning_sature_refuse", "error",
                         node_id=node_id, vmid=vmid, kind="resource", params=_p_sature)
            raise PoolSature(msg)
        _alerter_throttle(node_id, "sature", msg, "error",
                          cle="alert.resource.pinning_sature_partage", params=_p_sature)
        return pool, False

    # Le repli partagé est non-exclusif, mais il n'a AUCUNE raison de s'étaler sur les deux sockets :
    # on le borne au nœud NUMA visé (celui du GPU) dès qu'il y reste de la place. Garde-fou : si le
    # sous-ensemble local est vide, on garde le pool complet (mieux vaut partagé et distant que rien).
    if prefer_numa is not None:
        numa_of = numa_map_cached(node_id)
        locaux = [c for c in libres if numa_of.get(c) == prefer_numa]
        if locaux:
            libres = locaux
    shared = fmt_cpuset(libres)
    if n and int(n) > 0:
        log.info("core_pool: repli pool partagé pour vmid=%s (nœud %s) — pinning dédié de %s "
                 "cœur(s) indisponible (pool plein ?), cpuset borné à %s (non-exclusif, hors cœurs "
                 "dédiés)", vmid, node_id, n, shared)
    return shared, False


def read_nic_numa_map(node):
    """{ifname: nœud_NUMA} des interfaces réseau du nœud (celles qui ont un périphérique PCI).

    Pendant du GPU : sur un bi-socket, la carte média 2110 appartient elle aussi à UN socket, et
    c'est ELLE qui écrit les shm des flux RX (le moteur DMA vers son nœud local). Sur le lab Horace,
    les quatre ports E810 sont sur le NUMA 0 et la T4 sur le NUMA 1 : un multiview GPU qui consomme
    du 2110 ne peut donc PAS éviter la traversée inter-socket — il choisit seulement qui la paie.
    C'est l'information qui manquait à l'exploitant pour lire la page Monitoring.

    ★ Résolution par ADRESSE PCI, pas par `/sys/class/net`. Une carte média liée à **vfio pour
    DPDK** DISPARAÎT de la liste des interfaces du noyau : la première version de ce lecteur
    manquait donc précisément la carte 2110, et sur tous les nœuds de production (vérifié sur
    dl360-1 : `ens1f0np0`/`ens1f1np1` déclarées en base, absentes de /sys/class/net). Le nœud PCI,
    lui, est exposé quel que soit le pilote lié."""
    from . import node_driver
    from .database import db_get_node_interfaces
    try:
        ifs = [(it.get("ifname"), it.get("pci"))
               for it in (db_get_node_interfaces((node or {}).get("id")) or [])
               if it.get("ifname") and it.get("pci")]
    except Exception as e:
        log.debug("read_nic_numa_map(%s) interfaces: %s", (node or {}).get("host"), e)
        ifs = []
    if not ifs:
        return {}
    # Un seul exec pour toutes les interfaces déclarées (l'ifname reste la clé de sortie).
    cmd = "; ".join(
        "echo \"%s:$(cat /sys/bus/pci/devices/%s/numa_node 2>/dev/null || echo -1)\""
        % (nm, pci) for nm, pci in ifs)
    try:
        rc, out, _ = node_driver.host_exec(node, cmd, timeout=15)
        if rc != 0:
            return {}
    except Exception as e:
        log.warning("read_nic_numa_map(%s): %s", (node or {}).get("host"), e)
        return {}
    m = {}
    for ln in (out or "").splitlines():
        nm, _, nd = ln.strip().partition(":")
        try:
            n = int(nd)
        except ValueError:
            continue
        if nm and n >= 0:
            m[nm] = n
    return m


_nic_numa_cache = {}


def nic_numa_cached(node_id):
    """{ifname: nœud_NUMA} du nœud (caché à vie comme les autres topologies)."""
    nid = str(node_id)
    if _nic_numa_cache.get(nid):
        return _nic_numa_cache[nid]
    try:
        m = read_nic_numa_map(db_get_node(node_id)) or {}
    except Exception as e:
        log.debug("nic_numa_cached(%s): %s", node_id, e)
        m = {}
    if m:
        _nic_numa_cache[nid] = m
    return m


def capacite_par_socket(node_id):
    """Capacité du pool de calcul VENTILÉE PAR SOCKET (nœud NUMA), + les périphériques de chacun.

    `capacite()` agrège tout le nœud, et cet agrégat MENT par omission : sur Horace il annonçait
    « 2 cœurs physiques libres » alors que le socket du GPU n'en avait plus AUCUN et que les places
    restantes étaient toutes du mauvais côté du bus — un mur GPU déployé là tombait à 30 fps. Un
    exploitant qui lit « il reste de la place » doit savoir OÙ elle reste.

    Renvoie [] si la topologie NUMA n'est pas lisible (nœud injoignable, machine mono-socket sans
    lscpu) : l'appelant retombe alors sur l'affichage agrégé, en le sachant."""
    node = db_get_node(node_id) or {}
    numa_of = numa_map_cached(node_id)
    if not numa_of:
        return []
    pool = parse_cpuset(node.get("compute_cpuset"))
    core_of = core_map_cached(node_id)
    with get_db() as db:
        alloc = _allocated(db, node_id)
    gpus = gpu_numa_cached(node_id)
    nics = nic_numa_cached(node_id)
    # RÔLE des interfaces (table node_interfaces) : compter « les ports réseau » d'un socket ne veut
    # rien dire — sur un serveur il y a la management, l'iLO, le RDMA et la carte 2110, et un « 11 »
    # brut se lit même comme un nombre de flux. Seules les cartes du CHEMIN MÉDIA déterminent le
    # placement : on ne montre que celles-là, NOMMÉES par leur rôle.
    roles = {}
    try:
        from .database import db_get_node_interfaces
        for it in (db_get_node_interfaces(node_id) or []):
            if it.get("ifname"):
                roles[it["ifname"]] = it.get("role") or ""
    except Exception as e:
        log.debug("capacite_par_socket(%s) rôles d'interface: %s", node_id, e)

    def _phys(cores):
        return {core_of[c] for c in cores if c in core_of} if core_of else set(cores)

    out = []
    for nd in sorted(set(numa_of.values())):
        cpus = {c for c, n in numa_of.items() if n == nd}
        p_pool = pool & cpus
        if not p_pool and not cpus:
            continue
        dedies = {c for c in alloc if c in p_pool}
        phys = len(_phys(p_pool))
        phys_ded = len(_phys(dedies))
        out.append({
            "numa": nd,
            "cpus": fmt_cpuset(sorted(cpus)),
            "n_cpus": len(cpus),
            "pool": fmt_cpuset(sorted(p_pool)),
            "physical": phys,
            "physical_dedicated": phys_ded,
            "physical_free": max(0, phys - phys_ded),
            # Périphériques ANCRÉS sur ce socket : c'est ce qui rend la tuile actionnable
            # (« la place restante est du côté sans GPU » se lit d'un coup d'œil).
            "gpus": sorted(i for i, n in gpus.items() if n == nd),
            # Cartes du CHEMIN MÉDIA ancrées sur ce socket, par rôle. `media2110` = la carte qui
            # porte les flux ST 2110 (c'est ELLE qui écrit les shm RX, donc elle décide où vit la
            # donnée) ; `rdma` = la réplication inter-nœuds. Le reste (management, iLO) n'influe pas
            # sur le placement et n'a rien à faire ici.
            "nics_media": sorted(nm for nm, n in nics.items()
                                 if n == nd and roles.get(nm) == "media2110"),
            "nics_rdma": sorted(nm for nm, n in nics.items()
                                if n == nd and roles.get(nm) == "rdma"),
        })
    return out


def diagnostic_placement(node_id, vmid, pinned_cores):
    """Défaut de PLACEMENT constatable pour ce conteneur — ou None si le placement est sain
    (ou non jugeable). Destiné aux alertes de sous-cadence : « 31 fps pour 50 » sans cause nommée
    n'apprend rien à personne, et c'est précisément ce qui a laissé un mur à 30 fps toute une
    journée (cf. mémoire numa-blind-core-pool-halves-gpu-walls).

    Retourne un dict `{"message": <phrase FR, forme canonique historique>, "msg_key": <clé i18n
    "alert.cause.…">, "msg_params": {…}}` — le SEUL appelant (metrics._causes_cadence) consomme
    `msg_key`/`msg_params` ; `message` est gardé À CÔTÉ (canal en plus, pas un remplacement) pour
    tout consommateur qui voudrait encore la prose brute (log, debug).

    ⚠ Fait des host_exec au PREMIER appel sur un nœud (topologies NUMA/HT, ensuite cachées à vie) :
    à n'appeler QU'AU MOMENT D'ALERTER, jamais sur le chemin chaud des métriques."""
    cores = sorted(parse_cpuset(pinned_cores))
    if not cores:
        return None                      # pas de pinning dédié : ce n'est pas un défaut de placement
    numa_of = numa_map_cached(node_id)
    if not numa_of:
        return None                      # topologie illisible : on n'invente pas de cause
    nds = {numa_of.get(c) for c in cores}
    if len(nds) > 1 or None in nds:
        _cores = fmt_cpuset(cores)
        _numas = ', '.join(str(x) for x in sorted(n for n in nds if n is not None))
        return {
            "message": (f"ses cœurs {_cores} sont À CHEVAL sur plusieurs nœuds NUMA "
                        f"({_numas}) — chaque accès mémoire d'un cœur à la RAM de l'autre socket "
                        f"traverse le lien inter-socket"),
            "msg_key": "alert.cause.placement_numa_eclate",
            "msg_params": {"cores": _cores, "numas": _numas},
        }
    numa_coeurs = nds.pop()
    try:
        with get_db() as db:
            row = db.execute("SELECT gpu_index FROM node_gpu_alloc WHERE node_id=? AND vmid=?",
                             (node_id, vmid)).fetchone()
    except Exception as e:
        log.debug("diagnostic_placement(%s,%s) gpu: %s", node_id, vmid, e)
        return None
    if row is None:
        return None                      # pas de GPU : rien à confronter
    numa_gpu = gpu_numa_cached(node_id).get(int(row["gpu_index"]))
    if numa_gpu is None or numa_gpu == numa_coeurs:
        return None
    _cores = fmt_cpuset(cores)
    return {
        "message": (f"il est épinglé sur {_cores} (nœud NUMA {numa_coeurs}) alors que son GPU "
                    f"est sur le nœud NUMA {numa_gpu} : lectures shm et uploads vers le GPU "
                    f"traversent le lien inter-socket (mesuré : jusqu'à ×2 sur le temps de "
                    f"compositing)"),
        "msg_key": "alert.cause.placement_gpu_distant",
        "msg_params": {"cores": _cores, "numa_coeurs": numa_coeurs, "numa_gpu": numa_gpu},
    }


# État de sur-souscription par nœud (alerte à la TRANSITION, pas de spam). Le cache RAM sert le
# chemin chaud ; `_episodes` le SURVIT au redémarrage de l'orchestrateur (cf. app/episodes.py).
_oversub_etat = {}
_episodes = _Episodes("core_pool")


def verifier_capacite(node_id):
    """Sur-souscription du pool de calcul → alerte à la transition. Appelée par `surveillance` :
    l'exploitant doit le voir MÊME SANS déploiement (la sur-souscription arrive aussi quand un
    moteur grossit ou qu'un conteneur est ajouté ailleurs). Le compte qui fait foi est celui des
    cœurs PHYSIQUES : plus de conteneurs partagés que de cœurs physiques libres ⇒ aucun d'eux n'a de
    quoi tenir une cadence temps réel."""
    node = db_get_node(node_id) or {}
    if not (node.get("compute_cpuset") or "").strip():
        return
    cap = capacite(node_id)
    over = bool(cap.get("oversub"))
    prev = _oversub_etat.get(node_id)
    if prev is None:
        # Reprise après (re)démarrage : l'état vit en RAM, mais un pool sur-souscrit ne se répare
        # pas parce que l'orchestrateur redémarre. Sans cette relecture, chaque redémarrage
        # ré-annonçait la MÊME sur-souscription (26 fois le 2026-07-26). Cf. app/episodes.py.
        prev = _episodes.get(node_id)
    if over == prev:
        return
    _oversub_etat[node_id] = over
    _episodes.poser(node_id, over)
    if prev is None and not over:
        return          # 1ʳᵉ observation d'un nœud SAIN : rien à annoncer (pas de « retour » au boot)
    nom = node.get("name") or node_id
    if over:
        db_add_alert(
            "alert.resource.pool_sursouscrit", "error",
            node_id=node_id, kind="resource",
            params={"n": nom, "shared": cap['shared_containers'], "libres": cap['physical_free'],
                     "pool": cap['pool'], "physical": cap['physical'],
                     "dedicated": cap['physical_dedicated']})
    else:
        db_add_alert("alert.resource.pool_revenu_capacites", "info",
                     node_id=node_id, kind="resource",
                     params={"n": nom, "shared": cap['shared_containers'], "libres": cap['physical_free']})


class PoolSature(Exception):
    """Le pool de calcul du nœud n'a plus AUCUN cœur libre et l'exploitant a demandé le refus
    (`compute_refuse_oversubscribed`) plutôt qu'un déploiement en sur-souscription."""


def _strict_pool():
    from . import settings as st
    v = st.get("compute_refuse_oversubscribed")
    return str(v).strip().lower() in ("1", "true", "on", "oui", "yes")


# Anti-spam des alertes de pression du pool : (node_id, motif) → monotone du dernier envoi.
_ALERTE_THROTTLE_S = 300.0
_alerte_last = {}


def _alerter_throttle(node_id, motif, msg, niveau, cle=None, params=None):
    """`msg` reste le texte FR passe-plat historique (log de throttle + repli sans `cle`).
    `cle`/`params` (optionnels) : clé i18n complète de l'appelant → alerte réellement traduite."""
    import time
    k = (str(node_id), motif)
    now = time.monotonic()
    # ⚠⚠ « JAMAIS ÉMIS » N'EST PAS « ÉMIS À L'INSTANT 0 ». Ce test s'écrivait
    # `now - _alerte_last.get(k, 0.0) < _ALERTE_THROTTLE_S` : clé absente → défaut 0.0 → la
    # condition devenait `time.monotonic() < 300`. Or `monotonic()` compte DEPUIS LE DÉMARRAGE
    # DE LA MACHINE. Sur un contrôleur fraîchement redémarré, la PREMIÈRE alerte de chaque motif
    # était donc journalisée « throttlée » et jetée pendant cinq minutes — exactement la fenêtre
    # où les nœuds remontent et où les conteneurs se replacent, donc où ces alertes servent.
    # Invisible ici (machine debout depuis des jours, monotonic ≈ 1,2 million) ; constaté le
    # 2026-11-01 sur un exécuteur GitHub neuf, où `verif_dedie_frere_dedie` échouait seul.
    # On teste donc la PRÉSENCE de la clé, jamais une date par défaut.
    if k in _alerte_last and now - _alerte_last[k] < _ALERTE_THROTTLE_S:
        log.warning("core_pool[%s]: %s (alerte throttlée)", motif, msg)
        return
    _alerte_last[k] = now
    if cle:
        db_add_alert(cle, niveau, node_id=node_id, kind="resource", params=params)
    else:
        db_add_alert(msg, niveau, node_id=node_id, kind="resource")


def _noms_vmids(vmids):
    """« mur (145), recorder (143) » — pour NOMMER les victimes dans une alerte (jamais un vmid nu)."""
    from .database import db_get_container
    out = []
    for v in sorted(set(vmids)):
        try:
            c = db_get_container(v) or {}
        except Exception:
            c = {}
        hn = c.get("hostname") or "?"
        out.append(f"{hn} ({v})")
    return ", ".join(out) or "—"


def _exclure_du_pool(node, cores, core_of=None):
    """Retire de `nodes.compute_cpuset` les cœurs de l'empreinte MOTEUR `cores` (+ leurs siblings HT
    si `core_of` est fourni) — le pool de calcul ne doit JAMAIS recouvrir les lcores busy-poll.

    Sert de RATTRAPAGE quand l'empreinte moteur GRANDIT après la dérivation initiale du pool :
    `ensure_compute_cpuset` est idempotent (ne re-dérive jamais), or l'empreinte dépend de réglages
    (`mtl_lcore_max`, `mtl_service_cores`) qui peuvent changer APRÈS coup → le pool figé se met à
    chevaucher le moteur sans que personne ne le voie (dérive constatée le 2026-07-14 sur dl360-1 :
    `mtl_service_cores` 1→4 a poussé le cpuset moteur de 0-18 à 0-21 alors que le pool restait
    19-23,43-47 → 3 cœurs physiques de calcul sur 5 écrasés par des lcores à 100 %).
    Renvoie la liste des cœurs retirés."""
    pool = parse_cpuset((node or {}).get("compute_cpuset"))
    if not pool:
        return []
    interdits = set(int(c) for c in cores)
    if core_of:
        phys = {core_of.get(c) for c in interdits if c in core_of}
        interdits |= {c for c in pool if core_of.get(c) in phys}   # siblings HT des cœurs moteur
    retires = sorted(pool & interdits)
    if not retires:
        return []
    reste = sorted(pool - interdits)
    nom = (node or {}).get("name") or (node or {}).get("id")
    if not reste:
        # Le pool disparaîtrait ENTIÈREMENT : ne rien écrire (un compute_cpuset vide = conteneurs
        # SANS cpuset, libres de flotter sur les lcores → pire que le chevauchement). On alerte, fort.
        db_add_alert("alert.resource.pool_moteur_couvre_tout_ht" if core_of
                     else "alert.resource.pool_moteur_couvre_tout",
                     "error", node_id=node.get("id"), kind="resource",
                     params={"n": nom, "cores": fmt_cpuset(cores), "pool": fmt_cpuset(pool)})
        return []
    from .database import db_update_node
    db_update_node(node["id"], compute_cpuset=fmt_cpuset(reste))
    db_add_alert("alert.resource.pool_retreci_ht" if core_of else "alert.resource.pool_retreci",
                 "warning", node_id=node.get("id"), kind="resource",
                 params={"n": nom, "pool": fmt_cpuset(pool), "reste": fmt_cpuset(reste),
                         "cores": fmt_cpuset(cores), "retires": fmt_cpuset(retires)})
    return retires


def reserve_engine_cores(node_id, vmid, cores, core_of=None):
    """Enregistre le cpuset RÉELLEMENT POSÉ sur le conteneur moteur 2110 comme **source de vérité**
    de `node_core_alloc`, et met le pool de calcul en cohérence.

    ⚠ Remplace l'ancien `reserve_exact`, qui n'était qu'une COMPTABILITÉ « best-effort » : il
    n'enregistrait que les cœurs LIBRES et abandonnait silencieusement (simple `warning`) ceux déjà
    tenus par un autre vmid — alors que Docker, lui, posait le cpuset COMPLET. core_pool croyait
    donc le moteur sur 0-18 pendant qu'il spinnait à 100 % sur 0-21, dont 19-21 « dédiés » à un mur
    multiview (dl360-1, 2026-07-14 : 1,2-2,2 s par trame pour un budget de 20 ms).

    Le moteur PREND ces cœurs, qu'on le veuille ou non → la base doit le refléter :
      1. les cœurs du pin sont réservés au moteur, en évinçant les allocations concurrentes ;
      2. les propriétaires évincés sont NOMMÉS dans une alerte `error` (leur conteneur tourne
         encore avec un cpuset qui chevauche le moteur → redéploiement requis) ;
      3. le pool de calcul est rétréci pour exclure l'empreinte (+ siblings HT) — cf. _exclure_du_pool.
    Renvoie la liste des cœurs réservés."""
    cores = sorted(set(int(c) for c in (cores or [])))
    if not cores or not node_id:
        return []
    node = db_get_node(node_id) or {}
    victimes = {}
    with _alloc_lock:
        with get_db() as db:
            db.execute("DELETE FROM node_core_alloc WHERE node_id = ? AND vmid = ?",
                       (node_id, vmid))
            alloc = _allocated(db, node_id)
            for c in cores:
                owner = alloc.get(c)
                if owner is not None and owner != vmid:
                    victimes.setdefault(owner, []).append(c)
            if victimes:
                db.executemany("DELETE FROM node_core_alloc WHERE node_id=? AND core=?",
                               [(node_id, c) for lst in victimes.values() for c in lst])
            db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                           [(node_id, c, vmid) for c in cores])
    # Alertes + rétrécissement du pool HORS transaction (db_add_alert ouvre sa propre connexion).
    if victimes:
        # `detail` : noms + cpuset des victimes (identifiants, pas des phrases explicatives) — un
        # paramètre unique, sur le modèle mixer 0.22.1.
        detail = " ; ".join(f"{_noms_vmids([v])} sur {fmt_cpuset(cs)}"
                            for v, cs in sorted(victimes.items()))
        db_add_alert("alert.resource.moteur_empiete", "error", node_id=node_id, vmid=vmid,
                     kind="resource",
                     params={"n": node.get("name", node_id), "vmid": vmid,
                             "cores": fmt_cpuset(cores), "detail": detail})
    # Siblings HT du moteur occupés par d'autres (contention invisible : numéros de CPU disjoints,
    # mêmes unités d'exécution) — signalé, mais pas évincé (ce n'est pas le même CPU logique).
    if core_of:
        phys = {core_of.get(c) for c in cores if c in core_of}
        with get_db() as db:
            ht = {}
            for c, owner in _allocated(db, node_id).items():
                if owner != vmid and core_of.get(c) in phys:
                    ht.setdefault(owner, []).append(c)
        if ht:
            db_add_alert("alert.resource.ht_siblings_moteur", "warning",
                         node_id=node_id, vmid=vmid, kind="resource",
                         params={"n": node.get('name', node_id),
                                 "cores": fmt_cpuset([c for l in ht.values() for c in l]),
                                 "noms": _noms_vmids(ht.keys())})
    _exclure_du_pool(node, cores, core_of=core_of)
    # Le pool vient de rétrécir : les partagés déjà en marche gardent sinon les lcores du moteur.
    reapply_shared_after_reservation(node_id, vmid, cores)
    return cores


def reservations_orphelines(node_id):
    """Réservations de cœurs dont le vmid n'existe PLUS en base → {vmid: [cœurs]}.

    ★ POURQUOI CE CONSTAT N'EXISTAIT PAS, ET CE QU'IL A COÛTÉ (Z8, 28/10 vers 21:30).
    Un conteneur détruit sans libérer ses cœurs laisse des lignes dans `node_core_alloc`. Elles ne
    sont rattachables à rien : la carte CPU les range avec les lcores des moteurs 2110 (source
    « engine », sans nom de conteneur), et `cores_status` les compte en `used`. Douze conteneurs
    détruits tenaient ainsi 23 cœurs dédiés ; les suivants se sont partagé les 2 cœurs restants, et
    la panne s'est présentée comme un mélangeur à 22-32 fps avec deux cœurs à 203 % — rien qui
    désigne un compteur de cœurs. On ne pouvait pas le voir parce que personne ne le regardait.

    ⚠ L'INVARIANT qui rend ce constat sûr : une réservation n'est jamais créée pour un vmid qui
    n'a pas déjà sa ligne en base (`creer_container_compute` insère la ligne, puis le déploiement
    alloue ; `reserve_engine_cores` reçoit le vmid d'un conteneur moteur existant). Un vmid absent
    de `containers` est donc un vestige, pas une réservation en cours de constitution.
    """
    from .database import db_get_containers
    par_vmid = allocations_by_vmid(node_id)
    if not par_vmid:
        return {}
    vivants = {c["vmid"] for c in (db_get_containers() or [])}
    return {v: c for v, c in par_vmid.items() if v not in vivants and c}


def purger_orphelines(node_id):
    """Libère les réservations orphelines du nœud et les NOMME dans une alerte. → {vmid: [cœurs]}.

    Purger sans le dire referait de la fuite un non-événement : c'est le silence qui a coûté une
    image, pas l'oubli lui-même. L'alerte est `warning` — l'état est réparé, mais quelqu'un a
    détruit un conteneur sans passer par un chemin qui libère."""
    orph = reservations_orphelines(node_id)
    if not orph:
        return {}
    for v in orph:
        release_cores(v)
    db_add_alert("alert.resource.cores_orphelins_purges", "warning", node_id=node_id,
                 kind="resource",
                 params={"n": (db_get_node(node_id) or {}).get("name") or node_id,
                         "count": sum(len(c) for c in orph.values()),
                         "vmids": ", ".join(str(v) for v in sorted(orph)),
                         "cores": fmt_cpuset(sorted(c for l in orph.values() for c in l))})
    return orph


def release_cores(vmid):
    """Libère les cœurs alloués au vmid (à la suppression du conteneur). Best-effort, tous backends."""
    try:
        with get_db() as db:
            db.execute("DELETE FROM node_core_alloc WHERE vmid=?", (vmid,))
    except Exception as e:
        log.warning("release_cores vmid=%s: %s", vmid, e)


def allocated_for(node_id, vmid):
    """Nb de cœurs déjà alloués à ce vmid sur ce nœud (0 si aucun). Sert au dimensionnement
    dynamique : `allocate_cores` étant idempotent par vmid, un resize impose un `release_cores`
    préalable seulement si ce compte diffère du voulu."""
    try:
        with get_db() as db:
            return db.execute("SELECT COUNT(*) AS c FROM node_core_alloc WHERE node_id=? AND vmid=?",
                              (node_id, vmid)).fetchone()["c"]
    except Exception as e:
        log.warning("allocated_for node=%s vmid=%s: %s", node_id, vmid, e)
        return 0


def allocations_by_vmid(node_id):
    """{vmid: [cœurs triés]} réservés sur ce nœud (node_core_alloc). Couvre AUSSI les moteurs 2110
    (reserve_exact), dont les lcores DPDK ne sont pas dans containers.pinned_cores → sert à l'UI
    carte CPU pour montrer qui tient quoi, moteurs compris."""
    out = {}
    try:
        with get_db() as db:
            for r in db.execute("SELECT core, vmid FROM node_core_alloc WHERE node_id=?",
                                (node_id,)).fetchall():
                out.setdefault(r["vmid"], []).append(r["core"])
    except Exception as e:
        log.warning("allocations_by_vmid node=%s: %s", node_id, e)
    return {v: sorted(c) for v, c in out.items()}


def cores_status(node_id):
    """{total, used, free} du pool de pinning COMPUTE du nœud (pour l'UI Nœuds). `used` ne compte que
    les allocations DANS le pool compute_cpuset — les lcores moteur (réservés hors pool par
    reserve_engine_cores) n'en font pas partie et ne doivent pas grever le compte (sinon free
    sous-évalué). Enrichi de la CAPACITÉ RÉELLE (cf. `capacite`) : cœurs PHYSIQUES (l'HT ne double
    pas la puissance de calcul), conteneurs en partage, sur-souscription.

    `total` ne compte que les cœurs ORDONNANÇABLES du pool : un cœur isolé du noyau n'est pas de la
    capacité, c'est une ligne de comptabilité (même raison que dans `capacite`)."""
    node = db_get_node(node_id)
    pool = ordonnancables(node_id, (node or {}).get("compute_cpuset"))
    total = len(pool)
    with get_db() as db:
        rows = db.execute("SELECT core FROM node_core_alloc WHERE node_id=?", (node_id,)).fetchall()
    used = sum(1 for r in rows if r["core"] in pool)
    out = {"total": total, "used": used, "free": max(0, total - used)}
    # Publié même si `used` est confortable : c'est un défaut d'intendance, pas un seuil.
    try:
        _orph = reservations_orphelines(node_id)
        out["orphelins"] = {str(v): fmt_cpuset(c) for v, c in _orph.items()}
        out["orphelins_cores"] = sum(len(c) for c in _orph.values())
    except Exception as e:
        log.debug("cores_status orphelins(%s): %s", node_id, e)
    try:
        out.update(capacite(node_id))
    except Exception as e:
        log.debug("cores_status capacite(%s): %s", node_id, e)
    return out


# Carte {cpu_logique: cœur_physique} par nœud — invariante à chaud (topologie matérielle) → cachée
# en RAM : `capacite` est appelée à chaque rafraîchissement de la page Nœuds (5 s), pas question de
# faire un host_exec `lscpu` à chaque fois.
_core_map_cache = {}


# Cache NÉGATIF, même raison et même patron que `_numa_map_echec` plus bas : sans lui, un nœud
# injoignable faisait attendre le timeout du host_exec (2,8 s mesurées) À CHAQUE appel. Or
# `/api/nodes` appelle `cores_status` pour chaque nœud et la page Monitoring le poll toutes les
# 5 s : la carte HT à elle seule pesait 2,8 des 12 s de la requête (mesuré 2026-08-19). La
# topologie est du matériel : rien ne presse, et un nœud qui revient est repris au plus tard 60 s
# après. C'est exactement ce que fait déjà la carte NUMA — la carte HT avait simplement été oubliée.
_core_map_echec = {}
_CORE_MAP_RETRY_S = 60.0


def core_map_cached(node_id):
    import time as _t
    nid = str(node_id)
    if _core_map_cache.get(nid):
        return _core_map_cache[nid]
    if _t.time() - _core_map_echec.get(nid, 0.0) < _CORE_MAP_RETRY_S:
        return {}                    # échec récent : on ne retente pas (et on ne bloque pas l'UI)
    try:
        m = read_cpu_core_map(db_get_node(node_id)) or {}
    except Exception as e:
        log.debug("core_map_cached(%s): %s", node_id, e)
        m = {}
    if m:
        _core_map_cache[nid] = m     # topologie matérielle : invariante → cachée à vie
        _core_map_echec.pop(nid, None)
    else:
        _core_map_echec[nid] = _t.time()
    return m


# Topologies NUMA (CPU et GPU) par nœud — invariantes à chaud, mêmes règles de cache que la carte
# HT ci-dessus : cachées seulement en cas de succès (un nœud down doit être retenté).
_numa_map_cache = {}
_gpu_numa_cache = {}


# Cache NÉGATIF : un nœud injoignable fait échouer le host_exec au bout de son timeout (10 s). Sans
# ça, la page Monitoring — qui appelle `capacite_par_socket` à chaque affichage — attendrait ce
# timeout À CHAQUE FOIS. On ne retente donc qu'une fois par minute. La topologie est du matériel :
# rien ne presse, et un nœud qui revient est repris au plus tard 60 s après.
_numa_map_echec = {}
_NUMA_RETRY_S = 60.0


def numa_map_cached(node_id):
    """{cpu_logique: nœud_NUMA} du nœud (caché). {} si topologie illisible ou nœud injoignable."""
    import time as _t
    nid = str(node_id)
    if _numa_map_cache.get(nid):
        return _numa_map_cache[nid]
    if _t.time() - _numa_map_echec.get(nid, 0.0) < _NUMA_RETRY_S:
        return {}                       # échec récent : on ne retente pas (et on ne bloque pas l'UI)
    try:
        m = read_cpu_numa_map(db_get_node(node_id)) or {}
    except Exception as e:
        log.debug("numa_map_cached(%s): %s", node_id, e)
        m = {}
    if m:
        _numa_map_cache[nid] = m
        _numa_map_echec.pop(nid, None)
    else:
        _numa_map_echec[nid] = _t.time()
    return m


# Bande isolée par nœud. ⚠ Contrairement aux cartes HT/NUMA ci-dessus, elle N'EST PAS invariante à
# vie : elle change au REBOOT du nœud (nouveau cmdline appliqué), et l'orchestrateur survit aux
# reboots de ses nœuds. Un cache à vie ferait juger la flotte sur une bande périmée — exactement la
# classe d'erreur que ce chantier combat. TTL court, et un set VIDE est un RÉSULTAT VALIDE (nœud sans
# isolation) qu'il faut donc cacher aussi : d'où le sentinel None pour « illisible ».
_isolated_cache = {}          # node_id → (monotone, set|None)
_ISOLATED_TTL_S = 120.0


def isolated_cached(node_id):
    """Bande isolée ACTIVE du nœud (set d'ints, cachée 120 s), ou **None** si illisible (nœud
    injoignable). None ≠ set() : « on ne sait pas » ne doit jamais se lire « rien n'est isolé »,
    sinon un nœud down passerait pour sain (cf. anti-patron de l'échec silencieux)."""
    import time as _t
    nid = str(node_id)
    hit = _isolated_cache.get(nid)
    if hit and (_t.monotonic() - hit[0]) < _ISOLATED_TTL_S:
        return hit[1]
    try:
        s = read_isolated_cpus(db_get_node(node_id))
    except Exception as e:
        log.debug("isolated_cached(%s): %s", node_id, e)
        s = None
    _isolated_cache[nid] = (_t.monotonic(), s)
    return s


def gpu_numa_cached(node_id):
    """{index_gpu: nœud_NUMA} du nœud (caché). {} si aucun GPU ou NUMA non exposé."""
    nid = str(node_id)
    if _gpu_numa_cache.get(nid):
        return _gpu_numa_cache[nid]
    try:
        m = read_gpu_numa_map(db_get_node(node_id)) or {}
    except Exception as e:
        log.debug("gpu_numa_cached(%s): %s", node_id, e)
        m = {}
    if m:
        _gpu_numa_cache[nid] = m
    return m


def numa_of_gpu(node_id, gpu_sel):
    """Nœud NUMA du GPU désigné par un sélecteur `docker --gpus` ("device=0") → int, ou None.
    C'est l'entrée de `allocate_cores(prefer_numa=…)` pour un conteneur GPU."""
    if not gpu_sel:
        return None
    idx = str(gpu_sel).split("=", 1)[-1].strip()
    if not idx.isdigit():
        return None                  # "all" / sélecteur par UUID : aucune préférence dérivable
    return gpu_numa_cached(node_id).get(int(idx))


def numa_of_media_nic(node_id):
    """Nœud NUMA de la carte média 2110 du nœud (int), ou None si indécidable.

    Pendant de `numa_of_gpu`, pour les conteneurs SANS GPU dont les entrées sont les flux RX du
    moteur : c'est la carte qui DMA les trames, donc les shm 2110 vivent sur SON socket. Un
    consommateur plein-format posé de l'autre côté paie chaque octet au prix du lien inter-socket.

    ⚠ N'est PAS une préférence générale « tout le compute près de la carte » — ce serait vider le
    socket du GPU de son intérêt (48 des 58 cœurs du pool d'Horace y vivent). Réservé aux types qui
    lisent VRAIMENT le 2110 en pleine résolution, cf. `PREFERE_SOCKET_MEDIA`.

    None si plusieurs cartes média sont réparties sur des sockets DIFFÉRENTS : il n'y a alors pas de
    réponse, et inventer une préférence serait pire que ne rien préférer."""
    try:
        from .database import db_get_node_interfaces
        media = {it.get("ifname") for it in (db_get_node_interfaces(node_id) or [])
                 if (it.get("role") or "") == "media2110" and it.get("ifname")}
    except Exception as e:
        log.debug("numa_of_media_nic(%s): %s", node_id, e)
        return None
    if not media:
        return None
    nds = {nd for nm, nd in nic_numa_cached(node_id).items() if nm in media}
    return nds.pop() if len(nds) == 1 else None


_pci_numa_cache = {}


def numa_of_pci_vendor(node_id, vendor):
    """NUMA node of the PCI cards of `vendor` (sysfs id, e.g. "0xbdbd" for Blackmagic) on this
    node (int), or None when undecidable: no such card, unreadable topology, or cards spread over
    DIFFERENT sockets (inventing a preference there would be worse than none).

    A node I/O container (a DeckLink card, `resources.io_pci_vendor`) is pinned on the socket of
    its card: the card DMAs the frames there. Read once per node and kept, like the other
    topologies (a card does not change socket while the node runs)."""
    vendor = str(vendor or "").strip().lower()
    if not node_id or not vendor.startswith("0x") or not all(ch in "0123456789abcdefx" for ch in vendor):
        return None
    key = (str(node_id), vendor)
    if key in _pci_numa_cache:
        return _pci_numa_cache[key]
    from . import node_driver
    cmd = ("for d in /sys/bus/pci/devices/*; do [ \"$(cat $d/vendor 2>/dev/null)\" = %s ] && "
           "cat $d/numa_node 2>/dev/null; done; true" % vendor)
    try:
        rc, out, _ = node_driver.host_exec(db_get_node(node_id), cmd, timeout=15)
    except Exception as e:
        log.debug("numa_of_pci_vendor(%s, %s): %s", node_id, vendor, e)
        return None
    if rc != 0:
        return None
    nds = set()
    for ln in (out or "").split():
        try:
            nds.add(int(ln))
        except ValueError:
            continue
    nds.discard(-1)                       # -1 = single-socket machine or unknown: no preference
    found = nds.pop() if len(nds) == 1 else None
    _pci_numa_cache[key] = found
    return found


# Types SANS GPU dont les entrées sont les flux RX 2110 lus en PLEINE résolution : leur cpuset doit
# viser le socket de la carte média. La pyramide est le cas d'école — c'est même toute sa raison
# d'être : lire une fois, près de la carte, et ne publier que des proxies réduits (mesuré sur ce
# parc : 12 lectures pleines pour 4 sources distinctes, soit ×3 de déduplication en plus du ×16 du
# downscale). Posée du mauvais côté du bus, elle ne sert À RIEN : elle déplacerait à travers l'UPI
# exactement ce qu'elle est censée y éviter.
PREFERE_SOCKET_MEDIA = ("pyramide",)


def capacite_placement(node_id):
    """Capacité d'ACCUEIL d'un nœud, pour CHOISIR où déployer. Renvoie
    `{physiques, dedies, partages, libres, pool_declare}` — `libres` en cœurs PHYSIQUES
    ORDONNANÇABLES, éventuellement négatif (nœud déjà sur-souscrit).

    ⚠ Distinct de `capacite()`, qui décrit le POOL DÉCLARÉ pour l'affichage. Ici deux différences
    délibérées :

    1. **Un nœud SANS `compute_cpuset` n'a pas une capacité nulle, il a la machine entière.**
       `capacite()` rend 0 dans ce cas — ce qui est juste pour « combien de cœurs le pool
       réserve-t-il », et faux pour « ce nœud peut-il accueillir un conteneur ». Trier des nœuds
       sur ce zéro choisissait systématiquement le SEUL nœud doté d'un pool… qui se trouve être le
       plus contraint du parc. On retombe donc sur (tous les CPU − bande isolée).
    2. Les conteneurs déjà en PARTAGE sont soustraits : deux nœuds à 6 cœurs libres ne se valent
       pas si l'un en héberge déjà quatre."""
    node = db_get_node(node_id) or {}
    declare = parse_cpuset(node.get("compute_cpuset"))
    core_of = core_map_cached(node_id)
    if declare:
        pool = ordonnancables(node_id, declare)
    else:
        n = len(core_of) if core_of else int(node.get("cpu_count") or 0)
        pool = ordonnancables(node_id, set(range(n))) if n else set()

    def _phys(cores):
        return {core_of[c] for c in cores if c in core_of} if core_of else set(cores)

    with get_db() as db:
        alloc = _allocated(db, node_id)
    dedies = {c for c in alloc if c in pool}
    physiques = len(_phys(pool))
    n_dedies = len(_phys(dedies))
    partages = 0
    try:
        from .database import db_get_containers
        from .docker_compute import is_compute_container
        vmids_dedies = {alloc[c] for c in dedies}
        for c in db_get_containers() or []:
            if (c.get("node_id") == node_id and c.get("status") == "running"
                    and is_compute_container(c) and c.get("vmid") not in vmids_dedies):
                partages += 1
    except Exception as e:
        log.debug("capacite_placement(%s) conteneurs: %s", node_id, e)
    return {"physiques": physiques, "dedies": n_dedies, "partages": partages,
            "libres": physiques - n_dedies - partages,
            "pool_declare": bool(declare)}


def capacite(node_id):
    """CAPACITÉ RÉELLE de calcul du nœud — ce que l'UI doit afficher AVANT qu'un déploiement
    n'échoue en vol. Le compte qui fait foi est celui des **cœurs PHYSIQUES** : deux threads
    HyperThreading d'un même cœur partagent les unités d'exécution, ils ne portent pas deux murs.
      - physical            : cœurs physiques du pool compute
      - physical_dedicated  : cœurs physiques réservés en exclusivité (node_core_alloc)
      - physical_free       : cœurs physiques restants pour le pool PARTAGÉ
      - shared_containers   : conteneurs du nœud qui tournent SUR ce pool partagé (sans pin dédié)
      - oversub             : plus de conteneurs partagés que de cœurs physiques libres
      - pool_isole          : partie du pool qui est ISOLÉE du noyau, donc INUTILISABLE (cf. plus bas)
    Sans carte HT lisible (nœud injoignable), on retombe sur le compte LOGIQUE (borne haute).

    ★ Le pool est d'abord réduit à ses cœurs ORDONNANÇABLES (`ordonnancables`). Un cœur isolé
    présent dans `compute_cpuset` est compté comme de la capacité alors qu'aucun conteneur ne
    pourra s'y faire ordonnancer : ce sont des cœurs qui n'existent que dans la comptabilité.
    Le cas n'est pas théorique — `derive_compute_cpuset` prend le complément de l'empreinte moteur
    au moment où il est calculé, mais `mtl_lcore_max` / `mtl_service_cores` sont modifiables APRÈS,
    et un opérateur peut poser `compute_cpuset` à la main. `pool_isole` nomme l'écart au lieu de le
    laisser gonfler la capacité en silence."""
    node = db_get_node(node_id) or {}
    pool_declare = parse_cpuset(node.get("compute_cpuset"))
    pool = ordonnancables(node_id, pool_declare)
    pool_isole = pool_declare - pool
    core_of = core_map_cached(node_id)
    def _phys(cores):
        if core_of:
            return {core_of[c] for c in cores if c in core_of}
        return set(cores)                                   # pas de carte → 1 logique = 1 « cœur »
    with get_db() as db:
        alloc = _allocated(db, node_id)
    dedies = {c for c in alloc if c in pool}
    physical = len(_phys(pool))
    physical_ded = len(_phys(dedies))
    physical_free = max(0, physical - physical_ded)
    shared = 0
    try:
        from .database import db_get_containers
        from .docker_compute import is_compute_container
        vmids_dedies = {alloc[c] for c in dedies}
        for c in db_get_containers() or []:
            if c.get("node_id") != node_id or c.get("status") != "running":
                continue
            if not is_compute_container(c):
                continue                                    # moteur 2110 : hors pool compute
            if c.get("vmid") in vmids_dedies:
                continue                                    # pinning dédié → pas sur le pool partagé
            shared += 1
    except Exception as e:
        log.debug("capacite(%s) conteneurs: %s", node_id, e)
    return {"pool": fmt_cpuset(pool), "physical": physical,
            "physical_dedicated": physical_ded, "physical_free": physical_free,
            "shared_containers": shared,
            "oversub": bool(shared > physical_free),
            "pool_declare": fmt_cpuset(pool_declare),
            "pool_isole": fmt_cpuset(pool_isole),
            "pool_isole_n": len(pool_isole)}


# ═══ « Qui tient quoi » : la vue des cœurs d'un nœud, et sa réparation ══════════════════════════
#
# ★ POURQUOI CETTE VUE EXISTE (Cyril, 28/10 : « comment on règle ça seuls via l'interface ? »).
# `cores_status` disait « free 2, oversub true » — vrai, et inutilisable : il ne nomme personne.
# Sur la Z8, ces deux cœurs étaient le reste d'un pool dont 23 étaient tenus par douze conteneurs
# DÉTRUITS, et les cinq conteneurs recréés (mélangeur et decklink-io compris) s'y partageaient
# 50-51. Le symptôme fut un mélangeur à 22-32 fps : à aucun moment un compteur de cœurs n'était
# en cause pour qui regardait l'image. La seule réparation possible passait par un script.
#
# Trois principes, et ils ne sont pas cosmétiques :
#   1. ON MESURE LE CPUSET APPLIQUÉ, on ne le déduit pas. `containers.pinned_cores` ne porte que le
#      pinning DÉDIÉ ; le cpuset d'un conteneur partagé a été calculé à son déploiement et n'est
#      écrit nulle part. Il est donc relevé sur l'hôte (`placement.releve_cache`) — et cette mesure
#      permet en plus de comparer l'intention à la réalité pour les dédiés.
#   2. L'ORPHELIN EST NOMMÉ ROUGE. Une réservation dont le vmid n'existe plus était rangée avec les
#      lcores des moteurs 2110, sans nom : invisible jusqu'à ce que l'image saute.
#   3. ON RÉPARE SANS COUPER. `docker update --cpuset-cpus` élargit le cpuset d'un conteneur en
#      marche ; recréer serait une coupure d'antenne pour un geste d'intendance.

def _temps_reel(c):
    """Ce conteneur tient-il une cadence (≠ profil `batch`) ? Décide de l'alerte « pool trop bas »."""
    try:
        from .docker_compute import effective_resources, _type_of
        res = effective_resources(_type_of(c), c) or {}
        return str(res.get("priority") or "").lower() != "batch"
    except Exception:
        return True          # inconnu → compté comme temps réel : on préfère alerter pour rien


def _siblings_dedicated(vmid, reserve, owner, core_of, par_vmid, noms, vivants):
    """Frères HT des cœurs `reserve` de `vmid` tenus par une AUTRE réservation dédiée VIVANTE →
    [{vmid, hostname, cpuset (sa réservation entière), siblings (les frères concernés)}], trié par
    vmid. Carte HT absente → [] (c'est `exclusif` qui porte alors le « je ne sais pas »)."""
    if not core_of:
        return []
    phys = {core_of[y] for y in reserve if y in core_of}
    par_voisin = {}
    for x, v in owner.items():
        if v != vmid and v in vivants and x in core_of and core_of[x] in phys:
            par_voisin.setdefault(v, []).append(x)
    return [{"vmid": v, "hostname": noms.get(v, ""),
             "cpuset": fmt_cpuset(par_vmid.get(v) or []), "siblings": fmt_cpuset(sorted(xs))}
            for v, xs in sorted(par_voisin.items())]


def _charge_coeurs(c):
    """Charge CPU MESURÉE d'un conteneur, en CŒURS → (charge|None, minorant: bool).

    ★ UNITÉ. `containers.cpu_percent` est un pourcentage des CPU ALLOUÉS au conteneur (l'agent
    divise par `n_cpus`), PAS d'un cœur : 57 % sur 16 CPU valent 9 cœurs, 57 % sur 2 CPU en
    valent 1. La charge absolue est donc `cpu_percent × cpu_count / 100`, et sans `cpu_count`
    elle n'est pas calculable — on rend None plutôt qu'un chiffre sans unité (c'est l'erreur
    qui a produit un facteur 30 le 2026-08-01, cf. app/cpu_profiles.py).

    ★ MINORANT. L'agent ÉCRÊTE `cpu_pct` à 100 : un conteneur qui saturerait son cpuset et en
    réclamerait le double rend la même valeur que celui qui le remplit juste. `minorant=True`
    dit « au moins ça », et l'interface doit l'afficher comme tel (« ≥ »), jamais comme une
    mesure — sinon « réservé contre consommé » conclurait à tort au sur-dimensionnement."""
    pct, n = (c or {}).get("cpu_percent"), (c or {}).get("cpu_count")
    if pct is None or not n:
        return None, False
    try:
        return round(float(pct) * int(n) / 100.0, 2), float(pct) >= 100.0
    except (TypeError, ValueError):
        return None, False


def _famine(vmid):
    """PSI « full » avg60 du conteneur (cf. app/cpu_pressure.py), ou None si inconnu."""
    try:
        from . import cpu_pressure
        return (cpu_pressure.for_container(vmid) or {}).get("full")
    except Exception as e:                                                   # noqa: BLE001
        log.warning("vue_coeurs: PSI of %s unreadable (%s)", vmid, e)
        return None


def _place(c, reserve, res):
    """PLACE du conteneur — où il s'exécute, question distincte de son POIDS (cf. app/cpu_weight
    .py) : 'dedie' (des cœurs à lui), 'partage' (le pool du nœud), 'partage_plafonne' (le pool,
    borné par un quota `--cpus`). → (place, plafond|None)."""
    if reserve:
        return "dedie", None
    plafond = (res or {}).get("cores")
    if plafond:
        try:
            return "partage_plafonne", int(plafond)
        except (TypeError, ValueError):
            pass
    return "partage", None


def _grille_coeurs(pool, core_of, par_vmid, vivants, libres, noms, moteurs):
    """Carte des cœurs PHYSIQUES du nœud, deux threads HT côte à côte →
    [{phys, threads: [{cpu, kind, vmids, noms}], kind, frere_partage}].

    `kind` d'un thread : 'dedie' | 'moteur' (lcores d'un moteur 2110) | 'orphelin' (réservé par
    un conteneur DÉTRUIT) | 'partage' (pool partagé) | 'systeme' (hors du pool de calcul :
    noyau, agent-nœud, tout ce que l'orchestrateur ne place pas).

    `frere_partage` = ce cœur physique porte un thread DÉDIÉ et un thread qui ne l'est pas. C'est
    le défaut que la vue existait pour nommer : les deux threads partagent les unités
    d'exécution, donc « dédié » n'y veut plus dire dédié.

    Carte HT inconnue (nœud injoignable) → un emplacement par cpu logique, `phys=None` : on
    montre ce qu'on sait et on n'invente aucun appariement."""
    owner = {c: v for v, cs in par_vmid.items() for c in cs}
    libres = set(libres)
    pool = set(pool)
    tous = sorted(set(core_of) | pool | set(owner))
    groupes = {}
    for cpu in tous:
        groupes.setdefault(core_of.get(cpu, ("x", cpu)), []).append(cpu)

    def _thread(cpu):
        v = owner.get(cpu)
        if v is not None and v not in vivants:
            kind = "orphelin"
        elif v is not None:
            kind = "moteur" if v in moteurs else "dedie"
        elif cpu in libres:
            kind = "partage"
        elif cpu in pool:
            # Dans le pool de calcul, mais ni réservé ni offert au partage : c'est le cas d'un
            # cœur retiré du pool partagé par la règle d'exclusivité physique (frère d'un dédié).
            kind = "reserve_ht"
        else:
            kind = "systeme"
        return {"cpu": cpu, "kind": kind,
                "vmids": [v] if v is not None else [],
                "noms": [noms.get(v) or ("#%d" % v)] if v is not None else []}

    out = []
    for k in sorted(groupes, key=lambda x: (isinstance(x, tuple), x)):
        threads = [_thread(c) for c in sorted(groupes[k])]
        kinds = {t["kind"] for t in threads}
        frere = ("dedie" in kinds or "moteur" in kinds) and bool(
            kinds & {"partage", "systeme"} or
            len({tuple(t["vmids"]) for t in threads if t["vmids"]}) > 1)
        out.append({"phys": (None if isinstance(k, tuple) else k), "threads": threads,
                    "kind": ("dedie" if kinds <= {"dedie", "moteur"} and kinds else
                             "orphelin" if "orphelin" in kinds else
                             "mixte" if frere else
                             ("partage" if "partage" in kinds else "systeme")),
                    "frere_partage": bool(frere)})
    return out


def vue_coeurs(node_id, force=False):
    """Qui tient quoi sur le pool de calcul de ce nœud → dict prêt pour l'UI et pour une garde.

    `releve` dit si le cpuset APPLIQUÉ a pu être mesuré (nœud injoignable → False, et les champs
    `cpuset_applique` restent vides plutôt que remplis d'une supposition)."""
    from .database import db_get_containers
    node = db_get_node(node_id) or {}
    pool = parse_cpuset(node.get("compute_cpuset"))
    par_vmid = allocations_by_vmid(node_id)
    conteneurs = [c for c in (db_get_containers() or []) if c.get("node_id") == node_id]
    vivants = {c["vmid"] for c in conteneurs}

    applique = {}
    releve = None
    try:
        from . import placement
        releve = placement.releve_cache(node_id, force=force)
    except Exception as e:
        log.warning("vue_coeurs(%s): relevé du placement : %s", node_id, e)
    if releve:
        applique = {(c.get("name") or ""): (c.get("cpuset") or "")
                    for c in (releve.get("containers") or [])}

    # Le pool réellement partagé = le pool moins ce qui est dédié (aux vivants comme aux orphelins :
    # tant qu'une réservation est là, `effective_cpuset` l'évite).
    pris = {c for cs in par_vmid.values() for c in cs}
    core_of = core_map_cached(node_id) or {}
    libres, _phys_d = partage_disponible(node_id, pool=pool, core_of=core_of)
    voulu = set(libres)
    # ★ TOPOLOGIE HT : deux threads d'un même cœur physique partagent ses unités d'exécution. Un
    # cœur « dédié » dont le FRÈRE est dans le pool partagé n'est pas dédié du tout — le calcul,
    # lui, est partagé. Relevé sur la Z8 le 28/10 : pool partagé 1-23,50-51 entièrement fait des
    # frères des cœurs dédiés (50 et 51 sont les frères de 24 et 25, le multiview), segment
    # « output » du multiview à 13-14 ms et ~3 images répétées par seconde. `_exclure_du_pool`
    # tient déjà cet invariant pour les lcores des MOTEURS 2110 ; il n'était tenu nulle part pour
    # les réservations de conteneurs. Carte absente (nœud injoignable) → aucune affirmation.
    phys_dedies = {core_of[c] for c in pris if c in core_of}
    owner = {c: v for v, cs in par_vmid.items() for c in cs}
    noms = {c["vmid"]: (c.get("hostname") or "") for c in conteneurs}

    # Poids CPU POSÉ, mesuré sur l'hôte (cf. app/cpu_weight.py : il est hors signature de spec,
    # donc rien ne le rattrape — il se mesure ou il ne se sait pas).
    poids_mesures = {(c.get("name") or ""): c.get("cpu_shares")
                     for c in ((releve or {}).get("containers") or [])}

    dedies, partages = [], []
    for c in sorted(conteneurs, key=lambda x: x["vmid"]):
        v = c["vmid"]
        reserve = sorted(par_vmid.get(v) or [])
        nom_docker = c.get("docker_name") or ""
        app = applique.get(nom_docker, "")
        try:
            from .docker_compute import effective_resources, _type_of
            _res = effective_resources(_type_of(c), c) or {}
        except Exception as e:                                               # noqa: BLE001
            log.warning("vue_coeurs(%s): profil effectif de %s illisible (%s)", node_id, v, e)
            _res = {}
        place, plafond = _place(c, reserve, _res)
        _pw = cpu_weight.for_container(c)
        _pose = poids_mesures.get(nom_docker)
        _pose = None if _pose is None else (cpu_weight.DOCKER_DEFAUT if int(_pose) == 0
                                            else int(_pose))
        _charge, _minorant = _charge_coeurs(c)
        ligne = {"vmid": v, "hostname": c.get("hostname") or "", "statut": c.get("status") or "",
                 "cpuset_applique": app,
                 # ★ PLACE et POIDS, jamais confondus (arbitrage de Cyril, 30/10). `temps_reel`
                 # n'est plus publié pour un DÉDIÉ : « temps réel » y était une tautologie (un
                 # conteneur dédié n'est jamais `batch`) affichée comme une qualité, à côté d'un
                 # dédié qui, lui, pouvait très bien ne pas tenir sa cadence.
                 "place": place, "plafond": plafond,
                 "poids": _pw, "poids_applique": _pose,
                 "poids_coherent": (None if _pose is None else _pose == _pw["valeur"]),
                 "charge": _charge, "charge_minorant": _minorant,
                 # FAMINE = PSI « full » du conteneur (avg60, %) : part du temps où TOUTES ses
                 # tâches attendent un cœur. La charge ne peut pas la montrer — un conteneur
                 # affamé consomme PEU, il attend. None = pas de relevé.
                 "famine": _famine(v),
                 # A DeckLink card's cores follow its active connectors: not set by hand here.
                 "node_io": bool(_res.get("node_io"))}
        if reserve:
            ligne["cpuset_reserve"] = fmt_cpuset(reserve)
            ligne["cores"] = len(reserve)
            # Intention ≠ réalité : un conteneur qui n'a pas été recréé depuis un changement de
            # réservation tourne encore sur l'ancien cpuset. C'est précisément ce qu'une vue qui
            # se contente de lire la base ne peut pas dire.
            # Compared to what `allocate_cores` SETS, not to the bare reservation: a type that
            # asks for its HT sibling is right to carry it (cf. `cpuset_pose`).
            ligne["cpuset_pose"] = cpuset_pose(node_id, v, reserve, res=_res)
            ligne["coherent"] = (None if not app
                                 else parse_cpuset(app) == parse_cpuset(ligne["cpuset_pose"]))
            # Frères HT de SES cœurs qui se retrouvent dans le pool partagé : son exclusivité est
            # nominale, et c'est la cause la plus plausible d'un dédié qui ne tient pas sa cadence.
            freres = sorted(x for x in voulu
                            if x in core_of and core_of[x] in {core_of[y] for y in reserve
                                                               if y in core_of})
            ligne["freres_partages"] = fmt_cpuset(freres)
            # ★ Frères HT tenus par une AUTRE réservation dédiée (Z8, 30/10 : decklink-io #231
            # « dédié » 27-31 = les frères de 1-5, tenus par deux pyramides dédiées #232 et #230 ;
            # 265 % demandés, jamais 5 cœurs, images répétées/noires en SDI). Cherchés seulement
            # dans le pool partagé, ces frères-là étaient invisibles : `exclusif` valait True et la
            # vue mentait. Les orphelins sont exclus : une réservation sans conteneur ne calcule pas
            # (et `orphelins` la nomme déjà).
            ligne["siblings_dedicated"] = _siblings_dedicated(v, reserve, owner, core_of,
                                                              par_vmid, noms, vivants)
            ligne["exclusif"] = (None if not core_of
                                 else not freres and not ligne["siblings_dedicated"])
            ligne["physiques"] = len({core_of[y] for y in reserve if y in core_of}) or None
            # ★ PROTECTION : la question que Cyril pose en regardant l'écran — « ce conteneur
            # a-t-il ses cœurs pour lui ? ». Trois réponses et une inconnue, jamais un booléen :
            # « non protégé » (il est dans le pool) et « je ne sais pas » (topologie HT illisible)
            # ne sont pas la même chose, et les confondre rassurerait à tort.
            if ligne["exclusif"] is None:
                ligne["protection"] = None
                ligne["protection_raison"] = {"cle": "settings.cores.prot.ht_inconnu"}
            elif ligne["exclusif"]:
                ligne["protection"] = "protege"
                ligne["protection_raison"] = {"cle": "settings.cores.prot.cores_entiers"}
            else:
                ligne["protection"] = "partie"
                ligne["protection_raison"] = {
                    "cle": ("settings.cores.prot.freres_partages" if freres
                            else "settings.cores.prot.freres_dedies"),
                    "freres": ligne["freres_partages"],
                    "voisins": ", ".join(
                        "%s (#%d)" % (x["hostname"] or x["vmid"], x["vmid"])
                        for x in ligne["siblings_dedicated"]),
                }
            dedies.append(ligne)
        else:
            # ★ UN PARTAGÉ PÉRIMÉ (relevé par Z8 le 28/10 : stills #228 sur 27-42,44-51, qui
            # CHEVAUCHE trois réservations dédiées). Son cpuset a été calculé à son déploiement :
            # si des cœurs ont été dédiés depuis, il tourne PAR-DESSUS leurs propriétaires — et la
            # vue le listait sans rien dire. Le débordement est le cas grave, l'écart simple est
            # une occasion manquée : les deux sont nommés, séparément.
            ligne["temps_reel"] = _temps_reel(c)
            applique_set = parse_cpuset(app)
            if not app:
                ligne["coherent"] = None
            else:
                ligne["coherent"] = applique_set == voulu
                ligne["deborde"] = fmt_cpuset(sorted(applique_set & pris))
            ligne["physiques"] = (len({core_of[x] for x in (applique_set or voulu)
                                       if x in core_of}) or None) if core_of else None
            ligne["protection"] = "non"
            ligne["protection_raison"] = {
                "cle": ("settings.cores.prot.pool_poids_bas"
                        if ligne["poids"]["valeur"] < cpu_weight.DOCKER_DEFAUT
                        else "settings.cores.prot.pool"),
                "cores": fmt_cpuset(sorted(applique_set or voulu)),
            }
            partages.append(ligne)

    # ★ « PARTAGE AVEC » — la colonne que Cyril a demandée nommément, parce que « non protégé »
    # ne dit pas AVEC QUI. Deux voisinages, qui n'ont pas la même gravité et sont donc nommés
    # séparément : le même cpuset (`via: cpuset`, c'est le partage normal d'un pool) et le FRÈRE
    # HYPERTHREADING (`via: frere_ht`, le voisinage invisible — rien dans aucun cpuset ne le
    # montre). `via: deborde` = un partagé périmé qui tourne SUR les cœurs d'un dédié.
    def _vois(ligne, via):
        return {"vmid": ligne["vmid"], "hostname": ligne["hostname"], "via": via}

    for l in dedies:
        vois = [{"vmid": x["vmid"], "hostname": x["hostname"], "via": "frere_ht"}
                for x in l["siblings_dedicated"]]
        if l.get("freres_partages"):
            # Ses frères HT sont dans le pool : TOUS les partagés peuvent s'y exécuter.
            vois += [_vois(o, "frere_ht") for o in partages]
        l["partage_avec"] = vois
    for l in partages:
        cs = parse_cpuset(l["cpuset_applique"]) or voulu
        vois = [_vois(o, "cpuset") for o in partages
                if o is not l and (parse_cpuset(o["cpuset_applique"]) or voulu) & cs]
        for d in dedies:
            if parse_cpuset(d.get("freres_partages") or "") & cs:
                vois.append(_vois(d, "frere_ht"))
            elif parse_cpuset(d.get("cpuset_reserve") or "") & cs:
                vois.append(_vois(d, "deborde"))
        l["partage_avec"] = vois

    orphelins = [{"vmid": v, "cpuset": fmt_cpuset(sorted(c))}
                 for v, c in sorted(par_vmid.items()) if v not in vivants and c]
    tr = [l for l in partages if l["temps_reel"]]
    # Cœurs PHYSIQUES du pool partagé dont aucun thread n'est dédié : la vraie capacité de calcul
    # partagée. Sur la Z8 : 23 cœurs logiques, 0 cœur physique exclusif.
    phys_partages = {core_of[c] for c in libres if c in core_of}
    phys_exclusifs = phys_partages - phys_dedies
    etat_partage = {
        "cpuset": fmt_cpuset(libres), "cores": len(libres),
        "conteneurs": len(partages), "temps_reel": len(tr),
        "physiques": len(phys_partages), "physiques_exclusifs": len(phys_exclusifs),
        "freres_de_dedies": fmt_cpuset(sorted(c for c in libres
                                              if c in core_of and core_of[c] in phys_dedies)),
    }
    # ★ Le seuil est un FAIT, pas un réglage : moins de cœurs à partager que de conteneurs temps
    # réel qui s'y partagent, c'est de la sur-souscription certaine. Sur la Z8 : 2 pour 4.
    # Et ce qu'on compte, quand la topologie est connue, ce sont les cœurs PHYSIQUES EXCLUSIFS :
    # 23 cœurs logiques tous frères de cœurs dédiés ne font aucune capacité de calcul propre, et
    # la version précédente de ce verdict les comptait pour 23 — donc disait « tout va bien ».
    _capacite = len(phys_exclusifs) if core_of else len(libres)
    etat_partage["bas"] = bool(tr) and _capacite < len(tr)
    etat_partage["capacite_comptee"] = _capacite
    # ★ CE QUE COÛTERAIT « dédié = cœur physique », CHIFFRÉ AVANT LA DÉCISION. Sans ça, la
    # question « peut-on l'activer ? » se répond à la main sur un relevé — c'est ce que j'ai fait
    # pour la Z8 le 28/10, et c'est exactement le genre de calcul qu'on ne refait pas six mois
    # plus tard, sur un autre nœud, avant d'activer un réglage dont le coût a changé.
    if core_of and not reserve_physique(node_id):
        _strict, _ = partage_disponible(node_id, pool=pool, core_of=core_of, strict=True)
        etat_partage["si_strict"] = {
            "cpuset": fmt_cpuset(_strict), "cores": len(_strict),
            "physiques": len({core_of[c] for c in _strict if c in core_of}),
            "perdus": fmt_cpuset(sorted(set(libres) - set(_strict))),
            # Le verdict qui décide : reste-t-il de quoi faire tourner les partagés temps réel ?
            "viable": bool(_strict) and (not tr or len({core_of[c] for c in _strict
                                                        if c in core_of}) >= len(tr)),
        }
    # Moteurs 2110 : leurs lcores sont des réservations comme les autres, mais ce ne sont pas des
    # conteneurs de calcul — la grille les distingue (busy-poll : personne ne partage ça).
    moteurs = set()
    try:
        from .docker_compute import is_mtl_type, _type_of as _t
        moteurs = {c["vmid"] for c in conteneurs if is_mtl_type(_t(c))}
    except Exception as e:                                                   # noqa: BLE001
        log.debug("vue_coeurs(%s): moteurs non identifiés (%s)", node_id, e)

    lignes = dedies + partages
    synthese = {
        "protege": sum(1 for l in lignes if l.get("protection") == "protege"),
        "partie": sum(1 for l in lignes if l.get("protection") == "partie"),
        "non": sum(1 for l in lignes if l.get("protection") == "non"),
        "inconnu": sum(1 for l in lignes if l.get("protection") is None),
        "poids_bas": sum(1 for l in lignes
                         if l["poids"]["valeur"] < cpu_weight.DOCKER_DEFAUT),
        "poids_haut": sum(1 for l in lignes
                          if l["poids"]["valeur"] > cpu_weight.DOCKER_DEFAUT),
        "poids_divergents": sum(1 for l in lignes if l.get("poids_coherent") is False),
    }
    return {"pool": fmt_cpuset(pool), "pool_cores": len(pool),
            "ht_connu": bool(core_of),
            "ordonnancables": len(ordonnancables(node_id, node.get("compute_cpuset"))),
            "dedies": dedies, "partages": partages, "orphelins": orphelins,
            "coeurs": _grille_coeurs(pool, core_of, par_vmid, vivants, libres, noms, moteurs),
            "synthese": synthese,
            # Barème des poids : l'interface ne doit pas réécrire les valeurs (un 2048 recopié
            # dans le JS et un 2048 posé au `docker run` divergeraient en silence).
            "poids_niveaux": [{"niveau": l, "valeur": cpu_weight.WEIGHTS[l]}
                              for l in cpu_weight.LEVELS],
            "partage": etat_partage, "releve": bool(releve),
            "status": cores_status(node_id)}


def alerter_partage_bas(node_id, vue=None):
    """Alerte (throttlée) quand le pool partagé ne suffit plus aux conteneurs temps réel qui l'habitent.

    Ce n'est pas un doublon de `pinning_sature_*` : ceux-là parlent d'un conteneur qu'on DÉPLOIE et
    qui ne trouve pas sa place. Ici, personne ne déploie — la place s'est réduite sous des
    conteneurs déjà en marche (une réservation ailleurs, un orphelin), et aucun événement ne le
    signale. C'est l'état, pas la transition, qu'il faut dire."""
    vue = vue or vue_coeurs(node_id)
    p = vue["partage"]
    if not p["bas"]:
        return False
    noms = _noms_vmids([l["vmid"] for l in vue["partages"] if l["temps_reel"]])
    _alerter_throttle(node_id, "partage_bas",
                      "pool partagé réduit à %s (%d cœurs) pour %d conteneur(s) temps réel : %s"
                      % (p["cpuset"] or "—", p["cores"], p["temps_reel"], noms),
                      "warning", cle="alert.resource.partage_trop_bas",
                      params={"n": (db_get_node(node_id) or {}).get("name") or node_id,
                              "cpuset": p["cpuset"] or "—", "cores": p["cores"],
                              "count": p["temps_reel"], "noms": noms})
    return True


def alerter_dedies_non_exclusifs(node_id, vue=None):
    """Alerte (throttlée) quand un cœur « dédié » partage son cœur PHYSIQUE — avec le pool partagé
    (`alert.resource.dedie_frere_ht`) OU avec une autre réservation dédiée
    (`alert.resource.dedicated_sibling_dedicated`, voisin nommé avec son cpuset).

    ★ POURQUOI C'EST UNE ALERTE ET PAS UNE NOTE. « Dédié » est une promesse de cadence : c'est la
    seule raison de réserver. Un dédié dont le frère HT est dans le pool partagé ne la tient pas,
    et rien ne le signalait — le symptôme sort chez le spectateur (Z8, 28/10 : multiview #225
    « dédié » 24-26, segment output à 13-14 ms, ~3 images répétées par seconde, pendant que le
    mélangeur et le decklink-io travaillaient sur 50-51, frères de 24 et 25). La réservation
    s'affichait verte, le compte de cœurs était juste, et la machine mentait."""
    vue = vue or vue_coeurs(node_id)
    if not vue.get("ht_connu"):
        return False          # topologie non mesurée → aucune affirmation (pas de verdict deviné)
    nom_noeud = (db_get_node(node_id) or {}).get("name") or node_id
    leve = False
    # Cas 1 — frère HT dans le pool PARTAGÉ (clé historique, paramètres inchangés).
    coupables = [l for l in vue["dedies"] if l.get("exclusif") is False and l.get("freres_partages")]
    if coupables:
        _alerter_throttle(
            node_id, "dedie_frere_ht",
            "réservations dédiées dont le frère HT est dans le pool partagé : %s"
            % _noms_vmids([l["vmid"] for l in coupables]), "warning",
            cle="alert.resource.dedie_frere_ht",
            params={"n": nom_noeud,
                    "count": len(coupables),
                    "noms": _noms_vmids([l["vmid"] for l in coupables]),
                    "freres": ", ".join(l["freres_partages"] for l in coupables),
                    "partage": vue["partage"]["cpuset"] or "—"})
        leve = True
    # Cas 2 — frère HT tenu par une AUTRE réservation DÉDIÉE (Z8, 30/10 : decklink-io #231 sur
    # 27-31, frères de 1-5 tenus par les pyramides #232 et #230). Motif de throttle DISTINCT : un
    # cas ne doit pas taire l'autre. Le VOISIN est nommé avec son cpuset — c'est lui qu'on va voir.
    paires = [l for l in vue["dedies"] if l.get("exclusif") is False and l.get("siblings_dedicated")]
    if paires:
        detail = "; ".join(
            "%s (%s) %s ↔ %s" % (l["hostname"] or "?", l["vmid"], l["cpuset_reserve"],
                                 ", ".join("%s (%s) cpuset %s [%s]"
                                           % (s["hostname"] or "?", s["vmid"], s["cpuset"],
                                              s["siblings"])
                                           for s in l["siblings_dedicated"]))
            for l in paires)
        _alerter_throttle(
            node_id, "dedie_frere_dedie",
            "réservations dédiées dont le frère HT est tenu par un autre dédié : %s" % detail,
            "warning", cle="alert.resource.dedicated_sibling_dedicated",
            params={"n": nom_noeud, "count": len(paires),
                    "noms": _noms_vmids([l["vmid"] for l in paires]), "pairs": detail})
        leve = True
    return leve


def dedicated_exclusivity(node_id, vmid=None):
    """Verdict EXÉCUTABLE « chaque dédié a-t-il ses cœurs physiques pour lui ? » — pour la
    vérification d'après-recréation, sans relire la vue à l'œil.

    → {"node_id", "ht_known", "ok", "dedicated": [{vmid, hostname, cpuset, exclusive,
    shared_siblings, dedicated_siblings}]}. `ok` : True si tous exclusifs, False si l'un ne l'est
    pas, None si la topologie HT est inconnue (aucun verdict deviné). `vmid` restreint à une ligne
    (liste vide s'il n'a pas de réservation dédiée sur ce nœud)."""
    vue = vue_coeurs(node_id)
    lignes = [{"vmid": l["vmid"], "hostname": l["hostname"], "cpuset": l.get("cpuset_reserve", ""),
               "exclusive": l.get("exclusif"),
               "shared_siblings": l.get("freres_partages") or "",
               "dedicated_siblings": l.get("siblings_dedicated") or []}
              for l in vue["dedies"] if vmid is None or l["vmid"] == vmid]
    ht = bool(vue.get("ht_connu"))
    ok = None if not ht else all(l["exclusive"] is True for l in lignes)
    return {"node_id": node_id, "ht_known": ht, "ok": ok, "dedicated": lignes}


def reappliquer_partages(node_id, force=False):
    """Redonne aux conteneurs PARTAGÉS le pool élargi, SANS coupure (`docker update`).

    → {"faits": [{vmid, avant, apres}], "echecs": [{vmid, err}], "inchanges": [vmid…]}

    ★ POURQUOI C'EST NÉCESSAIRE, ET POURQUOI SANS RECRÉATION. Le cpuset d'un conteneur partagé est
    calculé à son DÉPLOIEMENT : il exclut les cœurs dédiés d'alors. Quand ces cœurs se libèrent, le
    conteneur garde son cpuset étroit — sur la Z8, deux cœurs pour cinq conteneurs alors que
    vingt-cinq étaient redevenus libres. Rien ne le rattrapait, et rien ne le disait. `docker
    update --cpuset-cpus` le corrige sur un conteneur EN MARCHE : recréer serait une coupure
    d'antenne pour un geste d'intendance.
    """
    from . import node_driver
    from .database import db_get_containers
    import shlex as _sh
    node = db_get_node(node_id) or {}
    vue = vue_coeurs(node_id, force=force)
    voulu = vue["partage"]["cpuset"]
    out = {"faits": [], "echecs": [], "inchanges": [], "voulu": voulu}
    if not voulu:
        return out
    par_vmid = {c["vmid"]: c for c in (db_get_containers() or [])}
    for ligne in vue["partages"]:
        c = par_vmid.get(ligne["vmid"]) or {}
        nom = (c.get("docker_name") or "").strip()
        avant = ligne.get("cpuset_applique") or ""
        if not nom:
            continue
        # Mesure obligatoire AVANT d'agir : sans relevé on ne sait pas ce qu'on change, et
        # « inchangé » ne voudrait rien dire. On ne touche alors à rien.
        if not vue["releve"]:
            out["echecs"].append({"vmid": ligne["vmid"],
                                  "err": "placement non mesurable (nœud injoignable) : "
                                         "aucun cpuset n'est modifié à l'aveugle"})
            continue
        if parse_cpuset(avant) == parse_cpuset(voulu):
            out["inchanges"].append(ligne["vmid"])
            continue
        try:
            rc, o, e = node_driver.host_exec(
                node, "docker update --cpuset-cpus=%s %s" % (_sh.quote(voulu), _sh.quote(nom)),
                timeout=30)
        except Exception as exc:
            rc, o, e = 1, "", str(exc)
        if rc == 0:
            out["faits"].append({"vmid": ligne["vmid"], "avant": avant, "apres": voulu})
        else:
            out["echecs"].append({"vmid": ligne["vmid"], "err": ((e or o) or "")[-200:]})
    if out["faits"]:
        db_add_alert("alert.resource.partages_reappliques", "info", node_id=node_id,
                     kind="resource",
                     params={"n": node.get("name") or node_id, "cpuset": voulu,
                             "count": len(out["faits"]),
                             "noms": _noms_vmids([f["vmid"] for f in out["faits"]])})
    if out["faits"]:
        try:
            from . import placement
            placement._dernier.pop(str(node_id), None)   # le relevé vient de devenir faux
        except Exception as e:
            log.debug("reappliquer_partages: cache de relevé non invalidé (%s)", e)
    return out


def reapply_shared_after_reservation(node_id, vmid=None, cores=None):
    """POINT D'ENTRÉE UNIQUE : après qu'une réservation de cœurs DÉDIÉS a RÉUSSI sur `node_id`,
    retire ces cœurs du cpuset des conteneurs PARTAGÉS déjà en marche (`reappliquer_partages`).

    → le `threading.Thread` lancé, ou None quand rien n'est réservé (aucun cœur, aucun nœud).

    ★ LA PANNE (Z8, 2026-10-29 21:30). Le mélangeur #227 recréé avec 3 cœurs dédiés (21-23) :
    #228, #231, #233, #234, #235, partagés et déjà en marche, avaient toujours 21-23 dans leur
    cpuset — calculé à LEUR déploiement, avant la réservation. « Dédié » ne l'était pas, la vue
    disait `coherent:false`, et il a fallu `POST /api/nodes/1/cores/reapply` à la main. Le remède
    n'était appelé qu'à la DESTRUCTION (quand des cœurs se libèrent), jamais quand une création,
    une recréation, un redimensionnement, une garantie à chaud ou un moteur 2110 en PREND.

    En tâche de fond (ça parle à l'hôte ; le déploiement n'a pas à l'attendre), au mieux — mais
    jamais en silence : un partagé qui reste sur des cœurs dédiés est exactement la panne qu'on
    corrige, donc tout échec est journalisé ET alerté, en nommant les conteneurs restés dessus.
    Le conteneur dédié lui-même n'est jamais touché : `reappliquer_partages` ne parcourt que
    `vue_coeurs()["partages"]`, c'est-à-dire les conteneurs SANS réservation.

    ★ MÊME FIL : la FRÉQUENCE des cœurs réservés (`sync_dedicated_cpufreq`). Z8, 29/10 22 h :
    nœud en powersave, cœurs du mélangeur à ~1,0 GHz pour 4,0 — plus lent au repos (11,1 ms) que
    sous charge (6,7 ms). `ensure_cpufreq_performance` n'était appelé que par la prép d'un nœud
    2110 ; un nœud DeckLink n'y passait jamais."""
    if isinstance(cores, str):
        reserved = parse_cpuset(cores)
    else:
        reserved = set(int(c) for c in (cores or []))
    if not node_id or not reserved:
        return None
    t = threading.Thread(target=_reapply_shared_after_reservation,
                         args=(node_id, vmid, fmt_cpuset(sorted(reserved))),
                         name="reapply-shared-%s" % node_id, daemon=True)
    t.start()
    return t


def _reapply_shared_after_reservation(node_id, vmid, cpuset):
    # Deux gestes indépendants dans le même fil : un échec de l'un n'empêche pas l'autre.
    try:
        return _reapply_shared(node_id, vmid, cpuset)
    finally:
        sync_dedicated_cpufreq(node_id, vmid)


def _reapply_shared(node_id, vmid, cpuset):
    try:
        res = reappliquer_partages(node_id, force=True)
    except Exception as e:                                                   # noqa: BLE001
        log.exception("shared cpuset reapply after reserving %s for vmid %s on node %s crashed",
                      cpuset, vmid, node_id)
        res = {"faits": [], "echecs": [{"vmid": None, "err": str(e)}], "inchanges": []}
    echecs = res.get("echecs") or []
    if not echecs:
        return res
    first = str((echecs[0] or {}).get("err") or "")[:160]
    log.warning("shared cpuset reapply after reserving %s for vmid %s on node %s: %d container(s) "
                "still on the reserved cores (first error: %s)",
                cpuset, vmid, node_id, len(echecs), first)
    try:
        node = db_get_node(node_id) or {}
        db_add_alert("alert.resource.partages_reapplication_echec", "warning",
                     node_id=node_id, vmid=vmid, kind="resource",
                     params={"n": node.get("name") or node_id, "cpuset": cpuset,
                             "count": len(echecs), "e": first,
                             "dedie": _noms_vmids([vmid]) if vmid is not None else "—",
                             "noms": _noms_vmids([x.get("vmid") for x in echecs
                                                  if x.get("vmid") is not None])})
    except Exception as e:                                                   # noqa: BLE001
        log.error("shared cpuset reapply failure on node %s could not be alerted: %s", node_id, e)
    return res



def after_cores_released(node_id):
    """Pendant de `reapply_shared_after_reservation` quand des cœurs se LIBÈRENT (destruction) :
    élargit les partagés, puis retire le plancher de fréquence des cœurs rendus — sinon ils
    chaufferaient pour rien, et toute mesure future de leur comportement normal serait faussée.
    Cible de fil de fond (cf. `containers._detruire_container_locked`)."""
    try:
        reappliquer_partages(node_id)
    except Exception as e:                                                   # noqa: BLE001
        log.warning("shared cpuset widening after release on node %s failed: %s", node_id, e)
    sync_dedicated_cpufreq(node_id, None)


def dedicated_floor_cores(node_id):
    """Cœurs DÉDIÉS du nœud à épingler au plancher de fréquence max : les réservations de
    `node_core_alloc` détenues par un conteneur EXISTANT (épinglés `resources.pin`, garanties
    d'étalonnage, lcores moteur). Les réservations orphelines n'en font pas partie — elles ne
    portent aucune échéance, les épingler serait chauffer pour personne."""
    from .database import db_get_containers
    vivants = {c["vmid"] for c in (db_get_containers() or []) if c.get("node_id") == node_id}
    return sorted({c for v, cs in allocations_by_vmid(node_id).items() if v in vivants for c in cs})


def sync_dedicated_cpufreq(node_id, vmid=None):
    """Met le nœud en `performance` + plancher max sur ses cœurs dédiés ACTUELS et veille profonde
    coupée sur eux, plancher retiré et veille rendue ailleurs — après MESURE (rien n'est
    réinstallé sur un nœud déjà conforme). Tout nœud, 2110 ou non. Best-effort, jamais
    silencieux : un échec est journalisé ET alerté. → (agi, ok, msg)."""
    node = db_get_node(node_id) or {}
    if not node:
        return False, False, "node not found"
    cores = dedicated_floor_cores(node_id)
    try:
        from . import mtl
        acted, ok, msg = mtl.sync_dedicated_cpufreq(node, cores)
    except Exception as e:                                                   # noqa: BLE001
        acted, ok, msg = True, False, "crash: %s" % e
    if ok:
        if acted:
            log.info("cpufreq on node %s set for dedicated cores [%s]: %s",
                     node_id, fmt_cpuset(cores), msg)
        return acted, ok, msg
    log.warning("cpufreq on node %s for dedicated cores [%s] failed: %s",
                node_id, fmt_cpuset(cores), msg)
    try:
        db_add_alert("alert.resource.cpufreq_dedies_echec", "warning", node_id=node_id,
                     vmid=vmid, kind="resource",
                     params={"n": node.get("name") or node_id,
                             "cores": fmt_cpuset(cores) or "—", "e": str(msg)[:200]})
    except Exception as e:                                                   # noqa: BLE001
        log.error("cpufreq failure on node %s could not be alerted: %s", node_id, e)
    return acted, ok, msg


# ─── MODE D'ÉNERGIE DU NŒUD (mesuré) ─────────────────────────────────────────────────────────
# ★ POURQUOI DANS LA FENÊTRE DES CŒURS. Un cœur dédié à 1,0 GHz n'est pas un cœur dédié : sur la
# Z8 (nœud non-2110, donc sans préparation hôte), le mélangeur passait de 11,1 ms à 4,4 ms au seul
# changement de gouverneur. Le réglage était posé, invisible, et les alertes de prép levées huit
# fois n'avaient jamais été lues. Dire « qui tient quel cœur » sans dire À QUELLE FRÉQUENCE laisse
# l'exploitant chercher une contention qui n'existe pas.
# ★ ET LA VEILLE PROFONDE, pour la même raison (mesure du 2026-11-07, dl360-1) : un cœur dédié
# dont le C6 reste actif paie jusqu'à 133 µs de sortie d'état et manque des blocs de 0,67 ms,
# sans qu'aucun compteur de charge ne bouge. C'est donc un cœur dédié qui rate ses échéances tout
# en ayant l'air parfaitement libre — exactement le genre d'écart qu'on cherche ensuite ailleurs.
_ENERGIE_TTL_S = 120.0
_energie_cache = {}          # node_id → (monotone, état)


def etat_energie(node_id, force=False):
    """État cpufreq MESURÉ du nœud → dict, caché `_ENERGIE_TTL_S` (la sonde est un aller-retour).

    `mesure=False` → nœud injoignable : aucun champ n'est alors une affirmation (gouverneur None,
    pas « powersave »). C'est la différence entre « il n'est pas en performance » et « je ne sais
    pas », et seule la première justifie de proposer un changement."""
    import time as _t
    nid = str(node_id)
    hit = _energie_cache.get(nid)
    if not force and hit and (_t.monotonic() - hit[0]) < _ENERGIE_TTL_S:
        return hit[1]
    node = db_get_node(node_id) or {}
    out = {"mesure": False, "gouverneur": None, "gouverneurs": {}, "unite": None,
           "min_mhz": None, "max_mhz": None, "conforme": None, "raison": "",
           "dedies": "", "dedies_voulus": "", "veille_mesuree": False, "veille_coupee": "",
           "veille_budget_us": None, "veille_etats_us": []}
    try:
        from . import mtl
        st = mtl.cpufreq_state(mtl._run_agent(node))
    except Exception as e:                                                   # noqa: BLE001
        log.warning("etat_energie(%s): %s", node_id, e)
        st = None
    if st is not None:
        voulus = dedicated_floor_cores(node_id)
        # Le MÊME budget que celui qui a servi à poser le script sur ce nœud : avec le budget
        # global, un nœud dont l'exploitant a changé le réglage s'afficherait « non conforme » en
        # permanence sans que rien ne puisse le satisfaire.
        _budget = mtl.cpuidle_budget_us(node_id)
        conforme, raison = mtl.cpufreq_conforme(st, voulus, budget_us=_budget)
        govs = {}
        for _c, (gov, _mn, _lo, _mx) in (st.get("cpus") or {}).items():
            govs[gov or "?"] = govs.get(gov or "?", 0) + 1
        mins = [v[1] for v in (st.get("cpus") or {}).values() if v[1]]
        maxs = [v[3] for v in (st.get("cpus") or {}).values() if v[3]]
        out.update({
            "mesure": True,
            # Un seul gouverneur sur toute la machine, ou « mixte » : la moyenne n'aurait aucun
            # sens, et un nœud à moitié en powersave est un nœud en powersave pour le conteneur
            # qui tombe du mauvais côté.
            "gouverneur": (sorted(govs)[0] if len(govs) == 1 else ("mixte" if govs else None)),
            "gouverneurs": govs, "unite": bool(st.get("unit")),
            "min_mhz": (min(mins) // 1000 if mins else None),
            "max_mhz": (max(maxs) // 1000 if maxs else None),
            "conforme": bool(conforme), "raison": raison,
            "dedies": fmt_cpuset(sorted(st.get("dedicated") or [])),
            "dedies_voulus": fmt_cpuset(voulus),
            # Veille profonde : les cœurs où elle est EFFECTIVEMENT coupée, relus sur la machine.
            # `veille_mesuree=False` = pas d'interface cpuidle (VM) : il n'y a rien à couper, et
            # la fenêtre ne doit pas afficher « rendue partout » comme si c'était un manque.
            "veille_mesuree": bool(st.get("idle")),
            "veille_coupee": fmt_cpuset(sorted(
                c for c, etats in (st.get("idle") or {}).items()
                if any(d for _lat, d in etats))),
            # L'ÉCHELLE MESURÉE de la machine, et le budget en vigueur : c'est ce qui rend le
            # réglage lisible. Un budget nu (« 5 ») ne dit pas ce qu'il coupe ; en regard des
            # latences annoncées par CE CPU, il le dit — et c'est la seule façon de régler un
            # autre modèle sans deviner (dl360-1 : 0, 2, 10, 133).
            "veille_budget_us": _budget,
            "veille_etats_us": sorted({lat for etats in (st.get("idle") or {}).values()
                                       for lat, _d in etats}),
        })
    _energie_cache[nid] = (_t.monotonic(), out)
    return out


def passer_en_performance(node_id):
    """Met le nœud en gouverneur `performance`, plancher max sur ses cœurs DÉDIÉS ACTUELS et
    veille profonde coupée sur eux.

    → {"ok", "agi", "msg", "energie"}. Geste EXPLICITE (un bouton, une confirmation) : il écrit
    une unité systemd sur l'hôte et change la consommation de la machine. `sync_dedicated_cpufreq`
    mesure d'abord et ne réinstalle rien sur un nœud déjà conforme."""
    agi, ok, msg = sync_dedicated_cpufreq(node_id)
    _energie_cache.pop(str(node_id), None)
    return {"ok": bool(ok), "agi": bool(agi), "msg": msg,
            "energie": etat_energie(node_id, force=True)}
