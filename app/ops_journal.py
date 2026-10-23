# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""JOURNAL D'EXPLOITATION DES PLUGINS — « qui a fait quoi » sur une console, un player, un mélangeur…

Cahier des charges : docs/chantiers/JOURNAL_EXPLOITATION.md. Contrat des plugins :
docs/reference/PLUGIN_JOURNAL.md. Règles (pures) : app/ops_journal_rules.py.

★ UN HELPER, UN TRY/EXCEPT, AUCUN ANTI-REBOND (modèle : app/security_log.py).
  - `record_post` / `record_structure` / `record_event` sont les SEULES portes d'écriture. Chacune
    avale ses propres exceptions (log.exception, bruyant) : un journal en panne ne doit JAMAIS faire
    échouer la commande qu'il raconte — le mute est parti, la ligne est perdue, c'est l'ordre des
    gravités.
  - Pas d'anti-rebond : deux mutes identiques sont deux gestes. Le volume est borné par la règle
    (rien de continu n'entre) et par la rétention, pas par une déduplication qui omettrait un fait.
  - Écrit APRÈS une réponse acceptée du conteneur (2xx) : une commande refusée n'a pas eu lieu.

★ TABLE DÉDIÉE, PAS UN `kind` DE PLUS DANS `alerts`. Décidé le 2026-10-17, pour trois raisons :
  1. le volume : une console active écrit des centaines de mutes/on-off par heure. Dans `alerts`
     (rétention en NOMBRE de lignes, 10 000 partagées), ce flux chasserait les vraies alertes en
     quelques jours — exactement le défaut mesuré le 2026-08-15 qui a valu sa rétention propre à la
     piste de sécurité ; et la page Alertes en serait noyée, ce que l'exploitant a exclu ;
  2. la rétention demandée est en JOURS (7 glissants), pas en lignes ;
  3. les filtres (instance, opérateur, type, période) et l'identité stable de l'instance
     (`instance_uuid`, 2e barreau) veulent des colonnes et des index à eux.
  Les SORTIES (syslog, SNMP…) restent celles de `services/alerting` : `notify()` est appelé quand
  le réglage `ops_journal_forward` est vrai (éteint par défaut — les canaux sont communs, un e-mail
  au seuil « info » recevrait chaque mute). Aucun I/O réseau ici : `notify` empile et rend la main.

★ AUTEUR (`actor`) : un dict {kind, name, detail}, `kind` dans ACTOR_KINDS.
  - `user`    : opérateur de l'interface (proxy de plugin, shotbox) — déduit du contexte de requête ;
  - `macro`   : macro lancée par `name` (l'utilisateur), `detail` = nom de la macro ;
  - `trigger` : déclencheur automatique `name`, `detail` = nom de la macro ;
  - `surface` : surface de contrôle `detail`, au nom de SON opérateur `name` (services/control_surface,
                à venir : `surface_actor(surface, operator)`) ;
  - `auto`    : l'orchestrateur lui-même (`detail` = la fonction, ex. « afv ») ;
  - `plugin`  : événement interne publié par le plugin (`detail` = son type) ;
  - `public`  : porteur d'un LIEN PUBLIC jouable (`name` = titre du lien, `detail` = début du jeton) —
                personne n'est identifié, c'est le lien qui l'est (2026-10-18, `record_public`).
  Hors contexte de requête et sans auteur explicite : `auto`. On ne devine jamais un humain.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from . import ops_journal_rules as rules

log = logging.getLogger(__name__)

ENABLED = True                  # interrupteur de banc (tests qui chargent de vrais manifestes)
ACTOR_KINDS = ("user", "macro", "trigger", "surface", "auto", "plugin", "public")
RETENTION_DAYS_DEFAULT = 7
RETENTION_DAYS_MAX = 366
PURGE_EVERY_S = 60.0            # purge au plus une fois par minute, sur le chemin d'écriture
PURGE_CHUNK = 5000              # … et par tranches bornées : jamais un DELETE de toute la table
MAX_ROWS = 500000               # plafond dur, quelle que soit la rétention (≈ 1 an d'une console active)
QUERY_MAX = 5000

_ready = set()
_ready_lock = threading.Lock()
_purge = {"at": 0.0}


# ─── Auteurs ──────────────────────────────────────────────────────────────────────────────────

def user_actor(username):
    return {"kind": "user", "name": username or None, "detail": None}


def macro_actor(run_user, macro_name=None):
    """Auteur d'une étape de macro. `run_user` = `_Run.user` : un login, ou `trigger:<nom>` pour un
    déclencheur automatique (cf. macros._triggers_tick)."""
    u = str(run_user or "")
    if u.startswith("trigger:"):
        return {"kind": "trigger", "name": u[len("trigger:"):] or None, "detail": macro_name}
    return {"kind": "macro", "name": u or None, "detail": macro_name}


def surface_actor(surface, operator=None):
    """Surface de contrôle (services/control_surface) : elle agit au nom de SON opérateur."""
    return {"kind": "surface", "name": operator or None, "detail": surface}


def auto_actor(source=None):
    return {"kind": "auto", "name": None, "detail": source}


def plugin_actor(plugin_type=None):
    return {"kind": "plugin", "name": None, "detail": plugin_type}


def public_actor(link):
    """Le porteur d'un lien public : on ne connaît que le LIEN. `detail` = les 6 premiers caractères du
    jeton (de quoi distinguer deux liens de même titre, pas de quoi le reconstituer)."""
    link = link or {}
    return {"kind": "public", "name": link.get("title") or None, "detail": (link.get("token") or "")[:6] or None}


def current_actor():
    """Auteur déduit du contexte : l'utilisateur de la requête, sinon « automatique »."""
    try:
        from flask import has_request_context
        if has_request_context():
            from .auth import current_user
            u = current_user() or {}
            if u.get("username"):
                return user_actor(u["username"])
    except Exception:
        pass
    return auto_actor()


def _norm_actor(actor):
    # Un auteur TEXTE est la forme `_Run.user` des macros : un login, ou `trigger:<nom>` (déclencheurs,
    # fader start de services/audio/fader_triggers.py). Sans ce repli il tombait sur « automatique ».
    if isinstance(actor, str) and actor:
        actor = macro_actor(actor)
    a = dict(actor) if isinstance(actor, dict) else current_actor()
    if a.get("kind") not in ACTOR_KINDS:
        log.warning("ops_journal: unknown actor kind %r, recorded as auto", a.get("kind"))
        a["kind"] = "auto"
    return {"kind": a["kind"], "name": a.get("name") or None, "detail": a.get("detail") or None}


# ─── Stockage ─────────────────────────────────────────────────────────────────────────────────

def _db():
    from .database import get_db, DB_PATH
    db = get_db()
    if DB_PATH not in _ready:
        with _ready_lock:
            if DB_PATH not in _ready:
                ensure_table(db)
                _ready.add(DB_PATH)
    return db


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS ops_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,                 -- heure locale ISO, à la milliseconde (comme `alerts`)
        vmid INTEGER,                     -- handle jetable, pour le lien vers la page du plugin
        instance_uuid TEXT,               -- 2e barreau : survit au recreate, c'est lui qu'on filtre
        instance_label TEXT,              -- hostname AU MOMENT de l'action
        plugin_type TEXT,
        project_id INTEGER,               -- portée des utilisateurs scopés projet
        actor_kind TEXT NOT NULL,         -- ACTOR_KINDS
        actor_name TEXT,                  -- l'opérateur (filtre « opérateur »)
        actor_detail TEXT,                -- macro, déclencheur, surface, fonction automatique
        category TEXT NOT NULL,           -- ops_journal_rules.CATEGORIES (filtre « type »)
        endpoint TEXT,
        target TEXT,                      -- chemins d'IDS (un libellé se renomme, un id non)
        summary TEXT,                     -- rendu canonique (langue de l'installation), recherche
        details TEXT,                     -- JSON : entrées {path, label, value} / diff / événement
        ext_key TEXT                      -- clé d'unicité d'un événement de plugin (boot:seq)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_ops_journal_ts ON ops_journal(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_ops_journal_inst ON ops_journal(instance_uuid, id)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_ops_journal_actor ON ops_journal(actor_name, id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ops_journal_ext ON ops_journal(instance_uuid, ext_key) "
               "WHERE ext_key IS NOT NULL")
    db.commit()


def _retention_days():
    try:
        from . import settings as st
        v = int(st.get("ops_journal_retention_days", RETENTION_DAYS_DEFAULT) or RETENTION_DAYS_DEFAULT)
    except (TypeError, ValueError):
        return RETENTION_DAYS_DEFAULT
    except Exception:
        return RETENTION_DAYS_DEFAULT
    return max(1, min(RETENTION_DAYS_MAX, v))


def purge(now=None, force=False):
    """Retire les lignes plus vieilles que la rétention, et le surplus au-delà de MAX_ROWS. Par
    tranches de PURGE_CHUNK : une purge ne bloque jamais la base plus que le temps d'une tranche
    (le reste part au passage suivant). Rend le nombre de lignes retirées."""
    t = time.monotonic()
    if not force and t - _purge["at"] < PURGE_EVERY_S:
        return 0
    _purge["at"] = t
    now = now or datetime.now()
    cut = (now - timedelta(days=_retention_days())).isoformat(timespec="milliseconds")
    db = _db()
    n = db.execute("DELETE FROM ops_journal WHERE id IN (SELECT id FROM ops_journal WHERE ts < ? "
                   "ORDER BY id LIMIT ?)", (cut, PURGE_CHUNK)).rowcount
    total = db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]
    if total > MAX_ROWS:
        n += db.execute("DELETE FROM ops_journal WHERE id IN (SELECT id FROM ops_journal ORDER BY id "
                        "LIMIT ?)", (min(PURGE_CHUNK, total - MAX_ROWS),)).rowcount
    db.commit()
    return n


def _container_ctx(vmid):
    """(conteneur, deploy_config) — lus en base, jamais dans le conteneur."""
    from .database import db_get_container
    c = db_get_container(vmid) or {}
    dc = c.get("deploy_config")
    try:
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    except ValueError:
        dc = {}
    return c, dc


def _persisted_doc(dc, sp):
    key = ((sp or {}).get("names") or {}).get("param")
    doc = ((dc or {}).get("params") or {}).get(key) if key else None
    return doc if isinstance(doc, dict) else {}


def _insert(vmid, c, dc, actor, category, endpoint, target, details, summary, ext_key=None, ts=None):
    a = _norm_actor(actor)
    db = _db()
    cur = db.execute(
        "INSERT OR IGNORE INTO ops_journal (ts, vmid, instance_uuid, instance_label, plugin_type, project_id, "
        "actor_kind, actor_name, actor_detail, category, endpoint, target, summary, details, ext_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ((ts or datetime.now()).isoformat(timespec="milliseconds"), vmid, c.get("instance_uuid"),
         c.get("hostname") or (("#%s" % vmid) if vmid is not None else None), (dc or {}).get("type"),
         c.get("project_id"), a["kind"], a["name"], a["detail"], category, endpoint, target,
         summary, json.dumps(details, ensure_ascii=False, default=str), ext_key))
    db.commit()
    rid = cur.lastrowid if cur.rowcount else None
    try:
        purge()
    except Exception:
        log.exception("ops_journal: purge failed")
    if rid:
        _forward(vmid, a, summary, c)
    return rid


def _forward(vmid, actor, summary, c):
    """Recopie vers les canaux du service d'alertes (syslog, SNMP…), si l'exploitant l'a demandé."""
    try:
        from . import settings as st
        if str(st.get("ops_journal_forward", False)).lower() not in ("1", "true", "yes", "on"):
            return
        from services.alerting import notify
        who = actor_text(actor, lang=None)
        notify("[%s] %s — %s" % (c.get("hostname") or "#%s" % vmid, who, summary), "info",
               vmid=vmid, kind="ops", user=actor.get("name"))
    except Exception:
        log.exception("ops_journal: forwarding to the alerting service failed")


# ─── Portes d'écriture ────────────────────────────────────────────────────────────────────────

def record_post(vmid, manifest, endpoint, body, status, actor=None):
    """Après un POST relayé à un plugin. Ne fait rien si le plugin ne déclare pas ce endpoint, si
    le conteneur a refusé, ou si rien de DISCRET n'y figure. Ne lève jamais. Rend l'id de ligne."""
    try:
        if not ENABLED:
            return None
        try:
            if not (200 <= int(status or 0) < 300):
                return None
        except (TypeError, ValueError):
            return None
        sp = rules.spec(manifest)
        rule = (sp or {}).get("endpoints", {}).get(endpoint)
        if not rule or rule["mode"] == "structure":
            return None                            # structure : écrite par record_structure
        raw = rules.extract(rule, body, sp)        # chemin chaud : sort ici pour un fader
        if not raw:
            return None
        if actor is None:
            actor = current_actor()
        c, dc = _container_ctx(vmid)
        entries = rules.label_entries(raw, _persisted_doc(dc, sp), sp)
        target = "; ".join(e["path"] for e in entries if e.get("path"))
        details = {"entries": entries}
        return _insert(vmid, c, dc, actor, rule["category"], endpoint, target, details,
                       render_details(rule["category"], details, lang=_install_lang()))
    except Exception:
        log.exception("ops_journal: line for #%s %s not written", vmid, endpoint)
        return None


def record_public(vmid, link, endpoint, body, status):
    """Après une action relayée par une page publique JOUABLE (app/routes/pages.py,
    public_page_action) : TOUJOURS une ligne, que le plugin déclare `control.journal` ou non — une
    action sans compte est précisément celle dont il faut garder la trace. Au nom du lien. Ne lève jamais."""
    try:
        if not ENABLED:
            return None
        try:
            if not (200 <= int(status or 0) < 300):
                return None
        except (TypeError, ValueError):
            return None
        args = ", ".join("%s=%s" % (k, json.dumps(v, ensure_ascii=False))
                         for k, v in sorted((body or {}).items()) if not isinstance(v, (dict, list)))[:200]
        label = "%s%s" % (endpoint, (" (%s)" % args) if args else "")
        c, dc = _container_ctx(vmid)
        details = {"entries": [{"head": True, "label": label, "path": endpoint}], "public": True}
        return _insert(vmid, c, dc, public_actor(link), "command", endpoint, endpoint, details,
                       "public link %r: %s" % ((link or {}).get("title") or "", label))
    except Exception:
        log.exception("ops_journal: public action line for #%s %s not written", vmid, endpoint)
        return None


def record_structure(vmid, manifest, endpoint, old_doc, new_doc, actor=None):
    """Après une application de structure ACCEPTÉE : résumé des différences ancien → nouveau.
    Une application sans différence discrète est quand même notée (QUI l'a appliquée est un fait)."""
    try:
        if not ENABLED:
            return None
        sp = rules.spec(manifest)
        rule = (sp or {}).get("endpoints", {}).get(endpoint)
        if not rule or rule["mode"] != "structure":
            return None
        if actor is None:
            actor = current_actor()
        diff, omitted = rules.diff_docs(old_doc, new_doc, sp)
        c, dc = _container_ctx(vmid)
        details = {"diff": diff, "omitted": omitted,
                   "version": [(old_doc or {}).get("version"), (new_doc or {}).get("version")]}
        target = "; ".join(d["path"] for d in diff[:10])
        return _insert(vmid, c, dc, actor, rule["category"], endpoint, target, details,
                       render_details(rule["category"], details, lang=_install_lang()))
    except Exception:
        log.exception("ops_journal: structure line for #%s not written", vmid)
        return None


def record_event(vmid, event, plugin_type=None):
    """Événement interne publié par le plugin (`GET /events`, cf. app/ops_journal_events.py).
    Idempotent : la clé `boot:seq` est unique par instance (une relecture ne double rien)."""
    try:
        if not ENABLED:
            return None
        if not isinstance(event, dict) or not event.get("code"):
            return None
        c, dc = _container_ctx(vmid)
        # Clé = (boot, seq). `boot` nomme la SÉQUENCE persistée avec les événements sur le volume
        # d'état, pas le processus : un rechargement resert les mêmes événements sous la même clé
        # (et fait sauter `seq` pour les suivants). L'index unique ne joue pas sur une instance sans
        # `instance_uuid` (NULL ≠ NULL en SQLite) : la vérification explicite couvre ce cas.
        ext = "%s:%s" % (event.get("boot") or "-", event.get("seq"))
        if _db().execute("SELECT 1 FROM ops_journal WHERE ext_key = ? AND "
                         + ("instance_uuid = ?" if c.get("instance_uuid") else "instance_uuid IS NULL AND vmid = ?"),
                         (ext, c.get("instance_uuid") or vmid)).fetchone():
            return None
        try:
            ts = datetime.fromtimestamp(float(event.get("ts")) / 1000.0)
        except (TypeError, ValueError):
            ts = None
        details = {"event": {k: event.get(k) for k in ("code", "level", "subject", "label", "params", "n", "end")
                             if event.get(k) is not None}}
        actor, category, endpoint = plugin_actor(plugin_type or dc.get("type")), "event", "/events"
        native = _native_attribution(vmid, dc, event)
        if native is not None:
            actor, category, endpoint, details["event"]["params"] = native
        return _insert(vmid, c, dc, actor, category, endpoint,
                       event.get("subject"), details,
                       render_details("event", details, lang=_install_lang(), plugin_type=dc.get("type")),
                       ext_key=ext, ts=ts)
    except Exception:
        log.exception("ops_journal: event line for #%s not written", vmid)
        return None


NATIVE_ENDPOINT = "native-gui"


def _native_attribution(vmid, dc, event):
    """Un événement qui nomme une session d'INTERFACE NATIVE (`params.gui` = son tag, 2026-10-18) n'est pas un fait
    du plugin : c'est un réglage d'opérateur, fait dans l'interface du plugin LV2 ouverte par services/audio/native_gui.py, que
    la console a regroupé en UNE ligne par geste. L'AUTEUR est l'utilisateur qui a ouvert cette session, lu dans la
    table du contrôleur (jamais dans le conteneur ni la console, qui ne font que porter le tag). Tag inconnu
    (réplique HA sans la session, liaison purgée) : auteur « interface native » sans nom — on ne devine jamais un
    humain. → (auteur, catégorie, endpoint, params enrichis du nom lisible de l'élément) ou None."""
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    tag = params.get("gui")
    if not isinstance(tag, str) or not tag:
        return None
    try:
        from services.audio import native_gui
        who = native_gui.author(vmid, tag)
    except ImportError:          # audio service absent: no native interface session to read the author from
        who = None
    except Exception:
        log.exception("ops_journal: native interface author of #%s not read", vmid)
        who = None
    if who and who.get("username"):
        actor = {"kind": "user", "name": who["username"], "detail": NATIVE_ENDPOINT}
    else:
        actor = {"kind": "plugin", "name": None, "detail": NATIVE_ENDPOINT}
    where = event.get("subject") or ""
    try:
        from . import plugins
        sp = rules.spec(plugins.get(dc.get("type")))
        if sp and where:
            where = rules.label_entries([{"path": where}], _persisted_doc(dc, sp), sp)[0].get("label") or where
    except Exception:
        log.exception("ops_journal: element name of %r not resolved", where)
    p = dict(params, where=where)
    p.setdefault("value", "—")
    return actor, "setting", NATIVE_ENDPOINT, p


# ─── Rendu (langue du LECTEUR, à la lecture) ──────────────────────────────────────────────────

def _install_lang():
    try:
        from . import settings as st
        from . import i18n
        return st.get("ui_lang_default") or i18n.DEFAULT_LANG
    except Exception:
        return "fr"


def _t(key, lang, **kw):
    from . import i18n
    return i18n.t(key, lang=lang, **kw)


def _value(v, lang):
    if v is True:
        return _t("opsj.value.true", lang)
    if v is False:
        return _t("opsj.value.false", lang)
    return "—" if v is None else str(v)


def render_details(category, details, lang=None, plugin_type=None):
    """Phrase lisible d'une ligne, dans `lang`. Jamais une valeur numérique (il n'y en a pas)."""
    details = details or {}
    if "entries" in details:
        heads, parts = [], []
        for e in details["entries"]:
            label = e.get("label") or e.get("path")
            if e.get("head"):                  # mode « action » : la commande, libellé fixe
                heads.append(label)
                continue
            value = e["display"] if e.get("display") is not None else _value(e.get("value"), lang)
            parts.append(_t("opsj.entry", lang, label=label, value=value))
        if heads:
            return " ; ".join(heads) + ((" — " + " ; ".join(parts)) if parts else "")
        return " ; ".join(parts)
    if "diff" in details:
        parts = []
        for d in details["diff"]:
            op = d.get("op")
            if op == "renamed":
                parts.append(_t("opsj.diff.renamed", lang, old=d.get("from") or "—", new=d.get("to") or "—"))
            elif op in ("added", "removed"):
                parts.append(_t("opsj.diff." + op, lang, what=d.get("label") or d.get("path")))
            elif "to" in d and d.get("from") is None:      # ajout / retrait dans une liste d'ids
                parts.append(_t("opsj.diff.set", lang, what=d.get("label") or d.get("path"),
                                new=_value(d.get("to"), lang)))
            elif "to" in d:
                parts.append(_t("opsj.diff.changed", lang, what=d.get("label") or d.get("path"),
                                old=_value(d.get("from"), lang), new=_value(d.get("to"), lang)))
            else:
                parts.append(_t("opsj.diff.modified", lang, what=d.get("label") or d.get("path")))
        if not parts:
            return _t("opsj.structure.none", lang)
        s = _t("opsj.structure.head", lang) + " " + " ; ".join(parts)
        if details.get("omitted"):
            s += " " + _t("opsj.diff.more", lang, n=details["omitted"])
        return s
    if "event" in details:
        ev = details["event"]
        from . import i18n
        key = "plugin.%s.event.%s" % (plugin_type, ev.get("code")) if plugin_type else None
        params = {k: v for k, v in (ev.get("params") or {}).items()
                  if isinstance(k, str) and k not in ("key", "lang")}     # noms réservés de i18n.t
        params.setdefault("label", ev.get("label") or ev.get("subject") or "")
        if key:
            # Un champ que le gabarit nomme et que l'événement n'a pas (valeur d'un patch:Set non affichable,
            # ligne écrite par un orchestrateur plus ancien) vaut « — » : sinon i18n.t rend le gabarit brut, accolades
            # comprises.
            try:
                import string
                for _lit, field, _spec, _conv in string.Formatter().parse(i18n.t(key, lang=lang) or ""):
                    if field and field.isidentifier() and field not in ("key", "lang"):
                        params.setdefault(field, "—")
            except (ValueError, TypeError):
                pass
        txt = i18n.t(key, lang=lang, **params) if key else None
        if not txt or txt == key:
            txt = SEP_EVENT.join(str(x) for x in (ev.get("label") or ev.get("subject"), ev.get("code")) if x)
        if ev.get("n") and int(ev.get("n") or 1) > 1:
            txt += " " + _t("opsj.event.repeat", lang, n=ev["n"])
        return txt
    return ""


SEP_EVENT = " · "


def actor_text(actor, lang=None):
    a = actor or {}
    k, name, det = a.get("kind"), a.get("name") or "", a.get("detail") or ""
    if k == "user":
        return name or "?"
    if k == "macro":
        return _t("opsj.actor.macro", lang, macro=det or "?", user=name or "?")
    if k == "trigger":
        return _t("opsj.actor.trigger", lang, name=name or "?", macro=det or "?")
    if k == "surface":
        return _t("opsj.actor.surface", lang, surface=det or "?", user=name or "?")
    if k == "plugin":
        return _t("opsj.actor.plugin", lang, type=det or "?")
    if k == "public":
        return _t("opsj.actor.public", lang, link=name or "?")
    return _t("opsj.actor.auto", lang) + ((" (%s)" % det) if det else "")


def render_row(r, lang=None):
    r = dict(r)
    try:
        details = json.loads(r.get("details") or "{}")
    except ValueError:
        details = {}
    r["details"] = details
    try:
        r["summary_text"] = render_details(r.get("category"), details, lang=lang,
                                           plugin_type=r.get("plugin_type")) or r.get("summary") or ""
    except Exception:                    # une ligne mal formée ne doit pas casser toute la page
        log.exception("ops_journal: row %s not renderable, stored summary used", r.get("id"))
        r["summary_text"] = r.get("summary") or ""
    r["actor_text"] = actor_text({"kind": r.get("actor_kind"), "name": r.get("actor_name"),
                                  "detail": r.get("actor_detail")}, lang=lang)
    r["category_text"] = _t("opsj.cat." + str(r.get("category")), lang)
    return r


# ─── Lecture ──────────────────────────────────────────────────────────────────────────────────

def _where(f):
    w, a = [], []
    f = f or {}
    if f.get("instance_uuid"):
        w.append("instance_uuid = ?"); a.append(f["instance_uuid"])
    if f.get("vmid") is not None:
        w.append("vmid = ?"); a.append(int(f["vmid"]))
    if f.get("actor"):
        w.append("actor_name = ?"); a.append(f["actor"])
    if f.get("actor_kind"):
        w.append("actor_kind = ?"); a.append(f["actor_kind"])
    if f.get("category"):
        cats = [c for c in str(f["category"]).split(",") if c in rules.CATEGORIES]
        if cats:
            w.append("category IN (%s)" % ",".join("?" * len(cats))); a.extend(cats)
    if f.get("since"):
        w.append("ts >= ?"); a.append(str(f["since"]))
    if f.get("until"):
        w.append("ts <= ?"); a.append(str(f["until"]))
    if f.get("before_id"):
        w.append("id < ?"); a.append(int(f["before_id"]))
    if f.get("q"):
        w.append("(summary LIKE ? OR target LIKE ? OR instance_label LIKE ?)")
        a.extend(["%" + f["q"] + "%"] * 3)
    if f.get("project_ids") is not None:
        pids = [int(p) for p in f["project_ids"]]
        if not pids:
            w.append("0")
        else:
            w.append("project_id IN (%s)" % ",".join("?" * len(pids))); a.extend(pids)
    return (" WHERE " + " AND ".join(w)) if w else "", a


def query(filters=None, limit=500, lang=None):
    """Lignes filtrées, les plus récentes d'abord, rendues dans `lang`."""
    limit = max(1, min(int(limit or 500), QUERY_MAX))
    where, args = _where(filters)
    rows = _db().execute("SELECT * FROM ops_journal%s ORDER BY id DESC LIMIT ?" % where, args + [limit]).fetchall()
    return [render_row(dict(r), lang) for r in rows]


def facets(project_ids=None):
    """Valeurs proposées par les filtres : instances, opérateurs, types présents dans le journal."""
    where, args = _where({"project_ids": project_ids})
    db = _db()
    inst = [dict(r) for r in db.execute(
        "SELECT instance_uuid, MAX(id) AS last_id, plugin_type FROM ops_journal%s GROUP BY instance_uuid "
        "ORDER BY last_id DESC" % where, args)]
    for i in inst:
        r = db.execute("SELECT instance_label, vmid FROM ops_journal WHERE id = ?", (i["last_id"],)).fetchone()
        i["label"], i["vmid"] = (r["instance_label"], r["vmid"]) if r else (None, None)
    actors = [r[0] for r in db.execute(
        "SELECT DISTINCT actor_name FROM ops_journal%s%s actor_name IS NOT NULL ORDER BY actor_name"
        % (where, " AND" if where else " WHERE"), args)]
    cats = [r[0] for r in db.execute("SELECT DISTINCT category FROM ops_journal%s" % where, args)]
    return {"instances": inst, "actors": actors,
            "categories": [c for c in rules.CATEGORIES if c in cats], "all_categories": list(rules.CATEGORIES)}


def last_action(instance_uuid=None, project_id=None):
    w, a = [], []
    if instance_uuid:
        w.append("instance_uuid = ?"); a.append(instance_uuid)
    if project_id is not None:
        w.append("project_id = ?"); a.append(project_id)
    sql = "SELECT * FROM ops_journal%s ORDER BY id DESC LIMIT 1" % ((" WHERE " + " AND ".join(w)) if w else "")
    r = _db().execute(sql, a).fetchone()
    return render_row(dict(r), _install_lang()) if r else None


# ─── Macros : fournisseur du cœur (core_plugins.CORE_PROVIDERS) ───────────────────────────────

def _journaled_instances():
    """Conteneurs dont le plugin déclare `control.journal` : chacun a ses états « dernière action »."""
    from . import plugins
    from .database import db_get_containers
    out = []
    for c in db_get_containers() or []:
        try:
            dc = json.loads(c.get("deploy_config") or "{}") if isinstance(c.get("deploy_config"), str) \
                else (c.get("deploy_config") or {})
        except ValueError:
            continue
        m = plugins.get(dc.get("type"))
        if c.get("instance_uuid") and m and rules.spec(m):
            out.append(c)
    return out


_STATIC_STATES = [
    {"id": "last_action", "label": "Journal d'exploitation : dernière action (projet)", "type": "text"},
    {"id": "last_actor", "label": "Journal d'exploitation : auteur de la dernière action (projet)", "type": "text"},
]


def _states():
    """États DYNAMIQUES : deux par instance journalisée (action, auteur), adressés par
    `instance_uuid` (2e barreau : un vmid ne survit pas au recreate). Un état ne prend pas de
    paramètre dans le moteur de macros ; d'où un état par instance plutôt qu'un état à choisir."""
    out = list(_STATIC_STATES)
    try:
        for c in _journaled_instances():
            name = c.get("hostname") or "#%s" % c.get("vmid")
            out.append({"id": "last_action:" + c["instance_uuid"],
                        "label": "Journal · %s : dernière action" % name, "type": "text"})
            out.append({"id": "last_actor:" + c["instance_uuid"],
                        "label": "Journal · %s : auteur de la dernière action" % name, "type": "text"})
    except Exception:
        log.exception("ops_journal: macro states listing failed")
    return out


class _Provider(dict):
    """Manifeste de fournisseur dont la liste d'états est calculée à la lecture."""

    def get(self, k, default=None):
        return _states() if k == "states" else super().get(k, default)

    def __getitem__(self, k):
        return _states() if k == "states" else super().__getitem__(k)


MACRO_PROVIDER = _Provider({"id": "ops_journal", "label": "Journal d'exploitation", "actions": []})


def macro_available():
    return True


def read_state(state_id, ctx):
    pid = (ctx or {}).get("project_id")
    kind, _, inst = str(state_id).partition(":")
    if kind not in ("last_action", "last_actor"):
        return None
    r = last_action(instance_uuid=inst or None, project_id=pid)
    if not r:
        return None
    if kind == "last_actor":
        return r.get("actor_name") or r.get("actor_text")
    return "%s — %s" % (r.get("ts", "")[11:19], r.get("summary_text") or "")
