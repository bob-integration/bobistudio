// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Chevron de la bande de sous-menu (`.subnav-deck`, cf. base.css), aligné sous l'entrée ALLUMÉE de
// la barre du haut : c'est lui qui dit de quelle page la bande est le sous-menu.
//
// Position MESURÉE et re-mesurée : la barre change de taille (mode condensé, passage sur deux
// lignes, chargement des polices, viewport réécrit après coup sur iPad par layout.html). Un calcul
// unique au chargement serait faux.
(function () {
    function place() {
        const on = document.querySelector('.topnav a.active');
        document.querySelectorAll('.subnav-deck').forEach(deck => {
            let notch = deck.querySelector(':scope > .subnav-deck-notch');
            if (!notch) {
                notch = document.createElement('span');
                notch.className = 'subnav-deck-notch';
                notch.setAttribute('aria-hidden', 'true');
                deck.appendChild(notch);
            }
            if (!on) { notch.hidden = true; return; }
            const r = on.getBoundingClientRect(), d = deck.getBoundingClientRect();
            notch.style.left = Math.round(r.left + r.width / 2 - d.left) + 'px';
            notch.hidden = false;
        });
        // Ensemble à deux rangs dans une page (Réglages, 07/11) : chevron sous son groupe allumé.
        document.querySelectorAll('.navpair').forEach(pair => {
            const deck = pair.querySelector(':scope > .navpair-deck');
            const lit = pair.querySelector(':scope > .navpair-top .navpair-items > .tab.active');
            if (!deck) return;
            let notch = deck.querySelector(':scope > .subnav-deck-notch');
            if (!notch) {
                notch = document.createElement('span');
                notch.className = 'subnav-deck-notch';
                notch.setAttribute('aria-hidden', 'true');
                deck.appendChild(notch);
            }
            if (!lit || deck.hidden) { notch.hidden = true; return; }
            const r = lit.getBoundingClientRect(), d = deck.getBoundingClientRect();
            notch.style.left = Math.round(r.left + r.width / 2 - d.left) + 'px';
            notch.hidden = false;
        });
    }
    function start() {
        place();
        const nav = document.querySelector('.topnav');
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(place);
            if (nav) ro.observe(nav);
            document.querySelectorAll('.subnav-deck, .navpair-deck, .navpair-top').forEach(d => ro.observe(d));
        } else {
            window.addEventListener('resize', place);
        }
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
    window.subnavDeckPlace = place;
})();
