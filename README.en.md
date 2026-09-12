# Bobi.Studio

*[Version française](README.md)*

> ## Beta release
>
> - **The APIs are not frozen.** The plugin proxy, the control endpoints and the `.mxlplugin`
>   format may change. Versions are `0.x.y`: the zero major says exactly that. A `1.0` will
>   commit to a compatibility we would rather promise once and keep.
> - **Try it off air first.** This is a broadcast product: what breaks in it breaks live.
>
> Two kinds of feedback help us most: what blocks you during installation, and what the
> documentation fails to say. Please open an issue.

Web orchestration interface for a **ST 2110** video pipeline on a **full-Docker** architecture.
A central Flask orchestrator (the controller) drives enrolled **nodes** that run the production
containers: 2110 receive/transmit, mixing, multiview, encoding, recording. Internal video and
audio transport goes over the **MXL bus** (MXL SDK, shared memory `/dev/shm/mxl`).

## Main features

- **Dual-role ST 2110 engine** (`2110_io`): receive + transmit via MTL/DPDK (AF-XDP,
  kernel-bypass), composable flows, 2022-7 redundancy driven by the node's declared interface
  pairing
- **Streams**: multi-destination encoding (UDP / SRT / WebRTC), multi-track audio, WHEP preview
- **Plugin system**: every container type is a versioned plugin (`plugins/<type>/`) —
  deployable, updatable and pinnable on its own
- **Live cabling**: a Cables page with graphical topology, hot source switching, automatic UDC
  insertion
- **NMOS**: **IS-04** (registration/discovery), **IS-05** (connection), **IS-07** (events —
  client, incoming and outgoing), **IS-12** and **IS-14** (MS-05-02 control), BCP-002, BCP-008
- **Other protocols**: SAP/SDP (AES67/Ravenna announce and discovery), Ember+, TSL 5.0
  (tally/UMD), read-only SNMPv3
- **Control surfaces**: ATEM switcher emulator (UDP 9910) and Skaarhoj Quick Bar integration
  (Raw Panel Protocol over TCP)
- **Production slots**: a **functional** identity, stable across container replacement — this
  is what an external control system addresses, never the disposable internal handle
- **Macros and triggers**: chains of actions and continuous parameters across a project's
  containers
- **WebRTC monitoring**: a global per-user side panel (embedded MediaMTX stream), with
  Monitoring buttons on the producing pages
- **Multi-node**: Docker node enrolment (node agent), inter-node **RDMA** replication of the
  MXL bus, multicast allocation
- **Measured resources**: CPU profiles per container type, NUMA-aware isolated core allocation,
  calibration
- **Fleet health**: per-node supervision (CPU, RAM, disk, sensors, PTP, GPU, RDMA, memory
  bandwidth)
- **Node installation**: dedicated installer, PXE/UEFI HTTP network boot or USB media, iLO
  takeover
- **Logging**: container logs, an operations journal, and an audit trail of user actions
- **Projects**: snapshots of cabling and configuration, recall with progress, per-user
  restricted access
- **i18n**: bilingual FR/EN interface

## Architecture

- **Controller**: a Flask application (port 5000) plus a supervision thread. No production
  container runs on it.
- **Nodes**: enrolled Debian machines (`nodes` table), driven by `app/node_driver.py` through a
  **node agent**.
- **Containers**: created by `app/docker_driver.py` (ST 2110 MTL engine, dedicated AF-XDP NIC)
  and `app/docker_compute.py` (compute/media, macvlan).

## Documentation

| Document | For whom |
|---|---|
| [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md) | **Before buying**: CPU, RAM and memory-bandwidth choices, network cards |
| [`INSTALL.md`](INSTALL.md) | **Commissioning**: from bare machine to first flow, with the points that matter |
| [`NODE_AGENT.md`](NODE_AGENT.md) | The node agent's HTTP contract |
| [`HA.md`](HA.md) | Controller high availability (warm standby) |
| [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | Third-party components and licences |

These documents are also rendered **inside the interface**, on the **Help** page — same source,
no copy to maintain. The online help additionally carries one section per plugin
(`plugins/<type>/help.md`). Several of these documents are in French only for now.

## Deployment

### On a bare machine, in one command

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobistudio/main/get.sh)
```

This is the shortest path, and it needs **neither `git` nor a prebuilt package**: GitHub serves
an archive per repository, so `curl` and `tar` are enough. The script picks its language from
the machine's, lists the published versions and lets you choose (default: the most recent; `d`
for the development branch), fetches the source, then opens the unified installer's menu
described below.

Useful options: `--liste` prints the available versions and stops, `--ref <tag|branch>` targets
one directly without the menu, and `--dry-run` fetches and verifies the source **without
installing anything** — enough to look before committing. `BOBI_LANG=fr|en` forces the language,
which matters for unattended installs. Full details in
[INSTALL.md](INSTALL.md#20-depuis-github-sur-une-machine-vierge-le-plus-court) (in French).

### Unified installer

`install.py` runs **as root, next to a `bobistudio.zip`**: it is a package installer, not a
script to run from a git clone. `get.sh` above puts it in place for you; otherwise there are two
ways to get it:

```bash
# From a controller already in service (it serves both the installer and the zip):
curl -O http://<controller>:5000/install/install.py
curl -O http://<controller>:5000/install/bobistudio.zip
sudo python3 install.py

# Or by building the package from source:
python3 tools/build_dist.py      # produces dist/bobistudio.zip + dist/install.py
cd dist && sudo python3 install.py
```

An interactive menu (standard library only) that provisions the machine as a **process node**
(node agent alone), an **orchestrator** (Flask controller), or **all-in-one** (orchestrator plus
a local node).

To install **from a git clone**, run `bash install.sh`: the bootstrap checks python3 (and
offers to install it), then hands over to the installer.

### Manual controller installation

```bash
cd /opt/bobistudio
cp config_local.example.py config_local.py
nano config_local.py     # site values (hosts, tokens, secrets)
bash install.sh          # venv + dependencies + systemd service
systemctl start bobistudio
```

### Enrolling a node

```bash
# On the node machine (Debian 13), capabilities à la carte:
./node_agent/install-node.sh --with compute,media \
    --macvlan-parent eno1 --macvlan-subnet 10.x.x.0/24 --macvlan-gateway 10.x.x.254
```

Then declare the node (URL plus the token printed at the end of the installation) in the
interface.

### Service management

```bash
systemctl {start|stop|restart|status} bobistudio
journalctl -u bobistudio -f    # live logs
```

### Running manually (development)

```bash
./venv/bin/python main.py      # Flask on 0.0.0.0:5000
```

## Configuration

| File | Role |
|---|---|
| `config_local.py` | Site-specific values (hosts, tokens, secrets) — **not versioned** |
| `config_local.example.py` | Template to copy |
| `app/config.py` | Neutral defaults (no secrets) |

Everything else (ST 2110 networking, NMOS, TSL, WebRTC, PTP, theme, users…) is configured from
the web interface, under **Settings**.

## Layout

```
main.py               ← Flask entry point (port 5000) + supervision thread
app/                  ← Python modules (deploy, plugins, node_driver, docker_driver, macros,
                        core_pool/placement, node_health, ptp, ha…) + app/routes/ (REST API)
plugins/              ← one git submodule per container type (plugin.json + script.py + UI)
                        plus `_*_runtime` directories (shared Docker images, not plugins)
services/             ← orchestrator services (nmos, emberplus, tsl, atem, skaarhoj, rdma,
                        alerting, files, media_manager, storage, webrtc_gateway…)
node_agent/           ← node agent (agent.py) + node installer (install-node.sh) + iso/
templates/            ← Jinja2 HTML views
static/               ← scripts.js, CSS (base.css, nav.css, themes), uploads/
i18n/                 ← translation catalogues (fr.json, en.json)
script_templates/     ← agent.py (per-container agent), bobimxl.py (MXL SDK), mxl_bench.py
tools/                ← utilities (create_admin.py, build_dist.py…)
docs/design/          ← UI guidelines, product doctrine, business context
docs/reference/       ← authoritative reference documents (TX layouts, clock model,
                        MXL interop, 2110 probe, projects)
docs/chantiers/       ← dated engineering journals (measurements, ports, decisions)
HA.md                 ← high availability (warm standby)
NODE_AGENT.md         ← node agent HTTP contract
CHANGELOG.md          ← version history (rendered on the Help page)
```

## Creating an administrator account

```bash
./venv/bin/python tools/create_admin.py
```

## Infrastructure prerequisites

- Controller: Debian 13 (trixie) with Python 3.13 (VM or dedicated machine)
- Nodes: Debian 13 (trixie) with Docker; for the `io2110` role, an Intel E810 NIC (MTL/DPDK
  AF-XDP) plus hugepages
- Network: a ST 2110 media plane, a macvlan segment for compute/media containers, and a control
  plane
- For WebRTC: the MediaMTX gateway is deployed from Settings → WebRTC

## Licence

Copyright (C) 2026 BOBI SAS, France
Author: Cyril Mazouer, on behalf of BOBI SAS.

Bobi.Studio is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License version 3** as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
the GNU General Public License for more details.

The full text is in the [`LICENSE`](LICENSE) file or at
<https://www.gnu.org/licenses/gpl-3.0.html>.

### Third-party components

Bobi.Studio integrates third-party components (MXL SDK, Intel Media Transport Library, DPDK,
FFmpeg, MediaMTX…) which remain subject to their own licences. The inventory, the copyright
notices to preserve and the points to watch are in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## Development

This project was developed with the assistance of Claude (Anthropic) as a code-generation tool,
under the direction and supervision of Cyril Mazouer.
