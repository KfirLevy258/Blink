cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(claude_usage_display)

# src/ on the global include path so the mbedTLS build can find
# src/mbedtls_user_config.h (CONFIG_MBEDTLS_USER_CONFIG_FILE).
zephyr_include_directories(src)

target_sources(app PRIVATE
	src/main.c
	src/proto.c
	src/msg_parse.c
	src/usage_view.c
	src/fmt.c
	src/cfg_store.c
	src/net_wifi.c
	src/portal.c
	src/ui_setup.c
	src/dns_hijack.c
	src/oauth.c
	src/usage_parse.c
	src/usage_client.c
	src/net_time.c
	src/tz_fetch.c
	src/ui_boot.c
	src/bootanim_dec.c
	src/ui_settings.c
	src/ui_anim.c
	src/ui_slide.c
	src/backlight.c
	src/ota_parse.c
	src/ota.c
	src/ui_touchfx.c
)

# Diagnostic only; CONFIG_CLAUGE_TOUCH_TRACE is off in release builds.
target_sources_ifdef(CONFIG_CLAUGE_TOUCH_TRACE app PRIVATE src/touch_trace.c)

