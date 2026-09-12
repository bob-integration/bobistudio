# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Inventaire docker de la flotte + réconciliation DB↔réalité (audit B5/B2).

Un seul `GET /v1/containers` PAR NŒUD (en parallèle) remplace les N `docker inspect`
par-conteneur de la boucle de surveillance — un nœud lent n'aveugle plus la boucle (B5) —
et fournit les deux directions de la réconciliation (B2) :
- DISPARU : conteneur censé tourner (`desired_state='running'`) absent de docker ps →
  alerte error à transition. NB piège --rm : un conteneur MTL ARRÊTÉ disparaît de docker ps
  LÉGITIMEMENT → « absent » n'est un incident QUE si desired_state='running'.
- ORPHELIN : conteneur `bobi-mtl-*`/`bobi-cmp-*` présent sur un nœud mais inconnu de la DB
  (ou rattaché à un autre nœud) → alerte warning à transition. AUCUNE destruction automatique.

Alerte à transition (pattern membw/node_health) : dicts d'état en mémoire, seed muet au
1er passage, info au retour à la normale.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .database import (db_get_nodes, db_get_container, db_update_status, db_update_fps,
                       db_add_alert)
from .episodes import EtatEpisodes as _Episodes

log = logging.getLogger(__name__)

# États de transition (mémoire process, reset au restart → seed muet au 1er passage)
_gone_prev = {}      # vmid → bool (True = déjà alerté disparu) — cache RAM du chemin chaud
_orphan_prev = {}    # (node_id, name) → bool — idem
# Les MÊMES états, SURVIVANT au redémarrage (cf. app/episodes.py) : un conteneur DISPARU le reste
# quand l'orchestrateur redémarre, et le ré-annoncer à chaque boot est du bruit pur. `_orphan_prev`
# garde en plus sa purge par-nœud existante (un résiduel détruit n'a rien à « résoudre »).
_episodes_gone = _Episodes("fleet_gone")
_episodes_orphan = _Episodes("fleet_orphan")
_MANAGED_RE = re.compile(r"^bobi-(?:mtl|cmp)-(\d+)$")


def _statut(entry):
    """STATUS humain de docker ps (« Up 3 hours » / « Exited (0) 2 days ago »…) → running|stopped."""
    return "running" if str(entry.get("status") or "").startswith("Up") else "stopped"


def poll_nodes():
    """Inventaire de tous les nœuds à agent, EN PARALLÈLE (motif node_health).
    → (states, unreachable, sans_agent) :
      states      = {node_id: {docker_name: "running"|"stopped"}}
      unreachable = {node_id} agents injoignables (on ne touche pas à leurs conteneurs)
      sans_agent  = {node_id} nœuds legacy sans agent_url (repli par-vmid du caller)."""
    from . import node_driver
    try:
        nodes = db_get_nodes()
    except Exception as e:
        log.error(f"fleet_status: lecture nodes: {e}")
        return {}, set(), set()
    avec_agent = [n for n in nodes if (n.get("agent_url") or "").strip()]
    sans_agent = {n["id"] for n in nodes if not (n.get("agent_url") or "").strip()}
    states, unreachable = {}, set()
    if not avec_agent:
        return states, unreachable, sans_agent

    def _un(node):
        return node["id"], node_driver.list_containers(node)

    with ThreadPoolExecutor(max_workers=min(8, len(avec_agent))) as ex:
        for nid, lst in ex.map(_un, avec_agent):
            if lst is None:
                unreachable.add(nid)
            else:
                states[nid] = {e.get("name"): _statut(e) for e in lst if e.get("name")}
    return states, unreachable, sans_agent


def status_of(states, unreachable, sans_agent, c):
    """Statut d'un conteneur DB d'après l'inventaire par-nœud.
    → "running" | "stopped" | "absent" | None (nœud injoignable : NE PAS transiter) |
      "fallback" (nœud sans agent : le caller garde le chemin docker inspect par-vmid)."""
    nid = c.get("node_id")
    if nid in unreachable:
        return None
    if nid in sans_agent or nid not in states:
        return "fallback"
    name = c.get("docker_name") or ""
    if not name:
        # Nom docker inconnu en DB (ligne jamais déployée en docker) : dériver du vmid.
        name = f"bobi-cmp-{c['vmid']}"
    return states[nid].get(name, "absent")


def _nom_noeud(nid):
    try:
        for n in db_get_nodes():
            if n["id"] == nid:
                return n.get("name") or n.get("host") or str(nid)
    except Exception:
        pass
    return str(nid)


def orphans_actuels():
    """Orphelins actuellement constatés → [{node_id, node, name}] (panneau Points d'attention).
    Source = _orphan_prev (rafraîchi à chaque tick de surveillance sur les nœuds joignables)."""
    out = []
    for (nid, name), flag in sorted(_orphan_prev.items()):
        if flag:
            out.append({"node_id": nid, "node": _nom_noeud(nid), "name": name})
    return out


def est_orphelin(node_id, name):
    """Garde-fou du destroy UI : on ne détruit que ce que la réconciliation flagge ENCORE
    orphelin (nom géré bobi-*), jamais un nom arbitraire."""
    return bool(_MANAGED_RE.match(name or "")) and bool(_orphan_prev.get((int(node_id), name)))


def oublier_orphelin(node_id, name):
    _orphan_prev.pop((int(node_id), name), None)


# ═══ NŒUD TOMBÉ ═════════════════════════════════════════════════════════════════════════════
#
# Constaté le 2026-08-02 : dl360-1 (le nœud du moteur 2110) s'est arrêté à 22h18 — injoignable sur
# ses TROIS réseaux, ARP muet depuis le même segment L2. Une heure plus tard, `nodes.status` disait
# toujours `up`, `last_seen` était figé, ses trois conteneurs s'affichaient `running` avec des `fps`
# gelés à leur dernière valeur, et AUCUNE alerte ne nommait le nœud. Deux causes distinctes :
#
#   1. `node_driver.refresh()` est la SEULE fonction qui écrive `status="down"` périodiquement, et
#      elle n'a AUCUN APPELANT. Toutes les autres écritures forcent `"up"` ; les seuls `"down"` du
#      dépôt sont dans les routes d'enrôlement, donc des gestes MANUELS. Le statut d'un nœud ne
#      pouvait que monter. Conséquence armée depuis longtemps : `docker_compute._eligible()` teste
#      `status != "down"` — condition jamais satisfaite — donc un nœud MORT restait éligible aux
#      déploiements.
#   2. `poll_nodes()` calcule pourtant `unreachable` à CHAQUE tour, en parallèle, un appel par
#      nœud. L'information existait déjà et était JETÉE : elle ne servait qu'à empêcher les
#      transitions de conteneurs.
#
# On ne sonde donc rien de plus : on exploite le poll qui a déjà lieu. `refresh()` reste sans
# appelant — un second aller-retour HTTP par nœud et par tour n'apprendrait rien de neuf.
#
# ⚠ Un agent qui ne répond pas n'est PAS forcément une machine morte, et les remèdes n'ont rien à
# voir (relancer un service / aller voir la machine). D'où la sonde `_sonde_hote`.

# ⚠ Les seuils sont en TEMPS RÉEL, pas en nombre de tours — et c'est la première version de ce
# code qui l'a appris. Un tour de surveillance dure normalement 5 s, mais il attend les timeouts
# des nœuds qui ne répondent pas : avec dl360-1 mort, le tour est passé à 65 s. « 3 tours ≈ 15 s »
# devenait donc « 3 tours ≈ 3 minutes » EXACTEMENT dans la situation où la détection sert. Un seuil
# compté en tours se dilate avec la panne qu'il doit détecter.
NODE_KO_S       = 15.0      # injoignable depuis 15 s → bascule `nodes.status` à "down"
NODE_KO_ALERT_S = 60.0      # depuis 60 s → épisode persisté + alerte `error` (absorbe un agent
                            # relancé, un `docker restart`, une reconfiguration réseau brève)
LAST_SEEN_S     = 30.0      # `last_seen` réécrit au plus toutes les 30 s par nœud (c'est un
                            # horodatage de fraîcheur, pas une métrique : inutile d'écrire à 5 s)

_node_ko_depuis = {}        # node_id → monotonic du PREMIER poll en échec de la série
_node_ko_prev = {}          # node_id → bool (« déjà annoncé injoignable »)
_last_seen_ecrit = {}       # node_id → monotonic de la dernière écriture de last_seen
_cont_absent_prev = {}      # vmid → bool (conteneur déjà marqué `unreachable` pour cause de nœud)
_episodes_node = _Episodes("node_gone")


def _sonde_hote(node, timeout=2.0):
    """« L'agent est mort » ou « la machine est morte » ? → "agent" | "hote" | "agent_vivant" | None

    Un `connect()` TCP sur le port de l'agent tranche sans ICMP ni privilège particulier :

      - **refus** (`ECONNREFUSED`) → la pile TCP de l'hôte RÉPOND, personne n'écoute sur le port :
        la machine est vivante, c'est l'agent qui est absent. On relance un service.
      - **délai / hôte injoignable** → rien ne répond du tout : machine (ou réseau) tombée. Il faut
        aller voir la machine — et sans BMC renseigné, physiquement.
      - **connexion établie** alors que le poll a échoué → l'agent écoute mais traînait : ce n'est
        pas une panne de nœud, c'est de la lenteur. On ne déclare rien.

    ⚠ Un pare-feu en DROP produit un délai sur une machine parfaitement vivante. Le message dit donc
    « ne répond pas », JAMAIS « éteinte » : on rapporte ce qu'on observe, pas ce qu'on suppose.
    """
    import socket
    import urllib.parse
    host, port = None, 9100
    try:
        u = urllib.parse.urlsplit(node.get("agent_url") or "")
        host, port = u.hostname or node.get("host"), int(u.port or 9100)
    except (ValueError, TypeError):
        host = node.get("host")
    if not host:
        return None
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return "agent_vivant"
    except ConnectionRefusedError:
        return "agent"
    except OSError:
        return "hote"
    finally:
        try:
            s.close()
        except OSError:
            pass


def _redemarrage(node, outage_s):
    """Le nœud qui revient a-t-il REDÉMARRÉ ? → True/False/None (indéterminable).

    Refermer un incident par « de nouveau joignable » sans dire s'il a rebooté laisse l'exploitant
    avec deux histoires possibles et aucun moyen de trancher : un agent relancé ne perd rien, un
    reboot fait disparaître le moteur MTL (`docker run --rm`) et déclenche `node_recovery`.
    """
    from . import node_driver
    try:
        data = node_driver.health(node) or {}
        up = float(data.get("host_uptime_s") or 0)
    except (TypeError, ValueError, AttributeError) as e:
        log.debug("fleet_status._redemarrage(%s): %s", node.get("id"), e)
        return None
    if up <= 0:
        return None
    return up < (float(outage_s) + 60.0)     # marge : la panne inclut le temps de POST/boot


def evaluer_noeuds(states, unreachable):
    """Statut, épisode et alerte pour les nœuds injoignables. → {node_id: {...}} pour les conteneurs.

    Alerte à TRANSITION et à deux seuils gradués, sur le motif éprouvé de `metrics._injoignable` :
    le statut bascule vite (on cesse d'affirmer que tout va bien), l'alerte attend une minute (un
    agent relancé ou un `docker restart` ne doit pas réveiller qui que ce soit).
    """
    from datetime import datetime
    from .database import db_update_node
    infos = {}
    try:
        nodes = db_get_nodes()
    except Exception as e:
        log.error(f"fleet_status: lecture nodes (évaluation): {e}")
        return infos
    now = time.monotonic()
    for n in nodes:
        nid = n.get("id")
        if not (n.get("agent_url") or "").strip():
            continue                       # nœud legacy sans agent : hors de ce modèle
        if nid not in unreachable and nid not in states:
            continue                       # pas sondé à ce tour : ne rien conclure
        if nid not in _node_ko_prev:
            _node_ko_prev[nid] = bool(_episodes_node.get(nid))    # reprise après (re)démarrage
        nom = n.get("name") or n.get("host") or str(nid)

        # ── Le nœud ne répond pas ───────────────────────────────────────────────────────────
        if nid in unreachable:
            t0 = _node_ko_depuis.setdefault(nid, now)
            depuis = int(now - t0)
            if depuis < NODE_KO_S:
                continue                   # fenêtre normale d'un agent lent : on ne dit rien
            quoi = _sonde_hote(n)
            if quoi == "agent_vivant" and depuis < NODE_KO_ALERT_S:
                continue                   # l'agent écoute mais traîne : lenteur, pas panne
            if (n.get("status") or "") != "down":
                try:
                    db_update_node(nid, status="down")
                except Exception as e:
                    log.error(f"fleet_status: statut down {nom}: {e}")
            # ⚠ La cause est un champ VIVANT, affiché tel quel sur la fiche du conteneur : elle
            # porte donc un HORODATAGE ABSOLU, jamais une durée. Écrite une seule fois (voir
            # `marquer_absent`), une durée y vieillirait sans se corriger — « injoignable depuis
            # 81 s » resterait à l'écran une heure plus tard, ce qui est exactement le genre de
            # demi-vérité que cette fonction existe pour supprimer.
            infos[nid] = {"nom": nom, "depuis_s": depuis, "quoi": quoi,
                          "cause": "nœud %s injoignable depuis %s" % (
                              nom, datetime.now().strftime("%H:%M:%S"))}
            if depuis >= NODE_KO_ALERT_S and not _node_ko_prev.get(nid):
                if quoi == "hote":
                    cle = "alert.node.injoignable_hote"
                elif quoi == "agent":
                    cle = "alert.node.injoignable_agent"
                else:
                    cle = "alert.node.injoignable_autre"
                db_add_alert(cle, "error", node_id=nid, kind="node",
                             params={"n": nom, "depuis": depuis})
                _node_ko_prev[nid] = True
                _episodes_node.poser(nid, True)
            continue

        # ── Le nœud répond ──────────────────────────────────────────────────────────────────
        t0 = _node_ko_depuis.pop(nid, None)
        outage = int(now - t0) if t0 else 0
        # ⚠ PANNE VUE SEULEMENT APRÈS COUP. Les seuils ne sont évalués qu'aux instants d'ÉCHANTILLON,
        # et un tour de surveillance dure normalement 5 s… mais 65 s quand un nœud mort fait traîner
        # les sondes de la boucle. Une panne plus longue que le seuil d'alerte peut donc n'être
        # échantillonnée qu'AVANT ce seuil, puis se résoudre : aucune alerte, pour une coupure d'une
        # minute et demie sur un nœud de production. Constaté en éprouvant ce code sur r620-3 le
        # 2026-08-03 (110 s d'absence, statut basculé, zéro alerte).
        # Au rétablissement la durée TOTALE est connue exactement : si elle dépassait le seuil sans
        # avoir été annoncée, on le dit une fois, au passé. Mieux vaut une alerte tardive qu'une
        # panne qui n'a jamais existé dans le journal.
        if outage >= NODE_KO_ALERT_S and not _node_ko_prev.get(nid):
            db_add_alert("alert.node.injoignable_resolu_tardif", "warning",
                         node_id=nid, kind="node", params={"n": nom, "outage": outage})
        if _node_ko_prev.get(nid):
            reboot = _redemarrage(n, outage)
            if reboot is True:
                cle = "alert.node.retabli_reboot"
            elif reboot is False:
                cle = "alert.node.retabli_sans_reboot"
            else:
                cle = "alert.node.retabli_reboot_inconnu"
            db_add_alert(cle, "info", node_id=nid, kind="node",
                         params={"n": nom, "outage": outage or "?"})
            _node_ko_prev[nid] = False
            _episodes_node.retirer(nid)
        # `last_seen` à CHAQUE poll réussi (throttlé) : il ne s'écrivait qu'au changement de
        # version d'agent, d'où un horodatage figé des jours durant — donc inutilisable pour
        # juger de la fraîcheur d'un nœud, ce qui est pourtant son seul rôle.
        if (n.get("status") or "") != "up" or now - _last_seen_ecrit.get(nid, 0) >= LAST_SEEN_S:
            _last_seen_ecrit[nid] = now
            try:
                db_update_node(nid, status="up",
                               last_seen=datetime.now().isoformat(timespec="seconds"))
            except Exception as e:
                log.error(f"fleet_status: last_seen {nom}: {e}")
    return infos


def marquer_absent(c, infos):
    """Le nœud du conteneur ne répond plus : le DIRE, sans prétendre savoir si le conteneur tourne.

    La boucle de surveillance faisait ici un `continue` sec, avec un commentaire juste — « ne pas
    fabriquer de fausse transition » : un timeout d'agent n'est pas un `stopped`. Mais le saut
    portait sur TOUT le traitement du conteneur, y compris `_soumettre_metrics`, c'est-à-dire la
    machinerie `unreachable` de `metrics` (seuils gradués, épisode, badge). Elle n'a donc jamais
    tourné pour un nœud mort, et le conteneur restait affiché `running` avec sa dernière cadence.

    Ne jamais écrire `stopped` : on ne sait pas. `unreachable` dit exactement ce qu'on sait.
    """
    nid = c.get("node_id")
    info = infos.get(nid)
    if not info:
        return                              # pas encore au seuil : la prudence reste de mise
    vmid = c.get("vmid")
    if _cont_absent_prev.get(vmid):
        return                              # déjà marqué : ne pas réécrire à chaque tour
    try:
        db_update_status(vmid, "unreachable", cause=info["cause"])
        db_update_fps(vmid, None)           # une cadence figée est un MENSONGE, pas une mesure
    except Exception as e:
        log.error(f"fleet_status: marquage absent {vmid}: {e}")
        return
    _cont_absent_prev[vmid] = True


def oublier_absent(vmid):
    """Le nœud est revenu : la prochaine passe normale réécrira statut et cadence."""
    if _cont_absent_prev.pop(vmid, None):
        try:
            db_update_status(vmid, "unknown", cause="")
        except Exception as e:
            log.error(f"fleet_status: oubli absent {vmid}: {e}")


def reconcile(states, unreachable):
    """Réconciliation DB↔réalité (B2) sur les nœuds JOIGNABLES uniquement."""
    from .node_recovery import _desired_running
    # ── Direction 1 : DISPARU (censé tourner, absent de docker ps) ──
    for nid, inv in states.items():
        try:
            mtl, compute = _desired_running(nid)
        except Exception as e:
            log.error(f"fleet_status: desired_running({nid}): {e}")
            continue
        for prefix, group in (("bobi-mtl", mtl), ("bobi-cmp", compute)):
          for c in group:
            vmid = c["vmid"]
            name = c.get("docker_name") or f"{prefix}-{vmid}"
            gone = name not in inv
            if vmid not in _gone_prev:
                _gone_prev[vmid] = bool(_episodes_gone.get(vmid))   # reprise après (re)démarrage
            if gone and not _gone_prev.get(vmid):
                try:
                    db_update_status(vmid, "stopped")
                except Exception:
                    pass
                db_add_alert("alert.deploy.container_disparu", "error", vmid=vmid, node_id=nid,
                             kind="deploy",
                             params={"h": c.get("hostname") or vmid, "dn": name,
                                     "n": _nom_noeud(nid)})
            elif not gone and _gone_prev.get(vmid):
                db_add_alert("alert.deploy.container_revenu", "info", vmid=vmid, node_id=nid,
                             kind="deploy",
                             params={"h": c.get("hostname") or vmid, "dn": name,
                                     "n": _nom_noeud(nid)})
            _gone_prev[vmid] = gone
            if gone:
                _episodes_gone.poser(vmid, True)
            else:
                _episodes_gone.retirer(vmid)

    # ── Direction 2 : ORPHELIN (bobi-* sur le nœud, inconnu de la DB / mauvais nœud) ──
    for nid, inv in states.items():
        for name in inv:
            m = _MANAGED_RE.match(name or "")
            if not m:
                continue          # conteneur non géré par nous (passerelle tierce, etc.)
            key = (nid, name)
            try:
                c = db_get_container(int(m.group(1)))
            except Exception:
                c = None
            if not c:
                orphan = True                                    # vmid inconnu de la DB
            else:
                # Ligne DB trouvée : orphelin si le conteneur vit sur le MAUVAIS nœud, ou si
                # son nom ne correspond pas au docker_name enregistré (résidu d'un ancien run).
                orphan = (c.get("node_id") != nid) or ((c.get("docker_name") or name) != name)
            if key not in _orphan_prev:
                _orphan_prev[key] = bool(_episodes_orphan.get(key))
            if orphan and not _orphan_prev.get(key):
                db_add_alert("alert.deploy.container_orphelin", "warning", node_id=nid,
                             kind="deploy", params={"cn": name, "n": _nom_noeud(nid)})
            elif not orphan and _orphan_prev.get(key):
                db_add_alert("alert.deploy.container_plus_orphelin", "info", node_id=nid,
                             kind="deploy", params={"cn": name, "n": _nom_noeud(nid)})
            _orphan_prev[key] = orphan
            if orphan:
                _episodes_orphan.poser(key, True)
            else:
                _episodes_orphan.retirer(key)
        # purge des clés d'orphelins qui ont physiquement disparu du nœud (pas d'info « résolu » :
        # la résolution normale EST la destruction du résiduel)
        for key in [k for k in _orphan_prev if k[0] == nid and k[1] not in inv]:
            _orphan_prev.pop(key, None)
            _episodes_orphan.retirer(key)
