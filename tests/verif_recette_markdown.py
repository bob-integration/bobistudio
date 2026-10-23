#!/usr/bin/env python3
"""Garde : les messages de la page Recette sont rendus en Markdown, et SANS injection.

DÉFAUT D'ORIGINE (relevé le 2026-10-20 par un testeur, photo à l'appui) : les réponses DEV sont
écrites en Markdown (`**gras**`, blocs ```, listes) mais la page les posait en `textContent` —
astérisques et clôtures de code affichés en clair, lecture pénible.

On exécute sous node le VRAI `mdRender` / `mdPlain` extraits de `templates/tests.html`.
Aucun réseau, aucune base.
"""
import json
import os
import re
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    src = open(os.path.join(RACINE, "templates/tests.html"), encoding="utf-8").read()
    esc = re.search(r"  function escapeHtml\(s\) \{.*?\n  \}\n", src, re.S).group(0)
    md = re.search(r"// ── Rendu Markdown des messages \(début\).*?// ── Rendu Markdown des messages \(fin\)",
                   src, re.S).group(0)
    cas = {
        "gras": "**2. Ce qui manque.** La sortie",
        "italique": "un *mot* ici, mais 2*3*4 non",
        "code": "le flux `stills_key` absent",
        "bloc": "avant\n```\nkey_flow = (vide)   <- rien\n```\naprès",
        "liste": "- un\n- deux\n\n3. trois\n4. quatre",
        "titre": "## Titre",
        "table": "| a | b |\n|---|---|\n| 1 | **2** |",
        "citation": "> cité\n> suite",
        "lien": "voir https://x.org/a?b=1&c=2.",
        "xss": '<img src=x onerror=alert(1)> **<script>x</script>** `<b>` [l](javascript:alert(1))',
        "xss_lien": 'https://x.org/"onmouseover="alert(1)',
        "deux_lignes": "ligne 1\nligne 2",
    }
    js = esc + md + "\nconst C=" + json.dumps(cas) + ";\nconst R={};for(const k in C)R[k]=mdRender(C[k]);" \
         "R.plain=mdPlain('**Gras** et `code`\\n```\\nx\\n```');console.log(JSON.stringify(R));"
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    verifier(p.returncode == 0, "le code s'exécute sous node (%s)" % p.stderr.strip()[:200])
    if p.returncode:
        return fin()
    R = json.loads(p.stdout)

    print("\n1. Le Markdown est rendu")
    verifier("<b>2. Ce qui manque.</b>" in R["gras"] and "**" not in R["gras"], "★ **gras**")
    verifier("<i>mot</i>" in R["italique"] and "2*3*4" in R["italique"], "*italique*, sans toucher 2*3*4")
    verifier("<code>stills_key</code>" in R["code"], "`code` en ligne")
    verifier('<pre class="tp-code">key_flow = (vide)   &lt;- rien</pre>' in R["bloc"] and "```" not in R["bloc"],
             "★ bloc ``` en <pre>, contenu échappé, espaces gardés")
    verifier("<ul><li>un</li><li>deux</li></ul>" in R["liste"] and '<ol start="3">' in R["liste"],
             "listes à puces et numérotées (numéro de départ gardé)")
    verifier('<div class="tp-h">Titre</div>' == R["titre"], "titre")
    verifier("<th>a</th>" in R["table"] and "<td><b>2</b></td>" in R["table"], "tableau")
    verifier(R["citation"] == "<blockquote>cité<br>suite</blockquote>", "citation")
    verifier('href="https://x.org/a?b=1&amp;c=2"' in R["lien"] and R["lien"].endswith("</a>.</p>"),
             "lien http(s), ponctuation finale hors du lien")
    verifier(R["deux_lignes"] == "<p>ligne 1<br>ligne 2</p>", "retour à la ligne simple gardé")

    print("\n2. Aucune balise venue d'un message n'atteint la page")
    x = R["xss"]
    verifier("<img" not in x and "<script" not in x and "&lt;img" in x, "★ <img>/<script> échappés")
    verifier("<code>&lt;b&gt;</code>" in x, "balise dans `code` échappée")
    verifier("javascript:" not in re.sub(r"\[l\]\(javascript:alert\(1\)\)", "", x) and "href=\"javascript" not in x,
             "★ aucun lien javascript:")
    verifier('href="https://x.org/&quot;onmouseover' in R["xss_lien"] and ' onmouseover=' not in R["xss_lien"],
             "★ un guillemet dans une URL ne sort pas de l'attribut")

    print("\n3. L'aperçu d'une ligne n'affiche pas les marques")
    verifier("**" not in R["plain"] and "`" not in R["plain"] and "Gras et code" in R["plain"], "aperçu propre")

    print("\n4. Branchement")
    verifier('txt.innerHTML = mdRender(m.text)' in src, "★ les messages passent par mdRender")
    verifier("textContent = m.text" not in src, "plus de textContent brut")
    verifier("mdPlain(last.text)" in src, "l'aperçu passe par mdPlain")

    print("\n5. Entrée envoie le message (demandé le 2026-10-20 : « ça perturbe Paul »)")
    h = re.search(r'ta\.addEventListener\("keydown", e => \{(.*?)\n    \}\);', src, re.S)
    verifier(h is not None, "★ la zone de réponse écoute le clavier")
    corps = h.group(1) if h else ""
    verifier('e.key !== "Enter"' in corps and "e.shiftKey" in corps, "★ Entrée envoie, Maj+Entrée va à la ligne")
    verifier("e.isComposing" in corps, "pas pendant une composition IME")
    verifier("!btn.disabled" in corps and "postReply(it, ta)" in corps, "★ pas de double envoi pendant un envoi")
    return fin()


def fin():
    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
