"""Libellés LIÉS : le nom d'un élément d'un plugin-document ↔ un niveau de la page Libellés.

Décision de l'exploitant (2026-10-14, console audio) : le nom d'un bus ou d'une tranche remplit un
niveau de libellé de son flux de sortie, CHOISI par console, et les deux restent liés — renommer
dans la console met la page Libellés à jour, modifier la case sur la page Libellés renomme
l'élément. Comme `plugin_persist`, c'est DÉCLARATIF : l'orchestrateur ne connaît pas le plugin.

    "control": {"persist": {…, "labels": {"levels": "console.label_levels"}}}
        `levels` : chemin pointé dans params vers {"long": N|null, "short": M|null}, N/M = niveau
        de libellé (2 à 9, colonnes `label_N` de `source_labels`) ; absent ou null = pas de lien.
    "wiring": {"produces": [{…, "label": "{label} — direct", "short": "{short}"}]}
        le gabarit de libellé de chaque sortie (déjà utilisé par la page Câbles) et, en option, celui
        du nom court. Un gabarit à UN seul champ se lit dans les deux sens : « Lyon — direct » sur la
        page Libellés redonne « Lyon » au champ `label` de la tranche.

Nom court vide : on écrit un nom DÉDUIT du long (même règle que l'interface), pour que le niveau
« court » ne reste jamais vide alors que l'interface en affiche un.

Ce module ne lève jamais vers l'appelant : un libellé non synchronisé est une alerte, pas un
geste refusé.
"""
import copy
import re

_ONE = re.compile(r"^(.*?)\{([A-Za-z_][A-Za-z0-9_.]*)\}(.*)$")
LEVELS = range(2, 10)
SHORT_MAX = 12


def derive_short(text):
    """Nom court déduit d'un nom long — la MÊME règle que `shortOf` de l'interface de la console :
    7 caractères, en gardant un numéro final (« Invité 3 » → « Invit 3 »)."""
    t = str(text or "").strip()
    m = re.match(r"^(.*?)[\s_-]*(\d+)$", t)
    if len(t) <= 7 or not m or not m.group(1):
        return t[:7].strip()
    return (m.group(1)[:max(2, 6 - len(m.group(2)))].strip() + " " + m.group(2))[:8]


def spec(manifest):
    """{"levels": chemin} si le plugin déclare des libellés liés, sinon None."""
    p = (((manifest or {}).get("control") or {}).get("persist") or {}).get("labels")
    if not isinstance(p, dict) or not isinstance(p.get("levels"), str) or not p["levels"]:
        return None
    return {"levels": p["levels"]}


def levels_of(params, sp):
    """{"long": N|None, "short": M|None} — niveaux valides seulement (2..9)."""
    from .wiring_document import get_path
    raw = get_path(params or {}, sp["levels"]) or {}
    out = {}
    for k in ("long", "short"):
        v = raw.get(k) if isinstance(raw, dict) else None
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) and v in LEVELS else None
    return out


def outputs(manifest, params, hostname):
    """[{shm, entry, long, short, label_tpl, short_tpl}] : les sorties déclarées par le document."""
    from .wiring_document import expand, fill, port_shm
    out = []
    for sp in ((manifest or {}).get("wiring") or {}).get("produces") or []:
        if not isinstance(sp, dict) or not sp.get("key") or not sp.get("label"):
            continue
        for key, entry in expand(sp, params, hostname):
            long_ = fill(sp["label"], entry, hostname=hostname, key=key)
            short = fill(sp["short"], entry, hostname=hostname, key=key) if sp.get("short") else ""
            if not short.strip():
                # déduit du NOM de l'élément (le champ du gabarit long), pas du libellé de la sortie :
                # « Entrée 1 — N-1 » donnerait « Entré 1 », le « 1 » venant de « N-1 »
                r = reverse(sp["label"], long_)
                short = derive_short(r[1] if r else long_)
            out.append({"shm": port_shm(sp, key, entry, hostname), "entry": entry, "long": long_,
                        "short": short, "label_tpl": sp["label"], "short_tpl": sp.get("short")})
    return out


def reverse(tpl, text):
    """(champ, valeur) qu'un texte saisi donne à l'élément, pour un gabarit à UN seul champ ; None
    sinon. Le texte qui ne porte pas le préfixe / suffixe du gabarit est pris tel quel."""
    m = _ONE.match(tpl or "")
    if not m or "{" in m.group(1) or "{" in m.group(3):
        return None
    pre, field, suf = m.group(1), m.group(2), m.group(3)
    t = str(text or "").strip()
    if pre and t.startswith(pre):
        t = t[len(pre):]
    if suf and t.endswith(suf) and len(t) > len(suf):
        t = t[:-len(suf)]
    return field, t.strip()


# ── Console → page Libellés ─────────────────────────────────────────────────────────
def push(vmid, type_, params):
    """Écrit les niveaux liés pour toutes les sorties du document. Appelé après chaque écriture
    du document persisté (plugin_persist._write). N'écrit que ce qui change. Renvoie le nombre
    de lignes écrites."""
    try:
        from . import plugins
        from .database import db_get_container, db_get_source_labels, db_upsert_source_label
        m = plugins.get(type_) or {}
        sp = spec(m)
        if not sp:
            return 0
        lv = levels_of(params, sp)
        if not lv["long"] and not lv["short"]:
            return 0
        host = (db_get_container(vmid) or {}).get("hostname") or ""
        cur = {r["shm"]: r for r in db_get_source_labels()}
        n = 0
        for o in outputs(m, params, host):
            want = {}
            if lv["long"]:
                want["label_%d" % lv["long"]] = o["long"]
            if lv["short"]:
                want["label_%d" % lv["short"]] = o["short"]
            row = cur.get(o["shm"]) or {}
            diff = {k: v for k, v in want.items() if (row.get(k) or "") != v}
            if diff:
                db_upsert_source_label(o["shm"], diff)
                n += 1
        if n:
            from .tally import invalider_libelles
            invalider_libelles()
        return n
    except Exception as e:
        _alert(vmid, "page Libellés non mise à jour depuis le document (%s: %s)" % (type(e).__name__, e))
        return 0


# ── Page Libellés → console ─────────────────────────────────────────────────────────
def pull(shm, fields):
    """La page Libellés a écrit `fields` ({label_N: texte}) pour `shm`. Si ce flux est la sortie
    d'un élément dont un niveau est LIÉ, renomme l'élément dans son document (application de
    structure par le conteneur ; conteneur injoignable : document persisté directement, appliqué
    au redémarrage). Renvoie la liste des vmid modifiés."""
    done = []
    try:
        from . import plugins
        from .database import db_get_containers
        from .plugin_persist import _dc
        for c in db_get_containers():
            if not c.get("deploy_config"):
                continue
            dc = _dc(c["vmid"]) or {}             # le TYPE vit dans deploy_config, pas sur la ligne
            m = plugins.get(dc.get("type") or "") or {}
            sp = spec(m)
            if not sp:
                continue
            params = dc.get("params") or {}
            lv = levels_of(params, sp)
            if not lv["long"] and not lv["short"]:
                continue
            hit = [o for o in outputs(m, params, c.get("hostname") or "") if o["shm"] == shm]
            if not hit:
                continue
            o = hit[0]
            changes = {}
            for which, tpl in (("long", o["label_tpl"]), ("short", o["short_tpl"])):
                col = "label_%d" % lv[which] if lv[which] else None
                if not col or col not in fields or not tpl:
                    continue
                r = reverse(tpl, fields[col])
                if r is None:
                    continue
                field, val = r
                if which == "short":
                    val = val[:SHORT_MAX]
                    rl = reverse(o["label_tpl"], o["long"])
                    if val == derive_short(rl[1] if rl else o["long"]):
                        val = ""                        # le court déduit n'est pas un choix
                elif not val:
                    continue                            # un nom long vide ne renomme rien
                if (o["entry"].get(field) or "") != val:
                    changes[field] = val
            if changes and _rename(c["vmid"], m, params, o["entry"].get("id"), changes):
                done.append(c["vmid"])
    except Exception as e:
        _alert(None, "renommage depuis la page Libellés non appliqué (%s: %s)" % (type(e).__name__, e))
    return done


def _find(doc, eid):
    """L'élément d'id `eid` dans un document (premier niveau de listes sous la clé du document)."""
    for v in (doc or {}).values():
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and x.get("id") == eid:
                    return x
    return None


def _rename(vmid, manifest, params, eid, changes):
    import requests
    from . import plugin_persist as pp
    from .wiring_document import get_path
    ps = pp.spec(manifest)
    sp = spec(manifest)
    if not ps or not ps["apply"] or not eid:
        return False
    root = sp["levels"].split(".")[0]                   # « console » : la clé du document
    doc = None
    url = pp._url(vmid, ps["snapshot"])
    if url:
        try:
            r = requests.get(url, timeout=pp.HTTP_TIMEOUT_S)
            doc = r.json() if r.status_code == 200 else None
        except Exception:
            doc = None
    live = doc is not None
    doc = copy.deepcopy(doc if live else get_path(params, root) or {})
    el = _find(doc, eid)
    if el is None:
        return False
    el.update(changes)
    if live:
        body = {"doc": {k: v for k, v in doc.items() if k != "version"}, "base_version": doc.get("version"),
                "accept_latency_jump": False}
        res = pp.structure_apply(vmid, manifest, ps["apply"], body)
        if res and res[1] == 200:
            return True
        _alert(vmid, "renommage depuis la page Libellés refusé par le conteneur (HTTP %s)" % (res[1] if res else "?"))
        return False
    pp._write(vmid, ps["param"], doc)                    # conteneur arrêté : appliqué au redémarrage
    return True


def _alert(vmid, msg):
    try:
        from .database import db_add_alert
        db_add_alert(("#%s : " % vmid if vmid else "") + "libellés liés — " + msg, "warning", vmid=vmid)
    except Exception:
        pass
