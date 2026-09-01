// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Visualiseur de JOURNAL de conteneur (composant global, ouvrable de n'importe où).
//
// Pourquoi un fichier à part plutôt qu'un ajout à scripts.js : ce dernier n'est chargé QUE par les
// pages Containers et Projets. Un helper global qu'on y met est ABSENT des autres pages, en
// silence. Celui-ci est chargé par layout.html, donc disponible partout — la page Containers
// aujourd'hui, l'onglet Monitoring demain.
//
// S'adosse à GET /api/containers/<vmid>/logs, qui interroge `journalctl` sur le nœud (et NON
// `docker logs`) : le journal appartient à l'hôte, donc on peut lire un conteneur DÉTRUIT.
//
// Deux partis pris assumés :
//  - PAS de rafraîchissement automatique. Chaque appel interroge le nœud ; un poll à 5 s sur un
//    onglet laissé ouvert, c'est une commande journalctl toutes les 5 s par onglet et par
//    utilisateur. L'actualisation est manuelle.
//  - La RÉTENTION est affichée. Le journal est plafonné (SystemMaxUse) : les entrées anciennes
//    sont purgées. Sans l'indiquer, on conclut « il n'y a rien eu » là où il n'y a plus rien.
(function () {
  'use strict';
  const T = (k, d) => (window.t ? window.t(k) : null) || d;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let box = null, etat = { vmid: null, nom: '', since: '', until: '' };

  function construire() {
    if (box) return box;
    box = document.createElement('div');
    box.className = 'bobi-logs-overlay';
    box.hidden = true;
    box.innerHTML = `
      <div class="bobi-logs-panel" role="dialog" aria-modal="true">
        <header class="bobi-logs-head">
          <h2 id="bl-title">${esc(T('js.logs.title', 'Journal'))}</h2>
          <span class="bobi-logs-src meta" id="bl-src"></span>
          <button class="btn" id="bl-close" aria-label="${esc(T('js.logs.close', 'Fermer'))}">✕</button>
        </header>
        <div class="bobi-logs-bar">
          <label>${esc(T('js.logs.lines', 'Lignes'))}
            <select id="bl-lines"><option>200</option><option>500</option><option>1000</option><option>2000</option></select>
          </label>
          <label>${esc(T('js.logs.priority', 'Niveau'))}
            <select id="bl-prio">
              <option value="">${esc(T('js.logs.prio_all', 'Tout'))}</option>
              <option value="err">${esc(T('js.logs.prio_err', 'Erreurs (stderr)'))}</option>
              <option value="warning">${esc(T('js.logs.prio_warn', 'Avertissements et plus'))}</option>
            </select>
          </label>
          <label>${esc(T('js.logs.since', 'Depuis'))}
            <input type="text" id="bl-since" placeholder="-1h, -30min, 2026-07-26 03:00" size="22">
          </label>
          <label>${esc(T('js.logs.grep', 'Contient'))}
            <input type="search" id="bl-grep" placeholder="${esc(T('js.logs.grep_ph', 'motif…'))}" size="16">
          </label>
          <button class="btn btn-blue" id="bl-refresh">${esc(T('js.logs.refresh', 'Actualiser'))}</button>
        </div>
        <pre class="bobi-logs-body" id="bl-body"></pre>
        <footer class="bobi-logs-foot meta" id="bl-foot"></footer>
      </div>`;
    document.body.appendChild(box);
    box.addEventListener('click', e => { if (e.target === box) fermer(); });
    box.querySelector('#bl-close').onclick = fermer;
    box.querySelector('#bl-refresh').onclick = charger;
    box.querySelector('#bl-grep').addEventListener('keydown', e => { if (e.key === 'Enter') charger(); });
    box.querySelector('#bl-since').addEventListener('keydown', e => { if (e.key === 'Enter') charger(); });
    document.addEventListener('keydown', e => { if (!box.hidden && e.key === 'Escape') fermer(); });
    return box;
  }

  function fermer() { if (box) box.hidden = true; }

  async function charger() {
    const body = box.querySelector('#bl-body');
    const foot = box.querySelector('#bl-foot');
    const src  = box.querySelector('#bl-src');
    body.textContent = T('js.logs.loading', 'Lecture du journal sur le nœud…');
    foot.textContent = ''; src.textContent = '';
    const p = new URLSearchParams();
    p.set('lines', box.querySelector('#bl-lines').value || '200');
    const prio = box.querySelector('#bl-prio').value;   if (prio) p.set('priority', prio);
    const grep = box.querySelector('#bl-grep').value.trim(); if (grep) p.set('grep', grep);
    const since = box.querySelector('#bl-since').value.trim(); if (since) p.set('since', since);
    if (etat.until) p.set('until', etat.until);
    // Conteneur oublié de la base : le vmid ne suffit plus à retrouver son nom Docker ni son nœud.
    // On passe alors les deux, seuls identifiants que le journal de l'hôte connaisse encore.
    if (etat.name) p.set('name', etat.name);
    if (etat.node_id != null && etat.node_id !== '') p.set('node_id', etat.node_id);
    try {
      const r = await fetch(`/api/containers/${etat.vmid}/logs?` + p.toString());
      const j = await r.json();
      if (!r.ok || j.ok === false) {
        body.textContent = '✕ ' + (j.error || r.status);
        return;
      }
      const lignes = j.lines || [];
      body.textContent = lignes.length ? lignes.join('\n')
        : T('js.logs.empty', 'Aucune ligne pour ces critères. Le journal est plafonné : une période ancienne peut avoir été purgée.');
      body.scrollTop = body.scrollHeight;
      // `source` dit d'où vient la réponse : journald (durable, lisible même conteneur détruit) ou
      // docker (repli pour un conteneur créé AVANT la bascule du pilote — il disparaîtra avec lui).
      src.textContent = j.source === 'journald'
        ? T('js.logs.src_journald', 'journald — conservé même après destruction du conteneur')
        : T('js.logs.src_docker', 'docker logs — conteneur non migré, ces traces mourront avec lui');
      src.className = 'bobi-logs-src meta' + (j.source === 'journald' ? '' : ' warn');
      const ret = j.retention || {};
      foot.textContent = [
        `${lignes.length} ${T('js.logs.shown', 'ligne(s)')}`,
        j.truncated ? T('js.logs.truncated', '— tronqué au plafond') : '',
        ret.oldest_entry ? `· ${T('js.logs.oldest', 'journal disponible depuis')} ${ret.oldest_entry}` : '',
        ret.disk_usage ? `· ${ret.disk_usage}` : '',
        j.note ? `· ${j.note}` : '',
      ].filter(Boolean).join(' ');
    } catch (e) {
      body.textContent = '✕ ' + e;
    }
  }

  // open(vmid, {nom, since, until, titre}) — `since`/`until` servent au point d'entrée « depuis une
  // alerte » : on ouvre le journal cadré sur la fenêtre de l'incident plutôt qu'à la fin du fichier.
  function open(vmid, opts) {
    opts = opts || {};
    construire();
    etat = { vmid, nom: opts.nom || '', since: opts.since || '', until: opts.until || '',
             name: opts.name || '', node_id: (opts.node_id != null ? opts.node_id : '') };
    box.querySelector('#bl-title').textContent =
      opts.titre || `${T('js.logs.title', 'Journal')} — ${etat.nom || '#' + vmid}`;
    box.querySelector('#bl-since').value = etat.since || '';
    box.querySelector('#bl-grep').value = '';
    box.hidden = false;
    charger();
  }

  window.BobiLogs = { open, close: fermer };
})();
