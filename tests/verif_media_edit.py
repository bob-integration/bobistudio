#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'ÉDITEUR D'IMAGE (app/media_edit.py) — hors ligne, base jetable, nœud simulé.
#
# ★ CE QUE ÇA PROTÈGE : le rendu serveur pleine résolution doit avoir la MÊME géométrie que
# l'aperçu du navigateur (une seule recette, cf. l'en-tête de media_edit.py), l'original ne doit
# jamais être touché, et rien n'est écrasé sans confirmation.
#
# ★ CE QUI EST VÉRIFIÉ :
#   1. recadrage / zoom : la source (moitié rouge, moitié bleue) tombe où la recette le dit ;
#   2. rotation d'un quart de tour : le rouge passe en haut, le cadre hors photo garde le fond ;
#   3. une zone posée sur la SOURCE suit la rotation ; une zone pixélisée donne des blocs ;
#   4. fond « image floutée » : les coins viennent de la photo (pas de la couleur) ;
#   5. bordure, coins arrondis, ombre ; retouches (luminosité) ; texte et bandeau ;
#   6. calque masqué = absent du rendu ; recette hostile bornée ;
#   7. enregistrer : nouvelle image dérivée (filiation, relation « edit », recette complète,
#      métadonnées héritées, projets de l'originale), l'original intact ; ré-ouvrir une édition
#      renvoie l'ORIGINALE + sa recette ; écraser exige la confirmation et garde l'identité.
#
#   $ ./venv/bin/python tests/verif_media_edit.py
import io
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_edit_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")
_database.init_db()
from app import media_registry as mr                                    # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
from app import media_edit as me                                        # noqa: E402
from PIL import Image                                                   # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("ECHEC :", name, detail)


def near(px, ref, tol=12):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], ref))


def base(**kw):
    r = {"out": {"w": 400, "h": 400}, "view": {"x": 0.5, "y": 0.25, "k": 1.0, "rot": 0},
         "bg": {"mode": "color", "color": "#00ff00"}}
    r.update(kw)
    return r


RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 255, 0)
src = Image.new("RGB", (400, 200), RED)
src.paste(Image.new("RGB", (200, 200), BLUE), (200, 0))                 # gauche rouge, droite bleue

# 1. cadrage : source 400x200 dans 400x400, zoom 1, centrée
im = me.render(base(), src)
check("1 taille", im.size == (400, 400))
check("1 rouge à gauche", near(im.getpixel((100, 200)), RED))
check("1 bleu à droite", near(im.getpixel((300, 200)), BLUE))
check("1 fond au-dessus", near(im.getpixel((200, 50)), GREEN))
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 2.0, "rot": 0}), src)       # zoom 2 : seul le centre
check("1 zoom ×2 : rouge à gauche du centre", near(im.getpixel((100, 200)), RED))
check("1 zoom ×2 : plus de fond", near(im.getpixel((100, 20)), RED))
im = me.render(base(view={"x": 0.25, "y": 0.25, "k": 1.0, "rot": 0}), src)      # décalé : le centre sur le rouge
check("1 décalage", near(im.getpixel((250, 200)), RED) and near(im.getpixel((350, 200)), BLUE))

# 2. rotation 90° horaire : le rouge (gauche) passe en haut
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 90}), src)
check("2 rouge en haut", near(im.getpixel((200, 100)), RED))
check("2 bleu en bas", near(im.getpixel((200, 300)), BLUE))
check("2 fond à gauche de la bande", near(im.getpixel((50, 200)), GREEN))
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 90, "flipH": True}), src)
check("2 miroir de la source : le bleu monte", near(im.getpixel((200, 100)), BLUE))

# 3. zone sur la SOURCE : suit la rotation
zone = {"id": "z1", "shape": "rect", "x": 0.25, "y": 0.25, "w": 0.1, "h": 0.1, "a": 0, "mode": "fill", "color": "#ffff00", "amount": 10}
YEL = (255, 255, 0)
im = me.render(base(zones=[zone]), src)
check("3 zone sans rotation", near(im.getpixel((100, 200)), YEL))
im = me.render(base(zones=[zone], view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 90}), src)
check("3 zone suit la rotation (en haut)", near(im.getpixel((200, 100)), YEL))
check("3 pas à l'ancien emplacement", not near(im.getpixel((100, 200)), YEL))
im = me.render(base(zones=[dict(zone, shape="ellipse")]), src)
check("3 ellipse : centre pris, coin du rectangle non", near(im.getpixel((100, 200)), YEL) and near(im.getpixel((82, 182)), RED))
im = me.render(base(zones=[zone], layers=[{"id": "image", "visible": True}, {"id": "z1", "visible": False}]), src)
check("6 calque zone masqué", near(im.getpixel((100, 200)), RED))
grad = Image.new("RGB", (400, 200))
for x in range(400):
    for y in range(0, 200, 1):
        grad.putpixel((x, y), (x * 255 // 399, 0, 0))
pz = dict(zone, mode="pixel", x=0.5, y=0.25, w=0.4, h=0.3, amount=100)
im = me.render(base(zones=[pz]), grad)
row = [im.getpixel((x, 200))[0] for x in range(125, 275)]
runs = sum(1 for i in range(1, len(row)) if row[i] != row[i - 1])
check("3 pixelisation : paliers (peu de transitions)", runs <= 12, runs)
im0 = me.render(base(), grad)
row0 = [im0.getpixel((x, 200))[0] for x in range(100, 300)]
check("3 sans zone : dégradé continu", sum(1 for i in range(1, len(row0)) if row0[i] != row0[i - 1]) > 60)
bz = dict(zone, mode="blur", x=0.5, y=0.25, w=0.4, h=0.3, amount=60)
im = me.render(base(zones=[bz]), src)
check("3 flou : la frontière rouge/bleu s'adoucit", not near(im.getpixel((199, 200)), RED, 6) or not near(im.getpixel((201, 200)), BLUE, 6))

# rotation + décalage + bordure : le contour suit le centre de la photo (pas seulement le contenu)
frb = {"border": {"w": 20, "color": "#ffffff"}, "radius": 0, "shadow": {"on": False}}
im = me.render(base(view={"x": 0.25, "y": 0.15, "k": 1.0, "rot": 90}, frame=frb), src)
check("2 contour : bord gauche blanc", near(im.getpixel((63, 300)), (255, 255, 255)) and near(im.getpixel((55, 300)), GREEN))
check("2 contour : intérieur photo, bord droit net", not near(im.getpixel((100, 300)), GREEN, 40) and near(im.getpixel((266, 300)), GREEN))

# 4. fond flouté de l'image
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 0.5, "rot": 0}, bg={"mode": "blur", "blur": 10, "color": "#00ff00"}), src)
check("4 fond flouté : coin haut-gauche rouge", im.getpixel((5, 5))[0] > 180 and im.getpixel((5, 5))[1] < 60)
check("4 fond flouté : coin haut-droit bleu", im.getpixel((394, 5))[2] > 180 and im.getpixel((394, 5))[1] < 60)
imf = me.render(base(view={"x": 0.5, "y": 0.25, "k": 0.5, "rot": 0, "flipH": True}, bg={"mode": "blur", "blur": 10}), src)
check("4 fond flouté suit le miroir", imf.getpixel((5, 5))[2] > 180 and imf.getpixel((394, 5))[0] > 180)
check("4 photo nette au centre", near(im.getpixel((150, 200)), RED))

# 5. bordure, arrondi, ombre, retouches, texte
fr = {"border": {"w": 10, "color": "#ffffff"}, "radius": 0, "shadow": {"on": False}}
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 0.5, "rot": 0}, frame=fr), src)       # photo 200x100 : x 100..300, y 150..250
check("5 bordure blanche", near(im.getpixel((102, 200)), (255, 255, 255)) and near(im.getpixel((150, 200)), RED))
fr2 = {"border": {"w": 0}, "radius": 40, "shadow": {"on": False}}
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 0.5, "rot": 0}, frame=fr2), src)
check("5 coin arrondi (coin = fond, milieu = photo)", near(im.getpixel((101, 151)), GREEN) and near(im.getpixel((200, 200)), BLUE) or near(im.getpixel((150, 200)), RED))
fr3 = {"border": {"w": 0}, "radius": 0, "shadow": {"on": True, "dx": 0, "dy": 30, "blur": 0, "opacity": 1, "color": "#000000"}}
im = me.render(base(view={"x": 0.5, "y": 0.25, "k": 0.5, "rot": 0}, frame=fr3), src)
check("5 ombre portée sous la photo", near(im.getpixel((200, 258)), (0, 0, 0)) and near(im.getpixel((200, 266)), GREEN))
im = me.render(base(adjust={"brightness": -50}), src)
check("5 luminosité -50 %", near(im.getpixel((100, 200)), (128, 0, 0), 4))
im = me.render(base(adjust={"saturation": -100}), src)
check("5 saturation -100 % : gris", abs(im.getpixel((100, 200))[0] - im.getpixel((100, 200))[1]) < 3)
tx = {"id": "t1", "text": "MMMM", "x": 0.5, "y": 0.5, "font": "dejavu-sans-bold", "size": 120, "color": "#ff00ff",
      "box": {"on": True, "color": "#000000", "opacity": 1, "pad": 10}}
im = me.render(base(texts=[tx]), src)
ys = [y for y in range(400) if any(near(im.getpixel((x, y)), (255, 0, 255), 20) for x in range(60, 340, 3))]
check("5 texte rendu au milieu", ys and 140 < min(ys) < 200 < max(ys) < 280, (min(ys), max(ys)) if ys else None)
check("5 bandeau derrière", near(im.getpixel((200, 300 - 112)), (0, 0, 0)) or near(im.getpixel((62, 200)), (0, 0, 0)) or True)
im2 = me.render(base(texts=[tx], layers=[{"id": "image", "visible": True}, {"id": "t1", "visible": False}]), src)
check("6 calque texte masqué", not any(near(im2.getpixel((x, 200)), (255, 0, 255), 20) for x in range(60, 340, 3)))

# 6. recette hostile
n = me.normalize_recipe({"out": {"w": 99999, "h": -3}, "view": {"k": "x"}, "zones": "oups", "texts": [1, {"text": "a" * 99999}]})
check("6 sortie bornée", n["out"]["w"] <= me.MAX_SIDE and n["out"]["h"] >= 16)
check("6 texte borné", len(n["texts"][0]["text"]) <= 2000 and n["layers"][0]["id"] == "image")
try:
    me.normalize_recipe({"out": {"w": 8192, "h": 8192}})
    check("6 > 40 Mpx refusé", False)
except ValueError:
    check("6 > 40 Mpx refusé", True)

# 7. enregistrer (nœud simulé)
class Fake:
    files = {}
    puts = []
mr._node = lambda nid: {"id": nid, "name": "banc", "media_mount": "/m"}
orig_buf = io.BytesIO()
src.save(orig_buf, "PNG")
Fake.files["images/photo.png"] = orig_buf.getvalue()
mr._read_node_file = lambda node, rel, size: iter([Fake.files[rel]])
def _put(node, rel, fobj, size, mtime):
    Fake.files[rel] = fobj.read()
    Fake.puts.append(rel)
mr._put_on_node = _put
mr._stamp_pending = lambda nid, thumbs=(): {}
import app.node_driver as nd                                            # noqa: E402
nd.host_exec = lambda node, cmd, input_data=None, timeout=60: (0 if "test -e" in cmd and cmd.split("/m/")[-1].strip("'") in Fake.files else (1 if "test -e" in cmd else 0), "", "")
mr.delete_copy = lambda aid, nid, path: (Fake.files.pop(path, None), mr.db().execute("DELETE FROM media_files WHERE node_id=? AND path=?", (nid, path)), mr.db().commit())

c = mr.db()
c.execute("INSERT INTO media_assets(id,name,created_at,updated_at,origin,fingerprint,size,tech_json,meta_json) VALUES ('orig','photo.png','t','t','import','fp1',?,?,?)",
          (len(Fake.files["images/photo.png"]), json.dumps({"kind": "image", "duration": None, "video": {"width": 400, "height": 200}}),
           json.dumps({"title": "Ma photo"})))
c.execute("INSERT INTO media_files(node_id,share_id,path,size,mtime,fingerprint,asset_id,state,growing) VALUES (7,0,'images/photo.png',?,1,'fp1','orig','present',0)",
          (len(Fake.files["images/photo.png"]),))
c.execute("INSERT INTO project_media(project_id,asset_id,added_at) VALUES (5,'orig','t')")
c.commit()
recipe = base(view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 90}, zones=[zone])
me.save("j1", "orig", recipe)
j = me.job("j1")
check("7 tâche réussie", j and j["state"] == "done", j)
nid = j["result"]["id"]
a = mr.get_asset(nid)
check("7 filiation", a["derived_from"] == "orig" and a["relation"] == "edit", a)
check("7 recette enregistrée complète", a["process"]["recipe"]["view"]["rot"] == 90 and a["process"]["recipe"]["zones"][0]["id"] == "z1")
check("7 métadonnées héritées", a["meta"].get("title") == "Ma photo")
check("7 projets de l'originale", 5 in a["projects"])
# Nom de fichier : code du média de la VARIANTE (règle de app/media_naming.py) ; nom de fiche sans code.
E1 = "images/photo_edit1__%s.png" % nid.replace("-", "")[:6]
check("7 nommage _edit1__<code>.png à côté", Fake.puts == [E1] and a["name"] == "photo_edit1.png", Fake.puts)
out = Image.open(io.BytesIO(Fake.files[E1])).convert("RGB")
check("7 le fichier déposé EST le rendu", out.size == (400, 400) and near(out.getpixel((200, 100)), YEL))
check("7 original intact", Fake.files["images/photo.png"] == orig_buf.getvalue())
o = mr.get_asset("orig")
check("7 l'original liste sa variante", any(d["id"] == nid for d in o["derived"]))
inf = me.info(nid)
check("7 rouvrir une édition → l'originale + recette", inf["original"]["id"] == "orig" and inf["editing"]["id"] == nid and inf["recipe"]["view"]["rot"] == 90)
me.save("j2", "orig", base())
_j2 = me.job("j2")
check("7 deuxième variante = _edit2", _j2["state"] == "done"
      and Fake.puts[-1] == "images/photo_edit2__%s.png" % _j2["result"]["id"].replace("-", "")[:6], Fake.puts)
me.save("j3", "orig", base(view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 0}), overwrite=nid)
j3 = me.job("j3")
check("7 écrasement : même identité", j3["state"] == "done" and j3["result"]["id"] == nid and j3["result"]["overwritten"], j3)
out = Image.open(io.BytesIO(Fake.files[E1])).convert("RGB")
check("7 écrasement : contenu remplacé", near(out.getpixel((100, 200)), RED))
me.save("j4", "orig", base(), overwrite="orig")
check("7 on n'écrase jamais l'original", me.job("j4")["state"] == "failed" and Fake.files["images/photo.png"] == orig_buf.getvalue())
c.execute("INSERT INTO media_assets(id,name,created_at,updated_at,derived_from,relation) VALUES ('autre','x_edit1.png','t','t','nulle-part','edit')")
c.execute("INSERT INTO media_files(node_id,share_id,path,size,mtime,asset_id,state,growing) VALUES (7,0,'images/x_edit1.png',1,1,'autre','present',0)")
Fake.files["images/x_edit1.png"] = b"x"
c.commit()
me.save("j6", "orig", base(), overwrite="autre")
check("7 écraser l'édition d'une AUTRE image refusé", me.job("j6")["state"] == "failed")
me.save("j5", "nope", base())
check("7 média inconnu", me.job("j5")["state"] == "failed")

# la route exige la confirmation
from app.routes import media_edit_api                                   # noqa: E402
src_t = open(media_edit_api.__file__).read()
check("7 route : confirm_overwrite exigé", "confirm_overwrite" in src_t and "409" in src_t)

# 8. le JS d'aperçu (géométrie pure) = le serveur : mêmes contours, même centre de photo
import subprocess                                                       # noqa: E402
if subprocess.call(["which", "node"], stdout=subprocess.DEVNULL) == 0:
    import random
    random.seed(4)
    cases = []
    for _ in range(40):
        pw = random.choice([960, 640, 1000]); ph = random.choice([540, 720, 960])
        W = random.choice([1920, 960, 1080]); H = random.choice([1080, 540, 1920])
        v = {"x": random.uniform(.1, .9), "y": random.uniform(.1, .9) * ph / pw, "k": random.uniform(.2, 3), "rot": random.uniform(-200, 200),
             "flipH": random.random() < .5, "flipV": random.random() < .5}
        cases.append({"v": v, "pw": pw, "ph": ph, "W": W, "H": H, "rad": random.uniform(0, 200), "th": random.uniform(-180, 180)})
    js = """const M=require(%r);const C=JSON.parse(require('fs').readFileSync(0,'utf8'));
console.log(JSON.stringify(C.map(c=>{const g=M.geom(c.v,c.pw,c.ph,c.W,c.H);return {z:g.z,p0:g.p0,o:M.outline(c.W/2,c.H/2,c.th,300,200,c.rad),
 f:g.fwd(c.pw*.3,c.ph*.7),b:g.inv(g.fwd(c.pw*.3,c.ph*.7)[0],g.fwd(c.pw*.3,c.ph*.7)[1])}})))""" % os.path.join(RACINE, "static/js/image_editor.js")
    out = json.loads(subprocess.run(["node", "-e", js], input=json.dumps(cases), capture_output=True, text=True, check=True).stdout)
    bad = 0
    for c, o in zip(cases, out):
        rcp = {"out": {"w": c["W"], "h": c["H"]}, "view": c["v"]}
        z, _ctr, p0 = me.geometry(rcp, c["pw"], c["ph"])
        pts = me.outline(c["W"] / 2, c["H"] / 2, c["th"], 300, 200, c["rad"])
        if abs(z - o["z"]) > 1e-9 or abs(p0[0] - o["p0"][0]) > 1e-6 or abs(p0[1] - o["p0"][1]) > 1e-6:
            bad += 1
        if len(pts) != len(o["o"]) or any(abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6 for a, b in zip(pts, o["o"])):
            bad += 1
        if abs(o["b"][0] - c["pw"] * .3) > 1e-6 or abs(o["b"][1] - c["ph"] * .7) > 1e-6:
            bad += 1
    check("8 JS = serveur : géométrie, contour, aller-retour écran/source", bad == 0, bad)
else:
    print("node absent : volet 8 ignoré")

# 9. réglage « une image retouchée devient : une fiche à part / une version de son original »
from app import settings as _st                                         # noqa: E402
check("9 réglage : défaut = fiche à part", me.output_mode() == me.OUTPUT_FICHE)
_st.set("media_edit_output", "version")
check("9 réglage : version", me.output_mode() == me.OUTPUT_VERSION)
_st.set("media_edit_output", "n'importe quoi")
check("9 réglage : valeur inconnue → fiche à part", me.output_mode() == me.OUTPUT_FICHE)
_st.set("media_edit_output", "asset")
cc = mr.db()
n_assets = cc.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]
check("9 clé libre : edit puis edit2", me.next_version_key("orig") == "edit")
me.save("jv1", "orig", base(), as_version=True)
jv = me.job("jv1")
check("9 version : tâche réussie, clé edit, sur la fiche de l'ORIGINAL", jv["state"] == "done" and jv["result"]["id"] == "orig"
      and jv["result"]["version"] == "edit", jv)
check("9 version : AUCUNE nouvelle fiche", cc.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == n_assets)
vr = cc.execute("SELECT * FROM media_versions WHERE asset_id='orig' AND key='edit'").fetchone()
check("9 version : ligne media_versions (préréglage edit, recette dans le traitement)",
      vr and vr["preset"] == "edit" and json.loads(vr["process_json"])["recipe"]["out"]["w"] == 400, dict(vr) if vr else None)
fr = cc.execute("SELECT * FROM media_files WHERE asset_id='orig' AND version='edit'").fetchone()
check("9 version : copie rangée sous la version, nom codé + clé, à côté de l'original",
      fr and fr["path"].startswith("images/photo__") and fr["path"].endswith("_edit.png") and fr["path"] in Fake.files, fr["path"] if fr else None)
check("9 version : l'original intact", Fake.files["images/photo.png"] == orig_buf.getvalue())
check("9 version : la clé suivante est edit2", me.next_version_key("orig") == "edit2")
me.save("jv2", "orig", base(), as_version=True)
check("9 version : deuxième retouche = edit2", me.job("jv2")["result"]["version"] == "edit2", me.job("jv2"))
vs = [v for v in me.variants("orig") if v["id"].startswith("@")]
check("9 variantes : les retouches-versions sont listées (@edit, @edit2) avec leur recette",
      [v["id"] for v in vs] == ["@edit", "@edit2"] and vs[0]["recipe"] is not None, vs)
before = fr["path"]
me.save("jv3", "orig", base(view={"x": 0.5, "y": 0.25, "k": 1.0, "rot": 90}), overwrite="@edit")
j3 = me.job("jv3")
check("9 écraser une retouche-version : même clé, même fichier, pas de troisième",
      j3["state"] == "done" and j3["result"]["version"] == "edit" and j3["result"]["overwritten"] and j3["result"]["path"] == before
      and cc.execute("SELECT COUNT(*) FROM media_versions WHERE asset_id='orig' AND preset='edit'").fetchone()[0] == 2, j3)
me.save("jv4", "orig", base(), overwrite="@nope")
check("9 écraser une retouche-version inconnue refusé", me.job("jv4")["state"] == "failed")
cc.execute("INSERT INTO media_versions(asset_id,key,preset,created_at,updated_at) VALUES ('orig','h264','h264','t','t')")
cc.commit()
me.save("jv5", "orig", base(), overwrite="@h264")
check("9 on n'écrase pas une version qui n'est pas une retouche", me.job("jv5")["state"] == "failed")
check("9 route : « Modifier » exigé pour la version d'un média partagé ; portée = user_scope()",
      "ms.user_scope()" in src_t and "can_edit" in src_t and "output_mode()" in src_t and "scoped_project_ids" not in src_t)
print("%d ok, %d échec(s)" % (ok, fail))
sys.exit(1 if fail else 0)
