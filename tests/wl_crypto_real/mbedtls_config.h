/* mbedTLS 3.6 configuration for the wl_crypto_real host test: exactly the
 * modules wl_crypto_mbedtls.c calls, the ones firmware/prj.conf turns on for
 * the board through Zephyr's Kconfig (spec S5.1's "Add for crypto" list).
 * Used through -DMBEDTLS_CONFIG_FILE by tests/ci/check_host_tests.sh, with
 * the library's own sources, so the test runs mbedTLS's code rather than a
 * copy of it. No MBEDTLS_PLATFORM_C: on the host md.c allocates with libc's
 * calloc, where the board's main.c points it at k_calloc. */
#ifndef WL_CRYPTO_REAL_MBEDTLS_CONFIG_H
#define WL_CRYPTO_REAL_MBEDTLS_CONFIG_H

#define MBEDTLS_CHACHA20_C
#define MBEDTLS_POLY1305_C
#define MBEDTLS_CHACHAPOLY_C
#define MBEDTLS_HKDF_C
#define MBEDTLS_MD_C
#define MBEDTLS_SHA256_C

#endif
