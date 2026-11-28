# Browser bench: multicast plan editor (Settings → Multicast)

Opens the real editor in a real browser engine, plays the operator's scenario and checks what
cannot be seen by reading code: alignment, clipping, focus, touch targets, console errors.
It needs a browser, so it lives here and not in `tests/`.

## What it runs

- `harness.py` (repo venv): throwaway SQLite DB created before any import that opens a DB,
  seeded with 3 media networks (no saved rule / saved rule + `first_fabric: blue` / one holding a
  pre-existing plan), a node with a red/blue pair, a node with an unpaired media interface, and one
  `2110_io` engine with 8 TX slots PROVISIONED and 3 ACTIVE (`active_tx_count: 3`; video + 2
  audios + ANC each; two of the provisioned slots already carry addresses). It renders the real
  `templates/settings.html` with the Flask test client (forged session, as
  `tests/verif_rendu_settings.py` does) and serves it with `/static/` on `127.0.0.1:<random>`.
  The plan API (`/api/mcast_targets`, `/api/flow_classes`, `/api/mcast_plans` GET/POST/PUT/DELETE,
  `/api/mcast_plans/preview`) is answered by the real `app.mcast_plans` functions on that DB.
  `POST /api/settings` goes to the real save path on the same DB, so a reload shows what was
  stored. Other GETs of the page are answered by the real app on the same DB (right shape, so other tabs
  do not throw); any other request gets an empty 200. Never touches `db_bobistudio.db` or `:5000`.
  Invented addresses only (239.20.x.x, 239.30.x.x).
- `scenario.py` (a Python with Playwright): one fresh harness per run, Chromium at 1440×900, then
  1024×768 with touch (iPad, desktop-site mode). Steps: tab loading state; new plan with the B rule
  still to choose (no dialog, amber button, B read-only, save blocked); rule dialog (nothing
  preselected, empty → error, centred); network with a saved rule (blue first); video /28 read-outs,
  derived B, capacity; audio + ANC lines (two-step type, one port per essence); `/xx` ↔
  `first – last` round trips; B typed by hand; overlap errors and warnings; flow states with
  unmatched rows; pair and unpaired interface targets; save / edit / delete; slot offsets.
  Also: the global pool on this tab (loaded, span read-out, invalid base refused, saved by its own
  button, values back after a reload, untouched by the Cluster save); active outputs first and
  provisioned ones folded (closed), shortage on active outputs = error, on provisioned ones =
  information; fixed ports refused at save with the error under each missing field (operator's
  path: save first, then type); ports hint per mode; an audio line in "slot" rank whose 2nd audio
  falls outside the line (error, step field opened); every line on one row at 1440, and below
  that the ports + free/capacity block moving as a whole, never a lone column.

The harness subprocess and the browser are stopped in a `finally`, whatever fails.

Checks that fail the run (exit 1): any console error or page error, any failed request (a 400
from the preview is allowed only where the scenario provokes the overlap), any broken
behavioural check, and the alignment checks: inside each line, every input, select, stepper and
button of a visual row shares the same top and height (±1 px), touch targets ≥ 34 px, nothing
clipped, no horizontal scroll. Shared-catalogue gaps (stepper buttons 30 px wide, the
`ios-toggle` switch, no `.btn:disabled` look) are printed as `[WARN]` and do not fail.
`KNOWN_UNRELATED` lists console errors raised by other parts of the page; they are printed, not
counted.

## Running it

Playwright is NOT in the repo venv. Use a separate venv whose Playwright matches the browsers in
`~/.cache/ms-playwright` (no download):

| installed folder | Playwright |
|---|---|
| `chromium_headless_shell-1243` | 1.63.0 |
| `webkit-2070` | 1.47.0 (WebKit did not start on the dev controller: missing libicu66, libxslt, libwoff2, libevent…) |

```bash
python3 -m venv /tmp/pw-venv
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 /tmp/pw-venv/bin/pip install playwright==1.63.0
cd /opt/bobistudio
/tmp/pw-venv/bin/python tools/bancs/banc_ui_mcast/scenario.py --out /tmp/mcast-shots
```

Options: `--engines chromium,webkit`, `--delay 1.5` (seconds the targets API is held back to
observe the loading state), `--inject-css '<css>'` (mutation check: break the layout on purpose,
e.g. `.mp-line-main .mp-span-btn{margin-top:10px!important}`, and the run must go red).

Screenshots: `<out>/<engine>-<width>-<n>-<step>.png`, one per step. Look at them: the checks
measure geometry, not taste.

`harness.py` alone serves the page for manual browsing:
`./venv/bin/python tools/bancs/banc_ui_mcast/harness.py`.
