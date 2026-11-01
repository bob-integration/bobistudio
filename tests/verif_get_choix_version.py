#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le MENU DE CHOIX DE VERSION de `get.sh` : il doit obéir, et se taire.

★ LE DÉFAUT QUI JUSTIFIE CE BANC — 2026-10-23, trouvé en pilotant le script PUBLIÉ par
pseudo-terminal, après la publication de la 0.9.9. Sur une simple frappe d'Entrée — le geste le
plus courant, celui du défaut proposé — le script affichait :

    get.sh: ligne 303 : [:  : nombre entier attendu comme expression

Le `case` testait `"${rep_:-1}"`, la valeur PAR DÉFAUT, et son corps lisait `"$rep_"` BRUT, qui
est vide dans ce cas. Le `case` voyait donc « 1 » et branchait sur le test numérique, qui recevait
du vide. ⚠ Et il TOMBAIT JUSTE : le repli valant `tags[0]`, c'est-à-dire précisément le défaut
voulu, la version retenue était la bonne. Un défaut qui donne le bon résultat pour la mauvaise
raison ne se corrige que si on le voit — d'où ce banc, qui exige les DEUX : le bon choix ET
l'absence de bruit. Présent depuis le 2026-08-31, invisible de tous les bancs existants parce que
`verif_get_api_injoignable.py` rend 403 sur toute l'API : le menu n'y apparaît jamais.

Aucun réseau : faux GitHub sur 127.0.0.1. Pseudo-terminal OBLIGATOIRE — sans TTY, `get.sh` part
en « non interactif » et saute le menu, donc le chemin fautif.
"""
import http.server
import io
import json
import os
import pty
import re
import select
import subprocess
import sys
import tarfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET_SH = os.path.join(RACINE, "get.sh")
TAGS = ["v9.9.9", "v9.9.8", "v9.9.7"]
ECHECS = []


def verifier(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        if detail:
            print("      " + str(detail)[:300].replace("\n", " | "))
        ECHECS.append(libelle)


def _archive(chemin):
    noms = ["main.py", "app/__init__.py", "install/install.py",
            "node_agent/install-node.sh", "plugins/_compute_runtime/meta.json"]
    if "service-nmos" in chemin:
        noms = ["__init__.py"]
    brut = io.BytesIO()
    with tarfile.open(fileobj=brut, mode="w:gz") as t:
        for n in noms:
            info = tarfile.TarInfo("racine/" + n)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    return brut.getvalue()


class FauxGitHub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if "/tags" in self.path:
            corps = json.dumps([{"name": t} for t in TAGS]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        if "/tar.gz/" in self.path:
            corps = _archive(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        # tout le reste de l'API : 404. Le repli des sous-modules sur « main » doit tenir.
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def _lancer(base, frappe, travail):
    """Pilote `get.sh --dry-run` par PTY, répond `frappe` à l'invite de version. Rend la sortie."""
    env = dict(os.environ, BOBI_API=base, BOBI_CODELOAD=base, BOBI_LANG="fr", TERM="dumb")
    env.pop("GITHUB_TOKEN", None)
    os.makedirs(travail, exist_ok=True)
    maitre, esclave = pty.openpty()
    p = subprocess.Popen(["bash", GET_SH, "--dry-run"], stdin=esclave, stdout=esclave,
                         stderr=esclave, env=env, cwd=travail, start_new_session=True)
    os.close(esclave)
    buf, t0, repondu = b"", time.time(), False
    while time.time() - t0 < 90:
        fini = p.poll() is not None
        pret = select.select([maitre], [], [], 0.4)[0]
        if fini and not pret:
            break
        if pret:
            try:
                d = os.read(maitre, 65536)
            except OSError:
                break
            if not d:
                break
            buf += d
            if not repondu and b"[1]" in re.sub(rb"\x1b\[[0-9;]*m", b"", buf):
                time.sleep(0.3)
                os.write(maitre, frappe + b"\n")
                repondu = True
    if p.poll() is None:
        p.kill()
    p.wait()
    os.close(maitre)
    return re.sub(rb"\x1b\[[0-9;]*m", b"", buf).decode("utf-8", "replace")


def main():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FauxGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_choix_version")

    print("\n── le menu doit s'afficher et désigner la plus récente")
    sortie = _lancer(base, b"", os.path.join(tmp, "defaut"))
    verifier(all(t in sortie for t in TAGS), "les trois versions sont listées", sortie[-400:])
    verifier(re.search(re.escape(TAGS[0]) + r"\s+\(la plus r", sortie) is not None,
             "la première est annoncée « la plus récente »")

    print("\n── ★ Entrée seule : la plus récente, ET AUCUN BRUIT")
    verifier("version retenue : %s" % TAGS[0] in sortie,
             "★ Entrée retient %s" % TAGS[0], sortie[-400:])
    # C'EST LE CONTRÔLE QUI ATTRAPE LE DÉFAUT : le bon choix ne suffisait pas, il était déjà bon.
    bruit = [l for l in sortie.splitlines()
             if "nombre entier attendu" in l or "integer expression expected" in l
             or re.search(r"\bligne \d+ *:", l)]
    verifier(not bruit, "★ aucune erreur bash affichée à l'utilisateur", bruit)
    verifier("@%s" % TAGS[0] in sortie, "la source est récupérée AU TAG, pas sur main")

    print("\n── un numéro explicite est respecté")
    s2 = _lancer(base, b"2", os.path.join(tmp, "deux"))
    verifier("version retenue : %s" % TAGS[1] in s2, "« 2 » retient %s" % TAGS[1], s2[-300:])

    print("\n── « d » installe la branche de développement")
    sd = _lancer(base, b"d", os.path.join(tmp, "dev"))
    verifier("version retenue : main" in sd, "« d » retient main", sd[-300:])

    print("\n── une saisie absurde retombe sur la plus récente, sans bruit")
    for frappe, nom in ((b"zz", "non numérique"), (b"99", "hors liste")):
        s = _lancer(base, frappe, os.path.join(tmp, nom.split()[0]))
        verifier("version retenue : %s" % TAGS[0] in s,
                 "« %s » (%s) retombe sur %s" % (frappe.decode(), nom, TAGS[0]), s[-300:])
        mauvais = [l for l in s.splitlines() if "nombre entier attendu" in l
                   or re.search(r"\bligne \d+ *:", l)]
        verifier(not mauvais, "   et sans erreur bash", mauvais)

    print("\n── le défaut de structure ne doit pas revenir")
    src = open(GET_SH, encoding="utf-8").read()
    verifier('case "${rep_:-1}"' not in src,
             "★ le `case` ne teste plus une valeur que son corps ne verra pas")
    verifier('rep_="${rep_:-1}"' in src, "   la saisie est normalisée UNE fois, avant le `case`")

    print()
    if ECHECS:
        print("✗ %d contrôle(s) en échec :" % len(ECHECS))
        for e in ECHECS:
            print("   · " + e)
        return 1
    print("✓ tout est vert")
    return 0


# ── Précondition : ce banc EXIGE root ───────────────────────────────────────────────
# Il lance l'installeur (`get.sh`), qui refuse de tourner sans root parce qu'il pose des
# services systemd. Sans root, chacun de ses contrôles échoue sur « à lancer en root. » : le
# banc dénonce alors la machine et non le code — c'est ce qui s'est produit en CI le
# 2026-10-25, sur un exécuteur qui tourne sous l'utilisateur `runner`.
# L'abstention est ANNONCÉE avec le marqueur NON MESURÉ, et non silencieuse : une garde qui se
# taît quand sa dépendance manque devient une garde inerte que personne ne remarque.
if os.geteuid() != 0:
    print("NON MESURÉ : ce banc exige root (get.sh pose des services systemd). "
          "Aucun contrôle exécuté.")
    sys.exit(0)

if __name__ == "__main__":
    import shutil
    try:
        c = main()
    finally:
        shutil.rmtree(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_tmp_choix_version"), ignore_errors=True)
    sys.exit(c)
