// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// ENVOI DE FICHIERS DEPUIS LE POSTE vers la base des médias — chargé à la demande par la
// bibliothèque commune (static/js/media_library.js : bouton « Importer depuis l'ordinateur… »,
// fichiers déposés depuis le bureau). Serveur : app/media_upload.py, routes /api/media/upload/*.
//
// Le fichier part par MORCEAUX (File.slice) ; chaque morceau est relayé par l'orchestrateur jusqu'au
// nœud. Un morceau en échec est réessayé (attente croissante) ; un refus « décalage » rend la taille
// RÉELLE reçue par le nœud, d'où l'on repart. Page rechargée ou réseau coupé : re-choisir le même
// fichier (même nom, taille et date) reprend là où le nœud en est. UN fichier à la fois.
//
//   MediaUpload.enqueue(files, {vmid} | {node_id, folder, project_id?}, {bin_id?, label?})
//     (nom déjà pris dans le dossier : fenêtre « Envoyer sous ce nom / Ignorer » AVANT l'envoi)
//   MediaUpload.chooseTarget({folder, project_id}) → Promise<{node_id, folder} | null>   (Gestionnaire)
//   MediaUpload.config() → Promise<{chunk_size, max_bytes, exts, kinds, nodes}>
// Chaque fichier importé émet `ml:uploaded` (detail {asset_id, session}) : les bibliothèques relisent.
(function(){
  'use strict';
  if(window.MediaUpload) return;
  const T = (k, d) => { const v = window.t ? window.t('medialib.' + k) : ''; return (v && v !== 'medialib.' + k) ? v : d; };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const toast = (m, k) => (window.mxlToast ? window.mxlToast(m, k) : console.log(m));
  const pref = {
    get(k, d){ try{ const v = localStorage.getItem('mlu.' + k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } },
    set(k, v){ try{ localStorage.setItem('mlu.' + k, JSON.stringify(v)); }catch(e){} },
  };
  const fmtBytes = b => { b = +b || 0;
    return b >= 1e9 ? (b / 1e9).toFixed(2) + ' ' + T('up_gb', 'Go') : b >= 1e6 ? (b / 1e6).toFixed(1) + ' ' + T('up_mb', 'Mo')
      : Math.max(0, Math.round(b / 1e3)) + ' ' + T('up_kb', 'Ko'); };
  const fmtEta = s => { if(!isFinite(s) || s < 0) return '—'; s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0'); };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000, 30000, 30000, 60000, 60000];   // ≈ 4 min avant d'abandonner

  let _cfg = null;
  async function config(force){
    if(_cfg && !force) return _cfg;
    const r = await fetch('/api/media/upload/config');
    const d = await r.json().catch(() => ({}));
    if(!r.ok) throw new Error(d.error || (r.status === 403 ? T('forbidden', 'Droit « gestion des médias » requis') : 'HTTP ' + r.status));
    return (_cfg = d);
  }

  // ── File d'envoi ──
  const queue = [];
  let running = false, _seq = 0;
  const active = () => queue.some(it => ['queued', 'starting', 'uploading', 'finishing', 'importing', 'retrying'].includes(it.state));

  // Les sélections passent l'une après l'autre : deux dépôts rapprochés ne superposent pas leurs questions.
  let _intake = Promise.resolve();
  function enqueue(files, target, opts){
    const p = _intake.then(() => intake(files, target, opts || {}));
    _intake = p.catch(() => {});
    return p;
  }
  async function intake(files, target, opts){
    let cfg;
    try{ cfg = await config(); }catch(e){ toast(e.message, 'error'); return; }
    const exts = new Set(cfg.exts || []), bad = [], big = [], ok = [];
    for(const f of Array.from(files || [])){
      const ext = (f.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
      if(!exts.has(ext)){ bad.push(f.name); continue; }
      if(f.size > cfg.max_bytes){ big.push(f.name); continue; }
      if(!f.size){ bad.push(f.name); continue; }
      ok.push(f);
    }
    if(bad.length) toast(T('up_bad_type', 'Non accepté (vidéo, audio ou image seulement) : {l}').replace('{l}', bad.slice(0, 5).join(', ')
      + (bad.length > 5 ? '…' : '')), 'warning');
    if(big.length) toast(T('up_too_big', 'Trop gros (maximum {m}) : {l}').replace('{m}', fmtBytes(cfg.max_bytes))
      .replace('{l}', big.slice(0, 5).join(', ')), 'warning');
    if(!ok.length) return;
    const plan = await resolveNames(ok, target, opts);
    for(const p of plan)
      queue.push({id:++_seq, file:p.file, name:p.name, target:Object.assign({}, target), bin_id:opts.bin_id || null, label:opts.label || '',
                  state:'queued', sent:0, size:p.file.size, err:'', samples:[]});
    render(); pump();
  }

  // ── Nom déjà pris dans le dossier de destination : demander AVANT d'envoyer ──
  // Le serveur dit, pour chaque fichier : libre, reprise d'un envoi interrompu (prioritaire, pas de
  // question), ou pris (fichier sur le nœud, envoi en cours, autre fichier de la sélection) avec une
  // proposition libre. Vérification impossible (réseau, agent ancien) : on envoie tel quel — le
  // serveur ne remplace jamais rien et suffixe en dernier recours.
  const meta = f => ({name:f.name, size:f.size, mtime:f.lastModified / 1000});

  // Empreinte du fichier du poste (static/js/blake2b.js, chargé au premier usage) : la MÊME que la
  // base (taille + 4 Mio de tête + 4 Mio de queue) — deux lectures File.slice, jamais le fichier entier.
  let _b2 = null;
  // Empreinte de chaque fichier choisi, transmise à l'ouverture de l'envoi : le serveur réserve
  // l'identité du média AVANT le nom de fichier (qui porte son code) — celle d'un contenu déjà connu.
  const _fpOf = new WeakMap();
  // Nom du média tiré d'un nom de fichier : sans le code du média (`<nom>__<code>.<ext>`, app/media_naming.py).
  // Sans le code du média ni la clé de version (règle de app/media_naming.py).
  const mediaName = n => String(n || '').replace(/__(?:[0-9a-f]{8}|[0-9a-f]{6})(?:(_[va]\d{1,3}|_proxy)|_[a-z][a-z0-9]{0,15})?(\.[^./]{1,8})?$/, '$1$2');
  function blake(){
    if(window.Blake2b) return Promise.resolve(window.Blake2b);
    if(!_b2) _b2 = new Promise((res, rej) => {
      const me = document.querySelector('script[src*="media_upload.js"]'), v = me && (me.src.split('?')[1] || '');
      const sc = document.createElement('script'); sc.src = '/static/js/blake2b.js' + (v ? '?' + v : '');
      sc.onload = () => res(window.Blake2b); sc.onerror = () => { _b2 = null; rej(new Error('blake2b.js')); };
      document.head.appendChild(sc);
    });
    return _b2;
  }
  async function fingerprints(files){
    try{
      const B = await blake(), out = [];
      for(const f of files){ try{ out.push(await B.mediaFingerprint(f)); }catch(e){ out.push(null); } if(out[out.length - 1]) _fpOf.set(f, out[out.length - 1]); }
      return out;
    }catch(e){ return files.map(() => null); }                               // empreinte impossible : pas de question de doublon
  }

  async function resolveNames(files, target, opts){
    opts = opts || {};
    const where = {vmid:target.vmid, node_id:target.node_id, folder:target.folder || ''};
    const fps = await fingerprints(files);
    const r = await api('POST', '/api/media/upload/check', Object.assign({items:files.map((f, i) => Object.assign(meta(f), {fp:fps[i]}))}, where))
      .catch(() => ({ok:false}));
    const rows = r.ok && Array.isArray(r.d.items) && r.d.items.length === files.length ? r.d.items : null;
    if(!rows) return files.map(f => ({file:f, name:f.name}));
    const dir = (r.d.folder ? r.d.folder : T('up_folder_ph', 'racine du volume')) + (r.d.node_name ? ' (' + r.d.node_name + ')' : '');
    const out = [], skipped = [], kept = [];
    let rest = null, dupRest = null;                                         // « Appliquer aux suivants » : 'rename' | 'skip' ; 'skip' | 'send'
    const reservedFor = i => out.map(p => p.name).concat(rows.filter((x, j) => j > i && (x.ok || x.exists))
      .map(x => x.ok ? x.final : x.suggestion)).filter(Boolean);
    for(let i = 0; i < rows.length; i++){
      const row = rows[i], f = files[i];
      // Même contenu déjà dans la base (la reprise d'un envoi interrompu n'arrive jamais ici : le
      // serveur la rend sans `duplicates`).
      const dupOf = x => !x.resume && ((x.duplicates && x.duplicates.length) || x.duplicate_hidden);
      if(dupOf(row)){
        const left = rows.slice(i + 1).filter(dupOf).length;
        const ctxFiling = !!(target.project_id || opts.bin_id);   // un projet ou un bin où le ranger ?
        const ans = dupRest ? {act:dupRest} : await askDuplicate(f, row, left, ctxFiling);
        if(ans.all) dupRest = ans.act;
        if(ans.act === 'skip' && !(row.duplicates || []).length){ kept.push(f.name); continue; }   // hors de ses projets : rien à ranger
        if(ans.act === 'skip'){
          // Rangé comme l'aurait été l'envoi : projet du contexte, bin.
          const a = await api('POST', '/api/media/upload/attach', {asset_id:row.duplicates[0].asset_id, vmid:target.vmid,
            project_id:target.project_id || null, bin_id:opts.bin_id || null}).catch(() => ({ok:false, d:{}}));
          if(a.ok && (a.d.project_id || a.d.bin_id)) document.dispatchEvent(new CustomEvent('ml:uploaded', {detail:{asset_id:row.duplicates[0].asset_id, session:null}}));
          kept.push(f.name);
          continue;
        }
      }
      if(!row.exists){ out.push({file:f, name:row.final || f.name}); continue; }
      const left = rows.slice(i + 1).filter(x => x.exists).length;
      const ans = rest ? {act:rest, name:row.suggestion} : await askName(f, row, dir, left, where, reservedFor(i));
      if(ans.all) rest = ans.act;
      if(ans.act === 'skip') skipped.push(f.name);
      else out.push({file:f, name:ans.name || row.suggestion});
    }
    if(kept.length) toast(T('up_dup_kept', 'Non envoyé (déjà dans la base) : {l}').replace('{l}', kept.slice(0, 5).join(', ') + (kept.length > 5 ? '…' : '')), 'info');
    if(skipped.length) toast(T('up_skipped', 'Ignoré : {l}').replace('{l}', skipped.slice(0, 5).join(', ') + (skipped.length > 5 ? '…' : '')), 'info');
    return out;
  }

  // « Ce média est déjà présent » : ne pas l'envoyer (défaut quand il est déjà dans le dossier de
  // destination du même nœud) ou l'envoyer quand même (défaut ailleurs : autre dossier, autre nœud).
  function askDuplicate(f, row, left, filing){
    return new Promise(res => {
      const dups = row.duplicates || [], first = dups[0], skipFirst = row.duplicate_default === 'skip';
      const where = d => (d.node || '') + ' · ' + d.path;
      const dlg = document.createElement('dialog');
      dlg.className = 'mlu-dlg mlu-dup';
      const bSkip = `<button type="${skipFirst ? 'submit' : 'button'}" class="btn${skipFirst ? ' btn-green' : ''} mlu-dskip">${esc(T('up_dup_skip', 'Ne pas l\'envoyer'))}</button>`;
      const bSend = `<button type="${skipFirst ? 'button' : 'submit'}" class="btn${skipFirst ? '' : ' btn-green'} mlu-dsend">${esc(T('up_dup_send', 'L\'envoyer quand même'))}</button>`;
      dlg.innerHTML = `<form method="dialog" class="mlu-dlg-f">
        <h3>${esc(T('up_dup_title', 'Déjà présent : « {n} »').replace('{n}', f.name))}</h3>
        <p>${esc(first ? T('up_dup_lead', 'Ce média est déjà présent : {w} (même contenu).').replace('{w}', where(first))
          : T('up_dup_hidden', 'Ce contenu existe déjà dans la base (hors de vos projets).'))}</p>
        ${dups.length > 1 ? `<ul class="meta">${dups.slice(1, 5).map(d => `<li>${esc(where(d))}</li>`).join('')}</ul>` : ''}
        ${first ? `<p class="meta">${esc(skipFirst ? T('up_dup_same_folder', 'Il est déjà dans le dossier de destination.')
          : T('up_dup_elsewhere', 'Il n\'est pas dans le dossier de destination : l\'envoyer en ferait une copie de plus, rattachée au même média.'))}
          ${filing ? esc(T('up_dup_attach2', '« Ne pas l\'envoyer » le range quand même dans le projet ou le bin en cours.')) : ''}</p>` : ''}
        ${left ? `<label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" class="mlu-all">
          ${esc(T('up_dup_apply_rest', 'Même choix pour les {n} suivants déjà présents').replace('{n}', left))}</label>` : ''}
        <div class="mlu-dlg-acts">${skipFirst ? bSend + bSkip : bSkip + bSend}</div></form>`;
      document.body.appendChild(dlg);
      const all = () => !!(dlg.querySelector('.mlu-all') || {}).checked;
      const end = v => { dlg.close(); dlg.remove(); res(v); };
      dlg.querySelector('.mlu-dskip').onclick = e => { e.preventDefault(); end({act:'skip', all:all()}); };
      dlg.querySelector('.mlu-dsend').onclick = e => { e.preventDefault(); end({act:'send', all:all()}); };
      dlg.querySelector('form').onsubmit = e => { e.preventDefault(); end({act:skipFirst ? 'skip' : 'send', all:all()}); };
      dlg.addEventListener('cancel', e => { e.preventDefault(); end({act:'skip', all:false}); });
      dlg.showModal();
      dlg.querySelector(skipFirst ? '.mlu-dskip' : '.mlu-dsend').focus();
    });
  }

  function askName(f, row, dir, left, where, reserved){
    return new Promise(res => {
      const ext = (f.name.match(/\.[^.]+$/) || [''])[0];
      const why = row.reason === 'batch' ? T('up_conflict_batch', 'Un autre fichier de cette sélection porte déjà ce nom.')
        : (row.reason === 'file' ? T('up_conflict_file', 'Un fichier porte déjà ce nom dans {d}.')
          : T('up_conflict_upload', 'Un envoi en cours vise déjà ce nom dans {d}.')).replace('{d}', dir);
      const dlg = document.createElement('dialog');
      dlg.className = 'mlu-dlg mlu-conflict';
      dlg.innerHTML = `<form method="dialog" class="mlu-dlg-f" novalidate>
        <h3>${esc(T('up_conflict_title', 'Nom déjà pris : « {n} »').replace('{n}', f.name))}</h3>
        <p class="meta">${esc(why)}</p>
        <label>${esc(T('up_conflict_name', 'Envoyer sous le nom'))} <input class="mlu-newname" type="text" spellcheck="false" autocomplete="off"
          value="${esc(row.suggestion || '')}" aria-describedby="mlu-cf-hint mlu-cf-err"></label>
        <p class="meta" id="mlu-cf-hint">${esc(T('up_conflict_hint', 'L\'extension {e} est conservée ; / \\ : * ? " < > | sont interdits.').replace('{e}', ext))}</p>
        <div class="mlu-err" id="mlu-cf-err" role="alert" hidden></div>
        ${left ? `<label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" class="mlu-all">
          ${esc(T('up_apply_rest', 'Appliquer aux {n} suivants en conflit (nom proposé, ou ignorés)').replace('{n}', left))}</label>` : ''}
        <div class="mlu-dlg-acts"><button type="button" class="btn mlu-skip">${esc(T('up_skip', 'Ignorer ce fichier'))}</button>
          <button type="submit" class="btn btn-green mlu-yes">${esc(T('up_send_as', 'Envoyer sous ce nom'))}</button></div></form>`;
      document.body.appendChild(dlg);
      const inp = dlg.querySelector('.mlu-newname'), errEl = dlg.querySelector('.mlu-err'), yes = dlg.querySelector('.mlu-yes');
      const all = () => !!(dlg.querySelector('.mlu-all') || {}).checked;
      const end = v => { dlg.close(); dlg.remove(); res(v); };
      const fail = m => { errEl.textContent = m; errEl.hidden = false; inp.setAttribute('aria-invalid', 'true'); inp.focus(); };
      dlg.querySelector('.mlu-skip').onclick = () => end({act:'skip', all:all()});
      dlg.addEventListener('cancel', e => { e.preventDefault(); end({act:'skip', all:false}); });
      inp.oninput = () => { errEl.hidden = true; inp.removeAttribute('aria-invalid'); };
      dlg.querySelector('form').onsubmit = async e => {
        e.preventDefault();
        const want = inp.value.trim();
        if(!want){ fail(T('up_name_empty', 'Saisissez un nom.')); return; }
        // Revérifié au serveur : caractères, extension, et toujours libre (les noms déjà promis aux
        // autres fichiers de la sélection comptent).
        yes.disabled = true;
        const r = await api('POST', '/api/media/upload/check', Object.assign({items:[Object.assign(meta(f), {new_name:want})], reserved}, where))
          .catch(() => ({ok:false, d:{}}));
        yes.disabled = false;
        const x = r.ok && r.d.items && r.d.items[0];
        if(!x){ fail(r.d && r.d.error || T('up_net', 'réseau coupé')); return; }
        if(x.error){ fail(x.error); return; }
        if(x.exists){
          fail(T('up_name_taken', '« {n} » est pris aussi — proposition : « {s} »').replace('{n}', x.final).replace('{s}', x.suggestion || ''));
          if(x.suggestion) inp.value = x.suggestion;
          return;
        }
        end({act:'rename', name:x.final, all:all()});
      };
      dlg.showModal();
      inp.focus();
      const dot = inp.value.lastIndexOf('.');
      try{ inp.setSelectionRange(0, dot > 0 ? dot : inp.value.length); }catch(_){}
    });
  }

  async function pump(){
    if(running) return;
    running = true;
    try{
      for(;;){
        const it = queue.find(x => x.state === 'queued');
        if(!it) break;
        await upload(it);
        render();
      }
    }finally{ running = false; }
  }

  async function api(method, url, body){
    const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    return {status:r.status, ok:r.ok && d.ok !== false, d};
  }

  // Un morceau par XHR (avancement DANS le morceau, annulable) → {status, d}.
  //
  // ★ UN MORCEAU NE PEUT PAS RESTER EN SUSPENS EN SILENCE. Recette du 2026-10-20, Safari 26.6 (macOS) :
  // le premier morceau reçoit « 200 OK » (vu dans l'inspecteur), puis la page n'envoie JAMAIS le
  // deuxième, sans une ligne en console — l'avancement reste à 16 Mo indéfiniment. Deux ceintures :
  //  - la fin est prise au premier de `load` / `readystatechange` (readyState 4) / `loadend` ;
  //  - un chien de garde : ni octet envoyé ni réponse depuis STALL_MS → abandon du morceau, qui est
  //    réessayé (état « nouvel essai », raison affichée), et lu en mémoire d'abord (`useBuffer`) :
  //    un Blob adossé au fichier est la seule chose qui distingue ce chemin d'un envoi ordinaire.
  const STALL_MS = 30000;
  // Après un blocage, les morceaux suivants passent par fetch() avec le morceau DÉJÀ LU en mémoire :
  // ni XMLHttpRequest ni Blob adossé au fichier, les deux suspects sous Safari. Pas d'avancement
  // DANS le morceau (fetch n'en donne pas à l'envoi), seulement morceau par morceau.
  async function putChunkFetch(it, offset, body){
    const ac = new AbortController();
    it.xhr = {abort: () => ac.abort()};
    const dog = setTimeout(() => ac.abort('stall'), 4 * STALL_MS);
    try{
      const r = await fetch(`/api/media/upload/${encodeURIComponent(it.sid)}?offset=${offset}`,
        {method:'PUT', headers:{'Content-Type':'application/octet-stream'}, body, signal:ac.signal});
      const d = await r.json().catch(() => ({}));
      return {status:r.status, d};
    }catch(e){
      if(it.state === 'cancelled') return {status:-1, d:{}};
      return {status:0, stalled:true, d:{error:T('up_stalled', 'envoi bloqué (aucun octet depuis {s} s) : nouvel essai').replace('{s}', Math.round(4 * STALL_MS / 1000))}};
    }finally{ clearTimeout(dog); it.xhr = null; }
  }
  function putChunk(it, offset, body){
    if(it.useBuffer && body instanceof ArrayBuffer) return putChunkFetch(it, offset, body);
    return new Promise(res => {
      const x = new XMLHttpRequest();
      let settled = false, stalled = false, last = performance.now();
      const finish = v => { if(settled) return; settled = true; clearInterval(dog); it.xhr = null; res(v); };
      const fromResponse = () => { let d = {}; try{ d = JSON.parse(x.responseText || '{}'); }catch(_){} finish({status:x.status, d}); };
      const dog = setInterval(() => {
        if(performance.now() - last < STALL_MS) return;
        stalled = true;
        try{ x.abort(); }catch(_){}
        finish({status:0, stalled:true, d:{error:T('up_stalled', 'envoi bloqué (aucun octet depuis {s} s) : nouvel essai').replace('{s}', Math.round(STALL_MS / 1000))}});
      }, 2000);
      it.xhr = x;
      x.open('PUT', `/api/media/upload/${encodeURIComponent(it.sid)}?offset=${offset}`);
      x.setRequestHeader('Content-Type', 'application/octet-stream');
      x.upload.onprogress = e => { last = performance.now(); it.inflight = e.loaded; tick(it); };
      x.onprogress = () => { last = performance.now(); };
      x.onreadystatechange = () => { if(x.readyState === 4 && x.status > 0) fromResponse(); };
      x.onload = fromResponse;
      x.onerror = () => finish({status:0, d:{error:T('up_net', 'réseau coupé')}});
      x.onabort = () => { if(!stalled) finish({status:-1, d:{}}); };
      x.onloadend = () => { if(x.status > 0) fromResponse(); else if(!stalled) finish({status:0, d:{error:T('up_net', 'réseau coupé')}}); };
      try{ x.send(body); }
      catch(e){ finish({status:0, d:{error:String(e && e.message || e)}}); }
    });
  }

  function tick(it){
    const now = performance.now(), done = it.sent + (it.inflight || 0);
    it.samples.push([now, done]);
    while(it.samples.length > 2 && now - it.samples[0][0] > 6000) it.samples.shift();
    paintItem(it);
  }

  async function upload(it){
    const f = it.file, tg = it.target;
    it.state = 'starting'; it.err = ''; paintItem(it);
    const st = await api('POST', '/api/media/upload/start', {name:it.name || f.name, source_name:f.name, size:f.size, mtime:f.lastModified / 1000,
      fp:_fpOf.get(f) || null,
      vmid:tg.vmid, node_id:tg.node_id, folder:tg.folder || '', project_id:tg.project_id || null, bin_id:it.bin_id});
    if(!st.ok){ it.state = 'failed'; it.err = st.d.error || 'HTTP ' + st.status; paintItem(it); return; }
    const s = st.d;
    it.sid = s.id; it.dest = s.dest; it.sent = s.received || 0; it.samples = [[performance.now(), it.sent]];
    const chunk = s.chunk_size || (16 << 20);
    if(s.resumed && it.sent > 0 && it.sent < f.size)
      toast(T('up_resumed', 'Reprise de « {n} » à {p} %').replace('{n}', f.name).replace('{p}', Math.floor(100 * it.sent / f.size)), 'info');
    if(s.renamed) toast(T('up_renamed', '« {n} » existe déjà dans ce dossier : envoyé sous « {d} »').replace('{n}', f.name)
      .replace('{d}', mediaName((s.dest || '').split('/').pop())), 'info');
    it.state = 'uploading'; paintItem(it);
    it.useBuffer = !!pref.get('fetch', false);
    let fails = 0;
    while(it.sent < f.size && !s.complete){
      if(it.state === 'cancelled') return;
      const end = Math.min(f.size, it.sent + chunk);
      it.inflight = 0;
      const part = f.slice(it.sent, end);
      let body = part;
      if(it.useBuffer){ try{ body = await part.arrayBuffer(); }catch(e){ body = part; } }
      const r = await putChunk(it, it.sent, body);
      // Retenu POUR CE NAVIGATEUR : sous Safari, chaque envoi perdait sinon 30 s au premier morceau.
      if(r.stalled){ it.useBuffer = true; pref.set('fetch', true); }
      it.inflight = 0;
      if(it.state === 'cancelled') return;
      if(r.status === 200){ it.sent = r.d.received; fails = 0; if(it.state === 'retrying'){ it.state = 'uploading'; } tick(it); continue; }
      // Décalage refusé : le nœud dit ce qu'il a VRAIMENT — on repart de là.
      if(r.status === 409 && r.d.received != null && !r.d.exists){ it.sent = r.d.received; tick(it); if(r.d.busy) await sleep(2000); continue; }
      const fatal = [400, 403, 404, 413, 415, 507].includes(r.status) || (r.status === 409 && r.d.exists);
      if(fatal || fails >= BACKOFF.length){ it.state = 'failed'; it.err = r.d.error || 'HTTP ' + r.status; paintItem(it); return; }
      it.state = 'retrying'; it.err = r.d.error || 'HTTP ' + r.status; paintItem(it);
      await sleep(BACKOFF[fails++]);
    }
    if(it.state === 'cancelled') return;
    it.state = 'finishing'; it.err = ''; paintItem(it);
    let fin = await api('POST', `/api/media/upload/${encodeURIComponent(it.sid)}/finish`);
    if(!fin.ok){ it.state = 'failed'; it.err = fin.d.error || 'HTTP ' + fin.status; paintItem(it); return; }
    it.state = 'importing'; paintItem(it);
    for(;;){
      await sleep(1500);
      const g = await api('GET', `/api/media/upload/${encodeURIComponent(it.sid)}`);
      if(!g.ok){ if(g.status >= 500 || g.status === 0) continue; it.state = 'failed'; it.err = g.d.error || 'HTTP ' + g.status; break; }
      if(g.d.state === 'importing') continue;
      if(g.d.state === 'done'){
        it.state = 'done'; it.asset_id = g.d.asset_id; it.known = g.d.known;
        document.dispatchEvent(new CustomEvent('ml:uploaded', {detail:{asset_id:g.d.asset_id, session:g.d}}));
      } else { it.state = 'failed'; it.err = g.d.error || g.d.state; }
      break;
    }
    paintItem(it);
  }

  async function cancel(it){
    const was = it.state;
    it.state = 'cancelled';
    if(it.xhr) try{ it.xhr.abort(); }catch(_){}
    if(it.sid && was !== 'done') await api('POST', `/api/media/upload/${encodeURIComponent(it.sid)}/cancel`);
    paintItem(it); render();
  }

  // ── Panneau (un seul par page, quel que soit le nombre de bibliothèques) ──
  let panel = null, _orphans = [];
  function ensurePanel(){
    if(panel && panel.isConnected) return panel;
    panel = document.createElement('section');
    panel.className = 'mlu-panel';
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', T('up_panel', 'Envois vers la base des médias'));
    panel.innerHTML = `<header class="mlu-head"><span class="mlu-title"></span><span class="ml-sp"></span>
      <button type="button" class="btn mlu-fold" aria-expanded="true"></button>
      <button type="button" class="btn mlu-close" aria-label="${esc(T('up_close', 'Fermer'))}" title="${esc(T('up_close', 'Fermer'))}">×</button></header>
      <div class="mlu-list" aria-live="polite"></div><div class="mlu-orphans"></div>`;
    document.body.appendChild(panel);
    panel.querySelector('.mlu-fold').onclick = () => { const f = panel.classList.toggle('folded');
      panel.querySelector('.mlu-fold').setAttribute('aria-expanded', !f); render(); };
    panel.querySelector('.mlu-close').onclick = () => {
      if(active()){ toast(T('up_close_busy', 'Envoi en cours : annulez-le d\'abord, ou repliez le panneau'), 'warning'); return; }
      for(let i = queue.length - 1; i >= 0; i--) queue.splice(i, 1);
      _orphans = []; panel.hidden = true; };
    panel.addEventListener('click', async e => {
      const b = e.target.closest('[data-act]'); if(!b) return;
      if(b.dataset.act === 'drop-orphan'){
        await api('POST', `/api/media/upload/${encodeURIComponent(b.dataset.sid)}/cancel`);
        _orphans = _orphans.filter(o => o.id !== b.dataset.sid); render(); return;
      }
      const it = queue.find(x => x.id === +b.dataset.id); if(!it) return;
      if(b.dataset.act === 'cancel') cancel(it);
      else if(b.dataset.act === 'retry'){ it.state = 'queued'; it.err = ''; render(); pump(); }
      else if(b.dataset.act === 'remove'){ queue.splice(queue.indexOf(it), 1); render(); }
    });
    return panel;
  }
  const STATE = () => ({queued:T('up_st_queued', 'en attente'), starting:T('up_st_starting', 'préparation…'), uploading:T('up_st_uploading', 'envoi'),
    retrying:T('up_st_retrying', 'nouvel essai…'), finishing:T('up_st_finishing', 'finalisation…'), importing:T('up_st_importing', 'import dans la base…'),
    done:T('up_st_done', 'importé'), failed:T('up_st_failed', 'en échec'), cancelled:T('up_st_cancelled', 'annulé')});
  function itemHtml(it){
    const S = STATE(), done = it.sent + (it.inflight || 0), pct = it.size ? done / it.size : 0;
    let rate = 0;
    if(it.samples.length > 1){ const a = it.samples[0], b = it.samples[it.samples.length - 1]; rate = (b[1] - a[1]) / Math.max(0.001, (b[0] - a[0]) / 1000); }
    const live = it.state === 'uploading' || it.state === 'retrying';
    const info = live ? `${fmtBytes(done)} / ${fmtBytes(it.size)}${rate > 0 ? ' · ' + fmtBytes(rate) + T('up_per_s', '/s') + ' · '
        + T('up_eta', '{t} restant').replace('{t}', fmtEta((it.size - done) / rate)) : ''}`
      : it.state === 'done' ? (it.known ? T('up_known', 'déjà dans la base : nouvelle copie') : T('up_done_where', 'dans {d}').replace('{d}', mediaName(it.dest || '')))
      : fmtBytes(it.size);
    const btn = ['done', 'failed', 'cancelled'].includes(it.state)
      ? (it.state === 'failed' ? `<button type="button" class="btn" data-act="retry" data-id="${it.id}">${esc(T('up_retry', 'Réessayer'))}</button>` : '')
        + `<button type="button" class="btn mlu-x" data-act="remove" data-id="${it.id}" aria-label="${esc(T('up_remove', 'Retirer de la liste'))}" title="${esc(T('up_remove', 'Retirer de la liste'))}">×</button>`
      : `<button type="button" class="btn" data-act="cancel" data-id="${it.id}">${esc(T('up_cancel', 'Annuler'))}</button>`;
    return `<div class="mlu-item is-${it.state}" data-item="${it.id}">
      <div class="mlu-row"><span class="mlu-name" title="${esc(it.file.name)}">${esc(it.file.name)}</span>
        <span class="mlu-state">${esc(S[it.state] || it.state)}</span>${btn}</div>
      <progress class="mlu-bar" max="1" value="${it.state === 'done' ? 1 : pct.toFixed(4)}"></progress>
      <div class="mlu-info">${esc(info)}${it.name && it.name !== it.file.name ? ' · ' + esc(T('up_as', 'sous « {n} »').replace('{n}', it.name)) : ''}${it.label ? ' · ' + esc(it.label) : ''}</div>
      ${it.err ? `<div class="mlu-err">${esc(it.err)}</div>` : ''}</div>`;
  }
  function paintItem(it){
    if(!panel || !panel.isConnected) return render();
    const el = panel.querySelector(`[data-item="${it.id}"]`);
    if(!el) return render();
    el.outerHTML = itemHtml(it);
    paintHead();
  }
  function paintHead(){
    const n = queue.filter(x => !['done', 'failed', 'cancelled'].includes(x.state)).length;
    panel.querySelector('.mlu-title').textContent = n ? T('up_title_n', 'Envois : {n} en cours').replace('{n}', n) : T('up_title', 'Envois');
    const folded = panel.classList.contains('folded');
    panel.querySelector('.mlu-fold').textContent = folded ? T('up_unfold', 'Déplier') : T('up_fold', 'Replier');
  }
  function render(){
    if(!queue.length && !_orphans.length){ if(panel) panel.hidden = true; return; }
    ensurePanel(); panel.hidden = false;
    panel.querySelector('.mlu-list').innerHTML = queue.map(itemHtml).join('');
    // Envois interrompus (page rechargée, poste éteint) : la session attend au serveur.
    const live = new Set(queue.map(x => x.sid).filter(Boolean));
    const orph = _orphans.filter(o => !live.has(o.id));
    panel.querySelector('.mlu-orphans').innerHTML = orph.length ? `<p class="mlu-orph-h">${esc(T('up_orphans', 'Envois interrompus — re-choisissez le même fichier pour reprendre :'))}</p>`
      + orph.map(o => `<div class="mlu-row mlu-orph"><span class="mlu-name" title="${esc(o.dest)}">${esc(o.name)}</span>
          <span class="mlu-state">${Math.floor(100 * (o.received || 0) / (o.size || 1))} %</span>
          <button type="button" class="btn" data-act="drop-orphan" data-sid="${esc(o.id)}">${esc(T('up_drop', 'Abandonner'))}</button></div>`).join('') : '';
    paintHead();
  }
  async function loadOrphans(){
    try{
      const r = await fetch('/api/media/upload/sessions'); if(!r.ok) return;
      _orphans = ((await r.json()).sessions || []).filter(s => s.state === 'uploading' || s.state === 'finishing');
      render();
    }catch(e){}
  }

  window.addEventListener('beforeunload', e => { if(active()){ e.preventDefault(); e.returnValue = ''; } });

  // ── Destination au Gestionnaire (vue globale) : nœud + dossier ──
  async function chooseTarget(defaults){
    defaults = defaults || {};
    let cfg;
    try{ cfg = await config(); }catch(e){ toast(e.message, 'error'); return null; }
    const nodes = cfg.nodes || [];
    if(!nodes.length){ toast(T('up_no_node', 'Aucun nœud avec un volume média'), 'error'); return null; }
    const last = pref.get('target', {});
    return new Promise(res => {
      const dlg = document.createElement('dialog');
      dlg.className = 'mlu-dlg';
      const pick = nodes.find(n => n.capable && n.id === last.node_id) || nodes.find(n => n.capable) || nodes[0];
      dlg.innerHTML = `<form method="dialog" class="mlu-dlg-f">
        <h3>${esc(T('up_dest_title', 'Où ranger ces fichiers ?'))}</h3>
        <p class="meta">${esc(defaults.count > 1 ? T('up_dest_lead_n', '{n} fichiers, envoyés un par un puis importés dans la base.').replace('{n}', defaults.count)
          : T('up_dest_lead', 'Le fichier est envoyé sur le volume média du nœud choisi, puis importé dans la base.'))}</p>
        <label>${esc(T('up_node', 'Nœud'))} <select class="mlu-node">${nodes.map(n => `<option value="${n.id}"${n.id === pick.id ? ' selected' : ''}${n.capable ? '' : ' disabled'}>${esc(n.name)}${n.capable ? ''
          : ' — ' + esc(T('up_node_old', 'agent à mettre à jour ({v})').replace('{v}', n.agent_version || '?'))}</option>`).join('')}</select></label>
        <label>${esc(T('up_folder', 'Dossier (sous le volume)'))} <input class="mlu-folder" type="text" placeholder="${esc(T('up_folder_ph', 'racine du volume'))}"
          value="${esc(defaults.folder != null ? defaults.folder : (last.folder || ''))}"></label>
        <div class="mlu-dlg-acts"><button type="button" class="btn mlu-no">${esc(T('up_cancel', 'Annuler'))}</button>
          <button type="submit" class="btn btn-green mlu-yes">${esc(T('up_send', 'Envoyer'))}</button></div></form>`;
      document.body.appendChild(dlg);
      const end = v => { dlg.close(); dlg.remove(); res(v); };
      dlg.querySelector('.mlu-no').onclick = () => end(null);
      dlg.addEventListener('cancel', e => { e.preventDefault(); end(null); });
      dlg.querySelector('form').onsubmit = e => {
        e.preventDefault();
        const t = {node_id:+dlg.querySelector('.mlu-node').value, folder:dlg.querySelector('.mlu-folder').value.trim().replace(/^\/+|\/+$/g, '')};
        pref.set('target', t); end(t);
      };
      dlg.showModal();
      dlg.querySelector('.mlu-folder').focus();
    });
  }

  window.MediaUpload = {enqueue, chooseTarget, config, open(){ loadOrphans(); }, active};
  loadOrphans();
})();
