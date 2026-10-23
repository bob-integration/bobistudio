// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// `bobiplayout` — sortie playout cadencée par la GRILLE TAI sur le bus MXL.
// Cf. docs/chantiers/PLAYER_NATIF.md. PHASE 1 : vidéo.
//
// Le principe, repris du player Python 0.25.1 où il a été mis au point au banc :
// - une boucle à elle (thread `grid`) écrit EXACTEMENT une trame par créneau de la grille, dans
//   TOUS les états — lecture, pause, chargement, fin : le signal ne s'interrompt jamais ;
// - la trame écrite est choisie par l'HORLOGE DE PRÉSENTATION media_time(G) = m0 + v·(G − g0),
//   ancrée sur la première trame disponible ; en pause, on répète la dernière ;
// - le décodeur court devant, ses trames attendent dans une file BORNÉE dont le dépassement
//   BLOQUE le pad (contre-pression) — jamais de trame jetée ;
// - l'élément n'est PAS un BaseSink : aucun préroll asynchrone, donc pas d'interblocage de mise
//   en pause (défaut corrigé dans le player 0.25.1).
//
// TÊTES DE LECTURE (decks). Le pipeline est PERMANENT ; un clip = une tête branchée sur les pads
// `video_<tête>` (et `audio_<tête>_<n>`, phase 2). `active-deck` est à l'antenne ; `next-deck`,
// ARMÉE, prend l'antenne d'elle-même, à la trame près, dès que l'active a fini (EOS + file vide) :
// boucle et enchaînement sans trame tenue.
//
// Choix qui engagent : libmxl chargée PAR SON NOM (variante glibc-hwcaps) ; AUCUN ramasse-miettes
// global de flux (il détruit les flux d'autres processus) ; flowDef fourni tel quel par Python.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::{Arc, Condvar, LazyLock, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use gst::glib;
use gst::subclass::prelude::*;
use gstreamer as gst;
use gstreamer_audio as gst_audio;
use gstreamer_video as gst_video;
use gst_video::prelude::*;

use super::anc;
use super::flowdef::{purge_if_stale, Geometry};

static CAT: LazyLock<gst::DebugCategory> = LazyLock::new(|| {
    gst::DebugCategory::new(
        "bobiplayout",
        gst::DebugColorFlags::empty(),
        Some("Sortie playout genlockée sur le bus MXL"),
    )
});

const LIBMXL: &str = "libmxl.so";

fn default_domain() -> String {
    std::env::var("MXL_DOMAIN").unwrap_or_else(|_| "/dev/shm/mxl".to_string())
}

// ─── Réglages (figés au passage READY → PAUSED) ─────────────────────────────────────────────
#[derive(Debug, Clone)]
struct Settings {
    domain: String,
    video_flow_def: String,
    /// flowDef JSON du flux AUDIO (bobimxl.build_audio_flow_def) ; vide = pas de sortie audio.
    audio_flow_def: String,
    /// flowDef JSON du flux MONITORING (2 canaux) ; vide = pas de sortie monitoring.
    monitor_flow_def: String,
    /// flowDefs JSON des deux flux ANC (bobimxl.build_data_flow_def) : TC clip, TC antenne.
    anc1_flow_def: String,
    anc2_flow_def: String,
    writer_options: String,
    /// Trames d'avance par tête (0 = une demi-seconde de la cadence du flux).
    queue_frames: u32,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            domain: default_domain(),
            video_flow_def: String::new(),
            audio_flow_def: String::new(),
            monitor_flow_def: String::new(),
            anc1_flow_def: String::new(),
            anc2_flow_def: String::new(),
            writer_options: String::new(),
            queue_frames: 0,
        }
    }
}

// ─── État partagé entre les pads (producteurs) et le thread de grille (consommateur) ─────────
struct Frame {
    /// Temps MÉDIA (stream time, s) de la trame.
    pts: f64,
    buf: gst::Buffer,
    info: gst_video::VideoInfo,
}

/// Une piste audio d'une tête : échantillons float32 ENTRELACÉS depuis `start` (temps média, en
/// échantillons à 48 kHz). Bornée : au-delà de AUDIO_MAX_S d'avance, le pad attend.
#[derive(Default)]
struct Track {
    channels: usize,
    rate: u32,
    segment: Option<gst::FormattedSegment<gst::ClockTime>>,
    /// Index média (échantillons) du premier échantillon de `data` ; None = rien reçu.
    start: Option<i64>,
    data: VecDeque<f32>,
    eos: bool,
    /// Vidage EN COURS sur CE pad (les événements de flush n'arrivent pas dans le même ordre sur
    /// les pads vidéo et audio : un drapeau commun pouvait rester levé et figer la tête).
    flushing: bool,
}

const AUDIO_RATE: u32 = 48_000;
/// Avance maximale d'une piste. Large devant la file vidéo (~0,5 s) : l'audio ne doit JAMAIS être
/// ce qui bloque le démuxeur (la vidéo attendrait alors l'audio, et réciproquement).
const AUDIO_MAX_S: i64 = 4;

#[derive(Default)]
struct Deck {
    /// Métadonnées fournies par Python (`deck-meta`) : TC embarqué du clip (trames), fin du clip
    /// (temps média, s ; négatif = inconnue) et durée de ce qui suit jusqu'à la cible du décompte.
    tc_base: i64,
    clip_end: f64,
    after_s: f64,
    tracks: BTreeMap<u32, Track>,
    frames: VecDeque<Frame>,
    eos: bool,
    flushing: bool,
    /// Tête retirée (pad vidéo relâché) ou grille arrêtée : tout pad de la tête rend Flushing.
    removed: bool,
    info: Option<gst_video::VideoInfo>,
    segment: Option<gst::FormattedSegment<gst::ClockTime>>,
    /// Incrémenté à chaque FLUSH_STOP : la grille ré-ancre l'horloge de présentation.
    epoch: u64,
}

/// Commande DATÉE (lecture synchrone de plusieurs players, cf. docs/reference/SYNC_PLAY.md) :
/// la grille l'applique au créneau dont l'index est celui de l'instant TAI `at` (arrondi au créneau
/// suivant), et ANCRE l'horloge de présentation sur CE créneau-là — même appliquée en retard
/// (commande arrivée après coup, tour de grille qui déborde), la sortie RATTRAPE sur la grille,
/// elle ne glisse jamais. Tous les players d'un groupe reçoivent la même (`at`, `m0`, `speed`) :
/// à grille commune, ils montrent la même image au même créneau, par construction.
#[derive(Clone, Debug)]
struct Sched {
    id: u64,
    at: u64,
    /// Tête à mettre à l'antenne à cet instant ; None = garder l'active (pause, vitesse, pas).
    deck: Option<String>,
    /// Temps média (s) de l'image à montrer au créneau `at`.
    m0: f64,
    /// Vitesse de l'horloge de présentation ; 0 = image FIGÉE à `m0` (pause, cue, pas à pas).
    speed: f64,
    /// Sortir du noir (cellule vide d'une ligne) au lieu de la dernière image.
    black: bool,
}

/// Trace d'une commande datée appliquée : `late_slots` = créneaux entre l'instant demandé et
/// l'application (0 = à l'heure). C'est le témoin d'un départ commun, pas une supposition.
#[derive(Clone, Copy, Debug, Default)]
struct Applied {
    id: u64,
    at: u64,
    k_at: u64,
    k: u64,
    late_slots: u64,
}

/// TÉMOIN du dernier créneau écrit : de quoi comparer des players entre eux sans rien supposer —
/// index de grille, instant TAI, temps de présentation, image RÉELLEMENT écrite (sa tête, son PTS)
/// et son retard en images sur l'horloge de présentation (décodeur en retard = image répétée).
#[derive(Clone, Debug, Default)]
struct Witness {
    k: u64,
    g: u64,
    mt: f64,
    pts: Option<f64>,
    shown_deck: Option<String>,
    deck: Option<String>,
    lag: i64,
    anchor: Option<(u64, f64, f64)>,
    fixed: bool,
    eos: bool,
    black: bool,
}

#[derive(Default, Clone, Copy)]
struct Stats {
    position: f64,
    playing: bool,
    eos: bool,
    fps: f64,
    fresh_fps: f64,
    slots_missed: u64,
    repeated: u64,
    switches: u64,
    format_errors: u64,
}

struct Ctl {
    decks: HashMap<String, Deck>,
    active: Option<String>,
    next: Option<String>,
    paused: bool,
    cue: bool,
    hold_black: bool,
    speed: f64,
    queue_max: usize,
    /// Octets par échantillon du flux de sortie (1 = 8 bits) : les pads réduisent le 10/12 bits.
    out_bps: usize,
    running: bool,
    stats: Stats,
    /// Routage : pour chaque sortie, n° du canal SOURCE (1-based, pistes mises bout à bout) ; 0 = silence.
    route: Vec<u32>,
    gain: Vec<f32>,
    mute: Vec<bool>,
    solo: Vec<bool>,
    /// Sortie MONITORING (2 canaux, flux à part) : canal SOURCE pour chaque côté (1-based, 0 =
    /// silence). Une écoute : ni gain, ni mute, ni solo — elle ne touche jamais l'antenne.
    mon_route: Vec<u32>,
    /// Crêtes par sortie (VU), avec retombée.
    levels: Vec<f32>,
    antenna_down: bool,
    antenna_frames: i64,
    anc_extra: Vec<anc::Packet>,
    tc_clip: String,
    tc_antenna: String,
    /// Commandes datées en attente (mode synchrone), dans l'ordre d'arrivée.
    sched: Vec<Sched>,
    /// Vitesse imposée par la dernière commande datée (0 = figée) ; None hors mode synchrone.
    sync_speed: Option<f64>,
    /// Sortie du mode synchrone demandée : la grille lâche son ancre fixe au créneau suivant.
    sync_release: bool,
    /// Dernières commandes datées appliquées (16 au plus).
    applied: VecDeque<Applied>,
    /// Créneaux écrits avec une image en RETARD sur l'horloge de présentation, sous ancre datée (cumul).
    sync_late_slots: u64,
    witness: Witness,
}

impl Default for Ctl {
    fn default() -> Self {
        Self {
            decks: HashMap::new(),
            active: None,
            next: None,
            paused: false,
            cue: false,
            hold_black: false,
            speed: 1.0,
            queue_max: 12,
            out_bps: 0,
            running: false,
            stats: Stats::default(),
            route: (1..=8).collect(),
            gain: vec![1.0; 8],
            mute: vec![false; 8],
            solo: vec![false; 8],
            mon_route: vec![1, 2],
            levels: vec![0.0; 8],
            antenna_down: false,
            antenna_frames: 0,
            anc_extra: Vec::new(),
            tc_clip: String::new(),
            tc_antenna: String::new(),
            sched: Vec::new(),
            sync_speed: None,
            sync_release: false,
            applied: VecDeque::new(),
            sync_late_slots: 0,
            witness: Witness::default(),
        }
    }
}

#[derive(Default)]
struct Shared {
    ctl: Mutex<Ctl>,
    /// Réveille les pads bloqués sur une file pleine (place libérée, flush, arrêt).
    space: Condvar,
}

pub struct BobiPlayout {
    settings: Mutex<Settings>,
    shared: Arc<Shared>,
    instance: Mutex<Option<mxl::MxlInstance>>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

fn deck_of(pad: &gst::Pad) -> String {
    let n = pad.name();
    let rest = n.split_once('_').map(|x| x.1).unwrap_or("");
    // video_<tête> ; audio_<tête>_<n>
    if n.starts_with("audio_") {
        rest.rsplit_once('_').map(|x| x.0).unwrap_or(rest).to_string()
    } else {
        rest.to_string()
    }
}

/// Vue f32 d'un tampon audio F32LE (x86 : petit-boutiste natif). Octets en trop ignorés.
fn bytemuck_cast(b: &[u8]) -> &[f32] {
    let n = b.len() / 4;
    if n == 0 || (b.as_ptr() as usize) % std::mem::align_of::<f32>() != 0 {
        return &[];
    }
    unsafe { std::slice::from_raw_parts(b.as_ptr() as *const f32, n) }
}

fn track_of(pad: &gst::Pad) -> u32 {
    pad.name().rsplit_once('_').and_then(|x| x.1.parse().ok()).unwrap_or(0)
}

fn parse_list<T: std::str::FromStr>(s: &str, n: usize, default: T) -> Vec<T>
where
    T: Clone,
{
    let mut v: Vec<T> = s.split([',', ';']).map(|x| x.trim()).filter(|x| !x.is_empty())
        .map(|x| x.parse().unwrap_or(default.clone())).collect();
    v.resize(n, default);
    v.truncate(n);
    v
}

// ─── Pads ────────────────────────────────────────────────────────────────────────────────────
impl BobiPlayout {
    fn video_chain(&self, pad: &gst::Pad, buffer: gst::Buffer) -> Result<gst::FlowSuccess, gst::FlowError> {
        let deck = deck_of(pad);
        let mut ctl = self.shared.ctl.lock().unwrap();
        loop {
            let qmax = ctl.queue_max;
            let running = ctl.running;
            let Some(d) = ctl.decks.get_mut(&deck) else {
                return Err(gst::FlowError::NotNegotiated);   // pas de caps reçues, ou tête retirée
            };
            if d.flushing || d.removed {
                return Err(gst::FlowError::Flushing);
            }
            if !running {
                // Pas encore (ou plus) de grille : on garde la trame, sans bloquer au-delà.
                if d.frames.len() >= qmax {
                    return Err(gst::FlowError::Flushing);
                }
                break;
            }
            if d.frames.len() < qmax {
                break;
            }
            ctl = self.shared.space.wait(ctl).unwrap();
        }
        let d = ctl.decks.get_mut(&deck).unwrap();
        let Some(info) = d.info.clone() else {
            return Err(gst::FlowError::NotNegotiated);
        };
        let Some(pts) = buffer.pts() else {
            gst::warning!(CAT, obj = pad, "tampon sans PTS ignoré");
            return Ok(gst::FlowSuccess::Ok);
        };
        let st = d.segment.as_ref().and_then(|s| s.to_stream_time(pts)).unwrap_or(pts);
        // Trame 10/12 bits vers un flux 8 bits : réduite ICI, dans le fil de la tête (priorité
        // abaissée, en parallèle de la grille) et HORS du verrou. Elle remplace un videoconvert
        // qui coûtait 1,3 cœur par tête en UHD 4:2:2 (le pipeline est en 8 bits par défaut,
        // `mxl_pipeline_bit_depth=force8`). Faite dans le fil de GRILLE, elle le mettait en retard
        // sur 3 cœurs saturés (71 créneaux manqués en 12 s en DNxHR UHD, mesuré le 2026-10-11).
        // Les trames en file pèsent en plus moitié moins.
        let (buf, info) = if ctl.out_bps == 1 && info.format_info().depth()[0] > 8 {
            drop(ctl);
            let r = narrow_frame(&buffer, &info);
            ctl = self.shared.ctl.lock().unwrap();
            match r {
                Some(x) => x,
                None => (buffer, info),
            }
        } else {
            (buffer, info)
        };
        let Some(d) = ctl.decks.get_mut(&deck) else {
            return Err(gst::FlowError::Flushing);       // tête retirée pendant la réduction
        };
        if d.flushing || d.removed {
            return Err(gst::FlowError::Flushing);
        }
        d.frames.push_back(Frame { pts: st.nseconds() as f64 / 1e9, buf, info });
        Ok(gst::FlowSuccess::Ok)
    }

    fn audio_chain(&self, pad: &gst::Pad, buffer: gst::Buffer) -> Result<gst::FlowSuccess, gst::FlowError> {
        let (deck, n) = (deck_of(pad), track_of(pad));
        let map = buffer.map_readable().map_err(|_| gst::FlowError::Error)?;
        let samples: &[f32] = bytemuck_cast(map.as_slice());
        let mut ctl = self.shared.ctl.lock().unwrap();
        loop {
            let running = ctl.running;
            let Some(d) = ctl.decks.get_mut(&deck) else {
                return Err(gst::FlowError::NotNegotiated);
            };
            if d.removed {
                return Err(gst::FlowError::Flushing);
            }
            let Some(t) = d.tracks.get_mut(&n) else {
                return Err(gst::FlowError::NotNegotiated);
            };
            if t.flushing {
                return Err(gst::FlowError::Flushing);
            }
            if t.channels == 0 {
                return Err(gst::FlowError::NotNegotiated);
            }
            let cap = AUDIO_MAX_S as usize * AUDIO_RATE as usize * t.channels;
            if !running || t.data.len() < cap {
                if !running && t.data.len() >= cap {
                    return Err(gst::FlowError::Flushing);
                }
                break;
            }
            ctl = self.shared.space.wait(ctl).unwrap();
        }
        let t = ctl.decks.get_mut(&deck).unwrap().tracks.get_mut(&n).unwrap();
        if t.start.is_none() {
            if let Some(pts) = buffer.pts() {
                let st = t.segment.as_ref().and_then(|s| s.to_stream_time(pts)).unwrap_or(pts);
                t.start = Some((st.nseconds() as i128 * AUDIO_RATE as i128 / 1_000_000_000) as i64);
            } else {
                return Ok(gst::FlowSuccess::Ok);
            }
        }
        t.data.extend(samples.iter().copied());
        Ok(gst::FlowSuccess::Ok)
    }

    fn sink_event(&self, pad: &gst::Pad, event: gst::Event) -> bool {
        let deck = deck_of(pad);
        let is_video = pad.name().starts_with("video_");
        use gst::EventView;
        match event.view() {
            EventView::Caps(c) if is_video => match gst_video::VideoInfo::from_caps(c.caps()) {
                Ok(info) => {
                    let mut ctl = self.shared.ctl.lock().unwrap();
                    ctl.decks.entry(deck).or_default().info = Some(info);
                    true
                }
                Err(_) => false,
            },
            EventView::Segment(s) if is_video => {
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().segment =
                    s.segment().downcast_ref::<gst::ClockTime>().cloned();
                true
            }
            EventView::FlushStart(_) if is_video => {
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().flushing = true;
                self.shared.space.notify_all();
                true
            }
            EventView::FlushStop(_) if is_video => {
                let mut ctl = self.shared.ctl.lock().unwrap();
                let d = ctl.decks.entry(deck.clone()).or_default();
                d.frames.clear();
                d.flushing = false;
                d.eos = false;
                d.epoch += 1;
                if ctl.active.as_deref() == Some(deck.as_str()) {
                    ctl.stats.eos = false;
                }
                true
            }
            EventView::Eos(_) if is_video => {
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().eos = true;
                true
            }
            EventView::Caps(c) => match gst_audio::AudioInfo::from_caps(c.caps()) {
                Ok(info) => {
                    if info.rate() != AUDIO_RATE || info.format() != gst_audio::AudioFormat::F32le {
                        gst::error!(CAT, obj = pad, "audio attendu en F32LE 48 kHz, reçu {:?} {} Hz", info.format(), info.rate());
                        return false;
                    }
                    let n = track_of(pad);
                    let mut ctl = self.shared.ctl.lock().unwrap();
                    let t = ctl.decks.entry(deck).or_default().tracks.entry(n).or_default();
                    t.channels = info.channels() as usize;
                    t.rate = info.rate();
                    true
                }
                Err(_) => false,
            },
            EventView::Segment(s) => {
                let n = track_of(pad);
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().tracks.entry(n).or_default().segment =
                    s.segment().downcast_ref::<gst::ClockTime>().cloned();
                true
            }
            EventView::FlushStart(_) => {
                let n = track_of(pad);
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().tracks.entry(n).or_default().flushing = true;
                self.shared.space.notify_all();
                true
            }
            EventView::FlushStop(_) => {
                let n = track_of(pad);
                let mut ctl = self.shared.ctl.lock().unwrap();
                let t = ctl.decks.entry(deck).or_default().tracks.entry(n).or_default();
                t.data.clear();
                t.start = None;
                t.eos = false;
                t.flushing = false;
                true
            }
            EventView::Eos(_) => {
                let n = track_of(pad);
                let mut ctl = self.shared.ctl.lock().unwrap();
                ctl.decks.entry(deck).or_default().tracks.entry(n).or_default().eos = true;
                true
            }
            // Puits : rien à relayer en aval.
            _ => true,
        }
    }

    fn make_sink_pad(&self, templ: &gst::PadTemplate, name: &str) -> gst::Pad {
        let is_video = name.starts_with("video_");
        let b = gst::Pad::builder_from_template(templ).name(name);
        let b = if is_video {
            b.chain_function(|pad, parent, buffer| {
                BobiPlayout::catch_panic_pad_function(parent, || Err(gst::FlowError::Error), |this| {
                    this.video_chain(pad, buffer)
                })
            })
        } else {
            b.chain_function(|pad, parent, buffer| {
                BobiPlayout::catch_panic_pad_function(parent, || Err(gst::FlowError::Error), |this| {
                    this.audio_chain(pad, buffer)
                })
            })
        };
        b.event_function(|pad, parent, event| {
            BobiPlayout::catch_panic_pad_function(parent, || false, |this| this.sink_event(pad, event))
        })
        .build()
    }
}

// ─── Cycle de vie ────────────────────────────────────────────────────────────────────────────
impl BobiPlayout {
    fn open_instance(&self) -> Result<(), gst::StateChangeError> {
        let domain = self.settings.lock().unwrap().domain.clone();
        let api = mxl::load_api(LIBMXL).map_err(|e| {
            gst::element_imp_error!(self, gst::CoreError::Failed, ["libmxl introuvable : {}", e]);
            gst::StateChangeError
        })?;
        let inst = mxl::MxlInstance::new(api, &domain, "").map_err(|e| {
            gst::element_imp_error!(self, gst::CoreError::Failed, ["instance MXL impossible sur {} : {}", domain, e]);
            gst::StateChangeError
        })?;
        gst::info!(CAT, imp = self, "instance MXL ouverte sur {}", domain);
        *self.instance.lock().unwrap() = Some(inst);
        Ok(())
    }

    fn start_grid(&self) -> Result<(), gst::StateChangeError> {
        let s = self.settings.lock().unwrap().clone();
        let inst = self.instance.lock().unwrap().clone().ok_or(gst::StateChangeError)?;
        let geo = Geometry::from_json(&s.video_flow_def).map_err(|e| {
            gst::element_imp_error!(self, gst::ResourceError::Settings, ["video-flow-def : {}", e]);
            gst::StateChangeError
        })?;
        if let Some(msg) = purge_if_stale(&inst, &s.domain, &geo.flow_id, &s.video_flow_def) {
            gst::warning!(CAT, imp = self, "{}", msg);
        }
        let opts = (!s.writer_options.is_empty()).then_some(s.writer_options.as_str());
        let (fw, _info, created) = inst.create_flow_writer(&s.video_flow_def, opts).map_err(|e| {
            gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["flux vidéo {} : {}", geo.flow_id, e]);
            gst::StateChangeError
        })?;
        let writer = fw.to_grain_writer().map_err(|e| {
            gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["écrivain de grains : {}", e]);
            gst::StateChangeError
        })?;
        let open_audio = |def: &str, what: &str| -> Result<Option<(mxl::SamplesWriter, usize)>, gst::StateChangeError> {
            if def.is_empty() {
                return Ok(None);
            }
            let v: serde_json::Value = serde_json::from_str(def).map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::Settings, ["{}-flow-def : {}", what, e]);
                gst::StateChangeError
            })?;
            let afid = v["id"].as_str().unwrap_or("").to_string();
            let ch = v["channel_count"].as_u64().unwrap_or(8) as usize;
            if let Some(msg) = purge_if_stale(&inst, &s.domain, &afid, def) {
                gst::warning!(CAT, imp = self, "{}", msg);
            }
            let (afw, _i, _c) = inst.create_flow_writer(def, opts).map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["flux {} {} : {}", what, afid, e]);
                gst::StateChangeError
            })?;
            let sw = afw.to_samples_writer().map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["écrivain d'échantillons : {}", e]);
                gst::StateChangeError
            })?;
            gst::info!(CAT, imp = self, "flux {} {} : {} canaux float32 à 48 kHz", what, afid, ch);
            Ok(Some((sw, ch)))
        };
        let audio = open_audio(&s.audio_flow_def, "audio")?;
        // Le monitoring suit le bloc audio de l'antenne : sans sortie audio, pas de monitoring.
        let monitor = if audio.is_some() { open_audio(&s.monitor_flow_def, "monitoring")? } else { None };
        let mut anc_w = Vec::new();
        for def in [&s.anc1_flow_def, &s.anc2_flow_def] {
            if def.is_empty() {
                continue;
            }
            let id = serde_json::from_str::<serde_json::Value>(def).ok()
                .and_then(|v| v["id"].as_str().map(str::to_string)).unwrap_or_default();
            if let Some(msg) = purge_if_stale(&inst, &s.domain, &id, def) {
                gst::warning!(CAT, imp = self, "{}", msg);
            }
            let (fw, _i, _c) = inst.create_flow_writer(def, opts).map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["flux ANC {} : {}", id, e]);
                gst::StateChangeError
            })?;
            anc_w.push(fw.to_grain_writer().map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::OpenWrite, ["écrivain ANC : {}", e]);
                gst::StateChangeError
            })?);
        }
        gst::info!(CAT, imp = self, "flux vidéo {} {} ({} i/s, {})", geo.flow_id,
            if created { "créé" } else { "rattaché" },
            geo.rate_num as f64 / geo.rate_den as f64,
            if geo.interlaced { if geo.bff { "entrelacé bff" } else { "entrelacé tff" } } else { "progressif" });
        {
            let mut ctl = self.shared.ctl.lock().unwrap();
            let fps = (geo.rate_num as f64 / geo.rate_den as f64).round() as usize;
            ctl.queue_max = if s.queue_frames > 0 { s.queue_frames as usize } else { (fps / 2).max(4) };
            ctl.out_bps = geo.bps;
            ctl.running = true;
        }
        let shared = self.shared.clone();
        let obj = self.obj().clone();
        let h = std::thread::Builder::new()
            .name("bobiplayout-grille".into())
            .spawn(move || grid_loop(obj, shared, inst, writer, geo, audio, monitor, anc_w))
            .map_err(|_| gst::StateChangeError)?;
        *self.thread.lock().unwrap() = Some(h);
        Ok(())
    }

    fn stop_grid(&self) {
        {
            let mut ctl = self.shared.ctl.lock().unwrap();
            ctl.running = false;
            for d in ctl.decks.values_mut() {
                d.removed = true;
            }
        }
        self.shared.space.notify_all();
        if let Some(h) = self.thread.lock().unwrap().take() {
            let _ = h.join();
        }
        let mut ctl = self.shared.ctl.lock().unwrap();
        ctl.decks.clear();
    }

    fn close_instance(&self) {
        if let Some(inst) = self.instance.lock().unwrap().take() {
            if let Err(e) = inst.destroy() {
                gst::warning!(CAT, imp = self, "fermeture de l'instance MXL : {}", e);
            }
        }
    }
}

// ─── Boucle-grille ───────────────────────────────────────────────────────────────────────────
enum Src<'a> {
    Frame(&'a Frame),
    Black,
}

/// Copie la trame (ou le noir) dans les grains de l'index de TRAME `k` : un grain en progressif,
/// deux grains-champs (2k, 2k+1) en entrelacé. Renvoie false si la trame ne correspond pas à la
/// géométrie du flux (on écrit alors du noir plutôt qu'une image déformée).
/// Réduction 10/12 bits → 8 bits d'une ligne : échantillons 16 bits petit-boutistes (valeur sur
/// `shift` + 8 bits) → octets, arrondis. Lue en u16 (x86 est petit-boutiste) pour que le
/// compilateur la vectorise ; repli octet par octet si la ligne n'est pas alignée.
#[inline]
fn narrow_row(dst: &mut [u8], src: &[u8], shift: u32) {
    let half = 1u16 << (shift - 1);
    let n = dst.len().min(src.len() / 2);
    if (src.as_ptr() as usize) % 2 == 0 {
        // SAFETY : `src` est aligné sur 2 et contient au moins `n` u16.
        let s16 = unsafe { std::slice::from_raw_parts(src.as_ptr() as *const u16, n) };
        for (d, &v) in dst[..n].iter_mut().zip(s16) {
            *d = (v.wrapping_add(half) >> shift).min(255) as u8;
        }
    } else {
        for (d, s) in dst[..n].iter_mut().zip(src.chunks_exact(2)) {
            *d = (u16::from_le_bytes([s[0], s[1]]).wrapping_add(half) >> shift).min(255) as u8;
        }
    }
}

/// Fils de la réduction d'une trame : un seul jusqu'au 1080 (≈ 1 ms), plusieurs en UHD — sinon
/// décodeur et réduction s'attendaient l'un l'autre (DNxHR UHD : 32 images neuves/s au lieu de
/// 46 avec un seul fil, mesuré le 2026-10-11).
fn narrow_threads(pixels: usize) -> usize {
    if pixels <= 1920 * 1080 {
        return 1;
    }
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(2).clamp(1, 4)
}

/// Trame 10/12 bits → la même en 8 bits (chroma et dimensions inchangées), pour un flux 8 bits.
/// None si le format n'est pas un planaire YUV profond connu.
fn narrow_frame(buf: &gst::Buffer, info: &gst_video::VideoInfo) -> Option<(gst::Buffer, gst_video::VideoInfo)> {
    use gst_video::VideoFormat as F;
    let fmt8 = match info.format() {
        F::I42210le | F::I42212le => F::Y42b,
        F::I42010le | F::I42012le => F::I420,
        F::Y44410le | F::Y44412le => F::Y444,
        _ => return None,
    };
    let shift = info.format_info().depth()[0].checked_sub(8).filter(|s| *s > 0)?;
    let oinfo = gst_video::VideoInfo::builder(fmt8, info.width(), info.height())
        .interlace_mode(info.interlace_mode())
        .fps(info.fps())
        .par(info.par())
        .colorimetry(&info.colorimetry())
        .build()
        .ok()?;
    let inf = gst_video::VideoFrameRef::from_buffer_ref_readable(buf.as_ref(), info).ok()?;
    let mut out = gst::Buffer::with_size(oinfo.size()).ok()?;
    {
        let ob = out.get_mut()?;
        ob.set_pts(buf.pts());
        ob.set_duration(buf.duration());
        let mut of = gst_video::VideoFrameRef::from_buffer_ref_writable(ob, &oinfo).ok()?;
        let nt = narrow_threads(info.width() as usize * info.height() as usize);
        for p in 0..oinfo.n_planes() {
            let w = oinfo.comp_width(p as u8) as usize;
            let h = oinfo.comp_height(p as u8) as usize;
            let (si, so) = (inf.plane_stride()[p as usize] as usize, of.plane_stride()[p as usize] as usize);
            let src = inf.plane_data(p).ok()?;
            let dst = of.plane_data_mut(p).ok()?;
            if src.len() < si * (h - 1) + 2 * w || dst.len() < so * (h - 1) + w {
                return None;
            }
            // Bandes de lignes, une par fil (les tranches de `dst` sont disjointes).
            let band = h.div_ceil(nt);
            std::thread::scope(|sc| {
                for (b, chunk) in dst.chunks_mut(band * so).enumerate() {
                    let r0 = b * band;
                    sc.spawn(move || {
                        for (i, line) in chunk.chunks_mut(so).enumerate() {
                            let r = r0 + i;
                            if r >= h || line.len() < w {
                                break;
                            }
                            narrow_row(&mut line[..w], &src[r * si..r * si + 2 * w], shift);
                        }
                    });
                }
            });
        }
    }
    Some((out, oinfo))
}

fn write_frame(w: &mxl::GrainWriter, geo: &Geometry, k: u64, src: Src, black: &[u8]) -> bool {
    let mapped;
    let mut planes: Vec<(&[u8], usize)> = Vec::with_capacity(3);
    let mut ok = true;
    match src {
        Src::Frame(f) => {
            // Profondeur différente du flux (trame non réduite) : noir plutôt qu'une image fausse.
            let src_bps = if f.info.format_info().depth()[0] > 8 { 2 } else { 1 };
            if src_bps != geo.bps {
                ok = false;
            }
            mapped = gst_video::VideoFrameRef::from_buffer_ref_readable(f.buf.as_ref(), &f.info).ok();
            match &mapped {
                Some(vf) if ok && vf.n_planes() as usize == geo.planes.len() => {
                    for (p, &(row, rows)) in geo.planes.iter().enumerate() {
                        let stride = vf.plane_stride()[p] as usize;
                        match vf.plane_data(p as u32) {
                            Ok(d) if stride >= row && d.len() >= stride * (rows - 1) + row => planes.push((d, stride)),
                            _ => ok = false,
                        }
                    }
                }
                _ => ok = false,
            }
        }
        Src::Black => {}
    }
    if !ok || planes.is_empty() {
        planes.clear();
        let mut off = 0;
        for &(row, rows) in &geo.planes {
            planes.push((&black[off..off + row * rows], row));
            off += row * rows;
        }
    }
    let fields: &[(u64, usize)] = if geo.interlaced {
        let first = if geo.bff { 1 } else { 0 };
        &[(2 * k, first), (2 * k + 1, 1 - first)]
    } else {
        &[(k, usize::MAX)]
    };
    for &(idx, parity) in fields {
        let Ok(mut g) = w.open_grain(idx) else { continue };
        let total = g.total_slices();
        let payload = g.payload_mut();
        let mut off = 0usize;
        for (p, &(row, rows)) in geo.planes.iter().enumerate() {
            let (data, stride) = planes[p];
            let (start, step, n) = if parity == usize::MAX { (0, 1, rows) } else { (parity, 2, (rows - parity).div_ceil(2)) };
            for i in 0..n {
                let r = start + i * step;
                if off + row > payload.len() {
                    break;
                }
                payload[off..off + row].copy_from_slice(&data[r * stride..r * stride + row]);
                off += row;
            }
        }
        let _ = g.commit(total);
    }
    ok
}

/// Bloc [start, start+n) d'un flux audio : `buf` est planaire (canal c = buf[c*n..(c+1)*n]).
fn write_samples(w: &mxl::SamplesWriter, ch: usize, buf: &[f32], start: u64, n: usize) -> Result<(), String> {
    let mut acc = w.open_samples(start + n as u64, n).map_err(|e| e.to_string())?;     // index ONE-PAST-THE-END
    for c in 0..ch.min(acc.channels()) {
        if let Ok((f1, f2)) = acc.channel_data_mut(c) {
            let src = &buf[c * n..(c + 1) * n];
            let n1 = (f1.len() / 4).min(n);
            for (i, x) in src[..n1].iter().enumerate() {
                f1[i * 4..i * 4 + 4].copy_from_slice(&x.to_le_bytes());
            }
            for (i, x) in src[n1..].iter().enumerate() {
                if i * 4 + 4 <= f2.len() {
                    f2[i * 4..i * 4 + 4].copy_from_slice(&x.to_le_bytes());
                }
            }
        }
    }
    let _ = acc.commit();
    Ok(())
}

fn grid_loop(
    obj: super::BobiPlayout,
    shared: Arc<Shared>,
    inst: mxl::MxlInstance,
    writer: mxl::GrainWriter,
    geo: Geometry,
    audio: Option<(mxl::SamplesWriter, usize)>,
    monitor: Option<(mxl::SamplesWriter, usize)>,
    anc_w: Vec<mxl::GrainWriter>,
) {
    let tc_fps = (geo.rate_num as f64 / geo.rate_den.max(1) as f64).round() as u32;
    let rate = geo.rate();
    let a_rate = mxl::Rational { numerator: AUDIO_RATE as i64, denominator: 1 };
    // Index (one-past-the-end) du dernier échantillon audio écrit, et curseur MÉDIA (échantillons)
    // de la prochaine lecture dans les pistes. Le curseur avance d'exactement n par bloc : jamais
    // d'arrondi d'un créneau à l'autre (un échantillon de trop ou de moins = un clic).
    let mut a_last_end: Option<u64> = None;
    let mut a_cursor: Option<i64> = None;
    // Clé du curseur audio : (tête, g0 de l'ancrage). Le curseur n'est recalé que si elle change.
    let mut a_key: Option<(String, u64)> = None;
    let a_channels = audio.as_ref().map(|a| a.1).unwrap_or(0);
    let decay = 0.6f32.powf((geo.rate_den as f32 / geo.rate_num.max(1) as f32) / 0.08);
    let period_s = geo.rate_den as f64 / geo.rate_num.max(1) as f64;
    let black = geo.black_frame();
    let mut last: Option<Frame> = None;
    let mut last_k: Option<u64> = None;
    let mut cur_deck: Option<String> = None;
    let mut cur_epoch: u64 = 0;
    // Horloge de présentation : (g0 ns TAI, m0 s média, vitesse).
    let mut anchor: Option<(u64, f64, f64)> = None;
    let mut pending_m0: Option<f64> = None;
    // Ancre posée par une commande DATÉE : elle survit à un flush de la tête (elle n'est lâchée
    // qu'à un changement de tête venu d'ailleurs, ou à la sortie du mode synchrone).
    let mut anchor_fixed = false;
    // Tête d'où vient l'image écrite (`last`) : après une bascule, l'ancienne image reste montrée
    // tant que la nouvelle tête n'a rien livré — le témoin doit le dire.
    let mut last_deck: Option<String> = None;
    let mut cur_pos = 0.0f64;
    let (mut win_t0, mut win_w, mut win_fresh) = (0u64, 0u64, 0u64);
    let mut missed = 0u64;

    loop {
        // Index de la PROCHAINE trame et sommeil jusqu'à son échéance. Un tour qui déborde saute
        // des créneaux : ils sont COMPTÉS (aucun autre compteur ne le verrait).
        let now_idx = inst.get_current_index(&rate);
        let mut k = now_idx + 1;
        if let Some(l) = last_k {
            if k <= l {
                k = l + 1;
            } else if k > l + 1 {
                missed += k - l - 1;
            }
        }
        last_k = Some(k);
        let g = inst.index_to_timestamp(k, &rate).unwrap_or(0);
        let now = inst.get_time();
        if g > now {
            std::thread::sleep(Duration::from_nanos(g - now));
        }

        // Bloc audio de CE créneau : échantillons TAI [g, g_suivant), contigu au précédent.
        let (a_start, a_n) = if a_channels > 0 {
            let s0 = inst.timestamp_to_index(g, &a_rate).unwrap_or(0);
            let g1 = inst.index_to_timestamp(k + 1, &rate).unwrap_or(g);
            let s1 = inst.timestamp_to_index(g1, &a_rate).unwrap_or(s0);
            let start = match a_last_end {
                Some(e) if e <= s0 && s0 - e < AUDIO_RATE as u64 => e,     // comble un créneau manqué
                Some(e) if e > s0 && e < s1 => e,                           // déjà en partie écrit
                _ => s0,
            };
            (start, s1.saturating_sub(start) as usize)
        } else {
            (0, 0)
        };
        let mut a_out: Vec<f32> = vec![0.0; a_n * a_channels];
        let m_channels = monitor.as_ref().map(|m| m.1).unwrap_or(0);
        let mut m_out: Vec<f32> = vec![0.0; a_n * m_channels];

        let mut fresh = false;
        let mut active_now = false;
        let hold_black;
        // Sorties en SOLO à ce créneau (lues sous le verrou) : elles pilotent le monitoring.
        let solo_now: Vec<usize>;
        let tc_clip_f: i64;
        let tc_ant_f: i64;
        let anc_extra: Vec<anc::Packet>;
        {
            let mut ctl = shared.ctl.lock().unwrap();
            if !ctl.running {
                break;
            }
            if ctl.sync_release {
                ctl.sync_release = false;
                anchor_fixed = false;
            }
            // Changement de tête demandé de l'extérieur, ou flush de la tête courante → ré-ancrage
            // (sauf ancre DATÉE : un flush ne la déplace pas, seul un changement de tête le fait).
            let epoch = cur_deck.as_ref().and_then(|n| ctl.decks.get(n)).map(|d| d.epoch).unwrap_or(0);
            let deck_changed = ctl.active != cur_deck;
            if deck_changed || epoch != cur_epoch {
                cur_deck = ctl.active.clone();
                cur_epoch = cur_deck.as_ref().and_then(|n| ctl.decks.get(n)).map(|d| d.epoch).unwrap_or(0);
                if deck_changed || !anchor_fixed {
                    anchor = None;
                    pending_m0 = None;
                    anchor_fixed = false;
                }
                ctl.stats.eos = false;
            }
            // ── Commandes DATÉES dues à ce créneau (mode synchrone) ──────────────────────────
            // L'instant demandé est ramené à SON créneau (index de grille de `at`, arrondi au
            // suivant) : deux players de même cadence en tirent le même index, donc la même ancre.
            if !ctl.sched.is_empty() {
                let mut due: Vec<Sched> = Vec::new();
                ctl.sched.retain(|s| {
                    if s.at <= g { due.push(s.clone()); false } else { true }
                });
                due.sort_by_key(|s| s.at);
                for s in due {
                    let mut k_at = inst.timestamp_to_index(s.at, &rate).unwrap_or(k);
                    if inst.index_to_timestamp(k_at, &rate).unwrap_or(0) < s.at {
                        k_at += 1;
                    }
                    let k_at = k_at.min(k);
                    let g_at = inst.index_to_timestamp(k_at, &rate).unwrap_or(g);
                    if s.black {
                        ctl.active = None;            // cellule vide de la ligne : noir
                        ctl.next = None;
                    } else if let Some(dk) = s.deck.clone() {
                        ctl.active = Some(dk);
                        ctl.next = None;
                    }
                    ctl.paused = false;
                    ctl.cue = false;
                    ctl.hold_black = s.black;
                    ctl.sync_speed = Some(s.speed);
                    ctl.stats.eos = false;
                    cur_deck = ctl.active.clone();
                    cur_epoch = cur_deck.as_ref().and_then(|n| ctl.decks.get(n)).map(|d| d.epoch).unwrap_or(0);
                    anchor = Some((g_at, s.m0, s.speed));
                    pending_m0 = None;
                    anchor_fixed = true;
                    let late = k - k_at;
                    if late > 0 {
                        gst::warning!(CAT, obj = obj, "commande datée {} appliquée avec {} créneau(x) de retard (rattrapé sur la grille)", s.id, late);
                    }
                    ctl.applied.push_back(Applied { id: s.id, at: s.at, k_at, k, late_slots: late });
                    while ctl.applied.len() > 16 {
                        ctl.applied.pop_front();
                    }
                }
            }
            let paused = ctl.paused;
            let speed = ctl.sync_speed.unwrap_or(ctl.speed);
            // Tête dont la trame est MONTRÉE à ce créneau — c'est elle qui fournit l'audio. Au créneau
            // d'une bascule qui montre encore la dernière trame de l'ancienne tête, c'est l'ancienne.
            let mut shown_deck: Option<String> = cur_deck.clone();
            let mut shown_key: Option<(String, u64)> = cur_deck.clone().zip(anchor.map(|a| a.0));
            if let (Some(name), false) = (cur_deck.clone(), paused) {
                let mut name = name;
                // Au plus deux passes : la seconde présente, DANS CE CRÉNEAU, la 1re trame de la
                // tête armée quand l'active vient de finir sans avoir eu de trame à montrer.
                for _pass in 0..2 {
                    let mut switch_to: Option<String> = None;
                    if let Some(d) = ctl.decks.get_mut(&name) {
                        if anchor.is_none() {
                            if let Some(f) = d.frames.front() {
                                // Reprise après pause : l'image à `pending_m0` est DÉJÀ à
                                // l'antenne — on repart de la SUIVANTE, sinon elle sort deux fois
                                // à chaque Play. Départ d'une tête : sa 1re trame, ce créneau-ci.
                                let m0 = match pending_m0.take() {
                                    Some(p) => p + period_s * speed,
                                    None => f.pts,
                                };
                                anchor = Some((g, m0, speed));
                            }
                        }
                        if let Some((g0, m0, sp)) = anchor {
                            let (g0, m0, sp) = if (sp - speed).abs() > 1e-6 {
                                anchor = Some((g, cur_pos, speed));
                                (g, cur_pos, speed)
                            } else {
                                (g0, m0, sp)
                            };
                            let mt = m0 + sp * (g.saturating_sub(g0)) as f64 / 1e9;
                            while d.frames.front().is_some_and(|f| f.pts <= mt + 1e-6) {
                                last = d.frames.pop_front();
                                last_deck = Some(name.clone());
                                fresh = true;
                            }
                            cur_pos = mt;
                            active_now = true;
                            if d.frames.is_empty() && d.eos {
                                let ready = ctl.next.as_ref().and_then(|n| ctl.decks.get(n)).is_some_and(|nd| !nd.frames.is_empty());
                                if ready {
                                    switch_to = ctl.next.clone();
                                } else {
                                    // Fini, rien d'armé : on TIENT la dernière image, mais on n'est
                                    // plus « en lecture » — sinon chaque créneau tenu compterait
                                    // comme une trame répétée (mesuré : 250 en 5 s sur un clip fini).
                                    ctl.stats.eos = true;
                                    active_now = fresh;
                                    // La position reste sur la dernière image montrée : l'horloge de
                                    // présentation continue, pas le clip (elle dépassait la durée).
                                    if let Some(f) = last.as_ref() {
                                        cur_pos = f.pts;
                                    }
                                }
                            }
                        }
                    }
                    shared.space.notify_all();
                    match switch_to {
                        Some(nx) => {
                            gst::debug!(CAT, obj = obj, "BASCULE {:?} → {} à mt={:.4} (dernière={:?}, montrée ce créneau={}) ; 1re de {} = {:?}",
                                cur_deck, nx, cur_pos, last.as_ref().map(|f| f.pts), fresh, nx,
                                ctl.decks.get(&nx).and_then(|d| d.frames.front()).map(|f| f.pts));
                            ctl.active = Some(nx.clone());
                            ctl.next = None;
                            ctl.stats.switches += 1;
                            ctl.stats.eos = false;
                            cur_deck = Some(nx.clone());
                            cur_epoch = ctl.decks.get(&nx).map(|d| d.epoch).unwrap_or(0);
                            anchor = None;
                            pending_m0 = None;
                            name = nx;
                            if fresh {
                                // la dernière trame de l'ancienne tête est montrée maintenant ; la
                                // nouvelle sort au créneau d'après — l'audio de CE créneau reste
                                // celui de l'ancienne (curseur continu, sans recalage).
                                break;
                            }
                            shown_deck = cur_deck.clone();
                            shown_key = None;
                        }
                        None => break,
                    }
                }
                if ctl.cue && fresh {
                    ctl.cue = false;
                    ctl.paused = true;
                }
            } else {
                if anchor.is_some() && !anchor_fixed {
                    pending_m0 = Some(cur_pos);
                }
                if !anchor_fixed || cur_deck.is_none() {
                    anchor = None;
                }
            }
            // Image FIGÉE par une commande datée (vitesse 0 : pause, cue, pas à pas) : ce n'est pas
            // une lecture — ni trame « répétée », ni audio, ni compteur d'antenne.
            if speed <= 0.0 {
                active_now = false;
            }
            // ── Audio : lu dans les pistes de la tête courante au curseur média, routé, gainé. ──
            if a_n > 0 {
                let playing_audio = active_now && !ctl.paused && (speed - 1.0).abs() < 1e-6;
                if playing_audio {
                    // shown_key = None : la tête vient d'être (re)ancrée dans ce créneau → clé neuve.
                    let key = shown_key.clone().or_else(|| shown_deck.clone().zip(anchor.map(|a| a.0)));
                    let same_deck = a_key.as_ref().map(|k| &k.0) == shown_deck.as_ref();
                    let keep = a_cursor.is_some() && (key == a_key || (same_deck && shown_key.is_some() && anchor.is_none()));
                    if !keep {
                        a_cursor = Some((cur_pos * AUDIO_RATE as f64).round() as i64);
                        if let Some(d) = shown_deck.as_ref().and_then(|n| ctl.decks.get(n)) {
                            gst::debug!(CAT, obj = obj, "AUDIO recalé tête={:?} curseur={:?} pistes={:?}", shown_deck, a_cursor,
                                d.tracks.iter().map(|(n, t)| (*n, t.start, t.data.len() / t.channels.max(1))).collect::<Vec<_>>());
                        }
                    }
                    a_key = key.or(a_key.clone());
                } else {
                    a_cursor = None;
                    a_key = None;
                }
                if let (Some(cur), Some(name)) = (a_cursor, shown_deck.clone()) {
                    let route = ctl.route.clone();
                    let gain = ctl.gain.clone();
                    let mute = ctl.mute.clone();
                    let mon_route = ctl.mon_route.clone();
                    if let Some(d) = ctl.decks.get_mut(&name) {
                        // canal SOURCE (1-based) → (n° de piste, canal dans la piste)
                        let mut map: Vec<(u32, usize)> = Vec::new();
                        for (&tn, t) in d.tracks.iter() {
                            for c in 0..t.channels {
                                map.push((tn, c));
                            }
                        }
                        for o in 0..a_channels.min(route.len()) {
                            let src = route[o] as usize;
                            if src == 0 || src > map.len() || mute[o] {
                                continue;
                            }
                            let (tn, c) = map[src - 1];
                            let t = &d.tracks[&tn];
                            let (Some(t0), ch) = (t.start, t.channels) else { continue };
                            let avail = (t.data.len() / ch.max(1)) as i64;
                            let gn = gain[o];
                            for i in 0..a_n {
                                let rel = cur + i as i64 - t0;
                                if rel >= 0 && rel < avail {
                                    a_out[o * a_n + i] = t.data[rel as usize * ch + c] * gn;
                                }
                            }
                        }
                        // Monitoring : le canal source tel quel (écoute, hors gain/mute/solo).
                        for o in 0..m_channels.min(mon_route.len()) {
                            let src = mon_route[o] as usize;
                            if src == 0 || src > map.len() {
                                continue;
                            }
                            let (tn, c) = map[src - 1];
                            let t = &d.tracks[&tn];
                            let (Some(t0), ch) = (t.start, t.channels) else { continue };
                            let avail = (t.data.len() / ch.max(1)) as i64;
                            for i in 0..a_n {
                                let rel = cur + i as i64 - t0;
                                if rel >= 0 && rel < avail {
                                    m_out[o * a_n + i] = t.data[rel as usize * ch + c];
                                }
                            }
                        }
                        // Ménage derrière le curseur (garde ~0,5 s), libère les pads en attente.
                        let keep_from = cur + a_n as i64 - (AUDIO_RATE as i64 / 2);
                        for t in d.tracks.values_mut() {
                            if let Some(t0) = t.start {
                                if keep_from > t0 && t.channels > 0 {
                                    let drop = ((keep_from - t0) as usize).min(t.data.len() / t.channels);
                                    t.data.drain(..drop * t.channels);
                                    t.start = Some(t0 + drop as i64);
                                }
                            }
                        }
                    }
                    a_cursor = Some(cur + a_n as i64);
                }
                for o in 0..a_channels.min(ctl.levels.len()) {
                    let peak = a_out[o * a_n..(o + 1) * a_n].iter().fold(0.0f32, |m, &x| m.max(x.abs()));
                    ctl.levels[o] = (ctl.levels[o] * decay).max(peak);
                }
            }
            // ── TC de la trame MONTRÉE (flux anc_1 = clip, anc_2 = antenne) ──
            if active_now {
                ctl.antenna_frames += 1;
            }
            (tc_clip_f, tc_ant_f) = {
                let d = shown_deck.as_ref().and_then(|n| ctl.decks.get(n));
                let base = d.map(|d| d.tc_base).unwrap_or(0);
                let clip = base + (cur_pos * tc_fps as f64).round() as i64;
                let ant = if ctl.antenna_down {
                    let rem = d.filter(|d| d.clip_end >= 0.0).map(|d| (d.clip_end - cur_pos).max(0.0) + d.after_s.max(0.0)).unwrap_or(0.0);
                    (rem * tc_fps as f64).round() as i64
                } else {
                    ctl.antenna_frames
                };
                (clip, ant)
            };
            anc_extra = if anc_w.is_empty() { Vec::new() } else { ctl.anc_extra.clone() };
            let fmt = |f: i64| { let (h, m, s, fr) = anc::frames_to_tc(f, tc_fps); format!("{h:02}:{m:02}:{s:02}:{fr:02}") };
            ctl.tc_clip = fmt(tc_clip_f);
            ctl.tc_antenna = fmt(tc_ant_f);
            hold_black = ctl.hold_black && !active_now;
            solo_now = ctl.solo.iter().enumerate().filter(|(_, x)| **x).map(|(o, _)| o).collect();
            if active_now && !fresh {
                ctl.stats.repeated += 1;
                if let Some(d) = cur_deck.as_ref().and_then(|n| ctl.decks.get(n)) {
                    gst::debug!(CAT, obj = obj, "RÉPÉTÉE tête={:?} mt={:.4} prochaine={:?} file={} eos={} dernière={:?}",
                        cur_deck, cur_pos, d.frames.front().map(|f| f.pts), d.frames.len(), d.eos,
                        last.as_ref().map(|f| f.pts));
                }
            }
            // ── Témoin du créneau (mode synchrone) ──
            // Retard en images de l'image ÉCRITE sur l'horloge de présentation : 0 = l'image due ;
            // n = le décodeur n'a pas livré, l'image d'avant est répétée (la grille, elle, ne glisse
            // pas : le créneau suivant montrera l'image due). Après une bascule de tête, tant que la
            // nouvelle n'a rien livré, c'est l'ANCIENNE image qui sort : retard compté depuis l'ancre.
            let lag = match (anchor, last.as_ref()) {
                _ if ctl.stats.eos || cur_deck.is_none() => 0,
                (Some((_, m0, _)), Some(f)) => {
                    if last_deck != cur_deck {
                        ((cur_pos - m0) / period_s + 1e-6).floor().max(0.0) as i64 + 1
                    } else {
                        ((cur_pos - f.pts) / period_s + 1e-6).floor().max(0.0) as i64
                    }
                }
                (Some((_, m0, _)), None) => ((cur_pos - m0) / period_s + 1e-6).floor().max(0.0) as i64 + 1,
                _ => 0,
            };
            if anchor_fixed && lag >= 1 {
                ctl.sync_late_slots += 1;
            }
            ctl.witness = Witness {
                k,
                g,
                mt: cur_pos,
                pts: if hold_black { None } else { last.as_ref().map(|f| f.pts) },
                shown_deck: if hold_black { None } else { last_deck.clone() },
                deck: cur_deck.clone(),
                lag,
                anchor,
                fixed: anchor_fixed,
                eos: ctl.stats.eos,
                black: hold_black,
            };
            ctl.stats.position = cur_pos;
            ctl.stats.playing = active_now;
            ctl.stats.slots_missed = missed;
            win_w += 1;
            if fresh {
                win_fresh += 1;
            }
            if win_t0 == 0 {
                win_t0 = g;
            }
            if g - win_t0 >= 1_000_000_000 {
                let dt = (g - win_t0) as f64 / 1e9;
                ctl.stats.fps = win_w as f64 / dt;
                ctl.stats.fresh_fps = win_fresh as f64 / dt;
                win_t0 = g;
                win_w = 0;
                win_fresh = 0;
            }
        }

        if let (Some((aw, ch)), true) = (audio.as_ref(), a_n > 0) {
            match write_samples(aw, *ch, &a_out, a_start, a_n) {
                Ok(()) => a_last_end = Some(a_start + a_n as u64),
                Err(e) => gst::debug!(CAT, obj = obj, "audio : open_samples({}) : {}", a_start + a_n as u64, e),
            }
        }
        // SOLO = écoute (comme le PFL d'une console) : il ne touche JAMAIS l'antenne, il remplace
        // ce qu'entend le monitoring. Un solo : cette sortie sur les deux côtés ; deux : la
        // première à gauche, la seconde à droite ; plus : leur somme sur les deux côtés. Ce qu'on
        // entend est la sortie telle qu'elle part (après routage, gain et mute). Sans solo, le
        // monitoring suit `monitor-route` (canaux du clip).
        if m_channels == 2 && !solo_now.is_empty() && a_n > 0 {
            let out = |o: usize| &a_out[o * a_n..(o + 1) * a_n];
            let s: Vec<usize> = solo_now.iter().copied().filter(|&o| o < a_channels).collect();
            match s.len() {
                0 => {}
                1 => {
                    let x = out(s[0]).to_vec();
                    m_out[..a_n].copy_from_slice(&x);
                    m_out[a_n..2 * a_n].copy_from_slice(&x);
                }
                2 => {
                    let (l, r) = (out(s[0]).to_vec(), out(s[1]).to_vec());
                    m_out[..a_n].copy_from_slice(&l);
                    m_out[a_n..2 * a_n].copy_from_slice(&r);
                }
                _ => {
                    for i in 0..a_n {
                        let v: f32 = s.iter().map(|&o| a_out[o * a_n + i]).sum();
                        m_out[i] = v;
                        m_out[a_n + i] = v;
                    }
                }
            }
        }
        // Même bloc, mêmes index que l'antenne : les deux flux restent calés à l'échantillon.
        if let (Some((mw, ch)), true) = (monitor.as_ref(), a_n > 0) {
            if let Err(e) = write_samples(mw, *ch, &m_out, a_start, a_n) {
                gst::debug!(CAT, obj = obj, "monitoring : open_samples({}) : {}", a_start + a_n as u64, e);
            }
        }
        for (i, w) in anc_w.iter().enumerate() {
            let (h, m, s_, f) = anc::frames_to_tc(if i == 0 { tc_clip_f } else { tc_ant_f }, tc_fps);
            let mut pk = vec![anc::Packet { did: 0x60, sdid: 0x60, line: 9, hori: 0xFFF, udw: anc::atc_udw(h, m, s_, f, false) }];
            pk.extend(anc_extra.iter().cloned());
            let bytes = anc::pack_rfc8331(&pk);
            if let Ok(mut g) = w.open_grain(k) {
                let total = g.total_slices();
                let pl = g.payload_mut();
                let n = bytes.len().min(pl.len());
                pl[..n].copy_from_slice(&bytes[..n]);
                pl[n..].fill(0);
                let _ = g.commit(total);
            }
        }
        let src = match (&last, hold_black) {
            (Some(f), false) => Src::Frame(f),
            _ => Src::Black,
        };
        if !write_frame(&writer, &geo, k, src, &black) {
            let mut ctl = shared.ctl.lock().unwrap();
            ctl.stats.format_errors += 1;
            if ctl.stats.format_errors == 1 {
                gst::warning!(CAT, obj = obj, "trame non conforme à la géométrie du flux : noir écrit à la place");
            }
        }
    }
    drop(writer);
    drop(audio);
    drop(monitor);
    drop(anc_w);
    gst::info!(CAT, obj = obj, "boucle-grille arrêtée ({} créneaux manqués)", missed);
}

// ─── GObject / GStreamer ─────────────────────────────────────────────────────────────────────
#[glib::object_subclass]
impl ObjectSubclass for BobiPlayout {
    const NAME: &'static str = "GstBobiPlayout";
    type Type = super::BobiPlayout;
    type ParentType = gst::Element;

    fn new() -> Self {
        Self {
            settings: Mutex::new(Settings::default()),
            shared: Arc::new(Shared::default()),
            instance: Mutex::new(None),
            thread: Mutex::new(None),
        }
    }
}

impl ObjectImpl for BobiPlayout {
    fn properties() -> &'static [glib::ParamSpec] {
        static PROPERTIES: LazyLock<Vec<glib::ParamSpec>> = LazyLock::new(|| {
            vec![
                glib::ParamSpecString::builder("domain")
                    .blurb("Répertoire du domaine MXL ($MXL_DOMAIN, sinon /dev/shm/mxl)")
                    .default_value(Some(default_domain().as_str()))
                    .mutable_ready()
                    .build(),
                glib::ParamSpecString::builder("video-flow-def")
                    .blurb("flowDef JSON du flux vidéo, fourni par bobimxl (identité et format du flux)")
                    .mutable_ready()
                    .build(),
                glib::ParamSpecString::builder("audio-flow-def")
                    .blurb("flowDef JSON du flux audio (bobimxl.build_audio_flow_def), vide = pas d'audio")
                    .mutable_ready()
                    .build(),
                glib::ParamSpecString::builder("monitor-flow-def")
                    .blurb("flowDef JSON du flux monitoring (2 canaux), vide = pas de monitoring")
                    .build(),
                glib::ParamSpecString::builder("monitor-route")
                    .blurb("Canal source (1-based, 0 = silence) de chaque canal du monitoring : « g,d »")
                    .build(),
                glib::ParamSpecString::builder("audio-route")
                    .blurb("Pour chaque sortie, n° du canal SOURCE (1 = 1er canal de la 1re piste ; 0 = silence), séparés par des virgules")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("audio-gains")
                    .blurb("Gain linéaire par sortie (0 à 4), séparés par des virgules")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("audio-mute")
                    .blurb("Mute par sortie (0/1), séparés par des virgules")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("audio-solo")
                    .blurb("Solo par sortie (0/1) : ÉCOUTE sur le flux monitoring (1 = cette sortie, 2 = G/D, plus = somme), sans effet sur l'antenne")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("anc1-flow-def").blurb("flowDef JSON du flux ANC « TC clip »").mutable_ready().build(),
                glib::ParamSpecString::builder("anc2-flow-def").blurb("flowDef JSON du flux ANC « TC antenne »").mutable_ready().build(),
                glib::ParamSpecString::builder("deck-meta")
                    .blurb("Métadonnées d'une tête : « tête|TC embarqué (trames)|fin du clip (s, -1 = inconnue)|durée après (s) »")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("antenna-mode").blurb("TC antenne : up (trames jouées) | down (décompte)").mutable_playing().build(),
                glib::ParamSpecBoolean::builder("antenna-reset").blurb("Remet le compteur ascendant du TC antenne à zéro").mutable_playing().build(),
                glib::ParamSpecString::builder("anc-extra")
                    .blurb("Paquets ANC ajoutés aux deux flux (JSON : [{did,sdid,line,hori,udw hex}]) — OP-47…")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("tc-clip").blurb("TC clip de la trame à l'antenne").read_only().build(),
                glib::ParamSpecString::builder("tc-antenna").blurb("TC antenne de la trame à l'antenne").read_only().build(),
                glib::ParamSpecString::builder("audio-levels").blurb("Crêtes par sortie (0..1), avec retombée").read_only().build(),
                glib::ParamSpecString::builder("audio-tracks").blurb("Canaux par piste de la tête active, dans l'ordre des pistes").read_only().build(),
                glib::ParamSpecString::builder("writer-options")
                    .blurb("Options JSON de mxlCreateFlowWriter (bobimxl._flow_options), vide = aucune")
                    .mutable_ready()
                    .build(),
                glib::ParamSpecUInt::builder("queue-frames")
                    .blurb("Trames d'avance par tête (0 = une demi-seconde)")
                    .maximum(1000)
                    .mutable_ready()
                    .build(),
                glib::ParamSpecString::builder("active-deck")
                    .blurb("Tête à l'antenne (nom après « video_ »)")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("next-deck")
                    .blurb("Tête ARMÉE : prend l'antenne à la trame près quand l'active a fini")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecBoolean::builder("paused")
                    .blurb("Pause : la dernière image est répétée, l'horloge de présentation s'arrête")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecBoolean::builder("cue")
                    .blurb("Montrer la prochaine image neuve puis se mettre en pause (une fois)")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecBoolean::builder("hold-black")
                    .blurb("Hors lecture, sortir du noir au lieu de la dernière image")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecDouble::builder("speed")
                    .blurb("Vitesse de l'horloge de présentation (doit suivre le taux du seek)")
                    .minimum(0.01)
                    .maximum(16.0)
                    .default_value(1.0)
                    .mutable_playing()
                    .build(),
                glib::ParamSpecDouble::builder("position").blurb("Temps média de l'image à l'antenne (s)").read_only().build(),
                glib::ParamSpecBoolean::builder("playing").blurb("Lecture effective (horloge active)").read_only().build(),
                glib::ParamSpecBoolean::builder("eos").blurb("Tête active finie, sans tête armée prête").read_only().build(),
                glib::ParamSpecDouble::builder("fps").blurb("Écritures par seconde").read_only().build(),
                glib::ParamSpecDouble::builder("fresh-fps").blurb("Images NEUVES par seconde").read_only().build(),
                glib::ParamSpecUInt64::builder("slots-missed").blurb("Créneaux de grille manqués (cumul)").read_only().build(),
                glib::ParamSpecUInt64::builder("repeated-frames").blurb("Trames répétées EN LECTURE (cumul)").read_only().build(),
                glib::ParamSpecUInt64::builder("deck-switches").blurb("Bascules automatiques vers la tête armée").read_only().build(),
                glib::ParamSpecUInt64::builder("format-errors").blurb("Trames non conformes remplacées par du noir").read_only().build(),
                glib::ParamSpecUInt::builder("queued-frames").blurb("Trames en attente sur la tête active").read_only().build(),
                glib::ParamSpecString::builder("schedule")
                    .blurb("Commande DATÉE (lecture synchrone) : « id|instant TAI ns|tête (vide = l'active)|temps média s|vitesse (0 = figée)|noir 0/1 », « cancel|id » ou « clear »")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecBoolean::builder("sync-release")
                    .blurb("Quitte le mode synchrone : commandes datées oubliées, l'ancre redevient ordinaire")
                    .mutable_playing()
                    .build(),
                glib::ParamSpecString::builder("sync-state")
                    .blurb("Témoin JSON du dernier créneau écrit (index, TAI, temps média, image écrite, retard) + commandes datées")
                    .read_only()
                    .build(),
            ]
        });
        PROPERTIES.as_ref()
    }

    fn set_property(&self, _id: usize, value: &glib::Value, pspec: &glib::ParamSpec) {
        let opt_s = || value.get::<Option<String>>().unwrap().unwrap_or_default();
        match pspec.name() {
            "domain" => {
                let v = value.get::<Option<String>>().unwrap().unwrap_or_else(default_domain);
                self.settings.lock().unwrap().domain = v;
            }
            "video-flow-def" => self.settings.lock().unwrap().video_flow_def = opt_s(),
            "writer-options" => self.settings.lock().unwrap().writer_options = opt_s(),
            "audio-flow-def" => self.settings.lock().unwrap().audio_flow_def = opt_s(),
            "monitor-flow-def" => self.settings.lock().unwrap().monitor_flow_def = opt_s(),
            "anc1-flow-def" => self.settings.lock().unwrap().anc1_flow_def = opt_s(),
            "anc2-flow-def" => self.settings.lock().unwrap().anc2_flow_def = opt_s(),
            "queue-frames" => self.settings.lock().unwrap().queue_frames = value.get().unwrap(),
            name => {
                let mut ctl = self.shared.ctl.lock().unwrap();
                match name {
                    "schedule" => {
                        let v = opt_s();
                        let f: Vec<&str> = v.split('|').collect();
                        match f.first().copied() {
                            Some("clear") => ctl.sched.clear(),
                            Some("cancel") => {
                                let id: u64 = f.get(1).and_then(|x| x.parse().ok()).unwrap_or(0);
                                ctl.sched.retain(|s| s.id != id);
                            }
                            _ if f.len() >= 5 => {
                                let id = f[0].parse().unwrap_or(0);
                                let at = f[1].parse().unwrap_or(0);
                                let m0 = f[3].parse().unwrap_or(f64::NAN);
                                let speed: f64 = f[4].parse().unwrap_or(f64::NAN);
                                if at == 0 || !m0.is_finite() || !speed.is_finite() || !(0.0..=16.0).contains(&speed) {
                                    gst::warning!(CAT, imp = self, "commande datée illisible ignorée : {}", v);
                                } else {
                                    let deck = Some(f[2].to_string()).filter(|s| !s.is_empty());
                                    let black = f.get(5).is_some_and(|x| *x == "1");
                                    ctl.sched.retain(|s| s.id != id);
                                    ctl.sched.push(Sched { id, at, deck, m0, speed, black });
                                }
                            }
                            _ => gst::warning!(CAT, imp = self, "commande datée illisible ignorée : {}", v),
                        }
                    }
                    "sync-release" => {
                        if value.get::<bool>().unwrap() {
                            ctl.sched.clear();
                            ctl.sync_speed = None;
                            ctl.sync_release = true;
                            ctl.hold_black = false;
                        }
                    }
                    "active-deck" => {
                        // Changement de tête ORDINAIRE (hors commande datée) : fin du mode synchrone.
                        ctl.sync_speed = None;
                        let new = Some(opt_s()).filter(|s| !s.is_empty());
                        // Fin de l'ANCIENNE tête effacée tout de suite, pas au prochain créneau de la
                        // grille : le pilote lit `eos` toutes les 10 ms et prenait cette fin pour
                        // celle du clip qu'il venait de charger (action de fin déclenchée à tort ;
                        // en playlist, un clip sauté — mesuré le 2026-10-11).
                        if new != ctl.active {
                            ctl.stats.eos = false;
                        }
                        ctl.active = new;
                    }
                    "next-deck" => ctl.next = Some(opt_s()).filter(|s| !s.is_empty()),
                    "paused" => ctl.paused = value.get().unwrap(),
                    "cue" => ctl.cue = value.get().unwrap(),
                    "hold-black" => ctl.hold_black = value.get().unwrap(),
                    "speed" => {
                        ctl.speed = value.get().unwrap();
                        ctl.sync_speed = None;
                    }
                    "audio-route" => ctl.route = parse_list(&opt_s(), 8, 0u32),
                    "monitor-route" => ctl.mon_route = parse_list(&opt_s(), 2, 0u32),
                    "deck-meta" => {
                        let v = opt_s();
                        let f: Vec<&str> = v.split('|').collect();
                        if let Some(name) = f.first().filter(|n| !n.is_empty()) {
                            let d = ctl.decks.entry(name.to_string()).or_default();
                            d.tc_base = f.get(1).and_then(|x| x.parse().ok()).unwrap_or(0);
                            d.clip_end = f.get(2).and_then(|x| x.parse().ok()).unwrap_or(-1.0);
                            d.after_s = f.get(3).and_then(|x| x.parse().ok()).unwrap_or(0.0);
                        }
                    }
                    "antenna-mode" => ctl.antenna_down = opt_s() == "down",
                    "antenna-reset" => {
                        if value.get::<bool>().unwrap() {
                            ctl.antenna_frames = 0;
                        }
                    }
                    "anc-extra" => ctl.anc_extra = anc::parse_extra(&opt_s()),
                    "audio-gains" => {
                        ctl.gain = parse_list(&opt_s(), 8, 1.0f32).into_iter().map(|g| g.clamp(0.0, 4.0)).collect()
                    }
                    "audio-mute" => ctl.mute = parse_list(&opt_s(), 8, 0u8).into_iter().map(|x| x != 0).collect(),
                    "audio-solo" => ctl.solo = parse_list(&opt_s(), 8, 0u8).into_iter().map(|x| x != 0).collect(),
                    _ => unimplemented!(),
                }
            }
        }
    }

    fn property(&self, _id: usize, pspec: &glib::ParamSpec) -> glib::Value {
        match pspec.name() {
            "domain" => self.settings.lock().unwrap().domain.to_value(),
            "video-flow-def" => self.settings.lock().unwrap().video_flow_def.to_value(),
            "writer-options" => self.settings.lock().unwrap().writer_options.to_value(),
            "audio-flow-def" => self.settings.lock().unwrap().audio_flow_def.to_value(),
            "monitor-flow-def" => self.settings.lock().unwrap().monitor_flow_def.to_value(),
            "anc1-flow-def" => self.settings.lock().unwrap().anc1_flow_def.to_value(),
            "anc2-flow-def" => self.settings.lock().unwrap().anc2_flow_def.to_value(),
            "queue-frames" => self.settings.lock().unwrap().queue_frames.to_value(),
            name => {
                let ctl = self.shared.ctl.lock().unwrap();
                let st = ctl.stats;
                match name {
                    "active-deck" => ctl.active.clone().unwrap_or_default().to_value(),
                    "next-deck" => ctl.next.clone().unwrap_or_default().to_value(),
                    "paused" => ctl.paused.to_value(),
                    "cue" => ctl.cue.to_value(),
                    "hold-black" => ctl.hold_black.to_value(),
                    "speed" => ctl.speed.to_value(),
                    "audio-route" => ctl.route.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(",").to_value(),
                    "monitor-route" => ctl.mon_route.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(",").to_value(),
                    "audio-gains" => ctl.gain.iter().map(|x| format!("{x:.3}")).collect::<Vec<_>>().join(",").to_value(),
                    "audio-mute" => ctl.mute.iter().map(|&x| if x { "1" } else { "0" }).collect::<Vec<_>>().join(",").to_value(),
                    "audio-solo" => ctl.solo.iter().map(|&x| if x { "1" } else { "0" }).collect::<Vec<_>>().join(",").to_value(),
                    "audio-levels" => ctl.levels.iter().map(|x| format!("{x:.4}")).collect::<Vec<_>>().join(",").to_value(),
                    "audio-tracks" => ctl.active.as_ref().and_then(|n| ctl.decks.get(n))
                        .map(|d| d.tracks.values().map(|t| t.channels.to_string()).collect::<Vec<_>>().join(","))
                        .unwrap_or_default().to_value(),
                    "position" => st.position.to_value(),
                    "deck-meta" => String::new().to_value(),
                    "antenna-mode" => (if ctl.antenna_down { "down" } else { "up" }).to_value(),
                    "antenna-reset" => false.to_value(),
                    "anc-extra" => String::new().to_value(),
                    "tc-clip" => ctl.tc_clip.clone().to_value(),
                    "tc-antenna" => ctl.tc_antenna.clone().to_value(),
                    "playing" => st.playing.to_value(),
                    "eos" => st.eos.to_value(),
                    "fps" => st.fps.to_value(),
                    "fresh-fps" => st.fresh_fps.to_value(),
                    "slots-missed" => st.slots_missed.to_value(),
                    "repeated-frames" => st.repeated.to_value(),
                    "deck-switches" => st.switches.to_value(),
                    "format-errors" => st.format_errors.to_value(),
                    "sync-release" => false.to_value(),
                    "schedule" => ctl.sched.iter().map(|s| format!("{}@{}", s.id, s.at)).collect::<Vec<_>>().join(",").to_value(),
                    "sync-state" => {
                        let w = &ctl.witness;
                        let decks: serde_json::Map<String, serde_json::Value> = ctl.decks.iter().map(|(n, d)| {
                            (n.clone(), serde_json::json!({
                                "frames": d.frames.len(),
                                "first_pts": d.frames.front().map(|f| f.pts),
                                "eos": d.eos,
                                "caps": d.info.is_some(),
                                "tracks": d.tracks.len(),
                            }))
                        }).collect();
                        serde_json::json!({
                            "k": w.k, "g": w.g, "mt": w.mt, "pts": w.pts,
                            "shown_deck": w.shown_deck, "deck": w.deck, "lag": w.lag,
                            "anchor": w.anchor.map(|(g0, m0, sp)| serde_json::json!({"g0": g0, "m0": m0, "speed": sp})),
                            "fixed": w.fixed, "eos": w.eos, "black": w.black,
                            "late_slots": ctl.sync_late_slots,
                            "slots_missed": st.slots_missed,
                            "sync_speed": ctl.sync_speed,
                            "pending": ctl.sched.iter().map(|s| serde_json::json!({"id": s.id, "at": s.at})).collect::<Vec<_>>(),
                            "applied": ctl.applied.iter().map(|a| serde_json::json!({
                                "id": a.id, "at": a.at, "k_at": a.k_at, "k": a.k, "late_slots": a.late_slots})).collect::<Vec<_>>(),
                            "decks": decks,
                        }).to_string().to_value()
                    }
                    "queued-frames" => (ctl.active.as_ref().and_then(|n| ctl.decks.get(n)).map(|d| d.frames.len()).unwrap_or(0) as u32).to_value(),
                    _ => unimplemented!(),
                }
            }
        }
    }
}

impl GstObjectImpl for BobiPlayout {}

impl ElementImpl for BobiPlayout {
    fn metadata() -> Option<&'static gst::subclass::ElementMetadata> {
        static META: LazyLock<gst::subclass::ElementMetadata> = LazyLock::new(|| {
            gst::subclass::ElementMetadata::new(
                "Bobi playout MXL",
                "Sink/Video/Audio",
                "Sortie playout cadencée par la grille TAI sur le bus MXL, têtes de lecture A/B",
                "BOBI SAS",
            )
        });
        Some(&*META)
    }

    fn pad_templates() -> &'static [gst::PadTemplate] {
        static TEMPLATES: LazyLock<Vec<gst::PadTemplate>> = LazyLock::new(|| {
            vec![
                gst::PadTemplate::new("video_%s", gst::PadDirection::Sink, gst::PadPresence::Request,
                    &gst::Caps::builder("video/x-raw").build()).unwrap(),
                gst::PadTemplate::new("audio_%s", gst::PadDirection::Sink, gst::PadPresence::Request,
                    &gst::Caps::builder("audio/x-raw").field("format", "F32LE").field("rate", AUDIO_RATE as i32)
                        .field("layout", "interleaved").build()).unwrap(),
            ]
        });
        TEMPLATES.as_ref()
    }

    fn request_new_pad(
        &self,
        templ: &gst::PadTemplate,
        name: Option<&str>,
        _caps: Option<&gst::Caps>,
    ) -> Option<gst::Pad> {
        let Some(name) = name else {
            gst::error!(CAT, imp = self, "pad demandé sans nom : video_<tête> ou audio_<tête>_<n> attendu");
            return None;
        };
        let pad = self.make_sink_pad(templ, name);
        pad.set_active(true).ok();
        self.obj().add_pad(&pad).ok()?;
        Some(pad)
    }

    fn release_pad(&self, pad: &gst::Pad) {
        if pad.name().starts_with("video_") {
            let deck = deck_of(pad);
            let mut ctl = self.shared.ctl.lock().unwrap();
            if let Some(d) = ctl.decks.get_mut(&deck) {
                d.removed = true;
            }
            self.shared.space.notify_all();
            drop(ctl);
            pad.set_active(false).ok();
            let mut ctl = self.shared.ctl.lock().unwrap();
            ctl.decks.remove(&deck);
            if ctl.next.as_deref() == Some(deck.as_str()) {
                ctl.next = None;
            }
        } else {
            pad.set_active(false).ok();
        }
        self.obj().remove_pad(pad).ok();
    }

    fn change_state(
        &self,
        transition: gst::StateChange,
    ) -> Result<gst::StateChangeSuccess, gst::StateChangeError> {
        match transition {
            gst::StateChange::NullToReady => self.open_instance()?,
            gst::StateChange::ReadyToPaused => self.start_grid()?,
            gst::StateChange::PausedToReady => self.stop_grid(),
            _ => {}
        }
        let ret = self.parent_change_state(transition)?;
        if transition == gst::StateChange::ReadyToNull {
            self.close_instance();
        }
        Ok(ret)
    }
}
