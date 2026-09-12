"""Verrou d'édition SOUPLE par conteneur — « qui a la main sur ce mur ? ».

Problème réel (signalé le 2026-08-11) : deux personnes ouvrent le même multiview ; le composer
poste `editorParams` EN ENTIER à chaque geste, donc celui qui enregistre écrase l'état de l'autre
avec la copie qu'il a chargée à l'ouverture. L'image posée par A disparaît dès que B bouge quoi que
ce soit — et revient quand A touche son image, puisque A repousse alors sa propre copie.

Deux protections, volontairement distinctes :

  1. CE VERROU, consultatif : il dit qui édite, il ne barre pas la route au serveur. Il évite la
     collision par la CONVERSATION (« Vincent édite ce mur depuis 3 min »), là où l'utilisateur
     peut encore décider — c'est ce qui manque le plus.
  2. La garde de RÉVISION (`containers.config_rev`), elle, est le filet dur : elle refuse un
     déploiement bâti sur un état périmé, y compris après une reprise de main, et couvre les
     écritures qui ne passent pas par le composer (page Câbles, macros, restauration de projet).

★ EN MÉMOIRE, délibérément. Un redémarrage de l'orchestrateur libère tous les verrous, et c'est
le comportement voulu : un verrou survivant à un redémarrage n'aurait plus personne derrière lui.
Même raison pour le TTL — un onglet fermé, un portable rabattu ou un réseau coupé ne doivent pas
condamner un mur. Le verrou n'est pas un mécanisme de sécurité : les permissions restent seules
juges de qui a le droit d'écrire.
"""
import threading
import time

# TTL : au-delà, le verrou est considéré abandonné. L'éditeur bat toutes les BATTEMENT_S ;
# la marge (4 battements) absorbe une page en arrière-plan, dont le navigateur ralentit les
# minuteries, sans laisser un mur bloqué plus d'une minute et demie après une fermeture.
BATTEMENT_S = 20
TTL_S = 90

_verrous = {}          # vmid (int) → {"user_id", "user_name", "depuis", "vu"}
_lock = threading.Lock()


def _expire(maintenant):
    for vmid, v in list(_verrous.items()):
        if maintenant - v["vu"] > TTL_S:
            del _verrous[vmid]


def etat(vmid, user_id=None):
    """État du verrou : {libre, user_id, user_name, depuis_s, a_moi}. Sans effet de bord (hors
    expiration des verrous abandonnés, qui n'appartient à personne)."""
    maintenant = time.time()
    with _lock:
        _expire(maintenant)
        v = _verrous.get(int(vmid))
        if not v:
            return {"libre": True, "a_moi": False, "user_id": None, "user_name": "", "depuis_s": 0}
        return {"libre": False,
                "a_moi": (user_id is not None and v["user_id"] == user_id),
                "user_id": v["user_id"], "user_name": v["user_name"],
                "depuis_s": int(maintenant - v["depuis"])}


def prendre(vmid, user_id, user_name, force=False):
    """Prend le verrou, ou le renouvelle s'il est déjà à nous (battement de cœur).
    `force` = reprise de main explicite : l'utilisateur a LU qui édite et a choisi de passer
    devant. Renvoie (obtenu: bool, etat: dict)."""
    vmid = int(vmid)
    maintenant = time.time()
    with _lock:
        _expire(maintenant)
        v = _verrous.get(vmid)
        if v and v["user_id"] != user_id and not force:
            return False, {"libre": False, "a_moi": False, "user_id": v["user_id"],
                           "user_name": v["user_name"], "depuis_s": int(maintenant - v["depuis"])}
        # Reprise de main : le compteur « depuis » repart, c'est un NOUVEL éditeur.
        depuis = v["depuis"] if (v and v["user_id"] == user_id) else maintenant
        _verrous[vmid] = {"user_id": user_id, "user_name": user_name or "", "depuis": depuis,
                          "vu": maintenant}
        return True, {"libre": False, "a_moi": True, "user_id": user_id,
                      "user_name": user_name or "", "depuis_s": int(maintenant - depuis)}


def rendre(vmid, user_id):
    """Rend le verrou s'il nous appartient (fermeture de l'éditeur, changement de mur). Rendre
    le verrou d'un autre est un no-op : seule une reprise de main explicite le déplace."""
    vmid = int(vmid)
    with _lock:
        v = _verrous.get(vmid)
        if v and v["user_id"] == user_id:
            del _verrous[vmid]
            return True
    return False


# ─── AUTEUR de l'écriture en cours ───────────────────────────────────────────
# La garde de révision doit distinguer « quelqu'un d'autre a écrit » de « j'ai écrit moi-même ».
# Sans cette distinction elle est inutilisable : le déploiement s'exécute dans un THREAD, la
# nouvelle révision n'existe donc pas encore quand la réponse HTTP part — un éditeur ne peut pas
# tenir son compteur à jour, et se ferait refuser ses PROPRES gestes suivants.
#
# On mémorise donc, à côté de la révision, QUI l'a produite : un conflit n'existe que si la
# dernière écriture vient d'un autre. Le porteur est un thread-local posé par `before_request`
# (thread de requête) et repropagé explicitement par les threads de déploiement, qui n'héritent
# de rien. Absent (None) = écriture MACHINE — surveillance, réconciliation, agent — jamais
# considérée comme un conflit humain.
_local = threading.local()


def poser_auteur(user_id):
    _local.auteur = user_id


def auteur_courant():
    return getattr(_local, "auteur", None)
