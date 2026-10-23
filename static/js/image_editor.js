// SPDX-License-Identifier: GPL-3.0-or-later
// Éditeur d'image de la base des médias — cf. app/media_edit.py (LA RECETTE y est définie : cadre,
// zoom, rotation, fond, zones, texte, calques ; ce fichier n'en est que l'aperçu en direct et
// l'interface. Le rendu pleine résolution est fait côté serveur, avec la MÊME géométrie).
//
// API globale :
//   ImageEditor.open(assetId, opts)
//     assetId : id d'un média IMAGE de la base. Si c'est déjà une édition, l'éditeur rouvre
//               l'ORIGINALE avec la recette de l'édition (calques modifiables).
//     opts.format  {w, h, label?}   format de sortie proposé (ex. celui du conteneur « Images
//                                   fixes ») ; défaut 1920×1080. Ignoré si l'on rouvre une édition.
//     opts.onSaved(result)          rappel après un enregistrement réussi ; result =
//                                   {id, path, node_id, overwritten, w, h, version?} (version = clé quand la retouche est rangée
//                                   comme version de l'original, réglage media_edit_output). L'événement
//                                   `imageeditor:saved` (detail = result) est aussi émis sur window.
//     opts.onClose()                rappel à la fermeture.
//   ImageEditor.close()
// Permission : lecture = connecté ; enregistrer = `media.manage`.
(function(){
  'use strict';

  // ─── Géométrie (pur, testé sous node contre app/media_edit.py) ──────────────────────────────
  const rad = d => d * Math.PI / 180;
  // Contour d'un rectangle lw×lh centré (cx, cy), tourné de theta (horaire), coins arrondis r.
  function outline(cx, cy, theta, lw, lh, r, n){
    n = n || 14;
    r = Math.max(0, Math.min(r, lw / 2, lh / 2));
    const hw = lw / 2, hh = lh / 2; let pts = [];
    if(r <= 0.01) pts = [[-hw,-hh],[hw,-hh],[hw,hh],[-hw,hh]];
    else for(const [ox, oy, a0] of [[hw-r,-hh+r,-90],[hw-r,hh-r,0],[-hw+r,hh-r,90],[-hw+r,-hh+r,180]])
      for(let i = 0; i <= n; i++){ const a = rad(a0 + 90 * i / n); pts.push([ox + r * Math.cos(a), oy + r * Math.sin(a)]); }
    const c = Math.cos(rad(theta)), s = Math.sin(rad(theta));
    return pts.map(([x, y]) => [cx + x * c - y * s, cy + x * s + y * c]);
  }
  // Vue : la source est (pw × ph) ; (Wc × Hc) = taille du cadre dans l'espace où l'on peint.
  function geom(view, pw, ph, Wc, Hc){
    const z = view.k * Wc / pw, th = rad(view.rot), c = Math.cos(th), s = Math.sin(th);
    const cx = view.x * pw, cy = view.y * pw;
    const g = {z, c, s, cx, cy, W: Wc, H: Hc, pw, ph, fh: !!view.flipH, fv: !!view.flipV};
    g.fwd = (sx, sy) => {                              // source (non retournée) → cadre
      const fx = g.fh ? pw - sx : sx, fy = g.fv ? ph - sy : sy, dx = fx - cx, dy = fy - cy;
      return [Wc / 2 + z * (c * dx - s * dy), Hc / 2 + z * (s * dx + c * dy)];
    };
    g.inv = (px, py) => {                              // cadre → source (non retournée)
      const vx = (px - Wc / 2) / z, vy = (py - Hc / 2) / z;
      const fx = cx + c * vx + s * vy, fy = cy - s * vx + c * vy;
      return [g.fh ? pw - fx : fx, g.fv ? ph - fy : fy];
    };
    g.p0 = g.fwd(pw / 2, ph / 2);
    return g;
  }
  // Ajuster (contain) / Remplir (cover) pour une rotation donnée : renvoie le zoom (px sortie / px source).
  function fitZoom(mode, rot, pw, ph, W, H){
    const c = Math.abs(Math.cos(rad(rot))), s = Math.abs(Math.sin(rad(rot)));
    if(mode === 'cover') return Math.max((W * c + H * s) / pw, (W * s + H * c) / ph);
    return Math.min(W / (pw * c + ph * s), H / (pw * s + ph * c));
  }
  const M = {outline, geom, fitZoom, rad};
  if(typeof module !== 'undefined') module.exports = M;
  if(typeof window === 'undefined') return;

  // ─── Interface ──────────────────────────────────────────────────────────────────────────────
  const L = (k, ...a) => { let v = window.t ? window.t(k) : k; a.forEach((x, i) => { v = v.replace('{' + i + '}', x); }); return v; };
  const h = (tag, at, ...kids) => {
    const e = document.createElement(tag);
    for(const k in (at || {})){
      const v = at[k];
      if(v == null || v === false) continue;
      if(k === 'class') e.className = v; else if(k === 'text') e.textContent = v;
      else if(k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? '' : v);
    }
    kids.flat().forEach(c => { if(c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c)); });
    return e;
  };
  const clone = o => JSON.parse(JSON.stringify(o));
  const uid = () => Math.random().toString(36).slice(2, 8);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const rgba = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`; };
  const FORMATS = [['hd', 1920, 1080], ['uhd', 3840, 2160], ['square', 1080, 1080], ['vertical', 1080, 1920]];

  let S = null;                       // état de la session ouverte
  const fontCache = {};               // clé de police → {family, ok}
  const imgCache = {};                // url → Image

  function loadImg(url){
    return new Promise((res, rej) => {
      if(imgCache[url] && imgCache[url].complete && imgCache[url].naturalWidth) return res(imgCache[url]);
      const im = new Image(); im.onload = () => { imgCache[url] = im; res(im); }; im.onerror = () => rej(new Error(url));
      im.src = url;
    });
  }
  function ensureFont(key, repaint){
    if(fontCache[key]) return fontCache[key];
    const fam = 'ie_' + key.replace(/[^a-z0-9]/gi, '_');
    const f = fontCache[key] = {family: fam, ok: false};
    try{
      const ff = new FontFace(fam, `url(/api/media/edit/font/${encodeURIComponent(key)})`);
      ff.load().then(x => { document.fonts.add(x); f.ok = true; if(repaint) repaint(); else if(S && S.cv) paint(); }).catch(() => {});
    }catch(e){}
    return f;
  }

  function defaultRecipe(out, A){
    const r = {v: 1, out: {w: out.w, h: out.h}, view: {x: .5, y: A.ph / A.pw / 2, k: 1, rot: 0, flipH: false, flipV: false},
      bg: {mode: 'blur', color: '#000000', blur: 40, dim: .15, image: ''}, adjust: {brightness: 0, contrast: 0, saturation: 0},
      frame: {border: {w: 0, color: '#ffffff'}, radius: 0, shadow: {on: false, dx: 0, dy: 12, blur: 24, opacity: .5, color: '#000000'}},
      zones: [], texts: [], layers: [{id: 'image', type: 'image', visible: true}]};
    fit(r, A, 'contain');
    return r;
  }
  function fit(r, A, mode){
    const z = fitZoom(mode, r.view.rot, A.pw, A.ph, r.out.w, r.out.h);
    r.view.k = z * A.pw / r.out.w; r.view.x = .5; r.view.y = A.ph / A.pw / 2;
  }

  // ─── Rendu d'aperçu (canvas) — miroir de app/media_edit.py:render ───────────────────────────
  function zonedSource(){
    const r = S.rcp, A = S.A, vis = r.layers.filter(l => l.type === 'zone' && l.visible).map(l => l.id);
    const zs = r.zones.filter(z => vis.includes(z.id)).sort((a, b) => vis.indexOf(a.id) - vis.indexOf(b.id));
    const key = JSON.stringify(zs);
    if(S.zc && S.zc.key === key) return S.zc.cv;
    const cv = h('canvas'); cv.width = A.pw; cv.height = A.ph;
    const c = cv.getContext('2d'); c.drawImage(A.img, 0, 0, A.pw, A.ph);
    const pw = A.pw;
    for(const z of zs){
      const cx = z.x * pw, cy = z.y * pw, zw = z.w * pw, zh = z.h * pw;
      const pts = zonePoly(z, pw);
      const feather = z.soft / 100 * Math.min(zw, zh) / 4, sigma = z.amount * pw / 3000;
      const margin = Math.ceil(3 * Math.max(feather, sigma) + 4);
      const x0 = Math.max(0, Math.floor(Math.min(...pts.map(p => p[0]))) - margin), y0 = Math.max(0, Math.floor(Math.min(...pts.map(p => p[1]))) - margin);
      const x1 = Math.min(A.pw, Math.ceil(Math.max(...pts.map(p => p[0]))) + margin), y1 = Math.min(A.ph, Math.ceil(Math.max(...pts.map(p => p[1]))) + margin);
      if(x1 <= x0 || y1 <= y0) continue;
      const w = x1 - x0, hh = y1 - y0;
      const eff = h('canvas'); eff.width = w; eff.height = hh; const e = eff.getContext('2d');
      if(z.mode === 'blur'){ const cr = layer(w, hh); cr.getContext('2d').drawImage(cv, x0, y0, w, hh, 0, 0, w, hh); e.drawImage(blurred(cr, sigma), 0, 0); }
      else if(z.mode === 'pixel'){
        const blk = Math.max(2, Math.round(z.amount * pw / 2500));
        const sm = h('canvas'); sm.width = Math.max(1, Math.floor(w / blk)); sm.height = Math.max(1, Math.floor(hh / blk));
        const s2 = sm.getContext('2d'); s2.imageSmoothingQuality = 'high'; s2.drawImage(cv, x0, y0, w, hh, 0, 0, sm.width, sm.height);
        e.imageSmoothingEnabled = false; e.drawImage(sm, 0, 0, w, hh);
      } else { e.fillStyle = z.color; e.fillRect(0, 0, w, hh); }
      const mk = h('canvas'); mk.width = w; mk.height = hh; const m = mk.getContext('2d');
      m.fillStyle = '#fff'; m.beginPath(); pts.forEach((p, i) => i ? m.lineTo(p[0] - x0, p[1] - y0) : m.moveTo(p[0] - x0, p[1] - y0)); m.closePath(); m.fill();
      e.globalCompositeOperation = 'destination-in'; e.drawImage(feather > .3 ? blurred(mk, feather) : mk, 0, 0);
      c.drawImage(eff, x0, y0);
    }
    S.zc = {key, cv};
    return cv;
  }
  function zonePoly(z, pw){
    const cx = z.x * pw, cy = z.y * pw, zw = z.w * pw, zh = z.h * pw;
    if(z.shape === 'ellipse'){
      const c = Math.cos(rad(z.a)), s = Math.sin(rad(z.a)), pts = [];
      for(let i = 0; i < 72; i++){ const x = zw / 2 * Math.cos(2 * Math.PI * i / 72), y = zh / 2 * Math.sin(2 * Math.PI * i / 72); pts.push([cx + x * c - y * s, cy + x * s + y * c]); }
      return pts;
    }
    return outline(cx, cy, z.a, zw, zh, 0);
  }
  function pathOf(c, pts){ c.beginPath(); pts.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1])); c.closePath(); }
  function layer(W, H){ const cv = h('canvas'); cv.width = W; cv.height = H; return cv; }
  // Le flou du canevas (`ctx.filter`) est IGNORÉ par Safari, donc sur iPad : l'aperçu restait net (fond, zones,
  // ombres) alors que le rendu serveur de l'enregistrement floutait bien — constaté le 2026-10-18. On teste UNE
  // fois, au pixel, que le filtre agit vraiment ; sinon flou approché par pyramide (réductions de moitié puis
  // agrandissements successifs, lissage du navigateur), d'un écart comparable au flou gaussien demandé.
  let _filterOk = null;
  function canFilter(){
    if(_filterOk !== null) return _filterOk;
    try{
      const t = layer(9, 1), x = t.getContext('2d');
      x.filter = 'blur(2px)'; x.fillStyle = '#fff'; x.fillRect(4, 0, 1, 1);
      _filterOk = x.getImageData(1, 0, 1, 1).data[3] > 0;
    }catch(e){ _filterOk = false; }
    return _filterOk;
  }
  // Mêmes formules que les filtres CSS brightness → contrast → saturate (Filter Effects, matrices de la spec),
  // bornées entre chaque étape comme le fait le navigateur.
  function adjusted(src, a){
    const out = layer(src.width, src.height), o = out.getContext('2d'); o.drawImage(src, 0, 0);
    const im = o.getImageData(0, 0, out.width, out.height), d = im.data;
    const br = 1 + (a.brightness || 0) / 100, ct = 1 + (a.contrast || 0) / 100, s = 1 + (a.saturation || 0) / 100;
    const cl = v => v < 0 ? 0 : v > 255 ? 255 : v;
    for(let i = 0; i < d.length; i += 4){
      let r = cl(d[i] * br), g = cl(d[i + 1] * br), bl = cl(d[i + 2] * br);
      r = cl((r - 127.5) * ct + 127.5); g = cl((g - 127.5) * ct + 127.5); bl = cl((bl - 127.5) * ct + 127.5);
      d[i]     = (.213 + .787 * s) * r + (.715 - .715 * s) * g + (.072 - .072 * s) * bl;
      d[i + 1] = (.213 - .213 * s) * r + (.715 + .285 * s) * g + (.072 - .072 * s) * bl;
      d[i + 2] = (.213 - .213 * s) * r + (.715 - .715 * s) * g + (.072 + .928 * s) * bl;
    }
    o.putImageData(im, 0, 0); return out;
  }
  function blurred(src, sigma){
    const w = src.width, hh = src.height, out = layer(w, hh), o = out.getContext('2d');
    if(sigma <= .3){ o.drawImage(src, 0, 0); return out; }
    if(canFilter()){ o.filter = `blur(${sigma}px)`; o.drawImage(src, 0, 0); return out; }
    const steps = [], target = sigma * 3;   // 3 : réglé contre le filtre natif (écart moyen 1 à 8 / 255, 2 à 100 px)
    let cur = src, f = 1;
    while(f * 2 <= Math.max(2, target) && cur.width > 2 && cur.height > 2){
      const n = layer(Math.max(1, Math.round(cur.width / 2)), Math.max(1, Math.round(cur.height / 2)));
      const nx = n.getContext('2d'); nx.imageSmoothingQuality = 'high'; nx.drawImage(cur, 0, 0, n.width, n.height);
      steps.push(cur); cur = n; f *= 2;
    }
    while(steps.length){
      const up = steps.pop(), n = layer(up.width, up.height), nx = n.getContext('2d');
      nx.imageSmoothingQuality = 'high'; nx.drawImage(cur, 0, 0, up.width, up.height); cur = n;
    }
    o.drawImage(cur, 0, 0); return out;
  }

  function textLayout(c, t, W, H, u){
    const sz = Math.max(2, t.size * u), f = ensureFont(t.font);
    c.font = `${sz}px "${f.family}", sans-serif`; c.textBaseline = 'alphabetic'; c.textAlign = 'left';
    const lines = (t.text || '').split('\n'), lh = 1.2 * sz, widths = lines.map(l => c.measureText(l).width);
    const bw = Math.max(...widths), n = lines.length, ax = t.x * W, top = t.y * H - n * lh / 2;
    const left = t.align === 'left' ? ax : t.align === 'center' ? ax - bw / 2 : ax - bw;
    return {sz, lines, lh, widths, bw, n, left, top};
  }
  function paintText(c, t, W, H, u){
    if(!(t.text || '').trim()) return;
    const T = textLayout(c, t, W, H, u), {sz, lines, lh, widths, bw, n, left, top} = T;
    const bx = t.box;
    if(bx.on && bx.opacity > 0){ const p = bx.pad * u; c.fillStyle = rgba(bx.color, bx.opacity); c.fillRect(left - p, top - p, bw + 2 * p, n * lh + 2 * p); }
    const ly = layer(W, H), l = ly.getContext('2d');
    l.font = c.font; l.textBaseline = 'alphabetic'; l.textAlign = 'left'; l.lineJoin = 'round'; l.miterLimit = 2;
    const outw = t.stroke.w * u + (t.bold ? .03 * sz : 0), boldw = t.bold ? .03 * sz : 0;
    lines.forEach((ln, i) => {
      const x = left + (t.align === 'left' ? 0 : t.align === 'center' ? (bw - widths[i]) / 2 : bw - widths[i]);
      const base = top + i * lh + .5 * lh + .35 * sz;
      if(t.stroke.w > 0){ l.lineWidth = 2 * outw; l.strokeStyle = t.stroke.color; l.strokeText(ln, x, base); l.fillStyle = t.stroke.color; }
      if(boldw > 0){ l.lineWidth = 2 * boldw; l.strokeStyle = t.color; l.strokeText(ln, x, base); }
      l.fillStyle = t.color; l.fillText(ln, x, base);
    });
    const sh = t.shadow;
    if(sh.on && sh.opacity > 0){
      let sl = layer(W, H); sl.getContext('2d').drawImage(ly, sh.dx * u, sh.dy * u);
      if(sh.blur > 0) sl = blurred(sl, sh.blur * u);
      const s = sl.getContext('2d');
      s.globalCompositeOperation = 'source-in'; s.fillStyle = sh.color; s.fillRect(0, 0, W, H);
      c.globalAlpha = sh.opacity; c.drawImage(sl, 0, 0); c.globalAlpha = 1;
    }
    c.drawImage(ly, 0, 0);
    return T;
  }

  function paint(){
    if(!S || !S.cv) return;
    const r = S.rcp, A = S.A, W = S.cv.width, H = S.cv.height, u = Math.min(W, H) / 1080, c = S.cx;
    c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, W, H); c.imageSmoothingQuality = 'high';
    const zoned = zonedSource(), g = S.g = geom(r.view, A.pw, A.ph, W, H), bg = r.bg;
    // fond
    if(bg.mode !== 'none'){
      c.fillStyle = bg.color; c.fillRect(0, 0, W, H);
      let im = null, q = 0;
      if(bg.mode === 'blur') im = zoned;
      else if(bg.mode === 'image' && bg.image && S.bgImg) im = S.bgImg;
      if(im){
        const sg = bg.blur * u, iw = im.width || im.naturalWidth, ih = im.height || im.naturalHeight;
        if(bg.mode === 'blur') q = ((Math.round(r.view.rot / 90) % 4) + 4) % 4;
        const ow = q % 2 ? ih : iw, oh = q % 2 ? iw : ih;
        const cover = Math.max(W / ow, H / oh) * (bg.mode === 'blur' ? 1 + 4 * sg / Math.min(W, H) : 1);
        const t = layer(W, H), tc = t.getContext('2d'); tc.imageSmoothingQuality = 'high';
        tc.translate(W / 2, H / 2);
        if(bg.mode === 'blur'){ tc.rotate(q * Math.PI / 2); tc.scale(r.view.flipH ? -1 : 1, r.view.flipV ? -1 : 1); }
        tc.scale(cover, cover); tc.translate(-iw / 2, -ih / 2); tc.drawImage(im, 0, 0);
        c.drawImage(sg > 0 ? blurred(t, sg) : t, 0, 0);
        if(bg.dim > 0){ c.fillStyle = `rgba(0,0,0,${bg.dim})`; c.fillRect(0, 0, W, H); }
      }
    }
    S.textBoxes = {};
    for(const ly of r.layers){
      if(!ly.visible) continue;
      if(ly.type === 'image') paintPhoto(c, zoned, g, W, H, u);
      else if(ly.type === 'text'){ const t = r.texts.find(x => x.id === ly.id); if(t){ const T = paintText(c, t, W, H, u); if(T) S.textBoxes[t.id] = T; } }
    }
    paintOverlay();
  }
  function paintPhoto(c, zoned, g, W, H, u){
    const r = S.rcp, A = S.A, fr = r.frame, pts = outline(g.p0[0], g.p0[1], r.view.rot, A.pw * g.z, A.ph * g.z, fr.radius * u);
    const sh = fr.shadow;
    if(sh.on && sh.opacity > 0){
      const sl = layer(W, H), s = sl.getContext('2d');
      s.translate(sh.dx * u, sh.dy * u); s.fillStyle = rgba(sh.color, sh.opacity); pathOf(s, pts); s.fill();
      c.drawImage(sh.blur > 0 ? blurred(sl, sh.blur * u) : sl, 0, 0);
    }
    const ph = layer(W, H), p = ph.getContext('2d'); p.imageSmoothingQuality = 'high';
    p.translate(W / 2, H / 2); p.rotate(rad(r.view.rot)); p.scale(g.z, g.z); p.translate(-g.cx, -g.cy);
    p.translate(A.pw / 2, A.ph / 2); p.scale(g.fh ? -1 : 1, g.fv ? -1 : 1); p.translate(-A.pw / 2, -A.ph / 2);
    p.drawImage(zoned, 0, 0);
    const a = r.adjust, f = [];
    if(a.brightness) f.push(`brightness(${1 + a.brightness / 100})`);
    if(a.contrast) f.push(`contrast(${1 + a.contrast / 100})`);
    if(a.saturation) f.push(`saturate(${1 + a.saturation / 100})`);
    let src = ph;
    if(f.length && !canFilter()) src = adjusted(ph, a);   // Safari : retouches calculées au pixel (cf. canFilter)
    c.save(); pathOf(c, pts); c.clip(); if(f.length && src === ph) c.filter = f.join(' '); c.drawImage(src, 0, 0); c.restore();
    const bw = fr.border.w * u;
    if(bw > .2){
      const inner = outline(g.p0[0], g.p0[1], r.view.rot, Math.max(1, A.pw * g.z - 2 * bw), Math.max(1, A.ph * g.z - 2 * bw), Math.max(0, fr.radius * u - bw));
      c.save(); c.fillStyle = fr.border.color; c.beginPath();
      pts.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.closePath();
      inner.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.closePath();
      c.fill('evenodd'); c.restore();
    }
  }

  // Surcouche : sélection, poignées (hors rendu).
  function zoneScreen(z){ const A = S.A; return zonePoly(z, A.pw).map(p => S.g.fwd(p[0], p[1])); }
  function zoneHandle(z){
    const A = S.A, a = rad(z.a), hw = z.w * A.pw / 2, hh = z.h * A.pw / 2, cx = z.x * A.pw, cy = z.y * A.pw;
    return S.g.fwd(cx + hw * Math.cos(a) - hh * Math.sin(a), cy + hw * Math.sin(a) + hh * Math.cos(a));
  }
  function paintOverlay(){
    const o = S.ov, c = o.getContext('2d'); c.clearRect(0, 0, o.width, o.height);
    const accent = getComputedStyle(S.root).getPropertyValue('--accent').trim() || '#7aa2c8';
    const vis = id => (S.rcp.layers.find(l => l.id === id) || {}).visible;
    c.lineWidth = 1.5; c.strokeStyle = accent;
    for(const z of S.rcp.zones){
      if(!vis(z.id)) continue;
      const sel = S.sel && S.sel.id === z.id;
      if(!sel && S.tab !== 'zones') continue;
      c.setLineDash(sel ? [] : [4, 4]); c.globalAlpha = sel ? 1 : .55; pathOf(c, zoneScreen(z)); c.stroke();
      if(sel){ const hp = zoneHandle(z); c.setLineDash([]); c.fillStyle = accent; c.fillRect(hp[0] - 5, hp[1] - 5, 10, 10); }
    }
    c.globalAlpha = 1; c.setLineDash([]);
    if(S.sel && S.textBoxes[S.sel.id]){
      const T = S.textBoxes[S.sel.id]; c.setLineDash([5, 3]); c.strokeRect(T.left - 4, T.top - 4, T.bw + 8, T.n * T.lh + 8); c.setLineDash([]);
    }
    if(S.drag && S.drag.mode === 'draw'){
      const d = S.drag; c.setLineDash([4, 3]); c.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
    }
  }

  // ─── Historique ─────────────────────────────────────────────────────────────────────────────
  function snap(){ return JSON.stringify(S.rcp); }
  function commit(){
    const s = snap();
    if(S.hist[S.hi] === s) return;
    S.hist = S.hist.slice(0, S.hi + 1); S.hist.push(s); if(S.hist.length > 100) S.hist.shift(); S.hi = S.hist.length - 1;
    S.dirty = true; syncChrome();
  }
  function goHist(d){
    const i = S.hi + d; if(i < 0 || i >= S.hist.length) return;
    S.hi = i; S.rcp = JSON.parse(S.hist[i]); S.dirty = true;
    if(S.sel && !S.rcp.layers.some(l => l.id === S.sel.id)) S.sel = null;
    S.zc = null; loadBgImage().then(() => { renderPane(); paint(); }); syncChrome(); renderPane(); paint();
  }

  // ─── Contrôles ──────────────────────────────────────────────────────────────────────────────
  function row(label, ctl, cls){ return h('label', {class: 'ie-row ' + (cls || '')}, h('span', {class: 'ie-lbl', text: label}), ctl); }
  function slider(label, get, set, min, max, step, unit){
    const val = h('span', {class: 'ie-val'}), inp = h('input', {type: 'range', min, max, step: step || 1});
    inp.value = get(); const show = () => { val.textContent = (+inp.value).toFixed(step && step < 1 ? (step < .1 ? 2 : 1) : 0) + (unit || ''); }; show();
    inp.addEventListener('input', () => { set(+inp.value); show(); paint(); });
    inp.addEventListener('change', () => { commit(); });
    return row(label, h('span', {class: 'ie-ctl'}, inp, val));
  }
  function color(label, get, set){
    const i = h('input', {type: 'color', value: get()});
    i.addEventListener('input', () => { set(i.value); paint(); }); i.addEventListener('change', commit);
    return row(label, i);
  }
  function check(label, get, set, redo){
    const i = h('input', {type: 'checkbox'}); i.checked = !!get();
    i.addEventListener('change', () => { set(i.checked); commit(); paint(); if(redo) renderPane(); });
    return row(label, i, 'ie-chk');
  }
  function select(label, opts, get, set, redo){
    const s = h('select', null, opts.map(([v, t]) => h('option', {value: v, text: t})));
    s.value = get(); s.addEventListener('change', () => { set(s.value); commit(); paint(); if(redo) renderPane(); });
    return row(label, s);
  }
  function btn(text, fn, cls, title){ return h('button', {type: 'button', class: 'btn ie-btn ' + (cls || ''), text, title, onclick: fn}); }

  // ─── Volets ─────────────────────────────────────────────────────────────────────────────────
  const TABS = ['frame', 'bg', 'zones', 'style', 'adjust', 'text'];
  function renderPane(){
    const p = S.pane; p.textContent = '';
    S.tabsEl.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.tab === S.tab));
    const r = S.rcp;
    if(S.tab === 'frame') paneFrame(p, r);
    else if(S.tab === 'bg') paneBg(p, r);
    else if(S.tab === 'zones') paneZones(p, r);
    else if(S.tab === 'style') paneStyle(p, r);
    else if(S.tab === 'adjust') paneAdjust(p, r);
    else paneText(p, r);
    renderLayers();
  }
  function setView(fn){ fn(S.rcp.view); paint(); commit(); }
  function paneFrame(p, r){
    const A = S.A, v = r.view;
    p.append(h('div', {class: 'ie-btns'},
      btn(L('imgedit.fit'), () => { setView(() => fit(r, A, 'contain')); }, '', L('imgedit.fit_tip')),
      btn(L('imgedit.fill'), () => { setView(() => fit(r, A, 'cover')); }, '', L('imgedit.fill_tip')),
      btn(L('imgedit.px1'), () => { setView(() => { v.k = A.swTrue / r.out.w; }); }, '', L('imgedit.px1_tip')),
      btn(L('imgedit.center'), () => { setView(() => { v.x = .5; v.y = A.ph / A.pw / 2; }); })));
    p.append(h('div', {class: 'ie-btns'},
      btn('↺ 90°', () => { setView(() => { v.rot = Math.round(v.rot / 90) * 90 - 90; fit(r, A, 'contain'); }); renderPane(); }),
      btn('↻ 90°', () => { setView(() => { v.rot = Math.round(v.rot / 90) * 90 + 90; fit(r, A, 'contain'); }); renderPane(); }),
      btn(L('imgedit.flip_h'), () => { setView(() => { v.flipH = !v.flipH; v.x = 1 - v.x; v.rot = -v.rot; }); renderPane(); }),
      btn(L('imgedit.flip_v'), () => { setView(() => { v.flipV = !v.flipV; v.y = A.ph / A.pw - v.y; v.rot = -v.rot; }); renderPane(); })));
    const q = () => Math.round(v.rot / 90);
    p.append(slider(L('imgedit.straighten'), () => v.rot - q() * 90, x => { v.rot = q() * 90 + x; }, -45, 45, .1, '°'));
    p.append(slider(L('imgedit.zoom'), () => Math.log(v.k) / Math.LN2, x => { v.k = Math.pow(2, x); }, -4, 6, .01));
    p.append(h('p', {class: 'ie-hint', text: L('imgedit.frame_hint')}));
  }
  function paneBg(p, r){
    const b = r.bg;
    p.append(select(L('imgedit.bg_mode'), [['color', L('imgedit.bg_color')], ['blur', L('imgedit.bg_blur')], ['image', L('imgedit.bg_image')], ['none', L('imgedit.bg_none')]],
      () => b.mode, x => { b.mode = x; }, true));
    if(b.mode !== 'none') p.append(color(L('imgedit.color'), () => b.color, x => { b.color = x; }));
    if(b.mode === 'blur' || b.mode === 'image'){
      p.append(slider(L('imgedit.blur'), () => b.blur, x => { b.blur = x; }, 0, 150, 1));
      p.append(slider(L('imgedit.dim'), () => b.dim * 100, x => { b.dim = x / 100; }, 0, 100, 1, ' %'));
    }
    if(b.mode === 'image'){
      p.append(btn(b.image ? L('imgedit.bg_change') : L('imgedit.bg_pick'), pickBgImage));
      if(b.image) p.append(h('p', {class: 'ie-hint', text: S.bgName || b.image}));
    }
  }
  function toolBtn(text, tool){
    const b = btn(text, () => { S.tool = S.tool === tool ? 'move' : tool; syncChrome(); }, S.tool === tool ? 'on' : ''); b.dataset.tool = tool; return b;
  }
  function paneZones(p, r){
    p.append(h('div', {class: 'ie-btns'},
      toolBtn(L('imgedit.zone_rect'), 'rect'), toolBtn(L('imgedit.zone_ellipse'), 'ellipse')));
    p.append(h('p', {class: 'ie-hint', text: L('imgedit.zone_hint')}));
    const z = S.sel && r.zones.find(x => x.id === S.sel.id);
    if(!z) return;
    p.append(select(L('imgedit.zone_effect'), [['blur', L('imgedit.eff_blur')], ['pixel', L('imgedit.eff_pixel')], ['fill', L('imgedit.eff_fill')]], () => z.mode, x => { z.mode = x; }, true));
    p.append(slider(z.mode === 'pixel' ? L('imgedit.block') : L('imgedit.strength'), () => z.amount, x => { z.amount = x; }, 1, 100, 1));
    if(z.mode === 'fill') p.append(color(L('imgedit.color'), () => z.color, x => { z.color = x; }));
    p.append(slider(L('imgedit.soft'), () => z.soft, x => { z.soft = x; }, 0, 100, 1, ' %'));
    p.append(select(L('imgedit.shape'), [['rect', L('imgedit.zone_rect')], ['ellipse', L('imgedit.zone_ellipse')]], () => z.shape, x => { z.shape = x; }));
    p.append(slider(L('imgedit.width'), () => z.w * 100, x => { z.w = x / 100; }, 1, 100, .5, ' %'));
    p.append(slider(L('imgedit.height'), () => z.h * 100, x => { z.h = x / 100; }, 1, 100, .5, ' %'));
    p.append(slider(L('imgedit.angle'), () => z.a, x => { z.a = x; }, -180, 180, .5, '°'));
  }
  function paneStyle(p, r){
    const f = r.frame, bd = f.border, sh = f.shadow;
    p.append(h('h4', {text: L('imgedit.border')}));
    p.append(slider(L('imgedit.thickness'), () => bd.w, x => { bd.w = x; }, 0, 80, 1, ' px'), color(L('imgedit.color'), () => bd.color, x => { bd.color = x; }));
    p.append(h('h4', {text: L('imgedit.corners')}), slider(L('imgedit.radius'), () => f.radius, x => { f.radius = x; }, 0, 400, 1, ' px'));
    p.append(h('h4', {text: L('imgedit.shadow')}), check(L('imgedit.enabled'), () => sh.on, x => { sh.on = x; }, true));
    if(sh.on){
      p.append(slider(L('imgedit.dx'), () => sh.dx, x => { sh.dx = x; }, -200, 200, 1), slider(L('imgedit.dy'), () => sh.dy, x => { sh.dy = x; }, -200, 200, 1),
        slider(L('imgedit.blur'), () => sh.blur, x => { sh.blur = x; }, 0, 150, 1), slider(L('imgedit.opacity'), () => sh.opacity * 100, x => { sh.opacity = x / 100; }, 0, 100, 1, ' %'),
        color(L('imgedit.color'), () => sh.color, x => { sh.color = x; }));
    }
  }
  function paneAdjust(p, r){
    const a = r.adjust;
    p.append(slider(L('imgedit.brightness'), () => a.brightness, x => { a.brightness = x; }, -100, 100, 1),
      slider(L('imgedit.contrast'), () => a.contrast, x => { a.contrast = x; }, -100, 100, 1),
      slider(L('imgedit.saturation'), () => a.saturation, x => { a.saturation = x; }, -100, 100, 1),
      btn(L('imgedit.reset'), () => { a.brightness = a.contrast = a.saturation = 0; commit(); paint(); renderPane(); }));
  }
  function paneText(p, r){
    p.append(btn(L('imgedit.add_text'), addText));
    const t = S.sel && r.texts.find(x => x.id === S.sel.id);
    if(!t){ p.append(h('p', {class: 'ie-hint', text: L('imgedit.text_hint')})); return; }
    const ta = h('textarea', {rows: 3}); ta.value = t.text;
    ta.addEventListener('input', () => { t.text = ta.value; paint(); renderLayersOnly(); }); ta.addEventListener('change', commit);
    p.append(row(L('imgedit.text'), ta, 'ie-col'));
    const fonts = S.fonts || [];
    if(!fonts.some(f => f.key === t.font)) fonts.push({key: t.font, name: t.font});
    p.append(select(L('imgedit.font'), fonts.map(f => [f.key, f.name]), () => t.font, x => { t.font = x; ensureFont(x, paint); }));
    p.append(slider(L('imgedit.size'), () => t.size, x => { t.size = x; }, 8, 400, 1, ' px'),
      color(L('imgedit.color'), () => t.color, x => { t.color = x; }),
      check(L('imgedit.bold'), () => t.bold, x => { t.bold = x; }),
      select(L('imgedit.align'), [['left', L('imgedit.left')], ['center', L('imgedit.centered')], ['right', L('imgedit.right')]], () => t.align, x => { t.align = x; }));
    p.append(h('h4', {text: L('imgedit.outline')}), slider(L('imgedit.thickness'), () => t.stroke.w, x => { t.stroke.w = x; }, 0, 40, .5, ' px'),
      color(L('imgedit.color'), () => t.stroke.color, x => { t.stroke.color = x; }));
    const sh = t.shadow;
    p.append(h('h4', {text: L('imgedit.shadow')}), check(L('imgedit.enabled'), () => sh.on, x => { sh.on = x; }, true));
    if(sh.on) p.append(slider(L('imgedit.dx'), () => sh.dx, x => { sh.dx = x; }, -60, 60, 1), slider(L('imgedit.dy'), () => sh.dy, x => { sh.dy = x; }, -60, 60, 1),
      slider(L('imgedit.blur'), () => sh.blur, x => { sh.blur = x; }, 0, 60, 1), slider(L('imgedit.opacity'), () => sh.opacity * 100, x => { sh.opacity = x / 100; }, 0, 100, 1, ' %'),
      color(L('imgedit.color'), () => sh.color, x => { sh.color = x; }));
    const bx = t.box;
    p.append(h('h4', {text: L('imgedit.box')}), check(L('imgedit.enabled'), () => bx.on, x => { bx.on = x; }, true));
    if(bx.on) p.append(color(L('imgedit.color'), () => bx.color, x => { bx.color = x; }), slider(L('imgedit.opacity'), () => bx.opacity * 100, x => { bx.opacity = x / 100; }, 0, 100, 1, ' %'),
      slider(L('imgedit.margin'), () => bx.pad, x => { bx.pad = x; }, 0, 200, 1, ' px'));
  }
  function addText(){
    const t = {id: 't' + uid(), text: L('imgedit.text_default'), x: .5, y: .5, font: 'dejavu-sans-bold', size: 90, color: '#ffffff', bold: true, align: 'center',
      stroke: {w: 0, color: '#000000'}, shadow: {on: true, dx: 3, dy: 3, blur: 8, opacity: .7, color: '#000000'}, box: {on: false, color: '#000000', opacity: .6, pad: 16}};
    S.rcp.texts.push(t); S.rcp.layers.push({id: t.id, type: 'text', visible: true});
    S.sel = {type: 'text', id: t.id}; S.tab = 'text'; ensureFont(t.font, paint); commit(); renderPane(); paint();
  }

  // ─── Calques ────────────────────────────────────────────────────────────────────────────────
  function layerName(l){
    if(l.type === 'image') return L('imgedit.layer_image');
    if(l.type === 'zone') return L('imgedit.layer_zone') + ' ' + (S.rcp.zones.findIndex(z => z.id === l.id) + 1);
    const t = S.rcp.texts.find(x => x.id === l.id); return '“' + ((t && t.text) || '').replace(/\n/g, ' ').slice(0, 22) + '”';
  }
  function renderLayersOnly(){ renderLayers(); }
  function renderLayers(){
    const box = S.layersEl; box.textContent = '';
    const ls = S.rcp.layers;
    for(let i = ls.length - 1; i >= 0; i--){
      const l = ls[i], sel = S.sel && S.sel.id === l.id;
      box.append(h('div', {class: 'ie-layer' + (sel ? ' on' : '') + (l.visible ? '' : ' off'), onclick: () => selectLayer(l)},
        h('button', {type: 'button', class: 'ie-ico', title: L('imgedit.toggle'), text: l.visible ? '👁' : '—', onclick: ev => { ev.stopPropagation(); l.visible = !l.visible; commit(); paint(); renderLayers(); }}),
        h('span', {class: 'ie-lname', text: layerName(l)}),
        h('button', {type: 'button', class: 'ie-ico', title: L('imgedit.up'), text: '▲', disabled: i === ls.length - 1, onclick: ev => { ev.stopPropagation(); move(i, 1); }}),
        h('button', {type: 'button', class: 'ie-ico', title: L('imgedit.down'), text: '▼', disabled: i === 0, onclick: ev => { ev.stopPropagation(); move(i, -1); }}),
        l.type === 'image' ? h('span', {class: 'ie-ico'}) : h('button', {type: 'button', class: 'ie-ico', title: L('imgedit.delete'), text: '✕', onclick: ev => { ev.stopPropagation(); delLayer(l); }})));
    }
  }
  function move(i, d){ const ls = S.rcp.layers, j = i + d; if(j < 0 || j >= ls.length) return; [ls[i], ls[j]] = [ls[j], ls[i]]; commit(); renderLayers(); paint(); }
  function delLayer(l){
    S.rcp.layers = S.rcp.layers.filter(x => x.id !== l.id); S.rcp.zones = S.rcp.zones.filter(x => x.id !== l.id); S.rcp.texts = S.rcp.texts.filter(x => x.id !== l.id);
    if(S.sel && S.sel.id === l.id) S.sel = null; commit(); renderPane(); paint();
  }
  function selectLayer(l){
    S.sel = l.type === 'image' ? null : {type: l.type, id: l.id};
    if(l.type === 'zone') S.tab = 'zones'; else if(l.type === 'text') S.tab = 'text'; else if(S.tab === 'zones' || S.tab === 'text') S.tab = 'frame';
    renderPane(); paint();
  }

  // ─── Souris ─────────────────────────────────────────────────────────────────────────────────
  function mouse(ev){ const b = S.ov.getBoundingClientRect(); return [(ev.clientX - b.left) * S.ov.width / b.width, (ev.clientY - b.top) * S.ov.height / b.height]; }
  function inPoly(pt, poly){
    let ins = false;
    for(let i = 0, j = poly.length - 1; i < poly.length; j = i++){
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if(((yi > pt[1]) !== (yj > pt[1])) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) ins = !ins;
    }
    return ins;
  }
  function onDown(ev){
    if(ev.button !== 0) return;
    const m = mouse(ev), r = S.rcp, g = S.g;
    S.ov.setPointerCapture(ev.pointerId);
    if(S.tool === 'rect' || S.tool === 'ellipse'){ S.drag = {mode: 'draw', x0: m[0], y0: m[1], x1: m[0], y1: m[1]}; return; }
    const selZ = S.sel && r.zones.find(z => z.id === S.sel.id);
    if(selZ){ const hp = zoneHandle(selZ); if(Math.hypot(hp[0] - m[0], hp[1] - m[1]) < 10){ S.drag = {mode: 'zresize', z: selZ}; return; } }
    const vis = id => (r.layers.find(l => l.id === id) || {}).visible;
    for(let i = r.layers.length - 1; i >= 0; i--){
      const l = r.layers[i]; if(!l.visible) continue;
      if(l.type === 'text'){
        const T = S.textBoxes[l.id];
        if(T && m[0] >= T.left - 6 && m[0] <= T.left + T.bw + 6 && m[1] >= T.top - 6 && m[1] <= T.top + T.n * T.lh + 6){
          const t = r.texts.find(x => x.id === l.id); S.sel = {type: 'text', id: t.id}; S.tab = 'text'; renderPane();
          S.drag = {mode: 'tmove', t, ox: m[0] - t.x * g.W, oy: m[1] - t.y * g.H}; paint(); return;
        }
      } else if(l.type === 'zone'){
        const z = r.zones.find(x => x.id === l.id);
        if(z && vis(z.id) && inPoly(m, zoneScreen(z))){
          S.sel = {type: 'zone', id: z.id}; S.tab = 'zones'; renderPane();
          S.drag = {mode: 'zmove', z, p: g.inv(m[0], m[1]), x: z.x, y: z.y}; paint(); return;
        }
      }
    }
    if(S.sel){ S.sel = null; if(S.tab === 'zones' || S.tab === 'text') renderPane(); else renderLayers(); }
    S.drag = {mode: 'pan', m, cx: r.view.x, cy: r.view.y}; paint();
  }
  function onMove(ev){
    if(!S.drag) return;
    const m = mouse(ev), d = S.drag, r = S.rcp, g = S.g, A = S.A;
    if(d.mode === 'pan'){
      const dx = (m[0] - d.m[0]) / g.z, dy = (m[1] - d.m[1]) / g.z;
      r.view.x = d.cx - (g.c * dx + g.s * dy) / A.pw; r.view.y = d.cy - (-g.s * dx + g.c * dy) / A.pw;
    } else if(d.mode === 'tmove'){ d.t.x = (m[0] - d.ox) / g.W; d.t.y = (m[1] - d.oy) / g.H; }
    else if(d.mode === 'zmove'){
      const p = g.inv(m[0], m[1]); d.z.x = d.x + (p[0] - d.p[0]) / A.pw; d.z.y = d.y + (p[1] - d.p[1]) / A.pw;
    } else if(d.mode === 'zresize'){
      const p = g.inv(m[0], m[1]), z = d.z, a = rad(z.a), dx = p[0] - z.x * A.pw, dy = p[1] - z.y * A.pw;
      const lx = dx * Math.cos(a) + dy * Math.sin(a), ly = -dx * Math.sin(a) + dy * Math.cos(a);
      z.w = clamp(2 * Math.abs(lx) / A.pw, .005, 50); z.h = clamp(2 * Math.abs(ly) / A.pw, .005, 50);
    } else if(d.mode === 'draw'){ d.x1 = m[0]; d.y1 = m[1]; }
    d.moved = true; paint();
  }
  function onUp(){
    const d = S.drag; S.drag = null; if(!d) return;
    if(d.mode === 'draw'){
      if(Math.abs(d.x1 - d.x0) > 6 && Math.abs(d.y1 - d.y0) > 6){
        const g = S.g, A = S.A, cx = (d.x0 + d.x1) / 2, cy = (d.y0 + d.y1) / 2, c = g.inv(cx, cy);
        const ex = [(g.fh ? -1 : 1) * g.c, (g.fv ? -1 : 1) * -g.s];
        const z = {id: 'z' + uid(), shape: S.tool, x: c[0] / A.pw, y: c[1] / A.pw, w: Math.abs(d.x1 - d.x0) / g.z / A.pw, h: Math.abs(d.y1 - d.y0) / g.z / A.pw,
          a: Math.atan2(ex[1], ex[0]) * 180 / Math.PI, mode: 'blur', amount: 45, color: '#000000', soft: 0};
        S.rcp.zones.push(z); S.rcp.layers.push({id: z.id, type: 'zone', visible: true}); S.sel = {type: 'zone', id: z.id};
      }
      S.tool = 'move'; syncChrome(); renderPane(); paint(); if(S.sel) commit(); return;
    }
    if(d.moved) commit(); renderPane();
  }
  function onWheel(ev){
    ev.preventDefault();
    const m = mouse(ev), r = S.rcp, g = S.g, A = S.A, k2 = clamp(r.view.k * Math.exp(-ev.deltaY * .0015), .01, 100);
    const z2 = k2 * g.W / A.pw, vx = m[0] - g.W / 2, vy = m[1] - g.H / 2, f = 1 / g.z - 1 / z2;
    r.view.x += (g.c * vx + g.s * vy) * f / A.pw; r.view.y += (-g.s * vx + g.c * vy) * f / A.pw; r.view.k = k2;
    paint(); clearTimeout(S.wt); S.wt = setTimeout(() => { commit(); if(S.tab === 'frame') renderPane(); }, 250);
  }

  // ─── Fond « autre image » ───────────────────────────────────────────────────────────────────
  async function loadBgImage(){
    const id = S.rcp.bg.image; S.bgImg = null;
    if(S.rcp.bg.mode === 'image' && id){
      try{ S.bgImg = await loadImg(`/api/media/assets/${id}/frame?t=0&w=960`); }catch(e){}
      try{ const a = await (await fetch('/api/media/assets/' + id)).json(); S.bgName = (a.asset || a).name; }catch(e){}
    }
  }
  function pickBgImage(){
    const wrap = h('div', {class: 'ie-pick'}), box = h('div', {class: 'ie-pick-box'});
    const close = () => wrap.remove();
    box.append(h('div', {class: 'ie-pick-h'}, h('b', {text: L('imgedit.bg_pick')}), btn(L('imgedit.cancel'), close)));
    const grid = h('div', {class: 'ie-pick-grid'}); box.append(grid); wrap.append(box); S.root.append(wrap);
    fetch('/api/media/assets').then(r => r.json()).then(j => {
      const list = (j.assets || j || []).filter(a => a.tech && a.tech.kind === 'image' && a.id !== S.origId);
      if(!list.length) grid.append(h('p', {class: 'ie-hint', text: L('imgedit.no_images')}));
      list.forEach(a => grid.append(h('button', {type: 'button', class: 'ie-pick-i', title: a.name, onclick: async () => {
        S.rcp.bg.image = a.id; S.rcp.bg.mode = 'image'; close(); await loadBgImage(); commit(); renderPane(); paint(); }},
        h('img', {src: `/api/media/assets/${a.id}/thumb${a.thumb && a.thumb.v ? '?v=' + a.thumb.v : ''}`, alt: ''}), h('span', {text: a.name}))));
    }).catch(() => grid.append(h('p', {class: 'ie-hint', text: L('imgedit.error')})));
  }

  // ─── Format de sortie ───────────────────────────────────────────────────────────────────────
  function setFormat(w, hh){
    const r = S.rcp; r.out.w = clamp(Math.round(w), 16, 8192); r.out.h = clamp(Math.round(hh), 16, 8192);
    fit(r, S.A, 'contain'); layoutCanvas(); commit(); paint(); renderPane();
  }
  function formatKey(){
    const o = S.rcp.out; const f = FORMATS.find(x => x[1] === o.w && x[2] === o.h);
    if(f) return f[0];
    return S.optFormat && S.optFormat.w === o.w && S.optFormat.h === o.h ? 'opt' : 'free';
  }
  function layoutCanvas(){
    const o = S.rcp.out, st = S.stage.getBoundingClientRect();
    const mw = Math.max(200, st.width - 24), mh = Math.max(150, st.height - 24), s = Math.min(mw / o.w, mh / o.h, 1.5);
    let W = Math.round(o.w * s), H = Math.round(o.h * s);
    const cap = 1400; if(W > cap){ H = Math.round(H * cap / W); W = cap; }
    [S.cv, S.ov].forEach(c => { c.width = W; c.height = H; c.style.width = W + 'px'; c.style.height = H + 'px'; });
    S.cx = S.cv.getContext('2d');
  }
  function renderFormatBar(){
    const bar = S.fmtEl; bar.textContent = '';
    const opts = FORMATS.map(([k, w, hh]) => [k, L('imgedit.fmt_' + k) + ` ${w}×${hh}`]);
    if(S.optFormat && !FORMATS.some(f => f[1] === S.optFormat.w && f[2] === S.optFormat.h)) opts.push(['opt', (S.optFormat.label || L('imgedit.fmt_opt')) + ` ${S.optFormat.w}×${S.optFormat.h}`]);
    opts.push(['free', L('imgedit.fmt_free')]);
    const sel = h('select', {title: L('imgedit.format')}, opts.map(([v, t]) => h('option', {value: v, text: t}))); sel.value = formatKey();
    const wi = h('input', {type: 'number', min: 16, max: 8192, value: S.rcp.out.w, class: 'ie-num'}), hi = h('input', {type: 'number', min: 16, max: 8192, value: S.rcp.out.h, class: 'ie-num'});
    sel.addEventListener('change', () => {
      const f = FORMATS.find(x => x[0] === sel.value);
      if(f) setFormat(f[1], f[2]); else if(sel.value === 'opt') setFormat(S.optFormat.w, S.optFormat.h);
      renderFormatBar();
    });
    const apply = () => setFormat(+wi.value || 1920, +hi.value || 1080);
    wi.addEventListener('change', () => { apply(); renderFormatBar(); }); hi.addEventListener('change', () => { apply(); renderFormatBar(); });
    bar.append(sel, wi, '×', hi);
  }

  // ─── Enregistrer ────────────────────────────────────────────────────────────────────────────
  function status(msg, kind){ S.msg.textContent = msg || ''; S.msg.className = 'ie-msg ' + (kind || ''); }
  async function save(overwrite){
    if(S.saving) return;
    S.saving = true; syncChrome(); S.prog.hidden = false; S.progBar.style.width = '3%';
    status(L('imgedit.saving'));
    try{
      const body = {recipe: S.rcp}; if(overwrite){ body.overwrite = overwrite; body.confirm_overwrite = true; }
      const r = await fetch(`/api/media/edit/${S.origId}/save`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const j = await r.json(); if(!r.ok) throw new Error(j.error || r.status);
      for(;;){
        await new Promise(res => setTimeout(res, 500));
        const s = await (await fetch('/api/media/edit/jobs/' + j.job)).json();
        S.progBar.style.width = (s.pct || 5) + '%'; status(L('imgedit.step_' + (s.step || 'load')));
        if(s.state === 'failed') throw new Error(s.error || '?');
        if(s.state === 'done'){
          S.dirty = false; status(L('imgedit.saved', s.result.path.split('/').pop()), 'ok');
          S.editing = {id: s.result.version ? '@' + s.result.version : s.result.id, name: s.result.path.split('/').pop()};
          if(S.opts.onSaved) try{ S.opts.onSaved(s.result); }catch(e){}
          window.dispatchEvent(new CustomEvent('imageeditor:saved', {detail: s.result}));
          refreshVariants(); break;
        }
      }
    }catch(e){ status(L('imgedit.save_failed', e.message), 'err'); }
    S.saving = false; S.prog.hidden = true; syncChrome();
  }
  async function refreshVariants(){
    try{ S.info = await (await fetch(`/api/media/edit/${S.origId}/info`)).json(); }catch(e){}
    syncChrome();
  }
  function confirmBox(text, yes, fn){
    const w = h('div', {class: 'ie-confirm'}, h('span', {text}), btn(yes, () => { w.remove(); fn(); }, 'danger'), btn(L('imgedit.cancel'), () => w.remove()));
    S.top.after(w);
  }

  function syncChrome(){
    if(!S) return;
    S.undoB.disabled = S.hi <= 0; S.redoB.disabled = S.hi >= S.hist.length - 1;
    S.saveB.disabled = S.saving; S.overB.hidden = !S.editing; S.overB.disabled = S.saving;
    if(S.editing) S.overB.textContent = L('imgedit.overwrite', S.editing.name);
    S.root.querySelectorAll('[data-tool]').forEach(b => b.classList.toggle('on', S.tool === b.dataset.tool));
    S.cv.style.cursor = (S.tool === 'rect' || S.tool === 'ellipse') ? 'crosshair' : 'grab';
    const vs = S.varEl; vs.textContent = '';
    const vl = (S.info && S.info.variants) || [];
    vs.append(h('option', {value: '', text: L('imgedit.new_variant')}), vl.map(v => h('option', {value: v.id, text: v.name})));
    vs.value = S.editing ? S.editing.id : ''; vs.hidden = !vl.length;
  }

  // ─── Ouverture ──────────────────────────────────────────────────────────────────────────────
  async function open(assetId, opts){
    close(true);
    opts = opts || {};
    const root = h('div', {class: 'ie', role: 'dialog', 'aria-modal': 'true'});
    document.body.append(root); document.body.classList.add('ie-open');
    root.append(h('div', {class: 'ie-loading', text: L('imgedit.loading')}));
    S = {root, opts, tool: 'move', tab: 'frame', sel: null, hist: [], hi: -1, dirty: false, saving: false, zc: null, textBoxes: {}};
    try{
      const ij = await fetch(`/api/media/edit/${assetId}/info`); const info = await ij.json();
      if(!ij.ok) throw new Error(info.error || ij.status);
      S.info = info; S.origId = info.original.id; S.editing = info.editing;
      const img = await loadImg(`/api/media/assets/${S.origId}/frame?t=0&w=960`);
      S.A = {img, pw: img.naturalWidth, ph: img.naturalHeight, swTrue: info.original.w || img.naturalWidth, shTrue: info.original.h || img.naturalHeight};
      try{ S.fonts = (await (await fetch('/api/media/edit/fonts')).json()).fonts; }catch(e){ S.fonts = []; }
      S.optFormat = opts.format && opts.format.w && opts.format.h ? opts.format : null;
      const out = info.recipe ? info.recipe.out : (S.optFormat || {w: 1920, h: 1080});
      S.rcp = info.recipe ? clone(info.recipe) : defaultRecipe(out, S.A);
      S.rcp.texts.forEach(t => ensureFont(t.font));
      await loadBgImage();
    }catch(e){
      root.textContent = ''; root.append(h('div', {class: 'ie-loading'}, L('imgedit.open_failed', e.message), h('br'), btn(L('imgedit.close'), () => close())));
      return;
    }
    build(info_title());
    S.hist = [snap()]; S.hi = 0; S.dirty = false;
    layoutCanvas(); renderFormatBar(); renderPane(); syncChrome(); paint();
    S.ro = new ResizeObserver(() => { if(S && S.stage){ layoutCanvas(); paint(); } }); S.ro.observe(S.stage);
    S.key = ev => {
      if(ev.target.matches('input, textarea, select')) { if(ev.key === 'Escape') ev.target.blur(); return; }
      if(ev.key === 'Escape') tryClose();
      else if((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'z'){ ev.preventDefault(); goHist(ev.shiftKey ? 1 : -1); }
      else if((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'y'){ ev.preventDefault(); goHist(1); }
      else if((ev.key === 'Delete' || ev.key === 'Backspace') && S.sel){ delLayer({id: S.sel.id}); }
    };
    document.addEventListener('keydown', S.key);
  }
  function info_title(){ return S.info.original.name; }
  function build(title){
    const root = S.root; root.textContent = '';
    S.undoB = btn('↶', () => goHist(-1), '', L('imgedit.undo')); S.redoB = btn('↷', () => goHist(1), '', L('imgedit.redo'));
    S.fmtEl = h('span', {class: 'ie-fmt'});
    S.varEl = h('select', {class: 'ie-var', title: L('imgedit.variants'), onchange: ev => switchVariant(ev.target.value)});
    S.saveB = btn(L('imgedit.save_new'), () => save(null), 'primary');
    S.overB = btn('', () => confirmBox(L('imgedit.overwrite_ask', S.editing.name), L('imgedit.overwrite_yes'), () => save(S.editing.id)), 'danger');
    S.msg = h('span', {class: 'ie-msg'});
    S.top = h('div', {class: 'ie-top'}, h('b', {class: 'ie-title', text: L('imgedit.title', title)}), S.fmtEl, S.undoB, S.redoB, S.varEl, h('span', {class: 'ie-sp'}), S.msg, S.saveB, S.overB,
      btn(L('imgedit.close'), tryClose));
    S.tabsEl = h('div', {class: 'ie-tabs'}, TABS.map(t => h('button', {type: 'button', 'data-tab': t, class: 'ie-tab', text: L('imgedit.tab_' + t), onclick: () => { S.tab = t; if(t !== 'zones' && t !== 'text') S.sel = null; else if(S.sel && S.sel.type !== (t === 'zones' ? 'zone' : 'text')) S.sel = null; renderPane(); paint(); }})));
    S.pane = h('div', {class: 'ie-pane'});
    S.left = h('div', {class: 'ie-left'}, S.tabsEl, S.pane);
    S.cv = h('canvas', {class: 'ie-cv'}); S.ov = h('canvas', {class: 'ie-ov'});
    S.ov.addEventListener('pointerdown', onDown); S.ov.addEventListener('pointermove', onMove); S.ov.addEventListener('pointerup', onUp); S.ov.addEventListener('pointercancel', onUp);
    S.ov.addEventListener('wheel', onWheel, {passive: false});
    S.stage = h('div', {class: 'ie-stage'}, h('div', {class: 'ie-cvbox'}, S.cv, S.ov));
    S.layersEl = h('div', {class: 'ie-layers'});
    S.right = h('div', {class: 'ie-right'}, h('h4', {text: L('imgedit.layers')}), S.layersEl);
    S.progBar = h('div', {class: 'ie-bar'}); S.prog = h('div', {class: 'ie-prog', hidden: true}, S.progBar);
    root.append(S.top, S.prog, h('div', {class: 'ie-main'}, S.left, S.stage, S.right));
  }
  function switchVariant(id){
    const doIt = () => {
      if(!id){ S.editing = null; syncChrome(); return; }
      const v = S.info.variants.find(x => x.id === id); if(!v || !v.recipe) return;
      S.rcp = clone(v.recipe); S.editing = {id: v.id, name: v.name}; S.sel = null; S.zc = null;
      S.hist = [snap()]; S.hi = 0; S.dirty = false;
      loadBgImage().then(() => { layoutCanvas(); renderFormatBar(); renderPane(); paint(); }); layoutCanvas(); renderFormatBar(); renderPane(); syncChrome(); paint();
    };
    if(S.dirty && id) confirmBox(L('imgedit.discard_ask'), L('imgedit.discard_yes'), doIt); else doIt();
    if(S.dirty && id) S.varEl.value = S.editing ? S.editing.id : '';
  }
  function tryClose(){
    if(S && S.dirty && !S.saving) confirmBox(L('imgedit.discard_ask'), L('imgedit.discard_yes'), () => close());
    else if(S && !S.saving) close();
  }
  function close(silent){
    if(!S) return;
    document.removeEventListener('keydown', S.key); if(S.ro) S.ro.disconnect();
    const cb = S.opts && S.opts.onClose; S.root.remove(); document.body.classList.remove('ie-open'); S = null;
    if(!silent && cb) try{ cb(); }catch(e){}
  }

  window.ImageEditor = {open, close: () => close(), _math: M};
})();
