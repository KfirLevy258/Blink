/* Host test for the pure OTA parsing logic.
 *   cc -I firmware/src tests/ota_parse/host_test.c firmware/src/ota_parse.c -o /tmp/ota && /tmp/ota
 */
#include <stdio.h>
#include <string.h>
#include "ota_parse.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

static void hdr_make(uint8_t *h, uint8_t ma, uint8_t mi, uint16_t rev)
{
	memset(h, 0, 64);
	h[0] = 0x3d; h[1] = 0xb8; h[2] = 0xf3; h[3] = 0x96;	/* 0x96f3b83d LE */
	h[20] = ma; h[21] = mi; h[22] = rev & 0xff; h[23] = rev >> 8;
}

int main(void)
{
	struct ota_manifest m;
	const char *good =
	  "{\"version\":\"0.4.0\",\"size\":1268532,"
	  "\"sha256\":\"aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899\"}";

	CK(ota_parse_manifest(good, strlen(good), &m) == 0, "manifest parses");
	CK(strcmp(m.version, "0.4.0") == 0, "version extracted");
	CK(m.size == 1268532, "size extracted");
	CK(strlen(m.sha256) == 64, "sha256 is 64 hex chars");
	CK(ota_parse_manifest("{\"version\":\"0.4.0\"}", 19, &m) < 0, "missing fields rejected");
	CK(ota_parse_manifest(good, 20, &m) < 0, "truncated rejected");
	const char *shortsha =
	  "{\"version\":\"0.4.0\",\"size\":5,\"sha256\":\"abc\"}";
	CK(ota_parse_manifest(shortsha, strlen(shortsha), &m) < 0, "short sha rejected");

	CK(ota_version_newer("0.4.0", "0.3.0"), "0.4.0 > 0.3.0");
	CK(ota_version_newer("1.0.0", "0.9.9"), "1.0.0 > 0.9.9");
	CK(ota_version_newer("0.3.10", "0.3.9"), "0.3.10 > 0.3.9 (numeric, not lexical)");
	CK(!ota_version_newer("0.3.0", "0.3.0"), "equal is not newer");
	CK(!ota_version_newer("0.2.9", "0.3.0"), "older is not newer");
	CK(!ota_version_newer("garbage", "0.3.0"), "unparseable is not newer");

	/* The MCUboot header on the first chunk, against running 1.4.0. */
	uint8_t h[64];

	hdr_make(h, 1, 4, 1);
	CK(ota_hdr_check(h, sizeof(h), "1.4.0", "1.4.1") == OTA_HDR_OK, "hdr_newer_ok");
	hdr_make(h, 1, 4, 0);
	CK(ota_hdr_check(h, sizeof(h), "1.4.0", "1.4.0") == OTA_HDR_NOT_NEWER, "hdr_same_refused");
	hdr_make(h, 1, 3, 9);
	CK(ota_hdr_check(h, sizeof(h), "1.4.0", "1.3.9") == OTA_HDR_NOT_NEWER, "hdr_older_refused");
	hdr_make(h, 1, 4, 2);
	CK(ota_hdr_check(h, sizeof(h), "1.4.0", "1.4.1") == OTA_HDR_NOT_OFFERED,
	   "hdr_newer_but_not_offered_refused");
	hdr_make(h, 1, 4, 1);
	h[0] ^= 1;
	CK(ota_hdr_check(h, sizeof(h), "1.4.0", "1.4.1") == OTA_HDR_BAD_MAGIC, "hdr_bad_magic_refused");
	hdr_make(h, 1, 4, 1);
	CK(ota_hdr_check(h, 27, "1.4.0", "1.4.1") == OTA_HDR_BAD_MAGIC, "hdr_short_refused");

	/* Bounds: in order, inside the image. */
	CK(ota_chunk_in_bounds(0, 0, 512), "chunk_first_in_bounds");
	CK(!ota_chunk_in_bounds(1024, 1536, 512), "gap_refused");
	CK(!ota_chunk_in_bounds(1024, 512, 512), "rewind_refused");
	CK(ota_chunk_in_bounds(OTA_IMAGE_MAX - 512, OTA_IMAGE_MAX - 512, 512), "chunk_ending_at_image_max_ok");
	CK(!ota_chunk_in_bounds(OTA_IMAGE_MAX - 512, OTA_IMAGE_MAX - 512, 513), "chunk_past_image_max_refused");
	CK(!ota_chunk_in_bounds(OTA_IMAGE_MAX, OTA_IMAGE_MAX, 1), "chunk_after_image_max_refused");

	const uint32_t size = 1000003;

	CK(ota_net_should_ack(16383, 16385, size) && !ota_net_should_ack(100, 600, size) &&
	   ota_net_should_ack(size - 7, size, size) && !ota_net_should_ack(5, 5, size),
	   "ack_at_boundary_and_at_end");
	CK(!ota_net_should_seal(size - 1, size) && ota_net_should_seal(size, size) &&
	   !ota_net_should_seal(0, 0), "partial_stream_not_sealed");

	CK(ota_resume_honoured(false, false, false) && ota_resume_honoured(true, true, true) &&
	   !ota_resume_honoured(true, false, true) && !ota_resume_honoured(true, true, false) &&
	   !ota_resume_honoured(true, false, false), "resume_over_wifi_only_after_a_tap");

	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
