# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Étalonnage : MESURER ce qu'un dispositif coûte réellement, pendant que l'utilisateur l'exerce.

Le principe, et il inverse la logique habituelle : ce n'est ni un manifeste ni une heuristique de
plugin qui décide du « niveau normal » d'un conteneur, c'est **l'utilisateur qui le joue**. Il monte
son dispositif, l'exerce vraiment (animations, transitions, bascules — le pire de ce qu'il fera),
lance une campagne, et obtient une distribution par conteneur. C'est ce constat, et lui seul, qui
autorise ensuite le mot « garanti ».

Ça résout d'un coup le problème que le découpage en régimes ne réglait qu'à moitié : une pointe qui
dure une seconde est invisible à un échantillonnage de 60 s, et personne — surtout pas un plugin —
ne sait dire à l'avance ce que « niveau normal » veut dire pour un dispositif donné.

## Pourquoi une sonde à part, et pas `containers.cpu_percent`

Parce que la métrique existante ne peut pas porter cette mesure :
  · arrondie au dixième, et NORMALISÉE par `cpu_count` — sur un nœud à 88 CPU, un cran vaut 8,8 %
    d'un cœur, donc deux charges séparées d'un seul cran passent pour un écart de 50 % ;
  · **écrêtée à 100** : un conteneur qui déborde de son cpuset rend exactement la même valeur qu'un
    conteneur qui le remplit tout juste. Or c'est précisément le débordement qu'un étalonnage doit
    voir.
Ici on lit `cpu.stat:usage_usec` du cgroup, en microsecondes, sans plafond et sans normalisation.
Validé le 2026-08-02 contre une voie indépendante (932,8 % contre 962 %).

## Ce qu'une mesure vaut, et où

Un profil est un fait à propos d'un COUPLE (configuration, machine) — il ne voyage pas. Le même
`avsync`, mêmes paramètres, coûte 40,8 % sur dl360-1 et 79,2 % sur r620-1 : un facteur 1,94. La
mesure appartient donc au nœud ; la GARANTIE, elle, appartient au projet, qui référence la mesure
dont elle provient. Restaurer un projet ailleurs ne transporte pas la garantie, seulement le besoin
— et le pré-vol doit le dire.

## Signature

Une mesure est indexée par `(signature, node_id)`. La signature identifie la configuration qui a
été mesurée : type + version de plugin + condensat des paramètres de déploiement. Changer les
réglages (passer un `split` de 4 à 9 boîtes) change la signature, donc PÉRIME la mesure au lieu de
la réutiliser en silence — le même piège que la version de plugin périmée.
"""
import hashlib
import json
import logging
import threading
import time

from .cpu_profiles import (pointe_vue as _pointe_vue,   # définition UNIQUE du « pic observé »
                           en_rafale as _en_rafale)
from .database import get_db, db_get_node, db_get_nodes, db_get_containers, db_add_alert
from . import node_driver

log = logging.getLogger(__name__)

# Période d'échantillonnage de la sonde. 1 Hz : une animation d'une seconde laisse au moins un
# point, là où les 60 s de `cpu_profiles` ne la voient jamais. En dessous, on paierait un aller-
# retour agent par seconde pour du bruit — le compteur cgroup est cumulatif, pas instantané.
PERIODE_S = 1.0
# Durée d'un appel à la sonde. Elle échantillonne EN LOCAL sur le nœud pendant cette durée puis
# rend la série entière : un aller-retour toutes les 6 s au lieu d'un par seconde.
# ⚠ C'est aussi la GRANULARITÉ d'accumulation : rien n'est enregistré avant la fin d'une fenêtre.
# Elle valait 20 s — un étalonnage de 15 s rendait donc ZÉRO point, sans rien dire d'utile.
# Constaté le 2026-08-02 sur un multiview : 7,5 s de campagne, 0 point, « mesure insuffisante ».
FENETRE_S = 6.0
# Garde-fou : une campagne oubliée ne tourne pas indéfiniment.
DUREE_MAX_S = 3600.0
# En dessous, la campagne ne prouve rien : on refuse de publier une distribution.
MIN_POINTS = 20

_lock = threading.Lock()
_campagne = None        # None si aucune campagne en cours (cf. `etat()`)


# ── Sonde : exécutée SUR le nœud, rend une série de (ts, usage_usec) par conteneur ────────────
_SONDE = r"""python3 - <<'EOF'
import glob, json, re, subprocess, time

def conteneurs():
    out = subprocess.run("docker ps --format '{{.ID}} {{.Names}}'", shell=True,
                         capture_output=True, text=True).stdout
    return dict(l.split(None, 1) for l in out.splitlines() if len(l.split(None, 1)) == 2)

def usage(cid):
    # Deux dispositions de cgroup v2 selon l'installation Docker (systemd ou cgroupfs).
    for pat in ("/sys/fs/cgroup/system.slice/docker-%s*.scope/cpu.stat",
                "/sys/fs/cgroup/docker/%s*/cpu.stat"):
        hits = glob.glob(pat % cid)
        if hits:
            try:
                for ln in open(hits[0]):
                    if ln.startswith("usage_usec"):
                        return int(ln.split()[1])
            except OSError:
                return None
    return None

def cid_de_pid(pid):
    # Rattache un PID de l'hôte à son conteneur : le cgroup porte l'id long, dont l'id court de
    # `docker ps` est le préfixe. Même technique que app/placement.py.
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            m = re.search(r"[0-9a-f]{64}", f.read())
        return m.group(0) if m else None
    except OSError:
        return None

# GPU : `pmon` échantillonne par PROCESSUS (sm/enc/dec/mem). Lancé EN TÂCHE DE FOND pour toute la
# fenêtre — un `pmon -c 1` dans la boucle bloquerait une seconde à chaque tour et fausserait la
# cadence de la sonde CPU. Absence de nvidia-smi = pas de GPU : ce n'est pas une erreur.
pmon, fpmon = None, None
try:
    fpmon = open("/tmp/bobi-pmon.txt", "w+")
    pmon = subprocess.Popen(["nvidia-smi", "pmon", "-c", str(int(@FENETRE@)), "-d", "1", "-s", "um"],
                            stdout=fpmon, stderr=subprocess.DEVNULL)
except (OSError, ValueError):
    pmon = None

noms = conteneurs()
serie = []
fin = time.time() + @FENETRE@
while time.time() < fin:
    t = time.time()
    serie.append([t, {n: u for n, u in ((n, usage(c)) for c, n in noms.items()) if u is not None}])
    time.sleep(max(0.0, @PERIODE@ - (time.time() - t)))

gpu = {}
if pmon is not None:
    try:
        pmon.wait(timeout=5)
    except Exception:
        pmon.kill()
    try:
        fpmon.seek(0)
        long_de_court = {}
        for court, nom in noms.items():
            long_de_court[court] = nom
        for ln in fpmon:
            if ln.startswith("#"):
                continue
            ch = ln.split()
            if len(ch) < 7 or ch[1] == "-":
                continue
            try:
                pid = int(ch[1])
            except ValueError:
                continue
            cid = cid_de_pid(pid)
            if not cid:
                continue
            nom = next((n for c, n in long_de_court.items() if cid.startswith(c)), None)
            if not nom:
                continue
            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return 0.0
            e = gpu.setdefault(nom, {"sm": [], "enc": [], "dec": [], "mem": []})
            e["sm"].append(num(ch[3])); e["mem"].append(num(ch[4]))
            e["enc"].append(num(ch[5])); e["dec"].append(num(ch[6]))
    except OSError:
        pass
print(json.dumps({"cpu": serie, "gpu": gpu}))
EOF"""


def _cles_signature(type_):
    """Clés déterminantes déclarées par le manifeste, ou None (= condenser tous les paramètres)."""
    try:
        from . import plugins
        if not type_ or not plugins.is_plugin(type_):
            return None
        res = ((plugins.get(type_) or {}).get("resources") or {})
        cles = res.get("signature_keys")
        return sorted(str(k) for k in cles) if isinstance(cles, (list, tuple)) and cles else None
    except Exception as e:
        log.debug("etalonnage._cles_signature(%s): %s", type_, e)
        return None


def signature(conteneur):
    """Identifie la CONFIGURATION mesurée : type + version de plugin + condensat des paramètres.

    Deux conteneurs de même signature sont interchangeables du point de vue du coût ; deux
    signatures différentes ne le sont pas, même pour un même type. C'est ce qui empêche de
    réutiliser en silence la mesure d'un `split` à 4 boîtes pour un `split` à 9.
    """
    try:
        dc = json.loads(conteneur.get("deploy_config") or "{}") or {}
    except (TypeError, ValueError):
        dc = {}
    params = dict(dc.get("params") or {})
    version = params.pop("plugin_version", None)     # porté à part : il change sans changer le réglage
    # Le manifeste peut DÉCLARER les paramètres qui déterminent le coût (`resources.signature_keys`).
    # Sans cette déclaration on condense TOUT — et on périme alors le profil à chaque écriture de
    # l'orchestrateur dans les paramètres. Constaté sur le moteur 2110 le 2026-08-02 : `rx_flows` et
    # `rx_fmt` sont réécrits depuis l'état runtime à chaque rattachement de flux, si bien que le
    # profil se périmait à chaque redémarrage sans que le coût ait bougé. Le plugin déclare ce que
    # l'orchestrateur ne peut pas deviner ; le défaut reste « tous les paramètres ».
    cles = _cles_signature(dc.get("type"))
    if cles:
        params = {k: params.get(k) for k in cles}
    brut = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.sha256(brut.encode("utf-8")).hexdigest()[:12]
    return "%s@%s#%s" % (dc.get("type") or "?", version or "?", h)


def _stats(valeurs):
    """Distribution d'une série de coûts (% d'UN CPU). Le max compte autant que le p95 : c'est le
    pic exercé par l'utilisateur qui dimensionne une garantie, pas le régime courant."""
    v = sorted(valeurs)
    n = len(v)
    if not n:
        return None
    def q(p):
        return round(v[min(n - 1, max(0, int(round(p * (n - 1)))))], 1)
    return {"n": n, "min": round(v[0], 1), "median": q(0.5), "p95": q(0.95),
            "p99": q(0.99), "max": round(v[-1], 1),
            "moyenne": round(sum(v) / n, 1)}


def _sonder_noeud(node, fenetre):
    """Un aller-retour : rend {docker_name: [coûts en % d'un CPU]} pour la fenêtre écoulée."""
    # Substitution par MARQUEURS, pas par `%` : la sonde contient des `%s` littéraux (motifs de
    # chemins cgroup) que le formateur consommerait. Même famille de piège que les accolades
    # doublées des scripts de plugin — un gabarit dont le langage cible partage la syntaxe du
    # formateur doit utiliser des marqueurs qui ne collident avec rien.
    cmd = (_SONDE.replace("@FENETRE@", repr(float(fenetre)))
                 .replace("@PERIODE@", repr(float(PERIODE_S))))
    rc, out, err = node_driver.host_exec(node, cmd, timeout=int(fenetre) + 60)
    if rc != 0:
        raise RuntimeError((err or out or "rc=%s" % rc).strip()[:200])
    charge = json.loads((out or "").strip().splitlines()[-1])
    serie, gpu = charge.get("cpu") or [], charge.get("gpu") or {}
    couts = {}
    for (t0, a), (t1, b) in zip(serie, serie[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        for nom, u1 in b.items():
            u0 = a.get(nom)
            if u0 is None:
                continue
            # µs de CPU consommées / µs écoulées × 100 = % d'UN CPU. Aucun plafond : un conteneur
            # sur 4 cœurs saturés rend 400, et c'est bien ce qu'on veut lire.
            couts.setdefault(nom, []).append((u1 - u0) / (dt * 1e6) * 100.0)
    return couts, gpu


def _boucle(camp):
    """Thread de campagne : sonde chaque nœud en continu jusqu'à l'arrêt."""
    while True:
        with _lock:
            if camp["etat"] != "en_cours":
                return
            if time.time() - camp["debut"] > DUREE_MAX_S:
                camp["etat"] = "expire"
                camp["message"] = ("durée maximale atteinte (%d min) — campagne arrêtée d'office"
                                   % int(DUREE_MAX_S / 60))
                db_add_alert("alert.prep.etalonnage_duree_max",
                             "warning", kind="prep")
                return
            noeuds = list(camp["noeuds"])
        for nid in noeuds:
            node = db_get_node(nid)
            if not node:
                continue
            try:
                couts, gpu = _sonder_noeud(node, FENETRE_S)
            except Exception as e:
                with _lock:
                    camp["erreurs"][str(nid)] = str(e)[:200]
                log.warning("étalonnage: sonde %s: %s", node.get("name"), e)
                continue
            with _lock:
                # On fusionne MÊME si la campagne vient d'être arrêtée : ces points sont déjà
                # mesurés, les jeter parce que le clic est arrivé une seconde trop tôt serait
                # perdre exactement ce que l'utilisateur venait d'exercer.
                fini = camp["etat"] != "en_cours"
                camp["erreurs"].pop(str(nid), None)
                for nom, vals in couts.items():
                    camp["brut"].setdefault(nom, []).extend(vals)
                for nom, e in (gpu or {}).items():
                    d = camp["gpu"].setdefault(nom, {"sm": [], "enc": [], "dec": [], "mem": []})
                    for k in d:
                        d[k].extend(e.get(k) or [])
                camp["points"] = sum(len(v) for v in camp["brut"].values())
                if fini:
                    return


def demarrer(node_ids=None, projet=None, libelle=None):
    """Ouvre une campagne. `node_ids` : nœuds à sonder (défaut : ceux qui portent des conteneurs)."""
    global _campagne
    with _lock:
        if _campagne and _campagne["etat"] == "en_cours":
            return False, "une campagne est déjà en cours"
    conts = db_get_containers() or []
    if node_ids:
        cibles = [int(n) for n in node_ids]
    else:
        cibles = sorted({c["node_id"] for c in conts
                         if c.get("node_id") and c.get("status") == "running"})
    if not cibles:
        return False, "aucun nœud à sonder (aucun conteneur en marche)"
    camp = {"etat": "en_cours", "debut": time.time(), "fin": None, "noeuds": cibles,
            "projet": projet, "libelle": libelle, "brut": {}, "gpu": {}, "points": 0, "erreurs": {},
            "message": ""}
    with _lock:
        _campagne = camp
    th = threading.Thread(target=_boucle, args=(camp,), daemon=True, name="etalonnage")
    camp["thread"] = th
    th.start()
    if libelle:
        db_add_alert("alert.prep.etalonnage_demarre_libelle", "info", kind="prep",
                     params={"n": len(cibles), "libelle": libelle})
    else:
        db_add_alert("alert.prep.etalonnage_demarre", "info", kind="prep",
                     params={"n": len(cibles)})
    return True, camp


def arreter():
    """Ferme la campagne et rend le résultat PAR CONTENEUR, sans rien enregistrer.

    L'enregistrement est un geste SÉPARÉ (`sauver`) : une campagne ratée — un pic jamais joué, un
    nœud injoignable la moitié du temps — ne doit pas devenir un profil par simple écoulement du
    temps. L'utilisateur voit la distribution, puis décide.
    """
    with _lock:
        camp = _campagne
        if not camp or camp["etat"] != "en_cours":
            return False, "aucune campagne en cours"
        camp["etat"] = "termine"
        camp["fin"] = time.time()
        th = camp.get("thread")
    # Laisse la fenêtre en vol se terminer et se fusionner : sans cette attente, l'arrêt amputerait
    # le résultat de la dernière fenêtre — celle que l'utilisateur vient précisément d'exercer.
    if th is not None and th.is_alive():
        th.join(timeout=FENETRE_S + 8)
    return True, resultat()


def resultat():
    """Distribution par conteneur de la campagne courante (ou de la dernière close)."""
    with _lock:
        camp = _campagne
        if not camp:
            return None
        brut = {k: list(v) for k, v in camp["brut"].items()}
        gpus = {k: {kk: list(vv) for kk, vv in v.items()} for k, v in (camp.get("gpu") or {}).items()}
        base = {k: camp[k] for k in ("etat", "debut", "fin", "noeuds", "projet", "libelle",
                                     "points", "erreurs", "message")}
    from .docker_compute import _type_of
    par_nom = {}
    for c in db_get_containers() or []:
        if c.get("docker_name"):
            par_nom[c["docker_name"]] = c
    lignes = []
    for nom, vals in sorted(brut.items()):
        c = par_nom.get(nom)
        st = _stats(vals)
        if not st:
            continue
        lignes.append({
            "docker_name": nom,
            "vmid": (c or {}).get("vmid"),
            "hostname": (c or {}).get("hostname") or nom,
            "node_id": (c or {}).get("node_id"),
            "signature": signature(c) if c else None,
            # Un conteneur inconnu de la base (créé hors du modèle, ex. `rdma-*`) est mesuré quand
            # même : c'est justement le genre de charge que le modèle ignore et qui pèse.
            "hors_modele": c is None,
            "mesure": st,
            # GPU : présent SEULEMENT si des processus du conteneur ont réellement travaillé sur la
            # carte. Un GPU attribué mais inutilisé ne produit aucune ligne pmon — et l'absence de
            # mesure ne doit pas se lire comme un zéro mesuré.
            "gpu": ({k: _stats(v) for k, v in gpus[nom].items() if v} or None) if nom in gpus else None,
            "suffisant": st["n"] >= MIN_POINTS,
            "manque": max(0, MIN_POINTS - st["n"]),
            # Régime déclaré par le manifeste du type (cf. cpu_profiles.en_rafale).
            "regime": ("rafale" if (c and _en_rafale(_type_of(c))) else "continu"),
            # Une série plate signale que le pic n'a probablement pas été joué. On le DIT, au lieu
            # de laisser croire que le maximum observé est le maximum possible.
            # ⚠ N'a de sens que pour un type EN RAFALE. Un multiview refait la même composition à
            # chaque trame : sa série plate EST la mesure complète, pas une mesure incomplète.
            # Afficher « pic probablement pas atteint » sur un type stable est un faux avertissement
            # — et un avertissement qui crie toujours finit ignoré quand il a raison.
            "pointe_vue": (_pointe_vue(st) if (c and _en_rafale(_type_of(c))) else None),
        })
    base["duree_s"] = round((base["fin"] or time.time()) - base["debut"], 1)
    base["min_points"] = MIN_POINTS
    # Durée minimale utile, en clair : une fenêtre pour amorcer + le temps d'atteindre MIN_POINTS.
    base["duree_min_s"] = int(FENETRE_S + MIN_POINTS * PERIODE_S)
    base["conteneurs"] = sorted(lignes, key=lambda r: -(r["mesure"]["max"] or 0))
    return base


def etat():
    """État courant, sans les séries brutes (pollable depuis l'interface)."""
    r = resultat()
    if not r:
        return {"etat": "aucune"}
    return r


def sauver(vmids=None, note=None):
    """Enregistre les profils MESURÉS de la dernière campagne (table `profils_cpu`).

    `vmids` restreint aux conteneurs retenus par l'utilisateur — une campagne peut être bonne pour
    certains conteneurs et inutilisable pour d'autres (nœud injoignable, pic jamais joué), et
    forcer un tout-ou-rien reviendrait à jeter des mesures valides.
    """
    r = resultat()
    if not r or r["etat"] == "en_cours":
        return False, "aucune campagne close à enregistrer"
    garde = set(int(v) for v in vmids) if vmids else None
    n = 0
    with get_db() as db:
        for l in r["conteneurs"]:
            if not l["signature"] or not l["suffisant"]:
                continue
            if garde is not None and l["vmid"] not in garde:
                continue
            db.execute(
                "INSERT INTO profils_cpu (signature, node_id, vmid, hostname, mesure, "
                "duree_s, projet, note, ts) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (l["signature"], l["node_id"], l["vmid"], l["hostname"],
                 json.dumps(l["mesure"], ensure_ascii=False), r["duree_s"], r.get("projet"), note))
            n += 1
    db_add_alert("alert.prep.etalonnage_profils_enregistres", "info", kind="prep",
                 params={"n": n})
    return True, {"enregistres": n}


def profil(signature_, node_id):
    """Dernier profil mesuré pour ce couple (configuration, nœud), ou None.

    Le couple est indissociable : une mesure faite sur dl360-1 ne dit rien de dell-1 (facteur 1,94
    constaté sur un même `avsync`). C'est ce qui distingue une GARANTIE d'une estimation.
    """
    with get_db() as db:
        r = db.execute("SELECT mesure, ts, duree_s FROM profils_cpu WHERE signature=? AND node_id=? "
                       "ORDER BY id DESC LIMIT 1", (signature_, node_id)).fetchone()
    if not r:
        return None
    try:
        m = json.loads(r["mesure"])
    except (TypeError, ValueError):
        return None
    return {"mesure": m, "ts": r["ts"], "duree_s": r["duree_s"]}


# ── Garantie : transformer une mesure en cœurs réellement réservés ────────────────────────────
# Marge par défaut au-dessus du pic MESURÉ. Elle ne compense pas l'incertitude de la mesure (le
# compteur cgroup est exact) mais la variabilité de ce qui n'a pas été joué pendant la campagne.
MARGE = 1.25


def besoin(mesure, marge=MARGE):
    """Nombre de cœurs à réserver pour ce profil : le PIC mesuré × marge, arrondi au cœur SUPÉRIEUR.

    Le pic, pas le p95 : réserver sur le cas courant, c'est garantir de manquer précisément au
    moment qui compte — la transition, l'animation, le direct. Un cœur de trop coûte un cœur ;
    un cœur de trop peu coûte l'émission.
    """
    pic = float((mesure or {}).get("max") or 0.0)
    return max(1, int(-(-(pic * float(marge)) // 100)))


def garantir(vmid, coeurs=None, marge=MARGE):
    """Réserve les cœurs et POSE le cpuset à chaud, sans recréer le conteneur.

    `docker update --cpuset-cpus` s'applique à un conteneur en marche : la garantie n'impose pas de
    couper le direct pour être installée. C'est ce qui rend l'étalonnage utilisable en production —
    sinon personne ne l'utiliserait sur un dispositif en service.

    ⚠ On RÉSERVE (cpuset exclusif via `core_pool`), on ne LIMITE pas (`--cpus`, qui étrangle).
    Ce sont deux gestes opposés : l'un met des cœurs de côté pour ce conteneur, l'autre l'empêche
    de dépasser. Un étalonnage sert à garantir, pas à plafonner.
    """
    from . import core_pool
    with get_db() as db:
        c = db.execute("SELECT vmid, hostname, docker_name, node_id, deploy_config "
                       "FROM containers WHERE vmid=?", (int(vmid),)).fetchone()
    if not c:
        return False, "conteneur introuvable"
    c = dict(c)
    node = db_get_node(c["node_id"])
    if not node:
        return False, "nœud introuvable"
    if coeurs is None:
        p = profil(signature(c), c["node_id"])
        if not p:
            return False, ("aucun profil mesuré pour cette configuration sur ce nœud — étalonner "
                           "d'abord (une mesure faite ailleurs ne garantit rien ici)")
        coeurs = besoin(p["mesure"], marge)
    coeurs = int(coeurs)
    # Redimensionner impose de libérer d'abord : `allocate_cores` est idempotent PAR VMID et
    # renverrait l'ancienne allocation telle quelle.
    if core_pool.allocated_for(c["node_id"], c["vmid"]) not in (0, coeurs):
        core_pool.release_cores(c["vmid"])
    # Deux refus TRÈS différents, que confondre laisserait l'utilisateur sans action possible :
    # un nœud sans pool déclaré se règle en déclarant le pool ; un pool trop petit se règle en
    # libérant des cœurs ou en réduisant la bande isolée. Le message doit nommer lequel des deux.
    etat_pool = core_pool.cores_status(c["node_id"]) or {}
    if not (etat_pool.get("pool") or "").strip():
        return False, ("%s n'a AUCUN pool de cœurs déclaré (`compute_cpuset` vide) : ses conteneurs "
                       "sont tous partagés, et rien ne peut y être réservé. Déclarer le pool du "
                       "nœud d'abord — la mesure, elle, reste valable." % node.get("name"))
    cpuset = core_pool.allocate_cores(c["node_id"], c["vmid"], coeurs)
    if not cpuset:
        return False, ("pas assez de cœurs ordonnançables sur %s : %s demandés, %s libres sur %s "
                       "physiques (pool %s) — la garantie est REFUSÉE plutôt qu'accordée à moitié"
                       % (node.get("name"), coeurs, etat_pool.get("physical_free"),
                          etat_pool.get("physical"), etat_pool.get("pool")))
    rc, out, err = node_driver.host_exec(
        node, "docker update --cpuset-cpus '%s' %s" % (cpuset, c["docker_name"]), timeout=60)
    if rc != 0:
        core_pool.release_cores(c["vmid"])      # ne jamais laisser une réserve comptée mais non posée
        return False, "docker update a échoué : %s" % ((err or out or "").strip()[:200])
    db_add_alert("alert.prep.garantie_posee", "info",
                 vmid=c["vmid"], node_id=c["node_id"], kind="prep",
                 params={"h": c["hostname"], "coeurs": coeurs, "cpuset": cpuset})
    return True, {"vmid": c["vmid"], "coeurs": coeurs, "cpuset": cpuset, "a_chaud": True}


def pressions_noeud(nid):
    """Ressources du NŒUD sous tension, que le widget SIGNALE sans les mesurer lui-même.

    Bande passante mémoire, disque, SHM ne s'attribuent pas à un conteneur : les afficher par
    conteneur donnerait une fausse précision. Mais elles peuvent être la VRAIE contrainte — un
    multiview est borné par la bande passante mémoire, pas par le CPU. Un moniteur qui n'affiche que
    le CPU annoncerait « tout va bien » pendant l'effondrement.

    D'où le compromis : on ne duplique pas la page Monitoring (qui relève tout ça, par nœud et dans
    la durée), on renvoie vers elle. Le widget dit OÙ regarder, Monitoring montre QUOI.
    """
    from . import node_health
    snap = ((node_health.latest() or {}).get("nodes") or {}).get(str(nid)) or {}
    out = []
    mb = snap.get("membw") or {}
    # `level` est posé par le moniteur de bande passante quand la mesure s'écarte de sa ligne de
    # base : on réutilise SON verdict plutôt que d'inventer un seuil concurrent.
    if mb.get("level") in ("warning", "error"):
        out.append({"kind": "membw", "niveau": mb["level"], "valeur": mb.get("gbps"),
                    "ref": mb.get("baseline")})
    for nom, d in (snap.get("disks") or {}).items():
        pct = (d or {}).get("pct")
        if pct is not None and pct >= 85:
            out.append({"kind": "disque", "niveau": "error" if pct >= 95 else "warning",
                        "valeur": pct, "ref": nom})
    return out


def ressources(type_=None, node_id=None, vmid=None):
    """Vue « réservé / consommé / mesuré » par conteneur — la brique du moniteur de ressources.

    Trois quantités dans la MÊME unité (% d'un CPU), plus l'état de la mesure. C'est cet état qui
    interdit les affichages menteurs : un profil dont la configuration a changé depuis l'étalonnage
    porterait une garantie fausse, ce qui est pire que pas de garantie du tout.

    `etat` ∈ {non_etalonne, etalonne, perime, autre_noeud} :
      · `perime`      — une mesure existe pour ce type mais pas pour la SIGNATURE courante : les
                        réglages ont changé depuis. On ne la réutilise pas en silence.
      · `autre_noeud` — mesuré ailleurs seulement : estimation, jamais garantie (facteur 1,94
                        constaté entre deux nœuds sur une charge identique).
    """
    from . import core_pool, gpu_pool
    from .docker_compute import _type_of
    out, caps, press, gpu_par_noeud = [], {}, {}, {}
    for c in db_get_containers() or []:
        if c.get("status") != "running":
            continue
        t = _type_of(c)
        if (type_ and t != type_) or (node_id and c.get("node_id") != int(node_id)) \
                or (vmid and c.get("vmid") != int(vmid)):
            continue
        nid, v = c.get("node_id"), c.get("vmid")
        sig = signature(c)
        p = profil(sig, nid)
        etat_mesure, ailleurs = ("etalonne" if p else "non_etalonne"), None
        if not p:
            with get_db() as db:
                r = db.execute("SELECT signature, node_id, mesure FROM profils_cpu "
                               "WHERE (signature=? OR signature LIKE ?) ORDER BY id DESC LIMIT 1",
                               (sig, (t or "") + "@%")).fetchone()
            if r:
                etat_mesure = "autre_noeud" if r["node_id"] != nid else "perime"
                ailleurs = r["node_id"]
        if nid not in gpu_par_noeud:
            try:
                gpu_par_noeud[nid] = gpu_pool.gpu_par_vmid(nid) or {}
            except Exception:
                gpu_par_noeud[nid] = {}
        gpu_of = gpu_par_noeud[nid]
        n_reserves = core_pool.allocated_for(nid, v) if nid else 0
        cpu, ncpu = c.get("cpu_percent"), c.get("cpu_count")
        if nid not in caps:
            try:
                caps[nid] = core_pool.capacite_placement(nid)
            except Exception:
                caps[nid] = None
            try:
                press[nid] = pressions_noeud(nid) or None
            except Exception:
                press[nid] = None
        out.append({
            "vmid": v, "hostname": c.get("hostname"), "type": t, "node_id": nid,
            "docker_name": c.get("docker_name"),
            "signature": sig,
            # Réservé : des CŒURS EXCLUSIFS, pas un quota. 0 = « partagé, non réservé », l'état
            # majoritaire aujourd'hui — et il doit s'afficher comme tel, pas comme une garantie.
            "reserve_pct": (n_reserves * 100) or None,
            "consomme_pct": (round(float(cpu) * int(ncpu), 1) if cpu is not None and ncpu else None),
            "mesure": (p or {}).get("mesure"),
            "mesure_ts": (p or {}).get("ts"),
            "besoin_pct": (besoin((p or {}).get("mesure")) * 100) if p else None,
            "etat": etat_mesure, "mesure_node_id": ailleurs,
            "noeud": caps.get(nid),
            # Ressources de MACHINE sous tension : signalées, jamais mesurées ici (cf. `pressions_noeud`).
            "pressions": press.get(nid),
            # ⚠ Tout ce qui précède est du CPU, et RIEN d'autre. `gpu_index` dit seulement qu'un GPU
            # est ATTRIBUÉ à ce conteneur — sa consommation n'est pas mesurée à ce jour (il faudrait
            # interroger nvidia-smi par processus). L'afficher comme « attribué, non mesuré » plutôt
            # que de laisser croire que la barre couvre les deux ressources.
            "gpu_index": gpu_of.get(v),
            # Consommation GPU RÉELLE (pmon, cumulée sur les processus du conteneur), ou None si la
            # carte est attribuée sans être utilisée. None n'est PAS 0 : l'absence de mesure ne doit
            # pas se lire comme un zéro mesuré.
            "gpu_live": (_gpu_live.get(nid) or {}).get(c.get("docker_name")),
        })
    # Rafraîchissement du cache GPU en fond, pour les seuls nœuds portant des GPU alloués.
    _assurer_sonde_gpu({r["node_id"] for r in out if r.get("gpu_index") is not None})
    return out


# ── Échantillonnage GPU CONTINU (hors campagne) ───────────────────────────────────────────────
# La campagne mesure le GPU, mais elle est ponctuelle : sur une page de plugin GPU, l'utilisateur
# veut voir la carte travailler MAINTENANT, sans avoir à lancer quoi que ce soit. D'où ce sondage
# léger, par nœud portant des GPU alloués.
#
# `nvidia-smi pmon -c 1` bloque ~1 s : il ne peut donc PAS tourner dans la requête HTTP (l'endpoint
# est sondé toutes les 5 s par l'interface). Il tourne dans un thread, et la requête lit un cache.
GPU_TTL_S = 15.0
_gpu_live = {}          # node_id → {docker_name: {sm, enc, dec, mem}}
_gpu_ts = {}            # node_id → horodatage du dernier relevé
_gpu_thread = None

_SONDE_GPU = r"""python3 - <<'EOF'
import json, re, subprocess
out = subprocess.run("docker ps --format '{{.ID}} {{.Names}}'", shell=True,
                     capture_output=True, text=True).stdout
noms = dict(l.split(None, 1) for l in out.splitlines() if len(l.split(None, 1)) == 2)
def cid_de_pid(pid):
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            m = re.search(r"[0-9a-f]{64}", f.read())
        return m.group(0) if m else None
    except OSError:
        return None
res = {}
try:
    txt = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
                         capture_output=True, text=True, timeout=15).stdout
except Exception:
    txt = ""
for ln in txt.splitlines():
    if ln.startswith("#"):
        continue
    ch = ln.split()
    if len(ch) < 7 or ch[1] == "-":
        continue
    try:
        pid = int(ch[1])
    except ValueError:
        continue
    cid = cid_de_pid(pid)
    nom = next((n for c, n in noms.items() if cid and cid.startswith(c)), None)
    if not nom:
        continue
    def num(x):
        try:
            return float(x)
        except ValueError:
            return 0.0
    # Plusieurs processus d'un même conteneur : on CUMULE (un multiview shardé en a plusieurs).
    e = res.setdefault(nom, {"sm": 0.0, "mem": 0.0, "enc": 0.0, "dec": 0.0})
    e["sm"] += num(ch[3]); e["mem"] += num(ch[4])
    e["enc"] += num(ch[5]); e["dec"] += num(ch[6])
print(json.dumps(res))
EOF"""


def _rafraichir_gpu(node_ids):
    for nid in node_ids:
        node = db_get_node(nid)
        if not node:
            continue
        try:
            rc, out, err = node_driver.host_exec(node, _SONDE_GPU, timeout=40)
            if rc == 0:
                _gpu_live[nid] = json.loads((out or "{}").strip().splitlines()[-1])
        except Exception as e:
            log.debug("etalonnage: sonde GPU %s: %s", node.get("name"), e)
        _gpu_ts[nid] = time.time()


def _assurer_sonde_gpu(node_ids):
    """Lance un rafraîchissement en fond si le cache a vieilli. Ne bloque JAMAIS l'appelant."""
    global _gpu_thread
    vieux = [n for n in node_ids if (time.time() - _gpu_ts.get(n, 0)) > GPU_TTL_S]
    if not vieux:
        return
    with _lock:
        if _gpu_thread is not None and _gpu_thread.is_alive():
            return
        _gpu_thread = threading.Thread(target=_rafraichir_gpu, args=(vieux,), daemon=True,
                                       name="etalonnage-gpu")
        _gpu_thread.start()
