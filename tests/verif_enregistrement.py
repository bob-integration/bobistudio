#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Enregistrement d'une installation (app/registration.py, 2026-11-08) : un mail PRÉPARÉ.

Décidé avec Cyril : un mail vers register@bobi.studio, que l'administrateur envoie lui-même.
Ce que la garde tient :
  1. la fenêtre n'est proposée qu'un jour après la première visite, jamais après « Ne plus me
     proposer » ni après un enregistrement ; « Plus tard » la ramène dans 30 jours ;
  2. le mail est EXACT : destinataire fixé dans le code, objet, chaque champ, et les faits de
     l'installation seulement si la case est cochée ; le lien mailto les porte tels quels ;
  3. le formulaire est borné : champs inconnus jetés, usage en vocabulaire fermé, organisation
     obligatoire ;
  4. l'adresse n'est PAS un réglage : écrire `register_email` en base n'y change rien ;
  5. RIEN ne part du contrôleur : le module n'importe aucune bibliothèque réseau ;
  6. l'API ne parle qu'aux administrateurs, et le chemin léger (chaque page) ne fait pas
     l'inventaire ;
  7. la page Réglages porte le formulaire et la mise en page la fenêtre, libellés fr et en.
"""
import ast
import json
import os
import re
import sys
import tempfile
import urllib.parse

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                   # noqa: E402

ECHECS = []
JOUR = 86400


def verifier(cond, libelle, detail=None):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ECHECS.append(libelle)


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import registration as reg, settings as st

    # ── 1. quand proposer ────────────────────────────────────────────────────────────────────
    t0 = 1_000_000.0
    verifier(not reg.offer_due(now=t0), "première visite : pas de fenêtre (on est en pleine installation)")
    verifier(not reg.offer_due(now=t0 + JOUR - 1), "… ni moins d'un jour après")
    verifier(reg.offer_due(now=t0 + JOUR), "un jour après la première visite : proposée")
    reg.act("later", now=t0 + JOUR)
    verifier(not reg.offer_due(now=t0 + JOUR + 29 * JOUR), "« Plus tard » : pas avant 30 jours")
    verifier(reg.offer_due(now=t0 + JOUR + 30 * JOUR), "… et de nouveau proposée à 30 jours")
    reg.act("decline", now=t0 + 40 * JOUR)
    verifier(not reg.offer_due(now=t0 + 4000 * JOUR), "« Ne plus me proposer » : plus jamais")
    reg.act("reset", now=t0 + 41 * JOUR)
    verifier(reg.offer_due(now=t0 + 41 * JOUR), "« Proposer de nouveau » garde la première visite")

    # ── 2. le mail exact ─────────────────────────────────────────────────────────────────────
    info = {"org": "Télé Exemple", "name": "Camille", "email": "camille@exemple.invalid",
            "country": "France", "usage": "ob_van", "comment": "Deux cars, 12 caméras",
            "facts": True, "pirate": "x" * 50}
    st_, mail = reg.act("prepare", info, now=t0 + 42 * JOUR)
    verifier(st_["status"] == "registered" and not reg.offer_due(now=t0 + 4000 * JOUR),
             "enregistrée : la fenêtre ne revient plus")
    verifier(mail["to"] == "register@bobi.studio", "destinataire : register@bobi.studio", mail["to"])
    verifier("Télé Exemple" in mail["subject"], "l'objet nomme l'organisation", mail["subject"])
    corps = mail["body"]
    verifier(all(v in corps for v in ("Télé Exemple", "Camille", "camille@exemple.invalid", "France",
                                       "Deux cars, 12 caméras")),
             "chaque champ saisi est dans le corps", corps)
    verifier("ob_van" not in corps and "Car régie" in corps, "l'usage est écrit en clair, pas son code")
    from app.version import VERSION
    verifier(f"Bobi.Studio {VERSION}" in corps, "case cochée : la version est jointe")
    verifier("pirate" not in json.dumps(st_["info"]), "un champ inconnu est jeté")
    q = urllib.parse.urlsplit(mail["mailto"])
    params = urllib.parse.parse_qs(q.query)
    verifier(q.scheme == "mailto" and q.path == "register@bobi.studio"
             and params.get("body") == [corps] and params.get("subject") == [mail["subject"]],
             "le lien mailto porte EXACTEMENT l'objet et le corps affichés")
    sans = reg.compose(dict(info, facts=False))["body"]
    verifier(VERSION not in sans and "Plugins" not in sans,
             "case décochée : ni version ni inventaire", sans)

    # ── 3. formulaire borné ──────────────────────────────────────────────────────────────────
    try:
        reg.act("prepare", {"org": "   "})
        verifier(False, "organisation vide refusée")
    except ValueError:
        verifier(True, "organisation vide refusée")
    verifier(reg.clean({"org": "x", "usage": "espionnage"})["usage"] == "",
             "usage hors liste : effacé")
    verifier(len(reg.clean({"org": "x" * 5000})["org"]) == 200, "champ borné en longueur")
    try:
        reg.act("pirater")
        verifier(False, "action inconnue refusée")
    except ValueError:
        verifier(True, "action inconnue refusée")

    # ── 4. l'adresse n'est pas un réglage ────────────────────────────────────────────────────
    st.set("register_email", "attaquant@exemple.invalid")
    verifier(reg.email() == "register@bobi.studio", "un réglage en base ne détourne pas l'adresse")

    # ── 5. rien ne part du contrôleur ────────────────────────────────────────────────────────
    arbre = ast.parse(open(os.path.join(RACINE, "app", "registration.py"), encoding="utf-8").read())
    modules = {(n.module or "") for n in ast.walk(arbre) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(arbre) if isinstance(n, ast.Import) for a in n.names}
    reseau = sorted(m for m in modules if m.split(".")[0] in
                    ("socket", "smtplib", "http", "requests", "urllib3") or m == "urllib.request"
                    or m.endswith("catalogue") or m.endswith("node_driver"))
    verifier(not reseau, "le module n'importe aucune bibliothèque réseau", reseau)

    # ── 6. API ───────────────────────────────────────────────────────────────────────────────
    import main as _main
    from app.auth import hash_password
    admin = database.db_create_user("adm", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    lect = database.db_create_user("lec", hash_password("Tulipe-Vent-9312"), "viewer", None, None, None)

    def client(uid):
        c = _main.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = uid
        return c
    ca, cl = client(admin), client(lect)
    verifier(cl.get("/api/registration?full=1").get_json() == {"offer": False},
             "lecture seule : rien (ni état, ni inventaire)")
    verifier(cl.post("/api/registration", json={"action": "decline"}).status_code in (401, 403),
             "lecture seule : ne peut pas décider pour le site")
    leger = ca.get("/api/registration").get_json()
    verifier(set(leger) == {"offer"}, "chemin léger (chaque page) : la décision seule", leger)
    plein = ca.get("/api/registration?full=1").get_json()
    verifier(plein["email"] == "register@bobi.studio" and "facts" in plein and plein["status"] == "registered",
             "chemin complet (Réglages) : état, adresse, inventaire")
    r = ca.post("/api/registration", json={"action": "prepare", "info": {"org": ""}})
    verifier(r.status_code == 400, "organisation vide refusée par l'API")
    r = ca.post("/api/registration/preview", json={"info": {"org": "Aperçu SA"}}).get_json()
    verifier("Aperçu SA" in r["subject"] and reg.state()["info"]["org"] == "Télé Exemple",
             "l'aperçu suit la saisie SANS rien enregistrer")

    # ── 7. pages et libellés ─────────────────────────────────────────────────────────────────
    page = ca.get("/settings").get_data(as_text=True)
    verifier('id="reg"' in page and all(f'id="reg_{k}"' in page for k in
                                         ("org", "name", "email", "country", "usage", "comment", "facts")),
             "Réglages → Mises à jour porte le formulaire")
    dlg = page[page.index('<dialog id="cat-reg-dlg"'):]
    verifier('id="reg"' in dlg[:dlg.index("</dialog>")],
             "le formulaire vit dans la fenêtre « Enregistrer l'installation… », pas sur la page")
    verifier("/settings?enregistrer=1#catalogue" in ca.get("/").get_data(as_text=True)
             and "get('enregistrer') === '1'" in page,
             "« S'enregistrer… » de la fenêtre d'accueil ouvre directement le formulaire")
    sec = page[page.index('id="reg"'):page.index('id="reg-mail-dlg"')]
    verifier("oninput" not in sec and "regApercu" not in page,
             "plus d'aperçu recalculé à chaque frappe : le mail se compose au clic")
    verifier('onclick="regAfficher()"' in sec and 'onclick="regMesInfos()"' in sec,
             "boutons « Afficher le mail » et « Reprendre mes informations »")
    verifier(all(f'id="reg_{k}" class="ctl-input"' in page for k in ("org", "name", "email", "country", "usage", "comment")),
             "les champs portent le style du catalogue partagé (ctl-input)")
    accueil = ca.get("/").get_data(as_text=True)
    verifier('id="reg-dlg"' in accueil and all(f'data-reg-act="{a}"' in accueil
                                               for a in ("register", "later", "decline")),
             "la fenêtre et ses trois choix sont dans la mise en page commune")
    gab = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
    gab += open(os.path.join(RACINE, "templates", "layout.html"), encoding="utf-8").read()
    cles = set(re.findall(r"settings\.reg\.[a-z_]+", gab))
    cles |= {"settings.reg.usage_" + u for u in reg.USAGES}
    cles |= set(re.findall(r'"(settings\.reg\.[a-z_]+)"',
                           open(os.path.join(RACINE, "app", "registration.py"), encoding="utf-8").read()))
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(RACINE, "i18n", f"{lang}.json"), encoding="utf-8"))
        manque = sorted(k for k in cles if k not in cat and not k.endswith("_"))
        verifier(not manque, f"libellés présents en {lang} ({len(cles)})", manque)

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
