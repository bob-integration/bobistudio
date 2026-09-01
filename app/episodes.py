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

import json
import logging
import os
import threading
import time

from .config import DB_PATH

log = logging.getLogger(__name__)

FLUSH_S = 30.0          # écriture au plus toutes les 30 s (l'état change à la transition, c'est rare)
_SEP = "\x1f"           # séparateur de clé composite (une clé JSON doit être une chaîne)


def _chemin(nom):
    return os.path.join(os.path.dirname(DB_PATH) or ".", f"episodes_{nom}.json")


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
        self.chemin = _chemin(nom)
        self._d = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._last_flush = time.time()
        self._charger()

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

    def flush(self, force=False):
        """Écrit l'état si nécessaire. Débouncé : la transition est rare, mais l'appelant peut
        appeler à chaque tick sans payer d'I/O."""
        now = time.time()
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._last_flush < FLUSH_S:
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
            # Jamais silencieux : si l'état n'est pas sauvegardé, la prochaine relance re-notifiera
            # et l'exploitant doit pouvoir relier les deux.
            with self._lock:
                self._dirty = True
            log.warning("Épisodes « %s » : écriture de l'état impossible (%s) — les incidents en "
                        "cours seront re-signalés au prochain redémarrage.", self.nom, e)
