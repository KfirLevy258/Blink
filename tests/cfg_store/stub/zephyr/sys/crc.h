/* Host stub: Zephyr's crc32_ieee is the zlib/IEEE 802.3 CRC-32 -- reflected,
 * polynomial 0xEDB88320, initial and final XOR 0xFFFFFFFF. */
#ifndef CFG_STUB_CRC_H
#define CFG_STUB_CRC_H
#include <stddef.h>
#include <stdint.h>

static inline uint32_t crc32_ieee(const uint8_t *data, size_t len)
{
	uint32_t crc = 0xFFFFFFFFu;

	for (size_t i = 0; i < len; i++) {
		crc ^= data[i];
		for (int k = 0; k < 8; k++) {
			crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
		}
	}
	return ~crc;
}
#endif
