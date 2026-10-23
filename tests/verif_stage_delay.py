#!/usr/bin/env python3
"""Vérifie `bobimxl.StageDelay` — le DÉLAI D'ÉTAGE en trames, mesure directe d'un plugin.

Tourne SANS libmxl (absente du contrôleur) : les Reader/Writer sont des doubles qui n'exposent
que `index_time_ns`, ce dont la classe a besoin. On contrôle surtout les REFUS, parce que c'est
là que se joue l'honnêteté de la métrique : bases d'index incompatibles, writer en compteur
libre, flux illisible — dans tous ces cas il ne faut RIEN publier, jamais un zéro.

    ./venv/bin/python tools/verif_stage_delay.py
"""
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_R, "script_templates"))
from bobimxl import StageDelay                    # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


class Flux:
    """Double : index → instant TAI à `fps`, décalé de `base_ns` (pour simuler une autre base)."""
    def __init__(self, fps=50, base_ns=0, illisible=False, index_mode="tai"):
        self.fps, self.base_ns, self.illisible = fps, base_ns, illisible
        self.index_mode = index_mode

    def index_time_ns(self, index):
        if self.illisible:
            return None
        return self.base_ns + int(index) * 1_000_000_000 // self.fps


def run():
    w = Flux(50)

    # ── Cas nominal : sortie N, entrée N−2, à 50 fps → 2 trames ─────────────────────────────
    sd = StageDelay()
    for n in range(100, 110):
        sd.observe(w, n, [(Flux(50), n - 2)])
    p = sd.publish()
    ok(p and abs(p["vieux_moy"] - 2.0) < 0.01, "entrée N−2 → 2,00 trames", "publish=%s" % p)
    ok(p and p.get("non_mesurable") == 0, "cas nominal → compteur de refus à zéro, et PUBLIÉ",
       "publish=%s" % p)
    ok(sd.non_mesurable == 0, "aucun refus sur le cas nominal", "non_mesurable=%d" % sd.non_mesurable)

    # ── Multi-entrées : deux BORNES distinctes, pas une moyenne ─────────────────────────────
    sd = StageDelay()
    r = Flux(50)
    for n in range(100, 110):
        sd.observe(w, n, [(r, n - 1), (r, n - 4)])
    p = sd.publish()
    ok(p and abs(p["recent_moy"] - 1.0) < 0.01, "plancher = entrée la plus RÉCENTE (1,00)", "publish=%s" % p)
    ok(p and abs(p["vieux_moy"] - 4.0) < 0.01, "délai subi = entrée la plus VIEILLE (4,00)", "publish=%s" % p)

    # ── Cadences DIFFÉRENTES : le passage par le temps doit tenir ───────────────────────────
    # Entrée à 25 fps index 50 → t=2,000 s. Sortie à 50 fps index 105 → t=2,100 s. Écart 100 ms
    # = 5 trames de sortie. Une soustraction d'index brute aurait donné 55.
    sd = StageDelay()
    sd.observe(w, 105, [(Flux(25), 50)])
    p = sd.publish()
    ok(p and abs(p["vieux_moy"] - 5.0) < 0.01,
       "cadences 25→50 : 5,00 trames (et non 55 par soustraction d'index)", "publish=%s" % p)

    # ── REFUS 1 : entrée illisible (flux absent du domaine) ─────────────────────────────────
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50, illisible=True), n - 2)])
    p_ = sd.publish()
    ok(p_ is not None and "vieux_moy" not in p_ and p_.get("non_mesurable") == 5,
       "entrée illisible → aucune mesure, mais le refus est COMPTÉ et publié", "publish=%s" % p_)
    ok(sd.non_mesurable == 5, "entrée illisible → comptée non mesurable", "non_mesurable=%d" % sd.non_mesurable)

    # ── REFUS 2 : writer en compteur LIBRE — son index n'est pas une coordonnée temporelle ───
    sd = StageDelay()
    wl = Flux(50, index_mode="free")
    for n in range(100, 105):
        sd.observe(wl, n, [(Flux(50), n - 2)])
    p_ = sd.publish()
    ok(p_ is not None and "vieux_moy" not in p_ and p_.get("non_mesurable") == 5,
       "writer en index libre → aucune mesure, refus publié", "publish=%s" % p_)
    ok(sd.non_mesurable == 5, "writer en index libre → compté non mesurable",
       "non_mesurable=%d" % sd.non_mesurable)

    # ── REFUS 3 : bases d'index incompatibles → valeur aberrante, donc pas de publication ────
    # C'est le cas que le passage par le temps ne suffit PAS à couvrir : la conversion réussit
    # des deux côtés, mais les deux horloges ne parlent pas de la même chose.
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50, base_ns=-3_600_000_000_000), n - 2)])   # 1 h d'écart
    p_ = sd.publish()
    ok(p_ is not None and "vieux_moy" not in p_ and p_.get("non_mesurable") == 5,
       "bases d'index incompatibles → aucune mesure, refus publié", "publish=%s" % p_)
    ok(sd.non_mesurable == 5, "bases incompatibles → comptées non mesurables",
       "non_mesurable=%d" % sd.non_mesurable)

    # ── REFUS 4 : sortie AVANT l'entrée (négatif franc) ──────────────────────────────────────
    sd = StageDelay()
    sd.observe(w, 100, [(Flux(50), 110)])
    p_ = sd.publish()
    ok(p_ is not None and "vieux_moy" not in p_, "sortie antérieure à l'entrée → aucune mesure",
       "publish=%s" % p_)

    # ── Un étage à délai NUL reste mesuré (0 légitime ≠ absence de mesure) ───────────────────
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50), n)])
    p = sd.publish()
    ok(p is not None and abs(p["vieux_moy"]) < 0.01,
       "délai réellement nul → 0,00 PUBLIÉ (≠ absence de mesure)", "publish=%s" % p)

    # ── Péremption : une mesure vieille ne doit plus être servie ────────────────────────────
    sd = StageDelay(peremption_ns=0)
    sd.observe(w, 100, [(Flux(50), 98)])
    p_ = sd.publish()
    ok(p_ is None or "vieux_moy" not in p_, "mesure périmée → plus de valeur servie",
       "publish=%s" % p_)

    # ── Robustesse : un défaut de mesure ne doit pas tuer la boucle de production ────────────
    class Cassé:
        index_mode = "tai"

        def index_time_ns(self, i):
            raise RuntimeError("boum")
    sd = StageDelay()
    res = sd.observe(Cassé(), 100, [(Flux(50), 98)])
    p_ = sd.publish()
    ok(res is False and (p_ is None or "vieux_moy" not in p_),
       "writer qui lève → False, pas d'exception propagée", "publish=%s" % p_)

    # ── AUCUNE entrée liée : c'est un refus, et il doit se compter ───────────────────────────
    sd = StageDelay()
    for n in range(100, 104):
        sd.observe(w, n, [])
    p_ = sd.publish()
    ok(p_ is not None and "vieux_moy" not in p_ and p_.get("non_mesurable") == 4,
       "aucune entrée liée → refus COMPTÉ (le cas mixer-test « sans entrée vivante »)",
       "publish=%s" % p_)

    # ── Index PROPAGÉ : zéro STRUCTUREL, à ne pas confondre avec un zéro mesuré ─────────────
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50), n)], propage=True)
    p_ = sd.publish()
    ok(p_ is not None and abs(p_["vieux_moy"]) < 0.01 and p_.get("propage") is True,
       "index propagé → 0,00 publié AVEC le drapeau `propage`", "publish=%s" % p_)
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50), n - 2)])
    p_ = sd.publish()
    ok(p_ is not None and p_.get("propage") is False,
       "étage qui re-cadence → `propage` faux (les deux zéros restent distinguables)",
       "publish=%s" % p_)


    # ── AUDIO : l'unité naturelle est la MILLISECONDE, pas la trame ─────────────────────────
    # Un StageDelay audio travaille sur des index d'ÉCHANTILLON (48 kHz). Publier « 960 trames »
    # là où il faut lire « 20 ms » serait une erreur de catégorie — et c'est en ms que l'écart
    # A/V se compare, puisque c'est la seule unité commune aux deux essences.
    wa = Flux(48000)
    sd = StageDelay(essence="audio")
    for n in range(48000, 48000 + 5):
        sd.observe(wa, n, [(Flux(48000), n - 960)])          # 960 échantillons = 20 ms
    p_ = sd.publish()
    ok(p_ is not None and abs(p_["ms_moy"] - 20.0) < 0.01,
       "audio : 960 échantillons → 20,000 ms", "publish=%s" % p_)
    ok(p_ is not None and "vieux_moy" not in p_,
       "audio : PAS de clé `trames` (erreur de catégorie évitée)", "publish=%s" % p_)
    ok(p_ is not None and p_.get("essence") == "audio", "audio : essence annoncée")

    # La vidéo garde ses trames ET gagne les ms — c'est par elles que l'écart A/V se calcule.
    sd = StageDelay()
    for n in range(100, 105):
        sd.observe(w, n, [(Flux(50), n - 2)])
    p_ = sd.publish()
    ok(p_ is not None and abs(p_["vieux_moy"] - 2.0) < 0.01 and abs(p_["ms_moy"] - 40.0) < 0.01,
       "vidéo : 2,00 trames ET 40,000 ms (les deux unités)", "publish=%s" % p_)

    # ── Borne haute EN TEMPS : l'inter-site doit passer, le désaccord de base doit tomber ────
    sd = StageDelay()
    sd.observe(w, 100 + 150, [(Flux(50), 100)])              # 150 trames = 3 s, liaison longue
    p_ = sd.publish()
    ok(p_ is not None and abs(p_["ms_moy"] - 3000.0) < 1.0,
       "liaison longue 3 s ACCEPTÉE (l'ancienne borne de 120 trames l'aurait rejetée)",
       "publish=%s" % p_)
    sd = StageDelay()
    sd.observe(w, 100, [(Flux(50, base_ns=-3_600_000_000_000), 98)])
    p_ = sd.publish()
    ok(p_ is None or "ms_moy" not in p_, "désaccord de base (1 h) toujours REFUSÉ",
       "publish=%s" % p_)

    print("\n%s" % ("Contrat respecté." if not ko else "%d contrôle(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(run())
