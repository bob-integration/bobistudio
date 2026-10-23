/* ═══ BobiControls : bibliothèque de contrôles UNIVERSELS ═══════════════════════════════════
 *
 * Fader (vertical/horizontal), rotatif (avec poussoir on/off optionnel), bouton (verrouillé/
 * momentané, normal ou petit), rangée de petits boutons (exclusifs ou multiples), liste, afficheur,
 * vumètre (crête + RMS + maintien), vumètre de réduction de gain. Sans dépendance.
 * Pages de plugin ET Workspace s'en servent : même rendu, même clavier, mêmes événements.
 *
 *   const c = BobiControls.create('fader', el, {min:-Infinity, max:10, scale:'db', unit:'dB'});
 *   c.on('input', (v) => …);   // throttlé à une image d'animation pendant le geste
 *   c.on('change', (v) => …);  // au relâchement / à chaque action discrète
 *   c.set(-6);                 // programmatique : n'émet RIEN (pas d'écho réseau)
 *   c.on('push', (on) => …);   // rotatif à poussoir : bascule on/off (c.setPush / c.getPush)
 *   BobiControls.fromLeaf(leaf, el)   // feuille param_tree → contrôle adapté
 *
 * Charger aussi static/css/bobi-controls.css. Libellés via window.t (repli français/anglais).
 * La logique pure (échelles, ballistique, mapping des feuilles) est exportée pour les tests
 * hors-ligne (tests/verif_bobi_controls.js) : AUCUN accès DOM au chargement.
 */
(function (root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.BobiControls = api;
})(typeof window !== 'undefined' ? window : null, function (win) {
    'use strict';

    const MINUS = '−';

    // ─── Utilitaires purs ─────────────────────────────────────────────────────────────
    function tr(key, fallback) {
        try {
            if (win && typeof win.t === 'function') {
                const v = win.t(key);
                if (v && v !== key) return v;     // layout.t renvoie la clé si elle manque
            }
        } catch (e) { /* repli */ }
        return fallback;
    }
    const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
    function decimals(step) {
        if (!(step > 0)) return 0;
        const s = String(step);
        if (s.indexOf('e-') >= 0) return parseInt(s.split('e-')[1], 10);
        const i = s.indexOf('.');
        return i < 0 ? 0 : s.length - i - 1;
    }
    const roundTo = (v, d) => { const f = Math.pow(10, d); return Math.round(v * f) / f; };
    // Borne tolérante : JSON ne sait pas écrire −∞ (null / "-inf" / "-Infinity" acceptés).
    function parseBound(b, dflt) {
        if (b === undefined) return dflt;
        if (b === null) return -Infinity;
        if (typeof b === 'string') {
            const s = b.trim().toLowerCase();
            if (s === '-inf' || s === '-infinity' || s === MINUS + '∞') return -Infinity;
            if (s === 'inf' || s === 'infinity' || s === '+inf') return Infinity;
            const n = parseFloat(s);
            return isNaN(n) ? dflt : n;
        }
        return typeof b === 'number' && !isNaN(b) ? b : dflt;
    }
    const NO_SPACE_UNITS = { '%': 1, '°': 1, '×': 1 };
    function fmtNumber(v, o) {
        o = o || {};
        if (v === -Infinity) return MINUS + '∞' + (o.unit ? ' ' + o.unit : '');
        if (typeof v !== 'number' || !isFinite(v)) return String(v == null ? '' : v);
        if (o.scale === 'log') {
            // Jusqu'à 4 chiffres significatifs, zéros finaux retirés ; kHz au-delà de 1000 Hz.
            const k = o.unit === 'Hz' && v >= 1000;
            const x = k ? v / 1000 : v;
            const d = Math.max(0, 3 - Math.floor(Math.log10(Math.max(x, 1e-9))));
            let t = x.toFixed(d);
            if (t.indexOf('.') >= 0) t = t.replace(/0+$/, '').replace(/\.$/, '');
            return t + (o.unit ? ' ' + (k ? 'k' : '') + o.unit : '');
        }
        const dec = o.precision != null ? o.precision : (o.step ? decimals(o.step) : (o.scale === 'db' ? 1 : 0));
        let s = v.toFixed(dec);
        if (s[0] === '-') s = MINUS + s.slice(1);
        if (o.signed && v > 0) s = '+' + s;
        if (o.unit) s += (NO_SPACE_UNITS[o.unit] ? '' : ' ') + o.unit;
        return s;
    }

    // ─── Échelle valeur ↔ position (0..1) ─────────────────────────────────────────────
    // Loi de fader dB (type console, pente décroissante vers le haut, 0 dB à 75 % de course) :
    // l'oreille juge en dB, la course utile est donc autour de l'unité, pas dans le bas.
    // Genoux [position, dB] ; sous 0,5 % de course : −∞ (le cran de coupure).
    const DB_KNOTS = [[0, -90], [0.05, -60], [0.25, -30], [0.5, -12], [0.75, 0], [1, 10]];
    const DB_FLOOR = DB_KNOTS[0][1];
    const DB_CUT = 0.005;
    function pToDb(p) {
        const K = DB_KNOTS;
        let i = 0;
        while (i < K.length - 2 && p > K[i + 1][0]) i++;
        const a = K[i], b = K[i + 1];
        return a[1] + (p - a[0]) * (b[1] - a[1]) / (b[0] - a[0]);   // extrapole hors [0,1]
    }
    function dbToP(db) {
        const K = DB_KNOTS;
        let i = 0;
        while (i < K.length - 2 && db > K[i + 1][1]) i++;
        const a = K[i], b = K[i + 1];
        return a[0] + (db - a[1]) * (b[0] - a[0]) / (b[1] - a[1]);
    }

    function roundSig(v, n) {
        if (!(v > 0)) return v;
        const e = Math.floor(Math.log10(v)) - n + 1;
        return e >= 0 ? Math.round(v / Math.pow(10, e)) * Math.pow(10, e) : roundTo(Math.round(v * Math.pow(10, -e)) / Math.pow(10, -e), -e);
    }
    function makeScale(o) {
        o = o || {};
        let min = parseBound(o.min, 0), max = parseBound(o.max, 1);
        if (!(max > min)) max = min + 1;
        const db = o.scale === 'db' && min < 0 && max > 0;     // sinon linéaire (documenté)
        // LOG (fréquences) : la course est répartie en octaves, pas en hertz — sur 10 Hz…20 kHz en
        // linéaire, 160 px de rotatif valent 125 Hz par pixel et le grave est inatteignable.
        // `step` y est une fraction d'OCTAVE (défaut 1/24) ; la grille d'affichage est de
        // 3 chiffres significatifs, 4 en réglage fin.
        const log = !db && o.scale === 'log' && min > 0 && isFinite(max) && max > min;
        const lr = log ? Math.log(max / min) : 1;
        const minInf = min === -Infinity;
        const pMin = db ? (minInf || min <= DB_FLOOR ? 0 : dbToP(min)) : 0;
        const pMax = db ? dbToP(max) : 1;
        let step = log ? (o.octaveStep > 0 ? o.octaveStep : 1 / 24)
                       : (o.step > 0 ? o.step : (db ? 0.5 : (isFinite(min) && isFinite(max) ? (max - min) / 100 : 1)));
        const dec = decimals(step);
        const fin = (v) => (v === -Infinity || v === Infinity ? v : roundTo(v, Math.max(dec, 3)));
        const S = {
            kind: db ? 'db' : (log ? 'log' : 'linear'), min, max, step, db, log,
            // Pas du réglage FIN (Maj) : un dixième du pas — sauf grandeur ENTIÈRE (numéro de ligne,
            // nombre de voies), où il n'existe rien entre deux crans : le fin ralentit, il n'affine pas.
            fineStep: o.fineStep > 0 ? o.fineStep : (o.integer && !log ? step : step / 10),
            clamp(v) { return clamp(v, min, max); },
            toPos(v) {
                if (typeof v !== 'number' || isNaN(v)) return 0;
                v = clamp(v, min, max);
                if (db) {
                    if (v === -Infinity) return 0;
                    return clamp((dbToP(v) - pMin) / (pMax - pMin), 0, 1);
                }
                if (log) return clamp(Math.log(v / min) / lr, 0, 1);
                return clamp((v - min) / (max - min), 0, 1);
            },
            fromPos(q) {
                q = clamp(q, 0, 1);
                if (q >= 1) return max;
                if (db) {
                    const p = pMin + q * (pMax - pMin);
                    if (minInf && p < DB_CUT) return -Infinity;
                    if (q <= 0) return min;
                    return clamp(pToDb(p), min, max);
                }
                if (q <= 0) return min;
                if (log) return clamp(min * Math.exp(q * lr), min, max);
                return min + q * (max - min);
            },
            // Quantifie sur la grille `st` (défaut : step), ancrée sur 0 en dB, sur min sinon.
            quantize(v, st) {
                if (v === -Infinity || v === Infinity) return clamp(v, min, max);
                st = st > 0 ? st : step;
                if (log) return clamp(roundSig(v, st < step ? 4 : 3), min, max);
                const ref = db ? 0 : min;
                const d = Math.max(decimals(st), dec);
                let r = ref + Math.round((v - ref) / st) * st;
                r = roundTo(r, d + 1);
                return clamp(r, min, max);
            },
            // Pas clavier : dir = ±1, size = pas (défaut step). Depuis −∞ on remonte au plancher.
            nudge(v, dir, size) {
                size = size > 0 ? size : step;
                if (log) return clamp(roundSig(v * Math.pow(2, dir * size), size < step ? 4 : 3), min, max);
                if (v === -Infinity) return dir > 0 ? clamp(DB_FLOOR, min, max) : -Infinity;
                let r = roundTo(v + dir * size, Math.max(decimals(size), dec) + 1);
                if (db && minInf && r < DB_FLOOR) return -Infinity;
                return clamp(r, min, max);
            },
            default: undefined,
        };
        let d = o.default;
        if (d === undefined || d === null) d = db ? 0 : (isFinite(min) ? min : 0);
        S.default = clamp(parseBound(d, 0), min, max);
        return S;
    }

    // ─── Ballistique du vumètre (pure) ────────────────────────────────────────────────
    const toDb = (v) => (typeof v === 'number' && !isNaN(v) ? v : -Infinity);
    function meterState() { return { peak: -Infinity, rms: -Infinity, hold: -Infinity, holdAt: 0, clip: false }; }
    // st : état d'un canal ; inp : {peak, rms, peakHold?} en dBFS ; dt en s ; now en ms.
    function meterStep(st, inp, dt, now, o) {
        const fall = o.fall > 0 ? o.fall : 24, holdFall = o.holdFall > 0 ? o.holdFall : 12;
        const p = toDb(inp.peak), r = toDb(inp.rms != null ? inp.rms : inp.peak);
        st.peak = Math.max(p, st.peak - fall * dt);
        st.rms = Math.max(r, st.rms - fall * dt);
        if (typeof inp.peakHold === 'number') {
            st.hold = inp.peakHold;
        } else if (p >= st.hold) {
            st.hold = p; st.holdAt = now;
        } else if (now - st.holdAt > (o.holdMs != null ? o.holdMs : 1500)) {
            st.hold = Math.max(p, st.hold - holdFall * dt);
        }
        if (p >= (o.clipAt != null ? o.clipAt : 0)) st.clip = true;
        return st;
    }
    const meterPos = (db, floor, ceil) => (db === -Infinity || isNaN(db) ? 0 : clamp((db - floor) / (ceil - floor), 0, 1));

    // ─── Feuille param_tree → spécification de contrôle ───────────────────────────────
    const LEAF_UNITS = { scale: '×', angle: '°', percent: '%', pixels: 'px', duration: 'ms', db: 'dB', gain_db: 'dB' };
    function leafToSpec(leaf) {
        if (!leaf || typeof leaf !== 'object') return null;
        const type = leaf.type || 'number';
        const target = { key: leaf.key, endpoint: leaf.endpoint, path: leaf.path };
        const base = { label: leaf.label, target };
        if (leaf.default !== undefined) base.default = leaf.default;
        if (leaf.value !== undefined) base.value = leaf.value;
        if (type === 'text') return null;                       // pas de contrôle : champ texte
        if (type === 'bool') return { kind: 'toggle', opts: Object.assign(base, { mode: leaf.mode || 'latch', tone: leaf.tone, size: leaf.widget === 'small' ? 'small' : undefined }) };
        // Ensemble de valeurs (affectations à des bus) : rangée de petits boutons multiples.
        if (type === 'set') return { kind: 'buttons', opts: Object.assign(base, { options: (leaf.options || []).slice(), multiple: true }) };
        if (type === 'enum' && leaf.widget === 'buttons') return { kind: 'buttons', opts: Object.assign(base, { options: (leaf.options || []).slice() }) };
        if (type === 'enum') {
            return { kind: 'select', opts: Object.assign(base, { options: (leaf.options || []).slice(), variant: leaf.widget === 'list' ? 'list' : 'dropdown' }) };
        }
        const unit = leaf.unit != null ? leaf.unit : (LEAF_UNITS[type] || '');
        const isDb = type === 'db' || type === 'gain_db' || unit === 'dB' || leaf.scale === 'db';
        const o = Object.assign(base, { min: leaf.min, max: leaf.max, step: leaf.step, unit });
        if (isDb) o.scale = 'db';
        else if (leaf.scale === 'log' || (unit === 'Hz' && leaf.min > 0 && leaf.max / leaf.min >= 10 && leaf.scale !== 'linear')) o.scale = 'log';
        if (leaf.integer || type === 'int' || type === 'integer') o.integer = true;
        let kind;
        const w = leaf.widget;
        if (w === 'knob') kind = 'knob';
        else if (w === 'readout') kind = 'readout';
        else if (w === 'fader' || w === 'hfader') kind = 'fader';
        else kind = isDb ? 'fader' : (type === 'angle' ? 'knob' : 'fader');
        if (kind === 'fader') o.orientation = leaf.orientation || (isDb && w !== 'hfader' ? 'vertical' : (w === 'fader' ? 'vertical' : 'horizontal'));
        return { kind, opts: o };
    }

    // ─── Émetteur avec 'input' throttlé à l'image d'animation ─────────────────────────
    function defaultRaf(f) {
        return win && win.requestAnimationFrame ? win.requestAnimationFrame(f) : setTimeout(() => f(Date.now()), 16);
    }
    function defaultCaf(id) {
        if (win && win.cancelAnimationFrame) win.cancelAnimationFrame(id); else clearTimeout(id);
    }
    function createEmitter(rafFn, cafFn, inst) {
        rafFn = rafFn || defaultRaf; cafFn = cafFn || defaultCaf;
        const H = { input: [], change: [], push: [] };
        let rafId = 0, has = false, pend;
        function emit(type, v, source) {
            for (const h of H[type].slice()) {
                try { h(v, { type, source: source || 'user', instance: inst }); } catch (e) { if (win && win.console) win.console.error(e); }
            }
        }
        return {
            on(type, fn) {
                if (!H[type]) throw new Error('BobiControls: événement inconnu ' + type);
                H[type].push(fn);
                return () => { const i = H[type].indexOf(fn); if (i >= 0) H[type].splice(i, 1); };
            },
            // Pendant le geste : au plus un 'input' par image, toujours la DERNIÈRE valeur.
            queueInput(v) {
                pend = v; has = true;
                if (!rafId) rafId = rafFn(() => { rafId = 0; if (has) { has = false; emit('input', pend); } });
            },
            // Fin de geste ou action discrète : purge l'input en attente puis 'change'.
            commit(v) {
                if (rafId) { cafFn(rafId); rafId = 0; }
                if (has) { has = false; emit('input', pend); }
                emit('change', v);
            },
            // Action discrète : input + change tout de suite.
            discrete(v) {
                if (rafId) { cafFn(rafId); rafId = 0; }
                has = false;
                emit('input', v); emit('change', v);
            },
            // Poussoir d'un rotatif : canal SÉPARÉ de la valeur (autre paramètre, autre cible).
            push(v) { emit('push', v); },
            pending() { return has; },
            destroy() { if (rafId) cafFn(rafId); rafId = 0; has = false; H.input.length = 0; H.change.length = 0; H.push.length = 0; },
        };
    }

    // ═══ À partir d'ici : DOM ═════════════════════════════════════════════════════════
    let uid = 0;
    function h(tag, cls, attrs) {
        const e = win.document.createElement(tag);
        if (cls) e.className = cls;
        if (attrs) for (const k in attrs) { if (attrs[k] != null) e.setAttribute(k, attrs[k]); }
        return e;
    }
    function svg(tag, attrs) {
        const e = win.document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        return e;
    }

    // Instance de base : écouteurs traçables (destroy propre), émetteur, état désactivé.
    // Classe de FAMILLE posée sur l'élément hôte (cf. controls.css).
    const KIND_CLASS = { fader: 'ctl-fader', knob: 'ctl-knob', toggle: 'ctl-ui--toggle', select: 'ctl-ui--select',
        readout: 'ctl-ui--readout', meter: 'ctl-vu', grmeter: 'ctl-gr', buttons: 'ctl-btnrow' };
    // `adopt` : l'élément porte DÉJÀ son balisage (rotatif déclaratif d'un plugin) — on ne le vide
    // pas et on n'y pose aucune classe : le plugin garde exactement sa mise en page.
    function makeBase(kind, el, opts, adopt) {
        const L = [];
        const inst = { kind, el, opts };
        const em = createEmitter(null, null, inst);
        const b = {
            inst, em,
            listen(node, type, fn, o) { node.addEventListener(type, fn, o); L.push([node, type, fn, o]); },
            disabled: !!opts.disabled,
            cleanup: [],
        };
        inst.on = em.on;
        inst.setDisabled = (d) => {
            b.disabled = !!d;
            el.classList.toggle('ctl-ui-disabled', b.disabled);
            if (b.focusNode) b.focusNode.setAttribute('aria-disabled', b.disabled ? 'true' : 'false');
        };
        inst.destroy = () => {
            for (const [n, t, f, o] of L) n.removeEventListener(t, f, o);
            L.length = 0;
            em.destroy();
            b.cleanup.forEach((f) => f());
            if (adopt) return;
            el.classList.remove('ctl-ui', KIND_CLASS[kind], 'ctl-ui-disabled');
            el.textContent = '';
        };
        if (adopt) return b;
        el.textContent = '';
        // Le rotatif est celui du catalogue tel quel : pas d'habillage `ctl-ui` (police réduite).
        if (kind !== 'knob') el.classList.add('ctl-ui');
        el.classList.add(KIND_CLASS[kind]);
        if (b.disabled) el.classList.add('ctl-ui-disabled');
        return b;
    }

    // Geste pointeur unifié (souris, tactile, stylet) avec capture.
    function gesture(b, node, g) {
        let id = null, last = null;
        b.listen(node, 'pointerdown', (e) => {
            if (b.disabled || id !== null) return;
            if (e.pointerType === 'mouse' && e.button > 0) return;
            id = e.pointerId;
            try { node.setPointerCapture(id); } catch (_) { /* ignoré */ }
            last = { x: e.clientX, y: e.clientY };
            g.start(e);
            if (node.focus) node.focus({ preventScroll: true });
            e.preventDefault();
        });
        b.listen(node, 'pointermove', (e) => {
            if (e.pointerId !== id) return;
            const dx = e.clientX - last.x, dy = e.clientY - last.y;
            last = { x: e.clientX, y: e.clientY };
            g.move(e, dx, dy);
        });
        const end = (e) => {
            if (e.pointerId !== id) return;
            id = null;
            try { node.releasePointerCapture(e.pointerId); } catch (_) { /* ignoré */ }
            g.end(e, e.type === 'pointercancel');
        };
        b.listen(node, 'pointerup', end);
        b.listen(node, 'pointercancel', end);
        return { active: () => id !== null };
    }

    function parseValue(v, S) {
        if (typeof v === 'string') v = parseBound(v, NaN);
        return typeof v === 'number' && !isNaN(v) ? S.clamp(v) : undefined;
    }
    const WIDTH_MEMO = new Map();        // largeur réservée de l'affichage, par échelle + format (cf. continuous)
    const DETENT_PX = 8;                // largeur du cran magnétique au défaut, en pixels de geste
    const HINT_DEFAULT = () => tr('controls.hint_default', 'Double-clic : valeur par défaut · Maj : réglage fin');
    const HINT_PUSH = () => tr('controls.hint_push', 'Clic : on/off · glisser : valeur · ↺ ou Entrée : valeur par défaut · Maj : réglage fin');

    // Squelette commun aux contrôles à valeur continue (fader, rotatif).
    function continuous(b, kind, el, opts, bodyNode, onRender) {
        const S = makeScale(opts);
        const o = opts;
        const state = { value: S.default, pos: 0, dragging: false, changed: false };
        const initial = parseValue(o.value, S);
        if (initial !== undefined) state.value = initial;
        const dec = o.format || ((v) => fmtNumber(v, { unit: o.unit, step: S.step, scale: S.kind, precision: o.precision }));
        const valueEl = o.showValue === false ? null : (o.valueEl || h('span', 'ctl-ui-value'));
        const setText = (t) => { if (valueEl.tagName === 'INPUT') valueEl.value = t; else valueEl.textContent = t; };
        const ariaMin = isFinite(S.min) ? S.min : DB_FLOOR;
        // Largeur RÉSERVÉE au plus long affichage possible (balayage de la course + bornes) : sans
        // elle, « 6.0 dB » → « −12.5 dB » élargit le texte et fait bouger toute la mise en page.
        // Le balayage (≈ 400 formatages) est MÉMORISÉ par échelle + format : une console de 60 tranches crée
        // ~1 700 rotatifs aux mêmes échelles, et ce calcul pesait ~20 % de son rendu (mesuré 2026-10-18, Chrome).
        // Un `format` fourni par l'appelant n'a pas de clé : recalculé à chaque fois, comme avant.
        if (valueEl) {
            const key = o.format ? null : [S.kind, S.min, S.max, S.step, S.fineStep, S.default, o.unit, o.precision].join('|');
            let w = key === null ? undefined : WIDTH_MEMO.get(key);
            if (w === undefined) {
                w = 0;
                for (let i = 0; i <= 200; i++) w = Math.max(w, dec(S.fromPos(i / 200)).length, dec(S.quantize(S.fromPos(i / 200) * 1.00123, S.fineStep)).length);
                for (const v of [S.min, S.max, S.default]) w = Math.max(w, dec(v).length);
                if (key !== null) WIDTH_MEMO.set(key, w);
            }
            valueEl.style.minWidth = w + 'ch';
        }
        b.focusNode = bodyNode;
        bodyNode.setAttribute('role', 'slider');
        bodyNode.setAttribute('tabindex', '0');
        bodyNode.setAttribute('aria-valuemin', String(ariaMin));
        bodyNode.setAttribute('aria-valuemax', String(S.max));
        if (o.label) bodyNode.setAttribute('aria-label', o.label);
        else if (!bodyNode.hasAttribute('aria-label')) bodyNode.setAttribute('aria-label', tr('controls.' + kind, kind === 'knob' ? 'Rotary control' : 'Fader'));
        if (!bodyNode.hasAttribute('title')) bodyNode.setAttribute('title', o.push ? HINT_PUSH() : HINT_DEFAULT());
        if (b.disabled) bodyNode.setAttribute('aria-disabled', 'true');

        function render() {
            const v = state.value;
            state.pos = S.toPos(v);
            bodyNode.setAttribute('aria-valuenow', String(isFinite(v) ? v : ariaMin));
            bodyNode.setAttribute('aria-valuetext', dec(v));
            if (valueEl) setText(dec(v));
            el.classList.toggle('ctl-unity', S.db && v === 0);
            onRender(state, S);
        }
        function apply(v) {
            if (v === state.value) return false;
            state.value = v; state.changed = true;
            render();
            return true;
        }
        function setExternal(v) {
            const p = parseValue(v, S);
            if (p === undefined || state.dragging) return;
            state.value = p; render();
        }
        // Mouvement en espace position : accumulé en flottant (la quantification n'avale pas le fin).
        // CRAN MAGNÉTIQUE à la valeur par défaut (opts.detent, défaut actif) : en glissant, la valeur
        // s'arrête sur le défaut le temps de `det` (en fraction de course, ~8 px) avant d'en ressortir.
        // Le geste avance dans une coordonnée BRUTE où le cran occupe une bande ; la position
        // affichée est la brute privée de cette bande — aucun saut à l'entrée ni à la sortie.
        // Désactivé en réglage fin (Maj), où l'on veut justement passer finement autour du défaut.
        const p0 = () => S.toPos(S.default);
        const toRaw = (p, d) => (!d ? p : (Math.abs(p - p0()) < 1e-9 ? p : (p > p0() ? p + d : p - d)));
        const toEff = (r, d) => (!d ? r : (Math.abs(r - p0()) <= d ? p0() : (r > p0() ? r - d : r + d)));
        function moveBy(delta, fine) {
            const d = fine ? 0 : state.det;
            if (d !== state.curDet) { state.fpos = toRaw(state.pos, d); state.curDet = d; }
            state.fpos = clamp(state.fpos + delta, toRaw(0, d), toRaw(1, d));
            const inNotch = d > 0 && Math.abs(state.fpos - p0()) <= d;
            const v = inNotch ? S.default : S.quantize(S.fromPos(clamp(toEff(state.fpos, d), 0, 1)), fine ? S.fineStep : S.step);
            if (apply(v)) b.em.queueInput(v);
        }
        // Position ABSOLUE (clic sur la piste du fader) : pas de cran, la brute est recalée derrière.
        function moveTo(pos, fine) {
            const v = S.quantize(S.fromPos(clamp(pos, 0, 1)), fine ? S.fineStep : S.step);
            if (apply(v)) b.em.queueInput(v);
            state.fpos = toRaw(S.toPos(state.value), state.curDet);
        }
        function begin(det) {
            state.dragging = true; state.changed = false;
            state.det = o.detent === false ? 0 : (det || 0); state.curDet = state.det;
            state.fpos = toRaw(state.pos, state.det);
            el.classList.add('ctl-dragging');
        }
        function finish(cancelled) {
            state.dragging = false; el.classList.remove('ctl-dragging');
            if (state.changed) b.em.commit(state.value);   // annulé ou non : la valeur affichée fait foi
            state.changed = false;
        }
        // Rotatif à poussoir : le double-clic ferait basculer deux fois le poussoir (coupure audible
        // on→off→on) ; la valeur par défaut passe alors par Alt-clic (cf. buildKnob).
        b.listen(bodyNode, 'dblclick', () => {
            if (b.disabled || o.push) return;
            if (apply(S.default)) b.em.discrete(S.default);
        });
        b.listen(bodyNode, 'keydown', (e) => {
            if (b.disabled) return;
            const fine = e.shiftKey, k = e.key;
            const unit = fine ? S.fineStep : S.step;
            let v;
            if (k === 'ArrowUp' || k === 'ArrowRight') v = S.nudge(state.value, +1, unit);
            else if (k === 'ArrowDown' || k === 'ArrowLeft') v = S.nudge(state.value, -1, unit);
            else if (k === 'PageUp') v = S.nudge(state.value, +1, o.pageStep || S.step * 10);
            else if (k === 'PageDown') v = S.nudge(state.value, -1, o.pageStep || S.step * 10);
            else if (k === 'Home') v = S.min;
            else if (k === 'End') v = S.max;
            else if (k === 'Enter' || (k === ' ' && !o.push)) v = S.default;   // Espace : poussoir s'il y en a un
            else return;
            e.preventDefault();
            if (apply(v)) b.em.discrete(v);
        });
        const inst = b.inst;
        inst.set = setExternal;
        inst.get = () => state.value;
        inst.scale = S;
        return { S, state, render, valueEl, moveTo, moveBy, begin, finish, dec, apply };
    }

    // ─── Fader ────────────────────────────────────────────────────────────────────────
    function buildFader(el, opts) {
        const orient = opts.orientation === 'horizontal' ? 'horizontal' : 'vertical';
        const vert = orient === 'vertical';
        const b = makeBase('fader', el, opts);
        el.classList.add('ctl-fader--' + orient);
        const label = opts.label && opts.showLabel !== false ? h('span', 'ctl-ui-label') : null;
        if (label) label.textContent = opts.label;
        const body = h('div', 'ctl-fader__body');
        const track = h('div', 'ctl-fader__track');
        const fill = h('div', 'ctl-fader__fill');
        track.appendChild(fill);
        const thumb = h('div', 'ctl-fader__thumb');
        body.appendChild(track);
        const c = continuous(b, 'fader', el, opts, body, (st) => {
            body.style.setProperty('--ctl-p', st.pos.toFixed(5));
        });
        const S = c.S;
        // Repères : dB par défaut en vertical, sinon uniquement si `ticks` est fourni.
        let ticks = opts.ticks;
        if (ticks === undefined && S.db && vert) ticks = [10, 5, 0, -10, -20, -30, -50, -Infinity];   // espacés d'au moins ~14 px sur 10rem avec la loi dB (−40/−60 se chevauchaient)
        if (Array.isArray(ticks) && ticks.length) {
            const tk = h('div', 'ctl-fader__ticks', { 'aria-hidden': 'true' });
            for (const t of ticks) {
                if (t > S.max || t < S.min) continue;
                const m = h('span', 'ctl-tick' + (t === 0 && S.db ? ' ctl-tick--unity' : ''));
                m.style.setProperty('--ctl-tp', S.toPos(t).toFixed(5));
                m.textContent = t === -Infinity ? MINUS + '∞' : (t > 0 ? '+' : (t < 0 ? MINUS : '')) + Math.abs(t);
                tk.appendChild(m);
            }
            body.appendChild(tk);
        }
        body.appendChild(thumb);
        if (vert) body.setAttribute('aria-orientation', 'vertical');
        else body.setAttribute('aria-orientation', 'horizontal');
        if (label) el.appendChild(label);
        el.appendChild(body);
        if (c.valueEl) el.appendChild(c.valueEl);

        let geo = null;
        gesture(b, body, {
            start(e) {
                const r = body.getBoundingClientRect(), tr_ = thumb.getBoundingClientRect();
                const tl = vert ? tr_.height : tr_.width;
                const len = Math.max(1, (vert ? r.height : r.width) - tl);
                geo = { r, tl, len };
                c.begin(DETENT_PX / len);
                const onThumb = e.target === thumb;
                if (!onThumb) {            // clic sur la piste : le capot saute sous le pointeur
                    const along = vert ? (r.bottom - e.clientY - tl / 2) : (e.clientX - r.left - tl / 2);
                    c.moveTo(along / len, e.shiftKey);
                }
            },
            move(e, dx, dy) {
                c.moveBy((vert ? -dy : dx) / geo.len * (e.shiftKey ? 0.1 : 1), e.shiftKey);
            },
            end(e, cancelled) { c.finish(cancelled); },
        });
        c.render();
        return b.inst;
    }

    // ─── Rotatif — LE rotatif du catalogue ────────────────────────────────────────────
    // Un seul rotatif dans le produit (fusion du 2026-10-13) : même balisage `.ctl-knob`, mêmes
    // six tracés, même moteur, qu'il soit DÉCLARÉ dans le HTML d'un plugin (MXLControls.
    // attachKnobGestures → adoptKnob) ou CRÉÉ par programme (BobiControls.create('knob')).
    //
    // Tracé : la variante est déclarée par la classe `.ctl-knob--<tracé>` et lue dans le style
    // calculé (--ctl-knob-draw). `v01` = POSITION 0..1 (déjà passée par l'échelle : log, dB…),
    // `def01` = position du défaut (repère), `ouvert` = témoin central (gate/index ; undefined =
    // aucun témoin sur index, allumé sur gate — comportement historique), `zero01` = départ du
    // remplissage (index), pour qu'un réglage −/+ se lise depuis le centre.
    function knobSvg(kind, v01, def01, ouvert, zero01) {
        const R = 26, C = 32, a0 = 135, a1 = 405;
        const v = Math.max(0, Math.min(1, v01 || 0));
        const ang = a0 + (a1 - a0) * v;
        const pt = (d, r) => [C + r * Math.cos(d * Math.PI / 180), C + r * Math.sin(d * Math.PI / 180)];
        const arc = (r, f, t, cls, w) => {
            const p0 = pt(f, r), p1 = pt(t, r);
            return '<path class="' + cls + '" stroke-width="' + w + '" stroke-linecap="round" d="M' +
                p0[0].toFixed(1) + ' ' + p0[1].toFixed(1) + ' A' + r + ' ' + r + ' 0 ' +
                ((t - f) > 180 ? 1 : 0) + ' 1 ' + p1[0].toFixed(1) + ' ' + p1[1].toFixed(1) + '"/>';
        };
        let g = '';
        if (kind === 'needle') {
            const p = pt(ang, R - 4);
            g = '<circle class="ctl-knob-body" cx="' + C + '" cy="' + C + '" r="' + (R - 3) + '" stroke-width="1"/>' +
                '<line class="ctl-knob-ptr" stroke-width="2" stroke-linecap="round" x1="' + C + '" y1="' + C +
                '" x2="' + p[0].toFixed(1) + '" y2="' + p[1].toFixed(1) + '"/>';
        } else if (kind === 'collar') {
            for (let i = 0; i < 12; i++) {
                const d = a0 + (a1 - a0) * i / 11, q = pt(d, R - 2), lit = (i / 11) <= v;
                g += '<circle class="ctl-knob-seg' + (lit ? (i >= 10 ? ' on hot' : ' on') : '') + '" cx="' +
                     q[0].toFixed(1) + '" cy="' + q[1].toFixed(1) + '" r="2.6"/>';
            }
            g += '<circle class="ctl-knob-body" cx="' + C + '" cy="' + C + '" r="' + (R - 10) + '" stroke-width="1"/>';
        } else if (kind === 'index') {
            const z = (zero01 != null && zero01 > 0 && zero01 < 1) ? zero01 : 0;
            const az = a0 + (a1 - a0) * z;
            g = arc(R, a0, a1, 'ctl-knob-track', 3.5);
            if (Math.abs(ang - az) > 0.5) g += arc(R, Math.min(az, ang), Math.max(az, ang), 'ctl-knob-fill', 3.5);
            g += '<circle class="ctl-knob-body" cx="' + C + '" cy="' + C + '" r="' + (R - 6) + '" stroke-width="1"/>';
            const p0 = pt(ang, R - 15), p1 = pt(ang, R - 7);
            g += '<line class="ctl-knob-ptr" stroke-width="3" stroke-linecap="round" x1="' + p0[0].toFixed(1) +
                 '" y1="' + p0[1].toFixed(1) + '" x2="' + p1[0].toFixed(1) + '" y2="' + p1[1].toFixed(1) + '"/>';
            if (ouvert !== undefined) g += '<circle class="ctl-knob-seg' + (ouvert ? ' on' : '') + '" cx="' + C + '" cy="' + C + '" r="6"/>';
        } else if (kind === 'thick') {
            g = arc(R - 2, a0, a1, 'ctl-knob-track', 6) + arc(R - 2, a0, ang, 'ctl-knob-fill', 6);
        } else {                                   /* arc (défaut) et gate */
            g = arc(R, a0, a1, 'ctl-knob-track', 3) + arc(R, a0, ang, 'ctl-knob-fill', 3);
            if (kind === 'gate') {
                g += '<circle class="ctl-knob-seg' + (ouvert === false ? '' : ' on') + '" cx="' + C + '" cy="' + C + '" r="7"/>';
            } else {
                for (let i = 0; i <= 10; i++) {
                    const d = a0 + (a1 - a0) * i / 10, p = pt(d, R - 6), q = pt(d, R - 9);
                    g += '<line class="ctl-knob-tick' + ((i / 10) <= v ? ' on' : '') + '" stroke-width="1.5" x1="' +
                         p[0].toFixed(1) + '" y1="' + p[1].toFixed(1) + '" x2="' + q[0].toFixed(1) + '" y2="' + q[1].toFixed(1) + '"/>';
                }
            }
        }
        // Repère de VALEUR PAR DÉFAUT : un trait sur l'arc, plus long que les graduations. Il dit où
        // est le défaut, et si on y est (il prend l'accent) — sans lui, savoir qu'un réglage a
        // bougé oblige à le remettre à zéro pour comparer.
        if (def01 != null && kind !== 'needle') {
            const d = a0 + (a1 - a0) * Math.max(0, Math.min(1, def01));
            const p0 = pt(d, R + 4), p1 = pt(d, R - 7);
            g += '<line class="ctl-knob-def' + (Math.abs(def01 - v) < 0.005 ? ' on' : '') + '" stroke-width="2" ' +
                 'stroke-linecap="round" x1="' + p0[0].toFixed(1) + '" y1="' + p0[1].toFixed(1) +
                 '" x2="' + p1[0].toFixed(1) + '" y2="' + p1[1].toFixed(1) + '"/>';
        }
        return '<svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">' + g + '</svg>';
    }
    // Variante déclarée sur l'élément (--ctl-knob-draw), '' si illisible (nœud hors document).
    function knobKind(el) {
        try { return (win.getComputedStyle(el).getPropertyValue('--ctl-knob-draw') || '').trim(); } catch (e) { return ''; }
    }
    // Bouton ↺ de remise au défaut, POSÉ par le catalogue (un plugin pouvait l'oublier — et l'a
    // oublié). Il émet `ctl-knob-reset` sur le rotatif ; le moteur y répond. Idempotent.
    function ensureKnobReset(knob, libelle) {
        if (knob.querySelector('.ctl-knob-reset')) return;
        const b = win.document.createElement('button');
        b.type = 'button'; b.className = 'ctl-knob-reset'; b.tabIndex = -1; b.textContent = '↺';
        b.title = libelle || tr('controls.reset', 'Remettre à la valeur par défaut');
        b.setAttribute('aria-label', b.title);
        b.addEventListener('click', (e) => {
            e.preventDefault(); e.stopPropagation();
            knob.dispatchEvent(new win.CustomEvent('ctl-knob-reset', { bubbles: true }));
        });
        knob.appendChild(b);
    }

    // Moteur commun. `knob` = l'élément `.ctl-knob`, `hit` = sa zone `.ctl-knob-hit`. `legacy` =
    // contrat DÉCLARATIF historique, inchangé pour les plugins : `data-val` tenu à jour,
    // `ctl-knob-input` (detail.value) à chaque valeur, `ctl-knob-gate` (detail.on) pour le
    // poussoir, `knob._ctlPose(v, mode)`, valeur affichée « v + data-unit ».
    function wireKnob(b, knob, hit, opts, legacy) {
        const P = opts.push ? (typeof opts.push === 'object' ? opts.push : {}) : null;
        let pushOn = P ? !!(P.value !== undefined ? P.value : (P.default !== undefined ? P.default : true)) : false;
        if (legacy && P) pushOn = knob.dataset.gate !== '0';
        let kind = opts.draw || '';
        const c = continuous(b, 'knob', knob, Object.assign({}, opts, { valueEl: knob.querySelector('.ctl-knob-val') || null }), hit, (st, S) => {
            if (!kind) kind = knobKind(knob);          // relu tant que le nœud n'est pas dans le document
            const k = kind || 'arc';
            const hasDef = legacy ? knob.dataset.def != null : true;
            const def01 = hasDef ? S.toPos(S.default) : null;
            let ouvert;
            if (P) ouvert = pushOn;
            else if (knob.dataset.gate != null) ouvert = knob.dataset.gate !== '0';
            else if (k === 'gate') ouvert = true;
            const z = opts.zero != null ? opts.zero : (!S.db && !S.log && S.min < 0 && Math.abs(S.min + S.max) < 1e-9 ? 0 : null);
            hit.innerHTML = knobSvg(k, st.pos, def01, ouvert, z == null ? null : S.toPos(z));
            knob.classList.toggle('ctl-knob--off', ouvert === false);
            if (legacy && isFinite(st.value)) knob.dataset.val = st.value;
        });
        const SENS = 160;                       // px pour la course complète (×10 en réglage fin)
        const TAP = 3;                          // px : en deçà, le geste est un clic (poussoir)
        let travel = 0;
        function setPush(v, emit) {
            if (v === pushOn) return;
            pushOn = v;
            if (legacy) knob.dataset.gate = v ? '1' : '0';
            c.render();
            if (!emit) return;
            b.em.push(pushOn);
            if (legacy) knob.dispatchEvent(new win.CustomEvent('ctl-knob-gate', { bubbles: true, detail: { on: pushOn } }));
        }
        function toDefault() { if (!b.disabled && c.apply(c.S.default)) b.em.discrete(c.S.default); }
        if (P) {
            knob.classList.add('ctl-knob--push');
            b.listen(hit, 'keydown', (e) => {
                if (b.disabled || e.key !== ' ') return;
                e.preventDefault(); e.stopPropagation(); if (!e.repeat) setPush(!pushOn, true);
            });
        }
        b.listen(knob, 'ctl-knob-reset', toDefault);
        b.listen(hit, 'wheel', (e) => {
            if (b.disabled) return;
            e.preventDefault();
            const v = c.S.nudge(c.state.value, e.deltaY < 0 ? 1 : -1, e.shiftKey ? c.S.fineStep : c.S.step);
            if (c.apply(v)) b.em.discrete(v);
        }, { passive: false });
        gesture(b, hit, {
            start() { travel = 0; c.begin(DETENT_PX / SENS); },
            move(e, dx, dy) {
                travel += Math.abs(dx) + Math.abs(dy);
                if (P && travel < TAP) return;
                c.moveBy((dx - dy) / SENS * (e.shiftKey ? 0.1 : 1), e.shiftKey);
            },
            end(e, cancelled) {
                c.finish(cancelled);
                if (P && !cancelled && travel < TAP) setPush(!pushOn, true);
            },
        });
        if (legacy) {
            b.em.on('input', (v) => knob.dispatchEvent(new win.CustomEvent('ctl-knob-input', { bubbles: true, detail: { value: v } })));
            /* `_ctlPose(v, mode)` — contrat historique : undefined → pose ET émet (geste) ;
             * false → pose sans émettre ; 'force' → redessine et émet même à valeur égale ;
             * 'muet' → redessine sans émettre (après un changement d'attribut que le tracé lit :
             * bornes, défaut, data-gate). Les bornes sont RELUES dans les attributs. */
            knob._ctlPose = (v, mode) => {
                if (mode === 'force' || mode === 'muet') {
                    Object.assign(c.S, makeScale(legacyOpts(knob)));
                    if (P) pushOn = knob.dataset.gate !== '0';
                }
                v = c.S.quantize(c.S.clamp(Number(v)), c.S.step);
                if (isNaN(v)) return c.state.value;
                if (mode === false || mode === 'muet') { c.state.value = v; c.render(); return v; }
                if (v === c.state.value && mode !== 'force') return v;
                c.state.value = v; c.render(); b.em.discrete(v);
                return v;
            };
        }
        b.inst.setPush = (v) => setPush(!!v, false);   // programmatique : n'émet rien
        b.inst.getPush = () => pushOn;
        c.render();
        return c;
    }

    // Options d'un rotatif DÉCLARÉ, lues dans ses attributs. Sans `data-scale`, la valeur s'affiche
    // comme avant (« v + data-unit ») et le réglage fin ne descend pas sous le pas, sauf
    // `data-fine` : un plugin dont le paramètre est entier (numéro de ligne) n'a rien à déclarer.
    function legacyOpts(knob) {
        const d = knob.dataset, n = (k, def) => { const x = parseFloat(d[k]); return isNaN(x) ? def : x; };
        const o = { min: n('min', 0), max: n('max', 100), step: n('step', 1) || 1, value: n('val', n('min', 0)), unit: d.unit || '' };
        if (d.def != null) o.default = n('def', o.min);
        if (d.scale) o.scale = d.scale;
        o.fineStep = d.fine != null ? n('fine', o.step / 10) : o.step;
        if (d.zero != null) o.zero = n('zero', 0);
        if (d.detent === '0') o.detent = false;
        if (d.push != null) o.push = {};
        if (!d.scale) {
            const dec = decimals(o.step);
            o.format = (v) => (isFinite(v) ? parseFloat(v.toFixed(dec)) : v) + (d.unit || '');
        }
        return o;
    }
    // Rotatif DÉCLARÉ par un plugin : balisage existant, contrat historique. Idempotent.
    function adoptKnob(knob, libelleReset) {
        if (knob._ctlGestes) return knob._ctlInst || null;
        const hit = knob.querySelector('.ctl-knob-hit');
        if (!hit) return null;
        knob._ctlGestes = true;
        ensureKnobReset(knob, libelleReset);
        const opts = legacyOpts(knob);
        const b = makeBase('knob', knob, opts, true);
        wireKnob(b, knob, hit, opts, true);
        knob._ctlInst = b.inst;
        return b.inst;
    }
    // Rotatif CRÉÉ par programme : même balisage que le déclaratif, posé ici.
    // opts.draw : 'arc' (règle de parc) | 'index' | 'needle' | 'collar' | 'thick' | 'gate'.
    function buildKnob(el, opts) {
        const b = makeBase('knob', el, opts);
        el.classList.add('ctl-knob--' + (opts.draw || 'arc'));
        if (opts.label && opts.showLabel !== false) { const n = h('span', 'ctl-knob-name'); n.textContent = opts.label; el.appendChild(n); }
        const hit = h('div', 'ctl-knob-hit');
        el.appendChild(hit);
        if (opts.showValue !== false) el.appendChild(h('span', 'ctl-knob-val'));
        ensureKnobReset(el);
        wireKnob(b, el, hit, Object.assign({ draw: opts.draw || 'arc' }, opts), false);
        return b.inst;
    }

    // ─── Bouton verrouillé / momentané ────────────────────────────────────────────────
    function buildToggle(el, opts) {
        const b = makeBase('toggle', el, opts);
        const momentary = opts.mode === 'momentary';
        // Le poussoir du catalogue (`.ctl-push--led`) : témoin + teinte, l'état se lit deux fois.
        // Petit (`size: 'small'`) : sans témoin, la teinte et le filet portent l'état — sauf `led: true` : un
        // INTERRUPTEUR posé à côté de boutons de CHOIX (GÉN près des types de générateur, ACTIF près des types de
        // bande) doit se distinguer d'eux d'un coup d'œil, et c'est le témoin qui le dit (2026-10-17).
        const small = opts.size === 'small';
        const btn = h('button', 'ctl-push ctl-push--led' + (small ? ' ctl-push--small' : ''), { type: 'button', role: momentary ? 'button' : 'switch', 'aria-pressed': 'false' });
        if (opts.tone && opts.tone !== 'accent') btn.classList.add('ctl-push--' + opts.tone);
        if (!small || opts.led) btn.appendChild(h('span', 'ctl-led'));
        const txt = h('span'); txt.textContent = opts.label || tr('controls.toggle', 'Toggle'); btn.appendChild(txt);
        b.focusNode = btn;
        if (b.disabled) btn.setAttribute('aria-disabled', 'true');
        el.appendChild(btn);
        let on = opts.value !== undefined ? !!opts.value : !!opts.default;
        function render() { btn.setAttribute('aria-pressed', on ? 'true' : 'false'); }
        function user(v) { if (v === on) return; on = v; render(); b.em.discrete(on); }
        if (momentary) {
            let down = false;
            const press = () => { if (b.disabled || down) return; down = true; user(true); };
            const release = () => { if (!down) return; down = false; user(false); };
            b.listen(btn, 'pointerdown', (e) => { if (e.pointerType === 'mouse' && e.button > 0) return; try { btn.setPointerCapture(e.pointerId); } catch (_) { /* ignoré */ } press(); e.preventDefault(); btn.focus({ preventScroll: true }); });
            b.listen(btn, 'pointerup', release);
            b.listen(btn, 'pointercancel', release);
            b.listen(btn, 'keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); if (!e.repeat) press(); } });
            b.listen(btn, 'keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); release(); } });
            b.listen(btn, 'blur', release);
        } else {
            b.listen(btn, 'click', () => { if (!b.disabled) user(!on); });
        }
        b.inst.set = (v) => { on = !!v; render(); };
        b.inst.get = () => on;
        render();
        return b.inst;
    }

    // ─── Rangée de petits boutons ─────────────────────────────────────────────────────
    // Exclusifs (Pré/Post, source d'écoute) ou multiples (affectation aux masters / groupes).
    // opts : options [{value, label, tone?}], multiple (bool), value (tableau si multiple),
    // allowNone (exclusif : un second clic éteint), label, tone. Émet la valeur complète.
    function buildButtons(el, opts) {
        const b = makeBase('buttons', el, opts);
        const O = normOptions(opts.options).map((o, i) => Object.assign(o, { tone: (opts.options[i] || {}).tone }));
        const multi = !!opts.multiple;
        const label = opts.label && opts.showLabel !== false ? h('span', 'ctl-ui-label') : null;
        if (label) { label.textContent = opts.label; el.appendChild(label); }
        const row = h('div', 'ctl-btnrow__row', { role: multi ? 'group' : 'radiogroup' });
        if (opts.label) row.setAttribute('aria-label', opts.label);
        el.appendChild(row);
        const norm = (v) => (multi ? (Array.isArray(v) ? v : (v == null ? [] : [v])).map(String) : (v == null ? null : String(v)));
        let cur = norm(opts.value !== undefined ? opts.value : opts.default);
        const out = () => (multi ? O.filter((o) => cur.indexOf(String(o.value)) >= 0).map((o) => o.value) : (O.find((o) => String(o.value) === cur) || { value: null }).value);   // aucun = null (allowNone)
        const btns = O.map((o) => {
            const bt = h('button', 'ctl-push ctl-push--led ctl-push--small', { type: 'button' });
            const tone = o.tone || opts.tone;
            if (tone && tone !== 'accent') bt.classList.add('ctl-push--' + tone);
            if (!multi) bt.setAttribute('role', 'radio');
            bt.textContent = o.label;
            row.appendChild(bt);
            b.listen(bt, 'click', () => {
                if (b.disabled) return;
                const k = String(o.value);
                if (multi) cur = cur.indexOf(k) >= 0 ? cur.filter((x) => x !== k) : cur.concat(k);
                else if (cur === k) { if (!opts.allowNone) return; cur = null; }
                else cur = k;
                render(); b.em.discrete(out());
            });
            return bt;
        });
        b.focusNode = btns[0] || row;
        function render() {
            O.forEach((o, i) => {
                const on = multi ? cur.indexOf(String(o.value)) >= 0 : cur === String(o.value);
                btns[i].setAttribute(multi ? 'aria-pressed' : 'aria-checked', on ? 'true' : 'false');
            });
        }
        b.inst.set = (v) => { cur = norm(v); render(); };
        b.inst.get = out;
        render();
        return b.inst;
    }

    // ─── Liste / menu ─────────────────────────────────────────────────────────────────
    function normOptions(list) {
        return (list || []).map((o) => (o && typeof o === 'object' ? { value: o.value, label: o.label != null ? o.label : String(o.value) } : { value: o, label: String(o) }));
    }
    function buildSelect(el, opts) {
        const b = makeBase('select', el, opts);
        const O = normOptions(opts.options);
        const label = opts.label && opts.showLabel !== false ? h('label', 'ctl-ui-label') : null;
        let idx = 0;
        const first = opts.value !== undefined ? opts.value : opts.default;
        if (first !== undefined) { const i = O.findIndex((o) => String(o.value) === String(first)); if (i >= 0) idx = i; }
        let node;
        if (opts.variant === 'list') {
            node = h('div', 'ctl-list', { role: 'listbox', tabindex: '0', 'aria-label': opts.label || tr('controls.list', 'List') });
            const items = O.map((o, i) => { const it = h('div', 'ctl-list__item', { role: 'option', id: 'ctl-o' + (++uid) }); it.textContent = o.label; it.dataset.i = i; node.appendChild(it); return it; });
            const render = () => { items.forEach((it, i) => { it.setAttribute('aria-selected', i === idx ? 'true' : 'false'); it.classList.toggle('on', i === idx); }); if (items[idx]) node.setAttribute('aria-activedescendant', items[idx].id); };
            const pick = (i) => { i = clamp(i, 0, O.length - 1); if (i === idx || !O.length) return; idx = i; render(); b.em.discrete(O[idx].value); };
            b.listen(node, 'click', (e) => { const it = e.target.closest && e.target.closest('.ctl-list__item'); if (it && !b.disabled) pick(+it.dataset.i); });
            b.listen(node, 'keydown', (e) => {
                if (b.disabled) return;
                const k = e.key;
                if (k === 'ArrowDown' || k === 'ArrowRight') pick(idx + 1);
                else if (k === 'ArrowUp' || k === 'ArrowLeft') pick(idx - 1);
                else if (k === 'Home') pick(0);
                else if (k === 'End') pick(O.length - 1);
                else return;
                e.preventDefault();
            });
            b.inst.set = (v) => { const i = O.findIndex((o) => String(o.value) === String(v)); if (i >= 0) { idx = i; render(); } };
            b.inst.render = render;
            render();
        } else {
            node = h('select', 'ctl-select', { 'aria-label': opts.label || tr('controls.select', 'Select') });
            O.forEach((o, i) => { const op = h('option'); op.value = String(i); op.textContent = o.label; node.appendChild(op); });
            node.value = String(idx);
            b.listen(node, 'change', () => { idx = +node.value; b.em.discrete(O[idx].value); });
            b.inst.set = (v) => { const i = O.findIndex((o) => String(o.value) === String(v)); if (i >= 0) { idx = i; node.value = String(i); } };
        }
        b.focusNode = node;
        if (b.disabled) { node.setAttribute('aria-disabled', 'true'); if ('disabled' in node) node.disabled = true; }
        if (label) { label.textContent = opts.label; el.appendChild(label); }
        el.appendChild(node);
        b.inst.get = () => (O[idx] ? O[idx].value : undefined);
        return b.inst;
    }

    // ─── Afficheur numérique ──────────────────────────────────────────────────────────
    function buildReadout(el, opts) {
        const b = makeBase('readout', el, opts);
        if (opts.label && opts.showLabel !== false) { const l = h('span', 'ctl-ui-label'); l.textContent = opts.label; el.appendChild(l); }
        // L'afficheur du catalogue : <span class="ctl-readout"><b>valeur</b><span>unité</span></span>.
        const box = h('span', 'ctl-readout'), v = h('b'), u = h('span');
        box.appendChild(v); if (opts.unit) { u.textContent = opts.unit; box.appendChild(u); }
        el.appendChild(box);
        let cur = opts.value !== undefined ? opts.value : (opts.default !== undefined ? opts.default : 0);
        const render = () => { v.textContent = fmtNumber(cur, { step: opts.step, scale: opts.scale, precision: opts.precision, signed: opts.signed }); };
        b.inst.set = (x) => { cur = typeof x === 'string' ? parseBound(x, x) : x; render(); };
        b.inst.get = () => cur;
        render();
        return b.inst;
    }

    // ─── Échelle de repères (vumètre / réduction de gain) ─────────────────────────────
    function scaleEl(values, posFn, fmt) {
        const sc = h('div', 'ctl-scale', { 'aria-hidden': 'true' });
        for (const t of values) {
            const m = h('span', 'ctl-tick');
            m.style.setProperty('--ctl-tp', posFn(t).toFixed(5));
            m.textContent = fmt(t);
            sc.appendChild(m);
        }
        return sc;
    }

    // ─── Vumètre (crête + RMS + maintien) ─────────────────────────────────────────────
    function buildMeter(el, opts) {
        const b = makeBase('meter', el, opts);
        const vert = opts.orientation !== 'horizontal';
        const floor = opts.floor != null ? opts.floor : -60, ceil = opts.ceil != null ? opts.ceil : 0;
        const n = opts.channels === 2 ? 2 : 1;
        el.classList.add('ctl-vu--' + (vert ? 'vertical' : 'horizontal'));
        el.setAttribute('role', 'meter');
        el.setAttribute('aria-valuemin', String(floor));
        el.setAttribute('aria-valuemax', String(ceil));
        el.setAttribute('aria-label', opts.label || tr('controls.meter', 'Level meter'));
        const okEnd = meterPos(opts.warnAt != null ? opts.warnAt : -9, floor, ceil) * 100;
        const warnEnd = meterPos(opts.errAt != null ? opts.errAt : -3, floor, ceil) * 100;
        const dir = vert ? 'to top' : 'to right';
        const grad = 'linear-gradient(' + dir + ', var(--ctl-ok) 0 ' + okEnd + '%, var(--ctl-warn) ' + okEnd + '% ' + warnEnd + '%, var(--ctl-err) ' + warnEnd + '% 100%)';
        const wrap = h('div', 'ctl-vu__chs');
        const chs = [];
        for (let i = 0; i < n; i++) {
            const ch = h('div', 'ctl-vu__ch');
            const pk = h('div', 'ctl-vu__peak'), rm = h('div', 'ctl-vu__rms'), hd = h('div', 'ctl-vu__hold');
            pk.style.background = grad; rm.style.background = grad;
            const clip = h('button', 'ctl-vu__clip', { type: 'button', title: tr('controls.clip_reset', 'Clip — click to reset'), 'aria-label': tr('controls.clip_reset', 'Clip — click to reset') });
            ch.appendChild(pk); ch.appendChild(rm); ch.appendChild(hd);
            wrap.appendChild(ch);
            chs.push({ st: meterState(), pk, rm, hd, clip, ch });
        }
        // Voyants de saturation : ligne séparée, hors des barres.
        const clips = h('div', 'ctl-vu__clips');
        chs.forEach((c) => clips.appendChild(c.clip));
        const body = h('div', 'ctl-vu__body');
        body.appendChild(clips); body.appendChild(wrap);
        el.appendChild(body);
        if (opts.scale !== false) {
            const tv = Array.isArray(opts.ticks) ? opts.ticks : [0, -6, -12, -18, -24, -36, -48, -60].filter((t) => t >= floor && t <= ceil);
            el.appendChild(scaleEl(tv, (t) => meterPos(t, floor, ceil), (t) => (t === 0 ? '0' : MINUS + Math.abs(t))));
        }
        const num = opts.readout ? h('span', 'ctl-ui-value') : null;
        if (num) {
            // Largeur réservée (cf. continuous) : le niveau change 10 fois par seconde.
            num.style.minWidth = Math.max(...[floor, ceil, -199.9, -Infinity].map((v) => fmtNumber(v, { unit: 'dBFS', precision: 1 }).length)) + 'ch';
            el.appendChild(num);
        }
        b.listen(clips, 'click', (e) => {
            if (!(e.target.closest && e.target.closest('.ctl-vu__clip'))) return;
            chs.forEach((c) => { c.st.clip = false; });
            paint(false);
        });

        let input = { peak: [], rms: [], peakHold: [] }, lastT = 0, rafId = 0, ariaT = 0;
        const arr = (x, i) => (Array.isArray(x) ? x[i] : (typeof x === 'number' ? x : undefined));
        const side = vert ? 'bottom' : 'left';
        // Écrit seulement ce qui change (pas de thrash) : un vumètre au repos ou à niveau stable ne touche plus au
        // style à chaque image (le commentaire le promettait, le code réécrivait tout — 2026-10-18).
        const styleIf = (n, k, v) => { const m = n._ctlSt || (n._ctlSt = {}); if (m[k] !== v) { m[k] = v; n.style[k] = v; } };
        function setClip(c, p) {
            const v = (100 - p * 100).toFixed(2);
            styleIf(c, 'clipPath', vert ? 'inset(' + v + '% 0 0 0)' : 'inset(0 ' + v + '% 0 0)');
        }
        function paint() {
            let worst = -Infinity;
            chs.forEach((c) => {
                setClip(c.pk, meterPos(c.st.peak, floor, ceil));
                setClip(c.rm, meterPos(c.st.rms, floor, ceil));
                const hp = meterPos(c.st.hold, floor, ceil);
                styleIf(c.hd, side, 'calc(' + (hp * 100).toFixed(2) + '% - 1px)');
                styleIf(c.hd, 'opacity', c.st.hold === -Infinity ? '0' : '1');
                if (c.clip.dataset.on !== (c.st.clip ? '1' : '0')) { c.clip.dataset.on = c.st.clip ? '1' : '0'; c.clip.classList.toggle('on', c.st.clip); }
                if (c.st.hold > worst) worst = c.st.hold;
            });
            return worst;
        }
        function frame(now) {
            rafId = 0;
            const dt = lastT ? clamp((now - lastT) / 1000, 0, 0.25) : 0;
            lastT = now;
            let busy = false;
            chs.forEach((c, i) => {
                const inp = { peak: arr(input.peak, i), rms: arr(input.rms, i), peakHold: arr(input.peakHold, i) };
                meterStep(c.st, inp, dt, now, opts);
                const tp = toDb(inp.peak), tr_ = toDb(inp.rms != null ? inp.rms : inp.peak);
                if ((c.st.peak > tp + 0.01 && c.st.peak > floor) || (c.st.rms > tr_ + 0.01 && c.st.rms > floor) || (typeof inp.peakHold !== 'number' && c.st.hold > tp + 0.01 && c.st.hold > floor)) busy = true;
            });
            const worst = paint();
            if (num) num.textContent = fmtNumber(worst, { unit: 'dBFS', precision: 1 });
            if (now - ariaT > 250) {          // aria : jamais plus de 4 mises à jour/s
                ariaT = now;
                const pk = Math.max.apply(null, chs.map((c) => c.st.peak));
                el.setAttribute('aria-valuenow', String(isFinite(pk) ? pk.toFixed(1) : floor));
                el.setAttribute('aria-valuetext', fmtNumber(pk, { unit: 'dBFS', precision: 1 }));
            }
            if (busy) schedule(); else lastT = 0;
        }
        function schedule() { if (!rafId) rafId = defaultRaf(frame); }
        b.cleanup.push(() => { if (rafId) defaultCaf(rafId); });
        b.inst.set = (v) => {
            if (typeof v === 'number') v = { peak: v, rms: v };
            input = v || {};
            schedule();
        };
        b.inst.get = () => input;
        b.inst.resetClip = () => { chs.forEach((c) => { c.st.clip = false; }); paint(); };
        paint();
        return b.inst;
    }

    // ─── Vumètre de réduction de gain ─────────────────────────────────────────────────
    function buildGrMeter(el, opts) {
        const b = makeBase('grmeter', el, opts);
        const vert = opts.orientation !== 'horizontal';
        const max = opts.max > 0 ? opts.max : 20;
        el.classList.add('ctl-gr--' + (vert ? 'vertical' : 'horizontal'));
        el.setAttribute('role', 'meter');
        el.setAttribute('aria-valuemin', '0');
        el.setAttribute('aria-valuemax', String(max));
        el.setAttribute('aria-label', opts.label || tr('controls.gr_meter', 'Gain reduction'));
        if (opts.label && opts.showLabel !== false) { const l = h('span', 'ctl-ui-label'); l.textContent = opts.label; el.appendChild(l); }
        const track = h('div', 'ctl-gr__track');
        const fill = h('div', 'ctl-gr__fill');
        track.appendChild(fill);
        const row = h('div', 'ctl-gr__row');
        row.appendChild(track);
        el.appendChild(row);
        const tv = Array.isArray(opts.ticks) ? opts.ticks : [0, 3, 6, 12, 20].filter((t) => t <= max);
        row.appendChild(scaleEl(tv, (t) => clamp(t / max, 0, 1), (t) => (t === 0 ? '0' : MINUS + t)));
        const num = opts.readout === false ? null : h('span', 'ctl-ui-value');
        if (num) {
            num.style.minWidth = Math.max(...[max, 99.9].map((v) => fmtNumber(-v, { unit: 'dB', precision: 1 }).length)) + 'ch';
            el.appendChild(num);
        }
        let cur = 0;
        function render() {
            const p = clamp(cur / max, 0, 1);
            const rest = (100 - p * 100).toFixed(2);
            // Vertical : la barre descend depuis le haut ; horizontal : elle part de la droite.
            fill.style.clipPath = vert ? 'inset(0 0 ' + rest + '% 0)' : 'inset(0 0 0 ' + rest + '%)';
            el.setAttribute('aria-valuenow', cur.toFixed(1));
            const txt = cur === 0 ? '0 dB' : fmtNumber(-cur, { unit: 'dB', precision: 1 });
            el.setAttribute('aria-valuetext', txt);
            if (num) num.textContent = txt;
        }
        b.inst.set = (v) => { v = Math.abs(typeof v === 'string' ? parseFloat(v) : v); cur = isNaN(v) ? 0 : v; render(); };   // + ou − accepté
        b.inst.get = () => cur;
        render();
        return b.inst;
    }

    // ─── API publique ─────────────────────────────────────────────────────────────────
    const BUILDERS = {
        fader: buildFader,
        hfader: (el, o) => buildFader(el, Object.assign({}, o, { orientation: 'horizontal' })),
        knob: buildKnob, toggle: buildToggle, select: buildSelect, buttons: buildButtons,
        list: (el, o) => buildSelect(el, Object.assign({}, o, { variant: 'list' })),
        readout: buildReadout, meter: buildMeter, grmeter: buildGrMeter,
    };
    function create(kind, el, opts) {
        const f = BUILDERS[kind];
        if (!f) throw new Error('BobiControls: type inconnu « ' + kind + ' »');
        if (!el || !el.classList) throw new Error('BobiControls: élément DOM requis');
        return f(el, Object.assign({}, opts));
    }
    // Retourne null si la feuille n'a pas de contrôle (type texte) : l'appelant garde son champ.
    function fromLeaf(leaf, el, extra) {
        const spec = leafToSpec(leaf);
        if (!spec) return null;
        const inst = create(spec.kind, el, Object.assign(spec.opts, extra));
        inst.leaf = leaf;
        return inst;
    }

    return {
        create, fromLeaf, leafToSpec,
        // Catalogue (controls.js délègue ici) : tracé, variante, ↺, rotatif déclaratif
        knobSvg, knobKind, ensureKnobReset, adoptKnob,
        // Exports de logique pure (tests)
        makeScale, meterStep, meterState, meterPos, createEmitter, fmtNumber,
        DB_KNOTS, version: '1.0.0',
    };
});
