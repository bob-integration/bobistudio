# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Coût CPU **MESURÉ** par type de conteneur — confronté à ce que son manifeste DÉCLARE.

Le projet sait allouer des cœurs avec beaucoup de soin (`core_pool`, neuf correctifs) et n'a
jamais su ce qu'un conteneur COÛTE. Les profils `resources.cores` des manifestes sont déclaratifs
et confrontés à rien : `streamer` annonce 4 cœurs depuis 2026-06 sans que personne ait vérifié si
c'est 1 ou 6. Toute question de capacité se répond donc par inférence — et l'inférence s'est
trompée trois fois dans la seule journée du 2026-08-01, dont une d'un facteur 30.

Ce module ne mesure rien lui-même : `containers.cpu_percent` est DÉJÀ alimenté en continu par
l'agent par-conteneur (cgroup v2, cf. `metrics.rafraichir_metrics`). Il ACCUMULE, par type, ce qui
passe déjà sous les yeux de l'orchestrateur, et en tire une statistique utilisable.

**Pourquoi accumuler plutôt que lire l'instantané** : un `cpu_percent` isolé est du bruit — un
conteneur qui vient de démarrer, une trame lourde, un GC. La médiane et le p95 sur des heures
disent quelque chose ; un échantillon ne dit rien. C'est exactement l'erreur méthodologique que ce
module existe pour empêcher (cf. [[cpu-consumption-must-be-measured-not-inferred]]).

**Unité** : pourcentage d'UN CPU (100 % = un cœur saturé), la même que `resources.cores × 100`.

⚠ Ce n'est **PAS** l'unité de `containers.cpu_percent`, qui est un pourcentage des CPU ALLOUÉS au
conteneur (l'agent divise par `n_cpus`, cf. `script_templates/agent.py:/stats`). Le coût absolu est
donc `cpu_percent × cpu_count`, d'où la colonne `containers.cpu_count`. Sans cette conversion, le
moteur 2110 ressort à « 57 % » alors qu'il consomme 9 cœurs, et deux types de cpuset différents
sont incomparables — c'est le genre d'unité implicite qui a produit toutes les erreurs de la
journée du 2026-08-01.

⚠ **Plancher d'écrêtage** : l'agent écrête `cpu_pct` à 100. Un conteneur qui saturerait son cpuset
et en réclamerait le double rend exactement la même valeur qu'un conteneur qui le remplit tout
juste. Les échantillons concernés sont donc des MINORANTS, comptés (`satures`) et signalés dans le
verdict plutôt que présentés comme des mesures.

⚠ Ce que ce module ne prétend PAS savoir : la part d'un coût qui vient du placement (NUMA, cœur
isolé, contention). Il mesure ce que le conteneur A CONSOMMÉ là où il tournait. Deux instances du
même type sur des nœuds différents peuvent légitimement diverger — d'où la statistique PAR TYPE ET
PAR NŒUD, et jamais une moyenne de flotte présentée comme une vérité.
"""
import json
import logging
import threading
import time

from .database import get_db
from .episodes import EtatEpisodes as _Episodes

log = logging.getLogger(__name__)

# Intervalle d'échantillonnage. Le coût d'un type bouge à l'échelle du déploiement, pas de la
# seconde : inutile d'empiler des points corrélés (et de gonfler l'état persisté pour rien).
INTERVALLE_S = 60.0
# Points conservés par (type, nœud). 240 × 60 s = 4 h de mémoire glissante : assez pour couvrir un
# régime de production, assez court pour qu'un changement de version de plugin se voie vite.
MAX_POINTS = 240
# En dessous, on n'affiche pas de statistique : trois points ne font pas une mesure.
MIN_POINTS = 10

_lock = threading.Lock()
_serie = {}                    # (type, node_id) → [(ts, cpu_percent), …]
_episodes = _Episodes("cpu_profiles")
_dernier = 0.0


def _cle(t, node_id):
    return "%s\x1f%s" % (t, node_id)


def _charger():
    """Recharge les séries persistées (le coût d'un type ne se réapprend pas à chaque redémarrage —
    sinon toute statistique est perdue au premier `systemctl restart`, et il y en a beaucoup)."""
    with _lock:
        if _serie:
            return
        for k in _episodes.cles():
            v = _episodes.get(k)
            if isinstance(v, list):
                # Points à 3 champs (ts, absolu, sature) UNIQUEMENT : les séries d'un format
                # antérieur portaient une autre unité, les relire les mélangerait en silence.
                _serie[k] = [tuple(p) for p in v if isinstance(p, (list, tuple)) and len(p) == 3]


def echantillonner(force=False):
    """Range un point par conteneur EN MARCHE dans la série de son (type, nœud). Appelée par la
    boucle de surveillance ; auto-throttlée à `INTERVALLE_S`."""
    global _dernier
    now = time.monotonic()
    if not force and (now - _dernier) < INTERVALLE_S:
        return
    _dernier = now
    _charger()
    from .database import db_get_containers
    from .docker_compute import _type_of
    ts = time.time()
    vus = set()
    try:
        conteneurs = db_get_containers() or []
    except Exception as e:
        log.warning("cpu_profiles: lecture des conteneurs impossible (%s)", e)
        return
    with _lock:
        for c in conteneurs:
            if c.get("status") != "running":
                continue
            cpu, n = c.get("cpu_percent"), c.get("cpu_count")
            t = _type_of(c)
            if cpu is None or not t:
                continue            # métrique pas encore remontée : ne PAS compter un 0 pour une mesure
            if not n:
                continue            # sans cpu_count le point n'a pas d'unité → il ne vaut RIEN
            # % des CPU alloués → % d'UN CPU. `sature` marque un point ÉCRÊTÉ par l'agent : c'est un
            # minorant, pas une mesure, et le confondre avec l'un fabriquerait un plafond invisible.
            absolu = float(cpu) * int(n)
            k = _cle(t, c.get("node_id"))
            s = _serie.setdefault(k, [])
            s.append((ts, absolu, 1 if float(cpu) >= 100.0 else 0))
            if len(s) > MAX_POINTS:
                del s[:len(s) - MAX_POINTS]
            vus.add(k)
    for k in vus:
        # ⚠ COPIE OBLIGATOIRE. `EtatEpisodes.poser` mémorise la valeur TELLE QUELLE et ne se marque
        # sale que si elle DIFFÈRE de la précédente. Lui passer la liste vivante lui fait stocker la
        # référence : au tour suivant on compare l'objet à lui-même (muté entre-temps par `append`),
        # l'égalité est toujours vraie, rien n'est jamais écrit. Constaté ici même — série à 5 points
        # en mémoire, fichier figé à 2. Le module d'épisodes existe précisément pour survivre aux
        # redémarrages : silencieusement inopérant, il ne sert à rien.
        _episodes.poser(k, [list(p) for p in _serie[k]])
    _episodes.flush()               # débouncé (30 s) : no-op tant que rien n'a changé


def _stat(points):
    vals = sorted(p[1] for p in points)
    n = len(vals)
    if not n:
        return None
    def q(p):
        return vals[min(n - 1, max(0, int(round(p * (n - 1)))))]
    satures = sum(p[2] for p in points)
    return {"n": n, "median": round(q(0.5), 1), "p95": round(q(0.95), 1),
            "max": round(vals[-1], 1), "min": round(vals[0], 1),
            # Nombre de points ÉCRÊTÉS par l'agent : au-delà de quelques pour cent, les
            # statistiques ci-dessus sont des minorants et doivent être lues comme tels.
            "satures": satures, "sature_pct": round(100.0 * satures / n, 1)}


def _resources(t):
    try:
        from . import plugins
        if not plugins.is_plugin(t):
            return {}
        m = plugins.get(t) or {}
        return ((m.get("resources") or {}) if isinstance(m, dict) else {}) or {}
    except Exception as e:
        log.debug("cpu_profiles._resources(%s): %s", t, e)
        return {}


def declare(t):
    """Coût DÉCLARÉ par le manifeste du type, en % d'un CPU (`resources.cores × 100`), ou None."""
    cores = _resources(t).get("cores")
    try:
        return None if cores in (None, "") else round(float(cores) * 100.0, 1)
    except (TypeError, ValueError):
        return None


# Rapport max/médiane au-delà duquel on considère qu'un régime NETTEMENT plus coûteux a été
# observé. Convention, assumée comme telle : il ne s'agit pas de mesurer le pic mais de savoir si
# on a le DROIT de conclure. 2× écarte la simple gigue sans exiger un cas extrême — le seul rapport
# documenté dans la flotte (split animé vs figé) vaut 12×, très au-dessus.
#
# ⚠ DÉFINITION UNIQUE, partagée avec `app/etalonnage.py`. Elle a d'abord existé en double (3× ici,
# 1,5× là-bas) : la même question rendait deux réponses opposées sur une même série.
RATIO_POINTE = 2.0
# Sur une série PASSIVE (des heures d'échantillons à 60 s), la gigue ordinaire atteint 2× sans
# qu'aucun régime coûteux n'ait eu lieu : `split` au repos donne médiane 8,4 / max 17,4. Il faut
# donc un critère plus exigeant que pour une campagne de 30 s, où un écart de 2× ne peut pas être
# du bruit. Même définition, sensibilité déclarée par contexte — plutôt que deux copies qui
# divergent en silence (c'était le cas : 3× ici, 1,5× dans l'étalonnage).
RATIO_POINTE_PASSIF = 3.0


def pointe_vue(st, ratio=RATIO_POINTE):
    """La série contient-elle une POINTE, ou n'a-t-on observé qu'un régime plat ?

    Une série plate autorise à dire « je n'ai pas vu », jamais « c'est trop ».
    """
    med = st.get("median") or 0.0
    return med > 0 and (st.get("max") or 0.0) >= float(ratio) * med


def en_rafale(t):
    """Le coût de ce type est-il BIMODAL (au repos vs en activité) ?

    `resources.regime == "rafale"`. Cas d'école : `split` — son propre script le chiffre, « sur CPU
    la même trame animée coûte jusqu'à 12× la trame fixe ». Un profil de ce type se dimensionne sur
    le PIC, et une fenêtre de mesure qui n'a jamais vu d'animation ne dit rigoureusement rien du
    pic. Sans cette distinction, le module criait au sur-dimensionnement en n'ayant observé que le
    repos — un verdict qui aurait fait rogner un manifeste correct (constaté 2026-08-02).
    """
    return str(_resources(t).get("regime") or "continu").strip().lower() == "rafale"


def profils(node_id=None):
    """Coût mesuré par type (et par nœud) : `[{type, node_id, mesure:{…}, declare, verdict}]`.

    `verdict` compare la mesure à l'INTENTION du manifeste — c'est tout l'objet du module :
      - `sur_dimensionne` : p95 < 40 % du déclaré → on réserve du vide (le pinning immobilise des
        cœurs que personne n'utilise, et le pré-vol projet refuse des déploiements pour rien) ;
      - `sous_dimensionne` : p95 > déclaré → le quota `--cpus` étrangle le conteneur, ou le profil
        ment au pré-vol ;
      - `sature` : ≥10 % des points écrêtés à 100 % de leur cpuset → le coût réel est INCONNU
        (minorant seulement) ; c'est un manque de mesure, pas un verdict de conformité ;
      - `regime_non_observe` : type déclaré `resources.regime == "rafale"` dont la fenêtre n'a
        montré qu'un régime plat — on a mesuré l'inactivité, pas la capacité (cf. `en_rafale`) ;
      - `conforme` / `non_declare` / `insuffisant` (moins de MIN_POINTS points).
    """
    _charger()
    out = []
    with _lock:
        elems = list(_serie.items())
    for k, points in elems:
        t, _, nid = k.partition("\x1f")
        if node_id is not None and str(node_id) != nid:
            continue
        st = _stat(points)
        if not st:
            continue
        d = declare(t)
        if st["n"] < MIN_POINTS:
            verdict = "insuffisant"
        elif st["sature_pct"] >= 10:
            # Trop de points écrêtés : on ne SAIT pas ce que ce type coûte, on sait seulement qu'il
            # remplit ce qu'on lui donne. Le dire, plutôt que de publier un p95 qui est un plancher.
            verdict = "sature"
        elif d is None:
            verdict = "non_declare"
        elif st["p95"] > d:
            verdict = "sous_dimensionne"
        elif st["p95"] < 0.4 * d:
            # ⚠ Un type EN RAFALE ne se juge pas sur une fenêtre de repos. On n'accuse de
            # sur-dimensionnement que si le régime coûteux a été VU au moins une fois : sans quoi
            # on mesure l'inactivité et on en conclut sur la capacité.
            verdict = ("sur_dimensionne"
                       if not en_rafale(t) or pointe_vue(st, RATIO_POINTE_PASSIF)
                       else "regime_non_observe")
        else:
            verdict = "conforme"
        # ⚠ Un profil ÉTALONNÉ sur ce nœud supersède tout ce qui précède : commenter l'intention du
        # manifeste n'a plus d'intérêt quand on dispose d'une mesure faite en exerçant réellement le
        # dispositif, à la microseconde et sans plafond. Le collecteur passif reste utile (il tourne
        # sans que personne n'agisse), mais il cesse de JUGER là où mieux a été mesuré — sinon deux
        # références concurrentes coexistent, et c'est la moins bonne qui parle le plus fort.
        # Les verdicts de QUALITÉ DE MESURE (`insuffisant`, `sature`) gardent la priorité : ils
        # portent sur la série elle-même, pas sur la référence à laquelle on la compare.
        etal = _etalonne(t, nid)
        if etal and verdict not in ("insuffisant", "sature"):
            verdict = "etalonne"
        out.append({"type": t, "node_id": (int(nid) if nid.isdigit() else None),
                    "mesure": st, "declare": d, "verdict": verdict,
                    # D'où vient la référence qui fait autorité pour ce couple (type, nœud).
                    "reference": ({"source": "etalonnage", "pic": etal} if etal
                                  else {"source": "manifeste", "declare": d})})
    out.sort(key=lambda r: (-(r["mesure"]["p95"] or 0), r["type"]))
    return out


def _etalonne(t, node_id):
    """Pic mesuré par ÉTALONNAGE pour ce type sur ce nœud, ou None.

    ⚠ On ne rattache PAS par type : l'étalonnage indexe par (signature, nœud), et la signature
    porte la version du plugin et le condensat des paramètres. Un profil mesuré sur une AUTRE
    configuration du même type superséderait le manifeste avec un chiffre qui ne décrit plus rien —
    exactement le piège du profil périmé. On ne retient donc que les profils dont la signature est
    celle d'un conteneur RÉELLEMENT en marche de ce type sur ce nœud.

    Plusieurs configurations en marche → on garde le pic le plus élevé : une garantie se
    dimensionne sur le pic.
    """
    from .etalonnage import signature as _sig        # tardif : `etalonnage` importe ce module
    from .docker_compute import _type_of
    from .database import db_get_containers
    try:
        sigs = {_sig(c) for c in (db_get_containers() or [])
                if c.get("status") == "running" and c.get("node_id") == int(node_id)
                and _type_of(c) == t}
    except Exception as e:
        log.debug("cpu_profiles._etalonne signatures(%s, %s): %s", t, node_id, e)
        return None
    if not sigs:
        return None
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT mesure FROM profils_cpu WHERE node_id=? AND signature IN (%s) "
                "ORDER BY id DESC LIMIT 20" % ",".join("?" * len(sigs)),
                [int(node_id)] + sorted(sigs)).fetchall()
    except Exception as e:
        log.debug("cpu_profiles._etalonne(%s, %s): %s", t, node_id, e)
        return None
    pics = []
    for r in rows:
        try:
            m = json.loads(r["mesure"])
            if m.get("max") is not None:
                pics.append(float(m["max"]))
        except (TypeError, ValueError):
            continue
    return max(pics) if pics else None


def cout_estime(t, defaut_cores=None, node_id=None):
    """Coût à RETENIR pour un type, en % d'un CPU, et d'où il vient :
    `(valeur, source)` avec source ∈ `etalonnage` | `mesure` | `manifeste` | `defaut`
    (par ordre d'autorité décroissante ; `node_id` requis pour que l'étalonnage soit consultable —
    une mesure ne vaut que sur la machine où elle a été faite).

    Le p95 mesuré prime sur le déclaré dès qu'il y a assez de points — c'est le point de tout
    l'exercice. Le p95 et non la médiane : dimensionner sur le cas courant garantit de manquer
    de CPU dans le cas qui compte. Toutes instances confondues (le pré-vol raisonne cluster)."""
    # Une mesure d'étalonnage sur le nœud visé prime sur tout : c'est la seule obtenue en
    # exerçant le dispositif, donc la seule qui couvre le régime coûteux.
    if node_id is not None:
        pic = _etalonne(t, node_id)
        if pic is not None:
            return pic, "etalonnage"
    _charger()
    tous = []
    with _lock:
        for k, points in _serie.items():
            if k.split("\x1f", 1)[0] == t:
                tous.extend(points)
    st = _stat(tous)
    if st and st["n"] >= MIN_POINTS:
        return st["p95"], "mesure"
    d = declare(t)
    if d is not None:
        return d, "manifeste"
    return ((float(defaut_cores) * 100.0) if defaut_cores else None), "defaut"
