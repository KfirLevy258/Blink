# Update plan: one release, two artifacts

Status: **implemented** on 2026-08-22, branch `credential-free-usage-source`.
Written as a plan, kept as the record of why it is shaped this way.

Covers three asks: a real self-update path for the daemon, a version
compatibility check on both sides of the serial link, and SHA-256 verification
of what we flash.

## What shipped, and where the plan was wrong

All four workstreams landed. Three things the hardware or the machine changed:

- **No baud escalation.** §2 planned to raise the flash speed to pay for the
  read-back. The CYD's CH340 fails at 460800 -- three consecutive attempts died
  with "Invalid head of packet" immediately after the baud change, while the
  same transfer at 115200 moved 1 MB cleanly in 96 s. A failed read is free; a
  failed *write* leaves a slot0 that will not boot. So the read-back stays and
  the speed does not move, which makes an update take about four minutes rather
  than two -- and the board's on-screen promise was corrected to match.
- **The self-test needs a generous timeout.** 60 s looked ample for
  `clauge --version` (about 2 s of CPU). Measured at 97 s on a machine under
  load average 89, which is exactly when someone might run an update. Timing
  out means refusing a good release, so it is 300 s.
- **CI cannot sign.** §4.5 assumed the workflow could assemble the manifest.
  It cannot: the release key stays on one machine, so `tools/release.sh` builds
  and signs the manifest locally after downloading what CI published, and
  `release-binaries.yml` lost its `release: published` trigger -- that trigger
  would have rebuilt the binaries after signing, and PyInstaller output is not
  reproducible, so every daemon would have refused its own release.

Verified on hardware: board at 0.6.0, daemon at 0.6.0, `[proto] host connected:
app 0.6.0, protocol 2`, usage flowing, OTA check answering correctly against
the live feed.

---

## 0. What is true today

Facts, with references, so the plan below is not arguing against a strawman.

| | |
|---|---|
| One release carries both | `tools/release.sh` tags `v<version.h>` and attaches `clauge-fw.bin` + `manifest.json`; publishing fires `release-binaries.yml`, which builds the four PyInstaller binaries **from the same tag** and attaches them. |
| Firmware updates | Board emits `ota_query{cur}` (`proto.c:148`) → daemon compares against `releases/latest/download/manifest.json` (`bridge.py:97`) → `ota_avail` → user consents on the panel → `ota_flash` → daemon runs esptool against slot0 (`ota.py:148`). |
| Daemon updates | **None.** No version constant, no check, no `update` subcommand. |
| Protocol version | `PROTO_VERSION 2` (`proto.c:20`) and `VERSION = 2` (`protocol.py:9`). Both sides stamp `"v"` on every message. **Neither side ever reads it.** |
| Daemon version on the wire | `Bridge(app_ver="0.3.0")` — a hardcoded default that matches nothing. The board prints `[proto] host connected` and discards it. |
| SHA-256 | The manifest's hash crosses to the board in `ota_avail` and is stored in `ota_m.sha256`, but in USB mode **nobody hashes the bytes**. `_on_ota_flash` checks `len(blob)` only (`bridge.py:127`). `ota.c`'s hashing is the WiFi path, now compiled out. |
| Safety net when flashing | MCUboot's signature check at boot. Slot0 is written in place, so there is no test-boot and no auto-revert (stated at `proto.c:132`). |
| Stale claim | `pc/ota.py`'s module docstring still says "the board re-hashes what it received". That stopped being true when the transfer moved to esptool. |
| Board-side stall | There is no timeout on `OTA_UI_DOWNLOADING`. If the daemon dies mid-flash the panel sits on "keep it connected" forever. |

The asymmetry that matters commercially: **firmware auto-advances and the
software that drives it never does.** `RELEASE_BASE` is pinned to `/latest/`,
so a binary a customer downloaded a year ago will happily fetch and flash
today's firmware.

---

## 1. Load-bearing decisions

**D1 — one version number for the pair.** A release is `X.Y.Z` and both
artifacts carry it. Firmware and daemon do not get independent version lines.
This is a locked release train, not a library ecosystem; two version axes buy
flexibility we do not want and a test matrix we cannot afford.

**D2 — `PROTO_VERSION` is a capability floor, not a wire-format switch.**
Protocol changes are additive: new fields, ignored by anything that does not
know them (`msg_get_*` already ignores unknown keys). `v` only increments for a
change that genuinely breaks an older peer. This keeps the compatibility check
below to two rules instead of a matrix.

**D3 — manifest changes are additive only.** Daemons already in the field
require `version`, `size`, `sha256` at the top level and read them as
*firmware*. Those three keys keep that exact meaning forever.

**D4 — consent stays on the board.** The panel is where the customer already
approves an update. A pair update is one tap there, not a separate thing to
discover on the computer.

**D5 — the daemon updates itself by replacing its binary and letting the
supervisor restart it.** launchd `KeepAlive` + `ThrottleInterval 10` and
systemd `Restart=always` + `RestartSec=10` both do this for free. Windows does
not — `schtasks /sc onlogon` has no restart-on-exit — so the Windows path
re-launches explicitly before exiting.

**D6 — sign the daemon manifest.** The firmware has a signature chain: a
compromised GitHub account cannot push an image the bootloader will run. The
daemon has nothing — it is a login agent, on a customer's machine, updating
itself from a URL. That gap should not ship. Details in §4.5.

---

## 2. Workstream A — SHA-256 (small, independent, ship first)

1. `pc/bridge.py::_on_ota_flash` — after the size check:
   ```python
   digest = hashlib.sha256(blob).hexdigest()
   if digest.lower() != self._manifest["sha256"].lower():
       self._ota_reset()
       self._write(protocol.ota_error("sha256 mismatch"))
       return
   ```
2. `pc/ota.py::flash` — optional second gate: run `esptool verify_flash
   0x20000 <img>` after the write. Catches a bad write before the board reboots
   into it. Costs ~20 s on a 1.3 MB image; worth it, since the failure mode it
   catches is a device that will not boot.
3. Fix the false claim in `pc/ota.py`'s module docstring.
4. `firmware/src/ui_settings.c` — surface `ota_error`'s `why` in the failure
   notice instead of collapsing every cause into "Update failed". The board
   already receives it; `proto.c:275` throws it away.
5. `firmware/src/ota.c` / `ui_settings.c` — time out `OTA_UI_DOWNLOADING`.
   Over USB the board gets no progress messages at all, so the timeout must be
   generous: no host message for 5 minutes → `OTA_UI_FAILED`.

Tests: `tests/pc/test_bridge.py` — mismatch refuses and reports; match still
flashes; a manifest with an uppercase hash still matches.

---

## 3. Workstream B — version diff on both sides

### 3.1 One source of truth per side

- New `pc/version.py`: `RELEASE_VERSION = "0.6.0"`, `PROTO_VERSION = 2`.
  `pc/protocol.py` imports `PROTO_VERSION` rather than defining its own `VERSION`.
- `firmware/src/version.h` keeps `CLAUGE_FW_VERSION`; move `PROTO_VERSION` there
  from `proto.c:20` so one header answers both questions.
- New `tests/ci/check_versions.sh`: greps both files, fails if
  `RELEASE_VERSION != CLAUGE_FW_VERSION` or the two `PROTO_VERSION`s differ.
  Runs in CI **and** as the first thing `tools/release.sh` does.

### 3.2 Put the real numbers on the wire

- `Bridge.__init__` takes `app_ver=version.RELEASE_VERSION` — delete the
  `"0.3.0"` default outright rather than updating it, so it cannot rot again.
- `welcome` already carries `v` and `app_ver`. Nothing new needed on the wire in
  this direction.
- Board `hello` already carries `fw` and `v`.

### 3.3 The two rules

Read `v` on both sides and act on exactly two cases:

| Case | Meaning | Behaviour |
|---|---|---|
| `welcome.v < board PROTO_VERSION` | The computer's app is older than the firmware | Board: persistent settings notice "The Clauge app on your computer is out of date", plus the update badge. Usage keeps flowing — that path is proto-stable. |
| `hello.v > daemon PROTO_VERSION` | Same thing, seen from the daemon | Daemon logs it once and **refuses to offer firmware updates**: it cannot safely drive a board it does not understand. `ota_query` gets `ota_none`. |

The reverse (daemon newer than board) is the normal upgrade path and needs no
special handling under D2.

### 3.4 Refuse to flash across a floor

`manifest.json` gains `fw.proto_min`. `_on_ota_query` will not offer an image
whose `proto_min` exceeds the running daemon's `PROTO_VERSION`; it logs
"firmware 0.7.0 needs a newer app" and — once Workstream C lands — offers the
daemon update instead. This is what stops a year-old binary from flashing a
firmware it cannot talk to.

### 3.5 Show it

Settings panel gains a line: firmware version *and* app version (from
`welcome.app_ver`). Cheapest support tool we will ever build.

---

## 4. Workstream C — daemon self-update

### 4.1 Manifest v2

Additive, per D3. Old daemons keep working because the first three keys never
change meaning.

```json
{
  "version": "0.6.0",
  "size": 661744,
  "sha256": "<firmware>",

  "schema": 2,
  "fw":     { "proto_min": 2 },
  "daemon": {
    "version": "0.6.0",
    "proto": 2,
    "auto": false,
    "artifacts": {
      "macos-arm64":       { "size": 11917888, "sha256": "..." },
      "macos-x86_64":      { "size": 11901234, "sha256": "..." },
      "linux-x86_64":      { "size": 11550000, "sha256": "..." },
      "windows-x86_64.exe":{ "size": 12100000, "sha256": "..." }
    }
  }
}
```

`daemon.auto` is a server-side kill switch: ship with `false` (manual updates
only), flip to `true` once the path has proven itself, flip back within minutes
if a release goes wrong. That switch is the reason to build auto-update at all —
without it, auto-update is a mechanism with no brake.

### 4.2 New module `pc/update.py`

- `platform_key()` → `macos-arm64` | `macos-x86_64` | `linux-x86_64` |
  `windows-x86_64.exe`, from `platform.system()` + `platform.machine()`.
  Note: an Intel binary under Rosetta reports `x86_64`, which is correct — we
  want the artifact matching the *running process*, not the silicon.
- `check(manifest)` → `(available: bool, version, artifact)` using the existing
  `ota.is_newer`.
- `download_and_verify(url, size, sha256)` → bytes; raises on either mismatch.
- `apply(blob) -> (ok, message)`:
  1. write `~/.clauge/bin/clauge.new` (same directory, so the replace is atomic)
  2. `chmod 0755`
  3. **run `clauge.new --version` and require it to print the expected version.**
     Non-negotiable: this is a login agent. A corrupt binary that gets as far as
     being the service's target is a device that never comes back and a customer
     who has to start over. A 200 ms self-test buys that away.
  4. `os.replace(installed_bin(), installed_bin() + ".old")` — required on
     Windows, harmless elsewhere; `_make_way_for_copy()` in `cli.py` already
     does this dance and should be reused
  5. `os.replace(clauge.new, installed_bin())`
  6. restart (§4.3)
- Rollback: a failure at 1–3 deletes `.new` and changes nothing. A failure
  between 4 and 5 leaves `.old` in place; `cmd_run` and `cmd_status` gain a
  `_recover_from_old()` that restores it when `installed_bin()` is missing or
  zero bytes. `.old` is cleaned up on the next successful start.

### 4.3 Restarting, per platform

| | From the CLI (`clauge update`) | From inside the daemon |
|---|---|---|
| macOS | `launchctl kickstart -k gui/$UID/com.clauge.bridge` | `sys.exit(0)` — KeepAlive brings it back within `ThrottleInterval` (10 s) |
| Linux | `systemctl --user restart clauge-bridge.service` | `sys.exit(0)` — `Restart=always`, `RestartSec=10` |
| Windows | `schtasks /end` then `/run` | spawn `installed_bin() run` detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), then exit. The onlogon task still points at the same path, so nothing is orphaned beyond this session. |

### 4.4 Entry points and cadence

- `clauge update` — explicit, prints what it will do, applies, restarts.
- `clauge --version` — needed by 4.2 step 3 anyway; also the first thing anyone
  asks in support.
- `clauge status` — gains an "App" line: current version, and "update available
  (X.Y.Z)" when there is one.
- Daemon: checks 60 s after the first successful board connect (not at process
  start — a crash-looping daemon must not hammer GitHub), then every 24 h.
  State in `~/.clauge/update.json`: `{"last_check", "available", "declined"}`.
- Opt-out that we honour regardless of `daemon.auto`: `CLAUGE_NO_AUTO_UPDATE=1`
  or a `~/.clauge/no-auto-update` file.

### 4.5 Signing (D6)

Manifest v2 is signed with a **separate** P-256 release key — not
`~/.clauge/ota_signing_key_p256.pem`, which is MCUboot's; mixing key purposes is
how one compromise becomes two. `release.sh` writes `manifest.json.sig`;
`pc/update.py` embeds the public half and refuses any manifest that does not
verify, before it reads a single field.

Cost: one signature-verification dependency in the bundle. `ecdsa` is pure
Python and small; `cryptography` is faster and much larger. Recommend `ecdsa` —
we verify one 200-byte document per day.

If this is deferred, say so out loud in the README rather than implying the
daemon has the same protection as the firmware. It does not.

---

## 5. Workstream D — the pair updates as one thing

Order is fixed: **daemon first, then firmware.** The new daemon is the thing
that knows how to drive the new firmware.

1. `_on_ota_query` computes both: newer firmware? newer daemon for this
   platform?
2. `ota_avail` gains an optional `"app"` field (ignored by older firmware).
3. Board copy when both are present: *"Update to 0.6.0 — this also updates the
   app on your computer."*
4. On `ota_flash`, if the daemon is behind: write
   `~/.clauge/pending_fw.json` = `{"version": "0.6.0"}`, self-update, restart.
5. The new daemon sees `pending_fw.json` on its next `hello`, re-fetches,
   re-verifies, flashes, deletes the file.
6. The board is showing "keep it connected" across the whole sequence — which is
   exactly why the §2.5 timeout has to exist before this ships. Without it, a
   daemon that dies during step 4 leaves the panel stuck forever.

Fallback if the resume machinery proves fiddly: skip `pending_fw.json`, let the
new daemon reconnect, and have the customer tap update a second time. Two taps,
zero state. Worth keeping in the back pocket.

---

## 6. Release engineering

**The ordering problem:** `release.sh` publishes `manifest.json` *before*
`release-binaries.yml` has built the binaries, so it cannot know their hashes.

**Fix — never publish a half-release.** `release.sh` creates the release as a
**draft** (drafts are invisible to `/latest/download/`), triggers
`release-binaries.yml` via `workflow_dispatch -f tag=$TAG`, waits, and only then
flips `--draft=false`. A new `manifest` job in that workflow (`needs: [build]`)
downloads the four artifacts, computes size + sha256, patches `manifest.json`,
signs it, and uploads both with `--clobber`.

Fallback: publish as today and let the workflow patch the manifest afterwards.
There is then a few-minute window where the manifest has no `daemon` block,
during which daemons simply do not self-update. Harmless, but the draft flow is
strictly better and not much more script.

Also in `release.sh`:
- run `tests/ci/check_versions.sh` first
- keep the existing artifact greps (`/api/oauth/usage`, `refresh_token`,
  `claude-code/`, `CONFIG_CLAUGE_WIFI_MODE=y`)
- assert each built binary's `--version` matches the tag

---

## 7. Bootstrapping the units already in the field

Existing installs have no self-update, and nothing we publish can give them one
— the daemon is the only thing that reaches out, and theirs will never look.

The path that does work: an old daemon can still flash new firmware, and new
firmware can nag. §3.3 gives us that for free — a board running new firmware
sees `welcome.v` (or a missing `app_ver`) from an old daemon and puts up "The
Clauge app on your computer is out of date". README gains an "Updating" section:
re-run the same curl one-liner. `cmd_install` is already idempotent over a live
install (bootout → copy → bootstrap), so that is a supported path, not a
workaround.

---

## 8. Tests

| Where | What |
|---|---|
| `tests/pc/test_bridge.py` | sha mismatch refuses; case-insensitive match; `proto_min` above ours is not offered; `hello.v` greater than ours suppresses offers |
| `tests/pc/test_update.py` (new) | `platform_key` on all four; size/sha mismatch raise; `apply` rolls back when the self-test fails; `.old` recovery restores a missing binary; Windows rename path |
| `tests/pc/test_ota.py` | manifest v2 parses; a v1 manifest still works (D3 regression guard) |
| `tests/ota_parse/host_test.c` | firmware-side manifest parse tolerates the new keys |
| `tests/ci/check_versions.sh` (new) | the four version numbers agree |
| `tests/ci/check_install.sh` | new `update` scenario: install an old build, point `CLAUGE_OTA_DIR` at a local feed, run `clauge update`, assert the binary was replaced, `--version` moved, and the service is still registered |
| `release-binaries.yml` | each built binary's `--version` matches the tag |

`CLAUGE_OTA_DIR` already exists for exactly this kind of local-feed testing and
should carry the daemon artifacts too.

---

## 9. Sequencing

| Phase | Contents | Ships independently? |
|---|---|---|
| A | SHA-256 verify, docstring, `why` on failures, DOWNLOADING timeout | Yes — do this now |
| B | version single-sourcing, the two compat rules, `proto_min`, version display | Yes |
| C | `pc/update.py`, `--version`, `update`, manifest v2, signing, release flow | Yes — manual updates only, `daemon.auto: false` |
| D | pair update from one tap, `pending_fw.json` resume | Needs A + B + C |

A and B are worth doing regardless of whether C ever ships. C is the one with
real risk attached, which is why it lands with `auto: false` and a kill switch.

## 10. Risks

- **Self-update is remote code execution on the customer's machine by design.**
  Today's trust anchor would be TLS plus the security of one GitHub account.
  §4.5 is the mitigation; skipping it is a decision to accept that, and should
  be a deliberate one.
- **Slot0 in place has no auto-revert.** Workstream A narrows the window
  (hash before write, verify after write) but does not close it. Closing it
  means MCUboot swap-with-revert, which costs the 121–357 s swap this design
  deliberately avoided.
- **A bad daemon release can brick the fleet's software side.** The self-test in
  4.2 step 3, `.old` recovery, and `daemon.auto` are the three independent
  brakes. Do not ship C without all three.
