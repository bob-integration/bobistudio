#!/usr/bin/env python3
"""La réouverture d'un lecteur MXL ne balaie JAMAIS le domaine.

`_StaleGuard.reopen()` appelait `Instance.garbage_collect()`, qui est **global au domaine** : il
ne vise pas la génération orpheline de l'appelant, il ramasse aussi les flux VIVANTS des autres
processus. Dégâts mesurés : quatre flux du moteur 2110 détruits sous leur producteur
(2026-08-19), et une réplique RDMA recréée sous son récepteur (2026-10-24, r620-2) qui a ensuite
écrit dans un inode délié. La parade de remplacement est une Instance NEUVE (cache vierge), qui
n'a aucun effet sur le domaine.

Hors ligne : libmxl n'est pas chargé, tout est bouchonné.
"""
import sys, pathlib
RACINE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "script_templates"))

import bobimxl

ECHECS = []


def controle(libelle, ok, pourquoi=""):
    print(("  OK   " if ok else "  ÉCHEC ") + libelle + ("" if ok else "  → " + pourquoi))
    if not ok:
        ECHECS.append(libelle)


class _InstanceBidon:
    """Instance de substitution : retient si on a balayé le domaine."""
    balayages = 0
    creees = 0

    def __init__(self, domain="/dev/shm/mxl", options=None):
        self.domain = domain
        self._h = object()
        _InstanceBidon.creees += 1

    def garbage_collect(self):
        _InstanceBidon.balayages += 1

    def close(self):
        pass


class _LibBidon:
    def mxlReleaseFlowReader(self, *a):
        return 0


def _lecteur_bouchonne():
    """Un Reader SANS __init__ (pas de libmxl), juste ce que `reopen` touche."""
    r = object.__new__(bobimxl.Reader)
    r.inst = _InstanceBidon()
    r.fid = "11111111-2222-3333-4444-555555555555"
    r.name = "flux-essai"
    r._opts = None
    r._own_inst = None
    r._stale_n = 0
    r._h = None
    r._open_handle = lambda: None      # l'ouverture réelle exige libmxl
    return r


print("reopen() ne balaie jamais le domaine")

bobimxl._lib = _LibBidon()
bobimxl.Instance = _InstanceBidon

r = _lecteur_bouchonne()
_InstanceBidon.balayages = 0
_InstanceBidon.creees = 0
n1 = r.reopen()

controle("aucun garbage_collect au 1er essai",
         _InstanceBidon.balayages == 0,
         "un balayage global détruit les flux vivants des AUTRES processus")

controle("le 1er essai rouvre sur une Instance NEUVE",
         _InstanceBidon.creees == 1 and r.inst is not None and r._own_inst is r.inst,
         "sans cache vierge, le nom résout encore vers l'orphelin et la boucle gèle en silence")

controle("le 1er essai est bien compté n°1", n1 == 1, "le recul exponentiel s'appuie sur ce rang")

# Acharnement : plusieurs réouvertures de suite ne doivent toujours rien balayer.
for _ in range(5):
    r.reopen()
controle("toujours aucun balayage après 6 réouvertures",
         _InstanceBidon.balayages == 0,
         "c'est l'acharnement qui faisait les dégâts : 850 cycles/jour mesurés sur r620-2")

# La méthode existe toujours (outil d'entretien), mais plus personne ne l'appelle en réouverture.
controle("Instance.garbage_collect existe encore",
         callable(getattr(_InstanceBidon, "garbage_collect", None)),
         "le test doit prouver qu'on ne l'APPELLE plus, pas qu'elle a disparu")

print(("ÉCHECS : %d" % len(ECHECS)) if ECHECS else "tout est vert")
sys.exit(1 if ECHECS else 0)
