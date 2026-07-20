/*
 * OTA engine: resolve the latest GitHub release, stream the signed image
 * into slot1, verify its SHA-256, and hand it to MCUboot as a test image.
 *
 * GitHub answers /releases/latest/download/<asset> with a 302 to a
 * pre-signed CDN URL, so every fetch is two TLS legs with different trust
 * anchors. Fixed asset names keep the device's request path constant; the
 * manifest's sha256 pins the exact bytes, so a release changing between
 * check and install fails safe at the hash step -- and even a hash-passing
 * forgery still dies at MCUboot's ECDSA-P256 check. Runs entirely on the
 * net-worker thread; the UI only ever touches the snapshot + request flags.
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/net/http/client.h>
#include <zephyr/dfu/flash_img.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/printk.h>
#include <mbedtls/sha256.h>
#include <string.h>
#include <stdio.h>

#include "ota.h"
#include "certs.h"
#include "version.h"

#define CA_TAG_GITHUB 3
#define CA_TAG_GH_CDN 4
#define OTA_HOST "github.com"
#define OTA_BASE "/KfirLevy258/clauge-releases/releases/latest/download/"
/* Fixed asset names; the manifest's sha256 pins the exact bytes, so a release
 * changing between check and install fails safe at the hash step. */
#define OTA_MANIFEST_PATH OTA_BASE "manifest.json"
#define OTA_IMAGE_PATH    OTA_BASE "clauge-fw.bin"

static uint8_t recv_buf[1536];

/* --- redirect capture: GitHub answers latest/download with a 302 --- */
static char redirect_url[768];
static char cdn_host[64];
static char cdn_path[768];	/* pre-signed CDN URLs carry a long token */
static char small_body[512];	/* manifest.json is ~120 bytes */
static size_t small_len;
static int http_status;

/* --- install-leg streaming state (single worker thread; no locking) --- */
static struct flash_img_context fictx;
static mbedtls_sha256_context sha;
static uint32_t dl_total, dl_got;
static int stream_err;

/* --- UI snapshot + request flags --- */
static struct ota_ui ui;
static struct k_spinlock ui_lock;
static atomic_t req_check;
static atomic_t req_install;
static atomic_t badge;
static struct ota_manifest last_m;	/* last successful check; worker only */

static void capture_headers(struct http_response *rsp)
{
	http_status = rsp->http_status_code;

	if ((http_status == 301 || http_status == 302) && !redirect_url[0]) {
		/* Scan the raw buffer: Zephyr's client exposes no per-header
		 * hook, and the whole 302 head fits one 1536-byte fragment. */
		const char *p = rsp->recv_buf;
		size_t n = rsp->data_len;

		for (size_t i = 0; i + 10 < n; i++) {
			if (strncasecmp(&p[i], "location:", 9) != 0) {
				continue;
			}
			i += 9;
			while (i < n && p[i] == ' ') {
				i++;
			}
			size_t o = 0;

			while (i < n && p[i] != '\r' && p[i] != '\n' &&
			       o < sizeof(redirect_url) - 1) {
				redirect_url[o++] = p[i++];
			}
			redirect_url[o] = '\0';
			break;
		}
	}
}

static void capture_cb(struct http_response *rsp, enum http_final_call final,
		       void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	capture_headers(rsp);
	if (rsp->body_frag_start && rsp->body_frag_len) {
		size_t n = rsp->body_frag_len;

		if (small_len + n > sizeof(small_body) - 1) {
			n = sizeof(small_body) - 1 - small_len;
		}
		if (n > 0) {
			memcpy(small_body + small_len, rsp->body_frag_start, n);
			small_len += n;
		}
	}
}

static void stream_cb(struct http_response *rsp, enum http_final_call final,
		      void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	capture_headers(rsp);
	if (http_status != 200 || stream_err ||
	    !rsp->body_frag_start || !rsp->body_frag_len) {
		return;
	}

	mbedtls_sha256_update(&sha, rsp->body_frag_start, rsp->body_frag_len);
	stream_err = flash_img_buffered_write(&fictx,
					      rsp->body_frag_start,
					      rsp->body_frag_len, false);
	if (stream_err) {
		printk("[ota] slot1 write failed: %d\n", stream_err);
		return;
	}
	dl_got += rsp->body_frag_len;
	if (dl_total) {
		uint8_t pct = (uint8_t)((uint64_t)dl_got * 100 / dl_total);
		static uint8_t last_pct = 255;

		if (pct != last_pct) {
			last_pct = pct;
			ota_ui_set(OTA_UI_DOWNLOADING, NULL, pct);
		}
	}
}

/* One TLS GET. body_cb==NULL captures into small_body. Returns http status
 * or negative errno. Caller passes the right CA tag per host. */
static int https_get(const char *host, const char *path, sec_tag_t tag,
		     http_response_cb_t body_cb, int32_t timeout_ms)
{
	static bool creds_added;

	if (!creds_added) {
		tls_credential_add(CA_TAG_GITHUB, TLS_CREDENTIAL_CA_CERTIFICATE,
				   ca_cert_github, sizeof(ca_cert_github));
		tls_credential_add(CA_TAG_GH_CDN, TLS_CREDENTIAL_CA_CERTIFICATE,
				   ca_cert_gh_cdn, sizeof(ca_cert_gh_cdn));
		creds_added = true;
	}

	small_len = 0;
	http_status = 0;
	redirect_url[0] = '\0';

	struct zsock_addrinfo hints = {
		.ai_family = AF_INET, .ai_socktype = SOCK_STREAM,
	};
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(host, "443", &hints, &res) != 0 || !res) {
		return -EIO;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype,
				IPPROTO_TLS_1_2);

	if (sock < 0) {
		zsock_freeaddrinfo(res);
		return -EIO;
	}

	sec_tag_t tags[] = { tag };

	zsock_setsockopt(sock, SOL_TLS, TLS_SEC_TAG_LIST, tags, sizeof(tags));
	zsock_setsockopt(sock, SOL_TLS, TLS_HOSTNAME, host, strlen(host) + 1);

	int rc = zsock_connect(sock, res->ai_addr, res->ai_addrlen);

	zsock_freeaddrinfo(res);
	if (rc < 0) {
		zsock_close(sock);
		return -EIO;
	}

	const char *headers[] = {
		"User-Agent: clauge/" CLAUGE_FW_VERSION "\r\n",
		NULL,
	};

	struct http_request req = {
		.method = HTTP_GET,
		.url = path,
		.host = host,
		.protocol = "HTTP/1.1",
		.header_fields = headers,
		.response = body_cb ? body_cb : capture_cb,
		.recv_buf = recv_buf,
		.recv_buf_len = sizeof(recv_buf),
	};

	rc = http_client_req(sock, &req, timeout_ms, NULL);
	zsock_close(sock);
	if (rc < 0) {
		return rc;
	}
	return http_status;
}

/* Follow github.com's 302 for `path` and leave cdn_host/cdn_path pointing at
 * the pre-signed CDN URL. Returns OTA_OK or an error. */
static enum ota_result resolve_redirect(const char *path)
{
	int st = https_get(OTA_HOST, path, CA_TAG_GITHUB, NULL, 15000);

	if (st < 0) {
		return OTA_ERR_NET;
	}
	if (st != 301 && st != 302) {
		printk("[ota] %s: status %d, expected redirect\n", path, st);
		return OTA_ERR_HTTP;
	}
	if (!redirect_url[0] ||
	    ota_split_url(redirect_url, cdn_host, sizeof(cdn_host),
			  cdn_path, sizeof(cdn_path)) != 0) {
		return OTA_ERR_PARSE;
	}
	return OTA_OK;
}

enum ota_result ota_check(struct ota_manifest *out, bool *newer)
{
	*newer = false;

	enum ota_result r = resolve_redirect(OTA_MANIFEST_PATH);

	if (r != OTA_OK) {
		return r;
	}

	int st = https_get(cdn_host, cdn_path, CA_TAG_GH_CDN, NULL, 15000);

	if (st < 0) {
		return OTA_ERR_NET;
	}
	if (st != 200) {
		printk("[ota] manifest fetch: status %d\n", st);
		return OTA_ERR_HTTP;
	}
	if (ota_parse_manifest(small_body, small_len, out) != 0) {
		return OTA_ERR_PARSE;
	}
	if (out->size > FIXED_PARTITION_SIZE(slot1_partition)) {
		printk("[ota] image %u exceeds slot1\n", out->size);
		return OTA_ERR_SIZE;
	}
	last_m = *out;
	*newer = ota_version_newer(out->version, CLAUGE_FW_VERSION);
	printk("[ota] latest %s (running %s) -> %s\n", out->version,
	       CLAUGE_FW_VERSION, *newer ? "update available" : "up to date");
	return OTA_OK;
}

void ota_last_manifest(struct ota_manifest *out)
{
	*out = last_m;
}

enum ota_result ota_install(const struct ota_manifest *m)
{
	enum ota_result r = resolve_redirect(OTA_IMAGE_PATH);

	if (r != OTA_OK) {
		return r;
	}

	if (flash_img_init(&fictx) != 0) {
		return OTA_ERR_FLASH;
	}
	mbedtls_sha256_init(&sha);
	mbedtls_sha256_starts(&sha, 0);
	dl_total = m->size;
	dl_got = 0;
	stream_err = 0;

	printk("[ota] downloading %s (%u bytes)\n", m->version, m->size);

	int st = https_get(cdn_host, cdn_path, CA_TAG_GH_CDN, stream_cb,
			   300000);

	if (st < 0) {
		mbedtls_sha256_free(&sha);
		return OTA_ERR_NET;
	}
	if (st != 200) {
		mbedtls_sha256_free(&sha);
		printk("[ota] image fetch: status %d\n", st);
		return OTA_ERR_HTTP;
	}
	if (stream_err) {
		mbedtls_sha256_free(&sha);
		return OTA_ERR_FLASH;
	}
	if (flash_img_buffered_write(&fictx, NULL, 0, true) != 0) {
		mbedtls_sha256_free(&sha);
		return OTA_ERR_FLASH;
	}
	if (flash_img_bytes_written(&fictx) != m->size) {
		mbedtls_sha256_free(&sha);
		printk("[ota] truncated: %zu of %u bytes\n",
		       flash_img_bytes_written(&fictx), m->size);
		return OTA_ERR_SIZE;
	}

	uint8_t digest[32];
	char hex[65];

	mbedtls_sha256_finish(&sha, digest);
	mbedtls_sha256_free(&sha);
	for (int i = 0; i < 32; i++) {
		snprintf(&hex[i * 2], 3, "%02x", digest[i]);
	}
	if (strcmp(hex, m->sha256) != 0) {
		printk("[ota] sha256 mismatch\n");
		return OTA_ERR_HASH;
	}

	/* Slot1 now holds verified bytes; hand it to MCUboot as a test
	 * image. Anything failing before this line leaves slot1 garbage,
	 * which is harmless -- it is never marked pending. */
	if (boot_request_upgrade(BOOT_UPGRADE_TEST) != 0) {
		return OTA_ERR_FLASH;
	}
	printk("[ota] %s verified and marked pending\n", m->version);
	return OTA_OK;
}

/* --- UI <-> worker handshake --- */

void ota_ui_get(struct ota_ui *out)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	*out = ui;
	k_spin_unlock(&ui_lock, key);
}

void ota_ui_set(enum ota_ui_state st, const struct ota_manifest *m,
		uint8_t pct)
{
	k_spinlock_key_t key = k_spin_lock(&ui_lock);

	ui.st = st;
	if (m) {
		strncpy(ui.version, m->version, sizeof(ui.version) - 1);
		ui.version[sizeof(ui.version) - 1] = '\0';
		ui.size = m->size;
	}
	ui.pct = pct;
	k_spin_unlock(&ui_lock, key);

	if (st == OTA_UI_AVAILABLE) {
		atomic_set(&badge, 1);
	} else if (st == OTA_UI_UP_TO_DATE || st == OTA_UI_REBOOTING) {
		atomic_set(&badge, 0);
	}
}

void ota_request_check(void)
{
	atomic_set(&req_check, 1);
}

void ota_request_install(void)
{
	atomic_set(&badge, 0);
	atomic_set(&req_install, 1);
}

bool ota_take_check_request(void)
{
	return atomic_set(&req_check, 0) != 0;
}

bool ota_take_install_request(void)
{
	return atomic_set(&req_install, 0) != 0;
}

bool ota_badge(void)
{
	return atomic_get(&badge) != 0;
}
