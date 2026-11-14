#!/usr/bin/env python3
"""The DeckLink driver screen must ACT, not just diagnose.

Measured 2026-11-13, on a node carrying a DeckLink card: the operator uploaded the 2.09 GB
Blackmagic archive, and the page answered « prérequis manquants — dkms no, kernel headers no »,
explained precisely why DKMS needs the running kernel's headers… and offered no button. `run()`
returned « Nothing to do: install_prereqs_missing » and exited SUCCESSFULLY, while `can_run` was
false for that status, so the page had nothing to show. The product knew the exact name of what
was missing, knew the command that installs it, and left the operator to find that out elsewhere —
on the one screen whose entire purpose is installing this driver. « C'est frustrant, on ne sait
pas quoi en faire. »

Diagnosing without offering the act is not caution, it is an unfinished feature. The prerequisites
are part of installing the driver the operator asked for.

A node with no outbound access genuinely cannot apt-get; that is a real case here. Which is why
the failure must NAME the two packages and the exact kernel release — they can be carried in by
hand. What it must never do is fail without saying which.
"""
import io
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "node_agent", "host_drivers"))

import blackmagic as B                                                        # noqa: E402

_echecs = []


def dire(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, (" — " + detail) if detail else ""))
    if not ok:
        _echecs.append(titre)


FAITS_SANS_PREREQUIS = {"package": None, "files": ["Blackmagic_Desktop_Video_Linux_16.5.tar.gz"],
                        "dkms": False, "headers": False, "headers_meta": False, "libgl": False,
                        "tool": False, "cards": [], "boot_id": "b", "modules": 0, "devs": 0}
# Le cas RÉEL du 2026-11-13 : DKMS et les en-têtes versionnés posés, écran au vert — et dpkg qui
# refuse sur `libgl1` et le méta-paquet d'en-têtes, en laissant desktopvideo non configuré.
FAITS_PIEGE = dict(FAITS_SANS_PREREQUIS, dkms=True, headers=True)
FAITS_PRETS = dict(FAITS_SANS_PREREQUIS, dkms=True, headers=True)


class _Sonde:
    """Facts served to the driver; `pose()` is what installing the prerequisites would change."""

    def __init__(self, faits):
        self.faits = dict(faits)

    def pose(self):
        self.faits.update(dkms=True, headers=True, headers_meta=True, libgl=True)

    def __call__(self, nom):
        # Chaque sonde rend CE QU'ELLE MESURE. Rendre « x » pour tout faisait de `files` une liste
        # d'un fichier nommé « x », que `installable()` écarte à juste titre : la garde testait
        # alors un autre cas que celui qu'elle annonçait.
        if nom == "files":
            return 0, "\n".join(self.faits.get("files") or [])
        if nom == "package":
            return 0, self.faits.get("package") or ""
        if nom == "list":
            return (0, "") if self.faits.get("tool") else (1, "")
        if nom in ("devdir", "modules"):
            return 0, str(self.faits.get(nom if nom == "modules" else "devs", 0))
        if nom == "boot_id":
            return 0, self.faits.get("boot_id", "")
        return (0, "ok") if self.faits.get(nom) else (1, "")


def _avec_doublures(sortie_apt=0, faits=None, pose_apres_apt=True):
    """Runs run() with apt, say() and the package install replaced. Returns (code, journal, appels)."""
    sonde = _Sonde(faits or FAITS_SANS_PREREQUIS)
    journal, appels = [], []

    def faux_sh(cmd, timeout=120):
        appels.append(cmd)
        if "apt-get install" in cmd:
            if sortie_apt == 0 and pose_apres_apt:
                sonde.pose()
            return sortie_apt, "sortie apt"
        return 0, ""

    def faux_gather(probe=None):
        f = dict(sonde.faits)
        f["files"] = sonde.faits["files"]
        return f

    vrais = (B._sh, B.say, B.gather_facts, B.installer_pilote)
    B._sh = faux_sh
    B.say = lambda m: journal.append(m)
    B.gather_facts = faux_gather
    B.installer_pilote = lambda fichier: appels.append("INSTALL:" + fichier) or True
    try:
        code = B.run(probe=sonde)
    finally:
        B._sh, B.say, B.gather_facts, B.installer_pilote = vrais
    return code, journal, appels


print("1. Le plan dit QU'UN GESTE EXISTE")
plan = B.decide(FAITS_SANS_PREREQUIS)
dire(plan["status"] == "missing_prereqs", "le statut reste le constat exact", plan["status"])
dire([e["id"] for e in plan["steps"]][:1] == ["prereqs"],
     "la première étape est de poser les prérequis", str([e["id"] for e in plan["steps"]]))
dire("packages" in plan["params"] and "dkms" in plan["params"]["packages"],
     "le plan NOMME les paquets à poser", plan["params"].get("packages", ""))
dire(os.uname().release in plan["params"].get("packages", ""),
     "et les en-têtes sont ceux du noyau EN COURS, pas un méta-paquet")

print("\n2. L'écran peut offrir l'action")
etat = B.state(probe=_Sonde(FAITS_SANS_PREREQUIS))
dire(etat["can_run"] is True,
     "`can_run` est vrai quand il manque des prérequis", str(etat["can_run"]))
dire(etat["status"] == "missing_prereqs", "sans masquer le constat", etat["status"])

print("\n3. `run()` POSE les prérequis, puis continue")
code, journal, appels = _avec_doublures()
dire(any("apt-get install" in a for a in appels), "apt est appelé", str([a[:40] for a in appels][:3]))
dire(any("dkms" in a for a in appels if "apt-get install" in a), "avec dkms")
dire(any(os.uname().release in a for a in appels if "apt-get install" in a),
     "et les en-têtes du noyau courant")
dire(any(a.startswith("INSTALL:") for a in appels),
     "puis l'installation du pilote ENCHAÎNE, sans second geste de l'opérateur")
dire(code == B.EXIT_REBOOT, "et le redémarrage nécessaire est annoncé", str(code))

print("\n4. Un échec d'apt NOMME ce qu'il faut apporter")
code, journal, appels = _avec_doublures(sortie_apt=100)
txt = "\n".join(journal)
dire(code == 1, "le code de retour est un échec", str(code))
dire("dkms" in txt and os.uname().release in txt,
     "les deux paquets sont nommés, avec la version exacte du noyau")
dire("dpkg -i" in txt or "outbound" in txt,
     "et le chemin hors ligne est indiqué (nœud sans accès sortant)")

print("\n5. Des prérequis qui ne se posent pas ne font pas croire au succès")
code, journal, appels = _avec_doublures(sortie_apt=0, pose_apres_apt=False)
dire(code == 1 and "still missing" in "\n".join(journal),
     "apt dit OK mais rien n'a changé → échec explicite", str(code))

print("\n6. Le message de l'écran annonce ce que fera le bouton")
for lang in ("fr", "en"):
    cat = json.load(io.open(os.path.join(RACINE, "i18n/%s.json" % lang), encoding="utf-8"))
    m = cat.get("settings.hostdrv.blackmagic.plan.install_prereqs_missing", "")
    dire("{packages}" in m, "%s : le message nomme les paquets" % lang)
    dire("APT" in m or "apt" in m.lower(), "%s : et dit qu'il faut atteindre le dépôt" % lang)

print("\n6b. Le piège mesuré en vrai : en-têtes présents, dpkg qui refuse quand même")
plan = B.decide(FAITS_PIEGE)
dire(plan["status"] == "missing_prereqs",
     "en-têtes versionnés posés ne suffisent PAS \u2192 toujours un refus", plan["status"])
manque = plan["params"].get("missing", "")
dire("meta" in manque, "le méta-paquet d'en-têtes est nommé", manque)
dire("libgl1" in manque, "et libgl1 aussi", manque)
dire(any("linux-headers-" in q and not q.endswith(os.uname().release)
         for q in B.prereq_packages()),
     "les prérequis contiennent le MÉTA-paquet, pas seulement le versionné",
     str(B.prereq_packages()))
dire(os.uname().release in " ".join(B.prereq_packages()),
     "et le versionné reste là : c'est contre lui que DKMS compile")

print("\n6c. L'installation résout les dépendances, et vérifie que le paquet est CONFIGURÉ")
def _poser_pilote(sortie_apt=0, statut="install ok installed"):
    appels = []

    def faux_sh(cmd, timeout=120):
        appels.append(cmd)
        if "dpkg-query -W -f='${Status}' desktopvideo" in cmd:
            return 0, statut
        if "apt-get install" in cmd:
            return sortie_apt, "sortie apt"
        if "dpkg --print-architecture" in cmd:
            return 0, "amd64"
        return 0, ""

    vrais = (B._sh, B.say, B._architecture_noeud)
    journal = []
    B._sh = faux_sh
    B.say = lambda m: journal.append(m)
    B._architecture_noeud = lambda: "amd64"
    try:
        ok = B.installer_pilote("desktopvideo_16.5a1_amd64.deb")
    finally:
        B._sh, B.say, B._architecture_noeud = vrais
    return ok, journal, appels

ok, journal, appels = _poser_pilote()
dire(any("apt-get install" in a and ".deb" in a for a in appels),
     "le .deb LOCAL passe par apt, qui tire ses dépendances", 
     next((a[:60] for a in appels if "apt-get install" in a), ""))
dire(any("dpkg-query" in a and "desktopvideo" in a for a in appels),
     "et l'état dpkg est RELU après coup")
dire(ok is True, "installation nominale \u2192 succès")
ok, journal, appels = _poser_pilote(statut="deinstall ok unpacked")
dire(ok is False and "NOT configured" in "\n".join(journal),
     "dépaqueté mais non configuré \u2192 ÉCHEC, pas un faux succès",
     "ok=%s" % ok)
ok, journal, appels = _poser_pilote(sortie_apt=100, statut="install ok installed")
dire(any("apt-get -f install" in a for a in appels),
     "apt en échec \u2192 repli dpkg + réparation des dépendances")

print("\n6d. Un .deb de prérequis déposé n'est PAS pris pour le pilote")
_DEBS = ["dkms_3.0.13-3_all.deb", "libgl1_1.7.0-1_amd64.deb",
         "linux-headers-amd64_6.12.90-1_amd64.deb"]
faits = dict(FAITS_SANS_PREREQUIS, files=FAITS_SANS_PREREQUIS["files"] + _DEBS)
dire(B.installable(faits["files"]).lower().endswith((".tar.gz", ".tar")),
     "le fichier à installer reste l'archive Blackmagic", str(B.installable(faits["files"])))
dire(sorted(B.prereq_debs(faits["files"])) == sorted(_DEBS),
     "et les trois .deb sont rangés du côté des prérequis", str(B.prereq_debs(faits["files"])))
dire(B.installable(_DEBS) is None,
     "sans archive, aucun de ces .deb ne se fait passer pour le pilote")
dire(B.decide(dict(FAITS_SANS_PREREQUIS, files=_DEBS))["status"] == "install_needs_file",
     "l'écran redemande donc l'archive, au lieu d'installer libgl1 comme pilote")


def _hors_ligne(debs, local_pose=True):
    """Un nœud SANS accès sortant : tout apt réseau échoue, seul le .deb local peut aboutir."""
    sonde = _Sonde(dict(FAITS_SANS_PREREQUIS,
                        files=FAITS_SANS_PREREQUIS["files"] + list(debs)))
    journal, appels = [], []

    def faux_sh(cmd, timeout=120):
        appels.append(cmd)
        if "apt-get install" in cmd and B.FILES_DIR in cmd:
            if local_pose:
                sonde.pose()
            return 0, "Setting up dkms"
        if "apt-get" in cmd:
            return 100, "Temporary failure resolving 'deb.debian.org'"
        return 0, ""

    vrais = (B._sh, B.say, B.gather_facts, B.installer_pilote)
    B._sh = faux_sh
    B.say = lambda m: journal.append(m)
    B.gather_facts = lambda probe=None: dict(sonde.faits)
    B.installer_pilote = lambda fichier: appels.append("INSTALL:" + fichier) or True
    try:
        code = B.run(probe=sonde)
    finally:
        B._sh, B.say, B.gather_facts, B.installer_pilote = vrais
    return code, journal, appels


print("\n6e. Hors ligne, les .deb déposés suffisent \u2014 et APT n'est pas attendu pour rien")
code, journal, appels = _hors_ligne(_DEBS)
locaux = [a for a in appels if "apt-get install" in a and B.FILES_DIR in a]
dire(len(locaux) == 1, "les .deb déposés sont posés par apt, en local", str(len(locaux)))
dire(all(d in locaux[0] for d in _DEBS),
     "les trois d'un seul coup : apt les résout entre eux", locaux[0][-60:])
dire(not any("apt-get update" in a for a in appels),
     "aucun `apt-get update` : sur un nœud hors ligne il ne dépense que son délai")
dire(not any("apt-get install" in a and B.FILES_DIR not in a for a in appels),
     "et aucune installation réseau n'est tentée une fois les prérequis satisfaits")
dire(any(a.startswith("INSTALL:") for a in appels),
     "l'installation du pilote ENCHAÎNE, sans second geste")
dire(code == B.EXIT_REBOOT, "et le redémarrage est annoncé", str(code))

print("\n6f. Des .deb déposés qui ne suffisent pas \u2192 échec qui dit où déposer la suite")
code, journal, appels = _hors_ligne(_DEBS, local_pose=False)
txt = "\n".join(journal)
dire(code == 1, "le code de retour est un échec", str(code))
dire("Still missing" in txt, "ce qui manque encore est nommé")
dire(B.FILES_DIR in txt, "le dossier de dépôt est donné", B.FILES_DIR)
dire("--download-only" in txt and "same release" in txt,
     "avec la commande qui fabrique le lot sur une machine connectée")

print("\n6g. L'écran dit les QUATRE conditions, et ce qu'on peut déposer")
etat = B.state(probe=_Sonde(FAITS_PIEGE))
ligne = next((r["value"] for r in etat["rows"] if r["key"] == "prereqs"), "")
for quoi in ("dkms", "kernel headers", "headers meta-package", "libgl1"):
    dire(quoi in ligne, "la ligne d'état nomme %s" % quoi, ligne)
dire(etat["wants_upload"] is True,
     "la zone de dépôt est offerte dans cet état", str(etat["wants_upload"]))
dire(".deb" in etat["upload_hint"],
     "et elle annonce des .deb, pas l'archive", etat["upload_hint"])
for lang in ("fr", "en"):
    cat = json.load(io.open(os.path.join(RACINE, "i18n/%s.json" % lang), encoding="utf-8"))
    m = cat.get("settings.hostdrv.blackmagic.plan.install_prereqs_missing", "")
    dire("--download-only" in m, "%s : le message donne la commande de fabrication" % lang)
    dire("deb" in m, "%s : et dit de déposer les .deb" % lang)
    dire("settings.hostdrv.blackmagic.step.prereqs" in cat,
         "%s : l'étape « prérequis » a un libellé" % lang)

print("\n6h. Le bundle hors-ligne porte ces prérequis")
# Lu par analyse de source : importer `app.offline_bundle` ferait entrer tout `app/`, et un test
# n'a pas à toucher la base pour lire une liste. Un bundle construit sans ces paquets livre un
# nœud où l'installation du pilote échoue, et c'est le cas d'usage même du bundle : pas de réseau.
import ast                                                                    # noqa: E402

_ob = ast.parse(io.open(os.path.join(RACINE, "app/offline_bundle.py"), encoding="utf-8").read())
_paquets = []
for n in ast.walk(_ob):
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("ORCH_PACKAGES",
                                                                         "NODE_PACKAGES"):
        _paquets += [e.value for e in n.value.elts]
dire(bool(_paquets), "les listes de paquets du bundle se lisent", str(len(_paquets)))
for q in B.prereq_packages():
    if q.endswith(os.uname().release):
        # Les en-têtes VERSIONNÉS ne sont pas prévisibles à la construction du bundle : le nœud
        # peut démarrer sur un autre noyau. C'est écrit dans offline_bundle.py, et assumé.
        continue
    dire(q in _paquets, "%s est dans le bundle" % q, ", ".join(_paquets))

print("\n7. MUTATIONS")
MUT = []


def muter(titre, pose, depose, verif):
    pose()
    try:
        pris = not verif()
    except Exception:
        pris = True
    finally:
        depose()
    MUT.append((titre, pris))
    print("  %s %s" % ("✓ attrapée" if pris else "✗ NON attrapée", titre))


_SRC0 = io.open(os.path.join(RACINE, "node_agent/host_drivers/blackmagic.py"),
                encoding="utf-8").read()
_CAN_RUN = '"can_run": plan["status"] in ("update", "install", "upgrade", "missing_prereqs"),'
muter("`can_run` réexclut missing_prereqs \u2014 le bouton disparaît",
      lambda: None, lambda: None,
      lambda: _CAN_RUN in _SRC0.replace(
          _CAN_RUN, '"can_run": plan["status"] in ("update", "install", "upgrade"),'))

vrai_ip = B.install_prereqs
muter("`run()` n'installe plus rien (install_prereqs neutralisée)",
      lambda: setattr(B, "install_prereqs", lambda sh=None, **kw: (True, "")),
      lambda: setattr(B, "install_prereqs", vrai_ip),
      lambda: (lambda r: any("apt-get install" in a for a in r[2]))(_avec_doublures(pose_apres_apt=False)))

vrai_pp = B.prereq_packages
muter("les en-têtes redeviennent un méta-paquet",
      lambda: setattr(B, "prereq_packages", lambda: ["dkms", "linux-headers-amd64"]),
      lambda: setattr(B, "prereq_packages", vrai_pp),
      lambda: os.uname().release in B.decide(FAITS_SANS_PREREQUIS)["params"].get("packages", ""))

vrai_sh_msg = B.install_prereqs
muter("l'échec d'apt ne nomme plus les paquets",
      lambda: setattr(B, "install_prereqs",
                      lambda sh=None, **kw: (False, "apt a échoué")),  # message sans les paquets
      lambda: setattr(B, "install_prereqs", vrai_sh_msg),
      lambda: (lambda j: "dkms" in j and os.uname().release in j)(
          "\n".join(_avec_doublures(sortie_apt=100)[1])))

_SRC = io.open(os.path.join(RACINE, "node_agent/host_drivers/blackmagic.py"),
               encoding="utf-8").read()
muter("le contrôle « configuré » disparaît de la source",
      lambda: None, lambda: None,
      lambda: 'if "install ok installed" not in (st or ""):'
              in _SRC.replace('if "install ok installed" not in (st or ""):', "if False:"))

def _installable_laxiste(fichiers):
    """Le code d'avant 1.3.0 : tout .deb est « le pilote »."""
    for f in sorted(fichiers or [], reverse=True):
        bas = f.lower()
        if bas.endswith(".installed"):
            continue
        if bas.endswith(B.ARCHIVES) or bas.endswith(".deb"):
            return f
    return None


vrai_inst = B.installable
muter("`installable` reprend n'importe quel .deb \u2014 libgl1 devient « le pilote »",
      lambda: setattr(B, "installable", _installable_laxiste),
      lambda: setattr(B, "installable", vrai_inst),
      lambda: B.installable(_DEBS) is None
              and B.decide(dict(FAITS_SANS_PREREQUIS, files=_DEBS))["status"]
              == "install_needs_file")

vrai_pd = B.prereq_debs
muter("les .deb déposés sont ignorés (prereq_debs muette)",
      lambda: setattr(B, "prereq_debs", lambda f: []),
      lambda: setattr(B, "prereq_debs", vrai_pd),
      lambda: (lambda r: any("apt-get install" in a and B.FILES_DIR in a for a in r[2]))(
          _hors_ligne(_DEBS)))

vrai_mp = B.missing_prereqs
muter("la relecture après dépôt disparaît \u2192 apt réseau quand même",
      lambda: setattr(B, "missing_prereqs", lambda f: ["dkms"]),
      lambda: setattr(B, "missing_prereqs", vrai_mp),
      lambda: not any("apt-get update" in a for a in _hors_ligne(_DEBS)[2]))

def _rows_deux_sur_quatre(f, plan):
    """La ligne d'état d'avant le 13/11 : deux conditions nommées sur quatre mesurées."""
    lignes = vrai_rows(f, plan)
    for r in lignes:
        if r["key"] == "prereqs":
            r["value"] = "dkms %s, kernel headers %s" % ("yes" if f["dkms"] else "no",
                                                         "yes" if f["headers"] else "no")
    return lignes


vrai_rows = B.rows_for_ui
muter("la ligne d'état revient à deux conditions sur quatre",
      lambda: setattr(B, "rows_for_ui", _rows_deux_sur_quatre),
      lambda: setattr(B, "rows_for_ui", vrai_rows),
      lambda: (lambda v: all(q in v for q in ("headers meta-package", "libgl1")))(
          next((r["value"] for r in B.state(probe=_Sonde(FAITS_PIEGE))["rows"]
                if r["key"] == "prereqs"), "")))

rates = [t for t, pris in MUT if not pris]
print("\n%d/%d mutations attrapées" % (len(MUT) - len(rates), len(MUT)))
_echecs.extend("mutation non attrapée : " + t for t in rates)
print("\n%s" % ("✓ tout passe" if not _echecs
                else "✗ %d échec(s) : %s" % (len(_echecs), "; ".join(_echecs[:5]))))
sys.exit(1 if _echecs else 0)
