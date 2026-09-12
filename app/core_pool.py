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


def allocate_cores(node_id, vmid, n, prefer_numa=None):
    """Attribue `n` cœurs LIBRES du pool du nœud au `vmid`. IDEMPOTENT : si le vmid a déjà des cœurs
    sur ce nœud → les renvoie tels quels (pas de ré-allocation au redéploiement). Renvoie la chaîne
    `--cpuset-cpus` (ex. "8-11"), ou None si pas de pool / cœurs libres insuffisants (+ alerte).

    `prefer_numa` : nœud NUMA visé (typiquement celui du GPU alloué, cf. `numa_of_gpu`). Les cœurs
    rendus sont TOUJOURS d'un seul nœud NUMA quand la topologie le permet — cf. `_choisir_numa`."""
    n = int(n or 0)
    if n <= 0 or not node_id:
        return None
    node = db_get_node(node_id)
    pool = parse_cpuset((node or {}).get("compute_cpuset"))
    if not pool:
        return None
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

            mine = sorted(c for c, v in alloc.items() if v == vmid)
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
                if score_actuel == 0 or len(mine) != n:
                    return fmt_cpuset(mine)      # déjà optimal, ou resize géré en amont → statu quo
                # Les cœurs qu'on rendrait redeviennent candidats : le meilleur placement possible
                # s'évalue sur (libres + les miens).
                cand = sorted((pool - set(alloc)) | set(mine))
                mieux, _m = _choisir_numa(cand, n, numa_of, prefer_numa,
                                          prio=_phys_libres(cand), poids=_poids)
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
            pick, motif = _choisir_numa(free, n, numa_of, prefer_numa, prio=_phys_libres(free),
                                        poids=_poids)
            if motif:
                _p = dict(motif["params"])
                _p["noms"] = _noms_vmids([vmid])
                _p["n"] = (node or {}).get("name", node_id)
                db_add_alert(motif["cle"], "warning", node_id=node_id, vmid=vmid, kind="resource",
                             params=_p)
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


def effective_cpuset(node_id, vmid, n=0, prefer_numa=None):
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
        dedicated = allocate_cores(node_id, vmid, int(n), prefer_numa=prefer_numa)
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
        pris = {c for c, owner in _allocated(db, node_id).items() if owner != vmid}
        proprios = {owner for c, owner in _allocated(db, node_id).items()
                    if owner != vmid and c in parse_cpuset(pool)}
    libres = [c for c in parse_cpuset(pool) if c not in pris]
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
    if now - _alerte_last.get(k, 0.0) < _ALERTE_THROTTLE_S:
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
    return cores


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
