#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Onglet Réglages → Jetons d'API : les boutons Révoquer et Copier MARCHENT.
#
# POURQUOI (constaté le 2026-10-25 sur la Z8, orchestrateur servi en HTTP) :
#   · Révoquer ne faisait rien. Le bouton était bâti par `onclick="jetRevoquer(id, ' +
#     JSON.stringify(label) + ')"` : les guillemets DOUBLES du JSON fermaient l'attribut, le
#     navigateur gardait `jetRevoquer(3, ` et le clic levait une SyntaxError dans la console —
#     même avec un libellé vide (`""`).
#   · Copier ne copiait jamais : `navigator.clipboard` n'existe qu'en contexte sécurisé, et la
#     fonction l'appelait en direct au lieu du helper global `window.copierTexte`.
#
# CE QU'IL FAIT : exécute dans node le bloc <script> RÉELLEMENT servi par /settings (même
# méthode que verif_rendu_settings.py), rend la table avec des libellés piégés, puis ÉVALUE
# chaque onclick tel qu'un navigateur le lirait (entités décodées). Copier est appelé sans
# `navigator`, comme en HTTP.
#
#   $ ./venv/bin/python tests/verif_jetons_api_ui.py
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

TMP = tempfile.mkdtemp(prefix="verif_jetons_api_ui_")
from app import config, database   # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "principale.db")   # jamais la base de prod
database._tls.__dict__.pop("conn", None)
database.init_db()
with database.get_db() as _db:   # utilisateur jetable pour forger la session
    _db.execute("INSERT INTO users (username, password_hash, role) VALUES ('verif', 'x', 'admin')")

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


PRELUDE = """
const DOM = {};
const EL = () => ({innerHTML:'', value:'', checked:false, style:{}, dataset:{}, hidden:false,
                   textContent:'', querySelector:()=>EL(), querySelectorAll:()=>[],
                   addEventListener:()=>{}, closest:()=>EL(), appendChild:()=>{}, remove:()=>{},
                   insertAdjacentHTML:()=>{}, select(){ this.selected = true; },
                   classList:{add(){}, remove(){}, toggle(){}, contains:()=>false}});
global.window = { t:(k,d)=>d||k, addEventListener:()=>{}, matchMedia:()=>({matches:false}),
                  localStorage:{getItem:()=>null, setItem:()=>{}, removeItem:()=>{}} };
global.document = { getElementById:(id)=>(DOM[id]=DOM[id]||EL()), querySelector:()=>null,
                    querySelectorAll:()=>[], addEventListener:()=>{}, createElement:()=>EL(),
                    body: EL() };
global.fetch = async () => ({ ok:false, status:503, json: async () => ({}) });
const TOASTS = [];
global.mxlToast = (m, k) => { TOASTS.push(k); };
global.setT = (k, d) => d || k;
global.apEsc = (s) => String(s == null ? '' : s);
global.escapeHtml = (s) => String(s == null ? '' : s);
global.prompt = () => null;
global.confirm = () => false;
global.setInterval = () => 0;
global.chargerNmosAvance = () => {};
global.rafraichirNmosStatus = () => {};
global.rafraichirNmosIs12 = () => {};
global.rafraichirNmosRegistry = () => {};
global.setTimeout = () => 0;
global.requestAnimationFrame = () => 0;
"""


def executer(bloc, script):
    """Bloc ENTIER, puis `script` dans une fonction async (pas de process.exit synchrone : il
    tuerait les promesses de jetCharger avant leur fin)."""
    # Le bloc définit son PROPRE mxlToast (qui touche le DOM) : on le rebouchonne après lui.
    src = (PRELUDE + bloc + "\nmxlToast = (m, k) => { TOASTS.push(k); };"
           + "\n(async () => { try { " + script
           + " } catch (e) { console.log('EXCEPTION:' + e.message); } })();\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    f.write(src)
    f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    finally:
        os.unlink(f.name)


def rendre():
    import main
    app = main.app
    app.config["TESTING"] = True
    with database.get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    cli = app.test_client()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]
    r = cli.get("/settings")
    if r.status_code != 200:
        raise SystemExit("/settings a répondu %s" % r.status_code)
    return r.data.decode("utf-8", "replace")


print("Réglages → Jetons d'API : Révoquer et Copier marchent\n")

html = rendre()
blocs = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
bloc = next((b for b in blocs if "async function jetCharger" in b), None)
controle("★ le bloc de l'onglet Jetons d'API est servi", bloc is not None)
if bloc is None:
    sys.exit(1)

# ── Révoquer ─────────────────────────────────────────────────────────────────
# Libellés piégés : vide (le cas le plus courant, cassé lui aussi), guillemets, apostrophe,
# et une tentative d'injection qui doit rester une simple chaîne.
LIBELLES = ["", 'Z8 "essai"', "l'autre", '"); APPELE_INJECTE = true; ("']
jetons = [{"id": i + 1, "label": l, "username": "verif", "prefix": "bobi_ab",
           "last_used_at": None, "revoked_at": None} for i, l in enumerate(LIBELLES)]
out = executer(bloc, """
global.fetch = async (u) => ({ ok: true, status: 200, json: async () =>
    (u === '/api/api-tokens' ? {tokens: %s} : {users: []}) });
await jetCharger();
const h = DOM['jet-tbody'].innerHTML;
// Ce que fait le navigateur : lire l'attribut jusqu'au guillemet fermant, décoder les entités.
const dec = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
                  .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const attrs = [...h.matchAll(/onclick="([^"]*)"/g)].map(m => dec(m[1]));
const appels = [];
global.APPELE_INJECTE = false;
const jetRevoquer = (id, label) => appels.push([id, label]);
for (const a of attrs) { try { eval(a); } catch (e) { appels.push(['ERREUR', e.message]); } }
console.log('APPELS:' + JSON.stringify(appels) + '|INJ:' + global.APPELE_INJECTE);
""" % json.dumps(jetons))
m = re.search(r"APPELS:(.*)\|INJ:(\w+)", out)
appels = json.loads(m.group(1)) if m else None
attendu = [[j["id"], j["label"]] for j in jetons]
controle("★★★ chaque bouton Révoquer appelle jetRevoquer(id, libellé) — libellé vide compris",
         appels == attendu,
         "les guillemets du JSON ferment l'attribut onclick : le clic ne fait rien. "
         "Attendu %r, obtenu %r" % (attendu, out[:300]))
controle("★★ un libellé piégé reste une chaîne, jamais du code", m is not None and m.group(2) == "false",
         "obtenu %r" % out[:200])

# ── Copier ───────────────────────────────────────────────────────────────────
# Pas de `navigator` du tout : c'est le contexte HTTP clair. Le helper global, lui, sait faire.
out = executer(bloc, """
let COPIE = null;
window.copierTexte = (t) => { COPIE = t; return Promise.resolve(true); };
DOM['jet-neuf-val'] = Object.assign(EL(), {value: 'bobi_SECRET'});
jetCopier();
await new Promise(r => r());
await new Promise(r => r());
console.log('COPIE:' + COPIE + '|TOAST:' + TOASTS.join(','));
""")
controle("★★★ Copier passe par window.copierTexte (marche en HTTP)",
         "COPIE:bobi_SECRET|TOAST:success" in out,
         "navigator.clipboard n'existe pas hors HTTPS : l'appel direct échoue toujours sur le "
         "LAN. Obtenu %r" % out[:200])
out = executer(bloc, """
window.copierTexte = (t) => Promise.resolve(false);
DOM['jet-neuf-val'] = Object.assign(EL(), {value: 'bobi_SECRET'});
jetCopier();
await new Promise(r => r());
await new Promise(r => r());
console.log('SEL:' + !!DOM['jet-neuf-val'].selected + '|TOAST:' + TOASTS.join(','));
""")
controle("★★ si la copie échoue, le jeton reste sélectionné et on le DIT",
         "SEL:true|TOAST:info" in out, "obtenu %r" % out[:200])

# ── Les autres boutons « copier » de la page ─────────────────────────────────
# URL Virtual Media, URL de boot PXE, mot de passe root, SDP NMOS : tous appelaient
# `navigator.clipboard` en direct (inerte en HTTP), et les trois premiers mettaient la valeur
# dans une chaîne JS de onclick — une apostrophe dans un mot de passe y cassait le bouton.
bloc_c = next((b for b in blocs if "function copyFromButton" in b), None)
controle("★ le helper copyFromButton est servi", bloc_c is not None)
if bloc_c:
    PIEGE = "pa'ss\"w<d>&"
    out = executer(bloc_c, """
let COPIE = null;
window.copierTexte = (t) => { COPIE = t; return Promise.resolve(true); };
// Ce que voit le navigateur : data-copy écrit par escapeHtml, relu décodé dans dataset.
const brut = %s;
const attr = escapeHtml(brut);
const dec = attr.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
                .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
copyFromButton({dataset: {copy: dec}});
await new Promise(r => r());
await new Promise(r => r());
console.log('EGAL:' + (COPIE === brut) + '|TOAST:' + TOASTS.join(','));
""" % json.dumps(PIEGE))
    controle("★★★ copyFromButton copie la valeur EXACTE (apostrophe, guillemet, chevrons)",
             "EGAL:true|TOAST:success" in out, "obtenu %r" % out[:200])

# Site d'APPEL, pas définition : aucun script servi ne doit appeler navigator.clipboard lui-même
# (commentaires retirés — ils citent justement le piège).
# Seule exception : le helper lui-même (layout.html), qui fait le test de contexte sécurisé.
code = "\n".join(re.sub(r"(^|\s)//.*$", "", ligne) for b in blocs
                 if "window.copierTexte = function" not in b for ligne in b.split("\n"))
directs = re.findall(r"navigator\.clipboard\??\.writeText\s*\(", code)
controle("★★ aucun appel direct à navigator.clipboard dans la page Réglages", not directs,
         "%d appel(s) direct(s) : inertes en HTTP clair" % len(directs))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
