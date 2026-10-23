// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// BLAKE2b (RFC 7693), sans clé, longueur de sortie 1 à 64 octets — écrit pour ce projet (aucun code
// tiers). WebCrypto ne le propose pas ; il sert à calculer DANS LE NAVIGATEUR l'empreinte d'un
// fichier exactement comme la base des médias (script_templates/media_scan.py:fingerprint) :
// blake2b-128 de (taille en décimal, 4 Mio de tête, 4 Mio de queue). Mots de 64 bits portés par des
// paires de 32 bits (bas, haut) dans des Uint32Array : pas de BigInt, rapide.
//
//   const h = Blake2b.create(16); h.update(u8); …; h.digest() → Uint8Array
//   Blake2b.hex(u8)
//   await Blake2b.mediaFingerprint(file)   (File/Blob du poste ; 2 × File.slice, jamais le fichier entier)
(function(root){
  'use strict';
  const IV = new Uint32Array([          // (bas, haut) des 8 mots d'initialisation
    0xF3BCC908, 0x6A09E667, 0x84CAA73B, 0xBB67AE85, 0xFE94F82B, 0x3C6EF372, 0x5F1D36F1, 0xA54FF53A,
    0xADE682D1, 0x510E527F, 0x2B3E6C1F, 0x9B05688C, 0xFB41BD6B, 0x1F83D9AB, 0x137E2179, 0x5BE0CD19]);
  const SIGMA = new Uint8Array([
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3,
    11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4,
    7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8,
    9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13,
    2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9,
    12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11,
    13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10,
    6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5,
    10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0,
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3].map(x => x * 2));   // index en mots de 32 bits
  const v = new Int32Array(32), m = new Int32Array(32), S = -0x80000000;

  // Une étape G sur les mots a, b, c, d (index de paires), messages x et y. Tout en int32 (|0) : un
  // uint32 au-delà de 2^31 sortirait des petits entiers de V8 et coûterait ×3. Additions 64 bits :
  // bas = (p + q) | 0, retenue = bas < p en NON signé, comparé ici après inversion du bit de signe.
  function G(a, b, c, d, x, y){
    let al = v[a], ah = v[a + 1], bl = v[b], bh = v[b + 1], cl = v[c], ch = v[c + 1], dl = v[d], dh = v[d + 1], t, u;
    t = (al + bl) | 0; ah = (ah + bh + ((t ^ S) < (al ^ S) ? 1 : 0)) | 0; al = t;
    t = (al + m[x]) | 0; ah = (ah + m[x + 1] + ((t ^ S) < (al ^ S) ? 1 : 0)) | 0; al = t;
    t = dl ^ al; u = dh ^ ah; dl = u; dh = t;                                                       // rotr 32
    t = (cl + dl) | 0; ch = (ch + dh + ((t ^ S) < (cl ^ S) ? 1 : 0)) | 0; cl = t;
    t = bl ^ cl; u = bh ^ ch; bl = (t >>> 24) ^ (u << 8); bh = (u >>> 24) ^ (t << 8);               // rotr 24
    t = (al + bl) | 0; ah = (ah + bh + ((t ^ S) < (al ^ S) ? 1 : 0)) | 0; al = t;
    t = (al + m[y]) | 0; ah = (ah + m[y + 1] + ((t ^ S) < (al ^ S) ? 1 : 0)) | 0; al = t;
    t = dl ^ al; u = dh ^ ah; dl = (t >>> 16) ^ (u << 16); dh = (u >>> 16) ^ (t << 16);             // rotr 16
    t = (cl + dl) | 0; ch = (ch + dh + ((t ^ S) < (cl ^ S) ? 1 : 0)) | 0; cl = t;
    t = bl ^ cl; u = bh ^ ch; bl = (u >>> 31) ^ (t << 1); bh = (t >>> 31) ^ (u << 1);               // rotr 63
    v[a] = al; v[a + 1] = ah; v[b] = bl; v[b + 1] = bh; v[c] = cl; v[c + 1] = ch; v[d] = dl; v[d + 1] = dh;
  }
  function compress(ctx, buf, off, last){
    const h = ctx.h;
    for(let i = 0; i < 16; i++){ v[i] = h[i]; v[i + 16] = IV[i]; }
    v[24] ^= ctx.t % 0x100000000; v[25] ^= Math.floor(ctx.t / 0x100000000);
    if(last){ v[28] = ~v[28]; v[29] = ~v[29]; }
    for(let i = 0; i < 32; i++){
      const p = off + 4 * i;
      m[i] = buf[p] | (buf[p + 1] << 8) | (buf[p + 2] << 16) | (buf[p + 3] << 24);
    }
    for(let r = 0; r < 12; r++){
      const s = r * 16;
      G(0, 8, 16, 24, SIGMA[s], SIGMA[s + 1]);
      G(2, 10, 18, 26, SIGMA[s + 2], SIGMA[s + 3]);
      G(4, 12, 20, 28, SIGMA[s + 4], SIGMA[s + 5]);
      G(6, 14, 22, 30, SIGMA[s + 6], SIGMA[s + 7]);
      G(0, 10, 20, 30, SIGMA[s + 8], SIGMA[s + 9]);
      G(2, 12, 22, 24, SIGMA[s + 10], SIGMA[s + 11]);
      G(4, 14, 16, 26, SIGMA[s + 12], SIGMA[s + 13]);
      G(6, 8, 18, 28, SIGMA[s + 14], SIGMA[s + 15]);
    }
    for(let i = 0; i < 16; i++) h[i] = h[i] ^ v[i] ^ v[i + 16];
  }

  function create(outlen){
    outlen = outlen || 64;
    if(outlen < 1 || outlen > 64) throw new Error('blake2b : longueur de sortie 1 à 64');
    const ctx = {h:new Uint32Array(IV), b:new Uint8Array(128), c:0, t:0, outlen};
    ctx.h[0] ^= 0x01010000 ^ outlen;                    // paramètres : fanout 1, profondeur 1, sans clé
    return {
      update(input){
        const n = input.length;
        let i = 0;
        while(i < n){
          if(ctx.c === 128){ ctx.t += 128; compress(ctx, ctx.b, 0, false); ctx.c = 0; }
          // Blocs entiers lus en place (le dernier bloc reste en tampon : il peut être le final).
          if(ctx.c === 0 && n - i > 128){
            ctx.t += 128; compress(ctx, input, i, false); i += 128; continue;
          }
          const k = Math.min(128 - ctx.c, n - i);
          ctx.b.set(input.subarray(i, i + k), ctx.c);
          ctx.c += k; i += k;
        }
        return this;
      },
      digest(){
        ctx.t += ctx.c;
        ctx.b.fill(0, ctx.c);
        compress(ctx, ctx.b, 0, true);
        const out = new Uint8Array(ctx.outlen);
        for(let i = 0; i < ctx.outlen; i++) out[i] = ctx.h[i >> 2] >>> (8 * (i & 3));
        return out;
      },
    };
  }
  const hex = u8 => Array.from(u8, b => b.toString(16).padStart(2, '0')).join('');

  // Empreinte de la base des médias — MÊME calcul que media_scan.fingerprint (CHUNK = 4 Mio) :
  // taille en décimal, CHUNK premiers octets, puis (taille > CHUNK) les octets à partir de
  // max(CHUNK, taille − CHUNK) : entre 4 et 8 Mio, la queue reprend juste après la tête.
  const FP_CHUNK = 4 << 20;
  async function mediaFingerprint(file){
    const size = file.size, h = create(16);
    const bytes = async b => new Uint8Array(await (b.arrayBuffer ? b.arrayBuffer() : new Response(b).arrayBuffer()));
    h.update(new TextEncoder().encode(String(size)));
    h.update(await bytes(file.slice(0, Math.min(size, FP_CHUNK))));
    if(size > FP_CHUNK) h.update(await bytes(file.slice(Math.max(FP_CHUNK, size - FP_CHUNK), size)));
    return hex(h.digest());
  }

  const api = {create, hex, mediaFingerprint, FP_CHUNK};
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Blake2b = api;
})(typeof window !== 'undefined' ? window : this);
