# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Rotation du journal de l'orchestrateur — anti-saturation disque.

La stdlib n'offre pas de handler combiné TAILLE+TEMPS. `SizedTimedRotatingFileHandler` roule quand le
fichier dépasse `maxBytes` (PRIORITAIRE) OU quand `rotate_seconds` se sont écoulés depuis la dernière
rotation. `backupCount` borne le nombre d'archives → le disque est borné à `maxBytes × (backupCount+1)`
quelle que soit l'option temps. Réglé depuis la base (`log_max_mb`/`log_backups`/`log_rotate_days`),
avec repli sur `config`. Pris en compte au (re)démarrage (le handler est posé une fois au boot).
"""
import glob
import logging
import logging.handlers
import os
import shutil
import time

log = logging.getLogger(__name__)


class SizedTimedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, filename, maxBytes=0, backupCount=0, rotate_seconds=0, **kw):
        super().__init__(filename, maxBytes=maxBytes, backupCount=backupCount, **kw)
        self.rotate_seconds = rotate_seconds
        # ⚠ La date de référence est celle du FICHIER, pas celle du démarrage du process.
        # Avec `time.time()`, la rotation par temps ne se déclenchait jamais : elle exige que
        # l'orchestrateur tourne `log_rotate_days` jours d'affilée, alors qu'il redémarre à
        # chaque livraison. Constaté le 2026-08-15 : archives figées au 11 juillet, réglage
        # « rotation tous les 7 jours » actif depuis, et jamais honoré une seule fois.
        try:
            self._last_rollover = os.path.getmtime(filename)
        except OSError:
            self._last_rollover = time.time()

    def shouldRollover(self, record):
        # Taille d'abord (borne dure du disque), puis temps.
        if super().shouldRollover(record):
            return 1
        if self.rotate_seconds and (time.time() - self._last_rollover) >= self.rotate_seconds:
            return 1
        return 0

    def doRollover(self):
        super().doRollover()
        self._last_rollover = time.time()


def _reglages():
    """(rotation, max_mb, backups, days) depuis la base, repli sur `config`. Best-effort :
    au tout 1er boot la table settings peut ne pas exister encore."""
    from . import config
    max_mb, backups, days = config.LOG_MAX_MB, config.LOG_BACKUPS, config.LOG_ROTATE_DAYS
    rotation = True
    try:
        from .database import db_get_setting
        max_mb = int(db_get_setting("log_max_mb", max_mb) or max_mb)
        backups = int(db_get_setting("log_backups", backups))
        days = int(db_get_setting("log_rotate_days", days))
        rotation = db_get_setting("log_rotation_active", True) not in (0, "0", False, "false", "False", "")
    except Exception:
        pass
    return rotation, max(1, max_mb), max(0, backups), max(0, days)


_handler = None     # handler VIVANT, pour appliquer les réglages sans redémarrer
_chemin = None


def make_handler(path):
    """Construit le handler de log depuis les réglages, et le retient (cf. `appliquer_reglages`)."""
    global _handler, _chemin
    rotation, max_mb, backups, days = _reglages()
    _chemin = path
    _handler = SizedTimedRotatingFileHandler(
        path,
        maxBytes=(max_mb * 1024 * 1024) if rotation else 0,
        backupCount=backups,
        rotate_seconds=(days * 86400) if rotation else 0,
        encoding="utf-8")
    return _handler


def appliquer_reglages():
    """Applique les réglages de rotation AU HANDLER EN COURS, sans redémarrage.

    Un réglage qui n'agit qu'au prochain (re)démarrage est un réglage qu'on croit posé alors
    qu'il ne l'est pas : on l'enregistre, l'occupation continue de grimper, et rien ne dit
    pourquoi. → (rotation, max_mb, backups, days) effectivement appliqués, ou None si aucun
    handler fichier n'est installé (mode console)."""
    if _handler is None:
        return None
    rotation, max_mb, backups, days = _reglages()
    _handler.maxBytes = (max_mb * 1024 * 1024) if rotation else 0
    _handler.backupCount = backups
    _handler.rotate_seconds = (days * 86400) if rotation else 0
    log.info("journal : rotation %s (%d Mo × %d archives, %d jour(s))",
             "active" if rotation else "DÉSACTIVÉE", max_mb, backups, days)
    return {"rotation": rotation, "max_mb": max_mb, "backups": backups, "days": days}


def stdout_va_dans_le_fichier(path):
    """La sortie standard du process pointe-t-elle DÉJÀ sur le fichier de journal ?

    C'est le cas sous systemd avec `StandardOutput=append:<path>`. Ajouter en plus un
    `StreamHandler` écrit alors CHAQUE LIGNE DEUX FOIS dans le même fichier — mesuré le
    2026-08-15 sur un journal de 304 Mo : exactement la moitié de doublons."""
    try:
        st_out = os.fstat(1)
        st_fic = os.stat(path)
        return (st_out.st_ino, st_out.st_dev) == (st_fic.st_ino, st_fic.st_dev)
    except OSError:
        return False


def _fichiers(path):
    """[(chemin, octets)] du journal actif, de ses archives, et du legacy `orchestrateur.log`."""
    out = []
    legacy = os.path.join(os.path.dirname(path), "orchestrateur.log")
    for f in [path] + sorted(glob.glob(path + ".*")) + [legacy]:
        try:
            out.append((f, os.path.getsize(f)))
        except OSError:
            pass
    return out


# Marge avant de crier au dépassement : le fichier actif peut légitimement dépasser sa taille
# maximale entre deux rollovers, et la somme se compare à un plafond, pas à une limite dure.
_MARGE_PLAFOND = 1.25


def etat(path=None):
    """Occupation des journaux, confrontée à ce que les RÉGLAGES promettent.

    C'est la forme d'alarme que ce projet s'impose (cf. docs/chantiers/OBSERVABILITE.md) :
    comparer le réalisé à l'INTENTION. Le plafond promis vaut `log_max_mb × (log_backups + 1)` ;
    le dépasser ne veut pas dire « beaucoup de logs », ça veut dire **la rotation ne fait pas ce
    qu'elle annonce** — le cas vécu, où trois archives figées au 11 juillet (dont une de 2,3 Go)
    survivaient à un réglage qui promettait 2 Go au total."""
    from .config import LOG_PATH
    path = path or _chemin or LOG_PATH
    rotation, max_mb, backups, days = _reglages()
    fichiers = _fichiers(path)
    total = sum(sz for _, sz in fichiers)
    plafond = (max_mb * (backups + 1) * 1024 * 1024) if rotation else None
    try:
        du = shutil.disk_usage(os.path.dirname(path) or "/")
        libre, capacite = du.free, du.total
    except OSError:
        libre = capacite = None
    return {
        "path": path,
        "rotation": rotation, "max_mb": max_mb, "backups": backups, "rotate_days": days,
        "fichiers": [{"name": os.path.basename(f), "bytes": sz} for f, sz in fichiers],
        "total_bytes": total,
        "plafond_bytes": plafond,
        "depasse": bool(plafond and total > plafond * _MARGE_PLAFOND),
        "libre_bytes": libre, "capacite_bytes": capacite,
        "doublon_stdout": stdout_va_dans_le_fichier(path) and _handler is not None
                          and any(isinstance(h, logging.StreamHandler)
                                  and not isinstance(h, logging.FileHandler)
                                  for h in logging.getLogger().handlers),
    }


_VERIF_INTERVALLE_S = 300
_verif_ts = [0.0]
_etats = {}          # motif → en cours ? (alerte à la TRANSITION, dans les deux sens)


def verifier():
    """Contrôle périodique de l'occupation des journaux (throttlé). Alerte à la transition.

    Appelé par la boucle de surveillance. Deux motifs, volontairement distincts :
      • `place` — l'espace libre de la partition passe sous le plancher réglé : **error**, c'est
        la panne qui emporte l'orchestrateur entier (incident 2026-07-11) et un nœud avant lui.
      • `plafond` — les journaux dépassent ce que les réglages promettent : **warning**, la
        rotation ne tient pas (archives figées, rotation désactivée et oubliée)."""
    if time.monotonic() - _verif_ts[0] < _VERIF_INTERVALLE_S:
        return
    _verif_ts[0] = time.monotonic()
    from .database import db_add_alert, db_get_setting
    e = etat()
    mo = lambda n: "%.0f Mo" % (n / (1024 * 1024))

    try:
        plancher_go = float(db_get_setting("log_disk_free_min_gb", 5) or 0)
    except (TypeError, ValueError):
        plancher_go = 5.0
    manque = (e["libre_bytes"] is not None and plancher_go > 0
              and e["libre_bytes"] < plancher_go * 1024 ** 3)
    if manque and not _etats.get("place"):
        db_add_alert("alert.disk.journaux_plancher", "error", kind="disk",
                     params={"occupe": mo(e["total_bytes"]), "path": os.path.basename(e["path"]),
                             "libre_go": e["libre_bytes"] / 1024 ** 3, "plancher_go": plancher_go})
    elif not manque and _etats.get("place"):
        db_add_alert("alert.disk.journaux_plancher_ok", "info", kind="disk",
                     params={"libre_go": e["libre_bytes"] / 1024 ** 3})
    _etats["place"] = manque

    if e["depasse"] and not _etats.get("plafond"):
        db_add_alert("alert.disk.journaux_plafond", "warning", kind="disk",
                     params={"occupe": mo(e["total_bytes"]), "plafond": mo(e["plafond_bytes"]),
                             "max_mb": e["max_mb"], "archives": e["backups"] + 1})
    elif not e["depasse"] and _etats.get("plafond"):
        db_add_alert("alert.disk.journaux_plafond_ok", "info", kind="disk",
                     params={"occupe": mo(e["total_bytes"])})
    _etats["plafond"] = e["depasse"]

    if e["doublon_stdout"]:
        log.warning("journal : la sortie standard pointe sur %s ET un StreamHandler est posé — "
                    "chaque ligne est écrite deux fois", e["path"])
