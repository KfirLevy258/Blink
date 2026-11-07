#pragma once
#include <stddef.h>
#include <stdint.h>
#define NET_REQUEST_WIFI_CONNECT 1ULL
#define NET_REQUEST_WIFI_DISCONNECT 2ULL
#define NET_REQUEST_WIFI_SCAN 3ULL
#define NET_REQUEST_WIFI_IFACE_STATUS 4ULL
#define WIFI_CHANNEL_ANY 255
#define WIFI_FREQ_BAND_2_4_GHZ 0
#define WIFI_MFP_OPTIONAL 1
enum wifi_security_type {
	WIFI_SECURITY_TYPE_NONE, WIFI_SECURITY_TYPE_PSK,
	WIFI_SECURITY_TYPE_SAE, WIFI_SECURITY_TYPE_WPA_PSK,
	WIFI_SECURITY_TYPE_UNKNOWN, WIFI_SECURITY_TYPE_WEP
};
enum wifi_iface_state {
	WIFI_STATE_DISCONNECTED, WIFI_STATE_INTERFACE_DISABLED,
	WIFI_STATE_INACTIVE, WIFI_STATE_SCANNING, WIFI_STATE_AUTHENTICATING,
	WIFI_STATE_ASSOCIATING, WIFI_STATE_ASSOCIATED,
	WIFI_STATE_4WAY_HANDSHAKE, WIFI_STATE_GROUP_HANDSHAKE,
	WIFI_STATE_COMPLETED
};
struct wifi_connect_req_params {
	const uint8_t *ssid; size_t ssid_length;
	const uint8_t *psk; size_t psk_length;
	int security, channel, band, mfp, timeout;
};
struct wifi_iface_status { enum wifi_iface_state state; int rssi; };
struct wifi_status { int status; };
struct wifi_scan_result {
	uint8_t ssid[32]; uint8_t ssid_length; int8_t rssi;
	enum wifi_security_type security;
};
