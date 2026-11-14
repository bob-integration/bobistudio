# SPDX-License-Identifier: GPL-3.0-or-later
"""A script repair that cannot succeed must not be retried.

`metrics._redeploy_impossible` names the failures no retry can heal (a pinned plugin version that
cannot be recalled). Anything else returns None, so the normal backoff and quarantine still apply.
No database, no network: the plugin registry is replaced by stubs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import metrics, plugins   # noqa: E402

failures = []


def check(label, ok):
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        failures.append(label)


real_is_plugin, real_deploy_files = plugins.is_plugin, plugins.deploy_files
try:
    plugins.is_plugin = lambda t: t != "not_a_plugin"

    def refuse(type_, version=None):
        raise ValueError("%s %s: archived version, recall refused" % (type_, version))

    plugins.deploy_files = refuse
    why = metrics._redeploy_impossible("demo", {"plugin_version": "0.1.0"})
    check("a refused recall is reported as impossible", bool(why) and "0.1.0" in why)

    plugins.deploy_files = lambda type_, version=None: []
    check("a deployable plugin is retried", metrics._redeploy_impossible("demo", {}) is None)

    def crash(type_, version=None):
        raise OSError("disk hiccup")

    plugins.deploy_files = crash
    check("an unexpected error is NOT taken for impossible",
          metrics._redeploy_impossible("demo", {}) is None)

    plugins.deploy_files = refuse
    check("a non-plugin type is never judged here",
          metrics._redeploy_impossible("not_a_plugin", {}) is None)
finally:
    plugins.is_plugin, plugins.deploy_files = real_is_plugin, real_deploy_files

if failures:
    print("%d failure(s)" % len(failures))
    sys.exit(1)
print("OK: all checks passed")
