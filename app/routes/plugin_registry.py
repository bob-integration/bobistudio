# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des plugins (liste/drift, import/export .mxlplugin, versions, activation/
désactivation/suppression, redéploiement) + shell générique de rubrique (Traitements/Médias/
I/O) + proxy de contrôle générique `/api/containers/<vmid>/plugin/<path>` vers :8082, seul
point d'entrée du contrôle live d'un plugin (aucun code plugin n'est exécuté in-process).

_render_plugin_section est aussi appelé par `traitements_index` (page Pages, __init__.py) —
importé localement là-bas, résolu à l'exécution d'une requête."""

import json
import os
import re
import threading

from flask import jsonify, request, render_template, send_file, Response

from . import bp
from .shared import _load_dc, _mixer_proxy
from ..auth import (require_login, require_perm, has_perm, check_vmid_access,
                    scoped_project_ids, current_user, vmid_project_ids)
from ..database import db_get_containers, db_get_container, db_get_projects, db_add_alert
from ..deploy import deployer_script


def _plugin_public(m):
    """Manifeste sans champs internes, sérialisable pour le client."""
    return {k: v for k, v in m.items() if not k.startswith("_")}

def _render_plugin_section(section_id, default_label):
    """Shell unifié de rubrique de plugins (Traitements, Médias, …) : onglets par type
    + sidebar d'instances + UI de contrôle. Un seul template `plugin_section.html`."""
    from .. import plugins
    section = plugins.sections().get(section_id) or {"label": default_label, "plugins": []}
    return render_template("plugin_section.html", page=section_id,
                           section_id=section_id,
                           section_label=section.get("label", default_label),
                           plugins=[_plugin_public(m) for m in section.get("plugins", [])])

@bp.route("/medias")
@require_login
def medias_page():
    return _render_plugin_section("medias", "Médias")

def _render_io():
    """Page I/O = shell unifié `plugin_section.html` avec une rubrique AGRÉGÉE des
    sections sources + streams + destinations (onglets par type : 2110_io,
    avsync, streamer)."""
    from .. import plugins
    secs = plugins.sections()
    pl = []
    for sid in ("sources", "streams", "destinations"):
        for m in (secs.get(sid) or {}).get("plugins", []):
            # Le moteur MTL (2110_io) a ses ONGLETS DÉDIÉS « Sources 2110 » + « Destinations 2110 »
            # (RX et TX) ci-dessous. On retire donc son onglet plugin générique, qui ne montrait que
            # les sources (RX) = doublon exact de « Sources 2110 » (même carte de contrôle réutilisée).
            if m.get("type") == "2110_io":
                continue
            pl.append(_plugin_public(m))
    # Onglets CUSTOM (hors logique plugin) : gestion du transport 2110 du moteur MTL (RX + TX).
    extra_tabs = []
    if plugins.get("2110_io"):
        from ..i18n import t as _t
        extra_tabs = [{"id": "sources_2110", "label": _t("io.tab.sources_2110")},
                      {"id": "destinations_2110", "label": _t("io.tab.destinations_2110")}]
    return render_template("plugin_section.html", page="io",
                           section_id="io", section_label="I/O", plugins=pl,
                           extra_tabs=extra_tabs)

@bp.route("/io")
@require_login
def io_page():
    return _render_io()

@bp.route("/api/plugins/<type_>/ui/<asset>", methods=["GET"])
@require_login
def plugin_ui_asset(type_, asset):
    """Sert un fragment UI déclaré dans le manifeste.
    Clés standard : html→control_html, js→control_js, css→control_css.
    Clés arbitraires (ex. extra_js, extra_css) : cherchées directement dans manifest.ui.
    Paramètre optionnel ?vmid=<vmid> : sert la version archivée correspondant au container
    (fallback sur dossier plat si l'archive est script-only ou absente)."""
    from .. import plugins
    from ..database import get_db
    STANDARD = {"html": "control_html", "js": "control_js", "css": "control_css"}
    key = STANDARD.get(asset, asset)
    version = None
    vmid = request.args.get("vmid")
    if vmid:
        try:
            db = get_db()
            row = db.execute("SELECT deploy_config FROM containers WHERE vmid=?", (int(vmid),)).fetchone()
            if row and row["deploy_config"]:
                cfg = json.loads(row["deploy_config"])
                version = (cfg.get("params") or {}).get("plugin_version")
        except Exception:
            pass
    path = plugins.ui_asset_path(type_, key, version=version)
    if not path:
        return jsonify({"error": "asset introuvable"}), 404
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"html": "text/html", "js": "application/javascript", "css": "text/css"}.get(ext, "application/octet-stream")
    return send_file(path, mimetype=mime)

@bp.route("/api/plugins/instances", methods=["GET"])
@require_login
def plugin_instances():
    """Containers dont le type déployé est un plugin : {vmid, hostname, status, type, version}.
    Optionnel ?type=player pour filtrer."""
    from .. import plugins
    from ..database import db_fabric_all
    want = (request.args.get("type") or "").strip() or None
    proj_by_id = {p["id"]: p for p in db_get_projects()}
    # Nœuds INTERNES du tissu de composition (shards `bobi-fab-*`) : ce sont des multiviews
    # matérialisés automatiquement (parallélisme/dédup), pas des murs éditables → on les masque
    # de la liste d'instances. Les ASSEMBLEURS (kind=assembler) sont les vrais murs (vmid =
    # le mur utilisateur) → conservés. Un shard a vmid=NULL et porte le vmid du conteneur en `ref`.
    fab_shard_vmids = set()
    try:
        for r in db_fabric_all():
            if r["kind"] == "shard" and r["ref"]:
                fab_shard_vmids.add(int(r["ref"]))
    except Exception:
        pass
    member_pids = scoped_project_ids()   # None = accès global (pas de filtre)
    uid = (current_user() or {}).get("id")
    out = []
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        t = dc.get("type")
        if not plugins.is_plugin(t):
            continue
        if want and t != want:
            continue
        if c["vmid"] in fab_shard_vmids:
            continue
        if member_pids is not None and not (vmid_project_ids(c["vmid"]) & member_pids) \
                and c.get("monitor_user_id") != uid:
            continue
        pid = c.get("project_id")
        proj = proj_by_id.get(pid) if pid else None
        out.append({
            "vmid": c["vmid"], "hostname": c.get("hostname"),
            "status": c.get("status"), "type": t, "ip": c.get("ip"),
            "version": (dc.get("params") or {}).get("plugin_version"),
            "source": c.get("source"), "shm_out": c.get("shm_out"),
            "project": {"id": proj["id"], "name": proj["name"]} if proj else None,
        })
    return jsonify(out)

def _plugins_overview():
    """Liste des plugins (paquets) + drift, en UNE passe sur les containers.
    Pour chaque plugin chargé : version, rubrique, nb d'instances, nb périmées (version
    déployée ≠ version du manifeste), et la liste des instances périmées. `errors` =
    plugins présents sur disque mais non chargés (raison)."""
    from .. import plugins
    # Projets par VMID : un container est « dans » un projet si son vmid figure dans le
    # snapshot du projet (identité sauvegardée). Construit en une passe.
    proj_by_vmid = {}
    try:
        for p in db_get_projects():
            for snap_c in (p.get("snapshot") or []):
                v = snap_c.get("vmid")
                if v is not None:
                    proj_by_vmid.setdefault(v, []).append(p.get("name"))
    except Exception:
        proj_by_vmid = {}
    # Instances groupées par type (parse unique des deploy_config).
    by_type = {}
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        t = dc.get("type")
        if not (t and plugins.is_plugin(t)):
            continue
        by_type.setdefault(t, []).append(
            (c, (dc.get("params") or {}).get("plugin_version")))
    out = []
    for t, m in sorted(plugins.REGISTRY.items()):
        ver = m.get("version")
        insts = by_type.get(t, [])
        instances = [
            {"vmid": c["vmid"], "hostname": c.get("hostname"),
             "deployed_version": dv, "outdated": dv != ver,
             "deployed_at": c.get("deployed_at"),
             "projects": sorted(set(proj_by_vmid.get(c["vmid"], [])))}
            for (c, dv) in insts
        ]
        nav = m.get("nav") or {}
        # Catégorie d'affichage = nav.section si présent, sinon `category` explicite
        # (types hors palette comme webrtc_gateway → "streams" sans émettre de chip).
        category = nav.get("section") or m.get("category")
        out.append({
            "type": t, "label": m.get("label", t), "version": ver,
            "versions": plugins.versions(t),
            "versions_meta": plugins.versions_meta(t),
            "disabled": plugins.is_disabled(t),
            "section": category, "description": m.get("description", ""),
            "has_nav": bool(nav.get("section")),
            "has_config_schema": bool(m.get("config_schema")),
            "control_endpoints": (m.get("control") or {}).get("endpoints") or [],
            "n_instances": len(instances),
            "n_outdated": sum(1 for i in instances if i["outdated"]),
            "instances": instances,
        })
    errors = [{"name": n, "reason": r} for n, r in sorted(plugins.scan_errors().items())]
    return {"plugins": out, "errors": errors}

@bp.route("/api/plugins", methods=["GET"])
@require_perm("settings.edit")
def plugins_list():
    return jsonify(_plugins_overview())

@bp.route("/api/plugins/help", methods=["GET"])
@require_login
def plugins_help():
    """Agrège les help.md de tous les plugins installés.
    Retourne une liste d'articles [{type, label, category, order, html, lang}]
    triée par catégorie puis order.

    ★ UN FICHIER PAR LANGUE, AVEC REPLI. Un plugin peut poser `help.<code>.md` à
    côté de son `help.md` ; on sert celui de la langue courante s'il existe,
    sinon `help.md`. Sans ce mécanisme, un plugin ne pouvait avoir SA doc que
    dans une seule langue — et un plugin destiné à être publié la voulait en
    anglais, ce qui la rendait anglaise pour tout le monde, y compris dans une
    interface française.

    Le repli est SILENCIEUX mais pas invisible : l'article porte `lang`, la
    langue réellement servie. Une aide affichée dans une autre langue que celle
    de l'interface doit pouvoir se DIRE, sinon on croit à un défaut de
    traduction du produit plutôt qu'à une doc que personne n'a encore traduite."""
    import markdown as _md
    from .. import plugins as _pl
    from ..i18n import current_lang
    lang = current_lang()
    articles = []
    for manifest in (_pl.all() or []):
        type_ = manifest.get("type") or ""
        if not type_:
            continue
        plugin_dir = manifest.get("_dir") or os.path.join(_pl.PLUGINS_DIR, type_)
        # ⚠ `lang` VIENT D'UNE REQUÊTE. Un code de langue est un fragment de
        # chemin : sans ce filtre, un `lang` fabriqué remonterait l'arborescence.
        # Les codes valides sont dans `i18n.LANG_CODES`, mais on ne s'appuie pas
        # dessus ici — la garde doit tenir même si une langue est ajoutée.
        code = re.sub(r"[^a-z0-9_-]", "", str(lang or "").lower())[:8]
        help_path = os.path.join(plugin_dir, "help.md")
        lang_servie = ""
        if code:
            p_lang = os.path.join(plugin_dir, "help.%s.md" % code)
            if os.path.isfile(p_lang):
                help_path, lang_servie = p_lang, code
        if not os.path.isfile(help_path):
            continue
        try:
            with open(help_path, encoding="utf-8") as f:
                md_text = f.read()
            # Le wrapper d'article rend déjà le titre (label plugin) → retirer le « # Titre »
            # de tête du markdown pour éviter un double <h1>.
            md_text = re.sub(r"^\s*#\s+[^\n]*\n", "", md_text, count=1)
            html = _md.markdown(md_text, extensions=["tables", "fenced_code"])
        except Exception as e:
            html = f"<p><em>Erreur de rendu : {e}</em></p>"
        help_meta = manifest.get("help") or {}
        nav = manifest.get("nav") or {}
        articles.append({
            "type":     type_,
            "label":    manifest.get("label") or type_,
            "version":  manifest.get("version") or "",
            "category": help_meta.get("category") or nav.get("section") or "autres",
            "order":    int(help_meta.get("order") or nav.get("order") or 99),
            "html":     html,
            # "" = le `help.md` générique a servi, la langue est donc inconnue.
            "lang":     lang_servie,
        })
    articles.sort(key=lambda a: (a["category"], a["order"], a["label"]))
    return jsonify(articles)

@bp.route("/api/plugins/reload", methods=["POST"])
@require_perm("settings.edit")
def plugins_reload():
    """Re-scan à chaud des dossiers plugins/ ET services/ (sans redémarrer le service)."""
    from .. import plugins, core_plugins
    plugins.reload()
    core_plugins.reload()
    db_add_alert("alert.deploy.registres_recharges", "info", kind="deploy")
    return jsonify(_plugins_overview())

@bp.route("/api/plugins/<type_>/redeploy", methods=["POST"])
@require_perm("containers.deploy")
def plugins_redeploy(type_):
    """Redéploie les containers d'un type plugin (par défaut seulement ceux en drift),
    pour les passer à la version courante du manifeste. Async (un thread par container)."""
    from .. import plugins
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    only_outdated = bool((request.json or {}).get("only_outdated", True))
    ver = (plugins.get(type_) or {}).get("version")
    queued = []
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        if dc.get("type") != type_:
            continue
        if only_outdated and (dc.get("params") or {}).get("plugin_version") == ver:
            continue
        params = dc.get("params") or {}
        threading.Thread(target=deployer_script, args=(c["vmid"], type_, params)).start()
        queued.append(c["vmid"])
    db_add_alert("alert.deploy.redeploiement_type", "info", kind="deploy",
                 params={"t": type_, "n": len(queued)})
    return jsonify({"queued": queued})

@bp.route("/api/plugins/redeploy-all", methods=["POST"])
@require_perm("containers.deploy")
def plugins_redeploy_all():
    """Redéploie TOUS les containers de type plugin en drift (toutes versions confondues),
    pour purger le drift d'un coup. Async (un thread par container)."""
    from .. import plugins
    only_outdated = bool((request.json or {}).get("only_outdated", True))
    queued = []
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        t = dc.get("type")
        if not (t and plugins.is_plugin(t)):
            continue
        ver = (plugins.get(t) or {}).get("version")
        if only_outdated and (dc.get("params") or {}).get("plugin_version") == ver:
            continue
        params = dc.get("params") or {}
        threading.Thread(target=deployer_script, args=(c["vmid"], t, params)).start()
        queued.append(c["vmid"])
    db_add_alert("alert.deploy.redeploiement_global", "info", kind="deploy",
                 params={"n": len(queued)})
    return jsonify({"queued": queued})

@bp.route("/api/containers/<int:vmid>/redeploy-version", methods=["POST"])
@require_perm("containers.deploy")
def container_redeploy_version(vmid):
    """Redéploie UN container plugin à une version précise, en réutilisant ses params
    existants (la config est préservée ; seule la version du script change)."""
    from .. import plugins
    c = db_get_container(vmid)
    if not c:
        return jsonify({"error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c) or {}
    t = dc.get("type")
    if not (t and plugins.is_plugin(t)):
        return jsonify({"error": f"#{vmid} n'est pas un container plugin"}), 400
    version = (request.json or {}).get("version") or None
    params = dc.get("params") or {}
    threading.Thread(target=deployer_script, kwargs={
        "vmid": vmid, "type_script": t, "params": params, "version": version}).start()
    if version:
        db_add_alert("alert.deploy.redeploiement_version", "info", vmid=vmid, kind="deploy",
                     params={"vmid": vmid, "t": t, "v": version})
    else:
        db_add_alert("alert.deploy.redeploiement_version_courante", "info", vmid=vmid, kind="deploy",
                     params={"vmid": vmid, "t": t})
    return jsonify({"queued": [vmid]})

@bp.route("/api/plugins/<type_>/export", methods=["GET"])
@require_perm("settings.edit")
def plugins_export(type_):
    """Télécharge le dossier complet du plugin (flat + versions/) en .mxlplugin (zip)."""
    import io, zipfile, os as _os
    from .. import plugins
    d = plugins.export_dir(type_)
    if not d:
        return jsonify({"error": "type inconnu"}), 404
    ver = (plugins.get(type_) or {}).get("version", "0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in _os.walk(d):
            for fn in files:
                full = _os.path.join(root, fn)
                z.write(full, _os.path.relpath(full, d))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{type_}-{ver}.mxlplugin")

@bp.route("/api/plugins/<type_>/versions/<version>/export", methods=["GET"])
@require_perm("settings.edit")
def plugins_export_version(type_, version):
    """Télécharge UNE version du plugin (script.py + meta.json + assets de cette version) en
    .mxlversion (zip). Pour la version courante (dossier plat), exclut le sous-dossier versions/."""
    import io, zipfile, os as _os
    from .. import plugins
    d, ver = plugins.export_version_dir(type_, version)
    if not d:
        return jsonify({"error": "version inconnue"}), 404
    flat = (d == (plugins.get(type_) or {}).get("_dir"))  # version courante → dossier plat
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in _os.walk(d):
            if flat and "versions" in dirs:
                dirs.remove("versions")  # n'embarque pas les autres versions
            for fn in files:
                full = _os.path.join(root, fn)
                z.write(full, _os.path.relpath(full, d))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{type_}-{ver}.mxlversion")

def _extract_validated_package(raw, tmp):
    """Extrait un paquet plugin (zip) dans `tmp` (anti zip-slip strict), tolère un dossier
    racine unique englobant, puis valide. Retourne (root, manifest, None) ou (None, None, err).
    Aucun code plugin n'est exécuté (lecture + dry-run str.format)."""
    import io, zipfile, shutil as _sh, os as _os
    from .. import plugins
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None, None, "fichier invalide (pas un zip)"
    base = _os.path.realpath(tmp)
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        dest = _os.path.realpath(_os.path.join(tmp, info.filename))
        if not (dest == base or dest.startswith(base + _os.sep)):
            return None, None, f"archive rejetée (chemin suspect : {info.filename})"
        _os.makedirs(_os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            _sh.copyfileobj(src, out)
    # Tolère un dossier racine unique englobant le plugin.
    root = tmp
    if not _os.path.isfile(_os.path.join(root, "plugin.json")):
        subs = [d for d in _os.listdir(root) if _os.path.isdir(_os.path.join(root, d))]
        if len(subs) == 1 and _os.path.isfile(_os.path.join(root, subs[0], "plugin.json")):
            root = _os.path.join(root, subs[0])
    man, err = plugins.validate_package(root)
    if err:
        return None, None, err
    return root, man, None

@bp.route("/api/plugins/import", methods=["POST"])
@require_perm("settings.edit")
def plugins_import():
    """Importe un .mxlplugin (zip). Piloté par la version (cf. règles). Anti zip-slip strict.
    Aucun code plugin n'est exécuté (lecture + dry-run str.format)."""
    import tempfile, shutil as _sh
    from .. import plugins
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "aucun fichier"}), 400
    raw = f.read()
    if len(raw) > 20 * 1024 * 1024:
        return jsonify({"error": "fichier trop volumineux (> 20 Mo)"}), 400
    force = (request.form.get("force") or "") == "replace"
    tmp = tempfile.mkdtemp(prefix="mxlplugin-")
    try:
        root, man, err = _extract_validated_package(raw, tmp)
        if err:
            return jsonify({"error": err}), 400
        plugins.stamp_imported_at(root)
        type_, ver = man["type"], man["version"]
        exists = plugins.is_plugin(type_)
        avail = plugins.versions(type_) if exists else []
        cur = (plugins.get(type_) or {}).get("version") if exists else None
        offer_activate = False
        if not exists:
            plugins.install_package(root, activate=True)
            status = "installed"
        elif ver in avail:
            if not force:
                return jsonify({"status": "conflict", "type": type_,
                                "version": ver, "current": cur}), 409
            plugins.install_package(root, activate=(ver == cur))
            status = "replaced"
        elif plugins._ver_key(ver) > plugins._ver_key(cur):
            plugins.install_package(root, activate=False)
            status = "imported"; offer_activate = True
        else:
            plugins.install_package(root, activate=False)
            status = "imported"
        db_add_alert("alert.deploy.plugin_importe", "info", kind="deploy",
                     params={"t": type_, "v": ver, "statut": status})
        return jsonify({"status": status, "type": type_, "version": ver,
                        "offer_activate": offer_activate})
    finally:
        _sh.rmtree(tmp, ignore_errors=True)

@bp.route("/api/plugins/<type_>/versions/import", methods=["POST"])
@require_perm("settings.edit")
def plugins_import_version(type_):
    """Importe UNE version (.mxlversion/zip) dans un plugin existant : rangée sous
    versions/<ver>/ (jamais activée). imported_at tamponné automatiquement. Conflit version
    existante → 409 (retry force=replace). Le type du paquet doit matcher <type_>."""
    import tempfile, shutil as _sh
    from .. import plugins
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "aucun fichier"}), 400
    raw = f.read()
    if len(raw) > 20 * 1024 * 1024:
        return jsonify({"error": "fichier trop volumineux (> 20 Mo)"}), 400
    force = (request.form.get("force") or "") == "replace"
    tmp = tempfile.mkdtemp(prefix="mxlversion-")
    try:
        root, man, err = _extract_validated_package(raw, tmp)
        if err:
            return jsonify({"error": err}), 400
        if man["type"] != type_:
            return jsonify({"error": f"le paquet est de type « {man['type']} », attendu « {type_} »"}), 400
        ver = man["version"]
        cur = (plugins.get(type_) or {}).get("version")
        if ver in plugins.versions(type_) and not force:
            return jsonify({"status": "conflict", "type": type_,
                            "version": ver, "current": cur}), 409
        plugins.stamp_imported_at(root)
        plugins.install_package(root, activate=False)
        db_add_alert("alert.deploy.plugin_version_importee", "info", kind="deploy",
                     params={"t": type_, "v": ver})
        return jsonify({"status": "imported", "type": type_, "version": ver})
    finally:
        _sh.rmtree(tmp, ignore_errors=True)

@bp.route("/api/plugins/<type_>/activate", methods=["POST"])
@require_perm("settings.edit")
def plugins_activate(type_):
    """Promeut une version (archivée) en version courante."""
    from .. import plugins
    version = (request.json or {}).get("version")
    if not version:
        return jsonify({"error": "version manquante"}), 400
    try:
        plugins.activate_version(type_, version)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db_add_alert("alert.deploy.plugin_version_activee", "info", kind="deploy",
                 params={"t": type_, "v": version})
    return jsonify({"status": "activated", "type": type_, "version": version})

@bp.route("/api/plugins/<type_>/disable", methods=["POST"])
@require_perm("settings.edit")
def plugins_disable(type_):
    """Active/désactive un plugin (politique orchestrateur). Désactivé = retiré de la
    palette/nav, mais reste installé et déployable (containers existants intacts)."""
    from .. import plugins
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    disabled = bool((request.json or {}).get("disabled", True))
    plugins.set_disabled(type_, disabled)
    if disabled:
        db_add_alert("alert.deploy.plugin_desactive", "info", kind="deploy", params={"t": type_})
    else:
        db_add_alert("alert.deploy.plugin_reactive", "info", kind="deploy", params={"t": type_})
    return jsonify({"status": "ok", "type": type_, "disabled": disabled})

@bp.route("/api/plugins/<type_>", methods=["DELETE"])
@require_perm("settings.edit")
def plugins_delete(type_):
    """Supprime un plugin du disque. Garde-fou : refusé si des containers l'utilisent."""
    from .. import plugins
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    used = []
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        if dc.get("type") == type_:
            used.append({"vmid": c["vmid"], "hostname": c.get("hostname")})
    if used:
        return jsonify({"status": "in_use", "type": type_, "containers": used,
                        "error": f"{len(used)} container(s) utilisent encore {type_}"}), 409
    try:
        plugins.delete_plugin(type_)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    db_add_alert("alert.deploy.plugin_supprime", "warning", kind="deploy", params={"t": type_})
    return jsonify({"status": "deleted", "type": type_})

# ─── Préréglages de plugin ──────────────────────────────────────────────────────────────────
# PARTAGÉS, côté serveur, et c'est le point : la disposition d'une grille vit dans le navigateur
# (c'est un confort personnel), mais un préréglage NOMMÉ — « Régie 1 », « Contrôle final » — doit
# se retrouver depuis n'importe quel poste. Les deux niveaux coexistent sans se contredire.
#
# Le CONTENU est opaque à l'orchestrateur : c'est le plugin qui sait ce qu'il range dedans. On
# stocke, on liste, on rend — on n'interprète pas. Un schéma imposé ici vieillirait mal.

@bp.route("/api/plugins/<type_>/presets", methods=["GET"])
@require_login
def plugin_presets_list(type_):
    from ..database import db_plugin_store_list
    return jsonify({"presets": db_plugin_store_list(type_, scope="")})


@bp.route("/api/plugins/<type_>/presets/<nom>", methods=["GET"])
@require_login
def plugin_preset_get(type_, nom):
    from ..database import db_plugin_store_get
    v = db_plugin_store_get(type_, nom, scope="")
    if v is None:
        return jsonify({"error": "préréglage introuvable"}), 404
    return jsonify({"nom": nom, "contenu": v})


@bp.route("/api/plugins/<type_>/presets/<nom>", methods=["PUT"])
@require_perm("plugins.operate")
def plugin_preset_save(type_, nom):
    from ..database import db_plugin_store_set
    nom = (nom or "").strip()
    if not nom or len(nom) > 60:
        return jsonify({"error": "nom invalide (1 à 60 caractères)"}), 400
    contenu = request.get_json(force=True, silent=True)
    if not isinstance(contenu, dict):
        return jsonify({"error": "contenu attendu : objet JSON"}), 400
    db_plugin_store_set(type_, nom, contenu, scope="")
    return jsonify({"ok": True, "nom": nom})


@bp.route("/api/plugins/<type_>/presets/<nom>", methods=["DELETE"])
@require_perm("plugins.operate")
def plugin_preset_delete(type_, nom):
    from ..database import db_plugin_store_delete
    return jsonify({"ok": db_plugin_store_delete(type_, nom, scope="")})


# ─── Témoins horodatés ──────────────────────────────────────────────────────────────────────
# Un témoin qui n'existe que le temps d'une requête ne prouve rien : il est PERSISTÉ ici, pas
# laissé au navigateur. `static/uploads` est l'endroit du projet pour les artefacts.
TEMOINS_MAX = 50          # par conteneur


def _dossier_temoins():
    import os
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "static", "uploads", "temoins")
    os.makedirs(d, exist_ok=True)
    return d


def _purger_temoins(vmid):
    """RÉTENTION. Un témoin pèse ~300 Ko : sans purge, un exploitant qui clique cent fois remplit
    le disque du contrôleur — le projet a déjà perdu un nœud à un journal Docker non tourné."""
    import os, glob
    fichiers = sorted(glob.glob(os.path.join(_dossier_temoins(), "scope-%d-*.json" % vmid)))
    for vieux in fichiers[:-TEMOINS_MAX]:
        for ext in (".json", ".jpg"):
            try:
                os.remove(vieux[:-5] + ext)
            except OSError:
                pass


@bp.route("/api/containers/<int:vmid>/temoin", methods=["POST"])
@require_perm("plugins.operate")
def container_temoin(vmid):
    """Capture un témoin horodaté et le PERSISTE. Rend les URL du JSON et de la vignette."""
    import base64, json as _json, os, time as _time
    import requests as _rq
    from ..addressing import get_container_ip
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    c = db_get_container(vmid)
    ip = (c or {}).get("ip") or get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP container introuvable"}), 404
    try:
        r = _rq.post("http://%s:8082/snapshot" % ip, json={}, timeout=20)
    except Exception as e:                                          # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    if r.status_code != 200:
        return jsonify(r.json() if r.headers.get("Content-Type", "").startswith("application/json")
                       else {"error": "HTTP %s" % r.status_code}), r.status_code
    snap = r.json()
    base = "scope-%d-%s" % (vmid, _time.strftime("%Y%m%d-%H%M%S", _time.gmtime()))
    d = _dossier_temoins()
    jpg = snap.pop("image_jpeg_b64", None)
    url_img = None
    if jpg:
        with open(os.path.join(d, base + ".jpg"), "wb") as f:
            f.write(base64.b64decode(jpg))
        url_img = "/static/uploads/temoins/%s.jpg" % base
    # La vignette sort du JSON et devient un fichier : un témoin qu'on ouvre doit montrer une
    # image, pas 34 Ko de base64 au milieu du texte. Le JSON garde tout le reste, plans compris.
    snap["image"] = url_img
    with open(os.path.join(d, base + ".json"), "w", encoding="utf-8") as f:
        _json.dump(snap, f, ensure_ascii=False)
    _purger_temoins(vmid)
    return jsonify({"ok": True, "nom": base, "json": "/static/uploads/temoins/%s.json" % base,
                    "image": url_img,
                    "horodatage": snap.get("horodatage")})


@bp.route("/api/containers/<int:vmid>/plugin/<path:p>", methods=["GET", "POST"])
@require_login
def plugin_proxy(vmid, p):
    """Forward générique vers le contrôle :8082 du script plugin. Valide le chemin
    contre les endpoints déclarés au manifeste (évite un forward ouvert).
    Permission : `plugins.operate` (contrôle live), SAUF les GET listés dans
    `control.read_endpoints` (lecture pure — état, preview) qui ne demandent que le login."""
    from .. import plugins
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    m = plugins.get((dc or {}).get("type"))
    if not m:
        return jsonify({"error": f"#{vmid} n'est pas un container plugin"}), 400
    ctrl = m.get("control") or {}
    read_only = set(ctrl.get("read_endpoints") or [])
    allowed = set(ctrl.get("endpoints") or []) | read_only
    if allowed and ("/" + p) not in allowed:
        return jsonify({"error": f"/{p} n'est pas autorisé pour le type {m['type']} (non listé dans control.endpoints du plugin.json)"}), 403
    is_read = request.method == "GET" and ("/" + p) in read_only
    # Scoping projet (chantier 1) : un utilisateur non-global ne pilote/lit que les
    # containers des projets dont il est membre (lecture dès viewer, action dès operator).
    err = check_vmid_access(vmid, "viewer" if is_read else "operator")
    if err:
        return err
    if not is_read and not has_perm("plugins.operate"):
        return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    if request.method == "GET":
        # GET : on forwarde nous-mêmes pour (1) transmettre les query params (que
        # _mixer_proxy ignore) et (2) laisser passer le binaire (vignettes image/*).
        from ..addressing import get_container_ip
        import requests as _req
        ip = get_container_ip(vmid)
        if not ip:
            return jsonify({"error": "IP container introuvable"}), 404
        try:
            r = _req.get(f"http://{ip}:8082/{p}", params=request.args, timeout=5)
        except Exception as e:
            return jsonify({"error": str(e)}), 502
        if r.status_code == 204:   # pas de contenu (ex. preview pas encore prête)
            return ("", 204)
        ctype = r.headers.get("Content-Type", "application/json")
        if ctype.startswith("application/json") or ctype.startswith("text/"):
            return (r.text, r.status_code, {"Content-Type": ctype})
        # Binaire (preview/vignettes) : contenu live, jamais mis en cache navigateur.
        return Response(r.content, status=r.status_code, content_type=ctype,
                        headers={"Cache-Control": "no-store"})
    body = request.get_json(force=True, silent=True)
    # Mur multiview SHARDÉ : le conteneur du vmid est l'ASSEMBLEUR du tissu (copie pure des
    # shards pré-rendus). Ne JAMAIS lui forwarder les hot-applies window/style (le chrome se
    # dessinerait autour de chaque BLOC de shard ; l'idx de fenêtre logique ≠ ses tuiles) : on
    # persiste dans deploy_config puis on RE-PLANIFIE le tissu (les signatures de cellule
    # incluent le style → shards re-matérialisés, assembleur reconfiguré derrière).
    if request.method == "POST" and (m.get("type") == "multiview") and p in ("window", "style") \
            and isinstance(body, dict):
        from ..database import db_fabric_get
        if db_fabric_get(f"asm:{vmid}"):
            try:
                # Rien n'a bougé → NE PAS re-planifier. Le composer poste un hot-apply à chaque
                # relâchement de souris, y compris un simple clic de SÉLECTION qui n'a rien
                # modifié ; un reconcile par clic coûte une lecture :8080 par multiview du nœud
                # et, si `/state` répond mal, un re-push d'assembleur (recuisson des overlays →
                # image figée). Une sélection ne doit rien coûter à la sortie.
                if not _persist_multiview_hot(vmid, dc, p, body):
                    return jsonify({"ok": True, "routed": "fabric", "unchanged": True})
                from ..deploy import _fabric_refresh_wall
                _fabric_refresh_wall(vmid)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
            return jsonify({"ok": True, "routed": "fabric"})
    result = _mixer_proxy(vmid, "/" + p, method=request.method, body=body)
    # Persistance des hot-applies multiview : window/style ne touchent que le conteneur live (:8082).
    # On reflète la modif dans deploy_config (DB) pour qu'elle SURVIVE à un changement de multiview +
    # rechargement de l'éditeur (sinon la modif reste active sur la SORTIE mais disparaît de l'AFFICHAGE).
    # Best-effort : n'altère jamais la réponse du forward.
    if request.method == "POST" and (m.get("type") == "multiview") and p in ("window", "style") \
            and isinstance(body, dict):
        try:
            _persist_multiview_hot(vmid, dc, p, body)
        except Exception:
            pass
    return result

# Champs géométrie/affichage d'une fenêtre (POST /plugin/window) reportés dans flux_config[idx].
# `name` exclu : c'est le nom d'AFFICHAGE calculé, pas la config de source persistée.
_MV_WINDOW_PERSIST = ("x", "y", "w", "h", "hidden", "show_label", "show_tally",
                      "label_proportional", "tsl_index", "meter_channels", "meter_position",
                      "meter_inside", "meter_opacity", "meter_scale",
                      # Métadonnées ANC par fenêtre. Le composer les applique À CHAUD depuis
                      # 0.29.0, mais elles n'étaient PAS persistées : cocher « timecode » se
                      # voyait tout de suite et disparaissait au prochain déploiement complet,
                      # sans trace en base (constaté 2026-08-07 : `anc_tc` à False partout dans
                      # `deploy_config` alors que le mur affichait bien un timecode).
                      "anc_types", "anc_tc", "anc_cc", "anc_afd", "anc_st352", "anc_scte",
                      "anc_crc", "anc_position", "anc_opacity",
                      # Habillage de la fenêtre : même trou que les drapeaux ANC — appliqué à
                      # chaud, jamais enregistré, donc perdu au déploiement complet suivant.
                      # `template_ref` accompagne OBLIGATOIREMENT `template` : le modèle résolu
                      # sans sa référence de bibliothèque laisse le sélecteur de l'éditeur sur
                      # l'ancienne entrée (le mur rend B, l'éditeur affiche A).
                      "label_col", "tally_level", "tally_red", "tally_green",
                      "template", "template_ref",
                      # `audio_path` (source des VU d'une fenêtre) : MÊME trou que les drapeaux ANC
                      # et l'habillage — le composer la pousse à chaud depuis 0.35.0 (c'est
                      # `_do_window` qui purge les états audio ouverts, pas `/reconfigure`), mais
                      # elle n'était pas persistée : le choix tenait jusqu'au premier déploiement
                      # complet, puis les VU repartaient sur la source AUTO. La source VIDÉO
                      # (`path`) n'est volontairement PAS dans cette liste : elle ne transite pas
                      # par ce hot-apply, elle passe par le déploiement (multiview.js:onEntryChange).
                      "audio_path")

# Drapeaux ANC : le composer les poste en 1/0, un déploiement complet les écrit en true/false.
# Les deux sont équivalents pour le script (`_as_bool`), mais PAS pour la signature de cellule du
# tissu, qui sérialise en JSON — `1` et `true` y donnent deux signatures différentes, donc une
# re-matérialisation de shard à chaque va-et-vient entre les deux chemins. On normalise.
_MV_BOOL_PERSIST = ("anc_types", "anc_tc", "anc_cc", "anc_afd", "anc_st352", "anc_scte",
                    "anc_crc", "hidden", "show_label", "show_tally", "label_proportional",
                    "meter_inside", "tally_red", "tally_green")
_MV_STYLE_PERSIST = ("show_no_signal", "freeze_detect_s", "show_proxy",
                     "default_template", "default_template_ref")

def _persist_multiview_hot(vmid, dc, kind, body):
    """Reporte un hot-apply window/style dans `deploy_config`. Renvoie True si la config a
    RÉELLEMENT changé (l'appelant s'en sert pour ne re-planifier le tissu qu'alors).

    L'écriture est conditionnelle : le composer renvoie l'état complet de la fenêtre à chaque
    geste, donc la majorité des appels sont des no-op (clic de sélection). Comparer AVANT d'écrire
    évite aussi une bascule de valeur gratuite (`1` stocké vs `True` posté sont égaux en Python →
    on garde le stocké), qui changerait la signature de cellule du tissu et ferait re-matérialiser
    un shard — donc couperait la sortie — pour une différence de pure représentation."""
    from ..database import db_update_deploy_config
    params = dict((dc or {}).get("params") or {})
    if kind == "window":
        idx = body.get("idx")
        if idx is None:
            return False
        idx = int(idx)
        fc = [dict(f) for f in (params.get("flux_config") or [])]
        if not (0 <= idx < len(fc)):
            return False
        change = False
        for k in _MV_WINDOW_PERSIST:
            if k not in body:
                continue
            v = bool(body[k]) if k in _MV_BOOL_PERSIST else body[k]
            if k not in fc[idx] or fc[idx][k] != v or type(fc[idx][k]) is not type(v):
                fc[idx][k] = v
                change = True
        if not change:
            return False
        params["flux_config"] = fc
    else:  # style
        change = False
        for k in _MV_STYLE_PERSIST:
            if k in body and (k not in params or params[k] != body[k]):
                params[k] = body[k]
                change = True
        if not change:
            return False
    db_update_deploy_config(vmid, "multiview", params)
    return True


# Garde anti-rafale : un redeploy déjà en vol pour un vmid → 409 plutôt que d'empiler les threads
# (chacun bloquerait jusqu'à 120s sur verrou_vmid). Protégé par _plugin_config_lock.
_plugin_config_pending = set()
_plugin_config_lock = threading.Lock()


class PluginConfigError(Exception):
    """Erreur de validation/application des réglages config_schema.
    `code` = statut HTTP suggéré ; `payload` = corps JSON additionnel (needs_confirm…)."""
    def __init__(self, message, code=400, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def _plugin_config_check(vmid, incoming, allow_system, confirm):
    """Validation commune (route plugin_config ET moteur de macros) : container plugin,
    clés dans le config_schema, scope system gaté par `allow_system` (= containers.deploy),
    garde-fou 2110_io. Renvoie (c, type_, m, running) ou lève PluginConfigError."""
    from .. import plugins
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    type_ = dc.get("type") if isinstance(dc, dict) else None
    m = plugins.get(type_)
    if not m:
        raise PluginConfigError(f"#{vmid} n'est pas un container plugin")
    if not isinstance(incoming, dict) or not incoming:
        raise PluginConfigError("params manquants")
    user_keys = plugins.config_scope_keys(type_, "user")
    sys_keys  = plugins.config_scope_keys(type_, "system")
    unknown = set(incoming) - user_keys - sys_keys
    if unknown:
        raise PluginConfigError(f"clés hors config_schema : {sorted(unknown)}")
    # Bornes du config_schema : une valeur hors bornes est REFUSÉE (message clair) — jamais
    # écrêtée en silence. Générique à tous les plugins (cf. plugins.validate_config).
    errs = plugins.validate_config(type_, incoming)
    if errs:
        raise PluginConfigError("Réglages hors bornes : " + " ".join(errs), 400, {"errors": errs})
    touched_sys = set(incoming) & sys_keys
    if touched_sys and not allow_system:
        raise PluginConfigError("forbidden", 403,
                                {"error": "forbidden", "missing_permission": "containers.deploy",
                                 "system_keys": sorted(touched_sys)})
    running = (c or {}).get("status") == "running"
    # Même garde-fou que /deploy : redéployer un moteur 2110_io en marche coupe tous les flux.
    if type_ == "2110_io" and running and not confirm:
        raise PluginConfigError(
            "Redéploiement du moteur 2110 — coupure brève de TOUS les flux.", 409,
            {"ok": False, "needs_confirm": True,
             "reason": "Redéploiement du moteur 2110 — coupure brève de TOUS les flux."})
    return c, type_, m, running


def _cles_changees(type_, incoming, persisted):
    """Les clés de `incoming` dont la valeur DIFFÈRE de ce qui est persisté.

    ★ SEULE UNE VALEUR QUI CHANGE PEUT EXIGER UN REDÉPLOIEMENT. Les écrans envoient tout le
    formulaire d'un coup — le panneau ⚙ poste TOUS les champs `system`, changés ou non. Sans ce
    tri, cocher un niveau de tally embarquait `format` dans le lot, et la route concluait qu'un
    redéploiement était nécessaire alors que rien de ce qu'il lit n'avait bougé. C'est ce qui
    faisait que le chemin « à chaud » ne servait jamais depuis cet écran.

    On compare des valeurs COERCÉES : sinon `"2"` et `2`, ou une liste et son équivalent
    dédoublonné, passeraient pour des changements — et on redéploierait pour rien."""
    from .. import plugins
    a = plugins.coerce_config(type_, dict(persisted or {}, **(incoming or {})))
    b = plugins.coerce_config(type_, dict(persisted or {}))
    return {k for k in (incoming or {}) if a.get(k) != b.get(k)}


def _plugin_config_apply(vmid, type_, m, incoming, running):
    """Chemin d'écriture UNIQUE des réglages config_schema (déjà validés) : merge frais
    sous verrou, persiste, et redéploie si le container tourne. Renvoie True si redéployé."""
    from .. import plugins
    from ..vmlocks import verrou_vmid
    from ..database import db_update_deploy_config

    def _merge_fresh(fresh_dc):
        """Merge défauts ← params persistés FRAIS (relus sous verrou, pas le snapshot de la requête
        — évite d'écraser une écriture concurrente : câblage, hot-persist multiview…) ← POST."""
        persisted = (fresh_dc or {}).get("params") or {}
        return plugins.coerce_config(type_, {**plugins.effective_deploy_defaults(type_),
                                              **persisted, **incoming})

    # ★ CERTAINS RÉGLAGES N'EXIGENT PAS DE REDÉPLOYER, et il faut le savoir AVANT de prendre le
    # verrou de déploiement. Un niveau de tally est lu par l'ORCHESTRATEUR (distributeur TSL,
    # hook `tally_targets`), jamais par le conteneur : redéployer pour ça coupe un flux vidéo
    # pour changer une case à cocher — et comme le déploiement est sérialisé, une rafale de
    # gestes se faisait refuser en 409, donc PERDRE. Cf. `plugins.cles_sans_redeploiement`.
    _persisted = (_load_dc(db_get_container(vmid)) or {}).get("params") or {}
    _changees = _cles_changees(type_, incoming, _persisted)
    # Rien n'a bougé : on ne redéploie pas, et on n'écrit même pas.
    a_chaud = not _changees or _changees <= plugins.cles_sans_redeploiement(type_)
    if not running or a_chaud:
        # Exploitant : ne PAS (re)démarrer un container arrêté (deployer_script force
        # desired_state=running). On persiste seulement les params.
        with verrou_vmid(vmid, op="config"):
            fresh = _load_dc(db_get_container(vmid))
            params = _merge_fresh(fresh)
            db_update_deploy_config(vmid, type_, params)
        return False
    with verrou_vmid(vmid, op="deploy"):
        fresh = _load_dc(db_get_container(vmid))
        params = _merge_fresh(fresh)
        # Pin de version : ne jamais upgrader silencieusement un container épinglé sur une
        # version archivée — un simple réglage ne doit changer QUE les params, pas la version.
        pv = params.get("plugin_version")
        cur_ver = m.get("version")
        version = pv if (pv and pv != cur_ver and pv in plugins.versions(type_)) else None
        deployer_script(vmid=vmid, type_script=type_, params=params, version=version)
    return True


def apply_plugin_config(vmid, incoming, allow_system=False, confirm=False):
    """Écriture SYNCHRONE des réglages config_schema — utilisée par le moteur de macros
    (étape `config`, 8e passe ch.6). Même validation + même chemin d'écriture que la
    route plugin_config, même garde anti-rafale. Lève PluginConfigError."""
    c, type_, m, running = _plugin_config_check(vmid, incoming, allow_system, confirm)
    with _plugin_config_lock:
        if vmid in _plugin_config_pending:
            raise PluginConfigError("déploiement déjà en cours", 409, {"pending": True})
        _plugin_config_pending.add(vmid)
    try:
        return _plugin_config_apply(vmid, type_, m, incoming, running)
    finally:
        with _plugin_config_lock:
            _plugin_config_pending.discard(vmid)


@bp.route("/api/containers/<int:vmid>/plugin_config", methods=["GET"])
@require_login
def plugin_config_get(vmid):
    """Les réglages `config_schema` PERSISTÉS de ce conteneur — la source de vérité.

    ★ POURQUOI LA PAGE NE PEUT PAS SE FIER À `/state`. Le conteneur répond avec la configuration
    qui lui a été REMISE À SON DÉPLOIEMENT : c'est une photo, pas un miroir. Tant qu'un réglage
    redéployait, la photo était toujours fraîche et personne ne voyait la différence. Depuis
    qu'un réglage peut s'appliquer À CHAUD (`redeploiement: false`), elle ne l'est plus : on
    retire un niveau, la base l'enregistre, et `/state` continue de rendre l'ancienne liste —
    la page se reconstruit dessus et défait ce qu'on vient de faire.

    ⚠ CE CHEMIN N'EXISTE PAS EN MODE PUBLIC (jeton) : la page publique est en lecture seule et
    ne doit joindre que le relais du plugin. Elle garde `/state`, dont la fraîcheur n'a pas
    d'importance pour un affichage qu'on ne peut pas modifier."""
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    from .. import plugins
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    type_ = dc.get("type") if isinstance(dc, dict) else None
    if not plugins.get(type_):
        return jsonify({"error": f"#{vmid} n'est pas un container plugin"}), 404
    cles = plugins.config_scope_keys(type_, "user") | plugins.config_scope_keys(type_, "system")
    params = (dc or {}).get("params") or {}
    return jsonify({"params": {k: params.get(k) for k in cles if k in params},
                    "type": type_})


@bp.route("/api/containers/<int:vmid>/plugin_config", methods=["POST"])
@require_perm("plugins.operate")
def plugin_config(vmid):
    """Édite les réglages déclaratifs (config_schema) d'un container plugin depuis sa page
    (panneau « Réglages »). Body : {params: {...}, confirm?: bool}.
    Droits PAR CLÉ selon le `scope` du champ : "user" → plugins.operate (suffit ici) ;
    "system" (structurel, défaut) → containers.deploy requis en plus. Les clés hors
    config_schema sont refusées (les params non déclaratifs restent du ressort de la
    palette / des pages bespoke). Merge dans les params persistés puis REDÉPLOIE — sauf si le
    container est ARRÊTÉ (les params sont alors seulement persistés, pas de redémarrage
    implicite ; le prochain déploiement explicite les prendra)."""
    err = check_vmid_access(vmid, "editor")
    if err:
        return err
    incoming = (request.json or {}).get("params") or {}
    try:
        c, type_, m, running = _plugin_config_check(
            vmid, incoming, allow_system=has_perm("containers.deploy"),
            confirm=bool((request.json or {}).get("confirm")))
    except PluginConfigError as e:
        body = dict(e.payload)
        if "needs_confirm" not in body:
            body.setdefault("error", str(e))
        return jsonify(body), e.code
    # La garde anti-rafale protège le DÉPLOIEMENT. Un réglage qui n'en demande pas n'a pas à s'y
    # heurter : c'est ce 409 qui faisait perdre les gestes successifs d'une sélection multiple.
    from .. import plugins as _plg
    _incoming = (request.json or {}).get("params") or {}
    _pers = (_load_dc(c) or {}).get("params") or {}
    _chg = _cles_changees(type_, _incoming, _pers)
    # ⚠ ON DÉCIDE SUR CE QUI CHANGE, pas sur ce qui est envoyé. Le panneau ⚙ poste tout le
    # formulaire : sans ce tri, cocher un niveau de tally embarquait `format` dans le lot et
    # forçait un redéploiement — donc la garde anti-rafale, donc les 409 qui perdaient les gestes.
    if not _chg or _chg <= _plg.cles_sans_redeploiement(type_):
        _plugin_config_apply(vmid, type_, m, _incoming, running)
        return jsonify({"ok": True, "redeploye": False,
                        "changees": sorted(_chg)})
    with _plugin_config_lock:
        if vmid in _plugin_config_pending:
            return jsonify({"error": "déploiement déjà en cours", "pending": True}), 409
        _plugin_config_pending.add(vmid)

    def _go():
        try:
            _plugin_config_apply(vmid, type_, m, incoming, running)
        finally:
            with _plugin_config_lock:
                _plugin_config_pending.discard(vmid)

    threading.Thread(target=_go).start()
    if not running:
        return jsonify({"status": "params_enregistres", "vmid": vmid})
    return jsonify({"status": "deploiement_en_cours", "vmid": vmid})
