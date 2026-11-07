#pragma once
#include <stddef.h>
#include <stdint.h>
#include <zephyr/net/net_if.h>
struct net_mgmt_event_callback { const void *info; };
typedef void (*net_mgmt_event_handler_t)(struct net_mgmt_event_callback *cb,
					 uint64_t ev, struct net_if *iface);
static inline void net_mgmt_init_event_callback(struct net_mgmt_event_callback *c,
						net_mgmt_event_handler_t h,
						uint64_t mask)
{
	(void)c; (void)h; (void)mask;
}
static inline void net_mgmt_add_event_callback(struct net_mgmt_event_callback *c)
{
	(void)c;
}
int net_mgmt(uint64_t req, struct net_if *iface, void *buf, size_t len);
