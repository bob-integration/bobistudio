# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""CA interne du plan de contrôle (mTLS).

Le contrôleur porte une autorité de certification privée. À l'enrôlement, chaque nœud
génère sa paire de clés *localement* (la clé privée ne quitte jamais le nœud), envoie un
CSR, et le contrôleur le signe (`sign_csr`). Les conteneurs, éphémères et recréés à volonté,
reçoivent un cert généré par le contrôleur (`generate_leaf`) injecté au `docker run`.

Matériel de CA : fichiers dans `config.TLS_DIR` (droits 600, hors DB, hors git), généré une
fois par `tools/ca-init.py` :
    ca.crt / ca.key                 ← racine (la clé signe tout ; NE JAMAIS distribuer)
    controller.crt / controller.key ← cert du contrôleur (serveur enroll/heartbeat/HA
                                       ET client quand il pilote un agent)

HA : répliquer TLS_DIR (au moins ca.key) sur le contrôleur standby pour qu'il puisse
re-signer après un failover — hors snapshot SQLite.

Le cert émis porte serverAuth + clientAuth : le même cert sert de cert serveur (l'agent qui
écoute) ET de cert client (quand l'entité initie une connexion). Les SAN sont fixés par le
CONTRÔLEUR (jamais recopiés aveuglément du CSR) : IP de contrôle + URI d'identité
`bobi://node/<id>`.
"""

import datetime
import ipaddress
import logging
import os
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import config

log = logging.getLogger(__name__)

_CA_CN   = "Bobi.Studio Internal CA"
_LEAF_DAYS_DEFAULT = 825          # < 825 j : borne CA/Browser, sans objet en interne mais raisonnable
_lock = threading.Lock()          # sérialise l'accès disque à la CA (numéro de série, écritures)


# ── Chemins ───────────────────────────────────────────────────────────────────
def _dir():
    return getattr(config, "TLS_DIR", "/opt/bobistudio/tls")


def paths():
    d = _dir()
    return {
        "ca_cert":   os.path.join(d, "ca.crt"),
        "ca_key":    os.path.join(d, "ca.key"),
        "ctrl_cert": os.path.join(d, "controller.crt"),
        "ctrl_key":  os.path.join(d, "controller.key"),
    }


def ca_available():
    """True si la CA racine ET le cert contrôleur sont présents (mTLS activable)."""
    p = paths()
    return all(os.path.exists(p[k]) for k in ("ca_cert", "ca_key", "ctrl_cert", "ctrl_key"))


def ca_cert_pem():
    """PEM de la CA racine (public — sûr à distribuer aux agents pour le trust)."""
    with open(paths()["ca_cert"], "rb") as f:
        return f.read()


def ca_info():
    """État de la CA pour l'UI (aucun secret). {available: False} si non initialisée."""
    if not ca_available():
        return {"available": False}
    try:
        with open(paths()["ca_cert"], "rb") as f:
            root = x509.load_pem_x509_certificate(f.read())
        with open(paths()["ctrl_cert"], "rb") as f:
            ctrl = x509.load_pem_x509_certificate(f.read())
        fp = root.fingerprint(hashes.SHA256()).hex()
        fp = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
        ctrl_sans = []
        try:
            ext = ctrl.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ctrl_sans = [str(g.value) for g in ext]
        except x509.ExtensionNotFound:
            pass
        return {
            "available": True,
            "subject": root.subject.rfc4514_string(),
            "fingerprint_sha256": fp,
            "not_before": root.not_valid_before_utc.isoformat(),
            "not_after": root.not_valid_after_utc.isoformat(),
            "controller_sans": ctrl_sans,
            "dir": _dir(),
        }
    except Exception as e:
        return {"available": True, "error": str(e)}


# ── Helpers internes ──────────────────────────────────────────────────────────
def _load_ca():
    p = paths()
    with open(p["ca_cert"], "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    with open(p["ca_key"], "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    return cert, key


def _san_list(ip=None, node_id=None, uri=None, dns=None):
    """Construit les SAN à PARTIR DE NOS PARAMÈTRES (jamais du CSR client)."""
    sans = []
    if dns:
        sans.append(x509.DNSName(str(dns)))
    if ip:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(str(ip))))
        except ValueError:
            pass  # une valeur non-IP est ignorée plutôt que de faire échouer la signature
    if uri:
        sans.append(x509.UniformResourceIdentifier(str(uri)))
    elif node_id is not None:
        sans.append(x509.UniformResourceIdentifier(f"bobi://node/{node_id}"))
    return sans


def _pem_private(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _sign(builder, ca_key):
    return builder.sign(private_key=ca_key, algorithm=hashes.SHA256())


def _leaf_builder(subject_cn, public_key, ca_cert, sans, days):
    now = datetime.datetime.now(datetime.timezone.utc)
    b = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(subject_cn))]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))   # tolérance de dérive d'horloge
        .not_valid_after(now + datetime.timedelta(days=int(days)))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        # SKI/AKI : requis par la validation stricte d'OpenSSL (chaîne RFC 5280).
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
    )
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return b


# ── API d'émission ────────────────────────────────────────────────────────────
def sign_csr(csr_pem, *, common_name=None, ip=None, node_id=None, uri=None,
             days=_LEAF_DAYS_DEFAULT, ips_sup=()):
    """Signe un CSR (nœud). La clé publique vient du CSR ; les SAN sont FIXÉS par nous.
    `common_name` par défaut = CN du CSR — sauf celui du contrôleur, refusé. → cert PEM (bytes)."""
    csr = x509.load_pem_x509_csr(csr_pem if isinstance(csr_pem, bytes) else csr_pem.encode())
    if not csr.is_signature_valid:
        raise ValueError("signature de CSR invalide")
    if common_name is None:
        try:
            common_name = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except (IndexError, x509.ExtensionNotFound):
            common_name = f"node-{node_id}" if node_id is not None else "bobi-leaf"
        # Le CN du contrôleur ne se DEMANDE pas : l'agent par-conteneur (:8081) et le repli des
        # agents-nœuds d'avant le marqueur le prennent pour une identité. Le seul chemin légitime
        # qui l'émet le fixe lui-même (`signer_controleur`, `common_name` explicite).
        if str(common_name).strip().lower() == "bobi-controller":
            raise ValueError("CSR refusé : CN « bobi-controller » réservé au contrôleur")
    with _lock:
        ca_cert, ca_key = _load_ca()
        sans = _san_list(ip=ip, node_id=node_id, uri=uri)
        for x in (_ip_san(i) for i in (ips_sup or ()) if i):
            if x is not None and x not in sans:
                sans.append(x)
        cert = _sign(_leaf_builder(common_name, csr.public_key(), ca_cert, sans, days), ca_key)
    return cert.public_bytes(serialization.Encoding.PEM)


def generate_leaf(common_name, *, ip=None, node_id=None, uri=None, days=_LEAF_DAYS_DEFAULT):
    """Génère une paire + un cert signé (conteneurs : le contrôleur produit tout et injecte).
    Retourne (cert_pem, key_pem) en bytes."""
    key = ec.generate_private_key(ec.SECP256R1())
    with _lock:
        ca_cert, ca_key = _load_ca()
        sans = _san_list(ip=ip, node_id=node_id, uri=uri)
        cert = _sign(_leaf_builder(common_name, key.public_key(), ca_cert, sans, days), ca_key)
    return cert.public_bytes(serialization.Encoding.PEM), _pem_private(key)


# ── Identité de CONTRÔLEUR ────────────────────────────────────────────────────
# Une même CA signe TROIS sortes de certificats : contrôleurs, nœuds (`bobi://node/<id>`) et
# conteneurs (`bobi://container/<vmid>`) — et les conteneurs exécutent du code de plugin TIERS.
# « Signé par notre CA » ne dit donc PAS « c'est un contrôleur ». Le CN non plus : `sign_csr` reprend
# celui du CSR, un nœud peut demander `bobi-controller` et l'obtenir. Ce qui distingue un
# contrôleur, c'est une URI de SAN que SEUL le code du contrôleur émet — les trois chemins
# d'émission fixent eux-mêmes leur URI, aucun ne la prend d'un client (relevé le 2026-10-10).
# C'est sur ce marqueur que la réplication HA décide d'accepter un pair : sans lui, n'importe quel
# conteneur compromis pourrait pousser une base au standby.
MARQUEUR_CONTROLEUR = "bobi://controller/"


def uris_san(cert):
    """Les URI du SAN d'un certificat (objet x509 ou PEM)."""
    if isinstance(cert, (bytes, str)):
        cert = x509.load_pem_x509_certificate(cert if isinstance(cert, bytes) else cert.encode())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [str(u) for u in san.get_values_for_type(x509.UniformResourceIdentifier)]


def est_controleur(cert):
    return any(u.startswith(MARQUEUR_CONTROLEUR) for u in uris_san(cert))


def _ecrire_atomique(path, data, mode):
    """Comme `_write`, mais sans fenêtre où le fichier est tronqué : on écrit à côté puis on
    remplace. Pour une clé privée, une coupure au mauvais moment ne doit pas laisser un demi-PEM."""
    tmp = path + ".tmp"
    _write(tmp, data, mode)
    os.replace(tmp, path)


def _ip_san(valeur):
    """`x509.IPAddress` d'une IP (un masque CIDR est ignoré), ou None si ce n'est pas une IP."""
    try:
        return x509.IPAddress(ipaddress.ip_address(str(valeur).split("/")[0].strip()))
    except ValueError:
        return None


def fin_validite(cert):
    """Date d'expiration (datetime UTC) d'un certificat PEM (bytes/str) ou déjà chargé."""
    if isinstance(cert, str):
        cert = cert.encode()
    if isinstance(cert, bytes):
        cert = x509.load_pem_x509_certificate(cert)
    return cert.not_valid_after_utc


def assurer_certificat_controleur(ips=(), ident=None, renouveler=False):
    """Ré-émet le certificat de CE contrôleur s'il lui manque le marqueur OU l'une des `ips`
    (l'IP virtuelle HA, cf. app/vip.py:synchroniser_certificat). → True si ré-émis.

    Même clé, mêmes SAN, plus ce qui manque : rien ne change pour les nœuds, qui vérifient la
    chaîne et le rôle, jamais l'adresse. AJOUT SEULEMENT — une IP virtuelle abandonnée reste dans
    le certificat : elle n'ouvre rien (il faudrait aussi notre clé), et la retirer exigerait de
    savoir laquelle des IP était virtuelle. Durée de vie conservée. L'ancien certificat est gardé
    (`.avant-marqueur` à la première pose du marqueur, `.precedent` ensuite).

    `renouveler=True` : ré-émet en tout cas, avec une durée de vie NEUVE (`_LEAF_DAYS_DEFAULT`)
    — c'est le renouvellement avant expiration (cf. app/cert_expiry.py). Même clé : les agents
    ne voient qu'une nouvelle échéance."""
    p = paths()
    voulues = [x for x in (_ip_san(i) for i in (ips or ()) if i) if x is not None]
    with _lock:
        with open(p["ctrl_cert"], "rb") as f:
            ancien = x509.load_pem_x509_certificate(f.read())
        try:
            sans = list(ancien.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value)
        except x509.ExtensionNotFound:
            sans = []
        marque = est_controleur(ancien)
        manquantes = [v for v in voulues if v not in sans]
        if marque and not manquantes and not renouveler:
            return False
        with open(p["ctrl_key"], "rb") as f:
            cle = serialization.load_pem_private_key(f.read(), password=None)
        ca_cert, ca_key = _load_ca()
        if not marque:
            ip = next((str(x.value) for x in sans if isinstance(x, x509.IPAddress)), None)
            sans.append(x509.UniformResourceIdentifier(
                MARQUEUR_CONTROLEUR + (ident or ip or "local")))
        sans.extend(manquantes)
        jours = _LEAF_DAYS_DEFAULT if renouveler else max(1, (
            ancien.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)).days)
        neuf = _sign(_leaf_builder("bobi-controller", cle.public_key(), ca_cert, sans, jours), ca_key)
        _write(p["ctrl_cert"] + (".precedent" if marque else ".avant-marqueur"),
               ancien.public_bytes(serialization.Encoding.PEM), 0o644)
        _ecrire_atomique(p["ctrl_cert"], neuf.public_bytes(serialization.Encoding.PEM), 0o644)
    log.info("certificat du contrôleur ré-émis%s%s%s",
             " (RENOUVELÉ, %d jours)" % jours if renouveler else "",
             "" if marque else " avec le marqueur " + MARQUEUR_CONTROLEUR,
             (" ; IP ajoutée(s) : " + ", ".join(str(m.value) for m in manquantes))
             if manquantes else "")
    return True


def assurer_marqueur_controleur(ident=None):
    """Pose le marqueur seul (cf. assurer_certificat_controleur). → True si ré-émis."""
    return assurer_certificat_controleur(ident=ident)


def signer_controleur(csr_pem, ip, ips_sup=()):
    """Certificat d'un AUTRE contrôleur (le standby, à l'appairage). CN et marqueur fixés ici,
    jamais pris du CSR. `ips_sup` : l'IP virtuelle, qu'il portera s'il est promu."""
    return sign_csr(csr_pem, common_name="bobi-controller", ip=ip, ips_sup=ips_sup,
                    uri=MARQUEUR_CONTROLEUR + (ip or "pair"))


# ── Contextes SSL (côté contrôleur) ───────────────────────────────────────────
def controller_client_context():
    """Contexte pour les connexions SORTANTES du contrôleur vers un agent (urllib.urlopen
    context=…). Vérifie le pair contre la CA et présente le cert client du contrôleur."""
    p = paths()
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=p["ca_cert"])
    ctx.load_cert_chain(certfile=p["ctrl_cert"], keyfile=p["ctrl_key"])
    return ctx


def controller_client_files():
    """(cert, key, ca) chemins — pour `requests` (verify=ca, cert=(cert,key))."""
    p = paths()
    return p["ctrl_cert"], p["ctrl_key"], p["ca_cert"]


def server_ssl_context(cert_path=None, key_path=None, *, require_client_cert=True):
    """Contexte pour un serveur ENTRANT (endpoints enroll/heartbeat/HA du contrôleur, et
    réutilisable côté agent). Par défaut exige un cert client signé par la CA (mTLS)."""
    p = paths()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=p["ca_cert"])
    ctx.load_cert_chain(certfile=cert_path or p["ctrl_cert"], keyfile=key_path or p["ctrl_key"])
    if require_client_cert:
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


# ── Création du matériel (tools/ca-init.py) ───────────────────────────────────
def create_ca_material(controller_sans=None, *, overwrite=False, days_ca=3650,
                       days_controller=_LEAF_DAYS_DEFAULT):
    """Génère la CA racine + le cert contrôleur dans TLS_DIR. Idempotent : ne réécrit rien
    sauf `overwrite=True`. `controller_sans` = liste d'IP et/ou d'hôtes (VIP HA incluse) que
    les nœuds utiliseront pour joindre le contrôleur. Retourne la liste des fichiers écrits."""
    d = _dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = paths()
    if ca_available() and not overwrite:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    # CA racine (EC P-384).
    ca_key = ec.generate_private_key(ec.SECP384R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CA_CN)])
    ca_cert = _sign(
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=int(days_ca)))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, key_encipherment=False, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False),
        ca_key,
    )

    # Cert contrôleur (serveur + client), signé par la CA fraîche.
    sans = []
    for s in (controller_sans or []):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(str(s))))
        except ValueError:
            sans.append(x509.DNSName(str(s)))
    # Le marqueur dès la naissance : sans lui, ce contrôleur ne pourrait pas servir de pair HA
    # (cf. MARQUEUR_CONTROLEUR) avant d'avoir été ré-émis par `assurer_marqueur_controleur`.
    sans.append(x509.UniformResourceIdentifier(
        MARQUEUR_CONTROLEUR + (str(controller_sans[0]) if controller_sans else "local")))
    ctrl_key = ec.generate_private_key(ec.SECP256R1())
    ctrl_cert = _sign(_leaf_builder("bobi-controller", ctrl_key.public_key(), ca_cert,
                                    sans, days_controller), ca_key)

    written = []
    _write(p["ca_cert"], ca_cert.public_bytes(serialization.Encoding.PEM), 0o644); written.append(p["ca_cert"])
    _write(p["ca_key"], _pem_private(ca_key), 0o600); written.append(p["ca_key"])
    _write(p["ctrl_cert"], ctrl_cert.public_bytes(serialization.Encoding.PEM), 0o644); written.append(p["ctrl_cert"])
    _write(p["ctrl_key"], _pem_private(ctrl_key), 0o600); written.append(p["ctrl_key"])
    return written


def _detect_control_ip():
    """IP de contrôle (source de la route par défaut) — pour le SAN du cert contrôleur."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def ensure_ca(controller_sans=None):
    """Auto-initialise la CA au démarrage si absente (install neuve). À N'APPELER QUE sur le
    contrôleur ACTIF : en HA, deux contrôleurs qui s'auto-initialisent généreraient des CA
    DIFFÉRENTES → confiance cassée. Le standby ne génère rien ; il reçoit TLS_DIR par réplication
    out-of-band. Idempotent (no-op si déjà présente). Retourne True si une CA a été créée."""
    if ca_available():
        return False
    sans = list(controller_sans or [])
    if not sans:
        ip = _detect_control_ip()
        if ip:
            sans.append(ip)
    create_ca_material(controller_sans=sans)
    return True


def _write(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode)   # force le mode même si le fichier préexistait
