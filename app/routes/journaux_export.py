# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Export des journaux en CSV — alertes, événements PTP, événements de sonde.

Besoin d'origine : pouvoir extraire l'intégralité des journaux, pour les verser dans l'outillage
d'un exploitant plutôt que de les lire uniquement à l'écran.

⚠ La référence au cahier des charges qui figurait ici a été retirée : ce fichier est PUBLIÉ, et
citer l'appel d'offres d'un client nomme ce client. Le besoin, lui, se dit sans lui — et c'est
d'ailleurs plus utile à qui lit le code, qui n'a pas le document. Le garde-fou de `publier.sh` a
refusé la publication tant que la ligne y était ; il a eu raison.

Rien n'est collecté ici : les trois journaux existent déjà, avec leurs filtres
(`db_get_alerts`, `db_get_ptp_events`, `db_get_probe_events`). Ce module ne fait que les
sérialiser. C'est délibéré — un export qui recalculerait ce que l'écran affiche finirait par
diverger de l'écran, et on aurait deux vérités.

⚠ LES ALERTES SONT STOCKÉES EN CLÉS i18n, PAS EN PHRASES. Une alerte est écrite une fois par un
thread de fond et relue par N utilisateurs qui n'ont pas la même langue (`app/i18n.py:241`).
Exporter la colonne `message` brute livrerait des lignes du genre `alert.ptp.perdu` à un
exploitant. On passe donc par `i18n.rendre_alertes`, comme l'écran — avec la langue du LECTEUR,
ou celle demandée en paramètre pour produire un livrable dans la langue du destinataire.

⚠ LE SÉPARATEUR PAR DÉFAUT EST LE POINT-VIRGULE, ET C'EST UN CHOIX. Ces fichiers sont ouverts
dans Excel en locale française, où la virgule est le séparateur DÉCIMAL : un CSV à virgules y
arrive en une seule colonne. Le BOM UTF-8 est là pour la même raison — sans lui Excel lit du
Latin-1 et massacre les accents. `?sep=,` rend un fichier RFC 4180 strict pour un outil qui en
a besoin. Aucun des deux choix n'est bon dans l'absolu ; celui-ci est bon pour qui reçoit le
fichier.
"""

import csv
import io
import re
from datetime import datetime

from flask import Response, jsonify, request

from . import bp
from ..auth import require_global_access
from ..database import db_get_alerts, db_get_ptp_events

# Plafond de lignes. La rétention des alertes est de 1000 (cf. `init_db`) ; les deux autres
# journaux sont bornés par leur propre rétention. On plafonne quand même : un export est servi
# en une fois, en mémoire, et « tous les logs » ne doit pas devenir « toute la RAM ».
LIMITE_MAX = 20000

# Colonnes exportées, par source. EXPLICITES et pas `SELECT *` : une colonne ajoutée en base ne
# doit pas apparaître d'elle-même dans un livrable client, et l'ordre des colonnes d'un CSV est
# un contrat pour qui l'importe.
COLONNES = {
    # ⚠ L'ORDRE EST UN CONTRAT ASCENDANT. L'export historique servait `timestamp, niveau,
    # message` : ces trois-là gardent leurs positions, et le contexte est AJOUTÉ derrière.
    # Les intervertir casserait tout import déjà en place chez un client.
    "alertes": [("timestamp", "horodatage"), ("niveau", "niveau"), ("message", "message"),
                ("kind", "nature"), ("vmid", "vmid"), ("node_id", "noeud_id")],
    "ptp":     [("ts", "horodatage"), ("level", "niveau"), ("type", "type"),
                ("node_name", "noeud"), ("network_name", "reseau"), ("ifname", "interface"),
                ("detail", "detail")],
    "sonde":   [("ts_start", "debut"), ("ts_end", "fin"), ("severity", "severite"),
                ("kind", "nature"), ("vmid", "vmid"), ("flow", "flux"),
                ("message", "message"), ("value", "valeur")],
}


def _entier(nom):
    v = (request.args.get(nom) or "").strip()
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _lignes(source, limite):
    """Les lignes d'un journal, filtrées comme à l'écran. Rend (lignes, nom_de_fichier)."""
    q = (request.args.get("q") or "").strip() or None
    if source == "alertes":
        from .. import i18n
        lignes = db_get_alerts(q=q, niveau=(request.args.get("niveau") or "").strip() or None,
                               limit=limite, vmid=_entier("vmid"), node_id=_entier("node_id"),
                               kind=(request.args.get("kind") or "").strip() or None)
        # Traduction à la LECTURE, exactement comme l'écran.
        return i18n.rendre_alertes(lignes, (request.args.get("lang") or "").strip() or None)
    if source == "ptp":
        return db_get_ptp_events(node_id=_entier("node_id"),
                                 network_id=_entier("network_id"), q=q, limit=limite)
    from ..probe_monitor import db_get_probe_events
    return db_get_probe_events(vmid=_entier("vmid"),
                               flow=(request.args.get("flow") or "").strip() or None,
                               kind=(request.args.get("kind") or "").strip() or None,
                               severity=(request.args.get("severite") or "").strip() or None,
                               limit=limite)


def _csv(lignes, colonnes, sep):
    """Sérialise en CSV. `lineterminator="\\r\\n"` : c'est ce que dit la RFC 4180, et c'est ce
    qu'attendent les tableurs sous Windows — un fichier en LF seul s'y ouvre en une ligne."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow([titre for _cle, titre in colonnes])
    for l in lignes:
        ligne = []
        for cle, _titre in colonnes:
            v = l.get(cle)
            # ⚠ ON NEUTRALISE LES FORMULES. Un tableur interprète une cellule commençant par
            # `=`, `+`, `-` ou `@` comme une FORMULE : un message d'alerte contenant du texte
            # d'origine externe (nom de flux, détail d'erreur) deviendrait du code exécuté à
            # l'ouverture. C'est l'injection CSV, et elle est réelle sur un livrable qu'on
            # envoie à un client. Préfixe apostrophe : le tableur affiche le texte tel quel.
            s = "" if v is None else str(v)
            if s[:1] in ("=", "+", "-", "@"):
                s = "'" + s
            # Un séparateur ou un saut de ligne dans une cellule est géré par `csv` (guillemets) ;
            # les caractères de contrôle, eux, ne le sont pas — on les remplace par un espace.
            ligne.append(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s))
        w.writerow(ligne)
    return buf.getvalue()


@bp.route("/api/journaux/export", methods=["GET"])
@require_global_access
def journaux_export():
    """CSV d'un journal. `source` = `alertes` | `ptp` | `sonde`.

    Les filtres sont ceux de l'écran correspondant : `q`, `niveau`, `vmid`, `node_id`, `kind`
    pour les alertes ; `node_id`, `network_id`, `q` pour PTP ; `vmid`, `flow`, `kind`,
    `severite` pour la sonde. Sans filtre, on exporte tout le journal (jusqu'à LIMITE_MAX)."""
    source = (request.args.get("source") or "alertes").strip().lower()
    if source not in COLONNES:
        return jsonify({"error": "source inconnue : %s (attendu %s)"
                                 % (source, ", ".join(sorted(COLONNES)))}), 400
    try:
        limite = max(1, min(int(request.args.get("limit", LIMITE_MAX)), LIMITE_MAX))
    except ValueError:
        limite = LIMITE_MAX
    sep = ";" if (request.args.get("sep") or ";") != "," else ","

    # ⚠ `require_global_access` ET PAS `require_login`, comme `/api/alerts/export` avant lui :
    # un export part en fichier, hors de l'écran et hors du produit. Un utilisateur scopé projet
    # ne lit pas le journal du cluster, et une liste vide serait une réponse ambiguë — le refus
    # est plus honnête.
    lignes = _lignes(source, limite)

    corps = _csv(lignes, COLONNES[source], sep)
    # BOM UTF-8 : sans lui, Excel lit le fichier en Latin-1 et rend « dépassé » en « dépassé ».
    donnees = "﻿" + corps
    horo = datetime.now().strftime("%Y%m%d-%H%M%S")
    nom = "bobistudio-%s-%s.csv" % (source, horo)
    return Response(donnees, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="%s"' % nom,
        "X-Lignes": str(len(lignes)),
        "Cache-Control": "no-store",
    })
