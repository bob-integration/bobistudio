# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Rôle de contrôle (paire HA warm-standby) — B3-2a.

CE contrôleur est soit `active` (il pilote : surveillance des nœuds, services NMOS/Ember+/ATEM/
TSL/Skaarhoj, sampler PTP, backup quotidien) soit `standby` (il boote passif : sert l'UI en
lecture seule, ne démarre aucun pilotage, attend une bascule manuelle). Le rôle est un réglage
(`control_role`, défaut `active`) lu à chaud — le boot (`main.py`) le consulte pour gater le
démarrage pilotant, et `routes.before_request` l'utilise pour refuser les mutations en standby.

B3-2b (réplication d'état) est ici : l'actif pousse un snapshot SQLite cohérent vers le standby,
qui le STAGE (sans toucher sa DB live — l'application = promote, B3-2c, pas encore implémenté).
"""
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import settings
from .config import DB_PATH

log = logging.getLogger(__name__)

ROLES = ("active", "standby")

TOKEN_HEADER = "X-MXL-Update-Token"   # secret partagé = `update_token` (réutilisé du pull/push code)
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Réplica reçu par le standby — stagé sur disque (PAS en DB : la DB est justement ce qu'on remplace
# au promote ; métadonnée façon updater.deploy_info.json).
STAGING_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "ha_staging")
STAGING_DB = os.path.join(STAGING_DIR, "db_replica.db")
STAGING_META = os.path.join(STAGING_DIR, "ha_replica.json")

# Statut du dernier push (côté actif) — en mémoire, pour l'UI.
_last_push = {"at": None, "ok": None, "msg": "—", "bytes": 0}
_last_fail_sig = None   # anti-spam des alertes d'échec (motif B2-3 mcast)


def role():
    """Rôle courant, normalisé (toute valeur inconnue → 'active', le défaut sûr)."""
    r = str(settings.get("control_role") or "active").strip().lower()
    return r if r in ROLES else "active"


def is_active():
    return role() == "active"


def is_standby():
    return role() == "standby"


# ─── Réplication d'état (B3-2b) ───────────────────────────────────────────────
def snapshot_db(dest):
    """Copie cohérente à chaud de la DB live vers `dest` (API online sqlite3.backup, comme
    backup.run_backup mais SANS alerte ni rétention — silencieux, appelé toutes les N min)."""
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _is_valid_sqlite(path):
    """Vrai si `path` est un SQLite ouvrable et cohérent (header + quick_check)."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                return False
    except OSError:
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            con.close()
    except sqlite3.Error:
        return False


def store_replica(raw_bytes, source="?"):
    """Côté STANDBY : valide puis stage un snapshot reçu. Écrit en .tmp, vérifie que c'est un
    SQLite exploitable, puis os.replace atomique → STAGING_DB (+ métadonnée). Lève si invalide
    (le standby ne stage jamais une DB corrompue / tronquée). Retourne le dict métadonnée."""
    os.makedirs(STAGING_DIR, exist_ok=True)
    tmp = STAGING_DB + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw_bytes)
    if not _is_valid_sqlite(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError("snapshot invalide (pas un SQLite cohérent)")
    os.replace(tmp, STAGING_DB)
    meta = {"at": datetime.now().isoformat(timespec="seconds"), "source": source,
            "bytes": len(raw_bytes), "sha256": hashlib.sha256(raw_bytes).hexdigest()}
    with open(STAGING_META, "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def staged_replica():
    """Métadonnée du dernier réplica stagé (ou None), enrichie de l'âge en secondes."""
    if not os.path.exists(STAGING_DB):
        return None
    meta = {}
    try:
        with open(STAGING_META) as f:
            meta = json.load(f)
    except Exception:
        pass
    try:
        meta.setdefault("bytes", os.path.getsize(STAGING_DB))
        meta["age_s"] = int(time.time() - os.path.getmtime(STAGING_DB))
    except OSError:
        pass
    return meta


def push_replica():
    """Côté ACTIF : snapshot + POST binaire vers `ha_standby_url/api/ha/replicate`. Met à jour
    `_last_push` (statut UI). Retourne (ok|None, msg) — None = réplication désactivée (URL vide)."""
    url = (settings.get("ha_standby_url") or "").strip().rstrip("/")
    if not url:
        return None, "désactivé (pas d'URL standby)"
    token = settings.get("update_token") or ""
    if not token:
        _set_push(False, "secret partagé absent — renseigne-le ci-dessous, à l'identique sur les DEUX contrôleurs")
        return False, "secret partagé absent (à poser à l'identique sur les deux contrôleurs)"
    fd, tmp = tempfile.mkstemp(prefix="ha-snap-", suffix=".db")
    os.close(fd)
    try:
        snapshot_db(tmp)
        size = os.path.getsize(tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        req = urllib.request.Request(url + "/api/ha/replicate", data=data, method="POST",
                                     headers={TOKEN_HEADER: token,
                                              "Content-Type": "application/octet-stream",
                                              "Content-Length": str(size)})
        with urllib.request.urlopen(req, timeout=60) as r:   # noqa: S310 (réseau interne)
            txt = r.read().decode()
        j = json.loads(txt) if txt else {}
        if j.get("ok"):
            _set_push(True, f"{size} o → {url}", size)
            return True, "ok"
        _set_push(False, j.get("error") or "réponse inattendue")
        return False, j.get("error") or "échec"
    except urllib.error.HTTPError as e:
        # Un 401 ici ne veut pas dire « pas de token » mais « pas LE MÊME token » : le standby a
        # comparé et refusé. Le distinguer épargne la chasse au mauvais bout de la chaîne.
        msg = ("le standby a REFUSÉ le secret partagé (les deux contrôleurs doivent porter la "
               "MÊME valeur)") if e.code == 401 else f"HTTP {e.code} depuis le standby"
        _set_push(False, msg)
        return False, msg
    except Exception as e:
        _set_push(False, str(e))
        return False, str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ─── Chien de garde du standby ────────────────────────────────────────────────
# Le standby est passif PAR CONCEPTION : quand l'actif meurt, rien ne bouge et rien ne le DIT —
# l'opérateur découvrait la panne en constatant que la production ne répondait plus. On ne bascule
# toujours pas tout seul (pas de quorum → risque de split-brain), mais on ARME une alarme et on
# l'expose à l'UI : la décision reste humaine, l'information ne l'attend plus.
_peer = {"checked_at": None, "alive": None, "fails": 0, "down_since": None, "reason": "—"}


def peer_url():
    """URL de l'AUTRE contrôleur. Un seul réglage pour les deux rôles : quand je suis actif il est
    mon standby (cible de push), quand je suis en veille il est mon actif (cible de sonde)."""
    return (settings.get("ha_standby_url") or "").strip().rstrip("/")


def probe_peer():
    """Sonde l'autre contrôleur (route `/api/ha/peer`, auth par le secret partagé).
    Retourne (alive: bool, raison). Un 401 = VIVANT mais secrets divergents — à ne surtout pas
    confondre avec une panne : l'un se corrige au clavier, l'autre au disjoncteur."""
    url = peer_url()
    if not url:
        return None, "pas d'URL de pair"
    token = (settings.get("update_token") or "").strip()
    req = urllib.request.Request(url + "/api/ha/peer", headers={TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:   # noqa: S310 (réseau interne)
            j = json.loads(r.read().decode() or "{}")
        return True, f"répond (rôle {j.get('role') or '?'})"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return True, "répond mais REFUSE le secret partagé (valeurs différentes)"
        return True, f"répond (HTTP {e.code})"
    except Exception as e:
        return False, str(e)


def peer_state():
    """État du pair pour l'UI (+ ancienneté de la panne en secondes)."""
    st = dict(_peer)
    if st.get("down_since"):
        try:
            st["down_for_s"] = int(time.time() - st["down_since"])
        except Exception:
            pass
    st["armed"] = bool(st.get("alive") is False
                       and st.get("fails", 0) >= int(settings.get("ha_watchdog_fails") or 4))
    st.pop("down_since", None)
    return st


def _watchdog_loop():
    """Boucle de sonde — DÉMARRÉE UNIQUEMENT par un contrôleur en veille (cf. main.py)."""
    from .database import db_add_alert
    armed = False
    while True:
        try:
            seuil = max(1, int(settings.get("ha_watchdog_fails") or 4))
            alive, why = probe_peer()
            _peer["checked_at"] = datetime.now().isoformat(timespec="seconds")
            _peer["alive"], _peer["reason"] = alive, why
            if alive is None:            # pas configuré → on ne prétend rien surveiller
                _peer["fails"] = 0
                _peer["down_since"] = None
            elif alive:
                if armed:
                    db_add_alert("alert.node.ha_actif_repond_de_nouveau", "info", kind="node")
                    armed = False
                _peer["fails"] = 0
                _peer["down_since"] = None
            else:
                _peer["fails"] += 1
                if _peer["down_since"] is None:
                    _peer["down_since"] = time.time()
                if _peer["fails"] >= seuil and not armed:
                    armed = True
                    db_add_alert("alert.node.ha_actif_ne_repond_plus", "error", kind="node",
                                 params={"fails": _peer['fails'], "why": why})
        except Exception as e:
            log.error("Chien de garde HA : %s", e)
        time.sleep(max(5, int(settings.get("ha_watchdog_interval_s") or 15)))


def start_watchdog():
    if not peer_url():
        log.info("Chien de garde HA : aucune URL de pair configurée (inactif).")
        return
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    log.info("Chien de garde HA démarré (sonde de l'actif toutes les %s s).",
             settings.get("ha_watchdog_interval_s"))


def _set_push(ok, msg, nbytes=0):
    _last_push.update({"at": datetime.now().isoformat(timespec="seconds"),
                       "ok": ok, "msg": msg, "bytes": nbytes})


def replication_status():
    """État pour l'UI : rôle + (actif) dernier push + (standby) réplica stagé + config."""
    return {"role": role(),
            "standby_url": (settings.get("ha_standby_url") or "").strip(),
            # Le secret partagé conditionne la réplication DES DEUX CÔTÉS (l'actif le présente,
            # le standby le compare au sien) — l'UI doit pouvoir dire « absent ici » avant que
            # l'utilisateur ne découvre l'échec au premier push. Booléen : jamais la valeur.
            "token_set": bool((settings.get("update_token") or "").strip()),
            "interval_min": int(settings.get("ha_replicate_interval_min") or 5),
            "last_push": dict(_last_push),
            "peer": peer_state(),
            "staged": staged_replica()}


def _replication_loop():
    """Boucle de poussée — DÉMARRÉE UNIQUEMENT par un contrôleur actif (cf. main.py). Pousse
    toutes les `ha_replicate_interval_min` minutes ; idle (log unique) si pas d'URL standby ;
    n'alerte qu'au passage en échec (anti-spam)."""
    global _last_fail_sig
    from .database import db_add_alert
    logged_idle = False
    while True:
        try:
            url = (settings.get("ha_standby_url") or "").strip()
            if not url:
                if not logged_idle:
                    log.info("Réplication HA : aucune URL standby configurée (inactive).")
                    logged_idle = True
            else:
                logged_idle = False
                ok, msg = push_replica()
                sig = f"{ok}:{msg}"
                if ok is False and sig != _last_fail_sig:
                    db_add_alert("alert.node.ha_replication_echec", "warning", kind="node",
                                 params={"url": url, "msg": msg})
                    _last_fail_sig = sig
                elif ok:
                    _last_fail_sig = None
        except Exception as e:
            log.error("Boucle de réplication HA : %s", e)
        time.sleep(max(60, int(settings.get("ha_replicate_interval_min") or 5) * 60))


def start_replication():
    threading.Thread(target=_replication_loop, daemon=True).start()


# ─── Bascule manuelle promote / demote (B3-2c) ────────────────────────────────
def _purge_sidecars(path):
    """Supprime les fichiers annexes SQLite (-wal/-shm/-journal) d'une DB après swap. La DB live
    est en journal_mode=delete (pas de WAL persistant) → défensif au cas où le mode changerait."""
    for ext in ("-wal", "-shm", "-journal"):
        try:
            os.remove(path + ext)
        except OSError:
            pass


def apply_replica_to(src, target):
    """Cœur (testable sur fichiers jetables) de l'application d'un replica : valide `src`, fait un
    backup de sûreté cohérent de `target`, puis remplace `target` par une COPIE de `src` (le staging
    reste intact pour diag/re-promote) et purge les sidecars. Retourne le chemin du backup de sûreté.
    Lève ValueError si `src` n'est pas un SQLite cohérent."""
    if not _is_valid_sqlite(src):
        raise ValueError("replica invalide (pas un SQLite cohérent) — bascule refusée")
    from . import backup as _bk
    os.makedirs(_bk.BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = os.path.join(_bk.BACKUP_DIR, f"db_bobistudio-prepromote-{ts}.db")
    if os.path.exists(target):
        scon = sqlite3.connect(target)
        try:
            dcon = sqlite3.connect(safety)
            try:
                with dcon:
                    scon.backup(dcon)
            finally:
                dcon.close()
        finally:
            scon.close()
    # Copie src → tmp à côté de target, puis replace atomique (ne pas consommer le staging).
    tmp = target + ".incoming"
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        fo.write(fi.read())
    os.replace(tmp, target)
    _purge_sidecars(target)
    return safety


def promote():
    """STANDBY → ACTIVE : applique le dernier replica stagé sur la DB live (après backup de sûreté),
    bascule le rôle, puis redémarre le service (il repart en pilotant). Retourne (ok, msg)."""
    from . import backup as _bk  # noqa: F401 (assure l'import de BACKUP_DIR via apply_replica_to)
    from . import updater
    from .database import db_set_setting, db_add_alert
    if is_active():
        return False, "déjà actif (rien à promouvoir)"
    if not (os.path.exists(STAGING_DB) and _is_valid_sqlite(STAGING_DB)):
        return False, "aucun replica stagé valide — réplication non reçue ?"
    try:
        safety = apply_replica_to(STAGING_DB, DB_PATH)
    except Exception as e:
        return False, f"application du replica échouée : {e}"
    # Marque le replica comme appliqué (trace).
    try:
        meta = staged_replica() or {}
        meta["applied_at"] = datetime.now().isoformat(timespec="seconds")
        with open(STAGING_META, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass
    db_set_setting("control_role", "active")
    db_add_alert("alert.node.ha_promote", "warning", kind="node",
                 params={"safety": os.path.basename(safety)})
    # VIP : la priorité VRRP suit le rôle → re-rendre AVANT le redémarrage (si la VIP n'est pas
    # activée, no-op silencieux et l'opérateur la déplace à la main comme avant).
    from . import vip as _vip
    _vip.refresh_for_role()
    updater.restart_service()
    return True, "promu actif — redémarrage en cours (déplace la VIP de management)"


def demote():
    """ACTIVE → STANDBY : bascule le rôle puis redémarre (repart passif). Ne touche PAS la DB.
    Retourne (ok, msg)."""
    from . import updater
    from .database import db_set_setting, db_add_alert
    if is_standby():
        return False, "déjà en veille (rien à rétrograder)"
    db_set_setting("control_role", "standby")
    db_add_alert("alert.node.ha_demote", "warning", kind="node")
    from . import vip as _vip
    _vip.refresh_for_role()
    updater.restart_service()
    return True, "rétrogradé en veille — redémarrage en cours (retire la VIP de management)"
