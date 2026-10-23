// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Lecture du flowDef vidéo FOURNI par Python (bobimxl.build_flow_def) : c'est lui qui fait foi
// sur l'identité et le format du flux — l'élément n'en invente aucun champ.

use serde_json::Value;

/// Géométrie du flux PLANAR telle que la décrit le flowDef. Un plan = (octets par ligne, lignes).
#[derive(Debug, Clone)]
pub struct Geometry {
    pub flow_id: String,
    pub rate_num: i64,
    pub rate_den: i64,
    pub interlaced: bool,
    /// Premier champ = lignes impaires (bff). tff sinon.
    pub bff: bool,
    pub bit_depth: u32,
    /// Octets par échantillon : 1 en 8 bits, 2 au-delà (PLANAR{N}LE, petit-boutiste).
    pub bps: usize,
    pub planes: Vec<(usize, usize)>,
}

impl Geometry {
    pub fn from_json(s: &str) -> Result<Self, String> {
        let v: Value = serde_json::from_str(s).map_err(|e| format!("flowDef illisible : {e}"))?;
        let flow_id = v["id"].as_str().ok_or("flowDef sans `id`")?.to_string();
        let rate_num = v["grain_rate"]["numerator"].as_i64().ok_or("grain_rate.numerator")?;
        let rate_den = v["grain_rate"]["denominator"].as_i64().unwrap_or(1).max(1);
        let scan = v["interlace_mode"].as_str().unwrap_or("progressive");
        let comps = v["components"].as_array().ok_or("flowDef sans `components`")?;
        if comps.len() != 3 {
            return Err(format!("{} composantes, 3 attendues (planar Y/Cb/Cr)", comps.len()));
        }
        let bit_depth = comps[0]["bit_depth"].as_u64().unwrap_or(8) as u32;
        let bps = if bit_depth <= 8 { 1 } else { 2 };
        let planes = comps
            .iter()
            .map(|c| {
                let w = c["width"].as_u64().unwrap_or(0) as usize;
                let h = c["height"].as_u64().unwrap_or(0) as usize;
                (w * bps, h)
            })
            .collect::<Vec<_>>();
        if planes.iter().any(|&(w, h)| w == 0 || h == 0) {
            return Err("composante de taille nulle".into());
        }
        Ok(Self {
            flow_id,
            rate_num,
            rate_den,
            interlaced: scan.starts_with("interlaced"),
            bff: scan == "interlaced_bff",
            bit_depth,
            bps,
            planes,
        })
    }

    pub fn rate(&self) -> mxl::Rational {
        mxl::Rational { numerator: self.rate_num, denominator: self.rate_den }
    }

    pub fn frame_bytes(&self) -> usize {
        self.planes.iter().map(|&(w, h)| w * h).sum()
    }

    /// Une trame NOIRE planar : Y = 16 << (N−8), Cb/Cr = 1 << (N−1), petit-boutiste.
    pub fn black_frame(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.frame_bytes());
        for (i, &(w, h)) in self.planes.iter().enumerate() {
            let val: u32 = if i == 0 { 16 << (self.bit_depth - 8) } else { 1 << (self.bit_depth - 1) };
            let n = (w / self.bps) * h;
            if self.bps == 1 {
                out.extend(std::iter::repeat_n(val as u8, n));
            } else {
                let le = (val as u16).to_le_bytes();
                for _ in 0..n {
                    out.extend_from_slice(&le);
                }
            }
        }
        out
    }
}

/// Champs STRUCTURANTS d'un flowDef : s'ils diffèrent de ceux du flux existant, `mxlCreateFlowWriter`
/// RATTACHERAIT en silence l'ancien flowDef (sémantique d'attachement, pas de remplacement) et
/// l'aval lirait l'ancien format — même garde que bobimxl.Writer.
const STRUCTURAL: [&str; 10] = [
    "media_type", "interlace_mode", "frame_width", "frame_height", "grain_rate", "components",
    "slice_height", "sample_rate", "channel_count", "bit_depth",
];

/// Purge le répertoire du flux si son flowDef existant diverge sur un champ structurant.
/// Renvoie un message à journaliser quand une purge a eu lieu.
pub fn purge_if_stale(
    inst: &mxl::MxlInstance,
    domain: &str,
    flow_id: &str,
    new_def: &str,
) -> Option<String> {
    let old = inst.get_flow_def(flow_id).ok()?;
    let old: Value = serde_json::from_str(&old).ok()?;
    let new: Value = serde_json::from_str(new_def).ok()?;
    let changed: Vec<&str> = STRUCTURAL.iter().copied().filter(|k| old.get(*k) != new.get(*k)).collect();
    if changed.is_empty() {
        return None;
    }
    let dir = std::path::Path::new(domain).join(format!("{flow_id}.mxl-flow"));
    match std::fs::remove_dir_all(&dir) {
        Ok(()) => Some(format!("flux {flow_id} périmé ({}) : purgé avant création", changed.join(", "))),
        Err(e) => Some(format!("flux {flow_id} périmé ({}) : purge échouée ({e})", changed.join(", "))),
    }
}
