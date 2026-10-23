#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""`get.sh` doit SURVIVRE à une API GitHub injoignable — et le DIRE.

★ LA PANNE, SIGNALÉE PAR UN INSTALLATEUR LE 2026-10-23. « L'installation depuis internet ne
marche pas » : l'écran affichait « aucune version étiquetée trouvée », puis « dépôt principal ✓ »,
puis **rien** — retour au prompt, sans un mot, sans code d'erreur lisible par un humain.

Deux défauts, une seule cause — l'API GitHub muette (quota anonyme de 60 requêtes/heure et par
IP, proxy, DNS) :

  1. `_sha_sous_module` est un pipeline `curl | python3` SANS `|| true`. L'appelant écrit
     `ref_sm="$(_sha_sous_module "$chemin")"` : sous `set -euo pipefail`, une affectation depuis
     une substitution de commande PREND le code de retour, donc le script MEURT, muet. Le repli
     sur « main » écrit à la ligne suivante n'était jamais atteint — le commentaire décrivait une
     intention que le code n'a jamais réalisée. `_etiquettes`, elle, portait ce `|| true` :
     c'était toute la différence entre les deux fonctions.
  2. Le message « aucune version étiquetée trouvée » AFFIRMAIT un fait sur le dépôt alors qu'on
     n'avait pas pu poser la question. Le dépôt a bel et bien des étiquettes (v0.9.8).

⚠ AUCUN ACCÈS RÉSEAU. Un faux GitHub tourne sur la boucle locale : l'API répond 403 (le quota
épuisé, tel que GitHub le rend), codeload sert une archive minimale. Un banc qui dépendrait du
vrai GitHub échouerait en CI et, pire, consommerait le quota qu'il est censé protéger.
"""
import http.server
import io
import os
import pty
import select
import subprocess
import sys
import tarfile
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET = os.path.join(RACINE, "get.sh")

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def _archive(depot):
    """tar.gz minimal, avec le dossier racine que GitHub ajoute et que `--strip-components=1`
    retire. Contient ce que `get.sh` VÉRIFIE après déballage — sinon il refuse la source, et on
    testerait son refus au lieu de sa survie."""
    noms = ["main.py", "app/__init__.py", "install/install.py",
            "node_agent/install-node.sh", "plugins/_compute_runtime/meta.json"]
    if "service-nmos" in depot:
        noms = ["__init__.py"]
    brut = io.BytesIO()
    with tarfile.open(fileobj=brut, mode="w:gz") as t:
        for n in noms:
            d = b"x"
            info = tarfile.TarInfo("racine/" + n)
            info.size = len(d)
            t.addfile(info, io.BytesIO(d))
    return brut.getvalue()


class FauxGitHub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # L'API : 403, exactement ce que GitHub rend quand le quota anonyme est épuisé.
        if self.path.startswith("/repos/"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"message":"API rate limit exceeded"}')
            return
        if "/tar.gz/" in self.path:
            corps = _archive(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FauxGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    env = dict(os.environ, BOBI_API=base, BOBI_CODELOAD=base, BOBI_LANG="fr")
    env.pop("GITHUB_TOKEN", None)

    # ⚠ PSEUDO-TERMINAL OBLIGATOIRE. Sans TTY, `get.sh` part en « non interactif » et saute
    # l'étape du choix de version — donc exactement le chemin qui a échoué chez l'installateur.
    # Un banc lancé avec un simple `subprocess` passait au vert sans jamais exercer le défaut.
    maitre, esclave = pty.openpty()
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.Popen(["bash", GET, "--dry-run"], stdin=esclave, stdout=esclave,
                                stderr=esclave, cwd=tmp, env=env, close_fds=True)
        os.close(esclave)
        os.write(maitre, b"\n")          # on prend le défaut proposé
        morceaux = []
        while True:
            if not select.select([maitre], [], [], 180)[0]:
                break
            try:
                bloc = os.read(maitre, 65536)
            except OSError:
                break
            if not bloc:
                break
            morceaux.append(bloc)
        os.close(maitre)
        code = proc.wait(timeout=60)
    sortie = b"".join(morceaux).decode("utf-8", "replace")

    class _R:
        returncode = code
    r = _R()

    print("\n── l'API est muette (403) : le script doit ABOUTIR quand même")
    verifier(r.returncode == 0,
             "★ code de sortie 0 (avant : mort muette, code 7 ou 22) — obtenu %d" % r.returncode)
    verifier("dépôt principal" in sortie, "le dépôt principal est récupéré")
    verifier("services/nmos" in sortie,
             "★ le sous-module AUSSI : le repli sur « main » est bien atteint")
    verifier("Simulation" in sortie, "la simulation va jusqu'au bout")

    print("\n── et il doit DIRE pourquoi, sans affirmer ce qu'il ignore")
    verifier("INDISPONIBLE" in sortie or "UNAVAILABLE" in sortie,
             "★ il annonce une liste INDISPONIBLE, pas « aucune version trouvée »")
    verifier("quota" in sortie.lower(), "il nomme la cause la plus fréquente (le quota)")
    verifier("GITHUB_TOKEN" in sortie, "il dit quoi faire (exporter un jeton)")

    print("\n── le défaut de structure qui l'a causé ne doit pas revenir")
    src = open(GET, encoding="utf-8").read()
    corps = src[src.index("_sha_sous_module() {"):]
    corps = corps[:corps.index("\n}\n")]
    verifier("|| true" in corps,
             "★ `_sha_sous_module` tolère l'échec (sinon `set -e` tue le script sur l'affectation)")

    print()
    if ECHECS:
        print("✗ %d échec(s)" % len(ECHECS))
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
