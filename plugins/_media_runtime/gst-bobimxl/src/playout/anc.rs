// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// ANC SMPTE 291 en grain RFC 8331 — portage OCTET POUR OCTET de bobimxl.anc_pack_rfc8331 et
// bobimxl.anc_atc_encode, qui font foi (relus par bobimxl.anc_unpack chez tous les consommateurs).
// Les deux pièges qui y sont documentés valent ici :
// - en-tête du grain = 6 octets (pas d'ESN), les paquets restent alignés sur 32 bits RELATIVEMENT
//   au corps (offsets 6, 10, 14…) — c'est ce qu'attend le parseur stock ;
// - ATC : UN chiffre BCD par quartet HAUT de chaque UDW pair (les impairs portent les drapeaux).

#[derive(Clone)]
pub struct Packet {
    pub did: u8,
    pub sdid: u8,
    pub line: u16,
    pub hori: u16,
    pub udw: Vec<u8>,
}

fn parity10(v: u8) -> u16 {
    let p = (v.count_ones() & 1) as u16;
    v as u16 | (p << 8) | ((p ^ 1) << 9)
}

fn checksum10(words: &[u16]) -> u16 {
    let s = words.iter().map(|w| (w & 0x1FF) as u32).sum::<u32>() & 0x1FF;
    (s as u16) | ((((s >> 8) & 1) ^ 1) as u16) << 9
}

struct BitWriter {
    acc: u64,
    n: u32,
    out: Vec<u8>,
}

impl BitWriter {
    fn write(&mut self, value: u32, bits: u32) {
        self.acc = (self.acc << bits) | (value as u64 & ((1u64 << bits) - 1));
        self.n += bits;
        while self.n >= 8 {
            self.n -= 8;
            self.out.push(((self.acc >> self.n) & 0xFF) as u8);
        }
        self.acc &= (1u64 << self.n) - 1;
    }

    fn align32(&mut self) {
        self.write(0, (8 - self.n) % 8);
        while self.out.len() % 4 != 0 {
            self.out.push(0);
        }
    }
}

/// Sérialise des paquets en grain RFC 8331 (F = 0 : progressif / indéfini).
pub fn pack_rfc8331(packets: &[Packet]) -> Vec<u8> {
    let mut w = BitWriter { acc: 0, n: 0, out: Vec::new() };
    for p in packets {
        let udw = &p.udw[..p.udw.len().min(255)];
        w.align32();
        w.write(0, 1); // C : luma
        w.write(p.line as u32 & 0x7FF, 11);
        w.write(p.hori as u32 & 0xFFF, 12);
        w.write(0, 1); // S
        w.write(0, 7); // StreamNum
        let mut words = vec![parity10(p.did), parity10(p.sdid), parity10(udw.len() as u8)];
        words.extend(udw.iter().map(|&u| parity10(u)));
        for &wd in &words {
            w.write(wd as u32, 10);
        }
        w.write(checksum10(&words) as u32, 10);
        w.align32();
    }
    let body = w.out;
    let mut out = vec![0u8; 6];
    out[0] = ((body.len() >> 8) & 0xFF) as u8;
    out[1] = (body.len() & 0xFF) as u8;
    out[2] = packets.len().min(255) as u8;
    out.extend_from_slice(&body);
    out
}

/// Les 16 UDW d'un paquet ATC (DID 0x60 / SDID 0x60) pour h:m:s:f.
pub fn atc_udw(h: u32, m: u32, s: u32, f: u32, drop_frame: bool) -> Vec<u8> {
    let mut q = [0u8; 16];
    q[0] = (f % 10) as u8;
    q[2] = (((f / 10) & 0x3) as u8) | if drop_frame { 0x4 } else { 0 };
    q[4] = (s % 10) as u8;
    q[6] = ((s / 10) & 0x7) as u8;
    q[8] = (m % 10) as u8;
    q[10] = ((m / 10) & 0x7) as u8;
    q[12] = (h % 10) as u8;
    q[14] = ((h / 10) & 0x3) as u8;
    q.iter().map(|&v| (v & 0x0F) << 4).collect()
}

/// Trames (à `fps` entier) → h, m, s, f, sur 24 h, non-drop.
pub fn frames_to_tc(total: i64, fps: u32) -> (u32, u32, u32, u32) {
    let fps = fps.max(1) as i64;
    let total = total.rem_euclid(24 * 3600 * fps);
    let f = total % fps;
    let secs = total / fps;
    ((secs / 3600) as u32, ((secs / 60) % 60) as u32, (secs % 60) as u32, f as u32)
}

/// Paquets supplémentaires fournis par Python (OP-47…) : JSON
/// `[{"did":67,"sdid":2,"line":20,"hori":4095,"udw":"<hex>"}]`.
pub fn parse_extra(json: &str) -> Vec<Packet> {
    let Ok(serde_json::Value::Array(items)) = serde_json::from_str::<serde_json::Value>(json) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|it| {
            let hex = it["udw"].as_str()?;
            let udw = (0..hex.len() / 2)
                .filter_map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).ok())
                .collect();
            Some(Packet {
                did: it["did"].as_u64()? as u8,
                sdid: it["sdid"].as_u64()? as u8,
                line: it["line"].as_u64().unwrap_or(9) as u16,
                hori: it["hori"].as_u64().unwrap_or(0xFFF) as u16,
                udw,
            })
        })
        .collect()
}
