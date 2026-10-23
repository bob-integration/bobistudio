#!/usr/bin/env python3
"""Garde : on ne redémarre (ni n'arrête) pas un nœud normalement s'il porte un processus bloqué
dans le noyau.

DÉFAUT D'ORIGINE (2026-08-12, refait le 2026-10-19) : `systemctl reboot` sur r620-1 alors que deux
`mxl-fabrics-demo` étaient bloqués en état D dans `cm_destroy_id`. L'arrêt attend ces processus,
qui ne finissent jamais : la machine coupe son réseau et reste là, ni éteinte ni relancée. Aucun
BMC — coupure physique nécessaire. Le bouton « Redémarrer » de l'interface faisait exactement ça.

On exécute le VRAI `app/node_power.py` contre un agent-nœud doublé, et on vérifie que les trois
routes (deux redémarrages, un arrêt) passent par lui. Aucun nœud contacté.
"""
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    from app import node_power as np_

    etat = {"ps1": "", "ps2": "", "rc_probe": 0}
    envoyes = []

    def host_exec(node, cmd, timeout=None, input_data=None):
        if cmd == np_._PROBE:
            return etat["rc_probe"], etat["ps1"] + "\n---\n" + etat["ps2"], ""
        envoyes.append(cmd)
        return 0, "", ""
    np_.node_driver.host_exec = host_exec
    node = {"id": 42, "name": "r620-1"}

    D1 = " 6254 D    mxl-fabrics-dem cm_destroy_id\n 6710 Ds   mxl-fabrics-dem cm_destroy_id"
    TRANSITOIRE = " 812 D    jbd2/sda1-8     -"

    print("\n1. Aucun processus bloqué : redémarrage normal, comme avant")
    body, code = np_.reboot(node)
    verifier(code == 200 and body.get("mode") == "normal", "accepté en mode normal (%s %s)" % (code, body))
    verifier(envoyes == [np_._NORMAL_REBOOT], "★ c'est `systemctl reboot` qui part")

    print("\n2. Un état D d'un instant (écriture disque) ne bloque rien")
    envoyes.clear()
    etat["ps1"], etat["ps2"] = TRANSITOIRE, ""
    body, code = np_.reboot(node)
    verifier(code == 200 and envoyes == [np_._NORMAL_REBOOT], "★ D vu à un seul relevé → ignoré")

    print("\n3. Processus bloqués (le cas r620-1) sans `force` : REFUS")
    envoyes.clear()
    etat["ps1"], etat["ps2"] = D1 + "\n" + TRANSITOIRE, D1
    body, code = np_.reboot(node)
    verifier(code == 409 and body.get("error") == "blocked_processes", "refusé en 409 (%s)" % code)
    verifier(envoyes == [], "★ RIEN n'est envoyé au nœud")
    verifier([p["pid"] for p in body.get("blocked") or []] == [6254, 6710],
             "les deux processus durables sont listés, pas le transitoire")
    verifier((body.get("blocked") or [{}])[0].get("wchan") == "cm_destroy_id", "avec leur point de blocage")

    print("\n4. Avec `force` : redémarrage sysrq")
    body, code = np_.reboot(node, force=True)
    verifier(code == 200 and body.get("mode") == "sysrq", "accepté en mode sysrq")
    verifier(envoyes == [np_._SYSRQ_REBOOT], "★ c'est sysrq qui part, jamais `systemctl reboot`")
    verifier("echo s >" in np_._SYSRQ_REBOOT and np_._SYSRQ_REBOOT.index("echo s >") < np_._SYSRQ_REBOOT.index("echo b >"),
             "les écritures sont vidées (s) AVANT le redémarrage (b)")
    verifier(np_._SYSRQ_REBOOT.startswith("setsid nohup") and np_._SYSRQ_REBOOT.rstrip().endswith("&"),
             "détaché : l'appel rend la main avant que le noyau ne tombe")

    print("\n5. Sonde impossible : on ne redémarre pas à l'aveugle")
    envoyes.clear()
    etat["rc_probe"] = 255
    body, code = np_.reboot(node, force=True)
    verifier(code == 502 and body.get("error") == "probe_failed" and envoyes == [],
             "★ refus, rien d'envoyé, même avec `force`")
    etat["rc_probe"] = 0

    print("\n6. Arrêt complet")
    verifier(np_.poweroff_guard(node)[1] == 409, "★ refusé tant qu'un processus est bloqué")
    etat["ps1"] = etat["ps2"] = ""
    verifier(np_.poweroff_guard(node) is None, "autorisé sinon")

    print("\n7. Les trois routes passent par le garde")
    def src(p):
        return open(os.path.join(RACINE, p), encoding="utf-8").read()
    nd, mt, hd = src("app/routes/node_disks.py"), src("app/routes/mtl_engine.py"), src("app/routes/host_drivers.py")
    # Un APPEL réel (début de ligne de code), pas une mention dans un commentaire.
    import re
    appel = lambda txt, motif: re.search(r"^\s*" + motif, txt, re.M) is not None
    verifier(appel(nd, r"body, code = node_power\.reboot\(") and "systemctl reboot" not in nd,
             "★ /api/nodes/<id>/reboot (panneau Stockage)")
    verifier(appel(mt, r"body, code = node_power\.reboot\("), "★ /api/mtl/reboot (cartes MTL et pilotes hôte)")
    verifier(appel(hd, r"refus = node_power\.poweroff_guard\(node\)"), "★ /api/nodes/<id>/host-poweroff")

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
