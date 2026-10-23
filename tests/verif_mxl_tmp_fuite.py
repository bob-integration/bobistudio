#!/usr/bin/env python3
"""Garde : un écrivain AUDIO ouvert sur un flux existant ne laisse pas de répertoire temporaire.

DÉFAUT D'ORIGINE (relevé le 2026-10-16 sur dl360-1) : libmxl 1.1.0 construit un flux continu dans
`.mxl-tmp-…` puis le renomme ; quand le flux existe déjà, le renommage échoue, le flux existant est
ouvert — et le temporaire (~600 Ko pour 8 canaux) n'est effacé QUE sur le chemin d'exception
(`FlowManager::createContinuousFlow`, à comparer au `defer` du chemin discret). Un plugin qui
recréait ses écrivains chaque seconde en a laissé 74 388 en 21 heures : /dev/shm plein (47 Go), et
plus aucun producteur du nœud ne pouvait créer de flux.

Tourne SANS libmxl : `_lib` est un double dont `mxlCreateFlowWriter` répond « flux existant » en
laissant un temporaire, comme la vraie bibliothèque. Domaine jetable.
"""
import ctypes
import json
import os
import sys
import tempfile
import time

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_R, "script_templates"))
import bobimxl                                    # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def temporaire(domaine, suffixe, fid, age_s, avec_def=True):
    d = os.path.join(domaine, ".mxl-tmp-XXXXXXXXXX" + suffixe)
    os.mkdir(d)
    if avec_def:
        with open(os.path.join(d, "flow_def.json"), "w") as f:
            json.dump({"id": fid}, f)
    with open(os.path.join(d, "channels"), "wb") as f:
        f.write(b"\0" * 1024)
    t = time.time() - age_s
    os.utime(d, (t, t))
    return d


def main():
    nous = bobimxl.flow_id("hello-world_hello_audio")
    autre = bobimxl.flow_id("avsync_audio")

    print("\n1. Le balayage, à nu")
    dom = tempfile.mkdtemp()
    vieux_nous = temporaire(dom, "a", nous, 30)
    jeune_nous = temporaire(dom, "b", nous, 0.5)          # création EN COURS : intouchable
    recent_autre = temporaire(dom, "c", autre, 60)        # celui d'un autre, récent : pas à nous
    debris = temporaire(dom, "d", autre, 3600)            # n'importe qui, 1 h : débris
    sans_def = temporaire(dom, "e", nous, 30, avec_def=False)
    flux = os.path.join(dom, nous + ".mxl-flow")          # un VRAI flux, vieux : jamais touché
    os.mkdir(flux)
    os.utime(flux, (time.time() - 86400,) * 2)
    fichier = os.path.join(dom, ".mxl-tmp-fichier")
    open(fichier, "w").close()
    os.utime(fichier, (time.time() - 86400,) * 2)

    n = bobimxl.sweep_leaked_flow_tmp(dom, nous)
    verifier(not os.path.exists(vieux_nous), "★ notre temporaire abandonné est retiré")
    verifier(not os.path.exists(debris), "★ un temporaire de plus de 10 min est retiré, quel qu'il soit")
    verifier(os.path.exists(jeune_nous), "une création EN COURS (< 2 s) n'est pas touchée")
    verifier(os.path.exists(recent_autre), "le temporaire récent d'un AUTRE flux n'est pas touché")
    verifier(os.path.exists(sans_def), "un temporaire illisible et récent n'est pas touché")
    verifier(os.path.isdir(flux), "★ un flux NOMMÉ n'est jamais touché, même vieux")
    verifier(os.path.exists(fichier), "un FICHIER au préfixe temporaire n'est pas touché")
    verifier(n == 2, "le compte rendu est juste (%d)" % n)
    verifier(bobimxl.sweep_leaked_flow_tmp(os.path.join(dom, "absent"), nous) == 0,
             "domaine absent : 0, sans exception")

    print("\n2. L'écrivain audio, avec une libmxl doublée")
    dom = tempfile.mkdtemp()
    os.mkdir(os.path.join(dom, nous + ".mxl-flow"))       # le flux EXISTE déjà
    fuite = {"n": 0}

    class Lib:
        @staticmethod
        def mxlCreateFlowWriter(inst, flow_def, opts, out_h, _cfg, out_created):
            # Ce que fait libmxl 1.1.0 sur un flux existant : temporaire laissé, created=False.
            fuite["n"] += 1
            temporaire(dom, "w%d" % fuite["n"], json.loads(flow_def)["id"], 5)
            ctypes.cast(out_created, ctypes.POINTER(ctypes.c_bool))[0] = False
            return bobimxl.MXL_STATUS_OK

    bobimxl._lib = Lib()
    inst = type("Inst", (), {"domain": dom, "_h": None})()
    for _ in range(50):                                   # 50 recréations, comme la boucle d'erreur
        w = bobimxl.AudioWriter(inst, "hello-world_hello_audio")
    restants = [e for e in os.listdir(dom) if e.startswith(".mxl-tmp-")]
    verifier(fuite["n"] == 50, "le double a bien simulé 50 fuites")
    verifier(not restants, "★ après 50 recréations, aucun temporaire ne reste (%d)" % len(restants))
    verifier(getattr(w, "leaked_tmp_swept", None) == 1, "l'écrivain dit ce qu'il a retiré")
    verifier(os.path.isdir(os.path.join(dom, nous + ".mxl-flow")), "le flux, lui, est intact")

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
