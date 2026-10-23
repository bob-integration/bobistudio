#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Capture / rejeu des échanges avec une SURFACE DE CONTRÔLE — outil de banc, à lancer À LA MAIN.

Pourquoi : le transport réseau de la surface (RTP-MIDI ? mode réseau natif ?) n'est PAS connu. On ne
l'écrit pas d'après une documentation : on le MESURE (décision du chantier, docs/chantiers/
SURFACE_CONTROLE.md). Le plus sûr est de se placer EN RELAIS entre la surface et un logiciel qui la
pilote déjà : tout ce qui passe est horodaté et enregistré, dans les deux sens, sans rien interpréter.

Sous-commandes :
  listen-udp  --port P [--port P2 …]              écoute et enregistre ce qui arrive (sans répondre)
  relay-udp   --listen P --to HÔTE:PORT [--listen P2 --to HÔTE:PORT2 …]
              relais UDP transparent : le logiciel parle à CETTE machine sur P, on transmet à la
              surface, ses réponses reviennent au dernier émetteur. Deux paires pour RTP-MIDI
              (port de contrôle + port de données).
  relay-tcp   --listen P --to HÔTE:PORT            même chose en TCP (une connexion à la fois)
  replay      FICHIER [--skip N] [--dir from_device|to_device|all]
              rejoue une capture dans le DÉCODEUR du service (mcu.MidiParser + mcu.decode) et imprime
              les événements reconnus ; `--skip N` saute N octets d'en-tête par paquet (ex. 12 pour un
              en-tête RTP) — l'en-tête réel se lit dans la capture, pas dans ce commentaire.

Format de capture : une ligne JSON par paquet
  {"t": <epoch s>, "mono": <s>, "proto": "udp"|"tcp", "dir": "from_device"|"to_device"|"in",
   "src": "ip:port", "dst": "ip:port", "len": n, "hex": "…"}
Écrit sur la sortie standard, ou dans `--out FICHIER` (ajout).

Exemple (relais RTP-MIDI présumé, ports à confirmer à la capture) :
  ./venv/bin/python tools/bancs/control_surface_capture.py relay-udp \\
      --listen 5004 --to 192.0.2.50:5004 --listen 5005 --to 192.0.2.50:5005 --out surface.jsonl
  ./venv/bin/python tools/bancs/control_surface_capture.py replay surface.jsonl --dir from_device
"""
import argparse
import json
import os
import selectors
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def record(proto, direction, src, dst, data):
    return {"t": round(time.time(), 6), "mono": round(time.monotonic(), 6), "proto": proto,
            "dir": direction, "src": "%s:%s" % src[:2], "dst": "%s:%s" % dst[:2],
            "len": len(data), "hex": bytes(data).hex()}


class Sink:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8") if path else sys.stdout
        self.n = 0

    def write(self, rec):
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()
        self.n += 1


def _hostport(s):
    host, _, port = s.rpartition(":")
    return host, int(port)


def listen_udp(ports, sink):
    sel = selectors.DefaultSelector()
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", p))
        sel.register(s, selectors.EVENT_READ)
        print("listening udp/%d" % p, file=sys.stderr)
    while True:
        for key, _ in sel.select():
            data, src = key.fileobj.recvfrom(65535)
            sink.write(record("udp", "in", src, key.fileobj.getsockname(), data))


def relay_udp(pairs, sink):
    """pairs = [(port local, (hôte surface, port))]."""
    sel = selectors.DefaultSelector()
    for lp, dev in pairs:
        front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        front.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        front.bind(("0.0.0.0", lp))
        back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        back.bind(("0.0.0.0", 0))
        st = {"front": front, "back": back, "dev": dev, "client": None}
        sel.register(front, selectors.EVENT_READ, ("front", st))
        sel.register(back, selectors.EVENT_READ, ("back", st))
        print("relay udp/%d <-> %s:%d" % (lp, dev[0], dev[1]), file=sys.stderr)
    while True:
        for key, _ in sel.select():
            side, st = key.data
            if side == "front":
                data, src = st["front"].recvfrom(65535)
                st["client"] = src
                sink.write(record("udp", "to_device", src, st["dev"], data))
                st["back"].sendto(data, st["dev"])
            else:
                data, src = st["back"].recvfrom(65535)
                sink.write(record("udp", "from_device", src, st["client"] or ("?", 0), data))
                if st["client"]:
                    st["front"].sendto(data, st["client"])


def relay_tcp(lp, dev, sink):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", lp))
    srv.listen(1)
    print("relay tcp/%d <-> %s:%d" % (lp, dev[0], dev[1]), file=sys.stderr)
    while True:
        cli, src = srv.accept()
        up = socket.create_connection(dev, timeout=5)
        up.settimeout(None)
        sel = selectors.DefaultSelector()
        sel.register(cli, selectors.EVENT_READ, "cli")
        sel.register(up, selectors.EVENT_READ, "up")
        alive = True
        while alive:
            for key, _ in sel.select():
                data = key.fileobj.recv(65535)
                if not data:
                    alive = False
                    break
                if key.data == "cli":
                    sink.write(record("tcp", "to_device", src, dev, data))
                    up.sendall(data)
                else:
                    sink.write(record("tcp", "from_device", dev, src, data))
                    cli.sendall(data)
        for s in (cli, up):
            try:
                s.close()
            except OSError:
                pass


def load(path):
    """Enregistrements d'une capture (lignes illisibles ignorées)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                r["data"] = bytes.fromhex(r.get("hex") or "")
            except (ValueError, TypeError):
                continue
            out.append(r)
    return out


def replay(records, skip=0, direction="all"):
    """Rejoue des enregistrements dans le décodeur du service : [(t, dir, événement | message brut)].
    Un parseur MIDI PAR SENS (le « running status » d'un sens ne vaut rien pour l'autre)."""
    from services.control_surface import mcu
    parsers, out = {}, []
    for r in records:
        if direction != "all" and r.get("dir") != direction:
            continue
        p = parsers.setdefault(r.get("dir"), mcu.MidiParser())
        for msg in p.feed(r["data"][skip:]):
            ev = mcu.decode(msg)
            out.append((r.get("t"), r.get("dir"), ev if ev is not None else {"type": "raw", "hex": msg.hex()}))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("listen-udp")
    a.add_argument("--port", type=int, action="append", required=True)
    a.add_argument("--out")
    a = sub.add_parser("relay-udp")
    a.add_argument("--listen", type=int, action="append", required=True)
    a.add_argument("--to", action="append", required=True)
    a.add_argument("--out")
    a = sub.add_parser("relay-tcp")
    a.add_argument("--listen", type=int, required=True)
    a.add_argument("--to", required=True)
    a.add_argument("--out")
    a = sub.add_parser("replay")
    a.add_argument("file")
    a.add_argument("--skip", type=int, default=0)
    a.add_argument("--dir", default="all", choices=("all", "from_device", "to_device", "in"))
    args = ap.parse_args(argv)
    if args.cmd == "replay":
        for t, d, ev in replay(load(args.file), args.skip, args.dir):
            print("%.6f %-12s %s" % (t or 0, d, json.dumps(ev, default=lambda b: b.hex())))
        return 0
    sink = Sink(args.out)
    try:
        if args.cmd == "listen-udp":
            listen_udp(args.port, sink)
        elif args.cmd == "relay-udp":
            if len(args.listen) != len(args.to):
                ap.error("one --to per --listen")
            relay_udp(list(zip(args.listen, (_hostport(x) for x in args.to))), sink)
        else:
            relay_tcp(args.listen, _hostport(args.to), sink)
    except KeyboardInterrupt:
        print("%d packet(s) recorded" % sink.n, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
