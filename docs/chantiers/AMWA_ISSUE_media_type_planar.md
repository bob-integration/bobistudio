# Brouillon d'Issue AMWA — un type de média PLANAR pour la vidéo MXL

⚠ **NON PUBLIÉE.** À ouvrir sur <https://github.com/AMWA-TV/nmos-parameter-registers/issues>
après relecture. Nous ne sommes pas membres AMWA : Issues oui, **pas de Pull Request**.

Rédigée le 2026-09-09. Séparée volontairement de
[`AMWA_ISSUE_transport_mxl.md`](AMWA_ISSUE_transport_mxl.md) : celle-là signale une **omission**
que BCP-007-03 impose (correction évidente) ; celle-ci est une **proposition** qui se discute.
Les mélanger ferait attendre la première derrière la seconde.

Ce qu'on demande n'est PAS de bénir notre nom `video/x-mxl-planar`, mais de trancher si un
tampon planaire non compressé a sa place au registre. On adoptera le nom retenu.

---

**Titre :** Media Types register: should there be a planar uncompressed video type for MXL?

**Corps :**

BCP-007-03 v1.0.0 requires every MXL Flow to set `media_type` to a value from the media types
parameter register. For MXL video the register currently offers `video/v210` and `video/v210a`.

We would like to ask whether a **planar** uncompressed video type belongs alongside them, and if
so, what it should be called. We are asking rather than proposing a name, because the answer
affects the SDK as much as the register.

### Why the question arises

MXL is shared memory, not a wire. A consuming Media Function maps the buffer and computes on it
in place. v210's 6-pixels-in-16-bytes packing exists to make efficient use of an RTP payload; in
shared memory that packing is not carried anywhere — it is unpacked and repacked by every
consumer, on every hop.

We measured this on a real MXL bus rather than in a micro-benchmark: 1080p50 4:2:2, Xeon Gold
6240R, a two-input stage (read A + read B → blend → write), progressive commit with 30 bands of
36 lines, ~996 frames per format, using a hand-written bit-exact SIMD (AVX2) v210 converter:

| bus format | bytes/frame | stage p50 | stage p99 |
|---|---|---|---|
| planar 8-bit  | 4.15 MB | 2.9 ms | 4.0 ms |
| planar 10-bit | 8.29 MB | 7.4 ms | 13.4 ms |
| v210 8-bit    | 5.53 MB | 8.2 ms | 15.2 ms |
| v210 10-bit   | 5.53 MB | 15.4 ms | **23.8 ms** |

At 50p the budget is 20 ms. A single v210 10-bit stage, doing a trivial blend, exceeds it at p99.
And that is with a SIMD converter written in C: an implementation converting in NumPy — which is
what a great many media functions are written in — pays roughly 17× the cost of planar rather
than roughly 3×.

Two caveats on those figures, so they are not read as stronger than they are. The converter was
compiled as a portable AVX2 build, not tuned to the host — the CPU has AVX-512, and that margin
is unexploited. And the conversion is called once per band (120 calls per frame), where a single
per-frame call would amortise better. A determined v210 implementation would land lower than the
table shows; our estimate is roughly 5 ms per stage, still about twice a complete planar stage.

To be fair to v210, the picture is not one-sided: v210 is ~33% less bus traffic than planar
10-bit, which matters on a memory-bandwidth-bound node, and the stock v210 grain commits per
line, which gives *better* per-band latency in our measurements (0.34 ms vs 0.53 ms). The
trade-off is real. Our reading is that it lands on the side of planar for CPU-based processing
chains, but we would not claim the question is settled for every implementation.

### Why we think it is a register question and not just ours

The register already contains `audio/float32`, applicable to BCP-007-03, described as "audio
stored as 32 bit float values". That is precisely an unpacked, compute-friendly representation
chosen over reusing a packed wire format such as `audio/L24`. The reasoning that justified it for
audio seems to us to apply to video as well.

### The interoperability failure mode, which may be the more useful part of this issue

We currently publish `video/x-mxl-planar` and carry a small patch to the reference SDK to add it.
While testing against an unpatched SDK (libmxl v1.1.0-beta-1, our patches removed, a consumer
compiled against the stock headers) we found an asymmetry: `mxlCreateFlowWriter` **rejects** an
unsupported video `media_type` (`flow.cpp:244 Unsupported video media_type`), but
`mxlCreateFlowReader` + `GetGrain` **succeed** — the reader maps the `mxlFlowInfo` struct and is
agnostic to the media type. A third party reading our flow therefore receives a correctly-sized
grain (8 294 400 bytes) of misinterpreted pixels, silently, rather than a refusal.

We should say plainly that this cuts against us as much as for us: it is the reason an
unregistered type is tolerable for us today, and the reason it should not be.

That is worth flagging independently of whether a planar type is ever registered: on a wire there
is an SDP to disagree about, but in shared memory an unrecognised `media_type` currently fails
silently. Rejecting unknown media types on read would turn a wrong picture into an error message.

### What we are doing meanwhile

We serve BCP-007-03 resources (IS-04 v1.3 / IS-05 v1.2, `mxl_domain_id` + `mxl_flow_id`,
`domain_def.json`, BCP-004-01 receiver capabilities) validated against the specification's own
JSON schemas, and we keep a v210 bridge that mirrors a planar flow as `video/v210` when a
specific third party needs to read a specific flow. Publishing `video/v210` on a planar buffer
was never an option — it would connect and decode noise.

We are not AMWA members, so we are raising this as an issue rather than opening a pull request,
and are happy to supply the measurement harness or the detailed figures if they are useful.
