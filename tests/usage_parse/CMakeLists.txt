cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(usage_parse_test)

target_sources(app PRIVATE src/main.c ../../firmware/src/usage_parse.c)
target_include_directories(app PRIVATE ../../firmware/src)
