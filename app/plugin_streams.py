"""FLUX AUDIO/OCTETS GÉNÉRIQUES et DÉCLARATIFS à travers le proxy de plugin (sans WebSocket).

Waitress n'a pas de WebSocket ; un plugin qui doit diffuser un flux continu au navigateur (écoute
PCM d'une console) ou en recevoir un (talkback) le DÉCLARE, et le proxy le relaie tel quel :

    "control": { …, "streams": [
        {"path": "/listen.pcm", "method": "GET",  "perm": "read",
         "content_type": "audio/L16;rate=48000;channels=2"},
        {"path": "/talk.pcm",   "method": "POST", "perm": "operate", "max_bytes": 65536}
    ]}

- GET  : le conteneur est ouvert en `stream=True` (délai de CONNEXION seul + délai de lecture par
  morceau — pas de délai total de 5 s qui couperait un flux continu), relayé morceau par morceau
  (`Response(direct_passthrough=True)`), amont fermé à la déconnexion du client (GeneratorExit).
- POST : corps OCTETS bruts (`application/octet-stream`) + query string relayés, `max_bytes`
  imposé (413), statut du conteneur rendu tel quel (204…). Aucune analyse JSON.

Droits (décidé 2026-10-13) :
- `perm: "read"`    = comme `control.read_endpoints` : login + accès projet viewer ;
- `perm: "operate"` = `plugins.operate` + accès projet operator. Les ZONES de console
  (app/plugin_zones.py) ne s'appliquent PAS : elles filtrent des réglages d'éléments (`/set`,
  `/mute`), or un flux n'appartient à aucun élément — le talkback est global à la console. Un
  utilisateur restreint à ses zones garde donc le talkback tant qu'il peut opérer ; qui ne doit
  pas parler n'a pas `plugins.operate`. (Pas de `zone: "global"` : ce serait un réglage sans
  effet, la valeur seule possible.)
- un GET `perm: "operate"` est permis (écoute réservée aux opérateurs).

Les chemins d'un flux NE DOIVENT PAS figurer dans `endpoints`/`read_endpoints` : le proxy JSON
(corps JSON, délai de 5 s, mise en tampon complète) ne doit jamais en hériter.

LATENCE (mesurée, cf. tests/verif_plugin_streams.py) : Waitress 3.0.2 a `send_bytes=1` par défaut
(déprécié mais effectif) → chaque morceau est vidé aussitôt ; ~0,5 ms ajoutés (p95 < 1 ms) sur un
PCM de 10 ms. Aucun changement de main.py n'est nécessaire — NE PAS fixer `send_bytes` plus haut
(18000 = ancien défaut : ~53 ms p50 / 100 ms p95 de retard sur ce flux). cheroot (mode tls) vide
aussi à chaque morceau (~0,6 ms mesuré).
"""
import logging

log = logging.getLogger("bobistudio.plugin_streams")

CHUNK = 1920                 # 10 ms de PCM int16 stéréo 48 kHz
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 3.0         # par morceau : un flux continu n'a pas de durée totale
DEFAULT_MAX_BYTES = 65536
HARD_MAX_BYTES = 16 * 1024 * 1024
_METHODS = ("GET", "POST")
_PERMS = ("read", "operate")


def _eps(ctrl):
    e = ctrl.get("endpoints") or []
    e = set(e) if isinstance(e, list) else {("/" + k) for k in e}
    return e | set(ctrl.get("read_endpoints") or [])


def validate_spec(m):
    """Erreurs de `control.streams` (liste vide = valide ou absente)."""
    ctrl = (m or {}).get("control") or {}
    s = ctrl.get("streams")
    if s is None:
        return []
    if not isinstance(s, list):
        return ["control.streams doit être une liste"]
    errs, seen, json_eps = [], set(), _eps(ctrl)
    for i, e in enumerate(s):
        w = "control.streams[%d]" % i
        if not isinstance(e, dict):
            errs.append(w + " doit être un objet")
            continue
        p, meth = e.get("path"), e.get("method")
        if not isinstance(p, str) or not p.startswith("/") or len(p) < 2 or ".." in p \
                or "?" in p or any(c.isspace() for c in p):
            errs.append(w + ".path : chemin absolu sans espace ni '..' requis")
            continue
        if meth not in _METHODS:
            errs.append(w + ".method : GET ou POST")
        if e.get("perm") not in _PERMS:
            errs.append(w + ".perm : read ou operate")
        if meth == "POST" and e.get("perm") != "operate":
            errs.append(w + " : un POST exige perm=operate")
        if p in json_eps:
            errs.append(w + ".path %s collisionne avec control.endpoints/read_endpoints" % p)
        if (p, meth) in seen:
            errs.append(w + " : doublon %s %s" % (meth, p))
        seen.add((p, meth))
        if meth == "GET" and not (isinstance(e.get("content_type"), str) and e["content_type"]):
            errs.append(w + ".content_type requis pour un GET")
        mb = e.get("max_bytes", DEFAULT_MAX_BYTES)
        if meth == "POST" and (isinstance(mb, bool) or not isinstance(mb, int)
                               or not 0 < mb <= HARD_MAX_BYTES):
            errs.append(w + ".max_bytes : entier de 1 à %d" % HARD_MAX_BYTES)
    return errs


def find(m, method, path):
    """Entrée déclarée pour (méthode, chemin) — `path` avec ou sans '/' initial — ou None."""
    if validate_spec(m):
        return None
    p = "/" + str(path).lstrip("/")
    for e in ((m or {}).get("control") or {}).get("streams") or []:
        if e["path"] == p and e["method"] == method:
            return e
    return None


def _deny(vmid, entry):
    """Réponse Flask de refus, ou None si l'utilisateur courant a le droit."""
    from flask import jsonify
    from .auth import check_vmid_access, has_perm
    operate = entry["perm"] == "operate"
    err = check_vmid_access(vmid, "operator" if operate else "viewer")
    if err:
        return err
    if operate and not has_perm("plugins.operate"):
        return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    return None


def _url(vmid, p):
    from .addressing import get_container_ip
    ip = get_container_ip(vmid)
    return ("http://%s:8082/%s" % (ip, p.lstrip("/"))) if ip else None


def relay_get(url, entry, args, *, http_get=None):
    """Réponse Flask en flux pour un GET déclaré. `http_get` injectable (tests)."""
    from flask import Response, jsonify
    import requests
    get = http_get or requests.get
    try:
        up = get(url, params=args, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
    except Exception as e:                                            # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    if up.status_code != 200:
        try:
            body, ct = up.content, up.headers.get("Content-Type", "application/json")
        finally:
            up.close()
        return Response(body, status=up.status_code, content_type=ct)

    def gen():
        try:
            # read1 rend ce qui est DISPONIBLE (≤ CHUNK) sans attendre de remplir le morceau :
            # `iter_content(CHUNK)` bloquerait jusqu'à CHUNK octets, donc retarderait la fin d'un
            # flux qui s'écrit par rafales plus petites.
            read = getattr(up.raw, "read1", None)
            while True:
                b = read(CHUNK) if read else up.raw.read(CHUNK)
                if not b:
                    return
                yield b
        except GeneratorExit:
            raise                       # client parti : le finally ferme l'amont
        except Exception as e:                                        # noqa: BLE001
            log.warning("flux %s interrompu : %s", url, e)
        finally:
            up.close()

    return Response(gen(), status=200, content_type=entry["content_type"],
                    direct_passthrough=True,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


_sess_local = None


def _session():
    """Session HTTP PARTAGÉE (connexions gardées ouvertes) : le micro d'ordre poste toutes les
    20-40 ms, et rouvrir une connexion TCP à chaque paquet coûtait une part notable des ~14 ms que
    l'orchestrateur ajoutait au trajet (mesuré sur banc le 2026-10-13 : conteneur seul 3,3 ms,
    via le proxy 17,6 ms au p50). requests.Session est sûre ici : un pool par hôte."""
    global _sess_local
    if _sess_local is None:
        import requests
        s = requests.Session()
        s.mount("http://", requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16))
        _sess_local = s
    return _sess_local


def relay_post(url, entry, req, *, http_post=None):
    """Relaie le corps OCTETS d'un POST déclaré ; 413 au-delà de `max_bytes`."""
    from flask import Response, jsonify
    cap = int(entry.get("max_bytes", DEFAULT_MAX_BYTES))
    if (req.content_length or 0) > cap:
        return jsonify({"error": "corps trop gros", "max_bytes": cap}), 413
    data = req.get_data(cache=False) if req.content_length is not None else req.stream.read(cap + 1)
    if len(data) > cap:
        return jsonify({"error": "corps trop gros", "max_bytes": cap}), 413
    post = http_post or _session().post
    try:
        r = post(url, params=req.args, data=data, timeout=5,
                 headers={"Content-Type": "application/octet-stream"})
    except Exception as e:                                            # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    if r.status_code == 204 or not r.content:
        return Response(b"", status=r.status_code)
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/json"))


def handle(vmid, m, p):
    """Point d'entrée du proxy : réponse Flask si `p` est un flux déclaré (droits compris), sinon
    None (le proxy poursuit son chemin JSON habituel)."""
    from flask import request, jsonify
    entry = find(m, request.method, p)
    if not entry:
        return None
    denied = _deny(vmid, entry)
    if denied:
        return denied
    url = _url(vmid, p)
    if not url:
        return jsonify({"error": "IP container introuvable"}), 404
    if entry["method"] == "GET":
        return relay_get(url, entry, request.args)
    return relay_post(url, entry, request)

