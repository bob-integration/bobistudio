/* Oracle indépendant : construit UN élément ANC RFC 8331 avec les primitives de libmtl
 * (st40_set_udw / st40_add_parity_bits / st40_calc_checksum + struct st40_rfc8331_payload_hdr),
 * exactement comme st_tx_ancillary_session.c, et imprime les octets.
 * → à comparer bit à bit avec bobimxl.anc_pack_rfc8331(). */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <arpa/inet.h>
#include <mtl/st40_api.h>

int main(void) {
  /* Paquet de test : ATC DID/SDID 0x60, line 9, hori 0xFFF, 16 UDW (TC 10:22:33:12). */
  uint8_t udw[16];
  int b[8] = {0}; int f = 12, s = 33, m = 22, h = 10;
  b[0] = (f % 10) | ((f / 10) & 0x3) << 4;
  b[2] = (s % 10) | ((s / 10) & 0x7) << 4;
  b[4] = (m % 10) | ((m / 10) & 0x7) << 4;
  b[6] = (h % 10) | ((h / 10) & 0x3) << 4;
  for (int i = 0; i < 8; i++) { udw[i*2] = b[i] & 0x0f; udw[i*2+1] = (b[i] >> 4) & 0x0f; }
  uint16_t udw_size = 16;

  uint8_t buf[256]; memset(buf, 0, sizeof(buf));
  struct st40_rfc8331_payload_hdr* p = (struct st40_rfc8331_payload_hdr*)buf;

  p->first_hdr_chunk.c = 0;
  p->first_hdr_chunk.line_number = 9;
  p->first_hdr_chunk.horizontal_offset = 0xFFF;
  p->first_hdr_chunk.s = 0;
  p->first_hdr_chunk.stream_num = 0;
  p->second_hdr_chunk.did = st40_add_parity_bits(0x60);
  p->second_hdr_chunk.sdid = st40_add_parity_bits(0x60);
  p->second_hdr_chunk.data_count = st40_add_parity_bits(udw_size);
  p->swapped_first_hdr_chunk = htonl(p->swapped_first_hdr_chunk);
  p->swapped_second_hdr_chunk = htonl(p->swapped_second_hdr_chunk);

  /* UDW à partir de l'index 3 DANS second_hdr_chunk (0,1,2 = DID, SDID, DC) → bit 62. */
  for (uint16_t i = 0; i < udw_size; i++)
    st40_set_udw(i + 3, st40_add_parity_bits(udw[i]), (uint8_t*)&p->second_hdr_chunk);

  uint16_t cs = st40_calc_checksum(3 + udw_size, (uint8_t*)&p->second_hdr_chunk);
  st40_set_udw(udw_size + 3, cs, (uint8_t*)&p->second_hdr_chunk);

  /* Taille totale de l'élément : 4 o (1er chunk) + bits(10×(3+udw+1)) arrondis à 32 b. */
  uint16_t total_bits = (3 + udw_size + 1) * 10;
  uint16_t elem = 4 + ((total_bits + 31) / 32) * 4;

  printf("LIBMTL_ELEM_BYTES=%u\n", elem);
  printf("LIBMTL_HEX=");
  for (int i = 0; i < elem; i++) printf("%02x", buf[i]);
  printf("\n");
  return 0;
}
