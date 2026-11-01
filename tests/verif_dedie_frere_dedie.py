#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Un cœur « dédié » dont le frère HT est tenu par un AUTRE dédié n'est pas dédié.

★ LA PANNE (Z8, 2026-10-30, antenne perdue). Topologie mesurée : le cœur N est frère de N+26
(52 logiques). decklink-io #231 avait 5 cœurs « dédiés » 27-31 = les frères de 1-5, tenus par deux
pyramides DÉDIÉES (#232 cpuset 1-4, 77 % ; #230 cpuset 5-8, 78 %). #231 demande 265 % et n'a
jamais eu 5 cœurs → images répétées/noires en SDI. Aucun compteur ne le disait : `vue_coeurs` ne
cherchait les frères que dans le pool PARTAGÉ, donc `exclusif` valait True et la vue mentait.

Ce que cette garde tient, sur les VRAIES fonctions :
1. la vue nomme les frères tenus par un autre dédié (`siblings_dedicated`) et `exclusif` = False ;
   un dédié qui tient les deux threads de ses cœurs reste exclusif ; carte HT inconnue → None ;
2. l'alerte couvre ce cas, en nommant le voisin et son cpuset (clé dédiée, l'ancienne intacte) ;
3. le verdict exécutable (`dedicated_exclusivity`) et sa route GET (login exigé) ;
4. ★★ RÈGLE STRICTE d'allocation (décision de Cyril, 30/10) : avec 1-4 et 5-8 dédiés, une
   demande de 5 cœurs n'obtient JAMAIS 27-34 ; faute de cœurs physiques entiers → repli PARTAGÉ
   et alerte qui chiffre le manque et nomme les tenants ; une allocation existante qui viole la
   règle est replacée, ou rendue (repli partagé) ;
5. le dimensionnement dynamique compte la capacité STRICTE (`free_dedicated_capacity`).

Hors ligne : base jetable, carte HT/NUMA et relevé de placement doublés — rien ne sort.

MUTATIONS (preuve que la garde mord), DEDIE_FRERE_MUTATION=<nom> — chacune doit donner rc=1 :
    vue_partage_seul     `exclusif` ignore les frères tenus par un autre dédié (le défaut d'origine)
    soi_meme_voisin      un dédié se compte comme son propre voisin
    alerte_cas2_retiree  l'alerte « frère tenu par un dédié » n'est plus émise
    alerte_sans_voisin   l'alerte ne nomme plus le voisin ni son cpuset
    verdict_toujours_ok  `dedicated_exclusivity` répond ok=True quoi qu'il arrive
    stricte_retiree      `allocate_cores` ne filtre plus les cœurs dont le frère est pris
    repli_retire         faute de cœurs conformes, `allocate_cores` alloue quand même
    violation_reconduite une allocation existante qui viole la règle est reconduite telle quelle
    rendue_au_lieu_de_replacer  une violation est rendue même quand un placement conforme existe
    capacite_logique     `free_dedicated_capacity` compte les cœurs logiques libres

    ./venv/bin/python tests/verif_dedie_frere_dedie.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

# Base jetable AVANT tout accès (get_db() garde une connexion par thread).
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_dedie_frere_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool, node_driver, docker_compute, placement             # noqa: E402

# ── Mutations : le code RÉEL est réécrit puis réexécuté dans son propre module ────────────────
MUT = os.environ.get("DEDIE_FRERE_MUTATION") or ""
MUTATIONS = {
    "vue_partage_seul": ('else not freres and not ligne["siblings_dedicated"])', "else not freres)"),
    "soi_meme_voisin": ("if v != vmid and v in vivants and x in core_of",
                        "if v in vivants and x in core_of"),
    "alerte_cas2_retiree": ("    if paires:\n        detail", "    if False:\n        detail"),
    "alerte_sans_voisin": ('"noms": _noms_vmids([l["vmid"] for l in paires]), "pairs": detail})',
                           '"noms": _noms_vmids([l["vmid"] for l in paires]), "pairs": "—"})'),
    "verdict_toujours_ok": ('ok = None if not ht else all(l["exclusive"] is True for l in lignes)',
                            "ok = None if not ht else True"),
    "stricte_retiree": ("                conformes = sorted(_phys_libres(free))",
                        "                conformes = sorted(free)"),
    "repli_retire": ("                if len(conformes) < n:\n                    bloques",
                     "                if False:\n                    bloques"),
    "violation_reconduite": ("                if partage_ht:\n                    voisins",
                             "                if False:\n                    voisins"),
    "rendue_au_lieu_de_replacer": ("                    if len(cand) >= n:\n                        sans, motif",
                                   "                    if False:\n                        sans, motif"),
    "capacite_logique": ("    return len({c for c in dispo if core_of.get(c) not in pris})",
                         "    return len(dispo)"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _old, _new = MUTATIONS[MUT]
    with open(core_pool.__file__, encoding="utf-8") as _f:
        _src = _f.read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) — la garde est périmée"
              % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), core_pool.__file__, "exec"), core_pool.__dict__)
    print("MUTATION active :", MUT)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Doublures d'E/S seulement ───────────────────────────────────────────────────────────────────
node_driver.host_exec = lambda node, cmd, timeout=None, **kw: (0, "", "")
docker_compute.ssh_run = lambda host, cmd, timeout=None, **kw: (0, "", "")
# Topologie MESURÉE de la Z8 : 52 cœurs logiques, N ↔ N+26 ; un seul socket.
Z8_HT = {c: c % 26 for c in range(52)}
_carte = {"ht": dict(Z8_HT)}
core_pool.core_map_cached = lambda nid: dict(_carte["ht"])
core_pool.numa_map_cached = lambda nid: {c: 0 for c in range(52)}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
placement.releve_cache = lambda node_id, ttl=None, force=False: None

NID = database.db_add_node("z8", "127.0.0.1")
database.db_update_node(NID, compute_cpuset="1-51", agent_url="http://127.0.0.1:9100")


def _ajoute(vmid, nom, typ):
    database.db_upsert_container_docker(vmid, nom, NID, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, typ, {})


def _reserve(vmid, cpuset):
    with database.get_db() as db:
        db.execute("DELETE FROM node_core_alloc WHERE vmid=?", (vmid,))
        db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                       [(NID, c, vmid) for c in core_pool.parse_cpuset(cpuset)])
        db.commit()


def _cles(depuis=0):
    with database.get_db() as db:
        return [(r["id"], r["message"]) for r in
                db.execute("SELECT id, message FROM alerts WHERE id>? ORDER BY id", (depuis,))]


def _diagnostic(depuis):
    """Ce qu'on a RÉELLEMENT en base quand 2b tombe — sinon l'échec ne dit rien.

    ⚠ `_msgs` est une LISTE VIDE quand rien n'a été écrit, et une liste vide ne s'imprime pas :
    le 2026-11-01, l'échec de l'exécuteur GitHub n'a affiché aucun détail trois runs de suite.
    On sait seulement que la fonction rend True (2a) et qu'aucune ligne n'arrive (2b) — il faut
    donc regarder les DEUX muselières connues (throttle de processus, anti-rebond en base) ET
    l'état brut de la table, pour pouvoir trancher sans un quatrième aller-retour.
    """
    print("\n   ┌─ DIAGNOSTIC (2b n'a rien trouvé) " + "─" * 40)
    try:
        with database.get_db() as db:
            tot = db.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
            lignes = db.execute(
                "SELECT id, niveau, kind, message, msg_key FROM alerts "
                "WHERE id>? ORDER BY id", (depuis,)).fetchall()
            eps = db.execute("SELECT signature, kind, niveau FROM alert_episodes").fetchall()
        print("   │ alerts : %d au total, %d depuis le repère (id>%s)" % (tot, len(lignes), depuis))
        for r in lignes[-8:]:
            print("   │   #%s [%s/%s] key=%r msg=%r"
                  % (r["id"], r["niveau"], r["kind"], r["msg_key"], (r["message"] or "")[:120]))
        print("   │ alert_episodes : %d" % len(eps))
        for r in eps[:8]:
            print("   │   %r [%s/%s]" % ((r["signature"] or "")[:90], r["kind"], r["niveau"]))
    except Exception as e:
        print("   │ lecture impossible : %r" % (e,))
    try:
        print("   │ core_pool._alerte_last : %r" % (dict(core_pool._alerte_last),))
    except Exception as e:
        print("   │ _alerte_last illisible : %r" % (e,))
    print("   └" + "─" * 72)


def _dernier_id():
    with database.get_db() as db:
        r = db.execute("SELECT MAX(id) AS m FROM alerts").fetchone()
    return r["m"] or 0


_ajoute(230, "pyramide-b", "pyramide")
_ajoute(231, "decklink-io", "decklink_io")
_ajoute(232, "pyramide-a", "pyramide")
_reserve(232, "1-4")
_reserve(230, "5-8")
_reserve(231, "27-31")

print("\n1. ★★ La vue : le cas Z8 réel")
vue = core_pool.vue_coeurs(NID)
L = {l["vmid"]: l for l in vue["dedies"]}
l231 = L.get(231, {})
verifie(vue["ht_connu"] is True, "1a. topologie HT connue")
verifie(l231.get("freres_partages") == "",
        "1b. décor : AUCUN frère de 27-31 n'est dans le pool partagé — c'est pour ça que l'ancienne "
        "vue concluait « exclusif »", l231.get("freres_partages"))
verifie(l231.get("exclusif") is False,
        "1c. ★★ #231 est NON EXCLUSIF : ses frères 1-5 sont tenus par deux pyramides dédiées", l231)
_sd = {s["vmid"]: s for s in l231.get("siblings_dedicated") or []}
verifie(set(_sd) == {230, 232}
        and _sd[232]["siblings"] == "1-4" and _sd[232]["cpuset"] == "1-4"
        and _sd[230]["siblings"] == "5" and _sd[230]["cpuset"] == "5-8"
        and _sd[232]["hostname"] == "pyramide-a",
        "1d. ★ les voisins sont NOMMÉS : pyramide-a (232) tient 1-4 de 1-4, pyramide-b (230) tient "
        "5 de 5-8", l231.get("siblings_dedicated"))
verifie(L.get(232, {}).get("exclusif") is False
        and [s["vmid"] for s in L[232]["siblings_dedicated"]] == [231]
        and L[232]["siblings_dedicated"][0]["siblings"] == "27-30",
        "1e. et c'est symétrique : #232 partage 1-4 avec #231 (27-30)", L.get(232))
verifie(L.get(230, {}).get("exclusif") is False and L[230]["freres_partages"] == "32-34"
        and [s["vmid"] for s in L[230]["siblings_dedicated"]] == [231],
        "1f. #230 cumule les deux cas : 32-34 dans le partagé, 31 chez #231", L.get(230))
verifie("freres_partages" in l231, "1g. `freres_partages` conservé (compatibilité)")

# Contrôle : un dédié qui tient les DEUX threads de ses cœurs physiques est exclusif.
_ajoute(250, "multiview", "multiview")
_reserve(250, "20-21,46-47")
_l250 = {l["vmid"]: l for l in core_pool.vue_coeurs(NID)["dedies"]}.get(250, {})
verifie(_l250.get("exclusif") is True and _l250.get("siblings_dedicated") == [],
        "1h. contrôle : un dédié qui tient les deux threads de ses cœurs reste EXCLUSIF "
        "(ses propres frères ne sont pas des voisins)", _l250)

# Orphelin : une réservation sans conteneur ne calcule pas → pas un voisin.
_reserve(299, "10")
_reserve(250, "36")          # frère de 10
_l250 = {l["vmid"]: l for l in core_pool.vue_coeurs(NID)["dedies"]}.get(250, {})
verifie(_l250.get("siblings_dedicated") == [],
        "1i. une réservation ORPHELINE n'est pas comptée comme voisine (la vue la nomme déjà)",
        _l250)
core_pool.release_cores(299)
core_pool.release_cores(250)
database.db_delete_container(250)

_carte["ht"] = {}
_lx = {l["vmid"]: l for l in core_pool.vue_coeurs(NID)["dedies"]}.get(231, {})
verifie(_lx.get("exclusif") is None and _lx.get("siblings_dedicated") == [],
        "1j. carte HT inconnue → `exclusif` None, aucun voisin deviné", _lx)
_v = core_pool.dedicated_exclusivity(NID)
verifie(_v["ht_known"] is False and _v["ok"] is None,
        "1k. et le verdict exécutable répond « je ne sais pas » (ok None)", _v)
_carte["ht"] = dict(Z8_HT)

print("\n2. ★ L'alerte nomme le voisin et son cpuset")
# ⚠ DEUX ÉTAGES DE MUSELIÈRE, PAS UN. `_alerte_last` est le throttle de PROCESSUS ; la table
# `alert_episodes` est l'anti-rebond EN BASE, indexé par signature, et `db_add_alert` n'écrit
# RIEN quand il retrouve l'épisode. Ne vider que le premier laisse le second armé : la fonction
# rend True (2a passe) et aucune ligne n'atteint la table (2b à 2e tombent), sans un mot.
# Constaté le 2026-11-01 sur l'exécuteur GitHub, où quelque chose émet avant ce point — vert ici
# et sur l'arbre dérivé, rouge là-bas seulement. Reproduit en émettant une fois avant le repère.
core_pool._alerte_last.clear()
database.get_db().execute("DELETE FROM alert_episodes")
database.get_db().commit()
_avant = _dernier_id()
verifie(core_pool.alerter_dedies_non_exclusifs(NID) is True, "2a. une alerte est levée")
_msgs = [m for _, m in _cles(_avant)]
_cas2 = [m for m in _msgs if "AUTRE réservation dédiée" in m]
verifie(len(_cas2) == 1, "2b. ★ l'alerte « frère tenu par un AUTRE dédié » est émise", _msgs)
if len(_cas2) != 1:
    _diagnostic(_avant)
_m = _cas2[0] if _cas2 else ""
verifie("decklink-io (231)" in _m and "pyramide-a (232) cpuset 1-4" in _m
        and "pyramide-b (230) cpuset 5-8" in _m,
        "2c. ★ elle nomme la victime, les voisins ET leur cpuset", _m)
verifie(any("pool partagé" in m and "32-34" in m for m in _msgs),
        "2d. l'ancienne alerte (frère dans le partagé) reste émise pour #230, inchangée", _msgs)
with database.get_db() as db:
    _kinds = {r["kind"] for r in db.execute("SELECT kind FROM alerts WHERE id>?", (_avant,))}
verifie(_kinds == {"resource"} and "resource" in database.ALERT_KINDS,
        "2e. kind d'alerte `resource`, valide (ALERT_KINDS)", _kinds)

print("\n3. Verdict exécutable + route GET")
_v = core_pool.dedicated_exclusivity(NID, vmid=231)
verifie(_v["ok"] is False and len(_v["dedicated"]) == 1
        and _v["dedicated"][0]["exclusive"] is False
        and [s["vmid"] for s in _v["dedicated"][0]["dedicated_siblings"]] == [230, 232],
        "3a. ★ dedicated_exclusivity(z8, 231) : NON exclusif, partage avec 230 et 232", _v)
_vall = core_pool.dedicated_exclusivity(NID)
verifie(_vall["ok"] is False and {d["vmid"] for d in _vall["dedicated"]} == {230, 231, 232},
        "3b. sans vmid : tous les dédiés du nœud", _vall)

import main as app_main                                                        # noqa: E402
from app.auth import hash_password                                             # noqa: E402

app = app_main.app
app.config["TESTING"] = True
database.db_create_user("lecteur", hash_password("Banc-Motdepasse-2026!"), "viewer")
anon = app.test_client()
r = anon.get("/api/nodes/%d/cores/exclusivity?vmid=231" % NID)
verifie(r.status_code in (302, 401, 403), "3c. la route exige un login", r.status_code)
cli = app.test_client()
with database.get_db() as db:
    _u = db.execute("SELECT id, username FROM users WHERE username='lecteur'").fetchone()
with cli.session_transaction() as s:
    s["user_id"] = _u["id"]
    s["username"] = _u["username"]
r = cli.get("/api/nodes/%d/cores/exclusivity?vmid=231" % NID)
j = r.get_json(silent=True) or {}
verifie(r.status_code == 200 and j.get("ok") is False
        and j.get("dedicated", [{}])[0].get("vmid") == 231
        and [s["vmid"] for s in j["dedicated"][0]["dedicated_siblings"]] == [230, 232],
        "3d. ★ GET /api/nodes/<id>/cores/exclusivity?vmid=231 (login simple) → ok:false, voisins "
        "230 et 232", (r.status_code, j))
r = cli.get("/api/nodes/999999/cores/exclusivity")
verifie(r.status_code == 404, "3e. nœud inconnu → 404", r.status_code)

print("\n4. ★★ Règle STRICTE d'allocation")
core_pool.release_cores(231)
# Nœud DENSE (l'état de la Z8 qui a produit 27-31) : 9-25 tenus par un autre dédié. Les seuls
# cœurs libres sont 26-51 ; tous sauf 26 sont frères de cœurs dédiés.
_ajoute(240, "multiview-z", "multiview")
_reserve(240, "9-25")
_ajoute(241, "decklink-io-2", "decklink_io")
verifie(core_pool.free_dedicated_capacity(NID, 241) == 1,
        "4a. capacité STRICTE : 1 seul cœur conforme (26), pas 26 logiques",
        core_pool.free_dedicated_capacity(NID, 241))
_avant = _dernier_id()
_got = core_pool.allocate_cores(NID, 241, 5)
_set = core_pool.parse_cpuset(_got or "")
verifie(not (_set & set(range(27, 35))),
        "4b. ★★ avec 1-4 et 5-8 dédiés, une demande de 5 cœurs n'obtient JAMAIS 27-34", _got)
verifie(_got is None and core_pool.allocated_for(NID, 241) == 0,
        "4c. ★ faute de cœurs physiques entiers : AUCUNE réservation (→ repli partagé)", _got)
_msgs = [m for _, m in _cles(_avant)]
_repli = [m for m in _msgs if "il en manque 4" in m]
verifie(len(_repli) == 1 and "pyramide-a (232)" in _repli[0] and "pyramide-b (230)" in _repli[0]
        and "multiview-z (240)" in _repli[0],
        "4d. ★ l'alerte chiffre le manque (4) et NOMME les tenants des frères", _msgs)
_cs, _ded = core_pool.effective_cpuset(NID, 241, 5)
verifie(_ded is False and _cs and not (core_pool.parse_cpuset(_cs) & set(range(1, 26))),
        "4e. effective_cpuset retombe sur le pool PARTAGÉ (hors cœurs dédiés)", (_cs, _ded))

# Une place se libère : 9-20 seulement → 21-26 sont des cœurs physiques entiers.
_reserve(240, "9-20")
verifie(core_pool.free_dedicated_capacity(NID, 241) == 11,
        "4f. capacité stricte : 21-26 et 47-51 (11 cœurs ; 35-46 sont frères de 9-20)", core_pool.free_dedicated_capacity(NID, 241))
_got = core_pool.allocate_cores(NID, 241, 5)
_set = core_pool.parse_cpuset(_got or "")
_pris_autres = {Z8_HT[c] for v, cs in core_pool.allocations_by_vmid(NID).items()
                if v != 241 for c in cs}
verifie(len(_set) == 5 and not ({Z8_HT[c] for c in _set} & _pris_autres),
        "4g. ★ placement conforme : aucun frère tenu par un autre dédié", _got)
core_pool.release_cores(241)

print("\n5. ★ Allocation existante qui VIOLE la règle (posée avant elle)")
_reserve(231, "27-31")
_avant = _dernier_id()
_got = core_pool.allocate_cores(NID, 231, 5)
_set = core_pool.parse_cpuset(_got or "")
verifie(_got and not (_set & set(range(27, 35)))
        and not ({Z8_HT[c] for c in _set} & {Z8_HT[c] for c in range(1, 21)}),
        "5a. ★ au (re)déploiement, #231 est REPLACÉ hors des frères des pyramides", _got)
verifie(any("27-31" in m and "replacée" in m for _, m in _cles(_avant)),
        "5b. et c'est dit (avant/après, voisins)", _cles(_avant))
verifie(core_pool.allocate_cores(NID, 231, 5) == _got,
        "5c. idempotent ensuite : plus de violation → statu quo")
core_pool.release_cores(231)
_reserve(240, "9-25")
_reserve(231, "27-31")
_avant = _dernier_id()
_got = core_pool.allocate_cores(NID, 231, 5)
verifie(_got is None and core_pool.allocated_for(NID, 231) == 0,
        "5d. ★ aucun placement conforme : la réservation est RENDUE → repli partagé", _got)
verifie(any("Réservation rendue" in m and "pyramide-a (232)" in m for _, m in _cles(_avant)),
        "5e. avec l'alerte qui nomme les voisins", _cles(_avant))

print()
if echecs:
    print("ÉCHEC : %d contrôle(s)" % len(echecs))
    for e in echecs:
        print("  -", e)
    sys.exit(1)
print("OK — tous les contrôles passent")
