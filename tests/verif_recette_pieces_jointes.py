#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : pièces jointes TEXTE (patch, journal) des discussions de la page Recette (/tests).

★ POURQUOI. Demandé par le testeur le 2026-11-27 : son agent prépare des correctifs et n'avait
que des blocs de code de 8000 caractères pour les faire passer — 5 morceaux à recoller pour un
seul patch. Un fichier joint règle ça, à deux conditions, et il faut LES DEUX :

  · à l'entrée, le CONTENU décide — pas le nom, pas le Content-Type du navigateur. Seul du texte
    entre (UTF-8 valide, aucun octet nul, aucune commande de terminal). L'extension déposée n'est
    qu'une étiquette, ramenée à notre liste et à `.txt` sinon, et le nom déposé ne sert JAMAIS à
    écrire sur le disque : le fichier porte un nom tiré au sort.
  · à la sortie, jamais dans la page — `text/plain`, en pièce jointe, `nosniff`. C'est le point
    qui compte le plus : un patch est du texte pour nous, mais une page HTML dont le nom finit
    par `.patch` est du texte aussi. On ne peut pas la refuser sur son contenu sans refuser les
    patchs ; on peut refuser de la RENDRE. C'est la même leçon que `_image_kind` pour un SVG,
    prise par l'autre bout.

Store et dossiers de pièces jointes dans un répertoire jetable ; aucune base, aucun réseau.

Lancement :
  $ ./venv/bin/python tests/verif_recette_pieces_jointes.py
"""
import base64, io, json, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask
from app import auth, testplan as tp

TMP = tempfile.mkdtemp(prefix="recette_pj_")
tp.STORE_PATH = os.path.join(TMP, "db_testplan.json")
tp.LOCK_PATH = tp.STORE_PATH + ".lock"
tp.IMAGES_DIR = os.path.join(TMP, "testplan_images")
tp.TEXTS_DIR = os.path.join(TMP, "testplan_fichiers")
tp.SEED_ITEMS = [("pt-1", "Zone", "Point 1", "", "")]
auth.current_user = lambda: {"id": 1, "username": "t"}
auth.has_perm = lambda p: True
tp._actif = lambda: True

app = Flask(__name__)
app.register_blueprint(tp.testplan_bp)
c = app.test_client()

PATCH = (b"diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n"
         b"@@ -1,2 +1,2 @@\n-vieux\n+neuf\n")
JOURNAL = "nov 27 12:00 bobi: flux périmé lu sur le nœud 55\n".encode("utf-8")
BINAIRE = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32           # octet nul : pas du texte
UTF16 = "patch".encode("utf-16")                               # UTF-8 invalide
ESCAPE = b"journal \x1b[2J\x1b]0;pwn\x07 ordinaire\n"          # commandes de terminal
HTML = b'<html><body><script>alert(1)</script></body></html>\n'

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def post(fichiers=(), images=(), text="", iid="pt-1"):
    data = {"id": iid, "text": text, "author": "Testeur"}
    if fichiers:
        data["fichier"] = [(io.BytesIO(b), n) for b, n in fichiers]
    if images:
        data["image"] = [(io.BytesIO(b), n) for b, n in images]
    return c.post("/api/tests/thread", data=data, content_type="multipart/form-data")


def stored():
    return sorted(os.listdir(tp.TEXTS_DIR)) if os.path.isdir(tp.TEXTS_DIR) else []


try:
    # ═══ 1. Un patch seul (sans texte) : accepté, écrit à côté du store, cité par le message.
    r = post([(PATCH, "masque-tissu.patch"), (JOURNAL, "orchestrateur.log")])
    j = r.get_json() or {}
    fics = (j.get("message") or {}).get("files") or []
    check(r.status_code == 200 and len(fics) == 2, "fichier seul refusé : %s %s" % (r.status_code, j))
    check(len(stored()) == 2, "2 fichiers attendus sur le disque, vus %s" % stored())
    check([f["label"] for f in fics] == ["masque-tissu.patch", "orchestrateur.log"],
          "étiquette perdue : %s" % fics)
    check([f["name"].rsplit(".", 1)[1] for f in fics] == ["patch", "log"],
          "extension non retenue : %s" % fics)
    check([f["bytes"] for f in fics] == [len(PATCH), len(JOURNAL)], "taille fausse : %s" % fics)
    # Le nom sur le disque est TIRÉ AU SORT : aucun des noms déposés ne s'y retrouve.
    check(not any("masque" in n or "orchestrateur" in n for n in stored()),
          "le nom déposé a servi à écrire : %s" % stored())
    raw = open(tp.STORE_PATH).read()
    check(b"diff --git" not in raw.encode() and len(raw) < 4000,
          "le patch est entré dans le store JSON (%d octets)" % len(raw))

    # ═══ 2. Relecture : texte brut, EN PIÈCE JOINTE, nosniff, nom proposé = l'étiquette.
    if fics:
        g = c.get("/api/tests/fichier/" + fics[0]["name"] + "?nom=masque-tissu.patch")
        check(g.status_code == 200 and g.data == PATCH, "patch mal resservi : %s" % g.status_code)
        check(g.headers.get("Content-Type") == "text/plain; charset=utf-8",
              "type servi : %r" % g.headers.get("Content-Type"))
        disp = g.headers.get("Content-Disposition") or ""
        check("attachment" in disp and "masque-tissu.patch" in disp, "pas en pièce jointe : %r" % disp)
        check(g.headers.get("X-Content-Type-Options") == "nosniff", "nosniff absent")
        # Le nom proposé ne peut pas sortir du nettoyage : pas de chemin, pas de guillemet.
        g2 = c.get("/api/tests/fichier/" + fics[0]["name"] + "?nom=" + "../../etc/passwd")
        d2 = g2.headers.get("Content-Disposition") or ""
        check("/" not in d2.split("filename")[-1], "chemin dans le nom proposé : %r" % d2)
    for faux in ("..%2Fdb_testplan.json", "0" * 20 + ".png", "pas-un-nom.patch", "0" * 19 + ".patch"):
        check(c.get("/api/tests/fichier/" + faux).status_code == 404, "nom non filtré : %s" % faux)

    # ═══ 3. Ce qui n'est PAS du texte est refusé, et un lot refusé n'écrit RIEN.
    for blob, quoi in ((BINAIRE, "binaire"), (UTF16, "UTF-16"), (ESCAPE, "commandes de terminal")):
        avant = stored()
        r = post([(PATCH, "bon.patch"), (blob, "piege.patch")], text="lot mixte")
        check(r.status_code == 400, "%s accepté (%s)" % (quoi, r.status_code))
        check(stored() == avant, "lot refusé (%s) mais fichiers écrits : %s"
              % (quoi, set(stored()) - set(avant)))

    # ═══ 4. Une page HTML EST du texte : elle entre, mais elle ne peut pas être RENDUE.
    #
    # C'est le cœur de la garde. On ne peut pas distinguer `<script>` d'un patch qui en parle,
    # donc la protection n'est pas à l'entrée : elle est dans la façon de servir.
    r = post([(HTML, "piege.html")])
    j = r.get_json() or {}
    fh = ((j.get("message") or {}).get("files") or [{}])[0]
    check(r.status_code == 200 and fh.get("name", "").endswith(".txt"),
          "extension hors liste non ramenée à .txt : %s" % j)
    if fh.get("name"):
        g = c.get("/api/tests/fichier/" + fh["name"])
        disp = g.headers.get("Content-Disposition") or ""
        check(g.headers.get("Content-Type") == "text/plain; charset=utf-8" and "attachment" in disp
              and g.headers.get("X-Content-Type-Options") == "nosniff",
              "HTML joint servi de façon rendable : %r / %r" % (g.headers.get("Content-Type"), disp))

    # ═══ 5. Plafonds, point inconnu, et le message texte en JSON qui marche toujours.
    avant = stored()
    r = post([(PATCH, "a.patch")] * (tp.TEXTS_PER_MESSAGE + 1))
    check(r.status_code == 400 and stored() == avant,
          "plafond par message non appliqué (%s, %s)" % (r.status_code, stored()))
    r = post([(b"x" * (tp.TEXT_MAX_BYTES + 1), "gros.log")])
    check(r.status_code == 400 and stored() == avant, "taille non bornée (%s)" % r.status_code)
    r = post([(PATCH, "a.patch")], iid="inconnu")
    check(r.status_code == 404 and stored() == avant,
          "point inconnu : %s, %s" % (r.status_code, stored()))
    r = c.post("/api/tests/thread", json={"id": "pt-1", "text": "texte seul", "author": "T"})
    check(r.status_code == 200 and "files" not in (r.get_json() or {}).get("message", {}),
          "message texte JSON cassé : %s" % r.status_code)

    # ═══ 6. Aller-retour export → import : les pièces jointes reviennent, et pas n'importe quoi.
    e = c.get("/api/tests/export?resultats=1")
    exp = json.loads(e.data)
    noms = sorted(f["name"] for f in fics) if fics else []
    check(all(n in (exp.get("fichiers") or {}) for n in noms),
          "export sans les fichiers : %s" % list(exp.get("fichiers") or {}))
    shutil.rmtree(tp.TEXTS_DIR)
    os.remove(tp.STORE_PATH)
    r = c.post("/api/tests/import", data={"file": (io.BytesIO(e.data), "c.json")},
               content_type="multipart/form-data")
    check(r.status_code == 200 and (r.get_json() or {}).get("fichiers") == len(exp.get("fichiers") or {}),
          "import n'a pas restitué les fichiers : %s" % (r.get_json(),))
    if noms:
        check(c.get("/api/tests/fichier/" + noms[0]).status_code == 200, "fichier importé absent")
    # Un export dont la pièce jointe n'est pas du texte est ignoré, message gardé.
    exp["fichiers"] = {"0" * 20 + ".patch": base64.b64encode(BINAIRE).decode()}
    r = c.post("/api/tests/import", data={"file": (io.BytesIO(json.dumps(exp).encode()), "c.json")},
               content_type="multipart/form-data")
    check(not os.path.exists(os.path.join(tp.TEXTS_DIR, "0" * 20 + ".patch")),
          "binaire importé comme patch")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

for f in fails:
    print("ÉCHEC", f)
print("OK" if not fails else "%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
