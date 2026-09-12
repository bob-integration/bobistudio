#!/usr/bin/env python3
"""Producteur AUDIO + ANC du banc croisé interop — à lancer dans un conteneur bobi-compute.

Publie, pendant DURATION s (noms préfixés v210xbench_, jamais de la prod) :
  - v210xbench_audio : audio/float32, 8 canaux 48 kHz (sinus repérable par canal)
  - v210xbench_anc   : video/smpte291, payload = NOTRE sérialisation MAISON
                       [u32 meta_num][u32 udw_fill][meta×16][udw]  (≠ RFC 8331)

Le consommateur STOCK doit ensuite :
  - LIRE l'audio correctement (l'audit annonce « conforme byte-identique ») ;
  - MAL INTERPRÉTER l'ANC (il attend du RFC 8331 dès le champ Length) → corruption silencieuse.
"""
import struct, time
import numpy as np
import bobimxl as bx

SR, CH, DURATION = 48000, 8, 90
AUDIO, ANC = "v210xbench_audio", "v210xbench_anc"

inst = bx.Instance()
wa = bx.AudioWriter(inst, AUDIO, channels=CH, sample_rate=SR, index_mode="free")
wd = bx.Writer(inst, ANC, 0, 0, flow_def=bx.build_data_flow_def(ANC, 25, 1))

print("AUDIO_FLOWID=%s" % bx.flow_id(AUDIO), flush=True)
print("ANC_FLOWID=%s" % bx.flow_id(ANC), flush=True)
print("READY", flush=True)


def anc_payload_maison(frame):
    """NOTRE format ANC (miroir de _atc_slot / mtl_rx.c / recorder) — un paquet ATC S12M.
    [u32 meta_num][u32 udw_fill][meta×16 : did,sdid,udw_size,line,offset…][udw]"""
    udw = bytes(range(16))                         # charge utile bidon, repérable
    meta = struct.pack("<HHHHHHHH", 0x60, 0x60, len(udw), 9, 0, 0, 0, 0)  # 16 o
    return struct.pack("<II", 1, len(udw)) + meta + udw


t0 = time.time()
k = 0
n = SR // 100                                       # blocs de 10 ms
t = np.arange(n, dtype=np.float32) / SR
while time.time() - t0 < DURATION:
    # Audio : un sinus par canal (440 Hz × (ch+1)) → valeurs vérifiables côté stock.
    blk = np.stack([np.sin(2 * np.pi * 440 * (c + 1) * (t + k * n / SR)) * 0.5
                    for c in range(CH)], axis=1).astype(np.float32)
    wa.write(blk)
    if k % 4 == 0:                                  # ANC à 25 fps
        wd.write(np.frombuffer(anc_payload_maison(k), dtype=np.uint8))
    k += 1
    time.sleep(max(0, (k * n / SR) - (time.time() - t0)))

print("DONE %d blocs" % k, flush=True)
wa.close(); wd.close()
inst.garbage_collect(); inst.close()
