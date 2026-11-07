#pragma once
#include <stdbool.h>
#include <zephyr/net/net_ip.h>
struct net_if { int admin_up; };
struct net_if *net_if_get_first_wifi(void);
void net_if_up(struct net_if *i);
void net_if_down(struct net_if *i);
bool net_if_is_admin_up(struct net_if *i);
struct in_addr *net_if_ipv4_get_global_addr(struct net_if *i, int state);
const char *net_addr_ntop(int af, const void *src, char *dst, size_t size);
