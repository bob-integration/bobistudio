# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sources 2110 list must not show the bus inputs of an engine's outputs.

A 2110 engine carries, under one vmid tag, its RTP receivers (network sources) and one
MXL-transport receiver per TX slot. Only the first kind is a source. No database, no network."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Throwaway database, set BEFORE the first access: the connection is kept per thread,
# so overriding DB_PATH afterwards isolates nothing.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)

from app.routes.nmos_detail import _is_tx_input_receiver as hidden   # noqa: E402

failures = []


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        failures.append(label)


engine = {"deploy_config": json.dumps({"type": "2110_io", "params": {}})}
other = {"deploy_config": json.dumps({"type": "multiview", "params": {}})}
rtp = {"transport": "urn:x-nmos:transport:rtp.mcast"}
mxl = {"transport": "urn:x-nmos:transport:mxl"}

check("engine: the bus input of an output is hidden", hidden(mxl, engine) is True)
check("engine: an RTP receiver is a source", hidden(rtp, engine) is False)
check("another container keeps its MXL receivers", hidden(mxl, other) is False)
check("engine config already decoded is understood", hidden(mxl, {"deploy_config": {"type": "2110_io"}}) is True)
check("unknown container: nothing is hidden", hidden(mxl, None) is False)
check("unreadable config: nothing is hidden", hidden(mxl, {"deploy_config": "{"}) is False)

if failures:
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("OK: all checks passed")
