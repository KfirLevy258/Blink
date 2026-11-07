#pragma once
#include <stdint.h>
#define AF_INET 2
#define INADDR_ANY 0u
#define htons(x) ((uint16_t)(x))
#define htonl(x) ((uint32_t)(x))
typedef unsigned int socklen_t;
struct in_addr_s { uint32_t s_addr; };
struct sockaddr { int sa_family; char sa_data[14]; };
struct sockaddr_in {
	int sin_family;
	uint16_t sin_port;
	struct in_addr_s sin_addr;
};
