#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/wifi_mgmt.h>

#include "net_wifi.h"
#include "secrets.h"

LOG_MODULE_REGISTER(net_wifi, LOG_LEVEL_INF);

static K_SEM_DEFINE(ip_sem, 0, 1);
static struct net_mgmt_event_callback ipv4_cb;

static void ipv4_handler(struct net_mgmt_event_callback *cb,
			 uint32_t event, struct net_if *iface)
{
	ARG_UNUSED(cb);
	ARG_UNUSED(iface);

	if (event == NET_EVENT_IPV4_ADDR_ADD) {
		k_sem_give(&ip_sem);
	}
}

int net_wifi_connect_blocking(int timeout_seconds)
{
	struct net_if *iface = net_if_get_first_wifi();

	if (iface == NULL) {
		LOG_ERR("no WiFi interface");
		return -ENODEV;
	}

	net_mgmt_init_event_callback(&ipv4_cb, ipv4_handler,
				     NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);

	struct wifi_connect_req_params params = {
		.ssid = (const uint8_t *)WIFI_SSID,
		.ssid_length = sizeof(WIFI_SSID) - 1,
		.psk = (const uint8_t *)WIFI_PSK,
		.psk_length = sizeof(WIFI_PSK) - 1,
		.security = WIFI_SECURITY_TYPE_PSK,
		.channel = WIFI_CHANNEL_ANY,
		.band = WIFI_FREQ_BAND_2_4_GHZ,
	};

	LOG_INF("connecting to SSID '%s'...", WIFI_SSID);
	int ret = net_mgmt(NET_REQUEST_WIFI_CONNECT, iface,
			   &params, sizeof(params));
	if (ret) {
		LOG_ERR("connect request failed: %d", ret);
		return ret;
	}

	if (k_sem_take(&ip_sem, K_SECONDS(timeout_seconds)) != 0) {
		LOG_ERR("timed out waiting for IPv4 lease");
		return -ETIMEDOUT;
	}

	LOG_INF("WiFi connected, IPv4 lease acquired");
	return 0;
}

bool net_wifi_has_ip(void)
{
	struct net_if *iface = net_if_get_first_wifi();

	if (iface == NULL) {
		return false;
	}

	return net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED) != NULL;
}
