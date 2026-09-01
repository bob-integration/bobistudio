// ─── Steppers numériques GLOBAUX : boutons −/+ confortables ──────────────────────────────────
// Les spinners natifs de <input type=number> sont minuscules → on enrobe chaque champ d'un stepper
// avec deux GROS boutons (clic + appui maintenu pour répéter). Généralisé depuis le multiview
// (étalon : le champ « Max entrées » d'un mur). Idempotent ; opt-out via la classe `no-stepper`.
// Conserve l'id et le onchange du champ (l'input est DÉPLACÉ, pas recréé).
//
// ★ POURQUOI CE FICHIER EXISTE (ne le refusionne pas dans scripts.js) :
// ce code vivait dans `static/scripts.js`, chargé UNIQUEMENT par containers.html et projects.html.
// Les pages de contrôle de plugin (templates/plugin_section.html — Traitements, Médias, Sources,
// Streams, Destinations) ne le chargent PAS : le MutationObserver n'y tournait donc jamais et les
// champs numériques des plugins restaient SANS boutons, en silence. Le CSS était bon, les champs
// étaient bons, personne ne posait les boutons. Même famille que window.BobiFonts, qui souffrait
// exactement du même mal. Il est désormais chargé par templates/layout.html → disponible PARTOUT.
// Multiplicateur de pas selon les modificateurs : Maj = ×10 (plus vite), Alt = ÷10 (plus fin).
// Lu à CHAQUE tick de la répétition (pas seulement au pointerdown) : on peut donc appuyer, puis
// enfoncer Maj en cours de route pour accélérer, ou Alt pour affiner — sans relâcher le bouton.
const STEP_FAST = 10, STEP_FINE = 0.1;

// Le raccourci ne doit pas être un secret : on le DIT au survol. Et on le dit avec le nom des touches
// TELLES QU'ELLES SONT GRAVÉES SUR LE CLAVIER de l'utilisateur : sur Mac, la touche `altKey` du
// navigateur s'appelle ⌥ Option (et Maj s'appelle ⇧). Écrire « Alt » à un utilisateur Mac, c'est le
// laisser chercher une touche qui n'existe pas chez lui. Le comportement, lui, est identique.
function _stepperIsMac() {
    const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
    return /mac|iphone|ipad|ipod/i.test(p);
}
function _stepperHint() {
    const mac = _stepperIsMac();
    const fast = mac ? '⇧ Maj' : 'Maj', fine = mac ? '⌥ Option' : 'Alt';
    if (document.documentElement.lang === 'en') {
        return `${mac ? '⇧ Shift' : 'Shift'} = ×10 (faster) · ${fine} = ÷10 (finer)`;
    }
    return `${fast} = ×10 (plus vite) · ${fine} = ÷10 (plus fin)`;
}

function _stepperFactor(mods) {
    if (mods.shift) return STEP_FAST;
    if (mods.alt)   return STEP_FINE;
    return 1;
}
function stepperStep(input, dir, factor) {
    const step = (parseFloat(input.step) || 1) * (factor || 1);
    const min  = input.min !== '' ? parseFloat(input.min) : -Infinity;
    const max  = input.max !== '' ? parseFloat(input.max) :  Infinity;
    let v = parseFloat(input.value); if (isNaN(v)) v = 0;
    v = Math.min(max, Math.max(min, v + dir * step));
    v = parseFloat(v.toFixed(6));   // évite les imprécisions flottantes (step 0.5…)
    input.value = v;
    // Un spinner natif <input type=number> émet `input` PUIS `change`. On reproduit les DEUX : sans
    // le `input`, tout champ câblé en `oninput=` (39 dans l'app, ex. le composer des modèles de carte)
    // ignorait les boutons +/− → la valeur affichée changeait mais l'état JS restait figé (« je mets
    // Audio: 0 au stepper mais ça compte encore 1 audio »). Additif : les champs en `onchange=` seul
    // ne voient pas le `input` (pas de handler) et gardent leur comportement.
    input.dispatchEvent(new Event('input',  { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}
function _stepperBindHold(btn, inp, fn) {
    let to = null, iv = null;
    // État des modificateurs SUIVI EN CONTINU pendant l'appui : un pointerdown ne porte que l'état
    // au moment du clic, or on veut pouvoir changer de vitesse en cours de répétition.
    const mods = {shift: false, alt: false};
    const onKey = e => { mods.shift = e.shiftKey; mods.alt = e.altKey; };
    const stop = () => {
        if (to === null && iv === null) return;
        clearTimeout(to); clearInterval(iv); to = iv = null;
        window.removeEventListener('pointerup', stop);
        window.removeEventListener('pointercancel', stop);
        window.removeEventListener('blur', stop);
        window.removeEventListener('keydown', onKey);
        window.removeEventListener('keyup', onKey);
    };
    const tick = () => {
        // ★ GARDE-FOU : le `change` émis par un pas peut faire RE-RENDRE le panneau du plugin →
        // le bouton (et parfois le champ) sont DÉTRUITS sous nos pieds. Sans ça, l'intervalle
        // tournait dans le vide À L'INFINI : la valeur défilait sans jamais s'arrêter (vécu).
        if (!inp.isConnected) { stop(); return; }
        fn(mods);
    };
    btn.addEventListener('pointerdown', e => {
        e.preventDefault();
        mods.shift = !!e.shiftKey; mods.alt = !!e.altKey;
        // ★ L'ARRÊT est écouté sur WINDOW, pas sur le bouton : un pointerup ne parvient jamais à un
        // bouton qui vient d'être remplacé par un re-rendu. La survie de l'arrêt ne doit PAS
        // dépendre de la survie de l'élément qui l'a déclenché.
        window.addEventListener('pointerup', stop);
        window.addEventListener('pointercancel', stop);
        window.addEventListener('blur', stop);      // Alt peut donner le focus au menu système
        window.addEventListener('keydown', onKey);
        window.addEventListener('keyup', onKey);
        tick();
        to = setTimeout(() => { iv = setInterval(tick, 60); }, 350);
    });
    btn.addEventListener('pointerleave', stop);     // le curseur quitte le bouton → on arrête
}
function enhanceSteppers(root) {
    (root || document).querySelectorAll('input[type="number"]').forEach(inp => {
        if (inp.closest('.num-stepper')) return;                       // déjà enrobé
        if (inp.classList.contains('no-stepper') || inp.closest('.no-stepper')) return;  // opt-out
        inp.classList.add('has-stepper');
        const wrap = document.createElement('div');
        wrap.className = 'num-stepper';
        inp.parentNode.insertBefore(wrap, inp);
        const mk = (cls, txt) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'num-btn ' + cls; b.tabIndex = -1;
            b.textContent = txt; b.setAttribute('aria-hidden', 'true');
            b.title = _stepperHint();
            return b;
        };
        const dec = mk('num-dec', '−'), inc = mk('num-inc', '+');
        wrap.append(dec, inp, inc);
        _stepperBindHold(dec, inp, m => stepperStep(inp, -1, _stepperFactor(m)));
        _stepperBindHold(inc, inp, m => stepperStep(inp, +1, _stepperFactor(m)));
    });
}
// Auto-enrobage des champs ajoutés après coup (schéma Réglages, config plugin, fragments de contrôle
// plugin injectés, palette de déploiement…). Debounce via rAF pour grouper les rafales de mutations.
let _stepperPending = false;
function _scheduleEnhanceSteppers() {
    if (_stepperPending) return;
    _stepperPending = true;
    requestAnimationFrame(() => { _stepperPending = false; enhanceSteppers(document); });
}
(function _initStepperObserver() {
    const arm = () => {
        if (!document.body) return;
        enhanceSteppers(document);          // passe initiale (rendu serveur)
        new MutationObserver(muts => {
            for (const m of muts) {
                for (const n of m.addedNodes) {
                    if (n.nodeType !== 1) continue;
                    if (n.matches && n.matches('input[type="number"]')) { _scheduleEnhanceSteppers(); return; }
                    if (n.querySelector && n.querySelector('input[type="number"]')) { _scheduleEnhanceSteppers(); return; }
                }
            }
        }).observe(document.body, { childList: true, subtree: true });
    };
    if (document.body) arm(); else document.addEventListener('DOMContentLoaded', arm);
})();
