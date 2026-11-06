# What the radio, a listener, the handshake and an OTA stream cost on the CYD

Task 0.2 of the WiFi transport plan, measured 2026-11-06 on the owner's desk
board (ESP32 CYD, `FLASH_CRYPT_CNT = 0`, plaintext), Zephyr 4.3.1, SDK
0.17.4. This is the record spec section 11 asks for, so that a later
regression is compared with the gate instead of re-derived. It holds no
network name, no address and no key.

The spike is on the local branch `spike/wifi-ram` (never pushed, never
merged):

| Commit | What |
|---|---|
| `a3a4668` | `wl_hs`, `wl_frame` and the PC peer |
| `b7c6161` | the network thread, the Connection tile; the **unpaired** row |
| `0ebd3b3` | credentials reach the image, `NET_MAX_CONN=6`, `ZVFS_POLL_MAX=4`; the **paired** row |
| `6134049` | lever 1: `LOG_PROCESS_THREAD_STACK_SIZE=1536` |
| `60cca12` | lever 2: `SYSTEM_WORKQUEUE_STACK_SIZE=1536` |
| `9062814` | a `sys_heap` listener: the largest chunk at the low-water instant; the pool's own peak |
| `9f39600` | the peer lets the board drain an `O` stream before closing |

Baselines are this branch's tree at `1f481ce` (the firmware has not changed
since). The board ran the same workload in every spike row: boot and join,
a scan, a WiFi session at two frames a second (a welcome, then invented
usage, time and pong lines built by `pc.protocol`), `session_touch.txt`
(`--touch-only --skip-missing`) and `popups_touch.txt` during that
session, a replacement session, a 1.3 MB `O` stream with
`download_touch.txt` (`--touch-only --loop 30`) for the paired row, then
down and up. The paired row's session ran ten minutes; the lever rows'
eight.

## Link map and image (app, bytes)

Regions: FLASH 4194176, iram0 229376, dram0 196608, dram1 98304.

| Build | FLASH | iram0 | dram0 used / free | dram1 used / free | signed image |
|---|---|---|---|---|---|
| Baseline injector | 697108 | 41216 | 128424 / 68184 | 57604 / 40700 | 697386 |
| Baseline plain (`burn.sh --edition claude`) | 692160 | 40960 | 123768 / 72840 | 57604 / 40700 | 692438 |
| Baseline plain + thread analyzer | 692536 | 41984 | 124208 / 72400 | 58628 / 39676 | 692815 |
| Spike unpaired (`b7c6161`, injector) | 1169324 | 63488 | 165928 / 30680 | 85004 / 13300 | 1169603 |
| Spike paired (`0ebd3b3`, injector) | 1169324 | 63488 | 166016 / 30592 | 85004 / 13300 | 1169602 |
| + lever 1 (`6134049`) | 1169324 | 63488 | 166016 / 30592 | 85516 / 12788 | 1169602 |
| + lever 2 (`60cca12`) | 1169324 | 63488 | 166016 / 30592 | 86028 / 12276 | 1169604 |
| + heap listener (`9062814`, measurement only) | 1169916 | 63488 | 166088 / 30520 | 86028 / 12276 | 1170196 |

The injector costs 4948 B of flash, 256 of iram0 and 4656 of dram0 and
nothing in dram1, so a plain spike image is about 1164655 B signed with
about 35.2 KB of dram0 free. Paired and unpaired are the same code; the
credentials only change string literals. The 88 B between the two spike
rows is the connection and poll limits.

Swap-move usable size, from the spike's MCUboot build (identical `.config`
in both rows): `BOOT_MAX_IMG_SECTORS=512`, `MCUBOOT_BOOT_MAX_ALIGN=32`,
`BOOT_WRITE_SZ=32`, `boot_trailer_sz(32)=49312` (compiled from MCUboot's
own `bootutil_area.c` with that build's command), so `app_max_size` =
1376256 - 53248 - 4096 = **1318912**, section 9's number.

## System heap (`HEAP_MEM_POOL_SIZE=86016`, 85920 usable)

`lowfree` is the heap's size less its high-water mark since boot, and is
exact. `largest` in the first rows is the largest free chunk **sampled
every 10 s**, which is not the largest chunk at the low-water point: the
low-water happens between samples. The first version of this record gave
51852 as "the largest chunk at the low-water point"; that was a sample
taken while free was 53656, and at the low-water instant free was 38488,
so the real figure was at most 38488 and unknown. The instrumented row
below (``9062814``, a `sys_heap` listener that reads free and the largest
chunk inside the allocation that sets each new maximum) is the
measurement.

| Point | free | largest | low-water free |
|---|---|---|---|
| Unpaired (driver initialised, interface down) | 56400 | 56364 | 56312 |
| Paired: after boot and join | 54024 | 53988 | 50616 |
| after a scan while joined | 53984 | 53884 | 50368 |
| during the session and session_touch | 53984 | 53884 | 45104 |
| after popups_touch with the session live | 53984 | 53884 | **38488** |
| during the 1.3 MB stream (sampled, not at the low-water) | 53656 | 51852 | 38488 |
| after the replacement, the stream and 30 loops | 53984 | 53884 | 38488 |
| Lever 1 run, whole workload | | 51980 (min) | 41744 |
| Lever 2 run, whole workload (sampled) | | 53796 (min) | **38120** |
| **Instrumented lever 2, same workload: at the low-water instant** | **40336** | **40252** | 40336 |
| **Instrumented, a fresh boot: 1 min session, replacement, the stream with download_touch: at the low-water instant** | **38528** | **38508** | 38528 |

On the instrumented run the low-water was set during popups_touch with the
session live, by a 1704 B allocation on the WiFi driver's own thread
(`wifi`), and at that instant the largest free chunk was 40252 of 40336
free: the heap was not fragmented, the transient simply took one end of
it. Successive maxima were set by the same thread: 1704 B at join,
128 B during session_touch, 1704 B again during popups_touch; in that
boot the stream, the replacement and down/up did not lower it. In a second
boot that ran the stream after only a minute of session, the stream set
the low-water instead: 38528 free, largest 38508, again a `wifi`-thread
allocation (128 B). Either way the largest chunk at the low-water instant
is within 100 B of the free total. So the roughly 15 KB
transient is the driver's: 1704 B is the size of an ESP32 WiFi RX buffer
allocation (`ESP32_WIFI_DYNAMIC_RX_BUFFER_NUM=32`), and a burst of traffic
while the main thread is busy drawing popups holds several of them at
once (nine would be 15336 B); that the others were RX buffers too is
inferred, not traced. The largest single allocation from the system heap
in the whole run was 3592 B, by `main`. The lever 2 run's 38120 was not
instrumented; with the heap unfragmented at its low-water the largest
chunk there would be near 38 KB, and is at most 38120.

No Zephyr error or warning line, and so no driver allocation failure,
appeared in any capture of the paired or lever runs; every touch run
counted zero LVGL out-of-memory lines and zero input-queue overflows.

## LVGL pool (32768, 32680 usable)

| Run | worst screen | peak | left |
|---|---|---|---|
| Baseline injector, popups_touch (stops at line 174, see below) | confirm-close | 28128 | 4552 |
| Spike unpaired, popups_touch (same stop) | confirm-close | 29168 | 3512 |
| Spike paired, popups_touch with the session live (to line 255) | confirm-close | 29280 | **3400** |
| Spike paired, session_touch during the session | forget-close | 28720 | 3960 |
| Spike paired, download_touch during the stream | no `[mem]` line (see below) | 23896 used | |
| Instrumented lever 2, the stream with download_touch (pool peak) | none (gauges) | 24776 | 7904 |
| Lever 1, popups_touch with the session live | confirm-close | 29280 | 3400 |
| Lever 2, popups_touch with the session live | confirm-close | 29280 | 3400 |

popups_touch stopped at line 174 on 2026-11-06 morning because the board's
theme (custom, id 12) opens the theme screen on page 3 and the script
expects page 1. For the paired runs the theme was switched to Dark (id 0)
first and set back to custom afterwards; the run then reached line 256,
`fake boot_notice`, the first step that only the host harness can play.
Everything a board can run was run.

The spike shows no download overlay for `O` frames (production's progress
screen is not in it), so download_touch's `expect ui_state modal=download`
fails by design (90 of 90 in 30 loops, run with `--keep-going`) and its
`mem` steps see no screen change to report. The first figure is the
spike's 10 s "used" sample; the instrumented build prints the pool's own
peak since the last `[mem]` screen line, which no screen line reset during
the stream, so 24776 B (7904 B left) is the stream run's real peak. It is
far from the popups peak, because the spike shows no overlay; production's
download screen will be its own measurement.

## Thread stacks, high-water across the whole run

| Thread | prio | Baseline plain | Spike paired | + lever 1 | + lever 2 |
|---|---|---|---|---|---|
| main | 0 | 4548 / 8192 (55 %) | 4724 / 8192 (57 %) | 4724 (57 %) | 4708 (57 %) |
| spike_net (the network thread) | 7 | | 1668 / 4096 (40 %) | 1668 (40 %) | 1668 (40 %) |
| wifi (driver) | 5 | | 1796 / 3584 (50 %) | 1796 (50 %) | 1780 (49 %) |
| esp_timer | 3 | | 484 / 4096 (11 %) | 452 (11 %) | 484 (11 %) |
| rx_q[0] | -1 | | 1332 / 2048 (65 %) | 1332 (65 %) | 1332 (65 %) |
| net_mgmt | -1 | | 436 / 2048 (21 %) | 436 (21 %) | 436 (21 %) |
| tcp_work | -14 | | 820 / 2048 (40 %) | 820 (40 %) | 820 (40 %) |
| sysworkq | -1 | 308 / 1024 (30 %) | **804 / 1024 (78 %)** | 804 (78 %) | 804 / 1536 (52 %) |
| logging | 14 | **916 / 1024 (89 %)** | **964 / 1024 (94 %)** | 964 / 1536 (62 %) | 964 / 1536 (62 %) |
| input | 14 | 308 / 1024 (30 %) | 308 / 1024 (30 %) | 308 (30 %) | 308 (30 %) |
| idle | 15 | 212 / 1024 (20 %) | 212 / 1024 (20 %) | 212 (20 %) | 212 (20 %) |
| ISR stack | | 340 / 2048 (16 %) | 452 / 2048 (22 %) | 452 (22 %) | 452 (22 %) |

The baseline column is the plain release image with only
`THREAD_ANALYZER(_AUTO)` added, run for three minutes under the installed
daemon; its analyzer thread is left out. The paired column is the worst of
the first paired build (`b7c6161` plus the credentials fix) and `0ebd3b3`.

Priorities, as section 11 asks: the network thread (7) is below main (0)
and the driver (5), above logging (14) and idle (15). The net stack's own
threads are cooperative (negative) and outrank main.

**The log thread was over 75 % before this feature.** 916 of 1024 B on
today's plain image; the spike adds 48 B. **The system work queue is the
radio's**: 308 B on the plain image, 788 B once the interface is up and
804 B after a rejoin.

## Down and up (D46)

| Check | Result |
|---|---|
| `esp_wifi_stop()` | `rc=0` (ESP_OK), every time |
| heap free after down vs the unpaired row | 56144 vs 56400: 256 B short, within 512 |
| largest chunk after down vs the unpaired row | 53988 vs 56364: **2376 B short, not within 512** |
| scan after down | succeeds (the driver restarts the RF itself) |
| join after down | succeeds, no `-EALREADY` |
| join while connected (disconnect-first path) | succeeds; **the same network**, as no second network was available to the bench |

The largest chunk after down is exactly what it was after the first join,
so something the first join allocates stays, mid-heap, after down. And
each join costs 40 B that down does not give back: free after successive
joins read 53984, 53944, 53904, 53864, and after successive downs 56144,
56064, 56024. Where it goes is not isolated (the spike's own join path
allocates nothing from the heap). A board that rejoins after every router
restart would lose 40 B a time; a thousand rejoins is 40 KB.

## Pass or fail, by section 11's criteria

| Criterion | Number | |
|---|---|---|
| Links with the LVGL pool at 32768 | links | pass |
| popups_touch with the tile and the thread live leaves >= 2320 B | 3400 (session_touch 3960, the stream 7904) | pass |
| dram1 >= 4096 B free, dram0 links | 13300 free (12276 with both levers) | pass |
| heap low-water >= 8192 B and largest chunk >= 4096 B at the low-water point, no driver allocation failure | low-water 38120 (worst run, not instrumented); at the instrumented low-water points **largest 40252** (40336 free) and **38508** (38528 free); none | pass |
| every thread's stack < 75 % | logging 94 %, sysworkq 78 % | **fail as built; pass with levers 1 and 2** (highest after both: rx_q[0], 65 %) |
| no brownout or watchdog reset in 30 min on a charger at 11 dBm | not run | **pending owner (charger run)** |
| signed image <= 1.2 MB (usable 1318912) | 1169602 (injector); about 1164655 plain | pass |
| D46 / Step 5 (not an S11 row): heap returns to the unpaired row within 512 B after down | free 256 B short; **largest 2376 B short**; 40 B kept per join; a second network not tested | **fail** (largest chunk, and the per-join loss) |

Section 11's lever list (net buffers, heap size, 16-bit colour, input
queue) answers RAM, and RAM passed, so none of it was applied. The only
failure was two stacks, and the levers for those were their sizes, one per
rebuild, 512 B of dram1 each. **Levers 1 and 2 are stack-size levers and
are not on section 11's list.** The controller ruled them admissible for
the stack criterion, and Task 0.3 amends section 11 to name them.

## The measured build's configuration

Kept outside the repository, in `~/Projects/Blink/spike-evidence/`:
`spike-paired2.config` (the paired row), `spike-lever1.config`,
`spike-lever2.config`, `spike-paired2-mcuboot.config`,
`baseline-plain-ta.config`. The symbols that matter:

```
CONFIG_WIFI=y
CONFIG_WIFI_ESP32=y
CONFIG_NETWORKING=y
CONFIG_NET_IPV4=y
# CONFIG_NET_IPV6 is not set
CONFIG_NET_DHCPV4=y
CONFIG_NET_TCP=y
CONFIG_NET_UDP=y
CONFIG_NET_SOCKETS=y
CONFIG_NET_MGMT=y
CONFIG_NET_MGMT_EVENT=y
CONFIG_NET_BUF_RX_COUNT=28
CONFIG_NET_BUF_TX_COUNT=40
CONFIG_NET_PKT_RX_COUNT=14
CONFIG_NET_PKT_TX_COUNT=16
CONFIG_NET_MAX_CONN=6
CONFIG_NET_MAX_CONTEXTS=6
CONFIG_NET_TCP_TIME_WAIT_DELAY=1500
CONFIG_ZVFS_POLL_MAX=4
CONFIG_ESP32_PHY_MAX_TX_POWER=11
# CONFIG_ESP32_WIFI_AMPDU_TX_ENABLED is not set
# CONFIG_ESP32_WIFI_AMPDU_RX_ENABLED is not set
CONFIG_ESP32_WIFI_STATIC_RX_BUFFER_NUM=10
CONFIG_ESP32_WIFI_DYNAMIC_RX_BUFFER_NUM=32
CONFIG_ESP32_WIFI_DYNAMIC_TX_BUFFER_NUM=32
CONFIG_MBEDTLS_CIPHER_CHACHA20_ENABLED=y
CONFIG_MBEDTLS_POLY1305=y
CONFIG_MBEDTLS_CHACHAPOLY_AEAD_ENABLED=y
CONFIG_MBEDTLS_HKDF_C=y
CONFIG_MBEDTLS_MD_C=y
CONFIG_MBEDTLS_SHA256=y
# CONFIG_MBEDTLS_TLS_VERSION_1_2 is not set
# CONFIG_MBEDTLS_TLS_VERSION_1_3 is not set
# CONFIG_DNS_RESOLVER is not set
# CONFIG_HTTP_CLIENT is not set
# CONFIG_SNTP is not set
CONFIG_ENTROPY_GENERATOR=y
CONFIG_CSPRNG_ENABLED=y
CONFIG_HEAP_MEM_POOL_SIZE=86016
CONFIG_LV_Z_MEM_POOL_SIZE=32768
CONFIG_LV_Z_BITS_PER_PIXEL=32
CONFIG_INPUT_QUEUE_MAX_MSGS=96
CONFIG_MAIN_STACK_SIZE=8192
CONFIG_ISR_STACK_SIZE=2048
CONFIG_LOG_PROCESS_THREAD_STACK_SIZE=1024   # 1536 with lever 1
CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE=1024     # 1536 with lever 2
CONFIG_THREAD_ANALYZER=y                    # measurement only
```

## What Phase 2 should take from this

- `NET_MAX_CONN` and `ZVFS_POLL_MAX` at Zephyr's defaults (4 and 3) refuse
  the replacement session at connect. Phase 2's config needs both raised.
- `LOG_PROCESS_THREAD_STACK_SIZE` is a finding about today's firmware, not
  about WiFi: 89 % on the plain release image.
- `SYSTEM_WORKQUEUE_STACK_SIZE` needs 1536 B in any image whose radio comes up.
- The 40 B a join, and the chunk the first join leaves mid-heap after down,
  are open; Phase 2's down/up proof should measure free and largest after
  many cycles, not one.
- Not measured here: the 30-minute charger run (pending the owner), and a
  second network for the re-pair join.
