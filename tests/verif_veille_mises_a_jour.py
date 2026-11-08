#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Veille des mises à jour (app/update_watch.py, 2026-11-08) : prévenir, jamais installer.

★ POURQUOI. Le produit savait comparer sa version à celle publiée sur GitHub, mais seulement
quand quelqu'un ouvrait Réglages → Mises à jour : personne n'était jamais PRÉVENU. Décidé avec
Cyril : vérification ACTIVÉE par défaut (une fois par jour, cœur seul, alerte + bandeau), dite à
l'installation, désactivable ; prévenir par alerte, bandeau ou fenêtre, au choix.

Ce que la garde tient :
  1. les défauts décidés ;
  2. une version publiée → UNE alerte, jamais deux pour la même ; la suivante → une de plus ;
  3. « plugins et services » ajoute les composants ; « cœur seul » ne les regarde même pas ;
  4. `catalogue_actif` = 0 l'emporte : AUCUN appel réseau, état « offline » ;
  5. GitHub muet → ni alerte ni « à jour » affirmé ;
  6. la boucle respecte l'interrupteur et la fréquence ;
  7. le bandeau ne parle qu'aux administrateurs, « Ignorer » tient jusqu'à la version suivante ;
  8. la saisie refuse une fréquence ou une portée hors vocabulaire ;
  9. l'installation le dit : résumé de l'installeur et étape de l'assistant, qui ÉCRIT le réglage.

GitHub est simulé : `catalogue.maj_core_disponible` / `lister` / `_reglages` sont remplacés.
Leur forme est celle des fonctions réelles (lue dans app/catalogue.py), pas inventée.
"""
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                   # noqa: E402

ECHECS = []


def verifier(cond, libelle, detail=None):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ECHECS.append(libelle)


class GitHub:
    """Ce que « publie » l'organisation pendant le scénario."""
    def __init__(self):
        self.core = "0.9.11"
        self.installee = "0.9.10"
        self.muet = False
        self.actif = True
        self.composants = []
        self.appels = 0

    def maj_core_disponible(self, force=False):
        self.appels += 1
        if self.muet:
            return False, None
        info = {"version": self.core, "tag": "v" + self.core, "url": "https://example.invalid/r",
                "version_installee": self.installee, "artefacts": ["bobistudio.zip"]}
        return self.core != self.installee, info

    def lister(self, force=False):
        self.appels += 1
        return {"actif": True, "entrees": list(self.composants), "erreur": None}

    def _reglages(self):
        return "org", 1800, self.actif


def cles_alertes():
    with database.get_db() as db:
        return [r[0] for r in db.execute(
            "SELECT msg_key FROM alerts WHERE msg_key LIKE 'alert.update.%' ORDER BY id")]


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    from app import catalogue, settings as st, update_watch as uw
    gh = GitHub()
    catalogue.maj_core_disponible = gh.maj_core_disponible
    catalogue.lister = gh.lister
    catalogue._reglages = gh._reglages

    # ── 1. défauts ───────────────────────────────────────────────────────────────────────────
    cfg = uw._settings()
    verifier(cfg == {"enabled": True, "interval_h": 24, "scope": "core", "alert": True,
                     "banner": True, "popup": False},
             "défauts : activée, une fois par jour, cœur seul, alerte + bandeau, pas de fenêtre", cfg)

    # ── 2. une alerte par version publiée ────────────────────────────────────────────────────
    e = uw.check(now=1000)
    verifier([i["key"] for i in e["available"]] == ["core"] and e["error"] is None,
             "une version publiée plus récente est trouvée", e)
    verifier(cles_alertes() == ["alert.update.core_available"], "… et annoncée par UNE alerte",
             cles_alertes())
    uw.check(now=2000)
    verifier(len(cles_alertes()) == 1, "la même version n'est jamais réannoncée", cles_alertes())
    gh.core = "0.9.12"
    uw.check(now=3000)
    verifier(len(cles_alertes()) == 2, "la version suivante, elle, l'est", cles_alertes())
    gh.installee = "0.9.12"
    e = uw.check(now=3500)
    verifier(e["available"] == [] and e["error"] is None, "une fois installée, plus rien n'est annoncé", e)

    st.set("update_notify_alert", "0")
    gh.core = "0.9.13"
    uw.check(now=3600)
    verifier(len(cles_alertes()) == 2, "alerte décochée : aucune ligne au journal", cles_alertes())
    verifier(uw.state()["available"][0]["version"] == "0.9.13",
             "… mais la version est bien trouvée (bandeau / fenêtre restent possibles)")
    st.set("update_notify_alert", "1")

    # ── 3. portée ────────────────────────────────────────────────────────────────────────────
    gh.composants = [
        {"genre": "plugin", "type": "mixer", "label": "Mélangeur", "version_dispo": "0.24.0",
         "version_installee": "0.23.46", "etat": "maj", "url": ""},
        {"genre": "plugin", "type": "split", "label": "Split", "version_dispo": "0.30.1",
         "version_installee": "0.30.1", "etat": "a_jour", "url": ""},
    ]
    avant = gh.appels
    uw.check(now=4000)
    verifier(gh.appels - avant == 1, "cœur seul : le catalogue des composants n'est même pas lu",
             gh.appels - avant)
    st.set("update_check_scope", "all")
    e = uw.check(now=5000)
    verifier(sorted(i["key"] for i in e["available"]) == ["core", "plugin:mixer"],
             "plugins et services : le composant en retard s'ajoute, celui à jour non", e["available"])
    verifier(cles_alertes()[-1] == "alert.update.component_available",
             "un composant a sa propre alerte", cles_alertes())
    st.set("update_check_scope", "core")

    # ── 4. site isolé ────────────────────────────────────────────────────────────────────────
    gh.actif = False
    avant = gh.appels
    e = uw.check(now=6000)
    verifier(gh.appels == avant and e["error"] == "offline" and e["available"] == [],
             "publications en ligne coupées : AUCUN appel, état « offline », rien d'annoncé", e)
    gh.actif = True

    # ── 5. GitHub muet ───────────────────────────────────────────────────────────────────────
    gh.muet = True
    n = len(cles_alertes())
    e = uw.check(now=7000)
    verifier(e["error"] == "core" and e["available"] == [] and len(cles_alertes()) == n,
             "GitHub muet : l'erreur est dite, rien n'est annoncé", e)
    gh.muet = False

    # ── 6. la boucle ─────────────────────────────────────────────────────────────────────────
    t = uw.state()["checked_at"]
    verifier(not uw._due(t + 23 * 3600) and uw._due(t + 24 * 3600),
             "fréquence 24 h : pas dû à 23 h, dû à 24 h")
    st.set("update_check_interval_h", 6)
    verifier(uw._due(t + 6 * 3600), "fréquence 6 h : dû à 6 h")
    st.set("update_check_enabled", "0")
    verifier(not uw._due(t + 1000 * 3600), "désactivée : jamais due")
    st.set("update_check_enabled", "1")
    st.set("update_check_interval_h", 24)

    # ── 7. API : administrateurs seuls, « Ignorer » ──────────────────────────────────────────
    import main as _main
    from app.auth import hash_password
    admin = database.db_create_user("adm", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    lect = database.db_create_user("lec", hash_password("Tulipe-Vent-9312"), "viewer", None, None, None)
    gh.core, gh.installee = "0.9.20", "0.9.10"
    uw.check(now=8000)

    def client(uid):
        c = _main.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
        return c
    ca, cl = client(admin), client(lect)
    j = ca.get("/api/update/notice").get_json()
    verifier(j["enabled"] and j["banner"] and [i["version"] for i in j["items"]] == ["0.9.20"],
             "l'administrateur reçoit l'avis", j)
    j = cl.get("/api/update/notice").get_json()
    verifier(j["items"] == [], "un compte en lecture seule ne reçoit rien", j)
    verifier(cl.post("/api/update/notice/dismiss").status_code in (401, 403),
             "il ne peut pas non plus « ignorer » pour les autres")
    verifier(ca.post("/api/update/notice/dismiss").status_code == 200, "« Ignorer cette version »")
    verifier(ca.get("/api/update/notice").get_json()["items"] == [], "… l'avis se tait pour lui")
    gh.core = "0.9.21"
    uw.check(now=9000)
    verifier([i["version"] for i in ca.get("/api/update/notice").get_json()["items"]] == ["0.9.21"],
             "… et revient à la version suivante")
    j = ca.post("/api/update/watch/check").get_json()
    verifier(j["available"] and j["checked_at"], "« Vérifier maintenant » rend l'état du passage", j)

    # ── 8. saisie ────────────────────────────────────────────────────────────────────────────
    r = ca.post("/api/settings", json={"update_check_interval_h": 5})
    verifier(r.status_code == 400 and st.get("update_check_interval_h") == 24,
             "fréquence hors liste refusée à la saisie", r.status_code)
    r = ca.post("/api/settings", json={"update_check_scope": "tout"})
    verifier(r.status_code == 400 and st.get("update_check_scope") == "core",
             "portée hors liste refusée à la saisie", r.status_code)
    r = ca.post("/api/settings", json={"update_check_interval_h": "168"})
    verifier(r.status_code == 200 and st.get("update_check_interval_h") == 168,
             "une fréquence de la liste est acceptée (et rangée en entier)")

    # ── 9. l'installation le dit, la page le montre ──────────────────────────────────────────
    page = ca.get("/settings").get_data(as_text=True)
    verifier(all(f'id="s_update_{k}"' in page for k in
                 ("check_enabled", "check_interval_h", "check_scope", "notify_alert",
                  "notify_banner", "notify_popup")),
             "Réglages → Mises à jour porte les six réglages")
    # Maquette A (2026-11-08) : les VERSIONS d'abord. Les réglages vivent dans une fenêtre,
    # sinon ils repoussent la liste des plugins tout en bas de l'onglet.
    dlg = page[page.index('<dialog id="cat-regler-dlg"'):]
    dlg = dlg[:dlg.index("</dialog>")]
    verifier('id="s_update_check_enabled"' in dlg and 'id="s_catalogue_actif"' in dlg,
             "les réglages (consultation en ligne, veille) sont dans la fenêtre « Régler »")
    verifier(page.index('id="cat-resume-txt"') < page.index('id="cat-plugins"')
             and 'id="s_update_check_enabled"' not in page[:page.index('<dialog id="cat-regler-dlg"')],
             "la page montre une ligne de résumé, puis les versions")
    accueil = ca.get("/").get_data(as_text=True)
    verifier('id="updw-banner"' in accueil and 'id="updw-dlg"' in accueil,
             "le bandeau et la fenêtre sont dans la mise en page commune")
    assistant = open(os.path.join(RACINE, "templates", "setup_wizard.html"), encoding="utf-8").read()
    etapes = re.search(r"this\.steps = \[(.*?)\];", assistant, re.S).group(1)
    verifier("this.updates" in etapes, "l'assistant de premier démarrage a son étape Mises à jour")
    verifier(re.search(r"wiz\.updates\.save = async function\(\) \{[^}]*update_check_enabled", assistant),
             "… et elle ÉCRIT le réglage (sinon décocher ne ferait rien)")
    inst = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
    verifier(inst.count('info(t("update_default"))') == 2 and inst.count('"update_default":') == 2,
             "l'installeur l'annonce à la fin des deux chemins qui installent un contrôleur, fr et en")
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(RACINE, "i18n", f"{lang}.json"), encoding="utf-8"))
        manque = [k for k in re.findall(r"_\('(settings\.updwatch\.[a-z_0-9]+)'\)", page + accueil)
                  if k not in cat]
        manque += [k for k in ("alert.update.core_available", "alert.update.component_available",
                               "wiz.updates.step", "wiz.updates.title", "wiz.updates.lead",
                               "wiz.updates.enable", "wiz.updates.hint") if k not in cat]
        verifier(not manque, f"libellés présents en {lang}", manque)

    main_py = open(os.path.join(RACINE, "main.py"), encoding="utf-8").read()
    verifier("_update_watch.start()" in main_py, "main.py démarre la veille")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
