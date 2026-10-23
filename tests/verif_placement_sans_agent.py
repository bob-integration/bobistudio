#!/usr/bin/env python3
"""Garde : la passe de constat du placement ne sonde que les nœuds qui ont un agent.

DÉFAUT D'ORIGINE (relevé le 2026-10-11) : une ligne de nœud en attente d'enrôlement, sans agent,
était sondée comme les autres — l'URL de base vide donnait « unknown url type: '/v1/host/exec' »
toutes les 2 minutes, 2 588 fois au journal. Le bruit couvrait les vrais avertissements de
placement, et faisait croire le constat en panne alors qu'il tournait sur les vrais nœuds.

Base jetable ; `verifier` est remplacé par un témoin, aucun nœud n'est contacté.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)


def main():
    chemin_db = os.path.join(tempfile.mkdtemp(), "t.db")
    from app import config, database
    config.DB_PATH = chemin_db
    database.DB_PATH = chemin_db
    # ⚠ Connexion THREAD-LOCAL : sans cette purge, la garde lirait la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import placement

    avec = database.db_add_node("dl360-1", "10.0.0.1")
    database.db_update_node(avec, agent_url="http://10.0.0.1:9100")
    sans = database.db_add_node("en attente", "")
    sondes = []
    placement.verifier = lambda nid: sondes.append(nid)
    placement.verifier_tous()
    ok = sondes == [avec]
    print(("  ok   " if ok else "  ÉCHEC ") + "seul le nœud avec agent est sondé (sondés : %s, sans agent : %s)"
          % (sondes, sans))
    print("TOUT PASSE" if ok else "ÉCHEC")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
