// ─── État palette de déploiement ─────────────────────────────

let selectedDeployVmid = null;
let lastContainers     = [];

// ─── Formats vidéo ───────────────────────────────────────────
// `loadVideoFormats()` / `window._videoFormats` / `_videoFormatDefault` vivent désormais dans
// static/video_formats.js (chargé par layout.html pour TOUTES les pages) : ils étaient ici, donc
// invisibles de Réglages, qui aurait dû recopier le parseur — une seconde vérité qui dérive.

function populateFormatSelect(host) {
    const sel = host.querySelector('.dp-format-preset');
    if (!sel || !window._videoFormats) return;
    const def = window._videoFormatDefault || '';
    const opts = window._videoFormats.map(f =>
        `<option value="${f.w}x${f.h}" data-label="${f.label}" data-w="${f.w}" data-h="${f.h}" data-fps="${f.fps}" data-scan="${f.scan}" data-chroma="${f.chroma}" data-bd="${f.bit_depth}" data-colorimetry="${f.colorimetry}">${f.label}</option>`
    );
    sel.innerHTML = '<option value="">' + escapeHtml(window.t('js.pick_format')) + '</option>' + opts.join('');
    if (def) {
        // Par INDEX, pas par valeur : deux préréglages de même résolution partagent la valeur
        // « <w>x<h> » (cf. _selectMatchingPreset) — `sel.value = …` retomberait sur le premier.
        const all = Array.from(sel.options);
        const match = all.find(o => o.dataset.label === def);
        if (match) sel.selectedIndex = all.indexOf(match);
    }
}

// onFormatPresetChange : le select suffit — les valeurs sont lues au déploiement
function onFormatPresetChange(sel) { /* sélection visuelle uniquement */ }

// Un plugin déclarant video_format:false est format-agnostique (ex. delay) : pas de
// sélecteur de format ni d'injection width/height/fps/scan. Les types bespoke en ont besoin.
// receiver_2110 et sa variante MTL partagent le même palette bespoke (sélecteur NIC,
// format vidéo, slots de simulation). Tout ce qui était gardé par `type === 'receiver_2110'`
// doit aussi s'appliquer à 2110_io, sinon ce dernier tombe dans le formulaire plugin générique.
function _isRx2110(type) {
    return type === 'receiver_2110' || type === '2110_io';
}

function _typeNeedsFormat(type) {
    if (!type) return true;
    const f = window.PLUGIN_VIDEO_FORMAT || {};
    return (type in f) ? !!f[type] : true;
}

// Valeurs du préréglage SÉLECTIONNÉ, ou null si aucun.
// ⚠ Renvoyait un 720p25 CODÉ EN DUR quand rien n'était choisi — un format inventé, silencieux,
// dans une fonction dont l'appelant poste le résultat au déploiement. C'est la même famille que
// la substitution de préréglage corrigée dans `_selectMatchingPreset` : un format ne se devine
// jamais. null oblige l'appelant à trancher explicitement (refuser, ou ne rien émettre pour un
// type format-agnostique).
function _getFormatValues(host) {
    const sel = host.querySelector('.dp-format-preset');
    if (!sel || !sel.value) return null;
    const opt = sel.selectedOptions[0];
    return {
        w:    parseInt(opt.dataset.w)    || 1280,
        h:    parseInt(opt.dataset.h)    || 720,
        fps:  parseFloat(opt.dataset.fps) || 25,
        scan: opt.dataset.scan           || 'p',
        chroma:      opt.dataset.chroma      || '422',
        bit_depth:   parseInt(opt.dataset.bd) || 10,
        colorimetry: opt.dataset.colorimetry || '709',
    };
}

// Sélectionne DANS LE SÉLECTEUR le préréglage correspondant au format d'un conteneur EXISTANT.
//
// ⚠ DEUX PIÈGES, tous deux vécus en production (« créé en 50p, redéployé en 50i », 2026-08-06) :
//
//  1. La VALEUR d'une option est « <w>x<h> » — donc 1080p50 et 1080i50 ont la MÊME. Un
//     `sel.value = match.value` sélectionne alors la PREMIÈRE option portant cette valeur, pas
//     celle qu'on avait trouvée. `_getFormatValues` lit ensuite le dataset de la mauvaise option
//     et le déploiement poste son scan. On sélectionne donc par INDEX, jamais par valeur.
//  2. Le repli « même résolution, tant pis pour fps/scan » substituait un format voisin en
//     silence. Un format n'est JAMAIS approché : ou bien on retrouve exactement celui du
//     conteneur, ou bien on l'ajoute tel quel au sélecteur (option « format actuel ») pour qu'un
//     redéploiement le REPOSTE à l'identique. Aucune substitution muette.
//
// `cur` = params du conteneur (null si pas encore déployé : on ne présélectionne alors rien, et
// la garde « ⚠ Sélectionner un format vidéo » force un choix explicite).
function _selectMatchingPreset(host, w, h, fps, scan, cur) {
    const sel = host.querySelector('.dp-format-preset');
    if (!sel) return;
    const opts = Array.from(sel.options);
    const exact = opts.find(o => o.dataset.w
        && parseInt(o.dataset.w) === parseInt(w) && parseInt(o.dataset.h) === parseInt(h)
        && parseFloat(o.dataset.fps) === parseFloat(fps) && o.dataset.scan === scan);
    if (exact) { sel.selectedIndex = opts.indexOf(exact); return; }
    if (!cur || !w || !h || !fps || !scan) return;   // pas de format connu → à l'utilisateur de choisir
    // Format du conteneur absent du catalogue (ou catalogue modifié depuis) : on l'expose TEL
    // QUEL plutôt que d'en approcher un autre — un redéploiement doit être neutre.
    const o = document.createElement('option');
    o.value = `${w}x${h}`;
    o.textContent = `${w}×${h} ${fps}${scan} — format actuel`;
    o.dataset.w = w; o.dataset.h = h; o.dataset.fps = fps; o.dataset.scan = scan;
    o.dataset.chroma = cur.chroma || '422';
    o.dataset.bd = cur.bit_depth != null ? cur.bit_depth : 10;
    o.dataset.colorimetry = cur.colorimetry || '709';
    o.dataset.label = o.textContent;
    sel.appendChild(o);
    sel.selectedIndex = sel.options.length - 1;
}

function toggleDpResources(btn) {
    const section = btn.closest('.dp-resources-section');
    const body    = section.querySelector('.dp-resources-body');
    const open    = body.hasAttribute('hidden');
    if (open) body.removeAttribute('hidden'); else body.setAttribute('hidden', '');
    section.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// ─── Filtres (type + statut) ─────────────────────────────────

const FILTER_KEY = 'mxl_containers_filter';
let filterState  = (() => {
    const def = {category: 'all', type: 'all', status: 'all', project: 'all', node: 'all', q: '', showFabricInternals: false};
    let st = def;
    try { st = Object.assign(def, JSON.parse(localStorage.getItem(FILTER_KEY)) || {}); }
    catch(e) {}
    st.q = '';   // la recherche texte NE persiste JAMAIS (réinitialisée à chaque chargement)
    return st;
})();

function setFilter(group, value) {
    filterState[group] = value;
    // Changer de catégorie : si le type actif n'est pas dans la nouvelle catégorie, on
    // le remet sur 'all', puis on masque les chips de Type hors catégorie.
    if (group === 'category') {
        const tsec = typeSectionMap();
        if (value !== 'all' && filterState.type !== 'all' && tsec[filterState.type] !== value) {
            filterState.type = 'all';
        }
    }
    // Persiste les filtres SAUF la recherche texte (q) — sinon elle « reviendrait » au rechargement.
    try { localStorage.setItem(FILTER_KEY, JSON.stringify({...filterState, q: ''})); } catch(e) {}
    document.querySelectorAll(`.filter-chip[data-group="${group}"]`).forEach(b => {
        b.classList.toggle('active', b.dataset.value === value);
    });
    if (group === 'category') {
        syncTypeChipsVisibility();
        // re-synchronise le surlignage du type (peut avoir été remis à 'all')
        document.querySelectorAll('.filter-chip[data-group="type"]').forEach(b =>
            b.classList.toggle('active', b.dataset.value === filterState.type));
    }
    const sel = document.getElementById('filter-' + group);
    if (sel && sel.value !== value) sel.value = value;
    if (lastContainers.length) updateContainers(lastContainers);
}

function applyFilterChipsActive() {
    Object.entries(filterState).forEach(([group, value]) => {
        if (group === 'q') return;
        const chips = document.querySelectorAll(`.filter-chip[data-group="${group}"]`);
        const sel   = document.getElementById('filter-' + group);
        // Auto-réparation : si la valeur persistée (localStorage) ne correspond à aucun
        // chip ni option (ex. type renommé), on retombe sur 'all' au lieu de filtrer
        // sur une valeur fantôme qui viderait la grille sans chip actif visible.
        if (chips.length) {
            const known = Array.from(chips).some(b => b.dataset.value === value);
            if (!known) { filterState[group] = 'all'; value = 'all'; }
            chips.forEach(b => b.classList.toggle('active', b.dataset.value === value));
        }
        if (sel) {
            const hasOpt = Array.from(sel.options).some(o => o.value === value);
            sel.value = hasOpt ? value : 'all';
            if (!hasOpt) filterState[group] = 'all';
        }
    });
    // Persiste les filtres SAUF la recherche texte (q) — sinon elle « reviendrait » au rechargement.
    try { localStorage.setItem(FILTER_KEY, JSON.stringify({...filterState, q: ''})); } catch(e) {}
    syncTypeChipsVisibility();
}

function containerType(c) {
    let dc = null;
    try { dc = c.deploy_config ? JSON.parse(c.deploy_config) : null; } catch(e) {}
    return (dc && dc.type) || 'none';
}

// Mapping type → catégorie (section), lu depuis les attributs data-section des chips de Type.
function typeSectionMap() {
    if (typeSectionMap._cache) return typeSectionMap._cache;
    const m = {};
    document.querySelectorAll('.filter-chip[data-group="type"][data-section]').forEach(b => {
        m[b.dataset.value] = b.dataset.section;
    });
    return (typeSectionMap._cache = m);
}
function containerSection(c) {
    return typeSectionMap()[containerType(c)] || null;
}

// Masque les chips de Type hors de la catégorie active (et le chip "Aucun" si une catégorie est choisie).
function syncTypeChipsVisibility() {
    const cat = filterState.category || 'all';
    document.querySelectorAll('.filter-chip[data-group="type"]').forEach(b => {
        const v = b.dataset.value;
        let show = true;
        if (cat !== 'all') {
            if (v === 'all') show = true;
            else if (b.dataset.section) show = (b.dataset.section === cat);
            else show = false;   // "Aucun" : pas de catégorie → masqué quand une catégorie est active
        }
        b.hidden = !show;
    });
}

// Recherche texte libre : ne persiste pas (réinitialisée à chaque visite).
function setSearch(value) {
    filterState.q = value || '';
    if (lastContainers.length) updateContainers(lastContainers);
}

function matchesFilter(c) {
    // Internes du tissu (shards, proxies pyramide) : repliés (masqués) par défaut sous leur
    // multiview logique. Le toggle « ⚙ Internes du tissu » les révèle.
    if (!filterState.showFabricInternals && (c.fabric_role === 'shard' || c.fabric_role === 'proxy'))
        return false;
    const q = (filterState.q || '').trim().toLowerCase();
    if (q) {
        const hay = [c.hostname, '#' + c.vmid, c.vmid, c.ip, c.source, c.shm_out, containerType(c)]
            .filter(Boolean).join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
    }
    if (filterState.category !== 'all' && containerSection(c) !== filterState.category) return false;
    if (filterState.type !== 'all' && containerType(c) !== filterState.type) return false;
    if (filterState.status !== 'all' && c.status !== filterState.status)     return false;
    if (filterState.node && filterState.node !== 'all'
        && String(c.node_id) !== String(filterState.node))                   return false;
    if (filterState.project !== 'all') {
        const projs = Array.isArray(c.projects) ? c.projects : [];
        if (filterState.project === 'none') {
            if (projs.length) return false;
        } else {
            const pid = parseInt(filterState.project, 10);
            if (!projs.some(p => p.id === pid)) return false;
        }
    }
    return true;
}

// ─── Onglets principaux (Création / Surveillance) ────────────

function switchMainTab(name) {
    ['creation', 'surveillance', 'ressources', 'inventaire'].forEach(t => {
        const panel = document.getElementById('main-tab-' + t);
        const btn   = document.getElementById('main-tab-btn-' + t);
        if (panel) panel.style.display = (t === name) ? '' : 'none';
        if (btn)   btn.classList.toggle('active', t === name);
    });
    // L'onglet Ressources ne poll qu'une fois actif (pas en arrière-plan) : démarré à la
    // première visite, jamais avant (évite un fetch/campagne fantôme sur une page jamais vue).
    if (name === 'ressources' && typeof resTabActivate === 'function') resTabActivate();
    if (name === 'inventaire' && typeof invTabActivate === 'function') invTabActivate();
    // L'onglet vit dans l'ADRESSE : sans ça, recharger renvoie à « Surveillance » et un lien vers
    // « la page Containers, onglet Ressources » n'existe pas. `replaceState` : changer d'onglet ne
    // doit pas remplir l'historique de navigation.
    const h = '#' + name;
    if (location.hash !== h) history.replaceState(null, '', h);
}

// Onglet demandé par l'adresse (rechargement, lien partagé, retour navigateur).
function _mainTabDepuisHash() {
    const n = (location.hash || '').replace(/^#/, '');
    if (['creation', 'surveillance', 'ressources', 'inventaire'].includes(n)) switchMainTab(n);
}
if (document.getElementById('main-tab-surveillance')) {
    document.addEventListener('DOMContentLoaded', _mainTabDepuisHash);
    window.addEventListener('hashchange', _mainTabDepuisHash);
    if (document.readyState !== 'loading') _mainTabDepuisHash();
}

// ─── Log de création ─────────────────────────────────────────

// ─── Batch state ─────────────────────────────────────────────

let _batchHostnames = [];           // historique accumulé (plus récent en tête)
let _batchTypes = {};               // hostname → type demandé (badge « à venir »)
let _survTimer      = null;
const _BATCH_KEY = 'mxl_batch';   // persistance localStorage (survit aux changements de page)
const _BATCH_MAX = 50;            // cap de l'historique

function _batchSave() {
    try {
        localStorage.setItem(_BATCH_KEY, JSON.stringify({
            hostnames: _batchHostnames, types: _batchTypes }));
    } catch (e) {}
}

function _batchLabel() {
    const lbl = document.getElementById('batch-label');
    if (lbl) lbl.textContent = window.t('js.batch_last_creations').replace('{n}', _batchHostnames.length);
}

function reinitBatch() {
    _batchHostnames = [];
    _batchTypes = {};
    if (_survTimer) { clearInterval(_survTimer); _survTimer = null; }
    try { localStorage.removeItem(_BATCH_KEY); } catch (e) {}
    const box = document.getElementById('batch-box');
    if (box) box.style.display = 'none';
}

// Restaure la liste de suivi depuis localStorage au chargement de la page : la liste
// des dernières créations reste affichée même après navigation (petit historique).
function _batchRestore() {
    if (!document.getElementById('batch-box')) return;   // page sans zone batch
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(_BATCH_KEY) || 'null'); } catch (e) {}
    if (!saved || !Array.isArray(saved.hostnames) || !saved.hostnames.length) return;
    _batchHostnames = saved.hostnames;
    _batchTypes = saved.types || {};
    const box = document.getElementById('batch-box');
    if (box) box.style.display = '';
    _batchLabel();
    _batchRenderTable([]);
    _batchStartPolling();   // se ré-arrête tout seul quand tout est stabilisé
}

// Bascule vers l'onglet Surveillance et ouvre la configuration du container.
function configurerDepuisBatch(vmid) {
    if (typeof switchMainTab === 'function') switchMainTab('surveillance');
    modifier(vmid);
}

// ─── Actions containers ──────────────────────────────────────

// Nettoie un hostname utilisateur pour qu'il passe les contraintes Proxmox / DNS :
// décompose les accents, retire tout caractère hors [A-Za-z0-9-], collapse les tirets,
// strip leading/trailing hyphens. Garde la casse d'origine (Proxmox l'accepte).
// FORME CANONIQUE — à utiliser à la validation (panier, hostname par défaut), PAS à la frappe :
// cf. hostnameDiag(), le strip du tiret FINAL rend la saisie impossible caractère par caractère.
// ⚠ MIROIR de `app/hostnames.py:normaliser()`, qui FAIT FOI (le serveur revalide et renvoie 400).
// Les deux doivent bouger ensemble : une divergence donne un nom accepté à l'écran puis refusé,
// ou pire, transformé en base.
function sanitizeHostname(v) {
    return hostnameDiag(v).value.replace(/-+$/, '');
}

// Le hostname applique DEUX règles opposées, et c'est là que la saisie devient incompréhensible :
// espace et « _ » sont CONVERTIS en tiret (l'utilisateur voit quelque chose apparaître), tandis
// qu'accents, points, « @ », etc. sont SUPPRIMÉS (le caractère disparaît sans explication).
// Cette fonction produit la valeur nettoyée ET de quoi DIRE ce qui a été fait.
// Le tiret de FIN est conservé ici : le retirer à chaque frappe empêche de taper « cam- » puis la
// suite du mot (le tiret s'efface, réapparaît, s'efface…), ce qui est le comportement le plus
// déroutant du champ. Il est retiré au blur / à la validation, par sanitizeHostname().
function hostnameDiag(v) {
    const brut = String(v || '');
    const deplie = brut.normalize('NFD').replace(/[̀-ͯ]/g, '');
    const enTirets = deplie.replace(/[\s_]+/g, '-');
    return {
        value: enTirets.replace(/[^A-Za-z0-9-]/g, '').replace(/-+/g, '-').replace(/^-+/, ''),
        accents: deplie !== brut,                                   // é → e
        espaces: /[\s_]/.test(deplie),                              // espace / _ → tiret
        retires: [...new Set(enTirets.match(/[^A-Za-z0-9-]/g) || [])],  // . @ / … → supprimés
    };
}

// Champ hostname : nettoie à la frappe ET explique le nettoyage sous le champ. Sans ce retour,
// la transformation est muette (anti-patron de l'échec silencieux appliqué à la saisie).
function ccHostnameInput(input) {
    const d = hostnameDiag(input.value);
    input.value = d.value;
    const hint = input.parentNode.querySelector('.cc-hostname-hint');
    if (!hint) return;
    const faits = [];
    if (d.espaces) faits.push(window.t('containers.create.hostname_fix_space'));
    if (d.accents) faits.push(window.t('containers.create.hostname_fix_accent'));
    if (d.retires.length) faits.push(window.t('containers.create.hostname_fix_drop')
                                       .replace('{chars}', d.retires.join(' ')));
    if (faits.length) {
        hint.textContent = window.t('containers.create.hostname_fixed').replace('{list}', faits.join(' · '));
        hint.classList.add('warn');
    } else {
        hint.textContent = window.t('containers.create.hostname_help');
        hint.classList.remove('warn');
    }
}

// Au blur seulement : forme canonique (tirets de bord retirés), pour que ce qui reste affiché
// soit exactement ce qui partira au serveur.
function ccHostnameBlur(input) {
    input.value = sanitizeHostname(input.value);
    const hint = input.parentNode.querySelector('.cc-hostname-hint');
    if (hint) { hint.textContent = window.t('containers.create.hostname_help'); hint.classList.remove('warn'); }
}

// Params par défaut déployés à la création quand un type est choisi.
// Réglages minimaux et sûrs ; on affine ensuite sur la page dédiée du type.
function defaultDeployParams(t) {
    const fmt = { width: 1280, height: 720, fps: 25 };
    switch (t) {
        case 'receiver_2110':
        case '2110_io':  return { video_count: 25, audio_count: 25 };
        default:                   return {};
    }
}

// ─── Assistant de création (étape 1 : type) ─────────────────────────────────
// Filtre la palette de chips par catégorie. '__all' montre tout ; '__none'
// (Container nu) reste toujours visible.
function selectCreateCat(btn) {
    const cat = btn.dataset.cat || '__all';
    document.querySelectorAll('#create-type-cats .dp-cat-btn').forEach(b =>
        b.classList.toggle('active', b === btn));
    document.querySelectorAll('#create-type-chips .create-type-chip').forEach(chip => {
        const c = chip.dataset.cat;
        chip.style.display = (cat === '__all' || c === cat || c === '__none') ? '' : 'none';
    });
}

// Type sélectionné pour l'entrée en cours d'édition (étape 2). null tant qu'aucun.
let _curType = null;   // {type, label, runtime}
let _curVis  = null;   // visibilité des champs dérivée du type

// Types média : un projet (volume média) peut être monté.
const _MEDIA_VOLUME_TYPES = new Set(['storage', 'player', 'recorder', 'stills']);

// Caches d'options (chargés une fois, réutilisés pour toutes les lignes).
let _ccNodes = null, _ccFormats = null, _ccProjects = null;
let _ccNodesAt = 0;      // horodatage du relevé affiché par le panneau de charge (cf. _ccAge)
// Mémoïsation sur la PROMESSE, pas sur le résultat : deux appels rapprochés (préchauffage à
// l'étape 1 + clic sur un type) partaient sinon en deux volées de requêtes — dont /api/nodes,
// qui sonde chaque nœud. Ici le second attend la première.
let _ccOptionsPromise = null;
function _ccEnsureOptions() {
    if (!_ccOptionsPromise) _ccOptionsPromise = (async () => {
        // En parallèle : les trois sources sont indépendantes, et /api/nodes domine le temps.
        await Promise.all([
            (async () => { try { _ccNodes = (await (await fetch('/api/nodes')).json()).nodes || []; } catch (e) { _ccNodes = []; } finally { _ccNodesAt = Date.now(); } })(),
            (async () => { try { _ccFormats = (await loadVideoFormats()) || []; } catch (e) { _ccFormats = []; } })(),
            (async () => { try { _ccProjects = (await (await fetch('/api/projects')).json()) || []; } catch (e) { _ccProjects = []; } })(),
        ]);
    })();
    return _ccOptionsPromise;
}

// Préchauffage : dès que le pointeur (ou le clavier) entre dans la palette de l'étape 1, on lance
// le chargement des options. L'utilisateur lit encore les types pendant que /api/nodes interroge
// le parc → dans le cas courant l'étape 2 s'ouvre déjà remplie. Aucune requête pour qui ne crée
// jamais de container : rien ne part au chargement de la page.
document.addEventListener('DOMContentLoaded', () => {
    const chips = document.getElementById('create-type-chips');
    if (!chips) return;
    const chauffer = () => _ccEnsureOptions();
    chips.addEventListener('pointerenter', chauffer, { once: true });
    chips.addEventListener('focusin', chauffer, { once: true });
});

// <option> du sélecteur de nœud. Full-Docker : tout container se crée SUR un nœud — plus de tête
// « — LXC (par défaut) — », qui laissait choisir « aucun nœud » pour finir sur un refus serveur.
function _ccNodeOptionsHtml() {
    if (!_ccNodes.length) return `<option value="">${window.t('containers.create.no_node')}</option>`;
    // On ne grise un nœud QUE si l'image RÉELLEMENT requise par le type choisi y manque
    // (kind = mtl|compute|media|webrtc, cf. plugins.image_kind). `n.images[kind].present` vient de
    // /api/nodes. Un mixer/player/multiview ne dépend PAS de bobi-mtl : plus de faux « image absente ».
    const kind = (_curType && _curType.imageKind) || 'compute';
    return _ccNodes.map(n => {
        const st = (n.images || {})[kind];
        const missing = st ? st.present === false : (n.image_ok === false && kind === 'mtl');
        return `<option value="${n.id}"${missing ? ' disabled' : ''}>${escapeHtml(n.name)} · ${escapeHtml(n.host)}${missing ? escapeHtml(window.t('js.node_image_missing')) : ''}</option>`;
    }).join('');
}
// ─── Panneau « charge des serveurs » de l'étape 2 ───────────────────────────────────────────
// Ce que le type choisi va PRENDRE sur le nœud, normalisé depuis `resources` du manifeste, en
// suivant exactement les règles de `docker_compute._apply_resources` :
//   · cœurs DÉDIÉS pris au pool  → seulement si `pin` ET `cores` (sinon le pool n'est pas entamé) ;
//   · sinon `cores` est un QUOTA --cpus dans le pool partagé (ça borne une part, pas une place) ;
//   · `cores_per_1080p_input` → dimensionnement dynamique au déploiement : `cores` est un PLANCHER ;
//   · aucun bloc `resources` → pool partagé, aucune réservation (mixer, split, avsync…).
function _ccCost() {
    const r = (_curType && _curType.resources) || {};
    const cores = parseInt(r.cores) || 0;
    const pin = !!r.pin;
    return {
        // « Déclaré » = porte au moins une clé de RESSOURCE. Le bloc `resources` sert aussi à
        // autre chose (mixer n'y met que `signature_keys`) : tester sa simple présence ferait
        // passer un type sans profil pour un type profilé.
        declared: ['cores', 'memory', 'pin', 'gpu', 'gpu_optional', 'priority']
                    .some(k => r[k] !== undefined && r[k] !== null),
        cores, pin,
        pinned:  (pin && cores) ? cores : 0,          // ce qui est retiré du pool dédié
        plancher: (parseFloat(r.cores_per_1080p_input) || 0) > 0,   // « ≥ N », pas « N »
        memory: parseInt(r.memory) || 0,
        gpu: parseInt(r.gpu) || 0,
        gpuOptional: !!r.gpu_optional,
    };
}

// Pool de cœurs : UN CARRÉ PAR CŒUR — réservé, ajouté par cette création, libre. Un cœur est une
// chose qui se compte, pas une proportion : 9/12 se lit d'un regard sur douze cases, là où une
// barre continue demande de convertir une longueur en nombre.
// Au-delà de `cap` cases (pool de 87 cœurs sur dell-1), les carrés deviendraient des traits d'un
// pixel : on retombe alors sur la barre continue, qui reste juste à défaut d'être dénombrable.
// Les carrés disent des QUANTITÉS, pas des CPU précis : l'allocateur ne choisit les cœurs qu'au
// déploiement (NUMA, physiques d'abord) — aucune position ici ne prétend désigner un cpu.
function _ccPoolCells(used, add, total, opts) {
    const o = Object.assign({ cap: 32, cls: '' }, opts || {});
    if (!total) return '';
    const T = window.t;
    const ajout = Math.max(0, Math.min(add, total - used));
    const titre = `${used} ${T('containers.create.load.reserved')}`
        + (ajout ? ` · +${ajout} ${T('containers.create.load.q_added')}` : '')
        + ` · ${Math.max(0, total - used - ajout)} ${T('containers.create.load.free')}`;
    if (total > o.cap) {
        const p = v => Math.max(0, Math.min(100, (v / total) * 100));
        return `<span class="cc-bar ${o.cls}" title="${titre}">
            <span class="cc-bar-used" style="width:${p(used).toFixed(1)}%"></span>
            <span class="cc-bar-add"  style="width:${p(ajout).toFixed(1)}%"></span>
        </span>`;
    }
    let cells = '';
    for (let i = 0; i < total; i++) {
        const k = i < used ? 'is-used' : (i < used + ajout ? 'is-add' : 'is-free');
        cells += `<i class="cc-c ${k}"></i>`;
    }
    return `<span class="cc-cells ${o.cls}" title="${titre}" aria-hidden="true">${cells}</span>`;
}

// Conservé sous son nom d'origine : la barre reste le repli au-delà du seuil.
function _ccPoolBar(used, add, total) { return _ccPoolCells(used, add, total); }

function _ccPct(v) { return v == null ? '—' : `${Math.round(v)} %`; }

// Détail déplié d'un nœud : comptabilité du pool + charge mesurée + effet de la création.
function _ccNodeDetailHtml(n, cost) {
    const T = window.t;
    const pool = n.cores_pool || {};
    const total = pool.total || 0, used = pool.used || 0, free = pool.free || 0;
    const L = [];

    // 1. Pool de cœurs dédiés — la comptabilité (déterministe).
    if (cost.pinned) {
        const restant = Math.max(0, free - cost.pinned);
        const sature = cost.pinned > free;
        L.push(`<div class="cc-det-line">
            <span class="cc-det-k">${T('containers.create.load.pool')}</span>
            <span class="cc-det-v">${_ccPoolCells(used, cost.pinned, total, {cap: 96, cls: 'cc-cells-lg'})}
                <strong>${used}</strong>/${total} ${T('containers.create.load.reserved')}
                · ${free} ${T('containers.create.load.free')}</span></div>`);
        // Projection : uniquement quand le pool PEUT servir la demande. Sinon `allocate_cores`
        // échoue et rien n'est réservé — annoncer « après création 12/12 » serait faux.
        const apres = sature ? `→ <span class="cc-det-warn-inline">${T('containers.create.load.not_enough')}</span>`
            : `→ ${T('containers.create.load.after')} <strong>${used + cost.pinned}</strong>/${total}`
              + ` (${restant} ${T('containers.create.load.free')})`;
        L.push(`<div class="cc-det-line cc-det-add">
            <span class="cc-det-k">${T('containers.create.load.will_take')}</span>
            <span class="cc-det-v"><strong>${cost.plancher ? '≥ ' : ''}${cost.pinned}</strong>
                ${T('containers.create.load.dedicated_cores')} ${apres}</span></div>`);
        if (sature) {
            // Ce n'est PAS un refus : `effective_cpuset` retombe sur le pool partagé (et alerte).
            // Le dire ici évite la surprise d'un container qui démarre sans ses cœurs exclusifs.
            L.push(`<div class="cc-det-warn">${T('containers.create.load.pool_full')}</div>`);
        } else if (pool.physical_free === 0 && free > 0) {
            // Les cœurs « libres » restants sont les JUMEAUX HT de cœurs physiques déjà dédiés :
            // ils occupent une ligne de comptabilité, pas de la puissance de calcul.
            L.push(`<div class="cc-det-warn">${T('containers.create.load.ht_only')}</div>`);
        }
    } else if (cost.cores) {
        L.push(`<div class="cc-det-line cc-det-add">
            <span class="cc-det-k">${T('containers.create.load.will_take')}</span>
            <span class="cc-det-v">${T('containers.create.load.quota').replace('{n}', cost.cores)}</span></div>`);
        L.push(`<div class="cc-det-line">
            <span class="cc-det-k">${T('containers.create.load.pool')}</span>
            <span class="cc-det-v">${_ccPoolCells(used, 0, total, {cap: 96, cls: 'cc-cells-lg'})} <strong>${used}</strong>/${total}
                ${T('containers.create.load.reserved')} — ${T('containers.create.load.pool_untouched')}</span></div>`);
    } else {
        L.push(`<div class="cc-det-line cc-det-add">
            <span class="cc-det-k">${T('containers.create.load.will_take')}</span>
            <span class="cc-det-v">${T('containers.create.load.shared_only')}</span></div>`);
    }

    // 2. Charge MESURÉE — l'autre moitié de l'histoire : réserver n'est pas consommer.
    const scope = n.cpu_pct_scope === 'ordonnancables'
        ? T('containers.create.load.cpu_scope_sched') : T('containers.create.load.cpu_scope_machine');
    L.push(`<div class="cc-det-line">
        <span class="cc-det-k">${T('containers.create.load.cpu_measured')}</span>
        <span class="cc-det-v">${_ccPct(n.cpu_pct)}${n.cpu_pct == null ? '' : ` <span class="meta">(${scope})</span>`}
            ${n.mem_pct == null ? '' : ` · ${T('containers.create.load.ram')} ${_ccPct(n.mem_pct)}`}</span></div>`);

    // 3. GPU — seulement si le type en demande un (ou sait s'en servir).
    // Un GPU porte PLUSIEURS contextes : on montre les cartes et les contextes déjà posés, pas un
    // « libre/occupé » qui laisserait croire à une exclusivité qui n'existe pas.
    const g = n.gpu_pool || {};
    if (cost.gpu || cost.gpuOptional) {
        let v;
        if (g.count) {
            v = `${g.count} ${T('containers.create.load.gpu_cards')} · ${g.used || 0} ${T('containers.create.load.gpu_ctx')}`
              + (cost.gpu ? ` → +1 ${T('containers.create.load.gpu_ctx')}`
                          : ` (${T('containers.create.load.gpu_optional')})`);
        } else {
            v = cost.gpu ? `<span class="cc-det-warn-inline">${T('containers.create.load.gpu_none')}</span>`
                         : T('containers.create.load.gpu_none_ok');
        }
        L.push(`<div class="cc-det-line"><span class="cc-det-k">GPU</span>
            <span class="cc-det-v">${v}</span></div>`);
    }
    if (cost.memory) {
        L.push(`<div class="cc-det-line">
            <span class="cc-det-k">${T('containers.create.load.ram_limit')}</span>
            <span class="cc-det-v">${cost.memory} MB</span></div>`);
    }
    if (!cost.declared) {
        L.push(`<div class="cc-det-note">${T('containers.create.load.no_profile')}</div>`);
    }
    return `<div class="cc-det">${L.join('')}</div>`;
}

// Ligne REPLIÉE : elle doit répondre seule à « où est-ce que ça tient ? ». On y met donc la
// projection (pool avant → après), pas seulement l'état courant — sinon il faut déplier chaque
// serveur pour comparer, ce qui est exactement le geste qu'on veut éviter.
function _ccQuickPool(n, cost) {
    const T = window.t;
    const pool = n.cores_pool || {};
    const total = pool.total || 0, used = pool.used || 0, free = pool.free || 0;
    if (!total) return `<span class="cc-q-pool meta">—</span>`;
    const base = `${used}/${total}`;
    let suffixe, add = 0, cls = '', titre;
    if (cost.pinned && cost.pinned > free) {
        suffixe = `<span class="cc-det-warn-inline">${T('containers.create.load.q_nofit')}</span>`;
        cls = ' is-warn';
        titre = T('containers.create.load.pool_full');
    } else if (cost.pinned) {
        add = cost.pinned;
        // `≥` quand le type est dimensionné dynamiquement au déploiement (cores = plancher) :
        // annoncer un chiffre net serait un minimum présenté comme un total.
        suffixe = `→ <strong>${cost.plancher ? '≥' : ''}${used + add}</strong>/${total}`;
        titre = `${T('containers.create.load.will_take')} ${cost.plancher ? '≥ ' : ''}${cost.pinned} `
              + `${T('containers.create.load.dedicated_cores')}`;
    } else if (cost.cores) {
        suffixe = `<span class="meta">· ${T('containers.create.load.q_quota').replace('{n}', cost.cores)}</span>`;
        titre = T('containers.create.load.quota').replace('{n}', cost.cores);
    } else {
        suffixe = `<span class="meta">· ${T('containers.create.load.q_shared')}</span>`;
        titre = T('containers.create.load.shared_only');
    }
    return `<span class="cc-q-pool${cls}" title="${titre}">${_ccPoolBar(used, add, total)}${base} ${suffixe}</span>`;
}

// Colonne GPU de la ligne repliée — rendue MÊME vide quand le type n'en demande pas, pour que les
// colonnes restent alignées d'un serveur à l'autre (c'est ce qui rend la liste comparable d'un
// coup d'œil).
function _ccQuickGpu(n, cost) {
    if (!(cost.gpu || cost.gpuOptional)) return '<span></span>';
    const T = window.t;
    const g = n.gpu_pool || {};
    if (!g.count) {
        return cost.gpu
            ? `<span class="cc-q-gpu cc-det-warn-inline" title="${T('containers.create.load.gpu_none')}">GPU —</span>`
            : `<span class="cc-q-gpu meta" title="${T('containers.create.load.gpu_none_ok')}">GPU —</span>`;
    }
    return `<span class="cc-q-gpu meta" title="${g.count} ${T('containers.create.load.gpu_cards')} · `
         + `${g.used || 0} ${T('containers.create.load.gpu_ctx')}">GPU ${g.count}·${g.used || 0}</span>`;
}

// Liste des serveurs : un par ligne, charge visible sans clic ; le nœud OUVERT montre le détail.
function _ccNodesPanelHtml(selectedId, openId) {
    const T = window.t;
    // Pas de titre : le panneau s'ouvre SOUS le champ « Nœud », dont le libellé sert déjà.
    const head = '';
    if (!_ccNodes || !_ccNodes.length) return `<div class="meta">${T('containers.create.no_node')}</div>`;
    const cost = _ccCost();
    const kind = (_curType && _curType.imageKind) || 'compute';
    return head + _ccNodes.map(n => {
        const st = (n.images || {})[kind];
        const missing = st ? st.present === false : (n.image_ok === false && kind === 'mtl');
        const sel = String(n.id) === String(selectedId);
        const open = String(n.id) === String(openId);
        const pool = n.cores_pool || {};
        return `<div class="cc-node-item${sel ? ' is-sel' : ''}${missing ? ' is-off' : ''}">
            <button type="button" class="cc-node-btn" role="radio" aria-checked="${sel}"
                    aria-expanded="${open}" ${missing ? 'disabled' : ''}
                    onclick="ccPickNode(this, '${n.id}')">
                <span class="cc-node-dot ${n.online ? 'is-up' : 'is-down'}"
                      title="${n.online ? T('containers.create.load.online') : T('containers.create.load.offline')}"></span>
                <span class="cc-node-id">
                    <span class="cc-node-name">${escapeHtml(n.name)}</span>
                    <span class="cc-node-host meta">${escapeHtml(n.host || '')}</span>
                </span>
                ${_ccQuickPool(n, cost)}
                <span class="cc-q-cpu meta" title="${n.cpu_pct_scope === 'ordonnancables'
                    ? T('containers.create.load.cpu_scope_sched') : T('containers.create.load.cpu_scope_machine')}"
                    >${T('containers.create.load.cpu')} ${_ccPct(n.cpu_pct)}</span>
                ${_ccQuickGpu(n, cost)}
                ${missing ? `<span class="cc-node-flag">${T('containers.create.load.image_missing')}</span>` : '<span></span>'}
                <span class="cc-node-chev" aria-hidden="true">${open ? '▾' : '▸'}</span>
            </button>
            ${open && !missing ? _ccNodeDetailHtml(n, cost) : ''}
        </div>`;
    }).join('') + `<div class="cc-nodes-foot meta">
        ${T('containers.create.load.sampled').replace('{age}', _ccAge())}
        <button type="button" class="btn btn-ghost cc-nodes-refresh"
                onclick="ccRefreshNodes(this)">${T('containers.create.load.refresh')}</button>
    </div>`;
}

// Âge du relevé. Il est DIT, pas sous-entendu : `/api/nodes` coûte plusieurs secondes (il sonde
// chaque nœud), donc il n'est appelé qu'une fois par page — un chiffre de charge vieux de dix
// minutes affiché comme s'il était live serait pire que pas de chiffre du tout.
function _ccAge() {
    const s = Math.max(0, Math.round((Date.now() - _ccNodesAt) / 1000));
    if (s < 60) return `${s} s`;
    return `${Math.round(s / 60)} min`;
}

// Rafraîchissement MANUEL du relevé (pas d'auto-refresh : la route est trop coûteuse pour être
// pollée, cf. _ccEnsureOptions). Reconstruit aussi les <option> : la disponibilité des images
// peut avoir changé.
async function ccRefreshNodes(btn) {
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try { _ccNodes = (await (await fetch('/api/nodes')).json()).nodes || []; } catch (e) { /* on garde le relevé précédent */ }
    _ccNodesAt = Date.now();
    document.querySelectorAll('#create-step2-rows .create-row').forEach(row => {
        const sel = row.querySelector('.cc-node-sel');
        if (sel) {
            const garde = sel.value;
            sel.innerHTML = _ccNodeOptionsHtml();
            sel.value = garde;
        }
        _ccSyncNodePanel(row);
    });
}

// Le panneau de charge est OPTIONNEL, par ligne. Empiler dix containers d'un coup ne doit pas
// afficher dix listes de serveurs : la vue par défaut reste le menu déroulant seul, et le panneau
// s'ouvre à la demande. La dernière préférence est mémorisée — quelqu'un qui veut toujours voir la
// charge ne doit pas la redemander à chaque ligne — mais elle ne s'applique qu'aux lignes NEUVES.
function _ccLoadPref() { return localStorage.getItem('mxl.cc.load') === '1'; }

function ccToggleLoad(btn) {
    const row = btn.closest('.create-row');
    const panel = row.querySelector('.cc-nodes');
    const ouvert = panel.dataset.shown !== '1';
    panel.dataset.shown = ouvert ? '1' : '0';
    try { localStorage.setItem('mxl.cc.load', ouvert ? '1' : '0'); } catch (e) { /* mode privé */ }
    _ccSyncNodePanel(row);
    // Le panneau ne sert à rien s'il n'est pas rempli : au premier affichage sur une page où
    // l'utilisateur n'a pas encore ouvert l'étape 2, les options peuvent manquer.
    if (ouvert && _ccNodes === null) _ccEnsureOptions().then(() => _ccSyncNodePanel(row));
}

// ─── « La charge mérite un coup d'œil » ───────────────────────────────────────
// Le panneau est REPLIÉ par défaut — empiler dix containers ne doit pas afficher dix tableaux
// de charge. Mais replié, il ne dit plus rien : y compris quand il aurait quelque chose à dire.
// Le bouton porte donc un état d'attention, et il DIT POURQUOI. Un repère qui clignote sans
// s'expliquer est du bruit : on apprend à l'ignorer, et le jour où il compte on ne le voit plus.
// Même règle que les pastilles de statut du produit — le texte fait foi, la couleur accompagne.
// L'ordre est celui de la gravité : ce qui EMPÊCHE de créer avant ce qui dégrade.
function _ccNodeAlerte(n, cost) {
    if (!n) return null;
    const T = window.t;
    const kind = (_curType && _curType.imageKind) || 'compute';
    const st = (n.images || {})[kind];
    const imageAbsente = st ? st.present === false : (n.image_ok === false && kind === 'mtl');
    if (imageAbsente) return T('containers.create.load.attn_image');
    if (n.online === false) return T('containers.create.load.attn_offline');
    // GPU EXIGÉ (pas seulement souhaité) sur un nœud sans carte : le déploiement échouera.
    if (cost.gpu && !((n.gpu_pool || {}).count)) return T('containers.create.load.attn_gpu');
    // Pool de cœurs dédiés insuffisant. Ce n'est pas un refus — `effective_cpuset` retombe sur le
    // pool partagé — mais le container démarrera SANS ses cœurs exclusifs : à savoir avant, pas après.
    const libres = (n.cores_pool || {}).free || 0;
    if (cost.pinned && cost.pinned > libres) {
        return T('containers.create.load.attn_pool')
            .replace('{n}', cost.pinned).replace('{free}', libres);
    }
    return null;
}

// (Re)dessine le panneau d'une ligne, aligné sur la valeur du <select> (source de vérité).
function _ccSyncNodePanel(row, openId) {
    const panel = row.querySelector('.cc-nodes');
    const sel   = row.querySelector('.cc-node-sel');
    const btn   = row.querySelector('.cc-load-toggle');
    if (!panel || !sel) return;
    // Signal d'attention : calculé dans les DEUX cas, montré seulement quand le panneau est
    // replié — déplié, l'information est déjà sous les yeux, insister deviendrait du harcèlement.
    if (btn) {
        const _nd = (_ccNodes || []).find(x => String(x.id) === String(sel.value));
        const _motif = _ccNodeAlerte(_nd, _ccCost());
        const _montrer = !!_motif && panel.dataset.shown !== '1';
        btn.classList.toggle('is-attn', _montrer);
        // Le motif REMPLACE l'infobulle générique : au survol comme au lecteur d'écran, on
        // apprend ce qui ne va pas sans avoir à déplier.
        // ⚠ `window.t` rend la CLÉ quand la traduction manque, et le catalogue JS est figé au
        // démarrage de l'orchestrateur : entre l'ajout d'une clé et le redémarrage, l'infobulle
        // afficherait « containers.create.load.attn_pool » à l'utilisateur. On préfère alors
        // l'infobulle générique — le signal visuel, lui, reste utile sans le texte.
        const _traduit = _motif && _motif.indexOf('containers.create.load.') !== 0 ? _motif : null;
        btn.title = _traduit || window.t('containers.create.load.toggle_title');
    }
    if (panel.dataset.shown !== '1') {
        panel.style.display = 'none';
        panel.innerHTML = '';                       // rien à garder en DOM tant que c'est replié
        if (btn) btn.setAttribute('aria-expanded', 'false');
        return;
    }
    if (btn) btn.setAttribute('aria-expanded', 'true');
    panel.style.display = '';
    panel.setAttribute('role', 'radiogroup');
    panel.setAttribute('aria-label', window.t('containers.create.load.aria'));
    const open = openId !== undefined ? openId : (panel.dataset.open || sel.value);
    panel.dataset.open = open == null ? '' : open;
    panel.innerHTML = _ccNodesPanelHtml(sel.value, open);
}

// Clic sur un serveur : le sélectionne ET déplie son détail. Re-cliquer le nœud ouvert le replie
// (la sélection, elle, reste — replier n'est pas désélectionner).
function ccPickNode(btn, nodeId) {
    const row = btn.closest('.create-row');
    const sel = row.querySelector('.cc-node-sel');
    const panel = row.querySelector('.cc-nodes');
    const etait = panel.dataset.open;
    const memeNoeud = String(etait) === String(nodeId) && String(sel.value) === String(nodeId);
    sel.value = nodeId;
    _ccSyncNodePanel(row, memeNoeud ? '' : nodeId);
}

// Changement par le <select> (clavier, ou clonage de ligne) → le panneau suit.
function ccNodeSelChange(sel) {
    const row = sel.closest('.create-row');
    if (row) _ccSyncNodePanel(row, sel.value);
}

function _ccFormatOptionsHtml() {
    const def = window._videoFormatDefault || '';
    const head = `<option value="">${window.t('js.default_from_settings')
        .replace('{suffix}', def ? ' (' + escapeHtml(def) + ')' : '')}</option>`;
    return head + _ccFormats.map(f =>
        `<option value="${escapeHtml(f.label)}" data-w="${f.w}" data-h="${f.h}" data-fps="${f.fps}"`
        + ` data-scan="${f.scan}" data-chroma="${f.chroma}" data-bd="${f.bit_depth}"`
        + ` data-colorimetry="${f.colorimetry}">${escapeHtml(f.label)}</option>`).join('');
}
function _ccProjectOptionsHtml() {
    return `<option value="">${window.t('containers.create.none_opt')}</option>` +
        _ccProjects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
}

// Override de format lu sur la ligne (ou null si « — Défaut Réglages — »).
function _ccRowFormatOverride(row) {
    const sel = row.querySelector('.cc-format-sel');
    if (!sel || !sel.value) return null;
    const o = sel.selectedOptions[0];
    return {
        width:       parseInt(o.dataset.w)     || undefined,
        height:      parseInt(o.dataset.h)     || undefined,
        fps:         parseFloat(o.dataset.fps) || undefined,
        scan:        o.dataset.scan            || undefined,
        chroma:      o.dataset.chroma          || undefined,
        bit_depth:   parseInt(o.dataset.bd)    || undefined,
        colorimetry: o.dataset.colorimetry     || undefined,
    };
}

// Choix d'un type (chip) → calcule la visibilité des champs, charge les options,
// remet l'étape 2 à une première ligne.
async function selectCreateType(chip) {
    const type    = chip.dataset.type || '';
    const label   = chip.dataset.label || 'Container nu';
    const runtime = chip.dataset.runtime || 'docker';
    const imageKind = chip.dataset.imageKind || 'compute';   // image requise par le type (mtl|compute|media|webrtc)
    let resources = {};
    try { resources = JSON.parse(chip.dataset.resources || '{}') || {}; } catch (e) { resources = {}; }
    document.querySelectorAll('.create-type-chip').forEach(c => c.classList.toggle('active', c === chip));
    _curType = { type, label, runtime, imageKind, resources };

    // Full-Docker : tout type se crée sur un nœud. `_curVis` ne porte plus que ce qui VARIE
    // encore d'un type à l'autre.
    _curVis = {
        showFormat:   _typeNeedsFormat(type),
        // Projet : proposé pour tous les types SAUF l'infra liée au nœud (2110_io :
        // un moteur par nœud, partagé entre projets via les ports). Le rattachement
        // porte le bind média, le niveau de tally du projet et l'appartenance.
        showProject:  type !== '2110_io',
    };

    const badge = document.getElementById('create-step2-type');
    if (badge) badge.innerHTML = type
        ? modeBadge({ type })
        : '<span class="mode-badge mode-none">container nu</span>';

    document.getElementById('create-step2').style.display = '';
    _createSetWarn('');

    // L'étape 2 s'ouvrait VIDE le temps de `_ccEnsureOptions()`. Ce n'est pas un calcul local :
    // /api/nodes fait un aller-retour PAR NŒUD (ré-enregistrement + état des images Docker), donc
    // plusieurs secondes sur un parc de quelques machines. Sans un mot, l'attente se lit comme une
    // page cassée (remonté par un testeur). On annonce donc ce qu'on attend, à la place où la
    // première ligne va apparaître.
    const host = document.getElementById('create-step2-rows');
    host.innerHTML = `<div class="meta cc-loading">${window.t('containers.create.loading_options')}</div>`;
    await _ccEnsureOptions();

    host.innerHTML = '';
    host.appendChild(_ccBuildRow(0));
    _ccRefreshRowButtons();
}

// Hostname par défaut de la ligne d'index i (slug du type, incrémenté).
function _ccDefaultHostname(i) {
    const base = sanitizeHostname(_curType ? _curType.type : '') || 'container';
    return i === 0 ? base : `${base}-${i + 1}`;
}

// Construit une ligne (un container) depuis le gabarit, options + visibilité injectées.
// `values` (optionnel) clone cœurs/RAM/nœud/format/projet depuis une ligne existante.
function _ccBuildRow(index, values) {
    const tpl = document.getElementById('cc-row-tpl');
    const row = tpl.content.firstElementChild.cloneNode(true);
    const v = _curVis || {};

    row.querySelector('.cc-hostname').value = _ccDefaultHostname(index);

    // Libellé du champ : posé par le gabarit (donc traduit) — il ne dépend plus du runtime.
    row.querySelector('.cc-node-sel').innerHTML = _ccNodeOptionsHtml();

    const fmtField = row.querySelector('.cc-format');
    fmtField.style.display = v.showFormat ? '' : 'none';
    if (v.showFormat) row.querySelector('.cc-format-sel').innerHTML = _ccFormatOptionsHtml();

    const projField = row.querySelector('.cc-project');
    projField.style.display = v.showProject ? '' : 'none';
    if (v.showProject) row.querySelector('.cc-project-sel').innerHTML = _ccProjectOptionsHtml();

    if (values) {
        const set = (sel, val) => { const el = row.querySelector(sel); if (el && val != null) el.value = val; };
        set('.cc-node-sel', values.node);
        set('.cc-format-sel', values.format);
        set('.cc-project-sel', values.project);
    }
    // Panneau de charge : rendu APRÈS l'application de `values` (il reflète la valeur du select).
    // Fermé sauf préférence contraire ; le détail d'un serveur ne s'ouvre d'office que sur la
    // première ligne (sur une ligne clonée, l'utilisateur a déjà vu le détail sur la précédente).
    const panel = row.querySelector('.cc-nodes');
    if (panel) panel.dataset.shown = _ccLoadPref() ? '1' : '0';
    _ccSyncNodePanel(row, index === 0 ? undefined : '');
    return row;
}

// « + Container » : ajoute une ligne du même type, en clonant les réglages de la
// dernière (hostname auto-incrémenté). Empile vite N containers identiques.
function addStep2Row() {
    const host = document.getElementById('create-step2-rows');
    const rows = host.querySelectorAll('.create-row');
    const last = rows[rows.length - 1];
    const values = last ? {
        node:    last.querySelector('.cc-node-sel')?.value,
        format:  last.querySelector('.cc-format-sel')?.value,
        project: last.querySelector('.cc-project-sel')?.value,
    } : null;
    host.appendChild(_ccBuildRow(rows.length, values));
    _ccRefreshRowButtons();
}

function removeStep2Row(btn) {
    const host = document.getElementById('create-step2-rows');
    if (host.querySelectorAll('.create-row').length <= 1) return;   // garder au moins une ligne
    btn.closest('.create-row')?.remove();
    _ccRefreshRowButtons();
}

// « + » seulement sur la dernière ligne ; « × » dès qu'il y a plusieurs lignes.
function _ccRefreshRowButtons() {
    const rows = [...document.querySelectorAll('#create-step2-rows .create-row')];
    rows.forEach((row, i) => {
        const add = row.querySelector('.cc-add');
        const del = row.querySelector('.cc-del');
        if (add) add.style.display = (i === rows.length - 1) ? '' : 'none';
        if (del) del.style.display = (rows.length > 1) ? '' : 'none';
    });
}

// ─── Panier de création ──────────────────────────────────────────────────────
// Chaque entrée empilée = UN container : {type, label, prefix, nodeId, projectId, params}.
// On lance toute la fournée d'un coup.
let _createCart = [];

// Bandeau d'erreur de l'assistant de création. Le nœud vit au niveau du WIZARD (cf. forms.html) :
// tant qu'il était dans l'étape 2, un refus émis après `_resetStep2()` (qui replie l'étape)
// s'écrivait dans un conteneur caché — refus MUET. On l'amène aussi dans le champ de vision :
// le panier peut être hors écran quand la liste est longue.
function _createSetWarn(msg) {
    const w = document.getElementById('create-warning');
    if (!w) return;
    w.textContent = msg || '';
    w.style.display = msg ? '' : 'none';
    if (msg) w.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// « Ajouter à la liste » : lit toutes les lignes de l'étape 2 → autant d'entrées
// dans le panier (une par container).
function addToCart() {
    if (!_curType) { _createSetWarn(window.t('js.create.pick_type_first')); return; }
    const rows = [...document.querySelectorAll('#create-step2-rows .create-row')];
    if (!rows.length) return;

    const lines = [];
    const seen = new Set();
    for (const row of rows) {
        const prefix = sanitizeHostname(row.querySelector('.cc-hostname').value);
        if (!prefix) { _createSetWarn(window.t('js.create.hostname_required')); return; }
        if (seen.has(prefix)) { _createSetWarn(window.t('js.hostname_dup_step2').replace('{h}', prefix)); return; }
        if (_createCart.some(l => l.prefix === prefix)) {
            _createSetWarn(window.t('js.hostname_already_listed').replace('{h}', prefix)); return;
        }
        seen.add(prefix);

        const nodeSel = row.querySelector('.cc-node-sel');
        const nodeId  = nodeSel && nodeSel.value ? parseInt(nodeSel.value) : null;
        if (!nodeId) { _createSetWarn(window.t('js.node_required').replace('{h}', prefix)); return; }

        const projectId = row.querySelector('.cc-project-sel')?.value || null;

        const params = _curType.type ? defaultDeployParams(_curType.type) : null;
        const fmt = _ccRowFormatOverride(row);   // override format (chemin nœud) ou null
        if (fmt && params) Object.assign(params, fmt);

        lines.push({
            type: _curType.type, label: _curType.label,
            prefix, nodeId, projectId, params,
        });
    }

    _createCart.push(...lines);
    _createSetWarn('');
    renderCart();
    _resetStep2();
}

// Réinitialise l'étape 2 pour saisir un autre type (la liste, elle, persiste).
function _resetStep2() {
    _curType = null; _curVis = null;
    document.querySelectorAll('.create-type-chip').forEach(c => c.classList.remove('active'));
    const host = document.getElementById('create-step2-rows'); if (host) host.innerHTML = '';
    const step2 = document.getElementById('create-step2'); if (step2) step2.style.display = 'none';
}

function removeCartLine(i) { _createCart.splice(i, 1); renderCart(); }

function viderPanier() { _createCart = []; renderCart(); }

function renderCart() {
    const zone  = document.getElementById('create-cart-zone');
    const tbody = document.getElementById('cart-tbody');
    if (!zone || !tbody) return;
    if (!_createCart.length) { zone.style.display = 'none'; tbody.innerHTML = ''; return; }
    zone.style.display = '';

    const total = _createCart.length;
    const totEl = document.getElementById('cart-total');
    if (totEl) totEl.textContent = total + ' container' + (total > 1 ? 's' : '');

    tbody.innerHTML = _createCart.map((l, i) => {
        const badge = modeBadge({ type: l.type });
        const res = window.t('js.cart_node').replace('{n}', l.nodeId || '?');
        return `<tr>
            <td>${badge}</td>
            <td>${escapeHtml(l.prefix)}</td>
            <td style="color:var(--text-muted)">${res}</td>
            <td><button class="btn btn-ghost btn-sm" onclick="removeCartLine(${i})">${window.t('js.cart_remove')}</button></td>
        </tr>`;
    }).join('');
}

// POST d'un container : un seul chemin, celui du nœud Docker (node_id).
// Les cœurs/RAM ne sont plus transmis — le profil `resources` du manifeste pilote le docker run.
function _postCreateOne(line, hostname) {
    const body = {
        hostname, deploy_type: line.type || null, deploy_params: line.params || null,
        node_id: line.nodeId,
        project_id: line.projectId ? parseInt(line.projectId) : null,
    };
    return fetch('/api/containers', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
}

// Lance toute la fournée : une entrée = un container. Contrôle des collisions,
// POST en parallèle, puis suivi dans #batch-box. Bouton « Création… » + toast.
async function lancerCreation() {
    if (!_createCart.length) { _createSetWarn(window.t('js.create.cart_empty')); return; }
    _createSetWarn('');

    const plan = _createCart.map(l => ({ line: l, hostname: l.prefix }));
    const seen = new Set(plan.map(p => p.hostname));

    // Collisions avec les containers existants (les doublons internes sont bloqués à l'ajout)
    try {
        const r = await fetch('/api/containers');
        if (r.ok) {
            // Comparaison INSENSIBLE À LA CASSE, alignée sur app/hostnames.py:valider() :
            // « Camera1 » et « camera1 » sont deux hostnames distincts en base mais produiraient
            // deux jeux de flux MXL indiscernables à l'œil.
            const existing = new Set((await r.json()).map(c => (c.hostname || '').toLowerCase()).filter(Boolean));
            const clash = [...seen].filter(h => existing.has(h.toLowerCase()));
            if (clash.length) {
                // Ce chemin court-circuite le POST : sans toast, un utilisateur qui a le panier
                // hors écran n'avait AUCUN signe que le clic n'avait rien fait (le toast
                // « Création lancée » ci-dessous n'est pas encore émis).
                _createSetWarn(window.t('js.hostname_taken_warn').replace('{list}', clash.join(', ')));
                showWireToast(window.t('js.hostname_taken_toast').replace('{list}', clash.join(', ')), 'error');
                return;
            }
        }
    } catch (e) { /* best-effort : on laisse passer si le check échoue */ }

    // Retour visuel immédiat
    const btn = document.getElementById('cart-launch-btn');
    const oldLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = window.t('js.creating'); }
    showWireToast(window.t('js.create_launched'), 'ok');

    // Alimente le suivi batch (mix de hostnames/types)
    const allHn = plan.map(p => p.hostname);
    plan.forEach(p => { _batchTypes[p.hostname] = p.line.type; });
    _batchHostnames = [...allHn, ..._batchHostnames.filter(h => !allHn.includes(h))].slice(0, _BATCH_MAX);
    _batchSave();
    const batchBox = document.getElementById('batch-box');
    if (batchBox) batchBox.style.display = '';
    _batchLabel();
    _batchRenderTable([]);

    // Lance toutes les créations en parallèle. Le REFUS du serveur (400 hostname invalide,
    // 409 doublon) était jusqu'ici avalé par un `.catch(() => {})` : le conteneur n'apparaissait
    // simplement jamais dans le suivi, sans un mot d'explication. On remonte l'erreur.
    const refus = [];
    await Promise.all(plan.map(p => _postCreateOne(p.line, p.hostname).then(async r => {
        if (r && r.ok) return;
        let msg = r ? ('HTTP ' + r.status) : window.t('js.request_failed');
        try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (_) {}
        refus.push(p.hostname + ' : ' + msg);
    }).catch(e => refus.push(p.hostname + ' : ' + (e && e.message ? e.message : window.t('js.request_failed'))))));
    if (refus.length) {
        _createSetWarn(window.t('js.create_refused').replace('{detail}', refus.join(' | ')));
        // Les refusés ne seront jamais vus par le polling : les retirer du suivi, sinon le batch
        // reste « en attente » indéfiniment sur un conteneur qui n'existera pas.
        const kos = new Set(refus.map(l => l.split(' : ')[0]));
        _batchHostnames = _batchHostnames.filter(h => !kos.has(h));
        _batchSave();
        showWireToast(window.t('js.create_refused_count').replace('{n}', refus.length), 'error');
    }

    batchBox?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    _batchStartPolling();

    // Panier vidé, bouton restauré
    _createCart = [];
    renderCart();
    if (btn) { btn.disabled = false; btn.textContent = oldLabel; }
}

// ─── Suivi batch (polling /api/containers) ───────────────────

// Vrai tant que l'erreur COURANTE a déjà été amenée à l'écran (anti-répétition du défilement).
let _batchErrVue = false;
let _batchPollDebut = null;        // horodatage du début de CETTE surveillance
const _BATCH_POLL_MAX_MS = 10 * 60 * 1000;   // au-delà, plus rien n'est en cours de déploiement

function _batchStartPolling() {
    if (_survTimer) clearInterval(_survTimer);
    _batchErrVue = false;          // nouveau lot : une erreur mérite à nouveau d'être montrée
    _batchPollDebut = Date.now();
    // Polls rapides au départ (1 s × 6), puis cadence normale (3 s)
    let fastCount = 0;
    _survTimer = setInterval(() => {
        _batchPoll();
        fastCount++;
        if (fastCount === 6 && _survTimer) {
            clearInterval(_survTimer);
            _survTimer = setInterval(_batchPoll, 3000);
        }
    }, 1000);
    _batchPoll();
}

let _batchEnVol = false;
async function _batchPoll() {
    if (!_batchHostnames.length) return;
    // Garde de recouvrement posée ICI plutôt que sur le timer : celui-ci est à deux phases
    // (1 s ×6 puis 3 s) et référencé en quatre endroits — le réécrire coûterait plus cher que la
    // protection ne rapporte. L'effet est le même : jamais deux passes en vol (cf. MXLPoll).
    if (_batchEnVol) return;
    _batchEnVol = true;
    try {
        return await _batchPollImpl();
    } finally {
        _batchEnVol = false;
    }
}
async function _batchPollImpl() {
    try {
        const [rC, rA] = await Promise.all([
            fetch('/api/containers'),
            fetch('/api/alerts?limit=200'),
        ]);
        if (!rC.ok) return;
        const allContainers = await rC.json();
        const allAlerts     = rA.ok ? await rA.json() : [];

        const batch = allContainers.filter(c => _batchHostnames.includes(c.hostname));
        _batchRenderTable(batch, allAlerts);

        // Fin du batch : tous running/script_stopped ou en erreur (plus rien à attendre)
        const allSettled = _batchHostnames.every(hn => {
            const c = batch.find(x => x.hostname === hn);
            if (c && (c.status === 'running' || c.status === 'script_stopped')) return true;
            return _lastAlertFor(hn, allAlerts)?.niveau === 'error';
        });
        const hasError = _batchHostnames.some(hn => _lastAlertFor(hn, allAlerts)?.niveau === 'error');
        // ★ AMENER À L'ÉCRAN UNE FOIS, PAS À CHAQUE TOUR (2026-08-30).
        // Ce `scrollIntoView` était réémis à CHAQUE passage du poll (1 s puis 3 s) tant qu'un
        // conteneur du lot portait une alerte d'erreur. Comme `_batchHostnames` est un HISTORIQUE
        // accumulé et persisté, une erreur ancienne suffisait : l'utilisateur remontait la page,
        // elle redescendait trois secondes plus tard, indéfiniment. Symptôme remonté depuis la
        // page de création (« ça redescend à chaque fois qu'on remonte »).
        // Une notification se donne UNE FOIS ; la répéter n'informe pas davantage, elle empêche
        // de travailler. On ne défile donc que sur la TRANSITION vers l'erreur.
        if (hasError && !_batchErrVue) {
            _batchErrVue = true;
            document.getElementById('batch-box')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else if (!hasError) {
            _batchErrVue = false;      // l'erreur a disparu : une NOUVELLE mérite à nouveau l'écran
        }
        // ★ ET BORNER LA SURVEILLANCE. `allSettled` exige que CHAQUE hostname soit running,
        // script_stopped, ou en erreur. Un conteneur du lot DÉTRUIT depuis n'est aucun des trois :
        // il n'existe plus dans /api/containers et sa dernière alerte n'est pas une erreur. La
        // condition ne devenait donc jamais vraie et le poll interrogeait /api/containers +
        // /api/alerts toutes les 3 s pour toujours — c'est ce qui rendait le défilement éternel.
        if (allSettled || (_batchPollDebut && Date.now() - _batchPollDebut > _BATCH_POLL_MAX_MS)) {
            if (_survTimer) { clearInterval(_survTimer); _survTimer = null; }
        }
    } catch(e) { /* silencieux */ }
}

function _alerteParams(a) {
    // `msg_params` arrive en JSON (colonne texte). Illisible → null : une alerte doit rester
    // affichable même si ses paramètres sont abîmés.
    if (!a || !a.msg_params) return null;
    if (typeof a.msg_params === 'object') return a.msg_params;
    try { return JSON.parse(a.msg_params); } catch { return null; }
}

// Étapes du suivi de création par lot, indexées par CLÉ d'alerte. Le suivi branchait auparavant
// sur le TEXTE français (`message.includes('cloné')`, …) : reformuler un libellé figeait le lot
// sans lever la moindre erreur, et la branche « cloné » ne correspondait déjà plus à aucun
// producteur depuis le retrait du backend LXC — l'étape « Configuration » ne s'affichait jamais.
// Ajouter une étape = ajouter une clé ici ET la produire côté orchestrateur, rien d'autre.
const _ETAPES_LOT = {
    'alert.deploy.compute.cree':    'containers.batch.starting',
    'alert.deploy.script.en_cours': 'containers.batch.configuring',
};

function _lastAlertFor(hostname, alerts) {
    // Alerte la plus récente concernant CE conteneur. Une ligne keyée porte son hostname dans
    // `msg_params.h` → égalité EXACTE. Le repli par sous-chaîne ne sert plus qu'aux lignes non
    // encore keyées, et il confond `bobi-cmp-14` avec `bobi-cmp-142` : ne jamais l'appliquer à
    // une ligne qui, elle, sait de quel conteneur elle parle.
    for (const a of alerts) {
        const p = _alerteParams(a);
        if (p && p.h) {
            if (p.h === hostname) return a;
            continue;
        }
        if (a.message && a.message.includes(`(${hostname})`)) return a;
        if (a.message && a.message.includes(` ${hostname} `)) return a;
    }
    return null;
}

function _batchRenderTable(batch, alerts) {
    const tbody = document.getElementById('batch-tbody');
    if (!tbody) return;
    const rows = _batchHostnames.map(hn => {
        const c    = batch.find(x => x.hostname === hn);
        const vmid = c ? c.vmid : '…';
        const dc = (() => { try { return c && c.deploy_config ? JSON.parse(c.deploy_config) : null; } catch { return null; } })();

        // Cellule Type : badge du type déployé, sinon le type demandé « à venir », sinon —
        const reqType = _batchTypes[hn];
        let typeCell;
        if (dc && dc.type) {
            typeCell = modeBadge(dc);
        } else if (reqType) {
            typeCell = `<span class="mode-badge mode-none" title="${window.t('containers.batch.pending_title')}">${modeLabel(reqType)} · ${window.t('containers.batch.upcoming')}</span>`;
        } else {
            typeCell = '<span style="color:var(--text-muted)">—</span>';
        }

        let status, color, detail = '';
        if (c) {
            if (c.status === 'script_stopped' && !dc) {
                status = window.t('containers.card.ready');
                color  = 'var(--status-running-fg)';
            } else if (c.status === 'stopped') {
                const st = window.t('containers.status.stopped');
                status = st.charAt(0).toUpperCase() + st.slice(1);
                color  = 'var(--status-stopped-fg)';
            } else {
                status = c.status;
                color  = 'var(--text-muted)';
            }
        } else {
            const a = alerts ? _lastAlertFor(hn, alerts) : null;
            if (a && a.niveau === 'error') {
                status = window.t('containers.batch.fail');
                color  = 'var(--status-error-fg, #ee0055)';
                detail = a.message;
            } else if (a && _ETAPES_LOT[a.msg_key]) {
                status = window.t(_ETAPES_LOT[a.msg_key]);
                color  = (a.msg_key === 'alert.deploy.compute.cree')
                    ? 'var(--status-running-fg)' : 'var(--text-muted)';
            } else {
                status = window.t('containers.batch.init');
                color  = 'var(--text-muted)';
            }
        }

        const isError = (status === window.t('containers.batch.fail'));
        const detailHtml = (isError && detail)
            ? `<div style="font-size:0.85em; color:${color}; margin-top:2px; white-space:normal">${detail}</div>`
            : '';
        const rowBg = isError ? 'background:rgba(238,0,85,0.08);' : '';
        // Container prêt → bouton vers sa configuration.
        const ready = c && (c.status === 'running' || c.status === 'script_stopped');
        const cfgBtn = ready
            ? `<button class="btn btn-purple" style="padding:1px 8px; font-size:0.85em" onclick="configurerDepuisBatch(${c.vmid})">${window.t('containers.card.configure')}</button>`
            : '';
        return `<tr style="${rowBg}">
            <td style="padding:4px 8px">${hn}</td>
            <td style="padding:4px 8px; color:var(--text-muted)">${vmid}</td>
            <td style="padding:4px 8px">${typeCell}</td>
            <td style="padding:4px 8px; color:${color}">${status}${detailHtml}</td>
            <td style="padding:4px 8px">${cfgBtn}</td>
        </tr>`;
    });
    tbody.innerHTML = rows.join('');
}

// Stub creer() conservé pour compatibilité éventuelle
async function creer() { await lancerCreation(); }

async function detruire(vmid) {
    if (!confirm(window.t('js.destroy.confirm') + ' ' + vmid + ' ?')) return;
    await fetch('/api/containers/' + vmid, { method: 'DELETE' });
}

async function restart(vmid) {
    await fetch('/api/containers/' + vmid + '/restart', { method: 'POST' });
}

async function stopScript(vmid)  { await fetch('/api/containers/' + vmid + '/stop_script',  { method: 'POST' }); }
async function startScript(vmid) { await fetch('/api/containers/' + vmid + '/start_script', { method: 'POST' }); }

async function saveContainerProject() {
    if (selectedDeployVmid === null) return;
    const sel    = document.querySelector('#deploy-palette .dp-project-sel');
    const status = document.querySelector('#deploy-palette .dp-project-status');
    if (!sel) return;
    const projName = sel.options[sel.selectedIndex]?.text || '—';
    if (!confirm(window.t('js.project.media_restart') + '\n\n' + window.t('js.label.project') + ' : ' + projName + '\n\n' + window.t('js.confirm.continue'))) return;
    if (status) status.textContent = window.t('js.status.restarting');
    const project_id = sel.value ? parseInt(sel.value) : null;
    const r = await fetch('/api/containers/' + selectedDeployVmid + '/project', {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({project_id})
    });
    if (status) status.textContent = r.ok
        ? window.t('js.restart_launched')
        : window.t('js.request_error');
}

// ─── Palette de déploiement (master-detail) ──────────────────

async function selectDeployContainer(vmid) {
    selectedDeployVmid = vmid;
    const r = await fetch('/api/containers/' + vmid + '/config');
    const c = await r.json();
    renderDeployPalette(c);
    _syncConfigureExpanded(vmid);
}

function fermerPalette() {
    selectedDeployVmid = null;
    const host = document.getElementById('deploy-palette');
    if (host) {
        host.style.display = 'none';
        host.innerHTML = '';
    }
    _syncConfigureExpanded(null);
}

// Met à jour aria-expanded sur tous les boutons "Configurer" de la grille :
// true uniquement pour la card du vmid actif, false partout ailleurs.
function _syncConfigureExpanded(activeVmid) {
    document.querySelectorAll('[data-action="configure"]').forEach(btn => {
        const card = btn.closest('.card');
        const on = !!(card && activeVmid != null && card.id === 'c-' + activeVmid);
        btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
}

function renderDeployPalette(c) {
    const host = document.getElementById('deploy-palette');
    const tpl  = document.getElementById('deploy-palette-tpl');
    if (!host || !tpl) return;

    host.style.display = 'block';
    host.innerHTML = '';
    host.appendChild(tpl.content.cloneNode(true));

    host.querySelector('.dp-hostname').textContent = c.hostname;
    host.querySelector('.dp-vmid').textContent     = c.vmid;
    const st = host.querySelector('.dp-status');
    st.textContent = c.status || 'unknown';
    st.classList.add(c.status || 'unknown');

    // Pré-remplit le bloc Ressources du conteneur
    host.querySelector('.dp-r-cores').value  = c.cores  || 2;
    host.querySelector('.dp-r-memory').value = c.memory || 2048;

    // Section projet : visible pour les plugins media_volume
    const dcType = (() => { try { return (typeof c.deploy_config==='string' ? JSON.parse(c.deploy_config) : c.deploy_config)?.type || ''; } catch(e){ return ''; } })();
    if (_MEDIA_VOLUME_TYPES.has(dcType)) {
        const sect = host.querySelector('.dp-project-section');
        if (sect) {
            sect.style.display = '';
            const sel = sect.querySelector('.dp-project-sel');
            const hint = sect.querySelector('.dp-project-hint');
            fetch('/api/projects').then(r=>r.json()).then(projs => {
                sel.innerHTML = `<option value="">${window.t('containers.palette.project_all')}</option>` +
                    (projs||[]).map(p=>`<option value="${p.id}"${c.project_id==p.id?' selected':''}>${p.name}</option>`).join('');
                if (c.project_id) hint.textContent = (projs||[]).find(p=>p.id==c.project_id)?.name || window.t('js.project_assigned');
            }).catch(()=>{});
        }
    }
    host.querySelector('.dp-r-pinned').value = c.pinned_cores || '';
    if (c.assigned_vf) {
        host.querySelector('.dp-r-pinned-hint').textContent =
            window.t('js.vf_assigned').replace('{vf}', c.assigned_vf);
    }

    // Décode la config existante si présente
    let dc = null;
    if (c.deploy_config) {
        try { dc = typeof c.deploy_config === 'string'
            ? JSON.parse(c.deploy_config) : c.deploy_config; } catch(e) {}
    }
    const type = (dc && dc.type) || 'receiver_2110';
    const p    = (dc && dc.params) || {};

    buildDpCatNav(host);   // niveau 1 : onglets de catégorie

    host.querySelector('.dp-type').value = type;
    // Premier déploiement (pas de config) → chips visibles. Déjà déployé → ligne récap.
    applyDeployType(host, type, { collapsed: !!dc });
    if (_isRx2110(type))             restaurerReceiver(host, p);
    else                                 restorePluginConfig(host, type, p);  // plugins (Tier 1)
    // Présélectionne la version déployée dans le sélecteur (si plusieurs versions).
    renderPluginVersions(host, type, p.plugin_version);

    // Sélection du preset format correspondant à la config existante
    let fmtW = 1280, fmtH = 720, fmtFps = null, fmtScan = null;
    if (_isRx2110(type))                     { fmtW = p.width||1280;       fmtH = p.height||720;      fmtFps = p.fps;       fmtScan = p.scan; }
    else                                { fmtW = p.width||1280;       fmtH = p.height||720;      fmtFps = p.fps;       fmtScan = p.scan; }  // plugins

    loadVideoFormats().then(() => {
        populateFormatSelect(host);
        _selectMatchingPreset(host, fmtW, fmtH, fmtFps, fmtScan, dc ? p : null);
    });
}

function _pluginBadgeClass(t) {
    return (window.MXL_PLUGINS?.[t]?.badge_class) || '';
}

// ─── Tier 1 : champs de config déclaratifs des plugins (config_schema) ───────
function _cfEsc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function _pluginFieldHtml(f){
    const k = _cfEsc(f.key), lbl = _cfEsc(f.label || f.key);
    const ph = f.placeholder != null ? ` placeholder="${_cfEsc(f.placeholder)}"` : '';
    // Valeur par défaut (default) pré-remplie dans le champ.
    const val = (f.default != null) ? ` value="${_cfEsc(f.default)}"` : '';
    let input;
    // Texte d'aide du champ (config_schema `help`) : rendu sous le champ — les manifestes le
    // posaient déjà (mixer, pyramide, clés tranche…) sans qu'aucun renderer ne l'affiche.
    const help = f.help ? `<small class="meta">${_cfEsc(f.help)}</small>` : '';
    if (f.type === 'checkbox' || f.type === 'bool') {   // bool = alias (schémas récents)
        const chk = f.default ? ' checked' : '';
        input = `<input type="checkbox" class="ios-toggle" data-cf="${k}" data-cft="checkbox"${chk}>`;
        return `<div class="palette-field-check" data-cf-wrap="${k}"><label><span class="dp-cf-input">${input}</span> ${lbl}</label>${help}</div>`;
    } else if (f.type === 'number') {
        const a = [f.min!=null?`min="${_cfEsc(f.min)}"`:'', f.max!=null?`max="${_cfEsc(f.max)}"`:'', f.step!=null?`step="${_cfEsc(f.step)}"`:''].join(' ');
        input = `<input type="number" data-cf="${k}" data-cft="number" ${a}${ph}${val}>`;
    } else if (f.type === 'multiselect') {
        // ★ UNE LISTE DE CHOIX, PAS UN SEUL. Le cas type est le niveau de tally : il se CUMULE
        // (une source suivie sur plusieurs chaînes de destination), et un `select` scalaire
        // obligeait à décider laquelle compte. Le cas « un seul » n'est que la liste à un
        // élément — pas un type de champ différent.
        //
        // ⚠ PAS UN `<select multiple>` : essayé, retiré. Il tient à quatre entrées ; un site a
        // autant de niveaux qu'il a de chaînes de destination, et à vingt la boîte de quatre
        // lignes devient inutilisable — sans compter le Ctrl+clic pour désélectionner, qui ne
        // s'invente pas. On monte le contrôle de CATALOGUE (`chooseList`) : une liste déroulante
        // pour ajouter, des puces pour dire l'état. Le montage se fait après l'insertion du HTML
        // (cf. `_montePluginFields`), le contrôle fabriquant ses propres éléments.
        input = `<div data-cf="${k}" data-cft="multiselect" data-cf-opts="${
            _cfEsc(JSON.stringify({o: f.options || [], v: f.default || []}))}"></div>`;
    } else if (f.type === 'select') {
        const opts = (f.options||[]).map(o=>{
            const sel = (f.default != null && String(o.value) === String(f.default)) ? ' selected' : '';
            return `<option value="${_cfEsc(o.value)}"${sel}>${_cfEsc(o.label||o.value)}</option>`;
        }).join('');
        input = `<select data-cf="${k}" data-cft="select">${opts}</select>`;
    } else {
        input = `<input type="text" data-cf="${k}" data-cft="text"${ph}${val}>`;
    }
    return `<div class="palette-field" data-cf-wrap="${k}"><label>${lbl}</label>${input}${help}</div>`;
}

// Monte les contrôles de catalogue des champs qui en demandent un. À appeler APRÈS avoir posé
// le HTML : `chooseList` fabrique ses éléments et les accroche à l'hôte.
function _montePluginFields(box){
    if (!box || !window.MXLControls) return;
    box.querySelectorAll('[data-cft="multiselect"]').forEach(el => {
        if (el._ctl) return;
        let d = {o: [], v: []};
        try { d = JSON.parse(el.dataset.cfOpts || '{}'); } catch (e) { /* champ sans options */ }
        el._ctl = window.MXLControls.chooseList(el, {
            options: (d.o || []).map(o => ({value: o.value, label: o.label || o.value})),
            valeurs: Array.isArray(d.v) ? d.v : (d.v ? [d.v] : []),
            vide: '— aucun —', ajouter: '+ Ajouter…', tout: 'tout est choisi',
        });
    });
}

function _readConfigBox(box){
    const out = {};
    if (!box) return out;
    box.querySelectorAll('[data-cf]').forEach(el => {
        const k = el.dataset.cf, t = el.dataset.cft;
        if (t === 'checkbox')      out[k] = el.checked;
        else if (t === 'number')   { const v = parseFloat(el.value); if (!isNaN(v)) out[k] = v; }
        // Toujours un TABLEAU, y compris vide. La valeur vit dans le contrôle de catalogue,
        // qui l'expose par `_ctl` — la lire dans le DOM obligerait à connaître son rendu.
        else if (t === 'multiselect') out[k] = el._ctl ? el._ctl.value() : [];
        else                       out[k] = el.value;
    });
    return out;
}

function _applyCfVisibility(box, schema){
    const vals = _readConfigBox(box);
    (schema||[]).forEach(f => {
        if (!f.visible_if) return;
        const wrap = box.querySelector(`[data-cf-wrap="${CSS.escape(f.key)}"]`);
        if (!wrap) return;
        // Valeur scalaire = égalité ; liste = appartenance (ex. {"mode": ["a","b"]}).
        const show = Object.entries(f.visible_if).every(([k,v]) =>
            Array.isArray(v) ? v.some(x => String(vals[k]) === String(x)) : String(vals[k]) === String(v));
        wrap.hidden = !show;
    });
}

// Champs scope "system" du config_schema d'un type — seuls ceux-là sont rendus/restaurés
// par la palette (les champs scope "user" s'éditent depuis la page du plugin / Réglages).
// `hidden: true` = champ jamais rendu dans l'UI (palette + panneau ⚙ de plugin_section.html)
// mais qui RESTE dans le schema → coerce_config, déploiement, macros et API inchangés
// (ex. clés du mode tranche, pilotées par le réglage global Réglages → Vidéo).
function _paletteSchema(type){
    return ((window.PLUGIN_CONFIG_SCHEMAS || {})[type] || [])
        .filter(f => !f.hidden && (f.scope || 'system') !== 'user');
}

// Clés scope "user" du config_schema d'un type — à ne jamais émettre depuis la palette
// (le merge serveur défauts←persistés←POST préserverait sinon la valeur du preset format
// au lieu de la valeur réglée en live, cf. avsync chroma).
function _userScopeKeys(type){
    return ((window.PLUGIN_CONFIG_SCHEMAS || {})[type] || [])
        .filter(f => (f.scope || 'system') === 'user').map(f => f.key);
}

function renderPluginConfig(host, type){
    const box = host.querySelector('#dp-plugin-config');
    if (!box) return;
    // La palette ne rend que les champs structurels (scope "system", défaut) —
    // les champs scope "user" s'éditent depuis la page du plugin (panneau Réglages).
    const schema = _paletteSchema(type);
    if (!schema.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = '<fieldset class="palette-group"><legend class="palette-group-title">Configuration</legend>'
        + schema.map(_pluginFieldHtml).join('') + '</fieldset>';
    _montePluginFields(box);
    box.querySelectorAll('[data-cf]').forEach(el =>
        el.addEventListener('change', () => _applyCfVisibility(box, schema)));
    _applyCfVisibility(box, schema);
}

function collectPluginConfig(host){
    return _readConfigBox(host.querySelector('#dp-plugin-config'));
}

// Bornes du config_schema (min/max/step sont déjà posés en attributs HTML par _pluginFieldHtml,
// mais rien ne les faisait respecter : la lecture se fait via .value, hors validation de
// formulaire → une valeur hors bornes partait au serveur, qui l'écrêtait EN SILENCE).
// Renvoie un message d'erreur (1er champ fautif) ou null.
function _cfValidate(box){
    if (!box || box.hidden) return null;
    for (const el of box.querySelectorAll('[data-cf]')) {
        if (el.checkValidity()) continue;
        const wrap = el.closest('[data-cf-wrap]');
        const lbl  = wrap ? (wrap.querySelector('label')||{}).textContent : '';
        el.reportValidity();
        return ((lbl || el.dataset.cf).trim()) + ' : ' + el.validationMessage;
    }
    return null;
}

// Sélecteur de version : visible seulement si le plugin a >1 version installée.
// `want` = version à présélectionner (sinon la courante = 1ʳᵉ de la liste).
function renderPluginVersions(host, type, want){
    const field = host.querySelector('.dp-plugin-version-field');
    const sel = host.querySelector('.dp-plugin-version');
    if (!field || !sel) return;
    const vers = (window.PLUGIN_VERSIONS || {})[type] || [];
    if (vers.length <= 1) { field.hidden = true; sel.innerHTML = ''; return; }
    field.hidden = false;
    sel.innerHTML = vers.map((v, i) =>
        `<option value="${v}">${v}${i === 0 ? window.t('js.version_current') : ''}</option>`).join('');
    sel.value = (want && vers.includes(want)) ? want : vers[0];
}

// Version choisie dans la palette (null si pas de sélecteur ou version courante).
function selectedPluginVersion(host){
    const field = host.querySelector('.dp-plugin-version-field');
    const sel = host.querySelector('.dp-plugin-version');
    if (!field || field.hidden || !sel || !sel.value) return null;
    const vers = sel.options;
    // null si c'est la courante (1ʳᵉ option) → laisse le backend choisir la courante
    return (sel.selectedIndex === 0) ? null : sel.value;
}

function restorePluginConfig(host, type, params){
    renderPluginConfig(host, type);
    const box = host.querySelector('#dp-plugin-config');
    if (!box || box.hidden) return;
    box.querySelectorAll('[data-cf]').forEach(el => {
        const k = el.dataset.cf;
        if (!params || !(k in params)) return;
        if (el.dataset.cft === 'checkbox') el.checked = !!params[k];
        else el.value = params[k];
    });
    _applyCfVisibility(box, _paletteSchema(type));
}

// Niveau 1 : construit les onglets de catégorie à partir des .dp-type-cat non vides.
// Chaque .dp-type-cat sans chip est masqué et n'a pas d'onglet.
function buildDpCatNav(host) {
    const nav = host.querySelector('.dp-type-cats-nav');
    if (!nav) return;
    nav.innerHTML = '';
    const cats = Array.from(host.querySelectorAll('.dp-type-cat'));
    cats.forEach((cat, idx) => {
        cat.dataset.catIdx = idx;
        if (!cat.querySelector('.dp-chip')) { cat.hidden = true; return; }
        const label = (cat.querySelector('.dp-type-cat-label')?.textContent || window.t('js.category_n').replace('{n}', idx+1)).trim();
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dp-cat-btn';
        btn.dataset.catIdx = idx;
        btn.setAttribute('role', 'tab');
        btn.textContent = label;
        btn.addEventListener('click', () => selectDpCat(host, idx));
        nav.appendChild(btn);
    });
}

// Active la catégorie d'index `idx` (onglet + groupe de chips), masque les autres.
function selectDpCat(host, idx) {
    host.querySelectorAll('.dp-type-cat').forEach(cat =>
        cat.classList.toggle('active', cat.dataset.catIdx === String(idx)));
    host.querySelectorAll('.dp-cat-btn').forEach(btn => {
        const on = btn.dataset.catIdx === String(idx);
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
}

// Active la catégorie contenant la chip du type donné (sinon la première dispo).
function _activateCatForType(host, type) {
    const chip = host.querySelector(`.dp-chip[data-type="${type}"]`);
    const cat = chip && chip.closest('.dp-type-cat');
    if (cat && cat.dataset.catIdx != null) { selectDpCat(host, cat.dataset.catIdx); return; }
    const first = host.querySelector('.dp-cat-btn');
    if (first) selectDpCat(host, first.dataset.catIdx);
}

function applyDeployType(host, type, opts) {
    opts = opts || {};
    host.querySelector('.dp-receiver').style.display           = _isRx2110(type)                    ? 'block' : 'none';
    const _txField = host.querySelector('.dp-mtl-tx');   // slots TX = moteur MTL uniquement
    if (_txField) _txField.style.display = (type === '2110_io') ? '' : 'none';
    // Format vidéo universel : masqué pour les plugins format-agnostiques (video_format:false).
    const fmtField = host.querySelector('.palette-field-format');
    if (fmtField) fmtField.style.display = _typeNeedsFormat(type) ? '' : 'none';
    renderPluginConfig(host, type);   // champs déclaratifs (Tier 1) si le plugin en a
    renderPluginVersions(host, type); // sélecteur de version si le plugin en a plusieurs
    // Sync visuel des chips + libellé de la ligne récap
    let activeLabel = '';
    host.querySelectorAll('.dp-chip').forEach(c => {
        const on = c.dataset.type === type;
        c.classList.toggle('active', on);
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on) activeLabel = c.textContent.trim();
    });
    // Type sans chip (auto_provision, ex. 2110_io) : pas de chip d'où lire le libellé →
    // repli sur le label du manifeste pour que la ligne récap reste correcte à l'édition.
    if (!activeLabel && type) activeLabel = modeLabel(type);
    _activateCatForType(host, type);   // niveau 1 : montre la catégorie du type actif
    const summaryValue = host.querySelector('.dp-type-summary-value');
    if (summaryValue) {
        summaryValue.textContent = activeLabel;
        const mode = _pluginBadgeClass(type) || (type ? 'mode-plugin' : 'mode-none');
        summaryValue.className = 'dp-type-summary-value mode-badge ' + mode;
    }
    // Repli/déplie le sélecteur. Si non spécifié, on laisse l'état actuel.
    if (opts.collapsed !== undefined) {
        const group = host.querySelector('.palette-group-type');
        if (group) group.dataset.collapsed = opts.collapsed ? 'true' : 'false';
    }
}

// Appelé par le clic sur une chip : met à jour l'input caché, applique le type,
// et replie le sélecteur (l'utilisateur a confirmé son choix).
function setDpType(chip, type) {
    const host = chip.closest('#deploy-palette') || document.getElementById('deploy-palette');
    const input = host.querySelector('.dp-type');
    if (input) input.value = type;
    onDeployTypeChange();
    const group = host.querySelector('.palette-group-type');
    if (group) group.dataset.collapsed = 'true';
}

// Re-déplie le sélecteur depuis la ligne récap.
function expandDpType() {
    const host = document.getElementById('deploy-palette');
    const group = host && host.querySelector('.palette-group-type');
    if (group) group.dataset.collapsed = 'false';
}

function onDeployTypeChange() {
    const host = document.getElementById('deploy-palette');
    const type = host.querySelector('.dp-type').value;
    applyDeployType(host, type);
}

// (Section Simulateur retirée de la palette : la simu — GÉN/IDENT, patterns, audio —
// se pilote en live depuis la page Sources 2110 ; les params sim_* persistés sont
// préservés au redeploy grâce au merge des params existants côté serveur.)

function restaurerReceiver(host, p) {
    // N'écraser les champs que si la clé est présente dans p (cas déployé).
    // Pour un container vierge (p = {}), on conserve les valeurs par défaut du template.
    if ('video_count' in p) host.querySelector('.dp-rx-vcount').value = p.video_count ?? 1;
    const nv = parseInt(host.querySelector('.dp-rx-vcount').value) || 1;
    // audio_per_video : nouveau modèle. Fallback : reconstruire depuis l'ancien
    // audio_count (round à la division entière, clampé 0-2) pour les receivers
    // déployés avant le changement de modèle.
    let aper = p.audio_per_video;
    if (aper == null && ('audio_count' in p || 'audio_per_video' in p)) {
        aper = nv > 0 ? Math.min(2, Math.round((p.audio_count ?? 0) / nv)) : 0;
    }
    if (aper != null) host.querySelector('.dp-rx-aper').value = Math.max(0, Math.min(2, aper));
    const _txEl = host.querySelector('.dp-rx-txcount');   // slots TX (moteur MTL)
    if (_txEl && 'tx_count' in p) _txEl.value = Math.max(0, parseInt(p.tx_count) || 0);
    const _ancEl = host.querySelector('.dp-rx-anccount');  // slots RX ANC (2110-40)
    if (_ancEl && 'anc_count' in p) _ancEl.value = Math.max(0, parseInt(p.anc_count) || 0);
}

async function majRessources() {
    if (selectedDeployVmid === null) return;
    const host = document.getElementById('deploy-palette');
    const status = host.querySelector('.dp-r-status');
    const payload = {
        cores:         parseInt(host.querySelector('.dp-r-cores').value)  || null,
        memory:        parseInt(host.querySelector('.dp-r-memory').value) || null,
        pinned_cores:  host.querySelector('.dp-r-pinned').value.trim(),
    };
    if (!confirm(window.t('js.resources.confirm_prefix') + ' ' + selectedDeployVmid + ' ?\n' + window.t('js.resources.confirm_restart'))) return;
    status.textContent = window.t('js.status.updating');
    status.style.color = 'var(--text-muted)';
    const r = await fetch('/api/containers/' + selectedDeployVmid + '/resources', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });
    if (r.ok) {
        status.textContent = window.t('js.update_in_progress');
        status.style.color = 'var(--status-running-fg)';
    } else {
        const j = await r.json().catch(() => ({}));
        status.textContent = '✕ ' + (j.error || ('HTTP ' + r.status));
        status.style.color = 'var(--status-stopped-fg)';
    }
    setTimeout(() => { status.textContent = ''; }, 6000);
}

async function deployerPalette() {
    if (selectedDeployVmid === null) return;
    const host = document.getElementById('deploy-palette');
    const type = host.querySelector('.dp-type').value;
    const status = host.querySelector('.dp-status-msg');

    // ⚠ La garde ne vaut QUE pour les types qui ont un format. Sans le test, un plugin
    // format-agnostique (`video_format:false` — delay, recorder, color_corrector, multiview dont
    // le format se règle dans le composer) était INDÉPLOYABLE sur une page fraîche : le champ est
    // masqué, mais le déploiement exigeait quand même une sélection — un message pointant un
    // champ invisible.
    const fmtSel = host.querySelector('.dp-format-preset');
    if (_typeNeedsFormat(type) && (!fmtSel || !fmtSel.value)) {
        status.textContent = window.t('js.select_video_format');
        status.style.color = 'var(--status-warning-fg, #f5a623)';
        return;
    }
    const fmt = _getFormatValues(host);
    let params = {};

    if (_isRx2110(type)) {
        const nv  = parseInt(host.querySelector('.dp-rx-vcount').value) || 0;
        const per = Math.max(0, Math.min(2, parseInt(host.querySelector('.dp-rx-aper').value) || 0));
        params = {
            video_count:      nv,
            audio_count:      nv * per,
            audio_per_video:  per,
            width:            fmt.w,
            height:           fmt.h,
            fps:              fmt.fps,
            scan:             fmt.scan,
            chroma:           fmt.chroma,
            bit_depth:        fmt.bit_depth,
            colorimetry:      fmt.colorimetry,
            // sim_* : plus émis par la palette — pilotés en live depuis la page
            // Sources 2110, et préservés au redeploy (merge params existants côté serveur).
        };
        if (type === '2110_io') {          // moteur bi-rôle : slots TX (émetteurs) + ANC RX
            const txEl  = host.querySelector('.dp-rx-txcount');
            const ancEl = host.querySelector('.dp-rx-anccount');
            params.tx_count  = txEl  ? (parseInt(txEl.value)  || 0) : 0;
            params.anc_count = ancEl ? (parseInt(ancEl.value) || 0) : 0;
        }
    } else {
        // Type plugin : champs déclaratifs (Tier 1). Le format vidéo universel n'est injecté
        // que si le plugin n'est pas format-agnostique (video_format:false → ex. delay).
        // Le backend complète avec les deploy_defaults + coerce selon le schéma.
        const base = _typeNeedsFormat(type)
            ? { width: fmt.w, height: fmt.h, fps: fmt.fps, scan: fmt.scan,
                chroma: fmt.chroma, bit_depth: fmt.bit_depth, colorimetry: fmt.colorimetry } : {};
        params = Object.assign(base, collectPluginConfig(host));
        // Ne jamais émettre les clés scope "user" (ex. avsync.chroma) : la palette ne les
        // affiche plus, donc leur valeur ici ne serait qu'un preset format stale qui
        // écraserait à tort la valeur persistée au merge côté serveur.
        _userScopeKeys(type).forEach(k => delete params[k]);
    }

    // Bornes du config_schema : refus AVANT l'envoi (le serveur revalide de toute façon —
    // plugins.validate_config → 400). Aucune valeur hors bornes n'est écrêtée en silence.
    const bad = _cfValidate(host.querySelector('#dp-plugin-config'));
    if (bad) {
        status.textContent = '✕ ' + bad;
        status.style.color = 'var(--status-stopped-fg)';
        setTimeout(() => { status.textContent = ''; }, 6000);
        return;
    }

    status.textContent = window.t('js.deploying');
    status.style.color = 'var(--text-muted)';

    const r = await fetch('/api/containers/' + selectedDeployVmid + '/deploy', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({type, params, version: selectedPluginVersion(host),
                              path: '/opt/script/main.py'})
    });
    if (r.ok) {
        status.textContent = window.t('js.deployed');
        status.style.color = 'var(--status-running-fg)';
    } else {
        // Message du serveur (ex. « Réglages hors bornes : … ») — pas un « ✕ erreur » muet.
        const j = await r.json().catch(() => ({}));
        status.textContent = '✕ ' + (j.error || r.statusText || window.t('js.generic_error'));
        status.style.color = 'var(--status-stopped-fg)';
        setTimeout(() => { status.textContent = ''; }, 8000);
        return;
    }
    setTimeout(() => { status.textContent = ''; }, 3000);
}

// ─── Modifier = sélectionner dans la palette ────────────────

function modifier(vmid) {
    selectDeployContainer(vmid);
    const host = document.getElementById('deploy-palette');
    if (host) host.scrollIntoView({behavior: 'smooth', block: 'start'});
}

// ─── Helper : libellé du statut container ────────────────────

function statusLabel(c, dc) {
    if (c.status === 'script_stopped' && !dc) return window.t('containers.card.ready');
    const k = 'containers.status.' + c.status, v = window.t(k);
    return v === k ? c.status : v;   // statut inconnu du catalogue → brut
}
function statusBadgeClass(c, dc) {
    if (c.status === 'script_stopped' && !dc) return 'badge ready';
    return 'badge ' + c.status;
}

// ─── Helper : badge cadence ───────────────────────────────────
// Le badge répond à UNE question — « la cadence demandée est-elle tenue ? » — et non « quel
// nombre le compteur a-t-il sorti à cet instant ». La nuance n'est pas cosmétique : la mesure
// d'un plugin porte ±1 fps de troncature de fenêtre, si bien qu'un conteneur parfaitement sain
// affichait « 49,8 fps » puis « 50,1 » puis « 49,9 ». L'exploitant lisait un défaut là où il n'y
// en avait pas, et un chiffre qui bouge en permanence finit par n'être plus regardé.
// Quatre situations, quatre affichages :
//   1. cible TENUE          → « 50 fps » (vert), le chiffre DEMANDÉ, stable tant qu'il est tenu ;
//   2. cible NON tenue      → « 43/50 fps » (orange) : le chiffre réel n'apparaît que lorsqu'il
//      porte une information, et il apparaît alors À CÔTÉ de l'intention, seule façon de le lire ;
//   3. cible INCONNUE       → « ~50 fps » (le deploy_config ne déclare pas de cadence) : on
//      arrondit et le tilde dit qu'on ne compare à rien ;
//   4. cadence NON MESURÉE  → « ? fps » (gris) : l'orchestrateur a périmé la valeur (colonne NULL)
//      parce que :8080 ne répond plus. On ne réaffiche JAMAIS la dernière valeur connue ;
//   5. conteneur arrêté / rien de déployé → pas de badge du tout (il n'y a rien à cadencer).
// Le verdict `tenue` vient du SERVEUR (metrics.cadence_etat) : il s'appuie sur le plancher
// glissant 30 s et sur le seuil de l'alarme de sous-cadence. Ne pas le recalculer ici — un badge
// vert sous une alerte « cadence NON TENUE » serait pire que pas de badge du tout.
function fpsBadge(c, dc) {
    if (dc && (dc.type === 'webrtc_gateway' || dc.type === 'ServeurStream')) return '';
    if (c.fps) {
        const cad = c.cadence || {};
        const vue = (cad.mesure != null) ? cad.mesure : c.fps;
        if (cad.cible > 0 && cad.tenue) {
            return `<span class="fps" title="${window.t('containers.card.fps_held_title')
                .replace('{mesure}', vue).replace('{cible}', Math.round(cad.cible))}">${
                Math.round(cad.cible)} fps</span>`;
        }
        if (cad.cible > 0) {
            return `<span class="fps fps-basse" title="${window.t('containers.card.fps_low_title')
                .replace('{mesure}', vue).replace('{cible}', Math.round(cad.cible))}">${
                vue}/${Math.round(cad.cible)} fps</span>`;
        }
        return `<span class="fps fps-approx" title="${
            window.t('containers.card.fps_notarget_title')}">~${Math.round(vue)} fps</span>`;
    }
    if (!dc || c.status === 'stopped' || c.status === 'script_stopped') return '';
    return `<span class="fps fps-unknown" title="${window.t('containers.card.fps_unknown_title')}">${
        window.t('containers.card.fps_unknown')}</span>`;
}

// ─── Helper : badge "mode" (type de script déployé) ─────────

function cpuBar(c) {
    if (c.cpu_percent == null) return '';
    const v = Math.max(0, Math.min(100, c.cpu_percent));
    const color = v < 50  ? 'var(--status-running-fg)'
                : v < 80  ? 'var(--status-warning-fg)'
                :           'var(--status-stopped-fg)';
    // Hint cores : affiché quand > 1 core alloué pour rappeler qu'un process
    // mono-thread plafonne à 100/N % (ex: "25% / 4c" = 1 thread saturé sur 4 cœurs)
    const n = c.cpu_count || c.cores || 0;
    const coresHint = n > 1
        ? ` <span class="cpu-cores-hint" title="${window.t('containers.card.cpu_cores_title').replace('{n}', n).replace('{pct}', Math.round(100/n))}">/${n}c</span>`
        : '';
    return `
        <div class="cpu-bar">
            <div class="cpu-bar-label">CPU <span class="cpu-bar-value">${v.toFixed(1)}%${coresHint}</span></div>
            <div class="cpu-bar-track">
                <div class="cpu-bar-fill" style="background:${color}; transform: scaleX(${(v/100).toFixed(4)})"></div>
            </div>
        </div>`;
}

function _modeBadgeBase(dc) {
    const m = window.MXL_PLUGINS?.[dc.type];
    if (m?.badge_class) return `<span class="mode-badge ${m.badge_class}">${m.badge_label || dc.type}</span>`;
    return `<span class="mode-badge mode-none">${dc.type}</span>`;
}
function modeBadge(dc) {
    if (!dc || !dc.type) return `<span class="mode-badge mode-none">${window.t('containers.card.none')}</span>`;
    let html = _modeBadgeBase(dc);
    // Suffixe version seulement pour les types qui ont plusieurs versions installées.
    const vers = (window.PLUGIN_VERSIONS || {})[dc.type] || [];
    const v = (dc.params || {}).plugin_version;
    if (vers.length > 1 && v) html += ` <span class="badge" title="${window.t('containers.card.plugin_version_title')}">v${v}</span>`;
    return html;
}

function modeLabel(t) {
    return (window.MXL_PLUGINS?.[t]?.label) || t;
}

// ─── Rafraîchissement cards (diff par VMID) ──────────────────
// Signature compacte des champs qui affectent le rendu d'une card.
// Si elle est identique au tick précédent, on saute la card.
function _cardSig(c) {
    return [c.hostname, c.fps || 0, c.status, c.cores, c.memory, c.ip || '',
            c.source || '', c.shm_out || '', c.restarts || 0, c.script || '',
            c.pinned_cores || '',
            c.cpu_percent != null ? Math.round(c.cpu_percent * 10) : 'x',
            c.deploy_config || '',
            c.fabric_role || '', (_fabricChildCount[c.vmid] || 0),
            (Array.isArray(c.projects) ? c.projects.map(p => p.id).join(',') : '')].join('|');
}
const _cardSigs = new Map();   // vmid → signature

function _renderCardInner(c, canDeploy, canDestroy, canMv) {
    let dc = null;
    try { dc = c.deploy_config ? JSON.parse(c.deploy_config) : null; } catch(e) {}
    // ─── Menu ACTIONS ────────────────────────────────────────────────────────
    // Une carte porte jusqu'à sept actions. Alignées en rangée, elles saturaient la carte, et le
    // bouton destructif finissait à part — visuellement en marge alors qu'il est le plus lourd de
    // conséquences. Un menu unique rend la carte lisible ET met Détruire à sa place : dans la même
    // liste que les autres, mais séparé et teinté, donc trouvable sans être à portée de clic
    // distrait.
    const item = (attrs, libelle, classe) =>
        `<button role="menuitem" class="card-menu-item${classe ? ' ' + classe : ''}" ${attrs}>${libelle}</button>`;
    const items = [];
    if (canDeploy) items.push(item(
        `onclick="_cardMenuFermer(); modifier(${c.vmid})" data-action="configure" aria-controls="deploy-palette"`,
        window.t('containers.card.configure')));
    if (canDeploy) items.push(item(`onclick="_cardMenuFermer(); restart(${c.vmid})"`, 'Restart'));
    if (canDeploy) items.push(c.status === 'running'
        ? item(`onclick="_cardMenuFermer(); stopScript(${c.vmid})"`, 'Stop')
        : item(`onclick="_cardMenuFermer(); startScript(${c.vmid})"`, 'Start'));
    if (canMv && dc && dc.type === 'multiview') items.push(item(
        `onclick="_cardMenuFermer(); ouvrirTally(${c.vmid}, '${c.hostname}')"`, 'Tally'));
    if (canDeploy) items.push(item(
        `onclick="_cardMenuFermer(); ouvrirMigration(${c.vmid}, '${c.hostname}')" title="${window.t('migration.button_title')}"`,
        window.t('migration.button')));
    if (canDeploy) items.push(item(
        `onclick="_cardMenuFermer(); BobiLogs.open(${c.vmid}, {nom: '${escapeHtml(c.hostname)}'})" title="${escapeHtml(window.t('js.logs.card_title') || '')}"`,
        escapeHtml(window.t('js.logs.card_btn') || 'Journal')));
    if (canDestroy) items.push('<div class="card-menu-sep"></div>' + item(
        `onclick="_cardMenuFermer(); detruire(${c.vmid})" aria-label="${window.t('containers.card.destroy')} ${escapeHtml(c.hostname)}"`,
        window.t('containers.card.destroy'), 'card-menu-danger'));
    // L'état ouvert survit au ré-affichage : la liste se rafraîchit toutes les 5 s et
    // `innerHTML` est reconstruit — sans ça, le menu se refermerait sous le curseur.
    const ouvert = (_cardMenuOuvert === c.vmid);
    const menu = items.length
        ? `<div class="card-menu">
               <button class="btn card-menu-btn" aria-haspopup="menu" aria-expanded="${ouvert}"
                       onclick="_cardMenuBascule(event, ${c.vmid})">${window.t('containers.card.actions')} ▾</button>
               <div class="card-menu-list" role="menu"${ouvert ? '' : ' hidden'}>${items.join('')}</div>
           </div>`
        : '';
    const _ver = dc && dc.params && dc.params.plugin_version;
    const metaRows = [
        `<dt>${window.t('containers.card.node')}</dt><dd class="${c.node_id ? '' : 'dimmed'}">${c.node_id ? escapeHtml(c.node_name || ('Nœud ' + c.node_id)) : window.t('containers.card.none')}</dd>`,
        _ver ? `<dt>${window.t('containers.card.version')}</dt><dd title="${escapeHtml(window.t('containers.card.plugin_version_title'))}">v${escapeHtml(String(_ver))}</dd>` : '',
        `<dt>${window.t('containers.card.cores')}</dt><dd>${c.cores}</dd>`,
        `<dt>RAM</dt><dd>${c.mem_used != null ? Math.round(c.mem_used / 1048576) + ' / ' + c.memory + ' MB' : c.memory + ' MB'}</dd>`,
        `<dt>IP</dt><dd class="${c.ip ? '' : 'dimmed'}">${c.ip || window.t('containers.card.ip_none')}</dd>`,
        c.source  ? `<dt>${window.t('containers.card.source')}</dt><dd>${escapeHtml(c.source)}</dd>`  : '',
        c.shm_out ? `<dt>SHM</dt><dd>${escapeHtml(c.shm_out)}</dd>`    : '',
        `<dt>${window.t('containers.card.restarts')}</dt><dd>${c.restarts}</dd>`,
        `<dt>${window.t('containers.card.script')}</dt><dd class="${c.script ? '' : 'dimmed'}">${c.script || window.t('containers.card.none')}</dd>`,
        (() => {
            const projs = Array.isArray(c.projects) ? c.projects : [];
            const label = projs.length > 1 ? window.t('containers.card.projects') : window.t('containers.card.project');
            const val   = projs.length
                ? escapeHtml(projs.map(p => p.name).join(', '))
                : window.t('containers.card.none');
            return `<dt>${label}</dt><dd class="${projs.length ? '' : 'dimmed'}">${val}</dd>`;
        })(),
        c.pinned_cores
            ? `<span class="meta-pin">⚲ CPU pinning : ${escapeHtml(c.pinned_cores)}</span>`
            : '',
    ].filter(Boolean).join('');
    return `
        <header class="card-head">
            <div class="card-title">
                <h2 class="card-host">${escapeHtml(c.hostname)}</h2>
                <span class="card-vmid">#${c.vmid}</span>
            </div>
            ${fpsBadge(c, dc)}
        </header>
        <div class="card-tags">
            <span class="${statusBadgeClass(c, dc)}" aria-live="polite">${statusLabel(c, dc)}</span>
            ${modeBadge(dc)}
            ${_fabricChildCount[c.vmid] ? `<span class="badge" title="${window.t('js.fabric_internals_title')}" style="cursor:pointer" onclick="event.stopPropagation(); if(!filterState.showFabricInternals) toggleFabricInternals()">⚙ ${window.t('js.fabric_internals_count').replace('{n}', _fabricChildCount[c.vmid])}</span>` : ''}
            ${c.fabric_role === 'shard' ? `<span class="badge" title="Shard interne du tissu (parent #${c.fabric_parent})">⊂ shard</span>` : ''}
            ${c.fabric_role === 'proxy' ? `<span class="badge" title="${window.t('js.proxy_pyramide_title')}">⊂ proxy</span>` : ''}
            ${c.gpu && c.gpu.gpu ? `<span class="badge ready" title="${window.t('containers.card.gpu_title')}${c.gpu.name ? ' ('+escapeHtml(c.gpu.name)+')' : ''}">⚡ ${window.t('containers.card.gpu')}</span>` : ''}
        </div>
        <dl class="card-meta">${metaRows}</dl>
        ${cpuBar(c)}
        <div class="card-actions">${menu}</div>`;
}

let _cardMenuOuvert = null;

function _cardMenuBascule(ev, vmid) {
    ev.stopPropagation();
    _cardMenuOuvert = (_cardMenuOuvert === vmid) ? null : vmid;
    document.querySelectorAll('.card-menu').forEach(m => {
        const btn = m.querySelector('.card-menu-btn');
        const liste = m.querySelector('.card-menu-list');
        const carte = m.closest('.card');
        const actif = carte && carte.id === ('c-' + _cardMenuOuvert);
        if (liste) liste.hidden = !actif;
        if (btn) btn.setAttribute('aria-expanded', String(!!actif));
    });
}

function _cardMenuFermer() {
    _cardMenuOuvert = null;
    document.querySelectorAll('.card-menu-list').forEach(l => { l.hidden = true; });
    document.querySelectorAll('.card-menu-btn').forEach(b => b.setAttribute('aria-expanded', 'false'));
}

// Un menu ouvert se ferme au clic AILLEURS et à Échap : sans ça il resterait ouvert pendant qu'on
// travaille sur une autre carte, et deux menus ouverts se disputeraient l'attention.
document.addEventListener('click', () => { if (_cardMenuOuvert !== null) _cardMenuFermer(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') _cardMenuFermer(); });

function toggleFabricInternals() {
    filterState.showFabricInternals = !filterState.showFabricInternals;
    const chip = document.getElementById('flt-fabric-internals');
    if (chip) chip.classList.toggle('active', filterState.showFabricInternals);
    if (window.lastContainers) updateContainers(window.lastContainers);
}

// Nb d'internes du tissu repliés sous chaque multiview logique (shards dont fabric_parent==vmid).
let _fabricChildCount = {};
function _computeFabricChildren(containers) {
    _fabricChildCount = {};
    containers.forEach(c => {
        if (c.fabric_role === 'shard' && c.fabric_parent != null)
            _fabricChildCount[c.fabric_parent] = (_fabricChildCount[c.fabric_parent] || 0) + 1;
    });
}

function updateContainers(containers) {
    lastContainers = containers;
    window.lastContainers = containers;
    _computeFabricChildren(containers);
    const grid = document.getElementById('containers-grid');
    if (!grid) return;
    const visible = containers.filter(matchesFilter);
    const countEl = document.getElementById('containers-count');
    if (countEl) {
        countEl.textContent = visible.length === containers.length
            ? containers.length
            : `${visible.length} / ${containers.length}`;
    }
    const canDeploy  = window.hasPerm ? hasPerm('containers.deploy') : true;
    const canDestroy = window.hasPerm ? hasPerm('containers.delete') : true;
    const canMv      = window.hasPerm ? hasPerm('multiview.edit')    : true;

    // Empty states : on remplace tout le grid et on vide les sigs.
    if (!visible.length) {
        _cardSigs.clear();
        grid.innerHTML = containers.length === 0
            ? `<div class="empty-state">
                   <div class="empty-state-title">${window.t('js.empty_no_container')}</div>
                   <div class="empty-state-msg">${window.t('js.empty_no_container_msg')}</div>
                   <button class="btn btn-blue" onclick="switchMainTab('creation')">${window.t('js.empty_open_creation')}</button>
               </div>`
            : `<div class="empty-state">
                   <div class="empty-state-title">${window.t('js.empty_no_match')}</div>
                   <div class="empty-state-msg">${window.t('js.empty_no_match_msg')}</div>
               </div>`;
        return;
    }
    // Si le grid contient un empty-state ou un layout obsolète, on repart à neuf
    if (grid.querySelector('.empty-state')) {
        grid.innerHTML = '';
        _cardSigs.clear();
    }

    // Diff par VMID : on patch seulement les cards dont la signature a bougé.
    const visibleIds = new Set();
    visible.forEach(c => {
        const id = 'c-' + c.vmid;
        visibleIds.add(id);
        const sig = _cardSig(c);
        if (_cardSigs.get(c.vmid) === sig) return;       // rien à faire
        _cardSigs.set(c.vmid, sig);
        let card = document.getElementById(id);
        if (!card) {
            card = document.createElement('article');
            card.className = 'card';
            card.id = id;
            grid.appendChild(card);
        }
        card.innerHTML = _renderCardInner(c, canDeploy, canDestroy, canMv);
    });
    // Suppression des cards qui ne sont plus visibles
    Array.from(grid.children).forEach(el => {
        if (el.id && !visibleIds.has(el.id)) {
            _cardSigs.delete(parseInt(el.id.slice(2), 10));
            el.remove();
        }
    });
    // Resync aria-expanded sur les Configurer après tout patch DOM
    _syncConfigureExpanded(selectedDeployVmid);
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
}

// ─── Rendu GÉNÉRIQUE d'un réglage depuis le schéma (refonte Réglages — consommé par les 6 groupes).
// spec = {type:bool|int|number|select|str|text, label, help, min, max, step, options:[{value,label}]}.
function buildSettingField(key, spec, value) {
    spec = spec || {};
    const id = 's2_' + key, label = escapeHtml(spec.label || key);
    const help = spec.help ? `<div class="meta" style="margin-top:2px">${escapeHtml(spec.help)}</div>` : '';
    const t = spec.type || 'str';
    if (t === 'bool') {
        return `<div class="field-row"><label class="field field-inline"><input type="checkbox" id="${id}" data-skey="${escapeHtml(key)}" data-stype="bool"${value ? ' checked':''}> ${label}</label>${help}</div>`;
    }
    let input;
    if (t === 'select') {
        const opts = (spec.options || []).map(o =>
            `<option value="${escapeHtml(String(o.value))}"${String(o.value)===String(value)?' selected':''}>${escapeHtml(o.label||o.value)}</option>`).join('');
        input = `<select id="${id}" data-skey="${escapeHtml(key)}" data-stype="select">${opts}</select>`;
    } else if (t === 'int' || t === 'number') {
        const a = ['min','max','step'].filter(k => spec[k] != null).map(k => `${k}="${spec[k]}"`).join(' ');
        input = `<input type="number" id="${id}" data-skey="${escapeHtml(key)}" data-stype="int" ${a} value="${value ?? ''}">`;
    } else {
        input = `<input type="text" id="${id}" data-skey="${escapeHtml(key)}" data-stype="str" value="${escapeHtml(value ?? '')}">`;
    }
    return `<div class="field"><label for="${id}">${label}</label>${input}${help}</div>`;
}
function collectSettingFields(root) {
    const out = {};
    (root || document).querySelectorAll('[data-skey]').forEach(el => {
        const k = el.dataset.skey, t = el.dataset.stype;
        out[k] = (t === 'bool') ? el.checked : (t === 'int') ? (parseInt(el.value) || 0) : el.value;
    });
    return out;
}

let _alertsSig = null;
// ─── Alerte → journal du conteneur (point d'entrée « voir ce qu'il disait à cet instant ») ───
// ★ LA COLONNE PRIME, LE TEXTE EST UN REPLI. `alerts` porte désormais un `vmid` (+ `node_id`,
// `kind`) : quand il est là, on l'utilise — c'est exact, et ça marche même pour un conteneur
// DÉTRUIT (dont le hostname n'est plus en base), c'est-à-dire le cas où le journal durable sert
// justement. Sinon on RETOMBE sur la déduction textuelle, seule option pour les alertes écrites
// avant la migration (la rétention à 1000 lignes les fait disparaître en ~2 jours) : « vmid N »,
// le nom de conteneur Docker, puis le hostname connu de la page — par fiabilité décroissante.
// Aucune correspondance → pas de lien, plutôt qu'un lien vers le journal d'un AUTRE conteneur.
// Même arbitrage côté serveur : `services/alerting/context.py`. Modifier les DEUX ensemble.
function _alerteVmid(a) {
    // Accepte l'ALERTE (objet) ou, par compatibilité, son seul message.
    if (a && typeof a === 'object') {
        if (a.vmid !== null && a.vmid !== undefined && a.vmid !== '') {
            const v = parseInt(a.vmid, 10);
            if (!isNaN(v)) return v;
        }
        // Puis les PARAMÈTRES : une ligne keyée dit son vmid sans qu'on ait à le lire dans une
        // phrase — d'autant que cette phrase est désormais rendue dans la langue du lecteur, et
        // qu'un motif français n'y trouverait rien.
        const p = _alerteParams(a);
        if (p && p.vmid !== undefined && p.vmid !== null) {
            const v = parseInt(p.vmid, 10);
            if (!isNaN(v)) return v;
        }
        return _alerteVmid(a.message);
    }
    const msg = a;
    if (!msg) return null;
    let m = /\bvmid[\s:]+(\d+)/i.exec(msg) || /\bbobi-(?:mtl|cmp)-(\d+)\b/.exec(msg);
    if (m) return parseInt(m[1], 10);
    const cs = window.lastContainers || [];
    // Plus long hostname d'abord : « multiview » ne doit pas capter une alerte de « multiview-2 ».
    const tri = cs.slice().sort((a, b) => (b.hostname || '').length - (a.hostname || '').length);
    for (const c of tri) {
        if (c.hostname && msg.indexOf(c.hostname) !== -1) return c.vmid;
    }
    return null;
}

// Fenêtre temporelle autour de l'incident : on ouvre le journal CADRÉ sur l'alerte, pas à la fin du
// fichier — sinon on lit l'état actuel au lieu de ce qui s'est passé.
function _alerteFenetre(ts) {
    const d = new Date(ts);
    if (isNaN(d)) return {};
    const fmt = x => new Date(x).toISOString().slice(0, 19).replace('T', ' ');
    return { since: fmt(d.getTime() - 120000), until: fmt(d.getTime() + 60000) };
}

function ouvrirJournalAlerte(vmid, ts) {
    const f = _alerteFenetre(ts);
    const c = (window.lastContainers || []).find(x => x.vmid === vmid);
    BobiLogs.open(vmid, { nom: (c && c.hostname) || ('#' + vmid), since: f.since || '', until: f.until || '' });
}

function updateAlerts(alerts) {
    const el = document.getElementById('alerts-list');
    if (!el) return;
    const countEl = document.getElementById('alerts-count');
    if (countEl) countEl.textContent = alerts.length
        ? `${alerts.length} ligne${alerts.length > 1 ? 's' : ''}`
        : '';
    // Diff léger : ne reconstruit le DOM que si le jeu d'alertes a changé. Évite
    // de réécrire jusqu'à 1000 lignes toutes les 5 s et préserve scroll + sélection.
    const first = alerts[0], last = alerts[alerts.length - 1];
    const sig = `${alerts.length}|${first ? first.timestamp + first.message : ''}|${last ? last.timestamp : ''}`;
    if (sig === _alertsSig) return;
    _alertsSig = sig;
    el.innerHTML = alerts.map(a => `
        <div class="log-line">
            <span class="log-ts">${escapeHtml(a.timestamp)}</span>
            <span class="log-niveau log-niveau-${escapeHtml(a.niveau)}">[${escapeHtml(a.niveau)}]</span>
            <span class="log-msg">${escapeHtml(a.message)}</span>${(() => {
                const v = _alerteVmid(a);
                return v == null ? '' : `<a class="log-jump" title="${escapeHtml(window.t('js.logs.from_alert') || 'Voir le journal du conteneur au moment de cette alerte')}" onclick="ouvrirJournalAlerte(${v}, '${escapeHtml(a.timestamp)}')">${escapeHtml(window.t('js.logs.from_alert_link') || '⤷ journal')}</a>`;
            })()}
        </div>
    `).join('') || '<div class="meta">' + window.t('js.alerts_none') + '</div>';
}

function alertsQueryString() {
    const q = (document.getElementById('alerts-q')?.value || '').trim();
    const n = (document.getElementById('alerts-niveau')?.value || '').trim();
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (n) p.set('niveau', n);
    return p.toString();
}

async function rafraichirAlertes() {
    const qs = alertsQueryString();
    const r = await fetch('/api/alerts' + (qs ? '?' + qs : ''));
    if (r.ok) updateAlerts(await r.json());
}

let _alertsFilterTimer = null;
function onAlertsFilterChange() {
    clearTimeout(_alertsFilterTimer);
    _alertsFilterTimer = setTimeout(rafraichirAlertes, 200);
}

function exporterAlertes(fmt) {
    const qs = alertsQueryString();
    const sep = qs ? '&' : '';
    window.location.href = `/api/alerts/export?format=${fmt}${sep}${qs}`;
}

// ─── Tally live ──────────────────────────────────────────────

let tallyVmid = null;
let tallyFlux = [];
let tallyState = {};

async function ouvrirTally(vmid, hostname) {
    tallyVmid = vmid;
    document.getElementById('tally-title').textContent = `${hostname} #${vmid}`;
    const cfg = await fetch('/api/containers/' + vmid + '/config').then(r => r.json());
    let dc = null;
    try { dc = cfg.deploy_config ? JSON.parse(cfg.deploy_config) : null; } catch(e) {}
    tallyFlux = (dc && dc.params && dc.params.flux_config) || [];
    try {
        const r = await fetch('/api/containers/' + vmid + '/tally');
        tallyState = r.ok ? await r.json() : {};
    } catch(e) { tallyState = {}; }
    renderTallyList();
    document.getElementById('tally-modal').style.display = 'flex';
}

function fermerTally() {
    document.getElementById('tally-modal').style.display = 'none';
    tallyVmid = null;
}

function renderTallyList() {
    const el = document.getElementById('tally-list');
    if (tallyFlux.length === 0) {
        el.innerHTML = '<div style="color:var(--text-muted)">' + window.t('js.no_flow_in_multiview') + '</div>';
        return;
    }
    el.innerHTML = tallyFlux.map((f, i) => {
        const enabled = !!f.show_tally;
        const cL = tallyState[`${i}_L`] || 'off';
        const cR = tallyState[`${i}_R`] || 'off';
        return `
        <div style="display:flex; align-items:center; gap:8px; padding:8px;
                    background:var(--bg-input); border:1px solid var(--border); border-radius:4px;
                    ${enabled ? '' : 'opacity:0.4'}">
            <div style="flex:0 0 30px; color:var(--text-muted)">#${i + 1}</div>
            <div style="flex:1">${f.name || f.path.split('/').pop()}</div>
            ${enabled ? `
              ${tallyBtnGroup(i, 'L', cL)}
              <span style="color:var(--text-muted)">|</span>
              ${tallyBtnGroup(i, 'R', cR)}
            ` : '<span style="color:var(--text-muted); font-size:0.85em">' + window.t('js.tally_disabled_composer') + '</span>'}
        </div>`;
    }).join('');
}

function tallyBtnGroup(idx, slot, current) {
    const colors = [
        { id: 'red',   bg: '#b91c1c', label: 'R' },
        { id: 'green', bg: '#166534', label: 'V' },
        { id: 'off',   bg: '#3a3a3a', label: '·' }
    ];
    return `<span style="font-size:0.8em; color:var(--text-muted)">${slot}:</span>` +
        colors.map(c => `
            <button onclick="setTally(${idx}, '${slot}', '${c.id}')"
                style="padding:4px 10px; border:none; border-radius:4px; cursor:pointer;
                       background:${c.bg}; color:white;
                       outline:${current === c.id ? '2px solid #ffffff' : 'none'};
                       outline-offset:1px;">${c.label}</button>
        `).join('');
}

async function setTally(idx, slot, color) {
    if (tallyVmid === null) return;
    tallyState[`${idx}_${slot}`] = color;
    renderTallyList();
    try {
        await fetch('/api/containers/' + tallyVmid + '/tally', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ flux_idx: idx, slot, color })
        });
    } catch(e) { console.error('Tally error:', e); }
}

// ─── Projets ─────────────────────────────────────────────────

async function sauvegarderProjet() {
    const name = document.getElementById('proj_name').value.trim();
    if (!name) { alert(window.t('js.project.need_name')); return; }
    const vmids = Array.from(document.querySelectorAll('.proj-vmid-cb:checked'))
        .map(cb => parseInt(cb.value));
    if (vmids.length === 0) { alert(window.t('js.project.need_container')); return; }
    const r = await fetch('/api/projects', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name, vmids })
    });
    if (r.ok) location.reload();
    else alert(window.t('js.project.save_error'));
}

async function restaurerProjet(pid, name) {
    // Pré-vol : détecte VMID à remapper et conflits hostname/SHM bloquants
    let plan = null;
    try {
        const r = await fetch('/api/projects/' + pid + '/restore_preview');
        if (r.ok) plan = await r.json();
    } catch (e) { /* fallback : confirmation simple */ }

    if (plan) {
        if (!plan.can_restore) {
            const parts = [];
            if (plan.hostname_conflicts?.length)
                parts.push(window.t('js.restore.hostname_used') + ' : ' + plan.hostname_conflicts.join(', '));
            if (plan.shm_conflicts?.length)
                parts.push(window.t('js.restore.shm_used') + ' : ' + plan.shm_conflicts.join(', '));
            (plan.capacity_issues || []).forEach(i => parts.push(i.detail || i.kind));
            if (plan.error) parts.push(plan.error);
            alert(window.t('js.restore.impossible_prefix') + ' "' + name + '" — ' + window.t('js.restore.impossible_conflict') + ' :\n\n'
                + parts.join('\n')
                + '\n\n' + window.t('js.restore.impossible_hint'));
            return;
        }
        if (plan.remaps?.length) {
            const lignes = plan.remaps
                .map(r => `  • ${r.hostname} : VMID ${r.from} → ${r.to}`)
                .join('\n');
            if (!confirm(
                window.t('js.restore.confirm_prefix') + ' "' + name + '" ?\n\n'
                + window.t('js.restore.remap_intro') + ' :\n' + lignes + '\n\n'
                + window.t('js.restore.remap_note')
            )) return;
        } else {
            if (!confirm(window.t('js.restore.confirm_prefix') + ' "' + name + '" ? ' + window.t('js.restore.confirm_simple'))) return;
        }
    } else {
        if (!confirm(window.t('js.restore.confirm_prefix') + ' "' + name + '" ? ' + window.t('js.restore.confirm_simple'))) return;
    }
    // Copie vs déplacement : OK = déplacement (conserve l'identité/UUID NMOS — la source ne doit
    // plus tourner) ; Annuler = copie (nouveaux UUID, indépendant). Les deux poursuivent le rappel.
    const preserve = confirm(window.t('js.restore.identity_confirm'));
    _restoreRun(pid, name, null, preserve);
}

// État courant du rappel (pour le bouton « réessayer »).
let _restoreCtx = { pid: null, name: '', preserve: false };

// Lance le rappel (ou la reprise des échecs si onlyVmids est fourni) en streaming.
async function _restoreRun(pid, name, onlyVmids, preserveUuid) {
    _restoreCtx = { pid, name, preserve: !!preserveUuid };
    const overlay  = document.getElementById('restore-overlay');
    const logEl    = document.getElementById('restore-log');
    const titleEl  = document.getElementById('restore-title');
    const abortBtn = document.getElementById('restore-abort-btn');
    const closeBtn = document.getElementById('restore-close-btn');
    const retryBar = document.getElementById('restore-retry-bar');

    titleEl.textContent   = (onlyVmids ? window.t('projects.retry_title') : window.t('projects.run_title')) + ' ' + name;
    logEl.textContent     = '';
    overlay.style.display = 'flex';
    abortBtn.style.display = '';  abortBtn.disabled = false; abortBtn.textContent = window.t('projects.abort');
    closeBtn.style.display = 'none';
    retryBar.style.display = 'none';

    let summary = null;
    try {
        const r = await fetch('/api/projects/' + pid + '/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(Object.assign(onlyVmids ? { only_vmids: onlyVmids } : {},
                                               { preserve_uuid: !!preserveUuid })),
        });
        if (!r.ok || !r.body) { logEl.textContent = '✕ ' + window.t('projects.http_error') + ' ' + r.status; return; }
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            // Extrait le marqueur de bilan machine s'il est arrivé.
            const idx = buf.indexOf('__SUMMARY__');
            let visible = buf;
            if (idx !== -1) {
                visible = buf.slice(0, idx);
                try { summary = JSON.parse(buf.slice(idx + '__SUMMARY__'.length).trim()); } catch (e) {}
            }
            logEl.textContent = visible;
            logEl.scrollTop = logEl.scrollHeight;
        }
    } catch (e) {
        logEl.textContent += '\n✕ ' + window.t('projects.error') + ' ' + e.message;
    } finally {
        abortBtn.style.display = 'none';
        closeBtn.style.display = '';
        // Propose de réessayer si des containers ont échoué.
        const failed = (summary && summary.failed) || [];
        if (failed.length) {
            const retryMsg = document.getElementById('restore-retry-msg');
            retryMsg.textContent = window.t('projects.failed_count').replace('{n}', failed.length) + ' '
                + failed.map(f => `${f.hostname || ('#' + f.vmid)} (${f.reason})`).join(', ');
            document.getElementById('restore-retry-btn').dataset.vmids =
                JSON.stringify(failed.map(f => f.vmid));
            retryBar.style.display = 'flex';
        }
    }
}

async function restoreInterrompre() {
    const btn = document.getElementById('restore-abort-btn');
    btn.disabled = true; btn.textContent = window.t('projects.aborting');
    try { await fetch('/api/projects/' + _restoreCtx.pid + '/restore/abort', { method: 'POST' }); }
    catch (e) { /* le bilan final indiquera l'état */ }
}

function restoreFermer() {
    document.getElementById('restore-overlay').style.display = 'none';
}

function restoreReessayer() {
    const vmids = JSON.parse(document.getElementById('restore-retry-btn').dataset.vmids || '[]');
    if (!vmids.length) return;
    _restoreRun(_restoreCtx.pid, _restoreCtx.name, vmids, _restoreCtx.preserve);
}

async function detruireContainersProjet(pid, name) {
    if (!confirm(window.t('js.project.destroy_all') + ' "' + name + '" ?')) return;
    await fetch('/api/projects/' + pid + '/destroy_containers', { method: 'POST' });
}

async function supprimerProjet(pid, name) {
    if (!confirm(window.t('js.project.delete') + ' "' + name + '" ? ' + window.t('js.project.delete_note'))) return;
    const r = await fetch('/api/projects/' + pid, { method: 'DELETE' });
    if (r.ok) location.reload();
}

async function rafraichirDonnees() {
    try {
        const qs = alertsQueryString();
        const [r1, r2] = await Promise.all([
            fetch('/api/containers'),
            fetch('/api/alerts' + (qs ? '?' + qs : ''))
        ]);
        const containers = await r1.json();
        updateContainers(containers);
        updateAlerts(await r2.json());
    } catch(e) {
        console.error('Erreur rafraîchissement:', e);
    }
}

window.MXLPoll(rafraichirDonnees, 5000);   // poll sans recouvrement (cf. layout.html)

// ─── Steppers numériques : EXTRAITS dans static/num_stepper.js ───────────────────────────────
// Ce code vivait ici, mais scripts.js n'est chargé QUE par containers.html et projects.html :
// les pages de contrôle de plugin (plugin_section.html) n'avaient donc AUCUN stepper, en silence.
// Déplacé dans num_stepper.js, chargé par layout.html → disponible sur TOUTES les pages.

// Sync UI au chargement : restaure l'état des chips et applique le filtre sur le rendu serveur
document.addEventListener('DOMContentLoaded', () => {
    applyFilterChipsActive();
    rafraichirDonnees();
    handleWireIntent();
    _batchRestore();   // ré-affiche le suivi des dernières créations (persisté)
    enhanceSteppers(document);   // balayage initial des champs numériques présents au rendu
});

// ─── Wire/unwire intent venant de la home ────────────────────────
// URL : /containers?wire_shm=cam1_0&wire_port=video#c-225
//       /containers?unwire=1#c-225  (streamer)
//       /containers?unwire=1&unwire_port=audio&unwire_shm=cam1_audio_0#c-228
function handleWireIntent() {
    console.log('[wire-intent] start, URL=', window.location.href);
    const params = new URLSearchParams(window.location.search);
    const wire   = params.get('wire_shm');
    const unwire = params.get('unwire') === '1';
    const hash   = window.location.hash || '';
    const m = hash.match(/^#c-(\d+)/);
    console.log('[wire-intent] parsed', { wire, unwire, hash, matched: !!m });
    if (!m || (!wire && !unwire)) { console.log('[wire-intent] no intent, skip'); return; }
    const vmid = parseInt(m[1]);
    showWireToast(wire ? window.t('js.wire_requested').replace('{wire}', wire).replace('{vmid}', vmid)
                        : window.t('js.unwire_requested').replace('{vmid}', vmid));
    // Attendre que les cards soient rendues par rafraichirDonnees, puis ouvrir la palette
    const waitAndApply = (tries = 0) => {
        const card = document.getElementById('c-' + vmid);
        if (!card) {
            if (tries < 40) { setTimeout(() => waitAndApply(tries + 1), 100); return; }
            console.error('[wire-intent] card #c-' + vmid + ' not found after 4s');
            showWireToast(window.t('js.container_not_found').replace('{vmid}', vmid), 'error');
            return;
        }
        console.log('[wire-intent] card found, opening palette via modifier(' + vmid + ')');
        modifier(vmid);
        setTimeout(() => {
            const host = document.getElementById('deploy-palette');
            if (!host) { console.error('[wire-intent] palette not in DOM'); return; }
            const type = host.querySelector('.dp-type')?.value;
            console.log('[wire-intent] palette open, type=', type);
            if (wire) {
                let applied = false;
                console.log('[wire-intent] wire applied=', applied);
                if (applied) {
                    showWireToast(window.t('js.field_prefilled').replace('{wire}', wire), 'ok');
                    flashDeployButton();
                } else {
                    showWireToast(window.t('js.type_not_wirable').replace('{type}', type || window.t('js.type_unknown')), 'warn');
                }
            } else if (unwire) {
                showWireToast(window.t('js.unwire_applied'), 'ok');
                flashDeployButton();
            }
            // Nettoyage URL différé de 3s pour qu'on puisse la voir
            setTimeout(() => {
                history.replaceState(null, '', window.location.pathname + '#c-' + vmid);
            }, 3000);
        }, 80);
    };
    waitAndApply();
}
function showWireToast(msg, kind) {
    const t = document.createElement('div');
    t.className = 'wire-toast wire-toast-' + (kind || 'info');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.classList.add('wire-toast-out'); setTimeout(() => t.remove(), 400); }, 4500);
}
function flashDeployButton() {
    const btn = document.querySelector('#deploy-palette .dp-deploy-btn');
    if (!btn) return;
    btn.classList.add('dp-deploy-flash');
    setTimeout(() => btn.classList.remove('dp-deploy-flash'), 2400);
    btn.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

// ─── Onglet Ressources (étalonnage CPU) ───────────────────────────────────────────────────────
// La brique (barre réservé/consommé/pic + états) vient de window.MXLRessources (static/ressources.js,
// partagée avec le widget compact des pages plugin). Ici : le contrôle de campagne (démarrer / chrono
// / arrêter / choisir / enregistrer) + la liste complète, propres à cette page.
let _resStarted = false;      // n'active le polling qu'à la première visite de l'onglet
let _resListTimer = null;
let _resChronoTimer = null;
let _resCampaignDebut = null;
let _resStatePollTimer = null;

function resTabActivate() {
    if (_resStarted) return;
    _resStarted = true;
    resLoadList();
    _resListTimer = setInterval(resLoadList, MXLRessources.REFRESH_MS);
    resRefreshCampaign();   // reprend une campagne déjà en cours (ex. onglet quitté puis revisité)

    const startBtn = document.getElementById('res-campaign-start');
    const stopBtn  = document.getElementById('res-campaign-stop');
    const saveBtn  = document.getElementById('res-campaign-save');
    if (!RES_CAN_DEPLOY) {
        startBtn.disabled = true;
        startBtn.title = window.t('containers.ressources.calibrate_need_perm');
    } else {
        startBtn.title = '';
        startBtn.onclick = async () => {
            startBtn.disabled = true;
            const libelle = (document.getElementById('res-campaign-libelle').value || '').trim();
            try {
                const r = await fetch('/api/etalonnage/start', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(libelle ? { libelle } : {}),
                });
                const j = await r.json().catch(() => ({}));
                if (!r.ok || !j.ok) {
                    alert(j.error || r.statusText);
                    return;
                }
                document.getElementById('res-campaign-result').hidden = true;
                resStartChrono(j.etat.debut);
                resStatePoll();
            } finally { startBtn.disabled = false; }
        };
        stopBtn.onclick = async () => {
            stopBtn.disabled = true;
            try {
                const r = await fetch('/api/etalonnage/stop', { method: 'POST' });
                const j = await r.json().catch(() => ({}));
                if (!r.ok || !j.ok) { alert(j.error || r.statusText); return; }
                resStopChrono();
                resRenderResult(j.resultat);
            } finally { stopBtn.disabled = false; }
        };
        saveBtn.onclick = async () => {
            const checked = Array.from(document.querySelectorAll('.res-campaign-result-row input[type=checkbox]:checked'))
                .map(cb => parseInt(cb.dataset.vmid, 10));
            const note = (document.getElementById('res-campaign-note').value || '').trim();
            const status = document.getElementById('res-campaign-save-status');
            saveBtn.disabled = true;
            status.className = 'res-brick-status'; status.textContent = '';
            try {
                const r = await fetch('/api/etalonnage/save', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ vmids: checked, note: note || undefined }),
                });
                const j = await r.json().catch(() => ({}));
                if (!r.ok || !j.ok) {
                    status.className = 'res-brick-status err'; status.textContent = '✕ ' + (j.error || r.statusText);
                } else {
                    status.className = 'res-brick-status ok';
                    status.textContent = window.t('containers.ressources.saved').replace('{n}', j.enregistres);
                    resLoadList();
                }
            } finally { saveBtn.disabled = false; }
        };
    }
}

function resStartChrono(debutEpoch) {
    _resCampaignDebut = debutEpoch;
    document.getElementById('res-campaign-idle').hidden = true;
    document.getElementById('res-campaign-live').hidden = false;
    if (_resChronoTimer) clearInterval(_resChronoTimer);
    _resChronoTimer = setInterval(() => {
        const el = document.getElementById('res-campaign-chrono');
        if (el) el.textContent = MXLRessources.fmtChrono(Date.now() / 1000 - _resCampaignDebut);
    }, 1000);
}
function resStopChrono() {
    if (_resChronoTimer) { clearInterval(_resChronoTimer); _resChronoTimer = null; }
    if (_resStatePollTimer) { clearInterval(_resStatePollTimer); _resStatePollTimer = null; }
    document.getElementById('res-campaign-idle').hidden = false;
    document.getElementById('res-campaign-live').hidden = true;
}

// Poll léger de l'état pendant une campagne EN COURS (points collectés, nœuds injoignables).
function resStatePoll() {
    if (_resStatePollTimer) clearInterval(_resStatePollTimer);
    const tick = async () => {
        const st = await MXLRessources.fetchState();
        if (st.etat !== 'en_cours') { resStopChrono(); if (st.etat === 'expire') resRenderResult(st); return; }
        const pts = document.getElementById('res-campaign-points');
        if (pts) pts.textContent = window.t('containers.ressources.points').replace('{n}', st.points || 0);
        const errBox = document.getElementById('res-campaign-errors');
        const nErr = Object.keys(st.erreurs || {}).length;
        if (errBox) {
            errBox.style.display = nErr ? '' : 'none';
            errBox.textContent = nErr ? window.t('containers.ressources.node_errors').replace('{n}', nErr) : '';
        }
    };
    tick();
    _resStatePollTimer = setInterval(tick, 3000);
}

// Reprend l'affichage d'une campagne déjà en cours (visite tardive de l'onglet) ou le dernier résultat clos.
async function resRefreshCampaign() {
    const st = await MXLRessources.fetchState();
    if (st.etat === 'en_cours') { resStartChrono(st.debut); resStatePoll(); }
    else if (st.etat === 'termine' || st.etat === 'expire') { resRenderResult(st); }
}

function resRenderResult(r) {
    const box = document.getElementById('res-campaign-result');
    const list = document.getElementById('res-campaign-results');
    if (!r || !r.conteneurs) { box.hidden = true; return; }
    box.hidden = false;
    const errBox = document.getElementById('res-campaign-errors');
    const nErr = Object.keys(r.erreurs || {}).length;
    if (r.etat === 'expire' && errBox) {
        errBox.style.display = ''; errBox.textContent = window.t('containers.ressources.expired');
    } else if (errBox && nErr) {
        errBox.style.display = ''; errBox.textContent = window.t('containers.ressources.node_errors').replace('{n}', nErr);
    } else if (errBox) { errBox.style.display = 'none'; }
    list.innerHTML = r.conteneurs.map(l => {
        const m = l.mesure || {};
        const vals = `n=${m.n} · ${window.t('js.res_median')} ${MXLRessources.fmtPct(m.median)}`
                   + ` · ${window.t('js.res_p95')} ${MXLRessources.fmtPct(m.p95)}`
                   + ` · ${window.t('js.res_peak')} ${MXLRessources.fmtPct(m.max)}`;
        const warn = !l.suffisant
            ? window.t('containers.ressources.insufficient').replace('{n}', m.n || 0)
            // `pointe_vue === null` = type au coût CONTINU : une série plate y est une mesure
            // complète, pas un manque. N'avertir que là où un régime coûteux existe vraiment.
            : (l.pointe_vue === false ? window.t('containers.ressources.no_peak') : '');
        return '<label class="res-campaign-result-row' + (l.suffisant ? '' : ' res-insufficient') + '">'
            + '<input type="checkbox" class="ios-toggle" data-vmid="' + (l.vmid ?? '') + '" '
            + (l.suffisant && l.vmid != null ? 'checked' : 'disabled') + '>'
            + '<span class="res-campaign-result-name">' + MXLRessources.esc(l.hostname)
            + (l.hors_modele ? ' <span class="meta">(' + MXLRessources.esc(window.t('containers.ressources.unknown_container')) + ')</span>' : '') + '</span>'
            + '<span class="res-campaign-result-vals">' + vals + '</span>'
            + (warn ? '<span class="res-campaign-result-warn">' + MXLRessources.esc(warn) + '</span>' : '')
            + '</label>';
    }).join('') || '<p class="meta">—</p>';
}

async function resLoadList() {
    const box = document.getElementById('res-list');
    if (!box) return;
    const items = await MXLRessources.fetchRessources();
    if (!items.length) { box.innerHTML = '<p class="meta">' + window.t('containers.ressources.no_containers') + '</p>'; return; }
    box.innerHTML = items.map(c => MXLRessources.brickHtml(c, { showActions: true })).join('');
    MXLRessources.wireGuarantees(box);
}




/* ─── Migration d'un conteneur vers un autre nœud ─────────────────────────────
   La modale montre la SIMULATION avant tout : verdict par nœud, vérifications, et
   conséquences pour les voisins (flux qui deviennent distants ou locaux). La bascule
   n'est proposée qu'ensuite, par nœud, et demande une confirmation nommée — c'est une
   opération destructive avec coupure, elle ne doit pas partir au premier clic. */
let _migVmid = null, _migHote = '';

function ouvrirMigration(vmid, hostname) {
    _migVmid = vmid; _migHote = hostname || String(vmid);
    document.getElementById('migration-hote').textContent = _migHote;
    document.getElementById('migration-corps').innerHTML =
        `<p style="color:var(--text-muted)">${window.t('migration.loading')}</p>`;
    document.getElementById('migration-modal').style.display = 'flex';
    fetch(`/api/containers/${vmid}/migration/simuler`)
        .then(r => r.json())
        .then(d => _migRendre(d.candidats || []))
        .catch(e => {
            document.getElementById('migration-corps').innerHTML =
                `<p class="migration-echec">${escapeHtml(String(e))}</p>`;
        });
}

function fermerMigration() {
    document.getElementById('migration-modal').style.display = 'none';
    _migVmid = null;
}

function _migRendre(candidats) {
    if (!candidats.length) {
        document.getElementById('migration-corps').innerHTML =
            `<p style="color:var(--text-muted)">—</p>`;
        return;
    }
    const html = candidats.map(p => {
        const nom = escapeHtml(p.node_cible_nom || String(p.node_cible));
        // Le nœud OÙ IL EST DÉJÀ : pas une destination, mais la référence de la comparaison —
        // il montre ce que le départ du conteneur lui rendrait. Ni bouton ni mention d'échec :
        // ce n'est pas un refus, c'est le point de départ.
        if (p.est_source) {
            return `<div class="migration-noeud migration-noeud-source">
                <div class="migration-noeud-tete"><strong>${nom}</strong>
                    <span class="migration-ici">${window.t('migration.current_node')}</span></div>
                ${_migJauges(p.ressources, 'libere')}
            </div>`;
        }
        // Un refus de TYPE est définitif : pas de bouton du tout, et le motif en clair —
        // un refus sans motif se lit comme une limitation arbitraire.
        if ((p.refus || []).length) {
            return `<div class="migration-noeud migration-noeud-refus">
                <div class="migration-noeud-tete"><strong>${nom}</strong>
                    <span class="migration-verdict-ko">${window.t('migration.refused')}</span></div>
                ${p.refus.map(r => `<div class="migration-motif">${escapeHtml(r)}</div>`).join('')}
            </div>`;
        }
        const verifs = (p.verifications || []).map(v =>
            `<div class="migration-verif ${v.ok ? 'ok' : 'ko'}">
                <span>${v.ok ? '✓' : '✗'}</span>
                <span>${escapeHtml(v.nom)}</span>
                <span class="migration-detail">${escapeHtml(v.detail || '')}</span>
             </div>`).join('');
        const jauges = _migJauges(p.ressources, 'prend');
        const conseq = (p.consequences || []).length
            ? `<div class="migration-conseq"><strong>${window.t('migration.consequences')}</strong>` +
              p.consequences.map(c =>
                  `<div>${escapeHtml(c.hostname || '')} (${c.vmid}) — ${escapeHtml((c.flux || []).join(', '))}
                    → ${escapeHtml(c.effet || '')}</div>`).join('') + `</div>`
            : '';
        // Vérifications en échec → on n'offre que « passer outre », explicitement nommé.
        const bouton = p.ok
            ? `<button class="btn btn-blue" onclick="_migLancer(${p.node_cible}, '${nom}', false)">${window.t('migration.go')}</button>`
            : `<button class="btn btn-orange" title="${window.t('migration.force_title')}"
                       onclick="_migLancer(${p.node_cible}, '${nom}', true)">${window.t('migration.force')}</button>`;
        return `<div class="migration-noeud">
            <div class="migration-noeud-tete"><strong>${nom}</strong>${bouton}</div>
            ${verifs}${jauges}${conseq}
        </div>`;
    }).join('');
    document.getElementById('migration-corps').innerHTML =
        html + `<p class="migration-conserve">${window.t('migration.keeps')}</p>`;
}

function _migJauge(r, libelle, mode) {
    // Deux barres pour la MÊME ressource du MÊME nœud : son état actuel, puis ce qu'il deviendrait.
    //   mode 'prend'  → la seconde barre ajoute, hachurée, la part que le conteneur occuperait ;
    //   mode 'libere' → elle retranche ce qu'il rendrait en partant (nœud d'origine).
    // La part se dessine à la frontière du plein, donc à sa place réelle sur l'échelle du nœud.
    if (!r || !r.total) return '';
    const total = r.total, libre = (r.libre == null) ? null : r.libre;
    const pris  = (libre == null) ? null : Math.max(0, total - libre);
    const libere = (mode === 'libere') ? Math.max(0, r.libere || 0) : 0;
    const part   = (mode === 'prend')  ? Math.max(0, r.requis || 0) : 0;
    const trop = part > 0 && libre != null && part > libre;
    const fmt = (v) => (r.unite === 'Mo' ? (v / 1024).toFixed(1) + ' Go' : v + ' ' + (r.unite || ''));
    const pct = (v) => Math.max(0, Math.min(100, v / total * 100)).toFixed(1);
    // Après : le plein retranche ce qui part, la hachure porte le delta (pris ou rendu).
    const pleinApres = (pris == null) ? 0 : Math.max(0, pris - libere);
    const libreApres = (libre == null) ? null : Math.max(0, libre - part + libere);
    const delta = libere ? `<span class="mig-gain">−${fmt(libere)}</span>`
                : part   ? `<span class="${trop ? 'mig-trop' : ''}">+${fmt(part)}</span>` : '';
    const barre = (pleinPct, hachPct, cls) => `<div class="mig-barre">
            <div class="mig-barre-pris" style="width:${pleinPct}%"></div>
            ${hachPct > 0 ? `<div class="mig-barre-part ${cls}" style="width:${hachPct}%"></div>` : ''}
        </div>`;
    return `<div class="mig-jauge">
        <div class="mig-jauge-tete"><span>${escapeHtml(libelle)}</span>
            <span class="mig-jauge-chiffres">${libre == null ? '—' : fmt(libre)} ${window.t('migration.free')}${delta ? ' · ' + delta : ''}</span></div>
        <div class="mig-duo">
            <div class="mig-duo-col"><span class="mig-duo-lbl">${window.t('migration.now')}</span>
                ${barre(pct(pris == null ? 0 : pris), 0, '')}</div>
            <div class="mig-duo-col"><span class="mig-duo-lbl">${window.t('migration.after')}</span>
                ${barre(pct(pleinApres), Math.min(100 - pct(pleinApres), Number(pct(part + libere))),
                        trop ? 'mig-barre-trop' : (libere ? 'mig-barre-gain' : ''))}</div>
        </div>
        <div class="mig-apres">${libreApres == null ? '' :
            window.t('migration.after_free').replace('{v}', fmt(libreApres))}</div>
    </div>`;
}

function _migProc(cpu) {
    // Le modèle de processeur, sa fréquence et son nombre de cœurs. « Plus de cœurs libres » ne
    // veut pas dire « plus puissant » : deux nœuds peuvent offrir le même nombre de cœurs sans
    // jouer dans la même catégorie. Les cœurs PHYSIQUES sont donnés à côté des threads — un
    // E5-2699 v4 annonce 88 threads pour 44 cœurs, et confondre les deux fait croire à un nœud
    // deux fois plus capable qu'il n'est.
    const p = cpu && cpu.proc;
    if (!p || !p.modele) return '';
    const bouts = [];
    if (p.physiques) bouts.push(window.t('migration.cpu_cores').replace('{n}', p.physiques));
    else if (p.threads) bouts.push(window.t('migration.cpu_threads').replace('{n}', p.threads));
    if (p.physiques && p.threads && p.threads > p.physiques)
        bouts.push(window.t('migration.cpu_threads').replace('{n}', p.threads));
    if (p.ghz) bouts.push(p.ghz.toFixed(2).replace(/0$/, '') + ' GHz');
    return `<div class="mig-proc" title="${escapeHtml(p.modele)}">
        <span class="mig-proc-nom">${escapeHtml(p.modele)}</span>
        <span class="mig-proc-det">${bouts.join(' · ')}</span>
    </div>`;
}

function _migJauges(res, mode) {
    if (!res) return '';
    return `<div class="mig-jauges-uni">
        ${_migProc(res.cpu)}
        ${_migJauge(res.cpu, window.t('migration.res_cpu'), mode)}
        ${_migJauge(res.ram, window.t('migration.res_ram'), mode)}
        ${_migGpu(res.gpu, mode)}
    </div>`;
}

function _migGpu(g, mode) {
    // Le GPU ne se RÉSERVE pas : aucune part n'y est dessinée. Ce qui change en migrant, c'est le
    // nombre de CLIENTS CUDA — mesuré sur ce parc : un mur seul consomme 16 % du GPU, trois murs
    // 40 %. C'est le compte AVANT → APRÈS qui informe, la VRAM libre n'ayant jamais annoncé une
    // cadence.
    if (!g) return '';
    const nom = g.nom ? escapeHtml(g.nom) : window.t('migration.res_gpu_none');
    if (!g.total) {
        return `<div class="mig-jauge"><div class="mig-jauge-tete"><span>${window.t('migration.res_gpu')}</span>
            <span class="mig-jauge-chiffres mig-gpu-absent">${nom}</span></div></div>`;
    }
    const n0 = g.clients;
    const n1 = (n0 == null) ? null : (mode === 'libere' ? Math.max(0, n0 - 1) : n0 + 1);
    const pctVram = Math.min(100, (g.total - (g.libre ?? g.total)) / g.total * 100).toFixed(1);
    return `<div class="mig-jauge">
        <div class="mig-jauge-tete"><span>${window.t('migration.res_gpu')}</span>
            <span class="mig-jauge-chiffres">${nom}</span></div>
        <div class="mig-barre" title="${escapeHtml(g.note || '')}">
            <div class="mig-barre-pris" style="width:${pctVram}%"></div>
        </div>
        <div class="mig-gpu-detail" title="${escapeHtml(g.note || '')}">
            ${n0 == null ? '—' : `${window.t('migration.res_gpu_clients').replace('{n}', n0)} → <span class="${
                mode === 'libere' ? 'mig-gain' : ''}">${n1}</span>`}${
            g.util_pct != null ? ` · ${Math.round(g.util_pct)} %` : ''}
        </div>
    </div>`;
}

function _migLancer(nodeId, nom, forcer) {
    if (!confirm(window.t('migration.confirm').replace('{h}', _migHote).replace('{n}', nom))) return;
    const corps = document.getElementById('migration-corps');
    corps.innerHTML = `<p>${window.t('migration.running')}</p>`;
    fetch(`/api/containers/${_migVmid}/migration`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({node_id: nodeId, forcer: !!forcer})
    }).then(r => r.json().then(d => ({ok: r.ok, d})))
      .then(({ok, d}) => {
        const etapes = (d.etapes || []).map(e =>
            `<div class="migration-verif ${e.ok ? 'ok' : 'ko'}"><span>${e.ok ? '✓' : '✗'}</span>
             <span>${escapeHtml(e.etape)}</span>
             <span class="migration-detail">${escapeHtml(e.detail || '')}</span></div>`).join('');
        corps.innerHTML =
            `<p class="${d.ok ? 'migration-ok' : 'migration-echec'}">
                ${d.ok ? window.t('migration.done') : window.t('migration.failed')}</p>` +
            (d.erreur ? `<div class="migration-motif">${escapeHtml(d.erreur)}</div>` : '') + etapes;
        // La liste des cartes se rafraîchit d'elle-même toutes les 5 s (rafraichirDonnees) ;
        // on la relance tout de suite pour que le conteneur apparaisse sur son nouveau nœud.
        if (d.ok && typeof rafraichirDonnees === 'function') rafraichirDonnees();
      })
      .catch(e => { corps.innerHTML = `<p class="migration-echec">${escapeHtml(String(e))}</p>`; });
}

// ─── Onglet Inventaire : ce que les agents voient, confronté à la base ──────────
//
// Les autres vues listent la table `containers`. Celle-ci liste `docker ps -a` par nœud et
// marque ce qui n'a plus de référence. C'est le seul endroit où un conteneur orphelin est
// visible — et donc destructible.

let _invCharge = false;

function invTabActivate() {
    if (_invCharge) return;          // pas de poll : un inventaire se rafraîchit à la demande
    _invCharge = true;
    invRefresh();
}

function _invEsc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _invBadge(classe) {
    const map = { managed: 'ok', rdma: 'ready', orphan: 'warn', foreign: 'ready' };
    return `<span class="badge ${map[classe] || 'ready'}">${_invEsc(window.t('containers.inv.class.' + classe))}</span>`;
}

function _invDetail(it) {
    const d = it.detail || {};
    if (it.classe === 'managed') {
        return _invEsc(window.t('containers.inv.detail.managed').replace('{vmid}', d.vmid)
                                                                 .replace('{hostname}', d.hostname || ''));
    }
    if (it.classe === 'rdma') {
        return _invEsc(window.t('containers.inv.detail.rdma').replace('{id}', d.link_id));
    }
    if (it.classe === 'orphan' && d.cause === 'rdma_link_absent') {
        return _invEsc(window.t('containers.inv.detail.orphan_rdma').replace('{id}', d.link_id));
    }
    if (it.classe === 'orphan') {
        return _invEsc(window.t('containers.inv.detail.orphan').replace('{vmid}', d.vmid));
    }
    return _invEsc(window.t('containers.inv.detail.foreign'));
}

async function invRefresh() {
    const body = document.getElementById('inv-body');
    const tot  = document.getElementById('inv-totals');
    if (!body) return;
    body.innerHTML = `<p class="meta">${_invEsc(window.t('containers.inv.loading'))}</p>`;
    let data;
    try {
        const r = await fetch('/api/inventaire');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        data = await r.json();
    } catch (e) {
        body.innerHTML = `<p class="meta">${_invEsc(window.t('containers.inv.load_error'))} ${_invEsc(e.message)}</p>`;
        return;
    }

    const t = data.totals || {};
    if (tot) {
        tot.innerHTML = ['orphan', 'foreign', 'rdma', 'managed'].map(c =>
            `<span class="inv-total"><b>${t[c] || 0}</b> ${_invEsc(window.t('containers.inv.class.' + c))}</span>`
        ).join('');
    }

    const parts = [];
    (data.nodes || []).forEach(n => {
        parts.push(`<section class="inv-node"><h3>${_invEsc(n.name)}</h3>`);
        if (!n.reachable) {
            parts.push(`<p class="meta inv-unreachable">${_invEsc(window.t('containers.inv.unreachable'))}`
                       + (n.error ? ' — ' + _invEsc(n.error) : '') + '</p></section>');
            return;
        }
        if (!n.items.length) {
            parts.push(`<p class="meta">${_invEsc(window.t('containers.inv.empty'))}</p></section>`);
            return;
        }
        parts.push('<table class="inv-table"><thead><tr>'
            + `<th>${_invEsc(window.t('containers.inv.col.name'))}</th>`
            + `<th>${_invEsc(window.t('containers.inv.col.state'))}</th>`
            + `<th>${_invEsc(window.t('containers.inv.col.image'))}</th>`
            + `<th>${_invEsc(window.t('containers.inv.col.classe'))}</th>`
            + `<th></th></tr></thead><tbody>`);
        n.items.forEach(it => {
            let action = '';
            if (it.destructible && INV_CAN_DELETE) {
                action = `<button type="button" class="btn btn-red inv-del"
                          data-node="${n.id}" data-name="${_invEsc(it.name)}"
                          >${_invEsc(window.t('containers.inv.destroy'))}</button>`;
            } else if (it.destructible) {
                action = `<span class="meta">${_invEsc(window.t('containers.inv.need_perm'))}</span>`;
            }
            parts.push(`<tr class="inv-row inv-${it.classe}">`
                + `<td><code>${_invEsc(it.name)}</code></td>`
                + `<td>${_invEsc(it.status)}</td>`
                + `<td class="inv-img">${_invEsc(it.image)}</td>`
                + `<td>${_invBadge(it.classe)}<div class="meta">${_invDetail(it)}</div></td>`
                + `<td class="inv-act">${action}</td></tr>`);
        });
        parts.push('</tbody></table></section>');
    });
    body.innerHTML = parts.join('');

    body.querySelectorAll('.inv-del').forEach(b => b.addEventListener('click', () => invDestroy(b)));
}

async function invDestroy(btn) {
    const node = btn.dataset.node, nom = btn.dataset.name;
    if (!confirm(window.t('containers.inv.confirm').replace('{name}', nom))) return;
    btn.disabled = true;
    btn.textContent = window.t('containers.inv.destroying');
    try {
        const r = await fetch(`/api/inventaire/${node}/${encodeURIComponent(nom)}/destroy`,
                              { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            // 409 = encore référencé : l'API refuse, et on dit par où passer.
            const cle = d.error === 'managed' ? 'containers.inv.refus_managed'
                      : d.error === 'rdma_link_alive' ? 'containers.inv.refus_rdma'
                      : 'containers.inv.destroy_error';
            alert(window.t(cle) + (d.detail ? '\n\n' + d.detail : ''));
            btn.disabled = false;
            btn.textContent = window.t('containers.inv.destroy');
            return;
        }
        invRefresh();
    } catch (e) {
        alert(window.t('containers.inv.destroy_error') + '\n\n' + e.message);
        btn.disabled = false;
        btn.textContent = window.t('containers.inv.destroy');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const b = document.getElementById('inv-refresh');
    if (b) b.addEventListener('click', invRefresh);
});
