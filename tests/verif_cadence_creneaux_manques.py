"""La cadence mesurée doit compter les images ÉCRITES, pas le temps écoulé.

★ CE QUE CETTE GARDE EMPÊCHE DE REVENIR (2026-10-23). `frame_index` d'un flux MXL est
normativement dérivé du TEMPS : il avance même quand le producteur saute un créneau. La cadence
« précise » calculée en Δindex/Δt rendait donc 50,0 pour un producteur qui n'écrivait que 34
images par seconde, et le verdict « tenue » qui en dérive affirmait que tout allait bien.
Mesuré sur stills-Satis : Δindex 508 en 10,00 s (50,79/s) contre 168 créneaux manqués, donc
33,99/s réellement publiées. Un testeur voyait ses images clignoter au noir devant une page
qui affichait « cadence 50,0 — tenue ».

Lancer : ./venv/bin/python tests/verif_cadence_creneaux_manques.py
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOBI_DB_PATH", "/tmp/verif_cadence.db")

from app import metrics  # noqa: E402

ECHECS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        ECHECS.append(nom)


class _Horloge:
    """Horloge contrôlée : la mesure dépend de Δt, on ne la laisse pas au hasard."""
    def __init__(self):
        self.t = 1000.0
        self._vrai = time.monotonic

    def __enter__(self):
        time.monotonic = lambda: self.t
        return self

    def __exit__(self, *a):
        time.monotonic = self._vrai

    def avancer(self, s):
        self.t += s


def _reset(vmid):
    metrics._fps_precis_prev.pop(vmid, None)
    metrics.fps_precis_cache.pop(vmid, None)


def main():
    print(__doc__.splitlines()[0])

    # ── 1. L'extraction du compteur, sous ses deux noms, et son absence.
    verifier("slots_missed lu", metrics.creneaux_manques_de({"slots_missed": 7}), 7)
    verifier("frames_missed lu", metrics.creneaux_manques_de({"frames_missed": 9}), 9)
    verifier("compteur absent", metrics.creneaux_manques_de({"fps": 50.0}), None)
    verifier("compteur non entier ignoré",
             metrics.creneaux_manques_de({"slots_missed": "beaucoup"}), None)

    # ── 2. LE CAS QUI A MENTI : index au rythme du temps, un tiers des images manquant.
    # 508 créneaux écoulés en 10 s, 168 sans publication → 340 images, soit 34,0/s.
    with _Horloge() as h:
        _reset(1252)
        metrics.fps_precis_note(1252, 1_000_000, 10_000)
        h.avancer(10.0)
        v = metrics.fps_precis_note(1252, 1_000_508, 10_168)
    verifier("producteur irrégulier : cadence RÉELLE, pas nominale", v, 34.0)

    # ── 3. Producteur sain : le compteur ne bouge pas, la mesure est inchangée.
    with _Horloge() as h:
        _reset(1248)
        metrics.fps_precis_note(1248, 500, 48_501)
        h.avancer(10.0)
        v = metrics.fps_precis_note(1248, 1000, 48_501)
    verifier("producteur sain : 50,0 préservé", v, 50.0)

    # ── 4. Plugin qui ne publie AUCUN compteur : comportement d'avant, pas de régression.
    with _Horloge() as h:
        _reset(777)
        metrics.fps_precis_note(777, 0)
        h.avancer(10.0)
        v = metrics.fps_precis_note(777, 500)
    verifier("sans compteur : Δindex/Δt inchangé", v, 50.0)

    # ── 5. Compteur reparti en arrière (script redéployé) : se taire plutôt qu'inventer.
    with _Horloge() as h:
        _reset(888)
        metrics.fps_precis_note(888, 0, 5_000)
        h.avancer(10.0)
        v = metrics.fps_precis_note(888, 500, 12)
    verifier("compteur remis à zéro : None", v, None)

    # ── 6. Plus de créneaux manqués que d'index écoulés : jamais de cadence négative.
    with _Horloge() as h:
        _reset(999)
        metrics.fps_precis_note(999, 0, 0)
        h.avancer(10.0)
        v = metrics.fps_precis_note(999, 100, 900)
    verifier("manques > écoulés : borné à 0,0", v, 0.0)

    # ── 7. PRODUCTEUR GELÉ : 0,0 sans attendre, jamais la dernière bonne valeur.
    # Ce cas a failli régresser le 2026-10-23. Pour amortir l'escalier d'échantillonnage j'avais
    # ajouté une « garde du palier » : quand l'index n'a pas bougé, renvoyer la valeur en cache.
    # Mesuré : elle affichait 50,0 pendant 37 s sur une source GELÉE (jusqu'à _FPS_PRECIS_DT_MAX),
    # là où le calcul normal rend 0,0 au bout de 2 s — et elle ne tirait quasiment jamais sur le
    # cas qu'elle visait (poll ~5 s contre republication ~1 s). Retirée ; verrouillée ici.
    with _Horloge() as h:
        _reset(1253)
        metrics.fps_precis_note(1253, 0, 0)
        h.avancer(10.0)
        sain = metrics.fps_precis_note(1253, 500, 0)       # 50,0 : le cache est garni
        h.avancer(2.0)
        gele = metrics.fps_precis_note(1253, 500, 0)       # index FIGÉ, 2 s plus tard
    verifier("producteur sain : cache garni avant le gel", sain, 50.0)
    verifier("producteur GELÉ : 0,0 en 2 s (pas la valeur en cache)", gele, 0.0)
    # Et il le reste : un gel prolongé ne doit pas « remonter » à l'ancienne cadence.
    with _Horloge() as h:
        _reset(1254)
        metrics.fps_precis_note(1254, 0, 0)
        h.avancer(10.0)
        metrics.fps_precis_note(1254, 500, 0)
        vus = []
        for dt in (2.0, 5.0, 10.0):
            h.avancer(dt)
            vus.append(metrics.fps_precis_note(1254, 500, 0))
    verifier("gel prolongé : 0,0 à chaque relevé", vus, [0.0, 0.0, 0.0])

    print()
    if ECHECS:
        print("ÉCHEC :", ", ".join(ECHECS))
        return 1
    print("OK — la cadence compte les images écrites, pas le temps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
