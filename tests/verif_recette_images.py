#!/usr/bin/env python3
"""Garde : images jointes aux discussions de la page Recette (/tests).

Un testeur colle une capture dans un fil : elle doit être stockée À CÔTÉ du store (jamais en
base64 dedans — la page relit le store en boucle), resservie par /api/tests/image/<nom>, et
refusée si ce n'est pas une image raster (un SVG ou une page HTML renommée .png, resservis
inline, exécuteraient du script dans l'origine de la page). On vérifie aussi qu'un lot refusé
n'écrit rien, que l'export de campagne embarque les images et que l'import les restitue.

Store et dossier d'images dans un répertoire jetable ; aucune base, aucun réseau."""
import base64, io, json, os, shutil, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask
from app import auth, testplan as tp

TMP = tempfile.mkdtemp(prefix="recette_img_")
tp.STORE_PATH = os.path.join(TMP, "db_testplan.json")
tp.LOCK_PATH = tp.STORE_PATH + ".lock"
tp.IMAGES_DIR = os.path.join(TMP, "testplan_images")
tp.SEED_ITEMS = [("pt-1", "Zone", "Point 1", "", "")]
auth.current_user = lambda: {"id": 1, "username": "t"}
auth.has_perm = lambda p: True
tp._actif = lambda: True

app = Flask(__name__)
app.register_blueprint(tp.testplan_bp)
c = app.test_client()

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def post(files, text="", iid="pt-1"):
    data = {"id": iid, "text": text, "author": "Testeur"}
    data["image"] = [(io.BytesIO(b), n) for b, n in files]
    return c.post("/api/tests/thread", data=data, content_type="multipart/form-data")


def stored():
    return sorted(os.listdir(tp.IMAGES_DIR)) if os.path.isdir(tp.IMAGES_DIR) else []


try:
    # 1. Message image seule (sans texte) : accepté, fichier écrit, nom dans le message.
    r = post([(PNG, "capture.png"), (JPG, "photo.jpg")])
    j = r.get_json() or {}
    imgs = (j.get("message") or {}).get("images") or []
    check(r.status_code == 200 and len(imgs) == 2, "image seule refusée : %s %s" % (r.status_code, j))
    check(len(stored()) == 2, "2 fichiers attendus, vus %s" % stored())
    check([n.rsplit(".", 1)[1] for n in imgs] == ["png", "jpg"],
          "extension non dérivée du contenu : %s" % imgs)
    raw = open(tp.STORE_PATH).read()
    check(base64.b64encode(PNG[:30]).decode()[:20] not in raw and len(raw) < 4000,
          "l'image est entrée dans le store JSON (%d octets)" % len(raw))

    # 2. Relecture : bon contenu, bon type, nosniff.
    if imgs:
        g = c.get("/api/tests/image/" + imgs[0])
        check(g.status_code == 200 and g.data == PNG and g.mimetype == "image/png",
              "image mal resservie : %s %s" % (g.status_code, g.mimetype))
        check(g.headers.get("X-Content-Type-Options") == "nosniff", "nosniff absent")
    check(c.get("/api/tests/image/..%2Fdb_testplan.json").status_code == 404, "nom non filtré")

    # 3. Un SVG — même nommé .png — est refusé, et le lot entier n'écrit RIEN.
    avant = stored()
    r = post([(PNG, "ok.png"), (SVG, "piege.png")], text="lot mixte")
    check(r.status_code == 400, "SVG déguisé accepté (%s)" % r.status_code)
    check(stored() == avant, "lot refusé mais fichiers écrits : %s" % (set(stored()) - set(avant)))

    # 4. Trop d'images ; point inconnu (rien d'écrit non plus).
    r = post([(PNG, "a.png")] * (tp.IMAGES_PER_MESSAGE + 1))
    check(r.status_code == 400, "plafond d'images non appliqué (%s)" % r.status_code)
    r = post([(PNG, "a.png")], iid="inconnu")
    check(r.status_code == 404 and stored() == avant, "point inconnu : %s, %s" % (r.status_code, stored()))

    # 5. Le message texte en JSON marche toujours.
    r = c.post("/api/tests/thread", json={"id": "pt-1", "text": "texte seul", "author": "T"})
    check(r.status_code == 200 and "images" not in (r.get_json() or {}).get("message", {}),
          "message texte JSON cassé : %s" % r.status_code)

    # 6. Aller-retour export → import sur une installation vide : les images reviennent.
    e = c.get("/api/tests/export?resultats=1")
    exp = json.loads(e.data)
    check(sorted(exp.get("images") or {}) == sorted(imgs), "export sans les images : %s" % list(exp.get("images") or {}))
    shutil.rmtree(tp.IMAGES_DIR)
    os.remove(tp.STORE_PATH)
    r = c.post("/api/tests/import", data={"file": (io.BytesIO(e.data), "c.json")},
               content_type="multipart/form-data")
    check(r.status_code == 200 and (r.get_json() or {}).get("images") == 2,
          "import n'a pas restitué les images : %s" % (r.get_json(),))
    if imgs:
        check(c.get("/api/tests/image/" + imgs[0]).data == PNG, "image importée altérée")
    # Une image d'export dont le contenu ne correspond pas à son extension est ignorée.
    exp["images"] = {"0" * 20 + ".png": base64.b64encode(SVG).decode()}
    r = c.post("/api/tests/import", data={"file": (io.BytesIO(json.dumps(exp).encode()), "c.json")},
               content_type="multipart/form-data")
    check(not os.path.exists(os.path.join(tp.IMAGES_DIR, "0" * 20 + ".png")), "SVG importé comme PNG")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

for f in fails:
    print("ÉCHEC", f)
print("OK" if not fails else "%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
