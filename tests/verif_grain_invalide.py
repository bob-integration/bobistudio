#!/usr/bin/env python3
"""Vérifie que `bobimxl.Reader` ÉCARTE les grains marqués invalides (MXL_GRAIN_FLAG_INVALID).

Pourquoi ce banc existe : quand un producteur saute des index, libmxl invalide les cases d'anneau
que l'écriture continue aurait traversées — elle y pose le bon `index`, `validSlices = 0` et le
drapeau, en laissant le CONTENU PÉRIMÉ de la case. Le lecteur rend alors MXL_STATUS_OK en
court-circuitant le test de tranches : rien dans le statut, l'index ou la cadence ne distingue ce
bouche-trou d'une image. Et l'idiome `int(gi.validSlices or k)`, répandu chez nos consommateurs,
retourne ce zéro en « k tranches valides » — le bouche-trou est composité à pleine confiance.

Ce banc tourne SANS libmxl (absente du contrôleur) : le Reader est construit sans `__init__` et
`_lib` est remplacée par un double qui fabrique les deux formes de grain.

    ./venv/bin/python tests/verif_grain_invalide.py
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


TOTAL = 30            # tranches d'une image complète, dans ce banc


class _FauxLib:
    """Double de libmxl. `invalides` = ensemble des index que le writer a comblés.

    Reproduit fidèlement `PosixDiscreteFlowWriter::openGrain` (drapeau + validSlices=0 sur le
    bouche-trou) ET `PosixDiscreteFlowReader::getGrainImpl` (OK sur le drapeau, SANS regarder les
    tranches). C'est ce court-circuit qui rend le défaut invisible côté consommateur."""

    def __init__(self, head=100, invalides=()):
        self.head = head
        self.invalides = set(invalides)
        self.servis = []

    def _remplir(self, index, gi_ref):
        gi = gi_ref._obj
        gi.index = index
        gi.grainSize = 4096
        gi.totalSlices = TOTAL
        if index in self.invalides:
            gi.flags = bobimxl.MXL_GRAIN_FLAG_INVALID
            gi.validSlices = 0            # ⚠ le bouche-trou n'a AUCUNE tranche
        else:
            gi.flags = 0
            gi.validSlices = TOTAL
        self.servis.append(index)
        return bobimxl.MXL_STATUS_OK

    def mxlCreateFlowReader(self, inst_h, fid, options, h_ref):
        return bobimxl.MXL_STATUS_OK

    def mxlFlowReaderGetRuntimeInfo(self, h, rt_ref):
        rt_ref._obj.headIndex = self.head
        return bobimxl.MXL_STATUS_OK

    def mxlFlowReaderGetGrain(self, h, index, timeout_ns, gi_ref, payload_ref):
        return self._remplir(index, gi_ref)

    def mxlFlowReaderGetGrainNonBlocking(self, h, index, gi_ref, payload_ref):
        return self._remplir(index, gi_ref)

    def mxlFlowReaderGetGrainSlice(self, h, index, mvs, timeout_ns, gi_ref, payload_ref):
        return self._remplir(index, gi_ref)

    def mxlFlowReaderGetGrainSliceNonBlocking(self, h, index, mvs, gi_ref, payload_ref):
        return self._remplir(index, gi_ref)


def _reader():
    """Reader SANS __init__ (qui exigerait le domaine MXL), garde de génération désarmée."""
    r = object.__new__(bobimxl.Reader)
    r.name = "essai"
    r._h = None
    r.auto_reopen = False            # neutralise _auto_garde (pas de réouverture en banc)
    r._auto_vu = False
    r.invalid_grains = 0
    r.last_invalid_index = None
    r._invalid_log_ns = 10 ** 18     # journal muet : on contrôle le contrat, pas la trace
    return r


def run():
    vrai_lib, vraie_vue = bobimxl._lib, bobimxl._np_view
    bobimxl._np_view = lambda payload, taille: "VUE"
    try:
        # ── Le prédicat seul ────────────────────────────────────────────────────────────────
        class _G:
            def __init__(self, f):
                self.flags = f
        ok(bobimxl.grain_is_invalid(_G(0)) is False, "flags=0 → grain valide")
        ok(bobimxl.grain_is_invalid(_G(1)) is True, "flags=INVALID → invalide")
        ok(bobimxl.grain_is_invalid(_G(0x11)) is True,
           "un AUTRE drapeau posé à côté ne masque pas INVALID (test par masque, pas par égalité)")
        ok(bobimxl.grain_is_invalid(object()) is False, "objet sans .flags → False (ne lève pas)")
        ok(bobimxl.MXL_GRAIN_FLAG_INVALID == 0x00000001,
           "valeur conforme à flow.h", "vaut %r" % bobimxl.MXL_GRAIN_FLAG_INVALID)

        # ── get() : le chemin genlock ───────────────────────────────────────────────────────
        bobimxl._lib = _FauxLib(invalides=[50])
        r = _reader()
        bon = r.get(49)
        ok(bon is not None and bon[0] == 49 and bon[2] == "VUE",
           "get() d'un grain normal → le triplet, inchangé", "renvoyé %r" % (bon,))
        ok(bon is not None and int(bon[1].validSlices) == TOTAL,
           "get() d'un grain normal → validSlices complet")
        ok(r.get(50) is None,
           "get() d'un BOUCHE-TROU → None (et non un triplet au contenu périmé)",
           "renvoyé %r" % (r.get(50),))

        # ── get_slice() : le chemin obligatoire de tout nouveau plugin ──────────────────────
        bobimxl._lib = _FauxLib(invalides=[50])
        r = _reader()
        ok(r.get_slice(49, 12) is not None, "get_slice() d'un grain normal → le triplet")
        ok(r.get_slice(50, 12) is None, "get_slice() d'un BOUCHE-TROU → None")
        ok(r.get_slice_nonblocking(50, 12) is None,
           "get_slice_nonblocking() d'un BOUCHE-TROU → None")

        # ── CE QUE LE FILTRE ÉVITE : l'idiome `validSlices or k` ────────────────────────────
        # Sans le filtre, le consommateur recevrait ce grain et appliquerait son idiome habituel.
        # On le DÉMONTRE via l'échappatoire, pour que la raison du None soit écrite ici.
        bobimxl._lib = _FauxLib(invalides=[50])
        r = _reader()
        brut = r.get_slice(50, 12, accept_invalid=True)
        ok(brut is not None, "accept_invalid=True → l'échappatoire rend bien le grain")
        need_k = 12
        trompe = max(need_k, int(brut[1].validSlices or need_k))
        ok(trompe == need_k and int(brut[1].validSlices) == 0,
           "l'idiome `validSlices or need_k` retourne 0 tranche en %d — d'où le filtrage" % need_k,
           "validSlices=%r → %r" % (int(brut[1].validSlices), trompe))
        ok(max(1, int(brut[1].validSlices or TOTAL)) == TOTAL,
           "et `validSlices or total` le retourne en TRAME COMPLÈTE (cas le plus grave)")

        # ── get_latest() : reculer d'une case plutôt que servir du périmé ───────────────────
        bobimxl._lib = _FauxLib(head=100, invalides=[100])
        r = _reader()
        v = r.get_latest()
        ok(v is not None and v[0] == 99,
           "get_latest() sur un head INVALIDE → recule sur 99 (dernière image réelle)",
           "renvoyé %r" % (v if v is None else v[0],))
        bobimxl._lib = _FauxLib(head=100, invalides=[100, 99])
        r = _reader()
        ok(r.get_latest() is None,
           "get_latest() quand head ET head-1 sont des bouches-trous → None")
        ok(r._auto_vu is True,
           "…mais le flux est marqué VU : un writer qui comble des trous est VIVANT, la garde de "
           "génération doit rester armée")

        # ── Observabilité : sans compteur, le comblement reste invisible ────────────────────
        bobimxl._lib = _FauxLib(invalides=[50, 51, 52])
        r = _reader()
        for i in (50, 51, 52, 49):
            r.get_slice(i, 12)
        ok(r.invalid_grains == 3, "invalid_grains compte les bouches-trous écartés",
           "vaut %r" % r.invalid_grains)
        ok(r.last_invalid_index == 52, "last_invalid_index = le dernier vu",
           "vaut %r" % r.last_invalid_index)

        # ── Un Reader construit SANS __init__ doit tenir ────────────────────────────────────
        # C'est le cas de ce banc et de toute introspection : les trois attributs doivent exister
        # sur la CLASSE, sinon `_is_filler` lève un AttributeError au premier bouche-trou — et il
        # lèverait dans la boucle de composition, là où rien ne le rattrape.
        bobimxl._lib = _FauxLib(invalides=[50])
        nu = object.__new__(bobimxl.Reader)
        nu.name, nu._h, nu.auto_reopen, nu._auto_vu = "nu", None, False, False
        try:
            rendu = nu.get_slice(50, 12)
            ok(rendu is None and nu.invalid_grains == 1,
               "Reader sans __init__ : _is_filler tient et compte", "rendu=%r" % (rendu,))
        except AttributeError as e:
            ok(False, "Reader sans __init__ : _is_filler tient et compte", "a levé %r" % (e,))
        ok(bobimxl.Reader.invalid_grains == 0,
           "…et le compteur de CLASSE reste à 0 (chaque lecteur a le sien)",
           "classe=%r" % bobimxl.Reader.invalid_grains)

        # ── L'AGRÉGAT publié sur :8080 ──────────────────────────────────────────────────────
        # `invalid_grains_report()` est le seul témoin du comblement d'un trou amont : cadence,
        # index et statut de lecture restent tous parfaits pendant l'épisode.
        bobimxl._live_readers.clear()
        ok(bobimxl.invalid_grains_report() == {"total": 0, "flows": {}},
           "aucun bouche-trou → total 0 et aucun flux cité",
           "rendu %r" % (bobimxl.invalid_grains_report(),))

        bobimxl._lib = _FauxLib(invalides=[50, 51])
        a, b = _reader(), _reader()
        a.name, b.name = "camera-1", "camera-2"
        bobimxl._live_readers.add(a)
        bobimxl._live_readers.add(b)
        a.get_slice(50, 12)
        a.get_slice(51, 12)
        b.get_slice(50, 12)
        rap = bobimxl.invalid_grains_report()
        ok(rap["total"] == 3, "total agrégé sur tous les lecteurs du processus",
           "rendu %r" % (rap,))
        ok(rap["flows"] == {"camera-1": 2, "camera-2": 1},
           "…et l'ENTRÉE fautive est désignée (ce qu'un total seul ne dit pas)",
           "rendu %r" % (rap["flows"],))

        # Un lecteur SAIN ne doit pas figurer dans `flows` : en exploitation la quasi-totalité
        # des lecteurs sont à zéro, et les lister noierait l'entrée fautive dans le bruit.
        sain = _reader(); sain.name = "camera-saine"
        bobimxl._live_readers.add(sain)
        sain.get_slice(49, 12)
        ok("camera-saine" not in bobimxl.invalid_grains_report()["flows"],
           "un lecteur sans bouche-trou n'apparaît pas dans flows",
           "rendu %r" % (bobimxl.invalid_grains_report()["flows"],))

        # Deux lecteurs sur le MÊME flux (tuiles d'un mur, couches d'un mixer) : on SOMME.
        c = _reader(); c.name = "camera-1"
        bobimxl._live_readers.add(c)
        c.get_slice(50, 12)
        ok(bobimxl.invalid_grains_report()["flows"]["camera-1"] == 3,
           "deux lecteurs du même flux sont sommés, pas écrasés",
           "rendu %r" % (bobimxl.invalid_grains_report()["flows"],))

        # Un lecteur détruit sort du registre (WeakSet) — sinon le processus fuit un lecteur par
        # réouverture, et le mur en rouvre à chaque décrochage.
        del b
        import gc
        gc.collect()
        ok("camera-2" not in bobimxl.invalid_grains_report()["flows"],
           "un lecteur détruit sort du registre (pas de fuite par réouverture)",
           "rendu %r" % (bobimxl.invalid_grains_report()["flows"],))

        # ── Le VRAI __init__ : c'est lui qui doit inscrire le lecteur au registre ───────────
        # Les contrôles ci-dessus inscrivent à la main (Reader construit par __new__). Ici on
        # exerce `Reader.__init__` pour de bon — `_load` neutralisé, `mxlCreateFlowReader` servi
        # par le double — sinon la LIGNE D'INSCRIPTION elle-même n'est testée par rien, et le
        # compteur resterait invisible sur :8080 sans qu'aucun banc ne le voie.
        bobimxl._live_readers.clear()
        vrai_load = bobimxl._load
        bobimxl._load = lambda: None
        try:
            class _FauxInst:
                _h = None
            bobimxl._lib = _FauxLib(invalides=[50])
            vrai = bobimxl.Reader(_FauxInst(), "camera-reelle")
            ok(vrai in bobimxl._live_readers,
               "__init__ inscrit le lecteur au registre (sinon rien ne remonte sur :8080)")
            ok(vrai.invalid_grains == 0 and vrai.last_invalid_index is None,
               "__init__ remet les compteurs à zéro sur l'INSTANCE")
            vrai.auto_reopen = False
            vrai.get_slice(50, 12)
            ok(bobimxl.invalid_grains_report() == {"total": 1, "flows": {"camera-reelle": 1}},
               "un lecteur construit normalement remonte dans l'agrégat",
               "rendu %r" % (bobimxl.invalid_grains_report(),))
        finally:
            bobimxl._load = vrai_load
        bobimxl._live_readers.clear()

        # ── L'échappatoire existe sur les quatre chemins de lecture ─────────────────────────
        import inspect
        for nom in ("get", "get_latest", "get_slice", "get_slice_nonblocking"):
            sig = inspect.signature(getattr(bobimxl.Reader, nom))
            p = sig.parameters.get("accept_invalid")
            ok(p is not None and p.default is False,
               "%s() porte accept_invalid=False par défaut (refus par DÉFAUT)" % nom,
               "signature %s" % sig)
    finally:
        bobimxl._lib, bobimxl._np_view = vrai_lib, vraie_vue

    print("\n%s" % ("Contrat respecté." if not ko else "%d contrôle(s) en échec." % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(run())
