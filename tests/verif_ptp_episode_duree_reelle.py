#!/usr/bin/env python3
"""Un épisode de perte d'horloge PTP s'annonce UNE fois et porte sa durée RÉELLE.

Défaut mesuré le 2026-10-25 sur dl360-1 : `clock_ok` alterne à la cadence d'échantillonnage
pendant un épisode (20:55:01 déverrouillée, :06 verrouillée, :11 déverrouillée, :16
verrouillée). Le détecteur étant à front par échantillon, et le chronomètre de l'escalade
remis à zéro par CHAQUE échantillon bon, le journal portait 167 paires
« déverrouillée / verrouillée 5 s après » et ZÉRO escalade en 25 h — pour 7 h 20 de
dégradation cumulée et des épisodes allant jusqu'à 893 s.

La garde exerce le DÉTECTEUR RÉEL sur une séquence qui bat, en temps virtuel. Elle vérifie
trois choses, parce que corriger l'une sans les autres laisse le défaut :
  1. une seule annonce de perte pour tout l'épisode (pas une paire par battement) ;
  2. l'escalade se déclenche malgré le battement — c'est elle qui publie une durée ;
  3. la fermeture n'arrive qu'après l'hystérésis, et annonce la durée de l'ÉPISODE.

Le scénario est joué DEUX fois : avec le réglage `ptp_relock_stable_s` posé, et avec le
réglage ABSENT — sinon la garde épingle le réglage et ne couvre pas la valeur par DÉFAUT
(`PTP_RELOCK_STABLE_S`), qui est celle qui s'applique sur une installation neuve. Vérifié :
sans ce second passage, ramener la constante à 0 laissait la garde verte.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                    # noqa: E402

# Isolation AVANT tout accès : la connexion est thread-local, après c'est trop tard.
config.DB_PATH = database.DB_PATH = os.path.join(tempfile.mkdtemp(), "principale.db")
database._tls.__dict__.pop("conn", None)

from app import ptp                                                 # noqa: E402

echecs = []
evenements = []          # (type, level, detail)
alertes = []             # (clé ou message, level, params)


class _Horloge:
    """Proxy du module `time` dont seul `time()` est virtuel (le détecteur n'appelle que lui)."""
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def __getattr__(self, n):
        import time as _vrai
        return getattr(_vrai, n)


horloge = _Horloge()
ptp.time = horloge
database.db_add_ptp_event = lambda nid, nname, net, netname, ifn, typ, detail, level: \
    evenements.append((typ, level, detail))
database.db_add_alert = lambda msg, niveau="info", **kw: \
    alertes.append((msg, niveau, kw.get("params") or {}))
database.db_last_ptp_event = lambda *a, **k: None
_REGLAGES = {"ptp_unlock_err_s": 30.0, "ptp_relock_stable_s": 30.0, "ptp_alerts_enabled": 1}
database.db_get_setting = lambda cle, defaut=None: _REGLAGES.get(cle, defaut)

NODE = {"id": 34, "name": "dl360-1"}


def echantillon(bon):
    """Un échantillon de nœud à PTP MOTEUR : `synced` fait foi (cf. ptp.clock_ok)."""
    return {"domains": [{
        "network_id": 1, "name": "2110", "ifaces_state": {},
        "grandmaster_id": "4C-77-6D-FF-FE-FB-C6-03",
        "locked": bool(bon), "synced": bool(bon),
        "engine_ptp": True, "engine_ptp_client": True, "engine_sessions": 4,
        "ptp4l_running": False, "error": None,
    }]}


def tic(bon, pas=5.0):
    ptp._detect_ptp_events(NODE, echantillon(bon))
    horloge.t += pas


def verifier(cas, obtenu, attendu):
    if obtenu != attendu:
        echecs.append(f"{cas} : attendu {attendu!r}, obtenu {obtenu!r}")


def scenario(avec_reglage):
    """Rejoue tout l'épisode. `avec_reglage` False → la constante par défaut s'applique."""
    quoi = "réglage posé" if avec_reglage else "réglage ABSENT (défaut)"
    evenements.clear(); alertes.clear()
    ptp._ptp_event_state.clear(); ptp._unlocked_since.clear(); ptp._unlock_alerted.clear()
    ptp._episode_ouvert.clear(); ptp._relock_depuis.clear(); ptp._alerte_diffusee.clear()
    _REGLAGES.pop("ptp_relock_stable_s", None)
    if avec_reglage:
        _REGLAGES["ptp_relock_stable_s"] = 30.0

    tic(True)                                   # 1er passage = seed MUET
    verifier(f"[{quoi}] le seed n'émet rien", len(evenements), 0)

    # ── 200 s de BATTEMENT : un échantillon bas, un échantillon haut, en alternance ───────────
    for _ in range(20):
        tic(False)
        tic(True)

    pertes = [e for e in evenements if e[2] == "horloge déverrouillée"]
    fermetures = [e for e in evenements if e[2].startswith("horloge verrouillée")]
    escalades = [e for e in evenements if e[0] in ptp._ESCALADE_TYPES]

    verifier(f"[{quoi}] UNE seule annonce de perte pour tout l'épisode", len(pertes), 1)
    verifier(f"[{quoi}] aucune fermeture pendant le battement", len(fermetures), 0)
    verifier(f"[{quoi}] l'escalade se déclenche malgré le battement", len(escalades), 1)
    if escalades and "depuis" not in escalades[0][2]:
        echecs.append(f"[{quoi}] l'escalade doit porter une durée, elle dit : {escalades[0][2]!r}")

    # ── RETOUR franc : l'horloge tient. Rien ne doit se fermer avant l'hystérésis ─────────────
    tic(True)
    tic(True)
    verifier(f"[{quoi}] pas de fermeture après 10 s de stabilité (hystérésis 30 s)",
             len([e for e in evenements if e[2].startswith("horloge verrouillée")]), 0)
    for _ in range(6):
        tic(True)

    fermetures = [e for e in evenements if e[2].startswith("horloge verrouillée")]
    verifier(f"[{quoi}] fermeture une fois l'hystérésis écoulée", len(fermetures), 1)
    if fermetures:
        m = re.search(r"épisode de (\d+) s", fermetures[0][2])
        if not m:
            echecs.append(f"[{quoi}] la fermeture doit annoncer la durée : {fermetures[0][2]!r}")
        else:
            duree = int(m.group(1))
            # L'épisode a duré ~200 s de battement ; tolérance large, on teste l'ORDRE DE GRANDEUR
            # contre le défaut historique qui annonçait 5 s.
            if not 150 <= duree <= 260:
                echecs.append(f"[{quoi}] durée annoncée {duree} s — attendu ~200 s (le défaut disait 5 s)")

    return fermetures


for _avec in (True, False):
    fermetures = scenario(_avec)

if echecs:
    print("✗ verif_ptp_episode_duree_reelle")
    for e in echecs:
        print("   -", e)
    sys.exit(1)
print(f"✓ verif_ptp_episode_duree_reelle : 1 perte, 1 escalade, 1 fermeture à "
      f"{re.search(r'(\d+) s', fermetures[0][2]).group(1)} s sur 41 échantillons battants")
