#!/usr/bin/env python3
"""Producteur ANC RFC 8331 du banc croisé — à lancer dans un conteneur bobi-compute.

Publie pendant DURATION s un flux DATA `v210xbench_anc8331` dont le grain est encodé par
`bobimxl.anc_pack_rfc8331` : un paquet ATC (DID/SDID 0x60, timecode 10:22:33:12) + un paquet
« données » arbitraire (DID 0x41/SDID 0x05, 30 UDW, stream_num=7).

ORACLE : le `mxl-data-probe` STOCK (parseur RFC 8331, non patché) doit annoncer
« ANC count: 2 » et restituer DID/SDID/Data_Count exacts. C'est LUI qui valide notre encodeur —
pas nos propres tests.
"""
import time
import bobimxl as bx

DURATION = 90
NAME = "v210xbench_anc8331"

inst = bx.Instance()
wd = bx.Writer(inst, NAME, 0, 0, flow_def=bx.build_data_flow_def(NAME, 25, 1))

print("ANC8331_FLOWID=%s" % bx.flow_id(NAME), flush=True)
print("READY", flush=True)

t0 = time.time()
k = 0
while time.time() - t0 < DURATION:
    s = int(time.time() - t0)
    packets = [
        {"did": 0x60, "sdid": 0x60, "line": 9, "hori": 0xFFF,
         "udw": bx.anc_atc_encode(10, 22, 33, k % 25)},
        {"did": 0x41, "sdid": 0x05, "line": 17, "hori": 100, "c": 1, "s": 1,
         "stream_num": 7, "udw": bytes(range(30))},
    ]
    wd.write(bx.anc_pack_rfc8331(packets, grain_size=4096), index=k)
    k += 1
    time.sleep(max(0, (k / 25.0) - (time.time() - t0)))

print("DONE %d grains" % k, flush=True)
wd.close()
inst.garbage_collect(); inst.close()
