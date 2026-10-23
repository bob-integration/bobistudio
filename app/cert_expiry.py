# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Échéance des certificats du plan de contrôle : la voir venir, et la repousser.

POURQUOI. Rien ne renouvelait ces certificats et rien ne prévenait. Relevé sur le banc le
2026-10-11 : contrôleur au 2028-10-06, nœuds en novembre 2028. Ce jour-là, le contrôleur ne peut
plus piloter AUCUN nœud (un agent refuse un client hors dates, le contrôleur refuse un agent hors
dates), la réplication HA tombe avec — et rien, avant, ne l'aurait dit.

TROIS SORTES DE CERTIFICATS, une même règle :
  · l'AUTORITÉ (ca.crt, dix ans) — son expiration invalide tout ; on alerte, on ne renouvelle
    pas ici (réémettre la CA impose de re-migrer chaque nœud : c'est le bouton « Réémettre ») ;
  · le CONTRÔLEUR — renouvelé sur place, même clé (`renouveler_controleur`) ;
  · chaque NŒUD — renouvelé par le flux d'enrôlement mTLS (`node_driver.rotate_tls`), clé gardée
    par l'agent, même un certificat DÉJÀ expiré.

PALIERS D'ALERTE (demande de l'exploitation, 2026-10-11) : une alerte à J-30, une à J-10, puis
EN CONTINU à partir de J-5 — répétée toutes les `REPETITION_S` tant que rien n'est fait, et au-delà
de l'échéance. Une alerte unique à J-5 se perdrait dans le flot ; une alerte à chaque tour de
boucle le noierait. L'état (palier atteint, dernière émission) est gardé PAR NUMÉRO DE SÉRIE : un
certificat renouvelé est un autre certificat, et ses paliers repartent de zéro sans rien effacer.

Les conteneurs ne sont PAS couverts : leur certificat est réémis à chaque recréation, et aucun ne
vit 825 jours sans redéploiement sur ce parc. Le certificat du STANDBY non plus : il est vérifié
par le standby lui-même quand il devient actif.
"""
import datetime
import logging
import threading
import time

log = logging.getLogger(__name__)

PALIERS = (30, 10)            # alertes uniques, en jours restants
CONTINU = 5                   # à partir de ce palier : répétée, jusqu'au renouvellement
REPETITION_S = 3600           # cadence de répétition en continu
PERIODE_S = 600               # cadence de vérification
CLE_ETAT = "cert_expiry_state"

_lock = threading.Lock()


def _maintenant():
    return datetime.datetime.now(datetime.timezone.utc)


def jours_restants(fin, maintenant=None):
    """Jours entiers restants (négatif = expiré depuis). Arrondi VERS LE BAS : à 29 j 23 h on est
    à J-29, pas à J-30 — mieux vaut alerter une heure trop tôt qu'un jour trop tard."""
    return int((fin - (maintenant or _maintenant())).total_seconds() // 86400)


def palier(jours):
    """Palier atteint pour `jours` restants : None, 30, 10, 5 (continu) ou 0 (expiré)."""
    if jours < 0:
        return 0
    if jours <= CONTINU:
        return CONTINU
    for p in sorted(PALIERS):
        if jours <= p:
            return p
    return None


def inventaire():
    """Tous les certificats surveillés : [{cle, type, nom, node_id, serie, fin}]. Aucun secret."""
    from . import ca
    from .database import db_get_nodes
    from cryptography import x509
    out = []
    if ca.ca_available():
        p = ca.paths()
        for typ, chemin in (("ca", p["ca_cert"]), ("controleur", p["ctrl_cert"])):
            try:
                with open(chemin, "rb") as f:
                    c = x509.load_pem_x509_certificate(f.read())
                out.append({"cle": typ, "type": typ, "nom": None, "node_id": None,
                            "serie": format(c.serial_number, "x"), "fin": c.not_valid_after_utc})
            except Exception as e:
                log.error("échéance : certificat %s illisible (%s)", typ, e)
    for n in db_get_nodes():
        pem = n.get("node_cert")
        if not pem or not n.get("tls_ready"):
            continue
        try:
            c = x509.load_pem_x509_certificate(pem.encode() if isinstance(pem, str) else pem)
        except Exception as e:
            log.error("échéance : certificat du nœud %s illisible (%s)", n.get("name"), e)
            continue
        out.append({"cle": "noeud:%s" % n["id"], "type": "noeud",
                     "nom": n.get("name") or "#%s" % n["id"], "node_id": n["id"],
                     "serie": format(c.serial_number, "x"), "fin": c.not_valid_after_utc})
    return out


def _alerter(cert, jours):
    from .database import db_add_alert
    expire = jours < 0
    # Clés LITTÉRALES, à portée de l'appel : tests/verif_cles_alertes.py ne voit que celles-là.
    cle = {("ca", False): "alert.securite.cert_ca_expire_bientot",
           ("ca", True): "alert.securite.cert_ca_expire",
           ("controleur", False): "alert.securite.cert_controleur_expire_bientot",
           ("controleur", True): "alert.securite.cert_controleur_expire",
           ("noeud", False): "alert.securite.cert_noeud_expire_bientot",
           ("noeud", True): "alert.securite.cert_noeud_expire"}[(cert["type"], expire)]
    niveau = "error" if jours <= CONTINU else "warning"
    params = {"date": cert["fin"].date().isoformat(), "j": abs(jours)}
    # `antirebond=False` : la cadence est DÉJÀ décidée ici (paliers, puis REPETITION_S). Laisser
    # l'anti-rebond général s'en mêler, c'est dépendre d'un réglage (`alerts_antirebond_s`) qui,
    # porté au-delà d'une heure, avalerait en silence l'alerte continue des cinq derniers jours.
    if cert["type"] == "noeud":
        params["n"] = cert["nom"]
        db_add_alert(cle, niveau, node_id=cert["node_id"], kind="node", params=params,
                     antirebond=False)
    else:
        # Contrôleur et autorité : la confiance de tout le plan de contrôle. Piste d'audit.
        db_add_alert(cle, niveau, kind="auth", params=params, antirebond=False)


def verifier(maintenant=None, horloge=None):
    """Un passage : émet les alertes dues. → liste des (cle, palier) émis (pour les gardes).

    `maintenant` (datetime) et `horloge` (secondes) sont injectables : on n'attend pas 30 jours
    pour éprouver la règle."""
    from .database import db_get_setting, db_set_setting
    maintenant = maintenant or _maintenant()
    horloge = time.time() if horloge is None else horloge
    emis = []
    with _lock:
        etat = db_get_setting(CLE_ETAT, {}) or {}
        vus = set()
        for cert in inventaire():
            vus.add(cert["cle"])
            j = jours_restants(cert["fin"], maintenant)
            p = palier(j)
            prec = etat.get(cert["cle"]) or {}
            if prec.get("serie") != cert["serie"]:
                prec = {}                       # renouvelé : un autre certificat, paliers à zéro
            if p is None:
                etat[cert["cle"]] = {"serie": cert["serie"]}
                continue
            deja = prec.get("palier")
            nouveau = deja is None or p < deja
            repete = p <= CONTINU and horloge - float(prec.get("ts") or 0) >= REPETITION_S
            if nouveau or repete:
                try:
                    _alerter(cert, j)
                    emis.append((cert["cle"], p))
                    prec = {"serie": cert["serie"], "palier": p, "ts": horloge}
                except Exception as e:
                    log.error("échéance : alerte non enregistrée pour %s (%s)", cert["cle"], e)
            etat[cert["cle"]] = prec or {"serie": cert["serie"]}
        for k in [k for k in etat if k not in vus]:
            del etat[k]                         # nœud supprimé : on n'en garde pas la trace
        db_set_setting(CLE_ETAT, etat)
    return emis


def etat_pour_ui(maintenant=None):
    """{cle: {fin (iso), jours, palier}} — ce que l'onglet Sécurité affiche."""
    maintenant = maintenant or _maintenant()
    out = {}
    for c in inventaire():
        j = jours_restants(c["fin"], maintenant)
        out[c["cle"]] = {"fin": c["fin"].isoformat(), "jours": j, "palier": palier(j)}
    return out


def renouveler_controleur():
    """Nouveau certificat pour CE contrôleur : même clé, mêmes adresses (IP virtuelle comprise),
    durée de vie neuve. Les écoutes servent le nouveau certificat sans redémarrage. → fin (iso)."""
    from . import ca, vip
    v = vip.ip_certificat()
    ca.assurer_certificat_controleur(ips=[v] if v else (), renouveler=True)
    vip.recharger_ecoutes()
    with open(ca.paths()["ctrl_cert"], "rb") as f:
        return ca.fin_validite(f.read()).isoformat()


def _boucle():
    while True:
        try:
            verifier()
        except Exception as e:
            log.error("échéance des certificats : vérification en échec (%s)", e)
        time.sleep(PERIODE_S)


def demarrer():
    """Lancé par main.py sur le contrôleur ACTIF (c'est lui qui pilote, et qui alerte)."""
    threading.Thread(target=_boucle, daemon=True, name="cert-expiry").start()
