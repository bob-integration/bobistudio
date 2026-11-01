#!/usr/bin/env python3
"""L'interrupteur du DISTRIBUTEUR DE FLUX (`stream_hub_enabled`, app/stream_hub.py) se pose par
l'API des réglages.

Défaut corrigé le 2026-10-29 (nuit d'épreuve du mélangeur) : la clé manquait à `settings.DEFAULTS`.
POST /api/settings {"stream_hub_enabled": "0"} répondait 200, mais `update_bulk` la jetait comme
clé inconnue (journal : « clé(s) INCONNUE(S) ignorée(s) ») — mesuré deux fois sur l'orchestrateur
local, le distributeur restait actif et les nouveaux flux ne repassaient pas sur un fil. Seul le
test du distributeur la simulait (enabled() remplacé), d'où le trou.

Contrat :
  1. la clé est connue de DEFAULTS, valeur "1" (comportement inchangé par défaut) ;
  2. update_bulk l'ACCEPTE (n'est pas dans les ignorées) et settings.get la relit ;
  3. stream_hub.enabled() suit la base : "0" → False, "1" → True (cache vidé entre les deux).

Base jetable (DB_PATH posé AVANT tout accès). MUTATION=no_default retire la clé de DEFAULTS en
mémoire : la garde doit tomber (code 1).

    ./venv/bin/python tests/verif_stream_hub_interrupteur.py
"""
import os
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

from app import database as _db                                 # noqa: E402
_TMP = tempfile.mkdtemp(prefix="verif_hub_switch_")
_db.DB_PATH = os.path.join(_TMP, "db.sqlite")
_db.init_db()
from app import settings, stream_hub                           # noqa: E402

if os.environ.get("MUTATION") == "no_default":
    settings.DEFAULTS.pop("stream_hub_enabled", None)

fails = 0


def check(cond, label):
    global fails
    print(("OK   " if cond else "ÉCHEC") + "  " + label)
    if not cond:
        fails += 1


def enabled_now():
    stream_hub._en = (0.0, True)       # vide le cache de 5 s
    return stream_hub.enabled()


check(settings.DEFAULTS.get("stream_hub_enabled") == "1", "clé connue de DEFAULTS, défaut \"1\"")
check(enabled_now() is True, "base vierge : distributeur actif")
res = settings.update_bulk({"stream_hub_enabled": "0"})
ignored = res[1] if isinstance(res, tuple) else []
check("stream_hub_enabled" not in (ignored or []), "update_bulk accepte la clé (ignorées : %s)" % (ignored,))
check(settings.get("stream_hub_enabled") == "0", "settings.get relit \"0\"")
check(enabled_now() is False, "\"0\" → distributeur coupé")
settings.update_bulk({"stream_hub_enabled": "1"})
check(enabled_now() is True, "\"1\" → distributeur rétabli")

if fails:
    print("\n%d échec(s)" % fails)
    sys.exit(1)
print("\nTOUT OK : l'interrupteur du distributeur se pose par l'API des réglages")
