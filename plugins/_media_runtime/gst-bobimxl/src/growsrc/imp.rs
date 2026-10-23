// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// `bobigrowsrc` — lit un fichier PENDANT qu'il s'écrit (lecture en différé d'un enregistrement,
// « chase play »). URI `bobigrow:///chemin`, donc utilisable tel quel par uridecodebin.
//
// Pourquoi une source et pas un réglage de démuxeur : en lecture directe (filesrc), qtdemux et
// mxfdemux lisent un fichier en cours jusqu'à sa fin DU MOMENT, puis rendent EOS ; en flux poussé
// (appsrc), mxfdemux refuse le MXF d'ffmpeg, même complet (« track position doesn't match »,
// GStreamer 1.26.2, mesuré le 2026-10-11). Cette source garde le mode TIRÉ (pull) des démuxeurs —
// donc leurs index et leurs seeks — et fait seulement ATTENDRE une lecture qui dépasse la fin
// actuelle, au lieu de la tronquer.
//
// Tant que `growing` est vrai, la taille est déclarée INCONNUE : GstBaseSrc ne borne alors aucune
// lecture, et un démuxeur qui chercherait la fin (index MXF, mfra MP4) n'y lit pas une fin
// provisoire. Fin d'enregistrement : `growing` passe à faux (posé par le player quand le
// recorder a clos le fichier) — ou, filet de sécurité, aucun octet neuf pendant `idle-timeout`
// (recorder mort) ; une lecture au-delà de la fin devient alors une vraie fin de flux.
//
// MODE « UNITÉS DE TAILLE FIXE » (`frame-size` > 0) : pour une essence qu'aucun démuxeur ne lit
// pendant l'écriture — l'OP-Atom « growing » d'Avid, où bmx écrit une longueur de KLV NULLE tant
// que le clip n'est pas clos (Media Composer en déduit la durée par la taille du fichier). La
// source saute alors l'en-tête (`data-offset`), sort des tampons de `frame-size` octets horodatés
// à `rate-n`/`rate-d` par seconde, avec des caps fixées (`caps`), en format TEMPS (seeks par
// numéro d'unité). Fin : la longueur réelle, lue à `length-offset` (8 octets gros-boutistes) une
// fois le fichier clos. Taille fixe = DNxHD (débit constant par CID) et PCM.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::{Condvar, LazyLock, Mutex};
use std::time::{Duration, Instant};

use gst::glib;
use gst::prelude::*;
use gstreamer as gst;
use gstreamer_base as gst_base;
use gst_base::prelude::BaseSrcExt;
use gst_base::subclass::prelude::*;

static CAT: LazyLock<gst::DebugCategory> = LazyLock::new(|| {
    gst::DebugCategory::new("bobigrowsrc", gst::DebugColorFlags::empty(), Some("Lecture d'un fichier en cours d'écriture"))
});

const POLL: Duration = Duration::from_millis(40);
const DEFAULT_IDLE_MS: u32 = 15_000;

#[derive(Debug)]
struct Settings {
    location: Option<String>,
    growing: bool,
    idle_ms: u32,
    /// Mode unités de taille fixe (0 = lecture d'octets ordinaire).
    frame_size: u32,
    data_offset: u64,
    length_offset: u64,
    rate_n: u32,
    rate_d: u32,
    caps: Option<gst::Caps>,
    /// Mode unités : départ et arrêt (ns ; arrêt 0 = aucun), posés AVANT la mise en route — une
    /// tête Avid a une source par piste, et un seek envoyé à chacune en plein démarrage faisait
    /// échouer certaines (« Internal data stream error », mesuré le 2026-10-11).
    start_ns: u64,
    stop_ns: u64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { location: None, growing: true, idle_ms: DEFAULT_IDLE_MS, frame_size: 0, data_offset: 0,
                   length_offset: 0, rate_n: 25, rate_d: 1, caps: None, start_ns: 0, stop_ns: 0 }
    }
}

#[derive(Default)]
struct Run {
    file: Option<File>,
    flushing: bool,
    /// Dernière taille vue et instant où elle a changé (filet `idle-timeout`).
    seen: u64,
    seen_at: Option<Instant>,
    /// Mode unités : numéro de la prochaine unité.
    n: u64,
}

#[derive(Default)]
pub struct BobiGrowSrc {
    settings: Mutex<Settings>,
    run: Mutex<Run>,
    wake: Condvar,
}

#[glib::object_subclass]
impl ObjectSubclass for BobiGrowSrc {
    const NAME: &'static str = "BobiGrowSrc";
    type Type = super::BobiGrowSrc;
    type ParentType = gst_base::BaseSrc;
    type Interfaces = (gst::URIHandler,);
}

impl BobiGrowSrc {
    fn growing(&self) -> bool {
        self.settings.lock().unwrap().growing
    }

    fn framed(&self) -> bool {
        self.settings.lock().unwrap().frame_size > 0
    }

    /// Mode unités : fin de l'essence si sa longueur est ÉCRITE (non nulle), sinon None. bmx ne
    /// l'écrit qu'à la fermeture du clip : une longueur non nulle VAUT « clos », sans attendre que
    /// le pilote le dise — sinon la source lisait le pied de fichier comme une unité de plus.
    fn essence_end(f: &File, data_offset: u64, length_offset: u64) -> Option<u64> {
        if length_offset == 0 {
            return None;
        }
        let mut b = [0u8; 8];
        f.read_exact_at(&mut b, length_offset).ok()?;
        let l = u64::from_be_bytes(b);
        (l > 0).then_some(data_offset + l)
    }

    /// Instant (ns) de l'unité `n`.
    fn unit_time(n: u64, rate_n: u32, rate_d: u32) -> u64 {
        ((n as u128 * rate_d as u128 * 1_000_000_000u128) / rate_n.max(1) as u128) as u64
    }

    fn set_location(&self, loc: Option<String>) -> Result<(), glib::Error> {
        if self.obj().current_state() > gst::State::Ready {
            return Err(glib::Error::new(gst::URIError::BadState, "location figée hors de NULL/READY"));
        }
        self.settings.lock().unwrap().location = loc;
        Ok(())
    }
}

impl ObjectImpl for BobiGrowSrc {
    fn properties() -> &'static [glib::ParamSpec] {
        static PROPS: LazyLock<Vec<glib::ParamSpec>> = LazyLock::new(|| {
            vec![
                glib::ParamSpecString::builder("location").blurb("Chemin du fichier").mutable_ready().build(),
                glib::ParamSpecBoolean::builder("growing")
                    .blurb("Le fichier s'écrit encore : une lecture au-delà de la fin ATTEND au lieu de finir")
                    .default_value(true)
                    .mutable_playing()
                    .build(),
                glib::ParamSpecUInt::builder("idle-timeout")
                    .blurb("ms sans octet neuf après lesquelles le fichier est tenu pour clos (0 = jamais)")
                    .default_value(DEFAULT_IDLE_MS)
                    .mutable_playing()
                    .build(),
                glib::ParamSpecUInt::builder("frame-size")
                    .blurb("Mode unités : octets par tampon (0 = lecture d'octets ordinaire)")
                    .mutable_ready()
                    .build(),
                glib::ParamSpecUInt64::builder("data-offset").blurb("Mode unités : début de l'essence").mutable_ready().build(),
                glib::ParamSpecUInt64::builder("length-offset")
                    .blurb("Mode unités : position de la longueur de l'essence (8 octets), lue une fois le fichier clos (0 = taille du fichier)")
                    .mutable_ready()
                    .build(),
                glib::ParamSpecUInt::builder("rate-n").blurb("Mode unités : unités par seconde, numérateur").minimum(1).default_value(25).mutable_ready().build(),
                glib::ParamSpecUInt::builder("rate-d").blurb("Mode unités : unités par seconde, dénominateur").minimum(1).default_value(1).mutable_ready().build(),
                glib::ParamSpecBoxed::builder::<gst::Caps>("caps").blurb("Mode unités : caps des tampons").mutable_ready().build(),
                glib::ParamSpecUInt64::builder("start-time").blurb("Mode unités : départ (ns), unité précédente").mutable_ready().build(),
                glib::ParamSpecUInt64::builder("stop-time").blurb("Mode unités : arrêt (ns, 0 = aucun)").mutable_ready().build(),
            ]
        });
        PROPS.as_ref()
    }

    fn set_property(&self, _id: usize, value: &glib::Value, pspec: &glib::ParamSpec) {
        match pspec.name() {
            "location" => {
                let _ = self.set_location(value.get::<Option<String>>().unwrap());
            }
            "growing" => {
                self.settings.lock().unwrap().growing = value.get().unwrap();
                let _g = self.run.lock().unwrap();
                self.wake.notify_all();
            }
            "idle-timeout" => self.settings.lock().unwrap().idle_ms = value.get().unwrap(),
            "frame-size" => {
                let fs: u32 = value.get().unwrap();
                self.settings.lock().unwrap().frame_size = fs;
                // Un tampon = une unité, en format TEMPS (avant PAUSED : propriété mutable_ready).
                if fs > 0 {
                    let obj = self.obj();
                    obj.set_format(gst::Format::Time);
                    obj.set_blocksize(fs);
                }
            }
            "data-offset" => self.settings.lock().unwrap().data_offset = value.get().unwrap(),
            "length-offset" => self.settings.lock().unwrap().length_offset = value.get().unwrap(),
            "rate-n" => self.settings.lock().unwrap().rate_n = value.get().unwrap(),
            "rate-d" => self.settings.lock().unwrap().rate_d = value.get().unwrap(),
            "caps" => self.settings.lock().unwrap().caps = value.get().unwrap(),
            "start-time" => self.settings.lock().unwrap().start_ns = value.get().unwrap(),
            "stop-time" => self.settings.lock().unwrap().stop_ns = value.get().unwrap(),
            _ => unimplemented!(),
        }
    }

    fn property(&self, _id: usize, pspec: &glib::ParamSpec) -> glib::Value {
        let s = self.settings.lock().unwrap();
        match pspec.name() {
            "location" => s.location.to_value(),
            "growing" => s.growing.to_value(),
            "idle-timeout" => s.idle_ms.to_value(),
            "frame-size" => s.frame_size.to_value(),
            "data-offset" => s.data_offset.to_value(),
            "length-offset" => s.length_offset.to_value(),
            "rate-n" => s.rate_n.to_value(),
            "rate-d" => s.rate_d.to_value(),
            "caps" => s.caps.to_value(),
            "start-time" => s.start_ns.to_value(),
            "stop-time" => s.stop_ns.to_value(),
            _ => unimplemented!(),
        }
    }
}

impl GstObjectImpl for BobiGrowSrc {}

impl ElementImpl for BobiGrowSrc {
    fn metadata() -> Option<&'static gst::subclass::ElementMetadata> {
        static META: LazyLock<gst::subclass::ElementMetadata> = LazyLock::new(|| {
            gst::subclass::ElementMetadata::new(
                "Bobi growing file source",
                "Source/File",
                "Lit un fichier pendant son écriture : attend la suite au lieu de finir",
                "BOBI SAS",
            )
        });
        Some(&*META)
    }

    fn pad_templates() -> &'static [gst::PadTemplate] {
        static T: LazyLock<Vec<gst::PadTemplate>> = LazyLock::new(|| {
            vec![gst::PadTemplate::new("src", gst::PadDirection::Src, gst::PadPresence::Always, &gst::Caps::new_any()).unwrap()]
        });
        T.as_ref()
    }
}

impl BaseSrcImpl for BobiGrowSrc {
    fn start(&self) -> Result<(), gst::ErrorMessage> {
        let loc = self.settings.lock().unwrap().location.clone()
            .ok_or_else(|| gst::error_msg!(gst::ResourceError::Settings, ["location non réglée"]))?;
        let f = File::open(&loc)
            .map_err(|e| gst::error_msg!(gst::ResourceError::OpenRead, ["ouverture de {} : {}", loc, e]))?;
        let mut r = self.run.lock().unwrap();
        r.seen = f.metadata().map(|m| m.len()).unwrap_or(0);
        r.seen_at = Some(Instant::now());
        r.file = Some(f);
        r.flushing = false;
        let s = self.settings.lock().unwrap();
        r.n = ((s.start_ns as u128 * s.rate_n as u128) / (s.rate_d.max(1) as u128 * 1_000_000_000u128)) as u64;
        drop(s);
        gst::debug!(CAT, imp = self, "ouvert {} ({} octets)", loc, r.seen);
        Ok(())
    }

    fn stop(&self) -> Result<(), gst::ErrorMessage> {
        self.run.lock().unwrap().file = None;
        Ok(())
    }

    fn query(&self, query: &mut gst::QueryRef) -> bool {
        // Durée en OCTETS tant que le fichier s'écrit : la taille ACTUELLE, alors que `size()`
        // reste inconnue (aucune lecture bornée). Répondre « -1 » envoyait qtdemux lire son index
        // de fin (mfro) à 2^64 − 17 ; ne pas répondre faisait échouer la détection de type
        // (typefind en mode tiré) — les deux mesurés le 2026-10-11. À la taille actuelle, un
        // démuxeur qui cherche un index de fin (mfro MP4, RIP MXF) n'y lit que des données
        // d'essence, ne reconnaît pas la clé, et continue sans.
        if let gst::QueryViewMut::Duration(q) = query.view_mut() {
            if q.format() == gst::Format::Bytes && self.growing() {
                let cur = self.run.lock().unwrap().file.as_ref().and_then(|f| f.metadata().ok()).map(|m| m.len());
                if let Some(cur) = cur {
                    q.set(gst::format::Bytes::from_u64(cur));
                    return true;
                }
            }
        }
        BaseSrcImplExt::parent_query(self, query)
    }

    fn is_seekable(&self) -> bool {
        true
    }

    fn caps(&self, filter: Option<&gst::Caps>) -> Option<gst::Caps> {
        let c = self.settings.lock().unwrap().caps.clone();
        match c {
            Some(c) if self.framed() => Some(match filter {
                Some(f) => f.intersect_with_mode(&c, gst::CapsIntersectMode::First),
                None => c,
            }),
            _ => BaseSrcImplExt::parent_caps(self, filter),
        }
    }

    fn do_seek(&self, segment: &mut gst::Segment) -> bool {
        if !self.framed() {
            return BaseSrcImplExt::parent_do_seek(self, segment);
        }
        // Mode unités : la position est un numéro d'unité, arrondi à l'unité PRÉCÉDENTE — l'audio
        // (unités de 20 ms) couvre ainsi l'instant visé au lieu de démarrer après l'image.
        let Some(seg) = segment.downcast_mut::<gst::ClockTime>() else { return false };
        // GstBaseSrc fait un seek initial à 0 au démarrage : il ne doit pas écraser `start-time`.
        let (rn, rd, start_ns) = {
            let s = self.settings.lock().unwrap();
            (s.rate_n as u128, s.rate_d as u128, s.start_ns)
        };
        let start = seg.start().map(|t| t.nseconds()).unwrap_or(0).max(start_ns);
        let n = ((start as u128 * rn) / (rd * 1_000_000_000u128)) as u64;
        self.run.lock().unwrap().n = n;
        // Le SEGMENT commence à l'unité de départ : annoncé à 0 avec des tampons datés plus loin,
        // videorate (en aval, dans le player) comblait l'écart en répétant la 1re image — la tête
        // repartait à 0 au lieu de la position demandée (mesuré le 2026-10-11).
        let t = gst::ClockTime::from_nseconds(Self::unit_time(n, rn as u32, rd as u32));
        seg.set_start(t);
        seg.set_time(t);
        seg.set_position(t);
        true
    }

    fn size(&self) -> Option<u64> {
        // Inconnue tant que le fichier s'écrit : cf. l'en-tête.
        if self.growing() {
            return None;
        }
        self.run.lock().unwrap().file.as_ref().and_then(|f| f.metadata().ok()).map(|m| m.len())
    }

    fn unlock(&self) -> Result<(), gst::ErrorMessage> {
        self.run.lock().unwrap().flushing = true;
        self.wake.notify_all();
        Ok(())
    }

    fn unlock_stop(&self) -> Result<(), gst::ErrorMessage> {
        self.run.lock().unwrap().flushing = false;
        Ok(())
    }

    fn fill(&self, offset: u64, length: u32, buffer: &mut gst::BufferRef) -> Result<gst::FlowSuccess, gst::FlowError> {
        let (fs, data_offset, length_offset, rate_n, rate_d, stop_ns) = {
            let s = self.settings.lock().unwrap();
            (s.frame_size as u64, s.data_offset, s.length_offset, s.rate_n, s.rate_d, s.stop_ns)
        };
        let mut r = self.run.lock().unwrap();
        if fs > 0 && stop_ns > 0 && Self::unit_time(r.n, rate_n, rate_d) >= stop_ns {
            return Err(gst::FlowError::Eos);
        }
        // Mode unités : on lit l'unité n, quel que soit l'offset demandé par GstBaseSrc.
        let (offset, length) = if fs > 0 { (data_offset + r.n * fs, fs as u32) } else { (offset, length) };
        let want = offset + length as u64;
        // Attente de la suite : seulement tant que le fichier s'écrit.
        loop {
            if r.flushing {
                return Err(gst::FlowError::Flushing);
            }
            let size = r.file.as_ref().and_then(|f| f.metadata().ok()).map(|m| m.len()).unwrap_or(0);
            let now = Instant::now();
            if size != r.seen {
                r.seen = size;
                r.seen_at = Some(now);
            }
            if fs > 0 {
                if let Some(end) = r.file.as_ref().and_then(|f| Self::essence_end(f, data_offset, length_offset)) {
                    if want > end {
                        return Err(gst::FlowError::Eos);
                    }
                }
            }
            if size >= want {
                break;
            }
            let (growing, idle_ms) = {
                let s = self.settings.lock().unwrap();
                (s.growing, s.idle_ms)
            };
            if !growing {
                break;
            }
            if idle_ms > 0 && r.seen_at.is_some_and(|t| now.duration_since(t) > Duration::from_millis(idle_ms as u64)) {
                gst::warning!(CAT, imp = self, "aucun octet neuf depuis {} ms : fichier tenu pour clos", idle_ms);
                self.settings.lock().unwrap().growing = false;
                drop(r);
                let _ = self.obj().post_message(
                    gst::message::Element::builder(gst::Structure::builder("bobigrowsrc-idle").field("size", size).build())
                        .src(&*self.obj())
                        .build(),
                );
                r = self.run.lock().unwrap();
                continue;
            }
            r = self.wake.wait_timeout(r, POLL).unwrap().0;
        }
        if fs > 0 && !self.growing() {
            // Clos : la fin de l'essence est sa longueur RÉELLE (le pied de fichier suit).
            let f = r.file.as_ref().ok_or(gst::FlowError::Error)?;
            let mut end = f.metadata().map(|m| m.len()).unwrap_or(0);
            if let Some(e) = Self::essence_end(f, data_offset, length_offset) {
                end = end.min(e);
            }
            if want > end {
                return Err(gst::FlowError::Eos);
            }
        }
        let f = r.file.as_ref().ok_or(gst::FlowError::Error)?;
        let n = {
            let mut map = buffer.map_writable().map_err(|_| gst::FlowError::Error)?;
            let mut got = 0usize;
            while got < map.len() {
                match f.read_at(&mut map[got..], offset + got as u64) {
                    Ok(0) => break,
                    Ok(k) => got += k,
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(e) => {
                        gst::element_imp_error!(self, gst::ResourceError::Read, ["lecture à {} : {}", offset, e]);
                        return Err(gst::FlowError::Error);
                    }
                }
            }
            got
        };
        if n == 0 {
            return Err(gst::FlowError::Eos);
        }
        if fs > 0 {
            if (n as u64) < fs {
                return Err(gst::FlowError::Eos);
            }
            let k = r.n;
            r.n += 1;
            let t0 = Self::unit_time(k, rate_n, rate_d);
            buffer.set_pts(gst::ClockTime::from_nseconds(t0));
            buffer.set_duration(gst::ClockTime::from_nseconds(Self::unit_time(k + 1, rate_n, rate_d) - t0));
            buffer.set_offset(k);
            buffer.set_offset_end(k + 1);
            return Ok(gst::FlowSuccess::Ok);
        }
        buffer.set_size(n);
        buffer.set_offset(offset);
        buffer.set_offset_end(offset + n as u64);
        Ok(gst::FlowSuccess::Ok)
    }
}

impl URIHandlerImpl for BobiGrowSrc {
    const URI_TYPE: gst::URIType = gst::URIType::Src;

    fn protocols() -> &'static [&'static str] {
        &["bobigrow"]
    }

    fn uri(&self) -> Option<String> {
        let loc = self.settings.lock().unwrap().location.clone()?;
        glib::filename_to_uri(&loc, None).ok().map(|u| u.replacen("file://", "bobigrow://", 1))
    }

    fn set_uri(&self, uri: &str) -> Result<(), glib::Error> {
        let file_uri = uri.replacen("bobigrow://", "file://", 1);
        let (path, _host) = glib::filename_from_uri(&file_uri)
            .map_err(|e| glib::Error::new(gst::URIError::BadUri, &format!("URI {uri} : {e}")))?;
        self.set_location(Some(path.to_string_lossy().into_owned()))
    }
}
