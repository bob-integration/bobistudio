"""Audit « déclaré et absent » — confronte le manifeste d'un plugin à ce que son script sert.

POURQUOI CE MODULE. En une seule journée, sept capacités déclarées au manifeste se sont révélées
absentes du script : `/input` alors que le plugin s'annonçait `hot-wire` (poser un câble rendait
502), huit réglages du waveform, `/reset_peak` et `/snapshot` proposés à l'éditeur de macros et
rendant 404, `phase_us` et `ecart_flux_us` publiés et toujours nuls, et un interrupteur d'alarmes
qui accusait réception sans rien basculer. Les sept ont été trouvées PAR ACCIDENT, en tombant
dessus. Rien, dans le projet, ne confrontait une promesse à son exécution.

Un manifeste n'est pas de la documentation : `plugin_proxy` valide contre `control.endpoints`,
l'éditeur de macros propose `actions[]`, la page Câbles lit `wiring`. Ce qui y est écrit devient
une capacité offerte à l'exploitant. L'écrire sans la servir est un échec SILENCIEUX — la panne
n'arrive que le jour où quelqu'un s'en sert, et elle ne dit pas d'où elle vient.

COMMENT. Les 20 plugins n'aiguillent pas de la même façon : `self.path.startswith("/x")` pour
trois d'entre eux, `path == "/x"` après normalisation pour la plupart, `urlparse(...).path` pour
les plus gros. Un vérificateur bâti sur un idiome donnerait des faux positifs en masse (mesuré :
`player` déclare 29 points d'entrée et n'expose littéralement aucun des deux idiomes les plus
courants). Le seul signal robuste est le LITTÉRAL : un chemin réellement servi a forcément sa
chaîne quelque part dans le code. On passe par l'AST plutôt que par `grep` pour ne pas compter
une mention en commentaire comme une implémentation.

CE QUE ÇA NE PROUVE PAS. La présence du littéral ne garantit pas que le point d'entrée fonctionne
— seulement qu'il n'a pas été oublié. C'est un détecteur d'OUBLI, pas un test. Il attrape la faute
qui s'est produite sept fois ; il ne remplace pas les auto-contrôles.
"""

import ast
import logging
import os

log = logging.getLogger(__name__)

_CORPUS = None      # sources de l'orchestrateur, lues une fois par processus

# Placeholders neutres pour rendre le gabarit : on ne veut que la forme du code, pas un script
# déployable. Même approche que le dry-run de `plugins._scan`.
_CONFIG_MUET = {"log_level": "info"}


def _litteraux(source):
    """Toutes les chaînes du script, VIA L'AST. Un commentaire n'en est pas une — c'est la
    différence avec un `grep`, et elle compte : plusieurs manifestes citent leurs points d'entrée
    dans un commentaire d'en-tête, ce qui suffirait à tromper une recherche textuelle."""
    out = set()
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def _identifiants(source):
    """Noms lus ou écrits dans le script (variables, attributs, clés de dict littérales).
    Sert à repérer un réglage déclaré au `config_schema` que le script ne consulte jamais."""
    noms = set()
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Name):
            noms.add(n.id)
        elif isinstance(n, ast.Attribute):
            noms.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            noms.add(n.value)
    return noms


def _chemins(endpoints):
    """(nom affiché, chemin) pour chaque point d'entrée déclaré. DEUX FORMES coexistent dans le
    parc : une simple liste de chemins, et un dictionnaire nom → {method, path, desc} (utilisé
    par `2110_io`). Ne traiter que la première faisait remonter les CLÉS du dictionnaire comme
    des chemins absents — cinq faux positifs d'un coup, sur le plugin le plus critique."""
    if isinstance(endpoints, dict):
        return [(n, (v or {}).get("path") or n, (v or {}).get("port")) for n, v in endpoints.items()]
    return [(e, e, None) for e in (endpoints or [])]


def _corpus_orchestrateur():
    """Sources de l'orchestrateur, concaténées une fois par audit.

    UN RÉGLAGE DU MANIFESTE N'EST PAS FORCÉMENT LU PAR LE SCRIPT. Vérifié : `max_inputs` est
    consommé par `compositor_fabric`, `cores_per_1080p_input` par `docker_compute`,
    `smpte_2022_7` par la vue NMOS, `light_angle` par le JS du plugin. Ne chercher que dans le
    script déclarait morts cinq réglages parfaitement vivants. Ce qu'on cherche vraiment, c'est
    un réglage que PERSONNE ne consulte."""
    global _CORPUS
    if _CORPUS is not None:
        return _CORPUS
    morceaux = []
    for racine, _dirs, fichiers in os.walk(os.path.join(os.path.dirname(__file__))):
        if "/__pycache__" in racine:
            continue
        for f in fichiers:
            # ⚠ S'EXCLURE SOI-MÊME. Ce fichier cite des clés en exemple dans ses commentaires
            # (`tally_emit`, `max_inputs`…) : sans cette ligne, l'audit se compte comme
            # consommateur et perd la trouvaille qu'il vient d'expliquer. Constaté en direct.
            if f == os.path.basename(__file__):
                continue
            if f.endswith((".py", ".html", ".js")):
                try:
                    with open(os.path.join(racine, f), encoding="utf-8", errors="ignore") as fh:
                        morceaux.append(fh.read())
                except OSError:
                    pass
    _CORPUS = "\n".join(morceaux)
    return _CORPUS


def _corpus_plugin(type_):
    """UI et fichiers annexes du plugin (control.js, gabarits) : un réglage peut n'exister que
    pour son interface, et c'est légitime."""
    from . import plugins as _pl
    d = None
    for m in _pl.all():
        if m.get("type") == type_:
            d = m.get("dir") or m.get("_dir")
    d = d or os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins", type_)
    # RÉCURSIF, et pas seulement `script.py`. Plusieurs plugins ne servent PAS leurs points
    # d'entrée depuis le gabarit poussé : `2110_io` a son contrôleur baké dans l'image
    # (`docker/controller.py`) et ses hooks côté orchestrateur (`hooks.py`), `probe_2110` de
    # même. Ne lire que `script.py` déclarait absents des chemins parfaitement servis.
    out = []
    for racine, dirs, fichiers in os.walk(d):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "versions", ".git")]
        for f in fichiers:
            # PAS les `.md` : un `help.md` qui décrit un réglage le PROMET à l'exploitant, il
            # ne l'implémente pas. Les compter comme consommateurs a masqué `tally_emit` du
            # mixer, documenté « panneau ⚙ du conteneur » et introuvable dans le code.
            if f.endswith((".py", ".js", ".html")) and f != "plugin.json":
                try:
                    with open(os.path.join(racine, f), encoding="utf-8", errors="ignore") as fh:
                        out.append(fh.read())
                except OSError:
                    pass
    return "\n".join(out)


def _cle_lue(k, ident, ailleurs=""):
    """Le script consulte-t-il ce réglage ? Trois formes légitimes, apprises en confrontant le
    verdict au code réel :

    · directe — `CONFIG.get("gain")` ;
    · POINTÉE — le manifeste déclare `video.encoder`, le script lit `VIDEO_CFG.get("encoder")` :
      c'est le dernier segment qui apparaît ;
    · CONSTRUITE — `CONFIG.get("region_%d" % i)` : aucune des quatre clés `region_0..3` n'existe
      littéralement, seul le préfixe est là.

    Les ignorer donnait onze faux positifs sur neuf plugins. Un garde-fou qui crie à tort est un
    garde-fou qu'on apprend à ignorer — c'est la règle des alarmes, et elle vaut ici aussi."""
    if k in ident or k.upper() in ident:
        return True
    if "." in k:
        dernier = k.rsplit(".", 1)[-1]
        if dernier in ident or dernier.upper() in ident:
            return True
    # Clé numérotée : on cherche le préfixe, qui est ce que porte le format.
    base = k.rstrip("0123456789")
    if base != k and base and any(s.startswith(base) for s in ident if isinstance(s, str)):
        return True
    # Consommé AILLEURS : orchestrateur (hook de déploiement, allocation de cœurs, vue NMOS…)
    # ou interface du plugin. Recherche textuelle, suffisante pour des identifiants distinctifs.
    return bool(ailleurs and k in ailleurs)


# ── Ce qu'on a ESSAYÉ ET RETIRÉ : le contrôle des vocabulaires ────────────────────────────
#
# Le 2026-08-26, l'instrument « phase » du scope était déclaré dans la liste des emplacements de
# sortie — donc proposé par l'éditeur, donc accepté par l'endpoint — et ne dessinait RIEN. C'est
# la famille « déclaré et absent » que ce fichier traque, et il ne la voyait pas : il contrôle
# les endpoints, les actions et les réglages, jamais les VOCABULAIRES publiés dans `/state.caps`.
#
# J'ai écrit ce contrôle, puis je l'ai confronté au défaut qu'il prétendait attraper. Il a
# échoué DEUX FOIS, et la seconde est concluante :
#
#   1. Compter les occurrences ne marche pas : « phase » apparaissait deux fois dans le script,
#      dans la liste des instruments ET dans la table des libellés de sortie. Une seconde table
#      DÉCLARATIVE le faisait passer pour traité.
#   2. Chercher la valeur en position de traitement (`== "x"`, `case "x"`, `"x":`) ne marche pas
#      davantage : une table de LIBELLÉS s'écrit exactement comme une table de TRAITEMENT.
#      Aucune expression régulière ne les distingue. Et le contrôle accusait au passage
#      `dispositions: quatre`, qui est le repli par défaut d'une fonction — traité sans jamais
#      être comparé.
#
# ★ CONCLUSION : ce contrôle-là ne peut pas être LEXICAL, il doit être COMPORTEMENTAL. Le bon
# endroit est l'auto-contrôle du plugin, qui tourne DANS le conteneur et peut simplement rendre
# chaque instrument déclaré et vérifier qu'il dessine quelque chose. C'est ce qui a été fait
# (scope : « instruments déclarés : chacun dessine »). Un garde-fou qui rate le cas pour lequel
# il a été écrit, et qui accuse du code correct, est pire que pas de garde-fou : il se fait
# ignorer, et c'est alors le vrai manquement qui passe.

def auditer_un(type_):
    """{type, ok, endpoints_absents, actions_absentes, reglages_morts, etat} pour un plugin.

    `etat` vaut « indéterminable » quand le script n'a pas pu être rendu ou analysé : on le DIT
    plutôt que de rendre une liste vide, qui se lirait comme « rien à signaler »."""
    from . import plugins as _pl
    m = _pl.get(type_)
    res = {"type": type_, "ok": True, "etat": "ok",
           "endpoints_absents": [], "actions_absentes": [], "reglages_morts": [],
           "hors_portee": []}
    if not m:
        res.update(ok=False, etat="manifeste introuvable")
        return res
    try:
        source = _pl.render_script(type_, dict(_CONFIG_MUET), "audit")
        lit = _litteraux(source)
        ident = _identifiants(source)
    except Exception as e:                                          # noqa: BLE001
        res.update(ok=False, etat="indéterminable (%s)" % e)
        return res

    ailleurs_plugin = _corpus_plugin(type_)
    ailleurs = _corpus_orchestrateur() + "\n" + ailleurs_plugin
    ctrl = m.get("control") or {}
    port_ctrl = ctrl.get("port") or 8082
    for nom, e, port in _chemins(ctrl.get("endpoints")):
        if port and int(port) != int(port_ctrl):
            # Servi par l'AGENT du conteneur (port 8081), qui vit dans l'image runtime, hors de
            # ce dépôt. On ne peut ni le confirmer ni l'infirmer : le DIRE, plutôt que de le
            # compter comme absent — un faux positif discrédite tout le reste du verdict.
            res["hors_portee"].append("%s (port %s)" % (nom, port))
            continue
        # Un chemin peut être servi par son préfixe (`startswith`) ou en entier : on accepte
        # toute chaîne du script qui commence par le chemin déclaré, ou dont il est le préfixe.
        if (not any(s == e or s.startswith(e) or (e.startswith(s) and len(s) > 1) for s in lit)
                and e not in ailleurs_plugin):
            res["endpoints_absents"].append(nom)
    for a in (m.get("actions") or []):
        p = a.get("path")
        if p and not any(s == p or s.startswith(p) for s in lit):
            res["actions_absentes"].append(a.get("id") or p)
    for c in (m.get("config_schema") or []):
        k = c.get("key")
        if k and not _cle_lue(k, ident, ailleurs):
            res["reglages_morts"].append(k)

    res["ok"] = not (res["endpoints_absents"] or res["actions_absentes"] or res["reglages_morts"])
    return res


def auditer():
    """Audit de TOUS les plugins, trié : les manquements d'abord."""
    from . import plugins as _pl
    out = []
    types = sorted(m["type"] for m in _pl.all() if m.get("type"))
    for t in types:
        try:
            out.append(auditer_un(t))
        except Exception as e:                                      # noqa: BLE001
            log.warning("audit du plugin %s impossible : %s", t, e)
            out.append({"type": t, "ok": False, "etat": "erreur (%s)" % e,
                        "endpoints_absents": [], "actions_absentes": [],
                        "reglages_morts": [], "hors_portee": []})
    out.sort(key=lambda r: (r["ok"], r["type"]))
    return out
