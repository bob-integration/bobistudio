# Draft — issue for dmf-mxl/mxl

> Prepared and **filed 2026-10-16: <https://github.com/dmf-mxl/mxl/issues/776>**. Target: <https://github.com/dmf-mxl/mxl/issues>,
> "Bug report" template. Searched beforehand (issues and PRs, open and closed: "mxl-tmp",
> "temporary directory", "createContinuousFlow", "continuous flow", "tmp leak", "No space left"),
> and checked the four open PRs touching `FlowManager.cpp` (#572, #653, #654, #746): none covers this.
> Still present on `main` at `16849aa`.

> **CLOSED upstream 2026-10-19.** Fixed by PR #777 (`jonasohland`), which applies to the continuous
> path the same `defer`-based cleanup the discrete path already had (+37/−35 in `FlowManager.cpp`)
> and adds 57 lines of test. Backported to `release/v1.1` by #781 (the automatic backport #780 was
> not retained) ⇒ ships in 1.1.1. Our local sweep workaround described below can be dropped once we
> move to 1.1.1 — but see the `mxl-fabrics-demo` C++ target blocker before that upgrade.

---

**Title:** Creating a continuous (audio) flow writer on an existing flow leaks a `.mxl-tmp-*` directory in the domain

**Describe the bug**

`FlowManager::createOrOpenContinuousFlow` builds the new flow in a temporary directory
(`.mxl-tmp-…`) and then tries to publish it with a no-replace rename. When the flow already exists,
the rename fails, the existing flow is opened and returned with `created == false` — but the
temporary directory is **not removed**. It is only removed on the exception path (`catch (...)`).

The discrete and event code paths do not have this problem: they remove the temporary directory in
a `defer`, on every path.

- v1.1.0, discrete — temporary directory removed by `defer`:
  [`FlowManager.cpp#L179-L189`](https://github.com/dmf-mxl/mxl/blob/v1.1.0/lib/internal/src/FlowManager.cpp#L179-L189)
- v1.1.0, continuous — removed only in `catch (...)`:
  [`FlowManager.cpp#L273`](https://github.com/dmf-mxl/mxl/blob/v1.1.0/lib/internal/src/FlowManager.cpp#L273),
  [`#L310`](https://github.com/dmf-mxl/mxl/blob/v1.1.0/lib/internal/src/FlowManager.cpp#L310) (`return {false, …}` without cleanup),
  [`#L313-L318`](https://github.com/dmf-mxl/mxl/blob/v1.1.0/lib/internal/src/FlowManager.cpp#L313-L318)
- `main` (`16849aa`), same pattern:
  [`#L344`](https://github.com/dmf-mxl/mxl/blob/16849aad95d73c76380645c566e44fa86c978062/lib/internal/src/FlowManager.cpp#L344),
  [`#L381`](https://github.com/dmf-mxl/mxl/blob/16849aad95d73c76380645c566e44fa86c978062/lib/internal/src/FlowManager.cpp#L381),
  [`#L384-L389`](https://github.com/dmf-mxl/mxl/blob/16849aad95d73c76380645c566e44fa86c978062/lib/internal/src/FlowManager.cpp#L384-L389)
  — while the event flow path added since then uses `defer`
  ([`#L268-L277`](https://github.com/dmf-mxl/mxl/blob/16849aad95d73c76380645c566e44fa86c978062/lib/internal/src/FlowManager.cpp#L268-L277)).

Each leaked directory contains `flow_def.json`, `data` and `channels`: about **625 KB** for an
8-channel, 48 kHz float32 flow. Nothing ever reclaims them: they survive the release of every
writer, the deletion of the flow and `mxlDestroyInstance`, and `mxlGarbageCollectFlows` only looks
at `*.mxl-flow` directories
([`Instance.cpp#L314`](https://github.com/dmf-mxl/mxl/blob/v1.1.0/lib/internal/src/Instance.cpp#L314)).

**Impact (production incident)**

A producer process that re-created its writers after an error, once per second, while an earlier
writer of the same audio flow was still alive, left 74,388 such directories in 21 hours. The 47 GB
tmpfs holding the domain filled up, and from then on no process on that host could create any flow
(`Could not resize shared memory segment.: No space left on device`), for three days until it was
noticed. Video and data flows of the same process, re-created just as often, leaked nothing.

**To Reproduce**

Self-contained script, public C API through `ctypes`, throw-away domain. The first writer is kept
open so that the flow exists when the following writers are created — the same situation as two
producers of one flow, or a producer re-opening its writer while an old handle is still alive.

```python
#!/usr/bin/env python3
import ctypes, json, os, shutil, uuid

DOMAIN = "/dev/shm/mxl-tmp-leak-repro"          # throw-away domain
VOID = ctypes.c_void_p
lib = ctypes.CDLL("libmxl.so.1")
lib.mxlCreateInstance.restype = VOID
lib.mxlCreateInstance.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
lib.mxlDestroyInstance.argtypes = [VOID]
lib.mxlCreateFlowWriter.argtypes = [VOID, ctypes.c_char_p, ctypes.c_char_p,
                                    ctypes.POINTER(VOID), VOID, ctypes.POINTER(ctypes.c_bool)]
lib.mxlReleaseFlowWriter.argtypes = [VOID, VOID]

AUDIO = json.dumps({
    "id": str(uuid.uuid4()), "label": "repro_audio",
    "tags": {"urn:x-nmos:tag:grouphint/v1.0": ["repro:Audio"]},
    "format": "urn:x-nmos:format:audio",
    "media_type": "audio/float32", "sample_rate": {"numerator": 48000, "denominator": 1},
    "channel_count": 8, "bit_depth": 32})
VIDEO = json.dumps({
    "id": str(uuid.uuid4()), "label": "repro_video",
    "tags": {"urn:x-nmos:tag:grouphint/v1.0": ["repro:Video"]},
    "format": "urn:x-nmos:format:video",
    "media_type": "video/v210", "grain_rate": {"numerator": 50, "denominator": 1},
    "frame_width": 1920, "frame_height": 1080, "interlace_mode": "progressive",
    "colorspace": "BT709", "components": [
        {"name": "Y", "width": 1920, "height": 1080, "bit_depth": 10},
        {"name": "Cb", "width": 960, "height": 1080, "bit_depth": 10},
        {"name": "Cr", "width": 960, "height": 1080, "bit_depth": 10}]})


def tmp_dirs():
    return [e for e in os.listdir(DOMAIN) if e.startswith(".mxl-tmp-")]


def run(kind, flow_def, n=5):
    first = None
    for i in range(n):
        w, created = VOID(), ctypes.c_bool(False)
        st = lib.mxlCreateFlowWriter(inst, flow_def.encode(), None, ctypes.byref(w), None,
                                     ctypes.byref(created))
        print("%s writer #%d: status=%d created=%-5s  .mxl-tmp-* dirs in domain: %d"
              % (kind, i + 1, st, created.value, len(tmp_dirs())))
        if first is None:
            first = w                                # keep the first writer: the flow stays alive
        else:
            lib.mxlReleaseFlowWriter(inst, w)
    lib.mxlReleaseFlowWriter(inst, first)
    print("%s: all writers released -> .mxl-tmp-* dirs: %d" % (kind, len(tmp_dirs())))


shutil.rmtree(DOMAIN, ignore_errors=True)
os.makedirs(DOMAIN)
inst = lib.mxlCreateInstance(DOMAIN.encode(), None)
run("audio", AUDIO)
run("video", VIDEO)
for d in tmp_dirs()[:1]:
    p = os.path.join(DOMAIN, d)
    print("leaked:", d, sorted(os.listdir(p)),
          sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p)), "bytes")
lib.mxlDestroyInstance(inst)
print("after mxlDestroyInstance -> .mxl-tmp-* dirs: %d" % len(tmp_dirs()))
shutil.rmtree(DOMAIN, ignore_errors=True)
```

Output (libmxl `1.1.0-+0 g8f3f1096e804`):

```
audio writer #1: status=0 created=True   .mxl-tmp-* dirs in domain: 0
audio writer #2: status=0 created=False  .mxl-tmp-* dirs in domain: 1
audio writer #3: status=0 created=False  .mxl-tmp-* dirs in domain: 2
audio writer #4: status=0 created=False  .mxl-tmp-* dirs in domain: 3
audio writer #5: status=0 created=False  .mxl-tmp-* dirs in domain: 4
audio: all writers released -> .mxl-tmp-* dirs: 4
video writer #1: status=0 created=True   .mxl-tmp-* dirs in domain: 4
video writer #2: status=0 created=False  .mxl-tmp-* dirs in domain: 4
video writer #3: status=0 created=False  .mxl-tmp-* dirs in domain: 4
video writer #4: status=0 created=False  .mxl-tmp-* dirs in domain: 4
video writer #5: status=0 created=False  .mxl-tmp-* dirs in domain: 4
video: all writers released -> .mxl-tmp-* dirs: 4
leaked: .mxl-tmp-XXXXXXXXXXGRAfSh ['channels', 'data', 'flow_def.json'] 624946 bytes
after mxlDestroyInstance -> .mxl-tmp-* dirs: 4
```

**Expected behavior**

Creating a writer on an existing continuous flow leaves the domain as it found it, as it already
does for discrete flows: one `.mxl-tmp-*` directory may exist only for the duration of the call.

**Suggested fix**

Use the same `defer` as `createOrOpenDiscreteFlow` / `createOrOpenEventFlow` in
`createOrOpenContinuousFlow`, and drop the `try` / `catch (...)` that only existed for cleanup. On
the success path the directory has already been renamed into place, so the deferred
`remove_all` is a no-op there, exactly as in the discrete path. We have not built or tested this
change; we are happy to open a PR if that helps.

Possibly worth considering separately: `mxlGarbageCollectFlows` could also reclaim stale
`.mxl-tmp-*` directories (by age), so that a leak of this kind, or a process killed in the middle of
a flow creation, does not accumulate forever.

**Environment**

- libmxl `1.1.0-+0 g8f3f1096e804` (v1.1.0 GA); same code on `release/v1.1` and `main` (`16849aa`)
- Debian 13 (trixie), kernel 6.12, x86_64; domain on tmpfs (`/dev/shm`); producers in Docker
  containers sharing the domain through a bind mount

**Additional context**

We work around it on our side by sweeping, right after `mxlCreateFlowWriter` returns
`created == false` for a continuous flow, the temporary directories that carry that flow's id and
are older than a couple of seconds (so that a creation in flight in another process is never
touched).
