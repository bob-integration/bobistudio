"""État d'ÉPISODE d'alarme PERSISTÉ — la mémoire d'une alarme ne doit pas être celle du process.

Une alarme correctement écrite est *edge-triggered* : elle alerte à l'APPARITION d'un incident, pas
à chaque échantillon. Cet état vit naturellement dans un dict de module… donc en RAM : au
redémarrage de l'orchestrateur il repart à vide, et l'incident TOUJOURS EN COURS est ré-annoncé
comme s'il venait de naître.

Ce n'est pas théorique. Journée du 2026-07-26, dix redémarrages du service en deux heures :

  - 26 alertes « PTP … horloge absente » pour UNE seule horloge absente ;
  - 26 alertes « pool de calcul SUR-SOUSCRIT » pour UN seul pool sur-souscrit ;
  - 17 alertes « EN FAMINE CPU » pour UNE seule famine (chacune à ~70 s d'un redémarrage — la
    signature était le délai constant entre le `systemd Started` et l'alerte).

Soit ~70 notifications pour trois conditions. L'exploitant apprend alors à ignorer son fil
d'alertes, ce qui coûte bien plus cher que les trois incidents réunis.

Le module fournit un dict {clé → état} persisté sur disque (JSON à côté de la DB, écriture
atomique tmp+rename, flush débouncé) et rechargé au boot. Il ne connaît RIEN à la sémantique des
alarmes : il ne décide pas quand alerter, il se souvient seulement de ce qui a déjà été annoncé.

Modèle repris de `metrics._flush_pannes` / `ptp._flush_stats` (mêmes garanties : jamais d'I/O sur
le chemin chaud, jamais d'échec silencieux — un état illisible repart à vide EN LE DISANT, car le
prix à payer est alors une re-notification, et elle doit être explicable).
"""

import atexit
import json
import logging
import os
import threading
import time
import weakref

from .config import DB_PATH

log = logging.getLogger(__name__)

FLUSH_S = 30.0          # écriture au plus toutes les 30 s (l'état change à la transition, c'est rare)
_SEP = "\x1f"           # séparateur de clé composite (une clé JSON doit être une chaîne)


_instances = weakref.WeakSet()     # every live state, flushed at a clean exit


@atexit.register
def _flush_all():
    for e in list(_instances):
        try:
            e.flush(force=True)
        except Exception:
            pass


def _base_db():
    """The database this process uses NOW. The copy of `DB_PATH` taken at import was the
    production path in any test that isolated its database after importing a module holding an
    episode state (2026-11-21: three guards would have written production episode files)."""
    try:
        from . import database
        return database.DB_PATH or DB_PATH
    except Exception:
        return DB_PATH


def _chemin(nom):
    return os.path.join(os.path.dirname(_base_db()) or ".", f"episodes_{nom}.json")


def _cle_txt(cle):
    """Clé publique (str, int, ou tuple de ceux-ci) → chaîne stable pour le JSON."""
    if isinstance(cle, tuple):
        return _SEP.join(str(x) for x in cle)
    return str(cle)


class EtatEpisodes:
    """Dict {clé → état} persisté, à la sémantique volontairement pauvre.

    `etat` est une valeur JSON quelconque (bool, str de niveau, dict…) : c'est au producteur de
    décider ce qu'il doit se rappeler. La règle d'usage est la même partout : ne PAS ré-alerter
    quand l'état relu est déjà celui qu'on s'apprête à annoncer.
    """

    def __init__(self, nom):
        self.nom = nom
        self._d = {}
        self._lock = threading.Lock()
        self._dirty = False
        # 0, not « now »: the first change after a start is written at once. With « now », a
        # change in the first 30 s waited for a LATER change that might never come — the stall
        # alert, raised ~30 s after a start, was lost by the next restart and said again.
        self._last_flush = 0.0
        self._timer = None              # the one pending deferred write, if any
        self._io = threading.Lock()     # the timer and a forced flush must not share the .tmp
        self._charger()
        _instances.add(self)

    @property
    def chemin(self):
        """Resolved at each use, next to the CURRENT database (cf. `_base_db`), unless a path
        was set explicitly (tests do)."""
        return getattr(self, "_chemin_force", None) or _chemin(self.nom)

    @chemin.setter
    def chemin(self, valeur):
        self._chemin_force = valeur

    def _charger(self):
        try:
            with open(self.chemin) as f:
                data = json.load(f)
        except FileNotFoundError:
            log.info("Épisodes « %s » : aucun état persisté (%s) — départ à vide.",
                     self.nom, self.chemin)
            return
        except (ValueError, OSError) as e:
            log.warning("Épisodes « %s » : état persisté illisible (%s) — départ à vide, les "
                        "incidents EN COURS seront re-signalés une fois.", self.nom, e)
            return
        if isinstance(data, dict):
            self._d = {str(k): v for k, v in data.items()}

    def get(self, cle, defaut=None):
        with self._lock:
            return self._d.get(_cle_txt(cle), defaut)

    def poser(self, cle, valeur):
        """Mémorise l'état annoncé pour cette clé. `None` équivaut à `retirer`."""
        if valeur is None:
            return self.retirer(cle)
        k = _cle_txt(cle)
        with self._lock:
            if self._d.get(k) == valeur:
                return
            self._d[k] = valeur
            self._dirty = True
        self.flush()

    def retirer(self, cle):
        k = _cle_txt(cle)
        with self._lock:
            if k not in self._d:
                return
            self._d.pop(k, None)
            self._dirty = True
        self.flush()

    def cles(self):
        with self._lock:
            return list(self._d.keys())

    def purger(self, garder):
        """Retire les clés dont le prédicat `garder(cle_txt)` est faux (objet disparu : conteneur
        détruit, nœud désenrôlé). Sans ça le fichier grossit indéfiniment et un vmid recyclé
        hériterait de l'état d'un autre."""
        with self._lock:
            morts = [k for k in self._d if not garder(k)]
            for k in morts:
                self._d.pop(k, None)
            if morts:
                self._dirty = True
        if morts:
            self.flush(force=True)
        return len(morts)

    def _flush_differe(self):
        with self._lock:
            self._timer = None
        self.flush(force=True)

    def flush(self, force=False):
        """Écrit l'état si nécessaire. Débouncé : la transition est rare, mais l'appelant peut
        appeler à chaque tick sans payer d'I/O."""
        # `_io` is taken BEFORE the snapshot: two writers (the deferred timer, a forced flush)
        # must neither share the .tmp nor let an older snapshot land after a newer one.
        with self._io:
            now = time.time()
            with self._lock:
                if not self._dirty:
                    return
                if not force and now - self._last_flush < FLUSH_S:
                    # Debounced, but GUARANTEED (2026-11-21): the write used to wait for the next
                    # change, and a rare state with no change behind it was never written. One
                    # timer per state, at the end of the debounce window.
                    if self._timer is None:
                        self._timer = threading.Timer(FLUSH_S - (now - self._last_flush) + 0.05,
                                                      self._flush_differe)
                        self._timer.daemon = True
                        self._timer.start()
                    return
                data = dict(self._d)
                self._last_flush = now
                self._dirty = False
            tmp = self.chemin + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self.chemin)
            except OSError as e:
                # Jamais silencieux : si l'état n'est pas sauvegardé, la prochaine relance
                # re-notifiera et l'exploitant doit pouvoir relier les deux.
                with self._lock:
                    self._dirty = True
                log.warning("Épisodes « %s » : écriture de l'état impossible (%s) — les incidents "
                            "en cours seront re-signalés au prochain redémarrage.", self.nom, e)
