#pragma once
#include <stddef.h>
static inline void mbedtls_platform_zeroize(void *p, size_t n)
{
	volatile unsigned char *v = p;

	while (n--) {
		*v++ = 0;
	}
}
