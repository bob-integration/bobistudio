#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc d'EXÉCUTION des fonctions de rendu de la page Réglages.
#
# POURQUOI CE BANC EXISTE (incident du 2026-09-01). Un commentaire HTML placé À L'INTÉRIEUR d'un
# gabarit JS (template literal) contenait des accents graves. Le premier FERME la chaîne, et ce
# qui suit — `…join('')[0,3,6,9]` — reste du JavaScript parfaitement VALIDE : une indexation puis
# un gabarit balisé. Conséquences, toutes silencieuses :
#   · `node --check` ne signale rien : il n'y a pas d'erreur de syntaxe ;
#   · la page se sert normalement, avec un code 200 et le bloc <script> au complet ;
#   · `_tslRenderConns` lève à l'EXÉCUTION, et la table des connexions TSL reste vide.
# L'exploitant voit ses réglages TSL disparaître, sans un message nulle part.
#
# CE QU'IL FAIT, ET EN QUOI IL DIFFÈRE DE `check_render_js.py`. Celui-ci EXTRAIT une fonction par
# comptage d'accolades ; sur un gabarit cassé il tronque et livre à node un fragment amputé qui
# s'exécute très bien — vérifié par mutation, il restait vert sur ce défaut. Ici on exécute le
# BLOC <script> ENTIER de la page réellement servie, puis on appelle la fonction de rendu.
#
#   $ ./venv/bin/python tools/verif_rendu_settings.py
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


# Bouchons de PAGE : tout ce qu'un bloc <script> attend du navigateur ou d'un autre bloc.
# `setInterval`/`setTimeout` rendent 0 sans jamais rappeler : les onglets arment des sondages,
# et un banc qui les laisse tourner ne se termine pas.
PRELUDE = """
const DOM = {};
const EL = () => ({innerHTML:'', value:'', checked:false, style:{}, dataset:{}, hidden:false,
                   textContent:'', querySelector:()=>EL(), querySelectorAll:()=>[],
                   addEventListener:()=>{}, closest:()=>EL(), appendChild:()=>{}, remove:()=>{},
                   insertAdjacentHTML:()=>{},
                   classList:{add(){}, remove(){}, toggle(){}, contains:()=>false}});
global.window = { t:(k,d)=>d||k, addEventListener:()=>{}, matchMedia:()=>({matches:false}),
                  localStorage:{getItem:()=>null, setItem:()=>{}, removeItem:()=>{}} };
global.document = { getElementById:(id)=>(DOM[id]=DOM[id]||EL()), querySelector:()=>null,
                    querySelectorAll:()=>[], addEventListener:()=>{}, createElement:()=>EL(),
                    body: EL() };
global.fetch = async () => ({ ok:false, status:503, json: async () => ({}) });
global.mxlToast = () => {};
// Helpers définis dans D'AUTRES blocs de la page : on les fournit, sinon le bloc sous test
// échoue pour une raison qui n'a rien à voir avec lui.
global.setT = (k, d) => d || k;
global.apEsc = (s) => String(s == null ? '' : s);
global.escapeHtml = (s) => String(s == null ? '' : s);
global.prompt = () => null;
global.confirm = () => false;
global.setInterval = () => 0;
// Fonctions définies dans D'AUTRES blocs de la page et appelées au chargement de celui-ci.
// Sans elles le bloc lève avant d'arriver à ce qu'on teste, pour une raison qui n'est pas la
// sienne. On les bouchonne au lieu de découper le bloc : c'est justement l'exécution du bloc
// ENTIER qui attrape les gabarits cassés.
global.chargerNmosAvance = () => {};
global.rafraichirNmosStatus = () => {};
global.rafraichirNmosIs12 = () => {};
global.rafraichirNmosRegistry = () => {};
global.setTimeout = () => 0;
global.requestAnimationFrame = () => 0;
"""


def rendre(page="/settings"):
    """HTML réellement servi, via le client de test Flask (session forgée)."""
    import main
    from app.database import get_db
    app = main.app
    app.config["TESTING"] = True
    with get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    if not u:
        raise SystemExit("aucun utilisateur en base : impossible de forger une session")
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]
    r = cli.get(page)
    if r.status_code != 200:
        raise SystemExit("%s a répondu %s" % (page, r.status_code))
    return cli, r.data.decode("utf-8", "replace")


def executer(bloc, donnees, appel, attendu):
    """Exécute le bloc ENTIER puis `appel`. Renvoie (ok, sortie)."""
    # ⚠ ON N'INJECTE LES DONNÉES QU'APRÈS LE BLOC. Les déclarer avant entre en collision avec
    # les `let` du bloc lui-même (« Identifier has already been declared »), et le banc échouerait
    # pour une raison qui n'est pas celle qu'il traque.
    src = (PRELUDE
           + bloc
           + "\n" + "".join("%s = %s;\n" % (k, json.dumps(v)) for k, v in donnees.items())
           + "\ntry { " + appel + " } catch (e) { console.log('EXCEPTION:' + e.message); }"
           + "\nprocess.exit(0);\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(src)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        out = (r.stdout or r.stderr).strip()
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    finally:
        os.unlink(f.name)
    return (attendu in out and "EXCEPTION" not in out), out


print("Réglages — les fonctions de rendu S'EXÉCUTENT, pas seulement se compilent\n")

cli, html = rendre()
blocs = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
controle("★ la page rend des scripts inline, sinon le banc ne prouve rien", len(blocs) > 3,
         "obtenu %d" % len(blocs))

api = {p: cli.get(p).get_json() for p in ("/api/tsl/connections", "/api/tally/levels",
                                          "/api/tsl/label_names")}
controle("★ les API de la page répondent", all(v is not None for v in api.values()))

# ── Connexions TSL ───────────────────────────────────────────────────────────
bloc = next((b for b in blocs if "_tslRenderConns" in b), None)
controle("★★ le bloc de l'onglet TSL est bien servi", bloc is not None,
         "un onglet de service absent de la page, c'est un blueprint non enregistré")
if bloc:
    donnees = {"_tslConns": api["/api/tsl/connections"],
               "_tslNiveaux": api["/api/tally/levels"],
               "_tslLabelNames": api["/api/tsl/label_names"],
               "_tslProjects": [], "_tslMapByConn": {}}
    ok, out = executer(
        bloc, donnees,
        "_tslRenderConns(); const h = DOM['tsl_conn_tbody'].innerHTML;"
        " console.log('LIGNES:' + (h.match(/<tr/g)||[]).length"
        " + '|NIV:' + (h.match(/tsl-c-niv\"/g)||[]).length);",
        "LIGNES:%d|NIV:%d" % (len(api["/api/tsl/connections"]),
                              len(api["/api/tsl/connections"])))
    controle("★★★ `_tslRenderConns` s'exécute et rend une ligne par connexion", ok,
             "c'est ICI qu'un accent grave dans un commentaire de gabarit se voit : la fonction "
             "lève, la table reste vide, et rien d'autre ne le signale. Obtenu %r" % out[:200])
    # ⚠ ON COMPTE DANS LE SEUL MENU DES NIVEAUX. Une première version comptait « selected »
    # sur toute la ligne : la colonne des libellés en produit aussi, donc le contrôle restait
    # vert même en cassant l'appariement du niveau (vérifié par mutation).
    ok2, out2 = executer(
        bloc, donnees,
        "_tslRenderConns(); const h = DOM['tsl_conn_tbody'].innerHTML;"
        " const m = h.match(/<select class=\"tsl-c-niv\"[\\s\\S]*?<\\/select>/g) || [];"
        " console.log('SEL:' + m.filter(x => x.includes('selected')).length"
        " + '/' + m.length);",
        "SEL:")
    attendu2 = "SEL:%d/%d" % (len([c for c in api["/api/tsl/connections"] if c.get("level_uuid")]),
                              len(api["/api/tsl/connections"]))
    controle("★★★ le niveau affecté ressort SÉLECTIONNÉ dans SON menu",
             attendu2 in out2,
             "un menu qui ne présélectionne rien fait croire qu'aucun niveau n'est affecté, et "
             "le premier enregistrement l'efface pour de bon. Attendu %s, obtenu %r"
             % (attendu2, out2[:140]))

# ── Plan du tally ────────────────────────────────────────────────────────────
bloc_t = next((b for b in blocs if "async function tallyCharger" in b), None)
controle("★★ le bloc du plan de tally est servi", bloc_t is not None)
if bloc_t:
    ok, out = executer(
        bloc_t, {}, "_tallyEntetes(); console.log('ENTETES:ok');", "ENTETES:ok")
    controle("★★ `_tallyEntetes` s'exécute", ok, "obtenu %r" % out[:160])

# ── IS-07 entrant : l'écran où l'on crée une connexion ───────────────────────
bloc_i7 = next((b for b in blocs if "function is07Rendre" in b), None)
controle("★★ le bloc de l'onglet Tally IS-07 est servi", bloc_i7 is not None,
         "sans lui, il n'y a AUCUN endroit pour créer une connexion entrante — l'API existe, "
         "mais personne ne peut s'en servir")
controle("★★ et son panneau est dans la page",
         'id="nmos-tab-tally"' in html,
         "un bloc de script sans son panneau ne s'affiche nulle part")
if bloc_i7:
    from app.database import (db_upsert_is07_connection, db_delete_is07_connection,
                              db_get_tally_levels)
    niv = db_get_tally_levels()
    cid = db_upsert_is07_connection({"name": "banc rendu", "enabled": 1,
                                     "level_uuid": niv[0]["uuid"] if niv else None})
    try:
        conns = (cli.get("/api/tally/is07/connections").get_json() or {}).get("connections") or []
        ok, out = executer(
            bloc_i7, {"_is07Conns": conns, "_is07Niveaux": niv},
            "is07Rendre(); const h = DOM['is07_conns_tbody'].innerHTML;"
            " console.log('L:' + (h.match(/<tr/g)||[]).length"
            " + '|SEL:' + (h.match(/is07-niveau/g)||[]).length"
            " + '|CHOISI:' + (h.match(/selected/g)||[]).length);",
            "L:%d|SEL:%d|CHOISI:%d" % (len(conns), len(conns), len(conns) if niv else 0))
        controle("★★★ `is07Rendre` s'exécute et rend une ligne par connexion", ok,
                 "obtenu %r" % out[:200])
        ok2, out2 = executer(
            bloc_i7, {"_is07Conns": conns, "_is07Niveaux": niv},
            "is07Rendre(); const h = DOM['is07_conns_tbody'].innerHTML;"
            " console.log('EDITABLE:' + /class=\"is07-nom\"/.test(h)"
            " + '|URL_LECTURE:' + !/is07-uri/.test(h));", "EDITABLE:true|URL_LECTURE:true")
        controle("★★★ le nom et le niveau s'éditent, l'URL du contrôleur NON", ok2,
                 "IS-05 décrit la connexion, pas l'intention d'exploitation : offrir un champ "
                 "d'URL ici ferait croire qu'on peut la choisir. Obtenu %r" % out2[:160])
    finally:
        db_delete_is07_connection(cid)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
