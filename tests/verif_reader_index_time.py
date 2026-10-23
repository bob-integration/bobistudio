#!/usr/bin/env python3
"""Vérifie `bobimxl.Reader.index_time_ns` — la conversion index → instant TAI, côté LECTURE.

Pourquoi elle existe : mesurer le délai d'un étage par `index_sortie − index_entrée` n'est licite
que si les deux flux partagent cadence ET base d'index. Un writer en `index_mode="free"` porte un
compteur libre, et plusieurs plugins ouvrent leurs grains en « index tai (genlock) OU compteur
libre » selon le câblage. En passant par le TEMPS, la mesure reste juste dans tous les cas.

Ce banc tourne SANS libmxl (absente du contrôleur) : le Reader est construit sans `__init__` et
`_lib` est remplacée par un double. On contrôle donc le CONTRAT — surtout le repli `None`, qui doit
signaler l'absence de mesure et jamais rendre 0 (un 0 se lirait comme un instant valide).

    ./venv/bin/python tools/verif_reader_index_time.py
"""
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_R, "script_templates"))
import bobimxl                                    # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


class _FauxLib:
    """Double de libmxl : rend un instant DÉTERMINISTE dérivé de la cadence et de l'index."""
    def __init__(self):
        self.appels = []

    def mxlIndexToTimestamp(self, rate_ref, index):
        r = rate_ref._obj
        self.appels.append((int(r.numerator), int(r.denominator), int(index)))
        return int(index) * 1_000_000_000 * int(r.denominator) // int(r.numerator)


def _reader(fmt):
    """Reader SANS __init__ (qui exigerait le domaine MXL), avec un `format()` imposé."""
    r = object.__new__(bobimxl.Reader)
    r._rate = None
    r._appels_format = [0]

    def _f():
        r._appels_format[0] += 1
        return fmt
    r.format = _f
    return r


def run():
    faux = _FauxLib()
    vrai = bobimxl._lib
    bobimxl._lib = faux
    try:
        # ── Flux absent du domaine : ABSENCE DE MESURE, pas un instant ───────────────────────
        r = _reader(None)
        v = r.index_time_ns(10)
        ok(v is None, "flux illisible → None (et surtout pas 0)", "renvoyé %r" % v)
        ok(not faux.appels, "flux illisible → libmxl n'est pas appelée", "appels=%s" % faux.appels)

        # ── Descripteur incomplet : même exigence ───────────────────────────────────────────
        for mauvais in ({}, {"fps_num": None, "fps_den": 1}, {"fps_num": "x", "fps_den": 1}):
            r = _reader(mauvais)
            v = r.index_time_ns(10)
            ok(v is None, "descripteur %r → None" % (mauvais,), "renvoyé %r" % v)

        # ── Cadence lisible : conversion, et la CADENCE DU FLUX est bien celle transmise ─────
        faux.appels.clear()
        r = _reader({"fps_num": 50, "fps_den": 1})
        v = r.index_time_ns(100)
        ok(v == 2_000_000_000, "50 fps, index 100 → 2,000 s", "renvoyé %r" % v)
        ok(faux.appels and faux.appels[-1][:2] == (50, 1),
           "la cadence du FLUX est transmise à libmxl", "appels=%s" % faux.appels)

        # ── Cadence non entière : c'est là qu'une cadence codée en dur se ferait prendre ─────
        faux.appels.clear()
        r = _reader({"fps_num": 60000, "fps_den": 1001})
        v = r.index_time_ns(60)
        att = 60 * 1_000_000_000 * 1001 // 60000
        ok(v == att, "59,94 fps, index 60 → %.6f s" % (att / 1e9), "renvoyé %r attendu %r" % (v, att))
        ok(faux.appels[-1][:2] == (60000, 1001), "rationnel 60000/1001 conservé",
           "appels=%s" % faux.appels)

        # ── La cadence est résolue UNE FOIS : `format()` lit le flow_def, pas gratuit ────────
        r = _reader({"fps_num": 25, "fps_den": 1})
        for i in range(5):
            r.index_time_ns(i)
        ok(r._appels_format[0] == 1, "cadence mise en cache (1 seule lecture du descripteur)",
           "format() appelé %d fois" % r._appels_format[0])

        # ── Symétrie avec le Writer : même nom, même signature ──────────────────────────────
        import inspect
        ok(str(inspect.signature(bobimxl.Reader.index_time_ns))
           == str(inspect.signature(bobimxl.Writer.index_time_ns)),
           "même signature que Writer.index_time_ns (pendant symétrique)")
    finally:
        bobimxl._lib = vrai

    print("\n%s" % ("Contrat respecté." if not ko else "%d contrôle(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(run())
