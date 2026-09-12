# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Rendu des pages Jinja (dashboard, containers, traitements, câbles, projets, réglages, aide)
+ les share links (page publique `/w/<token>` d'un flux WebRTC + son API de gestion), regroupés
ici car un share link EST une page (client externe, sans compte).

_attach_projects reste dans __init__.py (aussi utilisé par la route API générique
`liste_containers`) — importé localement dans les pages qui en ont besoin."""

import secrets

from flask import (jsonify, request, render_template, redirect, url_for, abort, Response,
                   send_file,
                   make_response)

from . import bp
from .shared import _load_dc
from ..auth import (require_login, require_perm, require_project_role, project_role_for,
                    has_global_access, current_user)
from ..i18n import rendre_alertes
from ..database import (db_get_containers, db_get_container, db_get_projects, db_get_alerts,
                      db_get_project,
                      db_create_share_link, db_get_share_link, db_list_share_links,
                      db_delete_share_link, db_set_share_link_cidrs, db_list_all_share_links,
                      db_delete_share_links_orphelins)


# ─── Espace projets (chantier 1, cf. docs/reference/PROJETS.md §12) ─────────

@bp.route("/workspaces")
@require_login
def workspaces_page():
    """Accueil projets : cartes des projets accessibles (atterrissage interface=projets)."""
    return render_template("workspaces.html", page="workspaces")

@bp.route("/workspace/<int:pid>")
@require_project_role("viewer")
def workspace_page(pid):
    """Espace de travail d'un projet (stub chantier 1 : résumé containers + monitoring ;
    les vues composables arrivent au chantier 2)."""
    proj = db_get_project(pid)
    if not proj:
        abort(404)
    return render_template("workspace.html", page="workspace", hide_topnav=True,
                           project={"id": proj["id"], "name": proj["name"]},
                           my_role=project_role_for(pid),
                           # accès global → section « Macros système » de l'overlay ⚡
                           # (édition des macros système via l'éditeur Scénario)
                           is_global=has_global_access())


@bp.route("/monitoring")
@require_login
def monitoring_page():
    return render_template("monitoring.html")

@bp.route("/aide")
@require_login
def aide_page():
    return render_template("aide.html")


@bp.route("/catalogue")
@require_perm("settings.edit")
def catalogue_page():
    """Redirige vers l'ONGLET catalogue des Réglages.

    Le catalogue a d'abord été une page autonome ; il est devenu un onglet, à sa
    place auprès de Plugins et Services. On garde l'URL et on redirige plutôt que
    de la supprimer : elle a pu être mise en favori, et un 404 sur une adresse qui
    marchait la veille n'apprend rien à personne.

    ⚠ `settings.edit`, PAS `require_login` : installer un plugin exécute son
    `hooks.py` dans l'orchestrateur. La porte d'entrée n'a pas à être plus ouverte
    que le geste qu'elle mène."""
    return redirect("/settings#catalogue")

@bp.route("/api/changelog", methods=["GET"])
@require_login
def api_changelog():
    """Lit CHANGELOG.md à la racine du projet et le rend en HTML. Renvoie aussi
    le mtime du fichier pour information."""
    import os
    try:
        import markdown as _md
    except ImportError:
        return jsonify({"ok": False, "error": "lib markdown non installée"}), 500
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "CHANGELOG.md")
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "CHANGELOG.md introuvable"}), 404
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    html = _md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    mtime = int(os.path.getmtime(path))
    return jsonify({"ok": True, "html": html, "mtime": mtime, "size": len(text)})

# Documents markdown VERSIONNÉS exposés à la page Aide. Liste BLANCHE volontaire : le nom vient
# de l'URL, on ne compose jamais un chemin avec une entrée non listée — les valeurs sont des
# chemins codés en dur, aucune traversée n'est possible.
# ⚠ Tout document ajouté ici doit être embarqué par `app/builder.py` (CORE_FILES pour la racine,
# CORE_DIRS pour `docs/`), sans quoi il donne un 404 sur toute instance installée alors que tout
# marche en dev.
_DOCS_RACINE = {
    "install": "INSTALL.md",
    "infrastructure": "INFRASTRUCTURE.md",
    "ha": "HA.md",
    "node-agent": "NODE_AGENT.md",
    "third-party": "THIRD-PARTY-NOTICES.md",
    "contributing": "CONTRIBUTING.md",
    # Hors racine : référence d'intégration de la MIB SNMP. C'est le document que lit
    # l'intégrateur du client pour brancher son système de supervision.
    "snmp-mib": "docs/reference/SNMP_MIB.md",
}

@bp.route("/api/doc/<name>", methods=["GET"])
@require_login
def api_doc(name):
    """Rend en HTML un document markdown versionné du projet (cf. _DOCS_RACINE).
    Même principe que /api/changelog, généralisé : la doc reste un fichier versionné,
    lisible sur GitHub, et la page Aide l'affiche depuis cette SEULE source."""
    import os
    fichier = _DOCS_RACINE.get((name or "").lower())
    if not fichier:
        return jsonify({"ok": False, "error": "document inconnu"}), 404
    try:
        import markdown as _md
    except ImportError:
        return jsonify({"ok": False, "error": "lib markdown non installée"}), 500
    racine = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(racine, fichier)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"{fichier} introuvable"}), 404
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    html = _md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    return jsonify({"ok": True, "html": html, "mtime": int(os.path.getmtime(path)),
                    "size": len(text), "fichier": fichier})

@bp.route("/")
@require_login
def home():
    # Dashboard data-driven : tout est fetch en JS via /api/home/summary
    resp = make_response(render_template("home.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@bp.route("/containers")
@require_login
def containers_page():
    from . import _attach_projects
    from ..database import db_get_nodes
    conts = _attach_projects(db_get_containers())
    # Options du filtre par nœud : les nœuds RÉELLEMENT porteurs d'un conteneur (pas juste la
    # table `nodes` — des conteneurs peuvent référencer un nœud absent de la table, ex. Horace).
    noms = {n["id"]: (n.get("name") or f"Nœud {n['id']}") for n in db_get_nodes()}
    ids = sorted({c.get("node_id") for c in conts if c.get("node_id")})
    noeuds = [{"id": i, "name": noms.get(i, f"Nœud {i}")} for i in ids]
    return render_template("containers.html",
        containers=conts,
        projets=db_get_projects(),
        noeuds=noeuds,
        node_names=noms,          # node_id → nom, pour l'affichage « Nœud » par tuile
        alerts=rendre_alertes(db_get_alerts()))

@bp.route("/traitements")
@require_login
def traitements_index():
    from .plugin_registry import _render_plugin_section
    return _render_plugin_section("traitements", "Traitements")

@bp.route("/composition")
@require_login
def composition_index():
    """Ce qui ASSEMBLE plusieurs signaux (mixer, multiview, split, pyramide), séparé de ce qui en
    TRANSFORME un seul. Ces quatre-là sont aussi les plus lourds du parc et les seuls à avoir des
    pages dédiées : les laisser dans Traitements noyait quatre outils de routine (correcteur, UDC,
    pont v210, délai) sous les quatre plus gros."""
    from .plugin_registry import _render_plugin_section
    return _render_plugin_section("composition", "Composition")


@bp.route("/mesure")
@require_login
def mesure_index():
    """Ce qui OBSERVE sans transformer. Le critère n'est pas le nombre d'entrées — le scope en a
    six, le correcteur une — mais le fait que le signal d'entrée ne RESSORT PAS : la sortie est un
    rendu de mesure. Un scope rangé dans « Traitements » laissait croire qu'il était dans la chaîne."""
    from .plugin_registry import _render_plugin_section
    return _render_plugin_section("mesure", "Mesure")


# Compat anciennes URLs /traitements/<key> (favoris) → onglet #type du shell unifié.
# La rubrique Traitements est désormais une page unique à onglets (hash).
_TRAITEMENT_KEY_TO_TYPE = {
    "multiview": "multiview", "melangeurs": "mixer", "correcteurs": "color_corrector",
    "split": "split", "udc": "udc", "delay": "delay",
}
@bp.route("/traitements/<key>")
@require_login
def traitements_legacy(key):
    """⚠ La rubrique de destination est LUE DANS LE REGISTRE, pas supposée « traitements ».
    Quatre de ces types ont déménagé vers Composition le 2026-08-30 ; un 301 câblé en dur sur
    /traitements aurait envoyé les favoris (« /traitements/multiview ») sur une page où l'onglet
    n'existe plus — une redirection qui ment est pire qu'un 404, elle affiche une page plausible."""
    t = _TRAITEMENT_KEY_TO_TYPE.get(key, key)
    return redirect(_route_du_type(t) + "#" + t, code=301)


def _route_du_type(type_):
    """Route de la rubrique qui HÉBERGE ce type aujourd'hui, /traitements par défaut."""
    from .. import plugins
    m = plugins.get(type_) or {}
    sec = ((m.get("nav") or {}).get("section")) or "traitements"
    return "/" + sec

# ─── Redirect compat ancien /multiview → /traitements/multiview ────
@bp.route("/multiview")
@require_login
def multiview_page():
    return redirect("/traitements#multiview", code=301)

@bp.route("/labels")
@require_login
def labels_page():
    return render_template("labels.html")

@bp.route("/tsl/sources")
def tsl_sources_redirect():
    return redirect("/labels", code=301)

@bp.route("/cables")
@require_login
def cables_page():
    resp = make_response(render_template("cables.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@bp.route("/streams")
@require_login
def streams_page():
    return redirect("/io#streamer", code=301)

# ─── Share links : page publique client pour un flux WebRTC ──────────────────
# Un jeton aléatoire non devinable (`secrets.token_urlsafe`) → URL `/w/<token>`
# (la seule route publique ajoutée). Révocable (delete). Le jeton protège la PAGE,
# pas le flux MediaMTX lui-même (qui reste diffusé sur la passerelle).

def _streamer_webrtc_path(vmid):
    """(container_dict, path) du 1er dest WebRTC activé d'un streamer, ou (c, None)."""
    c = db_get_container(vmid)
    if not c:
        return None, None
    dc = _load_dc(c)
    if not dc or dc.get("type") != "streamer":
        return c, None
    from ..scripts import normalize_worker_udp_params
    params = normalize_worker_udp_params(dc.get("params") or {})
    for d in params.get("destinations") or []:
        if d.get("type") == "webrtc" and d.get("enabled") and (d.get("path") or "").strip():
            return c, d["path"].strip()
    return c, None

def _share_url(token):
    return request.host_url.rstrip("/") + url_for("routes.public_watch", token=token)

def _share_public(link):
    # ⚠ `cidrs` EN FAIT PARTIE. Sans lui, la réponse de création ne dit pas la restriction
    # RÉELLEMENT appliquée : celui qui vient de restreindre un lien ne peut pas vérifier que
    # ça a pris, et c'est exactement le moment où il veut en être sûr.
    return {"token": link["token"], "title": link.get("title") or "",
            "note": link.get("note") or "", "created_at": link.get("created_at"),
            "cidrs": link.get("cidrs") or "",
            "url": _share_url(link["token"])}

@bp.route("/api/streams/<int:vmid>/share", methods=["GET"])
@require_login
def api_share_list(vmid):
    return jsonify([_share_public(l) for l in db_list_share_links(vmid)])

@bp.route("/api/streams/<int:vmid>/share", methods=["POST"])
@require_login
def api_share_create(vmid):
    body = request.get_json(force=True, silent=True) or {}
    c, path = _streamer_webrtc_path(vmid)
    if not c:
        return jsonify({"error": f"container #{vmid} introuvable"}), 404
    if not path:
        return jsonify({"error": "aucune destination WebRTC activée sur cet encodeur"}), 400
    token = secrets.token_urlsafe(16)   # 128 bits → non devinable / non énumérable
    title = (body.get("title") or "").strip() or (c.get("hostname") or path)
    note = (body.get("note") or "").strip()
    db_create_share_link(token, vmid, path, title, note)
    return jsonify(_share_public(db_get_share_link(token)))

@bp.route("/api/share/<token>", methods=["DELETE"])
@require_login
def api_share_delete(token):
    return jsonify({"deleted": db_delete_share_link(token)})

@bp.route("/w/<token>")
def public_watch(token):
    """PAGE PUBLIQUE (aucun @require_login) : lecteur WHEP brandé pour un flux."""
    link = db_get_share_link(token)
    if not link:
        abort(404)
    _langue_du_visiteur()      # même raison que pour `/p/` : le visiteur n'a pas de compte
    from .. import settings as _st
    enabled = _st.get("webrtc_enabled")
    gw_ip = _st.get("webrtc_gateway_ip")
    http_p = int(_st.get("webrtc_http_port") or 8889)
    path = link["path"]
    whep_url = embed_url = None
    if enabled and gw_ip and path:
        whep_url = f"http://{gw_ip}:{http_p}/{path}/whep"
        embed_url = f"http://{gw_ip}:{http_p}/{path}"
    return render_template("public_watch.html",
                           title=link.get("title") or path,
                           note=link.get("note") or "",
                           whep_url=whep_url, embed_url=embed_url)

# ─── Lien public d'une PAGE DE PLUGIN, en lecture seule ──────────────────────
# ★ MÉCANISME D'ORCHESTRATEUR, PAS CAPACITÉ D'UN PLUGIN. Rien ici ne connaît le scope : la
# page publique monte l'UI DU PLUGIN du conteneur visé, quel qu'il soit. Écrire ce mécanisme
# dans un plugin aurait garanti qu'on le recopie au suivant — c'est déjà arrivé quatre fois
# avec l'éditeur de layout, et le prix est écrit dans TODO.md.
#
# ★ POURQUOI LA PAGE ET PAS LE WEBRTC. `/w/<token>` existe déjà et sert un flux réencodé :
# parfait pour montrer une image, destructeur pour un instrument de mesure, dont les tracés
# fins sont précisément ce que l'encodeur jette. Ici la page reçoit les données et les dessine.
#
# ⚠ LE RELAIS EST EN LECTURE SEULE, ET C'EST SA RAISON D'ÊTRE. Trois verrous : la méthode (GET
# seul), le chemin (uniquement ce que le manifeste déclare dans `control.read_endpoints`), et
# le conteneur (celui du jeton, jamais un autre). Un relais qui accepterait `control.endpoints`
# au complet laisserait recâbler l'entrée d'un instrument de régie depuis un lien envoyé par
# courriel.
#
# ⚠ ET LE PLUGIN DOIT DÉCLARER QU'IL SAIT LE FAIRE (`ui.public_page`). Sa console doit honorer
# une BASE d'API différente ; celle qui ne le fait pas appellerait l'API privée depuis la page
# publique et échouerait en 401, sans rien expliquer à qui a reçu le lien. Mieux vaut refuser
# de créer le lien que d'en livrer un qui ne marche pas.

def _ip_client_pour_filtre():
    """L'adresse à confronter à la liste blanche d'un lien public.

    ⚠ CE CHOIX EST LA SÉCURITÉ ELLE-MÊME, et il n'est pas évident. `ProxyFix` est monté avec
    `x_proto=1, x_host=1` — mais `x_for` vaut **1 par défaut**, donc `request.remote_addr` est
    déjà réécrit depuis `X-Forwarded-For`. Cet en-tête est envoyé par le CLIENT : si
    l'orchestrateur est joignable en direct, n'importe qui peut s'annoncer à l'adresse de son
    choix, et un filtre bâti dessus ne filtrerait RIEN tout en ayant l'air de filtrer. C'est
    la pire forme de garde-fou.

    Par défaut on prend donc le VRAI pair TCP, que Werkzeug conserve dans
    `werkzeug.proxy_fix.orig` — inforgeable. Le réglage `public_trust_proxy` bascule sur
    l'adresse transmise, et il n'a de sens QUE si l'orchestrateur n'est joignable qu'à travers
    un reverse-proxy de confiance : sinon on rouvre soi-même la porte qu'on croit fermer."""
    from .. import settings as _st
    if _st.get("public_trust_proxy"):
        return request.remote_addr or ""
    orig = (request.environ.get("werkzeug.proxy_fix.orig") or {})
    return orig.get("REMOTE_ADDR") or request.remote_addr or ""


def _ip_autorisee(link):
    """(autorisée, adresse vue). Une liste VIDE n'autorise pas « rien » : elle n'autorise
    AUCUNE restriction — c'est le comportement des liens créés avant ce réglage, et le faire
    basculer aurait coupé des liens en service sans prévenir."""
    ip = _ip_client_pour_filtre()
    regles = [x.strip() for x in str(link.get("cidrs") or "").replace(";", ",").split(",")
              if x.strip()]
    if not regles:
        return True, ip
    import ipaddress
    try:
        adr = ipaddress.ip_address(ip)
    except ValueError:
        return False, ip          # pas d'adresse lisible → on refuse, on ne devine pas
    for r in regles:
        try:
            # Une adresse seule est acceptée comme un réseau /32 (ou /128) : écrire
            # « x.x.x.x » doit marcher sans que personne ait à savoir ce qu'est un CIDR.
            if adr in ipaddress.ip_network(r, strict=False):
                return True, ip
        except ValueError:
            continue              # règle illisible : ignorée, jamais interprétée « au mieux »
    return False, ip


def _cidrs_valides(txt):
    """Nettoie une saisie de règles. Rend (texte normalisé, liste des règles refusées)."""
    import ipaddress
    bonnes, mauvaises = [], []
    for x in str(txt or "").replace(";", ",").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ipaddress.ip_network(x, strict=False)
            bonnes.append(x)
        except ValueError:
            mauvaises.append(x)
    return ", ".join(bonnes), mauvaises


def _langue_du_visiteur():
    """Fixe la langue de CETTE requête depuis l'en-tête `Accept-Language`, et la rend.

    ★ SUR UNE PAGE PUBLIQUE, LA LANGUE DU SYSTÈME N'EST PAS LA BONNE. L'interface interne suit
    la préférence de chaque utilisateur, ce qui est juste — mais un lien public s'ouvre
    justement chez quelqu'un qui n'a pas de compte, et parfois hors de la maison. Il recevait
    la langue par défaut de l'installation quoi qu'il arrive.

    ⚠ LE DÉFAUT DU SYSTÈME EST EN TÊTE DES CANDIDATS, et ce n'est pas un détail : une
    installation réglée sur une VARIANTE (`fr-bob`) doit garder sa variante pour un visiteur
    francophone, pas se faire ramener au français standard par la négociation. L'ordre de la
    liste tranche les égalités chez Werkzeug.

    Aucun en-tête, ou aucune langue connue → on retombe sur le comportement d'avant."""
    from ..i18n import LANG_CODES, current_lang
    from flask import g
    defaut = current_lang()
    candidats = [defaut] + [c for c in sorted(LANG_CODES) if c != defaut]
    choix = request.accept_languages.best_match(candidats) if request.accept_languages else None
    g.lang_forcee = choix or defaut
    return g.lang_forcee


def _page_publique_ok(c):
    """(manifeste, erreur) : le conteneur peut-il avoir une page publique ?"""
    from .. import plugins
    dc = _load_dc(c) if c else None
    m = plugins.get((dc or {}).get("type")) if dc else None
    if not m:
        return None, "ce conteneur n'est pas un plugin"
    if not ((m.get("ui") or {}).get("public_page")):
        return None, ("le plugin %s ne déclare pas de page publique (ui.public_page)"
                      % m.get("type"))
    return m, None


def _container_du_lien(link):
    """Le conteneur désigné par un lien, par son IDENTITÉ D'INSTANCE.

    ⚠ PAS PAR LE VMID, ET C'EST UNE CORRECTION DE SÉCURITÉ. Le vmid est un handle local et
    jetable : réattribué à un autre conteneur, il ferait ouvrir la page de CET AUTRE conteneur
    par un vieux jeton — un accès sans identification qui se déplace tout seul sur une autre
    machine. L'identité d'instance, elle, n'est jamais réattribuée et survit
    recreate/restore/import (cf. CLAUDE.md, « Identité d'un conteneur : trois barreaux »).

    Corollaire voulu : un conteneur détruit puis RECRÉÉ dans un projet garde son identité, donc
    son lien continue de fonctionner. C'est le même appareil."""
    uuid = (link.get("instance_uuid") or "").strip()
    if not uuid:
        return None                    # lien d'avant la migration, conteneur disparu : orphelin
    for c in db_get_containers():
        if (c.get("instance_uuid") or "") == uuid:
            return c
    return None


def _lien_page(token):
    """(lien, container, manifeste) d'un jeton de PAGE, ou (None, None, None)."""
    link = db_get_share_link(token)
    if not link or (link.get("kind") or "webrtc") != "page":
        return None, None, None
    c = _container_du_lien(link)
    if not c:
        return None, None, None
    m, err = _page_publique_ok(c)
    if err:
        return None, None, None
    return link, c, m


@bp.route("/api/containers/<int:vmid>/share_page", methods=["GET"])
@require_login
def api_page_share_list(vmid):
    out = []
    for l in db_list_share_links(vmid):
        if (l.get("kind") or "webrtc") != "page":
            continue
        d = _share_public(l)
        d["cidrs"] = l.get("cidrs") or ""
        d["url"] = request.host_url.rstrip("/") + url_for("routes.public_page",
                                                          token=l["token"])
        out.append(d)
    return jsonify(out)


@bp.route("/api/containers/<int:vmid>/share_page", methods=["POST"])
@require_login
def api_page_share_create(vmid):
    body = request.get_json(force=True, silent=True) or {}
    c = db_get_container(vmid)
    if not c:
        return jsonify({"error": f"container #{vmid} introuvable"}), 404
    m, err = _page_publique_ok(c)
    if err:
        return jsonify({"error": err}), 400
    cidrs, refusees = _cidrs_valides(body.get("cidrs"))
    if refusees:
        return jsonify({"error": "adresses illisibles : " + ", ".join(refusees)}), 400
    token = secrets.token_urlsafe(16)     # 128 bits : non devinable, non énumérable
    db_create_share_link(token, vmid, m["type"],
                         (body.get("title") or "").strip() or (c.get("hostname") or m["type"]),
                         (body.get("note") or "").strip(), kind="page", cidrs=cidrs,
                         instance_uuid=c.get("instance_uuid") or "")
    d = _share_public(db_get_share_link(token))
    d["url"] = request.host_url.rstrip("/") + url_for("routes.public_page", token=token)
    return jsonify(d)


@bp.route("/api/share/<token>/cidrs", methods=["POST"])
@require_login
def api_share_cidrs(token):
    """Restreint (ou libère) un lien public à une liste d'adresses ou de réseaux."""
    body = request.get_json(force=True, silent=True) or {}
    cidrs, refusees = _cidrs_valides(body.get("cidrs"))
    if refusees:
        return jsonify({"error": "adresses illisibles : " + ", ".join(refusees)}), 400
    if not db_set_share_link_cidrs(token, cidrs):
        return jsonify({"error": "lien introuvable"}), 404
    return jsonify({"token": token, "cidrs": cidrs})


@bp.route("/api/share/all", methods=["GET"])
@require_login
def api_share_all():
    """TOUS les liens publics du système, pour la page Réglages. Un accès sans identification
    qu'on ne voit nulle part est un accès qu'on oublie."""
    out = []
    for l in db_list_all_share_links():
        d = _share_public(l)
        d["kind"] = l.get("kind") or "webrtc"
        d["cidrs"] = l.get("cidrs") or ""
        d["vmid"] = l["vmid"]
        # ⚠ L'ORPHELINAT SE JUGE SUR L'IDENTITÉ D'INSTANCE. Le juger sur le vmid ferait passer
        # pour vivant un lien dont le vmid a simplement été REPRIS par un autre conteneur —
        # c'est-à-dire exactement le cas dangereux.
        c = _container_du_lien(l)
        d["hostname"] = (c or {}).get("hostname")
        d["vmid_actuel"] = (c or {}).get("vmid")
        d["url"] = request.host_url.rstrip("/") + (
            url_for("routes.public_page", token=l["token"]) if d["kind"] == "page"
            else url_for("routes.public_watch", token=l["token"]))
        out.append(d)
    return jsonify(out)


@bp.route("/api/share/orphelins", methods=["DELETE"])
@require_login
def api_share_purge_orphelins():
    """Supprime tous les liens dont aucun conteneur ne porte plus l'identité d'instance.

    ⚠ ON NE LE FAIT PAS AUTOMATIQUEMENT À LA DESTRUCTION D'UN CONTENEUR. Détruit puis RECRÉÉ
    dans un projet, il garde son identité et son lien doit continuer de fonctionner — c'est le
    même appareil, et révoquer serait le contraire de ce que l'exploitant attend. Le ménage est
    donc une action VOULUE, jamais un effet de bord."""
    n = db_delete_share_links_orphelins()
    return jsonify({"supprimes": int(n)})


@bp.route("/p/<token>")
def public_page(token):
    """PAGE PUBLIQUE d'un plugin (aucun @require_login) : sa console, en lecture seule."""
    from .. import settings as _st
    from ..i18n import js_catalog, current_lang
    link, c, m = _lien_page(token)
    if not link:
        abort(404)
    _langue_du_visiteur()      # AVANT tout rendu : `_()` la lit dans `g` (cf. i18n.current_lang)
    # (le filtre d'adresses est appliqué juste après, avant tout rendu)
    ok, ip = _ip_autorisee(link)
    if not ok:
        # ⚠ CETTE PAGE EST LA SEULE CHOSE QUE VERRA SON DESTINATAIRE, et il n'est probablement
        # pas celui qui administre. Elle dit donc que ce n'est pas cassé (c'est restreint),
        # donne l'ADRESSE VUE — exactement celle qu'il faut faire autoriser — et nomme le site
        # à qui la demander. Un « 403 Forbidden » nu fait chercher la panne au mauvais endroit.
        # Elle ne révèle rien que le porteur du jeton n'ait déjà : le titre du lien et sa
        # propre adresse. Ni nom de conteneur, ni topologie.
        return render_template("public_refus.html", adresse=ip or "?",
                               titre=link.get("title") or "",
                               lang=current_lang(),
                               brand_logo=_st.get("brand_logo_url") or "",
                               brand_system=_st.get("brand_system_name") or "",
                               brand_org=_st.get("brand_org_name") or "",
                               brand_location=_st.get("brand_location") or ""), 403
    return render_template("public_page.html",
                           token=token, vmid=link["vmid"], type=m["type"],
                           base="/p/" + token,
                           title=link.get("title") or c.get("hostname") or m["type"],
                           note=link.get("note") or "",
                           adresse=request.host,
                           brand_logo=_st.get("brand_logo_url") or "",
                           brand_system=_st.get("brand_system_name") or "",
                           brand_org=_st.get("brand_org_name") or "",
                           brand_location=_st.get("brand_location") or "",
                           lang=current_lang(), js_catalog=js_catalog())


@bp.route("/p/<token>/ui/<asset>")
def public_page_ui(token, asset):
    """Fragments d'UI du plugin (gabarit, script, style), servis PAR LE JETON.

    ⚠ SANS CETTE ROUTE, LA PAGE PUBLIQUE NE CHARGE RIEN. `/api/plugins/<type>/ui/…` exige une
    session — c'est juste, ce sont les sources de l'interface — et la page publique n'en a pas.
    Elle rendait donc un cadre vide, sans message : la console ne se montait jamais et rien ne
    disait pourquoi. Attrapé en essayant le lien sans session, pas en lisant le code.

    On ne sert que les trois fragments de la console, et seulement pour le TYPE du conteneur du
    jeton : un jeton ne doit pas devenir un droit de lecture sur l'interface de tout le parc."""
    from .. import plugins
    link, _c, m = _lien_page(token)
    if not link:
        abort(404)
    ok, _ip = _ip_autorisee(link)
    if not ok:
        abort(403)
    cle = {"html": "control_html", "js": "control_js", "css": "control_css"}.get(asset, asset)
    if cle not in ("control_html", "control_js", "control_css"):
        abort(404)
    chemin = plugins.ui_asset_path(m["type"], cle)
    if not chemin:
        abort(404)
    ext = chemin.rsplit(".", 1)[-1].lower()
    mime = {"html": "text/html", "js": "application/javascript",
            "css": "text/css"}.get(ext, "application/octet-stream")
    return send_file(chemin, mimetype=mime)


@bp.route("/p/<token>/plugin/<path:p>")
def public_page_proxy(token, p):
    """Relais LECTURE SEULE vers le contrôle :8082 du conteneur désigné par le jeton."""
    from ..addressing import get_container_ip
    import requests as _req
    link, _c, m = _lien_page(token)
    if not link:
        abort(404)
    # Le filtre d'adresses vaut AUSSI pour le relais, et pas seulement pour la page : sans ça
    # l'adresse interdite n'aurait pas la page mais garderait les données, ce qui revient à
    # n'avoir aucun filtre pour qui sait lire une URL.
    ok, ip = _ip_autorisee(link)
    if not ok:
        return jsonify({"error": "adresse %s non autorisée pour ce lien" % (ip or "?")}), 403
    # ⚠ LA LISTE BLANCHE EST CELLE DU MANIFESTE, pas une copie locale. Une copie finirait par
    # diverger, et c'est du côté PERMISSIF qu'elle divergerait : on ajoute un endpoint de
    # lecture au plugin et on oublie d'en retirer un d'écriture ici.
    lecture = set(((m.get("control") or {}).get("read_endpoints")) or [])
    if ("/" + p) not in lecture:
        return jsonify({"error": "lecture seule : /%s n'est pas un endpoint de lecture" % p}), 403
    ip = get_container_ip(link["vmid"])
    if not ip:
        return jsonify({"error": "container injoignable"}), 502
    try:
        r = _req.get(f"http://{ip}:8082/{p}", params=request.args, timeout=5)
    except Exception as e:                                          # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    if r.status_code == 204:
        return ("", 204)
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/json"))


@bp.route("/compte")
@require_login
def compte_page():
    """Fiche du compte courant : identité, mot de passe, et ce qui ne se change pas soi-même.

    ⚠ ELLE MANQUAIT, ET LE MANQUE ÉTAIT DOUBLE. Changer son mot de passe n'était possible que
    depuis Réglages → Utilisateurs, sous la liste de TOUS les comptes — le dernier endroit où
    on cherche ses propres réglages. Et changer son e-mail n'était pas possible du tout : il
    fallait déranger un administrateur pour corriger une faute de frappe dans sa propre
    adresse."""
    # ⚠ L'ÉTAT DU MONITOR EST LU CÔTÉ SERVEUR, PAS PAR `/api/monitor/status`. Cette route-là
    # appelle `monitor.touch()` — le battement de cœur qui empêche le faucheur d'arrêter
    # l'encodeur après dix minutes. Une page de compte qui l'interroge MAINTIENDRAIT donc en
    # vie un encodeur que personne ne regarde, en consommant un conteneur et du CPU sur un
    # nœud. On lit sans réveiller.
    from .. import monitor
    u = current_user() or {}
    try:
        mon = monitor.status(u.get("id"))
    except Exception:                                               # noqa: BLE001
        mon = None
    # Les exigences de mot de passe viennent du SERVEUR, jamais recopiées dans le gabarit :
    # sinon changer de profil dans Réglages laisserait la page annoncer les anciens seuils.
    from ..auth import pwd_exigences
    return render_template("compte.html", monitor=mon, pwd_exigences=pwd_exigences())


@bp.route("/projects")
@require_login
def projects_page():
    from . import _attach_projects
    return render_template("projects.html",
        containers=_attach_projects(db_get_containers()),
        projects=db_get_projects())

@bp.route("/backup")
@require_login
def backup_page():
    # Redirige vers l'onglet Database de la page Réglages
    return redirect(url_for("routes.settings_page") + "#database")

@bp.route("/settings")
@require_login
def settings_page():
    from .. import core_plugins, settings as _st
    # Services explicitement désactivés (ont une clé _enabled = False)
    disabled = set()
    for entry in core_plugins.scan().values():
        m = entry["manifest"]
        key = next((k for k in m.get("settings_keys", {}) if k.endswith("_enabled")), None)
        if key and not _st.get(key):
            disabled.add(m["id"])
    tabs = []
    for tab in core_plugins.tab_groups():
        if tab.get("sub_tabs") is not None:
            subs = [s for s in tab["sub_tabs"] if s["id"] not in disabled]
            if subs:
                tabs.append({**tab, "sub_tabs": subs})
        else:
            if tab.get("id") not in disabled:
                tabs.append(tab)
    # L'organisation du catalogue est affichée, jamais saisie : elle vient du code
    # (`config.CATALOGUE_ORG`). La passer au gabarit évite un « {org} » brut à
    # l'écran avant la première lecture de l'API.
    from ..catalogue import _reglages as _cat_reglages
    return render_template("settings.html", core_tabs=tabs,
                           timezones=_timezones_par_region(),
                           catalogue_org=_cat_reglages()[0],
                           controls_inventory=_inventaire_controles(),
                           controls_overrides=_surcharges_catalogue())


@bp.route("/api/timezones", methods=["GET"])
@require_login
def api_timezones():
    """Fuseaux IANA disponibles, groupés par région. Sert les sélecteurs de fuseau (éditeur de
    modèles de PiP, page Réglages). Lecture seule, aucun secret → login simple suffit."""
    return jsonify({"groups": [{"region": r, "zones": z} for r, z in _timezones_par_region()]})


# Catalogue des VARIABLES DE TEXTE des multiviews. Défini ICI, en un seul endroit, et servi aux
# DEUX éditeurs (modèles de PiP dans les Réglages, composeur de mur dans la page Traitements) :
# la liste était sinon dupliquée dans deux fichiers JS qui auraient divergé au premier ajout.
# ⚠ Reste à tenir synchrone avec _TEXT_VARS / _SRC_VARS de plugins/multiview/script.py, qui est
# la source de vérité du RENDU.
_TEXT_VARIABLES = {
    "system": [
        ("conteneur", "Nom du conteneur"), ("systeme", "Nom du système"),
        ("noeud", "Nom du nœud"), ("mur", "Nom du mur"),
        ("version", "Version du plugin"), ("format", "Format de sortie"),
        ("heure", "Heure locale"), ("date", "Date"), ("fuseau", "Fuseau horaire"),
        ("cpu", "CPU du conteneur"), ("ram", "Mémoire utilisée"),
        ("fps", "Cadence mesurée"), ("entrees", "Nombre d'entrées câblées"),
        ("duree", "Durée depuis le démarrage"),
    ],
    # NŒUD, RDMA et CONTRÔLEUR — trois groupes distincts plutôt qu'un fourre-tout « infra ».
    # Un menu se parcourt à la souris : quinze entrées d'affilée se lisent mal, trois listes de
    # cinq se choisissent. Le découpage suit la question posée, pas l'implémentation : « comment va
    # la machine », « comment vont les liens », « comment va le contrôleur ».
    # Un conteneur ne voit que son cgroup : ces valeurs lui sont POUSSÉES par l'orchestrateur
    # (deploy.pousser_telemetrie), qui les échantillonne déjà pour la page Monitoring.
    "noeud": [
        ("nom_noeud", "Nom du nœud"), ("cpu_noeud", "CPU"), ("ram_noeud", "Mémoire (%)"),
        ("ram_noeud_mo", "Mémoire (Mo)"), ("disque_noeud", "Disque"),
        ("temp_noeud", "Température"), ("charge_noeud", "Charge (load1)"),
    ],
    "rdma": [
        ("rdma_pct", "Remplissage des liens"), ("rdma_rx", "Trafic entrant"),
        ("rdma_tx", "Trafic sortant"), ("rdma_debit", "Débit nominal"),
        ("rdma_liens", "Ports actifs"),
    ],
    "orchestrateur": [
        ("cpu_orch", "CPU"), ("ram_orch", "Mémoire"), ("disque_orch", "Disque"),
    ],
    "source": [
        ("src", "Source — nom"), ("src_flux", "Source — flux"),
        ("src_format", "Source — format"), ("src_fps", "Source — cadence"),
        ("src_scan", "Source — balayage"), ("src_colorimetrie", "Source — colorimétrie"),
        ("src_audio", "Source — flux audio"), ("src_projet", "Source — projet"),
    ] + [("src_label%d" % n, "Source — libellé niveau %d" % n) for n in range(2, 10)],
}


@bp.route("/api/text-variables", methods=["GET"])
@require_login
def api_text_variables():
    """Variables insérables dans un champ texte de multiview, par groupe. `source` n'a de sens
    que dans un composant de MODÈLE (rendu par cellule) : un overlay de mur n'a pas UNE source,
    et le composeur ne propose donc que les groupes `system` et `infra`.

    Les variables `infra` acceptent une CIBLE : `%cpu_noeud%` parle du nœud qui porte le mur,
    `%cpu_noeud:dl360-1%` du nœud nommé. De quoi faire un mur de supervision qui affiche tout le
    parc, sans dupliquer une variable par nœud."""
    # Libellés TRADUITS dans la langue du lecteur : ce catalogue est relu à chaque ouverture du
    # composeur, on peut donc le rendre par requête. Le nom de la variable (%cpu%) ne bouge JAMAIS —
    # c'est lui que le conteneur cherche dans le texte, le traduire casserait tous les habillages.
    from ..i18n import t as _t
    def _lbl(nom, defaut):
        cle = "vars.src_label" if nom.startswith("src_label") else "vars." + nom
        v = _t(cle)
        if v == cle:
            return defaut
        return v.replace("{n}", nom[len("src_label"):]) if nom.startswith("src_label") else v
    return jsonify({k: [{"name": n, "label": _lbl(n, l)} for n, l in v]
                    for k, v in _TEXT_VARIABLES.items()})


def _timezones_par_region():
    """Fuseaux IANA disponibles, GROUPÉS par région → [(région, [noms…]), …]. Liste lue de la
    tzdata RÉELLEMENT installée (`zoneinfo.available_timezones`) et non codée en dur : c'est la
    seule qui garantit qu'un choix de l'utilisateur sera applicable par le process. Le groupement
    par région existe parce qu'une liste plate de ~600 entrées est inutilisable à la souris."""
    try:
        from zoneinfo import available_timezones
        noms = sorted(available_timezones())
    except Exception:
        return []
    groupes = {}
    for n in noms:
        if n in ("UTC", "localtime"):
            region = "UTC"
        else:
            region = n.split("/", 1)[0] if "/" in n else "Autres"
        groupes.setdefault(region, []).append(n)
    # UTC en tête (choix explicite fréquent en broadcast), puis les régions par ordre alphabétique.
    ordre = (["UTC"] if "UTC" in groupes else []) + sorted(k for k in groupes if k != "UTC")
    return [(r, groupes[r]) for r in ordre]


def _surcharges_catalogue():
    """Règles d'un plugin qui ÉCRASENT en silence un contrôle du catalogue.

    Poser `.ctl-select` sur une liste ne suffit pas : une règle locale plus spécifique — un
    `.avs-f select` ou un `.split-ui .field select` — l'annule sans erreur ni avertissement.
    Le raccourci `background` est le cas type : il réinitialise `background-image`, donc efface
    la flèche du catalogue, et la liste cesse d'annoncer qu'elle a des options. Deux plugins
    l'avaient, dont un que je venais de migrer.

    Ce contrôle est fait AU RENDU de la page Contrôles, jamais au démarrage : ces fichiers ne
    changent qu'au développement, les relire à chaque boot serait payer en permanence pour une
    information figée.

    Ce qui est signalé : une règle visant `select`, `input[type=checkbox]` ou une classe `.ctl-*`,
    ET déclarant une propriété d'APPARENCE. La GÉOMÉTRIE reste légitime — la mise en page d'un
    plugin lui appartient, seule l'apparence appartient au catalogue.

    Il SIGNALE, il ne bloque pas : le plugin fonctionne, il est seulement mal habillé, et une
    liste courte qu'on relit vaut mieux qu'un blocage qu'on contourne. Il ne voit pas non plus
    les surcharges venues d'ailleurs que du plugin (base.css) — d'où « à relire », pas « faux »."""
    import glob
    import os
    import re
    APPARENCE = ("background", "background-image", "background-color", "appearance",
                 "-webkit-appearance", "border", "border-radius", "border-color",
                 "padding", "font-family", "color", "box-shadow")
    racine = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = []
    for css in sorted(glob.glob(os.path.join(racine, "plugins", "*", "control.css"))):
        plug = os.path.basename(os.path.dirname(css))
        try:
            with open(css, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            continue
        # Les commentaires portent souvent le NOM du piège (« ne pas remettre select ici ») :
        # les garder produirait un signalement sur la mise en garde elle-même.
        sans_com = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", sans_com):
            sel, corps = m.group(1).strip(), m.group(2)
            if not sel or sel.startswith("@"):
                continue
            # Ne juger que la CIBLE — le compound le plus à droite de chaque sélecteur.
            # `.cc-knob.ctl-mixed .cc-knob-value` ne vise pas un contrôle du catalogue : il s'en
            # sert comme QUALIFICATEUR pour styler un élément privé, ce qui est légitime. Tester
            # le sélecteur entier signalait ces cas et noyait les vrais dans le bruit.
            def _vise(part):
                cible = re.split(r"[\s>+~]+", part.strip())[-1]
                return bool(re.match(r"select\b", cible) or cible.startswith("select") or
                            re.search(r"(^|[.:#\[])select\b", cible) or
                            re.search(r'input\[type=["\']?checkbox', cible) or
                            ".ctl-" in cible)
            if not any(_vise(part) for part in sel.split(",")):
                continue
            props = [d.split(":", 1)[0].strip() for d in corps.split(";") if ":" in d]
            fautives = sorted({p for p in props if p in APPARENCE})
            if not fautives:
                continue
            ligne = sans_com[:m.start()].count("\n") + 1
            out.append({"plugin": plug, "ligne": ligne,
                        "selecteur": " ".join(sel.split())[:120],
                        "proprietes": fautives})
    return out


def _inventaire_controles():
    """Classes du catalogue (static/css/controls.css), lues DANS LE FICHIER.

    L'inventaire de la page Réglages → Contrôles ne doit pas être une liste tenue à la main :
    elle divergerait du catalogue au premier ajout, et la page finirait par mentir sur ce qui
    existe. On lit donc les sélecteurs `.ctl-*` à la source, et la page signale tout composant
    du catalogue qu'elle ne montre pas. Fichier illisible → inventaire vide, et la page le dit :
    jamais un silence."""
    import os
    import re
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "static", "css", "controls.css")
    try:
        with open(chemin, encoding="utf-8") as f:
            src = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning(
            "Inventaire des contrôles illisible (%s) : %s", chemin, e)
        return {"ok": False, "classes": [], "adoption": {}, "orphelins": []}
    classes = sorted(set(re.findall(r"\.(ctl-[a-z0-9-]+)", src)))
    adoption, orphelins = _adoption_controles(classes)
    return {"ok": True, "classes": classes, "adoption": adoption, "orphelins": orphelins}


# Familles de contrôle reconnaissables au NOM. Sert à repérer, dans le CSS privé d'un plugin, ce
# qui ressemble à un contrôle et devrait donc vivre au catalogue. Volontairement large : mieux vaut
# proposer une migration de trop, que l'exploitant écarte, qu'en rater une en silence.
_FAMILLES_CTL = {
    "knob": ("knob", "dial", "rotary", "encoder"),
    "push": ("push", "poussoir", "lamp", "led", "latch"),
    "meter": ("meter", "bargraph", "vu", "level"),
    "readout": ("readout", "display", "seg7", "digit", "value"),
    "strip": ("strip", "tranche", "channel"),
    "bus": ("bus", "pgm", "pvw", "tally"),
}


def _adoption_controles(classes):
    """Qui utilise quoi, et ce qui vit encore en privé.

    Renvoie (adoption, orphelins) :
      · adoption  {classe du catalogue → [plugins qui l'emploient]}, lu dans leurs control.html/js ;
      · orphelins [{plugin, classe, famille}] : classes de contrôle DÉFINIES en privé dans le CSS
        d'un plugin. C'est la liste de ce qu'on perdrait en centralisant sans regarder — elle doit
        être VUE et arbitrée, jamais supposée équivalente à une entrée du catalogue.

    Tout est relu dans les fichiers à chaque affichage : aucune liste tenue à la main, donc rien
    qui puisse diverger en silence du code réel."""
    import glob
    import os
    import re
    racine = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    adoption = {c: [] for c in classes}
    orphelins = []
    for css in sorted(glob.glob(os.path.join(racine, "plugins", "*", "control.css"))):
        plug = os.path.basename(os.path.dirname(css))
        try:
            src_css = re.sub(r"/\*.*?\*/", "", open(css, encoding="utf-8").read(), flags=re.S)
            usage = ""
            for autre in ("control.html", "control.js"):
                chem = os.path.join(os.path.dirname(css), autre)
                if os.path.exists(chem):
                    usage += open(chem, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for c in classes:
            if re.search(r"[\"'\s]%s[\"'\s]" % re.escape(c), usage):
                adoption[c].append(plug)
        _orphelins_du_css(orphelins, plug, src_css)
    # Les plugins ne sont pas les seuls consommateurs : des pages entières du produit sont rendues
    # par des scripts partagés (les onglets I/O 2110, le panneau de modèle de carte TX). Tant qu'on
    # ne lisait QUE plugins/*/control.js, l'inventaire les ignorait — une classe employée par la
    # plus grosse vue du parc pouvait s'y afficher « aucun plugin », et une migration entière
    # n'apparaissait nulle part. On les compte donc, sous leur nom de fichier pour qu'on voie d'un
    # coup d'œil que ce n'est pas un plugin. On ne descend PAS dans `static/js/` : c'est là que vit
    # `controls.js`, qui DÉFINIT le catalogue — l'y compter ferait croire que tout est adopté.
    for js in sorted(glob.glob(os.path.join(racine, "static", "*.js"))):
        nom = os.path.basename(js)
        try:
            src = open(js, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for c in classes:
            if re.search(r"[\"'\s]%s[\"'\s]" % re.escape(c), src):
                adoption[c].append(nom)
    return adoption, orphelins


def _orphelins_du_css(orphelins, plug, src_css):
    """Contrôles encore DÉFINIS en privé dans le CSS d'un plugin, regroupés par composant.

    On regroupe par COMPOSANT, pas par classe. `.cc-knob`, `.cc-knob-dial`, `.cc-knob-label` et
    `.cc-knob-value` sont UN rotatif éclaté en quatre classes : les lister séparément donne quatre
    vignettes dont aucune ne ressemble à un contrôle, et un arbitrage qui n'a pas de sens (on ne
    remplace pas un `-dial`, on remplace le rotatif)."""
    import re
    trouvees = []
    for cls in dict.fromkeys(re.findall(r"^\.([a-zA-Z][-a-zA-Z0-9_]*)", src_css, re.M)):
        if cls.startswith("ctl-"):
            continue
        bas = cls.lower()
        fam = next((f for f, mots in _FAMILLES_CTL.items() if any(m in bas for m in mots)), None)
        if fam:
            trouvees.append((cls, fam))
    for racine_cls, fam, parts in _grouper_composants(trouvees):
        orphelins.append({"plugin": plug, "classe": racine_cls, "famille": fam,
                          "parts": parts, **_apercu_composant(src_css, racine_cls, parts)})


def _grouper_composants(trouvees):
    """[(classe, famille)] → [(racine, famille, [classes du composant])].

    La racine est la classe la plus COURTE dont les autres sont des extensions (`cc-knob` pour
    `cc-knob-dial`). Une classe qui n'étend rien est son propre composant."""
    restant = sorted(trouvees, key=lambda t: (len(t[0]), t[0]))
    groupes, pris = [], set()
    for cls, fam in restant:
        if cls in pris:
            continue
        parts = [c for c, _ in restant if c == cls or c.startswith(cls + "-")]
        pris.update(parts)
        groupes.append((cls, fam, parts))
    return groupes


# Propriétés écartées de l'aperçu : elles ne décrivent pas l'ASPECT du contrôle et casseraient la
# mise en page de la vignette (un `position:absolute` sortirait de son cadre, un `width:100%`
# mangerait la ligne). L'aperçu montre à quoi ressemble la classe, pas où elle se place.
_APERCU_EXCLU = ("position", "top", "right", "bottom", "left", "z-index", "float", "grid-area",
                 "margin", "flex", "align-self", "justify-self", "order", "inset")


def _apercu_composant(src_css, racine, parts):
    """CSS d'aperçu d'un COMPOSANT entier (sa racine et toutes ses parties), scopé sur un bac.

    Chaque règle qui mentionne l'une des classes du composant est PRÉFIXÉE par le sélecteur du
    bac. Les noms de classe sont conservés tels quels à l'intérieur : le rendu peut donc utiliser
    le vrai balisage (racine + parties), et les relations parent-enfant du CSS d'origine
    fonctionnent. Rien ne fuit pour autant, puisque tout sélecteur émis commence par le bac.

    Les blocs @media sont écartés : leur contenu dépend de la largeur d'écran, hors sujet ici."""
    import re

    def selecteurs(cibles):
        mot = re.compile(r"(?<![-\w])\.(?:%s)(?![-\w])" % "|".join(re.escape(c) for c in cibles))
        out = []
        for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", src_css):
            sel, decls = m.group(1).strip(), m.group(2).strip()
            if not sel or not mot.search(sel):
                continue
            gardes = [p.strip() for p in sel.split(",") if mot.search(p)]
            if gardes:
                out.append((gardes, decls))
        return out

    # 1er passage : ce que le composant déclare. On y récolte aussi ce dont son rendu DÉPEND —
    # sans quoi une grille comme `.mx-bus-btns` s'affiche vide, puisque tout son contenu visible
    # est porté par des classes d'un AUTRE composant (`.nkk-btn`), et un `.mx-bus-label` reste
    # transparent parce que sa couleur vient d'un modificateur (`.pgm`).
    modifs, enfants = set(), set()
    for gardes, _ in selecteurs(parts):
        for sel in gardes:
            for c in parts:
                for m in re.finditer(r"\.%s((?:\.[-\w]+)+)" % re.escape(c), sel):
                    modifs.update(x for x in m.group(1).split(".") if x)
            for jeton in re.findall(r"\.([-\w]+)", sel):
                if jeton not in parts and not jeton.startswith("ctl-"):
                    enfants.add(jeton)
    enfants -= modifs
    etendu = list(parts) + sorted(enfants)

    regles, principales = [], []
    for gardes, decls in selecteurs(etendu):
        # L'ancêtre ÉTRANGER au composant (`.mx-panel .mx-bus-label`) n'existe pas dans le bac :
        # la règle serait inerte. On tronque le sélecteur à partir de la 1ʳᵉ classe du composant,
        # ce qui la rend applicable. L'aperçu montre alors le contrôle hors de son contexte —
        # c'est précisément ce qu'on veut voir ici.
        coupes = []
        for sel in gardes:
            m = re.search(r"(?<![-\w])\.(?:%s)(?![-\w])" % "|".join(re.escape(c) for c in etendu), sel)
            coupes.append(sel[m.start():] if m else sel)
        regles.append(("__PREV__ " + ", __PREV__ ".join(coupes), decls))
        if any(re.fullmatch(r"\." + re.escape(racine), p) for p in coupes):
            principales.append(decls)
    if not regles:
        return {"css": "", "regles": "", "modifs": [], "enfants": []}
    return {"css": "\n".join("%s { %s }" % (s, d) for s, d in regles),
            "regles": " ".join(" ".join(principales).split()),
            "modifs": sorted(modifs), "enfants": sorted(enfants)}


def _apercu_regle(src_css, cls):
    """CSS d'aperçu d'une classe privée, RÉÉCRIT pour être rendu dans un bac isolé.

    1ʳᵉ tentative (abandonnée) : appliquer la seule règle principale en style inline. Ça ne
    montrait quasiment rien, et pour une raison structurelle — la moitié de ces classes sont des
    CONTENEURS (`display:flex; gap:20px`), et l'autre porte son aspect dans des pseudo-éléments,
    des enfants ou des états. Un style inline ne rend aucun des trois.

    2ᵉ approche : on collecte TOUTES les règles qui mentionnent la classe (`.cls`, `.cls::before`,
    `.cls .enfant`, `.cls[aria-pressed]`…) et on remplace `.cls` par un sélecteur d'aperçu unique.
    Le rendu devient fidèle, ET reste cloisonné : rien ne peut s'appliquer hors du bac, puisque
    plus aucun sélecteur ne porte le nom d'origine. C'est ce qui permet de ne PAS charger la
    feuille du plugin — huit CSS globaux dans les Réglages recréeraient les collisions de noms
    qu'on vient de supprimer.

    Ce qui échappe encore : les règles qui dépendent d'un ANCÊTRE du plugin (`.mx-panel .cls`),
    qui n'existe pas dans le bac. Elles sont conservées telles quelles et simplement inertes."""
    import re
    motif = re.compile(r"(?<![-\w])\." + re.escape(cls) + r"(?![-\w])")
    regles, principales = [], []
    # Balayage des règles de premier niveau : `sélecteur { déclarations }`. Les blocs @media sont
    # ignorés (leur contenu est conditionnel à la taille d'écran, hors sujet pour une vignette).
    for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", src_css):
        sel, decls = m.group(1).strip(), m.group(2).strip()
        if not sel or not motif.search(sel):
            continue
        parts = [p.strip() for p in sel.split(",") if motif.search(p)]
        if not parts:
            continue
        regles.append((", ".join(parts), decls))
        if any(re.fullmatch(r"\." + re.escape(cls), p) for p in parts):
            principales.append(decls)
    if not regles:
        return {"css": "", "regles": "", "conteneur": False}
    brut = " ".join(" ".join(principales).split())
    # Un conteneur pur (flex/grid sans fond ni bordure ni taille) n'a rien à MONTRER : la page le
    # signalera plutôt que d'afficher un carré vide en laissant croire à un défaut de rendu.
    conteneur = (("display:flex" in brut.replace(" ", "") or "display:grid" in brut.replace(" ", ""))
                 and not re.search(r"background|border|width|height|box-shadow", brut))
    css = "\n".join("%s { %s }" % (motif.sub("__PREV__", s), d) for s, d in regles)
    return {"css": css, "regles": brut, "conteneur": conteneur}
