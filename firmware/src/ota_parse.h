#ifndef OTA_PARSE_H
#define OTA_PARSE_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/* Pure parsing/compare helpers for the OTA client. No Zephyr dependencies:
 * host-tested in tests/ota_parse/. */

struct ota_manifest {
	char version[16];	/* "0.4.0" */
	uint32_t size;		/* bytes of clauge-fw.bin */
	char sha256[65];	/* lowercase hex + NUL */
};

int ota_parse_manifest(const char *buf, size_t len, struct ota_manifest *out);
bool ota_version_newer(const char *cand, const char *cur);
int ota_split_url(const char *url, char *host, size_t hlen,
		  char *path, size_t plen);

#endif /* OTA_PARSE_H */
