// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Pyramide — console de monitoring embarquée (plugin). Montée par le shell Traitements via
// window.MXLPlugins.pyramide.mount(el, vmid, ctx). Lit /api/pyramide/overview (cache, pas de
// réseau live), affiche par source les proxies produits (#conso, orphelins), les besoins non
// couverts et les KPIs ; bouton « Optimiser » → /api/pyramide/reconcile à chaud.
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins.pyramide = (function () {
    let EL = null, VMID = null, TOAST = () => {}, timer = null, node = null;

    /* i18n (catalogue plugin.pyramide.*). Clé non résolue → on garde le français passé en repli :
     * `window.t` rend la clé brute quand elle manque, l'écrire tel quel afficherait son nom. */
    const T = (k, repli) => {
        const v = window.t ? window.t(k) : k;
        return (v && v !== k) ? v : (repli !== undefined ? repli : k);
    };

    const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
        c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

    async function load() {
        if (!EL) return;
        let data;
        try { data = await (await fetch("/api/pyramide/overview")).json(); }
        catch (e) { return; }
        const me = (data.pyramides || []).find(p => String(p.vmid) === String(VMID));
        render(me, data.kpi || {});
    }

    function render(me, kpi) {
        if (!EL) return;
        const kel = EL.querySelector('[data-el="kpi"]');
        const body = EL.querySelector('[data-el="body"]');
        if (!me) {
            kel.innerHTML = "";
            body.innerHTML = '<div class="pyr-empty">' + esc(T('plugin.pyramide.not_started', 'Pyramide non démarrée ou aucune donnée live.')) + '</div>';
            return;
        }
        node = me.node_id;
        const pct = kpi.pct || {};
        kel.innerHTML =
            `<span class="pyr-meta">${esc(T('plugin.pyramide.kpi_meta','seuil {seuil} · socle {socle}').replace('{seuil}', me.threshold).replace('{socle}', me.base_octaves))}</span>` +
            `<span class="pyr-pill pyr-copy">${esc(T('plugin.pyramide.kpi_copy','copie {n}%').replace('{n}', pct.copy || 0))}</span>` +
            `<span class="pyr-pill pyr-strided">${esc(T('plugin.pyramide.kpi_strided','strided {n}%').replace('{n}', pct.strided || 0))}</span>` +
            `<span class="pyr-pill pyr-gather">${esc(T('plugin.pyramide.kpi_gather','gather {n}%').replace('{n}', pct.gather || 0))}</span>` +
            `<span class="pyr-pill pyr-full">${esc(T('plugin.pyramide.kpi_full','plein {n}%').replace('{n}', pct.full || 0))}</span>` +
            (me.orphans ? `<span class="pyr-warn">${esc(T('plugin.pyramide.orphans','{n} orphelin(s)').replace('{n}', me.orphans))}</span>` : "") +
            (me.unmet && me.unmet.length ? `<span class="pyr-warn">${esc(T('plugin.pyramide.unmet_count','{n} non couvert(s)').replace('{n}', me.unmet.length))}</span>` : "");

        const srcs = me.sources || {};
        const live = me.live || {};
        const fpsBySrc = {};
        Object.values(live).forEach(s => { if (s && s.shm) fpsBySrc[s.shm] = s.fps; });
        const keys = Object.keys(srcs).sort();
        if (!keys.length) { body.innerHTML = '<div class="pyr-empty">' + esc(T('plugin.pyramide.no_source', 'Aucune source câblée.')) + '</div>'; return; }

        let html = "";
        keys.forEach(src => {
            const fps = fpsBySrc[src];
            html += `<div class="pyr-src"><div class="pyr-src-h">${esc(src)}` +
                (fps != null ? ` <span class="pyr-fps">${fps} fps</span>` : "") + `</div>`;
            html += '<table class="pyr-tbl"><thead><tr><th>' + esc(T('plugin.pyramide.col_proxy','proxy')) + '</th><th>'
                + esc(T('plugin.pyramide.col_size','taille')) + '</th><th>' + esc(T('plugin.pyramide.col_kind','type')) + '</th>'
                + '<th>' + esc(T('plugin.pyramide.col_consumers','conso')) + '</th><th></th></tr></thead><tbody>';
            (srcs[src] || []).forEach(pr => {
                html += `<tr class="${pr.orphan ? "pyr-orphan" : ""}">` +
                    `<td class="pyr-mono">${esc(pr.shm)}</td>` +
                    `<td>${pr.w || "?"}×${pr.h || "?"}</td>` +
                    `<td>${esc(pr.kind === "custom" ? T('plugin.pyramide.kind_custom','sur-mesure') : T('plugin.pyramide.kind_octave','octave'))}</td>` +
                    `<td>${pr.consumers}${pr.orphan ? ' <span class="pyr-warn">' + esc(T('plugin.pyramide.orphan','orphelin')) + '</span>' : ""}</td>` +
                    `<td><button class="pyr-mon" data-shm="${esc(pr.shm)}" title="${esc(T('plugin.pyramide.monitor_tip','Monitorer'))}">📺</button></td></tr>`;
            });
            html += "</tbody></table>";
            const um = (me.unmet || []).filter(u => u.src === src);
            if (um.length) {
                html += '<div class="pyr-unmet">' + esc(T('plugin.pyramide.unmet','Besoins non couverts : ')) +
                    um.map(u => `${u.w}×${u.h} ×${u.count}${u.would_qualify ? T('plugin.pyramide.above_threshold'," (≥seuil)") : ""}`).join(", ") +
                    "</div>";
            }
            html += "</div>";
        });
        body.innerHTML = html;
        body.querySelectorAll(".pyr-mon").forEach(b => b.onclick = () => {
            try { window.MXLMonitor.send(b.dataset.shm, b.dataset.shm); } catch (_) {}
        });
    }

    async function optimise() {
        try {
            await fetch("/api/pyramide/reconcile" + (node != null ? "?node_id=" + node : ""),
                { method: "POST" });
            TOAST(T('plugin.pyramide.optimise_started',"Optimisation des proxies lancée"));
        } catch (e) { TOAST(T('plugin.pyramide.optimise_failed',"Échec de l'optimisation")); }
        setTimeout(load, 1500);
    }

    return {
        mount(el, vmid, ctx) {
            EL = el; VMID = vmid; TOAST = (ctx && ctx.toast) || (() => {});
            // Fragment statique (control.html) : traduit AVANT le premier rendu.
            if (window.t) {
                el.querySelectorAll('[data-i18n]').forEach(n => {
                    const v = window.t(n.dataset.i18n);
                    if (v && v !== n.dataset.i18n) n.textContent = v;
                });
                el.querySelectorAll('[data-i18n-title]').forEach(n => {
                    const v = window.t(n.dataset.i18nTitle);
                    if (v && v !== n.dataset.i18nTitle) n.title = v;
                });
            }
            el.querySelector('[data-act="refresh"]').onclick = load;
            el.querySelector('[data-act="optimise"]').onclick = optimise;
            load();
            timer = setInterval(load, 2500);
        },
        unmount() { if (timer) clearInterval(timer); timer = null; EL = null; },
    };
})();
