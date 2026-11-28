# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Problem report — what an operator sends us when something does not work.

WHY THIS EXISTS (2026-11-14). An operator hit « ✕ HTTP 500 » while importing a plugin. Finding
the cause took eight shell commands typed by hand into a terminal on their own machine, read back
one screenshot at a time: journald, `df -h`, the open-but-deleted file in /proc, the size of the
archive. The information was all there, on their machine, and none of it was reachable from the
product. Someone without a shell, or without us on the other end, had nothing at all.

THREE RULES, and they are what makes this trustworthy rather than merely useful.

★ ONE ARTEFACT, READ BEFORE IT LEAVES. The report is a single TEXT file. The page shows the exact
  bytes the file carries — not a list of categories, not a summary: the content. There is nowhere
  for something to hide, and nothing in a second file nobody opened. The operator downloads it and
  sends it themselves; no instance phones home.

★ REDACTION IS CENTRAL, NEVER PER-SECTION. Every line of every section goes through one masking
  pass (`app/crash_tail.py:redact`, already in use for script output) before anything is shown or
  written. A collector added later CANNOT forget it — the safe path is the only path. A per-section
  rule would have been one forgotten call away from publishing a token.

★ WHAT IS WITHHELD IS SHOWN AS WITHHELD. A secret setting is not dropped, it is printed with its
  name and « — removed from the report — » in place of its value. Silently omitting it would leave
  the operator to trust a claim; naming it lets them check. The definition of « secret » is the
  one the HTTP paths already use (`settings._is_secret_key`): a second list would drift, and the
  one that drifted would be this one.

NEVER COLLECTED: `config_local.py` (site values, cf. CLAUDE.md « Sécurité »), the content of the
database, media, or anything from a node other than the fixed read-only commands listed in
`_NODE_COMMANDS`. An unreachable node is written INTO the report as unreachable — never omitted,
because « this node is not answering » is often the answer.
"""

import io
import logging
import os
import platform
import subprocess
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

RETIRE = "— retiré du rapport —"
# Per-section ceiling. A report is produced in one go, held in memory, and read by a human: a
# journal that fills a browser tab is as useless as no journal at all.
MAX_SECTION_BYTES = 120 << 10
LIGNES_JOURNAL = 400
LIGNES_ALERTES = 150
LIGNES_JOURNAL_NOEUD = 80

# Fixed, READ-ONLY command set run on each node through the agent. Written here, once, so that
# the report can never grow a command by accident: anything that is not in this list is not run.
_NODE_COMMANDS = (
    ("uname", "uname -a"),
    ("uptime", "uptime"),
    ("disque", "df -h -x overlay -x squashfs"),
    ("memoire", "free -h"),
    ("tmp", "findmnt -no FSTYPE,SIZE,USED,AVAIL /tmp 2>/dev/null || echo '(pas de /tmp distinct)'"),
    ("agent", "systemctl is-active bobi-node-agent 2>/dev/null; "
              "systemctl show -p ActiveEnterTimestamp --value bobi-node-agent 2>/dev/null"),
    ("journal", "journalctl -u bobi-node-agent -n %d --no-pager 2>/dev/null" % LIGNES_JOURNAL_NOEUD),
)


# ─── collecte ─────────────────────────────────────────────────────────────────────────────────────
def _sh(cmd, timeout=20):
    """Local read-only command. Returns its output, or the failure as text — never raises."""
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:                                                        # noqa: BLE001
        return "(échec de la commande : %s)" % e


def _kv(cle, valeur):
    return "%-26s %s" % (cle, "" if valeur is None else valeur)


def _section_instance():
    lignes = []
    try:
        from . import settings as st, builder, updater, ha
        info = builder.current_build_info() or {}
        lignes += [
            _kv("nom de l'instance", st.get("system_name") or st.get("brand_system_name") or "-"),
            _kv("version (étiquette)", info.get("label") or "-"),
            _kv("version (build)", info.get("build_id") or "-"),
            _kv("déployée le", (updater.deploy_info() or {}).get("deployed_at") or "-"),
            _kv("rôle du contrôleur", "actif" if ha.is_active() else "secours"),
        ]
    except Exception as e:                                                        # noqa: BLE001
        lignes.append("(identité illisible : %s)" % e)
    lignes += [
        _kv("machine", platform.node()),
        _kv("système", " ".join(platform.uname()[:3])),
        _kv("python", platform.python_version()),
        _kv("démarrée depuis", _sh("uptime -p")),
    ]
    try:
        from .database import DB_PATH, db_get_containers, db_get_nodes
        lignes += [
            _kv("base de données", "%s (%d octets)"
                % (DB_PATH, os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0)),
            _kv("conteneurs connus", len(db_get_containers() or [])),
            _kv("nœuds enrôlés", len(db_get_nodes() or [])),
        ]
    except Exception as e:                                                        # noqa: BLE001
        lignes.append("(inventaire illisible : %s)" % e)
    return lignes


def _section_espace():
    """Transit space and disk. First section after the identity, and deliberately so: this is
    what the 2026-11-13 incident came down to, and nothing in the product showed it."""
    lignes = []
    try:
        from . import spool
        etat = spool.state()
        lignes += [
            _kv("dossier de transit", etat.get("dir")),
            _kv("en RAM (tmpfs)", "oui" if etat.get("ram_backed") else "non"),
            _kv("place libre", "%d octet(s)" % (etat.get("free") or 0)),
            _kv("plafond de corps utilisable", "%d octet(s)" % spool.capacity()),
            _kv("raison", etat.get("reason")),
        ]
    except Exception as e:                                                        # noqa: BLE001
        lignes.append("(espace de transit illisible : %s)" % e)
    lignes.append("")
    lignes.append("df -h (hors couches Docker) :")
    lignes += ["  " + l for l in _sh("df -h -x overlay -x squashfs").splitlines()]
    lignes.append("")
    lignes.append("mémoire :")
    lignes += ["  " + l for l in _sh("free -h").splitlines()]
    try:
        from .config import LOG_PATH
        lignes.append("")
        lignes.append(_kv("journal applicatif", "%s (%d octets)"
                          % (LOG_PATH, os.path.getsize(LOG_PATH)
                             if os.path.exists(LOG_PATH) else 0)))
    except Exception:                                                             # noqa: BLE001
        pass
    return lignes


def _section_erreurs():
    """Tail of the controller's own log. Not filtered to ERROR lines: the line BEFORE the
    traceback is usually the one that says what was being attempted."""
    try:
        from .config import LOG_PATH
    except Exception as e:                                                        # noqa: BLE001
        return ["(chemin du journal inconnu : %s)" % e]
    if not os.path.exists(LOG_PATH):
        return ["(aucun journal applicatif à %s)" % LOG_PATH]
    sortie = _sh("tail -n %d %s" % (LIGNES_JOURNAL, LOG_PATH), timeout=30)
    return sortie.splitlines() or ["(journal vide)"]


def _section_alertes():
    try:
        from .database import db_get_alerts
        from . import i18n
        lignes_db = db_get_alerts(limit=LIGNES_ALERTES) or []
        rendues = i18n.rendre_alertes(lignes_db, "fr")
    except Exception as e:                                                        # noqa: BLE001
        return ["(alertes illisibles : %s)" % e]
    out = []
    for a in rendues:
        out.append("%-20s %-8s %s" % (a.get("timestamp") or "-", a.get("niveau") or "-",
                                      a.get("message") or ""))
    return out or ["(aucune alerte)"]


def _section_conteneurs():
    try:
        from .database import db_get_containers
        cs = db_get_containers() or []
    except Exception as e:                                                        # noqa: BLE001
        return ["(inventaire illisible : %s)" % e]
    out = ["%-6s %-22s %-18s %-12s %s" % ("vmid", "nom", "type", "statut", "nœud")]
    for c in cs:
        out.append("%-6s %-22s %-18s %-12s %s"
                   % (c.get("vmid"), (c.get("hostname") or "")[:22], (c.get("type") or "")[:18],
                      (c.get("status") or "")[:12], c.get("node_id")))
    return out if len(out) > 1 else ["(aucun conteneur)"]


def _section_plugins():
    try:
        from . import plugins
        manifestes = plugins.all() or []
    except Exception as e:                                                        # noqa: BLE001
        return ["(registre des plugins illisible : %s)" % e]
    out = []
    for m in sorted(manifestes, key=lambda x: str((x or {}).get("type") or "")):
        m = m or {}
        out.append("%-22s %-10s core_min=%s"
                   % (m.get("type") or "?", m.get("version") or "-",
                      (m.get("requires") or {}).get("core_min") or "-"))
    try:
        mauvais = plugins.scan_errors() or {}
        for t, err in sorted(mauvais.items()):
            # Un plugin ÉCARTÉ au scan n'apparaît nulle part ailleurs : c'est précisément ce
            # qu'il faut savoir quand « mon type de conteneur a disparu de la palette ».
            out.append("%-22s ÉCARTÉ AU SCAN : %s" % (t, str(err)[:140]))
    except Exception:                                                             # noqa: BLE001
        pass
    return out or ["(aucun plugin)"]


def _section_reglages():
    """Every setting, with the secret ones NAMED and their value withheld."""
    try:
        from . import settings as st
        tous = st.all()
    except Exception as e:                                                        # noqa: BLE001
        return ["(réglages illisibles : %s)" % e]
    out = []
    for k in sorted(tous):
        if st._is_secret_key(k):                                                   # noqa: SLF001
            out.append(_kv(k, RETIRE))
        else:
            v = tous[k]
            v = str(v)
            out.append(_kv(k, v if len(v) <= 160 else v[:160] + " …(tronqué)"))
    return out


def _section_noeuds():
    try:
        from .database import db_get_nodes
        from . import node_driver
        noeuds = db_get_nodes() or []
    except Exception as e:                                                        # noqa: BLE001
        return ["(nœuds illisibles : %s)" % e]
    if not noeuds:
        return ["(aucun nœud enrôlé)"]
    out = []
    for n in noeuds:
        out.append("── nœud %s (id %s) ──" % (n.get("name"), n.get("id")))
        out += [_kv("  adresse", n.get("host")),
                _kv("  statut en base", n.get("status")),
                _kv("  capacités", n.get("caps") or "-")]
        if not node_driver.has_agent(n):
            # ★ Un nœud sans agent est ÉCRIT comme tel. L'omettre laisserait croire à un parc
            #   de N-1 machines, et « cette machine ne répond pas » est souvent la réponse.
            out += ["  (aucun agent joignable sur ce nœud — rien n'a été collecté ici)", ""]
            continue
        for titre, cmd in _NODE_COMMANDS:
            try:
                rc, sortie, err = node_driver.host_exec(n, cmd, timeout=30)
            except Exception as e:                                                 # noqa: BLE001
                rc, sortie, err = 255, "", str(e)
            texte = (sortie or "") if rc == 0 else ((err or sortie or "").strip()
                                                    or "rc=%s" % rc)
            out.append("  %s :" % titre)
            out += ["    " + l for l in (texte or "").splitlines()[:LIGNES_JOURNAL_NOEUD]] \
                or ["    (rien)"]
        out.append("")
    return out


# Order matters: it is reading order. Identity, then the two things that explain most incidents
# (space, errors), then the inventories, then the settings, then the nodes — longest last.
SECTIONS = (
    ("instance", _section_instance),
    ("espace", _section_espace),
    ("erreurs", _section_erreurs),
    ("alertes", _section_alertes),
    ("conteneurs", _section_conteneurs),
    ("plugins", _section_plugins),
    ("reglages", _section_reglages),
    ("noeuds", _section_noeuds),
)
SECTIONS_NOEUDS = ("noeuds",)


# ─── assemblage ───────────────────────────────────────────────────────────────────────────────────
def _borner(lignes):
    """Keep the section under its ceiling, and SAY where it was cut."""
    out, taille = [], 0
    for l in lignes:
        n = len(l.encode("utf-8", "replace")) + 1
        if taille + n > MAX_SECTION_BYTES:
            out.append("… (section tronquée à %d octets)" % MAX_SECTION_BYTES)
            return out, True
        out.append(l)
        taille += n
    return out, False


def build(include_nodes=True, description=""):
    """Collect the whole report. Never raises: a collector that fails says so in its section."""
    from .crash_tail import redact
    debut = time.monotonic()
    sections, masquees, retirees = [], 0, 0
    for sid, collecte in SECTIONS:
        if not include_nodes and sid in SECTIONS_NOEUDS:
            continue
        try:
            lignes = [str(l) for l in (collecte() or [])]
        except Exception as e:                                                     # noqa: BLE001
            log.warning("support report: section %s failed: %s", sid, e)
            lignes = ["(cette rubrique n'a pas pu être collectée : %s)" % e]
        # ★ LE MASQUAGE EST ICI, ET NULLE PART AILLEURS. Une rubrique ajoutée plus tard ne peut
        #   pas l'oublier : elle n'a pas de chemin qui l'évite.
        nettes = []
        for l in lignes:
            m = redact(l).rstrip()
            if m != l.rstrip():
                masquees += 1
            if RETIRE in m:
                retirees += 1
            nettes.append(m)
        nettes, tronquee = _borner(nettes)
        sections.append({"id": sid, "lines": nettes, "truncated": tronquee})
    return {"generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "description": redact(str(description or "")[:4000]),
            "with_nodes": bool(include_nodes),
            "sections": sections,
            # Deux comptes, parce que ce sont deux gestes différents : une valeur RETIRÉE est
            # nommée et vide (un réglage secret), une valeur MASQUÉE a été trouvée au milieu d'un
            # texte et remplacée. Les additionner cacherait lequel des deux a eu lieu.
            "redacted": masquees,
            "withheld": retirees,
            "took_ms": int((time.monotonic() - debut) * 1000)}


def as_text(rapport):
    """The file, byte for byte what the page shows."""
    buf = io.StringIO()
    buf.write("RAPPORT DE PROBLÈME — Bobi.Studio\n")
    buf.write("produit le %s\n" % rapport.get("generated"))
    buf.write("valeurs retirées : %d (réglages secrets, nommés à leur place)\n"
              % rapport.get("withheld", 0))
    buf.write("valeurs masquées : %d (trouvées dans un texte)\n" % rapport.get("redacted", 0))
    if rapport.get("description"):
        buf.write("\nCE QUE DÉCRIT LA PERSONNE\n")
        buf.write(rapport["description"].rstrip() + "\n")
    for s in rapport.get("sections") or []:
        buf.write("\n" + "=" * 78 + "\n%s\n" % s["id"].upper() + "=" * 78 + "\n")
        for l in s["lines"]:
            # Écrites TELLES QUELLES : c'est `build` qui normalise, une fois. Rogner ici aussi
            # masquerait une divergence entre la page et le fichier au lieu de l'empêcher.
            buf.write(l + "\n")
    return buf.getvalue()


def filename(rapport):
    stamp = (rapport.get("generated") or "")[:19].replace(":", "-").replace("T", "_")
    return "bobi-rapport-%s.txt" % (stamp or "sans-date")


# ─── mémoire courte : ce qui a été MONTRÉ est ce qui se télécharge ───────────────────────────────
# Le téléchargement ne recollecte pas. Sinon le fichier différerait de ce que la personne vient de
# relire — et la transparence ne vaut que si les deux sont les mêmes octets. On garde les derniers
# rapports produits, bornés en nombre : c'est une file, pas un cache.
_MEM_MAX = 4
_mem = {}
_mem_ordre = []
_verrou = threading.Lock()


def remember(rapport):
    """Store the report and return its id."""
    import secrets
    rid = secrets.token_urlsafe(9)
    with _verrou:
        _mem[rid] = rapport
        _mem_ordre.append(rid)
        while len(_mem_ordre) > _MEM_MAX:
            _mem.pop(_mem_ordre.pop(0), None)
    return rid


def recall(rid):
    with _verrou:
        return _mem.get(rid)
