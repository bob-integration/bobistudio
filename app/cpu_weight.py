# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""POIDS CPU d'un conteneur — sa PART de CPU sous saturation. Distinct de sa PLACE.

★ DEUX NOTIONS, ET C'EST TOUT L'INTÉRÊT (arbitrage de Cyril, 2026-10-30).

    La **PLACE** (`docker_compute.cpu_mode_of`) dit OÙ le conteneur s'exécute : des cœurs
    DÉDIÉS qui n'appartiennent qu'à lui, ou le POOL PARTAGÉ du nœud (éventuellement plafonné
    par un quota `--cpus`). C'est une question d'exclusivité.

    Le **POIDS** (ce module) dit qui passe devant QUAND IL N'Y A PLUS ASSEZ DE CPU. C'est un
    rapport, pas une réservation : `--cpu-shares` n'a d'effet qu'en cas de saturation, et
    seulement entre conteneurs qui se disputent les MÊMES cœurs.

Les deux étaient confondus dans un seul champ `priority: batch`, qui enlevait à la fois
l'épinglage ET le poids. Un conteneur pouvait donc être « partagé » sans pouvoir être
« partagé mais prioritaire », et « dédié mais effaçable » n'existait pas du tout.

★ CE QUE LE POIDS NE FAIT PAS, et qui doit être DIT dans l'interface :
  - il ne fait RIEN sur un cœur dédié : il n'y a personne à qui prendre le CPU ;
  - il ne protège PAS du frère HyperThreading (cf. `core_pool.vue_coeurs`) : les deux threads
    d'un cœur physique se disputent des unités d'exécution, pas des parts d'ordonnanceur ;
  - il ne garantit AUCUN plancher : un conteneur à 2048 face à seize conteneurs à 1024 n'a pas
    « deux fois plus de CPU », il a deux fois plus de poids dans le partage du moment.

★ QUATRE NIVEAUX NOMMÉS, JAMAIS UN NOMBRE LIBRE. 1024 est le défaut de Docker et 128 était
écrit en dur pour le transcodeur : deux valeurs, aucune échelle. Un champ numérique libre
aurait produit des 1000, des 1500 et des 900 dont personne ne saurait dire l'intention six mois
plus tard — alors que « Dédié » et « Arrière-plan » se relisent.
"""
import logging
import shlex

log = logging.getLogger(__name__)

# Les quatre niveaux, du plus prioritaire au moins prioritaire. Les VALEURS sont des poids
# `--cpu-shares` relatifs : seul leur RAPPORT compte (2048 vaut deux parts pour une de 1024).
WEIGHTS = {
    "dedicated":  2048,   # Dédié — cœurs dédiés (la place suit le niveau, 2026-11-07)
    "normal":     1024,   # Normal — le défaut de Docker, et le nôtre
    "background":  256,   # Arrière-plan — moniteur, passerelle, aperçus : cède aux flux d'antenne
    "batch":       128,   # Batch — transcodage de fichiers : prend ce qui reste
}
LEVELS = ("dedicated", "normal", "background", "batch")
DEFAULT = "normal"
# Former names still accepted on input (saved projects, macros, manifests of other instances):
# « onair » was renamed « dedicated » on 2026-11-07, with the label « Antenne » → « Dédié ».
ALIASES = {"onair": "dedicated"}
# Poids que Docker applique quand on ne lui demande rien : la valeur à laquelle se compare un
# conteneur dont `HostConfig.CpuShares` vaut 0 (« non précisé »), pas une absence de poids.
DOCKER_DEFAUT = 1024


def value(level):
    """Poids `--cpu-shares` d'un niveau nommé. Niveau inconnu → le défaut (jamais une exception :
    un manifeste fautif ne doit pas empêcher un déploiement, il doit être journalisé)."""
    lvl = normalize(level)
    if lvl in WEIGHTS:
        return WEIGHTS[lvl]
    if lvl:
        log.warning("cpu weight level %r is unknown, falling back to %s", level, DEFAULT)
    return WEIGHTS[DEFAULT]


def normalize(level):
    """Level name as stored: lower case, a former name mapped to its current one."""
    lvl = str(level or "").strip().lower()
    return ALIASES.get(lvl, lvl)


def niveau_valide(level):
    return normalize(level) in WEIGHTS


def level_of(res):
    """Niveau de poids d'un profil de ressources EFFECTIF (cf. `docker_compute
    .effective_resources`) → une des clés de `WEIGHTS`.

    Ordre : `cpu_weight` du profil (manifeste ou surcharge du conteneur, déjà fusionnés par
    `effective_resources`) > repli sur l'ancien `priority: batch` > défaut.

    Le repli n'est pas une politesse : `priority: batch` est écrit dans des manifestes installés
    (transcodeur) et dans des `deploy_config` persistés. Le retirer d'ici relèverait en silence
    le transcodeur au poids normal — il mangerait le CPU des conteneurs d'antenne."""
    res = res or {}
    lvl = normalize(res.get("cpu_weight"))
    if lvl in WEIGHTS:
        return lvl
    if str(res.get("priority") or "").strip().lower() == "batch":
        return "batch"
    return DEFAULT


def source_of(res, c):
    """D'OÙ vient le niveau appliqué : 'override' (ce conteneur), 'manifest' (son type),
    'priority' (l'ancien champ du manifeste), 'default'. L'interface le dit : un exploitant qui
    voit « Arrière-plan » doit savoir s'il l'a choisi ou s'il l'a hérité.

    `res` is the type's MANIFEST profile, not the effective one: since 2026-11-07 the effective
    profile always carries the resolved level, so it can no longer tell where it came from. A
    manifest asking for `pin` makes its type dedicated by default: that is the manifest speaking."""
    if (c or {}).get("cpu_weight_override"):
        return "override"
    res = res or {}
    if normalize(res.get("cpu_weight")) in WEIGHTS:
        return "manifest"
    if str(res.get("priority") or "").strip().lower() == "batch":
        return "priority"
    if res.get("pin"):
        return "manifest"
    return "default"


def for_container(c, deploy_type=None):
    """→ {niveau, valeur, source} pour une ligne `containers`. Ne touche à rien."""
    from .docker_compute import effective_resources, _type_of
    c = c or {}
    t = deploy_type if deploy_type is not None else _type_of(c)
    try:
        res = effective_resources(t, c) or {}
    except Exception as e:                                                   # noqa: BLE001
        log.warning("cpu weight for %s: effective profile unreadable (%s)", c.get("vmid"), e)
        res = {}
    lvl = level_of(res)
    from . import plugins
    manifest = ((plugins.get(t) or {}).get("resources") or {}) if t else {}
    return {"niveau": lvl, "valeur": value(lvl), "source": source_of(manifest, c)}


# ─── Application À CHAUD ──────────────────────────────────────────────────────────────────────
# ★ POURQUOI SANS RECRÉATION, contrairement aux cœurs. Un cpuset ne se change pas à chaud sans
# conséquence sur le placement des threads déjà lancés ; un poids, si — c'est un rapport que
# l'ordonnanceur relit à chaque arbitrage. Recréer un conteneur pour changer son poids serait
# couper l'antenne pour un geste d'intendance (même raison que `core_pool.reappliquer_partages`).
#
# ★ ET POURQUOI LE POIDS EST HORS DE LA SIGNATURE DE SPEC (cf. `docker_compute._signature_spec`).
# L'y laisser ferait recréer le conteneur au déploiement suivant — la coupure qu'on vient
# d'éviter, différée et donc imprévisible. En échange, le poids POSÉ est MESURÉ (sonde de
# `placement`) et confronté au poids VOULU : un `docker update` manqué se voit dans la vue au
# lieu d'être rattrapé par une recréation surprise.

def appliquer(node_id, vmid, niveau=None):
    """`docker update --cpu-shares` sur le conteneur en marche. → (ok, msg).

    `niveau=None` : applique le niveau EFFECTIF (celui de la base) — c'est la réparation.
    N'écrit RIEN en base : l'appelant décide s'il persiste une surcharge (cf. `regler`)."""
    from . import node_driver
    from .database import db_get_container, db_get_node
    c = db_get_container(vmid)
    if not c:
        return False, "conteneur %s inconnu" % vmid
    node = db_get_node(c.get("node_id") or node_id) or {}
    if not node:
        return False, "nœud inconnu"
    nom = (c.get("docker_name") or "").strip()
    if not nom:
        return False, "conteneur %s sans nom Docker (jamais déployé)" % vmid
    poids = value(niveau) if niveau else for_container(c)["valeur"]
    try:
        rc, out, err = node_driver.host_exec(
            node, "docker update --cpu-shares=%d %s" % (poids, shlex.quote(nom)), timeout=30)
    except Exception as e:                                                   # noqa: BLE001
        return False, str(e)[:200]
    if rc != 0:
        return False, ((err or out) or "rc=%s" % rc).strip()[-200:]
    return True, "%s → %d" % (nom, poids)


def regler(vmid, niveau, reset=False):
    """Pose le niveau de poids PROPRE à ce conteneur et l'applique à chaud.

    → {"ok", "niveau", "applique", "err"}. `reset=True` rend le conteneur à son manifeste.

    L'écriture en base est FAITE même si l'application à chaud échoue (nœud injoignable) : le
    réglage est une intention, elle doit survivre ; c'est la vue des cœurs qui dira que le poids
    posé n'est pas encore le poids appliqué."""
    from .database import db_get_container, db_set_resources_override
    if not reset and not niveau_valide(niveau):
        return {"ok": False, "err": "niveau inconnu : %r (attendu %s)"
                                    % (niveau, ", ".join(LEVELS))}
    c = db_get_container(vmid)
    if not c:
        return {"ok": False, "err": "conteneur %s inconnu" % vmid}
    from .docker_compute import effective_resources, _type_of, apply_place_hot
    before = c.get("cpu_weight_override")
    was_dedicated = bool(effective_resources(_type_of(c), c).get("pin"))
    db_set_resources_override(vmid, cpu_weight=("" if reset else normalize(niveau)))
    c = db_get_container(vmid) or c
    # ★ The place follows the level (2026-11-07): entering or leaving « Dédié » moves the
    # container to or from dedicated cores, hot. Refused (no room) → the level is put back as it
    # was: a level that says « dedicated » on a shared container would be the lie this rule removes.
    place = None
    if bool(effective_resources(_type_of(c), c).get("pin")) != was_dedicated:
        ok_place, place = apply_place_hot(vmid)
        if not ok_place:
            db_set_resources_override(vmid, cpu_weight=(before or ""))
            return {"ok": False, "err": place, "place_refused": True}
    eff = for_container(c)
    ok, msg = appliquer(c.get("node_id"), vmid)
    return {"ok": True, "niveau": eff["niveau"], "valeur": eff["valeur"],
            "source": eff["source"], "applique": ok, "err": None if ok else msg, "place": place}


def poser_defaut(vmid, niveau):
    """Pose le niveau `niveau` sur un conteneur que l'ORCHESTRATEUR crée pour son propre service
    (moniteur WebRTC, passerelle, aperçus) — et SEULEMENT s'il n'en a pas déjà un.

    ★ POURQUOI PAS UNE LISTE DE TYPES DANS LE CODE. Le moniteur est un `streamer`, exactement
    comme un flux de diffusion : son type ne dit pas son rôle. Un défaut par type le mettrait
    donc en arrière-plan AUSSI pour un streamer d'antenne. C'est le créateur du conteneur qui
    sait à quoi il sert, et c'est lui qui le dit, ici, à la création.

    ★ ET POURQUOI « seulement s'il n'en a pas ». Sans cette garde, chaque recréation écraserait
    le choix de l'exploitant : il relèverait son moniteur en « Normal » et le retrouverait en
    « Arrière-plan » au prochain redéploiement, sans explication."""
    from .database import db_get_container, db_set_resources_override
    c = db_get_container(vmid)
    if not c or c.get("cpu_weight_override"):
        return False
    if not niveau_valide(niveau):
        log.warning("default cpu weight %r for container %s is unknown, ignored", niveau, vmid)
        return False
    db_set_resources_override(vmid, cpu_weight=str(niveau).strip().lower())
    log.info("container %s created for an orchestrator service: default cpu weight %s",
             vmid, niveau)
    return True


def reappliquer(node_id, force=False):
    """Remet à TOUS les conteneurs du nœud le poids que leur profil dit — ceux dont le poids
    MESURÉ diverge, et eux seuls.

    → {"faits": [{vmid, avant, apres}], "echecs": [{vmid, err}], "inchanges": [vmid…],
       "non_mesures": [vmid…]}

    Un conteneur dont le poids posé n'est pas mesurable n'est PAS corrigé à l'aveugle (même règle
    que `core_pool.reappliquer_partages`) : sans mesure, « inchangé » ne voudrait rien dire."""
    from .database import db_get_containers
    out = {"faits": [], "echecs": [], "inchanges": [], "non_mesures": []}
    mesure = poids_appliques(node_id, force=force)
    for c in sorted((db_get_containers() or []), key=lambda x: x["vmid"]):
        if c.get("node_id") != node_id:
            continue
        nom = (c.get("docker_name") or "").strip()
        if not nom:
            continue
        voulu = for_container(c)["valeur"]
        avant = mesure.get(nom)
        if avant is None:
            out["non_mesures"].append(c["vmid"])
            continue
        if avant == voulu:
            out["inchanges"].append(c["vmid"])
            continue
        ok, msg = appliquer(node_id, c["vmid"], None)
        if ok:
            out["faits"].append({"vmid": c["vmid"], "avant": avant, "apres": voulu})
        else:
            out["echecs"].append({"vmid": c["vmid"], "err": msg})
    if out["faits"]:
        from .database import db_add_alert, db_get_node
        from . import core_pool
        node = db_get_node(node_id) or {}
        db_add_alert("alert.resource.poids_reappliques", "info", node_id=node_id, kind="resource",
                     params={"n": node.get("name") or node_id, "count": len(out["faits"]),
                             "noms": core_pool._noms_vmids([f["vmid"] for f in out["faits"]])})
        try:
            from . import placement
            placement._dernier.pop(str(node_id), None)   # la mesure vient de devenir fausse
        except Exception as e:                                               # noqa: BLE001
            log.debug("reappliquer: placement cache not invalidated (%s)", e)
    return out


def poids_appliques(node_id, force=False):
    """Poids RÉELLEMENT posé par conteneur, MESURÉ sur le nœud → {nom docker: shares}.

    `{}` si le nœud est injoignable (jamais une supposition). Un `CpuShares` à 0 est traduit en
    `DOCKER_DEFAUT` : Docker ne dit pas « aucun poids », il dit « je n'ai rien eu à poser »."""
    try:
        from . import placement
        r = placement.releve_cache(node_id, force=force)
    except Exception as e:                                                   # noqa: BLE001
        log.warning("applied cpu weights on node %s: %s", node_id, e)
        return {}
    out = {}
    for c in ((r or {}).get("containers") or []):
        sh = c.get("cpu_shares")
        if sh is None:
            continue
        out[c.get("name") or ""] = DOCKER_DEFAUT if int(sh) == 0 else int(sh)
    return out


# ─── Macros (fournisseur du CŒUR, cf. app/core_plugins.py:CORE_PROVIDERS) ─────────────────────
# Règle CLAUDE.md « Exposer aux macros » : une capacité que les macros ne voient pas est une
# capacité morte. Le poids est une action DISCRÈTE (quatre valeurs nommées) → `actions[]`, et un
# état lisible en condition → `states[]`.
MACRO_PROVIDER = {
    "id": "cpu_weight",
    "label": "Poids CPU",
    "actions": [
        {"id": "set", "label": "Régler le poids CPU d'un conteneur",
         "availability": "project", "params": [
             {"key": "vmid", "label": "Conteneur", "type": "select", "options_hook": True},
             {"key": "niveau", "label": "Niveau", "type": "select", "options_hook": True,
              "default": DEFAULT},
         ]},
        {"id": "reset", "label": "Rendre le poids CPU au manifeste",
         "availability": "project", "params": [
             {"key": "vmid", "label": "Conteneur", "type": "select", "options_hook": True},
         ]},
    ],
    "states": [
        {"id": "niveau", "label": "Poids CPU d'un conteneur", "type": "text"},
        {"id": "poids_divergents", "label": "Conteneurs dont le poids posé n'est pas appliqué",
         "type": "number"},
    ],
}

# Libellés des niveaux pour les menus de macros. L'INTERFACE, elle, passe par i18n
# (`cores.weight.*`) : ces libellés-ci sont ceux du catalogue d'actions, qui n'est pas traduit.
LABELS = {"dedicated": "Dédié (2048)", "normal": "Normal (1024)",
          "background": "Arrière-plan (256)", "batch": "Batch (128)"}


def action_options(action_id, param_key, pid=None):
    """Listes vivantes — nom imposé par `core_plugins.service_actions`."""
    if param_key == "vmid":
        from .database import db_get_containers
        return [{"value": str(c["vmid"]),
                 "label": "%s (%s)" % (c.get("hostname") or c["vmid"], c["vmid"])}
                for c in (db_get_containers() or []) if c.get("vmid")]
    if action_id == "set" and param_key == "niveau":
        return [{"value": l, "label": LABELS[l]} for l in LEVELS]
    return []


def run_action(action_id, params, ctx):
    if action_id not in ("set", "reset"):
        raise RuntimeError("action inconnue : %s" % action_id)
    try:
        vmid = int((params or {}).get("vmid"))
    except (TypeError, ValueError):
        raise RuntimeError("conteneur à choisir")
    r = regler(vmid, (params or {}).get("niveau"), reset=(action_id == "reset"))
    if not r.get("ok"):
        # Une macro qui échoue DOIT le dire (cf. app/container_net.py) : un réglage avalé
        # donnerait une macro « réussie » dont l'effet n'a pas eu lieu.
        raise RuntimeError(r.get("err") or "réglage refusé")
    if not r.get("applique"):
        # Posé en base mais PAS appliqué au conteneur : c'est un demi-succès, et une macro ne
        # doit pas le présenter comme un succès. Le réglage survit (il s'appliquera à la
        # réapplication ou à la recréation), mais la macro échoue bruyamment.
        raise RuntimeError("poids enregistré (%s) mais non appliqué au conteneur : %s"
                           % (r.get("niveau"), r.get("err")))
    return {"ok": True, "message": "%s → %s (%s)" % (vmid, r["niveau"], r["valeur"])}


def read_state(state_id, ctx):
    from .database import db_get_containers, db_get_nodes
    if state_id == "niveau":
        # Sans conteneur désigné, l'état n'a pas de sens : les conditions de macro visent un
        # conteneur, et rendre « normal » par défaut serait une affirmation fausse.
        vmid = (ctx or {}).get("vmid")
        try:
            vmid = int(vmid)
        except (TypeError, ValueError):
            raise RuntimeError("état `niveau` : conteneur à désigner (vmid)")
        from .database import db_get_container
        c = db_get_container(vmid)
        if not c:
            raise RuntimeError("conteneur %s inconnu" % vmid)
        return for_container(c)["niveau"]
    if state_id == "poids_divergents":
        n = 0
        for node in (db_get_nodes() or []):
            mesure = poids_appliques(node.get("id"))
            if not mesure:
                continue
            for c in (db_get_containers() or []):
                if c.get("node_id") != node.get("id"):
                    continue
                pose = mesure.get((c.get("docker_name") or "").strip())
                if pose is not None and pose != for_container(c)["valeur"]:
                    n += 1
        return n
    raise RuntimeError("état inconnu : %s" % state_id)
