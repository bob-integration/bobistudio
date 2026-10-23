#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# run_bench.py — pilote du banc « plugins tiers, étape 0 » (docs/chantiers/CONSOLE_PLUGINS_TIERS_BANC0.md).
# Tourne DANS le conteneur jetable (image construite par ./Dockerfile). Python ne fait que lancer
# ./engine (C) avec chaque configuration et ranger les résumés JSON ; aucun chemin temps réel ici.
#
#   python3 run_bench.py list                     # scénarios
#   python3 run_bench.py run <scénario…> --minutes 3 --cpus 81,83 --fifo 70 --out results/
#   python3 run_bench.py inventory --out results/  # inventaire LV2 installé (python3-lilv)
import argparse
import json
import os
import subprocess
import sys
import time

DPL = "http://gareus.org/oss/lv2/dpl#stereo"                       # x42 Digital Peak Limiter
LSPC = "http://lsp-plug.in/plugins/lv2/compressor_stereo"           # LSP Compressor Stereo
LSPMB = "http://lsp-plug.in/plugins/lv2/mb_compressor_stereo"       # LSP Multiband Compressor Stereo
DEESS = "http://calf.sourceforge.net/plugins/Deesser"               # Calf Deesser
REVERB = "urn:dragonfly:room"                                       # Dragonfly Room Reverb


def scenarios(ec, hc):
    """(nom, args engine, durée relative : 1 = --minutes, sinon secondes fixes)."""
    S = []
    for name, uri in (("null", "null"), ("dpl", DPL), ("lspcomp", LSPC), ("lspmb", LSPMB),
                      ("deess", DEESS), ("reverb", REVERB)):
        S.append(("inproc-" + name, ["--mode", "inproc", "--uri", uri, "--engine-cpu", ec], 1))
        S.append(("coproc-same-" + name, ["--mode", "coproc", "--uri", uri, "--engine-cpu", ec,
                                          "--host-cpu", ec], 1))
        S.append(("coproc-other-" + name, ["--mode", "coproc", "--uri", uri, "--engine-cpu", ec,
                                           "--host-cpu", hc], 1))
    S.append(("async-other-dpl", ["--mode", "async", "--uri", DPL, "--engine-cpu", ec, "--host-cpu", hc,
                                  "--async-offset-us", "2500"], 60))
    # Fautes (co-processus, même cœur — configuration réaliste : la console a UN cœur épinglé)
    S.append(("fault-kill-dpl", ["--mode", "coproc", "--uri", DPL, "--engine-cpu", ec, "--host-cpu", ec,
                                 "--kill-every-s", "5", "--hang-ms", "50"], 120))
    S.append(("fault-kill-lspmb", ["--mode", "coproc", "--uri", LSPMB, "--engine-cpu", ec, "--host-cpu", ec,
                                   "--kill-every-s", "5", "--hang-ms", "50"], 120))
    S.append(("fault-slow3ms-dpl", ["--mode", "coproc", "--uri", DPL, "--engine-cpu", ec, "--host-cpu", ec,
                                    "--slow-us", "3000", "--slow-every", "200", "--hang-ms", "50"], 60))
    S.append(("fault-slow12ms-dpl", ["--mode", "coproc", "--uri", DPL, "--engine-cpu", ec, "--host-cpu", ec,
                                     "--slow-us", "12000", "--slow-every", "200", "--hang-ms", "50"], 60))
    S.append(("fault-hang-dpl", ["--mode", "coproc", "--uri", DPL, "--engine-cpu", ec, "--host-cpu", ec,
                                 "--slow-us", "100000000", "--slow-every", "2000", "--hang-ms", "50"], 60))
    # Boucle infinie ACTIVE en SCHED_FIFO, TOUT le processus moteur (surveillant compris) confiné au cœur
    # du moteur, comme la console qui n'a qu'UN cœur épinglé : le chien de garde tient-il ?
    S.append(("fault-spin-onecore-dpl", ["taskset", "-c", ec, "./engine", "--mode", "coproc", "--uri", DPL,
                                         "--engine-cpu", ec, "--host-cpu", ec, "--slow-us", "-100000000",
                                         "--slow-every", "2000", "--hang-ms", "50"], 60))
    S.append(("fault-spin-onecore-hostother-dpl", ["taskset", "-c", ec, "./engine", "--mode", "coproc", "--uri", DPL,
                                                   "--engine-cpu", ec, "--host-cpu", ec, "--host-fifo", "0",
                                                   "--slow-us", "-100000000", "--slow-every", "2000",
                                                   "--hang-ms", "50"], 60))
    S.append(("fault-spin-onecore-rtwd-dpl", ["taskset", "-c", ec, "./engine", "--mode", "coproc", "--uri", DPL,
                                              "--engine-cpu", ec, "--host-cpu", ec, "--slow-us", "-100000000",
                                              "--slow-every", "2000", "--rt-watchdog-blocks", "10"], 60))
    S.append(("coproc-same-hostother-dpl", ["--mode", "coproc", "--uri", DPL, "--engine-cpu", ec,
                                            "--host-cpu", ec, "--host-fifo", "0"], 1))
    S.append(("fault-crash-inproc-dpl", ["--mode", "inproc", "--uri", DPL, "--engine-cpu", ec,
                                         "--crash-at", "400"], 10))
    S.append(("fault-slow12ms-inproc-dpl", ["--mode", "inproc", "--uri", DPL, "--engine-cpu", ec,
                                            "--slow-us", "12000", "--slow-every", "200"], 60))
    S.append(("fault-slow3ms-inproc-dpl", ["--mode", "inproc", "--uri", DPL, "--engine-cpu", ec,
                                           "--slow-us", "3000", "--slow-every", "200"], 60))
    return S


def lv2_path(uri):
    """Restreint LV2_PATH au bundle du plugin : la relance après plantage n'a pas à rescanner tous les
    bundles installés (mesuré : c'est l'essentiel du temps d'instanciation)."""
    if uri == "null":
        return None
    import lilv
    w = lilv.World()
    w.load_all()
    p = w.get_all_plugins()[w.new_uri(uri)]
    path = p.get_bundle_uri().get_path()
    return os.path.dirname(path.rstrip("/"))


def run(args):
    ec, hc = args.cpus.split(",")
    allsc = {n: (a, d) for n, a, d in scenarios(ec, hc)}
    os.makedirs(args.out, exist_ok=True)
    for name in args.names:
        sel = [n for n in allsc if n == name or (name.endswith("*") and n.startswith(name[:-1]))]
        for n in sel:
            a, d = allsc[n]
            secs = args.minutes * 60 if d == 1 else d
            pre = []
            if a[0] == "taskset":
                pre, a = a[:3], a[4:]
            cmd = pre + ["./engine"] + a + ["--seconds", str(secs), "--fifo", str(args.fifo),
                                      "--deadline-us", str(args.deadline_us)]
            env = dict(os.environ)
            uri = a[a.index("--uri") + 1]
            if args.restrict_path:
                bp = lv2_path(uri)
                if bp:
                    env["LV2_PATH"] = bp
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            res = {"name": n, "cmd": " ".join(cmd), "rc": r.returncode, "stderr": r.stderr[-2000:],
                   "lv2_path": env.get("LV2_PATH"), "wall_s": round(time.time() - t0, 1)}
            if r.returncode == 0:
                res.update(json.loads(r.stdout))
            with open(os.path.join(args.out, n + ".json"), "w") as f:
                json.dump(res, f, indent=1)
            rt = res.get("roundtrip") or {}
            print("%-28s rc=%-4s ok=%-6s dry=%s/%s/%s rt p50=%s p99=%s max=%s overruns=%s" % (
                n, r.returncode, res.get("ok"), res.get("dry_deadline"), res.get("dry_busy"),
                res.get("dry_nohost"), rt.get("p50_us"), rt.get("p99_us"), rt.get("max_us"),
                res.get("engine_overruns")), flush=True)


def nodes_list(nodes):
    return [str(nodes[i]) for i in range(len(nodes))]


def inventory(args):
    """Tous les plugins LV2 installés : classe, licence déclarée (doap:license), latence déclarée
    (port lv2:reportsLatency / designation lv2:latency), fonctions requises, ports audio."""
    import lilv
    w = lilv.World()
    w.load_all()
    ns = lambda u: w.new_uri(u)
    LIC = ns("http://usefulinc.com/ns/doap#license")
    RL = ns("http://lv2plug.in/ns/lv2core#reportsLatency")
    DES = ns("http://lv2plug.in/ns/lv2core#designation")
    LAT = ns("http://lv2plug.in/ns/lv2core#latency")
    AUDIO, INP = ns(lilv.LILV_URI_AUDIO_PORT), ns(lilv.LILV_URI_INPUT_PORT)
    out = []
    for p in w.get_all_plugins():
        lic = w.get(p.get_uri(), LIC, None)
        ain = aout = 0
        lat = False
        for i in range(p.get_num_ports()):
            port = p.get_port_by_index(i)
            if port.is_a(AUDIO):
                if port.is_a(INP):
                    ain += 1
                else:
                    aout += 1
            if port.has_property(RL):
                lat = True
            d = port.get(DES)
            if d is not None and str(d) == str(LAT):
                lat = True
        out.append({"uri": str(p.get_uri()), "name": str(p.get_name()),
                    "class": str(p.get_class().get_label()),
                    "license": str(lic) if lic is not None else None,
                    "bundle": str(p.get_bundle_uri()), "audio_in": ain, "audio_out": aout,
                    "reports_latency": lat,
                    "required": nodes_list(p.get_required_features())})
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "inventory.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("%d plugins" % len(out))


def probe(args):
    """Pour CHAQUE plugin de l'inventaire qui a au moins une entrée et une sortie audio : 1 s en
    in-process (aucune faute) → chargeable ou non (et pourquoi), latence déclarée APRÈS run() avec les
    réglages par défaut, coût par bloc de 240 (p50/p99/max). Le coût dépend des réglages : indicatif."""
    from urllib.parse import unquote, urlparse
    inv = json.load(open(os.path.join(args.out, "inventory.json")))
    res = []
    for p in inv:
        if p["audio_in"] < 1 or p["audio_out"] < 1:
            continue
        env = dict(os.environ, LV2_PATH=os.path.dirname(unquote(urlparse(p["bundle"]).path).rstrip("/")))
        cmd = ["./engine", "--mode", "inproc", "--uri", p["uri"], "--seconds", str(args.seconds),
               "--engine-cpu", args.cpu]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
            rc, err = r.returncode, r.stderr.strip()[-300:]
            try:
                d = json.loads(r.stdout) if r.returncode == 0 else {}
            except ValueError as e:
                d, rc, err = {}, "bad_json", str(e)
        except subprocess.TimeoutExpired:
            d, rc, err = {}, "timeout", ""
        pp = d.get("plugin_process") or {}
        res.append({"uri": p["uri"], "name": p["name"], "class": p["class"], "rc": rc, "error": err,
                    "latency_samples": d.get("plugin_latency_samples"), "p50_us": pp.get("p50_us"),
                    "p99_us": pp.get("p99_us"), "max_us": pp.get("max_us"), "nan": d.get("nan_samples")})
        print("%-60s rc=%s lat=%s p50=%s p99=%s %s" % (p["name"][:60], rc, res[-1]["latency_samples"],
                                                      pp.get("p50_us"), pp.get("p99_us"), err[:80]), flush=True)
    with open(os.path.join(args.out, "probe.json"), "w") as f:
        json.dump(res, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    r = sp.add_parser("run")
    r.add_argument("names", nargs="+")
    r.add_argument("--minutes", type=float, default=3)
    r.add_argument("--cpus", default="81,83", help="cœur moteur,cœur hôte « autre »")
    r.add_argument("--fifo", type=int, default=0)
    r.add_argument("--deadline-us", type=float, default=2000)
    r.add_argument("--restrict-path", type=int, default=1)
    r.add_argument("--out", default="results")
    i = sp.add_parser("inventory")
    i.add_argument("--out", default="results")
    pr = sp.add_parser("probe")
    pr.add_argument("--out", default="results")
    pr.add_argument("--cpu", default="85")
    pr.add_argument("--seconds", type=float, default=1.0)
    a = ap.parse_args()
    if a.cmd == "list":
        for n, x, d in scenarios("E", "H"):
            print(n, " ".join(x), "(--minutes)" if d == 1 else "(%ss)" % d)
    elif a.cmd == "run":
        run(a)
    elif a.cmd == "probe":
        probe(a)
    else:
        inventory(a)


if __name__ == "__main__":
    sys.exit(main())
