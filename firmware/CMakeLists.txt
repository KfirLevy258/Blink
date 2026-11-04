cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(claude_usage_display)

target_sources(app PRIVATE
	src/main.c
	src/proto.c
	src/msg_parse.c
	src/usage_view.c
	src/fmt.c
	src/cfg_store.c
	src/net_time.c
	src/ui_boot.c
	src/bootanim_dec.c
	src/bootclip.c
	src/ui_sleep.c
	src/sleep_gate.c
	src/upd_prompt.c
	src/upd_row.c
	src/upd_tap.c
	src/whatsnew.c
	src/logo_parse.c
	src/logo.c
	src/ui_settings.c
	src/edit_hold.c
	src/settings_icons.c
	src/ui_anim.c
	src/ui_slide.c
	src/ui_swipe.c
	src/backlight.c
	src/ota_parse.c
	src/ota.c
	src/ui_touchfx.c
	src/usage_state.c
	src/usage_freshness.c
	src/fatal.c
	src/fatal_policy.c
	src/svc_state.c
	src/theme.c
	src/font_focus_40.c
	src/ui_theme.c
	src/burn5h.c
)

# Diagnostic only; CONFIG_BLINK_TOUCH_TRACE is off in release builds.
target_sources_ifdef(CONFIG_BLINK_TOUCH_TRACE app PRIVATE src/touch_trace.c)

# Bench only; CONFIG_BLINK_TOUCH_INJECT is off in every release build, and
# tools/lib_selftest.sh refuses an image that has it.
target_sources_ifdef(CONFIG_BLINK_TOUCH_INJECT app PRIVATE src/touch_inject.c
		     src/touch_widgets.c)


# The [mem] lines (src/mem_stats.c), on whenever prj.conf keeps the heap
# counters on. It reads the LVGL pool's free lists through Zephyr's private
# lib/heap/heap.h, so ZEPHYR_BASE is on the path for this one file only and
# the file includes "lib/heap/heap.h" by that full name.
target_sources_ifdef(CONFIG_SYS_HEAP_RUNTIME_STATS app PRIVATE src/mem_stats.c)
set_source_files_properties(src/mem_stats.c PROPERTIES
	INCLUDE_DIRECTORIES ${ZEPHYR_BASE})
