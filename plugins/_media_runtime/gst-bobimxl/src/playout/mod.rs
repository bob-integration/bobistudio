// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France

use gst::glib;
use gst::prelude::*;
use gstreamer as gst;

mod anc;
mod flowdef;
mod imp;

glib::wrapper! {
    pub struct BobiPlayout(ObjectSubclass<imp::BobiPlayout>) @extends gst::Element, gst::Object;
}

pub fn register(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    gst::Element::register(
        Some(plugin),
        "bobiplayout",
        gst::Rank::NONE,
        BobiPlayout::static_type(),
    )
}
