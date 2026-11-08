#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The commit a submodule gitlink points at must exist on that submodule's remote.

★ WHY. A session commits `plugins/<x>` in the parent and forgets to push the submodule. On
this machine everything keeps working — the object is in the local clone — so nothing shows.
The symptom only appears somewhere else: `git submodule update` cannot fetch that commit, and
the checkout stops. For the two PUBLISHED submodules that means a public repository nobody can
clone; for the others, a private repository no colleague and no fresh machine can restore.

It happened THREE TIMES on 2026-11-08 alone — `decklink_io`, then `hello_world` and `nmos`,
the two public ones — and nothing caught any of them. `tests/submodules-doctor.sh` checks the
branch and the working tree, never reachability, and it runs advisory in CI. It is also a hard
precondition of `tools/publier.sh`, which refuses an unreachable gitlink.

★ WHAT THIS GUARD DOES *NOT* FAIL ON, and why. Measured on 2026-11-08, after fetching: 21 of
the 23 submodules are in this state — private plugin and service repositories whose work is
committed locally and pushed only when it goes out. That is the house working mode, not a
regression, and failing the whole suite on it would get this file disabled within a day. So the
guard FAILS only for the PUBLISHED submodules, which is exactly what `publier.sh` requires and
what breaks strangers, and it COUNTS the others as information.

⚠ The others are not harmless either. A fresh clone of the work repository cannot check them
out. And — measured 2026-11-08 — three of them have a PUBLIC repository that is therefore
BEHIND our work: `plugins/multiview`, `services/emberplus`, `services/tsl`. Someone installing
those from the Catalogue gets an older version than ours. This guard does not know a
repository's visibility (no network in the offline suite), so it only lists them: the judgement
belongs to whoever publishes.

★ WHY THIS CANNOT GIVE A FALSE GREEN. It compares against the local remote-tracking ref, which
may be behind the real remote — no network here, the offline suite forbids it. Being behind can
only make a pushed commit look UNREACHABLE, never the reverse: a commit reachable from an older
`origin/main` is reachable from any newer one. So the only error this guard can make is a false
alarm, which a `git fetch` settles.

★ LOCAL AUTHORITY. In CI, `actions/checkout` leaves submodules without remote-tracking refs; a
submodule with none is SKIPPED, with its reason printed. Same stance as submodules-doctor:
this check is authoritative locally (see CONTRIBUTING.md).

Non-zero exit = at least one gitlink is not on its remote. The fix is printed per submodule.
"""
import pathlib
import subprocess
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
# Les sous-modules PUBLIÉS : un gitlink injoignable y est visible du monde entier, pas
# seulement de nous. `tools/publier.sh` ne publie que ceux-là (GARDES_SOUSMODULES).
PUBLIES = ("plugins/hello_world", "services/nmos")


def _git(args, cwd=RACINE):
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return (r.stdout.strip(), None) if r.returncode == 0 else (None, r.stderr.strip())


def _chemins():
    """Les sous-modules tels que l'INDEX les voit, pas `.gitmodules`.

    ⚠ Un sous-module peut être enregistré dans l'arbre sans figurer dans `.gitmodules` (c'est
    même ce que produit la dérivation publique, qui n'en garde que deux) : c'est le gitlink qui
    fait foi, et c'est lui qui casse un clone.
    """
    out, err = _git(["ls-tree", "-r", "-z", "HEAD"])
    if out is None:
        print("  ✗ impossible de lire l'arbre : %s" % err)
        return None
    for ligne in out.split("\0"):
        if not ligne:
            continue
        meta, _, chemin = ligne.partition("\t")
        champs = meta.split()
        if len(champs) >= 3 and champs[0] == "160000":
            yield chemin, champs[2]


def _ref_distante(sm):
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main",
                "refs/remotes/origin/master"):
        out, _ = _git(["rev-parse", "--verify", "-q", ref], cwd=sm)
        if out:
            return ref, out
    return None, None


def main():
    chemins = _chemins()
    if chemins is None:
        return 1
    echecs, sautes, prives, ok = [], [], [], 0
    for chemin, gitlink in sorted(chemins):
        sm = RACINE / chemin
        marque = " (PUBLIÉ)" if chemin in PUBLIES else ""
        if not (sm / ".git").exists():
            sautes.append((chemin, "pas extrait sur cette machine"))
            continue
        ref, tete = _ref_distante(sm)
        if not ref:
            sautes.append((chemin, "aucune référence de suivi origin/* (clone de CI ?)"))
            continue
        # Le commit doit d'abord EXISTER ici : l'objet d'une autre session peut n'avoir jamais
        # été rapatrié. Sans ce contrôle, `merge-base` échouerait et on lirait « injoignable »
        # pour une raison différente de celle qu'on annonce.
        _, err = _git(["cat-file", "-e", gitlink + "^{commit}"], cwd=sm)
        if err is not None:
            (echecs if chemin in PUBLIES else prives).append(
                (chemin + marque, gitlink, "le commit n'existe même pas localement")
                if chemin in PUBLIES else chemin)
            continue
        r = subprocess.run(["git", "merge-base", "--is-ancestor", gitlink, tete],
                           cwd=str(sm), capture_output=True, text=True)
        if r.returncode == 0:
            ok += 1
        elif chemin in PUBLIES:
            echecs.append((chemin + marque, gitlink, "absent de " + ref.split("/", 2)[2]))
        else:
            prives.append(chemin)

    for chemin, raison in sautes:
        print("  — %s : sauté (%s)" % (chemin, raison))
    if prives:
        print("  ℹ %d autre(s) sous-module(s) non poussé(s) : %s"
              % (len(prives), ", ".join(sorted(prives))))
        print("    (pas un échec — mode de travail ; mais un clone neuf ne les sort pas,")
        print("     et CERTAINS ont un dépôt PUBLIC donc en retard : à regarder en publiant)")
    if not echecs:
        print("  ✓ les %d sous-module(s) PUBLIÉ(S) sont joignables depuis leur distant"
              % len([c for c in PUBLIES]))
        return 0
    print("\n  ✗ %d sous-module(s) PUBLIÉ(S) que personne au monde ne peut récupérer :\n"
          % len(echecs))
    for chemin, gitlink, raison in echecs:
        print("    %s → %s  (%s)" % (chemin, gitlink[:8], raison))
        print("      git -C %s push origin main" % chemin.split(" (")[0])
    print("\n  Le dépôt public livrerait un sous-module inclonable, et `publier.sh` refuse.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
