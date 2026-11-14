"""What the operator is told when a request fails for a reason no route anticipated.

Until 2026-11-13 the application registered NO error handler at all: any unhandled exception got
Flask's default HTML page, and every caller in the browser fell back to the same four characters —
« HTTP 500 ». That number says a request failed. It says nothing about why, and nothing about
whether the fault lies with what the operator just did.

The case that produced this module: the transit directory (cf. app/spool.py) was full, so writing
the body of ANY request with one failed. The operator was importing a plugin — a few hundred
kilobytes, nothing to do with the real cause, which was a driver archive uploaded hours earlier.
They saw « HTTP 500 » next to the Import button. Nothing on that screen, in the alerts, or in the
UI could connect the two. It took reading /proc by hand to find a deleted 1.92 GiB file still held
open by the orchestrator.

So this handler does two things the default page cannot:
  - it names ENOSPC for what it is, rather than letting it surface as a generic failure;
  - on ANY unhandled error, it MEASURES the transit directory, and if that is full it says so.
    An out-of-space condition breaks things far from its cause; the handler is the one place that
    sees every failure and can check the one resource whose exhaustion explains most of them.
"""
import errno
import logging
import traceback

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)

# Below this, the transit directory is considered unable to take another request body of any size
# worth spooling (Waitress switches to a file past 512 KiB).
_SEUIL_TRANSIT = 8 << 20


def _errno_of(exc):
    """ENOSPC anywhere in the exception chain — the cause is often wrapped several times."""
    vu = set()
    while exc is not None and id(exc) not in vu:
        vu.add(id(exc))
        num = getattr(exc, "errno", None)
        if num in (errno.ENOSPC, errno.EDQUOT):
            return num
        exc = exc.__cause__ or exc.__context__
    return None


def _transit_sature():
    """(saturated, state) — measured, never assumed."""
    try:
        from . import spool
        etat = spool.state()
        return etat.get("free", 0) < _SEUIL_TRANSIT, etat
    except Exception:
        return False, {}


def _veut_json():
    return (request.path or "").startswith("/api/") or \
        "application/json" in (request.headers.get("Accept") or "")


def register(app):
    @app.errorhandler(Exception)
    def _non_rattrapee(e):
        # 404, 403, 413… are deliberate answers, not failures: they pass through untouched.
        if isinstance(e, HTTPException):
            return e
        # Flask logs the traceback itself ONLY when no handler is registered. Registering one
        # makes us responsible for it — losing the traceback would be a worse trade than the
        # generic page we are replacing.
        log.error("unhandled error on %s %s\n%s", request.method, request.path,
                  "".join(traceback.format_exception(type(e), e, e.__traceback__)))

        num = _errno_of(e)
        sature, etat = _transit_sature()
        if num or sature:
            from .i18n import t
            cle = "error.no_space"
            detail = "%s : %d o libre(s)" % (etat.get("dir", "?"), etat.get("free", 0))
            log.error("out of space: transit directory %s has %d byte(s) free (%s)",
                      etat.get("dir"), etat.get("free", 0), etat.get("reason", ""))
            charge = {"ok": False, "error": t(cle), "code": "no_space", "detail": detail}
            return (jsonify(charge), 507) if _veut_json() else (t(cle) + " — " + detail, 507)

        if _veut_json():
            from .i18n import t
            return jsonify({"ok": False, "error": t("error.unexpected"), "code": "unexpected",
                            "detail": "%s: %s" % (type(e).__name__, str(e)[:200])}), 500
        raise e
