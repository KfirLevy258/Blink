#ifndef MBEDTLS_USER_CONFIG_H
#define MBEDTLS_USER_CONFIG_H

/*
 * Appended to Zephyr's generated mbedTLS config (CONFIG_MBEDTLS_USER_CONFIG_FILE)
 * for symbols Zephyr's Kconfig cannot express.
 *
 * Zephyr ties BOTH record buffers to one knob:
 *   config-mbedtls.h:457  MBEDTLS_SSL_IN_CONTENT_LEN  = CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN
 *   config-mbedtls.h:458  MBEDTLS_SSL_OUT_CONTENT_LEN = CONFIG_MBEDTLS_SSL_MAX_CONTENT_LEN
 * so making the inbound buffer big enough for the OTA download would double
 * the cost for no reason -- we only ever SEND short GETs, but we must be able
 * to RECEIVE whatever record size the peer chooses.
 *
 * GitHub's release CDN starts at ~1.4 KB records and ramps to the 16384-byte
 * maximum once a transfer passes ~32 KB (measured on hardware 2026-07-25: 76
 * such records in the 1.28 MB image). Anything smaller than 16384 here makes
 * mbedtls_ssl_fetch_input() reject them with "requesting more data than fits"
 * (-0x7100), which is why the OTA download had never once completed. The peer
 * cannot be asked to send less: RFC 8449 record_size_limit is TLS-1.3-only in
 * mbedTLS 3.6, and TLS 1.3 drags in the PSA crypto stack.
 *
 * 16384 is the TLS protocol maximum, so this is a bound, not a guess.
 */
#undef MBEDTLS_SSL_IN_CONTENT_LEN
#define MBEDTLS_SSL_IN_CONTENT_LEN 16384

/* Our largest outbound message is a GET with a ~905-char redirect URL plus a
 * handful of headers, and the ClientHello. 2 KB is ample. */
#undef MBEDTLS_SSL_OUT_CONTENT_LEN
#define MBEDTLS_SSL_OUT_CONTENT_LEN 2048

#endif /* MBEDTLS_USER_CONFIG_H */
