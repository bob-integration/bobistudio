# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Appairage d'une paire HA : faire partager à deux contrôleurs la MÊME autorité (CA).

POURQUOI. Depuis la migration mTLS du plan de contrôle (2026-08-30), un agent-nœud exige un
certificat client signé par la CA qu'il a reçue à l'enrôlement — celle de l'actif. Or rien ne la
transmettait au standby : la réplication ne pousse que la base SQLite, la CA vit dans `tls/`, et
ni la sauvegarde, ni la bascule, ni HA.md ne la mentionnaient. Vérifié le 2026-10-10 en simulant
un agent réglé comme le vrai : l'actif est accepté, le standby promu est refusé dans les DEUX sens
(`certificate verify failed`, puis `tlsv1 alert unknown ca`). Un standby promu ne pouvait donc
piloter AUCUN nœud — et la bascule de bout en bout n'avait jamais été éprouvée.

LE PROBLÈME DE L'ŒUF ET DE LA POULE. La CA doit voyager vers le standby, et c'est justement elle
qui permettrait d'établir un canal sûr. On le brise par UN geste humain, une seule fois :

  1. sur l'actif, « Appairer un standby » affiche un CODE à usage unique (160 bits, 10 minutes) ;
  2. l'exploitant le saisit sur le standby, avec l'adresse de l'actif ;
  3. le standby génère SA clé (elle ne quitte jamais la machine) et envoie une demande : son CSR,
     un aléa, et une PREUVE qu'il connaît le code — un HMAC, pas le code ;
  4. l'actif vérifie la preuve, signe le CSR avec le marqueur de contrôleur, et renvoie la CA
     (certificat ET clé) avec ce certificat, CHIFFRÉS par AES-GCM sous une clé dérivée du code,
     plus sa propre preuve — le standby authentifie l'actif autant que l'inverse.

LE CODE NE PASSE JAMAIS SUR LE RÉSEAU. Qui écoute voit une demande et un paquet chiffré, et ne
peut ni lire la CA, ni forger une réponse, ni rejouer la demande (le code est brûlé au premier
succès, et après 5 preuves fausses). C'est l'humain qui fait le pont, sur le seul canal que
l'attaquant ne voit pas : ses yeux et son clavier.

Pourquoi partager LA CA plutôt que faire confiance à deux CA : le standby reçoit DÉJÀ tous les
secrets de l'actif (c'est sa raison d'être) et doit pouvoir faire tout ce que fait l'actif — y
compris enrôler un nœud ou renouveler un certificat après une bascule. Deux CA auraient exigé de
redéployer chaque agent-nœud pour qu'il accepte une liste.

Les fonctions de chiffrement sont PURES (matériel passé en argument) pour être éprouvées sans
réseau ni seconde machine — cf. tests/verif_ha_appairage.py.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

VERSION = 1
SEL = b"bobi-ha-appairage/v1"
TTL_S = 600                 # 10 minutes : le temps d'aller d'une console à l'autre, pas plus
ECHECS_MAX = 5              # au-delà, le code est brûlé (et ça se journalise)
OCTETS_CODE = 20            # 160 bits — hors de portée même sans limite d'essais

_lock = threading.Lock()
_en_cours = None            # côté ACTIF : {"code", "expire", "echecs"} — en mémoire seulement


# ─── Le code ─────────────────────────────────────────────────────────────────

def normaliser(code):
    """Insensible à la casse, aux tirets et aux espaces : un code se recopie à la main."""
    return "".join(c for c in str(code or "").upper() if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def nouveau_code():
    brut = base64.b32encode(secrets.token_bytes(OCTETS_CODE)).decode().rstrip("=")
    return "-".join(brut[i:i + 4] for i in range(0, len(brut), 4))


def _cles(code):
    """(clé d'authentification, clé de chiffrement), dérivées séparément du code par HKDF."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    brut = normaliser(code).encode()
    if len(brut) < 16:
        raise ValueError("code d'appairage trop court")

    def deriver(info):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=SEL, info=info).derive(brut)
    return deriver(b"auth"), deriver(b"enc")


def _mac(cle, *parts):
    return hmac.new(cle, b"|".join(parts), hashlib.sha256).hexdigest()


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest().encode()


# ─── Fonctions PURES (éprouvées sans réseau) ─────────────────────────────────

def preparer_demande(code):
    """Côté STANDBY. → (demande à envoyer, clé privée PEM à garder)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    cle = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bobi-controller")]))
           .sign(cle, hashes.SHA256()))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()
    k_auth, _ = _cles(code)
    nonce = secrets.token_hex(16)
    demande = {"v": VERSION, "nonce": nonce, "csr": csr_pem,
               "preuve": _mac(k_auth, b"demande", nonce.encode(), _sha(csr_pem))}
    cle_pem = cle.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return demande, cle_pem


def verifier_demande(code, demande):
    """Côté ACTIF. Lève ValueError si la preuve est fausse (comparaison à temps constant)."""
    if not isinstance(demande, dict) or demande.get("v") != VERSION:
        raise ValueError("demande d'appairage illisible ou d'une autre version")
    k_auth, _ = _cles(code)
    attendue = _mac(k_auth, b"demande", str(demande.get("nonce", "")).encode(),
                    _sha(str(demande.get("csr", ""))))
    if not hmac.compare_digest(attendue, str(demande.get("preuve", ""))):
        raise ValueError("preuve d'appairage invalide (code erroné ?)")


def sceller(code, nonce_demande, paquet):
    """Côté ACTIF. Chiffre le paquet (dict) et y joint la preuve de l'actif."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k_auth, k_enc = _cles(code)
    iv = secrets.token_bytes(12)
    # Données associées : l'aléa de LA demande. Un paquet ne peut donc pas être resservi à une
    # autre demande — il ne se déchiffrerait pas.
    aad = SEL + b"|" + str(nonce_demande).encode()
    chiffre = AESGCM(k_enc).encrypt(iv, json.dumps(paquet).encode(), aad)
    b64 = base64.b64encode(iv + chiffre).decode()
    return {"v": VERSION, "paquet": b64,
            "preuve": _mac(k_auth, b"reponse", str(nonce_demande).encode(), _sha(b64))}


def ouvrir(code, nonce_demande, reponse):
    """Côté STANDBY. Vérifie la preuve de l'actif PUIS déchiffre. Lève ValueError sinon."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(reponse, dict) or reponse.get("v") != VERSION:
        raise ValueError("réponse d'appairage illisible ou d'une autre version")
    k_auth, k_enc = _cles(code)
    b64 = str(reponse.get("paquet", ""))
    attendue = _mac(k_auth, b"reponse", str(nonce_demande).encode(), _sha(b64))
    if not hmac.compare_digest(attendue, str(reponse.get("preuve", ""))):
        # Ce n'est pas l'actif qui a répondu, ou pas avec ce code : on n'ouvre RIEN.
        raise ValueError("l'interlocuteur n'a pas prouvé connaître le code — ce n'est pas "
                         "l'actif attendu")
    brut = base64.b64decode(b64)
    try:
        clair = AESGCM(k_enc).decrypt(brut[:12], brut[12:],
                                      SEL + b"|" + str(nonce_demande).encode())
    except InvalidTag:
        raise ValueError("paquet d'appairage altéré en route")
    return json.loads(clair)


# ─── Côté ACTIF : le code en cours ───────────────────────────────────────────

def demarrer():
    """Ouvre un appairage : nouveau code (l'éventuel précédent est abandonné). → (code, expire)."""
    global _en_cours
    from . import ca
    if not ca.ca_available():
        raise RuntimeError("ce contrôleur n'a pas d'autorité (tls/) — rien à partager")
    # L'actif doit porter lui-même le marqueur avant que l'autre ne le vérifie (et l'IP virtuelle,
    # tant qu'on y est — avec rechargement des écoutes, cf. vip.synchroniser_certificat).
    from . import vip
    vip.synchroniser_certificat()
    code = nouveau_code()
    with _lock:
        _en_cours = {"code": code, "expire": time.time() + TTL_S, "echecs": 0}
    return code, int(_en_cours["expire"])


def annuler():
    global _en_cours
    with _lock:
        _en_cours = None


def etat_actif():
    with _lock:
        if not _en_cours or time.time() > _en_cours["expire"]:
            return {"en_cours": False}
        return {"en_cours": True, "expire": int(_en_cours["expire"]),
                "restant_s": int(_en_cours["expire"] - time.time())}


def traiter_demande(demande, ip_pair):
    """Côté ACTIF, appelé par la route publique `/api/ha/pairing/claim`.

    → réponse scellée. Lève PermissionError (pas d'appairage ouvert, expiré, brûlé) ou
    ValueError (preuve fausse — compte un échec)."""
    global _en_cours
    from . import ca
    from .database import db_add_alert
    with _lock:
        if not _en_cours:
            raise PermissionError("aucun appairage ouvert sur l'actif")
        if time.time() > _en_cours["expire"]:
            _en_cours = None
            raise PermissionError("code d'appairage expiré — en générer un nouveau")
        code = _en_cours["code"]
        try:
            verifier_demande(code, demande)
        except ValueError:
            _en_cours["echecs"] += 1
            if _en_cours["echecs"] >= ECHECS_MAX:
                _en_cours = None
                db_add_alert("alert.securite.ha_appairage_brule", "warning", kind="auth",
                             params={"ip": ip_pair or "?", "n": ECHECS_MAX})
            raise
        _en_cours = None          # BRÛLÉ au premier succès : une demande captée ne se rejoue pas
    # L'IP virtuelle suit le pilotage : le standby la portera s'il est promu.
    from . import vip
    v = vip.ip_certificat()
    cert = ca.signer_controleur(demande["csr"], ip_pair, ips_sup=[v] if v else ())
    p = ca.paths()
    with open(p["ca_cert"], "rb") as f:
        ca_cert = f.read().decode()
    with open(p["ca_key"], "rb") as f:
        ca_key = f.read().decode()
    with open(p["ctrl_cert"], "rb") as f:
        actif_cert = f.read().decode()
    paquet = {"ca_cert": ca_cert, "ca_key": ca_key,
              "cert": cert.decode() if isinstance(cert, bytes) else cert,
              "actif_cert": actif_cert}
    db_add_alert("alert.securite.ha_appairage_ok", "warning", kind="auth",
                 params={"ip": ip_pair or "?", "empreinte": empreinte_ca(ca_cert)})
    return sceller(code, demande["nonce"], paquet)


def empreinte_ca(ca_cert_pem):
    """Empreinte courte et LISIBLE de la CA, pour que l'exploitant compare les deux écrans."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    c = x509.load_pem_x509_certificate(ca_cert_pem.encode() if isinstance(ca_cert_pem, str)
                                       else ca_cert_pem)
    h = c.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(h[i:i + 4] for i in range(0, 24, 4))


# ─── Côté STANDBY : rejoindre ────────────────────────────────────────────────

def installer(paquet, cle_pem, tls_dir=None):
    """Côté STANDBY. Remplace l'autorité locale par celle de l'actif, avec SA propre clé.

    L'ancien `tls/` est GARDÉ (tls.avant-appairage-<horodatage>) : c'est un geste qui change
    l'identité de la machine, il doit pouvoir se défaire. Chaque fichier est écrit par
    remplacement atomique. Vérifie AVANT d'écrire que le certificat reçu porte le marqueur,
    chaîne à la CA reçue, et correspond à la clé gardée ici."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from . import ca
    ca_c = x509.load_pem_x509_certificate(paquet["ca_cert"].encode())
    cert = x509.load_pem_x509_certificate(paquet["cert"].encode())
    cle = serialization.load_pem_private_key(cle_pem, password=None)
    if not ca.est_controleur(cert):
        raise ValueError("le certificat reçu ne porte pas le marqueur de contrôleur")
    if cert.public_key().public_numbers() != cle.public_key().public_numbers():
        raise ValueError("le certificat reçu ne correspond pas à la clé de ce contrôleur")
    try:
        cert.verify_directly_issued_by(ca_c)     # quel que soit le type de clé de la CA
    except Exception as e:
        raise ValueError("le certificat reçu n'est pas signé par la CA reçue (%s)" % e)
    p = ca.paths() if tls_dir is None else {
        "ca_cert": os.path.join(tls_dir, "ca.crt"), "ca_key": os.path.join(tls_dir, "ca.key"),
        "ctrl_cert": os.path.join(tls_dir, "controller.crt"),
        "ctrl_key": os.path.join(tls_dir, "controller.key")}
    d = os.path.dirname(p["ca_cert"])
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.listdir(d):
        sauve = "%s.avant-appairage-%s" % (d.rstrip("/"), datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copytree(d, sauve)
        os.chmod(sauve, 0o700)
        log.warning("appairage : ancienne autorité gardée dans %s", sauve)
    with ca._lock:
        ca._ecrire_atomique(p["ca_cert"], paquet["ca_cert"].encode(), 0o644)
        ca._ecrire_atomique(p["ca_key"], paquet["ca_key"].encode(), 0o600)
        ca._ecrire_atomique(p["ctrl_cert"], paquet["cert"].encode(), 0o644)
        ca._ecrire_atomique(p["ctrl_key"], cle_pem, 0o600)
    return empreinte_ca(paquet["ca_cert"])


def rejoindre(url_actif, code, envoyer=None, tls_dir=None):
    """Côté STANDBY, de bout en bout. `envoyer(url, demande) -> réponse` est injectable pour la
    garde ; par défaut un POST JSON en HTTP — légitimement en clair : tout ce qui y passe est soit
    public (CSR, aléa), soit une preuve, soit chiffré."""
    from .database import db_add_alert
    demande, cle_pem = preparer_demande(code)
    url = (url_actif or "").strip().rstrip("/")
    if not url:
        raise ValueError("adresse de l'actif manquante")
    if "://" not in url:
        url = "http://" + url
    reponse = (envoyer or _envoyer_http)(url + "/api/ha/pairing/claim", demande)
    paquet = ouvrir(code, demande["nonce"], reponse)
    empreinte = installer(paquet, cle_pem, tls_dir=tls_dir)
    db_add_alert("alert.securite.ha_appairage_rejoint", "warning", kind="auth",
                 params={"actif": url, "empreinte": empreinte})
    return empreinte, url


def _envoyer_http(url, demande):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(demande).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:      # noqa: S310 (voir docstring)
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode() or "{}").get("error")
        except Exception:
            msg = None
        raise ValueError("l'actif a refusé : %s" % (msg or "HTTP %s" % e.code))
