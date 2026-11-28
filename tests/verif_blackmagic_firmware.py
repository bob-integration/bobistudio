#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Garde sur le pilote hôte `blackmagic` (micrologiciel des cartes DeckLink).
#
# Ce que ce fichier protège, et pourquoi chaque cas existe :
#
#   · une carte dont le micrologiciel ne correspond pas au pilote est ACTIVÉE par le noyau mais
#     INVISIBLE au SDK. C'est arrivé deux fois (Duo 2 au premier montage, Quad 2 le 2026-10-20),
#     et les deux fois il a fallu quelqu'un avec un shell. D'où ce pilote ;
#   · `--list` continue d'annoncer « out of date » APRÈS une mise à jour réussie, parce qu'il lit
#     le micrologiciel qui TOURNE. Sans garde, l'interface proposerait de reflasher en boucle ;
#   · la mise à jour exige une COUPURE D'ALIMENTATION, pas un redémarrage : le code de sortie
#     doit être 11, jamais 10, sinon on envoie l'exploitant faire un reboot qui ne change rien.
#
# Le relevé rejoué est le VRAI, pris sur dell-1 le 2026-10-20 avec une Duo 2 à jour et une
# Quad 2 périmée côte à côte.
import importlib.util
import pathlib
import sys

ici = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "bm_driver", ici.parent / "node_agent" / "host_drivers" / "blackmagic.py")
bm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bm)

RELEVE_REEL = """Available Devices:
 * DeckLink Duo 2 (Gf0d8aa00)
     Firmware is up to date
 * DeckLink Quad 2 (Gf0aaa170)
     Firmware is out of date
"""
TOUT_A_JOUR = """Available Devices:
 * DeckLink Duo 2 (Gf0d8aa00)
     Firmware is up to date
"""
BOOT = "11111111-2222-3333-4444-555555555555"


def sonde(liste=RELEVE_REEL, tool=True, list_rc=0, noeuds=12, boot=BOOT, flashe="",
          paquet="16.4a1", fichiers=(), dkms=True, headers=True, headers_meta=True,
          libgl=True, modules=2, pci=2, built=2, noyau="6.12.117+deb13-amd64"):
    reponses = {
        "tool": (0, "/usr/bin/DesktopVideoUpdateTool") if tool else (1, ""),
        "list": (list_rc, liste),
        "package": (0, paquet or ""),
        "devdir": (0, str(noeuds)),
        "modules": (0, str(modules)),
        "boot_id": (0, boot),
        "files": (0, "\n".join(fichiers)),
        "dkms": (0, "/usr/sbin/dkms") if dkms else (1, ""),
        "headers": (0, "/lib/modules/x/build") if headers else (1, ""),
        # ★ LA PRÉSENCE SE LIT SUR LE CODE DE RETOUR, pas sur la sortie (`gather_facts` :
        #   `rc_meta == 0`) — d'où `(0, "")` pour « présent ». Une sonde qui encoderait
        #   l'inverse rendrait ces deux prérequis manquants dans TOUS les cas du fichier,
        #   et la garde mesurerait autre chose que ce qu'elle annonce.
        "headers_meta": (0, "") if headers_meta else (1, ""),
        "libgl": (0, "") if libgl else (1, ""),
        "pci": (0, str(pci)),
        "built": (0, str(built)),
        "kernel": (0, noyau),
        "preempt_rt": (1, ""),            # non-RT node (cf. verif_pilote_blackmagic_rt.py)
    }
    bm._marqueur_lu = lambda: flashe          # noqa: SLF001 — on injecte le marqueur
    return lambda nom: reponses[nom]


ko = 0


def verifier(titre, ok, detail=""):
    global ko
    if not ok:
        ko = 1
    print("  %s %-56s %s" % ("ok  " if ok else "ÉCHEC", titre, detail))


# ── 1. Le relevé réel : une seule carte à mettre à jour, et c'est la bonne ────────────────────
f = bm.gather_facts(sonde())
p = bm.decide(f)
verifier("relevé réel : il y a une mise à jour à faire", p["status"] == "update", p["status"])
verifier("les deux cartes sont lues", len(f["cards"]) == 2, "%d carte(s)" % len(f["cards"]))
verifier("SEULE la Quad 2 est ciblée",
         [c["id"] for c in p["cards"]] == ["Gf0aaa170"], [c["id"] for c in p["cards"]])
verifier("la Duo 2 à jour n'est pas touchée",
         all(c["id"] != "Gf0d8aa00" for c in p["cards"]))
verifier("l'état se lit sur la ligne SUIVANTE, pas sur le modèle",
         [c["state"] for c in f["cards"]] == ["ok", "outdated"],
         [c["state"] for c in f["cards"]])

# ── 2. Tout à jour ────────────────────────────────────────────────────────────────────────────
p = bm.decide(bm.gather_facts(sonde(liste=TOUT_A_JOUR, noeuds=4)))
verifier("tout à jour : rien à proposer", p["status"] == "ok", p["status"])
verifier("et le panneau n'offre pas de bouton",
         bm.state(sonde(liste=TOUT_A_JOUR, noeuds=4))["can_run"] is False)

# ── 3. LE cas qui ferait reflasher en boucle ──────────────────────────────────────────────────
#     Après une mise à jour réussie, `--list` dit TOUJOURS « out of date » jusqu'à la coupure.
p = bm.decide(bm.gather_facts(sonde(flashe=BOOT)))
verifier("flashée, même démarrage : on attend la COUPURE, on ne reflashe pas",
         p["status"] == "pending_shutdown", p["status"])
verifier("et le bouton est retiré", bm.state(sonde(flashe=BOOT))["can_run"] is False)

# ── 4. Le marqueur se périme tout seul au redémarrage ─────────────────────────────────────────
p = bm.decide(bm.gather_facts(sonde(flashe="un-autre-demarrage")))
verifier("après un VRAI redémarrage, une carte encore périmée est de nouveau signalée",
         p["status"] == "update", p["status"])

# ── 5. Pas d'outil : une absence d'information, pas « aucune carte » ──────────────────────────
p = bm.decide(bm.gather_facts(sonde(tool=False)))
verifier("sans l'outil : on ne conclut PAS « aucune carte »", p["status"] == "no_tool", p["status"])

# ── 6. Le noyau voit du matériel que l'outil ne liste pas ─────────────────────────────────────
p = bm.decide(bm.gather_facts(sonde(liste="Available Devices:\n", noeuds=12)))
verifier("noyau voit, outil non : le désaccord est signalé",
         p["code"] == "kernel_sees_cards_tool_does_not", p["code"])
p = bm.decide(bm.gather_facts(sonde(liste="Available Devices:\n", noeuds=0)))
verifier("aucune carte nulle part : réponse franche", p["status"] == "no_card", p["status"])

# ── 7. LA FIN DE COURSE : coupure ou redémarrage, et jamais l'un pour l'autre ─────────────────
#     Poser le pilote exige un REDÉMARRAGE (modules construits par DKMS) ; écrire un micrologiciel
#     exige une COUPURE. Confondre les deux envoie l'exploitant faire une manœuvre qui ne change
#     rien, et la carte revient identique. Quand un même passage fait les deux, la coupure gagne :
#     c'est la plus forte, et un redémarrage ensuite serait une seconde interruption pour rien.
#
#     ⚠ Ce contrôle a d'abord été écrit comme une recherche de texte dans le fichier
#       (« `return EXIT_REBOOT` ne doit pas apparaître »). Il est devenu FAUX le jour où le kind a
#       appris à installer le pilote — un contrôle qui lit le code plutôt que son comportement se
#       périme à la première extension légitime. On exécute `run` pour de vrai.
def sortie_de(paquet_apres, liste_apres, fichiers=(), paquet="16.4a1", liste=RELEVE_REEL):
    """Lance `run` sur des faits scriptés, sans toucher au système."""
    etapes = {"n": 0}

    def probe(nom):
        pose = etapes["n"] > 0                       # vrai une fois l'installation passée
        # ⚠ L'ARCHIVE EST CONSOMMÉE PAR LA POSE (renommée « .installed ») : la simuler encore
        #   présente après coup faisait boucler la séquence sur « à installer » au lieu de
        #   passer au micrologiciel. C'est ce que la garde a révélé le jour même.
        base = sonde(liste=(liste_apres if pose else liste),
                     paquet=(paquet_apres if pose else paquet),
                     tool=True, noeuds=12,
                     fichiers=(tuple(f + ".installed" for f in fichiers) if pose else fichiers))
        return base(nom)

    bm._sh = lambda cmd, timeout=120: (0, "All updates complete")   # noqa: SLF001
    bm._marqueur_pose = lambda b: None                              # noqa: SLF001
    reel_install = bm.installer_pilote
    bm.installer_pilote = lambda f: (etapes.__setitem__("n", 1) or True)
    try:
        return bm.run(probe)
    finally:
        bm.installer_pilote = reel_install

verifier("micrologiciel seul → ARRÊT COMPLET (11)",
         sortie_de("16.4a1", RELEVE_REEL) == bm.EXIT_SHUTDOWN)
verifier("pilote seul → REDÉMARRAGE (10)",
         sortie_de("16.4a1", TOUT_A_JOUR, fichiers=("x.tar.gz",), paquet="") == bm.EXIT_REBOOT)
# ⚠ L'ARCHIVE EST CONSOMMÉE PAR LA POSE : après l'installation elle porte « .installed » et ne
#   redéclenche plus rien. Sans ça, l'état revenait à « à installer » et la séquence
#   pilote → micrologiciel ne se terminait JAMAIS — la garde l'a attrapé le jour même.
verifier("pilote PUIS micrologiciel → la coupure l'emporte (11)",
         sortie_de("16.4a1", RELEVE_REEL, fichiers=("x.tar.gz",), paquet="") == bm.EXIT_SHUTDOWN)
verifier("une archive déjà posée ne redéclenche pas l'installation",
         bm.installable(["x.tar.gz.installed"]) is None, bm.installable(["x.tar.gz.installed"]))
verifier("mais une archive NEUVE déposée à côté est bien vue",
         bm.installable(["x.tar.gz.installed", "y.tar.gz"]) == "y.tar.gz",
         bm.installable(["x.tar.gz.installed", "y.tar.gz"]))

# ★ IMPORTER UN MICROLOGICIEL = IMPORTER UN PILOTE. Les images de flash vivent DANS le paquet
#   (/usr/lib/blackmagic/DesktopVideo/Firmware/data-*.bin, vérifié sur dell-1) : il n'existe pas
#   de fichier de micrologiciel séparé. Le dépôt doit donc rester offert même quand tout va bien.
e = bm.state(sonde(liste=TOUT_A_JOUR, noeuds=4))
verifier("tout à jour : le dépôt d'une archive reste offert", e["wants_upload"] is True)
e = bm.state(sonde(liste=TOUT_A_JOUR, noeuds=4, fichiers=("neuf.tar.gz",)))
verifier("une archive déposée sur un système sain propose une MISE À NIVEAU",
         e["status"] == "upgrade" and e["can_run"] is True, e["status"])
verifier("rien à faire → 0", sortie_de("16.4a1", TOUT_A_JOUR, liste=TOUT_A_JOUR) == bm.EXIT_OK)

# ── 8. INSTALLATION HORS LIGNE du pilote Desktop Video ───────────────────────────────────────
#     Un nœud peut n'avoir AUCUN accès sortant, et l'archive se prend de toute façon à la main
#     sur une page de leur site (formulaire d'enregistrement, lien « Télécharger uniquement » —
#     ni compte ni contrat, contrairement à ce qui était écrit ici jusqu'au 2026-10-31).
#     Demander le fichier est donc le cas NORMAL, pas une erreur : c'est l'objet de la zone de dépôt.
ARCHIVE = "Blackmagic_Desktop_Video_Linux_16.4a1.tar.gz"

e = bm.state(sonde(paquet="", liste="", tool=False, noeuds=0))
verifier("pilote absent, rien de déposé : on RÉCLAME le fichier",
         e["status"] == "install_needs_file", e["status"])
verifier("et l'interface doit proposer le dépôt", e["wants_upload"] is True)
verifier("sans proposer de lancer quoi que ce soit", e["can_run"] is False)
verifier("un indice dit QUEL fichier déposer",
         "tar.gz" in (e.get("upload_hint") or ""), e.get("upload_hint"))

e = bm.state(sonde(paquet="", liste="", tool=False, noeuds=0, fichiers=(ARCHIVE,)))
verifier("fichier déposé : l'installation devient possible",
         e["status"] == "install" and e["can_run"] is True, e["status"])
verifier("et la zone de dépôt disparaît", e["wants_upload"] is False)
verifier("c'est bien l'archive déposée qui sera posée",
         e["plan"]["params"]["file"] == ARCHIVE, e["plan"]["params"].get("file"))

e = bm.state(sonde(paquet="", liste="", tool=False, noeuds=0, fichiers=(ARCHIVE,), dkms=False))
verifier("prérequis manquant : on le DIT au lieu d'échouer à mi-chemin",
         e["status"] == "missing_prereqs", e["status"])
verifier("et on NOMME ce qui manque",
         "dkms" in (e["plan"]["params"].get("missing") or ""), e["plan"]["params"].get("missing"))
# ★ TRANCHÉ LE 2026-11-14 EN FAVEUR DU PILOTE : `missing_prereqs` est devenu EXÉCUTABLE en
#   1.3.0, et ce fichier attendait encore `can_run is False`, c'est-à-dire le cul-de-sac d'avant.
#   Ce qui a changé n'est pas la règle mais le geste disponible : le plan porte désormais DEUX
#   étapes, `prereqs` puis `install`, et `install_prerequisites` pose les .deb déposés à côté de
#   l'archive avant de tenter APT. Sur un nœud sans accès sortant — le cas normal ici — nommer ce
#   qui manque sans accepter qu'on l'apporte ne laissait qu'un shell sur l'hôte, ce que cet écran
#   existe pour éviter.
#   L'INTENTION DU CAS NE CHANGE PAS pour autant : ce qui serait voué à l'échec, c'est de lancer
#   l'installation du PILOTE sans ses prérequis. C'est donc l'ORDRE des étapes qu'il faut tenir,
#   et c'est ce que ce cas mesure maintenant — un plan qui poserait `install` en tête serait
#   exactement la panne de 1.2.x, qu'un simple `can_run is True` ne verrait pas.
verifier("un geste existe : les prérequis s'installent, l'archive est déjà là",
         e["can_run"] is True, e["can_run"])
verifier("et les prérequis passent AVANT l'installation du pilote",
         [p["id"] for p in e["plan"]["steps"]] == ["prereqs", "install"],
         [p["id"] for p in e["plan"]["steps"]])
verifier("ce n'est plus l'archive qu'on réclame, mais les paquets manquants",
         "dkms_" in (e.get("upload_hint") or ""), e.get("upload_hint"))

# ★ LES DEUX PRÉREQUIS AJOUTÉS EN 1.3.0 N'AVAIENT AUCUN CAS, et ce sont EXACTEMENT ceux sur
#   lesquels dpkg a refusé le 2026-11-13, pendant que l'écran affichait « dkms oui, en-têtes
#   oui ». Un prérequis vérifié par le pilote et par aucune garde se périme sans bruit.
for nom, arg in (("le méta-paquet d'en-têtes", "headers_meta"), ("libgl1", "libgl")):
    e = bm.state(sonde(paquet="", liste="", tool=False, noeuds=0, fichiers=(ARCHIVE,),
                       **{arg: False}))
    verifier("%s manquant est DÉTECTÉ" % nom, e["status"] == "missing_prereqs", e["status"])
    verifier("et il est NOMMÉ à l'exploitant",
             arg.replace("headers_meta", "headers meta-package").replace("libgl", "libgl1")
             in (e["plan"]["params"].get("missing") or ""),
             e["plan"]["params"].get("missing"))

# Le pilote absent passe AVANT tout le reste : sans lui il n'y a ni outil, ni /dev/blackmagic.
e = bm.state(sonde(paquet="", liste=RELEVE_REEL, noeuds=12))
verifier("pilote absent : ce cas prime sur tout le reste",
         e["status"] in ("install_needs_file", "install"), e["status"])

# Paquet là mais outil absent = installation incomplète, PAS « aucune carte ».
p = bm.decide(bm.gather_facts(sonde(tool=False)))
verifier("paquet posé, outil absent : installation incomplète", p["status"] == "no_tool", p["status"])

# ── 9. UNE CARTE SUR LE BUS ET AUCUN MODULE N'EST PAS « AUCUNE CARTE » (2026-11-14) ───────────
#     Vécu sur un nœud : pilote posé, deux Quad 2 sur le bus PCI, DKMS avait compilé pour 6.12.116,
#     le redémarrage demandé par le panneau a démarré 6.12.117 — et l'écran disait « Aucune carte
#     DeckLink sur ce nœud ». Sans module, l'outil ne liste rien : la conclusion était fausse.
VIDE = "Available Devices:\n"
e = bm.state(sonde(liste=VIDE, noeuds=0, modules=0, pci=2, built=0))
verifier("cartes sur le bus, aucun module : ce n'est PAS « aucune carte »",
         e["status"] == "module_missing" and e["plan"]["code"] == "module_not_built",
         "%s/%s" % (e["status"], e["plan"]["code"]))
verifier("et un geste est offert : compiler pour le noyau EN COURS",
         e["can_run"] is True and e["plan"]["steps"][0]["params"]["kernel"] == "6.12.117+deb13-amd64",
         e["plan"]["steps"])
e = bm.state(sonde(liste=VIDE, noeuds=0, modules=0, pci=2, built=2))
verifier("module compilé mais pas chargé : on le CHARGE, sans recompiler",
         e["plan"]["code"] == "module_not_loaded" and e["plan"]["steps"] == ["load"],
         e["plan"]["code"])
e = bm.state(sonde(liste=VIDE, noeuds=0, modules=0, pci=2, built=0, headers=False))
verifier("sans les en-têtes du noyau en cours : on les pose AVANT de compiler",
         [x["id"] for x in e["plan"]["steps"]] == ["prereqs", "build"]
         and e["plan"]["code"] == "module_not_built_no_headers", e["plan"]["code"])
verifier("et un nœud hors ligne peut les déposer, le nom exact est donné",
         e["wants_upload"] is True and "linux-headers-6.12.117+deb13-amd64" in e["upload_hint"],
         e.get("upload_hint"))
p = bm.decide(bm.gather_facts(sonde(liste=VIDE, noeuds=0, modules=0, pci=0, built=0)))
verifier("rien sur le bus, rien de chargé : « aucune carte » reste la réponse", p["status"] == "no_card",
         p["status"])


def rejouer(cas, rc_dkms=0, apres_charge=True, install=False):
    """run() on the node's real situation; returns (code, dkms install calls, log)."""
    etat = {"charge": False}
    noyaux = "6.12.107+deb13-amd64 no\n6.12.116+deb13-amd64 yes\n6.12.117+deb13-amd64 yes\n"
    confs = ("/usr/src/blackmagic-io-16.5a1/dkms.conf:PACKAGE_NAME=blackmagic-io\n"
             "/usr/src/blackmagic-io-16.5a1/dkms.conf:PACKAGE_VERSION=16.5a1\n"
             "/usr/src/blackmagic-16.5a1/dkms.conf:PACKAGE_NAME=blackmagic\n"
             "/usr/src/blackmagic-16.5a1/dkms.conf:PACKAGE_VERSION=16.5a1\n")
    poses, journal = [], []

    def sh(cmd, timeout=120):
        if "dkms.conf" in cmd:
            return 0, confs
        if cmd.startswith("for d in /lib/modules"):
            return 0, noyaux
        if cmd.startswith("find /lib/modules/"):
            k = cmd.split()[1].rsplit("/", 1)[-1]
            return 0, "2" if (k == "6.12.116+deb13-amd64" or k in poses) else "0"
        if cmd.startswith("dkms install"):
            k = cmd.split("-k ")[1].split()[0]
            if rc_dkms == 0 and k not in poses:
                poses.append(k)
            return rc_dkms, "make failed" if rc_dkms else "installed"
        if cmd.startswith("modprobe"):
            etat["charge"] = apres_charge
            return 0, ""
        return 0, ""

    def probe(nom):
        ok = etat["charge"]
        return sonde(liste=(TOUT_A_JOUR if ok else VIDE), noeuds=(8 if ok else 0),
                     modules=(2 if ok else 0), pci=2, built=0,
                     paquet=("" if install and not etat.get("pose") else "16.5a1"),
                     fichiers=(("x.tar.gz",) if install and not etat.get("pose") else ()))(nom)

    vrais = (bm._sh, bm.say, bm.installer_pilote)
    bm._sh = sh
    bm.say = journal.append
    bm.installer_pilote = lambda fichier: etat.__setitem__("pose", True) or True
    try:
        return bm.run(probe), poses, journal
    finally:
        bm._sh, bm.say, bm.installer_pilote = vrais


code, poses, journal = rejouer("module")
verifier("le bouton compile pour le noyau en cours, charge, et rend 0",
         code == bm.EXIT_OK and "6.12.117+deb13-amd64" in poses, "%s %s" % (code, poses))
verifier("le noyau sans en-têtes est NOMMÉ, avec le paquet qui le répare",
         any("6.12.107+deb13-amd64" in l and "linux-headers-6.12.107" in l for l in journal))
verifier("pas de « Nothing to do » après un chargement réussi",
         not any(l.startswith("Nothing to do") for l in journal))
code, poses, journal = rejouer("module", rc_dkms=1)
verifier("compilation en échec : le geste ÉCHOUE et le dit", code == 1
         and any("FAILED" in l for l in journal), code)
code, poses, journal = rejouer("module", apres_charge=False)
verifier("module compilé mais refusé par le noyau : échec, pas un faux succès", code == 1, code)
# L'installation, elle, doit compiler pour TOUS les noyaux installés qui ont leurs en-têtes,
# avant de demander le redémarrage qui peut en démarrer un autre.
code, poses, journal = rejouer("install", install=True)
verifier("installation : le module est compilé pour le noyau EN ATTENTE (6.12.117)",
         "6.12.117+deb13-amd64" in poses, poses)
verifier("et le redémarrage n'est demandé qu'ensuite", code == bm.EXIT_REBOOT, code)

print("→ le pilote de micrologiciel dit la vérité et ne reflashe pas en rond." if not ko
      else "→ ÉCHEC : le pilote de micrologiciel se trompe.")
sys.exit(ko)
