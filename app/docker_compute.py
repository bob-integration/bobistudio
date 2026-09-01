# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Driver Docker « compute » — fait tourner un plugin de calcul (color_corrector, mixer, split,
multiview, avsync…) en conteneur Docker sur un nœud du cluster.

Différent du chemin MTL (`docker_driver.py`) : pas de --privileged, pas de hugepages, pas de
DPDK/XDP, pas de NMOS, pas de limite 1/nœud. Le conteneur embarque l'AGENT générique
(plugins/_compute_runtime, qui exécute script_templates/agent.py) → il expose le MÊME contrat
:8081 deploy/start/stop/status qu'un LXC. Réseau macvlan/ipvlan : IP propre par conteneur, donc
:8080/:8081/:8082 fixes. /dev/shm bind-monté → le conteneur rejoint le pipeline MXL.

Astuce centrale : `deploy_compute()` ne fait QUE garantir un conteneur up avec son IP + l'agent ;
le rendu/poussée du script.py est ensuite assuré par le **chemin agent standard** de
`deploy.deployer_script` (get_container_ip → POST :8081/deploy + /start), exactement comme un LXC.

IDEMPOTENCE (correctif boucle de recréation) : `deploy_compute` est appelé par `deployer_script`
à CHAQUE (re)déploiement de script. Or la création côté agent-nœud est un `docker rm -f` + `docker
run` INCONDITIONNEL → chaque poussée de script RECRÉAIT le conteneur, ce qui :
  1. coupait la sortie live (le mur/streamer repart de zéro à chaque changement de paramètre) ;
  2. VIDAIT le rootfs éphémère (/opt/script) — donc l'agent répondait `running:false, path:null`,
     signature que la boucle de surveillance (metrics) interprète comme « script perdu » et
     répare par… un redéploiement complet → qui recrée le conteneur → qui revide le rootfs.
     Boucle auto-entretenue (180 recréations mesurées sur une matinée).
On ne recrée donc le conteneur QUE s'il n'est pas en marche OU si sa spec `docker run` a
réellement changé (image, réseau, IP, mounts, ressources/cpuset, GPU) — comparaison par
SIGNATURE persistée (`containers.runtime_spec_sig`). Un simple push de script ne touche plus au
conteneur. Les appelants qui veulent VRAIMENT un conteneur neuf passent `force=True`.
"""

import hashlib
from .numerotation import cle_input, cle_input_v, cle_input_a
import json
import logging
import math
import os
import shlex
import time

import requests

from .host_ops import ssh_run
from .database import (db_update_container_image,
                       db_get_container, db_get_node, db_get_nodes, db_get_containers,
                       db_upsert_container_docker, db_update_status, db_update_docker_ip,
                       db_update_node, db_delete_container, db_add_alert, db_get_project,
                       db_update_deploy_config, db_update_resources, db_update_spec_sig)
from . import plugins
from . import journal as _journal
from . import allocations

log = logging.getLogger(__name__)


# ─── Prédicats de routage ────────────────────────────────────────────
def is_mtl_type(type_):
    """Vrai si le type est un plugin matériel MTL (chemin docker_driver, --network host,
    controller bâti dans l'image). Faux pour un plugin de calcul générique."""
    return bool(type_) and plugins.needs_dpdk(type_)


def is_compute_container(vmid_or_row):
    """Vrai si le container est un Docker « compute » — c'est-à-dire de type non-MTL. Tout
    conteneur est un conteneur Docker (le backend LXC est retiré), donc le seul discriminant
    restant est le TYPE. Prédicat utilisé par deploy/metrics/routes pour brancher ce chemin."""
    c = vmid_or_row if isinstance(vmid_or_row, dict) else db_get_container(vmid_or_row)
    if not c:
        return False
    return not is_mtl_type(_type_of(c))


def _type_of(c):
    """Type de plugin déployé (depuis deploy_config), '' si inconnu."""
    import json
    try:
        return (json.loads(c.get("deploy_config") or "{}") or {}).get("type") or ""
    except Exception:
        return ""


def _params_of(c):
    """Params déployés (depuis deploy_config), {} si absent/illisible."""
    import json
    try:
        return (json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        return {}


def _name(vmid):
    return f"bobi-cmp-{vmid}"


def _veut_gpu_instance(deploy_type, c, params=None):
    """Cette INSTANCE doit-elle se voir allouer un GPU ?

    `params` = les paramètres EN COURS DE DÉPLOIEMENT. Ils priment sur `deploy_config`, qui n'est
    persisté qu'APRÈS ce point : sans cela la décision GPU accuse un déploiement de retard —
    basculer un split de `render=cpu` à `render=gpu` gardait l'image CPU (et, en `render=auto`,
    serait retombé sur le CPU sans que rien ne le dise).

    Deux cas. Le type qui DÉCLARE un besoin (`resources.gpu`) : toujours oui, comportement
    historique. Le type seulement CAPABLE (`resources.gpu_optional`, ex. `streamer`) : oui
    seulement si ses params le demandent — `video.encoder` valant `nvenc` (exigé) ou `auto`
    (préféré). Un streamer en x264 ne réserve donc rien, ce qui évite qu'il immobilise une des
    quatre cartes exploitables du parc.

    ⚠ `auto` demande la carte SANS l'exiger : si l'allocation échoue (aucune libre) ou si le nœud
    n'a pas de GPU, le déploiement se poursuit et le script retombe sur x264 EN LE DISANT. C'est
    `nvenc` qui doit échouer bruyamment — cf. `plugins/streamer/script.py`."""
    from . import plugins
    if plugins.wants_gpu(deploy_type):
        return True
    if not plugins.gpu_optional(deploy_type):
        return False
    if not isinstance(params, dict):
        try:
            params = (json.loads((c or {}).get("deploy_config") or "{}") or {}).get("params") or {}
        except (ValueError, AttributeError):
            return False
    # Règle DÉCLARÉE par le manifeste (chemin pointé + valeurs acceptées). C'est la voie normale
    # pour tout nouveau plugin GPU-capable : sans elle, l'orchestrateur accumulerait un `if` par
    # plugin, chacun connaissant le nom d'un paramètre qui ne le regarde pas.
    rule = plugins.gpu_instance_rule(deploy_type)
    if rule:
        cur = params
        for seg in rule["param"].split("."):
            cur = (cur or {}).get(seg) if isinstance(cur, dict) else None
        return str(cur or "").lower() in rule["values"]
    # Repli historique (streamer, manifeste sans règle) : NVENC exigé ou préféré.
    try:
        enc = str(((params.get("video") or {}).get("encoder") or "cpu")).lower()
    except AttributeError:
        return False
    return enc in ("nvenc", "auto")


_reseau_alerte = {}          # node_id → dernier motif alerté (anti-répétition)


def _alerter_reseau(node, etat):
    """Alerte UNE fois par motif : la boucle de placement passe souvent, l'exploitant une seule."""
    motif = "derive" if etat.get("derive") else "sans_lien"
    nid = node.get("id")
    if _reseau_alerte.get(nid) == motif:
        return
    _reseau_alerte[nid] = motif
    from .database import db_add_alert
    if motif == "derive":
        db_add_alert("alert.node.reseau_derive", "warning", node_id=nid, kind="net",
                     params={"n": node.get("name"), "reel": etat.get("reel") or "?",
                             "declare": etat.get("declare")})
    else:
        db_add_alert("alert.node.reseau_sans_lien", "warning", node_id=nid, kind="net",
                     params={"n": node.get("name"),
                             "carte": etat.get("reel") or etat.get("declare") or "?"})


def _reseau_retabli(node):
    """Le réseau conteneurs est REDEVENU sain : lever l'écart, et le DIRE.

    Sans ça, `_reseau_alerte` gardait le motif pour toujours : l'exploitant restait devant un
    « r620-3 écarté des déploiements » qui n'était plus vrai (l'éligibilité, elle, est réévaluée à
    chaque passage — le nœud reprenait des conteneurs en silence), et surtout une RÉCIDIVE ne
    réalertait plus JAMAIS, puisque le motif mémorisé était déjà le bon. Une alarme qui ne peut
    plus se redéclencher est pire que pas d'alarme : elle donne le sentiment d'être surveillé.
    Constaté le 2026-08-02 sur r620-3 — alerte de dérive à 22h07:58, état sain relu à 23h20, aucun
    message entre les deux. Même convention que la boucle de surveillance, qui annonce déjà son
    « durée redevenue normale ».
    """
    nid = node.get("id")
    if _reseau_alerte.pop(nid, None) is None:
        return                      # aucun écart n'avait été signalé : rien à lever
    from .database import db_add_alert
    db_add_alert("alert.node.reseau_retabli", "info", node_id=nid, kind="net",
                 params={"n": node.get("name")})


def pick_compute_node(prefer_id=None, deploy_type=None):
    """Choisit un nœud pour un conteneur compute. `prefer_id` honoré s'il est éligible.

    Éligibilité inchangée : `compute_image` ET `docker_network` renseignés, statut ≠ down.
    Ce qui change, c'est le CLASSEMENT des éligibles — il n'y en avait aucun.

    ⚠ AVANT : « le premier nœud éligible », c'est-à-dire le premier par IDENTIFIANT. Aucun rapport
    avec la capacité. Sur ce parc, tout monitor atterrissait donc sur dl360-1 — 42 CPU isolés sur
    48, moteur 2110 à 9,3 cœurs, pool de 2 cœurs physiques — pour un `streamer` qui en coûte 3,
    pendant que dell-1 (88 CPU, sans moteur) restait inutilisé.

    Deux critères, dans cet ordre :

    **1. Affinité GPU, dans les DEUX SENS.** Un type qui sait exploiter une carte
    (`plugins.gpu_capable`) préfère un nœud qui en a une de LIBRE. Un type qui ne sait pas
    l'ÉVITE tant qu'un autre nœud convient — sinon un monitor occupe la seule T4 que cherche un
    multiview. L'attirance seule ne protège pas la ressource rare ; la répulsion seule empêche un
    type GPU d'aller la chercher.

    **2. Cœurs physiques ORDONNANÇABLES libres** (`core_pool.capacite_placement`), qui traite
    l'absence de pool comme « toute la machine » et non comme « aucune capacité ».

    À égalité, l'identifiant tranche : le choix reste déterministe (utile aux tests et à
    l'explication d'un placement)."""
    from . import core_pool, gpu_pool
    gpu_utile = bool(deploy_type) and plugins.gpu_capable(deploy_type)

    def _eligible(n):
        if not (n and n.get("compute_image") and n.get("docker_network")
                and (n.get("status") or "") != "down"):
            return False
        # ⚠ Un nœud dont le RÉSEAU CONTENEURS est mort reste « éligible » à tous les autres titres :
        # image présente, réseau déclaré, statut up, agent joignable. Ses conteneurs y démarrent,
        # prennent leur IP, lancent leur agent — et personne ne peut jamais leur parler, avec un
        # statut Docker « running » parfaitement vert. Constaté sur r620-3 le 2026-08-02 : un
        # monitor y a été placé (par ce même classement, qui répartit enfin la charge) sur un
        # macvlan accroché à une carte sans porteuse. On l'écarte, et on DIT pourquoi.
        try:
            from . import node_driver
            e = node_driver.etat_reseau_conteneurs(n)
            if e.get("derive") or e.get("sans_lien"):
                _alerter_reseau(n, e)
                return False
            _reseau_retabli(n)
        except Exception as exc:
            log.debug("état réseau conteneurs %s: %s", n.get("name"), exc)
        return True

    if prefer_id:
        n = db_get_node(prefer_id)
        if _eligible(n):
            return n["id"]

    candidats = [n for n in db_get_nodes() if _eligible(n)]
    if not candidats:
        return None

    def _score(n):
        try:
            st = gpu_pool.gpu_status(n["id"]) or {}
            libres_gpu = max(0, int(st.get("count") or 0) - int(st.get("used") or 0))
        except Exception:
            libres_gpu = 0
        affinite = (1 if libres_gpu > 0 else 0) if gpu_utile else (0 if libres_gpu > 0 else 1)
        try:
            libres = core_pool.capacite_placement(n["id"])["libres"]
        except Exception as e:
            log.debug("pick_compute_node: capacité illisible pour %s (%s)", n.get("name"), e)
            libres = 0
        return (affinite, libres, -int(n["id"]))

    meilleur = max(candidats, key=_score)
    if len(candidats) > 1:
        log.info("pick_compute_node(%s) → %s (%s)", deploy_type or "?", meilleur.get("name"),
                 " · ".join("%s:aff=%d,libres=%d" % (n.get("name"), _score(n)[0], _score(n)[1])
                            for n in candidats))
    return meilleur["id"]


# ─── Cycle de vie ────────────────────────────────────────────────────
def creer_container_compute(node_id, deploy_type, hostname=None):
    """Alloue un vmid synthétique (unicité globale → topologie/câblage inchangés) et enregistre
    une ligne backend='docker' rattachée au nœud. Pas de `docker run` ici (vient au deploy)."""
    node = db_get_node(node_id)
    if not node:
        db_add_alert("alert.deploy.compute.noeud_introuvable", "error", node_id=node_id,
                     kind="deploy", params={"node_id": node_id})
        return None
    if not node.get("compute_image"):
        db_add_alert("alert.deploy.compute.sans_image", "error", node_id=node_id,
                     kind="deploy", params={"n": node["name"]})
        return None
    if not node.get("docker_network"):
        db_add_alert("alert.deploy.compute.sans_reseau", "error", node_id=node_id,
                     kind="deploy", params={"n": node["name"]})
        return None

    vmid = allocations.next_free_vmid()
    if vmid is None:
        return None   # plage de VMID épuisée → alerte déjà émise par next_free_vmid ; pas de ligne vmid=None
    if not hostname:
        hostname = f"mxl{vmid}"
    db_upsert_container_docker(vmid, hostname, node_id, _name(vmid), status="created")
    # Persiste le type DÈS la création : si le push d'agent (:8081/deploy) échoue ensuite
    # (ex. IP macvlan en collision avec un équipement LAN), le container reste TYPÉ et
    # reprenable plutôt que de devenir un fantôme deploy_config=None invisible dans l'UI.
    if deploy_type:
        # Format de sortie d'un multiview = défaut SYSTÈME (jamais un littéral en dur). Les autres
        # types compute adaptent leur entrée → pas de format fixe à semer.
        _seed = {}
        if deploy_type == "multiview":
            from .scripts import multiview_output_format_defaults
            _seed = multiview_output_format_defaults()
        db_update_deploy_config(vmid, deploy_type, _seed)
    db_update_node(node_id, status="up")
    # `h` = hostname : le suivi de création par lot s'y accroche par comparaison EXACTE
    # (cf. `_lastAlertFor` dans static/scripts.js). Ne pas renommer ce paramètre sans migrer le JS.
    db_add_alert("alert.deploy.compute.cree", "info", vmid=vmid, node_id=node_id, kind="deploy",
                 params={"h": hostname, "vmid": vmid, "n": node["name"], "t": deploy_type})
    return vmid


# ─── Certificat mTLS du conteneur ────────────────────────────────────
# Le trio PEM est produit par le CONTRÔLEUR à chaque `docker run` et injecté dans la spec ; la clé
# privée n'est JAMAIS persistée sur le nœud (hygiène délibérée : elle vit en tmpfs, /run/bobi-tls,
# et meurt avec le nœud). Corollaire ASSUMÉ : un reboot de nœud efface le matériel → il faut le
# RE-PROVISIONNER (cf. app/node_recovery.py), pas le persister.
_CERT_TENTATIVES = 3
_CERT_BACKOFF_S = 1.0


class CertConteneurIndisponible(RuntimeError):
    """La CA interne est installée mais on n'a PAS pu émettre le cert du conteneur. Bloquant :
    créer le conteneur quand même donnerait un agent en clair, donc injoignable."""


def _ca_dir_hint():
    from . import ca as _ca
    try:
        return f"le matériel de CA dans {_ca.paths()['ca_cert']} (droits, disque, HA)"
    except Exception:
        return "le matériel de CA (config.TLS_DIR)"


def _cert_mtls_conteneur(vmid):
    """Trio PEM {cert,key,ca} pour ce conteneur, ou None si la CA interne n'est PAS installée
    (flotte historique en http — rétro-compat explicite, pas un échec).
    Lève CertConteneurIndisponible si la CA est là mais l'émission échoue après N tentatives."""
    from . import ca as _ca
    try:
        dispo = _ca.ca_available()
    except Exception as e:
        raise CertConteneurIndisponible(f"CA interne illisible : {e}") from e
    if not dispo:
        return None
    derniere = None
    for i in range(_CERT_TENTATIVES):
        try:
            cert_pem, key_pem = _ca.generate_leaf(
                common_name=f"mxl{vmid}", node_id=None, uri=f"bobi://container/{vmid}")
            return {"cert": cert_pem.decode(), "key": key_pem.decode(),
                    "ca": _ca.ca_cert_pem().decode()}
        except Exception as e:
            derniere = e
            log.warning("compute %s : émission cert mTLS ratée (essai %d/%d) : %s",
                        vmid, i + 1, _CERT_TENTATIVES, e)
            if i + 1 < _CERT_TENTATIVES:
                time.sleep(_CERT_BACKOFF_S)
    raise CertConteneurIndisponible(str(derniere))


def _env_identite_client():
    """Échappatoire d'exploitation pour la vérification d'identité du client mTLS faite par l'agent
    conteneur (script_templates/agent.py) : il n'accepte que le CN du contrôleur. Si une
    installation a un CN différent (CA reprise, cert regénéré à la main), un déploiement d'image
    la rendrait injoignable — d'où DEUX réglages, hors DEFAULTS (usage exceptionnel) :
      · `agent_tls_client_cn`        → CN attendu (défaut agent : bobi-controller) ;
      · `agent_tls_verify_client_cn` → « 0 » pour DÉSACTIVER la vérification (retour au
                                        comportement historique : signé-par-la-CA suffit).
    Rien n'est injecté tant que les réglages sont aux valeurs par défaut : la spec (donc la
    signature, donc la recréation des conteneurs) ne bouge pas pour la flotte."""
    from . import settings as _st
    env = {}
    cn = (_st.get("agent_tls_client_cn", "") or "").strip()
    if cn:
        env["MXL_TLS_CLIENT_CN"] = cn
    verif = _st.get("agent_tls_verify_client_cn", None)
    if verif is not None and str(verif).strip().lower() in ("0", "false", "off", "no"):
        env["MXL_TLS_VERIFY_CLIENT_CN"] = "0"
    return env


def _signature_spec(spec):
    """Signature stable de la spec `docker run` (dict sérialisable). Deux specs identiques →
    même signature → le conteneur en marche est DÉJÀ celui qu'on veut, inutile de le recréer.
    Le CONTENU du certificat mTLS (`tls`) est EXCLU : il est régénéré à chaque appel (donc
    différent à chaque fois) alors qu'il ne change pas ce qu'EST le conteneur — l'inclure
    rendrait la signature toujours différente et remettrait la recréation systématique. En
    revanche sa PRÉSENCE est signée : l'activation de la CA interne (http → mTLS) doit bien
    provoquer la recréation du conteneur (c'est ainsi que la flotte bascule en HTTPS)."""
    net = {k: v for k, v in (spec or {}).items() if k != "tls"}
    net["_tls"] = bool((spec or {}).get("tls"))
    return hashlib.sha1(
        json.dumps(net, sort_keys=True, default=str).encode()).hexdigest()


def _conteneur_deja_conforme(vmid, sig, force):
    """True si le conteneur tourne DÉJÀ avec exactement cette spec → ne pas le recréer.
    `force=True` (recréation explicitement voulue : changement de bind média, réparation d'un
    conteneur incohérent) court-circuite la garde."""
    if force or not sig:
        return False
    c = db_get_container(vmid) or {}
    if (c.get("runtime_spec_sig") or "") != sig:
        return False          # spec changée (image, cpuset, mounts, IP…) → recréation légitime
    return status_compute(vmid) == "running"


def _read_container_ip(host, name, network):
    """IP attribuée par l'IPAM macvlan, lue via `docker inspect`. '' si indisponible.
    Le nom de réseau doit être un LITTÉRAL CHAÎNE Go (entre guillemets) dans `index` — sans quoi
    le template l'interprète comme une fonction (« function ... not defined ») et ne renvoie rien."""
    fmt = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
    rc, out, _ = ssh_run(
        host, f"docker inspect -f {shlex.quote(fmt)} {shlex.quote(name)} 2>/dev/null",
        timeout=10)
    ip = (out or "").strip()
    return ip if (rc == 0 and ip and ip != "<no value>") else ""


def _diagnose_container(host, name):
    """Diagnostic court quand le conteneur ne donne pas d'IP : état + code de sortie + fin des logs.
    Aide à distinguer crash au démarrage (image/agent) d'un échec d'IPAM macvlan."""
    rc, out, _ = ssh_run(
        host, f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' {shlex.quote(name)} 2>/dev/null",
        timeout=10)
    state = (out or "").strip() or "introuvable"
    rc, logs, _ = ssh_run(host, f"docker logs --tail 6 {shlex.quote(name)} 2>&1 | tail -6", timeout=10)
    logs = " | ".join(l.strip() for l in (logs or "").splitlines() if l.strip())[:300]
    return f"état={state}" + (f" · logs: {logs}" if logs else "")


_REF_PIXELS = 1920 * 1080   # référence « 1080p » pour le dimensionnement pondéré


def pyramide_input_load(params):
    """(count, load_1080p) des sources CÂBLÉES d'un conteneur (input_0..input_{n_inputs-1} non vides).
    `load` = Σ(w*h)/(1920*1080) — pondération PAR RÉSOLUTION : un worker sur 4K pèse ~4× un 1080p.
    Dimensions résolues via `monitor._shm_fmt` (lecture DB du format du PRODUCTEUR — jamais de
    devinette SHM). Fallback poids 1.0 si le format est introuvable (producteur pas encore en DB).
    Compte tous les input_i quel que soit le type amont (2110, player, mélangeur…)."""
    from .monitor import _shm_fmt   # import paresseux : monitor n'importe pas docker_compute (pas de cycle)
    p = params or {}
    try:
        n = int(p.get("n_inputs") or 8)
    except (TypeError, ValueError):
        n = 8
    count = 0
    load = 0.0
    for i in range(n):
        src = p.get(cle_input(i))
        if not src:
            continue
        count += 1
        try:
            f = _shm_fmt(src)
        except Exception:
            f = None
        if f and f.get("w") and f.get("h"):
            load += (int(f["w"]) * int(f["h"])) / _REF_PIXELS
        else:
            load += 1.0
    return count, load


def deploy_compute(vmid, params=None, deploy_type=None, force=False):
    """Garantit un conteneur compute up (docker run de l'image agent générique) + son IP.
    NE rend PAS le script ici : `deploy.deployer_script` poursuit sur le chemin agent standard.
    Retourne True si le conteneur tourne et que l'agent :8081 répond.

    IDEMPOTENT (cf. en-tête du module) : si le conteneur tourne DÉJÀ avec exactement la spec
    demandée, on ne le recrée PAS (pas de `rm -f`/`run`) — la sortie live et le script déployé
    sont préservés. `force=True` impose la recréation (bind média changé, réparation).

    `deploy_type` → profil de ressources du manifeste (resources.cores/memory/pin) appliqué au
    `docker run` : --cpus (quota CPU, même rôle que les cœurs LXC), --memory, et --cpuset-cpus si
    `pin` ET le nœud expose un pool de cœurs compute (node.compute_cpuset). Cohérent avec le préréglage
    LXC (cf. routes création) : un streamer demande 4 cœurs des deux côtés."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    if not node:
        db_add_alert("alert.deploy.compute.deploy_noeud_introuvable", "error", vmid=vmid,
                     kind="deploy", params={"vmid": vmid})
        return False
    from . import plugins, core_pool, gpu_pool, node_driver
    from . import deploy as _deploy      # import tardif : deploy importe docker_compute (circulaire)
    # Image selon la variante déclarée par le plugin : 'media' (bobi-media, GStreamer+ffmpeg) →
    # node.media_image ; sinon 'compute' (bobi-compute) → node.compute_image.
    variant = plugins.image_variant(deploy_type) if deploy_type else "compute"
    image = node.get("media_image") if variant == "media" else node.get("compute_image")
    # Cas spécial passerelle WebRTC : image dédiée bobi-webrtc (MediaMTX pré-baké). Ce n'est pas un
    # plugin du registre (donc pas de variante) → image lue du setting `webrtc_image` (défaut
    # bobi-webrtc:0.1), avec repli sur node.webrtc_image si la colonne existe.
    if deploy_type == "webrtc_gateway":
        from . import settings as _st
        image = node.get("webrtc_image") or _st.get("webrtc_image") or "bobi-webrtc:0.1"
    # GPU : un plugin GPU-capable (manifest.resources.gpu) sur un nœud GPU-capable (gpu_capable +
    # compute_gpu_image buildée) prend l'image GPU + un sélecteur --gpus alloué (round-robin).
    # Sinon repli TRANSPARENT sur l'image compute CPU : le script.py auto-détecte l'absence de cupy
    # → numpy (aucune erreur). gpu_sel propagé aux DEUX chemins (spec agent + ssh legacy) ci-dessous.
    gpu_sel = None
    if deploy_type and node.get("gpu_capable") and _veut_gpu_instance(deploy_type, c, params):
        if variant == "media":
            # ★ Un plugin MÉDIA porte déjà ses encodeurs (ffmpeg/NVENC est dans bobi-media) : on
            # garde SON image et on n'alloue que la carte. Basculer sur compute_gpu_image le
            # priverait de ffmpeg — ce chemin visait les plugins compute (cupy), pas ceux-ci.
            gpu_sel = gpu_pool.allocate_gpu(node["id"], vmid)
        elif node.get("compute_gpu_image"):
            image = node.get("compute_gpu_image")
            gpu_sel = gpu_pool.allocate_gpu(node["id"], vmid)   # ex. "device=0"
        if not gpu_sel:
            # Le conteneur PARTIRA sans carte. Sans cette alerte, un `encoder: nvenc` refusé faute
            # de GPU libre se manifesterait seulement par un ffmpeg qui échoue en boucle, avec un
            # « Invalid argument » qui ne nomme rien. On dit ici ce que le déploiement a fait.
            db_add_alert("alert.deploy.compute.gpu_indisponible", "warning", vmid=vmid,
                         node_id=node.get("id"), kind="resource",
                         params={"vmid": vmid, "t": deploy_type, "n": node.get("name")})
    # ── EXIGENCE DE VERSION D'IMAGE ────────────────────────────────────────────────────────
    # Deux trains de version indépendants : le script d'un plugin est POUSSÉ (rapide, par
    # conteneur), tandis que `bobimxl.py` et les noyaux C vivent dans l'IMAGE (lente, par nœud,
    # à l'échelle du parc). Un plugin récent peut donc atterrir sur un nœud en retard, et
    # découvrir à l'exécution que la fonction qu'il appelle n'existe pas — boucle de
    # redémarrage, dont la cause n'apparaît nulle part.
    # Un plugin déclare donc `requires.image_min` dans son manifeste, et on REFUSE ici plutôt
    # que de laisser partir un conteneur qui ne peut pas fonctionner. Refuser est brutal, mais
    # l'alerte NOMME la version attendue et celle du nœud : c'est réparable en une lecture.
    #
    # ⚠ PORTÉE EXACTE, et elle est plus étroite que ce qu'on aimerait : ce contrôle est à la
    # CRÉATION du conteneur, seul moment où l'image du nœud EST celle du conteneur. Un
    # conteneur créé sur une image ancienne, puis redéployé après que le nœud a été promu,
    # passerait ce test tout en tournant sur l'ancienne image — `containers` ne mémorise pas
    # l'image posée au `docker run`. Ce cas-là n'est PAS couvert ici : il l'est côté script,
    # par un chargement défensif (`getattr(bobimxl, ...)`) et par un repli dont l'état publie
    # qu'il est dégradé. Le vrai correctif serait de stocker l'image à la création — noté, pas
    # fait.
    _req = (plugins.get(deploy_type) or {}).get("requires") if deploy_type else None
    _min = str((_req or {}).get("image_min") or "").strip()
    def _cle(tag):
        """« bobi-compute:0.30 » → (0, 30). None dès qu'un segment n'est PAS numérique :
        sur un tag comme « 0.24-fix », on ne compare pas du tout plutôt que d'inventer un
        ordre sur un tuple tronqué — un refus fondé sur une comparaison bancale serait pire
        que l'absence de contrôle."""
        v = tag.rsplit(":", 1)[-1]
        parts = v.split(".")
        if not all(p.isdigit() for p in parts):
            return None
        return tuple(int(p) for p in parts)

    if _min and image:
        _a, _b = _cle(image), _cle(_min)
        if _a is None or _b is None:
            log.warning("deploy %s: version non comparable (nœud %s, exigence %s) — "
                        "contrôle d'image IGNORÉ", vmid, image, _min)
        elif _a < _b:
            db_add_alert("alert.deploy.compute.image_trop_ancienne", "error", vmid=vmid,
                         node_id=node.get("id"), kind="deploy",
                         params={"vmid": vmid, "t": deploy_type, "n": node.get("name"),
                                 "img": image, "min": _min})
            log.error("deploy %s: %s exige %s, le nœud %s est en %s — déploiement refusé",
                      vmid, deploy_type, _min, node.get("name"), image)
            return False

    network = node.get("docker_network")
    mxl = node.get("mxl_mount") or "/dev/shm"
    if not (image and network):
        _quoi = "média (media_image)" if variant == "media" else "compute (compute_image)"
        db_add_alert("alert.deploy.compute.sans_image_ou_reseau", "error", vmid=vmid,
                     node_id=node.get("id"), kind="deploy",
                     params={"vmid": vmid, "n": node["name"], "quoi": _quoi})
        return False

    name = c.get("docker_name") or _name(vmid)
    # B2-2 : IPAM CENTRALISÉ du plan conteneurs (orchestrateur) si requis (topologie séparée OU
    # multi-nœud) → on alloue l'IP ici et on la passe en `docker run --ip`. Sinon (simple + mono-nœud)
    # : IPAM Docker (comportement actuel, l'IP est relue après le run).
    from . import allocations
    alloc_ip = None
    if network != "host" and allocations.centralized_ipam(node["id"]):
        alloc_ip = allocations.allocate_container_ip(vmid, node_id=node["id"])
        if not alloc_ip:
            db_update_status(vmid, "stopped")
            return False
    # Profil de ressources par type (cohérent avec le préréglage cœurs LXC) : --cpus = quota CPU
    # (≈ cœurs), --memory, et --cpuset-cpus si pin + pool de cœurs déclaré sur le nœud. La priorité
    # 'batch' (transcoder, fichier non temps réel) → --cpu-shares bas + pas de pin → cède aux
    # plugins temps réel (player/recorder, pinnés).
    res = (plugins.get(deploy_type) or {}).get("resources") or {} if deploy_type else {}
    # Profil de ressources : on construit À LA FOIS la chaîne d'options legacy (ssh_run) ET le
    # dict structuré (agent-nœud), à partir de la même logique → pas de divergence.
    resources = {}
    is_batch = str(res.get("priority") or "").lower() == "batch"
    want = 0
    if res.get("pin") and res.get("cores") and not is_batch:
        # Vrai pinning : cœurs DÉDIÉS non chevauchants demandés au pool du nœud (idempotent).
        base = int(res["cores"])
        # Dimensionnement DYNAMIQUE optionnel : `cores_per_1080p_input` (override par conteneur dans
        # params, sinon défaut manifeste) → cœurs = clamp(plancher + ratio*charge, plancher, pool libre).
        # `base` reste un PLANCHER. Absent ⇒ per=0 ⇒ comportement legacy (cœurs fixes).
        _p = params if params is not None else _params_of(c)
        try:
            per = float(_p.get("cores_per_1080p_input") or res.get("cores_per_1080p_input") or 0)
        except (TypeError, ValueError):
            per = 0.0
        want = base
        if per > 0:
            _, load = pyramide_input_load(_p)
            mine = core_pool.allocated_for(node["id"], vmid)
            free = core_pool.cores_status(node["id"])["free"]
            want = min(max(base, base + math.ceil(per * load)), max(base, free + mine))
            # Le cpuset Docker n'est PAS modifiable à chaud → un resize impose release+realloc (on
            # n'arrive ici, avec un resize, que lors d'un vrai (re)déploiement qui recréera le conteneur).
            if mine and mine != want:
                core_pool.release_cores(vmid)
    # CAUSE RACINE corrigée (2026-07-13, nœud 30) : `effective_cpuset` ne renvoie JAMAIS "vide" tant
    # que le nœud a un `compute_cpuset` — que le type n'ait AUCUN profil `resources` (mixer, avsync,
    # color_corrector, delay, split, stills, udc, v210_bridge, probe_2110, webrtc_gateway — pas de
    # plugin.json/manifest « resources » du tout → `res == {}` ci-dessus, `want` reste 0), qu'il
    # demande un pin mais que le pool DÉDIÉ soit plein (`want>0` mais `allocate_cores` échoue), ou
    # qu'il soit `batch` (want=0 volontaire) : dans TOUS les cas on retombe sur le POOL PARTAGÉ du
    # nœud (non-exclusif, mais TOUJOURS hors des lcores busy-poll du moteur 2110 — jamais un
    # conteneur sans cpuset flottant librement, cf. mémoire mtl-tx-frozen-uint64-race / node30-cpu-
    # contention-canary). `dedicated=True` seulement si `n` cœurs EXCLUSIFS ont été obtenus.
    # `effective_cpuset` ALERTE désormais (table alerts, propriétaires nommés) quand il installe le
    # conteneur par-dessus un pool déjà entièrement dédié, et LÈVE `PoolSature` si l'exploitant a
    # activé le refus (`compute_refuse_oversubscribed`) — un mur 50 fps qui n'a nulle part où tourner
    # ne rend service à personne, mais le REFUS reste opt-in : la boucle de surveillance recrée les
    # conteneurs, et refuser par défaut transformerait un pool saturé en panne totale du nœud.
    # NUMA : sur un bi-socket, le GPU appartient à UN socket. Un conteneur GPU épinglé de l'autre
    # côté paie chaque lecture de shm amont et chaque upload H2D au prix du lien inter-socket —
    # mesuré en prod (Horace, mur `multiview-vision` sur nœud NUMA 0 / T4 sur le nœud 1) : segment
    # `inputs` du compositing à 21,9 ms au lieu de 11,1 ms, 25-37 fps au lieu de 50. `prefer_numa`
    # est None quand il n'y a pas de GPU (ou pas de NUMA lisible) : `allocate_cores` garde alors sa
    # règle générale (ne jamais mettre un conteneur à cheval sur deux sockets).
    # Sans GPU, la préférence n'est pas nulle pour autant : un type qui lit les flux RX 2110 en
    # pleine résolution doit viser le socket de la CARTE MÉDIA (c'est elle qui DMA les trames, donc
    # les shm vivent là). Liste FERMÉE (`PREFERE_SOCKET_MEDIA`) et non heuristique : préférer la
    # carte pour tout le compute entasserait le parc sur un seul socket.
    prefer_numa = core_pool.numa_of_gpu(node["id"], gpu_sel)
    # `deploy_type` est un argument OPTIONNEL de cette fonction : le type qui fait foi ici est celui
    # de la base quand l'appelant ne l'a pas passé (sinon la préférence sauterait en silence).
    _t_eff = deploy_type or _type_of(c)
    if prefer_numa is None and _t_eff in core_pool.PREFERE_SOCKET_MEDIA:
        prefer_numa = core_pool.numa_of_media_nic(node["id"])
    try:
        cpuset, dedicated = core_pool.effective_cpuset(node["id"], vmid, want,
                                                       prefer_numa=prefer_numa)
    except core_pool.PoolSature as e:
        db_add_alert("alert.deploy.compute.pool_sature", "error", vmid=vmid,
                     node_id=node.get("id"), kind="deploy",
                     params={"vmid": vmid, "n": node["name"], "e": str(e)})
        db_update_status(vmid, "stopped")
        return False
    if is_batch:
        resources["cpu_shares"] = 128                        # poids CPU faible (défaut 1024)
        if res.get("cores"):
            resources["cpus"] = int(res["cores"])            # plafond optionnel
    elif res.get("cores") and not dedicated:
        # Pas de pinning dédié (non demandé, ou pool dédié plein) → quota --cpus EN PLUS du cpuset
        # partagé ci-dessous (le cpuset borne la PLACE, --cpus borne la PART de CPU dans cette place).
        resources["cpus"] = int(res["cores"])
    if cpuset:
        resources["cpuset"] = cpuset
    if res.get("memory"):
        resources["memory_mb"] = int(res["memory"])
    # `containers.pinned_cores` ne reflète QUE le pinning DÉDIÉ/exclusif (celui que `cpu_map`, cf.
    # routes/__init__.py, utilise pour détecter des CONFLITS de cœurs entre containers) — le repli
    # « pool partagé » est INTENTIONNELLEMENT non-exclusif (plusieurs containers dessus n'est pas un
    # conflit) et ne doit donc pas s'y confondre. On efface une valeur pinned_cores devenue stale
    # (ex. l'allocation dédiée précédente a été libérée par un resize) plutôt que de la laisser mentir.
    try:
        db_update_resources(vmid, pinned_cores=(cpuset if dedicated else ""))
    except Exception as _e:
        log.warning("compute %s: maj pinned_cores échouée: %s", vmid, _e)

    # Volume média : les plugins qui le déclarent (media_volume) reçoivent un bind du stockage
    # LOCAL du nœud → /mnt/media, scopé au sous-dossier du projet si le container y est rattaché.
    mounts = [{"host": mxl, "container": "/dev/shm"}]
    media_host_dir = None
    if deploy_type and plugins.wants_media_volume(deploy_type):
        media_root = node.get("media_mount") or "/srv/mxl-media"
        media_host_dir = media_root
        proj = db_get_project(c.get("project_id")) if c.get("project_id") else None
        if proj and proj.get("media_path"):
            media_host_dir = media_root.rstrip("/") + "/" + os.path.basename(proj["media_path"].rstrip("/"))
        mounts.append({"host": media_host_dir, "container": "/mnt/media"})

    # Volume d'état : dossier persistant par container (hôte → /var/lib/bobi). Le rootfs est
    # recréé à chaque déploiement ET au boot du nœud ; ce qui doit survivre (journaux
    # d'exploitation) s'écrit ici. Scopé au hostname : deux instances ne se marchent pas dessus.
    state_host_dir = None
    if deploy_type and plugins.wants_state_volume(deploy_type):
        state_host_dir = "/var/lib/bobi/state/" + (c.get("hostname") or f"vmid{vmid}")
        mounts.append({"host": state_host_dir, "container": "/var/lib/bobi"})

    # ─── Chemin AGENT (node.agent_url) vs LEGACY (ssh_run) ───────────────────────
    if node_driver.has_agent(node):
        spec = {
            "name": name, "image": image, "network": network,
            "privileged": False, "autoremove": False, "restart_policy": "unless-stopped",
            "mounts": mounts, "resources": resources,
            # Journal DURABLE (cf. app/journal.py) : EXTENSION DE CONTRAT AGENT — `log` =
            # {driver, opts} → `docker run --log-driver … --log-opt …`. Un agent-nœud ANTÉRIEUR
            # à 0.17.0 ignore la clé : le conteneur retombe sur le pilote par défaut du daemon
            # (json-file, non durable) — dégradé mais pas cassé, et la route de journal l'annonce
            # explicitement (`source: "docker"`). Mettre les agents à jour (Réglages → Nœuds).
            "log": _journal.log_opts(name),
        }
        if alloc_ip:
            spec["ip"] = alloc_ip   # B2-2 : IPAM centralisé → l'agent ajoute --ip
        if gpu_sel:
            # EXTENSION DE CONTRAT AGENT : l'agent-nœud doit traduire spec["gpus"] en `docker run
            # --gpus "<sel>"` (cf. NODE_AGENT.md). Un agent qui l'ignore → conteneur sans GPU →
            # cupy absent → repli numpy (dégradation silencieuse, pas de crash).
            spec["gpus"] = gpu_sel
        # mTLS du plan de contrôle (chantier feat/mtls) : quand la CA interne est dispo, le CONTRÔLEUR
        # génère un cert conteneur éphémère (signé CA, EKU serveur+client) et l'injecte au run. L'agent-
        # nœud écrit ces PEM dans le conteneur sous /etc/bobi-tls/{cert.pem,key.pem,ca.pem} (bind-mount) →
        # script_templates/agent.py sert :8081 en HTTPS. Le contrôleur (deploy/metrics) valide contre la CA
        # (hostname off). CA absente → clé omise → agent en http (rétro-compat).
        #
        # ÉCHEC D'ÉMISSION = ARRÊT. Auparavant l'exception était avalée en `log.warning` : on créait
        # sciemment un conteneur STRUCTURELLEMENT INJOIGNABLE (agent en clair, contrôleur en https),
        # qui tourne et consomme des ressources sans jamais répondre — l'anti-patron n°1 du projet.
        try:
            _tls = _cert_mtls_conteneur(vmid)
        except CertConteneurIndisponible as _e:
            db_add_alert(
                "alert.deploy.compute.cert_mtls_echoue", "error",
                vmid=vmid, node_id=node["id"], kind="deploy",
                params={"vmid": vmid, "n": node["name"], "tentatives": _CERT_TENTATIVES,
                        "e": str(_e), "hint": _ca_dir_hint()})
            db_update_status(vmid, "stopped")
            db_update_spec_sig(vmid, None)   # état inconnu → recréation franche au prochain coup
            return False
        if _tls:
            spec["tls"] = _tls
            _env_tls = _env_identite_client()
            if _env_tls:
                spec.setdefault("env", {}).update(_env_tls)
        # Auth de l'agent conteneur (:8081) — SECOND FACTEUR, indépendant du mTLS ci-dessus (qui ne
        # prouve que « signé par la CA » + CN du contrôleur). L'agent (script_templates/agent.py)
        # n'EXIGE l'en-tête X-MXL-Agent-Token que si MXL_AGENT_TOKEN est posé dans son environnement :
        # c'est ICI (et dans le chemin ssh plus bas, et dans docker_driver) que le contrôle devient
        # effectif. Sans cette ligne, le token existait des deux côtés du code et n'était jamais
        # appliqué nulle part. Le token entre dans la SIGNATURE de spec → un conteneur créé sans
        # token est recréé (avec) au prochain déploiement : c'est la bascule au fil de l'eau.
        _tok = _deploy.token_a_injecter(vmid)
        if _tok:
            spec.setdefault("env", {})["MXL_AGENT_TOKEN"] = _tok
        # ★ Lot de synchronisation RDMA (`maxSyncBatchSizeHint`), lu par `bobimxl._flow_options()`.
        # UN SEUL point d'injection pour toute la flotte : c'est une option de FLUX, posée à la
        # création, donc invisible pour les plugins — inutile de la propager plugin par plugin.
        # Mesuré le 2026-08-09 : au défaut du SDK (= totalSlices) l'initiateur RDMA attend la trame
        # ENTIÈRE avant de transférer — la 1ʳᵉ bande n'est lisible sur la réplique qu'à 22,63 ms.
        # À 2 tranches : 0,54 ms. Débit et paquets identiques, seul le CPU des initiateurs monte
        # (15 % → 44 % cumulés sur 12 conteneurs). Vide = comportement historique.
        # ⚠ N'agit que sur les flux CRÉÉS ENSUITE : un producteur qui se rattache garde son lot.
        try:
            from . import settings as _st_sb
            _sb = str(_st_sb.get("mxl_sync_batch") or "").strip()
        except Exception:
            _sb = ""
        if _sb:
            spec.setdefault("env", {})["MXL_SYNC_BATCH"] = _sb
        # ★ Capacités du pilote NVIDIA. `--gpus` seul n'accorde que `compute,utility` : le toolkit
        # n'injecte PAS `libnvidia-encode.so`, et ffmpeg échoue alors sur « Invalid argument (-22) »
        # — un message qui ne dit rien de la cause (vérifié au banc, ~20 min pour le comprendre).
        # `video` est ce qui rend NVENC/NVDEC utilisables ; on garde compute/utility pour ne pas
        # priver les plugins cupy qui partagent ce chemin.
        if gpu_sel:
            spec.setdefault("env", {})["NVIDIA_DRIVER_CAPABILITIES"] = "video,compute,utility"
        sig = _signature_spec(spec)
        if _conteneur_deja_conforme(vmid, sig, force):
            # Cas NOMINAL d'un simple (re)push de script : le conteneur tourne déjà avec cette
            # spec exacte. Le recréer viderait son rootfs éphémère (script perdu) et couperait
            # la sortie — c'est ce qui entretenait la boucle de recréation. On ne touche à rien.
            recree = False
            # ── CONTRÔLE D'IMAGE SUR CE QUI TOURNE VRAIMENT ──────────────────────────
            # Le conteneur n'est PAS recréé : il gardera l'image posée à son `docker run`,
            # qui peut être antérieure à celle du nœud si celui-ci a été promu depuis. Le
            # contrôle d'entrée, lui, a jugé l'image du NŒUD — c'est-à-dire la mauvaise.
            # Sans ce second contrôle, un plugin exigeant 0.32 se déployait sur un conteneur
            # tournant en 0.29, et la panne n'apparaissait qu'à l'exécution.
            _img_reelle = (c or {}).get("image")
            if _min and _img_reelle:
                _a2, _b2 = _cle(_img_reelle), _cle(_min)
                if _a2 is not None and _b2 is not None and _a2 < _b2:
                    db_add_alert("alert.deploy.compute.image_conteneur_ancienne", "error",
                                 vmid=vmid, node_id=node.get("id"), kind="deploy",
                                 params={"vmid": vmid, "t": deploy_type,
                                         "img": _img_reelle, "min": _min,
                                         "noeud": image})
                    log.error("deploy %s: le conteneur tourne sur %s, %s exige %s — "
                              "recréer le conteneur (le nœud est en %s)",
                              vmid, _img_reelle, deploy_type, _min, image)
                    return False
            ip = c.get("docker_ip") or alloc_ip or ""
        else:
            recree = True
            ok, r = node_driver.run_container(node, spec)   # docker auto-crée le dossier de bind média
            if not ok:
                db_add_alert("alert.deploy.compute.agent_echoue", "error", vmid=vmid,
                             node_id=node.get("id"), kind="deploy",
                             params={"vmid": vmid, "e": str(r)})
                db_update_status(vmid, "stopped")
                db_update_spec_sig(vmid, None)   # conteneur dans un état inconnu → forcer la recréation au prochain coup
                return False
            ip = (r or {}).get("ip") or alloc_ip or ""
            if not ip and network != "host":
                db_add_alert("alert.deploy.compute.sans_ip_macvlan", "error", vmid=vmid,
                             node_id=node.get("id"), kind="net",
                             params={"vmid": vmid, "network": network})
                db_update_spec_sig(vmid, None)
                return False
    else:
        # AGENT-NŒUD EXIGÉ (décision 2026-07-26). L'ancien chemin root-SSH construisait un
        # `docker run` à la main, en parallèle de la spec envoyée à l'agent. Deux chemins pour la
        # même chose, dont un régulièrement OUBLIÉ : il n'a jamais reçu les options de journal
        # (conteneur en json-file non durable), ni les variables mTLS (agent en HTTP clair alors
        # que le contrôleur parle HTTPS → conteneur injoignable en silence, la panne même qu'on
        # vient de corriger). Le maintenir revenait à entretenir une voie qui produit des
        # conteneurs dégradés SANS que rien ne le signale.
        #
        # Un nœud sans agent ne peut donc plus héberger de conteneur compute. C'est un REFUS
        # explicite et non un repli discret : mieux vaut ne pas créer que créer un conteneur dont
        # on sait qu'il sera à moitié configuré. `ssh_run` (host-ops : prép, vfio, images) garde
        # son repli SSH — il exécute des commandes ponctuelles, pas des conteneurs de production.
        db_add_alert(
            "alert.deploy.compute.sans_agent_noeud", "error", vmid=vmid,
            node_id=node.get("id"), kind="deploy",
            params={"vmid": vmid, "n": node.get("name") or node.get("host")})
        db_update_spec_sig(vmid, None)
        db_update_status(vmid, "stopped")
        return False

    db_update_docker_ip(vmid, ip)

    # Attendre que l'agent :8081 réponde avant de rendre la main (le chemin deploy va POSTer
    # immédiatement /deploy + /start dessus). HTTPS mTLS si la CA est dispo (agent recréé HTTPS-only),
    # sinon http — via le helper partagé de deploy.py (même session/schéma que le POST /deploy suivant).
    # L'en-tête d'auth est OBLIGATOIRE ici : depuis l'injection de MXL_AGENT_TOKEN, un agent sous
    # token répond 401 sans lui → la boucle n'aurait JAMAIS vu 200 et on aurait rendu la main après
    # 10 s en croyant l'agent muet (échec silencieux).
    from .deploy import (agent_session as _agent_session, agent_url as _agent_url,
                         agent_headers as _agent_headers)
    for _ in range(20):
        try:
            r = _agent_session().get(_agent_url(ip, "/status"), timeout=2,
                                     headers=_agent_headers(vmid))
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)

    db_update_status(vmid, "running")
    if recree:
        # Signature du conteneur RÉELLEMENT en marche → les prochains push de script n'auront
        # plus rien à recréer tant que la spec ne bouge pas.
        db_update_spec_sig(vmid, sig)
        # IMAGE RÉELLEMENT POSÉE, enregistrée ICI et pas à la création de la ligne : c'est
        # au `docker run` que le tag est choisi, et le nœud a pu être promu entre-temps.
        # Sans elle, `requires.image_min` ne protégeait que la création — un conteneur créé
        # sur une image ancienne puis redéployé passait le contrôle en tournant sur
        # l'ancienne. C'est le cas qui a coûté deux recréations le 2026-08-25.
        db_update_container_image(vmid, image)
        db_add_alert("alert.deploy.compute.up", "info", vmid=vmid, node_id=node.get("id"),
                     kind="deploy", params={"h": name, "vmid": vmid, "n": node["name"], "ip": ip})
    else:
        log.info("compute %s : conteneur déjà conforme (spec inchangée) — pas de recréation", vmid)
    return True


def start_compute(vmid):
    """Le conteneur n'est pas --rm : `docker start` suffit (l'agent + le dernier script repartent).
    S'il a disparu (rm), on redéploie depuis le deploy_config."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return False
    from .database import db_set_desired_state
    db_set_desired_state(vmid, "running")   # intention opérateur, même si la tentative échoue
    st = status_compute(vmid)
    if st == "absent":
        # conteneur disparu → re-run complet puis re-push du script via le chemin standard
        import json
        try:
            dc = json.loads(c.get("deploy_config") or "{}")
        except Exception:
            dc = {}
        params = (dc.get("params") or {}) if isinstance(dc, dict) else {}
        from .deploy import deployer_script
        return deployer_script(vmid, dc.get("type") or _type_of(c), params)
    rc, out, err = ssh_run(node["host"], f"docker start {shlex.quote(name)} 2>&1", timeout=30)
    db_update_status(vmid, "running" if rc == 0 else "stopped")
    db_add_alert("alert.deploy.compute.demarre", "info" if rc == 0 else "warning", vmid=vmid,
                 node_id=node.get("id"), kind="deploy", params={"h": name, "vmid": vmid})
    return rc == 0


def stop_compute(vmid):
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return (False, "nœud introuvable")
    from .database import db_set_desired_state
    db_set_desired_state(vmid, "stopped")   # arrêt VOULU → l'auto-recovery ne le relèvera pas
    rc, out, err = ssh_run(node["host"], f"docker stop {shlex.quote(name)} 2>&1", timeout=30)
    db_update_status(vmid, "stopped")
    db_add_alert("alert.deploy.compute.arrete", "info" if rc == 0 else "warning", vmid=vmid,
                 node_id=node.get("id"), kind="deploy", params={"h": name, "vmid": vmid})
    return (rc == 0, out or err)


def status_compute(vmid):
    """running | exited | absent (docker inspect)."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return "absent"
    rc, out, _ = ssh_run(
        node["host"],
        f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(name)} 2>/dev/null",
        timeout=10)
    st = (out or "").strip()
    return st if (rc == 0 and st) else "absent"


def image_courante_compute(vmid):
    """Image sur laquelle le conteneur TOURNE réellement (≠ celle que le nœud prescrit)."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return ""
    rc, out, _ = ssh_run(node["host"],
                         f"docker inspect -f '{{{{.Config.Image}}}}' {shlex.quote(name)} 2>/dev/null",
                         timeout=20)
    return (out or "").strip() if rc == 0 else ""


def recreer_compute(vmid, progress=None):
    """Recrée le conteneur SUR L'IMAGE COURANTE DU NŒUD, en gardant sa ligne en base.

    RAISON D'ÊTRE (2026-08-19) : un conteneur compute ne change JAMAIS d'image tout seul.
    `start_compute` fait un `docker start` — qui repart sur l'image d'origine — et le seul chemin
    qui relit `nodes.<image>` est la branche « conteneur absent → `deployer_script` ». Il n'existait
    aucune commande pour l'emprunter : après avoir buildé de nouvelles images à Horace, il a fallu
    faire `docker rm` à la main sur chaque mur, chaque shard et la pyramide. Un geste manuel répété
    sur une installation à l'antenne finit par sauter un conteneur ou en détruire un autre.

    Ce n'est PAS `destroy_compute` : la ligne en base, l'`instance_uuid`, l'emplacement, le
    `deploy_config` et les tokens SURVIVENT. Seul le conteneur Docker est refait.

    ⚠ COUPURE : le conteneur s'arrête et se recrée (quelques secondes, plus le redéploiement du
    script). Sur un mur ou un moteur à l'antenne, c'est une fenêtre à choisir, pas un geste anodin.
    ⚠ Un MOTEUR (`2110_io`) n'a pas besoin de ceci : il tourne en `--rm` et `redemarrer_container`
    le re-`docker run` déjà depuis son `deploy_config`, donc il adopte la nouvelle image tout seul.
    """
    _p = progress or (lambda m: None)
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return (False, "nœud introuvable")
    avant = image_courante_compute(vmid)
    _p(f"arrêt de {name}…")
    # Arrêt GRACIEUX d'abord (le script a le temps de fermer ses flux MXL et de rendre ses
    # mappings) : un `rm -f` direct laisse des lecteurs accrochés à des générations mortes.
    ssh_run(node["host"], f"docker stop -t 12 {shlex.quote(name)} >/dev/null 2>&1", timeout=45)
    _p(f"retrait du conteneur {name}…")
    ssh_run(node["host"], f"docker rm -f {shlex.quote(name)} >/dev/null 2>&1", timeout=30)
    if status_compute(vmid) != "absent":
        db_add_alert("alert.deploy.compute.retrait_echoue", "error", vmid=vmid,
                     node_id=node.get("id"), kind="deploy", params={"h": name, "vmid": vmid})
        return (False, "conteneur toujours présent après rm")
    _p("redéploiement sur l'image courante…")
    # `start_compute` voit « absent » et emprunte `deployer_script`, qui relit `nodes.<image>`.
    ok = start_compute(vmid)
    apres = image_courante_compute(vmid) if ok else ""
    if ok:
        msg = "%s recréé : %s → %s" % (name, avant or "?", apres or "?")
        db_add_alert("alert.deploy.compute.recree_ok", "info", vmid=vmid, node_id=node.get("id"),
                     kind="deploy", params={"h": name, "avant": avant or "?", "apres": apres or "?"})
    else:
        msg = "%s : recréation ÉCHOUÉE (était sur %s)" % (name, avant or "?")
        db_add_alert("alert.deploy.compute.recree_echec", "error", vmid=vmid, node_id=node.get("id"),
                     kind="deploy", params={"h": name, "avant": avant or "?"})
    return (ok, msg)


def destroy_compute(vmid, progress=None):
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if node:
        ssh_run(node["host"], f"docker rm -f {shlex.quote(name)} >/dev/null 2>&1", timeout=30)
    db_delete_container(vmid)
    db_add_alert("alert.deploy.compute.detruit", "info", vmid=vmid, kind="deploy",
                 params={"h": name, "vmid": vmid})
    return True
