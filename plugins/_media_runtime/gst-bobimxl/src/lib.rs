// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Plugin GStreamer « bobimxl » : `bobiplayout`, la sortie genlockée du player sur le bus MXL
// (cf. docs/chantiers/PLAYER_NATIF.md), et `bobigrowsrc`, la lecture d'un fichier en cours
// d'enregistrement (URI `bobigrow://`).

use gst::glib;
use gstreamer as gst;

mod growsrc;
mod playout;

fn plugin_init(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    playout::register(plugin)?;
    growsrc::register(plugin)
}

gst::plugin_define!(
    bobimxl,
    env!("CARGO_PKG_DESCRIPTION"),
    plugin_init,
    env!("CARGO_PKG_VERSION"),
    "GPL",
    "gst-bobimxl",
    "gst-bobimxl",
    "https://github.com/bob-integration/bobi.studio"
);
