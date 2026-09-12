# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Migration « le 0 n'existe pas » — renumérotation 1-based des flux du moteur 2110_io.

Décidée le 2026-08-11, exécutée le 2026-08-13. Convention et règle : cf. `app/io2110_flows.py`
(`numero()` / `indice()`), qui fait foi.

Ce que la migration DOIT déplacer EN UNE PASSE — un arrêt à mi-chemin laisse des câbles pointant
sur des flux disparus, et ça ne se voit pas tout de suite :

1. `containers.deploy_config` du moteur — CLÉS `tx{n}_shm` / `tx_audio{n}_shm` / `tx_anc{n}_shm`
   (+ suffixe `_fmt`) décalées de +1 ;
2. `containers.deploy_config` de TOUS les conteneurs, `containers.source`, `containers.shm_out` —
   VALEURS référençant un nom de flux du moteur (`<hn>_0` → `<hn>_1`, `_audio_`, `_anc_`, et les
   dérivés du tissu/pyramide `<hn>_0__p2`, `<hn>_0__s946x540`…) ;
3. `rdma_links.src_flow` — même renommage (l'UUID MXL est dérivé du NOM : les répliques sont à
   recréer derrière) ;
4. `nmos_resources.bind_slot` — `tx0:v` → `tx1:v`, `v:0` → `v:1` (sinon le registre ne retrouve
   plus le slot et re-sème une ressource orpheline À CÔTÉ de l'ancienne, qui resterait annoncée) ;
5. `settings.nmos_subscriptions` — RIEN. `recv_idx` est un INDICE DE TABLEAU (il part en
   `receiver_index` vers l'agent, qui s'en sert comme subscript de slot) : le décaler
   réabonne le slot d'à côté, en silence. Fait par erreur le 2026-08-13, corrigé le 15.

⚠ ORDRE DÉCROISSANT obligatoire partout (n → n+1 en partant du plus grand), sinon `_0`→`_1` puis
`_1`→`_2` décale deux fois le même flux.

⚠ IDEMPOTENCE : le marqueur `settings.migration_numerotation_1based` est posé à la fin. Sans lui,
un second passage re-décalerait tout. C'est le seul garde-fou — la migration n'est PAS détectable
à l'œil sur les données (un `_1` peut être un `_0` déjà migré ou un `_1` d'origine).

⚠⚠ L'ORCHESTRATEUR DOIT ÊTRE ARRÊTÉ. Vécu le 2026-08-13 : lancée pendant qu'un ancien processus
tournait, la migration a persisté quatre sites sur cinq — `nmos_resources.bind_slot` est revenu
en 0-based dans les secondes suivantes, SANS erreur ni trace. Cause : le processus vivant
republie NMOS, `_registry_id()` recalcule le MÊME id (les graines restent sur l'indice brut) et
`db_nmos_resource_upsert` fait `ON CONFLICT(id) DO UPDATE SET … bind_slot=excluded.bind_slot`.
L'ancien code réécrit donc le slot 0-based par-dessus le nôtre, à nombre de lignes constant —
d'où un état à mi-chemin indétectable au compteur de lignes.

Ordre OBLIGATOIRE : arrêter l'orchestrateur → migrer → redémarrer avec le nouveau code.
"""

import json
import re

MARQUEUR = "migration_numerotation_1based"

# Types de conteneurs dont un NOM DE FLUX porte un indice (miroir des manifestes — cf. le
# gabarit `{i1}` dans `wiring.produces`). `mixer` (`_pgm`/`_clean`/`_pvw`), `color_corrector`
# (`_cc`), `split`, `udc`, `stills`, `avsync`, `stream_in`, `multiview` (`shm_out` libre) et
# `sonde_latence` n'indexent pas leurs sorties : rien à décaler chez eux.
TYPES_INDEXES = ("2110_io", "delay", "probe_2110", "tone_gen", "player")

# Suffixes de flux dérivés d'un slot du moteur, tous construits sur `<hostname>_<n>` :
#   ''          → le flux vidéo lui-même
#   '_ident'    → mire d'identification RX
#   '__p2'…     → étages de pyramide, '__s946x540' → sorties de scaler (tissu)
# La frontière est donc « le nombre N'EST PAS suivi d'un autre chiffre ».
_FIN_NOMBRE = r"(?![0-9])"


def _motifs(hostname):
    """Motifs de renommage d'un moteur, du plus grand indice au plus petit.

    Renvoie une liste de `(regex_compilée, remplacement_fabriqué)` — le remplacement est calculé
    par fonction pour que le +1 soit fait sur le nombre CAPTURÉ, jamais sur une borne devinée.
    """
    hn = re.escape(hostname)
    gabarits = [
        r"(?P<pre>%s)_(?P<n>\d+)%s" % (hn, _FIN_NOMBRE),                    # <hn>_0, <hn>_0__p2
        r"(?P<pre>%s_audio)_(?P<n>\d+)%s" % (hn, _FIN_NOMBRE),              # <hn>_audio_0
        r"(?P<pre>%s_anc)_(?P<n>\d+)%s" % (hn, _FIN_NOMBRE),                # <hn>_anc_0
        r"(?P<pre>%s_txgen)_(?P<n>\d+)%s" % (hn, _FIN_NOMBRE),              # <hn>_txgen_0
        r"(?P<pre>%s_tx)(?P<n>\d+)(?=_ident|_static)" % hn,                 # <hn>_tx0_ident/_static
    ]
    # Les variantes préfixées (_audio/_anc/_txgen/_tx) DOIVENT passer avant le motif nu, sinon
    # `<hn>_audio_0` serait vu par le motif nu comme `<hn>` suivi de… rien (il ne matche pas),
    # mais l'ordre reste explicite pour ne pas dépendre de cette subtilité.
    ordre = gabarits[1:] + gabarits[:1]
    return [re.compile(g) for g in ordre]


def _decale_noms(txt, motifs):
    """Applique le +1 à tous les numéros de flux d'une chaîne. Sûr en une passe : `re.sub` ne
    ré-examine pas le texte qu'il vient d'écrire, donc pas de double décalage."""
    if not txt or not isinstance(txt, str):
        return txt, 0
    n_tot = 0
    for rx in motifs:
        def _r(m):
            nonlocal n_tot
            n_tot += 1
            return "%s_%d" % (m.group("pre"), int(m.group("n")) + 1) \
                if not m.group("pre").endswith("_tx") else "%s%d" % (m.group("pre"), int(m.group("n")) + 1)
        txt = rx.sub(_r, txt)
    return txt, n_tot


def _decale_dans(obj, motifs):
    """Parcours récursif d'une structure JSON : décale les noms dans toutes les CHAÎNES."""
    n = 0
    if isinstance(obj, str):
        v, k = _decale_noms(obj, motifs)
        return v, k
    if isinstance(obj, list):
        out = []
        for it in obj:
            v, k = _decale_dans(it, motifs); out.append(v); n += k
        return out, n
    if isinstance(obj, dict):
        out = {}
        for cle, val in obj.items():
            v, k = _decale_dans(val, motifs); out[cle] = v; n += k
        return out, n
    return obj, 0


_CLES_SLOT = (
    # Sorties TX du moteur 2110.
    (re.compile(r"^tx(\d+)_shm(_fmt)?$"), "tx%d_shm%s"),
    (re.compile(r"^tx_audio(\d+)_shm(_fmt)?$"), "tx_audio%d_shm%s"),
    (re.compile(r"^tx_anc(\d+)_shm(_fmt)?$"), "tx_anc%d_shm%s"),
)

# Les clés d'entrée ont trois formes (`input_{i}`, `input_v_{i}`, `input_a_{i}`), chacune avec un
# suffixe `_fmt` optionnel. Un seul motif générique les couvre : préfixe capturé tel quel, seul le
# NOMBRE est décalé.
_CLE_INPUT = re.compile(r"^(input(?:_[va])?|audio_shm)_(\d+)(_fmt)?$")


def _decale_cles(params, entrees_seulement=False):
    """Décale les CLÉS indexées de +1.

    Deux familles : les SORTIES TX du moteur (`tx{n}_shm` & co, moteur uniquement) et les
    ENTRÉES du câblage générique (`input_{n}`, `input_v_{n}`, `input_a_{n}`, + `_fmt`), qui
    concernent TOUS les types (mixer, pyramide, delay, sonde_latence…).

    ⚠ ORDRE DÉCROISSANT d'indice, sinon une clé décalée écrase une clé pas encore traitée —
    `input_0`→`input_1` avant d'avoir déplacé `input_1` perdrait purement et simplement une
    entrée câblée. (Piège vécu le 2026-08-13 sur les littéraux `_anc_0`/`_anc_1` de `player`.)
    """
    renommees = []
    familles = [] if entrees_seulement else list(_CLES_SLOT)
    for rx, gabarit in familles:
        cibles = []
        for cle in list(params.keys()):
            m = rx.match(cle)
            if m:
                cibles.append((int(m.group(1)), cle, m.group(2) or ""))
        for n, cle, suf in sorted(cibles, key=lambda t: -t[0]):
            neuve = gabarit % (n + 1, suf)
            params[neuve] = params.pop(cle)
            renommees.append((cle, neuve))
    # Entrées génériques (tous types).
    cibles = []
    for cle in list(params.keys()):
        m = _CLE_INPUT.match(cle)
        if m:
            cibles.append((int(m.group(2)), cle, m.group(1), m.group(3) or ""))
    for n, cle, pre, suf in sorted(cibles, key=lambda t: -t[0]):
        neuve = "%s_%d%s" % (pre, n + 1, suf)
        params[neuve] = params.pop(cle)
        renommees.append((cle, neuve))
    return renommees


def _decale_bind_slot(slot):
    """`tx0:v` → `tx1:v` ; `v:0` → `v:1`. Toute autre forme est laissée telle quelle."""
    m = re.match(r"^tx(\d+):(.+)$", slot or "")
    if m:
        return "tx%d:%s" % (int(m.group(1)) + 1, m.group(2))
    m = re.match(r"^([vad]):(\d+)$", slot or "")
    if m:
        return "%s:%d" % (m.group(1), int(m.group(2)) + 1)
    return slot


def migrer(conn, simulation=True):
    """Exécute (ou simule) la migration. Renvoie un rapport détaillé.

    `simulation=True` n'écrit RIEN et rend exactement ce qui serait fait — c'est le mode dans
    lequel la migration doit être relue avant d'être lancée pour de bon.
    """
    rap = {"simulation": simulation, "moteurs": [], "cles": 0, "noms": 0,
           "rdma": 0, "nmos_resources": 0, "abonnements": 0, "exemples": [], "deja_faite": False}

    cur = conn.execute("select value from settings where key = ?", (MARQUEUR,))
    if cur.fetchone():
        rap["deja_faite"] = True
        return rap

    # 1. Tous les producteurs dont le NOM DE FLUX porte un indice — leur hostname est la racine
    #    des noms à décaler. Élargi le 2026-08-13 au-delà du seul moteur 2110 : le 0 ne doit plus
    #    exister NULLE PART, et `delay`/`probe_2110`/`tone_gen`/`player` suffixaient aussi à 0
    #    (gabarits `{i}` dans `wiring.produces`, littéraux `_anc_0`/`_anc_1` pour `player`).
    #    ⚠ Cette liste DOIT rester le miroir des manifestes : un plugin qui gagne un `{i1}` dans
    #    un `shm` sans être ajouté ici verrait ses flux renommés par le moteur et pas en base.
    moteurs = []
    for vmid, hostname, dc in conn.execute(
            "select vmid, hostname, deploy_config from containers"):
        try:
            d = json.loads(dc or "{}")
        except (TypeError, ValueError):
            continue
        if d.get("type") in TYPES_INDEXES and hostname:
            moteurs.append((vmid, hostname, d.get("type")))
    rap["moteurs"] = moteurs
    if not moteurs:
        return rap
    motifs = []
    for _v, hn, _t in moteurs:
        motifs.extend(_motifs(hn))

    # 2. deploy_config de TOUS les conteneurs : valeurs (noms de flux) + clés (moteurs seulement).
    for vmid, hostname, dc, src, shm_out in conn.execute(
            "select vmid, hostname, deploy_config, source, shm_out from containers"):
        try:
            d = json.loads(dc or "{}")
        except (TypeError, ValueError):
            continue
        avant = json.dumps(d, sort_keys=True)
        d, n_noms = _decale_dans(d, motifs)
        # Clés : les sorties TX pour le moteur, les entrées de câblage pour TOUS les types.
        renommees = _decale_cles(d.setdefault("params", {}),
                                 entrees_seulement=(d.get("type") != "2110_io"))
        n_src, n_out = 0, 0
        src2, n_src = _decale_noms(src, motifs)
        out2, n_out = _decale_noms(shm_out, motifs)
        rap["noms"] += n_noms + n_src + n_out
        rap["cles"] += len(renommees)
        if renommees and len(rap["exemples"]) < 12:
            rap["exemples"].extend(["%s → %s (vmid %s)" % (a, b, vmid) for a, b in renommees[:6]])
        if not simulation and (json.dumps(d, sort_keys=True) != avant or n_src or n_out):
            conn.execute("update containers set deploy_config = ?, source = ?, shm_out = ? "
                         "where vmid = ?", (json.dumps(d), src2, out2, vmid))

    # 3. Liens RDMA — le nom du flux source. Les répliques sont à RECRÉER derrière (l'UUID MXL
    #    est dérivé du nom : l'ancienne réplique pointe sur un flux qui n'existe plus).
    for lid, flow in conn.execute("select id, src_flow from rdma_links"):
        neuf, k = _decale_noms(flow, motifs)
        if k:
            rap["rdma"] += 1
            if not simulation:
                conn.execute("update rdma_links set src_flow = ? where id = ?", (neuf, lid))

    # 4. Registre NMOS — `bind_slot`. SANS ce décalage, `_registry_id()` ne retrouve plus le slot
    #    et sème une ressource NEUVE à côté de l'ancienne, qui resterait annoncée : doublons.
    for rid, slot in conn.execute("select id, bind_slot from nmos_resources"):
        neuf = _decale_bind_slot(slot)
        if neuf != slot:
            rap["nmos_resources"] += 1
            if not simulation:
                conn.execute("update nmos_resources set bind_slot = ? where id = ?", (neuf, rid))

    # 5. Abonnements NMOS persistés — `recv_idx`.
    # ⚠ `settings.value` est DOUBLEMENT encodé pour cette clé : la colonne contient le json.dumps
    #   d'une CHAÎNE qui est elle-même du JSON. Un seul json.loads rend une `str`, pas un dict —
    #   et la version précédente de cette migration sautait alors les 16 abonnements SANS RIEN
    #   DIRE. On décode jusqu'au dict, et on RÉ-ENCODE dans la même forme (sinon le lecteur,
    #   qui refait deux décodages, casse).
    row = conn.execute("select value from settings where key = 'nmos_subscriptions'").fetchone()
    if row and row[0]:
        subs, profondeur = row[0], 0
        for _ in range(2):
            if isinstance(subs, dict):
                break
            try:
                subs = json.loads(subs); profondeur += 1
            except (TypeError, ValueError):
                subs = None
                break
        if isinstance(subs, dict):
            # ⚠ NE PAS TOUCHER `recv_idx` — c'est un INDICE DE TABLEAU, pas un numéro.
            # Il part tel quel dans `receiver_index` vers l'agent, qui s'en sert comme SUBSCRIPT
            # de slot. L'incrémenter réabonne le slot d'à côté — sans erreur, sans alerte : les
            # sources apparaissent simplement une entrée plus loin.
            # C'est ce qu'a fait cette migration le 2026-08-13 : les six sources sont passées des
            # slots 0-5 aux slots 1-6, toute la chaîne est tombée, et la cause a d'abord été
            # attribuée à la recréation du moteur. Corrigé le 2026-08-15.
            # La règle (app/numerotation.py) ne vise QUE ce qui sort en CHAÎNE — nom de flux, clé,
            # libellé. Un indice qui sert à adresser un tableau n'en fait pas partie, où qu'il
            # soit stocké.
            pass
            if not simulation and rap["abonnements"]:
                val = json.dumps(subs)
                for _ in range(profondeur - 1):      # ré-encapsule autant de fois qu'on a décodé
                    val = json.dumps(val)
                conn.execute("update settings set value = ? where key = 'nmos_subscriptions'",
                             (val,))

    if not simulation:
        conn.execute("insert or replace into settings (key, value) values (?, ?)",
                     (MARQUEUR, json.dumps({"fait": True})))
        conn.commit()
        # ── RELECTURE APRÈS COMMIT ────────────────────────────────────────────────────────────
        # Les compteurs ci-dessus comptent les lignes CANDIDATES, pas les écritures effectives :
        # ils ont affiché « 210 ressources NMOS » le 2026-08-13 alors que zéro n'avait survécu
        # (un autre processus les réécrivait derrière). Un rapport qui ne peut pas mentir doit
        # RELIRE. Si quelque chose est resté 0-based, on lève : mieux vaut un échec bruyant
        # qu'un parc à moitié migré dont personne ne sait qu'il l'est.
        restes = []
        n = conn.execute("select count(*) from nmos_resources "
                         "where bind_slot like 'tx0:%' or bind_slot like '%:0'").fetchone()[0]
        if n:
            restes.append("nmos_resources.bind_slot : %d ligne(s) encore en 0" % n)
        for vmid, dc in conn.execute("select vmid, deploy_config from containers"):
            try:
                params = (json.loads(dc or "{}").get("params") or {})
            except (TypeError, ValueError):
                continue
            zero = [k for k in params
                    if _CLE_INPUT.match(k) and _CLE_INPUT.match(k).group(2) == "0"]
            zero += [k for k in params if any(rx.match(k) and rx.match(k).group(1) == "0"
                                              for rx, _g in _CLES_SLOT)]
            if zero:
                restes.append("container %s : clés %s" % (vmid, sorted(zero)[:4]))
        if restes:
            raise RuntimeError("migration numérotation INCOMPLÈTE après commit — "
                               "l'orchestrateur tournait-il ? : " + " | ".join(restes))
        rap["relu"] = True
    return rap
