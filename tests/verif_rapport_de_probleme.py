#!/usr/bin/env python3
"""Le rapport de problème ne doit emporter QUE ce qu'il annonce, et l'annoncer avant de partir.

Pourquoi cette garde existe. Le rapport est le seul écran du produit dont la fonction est de faire
SORTIR de l'information. Il porte les réglages, les journaux, l'inventaire et le résultat de
commandes lancées sur chaque nœud : s'il laisse fuir un jeton, c'est nous qui l'aurons demandé, et
la personne nous aura crus. Trois propriétés doivent donc tenir mécaniquement, et pas par la
vigilance de qui ajoutera la prochaine rubrique :

  · le masquage est CENTRAL — une rubrique nouvelle ne peut pas l'éviter ;
  · un secret est NOMMÉ et vidé, jamais omis — sinon relire le rapport ne prouve rien ;
  · ce qui s'affiche est ce qui part — le fichier n'est pas recollecté au téléchargement.

Et deux propriétés apprises ailleurs dans ce projet :
  · une rubrique qui échoue le DIT dans le rapport (l'échec silencieux est l'anti-patron n°1) ;
  · un nœud injoignable est écrit comme injoignable, jamais sauté — « cette machine ne répond
    pas » est souvent la réponse.

Chaque vérification est une fonction, appelée deux fois : une fois sur le vrai code, une fois sur
le code muté. Une garde qui ne se relit pas sur du code cassé ne mesure rien.
Les faux nœuds sont en 192.0.2.x (RFC 5737), jamais une adresse de site.
"""
import io
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import app.crash_tail as CT                                                   # noqa: E402
import app.database as DB                                                     # noqa: E402
import app.node_driver as ND                                                  # noqa: E402
from app import settings as ST                                                # noqa: E402
from app import support_report as R                                           # noqa: E402

_echecs = []


def dire(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, (" — " + detail) if detail else ""))
    if not ok:
        _echecs.append(titre)


def lire(rel):
    with io.open(os.path.join(RACINE, rel), encoding="utf-8") as f:
        return f.read()


SRC_MODULE = lire("app/support_report.py")
SRC_ROUTE = lire("app/routes/support_report_api.py")
SRC_UI = lire("templates/settings.html")
SECTIONS_VRAIES = R.SECTIONS

FAUX_NOEUDS = [{"id": 1, "name": "dell-1", "host": "192.0.2.11", "status": "up",
                "caps": "compute"},
               {"id": 2, "name": "r620-1", "host": "192.0.2.12", "status": "down",
                "caps": "io2110"}]
JETON = "abcdef0123456789"
DANGER = (" rm ", "rm -", " mv ", " dd ", "mkfs", "systemctl start", "systemctl stop",
          "systemctl restart", "apt-get", " apt ", " > ", ">>", "docker run", "docker rm",
          "kill", "reboot", "poweroff", " tee ", "chmod", "chown", "truncate")


def _rubrique_bavarde():
    """Une rubrique qui ne se nettoie pas elle-même : c'est le cas normal, et c'est le point."""
    return ["token=%s" % JETON, "Authorization: Bearer %s" % JETON,
            "mail_password = tres-secret"]


# ─── les quatre propriétés, en fonctions réutilisables ────────────────────────────────────────────
def masquage_tient():
    """Vrai si une rubrique AJOUTÉE voit quand même ses secrets masqués."""
    R.SECTIONS = tuple(list(SECTIONS_VRAIES) + [("bavarde", _rubrique_bavarde)])
    try:
        rap = R.build(include_nodes=False)
    finally:
        R.SECTIONS = SECTIONS_VRAIES
    bav = next((s for s in rap["sections"] if s["id"] == "bavarde"), None)
    corps = "\n".join(bav["lines"]) if bav else ""
    return (bav is not None and JETON not in corps and "tres-secret" not in corps
            and "token" in corps and rap["redacted"] >= 3)


def secrets_nommes_et_vides():
    """Vrai si CHAQUE réglage secret est nommé, vidé, et nulle part en clair."""
    rap = R.build(include_nodes=False)
    regl = next((s for s in rap["sections"] if s["id"] == "reglages"), None)
    if not regl:
        return False
    joint = "\n".join(regl["lines"])
    tous = ST.all()
    secretes = [k for k in tous if ST._is_secret_key(k)]                      # noqa: SLF001
    if not secretes:
        return False
    for k in secretes:
        if not any(l.startswith(k + " ") and R.RETIRE in l for l in regl["lines"]):
            return False
        v = str(tous.get(k) or "")
        if len(v) >= 6 and v in joint:          # une valeur courte serait un faux positif
            return False
    return rap["withheld"] >= len(secretes)


def noeud_injoignable_ecrit():
    """Vrai si un nœud sans agent figure quand même au rapport, nommé comme injoignable."""
    vrais = (DB.db_get_nodes, ND.has_agent, ND.host_exec)
    DB.db_get_nodes = lambda: list(FAUX_NOEUDS)
    ND.has_agent = lambda n: n["id"] == 1
    ND.host_exec = lambda n, cmd, timeout=300, **kw: (0, "sortie de %s" % cmd.split()[0], "")
    try:
        bloc = "\n".join(R._section_noeuds())                                 # noqa: SLF001
    finally:
        DB.db_get_nodes, ND.has_agent, ND.host_exec = vrais
    return ("dell-1" in bloc and "r620-1" in bloc and "aucun agent joignable" in bloc
            and bloc.count("uname :") == 1)


def commandes_en_lecture_seule():
    mauvaises = [(t, c) for t, c in R._NODE_COMMANDS                          # noqa: SLF001
                 if any(d in (" " + c + " ") for d in DANGER)]
    return not mauvaises and len(R._NODE_COMMANDS) >= 5                       # noqa: SLF001


def telechargement_ne_recollecte_pas(src=None):
    src = src if src is not None else SRC_ROUTE
    bloc = src[src.index("def api_support_report_file"):]
    return ("support_report.build(" not in bloc and "recall(" in bloc
            and "report_expired" in bloc)


# ─── 1 ────────────────────────────────────────────────────────────────────────────────────────────
print("1. Le rapport se produit, et chaque rubrique rend des lignes")
rapport = R.build(include_nodes=False)
ids = [s["id"] for s in rapport["sections"]]
dire(bool(ids), "des rubriques sont rendues", ", ".join(ids))
dire("noeuds" not in ids, "sans les nœuds quand on ne les demande pas")
dire(all(isinstance(l, str) for s in rapport["sections"] for l in s["lines"]),
     "toutes les lignes sont du texte")
txt = R.as_text(rapport)
dire(len(txt) > 500, "le fichier a du contenu", "%d octets" % len(txt))
dire(R.filename(rapport).endswith(".txt"), "un seul fichier, texte", R.filename(rapport))

print("\n2. Ce qui s'affiche est ce qui part")
absentes = [l for s in rapport["sections"] for l in s["lines"] if l.strip() and l not in txt]
dire(not absentes, "chaque ligne montrée se retrouve dans le fichier",
     "%d absente(s)" % len(absentes))
dire(telechargement_ne_recollecte_pas(),
     "le téléchargement reprend le rapport en mémoire, sans recollecter")
rid = R.remember(rapport)
dire(R.recall(rid) is rapport, "l'identifiant rend le MÊME rapport, pas un équivalent")
for _ in range(R._MEM_MAX + 2):                                               # noqa: SLF001
    R.remember({"generated": "x", "sections": []})
dire(R.recall(rid) is None, "la mémoire est bornée, et un rapport sorti est dit perdu",
     "%d gardés" % R._MEM_MAX)                                                # noqa: SLF001

print("\n3. Le masquage est CENTRAL (une rubrique nouvelle ne peut pas l'éviter)")
dire(masquage_tient(), "secrets d'une rubrique ajoutée masqués, noms conservés")

print("\n4. Un réglage secret est NOMMÉ et vidé, jamais omis")
_secretes = [k for k in ST.all() if ST._is_secret_key(k)]                     # noqa: SLF001
dire(bool(_secretes), "l'instance a des réglages secrets", "%d" % len(_secretes))
dire(secrets_nommes_et_vides(),
     "tous nommés, tous vidés, aucune valeur en clair", ", ".join(sorted(_secretes)[:3]) + "…")

print("\n5. Jamais les valeurs de site, jamais le contenu de la base")
# Le mot figure dans la docstring du module, qui dit justement qu'on n'y touche pas : chercher
# le mot brut, c'était attraper sa propre définition. On cherche l'USAGE, code commentaires ôtés.
_code = re.sub(r'""".*?"""', "", SRC_MODULE, flags=re.S)
_code = "\n".join(l for l in _code.split("\n") if not l.lstrip().startswith("#"))
dire("config_local" not in _code, "le module ne lit pas config_local.py (valeurs de site)")
dire("getsize" in SRC_MODULE and "SELECT" not in SRC_MODULE.upper(),
     "de la base, on ne donne que la taille")

print("\n6. Une rubrique qui échoue le DIT")


def _rubrique_cassee():
    raise RuntimeError("panne de collecte")


R.SECTIONS = (("cassee", _rubrique_cassee),)
try:
    casse = R.build(include_nodes=False)
finally:
    R.SECTIONS = SECTIONS_VRAIES
dire([s["id"] for s in casse["sections"]] == ["cassee"],
     "la rubrique reste PRÉSENTE au lieu de disparaître")
dire("panne de collecte" in "\n".join(casse["sections"][0]["lines"]),
     "et elle porte la cause de son échec")

print("\n7. Un nœud injoignable est écrit comme injoignable")
dire(noeud_injoignable_ecrit(),
     "les deux nœuds figurent, celui sans agent est nommé, aucune commande n'a tourné chez lui")

print("\n8. Les commandes lancées sur un nœud sont en LECTURE SEULE, et listées")
dire(commandes_en_lecture_seule(), "aucune ne modifie le nœud",
     "%d commandes" % len(R._NODE_COMMANDS))                                  # noqa: SLF001
dire(SRC_MODULE.count("host_exec(") == 1,
     "un seul appel à host_exec, qui ne peut tirer que de cette liste")

print("\n9. Les routes exigent l'administration, et la production est un POST")
routes = re.findall(r'@bp\.route\(([^\n]*)\)\n(@require_\w+\([^\n]*\))', SRC_ROUTE)
dire(len(routes) == 2, "deux routes", str(len(routes)))
dire(all(r[1] == '@require_perm("settings.edit")' for r in routes),
     "toutes deux réservées à settings.edit", str([r[1] for r in routes]))
dire('methods=["POST"]' in routes[0][0],
     "la production est un POST : elle interroge les nœuds, ce n'est pas une lecture")

print("\n10. L'écran dit ce qui part AVANT le bouton")
entete = SRC_UI[SRC_UI.index('id="set-tab-report"'):SRC_UI.index('id="rap_btn"')]
dire("settings.report.what" in entete, "ce que le rapport contient est annoncé avant")
dire("settings.report.never" in entete, "et ce qu'il ne contient jamais aussi")
dire("settings.report.exact" in SRC_UI, "la page dit que ce qu'on lit est ce qui part")
dire('onclick="rapTelecharger()"' in SRC_UI,
     "le téléchargement passe par le code, pour pouvoir dire un refus")
dire("settings.report.expired" in SRC_UI, "et le refus « plus en mémoire » est traduit")

print("\n11. Les libellés existent dans les DEUX langues")
# Les clés de rubrique sont construites en JS ('settings.report.sec.' + s.id) : la regex
# ramassait le préfixe nu. On l'écarte et on vérifie les rubriques par leur vrai identifiant.
cles = sorted(c for c in set(re.findall(r"settings\.report\.[a-z_.]+", SRC_UI))
              if not c.endswith("."))
cles += ["settings.report.sec.%s" % sid for sid, _f in SECTIONS_VRAIES]
dire(len(cles) >= 12, "l'écran utilise les libellés du catalogue", "%d clés" % len(cles))
for lang in ("fr", "en"):
    cat = json.load(io.open(os.path.join(RACINE, "i18n/%s.json" % lang), encoding="utf-8"))
    manque = [c for c in cles + ["settings.tab.report"] if c not in cat]
    dire(not manque, "%s : aucune clé manquante" % lang, ", ".join(manque[:4]))

print("\n12. La page d'aide explique le rapport, et y mène")
SRC_AIDE = lire("templates/aide.html")


def aide_tient(src=None):
    """Vrai si l'article existe, est rangé dans la navigation, et pointe le bon onglet."""
    src = src if src is not None else SRC_AIDE
    if 'id="wiki-art-rapport-probleme"' not in src:
        return False
    if "{ id: 'rapport-probleme'" not in src:
        return False
    art = src[src.index('id="wiki-art-rapport-probleme"'):]
    art = art[:art.index("</template>")]
    # Le lien doit viser l'onglet RÉEL. Un onglet renommé sans ce contrôle laisserait un lien
    # qui ouvre Réglages sur la mauvaise page, sans rien dire.
    return ('href="/settings#report"' in art
            and "config_local" in art            # ce qui n'est jamais pris est écrit
            and "retiré du rapport" in art)      # et comment un secret apparaît


dire('id="wiki-art-rapport-probleme"' in SRC_AIDE, "l'article existe")
dire("{ id: 'rapport-probleme'" in SRC_AIDE, "il est rangé dans la navigation (rubrique Dépannage)")
dire(aide_tient(), "il pointe l'onglet réel et dit ce qui n'est jamais collecté")
_onglets = set(re.findall(r'id="set-tab-([a-z_0-9]+)"', SRC_UI))
dire("report" in _onglets, "et cet onglet existe bel et bien", "/settings#report")
for cible, doù in (("#rapport-probleme", "wiki-art-depannage"),
                   ("#rapport-probleme", "wiki-art-obtenir-aide")):
    bloc = SRC_AIDE[SRC_AIDE.index('id="%s"' % doù):]
    bloc = bloc[:bloc.index("</template>")]
    dire(cible in bloc, "%s y renvoie" % doù.replace("wiki-art-", ""))

# ─── mutations ───────────────────────────────────────────────────────────────────────────────────
print("\n13. MUTATIONS")
MUT = []


def muter(titre, pose, depose, verif):
    pose()
    try:
        pris = not verif()
    except Exception:
        pris = True
    finally:
        depose()
    MUT.append((titre, pris))
    print("  %s %s" % ("✓ attrapée" if pris else "✗ NON attrapée", titre))


_vrai_redact = CT.redact
muter("le masquage central disparaît (chaque rubrique se nettoierait elle-même)",
      lambda: setattr(CT, "redact", lambda t: t),
      lambda: setattr(CT, "redact", _vrai_redact),
      masquage_tient)


def _reglages_bavards():
    """La version sans expurgation : chaque réglage imprimé avec sa valeur."""
    tous = ST.all()
    return [R._kv(k, tous[k]) for k in sorted(tous)]                          # noqa: SLF001


def _remplacer_rubrique(sid, fn):
    """Remplace la rubrique DANS `SECTIONS`. Poser l'attribut du module ne suffirait pas : le
    tuple gèle les références de fonctions à l'import, donc `build` appellerait encore
    l'original — la mutation passerait pour attrapée sans avoir rien changé."""
    R.SECTIONS = tuple((s, fn if s == sid else f) for s, f in SECTIONS_VRAIES)


muter("un réglage secret est imprimé avec sa valeur",
      lambda: _remplacer_rubrique("reglages", _reglages_bavards),
      lambda: setattr(R, "SECTIONS", SECTIONS_VRAIES),
      secrets_nommes_et_vides)


def _noeuds_qui_sautent():
    """La version qui omet un nœud sans agent — le parc semble plus petit qu'il n'est."""
    out = []
    for n in DB.db_get_nodes() or []:
        if not ND.has_agent(n):
            continue
        out.append("── nœud %s (id %s) ──" % (n.get("name"), n.get("id")))
        for titre, cmd in R._NODE_COMMANDS:                                   # noqa: SLF001
            rc, sortie, err = ND.host_exec(n, cmd, timeout=30)
            out.append("  %s :" % titre)
            out.append("    " + (sortie or ""))
    return out


_vrai_noeuds = R._section_noeuds                                              # noqa: SLF001
muter("un nœud sans agent est sauté au lieu d'être écrit",
      lambda: setattr(R, "_section_noeuds", _noeuds_qui_sautent),
      lambda: setattr(R, "_section_noeuds", _vrai_noeuds),
      # `noeud_injoignable_ecrit` appelle R._section_noeuds par l'attribut, donc celle-ci suffit.
      noeud_injoignable_ecrit)

_vrai_cmds = R._NODE_COMMANDS                                                 # noqa: SLF001
muter("une commande qui MODIFIE entre dans la liste",
      lambda: setattr(R, "_NODE_COMMANDS", _vrai_cmds + (("purge", "rm -rf /tmp/x"),)),
      lambda: setattr(R, "_NODE_COMMANDS", _vrai_cmds),
      commandes_en_lecture_seule)

_ROUTE_MUTEE = SRC_ROUTE.replace("    rapport = support_report.recall(rid)",
                                 "    rapport = support_report.build()")
muter("la route de fichier se remet à recollecter",
      lambda: None, lambda: None,
      lambda: telechargement_ne_recollecte_pas(_ROUTE_MUTEE))

_AIDE_MUTEE = SRC_AIDE.replace('href="/settings#report"', 'href="/settings#rapport"')
muter("le lien de l'aide vise un onglet qui n'existe pas",
      lambda: None, lambda: None,
      lambda: aide_tient(_AIDE_MUTEE))

rates = [t for t, pris in MUT if not pris]
print("\n%d/%d mutations attrapées" % (len(MUT) - len(rates), len(MUT)))
_echecs.extend("mutation non attrapée : " + t for t in rates)
print("\n%s" % ("✓ tout passe" if not _echecs
                else "✗ %d échec(s) : %s" % (len(_echecs), "; ".join(_echecs[:5]))))
sys.exit(1 if _echecs else 0)
