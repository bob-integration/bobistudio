#!/usr/bin/env python3
"""The transit area, and what the operator is told when it runs out.

Waitress writes the WHOLE request body into TMPDIR before the application sees it. On a stock
Debian TMPDIR is /tmp, a tmpfs — RAM. Measured 2026-11-13 on a test install: 3.8 GiB of RAM, /tmp
a 2 GiB tmpfs, a declared request-body cap of 4 GiB and a driver-upload cap of 2 GiB. Nothing ever
compared a declared cap to the place the bytes had to land. A 2.09 GB archive passed every cap,
filled the tmpfs 38 MB before the end, died, and left its buffer open — so /tmp stayed full and
every later request with a body failed. The operator was importing a plugin and saw « HTTP 500 ».

This guard holds three things in place:
  - a directory in RAM is never chosen as transit space;
  - a cap is clamped to what is MEASURED, not to what other caps declare;
  - an out-of-space failure NAMES itself, on any route, and says it is probably unrelated to what
    the operator just did.
"""
import io
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
# Throwaway database, set BEFORE the first access: the connection is kept per thread,
# so overriding DB_PATH afterwards isolates nothing.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)

from app import spool, http_errors                                            # noqa: E402

_echecs = []


def dire(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, (" — " + detail) if detail else ""))
    if not ok:
        _echecs.append(titre)


def _app_essai():
    """A minimal Flask app with the handlers registered, and routes that fail on purpose."""
    from flask import Flask
    a = Flask(__name__)
    http_errors.register(a)

    @a.route("/api/boum")
    def _boum():
        raise RuntimeError("quelque chose")

    @a.route("/api/plein")
    def _plein():
        raise OSError(28, "No space left on device")

    @a.route("/api/enveloppe")
    def _enveloppe():
        try:
            raise OSError(28, "No space left on device")
        except OSError as e:
            raise RuntimeError("relayé") from e

    return a


print("1. Un répertoire en RAM n'est jamais choisi comme espace de transit")
vrai_fstype = spool._mount_fstype
try:
    spool._mount_fstype = lambda p: "tmpfs" if "/en-ram" in p else "ext4"
    tmp = tempfile.mkdtemp(prefix="verif-transit-")
    ok, pourquoi = spool._usable(os.path.join(tmp, "en-ram"), 0)
    dire(not ok and "RAM" in pourquoi, "un tmpfs est refusé", pourquoi)
    ok, pourquoi = spool._usable(os.path.join(tmp, "sur-disque"), 0)
    dire(ok, "un système de fichiers durable est accepté", pourquoi)
    # Et il doit avoir la PLACE : un plafond déclaré au-dessus du libre est le défaut mesuré.
    ok, pourquoi = spool._usable(os.path.join(tmp, "sur-disque"), 1 << 62)
    dire(not ok and "needed" in pourquoi, "trop petit pour ce qui est demandé → refusé", pourquoi)
finally:
    spool._mount_fstype = vrai_fstype

print("\n2. `configure` pose le répertoire AVANT le serveur, et l'annonce")
garde_tmpdir, garde_tempdir = os.environ.get("TMPDIR"), tempfile.tempdir
try:
    # Sur un hôte dont /tmp est un tmpfs, un candidat créé là serait refusé À JUSTE TITRE : il
    # faut un système de fichiers durable pour éprouver le chemin nominal.
    base = next((d for d in ("/var/tmp", RACINE, "/tmp")
                 if spool._mount_fstype(d) not in spool._RAM_FS), None)
    if base is None:
        print("  SKIP(env): aucun système de fichiers durable accessible — la section 2 ne "
              "prouve RIEN ici et n'a pas été exécutée")
        base = None
    tmp = tempfile.mkdtemp(prefix="verif-transit-", dir=base) if base else tempfile.mkdtemp()
    cible = os.path.join(tmp, "transit")
    d, pourquoi = spool.configure(required=1 << 20, candidates=[cible])
    dire(base is None or d == cible, "le candidat utilisable est choisi", d)
    dire(base is None or tempfile.tempdir == cible, "`tempfile.tempdir` est posé (c'est lui que Waitress consulte)")
    dire(base is None or os.environ.get("TMPDIR") == cible, "TMPDIR est posé pour ce qui sera lancé ensuite")
    etat = spool.state()
    dire(base is None or (etat["dir"] == cible and etat["ram_backed"] is False and etat["free"] > 0),
         "l'état publié porte le répertoire, la nature et la place", json.dumps(etat)[:90])
    dire(spool.capacity(margin=0) == spool.free_bytes(cible),
         "`capacity` est MESURÉE, pas déclarée")
    # Aucun candidat utilisable → repli, mais DIT (sinon c'est l'échec silencieux d'origine).
    # ⚠ `configure` vient de poser `tempfile.tempdir` : comparer le repli à `gettempdir()` sans
    #   le remettre à zéro serait une tautologie, vraie quoi que fasse le code.
    tempfile.tempdir = None
    os.environ.pop("TMPDIR", None)
    defaut_systeme = tempfile.gettempdir()
    d2, p2 = spool.configure(required=1 << 62, candidates=[cible])
    dire(d2 == defaut_systeme and d2 != cible,
         "sans candidat tenable, repli sur le défaut système", "%s (≠ %s)" % (d2, cible))
    dire("fail part-way" in p2 or "free" in p2,
         "et le repli est ANNONCÉ, pas silencieux", p2[:60])
finally:
    tempfile.tempdir = garde_tempdir
    if garde_tmpdir is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = garde_tmpdir

print("\n3. Les plafonds sont bornés par la place réelle, pas par les autres plafonds")
src_main = io.open(os.path.join(RACINE, "main.py"), encoding="utf-8").read()
dire("spool.configure(required=_WAITRESS_BODY_MAX)" in src_main,
     "l'espace de transit est choisi avant `create_server`")
dire(re.search(r"min\(_WAITRESS_BODY_MAX,\s*spool\.capacity\(\)\)", src_main),
     "le plafond de corps de requête est borné par `spool.capacity()`")
dire("max_request_body_size=max(_corps_max" in src_main,
     "c'est le plafond BORNÉ qui est passé à Waitress")
src_hd = io.open(os.path.join(RACINE, "app/routes/host_drivers.py"), encoding="utf-8").read()
dire("spool.capacity()" in src_hd and '"no_spool_space"' in src_hd,
     "le téléversement de pilote refuse d'avance ce qui ne tient pas")
dire(re.search(r"length > capacite", src_hd) and "), 507" in src_hd,
     "et il le refuse en 507 (place insuffisante), pas en 500")

print("\n4. Une panne de place SE NOMME, sur n'importe quelle route")
app = _app_essai()
c = app.test_client()
vrai_etat = spool.state
try:
    spool.state = lambda: {"dir": "/transit", "free": 0, "reason": "essai", "ram_backed": False}
    for chemin, titre in (("/api/plein", "OSError ENOSPC direct"),
                          ("/api/enveloppe", "ENOSPC enveloppé dans une autre exception"),
                          ("/api/boum", "erreur quelconque, mais le transit est plein")):
        r = c.get(chemin)
        j = r.get_json() or {}
        dire(r.status_code == 507 and j.get("code") == "no_space",
             titre, "%s %s" % (r.status_code, j.get("code")))
    r = c.get("/api/plein")
    j = r.get_json() or {}
    dire("transit" in (j.get("error") or "").lower() or "transit" in (j.get("detail") or ""),
         "le message dit DE QUOI il s'agit", (j.get("error") or "")[:70])
    dire("rapport" in (j.get("error") or "") or "unrelated" in (j.get("error") or ""),
         "et qu'elle n'a sans doute aucun rapport avec le geste de l'opérateur")
finally:
    spool.state = vrai_etat

print("\n5. Hors saturation : le détail remplace « HTTP 500 », et un 404 reste un 404")
try:
    spool.state = lambda: {"dir": "/transit", "free": 1 << 30, "reason": "essai", "ram_backed": False}
    r = c.get("/api/boum")
    j = r.get_json() or {}
    dire(r.status_code == 500 and j.get("code") == "unexpected" and "RuntimeError" in (j.get("detail") or ""),
         "une erreur inattendue rend du JSON nommé, pas une page HTML",
         "%s %s" % (r.status_code, (j.get("detail") or "")[:40]))
    r = c.get("/api/inexistant")
    dire(r.status_code == 404, "un 404 traverse le gestionnaire intact", str(r.status_code))
finally:
    spool.state = vrai_etat

print("\n6. Les messages existent dans les deux catalogues")
for lang in ("fr", "en"):
    cat = json.load(io.open(os.path.join(RACINE, "i18n/%s.json" % lang), encoding="utf-8"))
    manque = [k for k in ("error.no_space", "error.unexpected",
                          "settings.hostdrv.err.no_spool_space") if k not in cat]
    dire(not manque, "%s : les 3 clés sont traduites" % lang, str(manque))

print("\n7. MUTATIONS — chaque vérification doit virer au rouge")
MUT = []


def muter(titre, pose, depose, attendu):
    pose()
    try:
        avant = len(_echecs)
        attendu()
        pris = len(_echecs) > avant
        del _echecs[avant:]          # un échec PROVOQUÉ n'est pas un échec du test
    finally:
        depose()
    MUT.append((titre, pris))
    print("  %s %s" % ("✓ attrapée" if pris else "✗ NON attrapée", titre))


_silence = []
_vrai_dire = dire


def dire(ok, titre, detail=""):          # noqa: F811 — muet pendant les mutations
    if not ok:
        _echecs.append(titre)


# a. `_usable` cesse de regarder la nature du système de fichiers → un tmpfs passerait.
_vrai_ramfs = spool._RAM_FS


def _a_pose():
    spool._mount_fstype = lambda p: "tmpfs"
    spool._RAM_FS = ()                      # le code ne sait plus ce qu'est la RAM


def _a_depose():
    spool._mount_fstype = vrai_fstype
    spool._RAM_FS = _vrai_ramfs


muter("la nature du système de fichiers n'est plus regardée", _a_pose, _a_depose,
      lambda: dire(spool._usable(tempfile.mkdtemp(), 0)[0] is False, "tmpfs refusé"))

# b. La chaîne d'exceptions n'est plus remontée → un ENOSPC enveloppé redeviendrait un 500 nu.
vrai_errno = http_errors._errno_of
muter("l'ENOSPC enveloppé n'est plus reconnu",
      lambda: setattr(http_errors, "_errno_of", lambda e: getattr(e, "errno", None)),
      lambda: setattr(http_errors, "_errno_of", vrai_errno),
      lambda: (lambda r: dire(r.status_code == 507, "507 attendu"))(
          (lambda: (setattr(spool, "state", lambda: {"dir": "/t", "free": 1 << 30, "reason": "",
                                                     "ram_backed": False}),
                    c.get("/api/enveloppe"))[1])()))
spool.state = vrai_etat

# c. La sonde de saturation disparaît → une erreur quelconque ne nommerait plus la vraie cause.
vrai_sat = http_errors._transit_sature
muter("la sonde de saturation disparaît",
      lambda: setattr(http_errors, "_transit_sature", lambda: (False, {})),
      lambda: setattr(http_errors, "_transit_sature", vrai_sat),
      lambda: (lambda r: dire(r.status_code == 507, "507 attendu"))(
          (lambda: (setattr(spool, "state", lambda: {"dir": "/t", "free": 0, "reason": "",
                                                     "ram_backed": False}),
                    c.get("/api/boum"))[1])()))
spool.state = vrai_etat

# d. Le bornage du plafond disparaît de main.py (vérification sur la SOURCE).
muter("le plafond n'est plus borné par la place",
      lambda: None, lambda: None,
      lambda: dire(bool(re.search(r"min\(_WAITRESS_BODY_MAX,\s*spool\.capacity\(\)\)",
                                  src_main.replace("min(_WAITRESS_BODY_MAX, spool.capacity())",
                                                   "_WAITRESS_BODY_MAX"))),
                   "bornage présent"))

dire = _vrai_dire
rates = [t for t, pris in MUT if not pris]
print("\n%d/%d mutations attrapées" % (len(MUT) - len(rates), len(MUT)))
if rates:
    _echecs.extend("mutation non attrapée : " + t for t in rates)

print("\n%s" % ("✓ tout passe" if not _echecs else "✗ %d échec(s) : %s"
                % (len(_echecs), "; ".join(_echecs[:6]))))
sys.exit(1 if _echecs else 0)
