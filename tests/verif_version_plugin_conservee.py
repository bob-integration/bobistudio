#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""UN REDÉPLOIEMENT GARDE LA VERSION DU CONTENEUR — on ne migre que par un geste explicite.

★ CE QUI MANQUAIT (arbitrage de Cyril, 2026-10-31, point N7). Mesuré ce jour-là : 2 des 32
appels de `deployer_script` transmettaient une version. Pour les 30 autres — reprise après
redémarrage du nœud, restauration de projet, câblage, redémarrage d'un conteneur mort, attente
d'un pilote hôte, sonde de métriques — `version` valait None, et `render_script` prend alors la
COURANTE du manifeste. Un exploitant qui épinglait un conteneur de test sur une ancienne version
le voyait donc migrer au premier recâblage, sans que personne l'ait décidé. La règle « garder la
version épinglée » existait bel et bien… dans UN seul appel (`routes/plugin_registry.py`), d'où
le titre de cette garde : ce n'est pas la formule qui manquait, c'est le DÉFAUT.

Ce que cette garde tient :
1. aucune version nommée ⇒ celle que le conteneur porte déjà, pas la courante ;
2. une ACTIVATION de nouvelle version ne déplace aucun conteneur existant ;
3. un conteneur NEUF (ou déjà à la courante, ou d'un type qui n'est pas un plugin) prend la
   courante — l'épinglage ne doit pas se déclencher tout seul ;
4. l'estampille est lue dans les params REÇUS aussi, pas seulement en base (l'éditeur, lui, ne
   renvoie jamais `plugin_version`) ;
5. ★ une version épinglée que le registre ne sait PAS rendre ne migre pas en silence : elle
   retombe sur la courante AVEC une alerte, parce qu'un rendu muet est le défaut d'origine ;
6. ★ ET LE CHEMIN DE DÉPLOIEMENT RÉEL LE CONSOMME : le script effectivement rendu par
   `deployer_script` est celui de la version archivée. Une garde sur la seule fonction qui
   décide ne prouve pas que quiconque l'appelle (leçon du 30/10) ;
7. un geste explicite — `version=` — l'emporte toujours : c'est par là qu'on migre ;
8. la règle n'a qu'UNE définition : la copie locale de `plugin_registry` a été retirée, et deux
   copies d'une même règle finissent par diverger.

Hors ligne : base jetable, plugin de test posé dans un arbre temporaire, hôte et agent doublés.
"""
import io
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_pin_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import deploy, plugins                                               # noqa: E402

echecs = []
_n = [0]


def verifie(ok, titre, detail=""):
    _n[0] += 1
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


def section(t):
    print("\n" + t)


# ── Décor : un VRAI plugin, avec un VRAI historique git, dans un arbre temporaire ───────────
# Fabriqué, et c'est dit : aucun plugin du dépôt n'est touché, et la garde ne dépend pas de
# l'état des sous-modules (une autre session bumpe le mélangeur pendant que celle-ci tourne).
# En revanche le plugin passe le VRAI scan du registre, et son historique est un VRAI dépôt git :
# si l'un ou l'autre ne lui convient pas, la garde le DIT au lieu de sauter ses contrôles (une
# doublure ne peut pas contredire son auteur).
#
# `MARQUE` est la seule différence entre les scripts des trois versions : c'est elle qui dira
# quel FICHIER a été rendu, et d'où il venait (archive sur disque ou historique git).
TYPE = "verifpin"
V1, V2, V3 = "0.0.1", "0.0.2", "0.0.3"

_ARBRE = tempfile.mkdtemp(prefix="verif_pin_plugins_")
_PDIR = os.path.join(_ARBRE, TYPE)
os.makedirs(_PDIR)

# `{{ }}` doublés : le script passe par str.format (cf. CLAUDE.md).
_SCRIPT = ('CONFIG = {config}\nHOTE = "{hostname}"\nVERSION = "{plugin_version}"\n'
           'MARQUE = "%s"\nRIEN = {{}}\n')


def _ecrire(chemin, texte):
    with io.open(chemin, "w", encoding="utf-8") as f:
        f.write(texte)


def _poser_a_plat(version, marque):
    _ecrire(os.path.join(_PDIR, "script.py"), _SCRIPT % marque)
    _ecrire(os.path.join(_PDIR, "plugin.json"), json.dumps(
        {"type": TYPE, "label": "Garde d'épinglage", "version": version,
         "script_template": "script.py"}, ensure_ascii=False, indent=2))


_DATES = {}          # version → date de commit voulue, dans le PASSÉ


def _git(*args, quand=None):
    import subprocess
    env = dict(os.environ)
    if quand:
        # Datées dans le passé : sans ça, « date du commit » et « maintenant » se ressemblent,
        # et la section 11 ne saurait pas distinguer les deux.
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = quand
    return subprocess.run(("git", "-C", _PDIR) + args, capture_output=True, check=False,
                          env=env)


def _recharger():
    plugins.PLUGINS_DIR = _ARBRE
    plugins.reload()


# Historique : trois commits, un par version — exactement ce que fait le développement depuis
# juin (bump du manifeste, sans archiver dans versions/).
_git("init", "-q", "-b", "main")
_git("config", "user.email", "garde@example.invalid")
_git("config", "user.name", "Garde")
_GIT_OK = True
for _v, _m, _q in ((V1, "GIT-V1", "2026-06-01T09:00:00+0000"),
                   (V2, "GIT-V2", "2026-06-15T10:30:00+0000"),
                   (V3, "GIT-V3", "2026-07-02T11:45:00+0000")):
    _poser_a_plat(_v, _m)
    _git("add", "-A")
    _r = _git("commit", "-q", "-m", "version " + _v, quand=_q)
    if _r.returncode != 0:
        _GIT_OK = False
    _DATES[_v] = _q

# …et UNE archive posée à la main sur disque, pour distinguer les deux chemins : l'archive
# existante doit primer, la reconstitution ne servir qu'à ce qui manque.
os.makedirs(os.path.join(_PDIR, "versions", V1))
_ecrire(os.path.join(_PDIR, "versions", V1, "script.py"), _SCRIPT % "DISQUE-V1")

_poser_a_plat(V2, "GIT-V2")          # le plugin « installé » est en V2
_recharger()

section("0. Le décor passe le vrai scan du registre")
verifie(plugins.is_plugin(TYPE), "le plugin de test est enregistré",
        "erreurs de scan : %s" % dict(plugins.SCAN_ERRORS))
verifie((plugins.get(TYPE) or {}).get("version") == V2,
        "sa version courante est %s" % V2)
verifie(set(plugins.versions(TYPE)) == {V2, V1},
        "la courante et l'archive sur disque sont disponibles", str(plugins.versions(TYPE)))
verifie(_GIT_OK and bool(plugins._commit_for_version(_PDIR, V2)),
        "et l'historique git du plugin est lisible (sinon la section 2 ne mesurerait rien)")


def _conteneur(vmid, hostname, params=None, type_=TYPE):
    with database.get_db() as db:
        # Pas de colonne `type` : le type d'un conteneur vit dans le JSON `deploy_config`.
        db.execute("INSERT OR REPLACE INTO containers (vmid, hostname, status) "
                   "VALUES (?,?,?)", (vmid, hostname, "running"))
        db.commit()
    if params is not None:
        database.db_update_deploy_config(vmid, type_, params)


EPINGLE, NEUF, A_JOUR, INTROUVABLE, AUTRE = 9001, 9002, 9003, 9004, 9005
_conteneur(EPINGLE, "pin-epingle", {"plugin_version": V1})
_conteneur(NEUF, "pin-neuf")
_conteneur(A_JOUR, "pin-ajour", {"plugin_version": V2})
_conteneur(INTROUVABLE, "pin-introuvable", {"plugin_version": "0.0.99"})
_conteneur(AUTRE, "pin-autre", {"plugin_version": V1}, type_="2110_io")

section("1. Aucune version nommée ⇒ celle du conteneur")
verifie(deploy.pinned_version(EPINGLE, TYPE, {}) == V1,
        "un conteneur épinglé sur %s garde %s" % (V1, V1),
        str(deploy.pinned_version(EPINGLE, TYPE, {})))

section("2. Faire passer la courante ne déplace aucun conteneur existant")
# (a) L'ACTIVATION, telle que la route Réglages l'appelle. Elle archive la sortante elle-même.
plugins.activate_version(TYPE, V1)
verifie((plugins.get(TYPE) or {}).get("version") == V1,
        "activate_version(%s) a bougé la courante" % V1)
verifie(V2 in plugins.versions(TYPE),
        "et elle a archivé la version sortante %s" % V2, str(plugins.versions(TYPE)))
verifie(deploy.pinned_version(A_JOUR, TYPE, {}) == V2,
        "le conteneur qui était à jour est désormais épinglé sur %s, pas emporté" % V2,
        str(deploy.pinned_version(A_JOUR, TYPE, {})))

# (b) LE BUMP DE DÉVELOPPEMENT — le cas réel depuis juin : `plugin.json` passe à la version
# suivante sans que personne n'archive. Rien sur le disque pour la sortante : si le registre ne
# sait pas la reconstituer depuis git, tout conteneur à jour migre, et l'épinglage est un vœu.
import shutil as _sh                                                          # noqa: E402
_sh.rmtree(os.path.join(_PDIR, "versions", V2))        # on efface l'archive que (a) a posée
_poser_a_plat(V3, "GIT-V3")
_recharger()
verifie(V2 not in plugins.versions(TYPE),
        "décor : %s n'est plus archivée sur disque" % V2, str(plugins.versions(TYPE)))
verifie(deploy.pinned_version(A_JOUR, TYPE, {}) == V2,
        "★ un bump SANS archivage n'emporte pas le conteneur : %s est reconstituée depuis git" % V2,
        str(deploy.pinned_version(A_JOUR, TYPE, {})))
# ★ ET C'EST LE CODE DE CETTE VERSION-LÀ. Sans ce contrôle, une reconstitution qui prendrait le
# script de HEAD passerait : l'estampille dirait 0.0.2 et le conteneur exécuterait la courante —
# précisément la contradiction que tout ce chantier cherche à supprimer. (Mutation M12.)
_rendu_recon = plugins.render_script(TYPE, {}, "h", V2) or ""
verifie('MARQUE = "GIT-V2"' in _rendu_recon,
        "et c'est bien le script du commit de %s, pas celui de HEAD" % V2,
        [l for l in _rendu_recon.splitlines() if l.startswith("MARQUE")])
verifie('VERSION = "%s"' % V2 in _rendu_recon,
        "l'estampille injectée est celle de la version reconstituée")

_meta = plugins.read_version_meta(TYPE, V2)
verifie(bool(_meta.get("imported_at")),
        "l'archive reconstituée est datée (elle vient d'arriver dans versions/)")
_mp = os.path.join(_PDIR, "versions", V2, "meta.json")
verifie(os.path.isfile(_mp)
        and bool((json.load(io.open(_mp, encoding="utf-8")) or {}).get("rebuilt_from_git")),
        "et elle DIT qu'elle vient de git — un cache doit être reconnaissable comme jetable")

_poser_a_plat(V2, "GIT-V2")                # on revient au décor de départ
_recharger()

section("3. L'épinglage ne se déclenche pas tout seul")
verifie(deploy.pinned_version(NEUF, TYPE, {}) is None,
        "un conteneur sans estampille prend la courante (None)",
        str(deploy.pinned_version(NEUF, TYPE, {})))
verifie(deploy.pinned_version(A_JOUR, TYPE, {}) is None,
        "une estampille égale à la courante ne devient pas un pin",
        str(deploy.pinned_version(A_JOUR, TYPE, {})))
verifie(deploy.pinned_version(AUTRE, "2110_io", {}) is None,
        "un type qui n'est pas un plugin ne s'épingle pas (moteur MTL, pas de script rendu)",
        str(deploy.pinned_version(AUTRE, "2110_io", {})))

section("4. L'estampille est lue dans les params reçus aussi")
verifie(deploy.pinned_version(NEUF, TYPE, {"plugin_version": V1}) == V1,
        "params reçus portant %s ⇒ %s, sans rien en base" % (V1, V1),
        str(deploy.pinned_version(NEUF, TYPE, {"plugin_version": V1})))

section("5. Une version épinglée que le registre ne sait pas rendre ne migre pas en silence")
_av = len(database.db_get_alerts(vmid=INTROUVABLE) or [])
_r = deploy.pinned_version(INTROUVABLE, TYPE, {})
verifie(_r is None, "elle retombe sur la courante (le rendu doit rester possible)", str(_r))
# Contrôlé sur la DONNÉE, jamais sur un mot traduit : les alertes sont stockées RENDUES.
_al = [a for a in (database.db_get_alerts(vmid=INTROUVABLE) or [])
       if (a.get("kind") == "deploy") and ("0.0.99" in (a.get("message") or ""))]
verifie(len(_al) >= 1 and len(database.db_get_alerts(vmid=INTROUVABLE) or []) > _av,
        "et une alerte NOMME la version introuvable", "%d alerte(s)" % len(_al))
verifie(any(V2 in (a.get("message") or "") for a in _al),
        "l'alerte dit aussi sur quelle version le rendu repart")

section("6. Le chemin de déploiement RÉEL consomme ce défaut")
# Doublures minimales : tout ce qui touche l'hôte, l'agent-nœud et le réseau. Le rendu, lui,
# est le VRAI (`generer_script` n'est pas remplacé mais ESPIONNÉ) — c'est le seul moyen de
# prouver que le fichier servi est celui de l'archive, et pas seulement que l'argument était bon.
from app import docker_compute, host_driver_gate                              # noqa: E402

_vu = {}
_vrai_generer = deploy.generer_script


def _generer_espion(type_script, params, version=None):
    rendu = _vrai_generer(type_script, params, version)
    _vu["version"] = version
    _m = [x for x in ("DISQUE-V1", "GIT-V1", "GIT-V2", "GIT-V3")
          if ('MARQUE = "%s"' % x) in (rendu or "")]
    _vu["marque"] = _m[0] if _m else "?"
    _vu["estampille"] = [l for l in (rendu or "").splitlines() if l.startswith("VERSION")]
    return ""          # rendu vide ⇒ `deployer_script` s'arrête juste après, sans toucher l'agent


deploy.generer_script = _generer_espion
host_driver_gate.hold_if_missing = lambda *a, **k: False
docker_compute.is_mtl_type = lambda t: False
docker_compute.deploy_compute = lambda *a, **k: True
deploy.get_container_ip = lambda vmid: "192.0.2.42"      # RFC 5737, jamais une adresse de site

_vu.clear()
deploy.deployer_script(EPINGLE, TYPE, {})
verifie(_vu.get("version") == V1,
        "un redéploiement SANS version rend la version du conteneur", str(_vu.get("version")))
verifie(_vu.get("marque") == "DISQUE-V1",
        "et le script rendu vient bien de versions/%s/script.py, pas du dossier plat" % V1,
        "marque=%s" % _vu.get("marque"))
verifie(_vu.get("estampille") == ['VERSION = "%s"' % V1],
        "le script porte l'estampille de l'archive, pas celle de la courante",
        str(_vu.get("estampille")))

_vu.clear()
deploy.deployer_script(NEUF, TYPE, {})
verifie(_vu.get("marque") == "GIT-V2",
        "un conteneur neuf reçoit toujours la courante", "marque=%s" % _vu.get("marque"))

section("7. Un geste explicite migre")
_vu.clear()
deploy.deployer_script(EPINGLE, TYPE, {}, version=V2)
verifie(_vu.get("version") == V2 and _vu.get("marque") == "GIT-V2",
        "version= nommée l'emporte sur l'épinglage (c'est par là qu'on migre)",
        "%s / %s" % (_vu.get("version"), _vu.get("marque")))

deploy.generer_script = _vrai_generer

section("8. La règle n'a qu'une définition")
# Contrôlé par l'ARBRE, pas par une chaîne de caractères : mon premier contrôle cherchait le
# texte exact de l'ancienne formule, et une simple parenthèse en plus lui échappait (mutation M8
# inerte). Ce qui compte n'est pas comment la formule est écrite, c'est qu'aucun appel de
# `_plugin_config_apply` ne nomme `version=` — sinon il décide à la place du défaut central.
import ast as _ast                                                           # noqa: E402
_src = io.open(os.path.join(RACINE, "app", "routes", "plugin_registry.py"),
               encoding="utf-8").read()
_kw = [sorted(k.arg or "**" for k in _c.keywords)
       for _f in _ast.walk(_ast.parse(_src))
       if isinstance(_f, _ast.FunctionDef) and _f.name == "_plugin_config_apply"
       for _c in _ast.walk(_f)
       if isinstance(_c, _ast.Call) and getattr(_c.func, "id", "") == "deployer_script"]
verifie(bool(_kw) and all("version" not in k for k in _kw),
        "aucun appel de _plugin_config_apply ne décide de la version à la place du défaut",
        "%d appel(s) : %s" % (len(_kw), _kw))
_sd = io.open(os.path.join(RACINE, "app", "deploy.py"), encoding="utf-8").read()
verifie(_sd.count("version = pinned_version(") == 1,
        "et le défaut est posé en UN seul endroit du chemin de déploiement",
        "%d occurrence(s)" % _sd.count("version = pinned_version("))

section("9. Activer une version ne détruit pas le dépôt du plugin")
# ★ Trouvé en écrivant cette garde (31/10) : `_flat_names` rendait TOUT sauf `versions/`, donc
# `.git` avec. Une activation recopiait l'historique entier dans l'archive, puis l'effaçait du
# dossier du plugin. Les plugins étant des sous-modules, la page Plugins cassait le dépôt — et
# emportait l'historique dont la reconstitution ci-dessus a besoin.
verifie(os.path.isdir(os.path.join(_PDIR, ".git")), "décor : le plugin est bien un dépôt git")
plugins.activate_version(TYPE, V1)
verifie(os.path.isdir(os.path.join(_PDIR, ".git")),
        "★ le dépôt du plugin a survécu à l'activation")
verifie(bool(plugins._commit_for_version(_PDIR, V2)),
        "et son historique est toujours interrogeable (sans quoi plus rien n'est reconstituable)")
verifie(not os.path.exists(os.path.join(_PDIR, "versions", V2, ".git")),
        "l'archive ne contient pas une copie du dépôt",
        "l'historique entier était recopié dans chaque versions/<v>/")

section("10. La compatibilité avec ce cœur a TROIS états, pas deux")
# `version.au_moins` rend True quand la comparaison est impossible — volontaire : ne pas savoir
# comparer n'autorise pas à bloquer un déploiement. Mais AFFICHER « compatible » dans ce cas
# ferait passer l'absence de contrôle pour un contrôle réussi.
from app import version as _ver                                              # noqa: E402

_cm, _ok = plugins.version_compatible(TYPE, V2)
verifie((_cm, _ok) == ("", True), "sans exigence : rien d'exigé, et ça tourne ici",
        "%r / %r" % (_cm, _ok))

_exig = {"type": TYPE, "label": "x", "version": V2, "script_template": "script.py",
         "requires": {"core_min": "0.0.1"}}
_ecrire(os.path.join(_PDIR, "plugin.json"), json.dumps(_exig, ensure_ascii=False))
_recharger()
_cm, _ok = plugins.version_compatible(TYPE, V2)
verifie((_cm, _ok) == ("0.0.1", True), "exigence satisfaite : compatible", "%r / %r" % (_cm, _ok))

_exig["requires"]["core_min"] = "99.0.0"
_ecrire(os.path.join(_PDIR, "plugin.json"), json.dumps(_exig, ensure_ascii=False))
_recharger()
_cm, _ok = plugins.version_compatible(TYPE, V2)
verifie((_cm, _ok) == ("99.0.0", False),
        "exigence non satisfaite : refusée, et on dit laquelle", "%r / %r" % (_cm, _ok))

_exig["requires"]["core_min"] = "main"
_ecrire(os.path.join(_PDIR, "plugin.json"), json.dumps(_exig, ensure_ascii=False))
_recharger()
_cm, _ok = plugins.version_compatible(TYPE, V2)
verifie(_ok is None, "exigence non comparable : INCONNU, pas « compatible »",
        "%r / %r" % (_cm, _ok))
verifie(_ver.au_moins(_ver.VERSION, "main") is True,
        "décor : au_moins, lui, laisse passer — c'est bien l'affichage qui doit se taire")

_poser_a_plat(V2, "GIT-V2")
_recharger()
_cm, _ok = plugins.version_compatible(TYPE, V1)
verifie((_cm, _ok) == (None, None),
        "une archive sans manifeste à côté de son script : INCONNU", "%r / %r" % (_cm, _ok))
verifie(all(("core_min" in d and "core_ok" in d) for d in plugins.versions_meta(TYPE)),
        "et la liste des versions porte les deux champs (sinon l'écran n'a rien à rendre)")
# …ET L'ÉCRAN LES REND. Produire le verdict ne prouve pas qu'un lecteur l'affiche : la liste
# des plugins ET celle des services doivent appeler le même rendu (mutation M19).
_tpl0 = io.open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
verifie("function _coreBadge(vm)" in _tpl0 and _tpl0.count("${_coreBadge(vm)}") == 2,
        "les deux listes de versions — plugins et services — rendent le verdict",
        "%d appel(s) de _coreBadge" % _tpl0.count("${_coreBadge(vm)}"))

section("11. La date d'arrivée est rangée côté orchestrateur, pas dans le fichier versionné")
_meta_plat = os.path.join(_PDIR, "meta.json")
_d1 = plugins.imported_at(TYPE, V2)
verifie(bool(_d1), "une version sans date en reçoit une", repr(_d1))
# Contrôle ABSOLU, pas un avant/après : mon premier jet comparait l'état du fichier autour de
# l'appel, or la section 10 avait déjà demandé la liste des versions — donc déjà daté. La
# mutation qui écrit dans le meta.json passait inaperçue (M17 inerte). Ce qu'on veut dire est
# plus simple : ce fichier ne doit JAMAIS apparaître.
verifie(not os.path.isfile(_meta_plat),
        "★ le meta.json du plugin n'est jamais écrit (il est suivi par git dans les sous-modules)",
        "le dater laisserait les 25 plugins « modifiés » et pousserait la date d'une machine")
verifie((database.db_plugin_store_get(TYPE, V2, scope=plugins._IMPORTED_SCOPE) or {})
        .get("imported_at") == _d1,
        "elle est rangée dans la base de l'orchestrateur")
# Elle vient du COMMIT qui a posé cette version, pas de « maintenant » — comparé à ce que git
# dit lui-même, pour ne pas dépendre du fuseau de la machine qui lance la garde.
_sha = plugins._commit_for_version(_PDIR, V2)
_attendue = (plugins._git(_PDIR, "show", "-s", "--format=%cI", _sha) or "").strip()[:19]
verifie(_d1 == _attendue, "et c'est la date du commit de cette version", "%r ≠ %r" % (_d1, _attendue))
verifie(_d1.startswith("2026-06-15"),
        "donc une date du passé, pas l'heure du premier regard", repr(_d1))
verifie(plugins.imported_at(TYPE, V2) == _d1, "relue, elle ne bouge plus")
verifie(any(d["version"] == V2 and d["imported_at"] == _d1
            for d in plugins.versions_meta(TYPE)),
        "la liste des versions la rend")

section("12. Le sort d'un paquet du Catalogue se lit avant le clic")
_tpl = io.open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
verifie("async function catInstaller(btn, depot, activer)" in _tpl,
        "l'installation reçoit le choix en argument")
verifie(_tpl.count("catInstaller(this, ' + depot + ', true)") == 1
        and _tpl.count("catInstaller(this, ' + depot + ', false)") == 1,
        "deux boutons, un par issue — plus un seul geste dont l'effet dépend d'une case",
        "vrai=%d faux=%d" % (_tpl.count("catInstaller(this, ' + depot + ', true)"),
                             _tpl.count("catInstaller(this, ' + depot + ', false)")))
verifie("download_only" in _tpl, "« télécharger seulement » est offert au moment du geste")

print("\n%d contrôles, %d échec(s)" % (_n[0], len(echecs)))
for e in echecs:
    print("  ✗ " + e)
sys.exit(1 if echecs else 0)
