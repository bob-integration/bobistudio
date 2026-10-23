// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France

use gst::glib;
use gst::prelude::*;
use gstreamer as gst;

mod imp;

glib::wrapper! {
    pub struct BobiGrowSrc(ObjectSubclass<imp::BobiGrowSrc>)
        @extends gstreamer_base::BaseSrc, gst::Element, gst::Object,
        @implements gst::URIHandler;
}

pub fn register(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    // Rang NONE : choisi par le seul schéma `bobigrow://`, jamais à la place de filesrc.
    gst::Element::register(Some(plugin), "bobigrowsrc", gst::Rank::NONE, BobiGrowSrc::static_type())
}
