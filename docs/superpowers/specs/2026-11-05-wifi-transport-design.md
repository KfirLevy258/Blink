# WiFi transport: design

Status: draft for the owner's review, 2026-11-05, revised the same day after
review round 1 (`docs/superpowers/plans/2026-11-05-wifi-transport-review-log.md`
records every finding and what was done with it). Branch `feat/wifi-transport`
from `fork-picks` at 871ce9c. Nothing here is built.

The board today hears its daemon only over the USB cable. This document moves
that same conversation onto the home network, as an option, for a board that
sits away from the computer on a phone charger. The daemon still does all the
work; the board joins WiFi, listens for its one paired computer, and shows
which link it is on.

The implementation plan that goes with it is
`docs/superpowers/plans/2026-11-05-wifi-transport.md`.

---

> Renamed 2026-11-07: the board's tile is **Open on PC**, not "Open settings". The decisions below keep the name they were made under; read "Open settings" as "Open on PC" (the install hint in `pc/cli.py` says "press Open on PC on the board").

## 1. Who decided what

Each decision below carries a tag. **Owner** means the owner said it.
**Assistant, accepted** means the assistant proposed it, the owner accepted the
direction, and it is recorded here as the assistant's call so that it can be
argued with. **Assistant** means it was decided while writing this document and
nobody has seen it yet. Section 14 lists the questions only the owner can
answer.

| # | Decision | Tag |
|---|---|---|
| D1 | Purpose: the board on a phone charger, away from the computer; the daemon feeds it over the home network | Owner |
| D2 | USB serial stays the default. WiFi is opt-in configuration | Owner |
| D3 | A settings page on the PC, grown out of the theme builder. Its shape is the approved design (`docs/design/settings-page.md` and the mock beside it): the board drawn as the hero in the centre, one status line, three controls **Look / Connect / Board**, one panel open at a time docking beside the board. The earlier Theme/Connection/Device tabs are gone (section 16) | Owner ("the last UI I got was really good, we should use it", 2026-11-06) |
| D4 | The page opens from `blink settings` and from the board's Settings screen (the Open settings tile, D56). `blink install` does **not** open a browser | Owner (revised 2026-11-06, closing Q8) |
| D5 | The board does the minimum: join WiFi, serve its paired computer, show WiFi or USB. All complexity lives on the PC | Owner |
| D6 | Security designed carefully | Owner |
| D7 | The board listens (TCP server); the daemon connects | Assistant, accepted |
| D8 | Discovery: remembered IP first, then a UDP broadcast carrying an HMAC proof; the board is silent to anything else | Assistant, accepted |
| D9 | Pairing over USB only. A 32-byte key from `secrets`, sent with SSID and password over serial. Secrets never cross the network. A new network needs the cable | Assistant, accepted |
| D10 | Handshake: board nonce first, fixed-size reply within 2 s, mutual HMAC-SHA256 proofs, HKDF-SHA256 session keys. No JSON before auth. One session at a time. Backoff after failures | Assistant, accepted |
| D11 | Every session frame encrypted and authenticated with ChaCha20-Poly1305 and a counter nonce. Encryption is required, not optional: `pc/bridge.py:1082` (`_send_session_if_changed`) already puts project names on the wire today | Owner (2026-11-05, closing the former Q1) |
| D12 | OTA over WiFi through the session, MCUboot signature check, plus a no-downgrade check for network-delivered images | Assistant, accepted |
| D13 | USB wins when the cable is present. Board shows a link mark, a read-only Settings > Connection screen and a "Forget WiFi" button | Assistant, accepted ("Forget WiFi" is the assistant's addition) |
| D14 | Logs: password, key, scanned SSIDs and network buffers follow the serial-buffer rule. Key file in `~/.blink`, 0600 / owner-only ACL | Assistant, accepted |
| D15 | Settings page reuses `theme_server`'s security model. The password goes to the board over USB and is never stored on the PC | Assistant, accepted |
| D16 | Limits: 2.4 GHz only, no WPA3-only, no 802.1X, client isolation fails, whoever holds the board can read the stored password and key from its flash (fused or not, section 4), HOST LOST when the PC sleeps or leaves | Assistant, accepted |
| D17 | Phase 0 RAM spike is a hard gate; its result picks one image or a separate WiFi image | Assistant, accepted |
| D18 | Ports: TCP 49731 for sessions, UDP 49731 for discovery | Assistant |
| D19 | Fixed binary formats for every pre-auth message (section 6) | Assistant |
| D20 | Network frames carry a one-byte type: `L` an NDJSON line, `O` an OTA chunk. OTA chunks are binary, not base64 lines | Assistant |
| D21 | The board persists new credentials only after a successful join; a failed join keeps whatever was there | Assistant |
| D22 | Open (passwordless) networks are refused in v1; hidden SSIDs can be typed by hand | Assistant |
| D23 | No DNS resolver, no TLS, no HTTP client, no SNTP in the image, and no `connect()` anywhere in `firmware/src`: the board cannot originate a connection, which is what keeps the deleted standalone mode from coming back by accident | Assistant |
| D24 | A crypto known-answer test runs on the board at every boot before the listener opens; a failure disables WiFi | Assistant |
| D25 | The settings page lives inside the daemon, which owns the port. `blink settings` asks the running daemon through an owner-only request file that carries a one-time token, never a secret | Assistant |
| D26 | `blink theme` keeps working as today; on its page the Connect and Board controls say to use `blink settings` | Assistant |
| D27 | New cfg_store fields are appended behind a new seal (`crc_143`), the same downgrade-safe pattern as `crc_134` | Assistant |
| D28 | The old record's `mode`, `ssid`, `psk` are not trusted on their own: WiFi needs the new `pair_set` byte, so a board that once ran the deleted WiFi build does not start joining its old network | Assistant |
| D29 | A paired board answers a new connection while a session is live, and an authenticated newcomer replaces the old session (a roaming laptop's dead socket) | Assistant |
| D30 | IP addresses are not logged by either side; `blink status` and the board's Connection screen show them | Assistant |
| D31 | `BLINK_FORCE_WIFI=1` (bench and fleet only) makes the daemon skip serial, so a WiFi session can be proved on a desk while a passive reader keeps the board's console | Assistant |
| D32 | The board bounds every OTA write to the image region of slot 1: offset plus length never exceeds `OTA_IMAGE_MAX` (slot minus the swap-move padding sector minus the sector-aligned trailer), whatever the manifest says. The trailer is MCUboot's to write, never the daemon's (section 9) | Assistant, review round 1 (SEC-1, FW-10) |
| D33 | "Forget WiFi" and `cfg_clear_pairing` sanitize both config sectors before reporting success, and the edition latch survives (section 5.2) | Assistant, review round 1 (SEC-3, FW-9) |
| D34 | One frame ring, one owner: decrypted `L` and `O` frames go into the same ordered ring and the main thread drains both, so `ota_begin`, chunks and `ota_end` are handled in the order they were sent by the thread that already owns OTA state (section 5.4, 9) | Assistant, review round 1 (FW-3, PLAN-3) |
| D35 | `Origin` is required on every `POST /api/wifi/*`; `GET`s keep the cookie and Host checks only, because same-origin GETs commonly carry no Origin header (section 8.3) | Assistant, review round 1 (SEC-9) |
| D36 | "Set up from another computer" is diagnosed only from USB evidence (`wifi_state.kid` on the cable). A network handshake that fails is an unauthenticated failure with ordinary backoff, never a five-minute hold (section 7.2) | Assistant, review round 1 (SEC-7) |
| D37 | `blink settings` and the end of `blink install` open the browser from the CLI process; the daemon only starts (or re-uses) the page server headless and records its port. The daemon never calls a browser opener for a request file (section 8.2) | Assistant, review round 1 (PLAN-4) |
| D38 | A board running an unconfirmed (test-boot) image refuses `ota_begin`, so the rollback image in slot 1 cannot be erased before the new one has proved itself; `boot_write_img_confirmed()`'s return value is checked (section 9) | Assistant, review round 1 (FW-5) |
| D39 | Transcript redaction covers every `BLINK_TAP` path (tx, rx and console), not only tx (section 7.5) | Assistant, review round 1 (SEC-4, PLAN-6) |
| D40 | `tools/passive_log.py` is not used for any WiFi evidence: it pre-sets DTR/RTS before the open (which reboots the board, CLAUDE.md), pulses reset, and runs on import. A new import-inert `tools/console_tail.py` that opens plainly, never touches DTR/RTS and filters through `logbook.console_line` replaces it for charger runs and the fleet (section 13) | Assistant, review round 1 (SEC-4, FW-7, PLAN-10) |
| D41 | No forward secrecy: every session key derives from K and public nonces, so whoever later extracts K from a board or the PC can decrypt recorded sessions. Stated as a limit; re-pairing rotates K (section 4, 12) | Assistant, review round 1 (SEC-8) |
| D42 | With `CONFIG_WIFI=y` the ESP32 driver initialises at boot (`esp32_wifi_dev_init` calls `esp_wifi_init`, `esp_wifi_drv.c:933`) whether or not the board is paired. What the board defers is bringing the interface up (RF on, DHCP). Phase 0 measures the unpaired cost separately (section 5.1, 11) | Assistant, review round 1 (FW-11) |
| D43 | The daemon streams a WiFi OTA incrementally from its read loop, never from inside message dispatch, so acks, pings and pongs keep flowing while the window is full (section 9) | Assistant, review round 1 (FW-4, PLAN-2) |
| D44 | cfg_store recovery is layout-aware: the pairing tail is never copied from the other slot, and a slot read through an older seal recovers only the fields that seal did not cover (section 5.2) | Assistant, review round 1 (FW-2, PLAN-5) |
| D45 | The board acks an OTA chunk stream at every 16 KiB boundary **and** when the bytes accepted equal the manifest size; `off` means bytes accepted into the stream, and `ota_end` is what flushes and seals (section 9) | Assistant, review round 2 (final-ack stall) |
| D46 | "Radio down" is `NET_REQUEST_WIFI_DISCONNECT`, sockets closed, `net_if_down()`, then `esp_wifi_stop()` called directly, because the Zephyr ESP32 driver has no `stop` callback for `net_if_down()` to reach (section 5.1) | Assistant, review round 2 |
| D47 | The network thread starts at boot whenever the self-test passes; pairing is not a precondition. Only the listener and the discovery socket wait for a pairing (section 5.4) | Assistant, review round 2 |
| D48 | Secrets on Windows are created inside `~/.blink/private\`, a directory **created atomically with its own protected security descriptor** (`CreateDirectoryW` with `SECURITY_ATTRIBUTES` from an SDDL naming only the user's SID, through `ctypes`); its owner SID, DACL and reparse status are validated on every write and load, and so are the file's; a directory that already exists with another owner fails closed (section 7.3) | Assistant, review rounds 2 and 3 |
| D49 | The `BLINK_TAP` console path and `tools/console_tail.py` share one **allowlist-only** line policy (section 7.5): a protocol line of a known type is redacted, a printk line is cut by `logbook.console_line` to its fixed form, the fleet's applied/STALE markers are synthesized from that fixed form, and everything else is `(board line)`. Neither has a raw mode and nothing passes because of what it lacks | Assistant, review rounds 2 and 3 |
| D51 | A serial candidate whose open or probe failed is excluded from the next polls on its own backoff (5 s doubling to 60 s), so a port that is always busy or never ours cannot keep WiFi from its turn (section 7.2) | Assistant, review round 3 |
| D52 | A fused board gets the first WiFi-capable image only from the factory tool (`tools/flash_encrypted.sh --app-only`, application only, bootloader and logo untouched): the shipped 1.4.x image has no radio and the daemon refuses USB writes to an encrypted chip. Stated to customers (section 9, 12) | Assistant, review round 3 |
| D53 | On Windows the **file** is what is made safe: created with `CreateFileW(CREATE_NEW)` and its own protected owner-only descriptor, then validated through the open handle (`GetSecurityInfo`, owner SID, exact DACL, not a reparse point, final path inside the private directory) before a byte is written; loads open first and validate the handle the same way. The directory is defence in depth, not the guarantee (section 7.3) | Assistant, review round 4 |
| D54 | Transcript redaction is a per-type **field allowlist** (`protocol.TAP_FIELDS`): for every wire type, the fields that may be recorded, scalar values only, enum fields checked against their enum; unknown fields and nested values are dropped. The console allowlist's external strings (ROM reset lines, the Zephyr banner) live in their own separately-audited list (section 7.5) | Assistant, review round 4 |
| D55 | Bench UI stress during a live WiFi session uses `tools/board_touch.py --touch-only`: the three injector types only (`touch`, `ui_state`, `widgets`), no `welcome`, no usage, no `fake`; in the injector build those are dispatched before the host stamp and do not count as a USB host, so the session and an OTA in flight survive. Two scripts, one for the live session's screens and one for the download overlay, because the overlay swallows touches. Injector builds never ship (`release.sh`'s existing guard) (section 11, 13) | Assistant, review rounds 4 and 5 |
| D56 | The board gets no theme editor and no "Edit on computer" button. Its Settings screen's Theme tile becomes **Open settings**, which asks the daemon to open the settings page in the computer's browser over the existing `theme_edit` path. All configuration happens in the browser (section 15) | Owner, 2026-11-06 |
| D57 | The settings page uploads a company logo: the browser rasterises whatever image the owner picks onto a 320x240 canvas and posts the pixels; the daemon encodes them to the board's `BLGO`/`BAN1` logo format with the standard library and streams the result to the board over USB or the WiFi session with the OTA writer's ordered, acked, bounded path; the board validates with `logo_parse` before it switches; "Remove logo" erases the partition (section 15) | Owner, 2026-11-06 |
| D58 | The settings page shows and sets the main-screen view (including the burn view from `feat/ultra-burn`, not yet on this branch), Focus, the main provider and the weekly model scope, and carries page-only preview sliders for the session and weekly gauges. One additive wire message, `pref_set`, carries the choices; the board answers with its existing `pref` (section 15) | Owner, 2026-11-06 |
| D59 | Usage refresh (`feat/usage-refresh`) and a switch for the fixed four-hour doze are listed on the page as dependencies and open questions, not designed here (section 15, Q13) | Owner, 2026-11-06 |
| D60 | Logos may be animated GIFs, as the boards' factory logos are today (BAN1 clips). The board enforces playback limits before any logo is shown (frames, play time, hold, rects), so no CRC-valid blob can hold the boot; GIF decoding and BAN1 encoding are standard-library code in `pc/`, with parity tests against the factory tools on generated fixtures (section 15.2, closing Q12) | Owner, 2026-11-06 |
| D62 | The approved design is the page, not a skin on the old one: `pc/theme_builder.html` is rebuilt to `docs/design/settings-page.md` (committed with the mock in the first UI task, outside the gitignored `docs/superpowers`), reusing the page's existing SVG board renderer so the preview stays faithful to the firmware (section 16) | Owner, 2026-11-06 |
| D63 | The shipped page loads nothing external: Montserrat 500 and 600 (OFL) are vendored as two latin woff2 files served by the daemon, because the board preview must use LVGL's font to be faithful; the UI face is the design's system stack, not a bundled IBM Plex, because the wordmark is a drawing and 150 KB more download buys nothing the stack does not give (section 16.3) | Assistant, accepted direction (owner asked for offline) |
| D64 | The page is verified by Node tests per state and interaction, a committed Playwright script (test-venv only, never a runtime dependency) that screenshots the mock and the served page at 1440x900 and 400px in light and dark and asserts the layout invariants by bounding box, the design spec's accessibility list, and a desk check (section 16.4) | Assistant |
| D67 | The settings page is a persistent session: a theme save keeps the server alive (today's save handler ends it, `theme_server.py:485-488`), and the session ends on page close, idle, or the page's Done. `blink theme` keeps its foreground, save-ends-it behaviour (section 8.2) | Assistant, review round 12 |
| D65 | After pairing over USB the daemon runs one verify-only session to the board's WiFi address: the real handshake, one fixed frame each way, closed at once, accepted by the board only inside a 60 s window opened by the successful `wifi_set` and never taking the link over. The Connect panel's ticks are these real events (section 5.4, 8.3) | Assistant, review round 11 |
| D66 | The logo upload carries the page's hold time and fit as validated headers; the hold choices are 3, 5 and 10 s because play plus hold must fit the board's total cap, so the design's 30 s option is withdrawn (section 15.2) | Assistant, review round 11 |
| D61 | Weekly scope is not on the page: the board no longer reads `weekly_sel` (`usage_view.c:296`), so a control would do nothing. Removed, not implemented; the owner may revisit (section 15.3) | Owner, 2026-11-06 |
| D50 | `blink settings` is acknowledged by request id: the daemon writes `{"port", "ack": <id>}` after the token is registered, and the CLI opens nothing until it sees its own id (section 8.2) | Assistant, review round 2 |

---

## 2. Goals and non-goals

Goals:

- A board powered by any USB charger shows the same panel it shows on the
  cable, fed by the daemon on the owner's computer over the home network.
- Setting it up takes one cable session and one page in the browser.
- The board's network surface is small enough to read in one sitting: one TCP
  listener whose pre-auth input is 48 fixed bytes, and one UDP responder whose
  input is 36 fixed bytes.
- Nothing a customer sends us (bridge.log, the board's console) gains a
  password, key, SSID, IP or frame byte.
- Firmware updates still work for a board that is never plugged in again.

Non-goals:

- The board talking to anything but its paired computer. It never fetches
  usage, time or firmware from the internet. The standalone mode deleted in
  1e8abe5 does not come back in any part, and `tools/release.sh` keeps refusing
  its strings.
- Several computers feeding one board at once (see Q3 for one PC with several
  environments).
- 5 GHz, WPA3-only, enterprise (802.1X) networks, captive portals, a SoftAP or
  any setup that does not use the cable.
- Remote access from outside the home network.

---

## 3. Architecture

```
 PC (daemon, claude_usage_bridge.py)                      Board (ESP32 CYD)
 ----------------------------------                      ------------------
 Bridge (pc/bridge.py)  -- write_msg / on_message --      proto.c dispatch()
        |                                                        |
   link: one of                                             link.c: which link
     serial.Serial (USB, today)    <== USB serial ==>        UART ring (today)
     WifiLink (pc/wifi_link.py)    <== TCP 49731 ===>        wl_server.c thread
        |   handshake, AEAD frames                              |   handshake, AEAD
        |   (pc/wifi_crypto.py)                                 |   (wl_hs.c, wl_frame.c,
        |                                                       |    wl_crypto_mbedtls.c)
   discovery (UDP 49731 broadcast) ==================>     UDP responder (silent
                                                            to bad queries)
 Settings page (pc/theme_server.py, in the daemon)          net_link.c: STA join,
   Look | Connect | Board (section 16)                      scan, events
   Connection ops -> queue -> serial loop -> wifi_* lines   cfg_store: ssid, psk,
                                                             pair key (USB only)
```

The NDJSON protocol does not change. Every message that crosses USB today
crosses the network unchanged, inside an encrypted frame. The board's
`dispatch()` sees the same plaintext line whichever link it came from, and
`emit()` sends replies to whichever link is active. That is what keeps the board
small: the WiFi code is a transport, not a second protocol.

### 3.1 Why the board listens and the daemon connects (D7)

- An outbound TCP connection from the PC raises no firewall prompt on Windows
  or macOS. A PC that listened would prompt on first run, and a customer who
  clicked "Deny" would have a board that never connects and nothing on screen
  saying why.
- WSL2 in its default NAT mode can open outbound connections to the LAN but
  cannot be reached from it. A board that dialled the PC could not reach a
  daemon running in WSL.
- A laptop that changes networks and comes back simply reconnects. The board
  never needs to know the PC's address.
- The board's whole pre-auth surface is reading 48 bytes.

---

## 4. Threat model

What is protected: the usage numbers and, once `docs/session-name-hint-design.md`
ships, project names; the WiFi password; the pairing key; the board's flash
(nobody but the paired PC may install firmware); the owner's desk (nobody may
make the panel lie).

Attackers considered:

1. **Someone on the same LAN** (a guest, a compromised IoT device). Can see
   and inject traffic, connect to the board, replay anything captured, send
   broadcasts. Must not: read frames, alter them, replay them, learn the key
   or password, push firmware, make the board answer discovery, or crash the
   board with malformed input. Can: deny service by flooding the listener
   (accepted, section 5.5), and see that a device is listening on 49731.
2. **A web page in the owner's browser**. Can POST to loopback ports. Must
   not reach the settings server (one-time token, cookie, Host and Origin
   checks, as `pc/theme_server.py` already does).
3. **Another program as the same user on the PC**. Can read `~/.blink`. Not
   defended against: it already owns the account. The key file is owner-only
   so that *other* accounts cannot read it.
4. **Someone holding the board**. On an unfused board, esptool reads the
   config partition, which holds the SSID, password and pairing key in the
   clear. Documented as a limit (D16). A fused board (flash encryption on)
   resists reading the flash directly, but `pc/efuse_probe.py` checks only
   `FLASH_CRYPT_CNT`; whether a fused board also resists a replaced
   application that dumps the record depends on secure boot and the
   download-mode fuses, which this project has never measured. So the limit
   is stated for every board, fused or not, and "Forget WiFi" (D33) is the
   answer before a board changes hands. Because there is no forward secrecy
   (D41), a key read this way also opens recorded sessions.
5. **A malicious or buggy daemon on the paired PC**. Trusted by design for
   usage. For firmware it is still bounded by MCUboot's ECDSA-P256 signature
   check, by the no-downgrade check, by the image-region bound (D32: it
   cannot write a trailer that marks its image permanent and skips the test
   boot), and by the unconfirmed-image refusal (D38).

Out of scope: an attacker with the pairing key; RF jamming; the router itself
being hostile beyond attacker 1.

Accepted, stated so nobody re-derives them:

- **Tracking by discovery replay.** A captured discovery query stays valid
  for the life of K (the board has no clock, so freshness cannot be
  checked), and a replayer learns "the same paired board is here" after an
  address change, which is more than ARP tells them. Re-pairing rotates K.
  A replayer can also use up the one-answer-per-second budget and delay the
  paired PC's discovery; the PC tries its remembered address first and
  discovery is the fallback, so the delay costs a reconnect, not a session.
- **Timing of the PC's Poly1305.** `pc/wifi_crypto.py` computes Poly1305 in
  Python integers, which is not constant time. The attacker who could
  measure it is on the LAN sending forged frames to the daemon, and every
  forged frame closes the session (section 5.4), after which the board's
  backoff (5.5) and the daemon's (7.2) set the pace: one sample per
  reconnect, tens of seconds apart, against a per-session key. Tag
  comparison itself is `hmac.compare_digest`, and the tag is verified before
  any plaintext is produced. Accepted as a limit; the owner may still ask
  for an outside review of the cipher (Q9).

---

## 5. The board

### 5.1 Radio bring-up

Mined from the deleted `net_wifi.c` (`git show 1e8abe5^:firmware/src/net_wifi.c`)
for the station half only: `net_mgmt` event callbacks for connect, disconnect,
scan result and scan done, IPv4 address add, and the security-type choice from
the scan. Everything about the SoftAP, its DHCP server, the DNS hijack, the
portal, OAuth, usage fetching and timezone fetching stays deleted.

Config, starting from the old `wifi.conf` and cutting:

- Keep: `WIFI`, `WIFI_ESP32`, `NETWORKING`, `NET_IPV4`, `NET_IPV6=n`,
  `NET_DHCPV4` (client), `NET_TCP`, `NET_UDP`, `NET_SOCKETS`, `NET_MGMT`,
  `NET_MGMT_EVENT`, `NET_CONTEXT_RCVTIMEO`, `NET_CONTEXT_SNDTIMEO`,
  `ESP32_WIFI_AMPDU_TX_ENABLED=n`, `ESP32_WIFI_AMPDU_RX_ENABLED=n`,
  `ESP32_PHY_MAX_TX_POWER=11` (the brownout cap the old build measured).
- Drop: `ESP32_WIFI_AP_STA_MODE`, `WIFI_NM` multi-interface, `NET_DHCPV4_SERVER*`,
  `NET_IF_MAX_IPV4_COUNT=2`, `DNS_RESOLVER`, `SNTP`, `HTTP_CLIENT`, every
  `MBEDTLS_*` TLS, X.509, ECP and PEM option, `NET_SOCKETS_SOCKOPT_TLS`, QR code
  and textarea widgets.
- Add for crypto (symbols verified in the 4.3.1 workspace,
  `zephyr/modules/mbedtls/Kconfig.mbedtls`, mbedTLS 3.6.6):
  `MBEDTLS_CIPHER_CHACHA20_ENABLED`, `MBEDTLS_POLY1305`,
  `MBEDTLS_CHACHAPOLY_AEAD_ENABLED`, `MBEDTLS_HKDF_C`, `MBEDTLS_MD_C`,
  `MBEDTLS_SHA256` (already on), and the CSPRNG (`ENTROPY_GENERATOR`,
  `CSPRNG_ENABLED`) for `sys_csrand_get`. The ESP32's RNG is a true RNG only
  while the radio is on, which it is whenever nonces are drawn.
- Net buffer counts start at the old build's (`NET_BUF_RX_COUNT=28`,
  `TX=40`, `PKT_RX=14`, `PKT_TX=16`) and are trimmed in Phase 0 by measurement:
  those were sized for a SoftAP serving a portal page, and this link carries at
  most a few frames a second.
- `HEAP_MEM_POOL_SIZE=86016` is already in every image (prj.conf says it was
  sized for the WiFi build and never re-measured). The WiFi driver allocates
  from it. Phase 0 measures its low-water mark before anyone touches it.

What "dormant" means, precisely (D42): with `CONFIG_WIFI=y` Zephyr's device
initialiser runs `esp_wifi_init()` at boot (`esp_wifi_drv.c:933` in the
4.3.1 workspace), so the driver's own allocations happen on every board,
paired or not. What the board defers is `net_if_up()`: the interface, the
RF, DHCP and the sockets come up only when the board is paired or a scan is
asked for over USB, and `net_if_down()` takes them down again. An unpaired
board, which is every board until the owner opts in, never brings the
interface up. A scan with no pairing leaves it up for 60 s after the last
scan request and then takes it down. Phase 0 measures the unpaired cost
(driver initialised, interface down) as its own row, because that is the
cost every customer pays whether or not they opt in, and proves that
down/up/down actually releases and re-acquires what the measurement says.

What "down" has to do (D46): `net_if_down()` reaches a driver's `stop`
callback through the Ethernet L2 (`ethernet.c:830`), and the ESP32 WiFi
driver registers none (`esp_wifi_drv.c:971`), so `net_if_down()` alone
leaves the RF running and the association in place. The driver calls
`esp_wifi_stop()` only from its AP-disable path, and `esp_wifi_start()` from
connect and scan (`esp_wifi_drv.c:517`, `:643`), so stopping is ours to do.
`net_link_request_down()` therefore, in order: closes every socket the
network thread owns; `NET_REQUEST_WIFI_DISCONNECT` and waits for the
disconnect event (bounded, 2 s); `net_if_down()` (DHCP released); then
`esp_wifi_stop()` directly, the one ESP-IDF call in `net_link.c`, with a
comment saying why. A later scan or join restarts the RF through the
driver's own `esp_wifi_start()`. Before a join, if the driver's state is
still connecting or connected (which makes `esp32_wifi_connect` return
`-EALREADY`, `esp_wifi_drv.c:495`), the same disconnect-and-wait runs
first; that is what re-pairing to another network needs. The old
`net_wifi.c` never stopped the RF (it used `net_if_down()` and nothing
else), so this is new and Phase 0 proves it: `esp_wifi_stop()` returns
`ESP_OK` and a join afterwards succeeds (Phase 0 proved both). The heap does
not fully return to the unpaired row's number: free came back within 256 B,
but the largest free chunk stayed 2376 B short of the 512 B allowance (what
the first join allocates stays mid-heap), and each join kept 40 B that down
did not give back. That is not a section 11 criterion and did not fail the
gate; it is carried forward as a requirement on the `net_link` task, which
re-measures free and largest chunk on production firmware over at least 20
down/up cycles and fails if the retained amount keeps growing. The join that
disconnects first was only proven against the same network (the bench had no
second one), so re-pairing to another network is still to be proven there.

New Kconfig `CONFIG_BLINK_WIFI_LINK`, deliberately not the old
`CONFIG_BLINK_WIFI_MODE` name, so release.sh's guard on the old symbol keeps
meaning what it says.

### 5.2 What is stored (cfg_store)

The record keeps every existing field at its offset (cfg_store.h explains why).
The deleted mode's `mode`, `ssid` and `psk` fields are reused **only** for the
SSID and password; `mode` stays reserved and `CFG_MODE_WIFI` keeps meaning the
deleted mode. New fields are appended after `focus_win` behind a new seal:

```
	uint8_t focus_win;
	uint32_t crc_143;      /* the 1.4.x record's seal, kept where 1.4.x looks */
	uint8_t pair_set;      /* 1 = WiFi paired; anything else = not */
	uint8_t pair_key[32];  /* the pairing key; secret, never logged */
	uint32_t crc;          /* over everything above, always last */
```

Why the new seal: today a downgrade finds `crc` in the wrong place and falls
back to the `crc_134` reader, which zeroes theme and Focus. Sealing the 1.4.x
prefix the way `crc_134` sealed the 1.3.4 one means a board downgraded from a
WiFi build keeps its theme and loses only the WiFi pairing, which a 1.4.x image
cannot use anyway. `tests/cfg_store/host_test.c` plays both directions, as it
does for the theme fields.

Why `pair_set` (D28): a board that once ran the deleted WiFi build still holds
its SSID and password (`firmware/README.md` documents the erase). The new code
treats the SSID and password as present only when `pair_set == 1`, so such a
board does not suddenly start joining an old network. The first pairing
overwrites them; "Forget WiFi" clears all three, which also finally gives those
old boards a way to clear the leftover password from the screen.

Accessors (cfg_store.h):

```c
int  cfg_set_pairing(const char *ssid, const char *psk, const uint8_t key[32]);
bool cfg_get_pairing(char *ssid, size_t ssid_len, char *psk, size_t psk_len,
                     uint8_t key[32]);   /* false unless pair_set == 1 */
int  cfg_clear_pairing(void);            /* ssid, psk, key, pair_set */
```

`cfg_reset()` (factory reset) clears the pairing too. The edition latch is
untouched.

**Recovery is layout-aware (D44).** Today `cfg_init` (cfg_store.c:473)
copies everything from `theme_id` to `crc` out of the other slot when the
current slot was read through an older seal and the other slot is whole.
With the new tail that copy would carry `pair_set` and `pair_key` too, and
two bad things follow: a board downgraded to 1.4.x, written there, and
upgraded again would have its newer theme overwritten by the older slot's
and its revoked pairing resurrected; and a torn write whose 1.3.4 prefix
(new SSID and password) survived but whose tail did not would be stitched to
the older slot's key. So:

- a slot read through `crc_134` recovers `theme_id` to `focus_win` from the
  other slot, as today, and nothing after `crc_143`;
- a slot read through `crc_143` recovers nothing: the 1.4.x write was newer
  than whatever the other slot holds, and a pairing that a 1.4.x image could
  not see is not brought back;
- the pairing tail is never copied between slots. A torn pairing write
  leaves the board unpaired, which is the safe side (D21): the owner pairs
  again with the cable.

The power-cut test in `tests/cfg_store/host_test.c` currently cuts at every
byte of a 512-byte range; it is extended to every byte of `REC_WIRE` so the
tail is inside the loop, and run from both slots.

**Forget sanitizes both sectors (D33).** `persist()` writes the next slot and
deliberately leaves the previous sector intact (cfg_store.c:363), so after
one cleared write the old sector still holds the password and key for
whoever reads the flash. `cfg_clear_pairing` therefore: clears the fields in
RAM, persists (slot N), erases the other slot, persists again (slot O, seq
+1), and only then returns 0. Both sectors now hold a cleared record and
the edition latch (in the 1.3.4 prefix) is in both. A failure at any step
returns the error, the board logs `[wifi] forget failed` and the Connection
screen says `Could not forget WiFi. Try again.` (31 chars); the RAM copy is
already cleared, so the board stops using the network either way. The
`wifi_set` candidate buffers in `proto.c` are zeroed after use. The host test
inspects the whole `fake_flash` array, both sectors, for any byte of the
SSID, password or key, and the power-cut loop runs across the three steps.

### 5.3 Link arbitration (D13)

`firmware/src/link.c` owns one fact: which link protocol lines go to.

- `LINK_USB` while a USB host has spoken within `HOST_TIMEOUT_MS` (30 s, the
  constant proto.c already has). Any authenticated network session is closed
  the moment a USB host speaks, and the listener refuses new sessions until
  USB has been silent for `HOST_TIMEOUT_MS`.
- `LINK_WIFI` while a network session is authenticated and USB is silent.
- `LINK_NONE` otherwise. Replies (hello, ping) go to USB, as today, so a
  freshly plugged-in cable works exactly as it does now.

`emit()` in proto.c calls `link_emit(line)`: USB lines stay `printk` (the long
prj.conf comment on the deferred log still applies), WiFi lines go into the
network TX ring. Diagnostics (`printk("[tag] ...")`) never go to the network:
only `emit()` reaches `link_emit()`.

`dispatch()` learns which link a line came from. `wifi_scan`, `wifi_set` and
`wifi_forget` are honoured only from USB; arriving over the network they are
dropped without a reply. Pairing is a physical act.

"USB host has spoken" means a protocol line arrived on the UART, not that power
is present. A phone charger has no data lines; a computer with no daemon sends
nothing. Both leave WiFi in charge.

### 5.4 The listener and the session

`firmware/src/wl_server.c`: one thread (stack size decided in Phase 0) that
owns every network socket and runs `net_link_work()`. The main loop, which
owns LVGL, never blocks on the network.

The thread starts at boot, after the self-test passes, whether or not the
board is paired (D47): the first pairing's scan and join are this thread's
work, and a thread that waited for a pairing could never produce one. What
waits for a pairing is the listener and the discovery socket: they are
opened when `cfg_paired()` and the interface is up, closed on Forget. With
no pairing and no scan request the thread sleeps on its `poll()` with no
sockets, which costs its stack and nothing else.

- TCP listener on 49731, backlog 1. The thread `poll()`s the listener, the
  live session's socket, at most one candidate socket and the UDP socket
  together, with a 200 ms tick; nothing in the loop blocks on one socket
  while another has data. The candidate handshake is a small state machine
  with a 2 s deadline, not a blocking read, so an attacker who connects
  continuously cannot stall the live session's frames (SEC-7's starvation
  point); at most one candidate at a time, the rest are closed at accept.
- On accept: if the failure backoff is running, close at once. If USB is
  active, close at once **unless the verify window is open** (D65): a
  successful `wifi_set` over the cable opens a 60 s window in which the
  board accepts one **verify-only** session: the same M1/M2/M3, then
  exactly one sealed `{"t":"verify"}` line, answered with one sealed
  `{"t":"verified"}`, then close. It never calls `link_wifi_up`, nothing it
  carries reaches `dispatch()`, and it closes the window. It exists because
  pairing happens over USB while USB wins (5.3), so no real WiFi proof could
  otherwise run before the page says "You can unplug the cable"; this is
  that proof, done with the same key and the same handshake, read-only,
  time-boxed, and not a takeover. The page's four ticks are the events that
  actually happen: `wifi_set` acknowledged over the cable, `wifi_state up`,
  the verify handshake's M3 accepted, the `verified` frame opened.
- Draw a fresh `Nb` from `wl_random()` for every candidate. If the RNG
  fails, send nothing, close, log `[wifi] selftest failed` once and stop the
  listener until reboot: a repeated nonce would let a captured M2 replay.
- Send M1 (20 bytes, section 6.2). Read exactly 48 bytes by the deadline
  measured from accept. Short, late or closed early: close.
- Verify M2's tag with `mbedtls_ct_memcmp` (public in mbedTLS 3.6,
  `mbedtls/constant_time.h`). Wrong: close, count a failure.
- Send M3. Derive session keys. Any crypto call returning non-zero (HMAC,
  HKDF, `mbedtls_md_setup` can fail allocation, `hkdf.c:107`) aborts the
  candidate: close, `memset` the candidate state, count nothing (it was not
  an attacker). A session never starts with keys from a failed derivation.
- If a session was already live, the new one replaces it (D29): the old
  socket is closed, both rings are cleared, and the old session's keys and
  counters are zeroed before the new ones are installed.
- Session frames (section 6.3): read the 2-byte `len`; if it is 0 or over
  `WL_PT_MAX` (517) close without reading further; read `len + 16` bytes;
  open with the expected counter; any failure closes the session and zeroes
  its state. Decrypted frames, `L` and `O` alike, go into **one ordered frame
  ring** (D34) that `proto_service()` drains on the main thread: `L` bodies
  through the same over-long-line rule as the UART ring, `O` bodies to
  `ota_net_chunk()` (section 9). The network thread stops reading the socket
  while the ring is full; TCP flow control holds the daemon back.
- TX: `link_emit()` copies the whole line, newline included, into a TX ring
  of `WL_TX_LINES` lines (sized in Phase 0, no fewer than 8); the network
  thread seals and sends. The main thread never calls `send()`. If the ring
  is full, `link_emit()` waits up to 200 ms for room (the thread drains a
  line in well under a millisecond, so a full ring means the socket has
  stopped taking data), and if it is still full it closes the session with
  `[wifi] session closed`: an explicit failure the daemon reconnects from,
  never a silently dropped reply (the incident prj.conf's log block records,
  22 lost replies in one run). Rings are cleared on close and on replacement.
  Only `emit()` reaches `link_emit()`; deferred-log output never does.
- Counter reaches 2^64 - 1: close. (At one frame per millisecond that is 584
  million years; the check costs nothing and closes a class of argument.)

Liveness is the protocol's own: the board pings every 10 s and declares the
host gone after 30 s of silence, over WiFi exactly as over USB. No TCP
keepalive.

### 5.5 Failure backoff and denial of service

After a handshake failure the listener closes every new connection immediately
for `min(2^n, 60)` seconds, n the consecutive failures, reset by a success. The
backoff is global, not per address, because addresses on a LAN are cheap to
change. A live session is not affected by the backoff: an attacker cannot kick
an authenticated session, only delay a reconnect.

Accepted: an attacker on the LAN who connects continuously keeps the paired PC
from reconnecting. The board shows HOST LOST, which is honest. Documented as a
limit.

### 5.6 Discovery responder

UDP 49731. A datagram is answered only if it is exactly 36 bytes, starts with
the query magic, and its truncated tag verifies under the pairing key
(section 6.4). Everything else, including a valid query arriving while USB is
active, gets no answer. At most one answer per second. The answer goes unicast
to the query's source address and port.

A replayed valid query makes the board answer the replayer, which tells them
that *this paired board* is at that address, even after the address changed.
Accepted and written into the threat model (section 4), together with the
rate-limit starvation it allows.

### 5.7 What the board shows

- **Link icon** on the gauge screen, top right (owner decision 2026-11-07:
  "remove the right top dot ... and just use the usb/wifi logo instead -
  change the color based on the status"). The health dot is gone; this icon
  sits where it was and is never hidden. `LV_SYMBOL_WIFI` on a WiFi link,
  `LV_SYMBOL_USB` on USB or no link. Its colour is the dot's old data-health
  colour: theme live (OK, or stale on another page), theme stale (stale on this
  page), red (error), grey (disconnected). This supersedes the earlier "nothing
  when unpaired, panel pixel-identical" rule: every gauge scene changes. Both
  glyphs are in the Montserrat fonts already enabled.
- **Settings > Connection**, a fifth tile, read-only:
  - `Link: USB cable` / `Link: WiFi`
  - `Network: <ssid>` (through `fmt_ascii`, ellipsised)
  - `Address: 192.168.1.23`
  - `Signal: good` / `fair` / `weak` (RSSI above -60, above -75, below)
  - unpaired: `Set up WiFi on the computer: blink settings` (44 chars)
  - last failure, one of: `Wrong WiFi password`, `Network not found`,
    `No address from the router`, `WPA3-only networks are not supported`,
    `WiFi self-test failed`, `Could not forget WiFi. Try again.` (all under
    the 47-character limit)
  - a `Forget WiFi` button when paired. Confirm: `Forget this network? The
    board goes back to USB.` with `Forget` and `Cancel`. Forget clears the
    pairing, takes the radio down and closes any session.

The fifth tile changes the Settings grid, which is the screen the LVGL pool is
tightest on (prj.conf: popups_touch at 2320 B left at the old pool size, and a
fork halted the board with one more row). Phase 1 measures it with
`tools/board_touch.py script tools/bench/popups_touch.txt`. If the tile costs
more than the margin allows, the Connection screen moves to a row on the Update
screen instead; that fallback is recorded in the plan.

### 5.8 What the board logs

The console is the file customers send us, and `logbook.console_line` keeps it
to an allowlist. New board lines are fixed strings, no values:

```
[wifi] radio up            [wifi] joined           [wifi] join failed: <reason>
[wifi] lost the network    [wifi] session open     [wifi] session closed
[wifi] handshake refused   [wifi] paired           [wifi] forgotten
[wifi] selftest failed     [wifi] forget failed
```

`<reason>` is one of the fixed reason strings above. No SSID, IP, RSSI, nonce,
byte count or frame content, at any log level. The allowlist in
`pc/logbook.py` and `tests/pc/test_console_allowlist.py` gain exactly these.

---

## 6. Wire formats

### 6.1 New NDJSON messages over USB (pairing)

Additive to protocol v2, as every message since v2 has been. A board that
advertises `"wifi"` in `caps` (BOARD_CAPS in proto.c) handles them; the daemon
offers the Connect panel's WiFi controls only to such a board.

SSIDs are arbitrary bytes (up to 32), and passwords may hold any printable
character, so both travel as lowercase hex: no JSON escaping on the board,
no UTF-8 question, and a fixed worst-case length.

| Type | Direction | Fields | Widest line |
|---|---|---|---|
| `wifi_scan` | host -> board | none | ~25 B |
| `wifi_net` | board -> host | `i` index, `n` count, `s` SSID hex (<=64), `r` RSSI, `sec` `"wpa2"`/`"wpa3"`/`"mixed"`/`"open"` | ~120 B |
| `wifi_set` | host -> board | `s` SSID hex (<=64), `p` password hex (<=128), `k` key hex (64) | ~300 B |
| `wifi_forget` | host -> board | none | ~27 B |
| `wifi_state` | board -> host | `st` `"off"`/`"joining"`/`"up"`/`"fail"`, `link` `"usb"`/`"wifi"`, `s` SSID hex, `ip`, `port`, `r` RSSI, `kid` (8 hex: first 4 bytes of SHA-256 of the key), `why` fixed reason | ~200 B |
| `ota_ack` | board -> host (WiFi OTA) | `off` bytes written; `off:0` is the "ready" ack after `ota_begin` | ~40 B |
| `ota_end` | host -> board (WiFi OTA) | `size` | ~40 B |

All of these are added to `protocol.WIRE_TYPES`, so `log_name` names them and
nothing else. The widest of each is added to `tests/pc/test_protocol.py`'s
measurement; the budget is 512 bytes and the widest new line is about 300.

Scan results come one line per network, strongest first, at most 20. One line
for the whole list would cross 512 bytes on any apartment block's air.

`wifi_state` is sent after every welcome, on every change, and in answer to
`wifi_set`/`wifi_forget`. `kid` lets the daemon tell whether the board holds
*its* key (a board re-paired from another computer shows a different `kid`).

The password is validated on the PC before sending: 8 to 63 printable ASCII
characters, or exactly 64 hex digits (a raw PSK). The board validates again.

### 6.2 Handshake (TCP 49731, all fixed size)

```
K       32-byte pairing key
M1  board -> PC   20 B   "BLW" 0x01 || Nb[16]
M2  PC -> board   48 B   Np[16] || HMAC-SHA256(K, "blink-wifi-1 pc" || Nb || Np)
M3  board -> PC   32 B   HMAC-SHA256(K, "blink-wifi-1 board" || Nb || Np)
keys = HKDF-SHA256(salt = Nb || Np, ikm = K, info = "blink-wifi-1 session", L = 64)
k_pb = keys[0:32]   PC -> board
k_bp = keys[32:64]  board -> PC
```

The board sends first so that an attacker cannot choose the board's nonce; the
board answers only after the PC has proved key possession, so the board's own
proof is never an oracle for strangers. Both nonces feed the keys, so neither
side alone can force a session key to repeat. Labels differ per direction so a
proof cannot be reflected. Tags are compared in constant time
(`hmac.compare_digest` on the PC, a constant-time loop on the board).

The PC checks M1's magic and version and fails with a fixed message on
mismatch: that is how a future `0x02` is told apart.

### 6.3 Session frames

```
frame  = len[2, big-endian] || ciphertext[len] || tag[16]
len    = length of the ciphertext alone, which equals the plaintext length;
         1 <= len <= WL_PT_MAX; a frame on the wire is len + 18 bytes
nonce  = 0x00000000 || counter[8, big-endian]     (separate key per direction)
aad    = len[2]
plaintext = type[1] || body
  type 'L' (0x4C): body = one NDJSON line including its '\n', <= 512 bytes
  type 'O' (0x4F): body = offset[4, big-endian] || image bytes, <= 512 bytes
WL_PT_MAX = 1 + 4 + 512 = 517
```

Both readers check `len` against the same two bounds before reading a body,
and both test suites share the boundary vectors (`len` 0, 1, 517, 518,
65535): a reader that let 518 through would read 18 bytes past the frame
and desynchronise.

Counters start at 0 in each direction and must arrive exactly in order (TCP
delivers in order; a gap is an attack or a bug, and either closes the session).
The 512-byte line budget is the plaintext line's, identical to USB:
`encode_checked()` still decides what may be sent, and the frame adds 19 bytes
around it that do not count against it.

### 6.4 Discovery (UDP 49731)

```
query   PC -> broadcast 255.255.255.255:49731   36 B
        "BLQ" 0x01 || Nq[16] || HMAC-SHA256(K, "blink-disc-1 q" || Nq)[0:16]
answer  board -> query source                    22 B
        "BLA" 0x01 || HMAC-SHA256(K, "blink-disc-1 a" || Nq || port[2])[0:16] || port[2]
```

The PC learns the address from the answer's source. A 16-byte truncated tag is
ample for a message whose only power is to make the board say "here".

### 6.5 Why encrypt rather than only authenticate (D11)

HMAC-only would be all standard library on the PC and simpler on the board.
It was not chosen because the frames are not boring today: `pc/bridge.py`'s
`_send_session_if_changed` (line 1082) already sends the current project
name to the board on every change, and on a shared flat or office LAN a
project name is exactly what should not cross the air in the clear. An
authenticate-only link would have exposed that from its first session.
Adding encryption later would mean a second handshake version and a
migration across boards already on desks; doing it now costs one ~150-line
pure-Python ChaCha20-Poly1305 (RFC 8439) checked against the RFC's published
vectors, and on the board mbedTLS code that is already in the source tree
(mbedTLS is in every image for OTA's SHA-256). The owner decided this on
2026-11-05 (D11); the former Q1 is closed.

---

## 7. The daemon

### 7.1 Link abstraction

`claude_usage_bridge.py` uses a small surface of `serial.Serial`: `read(n)`,
`in_waiting`, `write(raw)`, `close()`, `reset_input_buffer()`, `dtr`, `rts`.
`pc/wifi_link.py` provides `WifiLink` with `read`, `in_waiting`, `write`,
`close`, raising `OSError` when the session drops, so the main read loop runs
unchanged on either link. `send()` stays the only writer and still writes
exactly `raw` from `encode_checked` (the AST test in
`tests/pc/test_protocol.py` pins that and keeps passing). Serial-only steps
(the probe, the reset pulse, console echo, esptool flashing) run only when the
link is serial. `WifiLink.kind` is `"wifi"`; the serial path is `"usb"`.

### 7.2 Choosing a link (USB preferred)

Today `main()` waits for a serial port in four places, and every one of them
blocks until a port exists: `wait_for_port()` before the reconnect loop
(`claude_usage_bridge.py:1003`), the "nothing here answers" loop that polls
`next_port()` every 3 s (line 1098), `wait_for_port()` after an open failure
(line 1132: a busy or permission-denied port on the same machine, which is
exactly the unrelated serial device a charger-board owner may have), and
`wait_for_port()` after a disconnect (line 1450). A board on a charger
never produces a port, so a WiFi attempt placed anywhere inside the loop
would never run. All four are replaced by `wait_for_link()`, which polls
serial and WiFi alternately and returns whichever answers; a test asserts
that no other call to `wait_for_port` or bare `next_port()` wait survives in
`main()`:

1. Serial first on every poll, exactly as today: remembered port, then
   candidates, probe.
2. On the same poll, when serial has no candidate and a pairing file exists:
   connect to the remembered address (3 s timeout), else broadcast discovery
   (three queries a second apart). A WiFi failure waits out a backoff of
   5 s doubling to 60 s before the next WiFi attempt; serial is still polled
   every 3 s in between, as `wait_for_port` does now.
3. While on WiFi, every pass checks `usb_topology()`. A change with a
   candidate port closes the WiFi link and goes back to step 1. The board
   independently drops the session when its USB host speaks.
4. After a session drops, reconnect immediately once, then back off.
5. After the daemon has streamed a WiFi OTA and sent `ota_end`, it expects
   the board to be away for the swap (measured in minutes on an encrypted
   part, sysbuild.conf) and polls the remembered address every 5 s for up to
   10 minutes with no backoff growth, because the test boot's 90 s health
   deadline (`main.c`, `ota_boot_pump`) starts when the new image boots and
   is met only by usage arriving; a daemon at the top of its backoff could
   miss it and the board would revert a good image.
6. `BLINK_FORCE_WIFI=1` (D31) skips step 1 entirely.
7. **A candidate that keeps failing does not keep its turn (D51).** Today
   an explicit `--port` is returned on every call (`claude_usage_bridge.py:
   984`) and an open failure (busy, permission denied, line 1132) retries
   the same port after 3 s forever, which is right for a desk with one
   board and wrong for a charger board next to a serial device that is
   never ours. `wait_for_link` keeps a per-port failure record: a port whose
   open or probe failed is left out of the serial candidates for 5 s, then
   10, 20, 40, 60 s, reset when it answers or when the USB topology
   changes; on a poll where every candidate is excluded, step 2 runs. The
   deduplicated "busy" and "permission denied" sentences stay, printed once
   as today.

A handshake that fails after M1 (M2 rejected, M3 wrong, or the peer closes)
is an **unauthenticated** failure (D36): a stranger at a stale address can
produce exactly that by answering a public M1 and closing, so it proves
nothing about keys. The outcome word is `"failed"`, the daemon falls through
to discovery on the same attempt and then to ordinary backoff, and logs
`[wifi] handshake refused` once per backoff cycle. The sentence "the board
was set up from another computer or forgot this one; plug it in and open
blink settings" is printed only on USB evidence: a `wifi_state` whose `kid`
differs from the pairing file's (section 7.3, 8.1).

### 7.3 The pairing file

`~/.blink/wifi_pair.json`. The secret is never on disk in a file another
account could have opened first, on either platform:

- **POSIX**: the temp file is created in the same directory with
  `os.open(..., O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0o600)` (the mode
  is set at creation, under the umask, so there is no window at 0644), the
  JSON is written through that descriptor, then `os.replace`. `load()` opens
  with `O_NOFOLLOW`, `fstat`s the descriptor it will read, and refuses the
  file unless it is a regular file, owned by `os.getuid()`, with no group or
  other bits, in a parent directory that is not world-writable; it says so
  once with a fixed sentence and treats the board as unpaired.
- **Windows** (D48): tightening anything after creating it leaves a window:
  another account under permissive inherited permissions can open the new
  object, keep the handle (a DACL change does not revoke one), and with
  `WRITE_DAC` on a directory handle restore inheritable access after our
  check has passed. So nothing is tightened after the fact: the directory
  is created **with** its protected descriptor. `~/.blink/private\` is made
  by `CreateDirectoryW(path, &sa)` where `sa.lpSecurityDescriptor` comes
  from `ConvertStringSecurityDescriptorToSecurityDescriptorW` on the SDDL
  `O:<SID>G:<SID>D:PAI(A;OICI;FA;;;<SID>)` (owner, group and the one ACE are
  the user's SID; `P` makes it protected from inheritance), all through
  `ctypes` on advapi32 and kernel32, which is standard library. The user's
  SID comes from `GetTokenInformation(TokenUser)` on the process token, not
  from a name. `ERROR_ALREADY_EXISTS` is not success: an existing directory
  is validated, never adopted. Validation, run before every write and every
  load, reads the directory's owner and DACL with `GetNamedSecurityInfoW`,
  converts them back to SDDL with
  `ConvertSecurityDescriptorToStringSecurityDescriptorW`, and requires the
  owner SID to be the user's and the DACL to be exactly the one ACE above;
  `GetFileAttributesW` must not report `FILE_ATTRIBUTE_REPARSE_POINT`. A
  directory that fails (someone else's, or re-opened to inheritance) is
  refused with a fixed sentence and the board is treated as unpaired: the
  daemon never writes into a directory it did not verify. The directory is
  defence in depth only (D53): `~/.blink` is not a trusted parent
  (`pc/cli.py:3091`'s `chmod` is POSIX-only), so another account with
  delete-child rights there could replace the validated directory between
  our check and a creation by pathname. The guarantee is therefore on the
  file and on handles, never on names:
  - **write**: `CreateFileW(tmp, GENERIC_WRITE, 0, &sa, CREATE_NEW,
    FILE_ATTRIBUTE_NORMAL)` with `sa` built from the same SDDL
    (`O:<SID>G:<SID>D:P(A;;FA;;;<SID>)`, protected, so no inheritance from
    whatever directory the name resolved to). Before any byte: validate
    **the handle** with `GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER |
    DACL)` (owner SID is the user's, DACL is exactly that one ACE),
    `GetFileInformationByHandleEx(FileAttributeTagInfo)` (no reparse
    point), and `GetFinalPathNameByHandleW` (the resolved path lies inside
    `\\?\<home>\.blink\private\`). The handle is opened with `GENERIC_WRITE |
    DELETE` and `FILE_SHARE_DELETE`, and it stays open from creation through
    the rename and any cleanup: write and flush through it
    (`msvcrt.open_osfhandle`, without closing the OS handle), then rename
    **by handle** with `SetFileInformationByHandle(FileRenameInfo,
    ReplaceIfExists)` to the final name, and only then close. No
    `MoveFileExW` by name at all. Two failures, two outcomes: a
    **validation** failure (owner, DACL, reparse, final path) happens before
    the first byte, so nothing is ever written; a **move** failure (a
    directory swapped under us: the source name no longer resolves to our
    file) happens after the bytes are in the temp file, so the write is
    reported as failed and the temp file is deleted through the same
    still-open handle (`SetFileInformationByHandle(FileDispositionInfo)`,
    which the `DELETE` access right permits) before the close: deletion by
    handle, not by a name an attacker may have moved. The handle's lifetime
    is one object: create, validate, write, rename-or-delete, close. Either way no readable copy of the secret is left where
    we did not put it.
  - **load**: `CreateFileW(final, GENERIC_READ, FILE_SHARE_READ, NULL,
    OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT)`, then the same handle
    validation (owner, exact DACL, not a reparse point, final path inside
    the private directory), then read through the handle. A file whose
    owner is not the user cannot be forged into passing: setting another
    account's SID as owner needs `SeRestorePrivilege`.
  - on POSIX the same shape: `O_CREAT | O_EXCL | O_NOFOLLOW`, `fstat` of the
    descriptor (regular file, our uid, 0600), write, `os.replace`; load
    opens `O_NOFOLLOW`, `fstat`s, reads the descriptor.
  `docs/windows-check.md` gains the manual checks: a second local account is
  refused at the directory, and a `private` directory pre-created by that
  account is refused by the daemon with the fixed sentence. `pc/cli.py`'s
  install `chmod 0o700` of `~/.blink` is POSIX-only (its comment says so);
  nothing here relies on the parent. On POSIX the same directory is used
  (0700 at `mkdir`, checked by `fstat` of an `O_DIRECTORY | O_NOFOLLOW`
  descriptor: owner, mode, directory) so the two platforms share one layout
  and one set of tests.
- The same protected-create helper writes `settings_request.json` (section
  8.2): its token is short-lived and single-use but it does grant a browser
  session on the settings page, so it is a secret for as long as it exists.

Contents:

```json
{"v": 1, "board_id": "20500d342b68", "key": "<64 hex>", "kid": "<8 hex>",
 "ip": "192.168.1.23", "port": 49731}
```

Path resolved inside functions only (`tests/test_sandbox.py` enforces it).
`blink uninstall` removes it. The key is generated with `secrets.token_bytes(32)`
when the owner presses Connect, and the file is written only when the board
answers `wifi_state` with `st:"up"` and the matching `kid`.

### 7.4 Crypto on the PC

`pc/wifi_crypto.py`, standard library only: ChaCha20 block and stream,
Poly1305 with Python integers, the RFC 8439 AEAD construction, HKDF-SHA256 from
`hmac`/`hashlib` (RFC 5869), and `hmac.compare_digest` for every tag.
`aead_open` verifies the tag first and decrypts only after it matched, so no
plaintext of a forged frame is ever produced. Tested against RFC 8439
sections 2.3.2, 2.4.2, 2.5.2, 2.8.2 and appendix A.5, and RFC 5869 test
cases 1 to 3, plus boundary lengths the vectors do not reach (0, 1, 63, 64,
65, 511, 512, 517 bytes) checked for round trip and for failure on every
single flipped bit. The hardware interop probe (plan Task 2.6) is the
differential test against mbedTLS on the real board. The board answers no
host request with a reply (`hello` goes out at boot and nowhere else, and
`proto.c` handles an incoming `pong`, not an incoming `ping`), so the probe
uses the one property the design guarantees: a frame the board cannot open
closes the session (section 5.4). PC -> board: a `pong` padded to every
plaintext length from the shortest to 512, in one session, followed by a
517-byte `O` frame that the board refuses at the OTA layer (no `ota_begin`)
but opens at the frame layer; the session is still open at the end and the
board's next `ping` arrives, so every frame opened. Board -> PC: the
board's `ping` every 10 s, its `hello` and its `wifi_state`, 500 frames at
distinct counters opened by the PC, plus the board's boot self-test on the
RFC 8439 2.8.2 vector, which is the same vector the PC's test opens. The
board emits no variable-length line a probe can ask for, so board -> PC is
not swept by length on hardware. It is swept off hardware instead: a
committed generator (`tools/gen_aead_vectors.py`, built on
`pc/wifi_crypto`) writes a C header of seal results for every plaintext
length 0..517, several counters, and corrupted ciphertext, AAD and tag
cases, and a host test compiles `wl_crypto_mbedtls.c` against the
workspace's own mbedTLS 3.6 sources (portable C; `check_host_tests.sh`
gains the include path and the seven library files it needs) and requires
mbedTLS to seal to the same bytes and to refuse every corrupted case. Same
vectors, both implementations, both directions, no production echo. The
sources come from `BLINK_MBEDTLS_SRC` when set, else from
`tools/lib_zephyr.sh`'s workspace at `modules/crypto/mbedtls` (the path
`west.yml:320` provisions; this Mac also has a `mbedtls-3.6` copy, which is
tried second); CI's `host-tests` job
(`.github/workflows/installer.yml:248`, which checks out only this
repository) fetches the exact module Zephyr 4.3.1 pins,
`zephyrproject-rtos/mbedtls` at `11c0f18ab888fed4363eeeb43fd734246dc3c93b`
(`west.yml:318`, version string 3.6.6), as a tarball with a pinned sha256,
and sets `BLINK_MBEDTLS_SRC` and `BLINK_REQUIRE_MBEDTLS=1` so a missing
source fails there rather than skips; a developer machine with neither
skips that one suite with one line and every other suite runs as today.
Pure-Python ChaCha20 runs at a few hundred KB/s, which is plenty for usage
frames and makes a 1.3 MB OTA image a few seconds of CPU. The timing
property of the Python Poly1305 is accepted in section 4; no stdlib
primitive exists to replace it, and `pc/` takes no dependency (CLAUDE.md).

### 7.5 What the daemon logs

`[wifi] connecting to the board`, `[wifi] connected`, `[wifi] board not found`,
`[wifi] handshake refused`, `[wifi] link lost`, `[wifi] paired`,
`[wifi] forgotten`. Never the key, the password, an SSID, an address, a nonce,
a tag, a byte count of a frame, or any part of a frame. Exceptions raised from
`pc/wifi_link.py` and `pc/wifi_crypto.py` carry fixed messages, never buffer
contents (a `ValueError(f"bad frame {data!r}")` is exactly the bug to avoid).

The `BLINK_TAP` transcript (`Tap` in `claude_usage_bridge.py`) has three
record paths, and all three are redacted (D39): `tx` records whole outbound
dicts, `rx` whole inbound dicts, and `console` every raw line the board
printed, protocol lines included (its docstring says so). One function,
`protocol.redact(msg)`, keeps only `t` for `wifi_set`, and for `wifi_net`
and `wifi_state` keeps `t`, `st`, `link`, `sec`, `why`, `i`, `n`, `kid` and
drops `s`, `p`, `k`, `ip`, `port`, `r`. `Tap.tx` and `Tap.rx` pass through
it. The console path and `tools/console_tail.py` share one function,
`logbook.tap_console_line(line)` (D49), because they are the same thing seen
from two ends of the cable:

It is an allowlist, as CLAUDE.md requires of the board's console, and
nothing passes because of what it lacks. Round 2's "no brace or quote means
printk" rule was wrong: the deleted firmware printed an unquoted SSID
(`1e8abe5^:cfg_store.c:363`, and `tests/pc/test_console_allowlist.py:66`
keeps that very line as a fixture), `proto.c:882` echoes the daemon's error
text into `[proto] ota failed: ...`, and reset noise is arbitrary. So:

- a whole line that parses as a JSON object whose `t` is in
  `protocol.WIRE_TYPES` is recorded as the encoding of
  `protocol.redact(obj)`, and `redact` is a **per-type field allowlist**
  (D54), not a per-type exception list: `protocol.TAP_FIELDS` maps every
  wire type to the fields a transcript may hold (`ping` and `pong`: `t`
  only; `usage`: the keys `frame_to_usage` emits, which the fleet reads
  (`session_pct`, `weekly_pct`, `tests/fleet/agent.py:722`); `wifi_set`:
  `t`; `wifi_state`: `t`, `st`, `link`, `kid`, `why`; and so on, one line
  per type). A field not in the type's set is dropped, a value that is not
  a scalar (`str`, `int`, `float`, `bool`, `None`) is dropped, a string in
  an enum field (`st`, `link`, `sec`, `why`, `kid`'s hex shape) that is
  not one of the enum's values is dropped, and a string longer than 64
  characters is dropped. `{"t":"ping","k":"SECRET"}` is recorded as
  `{"t":"ping"}`. An object of a type not in `WIRE_TYPES` is `(board
  line)`. A per-builder table test pins what each builder keeps and what it
  deliberately loses (`v` everywhere, `welcome.caps` as a list, `wifi_set`'s
  secrets), and a second test pins the fields the fleet reads.
- every other line goes through `logbook.console_line`, the allowlist
  `bridge.log` already uses: a known message becomes its constant text (so
  `[proto] ota failed: <anything>` becomes `[proto] ota failed`, the stem
  at `logbook.py:344`), a known tag on anything else becomes the bare tag
  (`[usage] sess{"t":...` becomes `[usage]`), and the rest is
  `logbook.BOARD_LINE`;
- the two fleet markers are synthesized, not copied: a line that
  `console_line` recognises as `[usage] session` is recorded as
  `[usage] session applied` or `[usage] session applied STALE` (the word
  `STALE` present as a token in the original), because `tests/fleet/
  tap_asserts.py` needs only `"[usage] session "` as a substring (line 62,
  143) and `"STALE"` in the line (line 258), never the numbers;
- the fixed strings the charger run needs as evidence are not printk lines
  of ours, and `tests/pc/test_console_allowlist.py:134` requires every
  `CONSOLE_MESSAGES` entry to be the stem of a printk in `firmware/src`. So
  they live in their own list, `logbook.CONSOLE_EXTERNAL`, consulted by
  `console_line` after `CONSOLE_MESSAGES` and audited by its own test
  (each entry names its origin: the ESP32 ROM's reset line prefixes
  `rst:0x1 (POWERON_RESET)`, `rst:0xc (SW_CPU_RESET)`, `rst:0xf
  (BROWNOUT_RESET)`, `rst:0x10 (RTCWDT_RTC_RESET)`, `rst:0x7
  (TG0WDT_SYS_RESET)`, `rst:0x8 (TG1WDT_SYS_RESET)`, and Zephyr's `***
  Booting Zephyr OS`); a prefix match keeps the reason and drops the rest
  of the ROM line. The `[wifi]` lines of section 5.8 join
  `CONSOLE_MESSAGES` in the task that adds each printk, never before, so
  the provenance test stays true at every commit.

The tap is the bench and fleet transcript under `BLINK_TAP`, written into
the run's sandbox home and never into `bridge.log`; it recorded printk
lines verbatim before this feature, and this rule cuts them to what
`bridge.log` would show. There is no raw mode anywhere.

`blink status` gains a line: `Link        USB cable` or `Link        WiFi,
board at 192.168.1.23` or `Link        WiFi set up, board not found`. On the
customer's own screen, so the address may appear. There is no daemon status
record today (`cmd_status` reads `backend().status()` and `read_desk()`,
which are the service and the Desktop paths), so the daemon gains one:
`~/.blink/link.json`, `{"link": "usb"|"wifi"|"none", "ip": ..., "t":
<epoch>}`, written atomically on every link change and every 60 s, read by
`cmd_status` only if `t` is under 3 minutes old, else `Link        unknown
(app not reporting)`.

---

## 8. The settings page

### 8.1 Panels (D3 revised; section 16 is the authority)

`pc/theme_builder.html` is rebuilt to the approved design. One file plus
two font files, no external fetches. The old tab plan is withdrawn; what
each control holds:

- **Look**: both sides of the board. Left: Themes (the 12 built-ins) and
  Logo (section 15.2). Right, "Customise": Colours (today's builder's
  colour and width controls), Screen (View, Focus, Main provider, section
  15.3) and Try it (the page-only preview sliders and the sweep).
- **Connect**: the status card, "Move to WiFi" / "Change network", the
  network list the board heard (scan), the password field, the handshake
  progress in the status line, success and failure sentences, all as the
  design spec's section 4 words them. Disabled with "Plug the board in with
  its USB cable to set up WiFi." when no board is on USB, because pairing is
  USB only.
- **Board**: "This board" (name, brightness, firmware, the Open settings
  note with "Show it on the board", Details with address, saved WiFi and
  Forget WiFi behind the in-page confirm) and "This computer" (usage source
  with the multi-environment item of Q3, service incidents, self-update,
  and the two "New" rows of D59, disabled until their branches land).

### 8.2 Where the page runs (D25)

The daemon owns the serial port, and the password must reach the board without
touching disk, so the page that takes it runs inside the daemon, on the same
`Launcher` that "Edit on computer" already uses.

- **Edit on computer** (board's `theme_edit`): as today, opens the page on the
  Look control open.
- **`blink settings`** (D37): the CLI writes `~/.blink/settings_request.json`
  (owner-only, section 7.3) holding `{"panel": "connect", "k": "<one-time
  token>"}`. The daemon picks it up in `_upkeep`, deletes it, and either
  starts the page server **headless** with that token pre-issued or, if a
  server is already up (a builder the board opened), registers the token on
  it; then it writes the server's port record. **The daemon never calls the
  browser opener for a request**: `Launcher.launch()` holds its lock across
  the opener, which has been measured stuck for 28.7 s
  (`theme_server.py`, `ended()`'s docstring), and on an opener failure it
  tears the fresh server down, which would also defeat the printed link.
  The request carries a random `id` as well as the token, and the daemon
  writes the builder record (`pc/theme.py`, `write_builder`, today
  `{"port": p}`) as `{"port": p, "ack": id}` **after** the token is
  registered (D50): a builder the board opened earlier leaves a port record
  behind, and a CLI that trusted the record alone could open its link
  before the daemon had registered the token and get a 403. `read_builder`
  keeps returning the port and ignores keys it does not know. The CLI waits
  up to 10 s for a record whose `ack` is its own id, opens the browser
  itself (a stuck opener stalls the CLI, where the owner is watching, not
  the serial loop) and prints the link with the token for a desk where no
  browser opened.
  The file never holds a password or a key; the token is single-use and
  expires with the page. If no daemon picks it up, the CLI removes the file
  and says `The app is not running. Run blink install, or blink run in
  another terminal.`
- **End of `blink install`**: nothing opens (D4, owner 2026-11-06). Install
  prints one line, `To set up WiFi and the display, press Open settings on
  the board or run: blink settings`, and that is all.
- **The session's life (D67)**: the settings page's server is created
  `persistent`: a theme save writes the save line and stops the live
  preview, as today, but does not end the server, because Connect and Board
  live on the same page and a person saves a theme and then sets up WiFi.
  The session ends when the page closes, after `IDLE_S` with no request of
  any kind, or on the page's Done; each end writes the cancel for an
  unsaved preview exactly as today's close and idle do.
- **`blink theme`**: unchanged, foreground, the Look control only (D26). Its save still ends its server.

### 8.3 Security

Everything `pc/theme_server.py`'s docstring lists still holds: 127.0.0.1 only,
ephemeral port, one-time token exchanged for an HttpOnly SameSite=Strict
cookie, Host header must be `127.0.0.1:<port>` or `localhost:<port>`, nothing
logged. Added for the WiFi endpoints:

- `Origin` is **required** (not merely checked when present) on every
  `POST /api/wifi/*`, and must equal the server's own origin; `null`, absent
  and foreign origins are all 403 (D35). `GET /api/wifi`, `GET
  /api/wifi/nets` and `GET /api/device` keep the cookie and Host checks only:
  browsers commonly send same-origin GETs without an Origin header, and a
  page that cannot read its own state is a page that looks broken.
- `Content-Type: application/json` is required on the POSTs, so a
  cross-origin form post cannot reach them without a preflight the server
  refuses.
- The password lives in the request body, is passed to the serial loop through
  an in-memory queue, is encoded into one `wifi_set` line, and every reference
  is dropped. It is never written to a file, a log, the tap, an exception or
  the page state. Python cannot zero a string, so "dropped" is the honest word.
- The join endpoint answers when the board answers (`wifi_state`), up to
  30 s, and then after the one-shot verify (5.4), run from a worker thread
  so the serial loop never waits; it returns the steps that really happened.

Endpoints: `GET /api/wifi` (state), `POST /api/wifi/scan`,
`GET /api/wifi/nets`, `POST /api/wifi/join` (`{"s": "<ssid hex>", "p": "<password>"}`),
`POST /api/wifi/forget`, `GET /api/device`.

### 8.4 Threading

The HTTP server is threaded; the serial loop is the only writer. Page requests
put an op on a `queue.SimpleQueue`; `Bridge.pump()` drains it, sends the
`wifi_*` line, and routes the board's answers back through a reply slot the
handler waits on with a timeout. The pattern is the one `theme_answers`
already uses. No page request ever waits while holding a lock the serial loop
needs (`theme_server.Launcher.ended`'s docstring records the 28.7 s browser
opener that made this a rule).

---

## 9. OTA over WiFi (D12)

Over USB nothing changes: the daemon writes slot 0 with esptool after the
`FLASH_CRYPT_CNT` check.

Over WiFi the board writes its own slot 1, which is what `ota.c`'s
`flash_img`/`stream_seal` half already does (it was the deleted mode's path):

1. `ota_query` / `ota_avail` / consent `ota_flash`, as today. The board sets
   `OTA_SRC_WIFI`, so the progress screen's existing "Over WiFi" wording and
   percentage apply.
2. **Unconfirmed image refuses (D38).** If the running image is still in its
   test boot (`ota_test_boot` in `main.c`), `ota_begin` is answered with
   `ota_ack` carrying `err:"unconfirmed"` and nothing is written: slot 1
   holds the only way back, and a second image would erase it.
3. **Ordering (D34).** The daemon sends `ota_begin` and waits for `ota_ack
   off:0`, the "ready" ack the board sends after `ota_net_begin()` has
   initialised the flash context and the hash on the main thread. Only then
   do `O` frames flow, 512 bytes each, in order, through the same ring as
   the lines, so a chunk can never be handled before the `ota_begin` that
   precedes it, and `ota_end` cannot overtake the last chunk. All OTA state
   stays on the main thread, which `ota.c` already assumes ("single worker
   thread; no locking"). **Acks (D45):** the board sends `ota_ack off:<n>`
   whenever the accepted byte count crosses a 16 KiB boundary **and** when
   it equals the manifest's `size`, so a stream whose length is not a
   multiple of 16 KiB (every release image: `tools/release.sh:226` keeps
   the OTA artifact unpadded) still gets a final ack. `off` counts bytes
   accepted into the stream (hashed and handed to
   `flash_img_buffered_write`), not bytes on flash: the writer keeps a
   partial block in RAM until `stream_seal` flushes it (`ota.c:460`), and
   the daemon does not need to know. The daemon keeps at most 32 KB
   unacknowledged and sends `ota_end` on the ack whose `off` equals the
   size; the board's `ota_end` handling then flushes, checks the length and
   hash, and seals. Tests on both ends use sizes that are not multiples of
   512 or 16384.
4. **Daemon side (D43).** `Bridge._on_ota_flash` today calls `self._flash()`
   synchronously from message dispatch (`pc/bridge.py:697`), which is fine
   for esptool but would deadlock a windowed stream: the acks that open the
   window arrive through the dispatch that is blocked. Over WiFi the handler
   instead creates a `WifiOtaStream` and the daemon's read loop pumps it
   (`stream.pump(link)` sends up to the window; `stream.ack(off)` from
   `ota_ack` handling), so pings, pongs and acks keep moving; `stream_seal`
   is requested with `ota_end` when the last chunk is acked.
5. **Image bound (D32).** Every chunk is checked on the board: `off` must
   equal the bytes received so far (no gaps, no rewinds) and `off + n` must
   not exceed `OTA_IMAGE_MAX`, a build-time constant derived from the
   deployed MCUboot configuration: slot 1 size minus the swap-move padding
   sector minus the sector-aligned trailer (`app_max_size()` in
   `swap_move.c:567`). The trailer is not one sector: with
   `CONFIG_BOOT_MAX_IMG_SECTORS=512` (`sysbuild/mcuboot.conf`) and a 32-byte
   write block (`sysbuild/mcuboot.overlay`), the swap-status area alone is
   512 x 3 x 32 = 49152 B, the trailer about 49312 B, aligned up to 53248 B.
   So the bound is 1376256 - 4096 - 53248 = **1318912 B**, not the 1372160 B
   sysbuild.conf quotes (which forgets the trailer) nor round 1's 1368064
   (which took the trailer for one sector). A 1.2 MB release still fits.
   Phase 0 confirms the number from the MCUboot build and `tools/release.sh`
   refuses a signed image above it.
   This keeps a daemon from writing MCUboot's trailer: a trailer with
   `image_ok` set makes a swap permanent and skips the test boot
   (`bootutil_public.c:128`).
6. **No-downgrade**: the first chunk carries the MCUboot image header. The
   board reads `ih_magic` and `ih_ver` and refuses unless the version is
   strictly newer than the running one and equals the version in the
   `ota_avail` it consented to. MCUboot's own downgrade prevention cannot be
   used: the bootloader on shipped boards cannot be updated in the field.
7. `ota_end`: the board checks length and SHA-256 against `ota_avail`
   (`stream_seal`), calls `boot_request_upgrade(BOOT_UPGRADE_TEST)`, writes the
   `cfg_set_ota_state` breadcrumb, and reboots. A session drop, a USB
   takeover or a replacement session during the stream calls
   `ota_net_abort()`: the flash context is dropped, nothing is sealed, the
   panel returns with `Update failed. The current version keeps running.`,
   and the next offer starts from zero.
8. MCUboot verifies the ECDSA-P256 signature and swaps (swap-using-move). The
   test boot, the 90 s health deadline and the revert in `main.c` already
   exist for this. `ota_health` is set from `usage_view_have_data()`, which
   is link-agnostic, so usage over WiFi counts (the comment at `main.c:114`
   says "over USB" and is updated). `boot_write_img_confirmed()`'s return
   value is checked (today, `main.c:154`, it is not): on failure the board
   logs `[ota] confirm failed`, keeps the watchdog and retries on the next
   pump until the deadline, so a confirm that never lands ends in the revert
   it would have ended in anyway rather than a false `[ota] image confirmed`.

The daemon holds `no_exit_while("firmware write")` across the stream, as it
does across an esptool write. The swap time on an encrypted part is measured in
Phase 5 and the progress screen states it.

**What the shipped bootloader needs proving (hardware-gated).** `tools/burn.sh`
writes MCUboot together with the app (`burn.sh:400`) and refuses fused boards
(`burn.sh:228`), so a `burn.sh` run proves nothing about the bootloader
customers already have. The plan's Task 5.5 therefore starts from a board
flashed with the **released** 1.4.x artifacts, takes the WiFi image over USB
(slot 0, application only, as `pc/ota.py:340` does), then a newer image over
WiFi, then a forced revert (an image built with
`-Dfirmware_CONFIG_BLINK_TEST_NEVER_HEALTHY=y`,
which never sets `ota_health`), then a USB write of slot 0 while slot 1
still holds the older image from the WiFi swap (USB -> WiFi -> USB).

The fused unit cannot run that sequence as written, and round 2's version
of it was circular: the released 1.4.x image has no radio (`v1.4.1:
firmware/prj.conf:219`, "No networking"), so it can neither pair nor take an
update over WiFi; `pc/ota.py:312` refuses every USB write to an encrypted
chip ("Encrypted chip; needs the factory tool"); and
`tools/flash_encrypted.sh:135` writes MCUboot together with the app. The
honest sequence (D52) is:

1. Flash the fused unit with the **released tag** built in its own worktree,
   through `BLINK_BUILD_DIR=<that build> tools/flash_encrypted.sh`. That is
   the state a fused customer board is in: the release's bootloader and
   the release's app. Record `sha256` of that build's
   `mcuboot/zephyr/zephyr.bin`.
2. Install the first WiFi-capable image **application only**:
   `tools/flash_encrypted.sh --app-only`, a new flag (plan Task 5.3) that
   runs the same `espsecure.py encrypt_flash_data --keyfile` step
   (`flash_encrypted.sh:114`) on the confirmed app alone and writes
   `0x20000` only, never `0x1000`, and never runs the logo `erase_region`
   the default path runs (`flash_encrypted.sh:150`): a unit's logo is the
   unit's. After it, read back `0x1000` to a file
   with `esptool.py read_flash` and compare its sha256 with step 1's
   ciphertext written there (both are ciphertext of the same bytes at the
   same address, so they match byte for byte): the bootloader is proved
   untouched.
3. Pair over USB; WiFi OTA to a newer WiFi build (Task 5.5 step 2's
   criteria); forced revert with the never-healthy image (the path the
   encrypted-erase patch exists for); WiFi OTA to a newer image again.

What fused customers will experience, stated in section 12: a fused board
already on a desk cannot take this release by itself, over USB or over the
air, because the image it runs has no radio and the daemon will not write
an encrypted chip. It needs the factory tool once, which means the board
comes back or the owner ships the tool and the key, which this design does
not do. Whether `esptool.py write_flash --encrypt` (the ROM encrypting with
the chip's own key, no key file on the PC) works on these units depends on
the `DISABLE_DL_ENCRYPT` eFuse, which nobody has measured; Task 5.5 step 5
tries it once on the fused unit and records the answer, and Q10 asks the
owner what to do with it. Power-cut
tests during the transfer, the swap and the confirmation run on both
boards, at maximum brightness with radio traffic on a weak supply, because
flash writes run with caches and interrupts off (`cache_utils.c:77`),
`prj.conf:108` records one cache-unsafe interrupt path already found on
this board, and on the fused part every write is an erase-then-rewrite
(the encrypted-erase patch, line 115), which widens the window. Three
Kconfig knobs, default n and refused by `release.sh`, make the other
failure windows reachable: `CONFIG_BLINK_TEST_NEVER_HEALTHY` never sets
`ota_health`, `CONFIG_BLINK_TEST_CONFIRM_FAILS` makes
`boot_write_img_confirmed()` report failure, and
`CONFIG_BLINK_TEST_HANG_AFTER_BOOT` spins with interrupts off after the
first panel, so the watchdog revert is exercised. They are set the way this
repository sets every sysbuild Kconfig symbol
(`-Dfirmware_CONFIG_BLINK_TEST_...=y`, the form `firmware/Kconfig:65-69`
documents for the self-test knob; a bare `-DBLINK_TEST_...=1` is a CMake
variable that reaches no `CONFIG_` symbol), guarded in C with
`IS_ENABLED(CONFIG_...)`, and each prints a fixed `[test] <knob>` boot line
so the binary carries a marker the guard can grep for. `tools/burn.sh` refuses such a build too (it signs confirmed, so there is no revert), unless `BLINK_BENCH_KNOBS=1` is set. Before every trial
the `.config` is grepped for the symbol and the console for the marker. The cache-call-path review is a step with a command and a pass
criterion in the plan (Task 5.5), not a sentence. The deleted mode's
download did write slot 1 while receiving over WiFi (ota.c records a 416 s
transfer), so the combination has run on this hardware before; what has
not been measured is losing power in the middle of it.

`ota.c`'s HTTPS half (`https_get`, `fetch_follow`, `ota_check`, `certs.h`) is
deleted in Phase 5. With a network stack in the image it would be a linker
decision away from a board that fetches from GitHub by itself, which is the
standalone mode by another name.

---

## 10. Release guards

`tools/release.sh` keeps both existing guards (the old symbol and the
`/api/oauth/usage`, `refresh_token`, `claude-code/` strings). Added:

- An image with `CONFIG_WIFI=y` must have `CONFIG_BLINK_WIFI_LINK=y` and
  must not have `CONFIG_DNS_RESOLVER`, `CONFIG_HTTP_CLIENT`, `CONFIG_SNTP`,
  `CONFIG_NET_SOCKETS_SOCKOPT_TLS` or `CONFIG_ESP32_WIFI_AP_STA_MODE`.
- The artifact must not contain `api.anthropic.com`, `console.anthropic.com`,
  `claude.ai` or `/releases/latest/download/`.
- The comments that say the radio left the image are rewritten to say what is
  in it now.

`tests/pc/test_release_gate.py` pins the new checks by running the guard
function against generated `.config` and binary fixtures.

---

## 11. RAM and the Phase 0 gate (D17)

Known numbers: dram1 is 98304 B and held 55556 B at f107c0c plus the 2048 B
the LVGL pool regained on 2026-10-28, so about 40 KB is free there. dram0 is
full ("growing the pool past 16 KiB fails to link"). The LVGL pool is 32768 B
(32680 usable) and the worst measured screen, popups_touch, left 2320 B at the
old 30720 size. The system heap is 86016 B and was sized for the WiFi build.

The spike is not an echo toy: an echo loop with a cipher in it measures the
driver and nothing of what the feature allocates. It builds the radio, IPv4,
TCP, UDP and **the production shapes of the Phase 2 code** into the current
image on the bench board, with no TLS: the real M1/M2/M3 handshake and HKDF
(the `wl_hs`/`wl_frame` files are written in the spike and carried into
Phase 2 as they are), the frame ring and TX ring at their intended sizes, a
second candidate connection replacing a live session, a UDP responder,
the OTA consumer **on the main thread exactly as production runs it**
(`O` frames through the frame ring, drained by `proto_service`, hashed
with `mbedtls_sha256_update` and written with `flash_img_buffered_write`
into slot 1 from a throwaway image of release size, abandoned, never
sealed; the network thread only moves bytes), and a fifth Settings tile
with the Connection screen's text. A spike that wrote flash on the network
thread would measure the wrong stack. The driver is driven from a throwaway PC script
built on `pc/wifi_crypto.py` (the first thing written, Task 0.1 in the plan,
because the spike needs it as the peer). It measures:

- link map regions (dram0, dram1, iram0, flash) for the unpaired build
  (driver initialised, interface down) and the paired build;
- LVGL pool peak on the popups script with the Connection tile in the grid
  and the network thread live, **with the session kept alive**: a bench
  that drives touches over USB must not look like a USB host, or section
  5.3 closes the session and section 9 aborts the OTA. So the stress runs
  `tools/board_touch.py --touch-only` (D55): the tool sends only the three
  injector types (`touch`, `ui_state`, `widgets`, the set
  `firmware/src/touch_inject.c` handles; `mem` is a `touch` op), refuses
  any other `WIRE_TYPES` line and any `fake` step, and the injector build
  dispatches those three before the host stamp in `dispatch()` (today
  `host_seen` is set at `proto.c:425`, before `touch_inject_dispatch` at
  `:441`), so `link_usb_heard` is never called for them. Only injector
  builds have that path, and `release.sh` refuses injector builds already.
  Two workloads, because the download overlay is full-screen and swallows
  every touch (`ui_settings.c:1018`), so nothing under it can be traversed
  while an OTA runs: `tools/bench/session_touch.txt` walks every screen and
  popup reachable without a `fake` injection (Settings, each tile, the
  Connection screen and its Forget confirm answered with Cancel, Theme
  until Phase 6 removes it, Brightness, back) with `mem` on leaving each,
  while the session is live; `tools/bench/download_touch.txt` runs during
  the OTA stream and only queries (`ui_state modal=download`, `widgets`,
  `mem` every 10 s for the length of the transfer). Memory numbers come
  from the injector's `mem` query and the board's `[mem]` lines, and the
  criterion is the worse of the two runs;
- system heap low-water **and largest free chunk** (`mem_stats.c`, whose
  comment explains why "free" alone is not allocation headroom) across
  join, scan, a 10-minute session, a session replacement and a 1.3 MB OTA
  stream into slot 1;
- stack high-water of **every** thread (`THREAD_ANALYZER`), not only the new
  one: the main thread now does the OTA writes;
- thread priorities written down: the network thread below main, above the
  idle and logging threads, so a flood cannot starve LVGL;
- resets during 30 minutes on a phone charger with the session live, read
  from a continuous `tools/console_tail.py` capture (D40), never from a
  reader that resets the board afterwards;
- the swap-move usable size, confirmed from the MCUboot build
  (`app_max_size`, section 9).

Pass, all of:

- the image links with the LVGL pool at 32768;
- popups_touch's pool peak with the Connection tile and the thread live
  leaves at least 2320 B, the margin prj.conf records as the tightest today;
- dram1 has at least 4096 B free and dram0 links;
- system heap low-water at least 8192 B free **and** largest free chunk at
  least 4096 B at the low-water point, with no driver allocation failure in
  the console across the whole run;
- every thread's stack below 75 % high-water (a larger stack is an admissible
  lever here, see the 2026-11-06 note below);
- no brownout or watchdog reset in 30 minutes on a phone charger at 11 dBm;
- the signed image at most 1.2 MB (usable 1318912 B, section 9).

The spike's branch is kept locally (never pushed, never merged), and
`docs/research/2026-11-wifi-ram-spike.md` records the commit, the `.config`,
the region table and the image sizes, so a later regression can be compared
with the gate instead of re-derived.

Outcomes:

- **Pass**: one image. WiFi code is in every build; the radio is dormant
  until the owner pairs.
- **Pass with levers**: apply, one at a time and each re-measured, in this
  order: trim net buffer counts; right-size `HEAP_MEM_POOL_SIZE` from the
  measured low-water; `LV_Z_BITS_PER_PIXEL=16` (gives back about 6 KB, doubles
  flush passes; full redraw must stay under 150 ms); `INPUT_QUEUE_MAX_MSGS`
  only as the last resort (it was raised to stop missed taps). If they get
  there: one image.
**Note, 2026-11-06 (Phase 0 verdict: Pass with levers, provisional).** The
lever list above answers a RAM shortfall: pool, heap and dram1. RAM passed with
no lever applied (dram1 12276 B free, heap low-water 38120 B free, pool 3400 B
left). The one failure was stack high-water: the logging thread at 94 % of 1024
B (89 % on today's plain image, so this is a finding about the current
firmware) and the system work queue at 78 % of 1024 B (the radio runs on it).
The only remedy for a stack high-water is a larger stack, so stack-size
increases are admissible levers for the "every stack below 75 %" criterion.
`LOG_PROCESS_THREAD_STACK_SIZE=1536` and `SYSTEM_WORKQUEUE_STACK_SIZE=1536`
were each applied singly and re-measured; each costs 512 B of dram1, and the
highest stack afterwards is `rx_q[0]` at 65 %. The verdict is provisional on
one criterion: the 30-minute phone-charger run was deferred by the owner to a
check before `feat/wifi-transport` merges, so a brownout there would mean
TX-power or power work, not RAM rework. The numbers and each criterion's row
are in `docs/research/2026-11-wifi-ram-spike.md`.

- **Fail**: a separate WiFi image, `blink-fw-wifi.bin`. This forks the OTA feed
  the way the edition comment in cfg_store.h warns against: the manifest names
  two images, `hello` must say which it runs (`caps` gains `"wifi"` only in the
  WiFi image), the daemon picks the asset by caps, and pairing first installs
  the WiFi image over USB. **Phase 1 does not start until the owner has chosen
  this path in writing**, because it is a product decision, not an engineering
  one.
- **Fail even as a separate image**: stop and report. The feature does not fit
  this board.

---

## 12. Limits (D16), stated to customers

- 2.4 GHz networks only (ESP32). A router with one combined name usually
  offers 2.4 GHz as well.
- WPA2 and WPA2/WPA3 mixed networks. Not WPA3-only, not enterprise/802.1X,
  not open networks (D22), not captive portals.
- Networks that isolate clients from each other (many guest and office
  networks) cannot work: the computer cannot reach the board.
- Changing networks needs the cable once.
- The board shows HOST LOST when the computer sleeps or leaves the network,
  as it does today when the cable is pulled.
- Someone holding the board can read the WiFi password and the pairing key
  from its flash (on every board; section 4 says why "fused" is not promised
  as protection), and with the key can read any session they recorded
  earlier: there is no forward secrecy. Forget WiFi before giving a board
  away; pairing again from the computer makes a new key.
- WSL2 in NAT mode cannot receive the discovery answer (broadcast does not
  cross the NAT); it relies on the remembered address (Q3, Q4).
- A board with flash encryption (a fused unit) that is already on a desk
  cannot get the WiFi feature by itself: its firmware has no radio and the
  app refuses to write an encrypted chip over USB. It needs the factory
  tool once (D52, Q10).

---

## 13. Proof

Firmware work is done when flashed and boot-verified, not when it builds.

- Host tests (`sh tests/ci/check_host_tests.sh`): handshake and frame parsers
  against short, long, late, replayed, reordered and tampered input; cfg_store
  migrations both directions; link arbitration.
- pytest (`.venv-test/bin/python -m pytest tests/ -q`) on macOS, Ubuntu
  (Docker) and Windows (ARM64 VM), each with its own count recorded.
- Board, bench: `tools/burn.sh` (FLASH_CRYPT_CNT checked, confirmed image),
  boot log, the crypto self-test line, pairing from the page, a session from
  the daemon, popups_touch memory report, the every-length ping probe.
- Shipped bootloader and fused unit, power cuts: the hardware-gated matrix
  in section 9, run as plan Task 5.5 with pass criteria written there.
- Desk: the board on a phone charger for at least 24 hours on the owner's
  network, through a computer sleep and wake, a router restart, and an OTA
  over WiFi from a locally served release (`BLINK_OTA_DIR`).
- Fleet per `docs/fleet-testing.md`, plus a `wifi_link` scenario run with
  `BLINK_FORCE_WIFI=1` (D31) and the board's console read by
  `tools/console_tail.py --tap <file>` (D40, D49), which appends records of
  the same shape `Tap.console` writes (`{"dir": "console", "t", "line"}`)
  into the run's tap file, so `tests/fleet/tap_asserts.py` reads the
  `[usage] session ` lines it reads today without a change to its
  contract. The fleet agent runs the daemon under a sandbox home
  (`tests/fleet/agent.py`, `sandbox_home()`), which has no pairing file, so
  the runner copies the desk's `wifi_pair.json` into that home's
  `private/` directory through the same helper when the scenario asks for
  the WiFi link.
- `docs/windows-check.md`: the ten-minute pass, plus pairing and a WiFi
  session from Windows (firewall prompt must not appear).
- `docs/README-full.md`: a WiFi section with the limits above.

---

## 14. Open questions for the owner

Settled by the owner on 2026-11-05 and recorded in section 1: encryption is
required (D11; Q1 closed); USB stays the default and WiFi is opt-in (D2); the
settings page is the approved design with Look, Connect and Board (D3, section 16); the board stays
minimal (D5); security is designed carefully (D6). The questions below are
still the owner's to answer; nothing in the plan decides them.

- **Q2. If Phase 0 fails: a separate WiFi image, or no WiFi?** Section 11
  lists what a second image costs.
- **Q3. Board panel, "This computer": "multi-environment on one PC".** WSL support
  (`blink install --wsl <name>`) exists on the local `feat/enterprise-spend`
  branch, not in this tree. Which of these is meant: (a) one daemon (Windows)
  that reads WSL's usage and feeds the board, with the Board panel choosing
  which environments it reads; (b) two daemons (Windows and WSL) that both
  know the key, one feeding the board at a time; (c) something else? (b)
  collides with "one session at a time" and D29 (the newcomer replaces the old
  session) and needs a rule for who wins.
- **Q4. WSL2 NAT discovery.** Is "remembered address, refreshed whenever the
  cable is plugged into the Windows side" enough, or should the Board panel let
  the owner type the board's address?
- **Q5. A fifth Settings tile, or the Connection screen as a row on the
  Update screen?** The plan measures the tile first; the owner may prefer one
  over the other regardless of memory.
- **Q6. Open networks.** Refused in v1 (D22). The session is encrypted either
  way, so they could be allowed; the question is whether to support them.
- **Q7. Ports 49731.** Any reason to prefer another number (a router rule, a
  conflict on the fleet desks)?
- **Q10. Fused boards in the field.** Section 9 says why a fused board on a
  desk cannot take this release by itself. Options: (a) accept it and say
  so in the release notes; (b) if Task 5.5 finds `write_flash --encrypt`
  works on these units, teach `pc/ota.py` to use it for encrypted chips (no
  key on the PC) in a later release; (c) offer the factory flash on return.
- **Q11. The board's remaining tiles.** With Theme gone (D56), do
  Brightness, Focus and Update stay on the board, move to the page, or
  both? The page shows Focus either way (D58); the plan keeps the three
  tiles on the board until the owner says otherwise.
- **Q13. Usage refresh and the doze switch.** Both are "new" on the mock
  and both depend on work outside this branch (`feat/usage-refresh`; the
  four-hour doze in `main.c`). Which release carries them, and does the
  doze switch mean "never doze" or a chosen number of hours? (Weekly
  scope, removed by D61, can be revisited here if a consumer for it comes
  back.)
- **Q14. A name for the board.** The design's Board panel has a "Name"
  field; nothing stores a board name today (the only `board_name` in the
  tree names themes). The page shows the board id there until the owner
  says whether a name exists, where it lives (the daemon's `config.json`,
  the board's record) and what it is for.
- **Q9. An outside review of `pc/wifi_crypto.py`?** The reviewers asked for
  an independent cryptographic review of the hand-written cipher before
  release. The plan tests it against the RFC vectors, boundary lengths and
  the board at every length (7.4), and section 4 states the accepted timing
  limit; whether to also commission a review is a cost the owner decides.

---

## 15. The settings page as the only place to configure (owner, 2026-11-06)

The owner reviewed the settings-page mock and settled three things on
2026-11-06: the board does not edit anything itself (D56), the page uploads
the company logo (D57), and the page shows the viewing preferences with
previews (D58). Two more items on the mock are dependencies, not design
(D59). This section is additive to sections 8 and 9; nothing above changes
except the Theme tile and the `theme_edit` path's name on the board.

### 15.1 Open settings (D56)

The Settings grid's Theme tile (`ui_settings.c`, `mk_tile(..., "Theme", ...)`)
becomes `Open settings`. A tap sends the board's existing `theme_edit`
request; the daemon's existing `Launcher` answers it as today (`ok 1` holds
the tile as `Opening on your computer` / `Editing on your computer`, `ok 0`
shows `On your computer, run: blink settings`), and the page opens on its
Look control open. The theme overlay (`show_theme`), the `theme` message's `op`s
that drive it, the `theme_now` echo and `theme_edit`'s "held" state keep
their code and their tests: the board still needs to know the saved theme
and to show the hold. What goes is the overlay's entry point from the
tile, so the board's own theme picker is unreachable. The 47-character
lines on the tile are the existing ones; nothing new is written for the
screen. The LVGL pool loses a screen, which is the direction prj.conf's
margin wants.

### 15.2 Company logo from the page (D57)

What exists: the logo partition at `0x330000`, `0x80000` bytes
(`esp32_devkitc_esp32_procpu.overlay:281`), the `BLGO` header and `BAN1`
blob (`logo_parse.h`), `logo_parse()` with its CRC, and `tools/encode_logo.py`,
which uses Pillow to load pictures and then pure Python (`quantise`,
`build`) to make the blob. The partition is written only by the factory
(`burn.sh --logo`), and `logo_parse.h`'s comment says nothing over USB can
write it. This section changes that last sentence, on purpose, with the
same boundary the OTA image has.

- **What is on the board today, unchanged.** The partition holds a 32-byte
  `BLGO` header (magic, version 1, `hold_ms`, data length, CRC-32,
  background RGB, zero pad; `logo_parse.h:21-31`, built at
  `encode_logo.py:134-144`) followed by `BAN1` data: frame 0 full, later
  frames as changed rects only, PackBits16 (`encode_bootanim.py:181-200`),
  0xFF-padded to 32, refused over 512 KB (`encode_logo.py:266`,
  `burn.sh:361`). `logo.c:33-47` maps it, `logo_parse.c:45-69` checks magic,
  version, length, CRC, canvas and at least one frame, and `ui_boot.c:186-206`
  plays it once after the boot clip and holds the last frame for `hold_ms`
  (skipped on an intentional reboot, `:233`). The board never stores or
  decodes a GIF, and nothing changes in that format or that player; what
  changes is who may write the partition and what `logo_parse` checks.
- **Formats (D60).** A still (PNG, JPEG, WebP, SVG, anything the browser
  draws) or an animated GIF. Today a GIF is an input to the factory tool
  only, and a lossy one: `burn.sh:342-356` sends `.gif` through ffmpeg like
  a video (the first 3 s at a fixed 15 fps, so the GIF's own frame timing
  is lost), `encode_logo.py:80-91` scales to fit 320x240 over a background
  colour, noise-gates (threshold 10, `:121-131`), converts to RGB565, and
  round-trip-decodes before writing (`:258-261`). The daemon's path keeps
  the scaling, the gate, the conversion and the round trip, and improves on
  the timing: `BAN1` has one `fps` for the whole clip, and its decoder
  treats a frame record with zero rects as "hold the previous frame"
  (`bootanim_dec.h:28`), so a GIF's per-frame delays are honoured on a
  grid, and the grid is an integer: BAN1 stores `fps` as one byte
  (`encode_bootanim.py:184`) and the unchanged player ticks every
  `1000 / fps` ms (`ui_boot.c:144`). So: a delay of 0 or under 20 ms is
  taken as 100 ms, as browsers do; `fps` is the integer in
  `[LOGO_FPS_MIN, LOGO_FPS_MAX]` whose tick `1000 // fps` (the player's own
  integer arithmetic, `ui_boot.c:144`: `next += 1000 / hdr.fps` in C) best
  fits the delays: the fps minimising `sum(|d - n * tick|)` over the
  frames, with `n = max(1, round(d / tick))`, ties to the lower fps; each
  source frame gets those `n` ticks, never zero, never negative: one
  changed-rects record followed by `n - 1` zero-rect hold records, each a
  few bytes. Worked: 30 ms frames give fps 33, tick 30, one tick each,
  error 0; 100/200/100 ms gives fps 10, tick 100, ticks 1/2/1, records
  `A, B, hold, C`; 30/100/1000 ms gives fps 39, tick 25, ticks 1/4/40,
  error 5 ms (fps 40, tick 25, ties and loses to the lower fps). The factory tool gains the same decoder for `.gif` input
  (ffmpeg stays for `--video`), so a GIF burned at the factory and one
  uploaded from the page produce the same bytes. Stills are rasterised by the browser onto a 320x240
  `<canvas>` (letterboxed on a background colour the owner picks, default
  black) and posted as raw RGBA, 307200 bytes, `Content-Type:
  application/octet-stream`, `X-Blink-Logo: rgba`. A GIF is posted as the
  file's own bytes, `X-Blink-Logo: gif`, at most 2 MiB, because browsers do
  not uniformly expose GIF frames and the daemon must see every frame. No
  base64, no JSON wrapper: the bytes are the body.
- **Hold and fit (D66).** The page's "Size on the screen" and "Stays up
  for" travel as `X-Blink-Fit: fill|large|margin` (scale 1, .8, .6; applied
  by the daemon to a GIF, already applied by the browser to a still) and
  `X-Blink-Hold-Ms: 3000|5000|10000`, both validated before decoding; 30 s
  is gone because `nframes * 1000 / fps + hold_ms` must fit
  `LOGO_MAX_TOTAL_MS`, and the facts line says "for up to 15 seconds".
- **The body limit.** `theme_server.py:82` caps every body at
  `MAX_BODY = 4096` and `:408` answers 413 above it. `/api/logo` gets its own
  bound, `MAX_LOGO_BODY = 2 MiB`, applied by route before the read; every
  other route keeps 4096. A test posts a full 307200-byte RGBA body and a
  generated 1.5 MiB GIF through the real handler and expects 200, and a
  2 MiB + 1 body expects 413 before any byte past the limit is read.
- **Decoding without a dependency.** `pc/gif_decode.py`, standard library
  only: GIF87a and 89a, LZW with code-size growth and clear codes, global
  and local palettes, interlace, the Graphic Control Extension's disposal
  methods (none, do-not-dispose, restore-to-background,
  restore-to-previous), transparency index and per-frame delay, looping
  ignored (one pass). Bounds come before any allocation, because the
  2 MiB body limit bounds compressed bytes and a tiny GIF can declare a
  65535x65535 screen (12 GiB for one RGB canvas): the logical screen and
  every image descriptor are refused above 2048 on either side (eight
  times the panel in each direction is more than any sensible logo and
  needs 12 MB for an RGB canvas, which a daemon may spend once), an image
  rect must lie inside the screen, the LZW expander stops the moment it
  has produced `w * h` pixels for the current image and refuses a stream
  that ends short, and the frame count cap is applied before each frame's
  data is read. The decoder keeps **one** canvas at logical size and the
  previous-frame copy that disposal method 3 needs; each composited frame
  is letterboxed onto 320x240 (nearest-neighbour, plain Python) and
  yielded as rows **as it is finished**, so what accumulates is 320x240
  frames, at most the frame cap of them (about 230 KB each as RGB888
  rows; 180 of them is 41 MB, the daemon's worst case, which the frame cap
  bounds). Fixtures are generated by a
  committed script (`tests/support/gen_gif.py`, which may use Pillow; it
  runs only in tests) covering each feature above, never captured files.
- **Encoding, rewritten.** `tools/encode_logo.py` and `encode_bootanim.py`
  are NumPy throughout (`encode_logo.py:128`, `encode_bootanim.py:182`), so
  moving functions would ship a dependency. `pc/logo_encode.py` is a
  standard-library rewrite of the same algorithm in the same order: the
  noise gate runs on **RGB888** canvases against the last displayed RGB888
  frame (`encode_logo.py:125-131`: a pixel moves only if some channel
  differs by more than the threshold, 10), and only then RGB565
  conversion, because the gate's decision is not visible after
  conversion (from black, red 10 and red 11 both convert to the same
  RGB565 value and must decide differently); then per-frame diffing
  into row bands and rectangles, the RLE payload, and the `BAN1`/`BLGO`
  headers; plus `parse` for the daemon's own checks. A parity test encodes
  generated fixtures with both the tools (NumPy and Pillow allowed in the
  test venv) and `pc/logo_encode` and requires identical bytes; an
  import-blocked test runs the daemon's encoder in a child whose
  `sys.modules` refuses `numpy` and `PIL`. The fps and the hold records come
  from the GIF's delays as above; a still is one frame with the tool's
  default hold (3 s; a clip's default is 0.5 s, `encode_logo.py:225-227`).
- **Board limits (D60).** `logo_parse()` validates the whole blob, not only
  its header, before the board calls a logo present, and it is what `logo.c`
  calls at every boot, so a bad blob already on the flash is treated as
  absent too. Limits, all in `logo_parse.h`, mirrored in `pc/logo_encode.py`
  so the daemon refuses before sending, and enforced by `encode_logo.py`
  and `burn.sh` so the factory cannot burn what the board would refuse:
  `nframes` 1 to `LOGO_MAX_FRAMES`; `fps` `LOGO_FPS_MIN` to `LOGO_FPS_MAX`;
  `nframes * 1000 / fps + hold_ms` at most `LOGO_MAX_TOTAL_MS` (the whole
  time the boot may spend on the logo); every frame's rect count at most
  `LOGO_MAX_RECTS`; every rect inside the 320x240 canvas with non-zero
  width and height; every rect's declared payload length inside the blob
  and its PackBits16 decoding to exactly `w * h` pixels; the walk ends
  exactly at `blob_len`. Provisional values: 180 frames, 1 to 60 fps (the
  tool's own range, `encode_logo.py:245`), 15000 ms total, 120 rects (2026-11-07: was 256, which a one-byte rect count cannot even write; the encoder cuts bands of changed rows, so 240 rows give at most 120 bands, and 120 is every frame it can produce). They
  are provisional because factory logos already exist (local `.bin` files
  up to about 150 KB, uncommitted, never described here beyond their
  headers): plan Task 6.3 runs `tools/encode_logo.py --info`, extended to
  print the frame count, fps, hold and the largest per-frame rect count
  from the headers alone, over every existing `.bin`, and the caps are set
  at or above what those need before `logo_parse` learns them. A 3 s clip
  at 15 fps with a 0.5 s hold (today's `burn.sh` default) is 45 frames and
  3.5 s, well inside. The walk is its own strict PackBits16 walker in
  `logo_parse.c`, not a call into `ba_decode_frame`: the player's decoder
  clips a run to the pixels it still wants (`bootanim_dec.c:63`) and then
  skips to the declared payload end without looking at what it did not
  use (`:120`), which is right for a player and wrong for a validator. The
  walker consumes every run, requires the runs to produce exactly `w * h`
  pixels with no run crossing that count, and requires the last run to
  end exactly at the declared payload length; an overlong run, a short
  payload and trailing payload bytes are each refused. It is pure C,
  host-tested with blobs that break each limit one
  at a time (65535 empty frames at 1 fps is the first case: today that
  passes the CRC and header and would hold the boot for 18 hours,
  `ui_boot.c:140`).
- **Transport.** The logo is a second kind of blob on the OTA writer's
  path. Over WiFi the session frame type `O` gains a sibling `G` (0x47):
  `body = offset[4] || bytes`, same 512-byte chunks, same ordered ring,
  same `off == got` and bound check (`LOGO_IMAGE_MAX = 0x80000`), same
  acks (`logo_ack off:<n>` at 16 KiB boundaries and at `got == size`),
  same window on the daemon side (32 KiB), same abort on drop, takeover or
  replacement. Over USB the same bytes travel as `logo_chunk` NDJSON lines
  (`off`, `d` base64 of at most 256 bytes, so the line stays under 512 B).
  Every USB byte lands in the board's 2 KB UART RX ring, so over USB the
  board acks EVERY chunk (`logo_ack off:<n>`, not at 16 KiB boundaries),
  after its flash write returns, and the daemon keeps one chunk in flight,
  so nothing arrives while an encrypted chip erases a sector with
  interrupts off (a 256-byte write there is a read, erase and rewrite of a
  4 KB sector, 45 ms or more with `irq_lock` held, and chunks behind it
  would overrun the UART's 128-byte hardware FIFO; review of Task 6.3,
  2026-11-07). A 32 KiB window would also overflow the ring and the board
  would drop lines silently (2026-11-07). The cost is a slower logo over
  the cable. The stream is done only when the board's `logo_state`
  arrives after `logo_end` (`present` 1). `logo_chunk` is the one place
  the OTA path has no USB equivalent (OTA over USB is esptool). The control lines are shared by both links: `logo_begin`
  (`size`, `crc`), the chunks, `logo_end`; `logo_remove`; and the board's
  `logo_state` (`present`, `bytes`, `frames`) sent after `hello` and after
  every change, so the page can show "Logo: company, 48 KB, 12 frames" or
  "none". The daemon refuses a blob over `LOGO_IMAGE_MAX` or over the
  playback limits before `logo_begin`.
- **Board side.** `logo_write.c`: `logo_begin` (refused if `size` exceeds
  `LOGO_IMAGE_MAX`) erases the partition and opens a `flash_area` stream
  (no `flash_img`: that is slot 1's); chunks write in order; `logo_end`
  runs the full `logo_parse()` over the memory-mapped partition and answers
  `logo_state`. A partition that fails `logo_parse` after `logo_end` is
  erased again, so "present" stays trustworthy, which is what `logo_parse.h`
  promises. `logo_remove` erases. The write runs on the main thread like
  the OTA consumer (D34). The boot logo is read at boot only (`logo.c`), so
  a change shows on the next power cycle; the page says so. Flash
  encryption: `flash_area_write` through the encrypted driver is what the
  OTA stream already relies on, and the memory-mapped read in `logo.c`
  decrypts through the cache, so nothing new is needed; the fused unit in
  Task 5.5 proves a logo write too. The edition latch is untouched: a logo
  makes a company unit, the edition says which clip plays, and `burn.sh`'s
  rule that a re-burn without `--logo` erases the partition stays.
- **Who may write it.** Anyone who can talk to the board's protocol: the
  paired PC over WiFi, or whoever holds the cable. That is the OTA
  boundary, and it is weaker than the factory's "esptool in bootloader
  mode" boundary `logo_parse.h` describes today. The comment is rewritten
  to say so. Logs: `[logo] written`, `[logo] removed`, `[logo] rejected`,
  fixed strings, added to the allowlist with their printks.

### 15.3 Preferences on the page (D58)

Today `main_src` and Focus are set only from the board's own tiles
(`cfg_set_main_src`, `cfg_set_focus(on, win)`, `cfg_store.h:145`); no wire
message sets them. One additive message does: `pref_set` (host -> board)
with optional fields `main_src` (`"claude"`/`"codex"`), `focus` (0/1),
`bright` (20, 40, 60, 80 or 100, the levels `cfg_set_bright_pct` stores;
the design's Board panel sets it) and `view` (the main-screen view enum, which `feat/ultra-burn` adds together
with the burn view; **until that branch is merged here, `view` is accepted
and ignored by the board and the page shows the choice greyed with "needs
the burn view"**). The board validates each field against its enum before
touching the store (`cfg_set_main_src` and the other setters accept any
byte, `cfg_store.c:610`, and a protocol client is not the page), applies
it through the existing setter, which persists as a tile tap does, and
then does what the tile does after the write: for Focus,
`cfg_set_focus(on, cfg_get_focus_win())` keeps the chosen window, then
`usage_view_set_focus(cfg_get_focus_on(), cfg_get_focus_win())` and
`focus_refresh()` redraw the live layout exactly as `ui_settings.c:2887-2894`
does on a tap. It answers with its existing `pref` line (which already
names the main source, `proto.c:229`), extended with `focus` and `view` so
the page can render what the board holds. Weekly scope is **not** on the
page (D61): `usage_view.c:296-308` records that per-model weekly windows
went with the standalone mode and the `weekly_sel` byte is kept unread, so
a control would persist a value nothing draws. These live on Look's
Customise side, Screen sub-tab (section 16): View, Focus, Main provider. The
session and weekly preview sliders drive the page's own gauge drawing
only; they send nothing (the mock marks them page-only, and a slider that
wrote to the board would spend a flash write per drag).

### 15.4 Dependencies and what is not designed here (D59)

- **Burn view**: `feat/ultra-burn` (local, board-tested, three platforms
  green per the memory note) carries the view enum and the burn panel.
  Task 6.3 is written against this branch and leaves `view` a no-op until
  the merge; the merge order is the owner's (Q13 names it).
- **Usage refresh**: `feat/usage-refresh` adds `blink install`'s opt-in
  background `/usage` fetch. The page's "Usage refresh" switch is a
  daemon-side setting with no board part; it is a disabled "New" row on the
  Board panel's This computer sub-tab
  as "coming with usage refresh" until that branch lands.
- **Doze switch**: `main.c` dozes the panel after four hours with no host;
  a page switch to disable or lengthen it needs a cfg field and a `pref_set`
  field. Not designed; Q13.

---

## 16. The approved page (owner, 2026-11-06)

The owner reviewed the settings-page design and said: "the last UI I got
was really good, we should use it" (D62). The design is
`docs/design/settings-page.md`, with the mock `settings-page.html` beside
it; both are committed by the first UI task (plan Task 4.2), outside the
gitignored `docs/superpowers`. That document is the authority for layout,
tokens, type, motion, components, copy, responsive rules and
accessibility; this section says only how it meets the rest of this
design and what the plan must prove.

### 16.1 What the page is

- The board is the hero: a drawing of the case, dead centre, lit, never
  moving. The opening state is the board, its name, one status line and
  the three controls Look / Connect / Board. One panel open at a time,
  docking beside the board with its top on the case's top edge; Look takes
  both sides (Themes and Logo left, Customise with Colours / Screen / Try
  it right); Connect and Board dock right. Escape closes and returns focus
  to the control; pressing the open control closes it.
- The board preview is drawn by the page's existing SVG renderer
  (`render()`, `gauge()`, `arcPath()` and the colour helpers in
  `theme_builder.html`, which already follow `theme.c` and `custom_dim`),
  moved into one `board.js`-style block the new page calls with the state
  it needs (theme, view, Focus, the preview percentages, the link mark,
  the Settings screen, the start-up screen with the logo). The mock's own
  board drawing is the reference for the case; the screen contents come
  from the firmware-faithful renderer, which is why the preview can be
  trusted (D62). What the board shows follows the design spec's "What the
  board shows" table (Logo tab: start-up screen; Board's "Show it on the
  board": the Settings screen; after a join: the connection screen for
  4.2 s; otherwise the usage screen in the chosen view).
- Light and dark follow the system, `data-theme` overrides;
  `prefers-reduced-motion` turns every animation off; below 1200px the hub
  sticks to the top and panels scroll beneath, checked at 400px with no
  horizontal scroll.

### 16.2 Where the earlier sections' functions land

| Function | Section | Where on the page |
|---|---|---|
| Theme builder (today's page) | 8 | Look > Themes, and Customise > Colours (colours, width, ends, dot, by) |
| View, Focus, Main provider (`pref_set`) | 15.3 | Look > Customise > Screen |
| Preview sliders, sweep (page only) | 15.3 | Look > Customise > Try it |
| Logo upload, remove | 15.2 | Look > Logo |
| Scan, pick, password, join, handshake progress | 8, 6.1 | Connect, with the status line |
| Forget WiFi | 5.7, 8 | Board > This board > Details, in-page confirm |
| Board id, firmware, edition, link, address | 8 | Board > This board |
| Multi-environment (Q3), service incidents, self-update | 8, Q3 | Board > This computer |
| Usage refresh, doze (D59) | 15.4, Q13 | Board > This computer, "New" rows, disabled until their branches land |
| Open settings on the board (D56) | 15.1 | Board > This board, "On the board" note and "Show it on the board" |
| Weekly scope | D61 | absent |

The four "New" items the design marks are therefore: usage refresh (a
dependency, `feat/usage-refresh`), the doze switch (Q13), logo sending
(plan Tasks 6.2 to 6.4) and Open settings on the board (plan Task 6.1).

### 16.3 Offline (D63)

The daemon serves the page; the page loads nothing from the network. The
mock loads Google Fonts and the shipped page must not. Decision: vendor
**Montserrat** 500 and 600, latin subset, as two woff2 files under
`pc/fonts/` with the OFL licence text beside them, served by
`theme_server` at `/font/<name>.woff2` (same origin, same cookie rule as
the page, about 50 KB together), because the board's screen is drawn in
LVGL's font and a preview in another face is not faithful. The UI face is
the design's system stack (`system-ui, -apple-system, "Segoe UI",
sans-serif`; mono `ui-monospace, Menlo, Consolas, monospace`): IBM Plex
would be about 150 KB more in a frozen app whose every megabyte the
customer downloads (CLAUDE.md), the wordmark is an SVG drawing that needs
no font, and the design spec names the stack as the fallback. A test
asserts the served page, its CSS and its scripts reference no `http://`,
`https://` or protocol-relative URL, no `<link rel="stylesheet">` to
anywhere but `/font/`, and no `@import`.

### 16.4 Proof (D64)

- Node page tests, the existing `tests/support/builder_page.py` approach,
  one per state and interaction: opening state; each control open and
  closed again by its own button and by Escape (focus returns); only one
  panel at a time; Look's sub-tabs with Left/Right keys and roving
  `tabindex`; theme preview on hover and focus, commit on click; the
  Customise header's "Custom, changed from Dark"; hex field validation and
  revert on blur; the view cards and Focus; the sliders changing only the
  preview; the Connect flow against a fake `BoardOps` (scan list rendered
  as text nodes, never `innerHTML`; unsupported networks disabled; the
  password field cleared after submit, success or failure; the handshake
  steps in the status line); the in-page confirm for Forget and Remove
  with focus on "Keep"; the light/dark override; reduced motion. Skips
  without Node, as today.
- A committed script, `tools/page_shots.py`, using Playwright from the
  test venv only (a dev-only dependency, never in `pc/requirements.txt`;
  the script and its test skip when it is absent): with the network
  blocked, it screenshots the mock and the served page at 1440x900 and
  400x900, light and dark, in each of the four states, prints the
  pixel-difference ratio per pair for a person to look at, and **asserts**
  the layout invariants by bounding box: the board's box is identical
  across the four states at 1440; a panel's top equals the case's top;
  Look and Board panels are at most 452px wide and Connect 372px; at
  400px the hub is sticky and `document.scrollingElement.scrollWidth`
  equals the viewport width; the top bar's `h1` is the only one.
- The accessibility list in the design spec's section 6, as a checklist
  in the task with the Node assertions that pin each line that can be
  pinned (labels, `aria-pressed`, `aria-controls`, `role=tablist`, live
  regions, `role=img` with a title, 44px targets measured by bounding box,
  no `alert`/`confirm`/`prompt` in the script).
- A desk check through `blink settings` on a real board.
- Security: the page keeps `theme_server`'s model (127.0.0.1, one-time
  token for a cookie, Host and Origin checks, section 8.3); SSIDs are
  bytes shown as text; the password is never kept after the POST.
