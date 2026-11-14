#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""La version de Bobi.Studio est VISIBLE, et la page Mises à jour la confronte à l'amont.

★ CE QU'ON RÉPARE. `app/version.py` fait autorité depuis le 2026-09-02, et deux routes savent
comparer cette instance à la dernière release publiée (`GET /api/update/core`) puis l'appliquer
(`POST /api/update/core/apply`). Aucune page n'appelait ni l'une ni l'autre : mesuré, la chaîne
« 0.9.5 » n'apparaissait dans AUCUN rendu. Un exploitant ne pouvait pas dire ce qu'il faisait
tourner, et un utilisateur extérieur n'avait aucun chemin vers la version suivante — alors que
ses plugins, eux, se mettaient à jour depuis cette même page.

Deux choses vérifiées, et la seconde est celle qui casse en silence :

  1. la version APPARAÎT dans les pages qui comptent, y compris celles qui n'ont pas de session
     (login) — c'est justement à quelqu'un qui n'est pas entré que le support la demande ;

  2. `catCoreCharger()` est EXÉCUTÉ sur les cinq formes que la route peut rendre. Un rendu par
     concaténation de chaînes passe `node --check` quoi qu'il arrive : seule l'exécution dit si
     l'état « pas d'artefact » affiche bien son explication au lieu d'un bouton qui échouerait.

  3. le GESTE `catCoreAppliquer()` est exécuté, serveur simulé : le message s'écrit À CÔTÉ du
     bouton, et la page attend que l'instance annonce la nouvelle version avant de se
     recharger. Le 2026-11-14, sur une 1.0.0, le serveur avait appliqué et redémarré en 4 s ;
     l'exploitant voyait un bouton grisé et une version inchangée — le message était parti en
     haut de l'onglet et rien ne relisait la version.

⚠ La mise à jour n'est PAS appliquée ici : elle tire du code depuis GitHub et redémarre le
service. C'est du banc, jamais de la CI.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                   # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


# Les cinq formes que `GET /api/update/core` peut rendre, et ce qu'on doit lire à l'écran.
# ⚠ AUCUN NUMÉRO DE VERSION EN DUR. `ICI` est la version réelle du code — l'y recopier obligerait
# à éditer ce fichier à chaque release, et un `sed` global y a déjà rendu la version installée
# IDENTIQUE à la publiée, ce qui vidait de son sens le cas « mise à jour disponible ». `AMONT` est
# volontairement un numéro distinct et plus élevé.
from app.version import VERSION as ICI                              # noqa: E402
AMONT = "99.0.0"

CAS = [
    ("à jour",
     {"version_installee": ICI, "disponible": False,
      "derniere": {"version": ICI, "tag": "v" + ICI, "artefacts": ["x"], "applicable": True}},
     [ICI], ["cat-core-btn"]),
    ("mise à jour applicable → un bouton",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": True, "url": "u"}},
     ["cat-core-btn", AMONT], []),
    ("mise à jour SANS artefact → pas de bouton, une explication",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": False,
                   "url": "https://example.invalid/r"}},
     ["cat-core-note"], ["cat-core-btn"]),
    ("pré-version signalée",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": True, "prerelease": True}},
     ["cat-chip new"], []),
    ("amont injoignable → on montre quand même la version locale",
     {"version_installee": ICI, "disponible": False, "derniere": None},
     [ICI, "cat-chip old"], ["cat-core-btn"]),
]

# Le chrome de la page (témoin de connexion, minuteries, stockage local) s'exécute au chargement :
# on lui donne de quoi ne pas planter, sans rien simuler de plus que nécessaire.
STUB = """
global.window = global;
global.window.addEventListener = function(){};
global.setInterval = function(){}; global.setTimeout = function(){};
global.location = { href: '/settings', pathname: '/settings', search: '', hash: '',
                    reload: function(){} };
global.localStorage = { getItem: function(){ return null; }, setItem: function(){},
                        removeItem: function(){} };
Object.defineProperty(globalThis, 'navigator',
    { value: { onLine: true, language: 'fr' }, configurable: true, writable: true });
var _sortie = '';
var _el = { set innerHTML(v){ _sortie = v; }, get innerHTML(){ return _sortie; },
            set textContent(v){ _sortie = v; }, get textContent(){ return _sortie; } };
// ★ Un ÉLÉMENT INERTE pour tout autre id, PAS `null`. Le test exécute TOUS les scripts en
// ligne de la page : un bloc étranger qui cherche son propre élément (bandeau de veille des
// versions, 2026-11-08) plantait sur `null` et `catCoreCharger` n'était jamais atteint — la
// garde échouait sur du code qu'elle ne mesure pas.
// ⚠ Inerte, et surtout DISTINCT de `_el` : si tout id rendait `_el`, un `catCoreCharger` qui
// écrirait dans le MAUVAIS élément passerait quand même. La sortie ne vient que de `cat-core`.
var _inerte = { hidden: false, textContent: '', innerHTML: '', value: '',
                style: {}, classList: { add: function(){}, remove: function(){},
                toggle: function(){}, contains: function(){ return false; } },
                addEventListener: function(){}, appendChild: function(){},
                removeChild: function(){}, setAttribute: function(){},
                focus: function(){}, select: function(){}, close: function(){},
                showModal: function(){}, querySelector: function(){ return null; },
                querySelectorAll: function(){ return []; } };
global.document = { getElementById: function(id){ return id === 'cat-core' ? _el : _inerte; },
                    querySelectorAll: function(){ return []; },
                    querySelector: function(){ return null; },
                    addEventListener: function(){}, readyState: 'complete',
                    body: { classList: { add: function(){}, remove: function(){} } } };
global.REPONSE = null;
global.fetch = async function(){ return { json: async () => global.REPONSE, ok: true, status: 200 }; };
"""


GESTE = """
var _msg = { textContent: '' }, _haut = { textContent: '' }, _btn = { disabled: false };
var _recharge = false, _lectures = 0;
document.getElementById = function(id){
  return id === 'cat-core-msg' ? _msg : (id === 'cat-result' ? _haut : _inerte); };
setTimeout = function(f){ f(); };
mxlConfirm = async function(){ return true; };
location.reload = function(){ _recharge = true; };
fetch = async function(url){
  if (url === '/api/update/core/apply')
    return { ok: APPLY.ok, status: APPLY.ok ? 200 : 400, json: async () => APPLY };
  _lectures++;
  // 1st read: the service is restarting; 2nd: still the old code; then the new version.
  if (_lectures === 1) throw new Error('connection reset');
  return { ok: true, status: 200,
           json: async () => ({ version_installee: _lectures === 2 ? '1.0.0' : '1.0.1' }) };
};
catCoreAppliquer(_btn, 'v1.0.1', '1.0.1').then(function(){
  console.log(JSON.stringify({ msg: _msg.textContent, haut: _haut.textContent,
                               recharge: _recharge, desactive: _btn.disabled,
                               lectures: _lectures })); });
"""


def executer_geste(blocs, apply_reponse):
    """Run catCoreAppliquer() against a simulated server; dict of what the operator sees."""
    f = os.path.join(tempfile.mkdtemp(), "g.js")
    open(f, "w", encoding="utf-8").write(
        # REPONSE `{}`, not null: the page's other inline blocks fetch at load (version-watch
        # banner) and would crash on null before the gesture is reached.
        STUB + "\nglobal.REPONSE = {};\nvar APPLY = %s;\n" % json.dumps(apply_reponse)
        + blocs + GESTE)
    r = subprocess.run(["node", f], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"plante": r.stderr.strip()[:200]}
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    import main as _main
    from app.auth import hash_password
    from app.version import VERSION

    database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    cli = _main.app.test_client()

    # ── 1. La version est VISIBLE. `/login` d'abord : sans session, c'est le cas qui prouve
    #      que l'injection ne dépend pas d'une route métier.
    print("\n── la version apparaît dans les pages")
    verifier(VERSION in cli.get("/login").get_data(as_text=True),
             f"/login affiche {VERSION} (sans session)")
    # `/setup` ne s'affiche que tant qu'AUCUN compte n'existe : on l'interroge donc avant de
    # créer l'administrateur, avec un client neuf.
    import main as _m
    _base2 = os.path.join(tempfile.mkdtemp(), "t2.db")
    config.DB_PATH = _base2
    database.DB_PATH = _base2
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    verifier(VERSION in _m.app.test_client().get("/setup").get_data(as_text=True),
             f"/setup affiche {VERSION} (premier démarrage, aucun compte)")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    with cli.session_transaction() as sess:
        sess["user_id"] = database.db_get_user("essai")["id"]
    for url in ("/", "/aide", "/settings", "/setup/wizard?force=1"):
        verifier(VERSION in cli.get(url).get_data(as_text=True), f"{url} affiche {VERSION}")

    # ── 2. Le bloc du cœur, EXÉCUTÉ sur chaque forme de réponse.
    print("\n── catCoreCharger() sur les cinq réponses possibles")
    page = cli.get("/settings").get_data(as_text=True)
    blocs = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S))
    tmp = tempfile.mkdtemp()
    for libelle, reponse, attendus, interdits in CAS:
        drive = ("\nglobal.REPONSE = %s;\n"
                 "catCoreCharger().then(function(){ console.log(_sortie); });\n"
                 % json.dumps(reponse))
        f = os.path.join(tmp, "c.js")
        open(f, "w", encoding="utf-8").write(STUB + blocs + drive)
        r = subprocess.run(["node", f], capture_output=True, text=True)
        if r.returncode != 0:
            verifier(False, f"{libelle} → le script a planté : {r.stderr.strip()[:160]}")
            continue
        html = r.stdout
        ok = all(a in html for a in attendus) and not any(i in html for i in interdits)
        verifier(ok, libelle)
        if not ok:
            print("      rendu :", html.strip()[:200])

    # ── 2b. Le bouton rendu, CLIQUÉ comme le ferait un navigateur : l'attribut `onclick` est
    #      lu par un analyseur HTML, puis exécuté. Le 2026-11-14, `JSON.stringify` non échappé
    #      fermait l'attribut : le clic levait une SyntaxError et n'envoyait RIEN. Appeler
    #      `catCoreAppliquer()` directement (section 3) ne pouvait pas le voir.
    print("\n── le bouton rendu, cliqué")
    from html.parser import HTMLParser

    class _Onclick(HTMLParser):
        valeur = None

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if d.get("id") == "cat-core-btn":
                _Onclick.valeur = d.get("onclick")

    applicable = next(c[1] for c in CAS if "cat-core-btn" in c[3 - 1] and c[1]["disponible"])
    f = os.path.join(tmp, "b.js")
    open(f, "w", encoding="utf-8").write(
        STUB + blocs + "\nglobal.REPONSE = %s;\ncatCoreCharger().then(function(){ console.log(_sortie); });\n"
        % json.dumps(applicable))
    r = subprocess.run(["node", f], capture_output=True, text=True)
    _Onclick().feed(r.stdout)
    onclick = _Onclick.valeur or ""
    f2 = os.path.join(tmp, "o.js")
    open(f2, "w", encoding="utf-8").write(
        "function catCoreAppliquer(b, tag, v){ console.log(JSON.stringify([tag, v])); }\n"
        "(function(){ %s }).call({});\n" % onclick)
    r2 = subprocess.run(["node", f2], capture_output=True, text=True)
    attendu = json.dumps(["v" + AMONT, AMONT], separators=(",", ":"))
    verifier(r2.returncode == 0 and r2.stdout.strip() == attendu,
             "le clic sur le bouton appelle catCoreAppliquer(%s)" % attendu
             + ("" if r2.returncode == 0 else " — " + r2.stderr.strip().splitlines()[-1][:120]
                if r2.stderr.strip() else ""))

    # ── 3. Le GESTE, exécuté.
    print("\n── catCoreAppliquer() : ce que voit l'exploitant après le clic")
    g = executer_geste(blocs, {"ok": True, "message": "mise à jour v1.0.1 appliquée — redémarrage en cours"})
    verifier("plante" not in g, "le geste s'exécute sans planter" + (" : " + g.get("plante", "") if "plante" in g else ""))
    verifier("1.0.1" in g.get("msg", "") and not g.get("haut"),
             "succès : le message est écrit À CÔTÉ du bouton, pas en haut de l'onglet")
    verifier(g.get("recharge") is True and g.get("lectures", 0) >= 3,
             "succès : la page relit la version jusqu'à la nouvelle, puis se recharge")
    g = executer_geste(blocs, {"ok": False, "message": "téléchargement échoué : quota"})
    verifier("quota" in g.get("msg", "") and g.get("desactive") is False and not g.get("recharge"),
             "refus : le motif s'affiche à côté du bouton, le bouton redevient actif, pas de rechargement")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
