# Writing a Bobi.Studio plugin

*[Version française](AUTHORING.fr.md)*

A plugin is a **type of Docker container** driven by the orchestrator. It lives in a
`plugins/<type>/` folder, versioned in its own git repository (a submodule).

> **Start by reading code, not this guide.** `plugins/hello_world/` is the **executable**
> reference for the contract: it shows all three essences in and out, slice mode, macro exposure,
> the public page and the metrics — every rule commented with the *why* and with what breaks
> **silently** when it is left out. This guide gives you the map; the example gives you the
> ground.
>
> `tests/verif_plugin_hello_world.py` checks that example in continuous integration. A guide, on
> the other hand, does not execute: if you find something here that the code contradicts, **the
> code is right**, and this file is what needs fixing.

---

## Layout

```
plugins/<type>/
├── plugin.json       ← manifest (required)
├── script.py         ← the plugin, run INSIDE the container (required)
├── hooks.py          ← logic run INSIDE THE ORCHESTRATOR (optional)
├── control.html/.js/.css  ← console on the plugin's page (optional)
├── i18n/{fr,en}.json ← console labels (required if there is a console)
├── help.md           ← article for the Help page (optional)
├── meta.json         ← version + changelog
└── versions/         ← archived versions, managed by the orchestrator
```

---

## The five rules that matter

The rest of this guide is reference material. These five decide whether your plugin is usable in
production, and forgetting them produces **no error at all** — just a product that lies.

### 1. Slice mode — mandatory for every new plugin

Read the input band by band (`get_slice`) and publish by progressive commit
(`commit(gi, valid_slices=k)`), instead of waiting for the whole frame.

A plugin that works whole-frame adds **one frame of latency** to every chain that crosses it, and
that debt shows on **no counter**: the plugin reports a perfect cadence. Measured on the `scope`
plugin: the compute left after the last line arrives drops from 5.09 to 1.48 ms, at equal
cadence.

**Contract**: `k` valid slices ⇔ lines `[0, k × slice_height)` written **on all three planes**
(Y, Cb, Cr). A consumer reading `k` slices must find the three planes consistent up to that line,
or it tears at the chroma boundary.

**Eligibility condition**: the processing must be **line-local** — each output line depends only
on the same input line and on the line number. A blur, a deinterlace, anything that looks at
neighbouring lines is not: stay whole-frame, and **write it down in the code**. Interlace and
line selection are the documented exceptions.

`slice_mode` goes into `config_schema` as `hidden: true`: the setting that matters is the global
switch under Settings → Video. A plugin exposing its own would leave a fleet configured at
random.

### 2. Expose everything to macros

Any feature or parameter not exposed to the macro system is a **dead capability**: it exists,
nobody can trigger it, and nothing says so.

| What you add | Where to declare it |
|---|---|
| continuous parameter, adjustable live | `param_tree` (element → group → typed/bounded parameter) |
| discrete action, file load, recall | `actions[]`, with `options_endpoint` for a live list |
| state readable as a **condition** | published in `/state`, declared in `control.read_endpoints` |

Every action or `param_tree` target must appear in `control.endpoints`: the proxy **refuses** an
undeclared path, and the macro then "succeeds" while changing nothing.

### 3. Metrics that say whether the stage does what it was asked to

`fps` only says the loop is turning. Publish at least:

- `slice_mode` — is the output **really** published in slices;
- `own_latency_ms` — compute time per frame, hence the margin;
- `source` — where the picture comes from.

And per input when there is more than one: "nothing is arriving" does not say **which** one is
missing, and "not wired" is not "wired but silent" — those are opposite faults, one is fixed at
the patch, the other at the producer.

### 4. The output does not depend on its producer

Publish your flows **even with no input at all**: coloured background, audio silence, regenerated
ANC. A subscribed downstream must not see its chain go dark because an upstream source fell over.

That is also what makes a plugin deployable **with nothing wired** — hence usable as a smoke test
on installation day, precisely when no chain exists yet.

### 5. Survive SIGBUS and exceptions

A producer that re-creates its flow invalidates the readers' memory mapping. The trap: the dead
generation stays **readable** — grains are served, the index frozen, no exception. Without a
`SIGBUS` handler the process dies on a signal and Docker restarts it in a loop with nobody
understanding why.

And an uncaught exception in the loop restarts the container without ever saying **why**: the
operator reads "restarted" in the alerts, and nothing more.

---

## `script.py` — the template

The script is a **`str.format()` template** with exactly three substitutions:

| Substitution | Injected value |
|---|---|
| `{config}` | `repr(params)` — Python dict of the deployed parameters |
| `{hostname}` | the container's hostname |
| `{plugin_version}` | the plugin version at deploy time |

**Critical rule**: every literal brace must be **doubled**, comments and f-strings included.

```python
# ✅ correct
state = {{"running": False, "fps": 0}}
url = f"http://{{ip}}:{{port}}/path"

# ❌ the plugin vanishes from the registry
state = {"running": False}
```

**Guard rail**: `plugins._scan()` runs a `.format()` dry run at start-up. A plugin with an
undoubled brace is **discarded** — it appears neither in the palette nor in the navigation, and
that takes far longer to diagnose than an outright error. `tests/check_plugins.py` checks it in
CI.

⚠ Rendering is not enough: a script that renders may well fail to **compile**, and the container
then loops in silence. Check both:

```bash
./venv/bin/python - <<'EOF'
import json
m = json.load(open("plugins/my_plugin/plugin.json"))
r = open("plugins/my_plugin/script.py", encoding="utf-8").read().format(
    config=repr(dict(m["deploy_defaults"])), hostname="test", plugin_version=m["version"])
compile(r, "<rendered>", "exec")
print("render + compile OK")
EOF
```

### Reaching the parameters

```python
CONFIG = {config}
HOSTNAME = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

my_value = CONFIG.get("my_key") or "default"
```

### The two ports

| Port | Role |
|---|---|
| `8080` | metrics — `GET /` returns the JSON the orchestrator reads |
| `8082` | control — the endpoints declared in `control.endpoints` |

Port 8080 also receives **pushes from the TSL service** (tally and live labels): a GET-only
server would answer 501 and the tally would never arrive, with nothing to signal it.

---

## `hooks.py` — what runs in the orchestrator

This is the **one exception** to the rule "no plugin code in the controller". The file is
imported and executed by the orchestrator, with its rights: the database, the agent tokens, the
control network. Write it accordingly — frugal, free of side effects, never blocking: it is
called on the path of an operator's gesture.

A hook that raises is ignored with a warning in the log; it does not block the deployment.

### The recognised hooks

| Hook | When |
|---|---|
| `before_deploy(params, context)` | before the script is rendered — normalise, resolve, inject |
| `wire_followers(kind, shm, slot, params, ctx)` | wiring one essence makes others follow |
| `wire_input` / `unwire_input` | an input is wired or unwired |
| `consumes` / `consumed_shms` / `produced_shms` / `produced_flow_count` / `source_shm` | topology and cabling |
| `topology_ports` | ports shown on the Cabling page |
| `tally_targets(params, context)` | what the TSL distributor must resolve |
| `ember_sources` / `ember_targets` / `ember_clear_slot` | Ember+ tree |
| `control_action` / `sync_state` | actions and state on the orchestrator side |

Read `plugins/hello_world/hooks.py`: it implements three of them (`before_deploy`,
`wire_followers`, `tally_targets`), each with the reason it exists.

⚠ **Adding or changing `hooks.py` requires a registry reload** — the orchestrator imports it
once, at scan time. Without a reload the hook **never fires**: a perfectly silent failure.
Settings → Plugins → *Reload*, or `POST /api/plugins/reload`.

### What a hook is allowed to do

Contrary to what an earlier version of this guide claimed, a hook **may** read the database and
the settings: that is often its very purpose, since the container has no access to them.
`hello_world` uses it to resolve the video format from the Settings list, the orchestrator's time
zone and the system's default language.

What a hook must not do: block (a long network call, a lock), leave lasting side effects, or
depend on mutable global state.

---

## `plugin.json` — the manifest

Required fields: `type`, `label`, `version`, `script_template`.

Notable ones:

| Field | Role |
|---|---|
| `deploy_defaults` | default parameters at creation |
| `wiring` | `produces` / `consumes` / `mode` — every input declares its `state_field` |
| `config_schema` | fields of the deployment palette (Tier 1) |
| `control.endpoints` | allow-list of proxy paths — anything else gets a 403 |
| `control.read_endpoints` | subset readable with the login alone (state, preview) |
| `param_tree` / `actions` | the surface exposed to macros |
| `ui.public_page` | allows a public `/p/<token>` link |
| `nav` | section and route. **Without `nav`**, no palette chip and no nav entry |
| `help.category` / `help.order` | placement of the help article |

---

## Control console

`control.js` exposes `window.MXLPlugins.<type> = { mount, unmount }`.

Four rules, each for a fault already seen in the wild:

1. **Controls come from the catalogue** (`window.MXLControls`), never rewritten. Knobs, switches,
   gauges and toolbars are already drawn, keyboard-accessible and consistent across pages. The
   living inventory is under Settings → Controls. **Before creating a new one: ask.**
2. **Geometry comes from the shared engine** `window.MXLLayout` — align, equalise, distribute,
   snap, multiple selection. Four copies of it had already diverged.
3. **One function builds every URL.** A console can be mounted behind a public token with a
   different API base (`ctx.base`): one forgotten `if (public)` hits the private API and fails
   with a 401 that explains nothing.
4. **Polling stops on unmount** (`clearInterval` in `unmount`), or it outlives the page and
   accumulates with every mount.

**i18n is mandatory, and it lives in the plugin**: labels go in the plugin's own
`i18n/{fr,en}.json` — prefix `plugin.<type>.` for the console, `type.<type>.*` for the palette
and the wiring ports. French remains the reference for default values.

⚠ **Never put them in the core catalogue** (`i18n/<lang>.json` at the repo root). Two things
break, both silently:

- The plugin **travels without its languages**. Installed from the Catalogue on another
  instance, the key does not exist there and `plugins._traduit` falls back to the label in the
  **manifest** — which is French. Nothing errors; the English UI just shows French.
- The core **wins** over the plugin (`i18n._file_catalog_for` ends with `merged.update(core)`).
  A key left at the core **shadows** the plugin's: the author edits their file, reloads, and
  sees nothing change.

`tests/check_i18n_scope.py` enforces this in CI. It was written after finding 444 palette keys
sitting in the core for 19 plugins out of 20 — only `hello_world`, written after this rule
existed, got it right.

---

## Versions

`meta.json` carries the version and the changelog. To publish:

1. change the code;
2. bump `version` in `plugin.json` **and** `meta.json`, with a changelog entry;
3. reload the registry.

Archiving under `versions/<ver>/` is done by the orchestrator when a package is installed
(`install_package`) — not by hand. Containers already deployed keep running on their version
until they are redeployed: the Plugins page shows the drift.

---

## Git submodules

Each plugin is an independent repository.

```bash
cd plugins/my_plugin
git add . && git commit -m "feat: my change"
git push

cd ../..
git add plugins/my_plugin
git commit -m "chore: bump plugin my_plugin"
```

Clone the superproject with its plugins: `git clone --recurse-submodules <url>`.

---

## Creating a plugin

1. **Copy `plugins/hello_world/`** rather than starting from a blank page.
2. Rename the `type` in `plugin.json`, strip what you do not need.
3. Reload the registry — the plugin is scanned and registered.
4. Deploy it with nothing wired: it must produce a picture.
5. Check both the rendering **and** the compilation of the template (box above).

---

## Proposing a plugin

The catalogue reads a single trusted GitHub organisation, and that is not a convenience:
installing a plugin runs its `hooks.py` **inside the orchestrator**. A third-party plugin
therefore does not appear there on its own.

Two routes:

- **Propose it** — open an issue on the public repository **before** writing a thousand lines:
  what it is for, which essences, which settings. If it is taken up, develop it in your own
  repository; it will then be brought into the organisation and appear in everyone's catalogue.
- **Distribute it yourself** — export a `.mxlplugin` package from Settings → Plugins. Anyone can
  install it deliberately, without asking you for anything.

Either way: a **GPL-3.0**-compatible licence if the plugin is to be hosted in the organisation,
and pass the conformance checks before proposing.

---

## Security

- `script.py` runs **inside the container only**, never in the orchestrator — the controller's
  credentials do not leak. `hooks.py` is the exception, see above.
- The `/api/containers/<vmid>/plugin/*` endpoints are filtered by `control.endpoints`: any
  undeclared path returns 403.
- UI asset paths are sanitised to stay inside the plugin's folder.
- An imported package is extracted with anti *zip-slip* protection, its manifest validated and
  its template rendered dry — **no plugin code is executed** on import.
