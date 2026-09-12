# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Migration d'un conteneur d'un nœud à un autre — simulation et bascule.

`plan_migration` établit le verdict SANS RIEN TOUCHER ; `migrer` exécute. La bascule ne fait
qu'appliquer le plan : elle refuse d'avancer s'il porte un refus, et n'outrepasse les
vérifications de capacité que sur demande explicite.

★ LE VMID EST CONSERVÉ. Les vmid sont alloués GLOBALEMENT (`allocations._used_vmids` balaie toute
la table, pas le nœud), donc rien n'oblige à en changer en migrant. C'est ce qui rend l'opération
tenable : le vmid est ce que visent le câblage, les enregistrements NMOS, les macros et le pont
ATEM. En le gardant, une migration se réduit à un changement de `node_id` — aucune de ces
références n'a besoin d'être réécrite, et l'`instance_uuid` comme l'emplacement suivent d'eux-mêmes.
"""
import json
import logging

from .database import db_get_container, db_get_containers, db_get_node, db_get_nodes

log = logging.getLogger(__name__)

# Types qu'on ne migre PAS, avec la raison — affichée telle quelle à l'opérateur : un refus sans
# motif se lit comme une limitation arbitraire, et il retentera ailleurs.
INTERDITS = {
    "2110_io": ("le moteur ST 2110 est lié au MATÉRIEL de son nœud (ports en vfio, files DPDK, "
                "horloge PTP disciplinée localement). Le déplacer n'aurait pas de sens : c'est la "
                "carte réseau qu'il faudrait déplacer."),
    "pyramide": ("une pyramide est de l'INFRASTRUCTURE DE NŒUD, partagée par tous les "
                 "consommateurs locaux — elle réduit les sources là où elles sont produites. Quand "
                 "un autre nœud a besoin de proxies, la réponse n'est pas d'y déplacer celle-ci "
                 "mais d'EN CRÉER UNE SECONDE : plusieurs nœuds portant des sources ont chacun "
                 "vocation à avoir la leur. Le provisionnement à la demande s'en charge dès qu'un "
                 "consommateur du nœud réclame un proxy."),
}


def _dc(c):
    try:
        return json.loads(c.get("deploy_config") or "{}") or {}
    except Exception:                                                      # noqa: BLE001
        return {}


def _shms(kind, hostname, params):
    """(produits, consommés) d'un conteneur, en noms de shm. Best-effort : un plugin sans wiring
    déclaré rend deux listes vides, ce qui ne fait perdre que le détail des conséquences."""
    try:
        from . import plugins as _pl
        w = _pl.derive_wiring(kind, hostname or "", params or {}) or {}
    except Exception:                                                      # noqa: BLE001
        return [], []
    prod = [p.get("shm") for p in (w.get("produces") or []) if p.get("shm")]
    cons = [c.get("shm") for c in (w.get("consumes") or []) if c.get("shm")]
    return prod, cons


def _cores_libres(node_id):
    """(libres, taille du pool) sur un nœud. None si le nœud n'a pas de pool compute défini —
    auquel cas on ne sait rien, et on le dit plutôt que de supposer que ça passe."""
    try:
        from . import core_pool as _cp
        node = db_get_node(node_id) or {}
        pool = _cp.parse_cpuset(node.get("compute_cpuset"))
        if not pool:
            return None, 0
        pris = set()
        for _v, _cs in (_cp.allocations_by_vmid(node_id) or {}).items():
            pris |= set(_cs or [])
        return len(set(pool) - pris), len(pool)
    except Exception as _e:                                                # noqa: BLE001
        log.warning("migration : pool de cœurs du nœud %s illisible (%s)", node_id, _e)
        return None, 0


# Jeux d'instructions par nœud — topologie matérielle, donc invariante : lue une fois puis cachée.
_FLAGS_CACHE = {}
# Ceux qui font la différence pour nos charges. AVX2 en tête : libmxl l'exige, et un binaire compilé
# pour lui meurt en SIGILL sur un processeur qui ne l'a pas — pas d'erreur propre, pas de message,
# juste « script terminé (code -4) » en boucle. Vécu le 2026-08-07 : un mur migré sur un Sandy
# Bridge (E5-2630 v0) est mort à chaque démarrage, et rien dans le plan de migration ne l'avait
# annoncé.
_FLAGS_UTILES = ("avx", "avx2", "avx512f", "fma", "bmi2", "sse4_2")


def _flags_cpu(node_id):
    """Jeux d'instructions du processeur d'un nœud (sous-ensemble utile), ou None si illisible."""
    cle = str(node_id)
    if cle in _FLAGS_CACHE:
        return _FLAGS_CACHE[cle]
    try:
        from .node_driver import host_exec
        node = db_get_node(node_id)
        if not node:
            return None
        r = host_exec(node, "grep -m1 ^flags /proc/cpuinfo", timeout=15)
        sortie = (r[1] + r[2]) if isinstance(r, (tuple, list)) else str(r)
        tous = set(sortie.split())
        vus = {f for f in _FLAGS_UTILES if f in tous}
        if vus:
            _FLAGS_CACHE[cle] = vus       # matériel : ne change pas → caché à vie
        return vus or None
    except Exception as _e:                                                # noqa: BLE001
        log.warning("migration : jeux d'instructions du nœud %s illisibles (%s)", node_id, _e)
        return None


def _inotify(node_id):
    """(utilisées, plafond) d'instances inotify sur un nœud, ou (None, None).

    CHAQUE conteneur MXL en consomme une : la bibliothèque surveille le domaine `/dev/shm/mxl` par
    inotify. Sur un nœud qui porte des dizaines de conteneurs de réplication, le plafond par
    défaut (128) se remplit — et le symptôme ne ressemble PAS à une limite atteinte :
    `mxlCreateInstance(/dev/shm/mxl) a échoué (tmpfs ? droits ?)`, ce qui envoie chercher du côté
    des permissions. Constaté le 2026-08-07 sur dl360-1 : 129 utilisées pour 128, et un mur qui
    refusait de démarrer sans que rien n'évoque inotify dans le message principal.
    Lu EN DIRECT (ça change à chaque conteneur créé), jamais caché."""
    try:
        from .node_driver import host_exec
        node = db_get_node(node_id)
        if not node:
            return None, None
        r = host_exec(node, "cat /proc/sys/fs/inotify/max_user_instances; "
                            "find /proc/*/fd -lname anon_inode:inotify 2>/dev/null | wc -l",
                      timeout=12)
        lignes = ((r[1] + r[2]) if isinstance(r, (tuple, list)) else str(r)).split()
        return int(lignes[1]), int(lignes[0])
    except Exception as _e:                                                # noqa: BLE001
        log.warning("migration : inotify du nœud %s illisible (%s)", node_id, _e)
        return None, None


def _cores_requis(kind):
    try:
        from . import cpu_profiles as _cpr
        return int((_cpr._resources(kind) or {}).get("cores") or 0)
    except Exception:                                                      # noqa: BLE001
        return 0


def _ressources_noeud(node_id, besoin_cores, besoin_ram_mb, veut_gpu):
    """Ce que la cible a, et ce que la migration y prendrait — de quoi tracer une jauge.

    Trois grandeurs de natures différentes, et il faut le dire plutôt que de les uniformiser :
      • les CŒURS sont une réservation EXCLUSIVE (pool de pinning) — la part prise est exacte ;
      • la RAM est un plafond déclaré, pas une consommation ;
      • le GPU ne se réserve pas : plusieurs conteneurs le partagent, et le coût réel n'est pas
        une fraction de mémoire mais un CLIENT CUDA de plus. Mesuré le 2026-08-07 sur ce parc :
        un mur seul consomme 16 % du GPU, trois murs 40 % — le nombre de processus compte plus
        que ce que chacun alloue. On expose donc la VRAM ET le nombre de clients, sans prétendre
        que la place restante en VRAM prédit quoi que ce soit.
    """
    out = {}
    libres, taille = _cores_libres(node_id)
    if taille:
        out["cpu"] = {"unite": "cœurs", "total": taille, "libre": libres or 0,
                      "requis": besoin_cores or 0, "exclusif": True}
    try:
        from . import node_health as _nh
        snap = (_nh.latest().get("nodes") or {}).get(str(node_id)) or {}
        # SIGNALEMENT du processeur. « Plus de cœurs libres » ne veut pas dire « plus puissant » :
        # deux nœuds peuvent offrir le même nombre de cœurs et ne pas jouer dans la même catégorie.
        # On donne donc le modèle, sa fréquence nominale, et le compte de cœurs PHYSIQUES à côté
        # des logiques — un E5-2699 v4 annonce 88 threads pour 44 cœurs, et confondre les deux
        # fait croire à un nœud deux fois plus capable qu'il n'est.
        _res = snap.get("resources") or {}
        _modele = _res.get("cpu_model")
        if _modele and out.get("cpu") is not None:
            import re as _re
            _f = _re.search(r"@\s*([\d.]+)\s*GHz", _modele)
            _phys = None
            try:
                from . import core_pool as _cp2
                _map = _cp2.core_map_cached(node_id) or {}
                if _map:
                    _phys = len(set(_map.values()))
            except Exception:                                              # noqa: BLE001
                _phys = None
            out["cpu"]["proc"] = {
                "modele": _re.sub(r"\s*@.*$", "", _modele).replace("(R)", "").replace("(TM)", "").strip(),
                "ghz": float(_f.group(1)) if _f else None,
                "threads": _res.get("cpu_count"),
                "physiques": _phys,
            }
        # La mémoire vit dans le SOUS-OBJET `resources` du snapshot, pas à sa racine (où l'on ne
        # trouve que membw/disks/gpu/…). Se tromper de niveau rendrait une jauge vide sans rien
        # signaler — l'opérateur en conclurait que le nœud n'a pas de RAM à offrir.
        res = snap.get("resources") or {}
        _mt = res.get("mem_total_mb")
        _mu = res.get("mem_used_mb")
        if _mt:
            out["ram"] = {"unite": "Mo", "total": int(_mt),
                          "libre": (int(_mt) - int(_mu)) if _mu is not None else None,
                          "requis": int(besoin_ram_mb or 0), "exclusif": False}
    except Exception:                                                      # noqa: BLE001
        pass
    if veut_gpu:
        g = {"unite": "Mo VRAM"}
        try:
            from . import gpu as _gpu, gpu_pool as _gp
            cartes = ((_gpu.latest().get(node_id) or {}).get("gpus")) or []
            if cartes:
                c0 = cartes[0]
                g.update({"nom": c0.get("name"), "total": c0.get("mem_total_mb"),
                          "libre": (c0.get("mem_total_mb") - c0.get("mem_used_mb"))
                                   if (c0.get("mem_total_mb") and c0.get("mem_used_mb") is not None)
                                   else None,
                          "util_pct": c0.get("util_pct"), "cartes": len(cartes)})
            st = _gp.gpu_status(node_id) or {}
            g["clients"] = st.get("used")
            g["cartes"] = g.get("cartes") or st.get("count")
        except Exception:                                                  # noqa: BLE001
            pass
        g["requis"] = None      # un GPU ne se réserve pas : c'est un client de plus, pas une part
        g["exclusif"] = False
        g["note"] = ("un GPU ne se réserve pas : ce conteneur y ajoutera un CLIENT CUDA. Sur ce "
                     "parc, le nombre de processus pèse plus que la VRAM allouée.")
        out["gpu"] = g
    return out


def plan_migration(vmid, node_cible_id):
    """Plan et verdict d'une migration, SANS RIEN TOUCHER.

    Renvoie {ok, refus[], verifications[], consequences[], conserve{}, coupure}. `ok` est vrai
    seulement si aucun refus ET aucune vérification en échec — une conséquence, elle, n'empêche
    pas : elle informe."""
    out = {"vmid": vmid, "node_cible": node_cible_id, "refus": [], "verifications": [],
           "consequences": [], "conserve": {}, "ok": False}

    def _verif(nom, ok, detail):
        out["verifications"].append({"nom": nom, "ok": bool(ok), "detail": detail})

    c = db_get_container(vmid)
    if not c:
        out["refus"].append("conteneur %s introuvable" % vmid)
        return out
    dc = _dc(c)
    kind = dc.get("type")
    params = dc.get("params") or {}
    hostname = c.get("hostname") or params.get("hostname") or ""
    node_src = c.get("node_id")
    out["hostname"] = hostname
    out["type"] = kind
    out["node_source"] = node_src

    cible = db_get_node(node_cible_id)
    if not cible:
        out["refus"].append("nœud cible %s inconnu" % node_cible_id)
        return out
    out["node_cible_nom"] = cible.get("name")
    besoin_c = _cores_requis(kind)
    veut_gpu_c = bool(params.get("gpu") or params.get("use_gpu")) or kind in ("multiview",)
    if str(node_src) == str(node_cible_id):
        # Le nœud OÙ IL EST DÉJÀ. Ce n'est pas une destination, mais c'est la référence qui
        # justifie le déplacement : on montre ce qu'il RÉCUPÉRERAIT si le conteneur partait.
        # Sans ce bloc, on voit ce que la migration coûte quelque part sans voir ce qu'elle
        # rapporte ici — soit exactement la moitié de la décision.
        out["est_source"] = True
        res = _ressources_noeud(node_src, besoin_c, c.get("memory"), veut_gpu_c)
        for _cle, _val in (("cpu", besoin_c), ("ram", c.get("memory"))):
            if res.get(_cle) is not None:
                res[_cle]["libere"] = int(_val or 0)
                res[_cle]["requis"] = 0
        if res.get("gpu") is not None:
            res["gpu"]["libere_client"] = True
        out["ressources"] = res
        return out

    # ── Refus de type ────────────────────────────────────────────────────────
    if kind in INTERDITS:
        out["refus"].append("%s : %s" % (kind, INTERDITS[kind]))
    if str(hostname).startswith("bobi-fab-"):
        out["refus"].append(
            "ce conteneur est un nœud INTERNE du tissu de composition (shard ou assembleur). Le "
            "tissu décide seul de leur placement et les recrée à sa prochaine réconciliation : une "
            "migration manuelle serait défaite sans prévenir. Pour déplacer la charge, agir sur le "
            "MUR logique, pas sur ses rouages.")

    # ── Vérifications de capacité ────────────────────────────────────────────
    _verif("nœud cible joignable", (cible.get("status") or "").lower() in ("up", "online", "ok"),
           "statut rapporté : %s" % (cible.get("status") or "inconnu"))

    besoin = _cores_requis(kind)
    libres, taille = _cores_libres(node_cible_id)
    if libres is None:
        _verif("cœurs disponibles", False,
               "le nœud %s n'a pas de pool compute défini (`compute_cpuset` vide) — impossible de "
               "savoir s'il peut épingler ce conteneur" % cible.get("name"))
    elif besoin <= 0:
        _verif("cœurs disponibles", True,
               "le type « %s » ne réclame pas d'épinglage (profil sans `cores`)" % kind)
    else:
        _verif("cœurs disponibles", libres >= besoin,
               "%d libre(s) sur %d dans le pool ; ce type en demande %d"
               % (libres, taille, besoin))

    veut_gpu = bool(params.get("gpu") or params.get("use_gpu")) or kind in ("multiview",)
    if veut_gpu:
        _verif("GPU sur la cible", bool(cible.get("gpu_capable")),
               "gpu_capable=%s" % (cible.get("gpu_capable") or 0))

    # ── CAPACITÉS DU PROCESSEUR ──────────────────────────────────────────────
    # Un nœud cible qui offre MOINS que le nœud actuel peut tuer le conteneur au démarrage, sans
    # message exploitable : un binaire compilé pour AVX2 meurt en SIGILL sur un processeur qui ne
    # l'a pas — « script terminé (code -4) », en boucle. On compare donc les deux jeux
    # d'instructions plutôt que de tenir une liste des besoins de chaque type : la règle « la
    # cible ne doit pas être moins capable que l'origine » couvre tous les cas sans rien supposer,
    # et elle aurait suffi à empêcher le mur de tomber le 2026-08-07.
    _f_src = _flags_cpu(node_src)
    _f_dst = _flags_cpu(node_cible_id)
    if _f_src and _f_dst:
        _perdus = sorted(_f_src - _f_dst)
        _verif("jeux d'instructions du CPU", not _perdus,
               ("la cible offre au moins autant que le nœud actuel"
                if not _perdus else
                "la cible N'A PAS %s, dont dispose %s. Un binaire compilé pour ces instructions "
                "meurt en SIGILL au démarrage (« script terminé, code -4 »), en boucle et sans "
                "message exploitable. libmxl EXIGE avx2."
                % (", ".join(_perdus).upper(), db_get_node(node_src).get("name"))))
    else:
        _verif("jeux d'instructions du CPU", True,
               "non vérifiable (processeur d'un des deux nœuds illisible)")

    # ── INSTANCES INOTIFY ────────────────────────────────────────────────────
    # Chaque conteneur MXL en consomme une. Plafond atteint = le script ne démarre pas, avec un
    # message qui parle de tmpfs et de droits, donc qui n'oriente pas vers la vraie cause.
    _ino_u, _ino_max = _inotify(node_cible_id)
    if _ino_u is not None and _ino_max:
        _ok_ino = (_ino_max - _ino_u) >= 4
        _verif("instances inotify", _ok_ino,
               ("%d utilisées sur %d" % (_ino_u, _ino_max)) if _ok_ino else
               ("%d utilisées sur %d — PLAFOND ATTEINT. Chaque conteneur MXL en prend une ; le "
                "script échouera sur « mxlCreateInstance a échoué (tmpfs ? droits ?) », un message "
                "qui n'évoque pas inotify et envoie chercher du côté des droits. Relever "
                "fs.inotify.max_user_instances sur ce nœud." % (_ino_u, _ino_max)))
    else:
        _verif("instances inotify", True, "non vérifiable")

    # IP du plan conteneurs : on regarde s'il en reste, sans en réserver.
    try:
        from . import allocations as _al
        st = _al.ip_stats() or {}
        _libres_ip = st.get("free") if isinstance(st, dict) else None
        _verif("IP conteneur disponible", (_libres_ip is None) or _libres_ip > 0,
               "adresses libres dans le plan conteneurs : %s"
               % ("inconnu" if _libres_ip is None else _libres_ip))
    except Exception as _e:                                                # noqa: BLE001
        _verif("IP conteneur disponible", True, "non vérifiable (%s)" % _e)

    # ── Conséquences : ce que la migration change pour les VOISINS ───────────
    # Un flux lu dans le même /dev/shm devient un flux à RÉPLIQUER. C'est automatique, mais ça
    # consomme de la bande passante inter-nœuds : l'opérateur doit le savoir AVANT, pas le
    # découvrir après.
    prod, cons = _shms(kind, hostname, params)
    autres = [x for x in db_get_containers() if x.get("vmid") != vmid]
    for o in autres:
        odc = _dc(o)
        o_prod, o_cons = _shms(odc.get("type"), o.get("hostname") or "", odc.get("params") or {})
        meme_noeud_avant = str(o.get("node_id")) == str(node_src)
        meme_noeud_apres = str(o.get("node_id")) == str(node_cible_id)
        if meme_noeud_avant == meme_noeud_apres:
            continue                       # rien ne change pour ce voisin
        partages = (set(prod) & set(o_cons)) | (set(cons) & set(o_prod))
        if not partages:
            continue
        out["consequences"].append({
            "vmid": o.get("vmid"), "hostname": o.get("hostname"),
            "flux": sorted(partages),
            "effet": ("deviendra DISTANT (réplication RDMA à provisionner)" if meme_noeud_avant
                      else "deviendra LOCAL (réplication RDMA devenue inutile)"),
        })

    # Ressources du nœud CONCERNÉ par ce bloc. L'UI en tire deux colonnes — son état actuel et
    # ce qu'il deviendrait — pour que l'avant/après se lise sur le même nœud, au même endroit.
    # Un ASSEMBLEUR déplacé laisse ses shards derrière lui : ils produisent leurs tuiles dans le
    # /dev/shm du nœud d'origine, et l'assembleur ne les y lit plus. Le tissu finira par
    # re-planifier, mais entre-temps le mur n'affiche rien — autant le dire avant.
    try:
        from . import compositor_fabric as _cf2
        _shards = (_cf2.shards_par_parent() or {}).get(vmid) or []
        if _shards:
            out["shards_a_detruire"] = _shards
            out["consequences"].append({
                "vmid": vmid, "hostname": hostname,
                "flux": ["shard #%s" % _s for _s in _shards],
                "effet": ("ces shards seront DÉTRUITS : ils produisent dans le /dev/shm de %s, que "
                          "l'assembleur ne lira plus. Le mur repart en monolithe sur la cible, et "
                          "le tissu l'y re-découpera s'il sature."
                          % (db_get_node(node_src) or {}).get("name")),
            })
    except Exception:                                                      # noqa: BLE001
        pass
    out["ressources"] = _ressources_noeud(node_cible_id, besoin, c.get("memory"), veut_gpu)
    out["conserve"] = {
        "vmid": vmid,
        "hostname": hostname,
        "instance_uuid": c.get("instance_uuid"),
        "note": ("le vmid est conservé : câblage, enregistrements NMOS, macros et emplacement "
                 "continuent de le viser sans être réécrits"),
    }
    out["coupure"] = ("le conteneur est DÉTRUIT puis RECRÉÉ : quelques secondes à une minute selon "
                      "l'image. Ce n'est pas une migration à chaud.")
    out["ok"] = (not out["refus"]) and all(v["ok"] for v in out["verifications"])
    return out


def noeuds_candidats(vmid):
    """Plan pour CHAQUE nœud, le nœud actuel EN TÊTE.

    Le nœud d'origine n'est pas une destination, mais il ouvre la liste : c'est lui qui dit ce que
    la migration rapporterait. Le laisser dehors ne montrait que le coût du déplacement, jamais
    son bénéfice — soit la moitié de la décision."""
    c = db_get_container(vmid)
    if not c:
        return []
    src = c.get("node_id")
    noeuds = sorted(db_get_nodes(), key=lambda n: 0 if str(n["id"]) == str(src) else 1)
    return [plan_migration(vmid, n["id"]) for n in noeuds]


# ─── Bascule ────────────────────────────────────────────────────────────────
# Migrer, c'est retirer le conteneur Docker du nœud source puis le REDÉPLOYER sur la cible avec le
# MÊME vmid. On ne passe donc PAS par `detruire_container` : celui-ci supprime la ligne DB, purge
# les ressources NMOS devenues orphelines et coupe EN CASCADE les liens RDMA dont ce conteneur
# était la source — trois nettoyages parfaitement justifiés pour une suppression, et trois dégâts
# pour une migration, où tout cela doit précisément survivre.

def _retirer_docker(node, name):
    """Retire le conteneur Docker d'un nœud. Agent-nœud d'abord, repli ssh — le repli existe pour
    les nœuds encore sans agent, et son échec n'est pas bloquant : un conteneur résiduel sur le
    nœud source serait gênant, mais moins que d'abandonner la migration à mi-chemin."""
    try:
        from . import node_driver
        if node_driver.has_agent(node):
            ok, err = node_driver.container_action(node, name, "destroy")
            if ok:
                return True, "retiré via l'agent-nœud"
            log.warning("migration : agent-nœud n'a pas retiré %s (%s) — repli ssh", name, err)
    except Exception as _e:                                                # noqa: BLE001
        log.warning("migration : agent-nœud indisponible pour %s (%s) — repli ssh", name, _e)
    try:
        import shlex
        from .host_ops import ssh_run
        ssh_run(node["host"], "docker rm -f %s >/dev/null 2>&1" % shlex.quote(name), timeout=30)
        return True, "retiré via ssh"
    except Exception as _e:                                                # noqa: BLE001
        return False, "impossible de retirer le conteneur du nœud source : %s" % _e


def migrer(vmid, node_cible_id, forcer=False):
    """Déplace un conteneur vers un autre nœud, EN CONSERVANT SON VMID.

    `forcer` outrepasse les vérifications de CAPACITÉ (cœurs, GPU, adresses) — jamais les refus de
    TYPE : un moteur 2110 reste lié à son matériel et une pyramide reste de l'infra de nœud, quelle
    que soit l'insistance de l'appelant. Un drapeau qui permettrait de passer outre transformerait
    un garde-fou en suggestion.

    Étapes, dans cet ordre : plan → retrait du Docker source → libération des cœurs/GPU de la
    source → bascule de `node_id` → redéploiement (qui refait le `docker run`, réalloue adresse et
    cœurs sur la cible) → réconciliation des câbles inter-nœuds."""
    from .vmlocks import verrou_vmid
    from .database import (db_add_alert, db_upsert_container_docker, get_db)

    plan = plan_migration(vmid, node_cible_id)
    etapes = []
    if plan["refus"]:
        return {"ok": False, "plan": plan, "etapes": etapes,
                "erreur": "migration refusée : " + " / ".join(plan["refus"])}
    if not plan["ok"] and not forcer:
        echecs = [v["nom"] for v in plan["verifications"] if not v["ok"]]
        return {"ok": False, "plan": plan, "etapes": etapes,
                "erreur": "vérifications en échec (%s) — relancer avec forcer=1 pour passer outre"
                          % ", ".join(echecs)}

    with verrou_vmid(vmid, op="migrate"):
        c = db_get_container(vmid)
        if not c:
            return {"ok": False, "plan": plan, "etapes": etapes, "erreur": "conteneur disparu"}
        dc = _dc(c)
        kind, params = dc.get("type"), (dc.get("params") or {})
        hostname = c.get("hostname") or ""
        src_id = c.get("node_id")
        src = db_get_node(src_id) or {}
        cible = db_get_node(node_cible_id) or {}
        name = c.get("docker_name") or ("bobi-cmp-%s" % vmid)

        # TISSU : un assembleur déplacé ne doit pas laisser ses shards derrière lui. Ils produisent
        # leurs tuiles dans le /dev/shm du nœud d'origine, que l'assembleur ne lira plus — ils
        # tourneraient donc à vide, en consommant cœurs et GPU, pendant que le mur n'affiche rien.
        # On démonte AVANT de déplacer : le mur repart en monolithe (son deploy_config porte les
        # params LOGIQUES), et le tissu le re-découpera sur la cible s'il y sature.
        try:
            from . import compositor_fabric as _cf3
            from .database import db_fabric_delete as _dfd, db_fabric_all as _dfa
            _shards = (_cf3.shards_par_parent() or {}).get(vmid) or []
            if _shards:
                from . import containers as _ct3
                for _sv in _shards:
                    try:
                        _ct3.detruire_container(int(_sv))
                    except Exception as _e:                                # noqa: BLE001
                        log.warning("migration %s : shard %s non détruit (%s)", vmid, _sv, _e)
                for _row in _dfa():
                    _r = dict(_row)
                    if str(_r.get("ref")) in {str(x) for x in _shards}:
                        _dfd(_r["signature"])
                _dfd("asm:%s" % vmid)
                etapes.append({"etape": "démontage du tissu", "ok": True,
                               "detail": "%d shard(s) détruit(s) — le mur repart en monolithe"
                                         % len(_shards)})
        except Exception as _e:                                            # noqa: BLE001
            etapes.append({"etape": "démontage du tissu", "ok": False, "detail": str(_e)})

        ok, detail = _retirer_docker(src, name)
        etapes.append({"etape": "retrait du nœud source", "ok": ok, "detail": detail})

        for lib, fn in (("cœurs", "core_pool"), ("GPU", "gpu_pool")):
            try:
                mod = __import__("app.%s" % fn, fromlist=["x"])
                (mod.release_cores if fn == "core_pool" else mod.release_gpu)(vmid)
                etapes.append({"etape": "libération %s (source)" % lib, "ok": True, "detail": ""})
            except Exception as _e:                                        # noqa: BLE001
                etapes.append({"etape": "libération %s (source)" % lib, "ok": False,
                               "detail": str(_e)})

        # Bascule de la ligne. L'adresse et les cœurs sont REMIS À ZÉRO : ils appartiennent au nœud
        # source (pool de pinning, plage macvlan) et seront réattribués sur la cible au déploiement.
        db_upsert_container_docker(vmid, hostname, node_cible_id, name, status="created")
        # Les CŒURS épinglés appartiennent au pool du nœud source : ils doivent être réattribués
        # sur la cible. L'ADRESSE, elle, est allouée sur un plan macvlan commun à la flotte — la
        # remettre à zéro forçait une réallocation inutile, et si le redéploiement ne la
        # repersistait pas, l'orchestrateur perdait le contact avec un conteneur pourtant vivant
        # (vécu : `docker_ip` NULL en base alors que le conteneur répondait sur son IP d'origine).
        # On la conserve donc ; `allocate_container_ip` est idempotent et la revalidera.
        try:
            with get_db() as db:
                db.execute("UPDATE containers SET pinned_cores=NULL WHERE vmid=?", (vmid,))
                db.commit()
        except Exception as _e:                                            # noqa: BLE001
            log.warning("migration %s : remise à zéro des cœurs (%s)", vmid, _e)
        etapes.append({"etape": "bascule de node_id", "ok": True,
                       "detail": "%s → %s (vmid conservé)" % (src.get("name"), cible.get("name"))})

        try:
            from .deploy import deployer_script
            ok_dep = bool(deployer_script(vmid, kind, params))
        except Exception as _e:                                            # noqa: BLE001
            ok_dep, _err = False, str(_e)
            etapes.append({"etape": "redéploiement sur la cible", "ok": False, "detail": _err})
        else:
            etapes.append({"etape": "redéploiement sur la cible", "ok": ok_dep, "detail": ""})

    # Hors verrou : les câbles devenus inter-nœuds réclament une réplication, ceux devenus locaux
    # n'en ont plus besoin. Idempotent.
    try:
        from .deploy import reconcile_fabric_node as _rfn
        _rfn(node_cible_id)      # le tissu re-découpe sur la cible si le mur y sature
        etapes.append({"etape": "réconciliation du tissu", "ok": True, "detail": ""})
    except Exception as _e:                                                # noqa: BLE001
        etapes.append({"etape": "réconciliation du tissu", "ok": False, "detail": str(_e)})
    try:
        from services import rdma as _rdma
        _rdma.reconcilier_cables(force=True)
        etapes.append({"etape": "réconciliation des câbles", "ok": True, "detail": ""})
    except Exception as _e:                                                # noqa: BLE001
        etapes.append({"etape": "réconciliation des câbles", "ok": False, "detail": str(_e)})

    ok_global = all(e["ok"] for e in etapes if e["etape"] != "libération GPU (source)")
    _p = {"hostname": hostname, "vmid": vmid, "src": src.get("name"), "cible": cible.get("name")}
    if ok_global:
        db_add_alert("alert.deploy.migration_ok", "info", vmid=vmid, node_id=node_cible_id,
                     kind="deploy", params=_p)
    else:
        db_add_alert("alert.deploy.migration_erreurs", "warning", vmid=vmid, node_id=node_cible_id,
                     kind="deploy", params=_p)
    return {"ok": ok_global, "plan": plan, "etapes": etapes}
