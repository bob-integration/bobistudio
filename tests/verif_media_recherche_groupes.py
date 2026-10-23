#!/usr/bin/env python3
"""Recherche de la base des médias : critères en GROUPES imbriqués — « (A ET B) OU C ».

Demande du 2026-10-12 : mêler ET et OU dans une même recherche (avant : toutes les `all` ET au moins
une des `any`, rien d'autre). Garde : la requête arborescente {match, items} est parenthésée
correctement, un groupe vide est neutre, les bornes sont tenues, une valeur hostile reste une valeur ;
l'ANCIEN format all/any (bins intelligents déjà enregistrés) donne exactement le même résultat
qu'avant et le bin n'est pas réécrit."""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_media_grp_")
import app.database as _database                                       # noqa: E402
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()
from app import media_registry as mr                                    # noqa: E402

mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")

ok = fail = 0


def check(cond, msg):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("  ✓ " if cond else "  ✗ ") + msg)


# Quatre médias qui DISTINGUENT les parenthésages : (A∧B)∨C ≠ A∧(B∨C) ≠ A∧B∨C lu de travers.
#   A = durée 0:08, B = codec h264, C = largeur 720
c = mr.db()
for aid, name, dur, codec, w in (("1", "ab", 8.2, "h264", 1920),      # A B
                                 ("2", "a", 8.4, "prores", 1920),     # A
                                 ("3", "bc", 30.0, "h264", 720),      # B C
                                 ("4", "c", 12.0, "prores", 720),     # C
                                 ("5", "rien", 60.0, "dnxhd", 1280)):
    c.execute("INSERT INTO media_assets(id, name, tech_json, created_at, updated_at) VALUES (?,?,?,?,?)",
              (aid, name, json.dumps({"duration": dur, "video": {"codec": codec, "width": w}}),
               "2026-10-12T10:00:0" + aid, "2026-10-12T10:00:00"))
c.commit()

A = {"field": "duration", "op": "eq", "value": "0:08"}
B = {"field": "vcodec", "op": "eq", "value": "h264"}
C = {"field": "width", "op": "eq", "value": 720}


def names(q, **kw):
    return sorted(a["name"] for a in mr.search(q, **kw))


def grp(match, *items):
    return {"match": match, "items": list(items)}


check(names(grp("any", grp("all", A, B), C)) == ["ab", "bc", "c"], "(A ET B) OU C")
check(names(grp("all", A, grp("any", B, C))) == ["ab"], "A ET (B OU C)")
check(names(grp("any", A, grp("all", B, C))) == ["a", "ab", "bc"], "A OU (B ET C)")
check(names(grp("all", A, B)) == ["ab"], "un seul niveau, tous")
check(names(grp("any", A, C)) == ["a", "ab", "bc", "c"], "un seul niveau, au moins un")
# Groupe vide : neutre, jamais une erreur SQL — ni à la racine ni dans un OU (il ne doit pas tout ouvrir).
everything = ["a", "ab", "bc", "c", "rien"]
check(names(grp("all")) == everything, "racine vide → tout")
check(names(grp("any")) == everything, "racine vide en « au moins un » → tout (pas rien)")
check(names(grp("any", A, grp("all"))) == ["a", "ab"], "groupe vide dans un OU : ignoré, n'ouvre pas tout")
check(names(grp("all", A, grp("any", grp("all")))) == ["a", "ab"], "groupes vides imbriqués : ignorés")
# Imbrication : trois niveaux sous la racine.
deep = grp("any", grp("all", A, grp("any", B, grp("all", C, {"field": "name", "op": "contains", "value": "b"}))),
           {"field": "name", "op": "eq", "value": "rien"})
check(names(deep) == ["ab", "rien"], "imbrication sur 4 niveaux (racine comprise)")
# Texte libre et types s'ajoutent en ET à l'arbre.
check(names(dict(grp("any", A, C), q="b")) == ["ab", "bc"], "texte libre ET l'arbre")

# Ancien format : identique au comportement d'avant (toutes les all ET au moins une des any).
check(names({"all": [A]}) == ["a", "ab"], "ancien : all seul")
check(names({"any": [A, C]}) == ["a", "ab", "bc", "c"], "ancien : any seul")
check(names({"all": [B], "any": [A, C]}) == ["ab", "bc"], "ancien : all ET (any)")
check(names({"all": [], "any": []}) == everything, "ancien : listes vides → tout")
check(names({}) == everything and names(None) == everything, "requête absente → tout")
check(mr.query_tree({"all": [A], "any": [B, C]}) == grp("all", A, grp("any", B, C)), "ancien format converti en arbre")

# Bin intelligent ENREGISTRÉ à l'ancien format : relu tel quel, filtré, jamais réécrit.
old_q = {"q": "", "all": [B], "any": [A, C]}
cur = c.execute("INSERT INTO media_bins(name, query_json, created_at) VALUES (?,?,?)",
                ("ancien", json.dumps(old_q), "2026-10-01T00:00:00"))
c.commit()
bid = cur.lastrowid
check(sorted(a["name"] for a in mr.bin_assets(bid)) == ["ab", "bc"], "bin ancien format : même contenu")
b = next(x for x in mr.list_bins() if x["id"] == bid)
check(b["smart"] and b["count"] == 2 and b["query"] == old_q, "bin ancien format : compté, requête rendue telle quelle")
# Filtre dans le bin : ses `any` ne sont plus perdus (l'ancienne fusion gardait ceux du bin seuls).
check(sorted(a["name"] for a in mr.bin_assets(bid, extra={"any": [{"field": "name", "op": "eq", "value": "bc"},
                                                                  {"field": "name", "op": "eq", "value": "rien"}]}))
      == ["bc"], "filtre dans un bin : son OU s'applique en plus de celui du bin")
check(sorted(a["name"] for a in mr.bin_assets(bid, extra=grp("any", grp("all", C)))) == ["bc"], "filtre arborescent dans un bin ancien")
check(json.loads(c.execute("SELECT query_json FROM media_bins WHERE id=?", (bid,)).fetchone()[0]) == old_q,
      "bin ancien format : pas réécrit par la lecture")
# Bin intelligent au nouveau format : enregistré et relu en arbre.
new_q = {"q": "", "match": "any", "items": [grp("all", A, B), C]}
nid = mr.save_bin("nouveau", query=new_q)
check(sorted(a["name"] for a in mr.bin_assets(nid)) == ["ab", "bc", "c"], "bin nouveau format : (A ET B) OU C")
check(next(x for x in mr.list_bins() if x["id"] == nid)["query"] == new_q, "bin nouveau format : arbre rendu à l'éditeur")


def refused(q, msg):
    try:
        mr.search(q, limit=1)
        check(False, msg)
    except ValueError:
        check(True, msg)


too_deep = A
for _ in range(mr.TREE_MAX_DEPTH + 1):
    too_deep = grp("all", too_deep)
refused(too_deep, "refus au-delà de %d niveaux de groupes" % mr.TREE_MAX_DEPTH)
ok_deep = A
for _ in range(mr.TREE_MAX_DEPTH):
    ok_deep = grp("all", ok_deep)
check(names(ok_deep) == ["a", "ab"], "exactement %d niveaux de groupes : accepté" % mr.TREE_MAX_DEPTH)
refused(grp("all", *([A] * (mr.TREE_MAX_CONDS + 1))), "refus au-delà de %d critères" % mr.TREE_MAX_CONDS)
check(names(grp("any", *([A] * mr.TREE_MAX_CONDS))) == ["a", "ab"], "exactement %d critères : accepté" % mr.TREE_MAX_CONDS)
refused(grp("all", *([grp("all")] * 120)), "refus d'un déluge de groupes vides")
refused(grp("xor", A), "combinaison inconnue refusée")
refused(grp("all", "A"), "critère qui n'est pas un objet refusé")
refused({"match": "all", "items": "A"}, "liste de critères invalide refusée")
refused(grp("all", {"field": "name); DROP TABLE media_assets; --", "op": "eq", "value": 1}), "champ hors liste blanche refusé")
try:
    mr.save_bin("trop", query=too_deep)
    check(False, "un bin trop profond n'est pas enregistré")
except ValueError:
    check(True, "un bin trop profond n'est pas enregistré")

# Injection dans une VALEUR : reste une valeur (paramètre SQL), la table est intacte.
evil = "x') OR 1=1 OR ('"
check(names(grp("any", {"field": "name", "op": "eq", "value": evil}, {"field": "name", "op": "contains", "value": evil})) == [],
      "valeur hostile : aucun résultat, pas d'ouverture")
check(names(grp("all", grp("any", {"field": "name", "op": "eq", "value": "'; DROP TABLE media_assets; --"}))) == []
      and c.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 5, "valeur hostile dans un groupe : table intacte")

# Côté éditeur (static/js/media_library.js) : même conversion de l'ancien format que le moteur, et
# l'état de panneau exposé au Gestionnaire (getState/setState) — validé, borné.
import re                                                               # noqa: E402
import shutil                                                           # noqa: E402
import subprocess                                                       # noqa: E402
JS = open(os.path.join(RACINE, "static/js/media_library.js"), encoding="utf-8").read()
check(re.search(r"selectAll, renameFile, openMenu, getState, setState,", JS) is not None,
      "MediaLibrary.mount expose getState / setState")
# Fiche Infos → renommage refusé : la RAISON du serveur est écrite sous le champ (contrôle statique).
check("why.textContent = err.message" in JS and "inp.setAttribute('aria-describedby', why.id)" in JS
      and "if(err) err.message = m" in JS, "renommage refusé : la raison du serveur s'affiche sous le champ")
if shutil.which("node"):
    # Éditeur à LIAISONS (une par paire d'éléments, et/ou réglable seule ; ET lie plus fort que OU) :
    # conversions liaisons ↔ arbre extraites du JS et jugées par le MOTEUR sur la base jetable.
    helpers = JS[JS.index("    const MAX_DEPTH = 4"):JS.index("    function groupHtml(")]
    sanit = JS[JS.index("    function sanitizeTree("):JS.index("    function getState(")]
    totree = JS[JS.index("    function toTree("):JS.index("    // Recherche d'un bin intelligent rechargée")]
    old = {"all": [B], "any": [A, C]}
    deep_js = {"match": "all", "items": [{"match": "any", "items": [{"match": "all", "items": [{"match": "all", "items": [
        {"match": "all", "items": [A]}]}]}]}, B]}
    D = {"field": "name", "op": "eq", "value": "rien"}
    trees = {   # arbres relus (bins, setState) → liaisons → arbre : même résultat au moteur
        "or_of_ands": grp("any", grp("all", A, B), C),
        "and_of_or": grp("all", A, grp("any", B, C)),
        "mixed": grp("any", A, grp("all", B, grp("any", C, D))),
        "nested_same": grp("all", A, grp("all", B, grp("all", C))),
        "any_any": grp("any", A, grp("any", C, D)),
    }
    I = {"old": old, "neu": new_q, "deep": deep_js, "A": A, "B": B, "C": C, "D": D, "trees": trees}
    prog = ("const st={tree:null}; const L=(k,d)=>d; const fieldOf=()=>null; const noVal=()=>false; const valLabel=(f,v)=>String(v);\n"
            + helpers + sanit + totree + "\nconst I=%s; const {A,B,C,D}=I;\n" % json.dumps(I) + r"""
const lk = (items, links) => ({items, links});
const out = {conv: toTree(I.old), conv2: toTree(I.neu), deep: sanitizeTree(I.deep, 1),
  bad: sanitizeTree({match:'x', items:[{field:1, op:'eq'}, 'z', {field:'name', op:'eq', value:[1]}, I.A]}, 1),
  q_and_or: toQuery(lk([A, B, C], ['and', 'or'])),
  q_or_and: toQuery(lk([A, B, C], ['or', 'and'])),
  q_4: toQuery(lk([A, B, C, D], ['and', 'or', 'and'])),
  q_all: toQuery(lk([A, B, C], ['and', 'and'])), q_any: toQuery(lk([A, B, C], ['or', 'or'])),
  q_grp: toQuery(lk([A, {items:[B, C], links:['or'], grp:true}], ['and'])),
  q_empty: toQuery(lk([A, {items:[], links:[], grp:true}, {field:'', op:''}, B], ['or', 'and', 'and'])),
  ed_old: fromTree(toTree(I.old)), ed_or_of_ands: fromTree(I.trees.or_of_ands),
  rt: Object.fromEntries(Object.entries(I.trees).map(([k, t]) => [k, toQuery(fromTree(t))])),
  rt2: Object.fromEntries(Object.entries(I.trees).map(([k, t]) => [k, toQuery(fromTree(toQuery(fromTree(t))))])),
  read: exprText(toQuery(lk([{field:'a', op:'eq', value:1}, {field:'b', op:'eq', value:2}, {field:'c', op:'eq', value:3}, {field:'d', op:'eq', value:4}], ['and', 'or', 'and'])), false),
  rm: (() => { const g = lk([A, B, C], ['or', 'and']); removeAt(g, 1); const h = lk([A, B, C], ['and', 'or']); removeAt(h, 0); const m = lk([A, B, C], ['and', 'or']); removeAt(m, 1); return [g.links, h.links, m.links]; })(),
  ed_same: fromTree(I.trees.nested_same),
};
console.log(JSON.stringify(out));""")
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
        print(r.stderr[-1500:])
    check(out.get("conv") == mr.query_tree(old), "éditeur : ancien format → même arbre que le moteur")
    check(out.get("conv2") == mr.query_tree(new_q), "éditeur : nouveau format rechargé tel quel")
    check(out.get("deep") == {"match": "all", "items": [{"match": "any", "items": [{"match": "all", "items": [
        {"match": "all", "items": []}]}]}, B]}, "setState : un groupe au-delà de 4 niveaux est écarté")
    check(out.get("bad") == {"match": "all", "items": [A]}, "setState : combinaison et critères invalides écartés")
    q = lambda k: names(out[k]) if k in out else None                                      # noqa: E731
    check(q("q_and_or") == ["ab", "bc", "c"], "liaisons « A et B ou C » = (A ET B) OU C")
    check(q("q_or_and") == ["a", "ab", "bc"], "liaisons « A ou B et C » = A OU (B ET C) — ET lie plus fort")
    check(out.get("q_4") == grp("any", grp("all", A, B), grp("all", C, D)), "« A et B ou C et D » = (A ET B) OU (C ET D)")
    check(out.get("q_all") == grp("all", A, B, C) and out.get("q_any") == grp("any", A, B, C), "liaisons toutes égales → un seul groupe")
    check(q("q_grp") == ["ab"], "groupe explicite : A et (B ou C)")
    check(out.get("q_empty") == grp("any", A, B), "groupe vide et ligne incomplète retirés, le OU qui les entourait reste")
    check(out.get("read") == "(a eq 1 ET b eq 2) OU (c eq 3 ET d eq 4)", "lecture en clair : parenthèses de la précédence")
    check(out.get("rm") == [["or"], ["or"], ["or"]], "retrait d'un élément : la liaison la plus faible (OU) reste")
    check(out.get("ed_old") == {"items": [B, {"items": [A, C], "links": ["or"], "grp": True}], "links": ["and"]},
          "bin ancien format → B et (A ou C) : groupe explicite là où la précédence ne suffit pas")
    check(out.get("ed_or_of_ands") == {"items": [A, B, C], "links": ["and", "or"]}, "(A ET B) OU C relu en liaisons, sans groupe")
    check(out.get("ed_same") == {"items": [A, B, C], "links": ["and", "and"]}, "« tous » dans « tous » : aplati en liaisons ET")
    for k, t in trees.items():
        rt = (out.get("rt") or {}).get(k)
        check(rt is not None and names(rt) == names(t) and out["rt2"][k] == rt,
              "aller-retour arbre → liaisons → arbre (%s) : même résultat, stable" % k)
else:
    print("  (node absent : contrôles de l'éditeur sautés)")

print("\n%d ok, %d en échec" % (ok, fail))
sys.exit(1 if fail else 0)
