# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Le MODÈLE DE TALLY de l'orchestrateur — indépendant de tout protocole.

★ POURQUOI CE MODULE EXISTE. Ce code vivait dans `services/tsl`, parce que c'est TSL qui
l'a fait naître. Conséquence : `services/nmos/is07*.py` — le protocole censé prendre la
relève de TSL — IMPORTAIT TSL pour poser son tally. Supprimer le service TSL aurait donc
emporté IS-07 avec lui, et le distributeur, et les libellés. Le protocole qu'on remplace
tenait le modèle en otage.

Ici, un protocole n'est plus qu'un ADAPTATEUR : TSL et IS-07 sont à égalité au-dessus de ce
module, et aucun des deux n'est nécessaire à l'autre. Retirer TSL retire le TSL, rien de plus.

★ CE QUE CE MODULE NE CONNAÎT PAS, ET NE DOIT JAMAIS CONNAÎTRE : les tables d'un protocole.
Pas de `tsl_connections`, pas de `is07_connections`. Les porteurs de niveaux s'ENREGISTRENT
(cf. `enregistrer_porteur`), ils ne se découvrent pas. Ajouter un protocole ne doit pas
demander de rouvrir ce fichier.

★ CE QU'IL CONTIENT, en quatre familles :
  · l'état du tally en DEUX COUCHES (contributions par source, puis leur cumul) ;
  · la PROPAGATION du tally dans le graphe de traitement ;
  · la résolution des références de source (`resolve_ref`, ports virtuels de projet) ;
  · le registre des porteurs de niveaux.

⚠ Les imports de `app.database` / `app.plugins` sont PARESSEUX, dans les corps de fonction.
C'est ce qui évite les cycles — le risque est plus élevé ici que dans un service, `app.tally`
étant importé par du code de `app`.
"""
import json
import threading
import time

from app.numerotation import cle_input           # 0-based en entrée, clé 1-based en sortie

log = __import__("logging").getLogger(__name__)


# ─── Le verrou du MODÈLE ────────────────────────────────────────────────────────────────
# ⚠ DISTINCT de celui du service TSL, et ce n'est pas cosmétique : un seul verrou protégeait
# jusqu'ici deux invariants disjoints — l'état du tally d'un côté, le dictionnaire des
# connexions TCP de l'autre. Aucun chemin ne prend les deux domaines à la fois, la séparation
# est donc sûre ; les garder confondus aurait fait qu'un `reload()` de connexions bloque le
# distributeur.
_lock = threading.Lock()


# ═══ L'ÉTAT DU TALLY, EN DEUX COUCHES ═════════════════════════════════════════════════════════
#
# ★ PLUSIEURS SOURCES PEUVENT SERVIR LE MÊME NIVEAU, et c'est un cas voulu : deux contrôleurs
# broadcast sur une même chaîne de destination, un émetteur TSL doublé par un Receiver IS-07, un
# mélangeur qui complète ce qu'un pupitre externe annonce. Une seule couche ne pouvait pas
# l'exprimer : le dernier écrivain écrasait les autres, et surtout, une source qui repasse au vert
# écrivait « off » sur le rouge d'une AUTRE — un tally qui s'éteint sans que personne ne l'ait
# demandé, sur une fonction d'antenne.
#
#   `_tally_par_source[(index, niveau)][source]` — ce que CHAQUE source affirme. Une source
#   remplace toujours sa contribution ENTIÈRE (`poser_tally`), jamais case par case : sinon un
#   signal qui sort du programme garderait son rouge, faute d'un « off » explicite.
#
#   `_tally_state[(index, niveau)]` — le CUMUL, seul lu par les consommateurs. Rouge + vert donne
#   l'ambre, exactement comme deux contributions d'un même mélangeur.
_tally_par_source: dict = {}
_tally_state: dict = {}
_tally_dirty = threading.Event()



# ─── Ports virtuels de projet (chantier 4/5) ──────────────────────────────────
# Un mapping/label peut référencer "port:<id>" au lieu d'un shm brut : l'adresse reste
# stable côté contrôleur broadcast, le binding du port suit les rebinds/chargements.
_ports_cache = {"ts": 0.0, "by_id": {}, "by_pid": {}}

def _ports_snapshot():
    import time as _t
    now = _t.monotonic()
    if now - _ports_cache["ts"] > 3.0:
        try:
            from app.database import db_project_ports
            ports = db_project_ports(None)
        except Exception:
            ports = []
        _ports_cache["by_id"] = {p["id"]: p for p in ports}
        by_pid: dict = {}
        for p in ports:
            by_pid.setdefault(p["project_id"], []).append(p)
        _ports_cache["by_pid"] = by_pid
        _ports_cache["ts"] = now
    return _ports_cache

def _port_shm(port):
    """shm réel d'un port : binding.shm (source) ou binding.internal_shm (destination)."""
    b = (port or {}).get("binding") or {}
    return (b.get("shm") if (port or {}).get("kind") == "source"
            else b.get("internal_shm")) or None

def resolve_ref(ref):
    """"port:<id>" → shm réel du binding ; sinon renvoie ref tel quel.

    Tout ce qui n'est pas une chaîne ressort inchangé : le modèle traite sa clé comme OPAQUE, et
    doit continuer de le faire même quand un appelant l'adresse autrement. Lever ici arrêterait
    le distributeur — donc tout le tally du parc — pour une clé mal typée."""
    if not isinstance(ref, str):
        return ref
    ref = ref.strip()
    if ref.startswith("port:"):
        try:
            port = _ports_snapshot()["by_id"].get(int(ref[5:]))
        except (TypeError, ValueError):
            port = None
        return _port_shm(port)
    return ref



_CUMUL = {frozenset(("red", "green")): "amber"}

def cumuler(a, b):
    """Cumul de deux états sur UN MÊME niveau. Rouge + vert = ambre.

    ⚠ Ce n'est pas « PGM + PVW = orange » : rien ici ne connaît de bus. Deux CONTRIBUTIONS
    arrivent sur le même niveau pour le même index, et un niveau a plusieurs états dont l'un
    exprime la coexistence. C'est ce cumul, et lui seul, qui produit l'orange que voit
    l'exploitant quand une source est à la fois au programme et en préparation."""
    a, b = a or "off", b or "off"
    if a == "off":  return b
    if b == "off":  return a
    if a == b:      return a
    return _CUMUL.get(frozenset((a, b)), "amber")




# ══════════════════════════════════════════════════════════════════════════════════════════════
# PROPAGATION du tally — remonter le graphe depuis les sorties à l'antenne
# ══════════════════════════════════════════════════════════════════════════════════════════════
# La règle, en une ligne :
#
#     tally(entrée d'un élément) = tally(sortie de cet élément) ET (cette entrée CONTRIBUE)
#
# La récursion part des flux qu'un ÉMETTEUR a tallyés — aujourd'hui un contrôleur broadcast (VSM)
# via TSL, demain un Receiver IS-07 — et remonte le graphe de câblage (`derive_wiring`).
#
# ★ « CONTRIBUE » DÉPEND DU TYPE, et ce qu'on ne sait pas ne propage RIEN. Un élément traversant
#   (delay, correcteur, UDC) contribue toujours : sa sortie EST son entrée, transformée. Un
#   mélangeur ne contribue que par sa source PGM. Un DVE ne contribue que par ses sources
#   VISIBLES — et il ne sait pas encore le dire, donc il ne propage rien.
#
#   Inventer une contribution allumerait un rouge sur une source qui n'est pas à l'antenne : c'est
#   exactement le défaut qu'on corrige, à l'envers. `_CONTRIBUTION` est donc une liste FERMÉE, et
#   tout type absent vaut « je ne sais pas » — pas « tout ».
#
# ⚠ PLAFOND DE PROFONDEUR. Le graphe MXL peut boucler (une sortie recâblée sur une entrée en
#   amont, un aller-retour d'incrustation). Sans plafond, la propagation ne rendrait jamais la
#   main — et elle tourne dans la boucle du distributeur.

_PROFONDEUR_MAX = 12

# Ce qui contribue à la sortie d'un élément, PAR TYPE. Liste fermée : un type absent ne propage
# rien. Voir TODO.md § TALLY pour les deux familles qui manquent encore (mélangeur configurable,
# DVE), différées parce qu'elles demandent de toucher des plugins.
_CONTRIBUTION = {
    "delay":            "toutes",
    "color_corrector":  "toutes",
    "udc":              "toutes",
    "avsync":           "toutes",
    "transcoder":       "toutes",
    "v210_bridge":      "toutes",
    "mixer":            "pgm",
}


def _producteur_de(shm, par_shm):
    return par_shm.get(shm)


def _entrees_contributives(ct, dc, etat_ctrl):
    """Les shm d'entrée qui contribuent à la sortie de ce conteneur. [] si on ne sait pas.

    `etat_ctrl` = le `/state` du conteneur, ou None. Un mélangeur ne contribue que par sa source
    PGM : sans son état, on ne SAIT pas laquelle — et on ne propage rien plutôt que de deviner."""
    from app import plugins as _plg
    regle = _CONTRIBUTION.get(dc.get("type") or "")
    if not regle:
        return []
    params = dc.get("params") or {}
    w = _plg.derive_wiring(dc.get("type"), ct.get("hostname"), params) or {}
    entrees = []
    for p in (w.get("consumes") or []):
        if (p.get("essence") or "video") != "video":
            continue
        shm = (params.get(p.get("state_field") or "") or "").strip()
        if shm:
            entrees.append(shm)
    if regle == "toutes":
        return entrees
    if regle == "pgm":
        if not etat_ctrl:
            return []
        pgm = etat_ctrl.get("pgm")
        if pgm is None:
            return []
        # Le câblage vient de l'ÉTAT VIVANT, comme dans `_mixer_publisher_tick` : c'est lui qui
        # sait sur quoi le mélangeur est réellement branché à cet instant. Les params ne servent
        # que de repli — ils peuvent être en retard d'un câblage à chaud.
        shm = (etat_ctrl.get(cle_input(pgm)) or params.get(cle_input(pgm)) or "").strip()
        return [shm] if shm else []
    return []


def propager(etat, par_shm, etat_ctrl_de):
    """{(flux, niveau): couleur} À AJOUTER par propagation. Ne modifie rien.

    `par_shm`       : shm produit → (conteneur, deploy_config)
    `etat_ctrl_de`  : vmid → `/state` du conteneur, ou None

    ★ L'état étant adressé PAR FLUX, il n'y a plus rien à traduire ici. La version précédente
    recevait une callable `idx_de` et balayait tous les flux connus pour retrouver lequel portait
    l'index allumé — une recherche inverse en O(état × flux) qui, en prime, abandonnait
    (`continue`) dès qu'un flux amont n'avait pas d'index chez le porteur du niveau. Une caméra
    parfaitement câblée ne recevait donc aucun tally propagé au seul motif qu'un protocole tiers
    ne la connaissait pas.

    Renvoie un dict SÉPARÉ plutôt que d'écrire dans `_tally_state` : l'appelant doit pouvoir
    distinguer ce qu'un émetteur a dit de ce que nous avons déduit. Sans cette séparation, un
    tally propagé deviendrait indiscernable d'un tally reçu au tour suivant, et se propagerait
    à son tour — la boucle se referme sur elle-même."""
    ajouts = {}
    # File des (shm à l'antenne, niveau, couleur) à remonter.
    file = []
    for (ref, niveau), couleur in (etat or {}).items():
        if couleur == "off":
            continue
        if ref in (par_shm or {}):
            file.append((ref, niveau, couleur, 0))
    vus = set()
    while file:
        shm, niveau, couleur, prof = file.pop()
        if prof >= _PROFONDEUR_MAX or (shm, niveau) in vus:
            continue
        vus.add((shm, niveau))
        cible = _producteur_de(shm, par_shm)
        if not cible:
            continue
        ct, dc = cible
        for amont in _entrees_contributives(ct, dc, etat_ctrl_de.get(ct.get("vmid"))):
            cle = (amont, niveau)
            if (etat or {}).get(cle, "off") == "off" and ajouts.get(cle, "off") == "off":
                ajouts[cle] = couleur
            file.append((amont, niveau, couleur, prof + 1))
    return ajouts



def _cumul_des_sources(cle):
    """Couleur résultante d'une case, tous écrivains confondus. À appeler SOUS `_lock`."""
    couleur = "off"
    for c in (_tally_par_source.get(cle) or {}).values():
        couleur = cumuler(couleur, c)
    return couleur


def _poser_cases(source, cases):
    """Pose/actualise les cases nommées pour cette source, sans toucher aux autres. Sous `_lock`
    en interne ; renvoie True si le CUMUL a bougé quelque part."""
    change = False
    with _lock:
        for cle, couleur in (cases or {}).items():
            par = _tally_par_source.setdefault(cle, {})
            if couleur == "off":
                if par.pop(source, None) is None:
                    continue
            elif par.get(source) == couleur:
                continue
            else:
                par[source] = couleur
            neuf = _cumul_des_sources(cle)
            if not par:
                _tally_par_source.pop(cle, None)
            if _tally_state.get(cle, "off") != neuf:
                if neuf == "off":
                    _tally_state.pop(cle, None)
                else:
                    _tally_state[cle] = neuf
                change = True
    return change


def poser_tally(source, cases, reveiller=True):
    """Remplace la contribution ENTIÈRE de `source`. Renvoie True si le cumul a bougé.

    ★ ENTIÈRE, ET C'EST LE POINT. Un écrivain qui ne poserait que ses cases allumées ne pourrait
    jamais en éteindre une : la source qui sort du programme garderait son rouge indéfiniment,
    faute d'un « off » explicite. On retire donc d'abord tout ce que cette source affirmait et
    qu'elle n'affirme plus — sans toucher à ce que les AUTRES affirment sur les mêmes cases.

    `source` est une chaîne qui identifie l'écrivain : `tsl:<id>`, `mixer:<vmid>`,
    `is07:<receiver>`. Deux écrivains sur le même niveau se CUMULENT (rouge + vert = ambre) au
    lieu de s'écraser."""
    cases = {k: v for k, v in (cases or {}).items() if v and v != "off"}
    change = False
    with _lock:
        # ⚠ CE FILTRE EST UNE OPTIMISATION, PAS LE GARDE-FOU. Ce qui protège les autres écrivains,
        # c'est le `par.pop(source)` de `_poser_cases` : retirer sa propre entrée d'une case ne
        # touche à rien d'autre. Vérifié par mutation — élargir cette liste à toutes les cases ne
        # change aucun résultat. Ne pas la « corriger » en croyant renforcer quelque chose.
        anciennes = [cle for cle, par in _tally_par_source.items() if source in par]
    a_retirer = {cle: "off" for cle in anciennes if cle not in cases}
    if a_retirer:
        change = _poser_cases(source, a_retirer) or change
    change = _poser_cases(source, cases) or change
    if change and reveiller:
        _tally_dirty.set()
    return change


def sources_du_tally() -> dict:
    """`{"<ref>_<niveau>": {source: couleur}}` — QUI affirme quoi. Sert au diagnostic : sans
    ça, un niveau servi par deux écrivains ne dit pas lequel allume la lampe."""
    with _lock:
        return {f"{ref}_{lvl}": dict(par)
                for (ref, lvl), par in _tally_par_source.items() if par}


def get_tally_state() -> dict:
    with _lock:
        # Clé plate `<ref>_<niveau>`. Le niveau est un UUID — donc sans souligné — ce qui rend
        # la coupure au DERNIER souligné non ambiguë, même quand la référence en contient.
        return {f"{ref}_{lvl}": color for (ref, lvl), color in _tally_state.items()}



def get_tally_level(ref: str, level: str) -> str:
    """État cumulé de CE flux sur CE niveau. `ref` est une référence de source (shm), pas un
    index de protocole : le modèle ne connaît aucun protocole, et un index n'y entre jamais."""
    with _lock:
        return _tally_state.get((resolve_ref(ref) or ref, level), "off")


# ─── Signal de changement — API PUBLIQUE ────────────────────────────────────────────────
# ★ POURQUOI UNE API PLUTÔT QUE L'ÉVÉNEMENT NU. `services/nmos/is07.py` faisait
# `tsl._tally_dirty.wait(...)` : un service atteignait l'attribut PRIVÉ d'un autre service, pour
# un signal qui n'appartenait ni à l'un ni à l'autre. Trois consommateurs l'attendent aujourd'hui
# — le distributeur, le client TSL sortant, la veille IS-07 — et rien ne dit lesquels dans le nom.

def attendre_changement(timeout=None):
    """Bloque jusqu'au prochain changement du CUMUL. Vrai si le signal est venu, faux au timeout.

    Le signal est levé par `poser_tally`/`poser_cases` UNIQUEMENT quand le cumul a bougé
    quelque part : le lever pour rien, c'est un push vers tous les murs à chaque trame reçue."""
    return _tally_dirty.wait(timeout=timeout)


def signaler_changement():
    """Lève le signal à la main. Sert à ceux qui changent ce que le tally RÉSOUT sans toucher aux
    couleurs — un libellé de source réécrit, par exemple : les murs doivent re-résoudre leur texte."""
    _tally_dirty.set()


def acquitter_changement():
    """Consomme le signal. À appeler par un consommateur AVANT de lire l'état, jamais après :
    entre la lecture et l'acquittement, un changement serait perdu."""
    _tally_dirty.clear()


def etat_brut():
    """Copie de `_tally_state` avec ses clés TUPLES `(index, niveau)`.

    ⚠ Distinct de `get_tally_state()`, qui APLATIT les clés en `"index_niveau"` pour l'API HTTP.
    Le distributeur et le client TSL sortant ont besoin des tuples ; leur faire re-découper la
    chaîne serait un aller-retour absurde, et une source de bug le jour où un niveau contient
    un souligné."""
    with _lock:
        return dict(_tally_state)


def poser_cases(source, cases):
    """Pose/actualise les cases NOMMÉES de cette source, sans toucher à ses autres cases.

    ⚠ EXCEPTION ASSUMÉE à la règle de `poser_tally` (« une source remplace sa contribution
    ENTIÈRE »). Un serveur TSL reçoit ses index un par un, chaque trame n'en portant qu'un :
    lui faire remplacer tout à chaque trame éteindrait les index dont il n'a pas encore reparlé.
    Ne pas « uniformiser » ces deux fonctions — cf. la docstring de `poser_tally` pour la règle
    générale, et pourquoi elle ne s'applique pas ici."""
    return _poser_cases(source, cases)


# ─── Registre des PORTEURS de niveaux ───────────────────────────────────────────────────
# ★ CE REGISTRE EST CE QUI REND LE MODÈLE INDÉPENDANT DES PROTOCOLES. Le distributeur allait
# chercher ses porteurs dans `db_get_tsl_connections()` : le modèle lisait donc la table d'un
# protocole, et il aurait fallu lui apprendre `is07_connections`, puis la table du suivant.
#
# Un porteur possède des niveaux et sait, pour chacun, quel INDEX désigne une source donnée.
# Deux porteurs peuvent employer le même index pour des sources différentes — c'est pourquoi
# l'index se résout TOUJOURS chez un porteur, jamais dans une table à plat.
#
# Chaque protocole s'enregistre au démarrage et se retire à l'arrêt. Le modèle ne sait pas qui
# ils sont, et c'est le but.
_porteurs: dict = {}


def enregistrer_porteur(cle, niveaux, index_de, nom=None, ref_de=None):
    """Déclare un porteur de niveaux.

    `cle`       — identifiant stable et unique, préfixé par le protocole : `"tsl:3"`, `"proj:12"`.
    `niveaux`   — liste d'UUID de niveaux que ce porteur alimente.
    `index_de`  — callable `(shm, niveau) -> index | None`. C'EST UNE FONCTION, pas une table :
                  le porteur seul sait comment il adresse ses sources, et il peut le faire
                  évoluer sans que le modèle en soit informé.
    `nom`       — libellé d'affichage, facultatif.
    `ref_de`    — callable `(shm) -> ref | None`, facultative. La référence sous laquelle le
                  LIBELLÉ de cette source a été écrit, quand elle diffère du shm : un mapping
                  peut viser `port:<id>`, et le libellé reste attaché à cette écriture-là.
                  Perdre ce chemin fait disparaître un texte sans la moindre erreur.
    """
    if not cle:
        return
    with _lock:
        _porteurs[str(cle)] = {"niveaux": [n for n in (niveaux or []) if n],
                               "index_de": index_de, "ref_de": ref_de,
                               "nom": nom or str(cle)}


def retirer_porteur(cle):
    """Retire un porteur. Ses cases de tally ne sont PAS effacées : c'est à l'écrivain de les
    retirer avec `poser_tally(source, {})`. Un porteur qui disparaît n'éteint pas ce qu'un
    AUTRE écrivain affirmait sur les mêmes niveaux."""
    with _lock:
        _porteurs.pop(str(cle), None)


def liste_porteurs():
    """Copie du registre. Nommée `liste_porteurs` et non `porteurs` : ce dernier est le nom
    d'une variable LOCALE du distributeur, qui masquerait silencieusement la fonction. L'ordre d'insertion fait foi : le PREMIER porteur qui déclare un
    niveau en est le porteur retenu — deux protocoles qui prétendent au même niveau ne peuvent
    pas se le disputer à chaque tour."""
    with _lock:
        return dict(_porteurs)


def porteur_pour(niveaux):
    """`(niveau, porteur)` du premier niveau demandé dont un porteur est enregistré, sinon
    `(None, None)`. C'est la question que pose le distributeur pour chaque tuile."""
    reg = liste_porteurs()
    for n in (niveaux or []):
        for cle, p in reg.items():
            if n in p["niveaux"]:
                return n, dict(p, cle=cle)
    return None, None


def rafraichir_porteurs_projets():
    """(Re)déclare un porteur par PRODUCTION qui possède au moins un niveau.

    ★ POURQUOI LE MODÈLE PEUT LE FAIRE LUI-MÊME. Une production et ses niveaux sont des
    notions du modèle — tables `projects` et `tally_levels`. Aucune table de protocole n'est
    lue ici, et c'est la différence avec les porteurs TSL ou IS-07, qui doivent, eux,
    s'enregistrer depuis leur service.

    L'index d'une source y est l'`ord` de son port de projet : une production adresse ses
    sources par leur position, pas par un numéro qu'un contrôleur externe lui aurait imposé.

    À appeler à chaque tour du distributeur : productions et niveaux bougent à chaud."""
    try:
        from app.database import db_get_projects, db_get_tally_levels_of
        projets = db_get_projects() or []
    except Exception as e:
        log.debug("tally: porteurs de production non rafraîchis (%s)", e)
        return

    vivants = set()
    for pr in projets:
        pid = pr.get("id")
        try:
            niv = db_get_tally_levels_of("project", pid) or []
        except Exception:
            continue
        if not niv:
            continue
        cle = "proj:%s" % pid
        vivants.add(cle)
        enregistrer_porteur(cle, niv, _index_de_projet(pid),
                            nom=pr.get("name") or ("Projet #%s" % pid),
                            ref_de=_ref_de_projet(pid))

    # Une production qui perd son dernier niveau cesse d'être un porteur. Ne retirer QUE les
    # `proj:*` : les porteurs d'un protocole ne nous appartiennent pas.
    for cle in [c for c in liste_porteurs() if c.startswith("proj:")]:
        if cle not in vivants:
            retirer_porteur(cle)


def _index_de_projet(pid):
    """Fabrique le `index_de` d'une production : shm → `ord` de son port.

    ⚠ Une FERMETURE par production, et non une table figée : les ports se rebindent à chaud
    (chargement d'un projet, bascule de source). Une table capturée au moment de
    l'enregistrement serait périmée au premier rebind, et le tally partirait sur l'ancienne
    source sans que rien ne le signale."""
    def index_de(shm, _niveau=None):
        if not shm:
            return None
        for p in _ports_snapshot()["by_pid"].get(pid, []):
            if _port_shm(p) == shm:
                try:
                    return int(p.get("ord") or 0)
                except (TypeError, ValueError):
                    return None
        return None
    return index_de


def _ref_de_projet(pid):
    """Fabrique le `ref_de` d'une production : shm → `port:<id>` du port qui le porte.

    Le libellé d'une source de production est écrit sur la ligne du PORT, pas sur celle du shm :
    le port est stable quand le shm change au rebind. Chercher le libellé sous le shm seul le
    ferait disparaître au premier chargement de projet."""
    def ref_de(shm):
        if not shm:
            return None
        for p in _ports_snapshot()["by_pid"].get(pid, []):
            if _port_shm(p) == shm:
                return "port:%s" % p["id"]
        return None
    return ref_de


def index_chez(porteur, shm, niveau=None):
    """Index de `shm` chez ce porteur, ou None. Enveloppe les erreurs : un porteur dont la
    résolution lève ne doit pas arrêter le distributeur pour tous les autres."""
    if not porteur:
        return None
    try:
        return (porteur.get("index_de") or (lambda *_: None))(shm, niveau)
    except Exception:
        return None


# Ordre de repli des colonnes de libellé. La colonne demandée passe d'abord ; ensuite les
# colonnes SAISIES (2→9), qui portent un nom écrit par un humain ; puis le nom d'hôte (0), qui
# est toujours renseigné mais technique ; le shm brut (1) en dernier — c'est un identifiant, pas
# un libellé, et l'afficher est un aveu d'échec plutôt qu'une information.
ORDRE_REPLI_COLONNES = (2, 3, 4, 5, 6, 7, 8, 9, 0, 1)


_labels_cache = {"ts": 0.0, "par_shm": {}}
_LABELS_TTL_S = 1.0


def invalider_libelles():
    """À appeler dès qu'un libellé est écrit. Sans ça, l'instantané ferait attendre son TTL —
    une seconde pendant laquelle l'exploitant voit son texte refusé, et se demande s'il a été
    pris en compte."""
    _labels_cache["ts"] = 0.0


def _labels_snapshot():
    """shm → ligne de `source_labels`, relu au plus une fois par seconde.

    Le distributeur tourne à 10 Hz et lit les libellés de CHAQUE tuile de CHAQUE mur : sans
    instantané, un parc de dix murs ferait quelques centaines de requêtes par seconde pour une
    table qui bouge une fois par jour."""
    import time as _t
    now = _t.monotonic()
    if now - _labels_cache["ts"] > _LABELS_TTL_S:
        try:
            from app.database import db_get_source_labels
            rows = db_get_source_labels() or []
        except Exception:
            rows = []
        _labels_cache["par_shm"] = {(r.get("shm") or "").strip(): r for r in rows
                                    if (r.get("shm") or "").strip()}
        _labels_cache["ts"] = now
    return _labels_cache["par_shm"]


def veut_texte_pousse(fc, params):
    """Cette fenêtre CONSOMME-t-elle le texte poussé par l'orchestrateur ?

    ★ LA RÈGLE EST CELLE DU PLUGIN, et elle doit le rester. Le multiview a déjà cette décision
    sur son chemin TSL DIRECT (`wants_tsl_text`) : le libellé classique en mode « protocol », OU
    n'importe quel composant `umd` d'un modèle sourcé `tsl`. Le chemin CENTRAL n'en gardait que
    la première moitié — or l'installation courante utilise justement la seconde : des fenêtres
    en `label_source: hostname` dont le modèle porte un umd `text_source: tsl`. Elles ne
    recevaient plus rien, et un conteneur redémarré repartait donc SANS AUCUN LIBELLÉ.

    Le filtre n'est pas une optimisation de confort : pousser un texte à une fenêtre qui ne
    l'affiche pas la fait quand même re-baker son habillage plein cadre (~25 ms, une trame
    perdue), parce qu'un changement de glyphes n'est pas énumérable.

    Résolution du modèle identique à `_tpl_comps` : celui de la fenêtre, sinon celui du mur,
    sinon le modèle classique — qui se réduit précisément au test de gauche."""
    if fc.get("show_label") and fc.get("label_source") == "protocol":
        return True
    comps = (fc.get("template") or {}).get("components")
    if comps is None:
        comps = (params.get("default_template") or {}).get("components")
    for c in (comps or ()):
        if (isinstance(c, dict) and c.get("type") == "umd"
                and (c.get("text_source") or "name") == "tsl"):
            return True
    return False


def colonnes_de(shm, par_shm=None):
    """Les huit colonnes de libellé de ce flux, VIDES COMPRISES, plus son projet.

    ★ TOUJOURS LES HUIT. Une colonne absente et une colonne vide ne se distinguent pas côté
    conteneur : ne poser que les colonnes remplies laisserait les autres sur leur valeur d'avant,
    exactement la faute que ce mécanisme existe pour supprimer.

    Aucun repli de colonne ici, à la différence de `libelle_de` : un gabarit qui écrit
    `%src_label5%` désigne LA colonne 5, et lui servir le texte de la 2 serait afficher autre
    chose que ce qu'il demande. Le repli du conteneur — le nom de la fenêtre — reste le bon."""
    par_shm = _labels_snapshot() if par_shm is None else par_shm
    r = None
    for ref in (shm, resolve_ref(shm)):
        if ref and ref in par_shm:
            r = par_shm[ref]
            break
    r = r or {}
    return ({str(n): (r.get("label_%d" % n) or "") for n in range(2, 10)},
            r.get("projet") or "")


def libelle_de(shm, col, replier=True):
    """Libellé de ce flux dans la colonne demandée, à défaut dans la première qui en porte un.

    ★ REMONTER LES COLONNES, JAMAIS GARDER LE PRÉCÉDENT. Une colonne vide est une absence de
    libellé pour CE flux, pas une invitation à laisser en place celui du flux d'avant : c'est
    ainsi qu'un PiP a affiché « HyperDeck 2 Out » sur le mélangeur. On renvoie donc "" quand on
    ne trouve rien, et un "" poussé jusqu'au conteneur EFFACE — il ne s'ignore pas.

    On essaie les deux formes de référence (brute et résolue) parce que la ligne de libellé peut
    avoir été créée sous l'une ou l'autre selon l'époque et l'éditeur employé."""
    from app.database import db_get_source_label_for_shm
    refs = [r for r in (shm, resolve_ref(shm)) if r]
    vus = set()
    cols = [int(col or 0)] + ([c for c in ORDRE_REPLI_COLONNES] if replier else [])
    for c in cols:
        if c in vus:
            continue
        vus.add(c)
        for r in refs:
            try:
                t = (db_get_source_label_for_shm(r, c) or "").strip()
            except Exception:
                t = ""
            if t:
                return t
    return ""


def ref_chez(porteur, shm):
    """Référence d'origine du libellé de `shm` chez ce porteur, ou None."""
    if not porteur or not porteur.get("ref_de"):
        return None
    try:
        return porteur["ref_de"](shm)
    except Exception:
        return None


# ═══ LES DEUX FILS D'EXÉCUTION DU MODÈLE ════════════════════════════════════════════════
#
# ★ POURQUOI ILS SONT ICI ET PLUS DANS `services/tsl`. Le distributeur pousse couleur et texte
# aux fenêtres des multiviews, en HTTP ; le publisher lit le `/state` d'un mélangeur pour en
# déduire un tally. Ni l'un ni l'autre n'émet un octet de TSL. Les laisser dans le service
# voulait dire qu'arrêter TSL arrêtait la distribution du tally à tous les murs — y compris
# celui qui vient d'IS-07 ou du mélangeur.
#
# Le multiview GARDE SON CHEMIN DÉDIÉ dans le distributeur, plutôt que de passer par le hook
# `tally_targets` comme les autres plugins. C'est délibéré : c'est le composant le plus
# sensible du produit et il tourne. On ne le fait pas migrer sur du code neuf pour l'élégance.

_dist_thr = None
_mixer_pub_thr = None
_stop_evt = threading.Event()

# Dernier `/state` connu de chaque mélangeur, DÉPOSÉ par l'émetteur qui l'interroge déjà toutes
# les 0,3 s. La propagation le relit : refaire la requête depuis le distributeur doublerait le
# trafic vers les conteneurs pour la même information, à la même fraîcheur.
_etat_mixer: dict = {}


def _plg_wiring(type_, hostname, params):
    from app import plugins as _plg
    return _plg.derive_wiring(type_, hostname, params) or {}

def _sortie_a_l_antenne(ct, niveaux):
    """La sortie PGM de ce mélangeur porte-t-elle un tally, sur les niveaux de SA production ?

    ★ SUR SES NIVEAUX À LUI, pas sur n'importe lesquels. Le système fait tourner plusieurs
    productions en même temps, chacune possédant ses niveaux (`tally_levels.owner_*`) :
    « à l'antenne » n'a de sens que rapporté à une production. Regarder tous les niveaux ferait
    qu'un mélangeur de la production 2 s'allume parce qu'un signal homonyme est à l'antenne
    sur la production 5.

    C'est la sortie **PGM** qui décide — `CLEAN` et `PVW` ne disent rien de la diffusion.
    Renvoie False si on ne sait pas : ne pas savoir n'est pas une raison d'allumer un rouge."""
    import json as _json
    try:
        from app import plugins as _plg
        dc = ct.get("deploy_config")
        dc = _json.loads(dc) if isinstance(dc, str) else (dc or {})
        # ⚠ AVEC LES PARAMS. `derive_wiring` déplie les ports `repeat` sur eux : sans params, un
        # plugin dont les sorties se déplient (`repeat: "video_channels"`) renvoie une liste VIDE,
        # et la garde bloquerait son émission pour toujours. Le mélangeur y échappait parce que
        # ses trois sorties sont statiques — c'est une coïncidence, pas une propriété.
        w = _plg.derive_wiring(dc.get("type"), ct.get("hostname"), dc.get("params") or {}) or {}
        prod = w.get("produces") or []
        pgm = next((p for p in prod if (p.get("label") or "").upper() == "PGM"), None) or \
            (prod[0] if prod else None)
        shm = (pgm or {}).get("shm")
        if not shm:
            return False
        shm = resolve_ref(shm) or shm
        etat = etat_brut()               # le modèle prend SON verrou
        return any(etat.get((shm, n)) == "red" for n in (niveaux or ()))
    except Exception as e:
        log.debug("TSL: propagation — sortie de %s indéterminable (%s)", ct.get("vmid"), e)
        return False


def _mixer_publisher():
    import requests as _req
    from app.database import db_get_containers
    while not _stop_evt.is_set():
        try:
            _mixer_publisher_tick(_req, db_get_containers)
        except Exception as e:
            log.debug(f"TSL mixer publisher: {e}")
        if _stop_evt.wait(timeout=0.3):
            break

_mixers_publies: set = set()


def _retirer_mixer(vmid):
    """Retire la contribution de ce mélangeur. Renvoie True si le cumul a bougé."""
    if vmid not in _mixers_publies:
        return False
    _mixers_publies.discard(vmid)
    return poser_tally("mixer:%s" % vmid, {}, reveiller=False)


def _mixer_publisher_tick(_req, db_get_containers):
    from app.metrics import get_container_ip
    # ⚠ `db_get_projects` et `db_get_tsl_connections` étaient INJECTÉS SANS ÊTRE UTILISÉS —
    # un commentaire les disait gardés « pour les bancs », mais aucun banc n'appelle cette
    # fonction. Retirés : ce module ne doit citer aucune table de protocole, et l'en-tête le
    # promet. Une signature qui traîne des paramètres morts finit par les faire ré-utiliser.
    changed = False
    vus: set = set()
    for ct in db_get_containers():
        dc_raw = ct.get("deploy_config")
        if not dc_raw:
            continue
        try:
            dc = json.loads(dc_raw) if isinstance(dc_raw, str) else dc_raw
        except Exception:
            continue
        if (dc.get("type") or "") != "mixer":
            continue
        params = dc.get("params") or {}
        vus.add(ct["vmid"])
        if not params.get("tally_emit"):
            # ★ SE TAIRE, C'EST DIRE « RIEN », PAS SE TAIRE. Décocher « émettre le tally »
            # faisait `continue` : la contribution précédente de ce mélangeur restait dans le
            # modèle, et sa caméra gardait son rouge INDÉFINIMENT. L'exploitant a coupé
            # l'émission, il n'a pas demandé à figer un plateau. Reproduit avant correction.
            if _retirer_mixer(ct["vmid"]):
                changed = True
            continue
        # NIVEAUX de ce mélangeur : ceux qu'il déclare, sinon ceux de sa production. C'est une
        # LISTE depuis le dénouement — le cas « un seul » n'est que la liste à un élément.
        from app.database import db_get_tally_levels_of
        pid = ct.get("project_id")
        niveaux = params.get("tally_level_base") or []
        if not isinstance(niveaux, list):
            niveaux = [niveaux]
        if not niveaux:
            niveaux = db_get_tally_levels_of("project", pid)
        if not niveaux:
            # Idem : un mélangeur qui n'a plus de niveau n'adresse plus personne.
            if _retirer_mixer(ct["vmid"]):
                changed = True
            continue
        try:
            ip = get_container_ip(ct["vmid"])
            if not ip:
                continue
            st = _req.get(f"http://{ip}:8082/state", timeout=0.8).json()
        except Exception:
            # ⚠ ICI ON GARDE, ET C'EST DÉLIBÉRÉ. Un mélangeur injoignable est le plus souvent un
            # hoquet réseau d'un tour ; éteindre le tally d'une source à l'antenne pour 800 ms de
            # timeout serait pire que de le garder. Un mélangeur DÉTRUIT, lui, disparaît de la
            # liste des conteneurs et se fait retirer plus bas — c'est ce chemin-là qui répond du
            # cas définitif, pas celui-ci.
            continue
        _etat_mixer[ct["vmid"]] = st
        pgm, pvw = st.get("pgm"), st.get("pvw")
        shm_pgm = (st.get(cle_input(pgm)) or "") if pgm is not None else ""
        shm_pvw = (st.get(cle_input(pvw)) or "") if pvw is not None else ""
        want = {}
        def _poser(shm, couleur):
            """Pose une contribution sur TOUS les niveaux du mélangeur, en cumulant."""
            if not shm:
                return
            for lvl in niveaux:
                cle = (shm, lvl)
                want[cle] = cumuler(want.get(cle), couleur)
        # ★ LE TALLY SE PROPAGE : un mélangeur ne tallye ses entrées que si SA PROPRE SORTIE est
        # à l'antenne. Jusqu'ici l'émission était inconditionnelle — un mélangeur de préparation
        # allumait un rouge sur une caméra qui n'était diffusée nulle part. C'est le premier étage
        # du chantier « TALLY : le calculer par propagation » (TODO.md).
        #
        # `tally_force` (défaut VRAI) conserve l'ancien comportement : on livre la correction pour
        # tous, mais un site dont la sortie de mélangeur n'est mappée nulle part perdrait sinon
        # son tally du jour au lendemain, sans avoir rien demandé — sur une fonction d'antenne.
        # Le décocher, c'est demander la propagation.
        if not params.get("tally_force", True) and not _sortie_a_l_antenne(ct, niveaux):
            want = {}
        else:
            i_pgm = resolve_ref(shm_pgm) or shm_pgm
            i_pvw = resolve_ref(shm_pvw) or shm_pvw
            # ★ MÊME NIVEAU pour les deux. Avant le dénouement, le rouge et le vert partaient sur
            # DEUX niveaux distincts (le 1er et le 2nd du mélangeur) : une source au programme ET
            # en préparation occupait deux entrées qui ne se rencontraient jamais, et c'est
            # l'afficheur qui recomposait l'orange en lisant les deux champs de la trame. Le cumul
            # a désormais lieu ICI, sur le niveau — donc IS-07 et le multiview le voient aussi.
            _poser(i_pgm, "red")
            _poser(i_pvw, "green")
        # ★ REMPLACEMENT INTÉGRAL, PAR MÉLANGEUR. `poser_tally` retire tout ce que CE mélangeur
        # avait posé et qu'il ne pose plus — un changement de PGM éteint donc l'ancienne source —
        # sans jamais toucher à ce qu'un autre écrivain affirme sur les mêmes clés.
        if poser_tally("mixer:%s" % ct["vmid"], want, reveiller=False):
            changed = True
        _mixers_publies.add(ct["vmid"])
    # Un mélangeur DÉTRUIT, ou dont le conteneur a changé de type, ne repassera jamais par la
    # boucle : sans ce balayage, sa dernière contribution resterait dans le modèle pour toujours.
    for _vm in list(_mixers_publies - vus):
        if _retirer_mixer(_vm):
            changed = True
    if changed:
        _tally_dirty.set()

def _distributor():
    """Pousse tally + texte label vers chaque multiview selon sa flux_config."""
    import requests as _req
    # ★ PLUS AUCUNE TABLE TSL ICI, ni aucun index — c'est le but du chantier, mené en deux
    # temps. Ce distributeur lisait d'abord `db_get_tsl_connections` et `db_get_tsl_mappings_all` :
    # le modèle connaissait la table d'un protocole, et il aurait fallu lui apprendre
    # `is07_connections`, puis celle du suivant. Les porteurs se sont donc mis à se DÉCLARER.
    # Mais il restait à demander au porteur « quel est le NUMÉRO de ce signal », pour lire
    # ensuite l'état sous ce numéro — et une tuile dont la source n'avait de numéro chez personne
    # était SAUTÉE, donc figée sur son état d'avant (PiP4, 2026-09-01).
    #
    # L'état est désormais adressé PAR SOURCE. Le distributeur lit directement l'état du flux de
    # chaque tuile ; il n'y a plus de traduction, donc plus d'échec de traduction, donc plus de
    # tuile sautée. Les porteurs subsistent pour les protocoles SORTANTS, qui eux doivent bien
    # écrire un numéro sur un fil — mais plus rien ne LIT à travers eux.
    from app.database import db_get_containers
    _last_push: dict = {}   # vmid → (dernier payload poussé, ts) — anti-repush identique (cf. plus bas)

    while not _stop_evt.is_set():
        _tally_dirty.wait(timeout=0.1)
        if _stop_evt.is_set():
            break
        _tally_dirty.clear()

        state = etat_brut()          # le modèle prend SON verrou

        try:
            containers = db_get_containers()
            # PORTEURS de niveaux : les connexions ENTRANTES actives (les sortantes consomment
            # l'état, elles ne le servent pas) et les productions. Depuis le dénouement, chacun
            # POSSÈDE ses niveaux — on ne les recoupe plus par une bande commune, et deux
            # porteurs ne peuvent plus se disputer un niveau par construction.
            from app.database import db_get_tally_levels_of, db_get_projects
            # ★ LES PORTEURS VIENNENT DU REGISTRE, PLUS DES TABLES. Ce bloc lisait
            # `db_get_tsl_connections()` : le distributeur — donc le modèle — connaissait la
            # table d'un protocole, et il aurait fallu lui apprendre `is07_connections`, puis
            # celle du suivant. Chaque protocole se déclare désormais lui-même
            # (`_publier_porteurs`), et les productions sont déclarées par le modèle.
            rafraichir_porteurs_projets()

            def _porteur_pour(niveaux):
                """(niveau servi, porteur) — le premier niveau demandé dont quelqu'un écrit l'état.

                ★ LE NIVEAU DEMANDÉ EST LE NIVEAU LU. Avant le dénouement, on retrouvait le
                porteur puis on RE-CHOISISSAIT deux de ses trois niveaux via `rouge_field`/
                `vert_field` : le niveau demandé ne servait qu'à désigner le porteur, et pouvait
                n'être lu par personne. Le rouge et le vert sont maintenant deux ÉTATS du même
                niveau, et les champs TSL ne concernent plus que la mise sur le fil."""
                return porteur_pour(niveaux)
            # ★ PLUS DE TABLE À PLAT, ET PLUS DE TRADUCTION DU TOUT. Deux dictionnaires
            # étaient reconstruits à chaque tour — (connexion, shm) → index, et (connexion, shm)
            # → référence du libellé — en lisant `db_get_tsl_mappings_all()`. Ils ont d'abord
            # cédé la place au porteur, qui savait, lui, comment il adressait ses sources. Puis
            # la question elle-même a disparu : l'état étant indexé par flux, il n'y a plus rien
            # à traduire pour le lire. Le porteur ne sert plus qu'aux protocoles SORTANTS.
            # Niveaux par projet : défaut d'un conteneur qui n'en déclare pas.
            try:
                proj_niv = {pr["id"]: db_get_tally_levels_of("project", pr["id"])
                            for pr in db_get_projects()}
            except Exception:
                proj_niv = {}

            # ── PROPAGATION : remonter le graphe depuis les flux à l'antenne ──────────────
            # Ses déductions s'ajoutent à `state` POUR CE TOUR seulement, jamais à
            # `_tally_state`. C'est ce qui empêche la boucle : un tally propagé qu'on écrirait
            # dans l'état deviendrait, au tour suivant, indiscernable d'un tally REÇU, et se
            # propagerait à son tour d'un cran de plus, indéfiniment.
            try:
                par_shm = {}
                for _ct in containers:
                    _dc = _ct.get("deploy_config")
                    _dc = json.loads(_dc) if isinstance(_dc, str) else (_dc or {})
                    if not _dc:
                        continue
                    _w = _plg_wiring(_dc.get("type"), _ct.get("hostname"), _dc.get("params") or {})
                    for _p in (_w.get("produces") or []):
                        _shm = (_p.get("shm") or "").strip()
                        if _shm:
                            par_shm.setdefault(_shm, (_ct, _dc))

                deduits = propager(state, par_shm, _etat_mixer)
                for _k, _v in deduits.items():
                    state.setdefault(_k, _v)
            except Exception as e:
                log.debug("TSL: propagation ignorée ce tour (%s)", e)
        except Exception:
            continue

        updates_by_vmid: dict = {}
        overlays_by_vmid: dict = {}
        labels_by_vmid: dict = {}
        # UN SEUL instantané pour tout le tour : la table est la même pour tous les murs.
        labels_snap = _labels_snapshot()
        for ct in containers:
            dc_raw = ct.get("deploy_config")
            if not dc_raw:
                continue
            try:
                dc = json.loads(dc_raw) if isinstance(dc_raw, str) else dc_raw
            except Exception:
                continue
            type_ct = (dc.get("type") or "")
            if type_ct != "multiview":
                # ── AUTRES PLUGINS : le plugin DIT ce qu'il veut voir allumé ──────────────
                # ★ UN HOOK, PAS UNE BRANCHE PAR TYPE. Le distributeur connaissait un seul
                # modèle de données (`flux_config` du mur) ; chaque plugin qui voudrait du
                # tally aurait ajouté ici sa propre lecture, et ce fichier serait devenu un
                # catalogue de modèles étrangers. Le plugin déclare `tally_targets` et rend
                # une liste plate : le distributeur ne sait plus rien de personne.
                #
                # ⚠ LE MUR RESTE SUR SON CHEMIN. C'est le plus sensible du produit et il
                # tourne : on ne le fait pas passer sur du code neuf pour l'élégance.
                try:
                    from app import plugins as _plug
                    _h = _plug.get_hook(type_ct, "tally_targets")
                except Exception:
                    _h = None
                if not _h:
                    continue
                try:
                    cibles = _h(dc.get("params") or {},
                                {"vmid": ct["vmid"], "project_id": ct.get("project_id")}) or []
                except Exception:
                    continue
                for cible in cibles:
                    if not isinstance(cible, dict):
                        continue
                    shm_c = (cible.get("shm") or "").strip()
                    if shm_c.startswith("/dev/shm/"):
                        shm_c = shm_c[len("/dev/shm/"):]
                    if not shm_c:
                        continue
                    # Niveaux demandés par le plugin : liste d'identifiants, vide = ceux de
                    # son projet. Le champ garde son nom historique `niveau`, mais ce n'est plus
                    # un numéro de bande.
                    niv_c = cible.get("niveau") or []
                    if not isinstance(niv_c, list):
                        niv_c = [niv_c]
                    if not niv_c:
                        niv_c = proj_niv.get(ct.get("project_id")) or []
                    lvl_c, conn_c = _porteur_pour(niv_c)
                    # LE TEXTE EST RÉSOLU MÊME SANS NIVEAU DE TALLY. Un scope peut vouloir le
                    # libellé vivant d'une source sans jamais l'allumer en rouge — et c'est
                    # même le cas courant : un instrument n'est pas à l'antenne.
                    txt_c = libelle_de(shm_c, cible.get("label_col"))
                    coul_r = coul_v = "off"
                    if lvl_c:
                        _e = state.get((resolve_ref(shm_c) or shm_c, lvl_c), "off")
                        coul_r = "red"   if _e in ("red", "amber")   else "off"
                        coul_v = "green" if _e in ("green", "amber") else "off"
                    updates_by_vmid.setdefault(ct["vmid"], []).append(
                        {"cle": str(cible.get("cle") or shm_c), "shm": shm_c,
                         "rouge": coul_r, "vert": coul_v, "texte": txt_c})
                continue
            params = dc.get("params") or {}
            # Mode Direct : le multiview reçoit le TSL via son serveur local → ne pas double-piloter.
            tsl_mode = params.get("tsl_mode") or (
                "direct" if (int(params.get("tsl_port") or 0) > 0 and not params.get("tsl_remote"))
                else "central")
            if tsl_mode == "direct":
                continue
            flux_config = params.get("flux_config") or []
            vmid = ct["vmid"]
            for i, fc in enumerate(flux_config):
                if not isinstance(fc, dict):
                    continue
                # NIVEAUX de cette tuile : une LISTE d'identifiants depuis le dénouement.
                # Vide = « ceux de mon projet ». Le numéro de bande 1-based a disparu : il
                # réintroduisait le « 3 » de TSL au cœur d'un réglage de multiview.
                # ⚠ UNE CHAÎNE N'EST PAS UNE LISTE, et Python ne le dira pas : depuis que les
                # niveaux sont des UUID, un scalaire hérité est une CHAÎNE, et `for n in ...`
                # l'aurait parcourue caractère par caractère — trente-six « niveaux » d'une
                # lettre, dont aucun n'existe, donc un tally qui ne s'allume jamais et pas la
                # moindre erreur.
                # flux_config[i] câble via "path" ("/dev/shm/<shm>"), jamais "shm".
                shm = (fc.get("path") or "").strip()
                if shm.startswith("/dev/shm/"):
                    shm = shm[len("/dev/shm/"):]

                # ═══ LES LIBELLÉS, POUSSÉS SANS AUCUNE CONDITION ═══════════════
                #
                # ★ AVANT TOUT `continue`, ET C'EST LE POINT. Les colonnes de libellé étaient
                # CUITES dans la config au déploiement, le conteneur ne sachant pas lire la base.
                # Éditer un libellé ne l'atteignait donc jamais : le mur affichait la valeur du
                # jour de son dernier déploiement, indéfiniment et sans que rien ne le signale.
                # Constaté en production le 2026-09-01 — une fenêtre passée sur le Clean du
                # mélangeur montrait encore « Mire Externe ».
                #
                # Ce canal ne dépend ni d'un niveau de tally, ni d'un porteur, ni du mode de
                # libellé de la tuile : un libellé est un libellé. Toutes les autres conditions
                # de cette boucle ne gouvernent QUE le tally, plus bas.
                if shm:
                    _cols, _proj = colonnes_de(shm, labels_snap)
                    labels_by_vmid.setdefault(vmid, []).append(
                        {"flux_idx": i, "labels": _cols, "projet": _proj})

                # ═══ LE TALLY, lui, se gouverne ══════════════════════════
                niveaux_fc = fc.get("tally_level") or []
                if not isinstance(niveaux_fc, list):
                    niveaux_fc = [niveaux_fc]
                want_red   = bool(fc.get("tally_red"))
                want_green = bool(fc.get("tally_green"))
                want_text  = veut_texte_pousse(fc, params)
                if not niveaux_fc:
                    niveaux_fc = proj_niv.get(ct.get("project_id")) or []
                # ⚠ LE TEXTE NE DÉPEND PAS DU TALLY. Le `continue` groupait les trois conditions :
                # une fenêtre qui veut son libellé mais n'a aucun niveau de tally — le cas le plus
                # courant, une source qui n'est pas à l'antenne — n'en recevait aucun.
                if not (want_red or want_green or want_text):
                    continue
                if not niveaux_fc and not want_text:
                    continue
                # ⚠ LE PORTEUR NE COMMANDE PLUS RIEN ICI. `lvl_fc, conn = _porteur_pour(...)`
                # était suivi d'un `if not conn: continue` — une tuile dont les niveaux n'avaient
                # de porteur déclaré nulle part était SAUTÉE, donc figée sur son état d'avant.
                # Quatrième occurrence de la même faute dans cette chaîne. `conn` ne servait plus
                # qu'à ce test depuis que l'état s'adresse par source : il disparaît.
                lvl_fc, _ = _porteur_pour(niveaux_fc)
                # ★ SAUTER N'EST PAS ÉTEINDRE, et il n'y a plus de quoi sauter. Cette tuile
                # traduisait d'abord sa source en index de protocole, puis faisait `continue`
                # quand la traduction échouait — et le conteneur, qui n'applique QUE ce qu'on lui
                # pousse, gardait son dernier état. Une tuile basculée sur un flux sans
                # correspondance restait AU ROUGE, avec le libellé de l'ancienne source ; constaté
                # en production le 2026-09-01, un PiP passé sur le mélangeur gardait le rouge et
                # le nom d'un enregistreur.
                #
                # La tuile lit désormais l'état DE SA SOURCE. Il n'existe plus de cas « je ne sais
                # pas traduire » : soit ce flux a un tally, soit il n'en a pas, et l'absence
                # s'écrit "off" comme n'importe quel autre état. La panne n'est plus corrigée,
                # elle est devenue inexprimable.
                label_col = int(fc.get("label_col") or 0)

                # Le niveau a plusieurs états : `amber` allume les DEUX bandeaux de la tuile,
                # c'est ainsi que l'orange se voit sur le mur.
                _e = state.get((resolve_ref(shm) or shm, lvl_fc), "off") if lvl_fc else "off"
                color_l = "red"   if (want_red   and _e in ("red", "amber"))   else "off"
                color_r = "green" if (want_green and _e in ("green", "amber")) else "off"
                # ★ LE TEXTE N'EST POUSSÉ QUE SI LA TUILE LE DEMANDE. `want_text` était calculé
                # puis oublié : on écrasait le libellé de toute tuile ayant un tally, y compris
                # celles en `hostname` ou `mxl_path`, qui résolvent leur nom elles-mêmes. Une
                # tuile en `hostname` doit afficher le nom de son conteneur, que TSL existe ou non.
                upd = updates_by_vmid.setdefault(vmid, [])
                if want_text:
                    text = libelle_de(shm, label_col)
                    upd.append({"flux_idx": i, "slot": "L", "color": color_l, "text": text})
                    upd.append({"flux_idx": i, "slot": "R", "color": color_r, "text": text})
                else:
                    upd.append({"flux_idx": i, "slot": "L", "color": color_l})
                    upd.append({"flux_idx": i, "slot": "R", "color": color_r})

            # Overlays texte « TSL/Tableau » : reliés à une LIGNE du tableau /labels (label_row)
            # + une colonne (texte) + un niveau de Tally (allumage). Tout résolu côté orchestrateur.
            for ov in (params.get("overlays") or []):
                if not isinstance(ov, dict) or (ov.get("kind") or "") != "text":
                    continue
                if (ov.get("text_source") or "local") != "tsl":
                    continue
                # ★ UNE LIGNE VIDÉE S'EFFACE, elle ne se fige pas. Un `continue` ici laissait
                # l'overlay absent du paquet, donc le conteneur — qui n'applique que ce qu'on lui
                # pousse — sur le texte de la ligne PRÉCÉDENTE. Un bandeau d'antenne qui garde le
                # nom d'un invité parti est exactement ce qu'on ne veut pas.
                #
                # Un overlay basculé en `local`, lui, est bien SAUTÉ plus haut : le conteneur
                # ignore alors la couche centrale et rend son propre texte — il n'y a rien à
                # effacer, et pousser du vide écraserait une valeur qu'il tient lui-même.
                row_shm = (ov.get("label_row") or "").strip()
                o_text = libelle_de(row_shm, ov.get("label_col")) if row_shm else ""
                active = False
                o_niv = ov.get("tally_level") or []
                if not isinstance(o_niv, list):
                    o_niv = [o_niv]
                if not o_niv:
                    o_niv = proj_niv.get(ct.get("project_id")) or []
                # ⚠ `row_shm` d'abord : sans ligne, il n'y a pas de signal à interroger, et le
                # repli sur les niveaux du projet aurait sinon fait lire l'état de la clé VIDE.
                if row_shm and o_niv and (ov.get("tally_red") or ov.get("tally_green")):
                    lvl_o, _ = _porteur_pour(o_niv)
                    if lvl_o:
                        _eo = state.get((resolve_ref(row_shm) or row_shm, lvl_o), "off")
                        red_on   = bool(ov.get("tally_red"))   and _eo in ("red", "amber")
                        green_on = bool(ov.get("tally_green")) and _eo in ("green", "amber")
                        active = red_on or green_on
                ovl = overlays_by_vmid.setdefault(vmid, [])
                ovl.append({"id": ov.get("id"), "text": o_text, "active": active})

        from app.metrics import get_container_ip
        _now_p = time.time()
        for vmid in set(updates_by_vmid) | set(overlays_by_vmid) | set(labels_by_vmid):
            try:
                ip = get_container_ip(vmid)
                if not ip:
                    continue
                payload = {"updates": updates_by_vmid.get(vmid, []),
                           "overlays": overlays_by_vmid.get(vmid, []),
                           "labels": labels_by_vmid.get(vmid, [])}
                # ★ PERF : ne POSTER que si l'état a RÉELLEMENT changé. Ce distributeur tourne
                # sur un timeout de 100 ms (il repasse même sans événement TSL) : re-pousser un
                # paquet identique 10×/s faisait re-baker l'habillage PLEIN CADRE du multiview
                # 10×/s (PIL + RGBA→YUV + upload GPU ≈ 25 ms, soit une trame perdue à chaque
                # fois — mur 333 Horace mesuré à 28-36 fps au lieu de 50). Le mur a lui aussi
                # sa garde (comparaison de valeur avant de marquer sale, multiview ≥ 0.39.2) ;
                # celle-ci évite en plus 10 requêtes HTTP/s et par mur.
                # Re-synchro périodique (5 s) : un mur redéployé repart avec un tally VIDE — sans
                # ce filet, il resterait éteint jusqu'au prochain changement TSL. Coût nul côté
                # mur grâce à sa garde de valeur (paquet identique = aucun re-bake).
                _prev, _pts = _last_push.get(vmid, (None, 0.0))
                if _prev == payload and (_now_p - _pts) < 5.0:
                    continue
                _req.post(f"http://{ip}:8080/tally_bulk", json=payload, timeout=1)
                _last_push[vmid] = (payload, _now_p)
            except Exception:
                _last_push.pop(vmid, None)   # échec → re-pousser au prochain tour


def demarrer():
    """Démarre les deux fils du modèle. Idempotent : arrête d'abord ce qui tourne.

    ⚠ À APPELER AVANT le démarrage des protocoles. Un serveur TSL qui reçoit une trame avant
    que le distributeur ne tourne pose un tally que personne ne distribue — bénin, mais le
    mur reste éteint jusqu'au changement suivant."""
    global _dist_thr, _mixer_pub_thr
    arreter()
    _stop_evt.clear()
    _tally_dirty.clear()
    _dist_thr = threading.Thread(target=_distributor, daemon=True)
    _dist_thr.start()
    _mixer_pub_thr = threading.Thread(target=_mixer_publisher, daemon=True)
    _mixer_pub_thr.start()


def arreter():
    """Arrête les deux fils. `_tally_dirty` est levé pour débloquer les `wait` en cours."""
    global _dist_thr, _mixer_pub_thr
    _stop_evt.set()
    _tally_dirty.set()
    for thr in (_dist_thr, _mixer_pub_thr):
        if thr and thr.is_alive():
            thr.join(timeout=3)
    _dist_thr = _mixer_pub_thr = None


def fils_actifs():
    """(distributeur, publisher) — vrai si le fil tourne. Sert au diagnostic : un tally qui
    n'arrive nulle part alors que l'état est correct désigne un fil mort."""
    return (bool(_dist_thr and _dist_thr.is_alive()),
            bool(_mixer_pub_thr and _mixer_pub_thr.is_alive()))


# ═══ LES COLONNES DE LIBELLÉ ════════════════════════════════════════════════════════════
# ★ POURQUOI C'EST DU MODÈLE. Le réglage s'appelle `tsl_label_names` — nom hérité, TSL ayant
# introduit la notion. Mais ces colonnes servent aujourd'hui le multiview, IS-07 et les macros
# autant que TSL. Les laisser dans le service voulait dire qu'un site sans TSL n'avait plus
# de libellés du tout.


NOMS_COLONNES_DEFAUT = ["Hostname", "MXL", "Label 2", "Label 3", "Label 4",
                        "Label 5", "Label 6", "Label 7", "Label 8", "Label 9"]


def noms_colonnes():
    """Les DIX noms de colonnes, toujours — y compris ceux des colonnes masquées."""
    from app.database import db_get_setting
    noms = db_get_setting("tsl_label_names", None)
    if isinstance(noms, str):
        try:
            noms = json.loads(noms)
        except Exception:
            noms = None
    if not isinstance(noms, list):
        noms = []
    return [str(noms[i]) if i < len(noms) and noms[i] else NOMS_COLONNES_DEFAUT[i]
            for i in range(10)]


def nb_colonnes_actives():
    """Combien de colonnes PERSONNALISÉES sont offertes (1 à 8). Deux par défaut.

    ⚠ LE DÉFAUT NE S'APPLIQUE QU'AUX INSTALLATIONS NEUVES. `_migrer_colonnes_libelles` pose la
    valeur initiale au premier démarrage d'après ce qui EXISTE — une colonne renommée ou
    remplie compte — sinon un site qui se sert de six colonnes en verrait quatre disparaître de
    ses tableaux, sans un mot, pour un défaut qui ne le concernait pas."""
    from app.database import db_get_setting
    try:
        n = int(db_get_setting("label_cols_actives", 2))
    except (TypeError, ValueError):
        n = 2
    return max(1, min(8, n))
