"""Sérialisation par-VMID des opérations de cycle de vie des conteneurs.

CONTEXTE
--------
Les routes Flask retournent immédiatement et dispatchent le travail lifecycle
(deploy / destroy / restart / resize / wire / push_tx_slots) dans des
``threading.Thread`` fire-and-forget. Il n'existait AUCUN verrou par ressource :
deux opérations concurrentes sur le MÊME vmid (ex. DELETE puis deploy_config)
couraient en parallèle sur l'agent-nœud et l'API Docker → ordre indéterminé,
corruption d'état (un deploy pouvait écrire sur un conteneur en cours de
suppression).

Ce module fournit un registre de verrous PAR vmid + deux context managers
(`verrou_vmid`, `verrou_vmids`) à prendre DANS le thread de fond, autour du
corps de l'opération.

SÉMANTIQUE CHOISIE (et justification)
-------------------------------------
- **Un verrou ré-entrant (RLock) par vmid.** Deux opérations sur le même vmid
  sont sérialisées (l'une attend l'autre) ; des opérations sur des vmid
  DIFFÉRENTS restent parallèles (pas de verrou global sur le chemin chaud —
  seul le registre est protégé le temps de la création paresseuse du verrou).
  RLock (et non Lock simple) : si un helper lifecycle en imbrique un autre DANS
  LE MÊME THREAD (même vmid), il n'y a pas d'auto-interblocage.

- **Bloquant, avec timeout.** Pour du lifecycle, sérialiser dans l'ordre
  d'arrivée est plus sûr que rejeter : on ne veut pas « perdre » un destroy ou
  un deploy déjà accepté (côté agent l'op resterait à moitié faite). Le
  garde-fou 409 « redéploiement moteur 2110 » (routes/__init__.py) reste la
  protection MÉTIER contre une action disruptive ; ce verrou est la protection
  GÉNÉRALE contre les races d'exécution — les deux sont complémentaires.

- **Dégradation sur timeout (pas de blocage infini).** Si le verrou n'est pas
  acquis en `TIMEOUT_S` (défaut 120 s — un holder sain finit bien avant), on
  LOG + ALERTE et on exécute quand même le corps (best-effort, comportement
  historique). Justification : à 120 s le détenteur est vraisemblablement
  bloqué/mort ; DROP silencieux d'un destroy/deploy laisserait un état
  incohérent (zombie / conteneur non reconfiguré), ce qui est PIRE que de
  ré-accepter brièvement le risque de race — avec, cette fois, une alerte forte.

- **Pas de nettoyage du registre.** Un vmid supprimé conserve son RLock
  (quelques dizaines/centaines de vmid au plus → coût mémoire négligeable). Ne
  pas sur-ingénier : nettoyer des verrous « non tenus » ré-introduirait une
  fenêtre de course entre le nettoyage et une nouvelle acquisition. On assume
  la fuite bornée.

MULTI-VMID
----------
Une opération qui touche PLUSIEURS vmid (théorique ici : le câblage ne
reconfigure QUE le consommateur `to_vmid`, la source n'étant que lue) doit
prendre les verrous dans un ORDRE TOTAL déterministe pour éviter les deadlocks.
`verrou_vmids(*vmids)` trie et dédoublonne les vmid puis les acquiert par ordre
croissant → deux opérations multi-vmid concurrentes prennent toujours les
verrous dans le même ordre, jamais d'attente croisée.
"""

import logging
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Délai d'acquisition avant dégradation (voir docstring).
TIMEOUT_S = 120.0

# Registre {vmid: RLock} + méta-verrou protégeant sa création paresseuse.
_locks = {}
_registry_lock = threading.Lock()


def _lock_for(vmid):
    """Retourne (en le créant au besoin) le RLock associé à `vmid`.

    Clé normalisée en str : les appelants passent indifféremment int ou str
    (routes vs helpers bas niveau) — un même vmid doit toujours résoudre le
    MÊME verrou, sinon la ré-entrance route → opération est perdue."""
    key = str(vmid)
    with _registry_lock:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _locks[key] = lk
        return lk


def est_verrouille(vmid):
    """True si une opération lifecycle (deploy/destroy/restart) est DÉJÀ en vol sur ce vmid.

    Sonde NON BLOQUANTE, réservée aux mécanismes de RÉPARATION automatique (boucle de
    surveillance) : réparer un conteneur pendant qu'un déploiement le reconstruit, c'est
    empiler deux réparations qui se défont l'une l'autre (une recréation vide le rootfs que
    l'autre vient de garnir). Le réparateur doit simplement PASSER SON TOUR — le déploiement
    en vol est précisément ce qui va rétablir l'état voulu.

    NB : c'est une sonde, pas une réservation — l'appelant ne prend PAS le verrou. Il n'y a
    donc pas de garantie qu'un déploiement ne démarre pas juste après le test ; c'est sans
    conséquence (le pire cas est le comportement historique, une réparation de plus), et ça
    évite d'ajouter un point de blocage dans la boucle de surveillance."""
    lk = _lock_for(vmid)
    if not lk.acquire(blocking=False):
        return True
    lk.release()
    return False


@contextmanager
def verrou_vmid(vmid, timeout=TIMEOUT_S, op=""):
    """Sérialise les opérations lifecycle sur `vmid`.

    Bloquant jusqu'à `timeout` secondes. Sur timeout : log + alerte, puis on
    exécute quand même le corps (best-effort). `op` = libellé pour les logs.
    """
    lk = _lock_for(vmid)
    acquired = lk.acquire(timeout=timeout)
    if not acquired:
        _alerter_timeout(vmid, op)
    try:
        yield acquired
    finally:
        if acquired:
            lk.release()


@contextmanager
def verrou_vmids(*vmids, timeout=TIMEOUT_S, op=""):
    """Sérialise une opération touchant PLUSIEURS vmid.

    Acquiert les verrous par ordre de vmid CROISSANT (ordre total déterministe)
    pour éviter les interblocages. Les vmid falsy (0/None) et doublons sont
    ignorés. Même politique de dégradation sur timeout que `verrou_vmid`.
    """
    # Normalisation str AVANT tri/dédoublonnage : un mélange int/str planterait sorted()
    # et {301, "301"} ne dédoublonnerait pas. L'ordre lexicographique suffit : il est
    # TOTAL et identique pour tous les appelants, c'est tout ce que l'anti-deadlock exige.
    ordered = sorted({str(v) for v in vmids if v})
    acquired = []  # (lock, ok)
    try:
        for v in ordered:
            lk = _lock_for(v)
            ok = lk.acquire(timeout=timeout)
            if not ok:
                _alerter_timeout(v, op)
            acquired.append((lk, ok))
        yield
    finally:
        for lk, ok in reversed(acquired):
            if ok:
                lk.release()


def _alerter_timeout(vmid, op):
    label = ("%s " % op) if op else ""
    log.warning("verrou_vmid timeout: vmid=%s op=%s", vmid, op)
    try:
        from .database import db_add_alert
        db_add_alert("alert.deploy.verrou_timeout", "warning", vmid=vmid, kind="deploy",
                     params={"vmid": vmid, "timeout": TIMEOUT_S, "label": label})
    except Exception:
        pass
