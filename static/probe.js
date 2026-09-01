// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// probe_2110 — page « Sonde 2110 » (analyseur ponctuel NMOS, Phase A).
// Sélecteur de flux IS-04 → abonnement IS-05 → rapport de conformité 2110-21 live.
(function () {
  "use strict";
  // Repli i18n local pour le préfixe `probemon.*` (Phase B) : le catalogue js n'exporte pas encore
  // ce préfixe (cf. i18n.js_catalog) — clés à consolider dans i18n/{fr,en}.json + i18n.py. On tente
  // window.t d'abord ; si elle rend la clé telle quelle (= absente), on retombe sur PMSTR.
  var PMSTR = {
    "probemon.monitoring_title": "Surveillance longue durée",
    "probemon.active_only": "Incidents actifs seulement",
    "probemon.sev_all": "Toutes sévérités", "probemon.sev_error": "Erreurs",
    "probemon.sev_warning": "Avertissements", "probemon.sev_info": "Info",
    "probemon.kind_all": "Tous types", "probemon.watched_title": "Signaux surveillés :",
    "probemon.watch_add": "Surveiller", "probemon.loading": "Chargement…",
    "probemon.no_events": "Aucun incident journalisé.", "probemon.open": "en cours",
    "probemon.closed": "résolu", "probemon.unwatch": "retirer",
    "probemon.watch_none": "aucun", "probemon.watch_prompt_vmid": "vmid du receiver requis",
    "probemon.watch_added": "Signal mis sous surveillance", "probemon.watch_removed": "Surveillance retirée",
    "probemon.kind.conformance": "conformité", "probemon.kind.freeze": "gel",
    "probemon.kind.black": "noir", "probemon.kind.silence": "silence",
    "probemon.kind.no_signal": "pas de signal", "probemon.kind.rx_error": "erreur RX",
    "probemon.kind.loss": "pertes", "probemon.kind.ptp": "PTP", "probemon.kind.unreachable": "injoignable"
  };
  var t = function (k) {
    var v = (window.t ? window.t(k) : k);
    return (v && v !== k) ? v : (PMSTR[k] || k);
  };
  var $ = function (id) { return document.getElementById(id); };
  var senders = [];      // flux NMOS abonnables (partagés par toutes les sondes)
  var nodes = [];        // nœuds + PF candidates

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function toast(msg, kind) {
    var d = document.createElement("div");
    d.textContent = msg;
    d.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:9999;" +
      "padding:10px 16px;border-radius:8px;color:#fff;font:inherit;" +
      "background:" + (kind === "error" ? "#c0392b" : "#2e9e5b");
    document.body.appendChild(d);
    setTimeout(function () { d.remove(); }, 3200);
  }
  function api(url, opts) {
    return fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, opts))
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, code: r.status, j: j }; }); });
  }

  // ── Verdict de conformité → feu ─────────────────────────────────────────────
  function verdictLight(rec) {
    if (!rec || rec.fps == null || Number(rec.fps) <= 0)
      return { cls: "nosignal", label: t("probe.verdict_nosignal") };
    var c = String(rec.compliant || "").toUpperCase();
    if (c.indexOf("NARROW") >= 0) return { cls: "narrow", label: t("probe.verdict_narrow") };
    if (c.indexOf("WIDE") >= 0) return { cls: "wide", label: t("probe.verdict_wide") };
    if (c.indexOf("FAIL") >= 0) return { cls: "failed", label: t("probe.verdict_failed") };
    // Sans champ compliant (parser off / af_xdp) → pas de verdict absolu.
    return { cls: "nosignal", label: "—" };
  }
  function gauge(key, val, unit) {
    var v = (val == null || val === "") ? "—" : val;
    return '<div class="gauge"><div class="k">' + esc(key) + '</div>' +
      '<div class="v">' + esc(v) + (unit ? ' <span class="u">' + esc(unit) + "</span>" : "") + "</div></div>";
  }

  function reportHtml(rep) {
    if (!rep || !rep.ok) return '<div class="light nosignal">' + t("probe.verdict_nosignal") + "</div>";
    var recs = (rep.receivers || []).filter(function (r) { return r.essence == null || r.essence === "video"; });
    var rec = recs[0] || null;
    var lt = verdictLight(rec);
    var h = '<span class="light ' + lt.cls + '">' + esc(lt.label) + "</span>";
    if (rec) {
      h += '<div class="gauges">';
      h += gauge(t("probe.metric_cinst"), rec.cinst_max);
      h += gauge(t("probe.metric_vrx"), rec.vrx_max);
      h += gauge(t("probe.metric_vrx_span"), rec.vrx_span);
      h += gauge(t("probe.metric_fpt"), rec.fpt, "µs");
      h += gauge(t("probe.metric_latency"), rec.latency, "µs");
      h += gauge(t("probe.metric_fps"), rec.fps);
      h += "</div>";
      if (rec.failed_cause && String(rec.failed_cause).trim())
        h += '<div class="cause">' + t("probe.cause") + " : " + esc(rec.failed_cause) + "</div>";
      // Transport : compteurs libmtl éventuellement présents (port_user_stats) — affichage tolérant.
      var tr = rec.transport || rep.transport || {};
      var keys = Object.keys(tr);
      if (keys.length) {
        h += '<div class="transport"><b>' + t("probe.transport_title") + "</b>";
        keys.forEach(function (k) {
          h += '<div class="t-row"><span>' + esc(k) + "</span><span>" + esc(tr[k]) + "</span></div>";
        });
        h += "</div>";
      }
    }
    return h;
  }

  // ── Cartes des sondes ────────────────────────────────────────────────────────
  function senderOptions(selId) {
    if (!senders.length) return '<option value="">' + t("probe.sender_none") + "</option>";
    var o = '<option value="">' + t("probe.sender_select") + "</option>";
    senders.forEach(function (s) {
      var lbl = (s.label || s.id) + (s.multicast_ip ? " · " + s.multicast_ip + ":" + (s.port || "") : "") +
        (s.origin === "registry" ? " · IS-04" : "");
      o += '<option value="' + esc(s.id) + '">' + esc(lbl) + "</option>";
    });
    return o;
  }

  function cardHtml(p) {
    var h = '<div class="probe-card" data-vmid="' + p.vmid + '">';
    h += '<div class="probe-card-head"><span class="hn">' + esc(p.hostname) + "</span>" +
      '<span class="node">' + esc(p.node) + " · " + esc(p.probe_iface || "—") + "</span></div>";
    h += '<div class="probe-card-body">';
    h += '<div class="report">' + reportHtml(p.report) + "</div>";
    // Sélecteur de flux + SDP collé.
    h += '<div class="probe-field" style="margin-top:12px">' +
      '<label>' + t("probe.sender_title") + "</label>" +
      '<select class="sel-sender">' + senderOptions() + "</select></div>";
    h += '<div class="card-actions">' +
      '<button class="btn-probe act-sub">' + t("probe.sender_subscribe") + "</button>" +
      '<button class="btn-ghost act-unsub">' + t("probe.unsubscribe") + "</button></div>";
    h += '<div class="probe-field" style="margin-top:10px">' +
      '<label>' + t("probe.sender_paste_sdp") + "</label>" +
      '<textarea class="sdp-paste" placeholder="v=0..."></textarea>' +
      '<div style="margin-top:6px"><button class="btn-ghost act-sub-sdp">' + t("probe.sender_paste_btn") + "</button></div></div>";
    h += "</div></div>";
    return h;
  }

  function bindCard(el) {
    var vmid = el.getAttribute("data-vmid");
    el.querySelector(".act-sub").addEventListener("click", function () {
      var sid = el.querySelector(".sel-sender").value;
      if (!sid) { toast(t("probe.need_flux"), "error"); return; }
      api("/api/probe/" + vmid + "/subscribe", { method: "POST", body: JSON.stringify({ sender_id: sid }) })
        .then(function (r) { toast(r.ok ? t("probe.sub_ok") : (r.j.error || t("probe.sub_fail")), r.ok ? "ok" : "error"); refresh(); });
    });
    el.querySelector(".act-sub-sdp").addEventListener("click", function () {
      var sdp = el.querySelector(".sdp-paste").value.trim();
      if (!sdp) { toast(t("probe.need_flux"), "error"); return; }
      api("/api/probe/" + vmid + "/subscribe", { method: "POST", body: JSON.stringify({ sdp: sdp }) })
        .then(function (r) { toast(r.ok ? t("probe.sub_ok") : (r.j.error || t("probe.sub_fail")), r.ok ? "ok" : "error"); refresh(); });
    });
    el.querySelector(".act-unsub").addEventListener("click", function () {
      if (!confirm(t("probe.confirm_unsub"))) return;
      api("/api/probe/" + vmid + "/unsubscribe", { method: "POST", body: JSON.stringify({ slot: 0 }) })
        .then(function () { refresh(); });
    });
  }

  // ── Déploiement ──────────────────────────────────────────────────────────────
  function fillNodeSelect() {
    var sel = $("probe-node");
    var none = $("probe-deploy-none");
    if (!nodes.length) {
      sel.innerHTML = ""; none.style.display = "block";
      $("probe-deploy-btn").disabled = true; fillIfaceSelect(); return;
    }
    none.style.display = "none"; $("probe-deploy-btn").disabled = false;
    sel.innerHTML = nodes.map(function (n) {
      return '<option value="' + n.node_id + '">' + esc(n.name) + "</option>";
    }).join("");
    fillIfaceSelect();
  }
  function fillIfaceSelect() {
    var nid = $("probe-node").value;
    var node = nodes.filter(function (n) { return String(n.node_id) === String(nid); })[0];
    var sel = $("probe-iface");
    if (!node) { sel.innerHTML = ""; return; }
    sel.innerHTML = node.ifaces.map(function (i) {
      var tag = i.busy ? (" — " + t("probe.iface_busy"))
        : (i.conformance_ready ? " — " + t("probe.iface_dpdk") : " — " + t("probe.iface_afxdp"));
      return '<option value="' + esc(i.ifname) + '"' + (i.busy ? " disabled" : "") + '>' +
        esc(i.ifname) + (i.pci ? " (" + esc(i.pci) + ")" : "") + esc(tag) + "</option>";
    }).join("");
  }

  function deploy() {
    var nid = $("probe-node").value, iface = $("probe-iface").value;
    if (!nid || !iface) { toast(t("probe.need_iface"), "error"); return; }
    api("/api/probe/deploy", {
      method: "POST",
      body: JSON.stringify({ node_id: Number(nid), probe_iface: iface, measure_audio: $("probe-audio").checked })
    }).then(function (r) {
      toast(r.ok ? t("probe.deploy_ok") : (r.j.error || t("probe.deploy_fail")), r.ok ? "ok" : "error");
      setTimeout(refresh, 1500);
    });
  }

  // ── Rafraîchissement ─────────────────────────────────────────────────────────
  function renderCards(probes) {
    var wrap = $("probe-cards");
    if (!probes.length) { wrap.innerHTML = '<div class="muted">' + t("probe.no_probes") + "</div>"; return; }
    // Conserver la sélection de flux par carte au refresh.
    var kept = {};
    wrap.querySelectorAll(".probe-card").forEach(function (el) {
      kept[el.getAttribute("data-vmid")] = el.querySelector(".sel-sender") && el.querySelector(".sel-sender").value;
    });
    wrap.innerHTML = probes.map(cardHtml).join("");
    wrap.querySelectorAll(".probe-card").forEach(function (el) {
      var v = kept[el.getAttribute("data-vmid")];
      if (v) { var s = el.querySelector(".sel-sender"); if (s) s.value = v; }
      bindCard(el);
    });
  }

  function refresh() {
    Promise.all([api("/api/probe/engines", {}), api("/api/probe/senders", {})])
      .then(function (res) {
        var eng = res[0].j || {}, snd = res[1].j || {};
        senders = snd.senders || [];
        nodes = eng.nodes || [];
        fillNodeSelect();
        renderCards(eng.probes || []);
      })
      .catch(function () { });
  }

  // ── Phase B : monitoring longue durée + timeline d'incidents ────────────────
  var KINDS = ["conformance", "freeze", "black", "silence", "no_signal", "rx_error", "loss", "ptp", "unreachable"];

  function fmtTs(s) {
    if (!s) return "—";
    // ISO "2026-07-07T18:20:31.123" → "07-07 18:20:31"
    var m = String(s).replace("T", " ").split(".")[0];
    return m.length > 6 ? m.slice(5) : m;
  }
  function kindLabel(k) { return t("probemon.kind." + k) || k; }

  function watchedChips(list) {
    var box = $("pm-watched");
    if (!box) return;
    if (!list || !list.length) { box.innerHTML = '<span class="muted">' + t("probemon.watch_none") + "</span>"; return; }
    box.innerHTML = list.map(function (w) {
      var lbl = "#" + w.vmid + "/" + (w.idx || 0) + "/" + (w.essence || "video") + (w.label ? " " + w.label : "");
      return '<span class="pm-chip" data-vmid="' + esc(w.vmid) + '" data-idx="' + esc(w.idx || 0) +
        '" data-ess="' + esc(w.essence || "video") + '">' + esc(lbl) +
        ' <button class="pm-unwatch" title="' + t("probemon.unwatch") + '">✕</button></span>';
    }).join("");
    box.querySelectorAll(".pm-unwatch").forEach(function (b) {
      b.addEventListener("click", function () {
        var c = b.parentElement;
        api("/api/probe/watch", { method: "POST", body: JSON.stringify({
          vmid: Number(c.getAttribute("data-vmid")), idx: Number(c.getAttribute("data-idx")),
          essence: c.getAttribute("data-ess"), on: false }) })
          .then(function () { toast(t("probemon.watch_removed"), "ok"); refreshMonitor(); });
      });
    });
  }

  function fillKindFilter() {
    var sel = $("pm-kind");
    if (!sel || sel.dataset.filled) return;
    KINDS.forEach(function (k) {
      var o = document.createElement("option"); o.value = k; o.textContent = kindLabel(k); sel.appendChild(o);
    });
    sel.dataset.filled = "1";
  }

  function evHtml(e) {
    var sev = e.severity || "info";
    var open = !e.ts_end;
    return '<div class="pm-ev ' + esc(sev) + '">' +
      '<span class="pm-ts">' + esc(fmtTs(e.ts_start)) + (e.ts_end ? " → " + esc(fmtTs(e.ts_end)) : "") + "</span>" +
      '<span class="pm-flow">' + esc(e.flow || "") + "</span>" +
      '<span class="pm-msg">' + esc(e.message || "") +
      ' <span class="pm-kind">[' + esc(kindLabel(e.kind)) + "]</span></span>" +
      '<span class="pm-badge ' + (open ? "open" : "closed") + '">' +
      (open ? t("probemon.open") : t("probemon.closed")) + "</span></div>";
  }

  function renderTimeline(events) {
    var wrap = $("pm-timeline");
    if (!wrap) return;
    if (!events || !events.length) { wrap.innerHTML = '<div class="muted">' + t("probemon.no_events") + "</div>"; return; }
    wrap.innerHTML = events.map(evHtml).join("");
  }

  function refreshMonitor() {
    fillKindFilter();
    var q = [];
    if ($("pm-active-only") && $("pm-active-only").checked) q.push("active=1");
    if ($("pm-sev") && $("pm-sev").value) q.push("severity=" + encodeURIComponent($("pm-sev").value));
    if ($("pm-kind") && $("pm-kind").value) q.push("kind=" + encodeURIComponent($("pm-kind").value));
    Promise.all([
      api("/api/probe/events?" + q.join("&"), {}),
      api("/api/probe/monitor", {})
    ]).then(function (res) {
      renderTimeline((res[0].j || {}).events || []);
      watchedChips((res[1].j || {}).watched || []);
    }).catch(function () { });
  }

  function bindMonitorControls() {
    ["pm-active-only", "pm-sev", "pm-kind"].forEach(function (id) {
      var el = $(id); if (el) el.addEventListener("change", refreshMonitor);
    });
    var add = $("pm-watch-add");
    if (add) add.addEventListener("click", function () {
      var vmid = ($("pm-watch-vmid").value || "").trim();
      if (!vmid) { toast(t("probemon.watch_prompt_vmid"), "error"); return; }
      api("/api/probe/watch", { method: "POST", body: JSON.stringify({
        vmid: Number(vmid), idx: Number(($("pm-watch-idx").value || "0").trim()) || 0, on: true }) })
        .then(function (r) {
          toast(r.ok ? t("probemon.watch_added") : (r.j.error || "err"), r.ok ? "ok" : "error");
          $("pm-watch-vmid").value = ""; $("pm-watch-idx").value = ""; refreshMonitor();
        });
    });
  }

  // Applique le repli i18n local aux éléments statiques marqués data-t.
  function applyStaticI18n() {
    document.querySelectorAll("[data-t]").forEach(function (el) {
      var k = el.getAttribute("data-t"), v = t(k);
      if (v && v !== k) el.textContent = v;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("probe-deploy-btn").addEventListener("click", deploy);
    $("probe-node").addEventListener("change", fillIfaceSelect);
    applyStaticI18n();
    bindMonitorControls();
    // Poll sans recouvrement (cf. layout.html) — MXLPoll lance lui-même la 1re passe.
    window.MXLPoll(refresh, 3000);
    window.MXLPoll(refreshMonitor, 5000);
  });
})();
