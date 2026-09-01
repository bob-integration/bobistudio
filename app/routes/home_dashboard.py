# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Agrégation de la home dashboard (/api/home/summary, une seule requête → tout : PTP, containers,
NMOS, topologie pipeline câblage, santé cluster, points d'attention) + /api/sources (sorties de la
flotte dérivées de la DB seule, pour les listes de sources de l'éditeur multiview).

_eff_node_id reste dans __init__.py (générique, aussi utilisé par _req_host) ; _fetch_plugin_state
vit désormais dans app/routes/cabling.py — importés localement ici (résolus à l'exécution d'une
requête, donc peu importe l'ordre d'import des modules)."""

import json
import logging
import time
from datetime import datetime

from flask import jsonify, request

from . import bp
from .shared import _load_dc
from ..auth import require_login, require_global_access
from ..database import db_get_containers, db_get_nodes, db_get_node, db_get_projects, db_cable_snapshots_list

log = logging.getLogger(__name__)


# ─── Ancienneté des points d'attention ───────────────────────────────────────────────────────
# Les caches live du moteur (metrics.rx_stalled_cache / tx_stalled_cache) ne portent QU'UN BOOLÉEN :
# il n'existe nulle part d'horodatage de DÉBUT de panne. Sans rien, le dashboard ne peut afficher
# que la fraîcheur du journal d'alertes (« il y a 2 minutes ») pour une panne vieille de 33 h — ce
# qui désamorce l'urgence. On mémorise donc ici, par clé stable, la PREMIÈRE fois où CE processus a
# vu le point d'attention. C'est une BORNE INFÉRIEURE, jamais l'âge réel : elle repart à zéro au
# redémarrage de l'orchestrateur. Elle est exposée comme telle (`age_is_lower_bound`) et l'UI écrit
# « ≥ … ». Pour un âge VRAI il faudrait horodater le passage à stalled dans app/metrics.py.
_ATT_FIRST_SEEN = {}          # clé → timestamp epoch du premier passage où on a vu le point
_ATT_PROCESS_START = time.time()


def _att_key(a):
    """Clé stable d'un point d'attention (identité indépendante des compteurs volatils)."""
    return "|".join(str(a.get(k, "")) for k in ("kind", "vmid", "node_id", "name", "scope"))


def _annoter_anciennete(attention):
    """Enrichit chaque point d'attention de son ancienneté OBSERVÉE (borne inférieure) et purge
    les clés disparues (un point résolu ne doit pas garder son ancienneté s'il revient)."""
    now = time.time()
    vues = set()
    for a in attention:
        k = _att_key(a)
        vues.add(k)
        first = _ATT_FIRST_SEEN.setdefault(k, now)
        a["first_seen_ts"] = first
        a["age_s"] = int(now - first)
        # Vu dès le premier tick suivant le démarrage → l'âge observé ne dit rien de l'âge réel.
        a["age_is_lower_bound"] = first <= _ATT_PROCESS_START + 30
        # ÂGE RÉEL quand il existe : `metrics` tient un épisode de panne PERSISTÉ par flux
        # (clé vmid/sens/slot, champ `since`), qui survit à un redémarrage du contrôleur. Il prime
        # sur l'ancienneté simplement OBSERVÉE ci-dessus, laquelle repart à zéro à chaque restart
        # et affichait « il y a 2 minutes » pour une panne de 33 h — le défaut même que ce chantier
        # corrige. On retient le plus ANCIEN épisode des slots concernés (le point d'attention
        # agrège plusieurs slots ; sa gravité, c'est celui qui dure depuis le plus longtemps).
        # `unreachable` a un épisode PERSISTÉ lui aussi (sens « agent », idx None) : on peut donc
        # afficher l'ancienneté RÉELLE de la panne, pas seulement celle observée depuis le
        # démarrage du processus — c'est précisément ce qui manquait quand un multiview est resté
        # injoignable sans que personne ne mesure depuis combien de temps.
        if a.get("kind") == "unreachable" and a.get("vmid") is not None:
            try:
                from ..metrics import panne_en_cours as _panne_ag
                _ep = _panne_ag(a["vmid"], "agent", None)
                if _ep and _ep.get("since"):
                    a["first_seen_ts"] = _ep["since"]
                    a["age_s"] = int(now - _ep["since"])
                    a["age_is_lower_bound"] = False
                    continue
            except Exception as e:
                log.debug("ancienneté agent indisponible (vmid %s) : %s", a.get("vmid"), e)
        _sens = {"rx_stall": "rx", "tx_stall": "tx"}.get(a.get("kind"))
        if _sens and a.get("vmid") is not None:
            try:
                from ..metrics import panne_en_cours as _panne
                # `slots` porte les clés d'affichage « <hostname>_<idx> » (cf. metrics: skey), pas
                # des entiers : l'épisode, lui, est keyé sur l'INDEX. Extraire le suffixe numérique.
                _idx = []
                for _s in (a.get("slots") or []):
                    _t = str(_s).rsplit("_", 1)[-1]
                    if _t.isdigit():
                        _idx.append(int(_t))
                _since = [ep["since"] for i in _idx
                          for ep in (_panne(a["vmid"], _sens, i),) if ep and ep.get("since")]
                if _since:
                    a["first_seen_ts"] = min(_since)
                    a["age_s"] = int(now - min(_since))
                    a["age_is_lower_bound"] = False      # horodatage réel, persisté
            except Exception as e:                       # jamais silencieux : on garde l'observé
                log.debug("ancienneté réelle indisponible (vmid %s) : %s", a.get("vmid"), e)
    for k in [k for k in _ATT_FIRST_SEEN if k not in vues]:
        _ATT_FIRST_SEEN.pop(k, None)
    return attention



# ─── Sélection des alertes mises en avant sur l'accueil ──────────────────────────────────────
# Constat d'audit : la page affichait `db_get_alerts(limit=5)`, soit les CINQ DERNIÈRES, sans tri
# par gravité ni dédoublonnage. Deux conséquences mesurées sur ce parc (584 alertes/24 h, à peu
# près un tiers de chaque niveau) :
#   · une `error` sort du top-5 en quelques secondes, chassée par du bruit `info` (« signal
#     rétabli », etc.) — c'est-à-dire qu'on perd de vue le problème au moment où il dure ;
#   · un même problème persistant occupe les 5 lignes (un message vu 16 fois dans une fenêtre de
#     200), et masque tous les autres. Sur cette fenêtre, 200 lignes ne portent que 113 messages
#     distincts : 43 % de redondance.
# On classe donc par GRAVITÉ puis par récence, et on regroupe les occurrences IDENTIQUES pour
# montrer 5 problèmes DISTINCTS plutôt que 5 lignes du même. Le regroupement est fait sur le
# message EXACT (pas de normalisation) : fusionner des messages seulement ressemblants ferait
# disparaître des incidents différents, ce qui serait pire que la redondance qu'on corrige.
_ORDRE_NIVEAU = {"error": 3, "warning": 2, "info": 1}


def _en_cours_seulement(groupes):
    """Ne garde que les épisodes ENCORE VIVANTS, et seulement ceux qui décrivent un défaut.

    « En cours » = le message a encore été émis dans la fenêtre d'anti-rebond (réglage
    `alerts_antirebond_s`, 900 s par défaut) — c'est-à-dire la même définition que
    `database.db_alert_episodes`, pour que deux endroits du produit ne se contredisent pas.

    ⚠ Ce n'est PAS une preuve de résolution : c'est une ABSENCE DE RÉCIDIVE. Un défaut dont le
    conteneur a été détruit cesse d'émettre et sort donc de la liste. On affiche ce qu'on a
    mesuré, jamais un verdict qu'on n'a pas.

    Les lignes `info` (déploiements, gestes d'exploitation) sont écartées : un déploiement est un
    ÉVÉNEMENT, pas un état, et il n'a rien à faire dans une zone qui répond à « est-ce nominal ? ».
    Elles restent intégralement dans Monitoring → Journaux."""
    from ..database import _antirebond_fenetre, _ecart_s
    fenetre = _antirebond_fenetre()
    if fenetre <= 0:            # anti-rebond désactivé : aucune notion d'épisode, on ne filtre pas
        return [g for g in groupes if g.get("niveau") in ("warning", "error")]
    maintenant = datetime.now()
    out = []
    for g in groupes:
        if g.get("niveau") not in ("warning", "error"):
            continue
        age = _ecart_s(g.get("timestamp"), maintenant)
        if age is not None and age <= fenetre:
            out.append(g)
    return out


def _saillantes_depuis_groupes(groupes, n=5):
    """Même verdict que `_alertes_saillantes`, mais à partir des groupes DÉJÀ agrégés par SQLite
    (`database.db_alertes_groupees`) : on ne matérialise plus 1000 lignes en Python à chaque poll.
    L'ordre des niveaux reste défini ICI et nulle part ailleurs — c'est la seule chose que le SQL
    ne doit pas savoir, sous peine de la voir diverger en silence."""
    out = []
    for g in groupes or []:
        d = dict(g)
        niveaux = d.pop("niveaux", None) or [d.get("niveau")]
        d["niveau"] = max(niveaux, key=lambda x: _ORDRE_NIVEAU.get(x, 0))   # pire niveau du groupe
        d.setdefault("count", 1)
        d.setdefault("first_timestamp", d.get("timestamp"))
        out.append(d)
    return sorted(out,
                  key=lambda g: (_ORDRE_NIVEAU.get(g.get("niveau"), 0), g.get("timestamp") or ""),
                  reverse=True)[:max(1, n)]


def _alertes_saillantes(rows, n=5):
    """Regroupe les alertes identiques et rend les `n` plus saillantes (gravité, puis récence).

    Chaque entrée porte `count` et `first_timestamp` en plus des champs d'origine : une alerte
    remontée en tête parce qu'elle est GRAVE peut être ancienne, et l'afficher sans son
    ancienneté ferait croire à un incident frais — le défaut exact qu'on vient de corriger
    ailleurs (« il y a 2 minutes » pour une panne de 33 h)."""
    groupes = {}
    for r in rows or []:
        cle = r.get("message") or ""
        g = groupes.get(cle)
        if g is None:
            groupes[cle] = {**r, "count": 1, "first_timestamp": r.get("timestamp")}
            continue
        g["count"] += 1
        # `rows` arrive du plus récent au plus ancien : la 1re vue est la dernière occurrence,
        # les suivantes reculent dans le temps → elles fixent le début de l'épisode.
        if (r.get("timestamp") or "") < (g.get("first_timestamp") or ""):
            g["first_timestamp"] = r.get("timestamp")
        if _ORDRE_NIVEAU.get(r.get("niveau"), 0) > _ORDRE_NIVEAU.get(g.get("niveau"), 0):
            g["niveau"] = r.get("niveau")        # pire niveau vu pour ce message
    return sorted(groupes.values(),
                  key=lambda g: (_ORDRE_NIVEAU.get(g.get("niveau"), 0), g.get("timestamp") or ""),
                  reverse=True)[:max(1, n)]

def _pyramide_kpi():
    """KPI compact pyramide pour le dashboard (None s'il n'y a aucune pyramide → encart masqué)."""
    try:
        from ..metrics import pyramide_overview
        ov = pyramide_overview()
    except Exception:
        return None
    pyrs = ov.get("pyramides") or []
    if not pyrs:
        return None
    k = ov.get("kpi") or {}
    return {"count": len(pyrs), "pct": k.get("pct") or {}, "tiles": k.get("tiles") or 0,
            "orphans": k.get("orphans") or 0, "unmet": k.get("unmet") or 0}


@bp.route("/api/sources", methods=["GET"])
@require_login
def api_sources():
    """Sorties (produces[]) de toute la flotte, dérivées de la DB SEULE (deploy_config +
    manifestes via topology_ports/derive_wiring) — aucun appel réseau, contrairement à
    /api/home/summary (PTP, sondes :8082, edges). Utilisé par l'éditeur multiview pour
    ses listes de sources. Optionnel ?kind=video|audio pour filtrer."""
    from .. import plugins as _plugins
    want = (request.args.get("kind") or "").strip() or None
    # Nœuds Docker par id → nom ; LXC rattachés au nœud Proxmox principal
    try:
        nodes_by_id = {n["id"]: n["name"] for n in db_get_nodes()}
    except Exception:
        nodes_by_id = {}
    default_node = "local"   # label de repli pour un conteneur legacy sans node_id
    out = []
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        kind = dc.get("type")
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        node_id = c.get("node_id")
        node = nodes_by_id.get(node_id, default_node) if node_id else default_node
        tp_hook = _plugins.get_hook(kind, "topology_ports") if kind else None
        if tp_hook:
            produces = tp_hook(hn, p, {})["produces"]
        elif _plugins.is_plugin(kind):
            produces = [{"shm": prod["shm"], "kind": prod.get("essence") or "video",
                         "label": prod.get("label") or "", "format": prod.get("format")}
                        for prod in _plugins.derive_wiring(kind, hn, p)["produces"]
                        if prod.get("shm")]
        else:
            continue
        for port in produces:
            k = port.get("kind") or "video"
            if not port.get("shm") or (want and k != want):
                continue
            out.append({"vmid": c["vmid"], "hostname": c.get("hostname"),
                        "node": node, "shm": port["shm"], "kind": k,
                        "label": port.get("label") or "", "format": port.get("format")})
    return jsonify(out)

# ─── CADENCE DE CONTENU NEUF : la référence vient de l'AMONT, jamais de soi-même ──────────────
# `fps_content` (publié par le plugin) = à quelle vitesse un nœud relaie de la matière NOUVELLE,
# par opposition à la vitesse à laquelle il compose. La page Câbles comparait cette valeur à la
# cadence de composition du nœud LUI-MÊME. Cette référence est fausse, et elle l'est dans les
# deux sens (constaté à Horace le 2026-08-17, murs 1080p50 sur sources 1080i25) :
#
#  • FAUX POSITIF, en permanence. Des sources à 25 trames/s ne peuvent pas alimenter un mur à
#    50 Hz en contenu neuf : `fps_content < fps` est structurellement garanti, l'avertissement ne
#    peut jamais s'éteindre. Les 4 shards du parc l'affichaient en continu alors qu'ils relaient
#    ~100 % de la matière disponible (24,7 sur 24,9), avec 0 trame perdue.
#  • FAUX NÉGATIF, là où ça compte. Les entrées d'un assembleur sont ses SHARDS, qui réémettent
#    un grain à chaque créneau que leur contenu ait changé ou non : « une entrée a avancé » est
#    donc toujours vrai, et l'assembleur publie fps_content = fps quoi qu'il arrive en amont. Un
#    shard qui gèlerait son image passerait pour parfaitement sain.
#
# La référence légitime est la cadence de contenu neuf ATTENDUE EN AMONT :
#   — entrée lue depuis une vraie source → sa cadence TRAME déclarée (25 pour du 1080i25, pas la
#     cadence de sortie du mur) ;
#   — entrée lue depuis un nœud qui publie lui-même du contenu neuf (un shard) → l'ATTENTE de ce
#     producteur, propagée récursivement. On propage l'attente et non la mesure : sinon un shard
#     qui décroche fait baisser la référence de son assembleur d'autant, et le défaut s'annule
#     lui-même au lieu de se voir.
#   — MAX sur les entrées, pas min ni somme : si une entrée avance à 25 Hz, le nœud doit relayer
#     au moins 25 fois par seconde ou il jette de la matière. C'est un minorant rigoureux, qui ne
#     demande aucun modèle de phase entre les entrées (des sources déphasées font légitimement
#     monter fps_content au-dessus du max — d'où « au moins »).
#
# ⚠ Une TOLÉRANCE devient nécessaire, contrairement à la règle d'origine (« aucune tolérance »).
# Elle se défendait tant que les deux termes sortaient du MÊME compteur ; ici on compare deux
# mesures indépendantes, et l'écart de mesure n'est pas un défaut. La marge est large à dessein :
# les vrais défauts de ce genre sont grossiers (moitié de cadence, gel complet), jamais marginaux.
_CONTENU_MARGE = 0.80      # décroche sous 80 % de l'attendu

# ⚠ ABSENCE DE RÉFÉRENCE = AUCUN VERDICT (`tenue: None`), jamais une alerte. Un format non
# déclaré, un producteur hors topologie, un cycle : autant de raisons de ne rien conclure. Une
# page de diagnostic qui invente une alarme à partir d'une inconnue est pire que muette.
def _contenu_etats(topo_nodes, producers, shards_par_parent, noms):
    """Pose `contenu_etat` = {ref, mesure, tenue, maillon, maillon_mesure} sur chaque nœud topo.

    Verdict PRÊT À AFFICHER, sur le modèle de `cadence_etat` : le front reçoit une décision, pas
    de quoi en fabriquer une autre. `maillon` nomme le shard fautif quand le défaut est invisible
    depuis l'assembleur — sans lui, l'alerte se poserait sur un conteneur interne REPLIÉ dans
    l'interface, donc sur rien."""
    from ..metrics import fps_plancher, fps_content_cache

    par_vmid = {}
    for n in topo_nodes:
        par_vmid.setdefault(n["vmid"], []).append(n)
    # shm → vmid qui l'écrit, et shm → cadence TRAME déclarée par son producteur.
    prod_vmid, shm_fps = {}, {}
    for shm, lst in (producers or {}).items():
        for pn in lst:
            if pn.get("kind") == "video":
                prod_vmid.setdefault(shm, pn["vmid"])
                break
    for n in topo_nodes:
        for pp in (n.get("produces") or []):
            if pp.get("kind") != "video" or not pp.get("shm"):
                continue
            try:
                f = float((pp.get("format") or {}).get("fps"))
            except (TypeError, ValueError):
                continue
            if f > 0:
                shm_fps.setdefault(pp["shm"], f)
    # Un vmid « à contenu » publie fps_content : c'est lui dont on propage l'ATTENTE plutôt que la
    # cadence de son flux de sortie (laquelle vaut 50 sur un shard qui ne relaie que 25 de neuf).
    a_contenu = {v for v, f in (fps_content_cache or {}).items() if f is not None}

    memo, encours = {}, set()

    def _ref(vmid):
        if vmid in memo:
            return memo[vmid]
        if vmid in encours:
            return None                      # cycle de câblage : on ne conclut rien
        encours.add(vmid)
        r = None
        for n in par_vmid.get(vmid, ()):
            for port in (n.get("consumes") or []):
                if port.get("kind") != "video" or not port.get("shm"):
                    continue
                p = prod_vmid.get(port["shm"])
                v = _ref(p) if (p is not None and p != vmid and p in a_contenu) \
                    else shm_fps.get(port["shm"])
                if v and (r is None or v > r):
                    r = v
        encours.discard(vmid)
        memo[vmid] = r
        return r

    def _deficit(vmid):
        """(mesure, ref) si ce vmid relaie MOINS de contenu neuf qu'il n'en reçoit, sinon None."""
        ref = _ref(vmid)
        mes = fps_plancher(vmid, canal="contenu")
        if not ref or mes is None:
            return None
        return (mes, ref) if mes < ref * _CONTENU_MARGE else None

    for n in topo_nodes:
        vmid = n["vmid"]
        ref, mes = _ref(vmid), fps_plancher(vmid, canal="contenu")
        etat = {"ref": round(ref, 1) if ref else None,
                "mesure": round(mes, 1) if mes is not None else None,
                "tenue": None, "maillon": None, "maillon_mesure": None}
        if ref and mes is not None:
            etat["tenue"] = mes >= ref * _CONTENU_MARGE
        # MAILLON FAIBLE d'un mur shardé, même raison d'être que `fps_shard_min` : l'assembleur va
        # bien par construction, ce sont ses shards qu'il faut regarder — et ils sont repliés.
        pire = None
        for r in (shards_par_parent.get(vmid) or ()):
            d = _deficit(r)
            if d and (pire is None or d[0] < pire[1]):
                pire = (noms.get(r, str(r)), d[0], d[1])
        if pire:
            etat["tenue"] = False
            etat["maillon"], etat["maillon_mesure"] = pire[0], round(pire[1], 1)
            if not etat["ref"]:
                etat["ref"] = round(pire[2], 1)
        n["contenu_etat"] = etat

@bp.route("/api/home/summary", methods=["GET"])
@require_global_access
def api_home_summary():
    """Agrège l'état système pour la home dashboard. Une seule requête → tout."""
    from . import _eff_node_id
    from .. import settings as st
    from services import nmos as _nmos
    from collections import Counter

    # PTP — AGRÉGÉ sur tous les nœuds PTP-activés (cohérence multi-nœuds). Dernier relevé du
    # sampler (ptp.cached_status), JAMAIS de status() live ici : cette route est pollée (home,
    # multiview, câbles) et un status() = jusqu'à 5 commandes SSH séquentielles (~1 s, 40 s si
    # hôte injoignable). Au boot (sampler pas encore passé) les champs restent aux défauts.
    # enabled=False → le front masque entièrement la pastille.
    from .. import ptp as _ptp
    _ptp_ref_nid = _eff_node_id()
    _ptp_nodes = []   # [(nid, status_dict)] des nœuds PTP-activés
    for _n in (db_get_nodes() or []):
        try:
            if st.setting_for("ptp_enabled", _n["id"]):
                _ptp_nodes.append((_n["id"], _ptp.cached_status(_n["id"]) or {}))
        except Exception:
            pass
    # Repli mono-box (table nodes vide / transition 1 nœud) : comportement nœud unique d'origine.
    if not _ptp_nodes and st.setting_for("ptp_enabled", _ptp_ref_nid):
        _ptp_nodes.append((_ptp_ref_nid, _ptp.cached_status(_ptp_ref_nid) or {}))
    ptp_info = {"enabled": bool(_ptp_nodes), "locked": False, "port_state": None,
                "offset_ns": None, "grandmaster_id": None,
                "nodes_ptp": len(_ptp_nodes), "nodes_locked": 0}
    if _ptp_nodes:
        # Critère = ptp.clock_ok (synchro au GM), PAS `locked` brut : sur un nœud full-PF DPDK
        # `locked` est le verrou servo STRICT de libmtl, resté False sur E810 alors que l'offset au
        # GM tient la nanoseconde → la pastille d'accueil passait en warning à tort.
        _locked = [_ptp.clock_ok(s) for _, s in _ptp_nodes]
        ptp_info["nodes_locked"] = sum(_locked)
        ptp_info["locked"] = all(_locked)   # verrouillé seulement si TOUS les nœuds le sont
        _offs = [s.get("offset_ns") for _, s in _ptp_nodes if s.get("offset_ns") is not None]
        if _offs:
            ptp_info["offset_ns"] = max(_offs, key=lambda o: abs(o))  # pire offset absolu
        _ref = next((s for nid, s in _ptp_nodes if nid == _ptp_ref_nid), _ptp_nodes[0][1])
        ptp_info["port_state"] = _ref.get("port_state")
        ptp_info["grandmaster_id"] = _ref.get("grandmaster_id")

    # Containers + ventilation par type
    from .. import io2110_flows as _iof
    containers = db_get_containers()
    running = sum(1 for c in containers if c.get("status") == "running")
    by_type = Counter()
    multiview_count = 0
    # Comptes RX/TX agrégés des moteurs 2110_io (bi-rôle) : le moteur compte comme 1 conteneur
    # en section « Sources », mais il porte N RX *et* M TX. On agrège les FLUX vidéo ACTIFS pour
    # alimenter le schéma pipeline (Sources = Rx 2110, Destinations = Tx 2110).
    io_rx_v = io_rx_a = io_tx_v = io_tx_a = 0
    io_rx_eng = io_tx_eng = 0   # nb de MOTEURS ayant respectivement du RX / du TX vidéo actif
    io_engines = io_verbose = 0  # moteurs 2110_io : total / en log verbeux (params.mtl_log_level ≠ warning)
    for c in containers:
        dc = c.get("deploy_config")
        try: dc = json.loads(dc) if isinstance(dc, str) else dc
        except Exception: dc = None
        if dc and dc.get("type"):
            by_type[dc["type"]] += 1
            if dc["type"] == "multiview":
                multiview_count += 1
            if dc["type"] == "2110_io":
                _p = dc.get("params") or {}
                io_engines += 1
                # Niveau de log EFFECTIF du moteur (tracé au déploiement, cf. docker_driver). Absent =
                # moteur déployé avant la feature (assimilé warning pour ne pas fausse-alarmer). Sont
                # VERBEUX (dump de stats périodique → hoquet) les niveaux plus bavards que warning :
                # debug/info/notice. warning/err/crit sont silencieux (pas de hoquet).
                if str(_p.get("mtl_log_level") or "warning").lower() in ("debug", "info", "notice"):
                    io_verbose += 1
                try:
                    _rxf = _iof.active_flows(_p, "rx")
                    _txf = _iof.active_flows(_p, "tx")
                    _v_rx = sum(1 for f in _rxf if f.get("essence") == "video")
                    _v_tx = sum(1 for f in _txf if f.get("essence") == "video")
                    io_rx_v += _v_rx
                    io_rx_a += sum(1 for f in _rxf if f.get("essence") == "audio")
                    io_tx_v += _v_tx
                    io_tx_a += sum(1 for f in _txf if f.get("essence") == "audio")
                    if _v_rx: io_rx_eng += 1   # ce moteur a un rôle de source
                    if _v_tx: io_tx_eng += 1   # ce moteur a un rôle de destination
                except Exception:
                    pass
    io2110_counts = {"rx_video": io_rx_v, "rx_audio": io_rx_a,
                     "tx_video": io_tx_v, "tx_audio": io_tx_a,
                     "rx_engines": io_rx_eng, "tx_engines": io_tx_eng}
    # Voyant « log moteur verbeux » : le réglage courant (intention) + l'état RÉEL des moteurs
    # tournants (params.mtl_log_level tracé au déploiement). Le voyant s'allume si l'un OU l'autre
    # n'est pas "warning" (cf. renderHealth côté home.html). ≥ INFO = dump de stats périodique de
    # libmtl → micro-hoquet de cadence + log volumineux (diagnostic ponctuel seulement).
    from .. import settings as _settings_mod
    _mtl_lvl = str(_settings_mod.get("mtl_log_level") or "warning").lower()
    mtl_log_info = {"setting": _mtl_lvl,
                    "setting_verbose": _mtl_lvl in ("debug", "info", "notice"),
                    "engines": io_engines, "verbose_engines": io_verbose}

    # NMOS : ventiler par format
    with _nmos._lock:
        recv_list = list(_nmos._receivers.values())
        send_list = list(_nmos._senders.values())
        recv_subscribed = sum(1 for r in recv_list
                              if (r.get("subscription") or {}).get("active"))
    recv_video = sum(1 for r in recv_list if r.get("format") == "urn:x-nmos:format:video")
    recv_audio = sum(1 for r in recv_list if r.get("format") == "urn:x-nmos:format:audio")
    # Senders : on compte directement les NMOS Senders exposés (vidéo vs audio par format de leur flow)
    send_video = sum(1 for s in send_list
                     if _nmos._flows.get(s.get("flow_id") or "", {}).get("format") == "urn:x-nmos:format:video")
    send_audio = sum(1 for s in send_list
                     if _nmos._flows.get(s.get("flow_id") or "", {}).get("format") == "urn:x-nmos:format:audio")

    # Flux MXL = nombre de shms /dev/shm/* écrits par les pipelines vidéo (count
    # depuis les containers qui ont un script vidéo déployé). Approximation rapide :
    # un container avec dc.type in {receiver, audio_receiver, multiview} écrit
    # au moins 1 shm.
    from .. import plugins as _plugins
    mxl_flows = 0
    for c in containers:
        dc = c.get("deploy_config")
        try: dc = json.loads(dc) if isinstance(dc, str) else dc
        except Exception: dc = None
        if not dc: continue
        t = dc.get("type")
        p = dc.get("params") or {}
        h = _plugins.get_hook(t, "produced_flow_count") if t else None
        if h:
            mxl_flows += (h(p, {}) or 0)

    # mDNS + Ember+
    mdns_active = False
    try:
        mdns_active = bool(_nmos._state.get("mdns_active"))
    except Exception:
        pass

    # (Le badge SR-IOV/pool VF a été retiré : modèle full-Docker = PF en AF-XDP, pas de VF.)

    # Alertes récentes — SAILLANTES, pas « les 5 dernières » (cf. _alertes_saillantes).
    # Le regroupement se fait en SQL : cette route est pollée toutes les 2 s et matérialiser
    # 1000 lignes complètes en Python à chaque passe en avait fait le 1er poste de CPU du
    # contrôleur. Et le total est un vrai COUNT : `len()` d'une fenêtre plafonnée à 1000
    # annonçait « 1000 alerte(s) au total » sur une base qui en garde 10 000.
    from ..database import db_alertes_groupees, db_alerts_count
    alerts_total = db_alerts_count()
    # Le regroupement travaille sur `message` (forme canonique, indépendante du lecteur) ; le
    # rendu vient APRÈS, sur les 5 lignes retenues seulement — regrouper sur du texte traduit
    # ferait dépendre le dédoublonnage de la langue de celui qui regarde.
    from ..i18n import rendre_alertes
    # ── Accueil = ÉTAT, pas historique ────────────────────────────────────────────────
    # Ce bandeau montrait les 5 alertes les plus SAILLANTES (gravité d'abord) parmi les 1000
    # dernières : une erreur close depuis trois jours y restait en tête indéfiniment, sous un
    # en-tête « SYSTÈME OK ». Mesuré le 2026-08-30 : 5 erreurs rouges affichées alors qu'AUCUN
    # épisode de niveau erreur n'était actif. L'exploitant ne pouvait pas distinguer une panne
    # vivante d'une cicatrice, et finissait par ne plus lire la zone.
    # Désormais : seulement ce qui est EN COURS. L'historique complet vit dans Monitoring →
    # Journaux, qui a les filtres et l'export pour ça.
    alerts_recent = rendre_alertes(_en_cours_seulement(
        _saillantes_depuis_groupes(db_alertes_groupees(1000), 40)))[:5]

    # Topologie pipeline : nodes par container + edges shm producteur → consommateur
    # Chaque port (produces/consumes) porte son kind 'video' ou 'audio' pour
    # colorer les arêtes et les pastilles côté front.
    from .. import plugins as _plugins
    from ..metrics import av_sync_cache, latency_cache as _lat_cache, shm_active_cache as _shm_active, cpu_count_cache as _cpu_count, rx_latency_cache as _rx_lat_cache, own_latency_cache as _own_lat_cache, rx_stalled_cache as _rx_stalled_cache, rx_fps_cache as _rx_fps_cache, rx_served_cache as _rx_served_cache, gpu_cache as _gpu_cache, slice_cache as _slice_cache
    # Résolution paresseuse du nom de nœud d'exécution par container : node_id non nul → nodes.name ;
    # node_id nul (conteneur legacy sans nœud) → label de repli. Mémoïsé (évite N requêtes DB).
    _node_name_memo = {}
    def _node_label_for(node_id):
        if node_id is None:
            return "local"
        if node_id not in _node_name_memo:
            _n = db_get_node(node_id)
            _node_name_memo[node_id] = (_n or {}).get("name") or f"node{node_id}"
        return _node_name_memo[node_id]
    topo_nodes = []
    producers = {}
    consumers = []
    # shm → alimenté (bool), renseigné par les producteurs qui publient l'inventaire de ce qu'ils
    # servent. Sert ensuite à éclairer le voyant des ENTRÉES : une entrée est alimentée si le flux
    # auquel elle est câblée est réellement produit. Un shm absent de cette table = on ne sait pas
    # (producteur qui ne publie rien), pas « éteint ».
    fed_shm = {}
    vmid_params = {}                       # vmid → params (pour le délai cumulé : entrée de réf)
    vmid_kind = {}                         # vmid → type
    # Rôle de chaque conteneur dans le tissu (pour replier les internes dans Câbles, comme Containers)
    try:
        from .. import compositor_fabric as _cf
        _fab = _cf.fabric_layout(containers)
        _present = {c.get("vmid") for c in containers}
    except Exception:
        _fab, _present = {}, set()
    # MAILLON FAIBLE d'un mur shardé. L'assembleur recompose à sa cadence nominale quoi qu'il
    # arrive : son fps ne dit rien de la santé de ses shards, et ceux-ci sont volontairement
    # repliés dans l'interface. Sans ce calcul, un mur dont un shard tombe à 44 s'affiche à 50.
    _shard_bas, _shard_perdues, _perdues_par_vmid = {}, {}, {}
    try:
        from ..metrics import fps_plancher as _plancher, fps_pic as _pic
        _fps_par_vmid = {}
        for _c in containers:
            # PLANCHER sur 30 s, pas la valeur instantanée : la cadence d'un shard oscille de
            # quelques images, et comparer l'instantané à un seuil faisait CLIGNOTER
            # l'avertissement d'un rafraîchissement à l'autre. Repli sur la valeur courante quand
            # la fenêtre est vide (conteneur qui vient de démarrer).
            _p = _plancher(_c["vmid"])
            if _p is None:
                try:
                    _p = float(_c.get("fps"))
                except (TypeError, ValueError):
                    continue
            _fps_par_vmid[_c["vmid"]] = _p
            # PIC de trames perdues sur la fenêtre : une trame perdue il y a vingt secondes reste
            # une trame perdue. C'est le signal qui déclenche l'avertissement — pas un écart de
            # cadence, qui obligerait à choisir une tolérance.
            _perdues_par_vmid[_c["vmid"]] = _pic(_c["vmid"], canal="perdues")
        _noms = {_c["vmid"]: (_c.get("hostname") or str(_c["vmid"])) for _c in containers}
        for _parent, _refs in (_cf.shards_par_parent() or {}).items():
            _vals = [(_fps_par_vmid[_r], _noms.get(_r, str(_r))) for _r in _refs
                     if _r in _fps_par_vmid]
            if _vals:
                _shard_bas[_parent] = min(_vals)   # (fps, nom) — le plus lent des shards
            _pertes = [_perdues_par_vmid.get(_r) or 0.0 for _r in _refs]
            _shard_perdues[_parent] = max(_pertes) if _pertes else 0.0
    except Exception:                                                      # noqa: BLE001
        _shard_bas, _shard_perdues = {}, {}
    # États live des plugins (:8082/state) PRÉ-CHARGÉS EN PARALLÈLE. Ils étaient lus un par un au
    # fil de la boucle ci-dessous, chacun avec 0,5 s de timeout : sur cette route pollée toutes les
    # 2 s, c'était le premier poste de coût (579 ms sur 706 — mesuré 2026-08-13), et un seul
    # conteneur injoignable retardait tous les suivants. Même contenu, même repli sur {}.
    from .cabling import _fetch_plugin_states_cache
    _live_cibles = []
    # Statuts depuis lesquels un :8082 peut répondre : le conteneur tourne. `script_stopped` compte
    # (conteneur up, script arrêté : l'agent répond). Tout le reste — `unreachable`, `stopped`,
    # `unknown` — c'est l'orchestrateur qui DIT DÉJÀ que rien ne répondra : l'interroger quand même
    # coûtait un timeout plein (0,5 s) par conteneur et par poll, pour un `{}` connu d'avance.
    # Un seul conteneur `unreachable` pesait ainsi 500 des 626 ms de la requête (mesuré 2026-08-13).
    _STATUTS_JOIGNABLES = ("running", "script_stopped")
    _wiring_par_vmid = {}   # le wiring calculé ici est RÉUTILISÉ par la boucle (sinon on le
                            # dériverait deux fois par conteneur plugin, à chaque poll)
    for _c in containers:
        if _c.get("status") not in _STATUTS_JOIGNABLES:
            continue
        _dc = _c.get("deploy_config")
        try:
            _dc = json.loads(_dc) if isinstance(_dc, str) else _dc
        except Exception:                                                  # noqa: BLE001
            _dc = None
        _k = (_dc or {}).get("type")
        if not _k or not _plugins.is_plugin(_k):
            continue
        try:
            _w = _plugins.derive_wiring(_k, (_c.get("hostname") or ""), (_dc or {}).get("params") or {})
        except Exception:                                                  # noqa: BLE001
            continue
        _wiring_par_vmid[_c["vmid"]] = _w
        if any(x.get("state_field") for x in _w.get("consumes") or []):
            _live_cibles.append((_c["vmid"], _c.get("ip"), _w.get("state_endpoint") or "/state"))
    # Cache tiède : cette route est pollée toutes les 2 s par l'accueil ET par la
    # page Câbles ; l'état câblé d'un plugin ne change que sur un geste. On sert
    # le cache et on rafraîchit derrière (cf. _fetch_plugin_states_cache).
    _live_par_vmid = _fetch_plugin_states_cache(_live_cibles)
    for c in containers:
        dc_raw = c.get("deploy_config")
        try:
            dc = json.loads(dc_raw) if isinstance(dc_raw, str) else dc_raw
        except Exception:
            dc = None
        kind = (dc or {}).get("type")
        p = (dc or {}).get("params") or {}
        vmid_params[c["vmid"]] = p; vmid_kind[c["vmid"]] = kind
        produces, consumes_ = [], []
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        tp_hook = _plugins.get_hook(kind, "topology_ports") if kind else None
        if tp_hook:
            _tp = tp_hook(hn, p, {})
            produces, consumes_ = _tp["produces"], _tp["consumes"]
            # Étage 3 (docs/reference/TX_LAYOUTS.md) : le format DÉCLARÉ d'une sortie TX vit dans `tx_slots[i]`, pas
            # dans le wiring du plugin (un slot TX n'a pas de format « attendu » au manifeste : il
            # suit sa source). On l'expose ici, côté orchestrateur, pour que la page Câbles AFFICHE
            # le format de chaque sortie et signale l'écart AVANT le clic (et pas seulement après,
            # dans une modale). C'est le format qu'annonce le SDP — donc le contrat de la sortie.
            if kind == "2110_io":
                from .. import tx_maintenance as _txm
                for _port in consumes_:
                    if _port.get("kind") == "video" and _port.get("slot") is not None:
                        _sf = _txm.slot_format(p, _port["slot"])
                        if _sf and _sf.get("width"):
                            _port["format"] = dict(_sf, chroma=str(p.get("chroma") or "422"))
                            _port["tx_slot"] = True   # gate de format spécifique (axes de signature)
        elif _plugins.is_plugin(kind):
            # Plugins : I/O déclarées au manifeste (wiring). produces = gabarits shm
            # résolus ; consumes hot-wire = shm live lu via state_field sur :8082.
            # Déjà dérivé par la pré-passe ci-dessus pour les conteneurs joignables ; recalculé
            # seulement pour les autres (elle les saute).
            w = _wiring_par_vmid.get(c["vmid"]) or _plugins.derive_wiring(kind, hn, p)
            for prod in w["produces"]:
                if prod.get("shm"):
                    pp = {"shm": prod["shm"], "kind": prod.get("essence") or "video"}
                    if prod.get("label"):
                        pp["label"] = prod["label"]
                    if prod.get("format"):
                        pp["format"] = prod["format"]
                    produces.append(pp)
            cons_specs = w["consumes"]
            # Les plugins qui ADAPTENT leur entrée (scale/convert : udc, multiview, delay)
            # n'exposent pas de format attendu → pas de proposition d'UDC au câblage.
            adapts_input = bool((_plugins.get(kind) or {}).get("adapts_input"))
            live = _live_par_vmid.get(c["vmid"], {})   # pré-chargé en parallèle (cf. plus haut)
            for spec in cons_specs:
                ess = spec.get("essence") or "video"
                shm = spec.get("shm") or (live.get(spec["state_field"]) if spec.get("state_field") else None)
                port = {"kind": ess}
                if spec.get("label"):
                    port["label"] = spec["label"]
                if spec.get("slot") is not None:
                    port["slot"] = spec["slot"]
                if spec.get("format") and not adapts_input:
                    port["format"] = spec["format"]   # format ATTENDU (pour détection mismatch au câblage)
                if shm:
                    port["shm"] = shm
                else:
                    port["shm"] = ""; port["disconnected"] = True
                consumes_.append(port)
        # Latence de RÉCEPTION (segment A = capture média → écriture shm) par port producteur :
        # exposée sur les sources 2110_io (réseau + framebuffers MTL + de-jitter). Distincte du badge
        # segment B (transit shm → lecteur) porté par les arêtes. Affichée sur le port source.
        _rxl = _rx_lat_cache.get(c["vmid"]) or {}
        _rxstall = _rx_stalled_cache.get(c["vmid"]) or {}
        if _rxl or _rxstall:
            for pp in produces:
                _v = _rxl.get(pp.get("shm"))
                if isinstance(_v, (int, float)):
                    pp["rx_latency_ms"] = round(_v, 1)
                # « Abonné mais ne reçoit pas » : slot mtl dont le flux n'avance pas.
                if _rxstall.get(pp.get("shm")):
                    pp["rx_stalled"] = True
        # fps PAR FLUX (moteur multi-flux 2110_io, où un fps de carte agrégé n'a pas de sens) :
        # exposé sur chaque port de sortie depuis receivers[].fps (rx_fps_cache, keyé par shm).
        _rxf = _rx_fps_cache.get(c["vmid"]) or {}
        if _rxf:
            for pp in produces:
                _vf = _rxf.get(pp.get("shm"))
                if isinstance(_vf, (int, float)):
                    pp["fps"] = round(_vf, 1)
        # ALIMENTATION du port (`fed`), TRI-ÉTAT — absent de la charge utile quand on ne sait pas.
        #
        # Un moteur multi-flux déclare bien plus de ports qu'il n'en sert : le moteur 2110 annonce
        # 25 sorties vidéo, 25 audio et 25 ANC, alors que seules les sessions réellement abonnées
        # produisent un flux MXL. La page les offrait toutes au câblage sans distinction, ce qui
        # laissait croire à des flux audio/ANC qui n'ont jamais existé.
        #
        # On ne masque PAS les ports non alimentés : pré-câbler un port avant que sa source arrive
        # est un usage légitime (décision produit). On dit seulement lesquels sont servis.
        #
        # ⚠ `fed=False` exige une PREUVE POSITIVE d'absence : le producteur publie l'inventaire de
        # ses flux servis, et celui-ci n'y est pas. Un producteur qui ne publie rien (la plupart des
        # plugins, mono-flux) ne reçoit aucun `fed` — traiter son silence comme « non alimenté »
        # marquerait toute la flotte en erreur, ce qui est le contraire de l'information cherchée.
        _served = _rx_served_cache.get(c["vmid"])
        if _served is not None:
            for pp in produces:
                _e = _served.get(pp.get("shm"))
                # UNE SESSION QUI LIVRE DES TRAMES N'EST PAS DU SIGNAL. Constaté : deux entrées RX
                # recevaient à 50,3 fps tout en étant `black` ET `frozen` — le moteur travaillait,
                # le contenu était vide, et le voyant s'allumait quand même. « Allumé » doit vouloir
                # dire « il se passe quelque chose », pas « le transport fonctionne ».
                #
                # Les deux drapeaux ENSEMBLE, jamais l'un seul : `frozen` est vrai pour toute mire
                # fixe — l'exiger éteindrait des sources parfaitement valides — et `black` seul peut
                # être un vrai noir à l'antenne. Leur conjonction signe l'absence de contenu.
                #
                # Contrepartie assumée : une source volontairement noire ET immobile s'affichera
                # éteinte. Pour un voyant de présence de signal, c'est le bon compromis.
                _sig = (_e or {}).get("signal") or {}
                pp["fed"] = bool(_e) and not (_sig.get("black") and _sig.get("frozen"))
                fed_shm[pp.get("shm")] = pp["fed"]
        # Producteur sans inventaire par flux : `shm_active` sert de preuve, mais UNIQUEMENT DANS LE
        # SENS POSITIF. Ce drapeau vaut `prev is not None and fi > prev` : quand il est VRAI, le
        # frame_index a réellement avancé entre deux relevés — preuve de vie irréfutable, on allume.
        # Quand il est FAUX, il ne distingue pas « à l'arrêt » de « pas encore deux relevés » : il
        # vaut donc faux au premier tick suivant chaque redémarrage, et pour tout conteneur
        # momentanément injoignable. L'asymétrie est délibérée — une preuve positive allume, une
        # absence de preuve n'éteint jamais.
        elif _shm_active.get(c["vmid"]) is True and len(produces) == 1:
            produces[0]["fed"] = True
            fed_shm[produces[0].get("shm")] = True
        # Containers sans aucune I/O (Stockage, webrtc_gateway, …) : hors topologie/Câbles.
        if not produces and not consumes_:
            continue
        # split_io : un moteur RX+TX (ex. 2110_io) n'est PAS un process passthrough — ses
        # produces (RX, réseau→shm) et consumes (TX, shm→réseau) sont indépendants. On le rend en
        # DEUX nœuds : RX dans la colonne sources (gauche), TX dans la colonne sinks (droite). Même
        # vmid (le câblage cible le vmid ; le slot/shm désambiguïse — endpointOf trouve le bon nœud).
        _split = bool((_plugins.get(kind) or {}).get("split_io")) and produces and consumes_

        def _emit_node(prods, cons, col, host_suffix="", split=False):
            from ..metrics import fps_content_cache as _fps_content
            from ..metrics import cadence_etat as _cadence_etat
            topo_nodes.append({
                "vmid": c["vmid"],
                "hostname": (c.get("hostname") or "") + host_suffix,
                "status": c.get("status"),
                "fps": c.get("fps"),
                # Cadence de CONTENU NEUF, à côté de la cadence de composition. L'ÉCART entre les
                # deux est le diagnostic : un mur qui compose 50 fois par seconde sur des tuiles
                # inchangées publie 50 en toute honnêteté, pendant que l'émetteur aval sort à 38.
                # None = le plugin ne la publie pas → l'UI n'affiche rien de plus.
                "fps_content": _fps_content.get(c["vmid"]),
                # Cadence du SHARD LE PLUS LENT d'un mur shardé, et son nom. L'assembleur ne peut
                # pas relayer plus vite que son maillon le plus lent ; c'est cette valeur-là qui
                # décrit la réalité de ce que le mur produit. None = mur non shardé.
                "fps_shard_min": (_shard_bas.get(c["vmid"]) or (None, None))[0],
                "fps_shard_maillon": (_shard_bas.get(c["vmid"]) or (None, None))[1],
                # Cadence NOMINALE déclarée du mur : c'est l'INTENTION, donc la seule référence
                # légitime d'une alarme. Comparer un shard à la cadence de l'assembleur reviendrait
                # à comparer un défaut à un autre défaut.
                "fps_nominal": p.get("fps"),
                # Verdict de cadence PRÊT À AFFICHER ({cible, tenue, mesure}) — le même que celui
                # des cartes et que celui de l'alarme de sous-cadence. Sans lui, cette page
                # affichait la mesure brute, qui porte ±1 image de troncature de fenêtre : un
                # nœud sain y montrait « 49,8 fps », un chiffre qui bouge sans rien signaler.
                "cadence": _cadence_etat(c["vmid"], c, dc or {}),
                # Trames perdues par seconde, le PIRE de ses shards sur 30 s. > 0 = des images ont
                # été perdues, point : aucun seuil à régler.
                "shard_frames_missed": _shard_perdues.get(c["vmid"]),
                # … et les siennes propres, pour les modules non shardés.
                "frames_missed": _perdues_par_vmid.get(c["vmid"]),
                "kind": kind,
                "plugin_version": p.get("plugin_version"),
                "node_id": c.get("node_id"),
                "node_label": _node_label_for(c.get("node_id")),
                "project_id": c.get("project_id"),
                "fabric_role": (_fab.get(c["vmid"]) or {}).get("role", "logical"),
                "fabric_shards": (_fab.get(c["vmid"]) or {}).get("shards", 0),
                "fabric_parent": next((pp for pp in
                                       (int(x) for x in (_fab.get(c["vmid"]) or {}).get("parents", []) if str(x).isdigit())
                                       if pp in _present), None),
                "col": col,
                "split": split,
                "source": c.get("source"),
                "shm_out": c.get("shm_out"),
                "restarts": c.get("restarts") or 0,
                "produces": prods,
                "consumes": cons,
                "max_inputs": 0,
                "slots_free": 0,
                "cpu_percent": c.get("cpu_percent"),
                "mem_used": c.get("mem_used"),
                "cores": c.get("cores"),
                "memory": c.get("memory"),
                "cpu_count": _cpu_count.get(c["vmid"]),
                "av_sync": av_sync_cache.get(c["vmid"]),   # calage A/V (streamer) : {applied,live,drift} ms
                # Latence PROPRE du nœud (traitement). Source : own_latency_ms rapporté par le plugin
                # (ts_out − ts_cycle_start). Repli pour un plugin non migré : max des transits (ancien
                # comportement, surestimé mais non nul).
                "own_latency_ms": (round(_own_lat_cache[c["vmid"]], 1) if isinstance(_own_lat_cache.get(c["vmid"]), (int, float)) else
                    (lambda _lc: round(max(v for v in _lc.values() if isinstance(v, (int, float)) and v > 0), 1) if any(isinstance(v, (int, float)) and v > 0 for v in _lc.values()) else None)(_lat_cache.get(c["vmid"]) or {})),
                "shm_active": _shm_active.get(c["vmid"]),  # True/False/None (None = pas encore mesuré)
                "gpu": _gpu_cache.get(c["vmid"]),          # {gpu:bool, name} si compositing GPU (cupy), sinon None
                # Mode tranche (composition/publication bande par bande) : badge discret page
                # Câbles. RUNTIME d'abord (le script dit s'il tranche VRAIMENT — un slice_mode
                # de config peut être replié en trame entière : GPU sans gpu_slice, portrait…) ;
                # repli config str-aware (bool("false") serait True) si métriques pas encore lues.
                "slice_mode": (_slice_cache[c["vmid"]] if c["vmid"] in _slice_cache else
                               str(p.get("slice_mode") or "").strip().lower() in ("1", "true", "yes", "on")),
            })

        if _split:
            _emit_node(produces, [], "sources", " (RX)", split=True)
            _emit_node([], consumes_, "sinks", " (TX)", split=True)
        else:
            if kind == "streamer":
                col = "sinks"
            elif _plugins.is_plugin(kind):
                col = "composition" if (produces and consumes_) else ("sinks" if consumes_ else "sources")
            else:
                col = "sources"
            _emit_node(produces, consumes_, col)

        for port in produces:
            producers.setdefault(port["shm"], []).append({"vmid": c["vmid"], "kind": port["kind"]})
        # slot = MÊME valeur que le front rendra en data-slot (renderPort : port.slot ?? index dans
        # consumes). Indispensable pour que endpointOf vise le BON dot d'entrée quand un même shm est
        # câblé sur plusieurs slots d'un même nœud (ex. multiview : 5 PiP du même flux) — sinon les
        # arêtes se superposent toutes sur le slot 0. On énumère la liste COMPLÈTE (slots déconnectés
        # inclus) pour conserver l'alignement d'index avec le rendu front.
        for _i, port in enumerate(consumes_):
            if not port.get("shm"):
                continue  # slot disconnected, pas d'edge
            _slot = port.get("slot") if port.get("slot") is not None else _i
            consumers.append({"vmid": c["vmid"], "shm": port["shm"],
                              "kind": port["kind"], "slot": _slot})
    # Verdict de CONTENU NEUF, une fois la topologie complète (il faut `producers` pour savoir qui
    # écrit quel shm, donc après la boucle). Best-effort : ce diagnostic ne doit jamais faire
    # échouer la page.
    try:
        from .. import compositor_fabric as _cf2
        _contenu_etats(topo_nodes, producers, _cf2.shards_par_parent() or {},
                       {_c2["vmid"]: (_c2.get("hostname") or str(_c2["vmid"])) for _c2 in containers})
    except Exception:                                                      # noqa: BLE001
        log.debug("verdict contenu neuf indisponible", exc_info=True)
    # ─── Vue d'ensemble PROJETS (?view=projects, chantier 4) ────────────────────────
    # Chaque projet est replié en UN module boîte noire : ses containers disparaissent,
    # le module n'expose que les PORTS (sources consommées / destinations publiées).
    # Les échanges inter-projets et hors-projet restent visibles ; l'intérieur, non.
    if (request.args.get("view") or "") == "projects":
        from ..auth import vmid_project_ids
        from ..database import db_project_ports
        projs_ov = {p["id"]: p for p in db_get_projects()}
        ports_by_pid: dict = {}
        for pt in db_project_ports(None):
            ports_by_pid.setdefault(pt["project_id"], []).append(pt)
        node_pid = {}
        for tn in topo_nodes:
            pids = vmid_project_ids(tn["vmid"])
            if pids:
                node_pid[tn["vmid"]] = sorted(pids)[0]
        hidden = set(node_pid)
        topo_nodes = [tn for tn in topo_nodes if tn["vmid"] not in hidden]
        producers = {shm: [pn for pn in lst if pn["vmid"] not in hidden]
                     for shm, lst in producers.items()}
        consumers = [cn for cn in consumers if cn["vmid"] not in hidden]
        for pid_ in sorted(set(node_pid.values()) | set(ports_by_pid)):
            pr = projs_ov.get(pid_)
            if not pr:
                continue
            pports = sorted(ports_by_pid.get(pid_, []),
                            key=lambda x: (x.get("kind"), x.get("ord") or 0, x["id"]))
            prods, cons = [], []
            for pt in pports:
                b = pt.get("binding") or {}
                if pt.get("kind") == "source":
                    cons.append({"shm": b.get("shm") or "", "kind": pt.get("media") or "video",
                                 "label": pt.get("name")})
                elif b.get("internal_shm"):
                    prods.append({"shm": b["internal_shm"], "kind": pt.get("media") or "video",
                                  "label": pt.get("name")})
            svmid = -int(pid_)   # vmid synthétique (négatif : jamais un container réel)
            topo_nodes.append({
                "vmid": svmid, "hostname": pr["name"], "status": None,
                "fps": None, "kind": "project", "plugin_version": None,
                "node_id": None, "node_label": "", "project_id": pid_,
                "project_module": True, "project_state": pr.get("state") or "saved",
                "fabric_role": "logical", "fabric_shards": 0, "fabric_parent": None,
                "col": "composition", "split": False, "source": None, "shm_out": None,
                "restarts": 0, "produces": prods, "consumes": cons,
                "max_inputs": 0, "slots_free": 0, "cpu_percent": None, "mem_used": None,
                "cores": None, "memory": None, "cpu_count": None, "av_sync": None,
                "own_latency_ms": None, "shm_active": None, "gpu": None,
            })
            for port in prods:
                producers.setdefault(port["shm"], []).append(
                    {"vmid": svmid, "kind": port["kind"]})
            for _i, port in enumerate(cons):
                if port.get("shm"):
                    consumers.append({"vmid": svmid, "shm": port["shm"],
                                      "kind": port["kind"], "slot": _i})

    # Voyant des ENTRÉES : une entrée est alimentée si le flux auquel elle est câblée est produit.
    # Deuxième passe nécessaire — le producteur d'un flux peut apparaître APRÈS son consommateur
    # dans l'ordre de parcours, donc on ne peut pas renseigner l'entrée au vol.
    #
    # Trois cas, et seulement le deuxième allume/éteint :
    #   • entrée non câblée (shm vide) → aucun voyant, elle porte déjà « déconnectée » ;
    #   • shm connu de la table → allumé/éteint selon son producteur ;
    #   • shm inconnu (producteur muet, ou flux répliqué par RDMA dont la source est ailleurs)
    #     → pas de voyant. Éteindre faute de savoir signalerait une panne inexistante.
    for _tn in topo_nodes:
        for _port in (_tn.get("consumes") or []):
            _s = _port.get("shm")
            if not _s:
                # Entrée SANS CÂBLE : rien n'y arrive, et ce n'est pas une ignorance mais une
                # certitude — aucun flux ne lui est raccordé. Le laisser neutre revenait à
                # l'afficher comme alimentée, ce qui est faux pour la majorité des ports d'un
                # moteur TX (64 entrées déclarées, une seule câblée).
                _port["fed"] = False
            elif _s in fed_shm:
                _port["fed"] = fed_shm[_s]

    from ..metrics import latency_cache
    # Seuil de FRAÎCHEUR : la « latence » d'entrée rapportée par un consommateur est l'ÂGE de la
    # dernière trame lue (now_tai − last_write, cf. multiview script). Pour une source vivante c'est
    # ~1-2 trames (<100 ms) ; pour une entrée GELÉE (source coupée / non abonnée) l'âge grandit sans
    # borne (minutes). Au-delà de ce seuil on ne compte plus ça comme une latence de transport :
    # latency_ms=None (exclue des agrégats max/moyenne/cumul partout), âge brut conservé dans age_ms
    # + flag stale → l'UI affiche « figé » plutôt qu'un nombre aberrant.
    _STALE_INPUT_MS = 5000.0
    # Map vmid → nœud : une arête producteur→consommateur sur deux nœuds DIFFÉRENTS passe forcément
    # par RDMA (seul transport inter-nœud du bus MXL) → on la marque pour le voyant « RDMA » côté UI.
    _vmid_node = {tn["vmid"]: tn.get("node_id") for tn in topo_nodes if tn.get("vmid") is not None}
    # (flux, nœud destination) → statut du lien RDMA qui le réplique. Lu une fois : la table des
    # liens est petite et vit en base, aucun appel réseau.
    _rdma_par_flux = {}
    try:
        from services import rdma as _rdma_svc
        for _l in _rdma_svc.db_list_rdma_links():
            _rdma_par_flux[(_l.get("src_flow"), _l.get("dst_node_id"))] = _l.get("status")
    except Exception:
        pass          # service absent ou non chargé : on n'affirme rien plutôt que d'affirmer faux
    topo_edges = []
    consumed_shms = set()
    for cn in consumers:
        consumed_shms.add(cn["shm"])
        for pn in producers.get(cn["shm"], []):
            _lc = latency_cache.get(cn["vmid"]) or {}
            lat = _lc.get(cn["shm"]) if _lc.get(cn["shm"]) is not None else _lc.get("*")
            _stale = isinstance(lat, (int, float)) and lat > _STALE_INPUT_MS
            _fn, _tn = _vmid_node.get(pn["vmid"]), _vmid_node.get(cn["vmid"])
            topo_edges.append({
                "from": pn["vmid"],
                "to": cn["vmid"],
                "shm": cn["shm"],
                "kind": cn["kind"],
                "slot": cn.get("slot"),
                "latency_ms": (None if _stale else lat),
                "stale": _stale,
                "age_ms": (round(lat) if _stale else None),
                "rdma": bool(_fn is not None and _tn is not None and _fn != _tn),
                # ÉTAT RÉEL de la réplication, et non le simple fait de traverser deux nœuds.
                #
                # Le drapeau `rdma` ci-dessus ne dit QUE « les deux bouts sont sur des nœuds
                # différents ». Le câble affichait donc « ⇄ RDMA » même sans aucun lien provisionné :
                # constaté sur treize câbles audio et ANC dont le consommateur lisait un flux
                # absent de son propre nœud. Le libellé affirmait le transport qui manquait
                # précisément — on ne pouvait pas trouver la panne en regardant l'endroit où elle
                # était. `rdma_link` vaut le statut du lien, ou None quand il n'en existe aucun.
                **({"rdma_link": _rdma_par_flux.get((cn["shm"], _tn))}
                   if (_fn is not None and _tn is not None and _fn != _tn) else {}),
                # Un câble n'est « vivant » que si le flux qu'il porte est réellement produit.
                # Même tri-état que les voyants : la clé est ABSENTE quand on ne sait pas, jamais
                # False par défaut — un câble en pointillé annonce une absence de signal, pas une
                # absence de mesure.
                **({"fed": fed_shm[cn["shm"]]} if cn["shm"] in fed_shm else {}),
            })
    # ─── Suivi des temps de traitement (page Câbles) ─────────────────────────────────────────
    # delay_total = délai de traitement CUMULÉ d'un signal de sortie (somme des latences le long du
    # chemin de la RÉFÉRENCE) → ce qu'il faut compenser sur un audio externe. Le mélangeur/DVE
    # « cassent » l'accumulation linéaire : on suit le chemin de l'entrée de référence, et on expose
    # le délai + le skew de CHAQUE entrée sur leurs ports (immédiat = réf, sinon aligné / en retard).
    from ..metrics import align_cache, inputs_lag_cache
    prod_of = {shm: lst[0]["vmid"] for shm, lst in producers.items() if lst}  # 1er producteur d'un shm
    in_v_edges = {}                        # vmid → arêtes vidéo entrantes
    for e in topo_edges:
        if (e.get("kind") or "video") == "video":
            in_v_edges.setdefault(e["to"], []).append(e)

    # ⚠ MOTEUR SCINDÉ (split_io) : ses deux moitiés — RX (produces) et TX (consumes) — sont DEUX
    # nœuds de topologie portant le MÊME vmid. Raisonner le retard PAR CONTENEUR faisait donc
    # remonter, depuis le RX, l'arête d'entrée du TX jusqu'au mur, et rapportait le temps de calcul
    # de celui-ci sur les SORTIES du RX (`cum=4,3 ms` sur une arête RX→mur, avant que le mur ait
    # rien fait). Circulaire : le garde-fou anti-cycle évitait la récursion infinie et laissait la
    # valeur absurde passer pour une mesure.
    # Le retard est une propriété du FLUX, pas du conteneur — c'est par le shm qu'on raisonne.
    _split_vmids = {n["vmid"] for n in topo_nodes if n.get("split")}
    # SEGMENT A par flux : capture réseau → écriture shm, mesuré par le moteur (`rx_latency_ms`).
    _rx_lat_by_shm = {}
    for _n in topo_nodes:
        for _pp in _n.get("produces") or []:
            if isinstance(_pp.get("rx_latency_ms"), (int, float)):
                _rx_lat_by_shm[_pp["shm"]] = _pp["rx_latency_ms"]

    def _transit_of(vmid, shm, slot=None):
        """Transit de l'arête qui amène `shm` (et ce slot) dans `vmid`."""
        for e in in_v_edges.get(vmid) or []:
            if e["shm"] == shm and (slot is None or e.get("slot") == slot):
                return e.get("latency_ms") or 0.0
        return 0.0

    def _ref_edge(vmid, edges):
        """Arête de l'entrée de RÉFÉRENCE d'un nœud (celle qui porte la timeline de sortie)."""
        k = vmid_kind.get(vmid); pr = vmid_params.get(vmid) or {}
        inputs = pr.get("inputs") or []
        ref_shm = None
        if k == "mixer":
            sr = (align_cache.get(vmid) or {}).get("sync_ref")
            sr = int(sr) if sr is not None else int(pr.get("sync_ref") or 0)
            ref_shm = inputs[sr] if 0 <= sr < len(inputs) else None
        else:
            rs = (_plugins.REGISTRY.get(k) or {}).get("reference_slot")
            if rs is not None:
                ref_shm = inputs[int(rs)] if int(rs) < len(inputs) else None
        if ref_shm:
            for e in edges:
                if e["shm"] == ref_shm:
                    return e
        return edges[0] if edges else None     # mono-entrée / défaut : la 1re entrée vidéo

    _memo = {}
    def _delay_out_shm(shm, stack):
        """Retard cumulé du signal à la SORTIE du producteur de `shm`.

        Point d'entrée unique du cumul : on interroge le FLUX, jamais le conteneur. Un moteur 2110
        scindé produit (RX) et consomme (TX) sous le même vmid — seul le shm dit de quelle moitié
        on parle."""
        v = prod_of.get(shm)
        if v is None:
            return 0.0
        if v in _split_vmids:
            # ORIGINE DE LA CHAÎNE : ce flux vient du réseau, pas d'une entrée shm. Son retard est
            # le SEGMENT A (capture → écriture shm), pas zéro. L'exclure amputait le cumul d'une
            # TRAME ENTIÈRE (19,3 ms mesurés) et interdisait structurellement au total d'approcher
            # le fil-à-fil — `rx_latency_ms` n'était qu'un badge isolé, jamais additionné.
            return _rx_lat_by_shm.get(shm) or 0.0
        return _delay_out(v, stack)

    def _delay_out(vmid, stack):
        if vmid in _memo: return _memo[vmid]
        if vmid is None or vmid in stack: return 0.0
        if vmid in _split_vmids:
            # Le retard en sortie d'un moteur scindé se lit par flux (segment A), pas par vmid :
            # `in_v_edges[vmid]` porte ici les entrées du TX, qui ne mènent PAS aux sorties du RX.
            return 0.0
        edges = in_v_edges.get(vmid) or []
        if not edges:                          # source / générateur → origine
            _memo[vmid] = 0.0; return 0.0
        ref = _ref_edge(vmid, edges)
        base = _delay_out_shm(ref["shm"], stack | {vmid}) if ref else 0.0
        # Cumul = chemin de la réf jusqu'à la PRODUCTION amont (base) + TRANSIT de l'arête de réf
        # (arrivée) + traitement PROPRE de ce nœud (own). La somme télescope en ts_out − origine.
        transit = (ref.get("latency_ms") or 0.0) if ref else 0.0
        own = _own_lat_cache.get(vmid)
        own = own if isinstance(own, (int, float)) else 0.0
        d = (base or 0.0) + transit + own
        _memo[vmid] = d; return d

    for n in topo_nodes:
        ac = align_cache.get(n["vmid"]) or {}
        sr_idx = ac.get("sync_ref")
        sr_idx = int(sr_idx) if sr_idx is not None else (
            int((vmid_params.get(n["vmid"]) or {}).get("sync_ref") or 0) if n.get("kind") == "mixer" else None)
        # Sorties : délai cumulé total du signal produit.
        for port in n["produces"]:
            dt = (_rx_lat_by_shm.get(port.get("shm")) or 0.0) if n["vmid"] in _split_vmids \
                 else _delay_out(n["vmid"], set())
            port["delay_total_ms"] = round(dt, 1) if dt else 0.0
        # Entrées de nœuds SINK (TX 2110, destinations finales) : délai cumulé total jusqu'à la
        # sortie réseau. Permet de lire le retard bout-en-bout sur le port d'entrée du TX.
        if n.get("col") == "sinks" and not n.get("produces"):
            for port in n["consumes"]:
                shm = port.get("shm")
                if not shm:
                    continue
                dt = _delay_out_shm(shm, set()) + _transit_of(n["vmid"], shm, port.get("slot"))
                if dt:
                    port["delay_in_ms"] = round(dt, 1)
        # Entrées de mélangeur/DVE : délai propre + alignement (immédiat / aligné / en retard).
        if n.get("kind") in ("mixer", "split"):
            for port in n["consumes"]:
                shm = port.get("shm")
                if not shm:
                    continue
                din = _delay_out_shm(shm, set())
                port["delay_in_ms"] = round(din, 1) if din else 0.0
                sk = (ac.get("skew") or {}).get(shm)
                if sk is not None:
                    port["skew_ms"] = round(float(sk), 1)
                port["late"] = shm in (ac.get("late") or [])
                if n["kind"] == "mixer":
                    port["is_ref"] = (sr_idx is not None and port.get("slot") == sr_idx)
                else:   # split : le fond (slot 4) est la référence
                    port["is_ref"] = (port.get("slot") == 4)
        # Entrées de multiview (input-locked) : retard par source en IMAGES (0 = synchrone). Une
        # entrée qui dépasse le budget d'1 image est « décalée » → badge « +N img » sur son port.
        if n.get("kind") == "multiview":
            _lag = inputs_lag_cache.get(n["vmid"]) or {}
            for port in n["consumes"]:
                shm = port.get("shm")
                if shm:
                    port["lag_frames"] = int(_lag.get(shm) or 0)

    # Cumul à l'ARRIVÉE par arête (pour le toggle « Cumulé » de la page Câbles) : délai cumulé jusqu'à
    # la sortie du producteur (_delay_out) + transit de l'arête. = retard total du signal quand il
    # ENTRE dans le consommateur. latency_ms (transit) reste la valeur « par étape ».
    for e in topo_edges:
        if (e.get("kind") or "video") != "video":
            continue
        cum = _delay_out_shm(e["shm"], set()) + (e.get("latency_ms") or 0.0)
        if cum:
            e["cum_ms"] = round(cum, 1)

    # ═══ AXE B — DÉLAI DU SIGNAL, en TRAMES ══════════════════════════════════════════════════
    # Tout ce qui précède relève de l'axe CHARGE : temps de CALCUL et transits, en millisecondes
    # sous-trame. Ils disent si un étage a de la MARGE — pas combien de temps le signal met.
    # Ici on ne modélise rien : on relaie la mesure DIRECTE de chaque étage (`delai_etage_trames`
    # = index de sortie − index d'entrée) et le segment A du moteur. Un étage qui ne mesure pas
    # rend le total INCOMPLET et on le NOMME. Absence de mesure = absence de chiffre — jamais un
    # zéro, qui se lirait « cet étage n'ajoute aucun délai ».
    # Cf. docs/reference/LATENCE_CHAINE.md.
    from ..metrics import delai_etage_cache as _delai_cache

    _noeud_prod = {}
    for _n in topo_nodes:
        if _n["produces"] and _n["vmid"] not in _noeud_prod:
            _noeud_prod[_n["vmid"]] = _n

    def _periode_ms_n(_n):
        """Durée d'une trame pour CE nœud, depuis sa cadence NOMINALE (l'intention, pas la mesure)."""
        f = (_n or {}).get("fps_nominal") or (((_n or {}).get("cadence")) or {}).get("cible")
        try:
            f = float(f)
        except (TypeError, ValueError):
            return None
        return (1000.0 / f) if f > 0 else None

    def _periode_ms(vmid):
        _n = _noeud_prod.get(vmid) or {}
        f = _n.get("fps_nominal") or ((_n.get("cadence") or {}).get("cible"))
        try:
            f = float(f)
        except (TypeError, ValueError):
            return None
        return (1000.0 / f) if f > 0 else None

    for _n in topo_nodes:
        _d = _delai_cache.get(_n["vmid"])
        _p = _periode_ms(_n["vmid"])
        # Le moteur scindé n'a pas d'« étage » : sa moitié RX EST le segment A, portée par ses ports.
        _n["delai_etage"] = ({"trames": _d["trames"], "trames_max": _d.get("trames_max"),
                              "ms": (round(_d["trames"] * _p, 1) if _p else None),
                              "propage": bool(_d.get("propage"))}
                             if (_d and _n["produces"] and _n["vmid"] not in _split_vmids) else None)
        # MOITIÉ RX D'UN MOTEUR SCINDÉ : son « étage » EST la réception (capture réseau → shm),
        # et elle est MESURÉE (`rx_latency_ms`). Elle n'apparaissait que sous forme de badge ⇣ sur
        # l'axe Charge, alors que c'est un vrai délai de chaîne : l'axe Délai affichait « rien »
        # pour le premier maillon. On retient le PIRE port, comme partout ailleurs.
        if _n["delai_etage"] is None and _n["vmid"] in _split_vmids and _n.get("col") == "sources":
            _rx = [pp["rx_latency_ms"] for pp in _n["produces"]
                   if isinstance(pp.get("rx_latency_ms"), (int, float))]
            if _rx:
                _pire_rx = max(_rx)
                _n["delai_etage"] = {"trames": (round(_pire_rx / _p, 2) if _p else None),
                                     "trames_max": (round(_pire_rx / _p, 2) if _p else None),
                                     "ms": round(_pire_rx, 1), "propage": False,
                                     "reception": True}

    _memo_sig = {}

    def _delai_signal_shm(shm, stack):
        """(ms, complet, étages non mesurés) du SIGNAL à la sortie du producteur de `shm`."""
        if shm in _memo_sig:
            return _memo_sig[shm]
        v = prod_of.get(shm)
        if v is None or shm in stack:
            return (0.0, True, [])
        if v in _split_vmids:
            a = _rx_lat_by_shm.get(shm)
            r = (a, True, []) if isinstance(a, (int, float)) else (0.0, False, ["réception 2110"])
            _memo_sig[shm] = r
            return r
        edges = in_v_edges.get(v) or []
        if not edges:                                   # générateur (mire, lecteur) → origine
            _memo_sig[shm] = (0.0, True, [])
            return _memo_sig[shm]
        # ⚠ MAX SUR TOUTES LES ENTRÉES, pas l'entrée de référence. L'axe A suit la référence
        # (c'est la timeline de sortie qui l'intéresse) ; l'axe B décrit l'ÂGE DU CONTENU, et un
        # compositeur ne peut pas être plus frais que sa plus VIEILLE entrée. Sur un mur shardé,
        # suivre `edges[0]` aurait rendu invisible un shard n°3 en retard alors que ses tuiles
        # sont dans l'image. Même doctrine que `StageDelay`, qui retient déjà l'entrée la plus
        # vieille À L'INTÉRIEUR d'un étage — il fallait la tenir aussi ENTRE les étages.
        base, complet, manq = 0.0, True, []
        for _e in edges:
            _b, _c, _m = _delai_signal_shm(_e["shm"], stack | {shm})
            if _b > base:
                base = _b
            complet = complet and _c
            for _x in _m:
                if _x not in manq:
                    manq.append(_x)
        _d, _p = _delai_cache.get(v), _periode_ms(v)
        if _d and _p:
            r = (base + _d["trames"] * _p, complet, list(manq))
        else:
            _nm = (_noeud_prod.get(v) or {}).get("hostname") or str(v)
            r = (base, False, list(manq) + [_nm])
        _memo_sig[shm] = r
        return r

    def _sig_port(_ms, _complet, _manq, _p):
        return {"ms": round(_ms, 1), "trames": (round(_ms / _p, 2) if _p else None),
                "complet": bool(_complet), "manquants": _manq}

    for _n in topo_nodes:
        _p = _periode_ms_n(_n)
        # ORIGINE DE CHAÎNE (aucune arête vidéo entrante : moteur RX, mire, lecteur) : le « cumul »
        # y vaut son PROPRE segment A et rien d'autre. Le répéter sur chacun des 18 ports du moteur
        # n'apprend rien — c'est déjà le badge ⇣ de l'axe Charge — et fait déborder la tuile.
        _origine = not (in_v_edges.get(_n["vmid"]) or []) or _n["vmid"] in _split_vmids
        if not _origine:
            for port in _n["produces"]:
                port["delai_signal"] = _sig_port(*_delai_signal_shm(port.get("shm"), set()), _p)
        # SINKS (aucune sortie : moniteur, streamer, TX 2110) : le cumul se lit sur leurs ENTRÉES.
        # C'est l'endroit le plus utile de toute la page — « de combien est décalé ce que je
        # regarde ? » — et il n'y avait rien, le cumul n'étant posé que sur des sorties.
        if not _n["produces"]:
            for port in _n["consumes"]:
                _shm = port.get("shm")
                if _shm:
                    port["delai_signal"] = _sig_port(*_delai_signal_shm(_shm, set()), _p)
        # ÉMISSION 2110 : jamais mesurée à ce jour — le 1,00 trame de référence est une
        # SOUSTRACTION sur la boucle mur→TX→fil→RX (2026-08-12), pas une mesure. On l'affiche donc
        # comme une constante ÉTIQUETÉE : une constante annoncée est honnête, une constante
        # silencieuse est un mensonge. Mesurable (chemin tranche : notify_frame_done + meta->epoch),
        # cf. docs/reference/LATENCE_CHAINE.md §6.
        if _n["vmid"] in _split_vmids and _n.get("col") == "sinks":
            _pe = _periode_ms(_n["vmid"]) or ((_n.get("cadence") or {}).get("cible")
                                              and 1000.0 / float(_n["cadence"]["cible"]))
            _n["delai_emission"] = {"trames": 1.0, "ms": (round(_pe, 1) if _pe else None),
                                    "mesure": False, "estime": True}
            # CUMUL AU FIL = ce qui ARRIVE sur le TX + ce que le TX ajoute en émettant. Le port
            # d'entrée porte l'arrivée (juste, et utile) ; la carte, elle, doit répondre « quel âge
            # a l'image qui part sur le fil ? ». Sans ce terme, le TX affichait exactement le même
            # chiffre que sa source alors qu'on lui compte une image — l'écart passait à la trappe.
            # Marqué `estime` : le dernier terme est une constante déclarée, pas une mesure (§6 de
            # docs/reference/LATENCE_CHAINE.md — le chemin tranche du moteur permettrait de la
            # mesurer pour de bon).
            _ins = [pp.get("delai_signal") for pp in _n["consumes"] if pp.get("delai_signal")]
            if _ins and _pe:
                _pire_in = max(_ins, key=lambda q: q.get("ms") or 0)
                _tot = (_pire_in.get("ms") or 0) + _pe
                _n["delai_fil"] = {"ms": round(_tot, 1),
                                   "trames": round(_tot / _pe, 2),
                                   "complet": bool(_pire_in.get("complet")),
                                   "manquants": list(_pire_in.get("manquants") or []),
                                   "estime": True}

    # Tag les ports producteurs comme "free" s'ils ne sont consommés nulle part
    for n in topo_nodes:
        for port in n["produces"]:
            port["free"] = port["shm"] not in consumed_shms

    # Raccourcis : top 3 projets et top 3 snapshots de câblage les plus récents
    try:
        recent_projects = db_get_projects()[:3]
        recent_projects = [{"id": p["id"], "name": p["name"], "created_at": p["created_at"],
                            "vmid_count": len(p.get("snapshot") or [])}
                           for p in recent_projects]
    except Exception:
        recent_projects = []
    try:
        recent_cables = db_cable_snapshots_list()[:3]
        recent_cables = [{"id": s["id"], "name": s["name"], "created_at": s["created_at"],
                          "edge_count": len((s["payload"] or {}).get("edges") or [])}
                         for s in recent_cables]
    except Exception:
        recent_cables = []

    # Route nav par type (déclarée au manifeste) → chips cliquables sur la home
    nav_routes = {}
    for m in _plugins.all():
        route = (m.get("nav") or {}).get("route")
        if route:
            nav_routes[m.get("type")] = route

    # Stats agrégées du pipeline MXL (pour le graphique de la home). Tout est dérivé de
    # topo_nodes (produces[].format) → aucun coût supplémentaire par-frame.
    from ..scripts import CHROMA_DIV
    _VIDEO_RING, _AUDIO_RING = 10, 100   # tailles de ring shm (cf. scripts des plugins)
    _flows_v = _flows_a = _grains = 0
    _bw_bps = 0.0
    _bw_par_noeud = {}          # node_id → bande passante produite sur CE nœud (bit/s)
    for n in topo_nodes:
        _nid = n.get("node_id")
        for pr in (n.get("produces") or []):
            fmt = pr.get("format") or {}
            if (pr.get("kind") or "video") == "audio":
                _flows_a += 1; _grains += _AUDIO_RING
                sr = int(fmt.get("sample_rate") or 48000); ch = int(fmt.get("channels") or 8)
                bd = int(fmt.get("bit_depth") or 24)
                _b = sr * ch * (bd / 8.0) * 8.0
            else:
                _flows_v += 1; _grains += _VIDEO_RING
                w = int(fmt.get("width") or 0); h = int(fmt.get("height") or 0)
                fps = float(fmt.get("fps") or 0)
                cw, ch2 = CHROMA_DIV.get(str(fmt.get("chroma") or "422"), CHROMA_DIV["422"])
                _b = w * h * (1.0 + 2.0 / (cw * ch2)) * fps * 8.0 if (w and h and fps) else 0.0
            _bw_bps += _b
            _bw_par_noeud[_nid] = _bw_par_noeud.get(_nid, 0.0) + _b
    # ── EMPREINTE DU BUS MXL, et pourquoi ce n'est PAS une somme par conteneur ────────────────
    # La tuile sommait `containers.mem_used` sur un total de `containers.memory`. Les deux étaient
    # faux, chacun à sa façon (mesuré le 2026-08-30, dl360-1) :
    #  · `containers.memory` vaut 2048 pour TOUT LE MONDE — c'est le défaut du paramètre de
    #    `db_upsert_container_docker`, que personne n'écrase jamais. Colonne héritée de LXC, elle
    #    ne décrit plus rien : on divisait une mesure réelle par une constante.
    #  · `mem_used` est le `memory.current` du cgroup, et les pages d'un tmpfs sont facturées au
    #    cgroup qui les a TOUCHÉES EN PREMIER. Relevé : le moteur portait 222 Mio de `shmem`,
    #    `hello-world` 40 Mio d'un flux qu'il ne faisait que LIRE, et le streamer 0. Cette
    #    répartition change à chaque redémarrage de conteneur — on additionnait un découpage
    #    arbitraire et instable de la mémoire PARTAGÉE.
    # Le bus, lui, a une taille EXACTE et calculable : la profondeur d'anneau du SDK MXL n'est pas
    # un nombre de cases mais une DURÉE (`history_duration`, réglage `mxl_history_ms`), donc
    #     octets = débit du domaine × durée d'historique.
    # Vérifié : un flux 1080p50 4:2:2 8 bits = 1,66 Gbit/s × 0,2 s = 41,5 Mo, et les flux vidéo du
    # domaine de dl360-1 pèsent 40 Mio chacun sur disque. La durée est lue PAR NŒUD (le réglage
    # l'est), jamais supposée : deux nœuds peuvent porter deux profondeurs.
    from ..mtl import MXL_HISTORY_MS_DEFAULT
    _mxl_bytes = 0.0
    for _nid, _b in _bw_par_noeud.items():
        try:
            _ms = int(st.setting_for("mxl_history_ms", _nid) or MXL_HISTORY_MS_DEFAULT)
        except (TypeError, ValueError):
            _ms = MXL_HISTORY_MS_DEFAULT
        _mxl_bytes += _b / 8.0 * (_ms / 1000.0)
    stats = {
        "flows": {"video": _flows_v, "audio": _flows_a, "total": _flows_v + _flows_a},
        "grains": _grains,
        "bandwidth_bps": int(_bw_bps),
        # Empreinte CALCULÉE du bus, et RAM PHYSIQUE des nœuds (complétée plus bas, une fois les
        # snapshots node_health lus). `ram_node_total` reste None si aucun snapshot n'est encore
        # arrivé : le front affiche alors la valeur seule, jamais un ratio sur zéro.
        "mxl_bytes": int(_mxl_bytes),
        "ram_node_used": None,
        "ram_node_total": None,
    }

    # ─── Santé CLUSTER : agrégat pire-cas multi-nœuds (lu du cache node_health, AUCUN SSH ici) ──
    # node_health échantillonne déjà CPU/RAM/disque/membw/GPU par nœud + contrôleur (sampler 5 s).
    # On ne fait qu'agréger le dernier snapshot. Esprit défensif : tout champ peut manquer au boot.
    from .. import node_health as _nh
    from ..database import db_list_rdma_links
    _hsnap = _nh.latest()
    _node_snaps = [s for s in (_hsnap.get("nodes") or {}).values() if isinstance(s, dict)]
    if isinstance(_hsnap.get("controller"), dict):
        _node_snaps.append(_hsnap["controller"])

    # RAM PHYSIQUE des nœuds, pour donner un dénominateur à l'empreinte MXL calculée plus haut.
    # ⚠ NŒUDS SEULEMENT — le contrôleur ne porte pas de domaine MXL, l'inclure gonflerait le total
    # d'une machine qui n'héberge aucun flux et ferait paraître la marge plus grande qu'elle n'est.
    _rn_used = _rn_total = 0
    for _s in (_hsnap.get("nodes") or {}).values():
        if not isinstance(_s, dict):
            continue
        _r = _s.get("resources") or {}
        if _r.get("mem_total_mb"):
            _rn_total += int(_r["mem_total_mb"]) * 1024 * 1024
            _rn_used += int(_r.get("mem_used_mb") or 0) * 1024 * 1024
    if _rn_total:
        stats["ram_node_used"], stats["ram_node_total"] = _rn_used, _rn_total

    def _max_ok(vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    _cpu_vals, _mem_vals, _disk_vals, _gpu_util, _gpu_pcie = [], [], [], [], []
    _membw_level = None
    _gpu_present = False
    _mb_warn = st.setting_for("membw_warn_ratio", _ptp_ref_nid) or 0.5
    _mb_err = st.setting_for("membw_err_ratio", _ptp_ref_nid) or 0.3
    for _s in _node_snaps:
        _res = _s.get("resources") or {}
        _cpu = _res.get("cpu_pct_real")
        if _cpu is None:
            _cpu = _res.get("cpu_pct")
        _cpu_vals.append(_cpu)
        if _res.get("mem_total_mb"):
            _mem_vals.append((_res.get("mem_used_mb") or 0) / _res["mem_total_mb"] * 100)
        for _d in (_s.get("disks") or {}).values():
            _disk_vals.append(_d.get("pct"))
        _ratio = (_s.get("membw") or {}).get("ratio")
        if _ratio is not None:
            _lvl = "err" if _ratio < _mb_err else ("warn" if _ratio < _mb_warn else None)
            if _lvl == "err" or (_lvl == "warn" and _membw_level != "err"):
                _membw_level = _lvl
        _gp = (_s.get("gpu") or {}).get("gpus") or []
        if _gp:
            _gpu_present = True
            _gpu_util += [g.get("util_pct") for g in _gp]
            _gpu_pcie += [g.get("pcie_pct") for g in _gp]

    _nodes = db_get_nodes() or []
    if _nodes:
        _nodes_total = len(_nodes)
        _nodes_online = sum(1 for n in _nodes if n.get("status") == "up")
        if any(n.get("gpu_capable") for n in _nodes):
            _gpu_present = True
    else:  # mono-box legacy (table nodes vide) : le contrôleur EST le seul nœud.
        _nodes_total = 1
        _nodes_online = 1 if isinstance(_hsnap.get("controller"), dict) else 0

    _cpu_max, _mem_max, _disk_max = _max_ok(_cpu_vals), _max_ok(_mem_vals), _max_ok(_disk_vals)
    _gpu_u, _gpu_p = _max_ok(_gpu_util), _max_ok(_gpu_pcie)
    _disk_warn = st.setting_for("node_health_disk_warn_pct", _ptp_ref_nid) or 85.0
    _disk_err = st.setting_for("node_health_disk_err_pct", _ptp_ref_nid) or 95.0

    # RDMA — liens persistés (vide → pastille masquée côté front). Lecture DB, pas de host_exec.
    # ⚠ VOCABULAIRE DE STATUT. Le service émet `pending | running | error | waiting | stopped`.
    # Le comptage cherchait ("established", "up", "ok", "active") — quatre valeurs qui n'existent
    # nulle part : AUCUN lien n'était donc jamais compté comme établi, `degraded` valait le total,
    # et la pastille d'accueil restait ROUGE en permanence en annonçant « 17 liens ». Une alarme qui
    # est toujours allumée n'alarme plus personne ; elle apprend seulement à ignorer la pastille.
    #
    # SÉVÉRITÉ, ensuite. Seul `error` est un échec d'infrastructure. `waiting` signifie que la
    # source n'est pas (encore) produite — c'est bénin par construction, et c'est même l'état
    # NORMAL d'un lien pré-câblé en attente de son flux. `pending` est transitoire (établissement
    # en cours). Les compter comme dégradés remettrait la pastille au rouge en permanence, par un
    # autre chemin.
    _rdma_links = db_list_rdma_links() or []
    _rdma_st = [(l.get("status") or "").lower() for l in _rdma_links
                if (l.get("status") or "").lower() != "stopped"]
    _rdma = {"count": len(_rdma_st),
             "running": _rdma_st.count("running"),
             "waiting": _rdma_st.count("waiting"),
             "pending": _rdma_st.count("pending"),
             "error": _rdma_st.count("error")}
    # Conservé pour les consommateurs existants, mais avec le sens qu'il aurait toujours dû avoir.
    _rdma["degraded"] = _rdma["error"]

    # Santé globale : err > warn > ok. La pastille reflète la santé de l'INFRASTRUCTURE
    # (plateforme) — nœuds, disque, RAM, bande passante mémoire, PTP, liens RDMA — et NON les
    # anomalies de flux/config (RX/TX sans signal, collisions multicast, proxies orphelins). Ces
    # dernières sont des « Points d'attention » (cf. bloc `attention` + panneau Monitoring) : elles
    # ne doivent pas faire passer tout le système en « dégradé » alors qu'il est fonctionnel. On ne
    # gate donc PLUS la santé sur le journal d'alertes (opérationnel) ; les conditions infra qui
    # persistent sont déjà couvertes par les signaux live ci-dessous.
    _failed = (
        _nodes_online < _nodes_total or _membw_level == "err"
        or (_disk_max is not None and _disk_max >= _disk_err)
        or (ptp_info["enabled"] and not ptp_info["locked"])
        or _rdma["error"] > 0
    )
    _degraded = (
        _membw_level == "warn"
        or (_cpu_max is not None and _cpu_max >= 90)
        or (_mem_max is not None and _mem_max >= 90)
        or (_disk_max is not None and _disk_max >= _disk_warn)
    )
    _round = lambda v: round(v) if v is not None else None
    cluster = {
        "health": "err" if _failed else ("warn" if _degraded else "ok"),
        "nodes_online": _nodes_online, "nodes_total": _nodes_total,
        "cpu_max_pct": _round(_cpu_max), "mem_max_pct": _round(_mem_max),
        "disk_max_pct": _round(_disk_max), "membw_level": _membw_level,
        "gpu_present": _gpu_present,
        "gpu_util_max_pct": _round(_gpu_u), "gpu_pcie_max_pct": _round(_gpu_p),
        "rdma": _rdma,
    }

    # Listes pour les filtres de la page Câbles : seuls les projets/nœuds RÉELLEMENT présents
    # parmi les nœuds de topologie (évite de proposer des entrées vides).
    _proj_names = {p["id"]: p["name"] for p in db_get_projects()}
    _f_proj_ids = sorted({n.get("project_id") for n in topo_nodes if n.get("project_id")})
    _f_node_ids = {n.get("node_id") for n in topo_nodes}
    cable_filters = {
        "projects": [{"id": pid, "name": _proj_names.get(pid, f"#{pid}")} for pid in _f_proj_ids],
        "nodes": [{"id": nid, "name": _node_label_for(nid)}
                  for nid in sorted(_f_node_ids, key=lambda x: (x is not None, x))],
    }

    # ─── Points d'attention (état LIVE) ──────────────────────────────────────────────────────
    # Anomalies opérationnelles de FLUX/CONFIG, lues des caches LIVE (rx/tx_stalled_cache, conflits
    # multicast, KPI pyramide) — PAS du journal d'alertes, qui n'émet qu'au franchissement et ne
    # refléterait pas une panne persistante. Alimente le panneau « Points d'attention » du
    # Monitoring. N'affecte PAS la pastille « Système » (qui reste infra, cf. cluster.health).
    from ..metrics import rx_stalled_cache as _rx_stalled, tx_stalled_cache as _tx_stalled
    _host_by_vmid = {tn["vmid"]: tn.get("hostname") for tn in topo_nodes if tn.get("vmid") is not None}
    attention = []
    for _cache, _kind in ((_rx_stalled, "rx_stall"), (_tx_stalled, "tx_stall")):
        for _vmid, _slots in (_cache or {}).items():
            _bad = sorted(i for i, _stl in (_slots or {}).items() if _stl)
            if _bad:
                attention.append({"severity": "warning", "kind": _kind, "vmid": _vmid,
                                  "host": _host_by_vmid.get(_vmid), "count": len(_bad),
                                  "slots": _bad})
    # Conteneur INJOIGNABLE (statut `unreachable`, cf. metrics) : son agent ne répond plus alors
    # qu'il tourne toujours. C'est le cas qui a produit l'incident des multiviews « à 49,8 fps » —
    # il n'apparaissait NULLE PART en état live, seulement dans le journal d'alertes où il se
    # noyait. Sévérité `error` : contrairement à un flux qui décroche, on ne peut plus RIEN piloter.
    for _c in containers:
        if (_c.get("status") or _c.get("statut")) != "unreachable":
            continue
        attention.append({"severity": "error", "kind": "unreachable", "vmid": _c.get("vmid"),
                          "host": _c.get("hostname"), "node_id": _c.get("node_id"), "count": 1})

    # Registre NMOS lu UNE fois pour les deux constats multicast qui suivent (conflits + plages
    # épuisées). Ils le relisaient chacun de leur côté : deux scans + re-parse JSON de toutes les
    # ressources par requête, sur une route pollée toutes les 2 s.
    try:
        from ..allocations import _registry_transports as _reg_tr
        _transports = _reg_tr()
    except Exception:                                                      # noqa: BLE001
        _transports = None
    try:
        from ..allocations import multicast_conflicts as _mc_conf
        _mc = _mc_conf(_transports) or []
        if _mc:
            attention.append({"severity": "warning", "kind": "mcast",
                              "count": len(_mc), "items": sorted(_mc)[:8]})
    except Exception:
        pass
    try:
        from ..allocations import plages_epuisees as _pl_ep
        _ep = _pl_ep(_transports) or []
        if _ep:
            attention.append({"severity": "error", "kind": "mcast_range_exhausted", "count": len(_ep),
                              "items": [{"label": r.get("label") or r["base_ip"], "scope": r["scope"]}
                                        for r in _ep][:8]})
    except Exception:
        pass
    _pyr = _pyramide_kpi()
    if _pyr and ((_pyr.get("orphans") or 0) or (_pyr.get("unmet") or 0)):
        attention.append({"severity": "warning", "kind": "pyramide",
                          "orphans": _pyr.get("orphans") or 0, "unmet": _pyr.get("unmet") or 0})
    # Conteneurs orphelins sur les nœuds (réconciliation DB↔réalité, audit B2) : une entrée
    # PAR orphelin — le panneau Monitoring offre l'action « Détruire » (perm containers.delete).
    try:
        from ..fleet_status import orphans_actuels as _orph
        for _o in _orph():
            attention.append({"severity": "warning", "kind": "orphan_container", **_o})
    except Exception:
        pass
    _annoter_anciennete(attention)

    return jsonify({
        "ptp": ptp_info,
        "cluster": cluster,
        "stats": stats,
        "nmos": {
            "receivers_video": recv_video,
            "receivers_audio": recv_audio,
            "receivers_subscribed": recv_subscribed,
            "senders_video": send_video,
            "senders_audio": send_audio,
            "mdns_active": mdns_active,
        },
        "containers": {
            "running": running,
            "total": len(containers),
            "multiview_count": multiview_count,
            "by_type": dict(by_type),
            "nav_routes": nav_routes,
            "io2110": io2110_counts,
        },
        "mtl_log": mtl_log_info,
        "mxl_flows": mxl_flows,
        "alerts": {
            "total": alerts_total,
            "recent": alerts_recent,
        },
        "topology": {
            "nodes": topo_nodes,
            "edges": topo_edges,
        },
        "cable_filters": cable_filters,
        "shortcuts": {
            "projects": recent_projects,
            "cable_snapshots": recent_cables,
        },
        "pyramide": _pyr,
        "attention": attention,
    })
