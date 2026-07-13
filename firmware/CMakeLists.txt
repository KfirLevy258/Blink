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
	src/net_wifi.c)
