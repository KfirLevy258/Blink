# The Windows check

Ten minutes on a real Windows machine, once per release. CI proves the
installer, the daemon's Windows branches and the real Claude Code CLI there;
it cannot prove the two things a customer hits first, because no runner has a
signed-in Claude Desktop or a board on a COM port.

**Needs:** a Windows 10/11 PC or VM with USB passthrough; Claude Desktop
installed and signed in; Claude Code installed (`npm install -g
@anthropic-ai/claude-code`); a BLINK on a USB cable.

Download `blink-windows-latest` from the latest CI run's artifacts (Actions ->
the run -> Artifacts), or the release's `blink-windows-x86_64.zip` (unzip it; run `blink\blink.exe`).

| # | Do | Expect |
|---|---|---|
| 1 | Open Claude Desktop, use it once, close it. | -- |
| 2 | In PowerShell: `dir $env:APPDATA\Claude\plan-usage-history.json` | **The file exists.** If it does not, the Desktop path assumption is wrong: find the file (`dir -Recurse $env:APPDATA,$env:LOCALAPPDATA -Filter plan-usage-history.json`) and report the real path. |
| 3 | `.\blink.exe` | The disclosure, then six steps ending `[6/6] Background service ... running`. No stack trace. Step `[5/6] USB driver` either says `already installed` with no prompt, or raises **one** Windows permission prompt and then says `installed`. **Run this NOT as administrator** -- that is what a customer's double-click does, and the permission prompt is the one path they all take. `this build carries no driver` means the build did not include `vendor/ch341ser`: a release blocker, not a test failure. |
| 4 | `.\blink.exe status` | `Bridge registered as a Scheduled Task`, `Claude Code <version>`, `Activity hooks installed (10/10 events)`, `Desktop usage cache parsed, reading N min old`. **If it says `looked at ...`, step 2's path is not the one it checked -- report both.** |
| 5 | Open a terminal, run `claude`, ask it something, wait for the reply. Then `.\blink.exe status` again. | `Usage data fresh`, `1 live session`. |
| 6 | `.\blink.exe status --wire` | One JSON line with `session_pct`, `weekly_pct`, `provider":"claude"`, `src":"cli"`, `state`. |
| 7 | Plug the board in. Device Manager -> Ports: a `USB-SERIAL CH340 (COMn)` entry. | Step 3 installed the driver, so the entry is there and clean. |
| 7a | The undriven case, on a desk whose driver already works. Elevated: `pnputil /delete-driver <oem>.inf /uninstall /force` (`<oem>` from `pnputil /enum-drivers`, the one whose Original Name is `ch341ser.inf`), then **reboot** -- the board has to enumerate with no driver, and deleting the package alone leaves the running device bound until it does. Then `.\blink.exe status`. | `Board  a board is plugged in, but Windows has no driver for it` and `run ... driver` -- **never** `not plugged in`. Then `.\blink.exe driver` says `installed`, a COM port appears within seconds, and `status` names the board. Verified 2026-09-06. |
| 7b | Note while doing 7a: `pnputil /enum-devices /problem` reports nothing, and the device's problem code is 0. | Expected. An undriven device is not a "problem" device to Windows -- detection keys on having no driver bound, not on a problem code. If a future change makes step 7a print `not plugged in`, this is the reason to look at first. |
| 8 | Within 60 s the panel shows the numbers from step 6. | Boot clip, then the gauges. |
| 9 | Start a Claude Code turn; watch the pip. Leave the terminal idle at its prompt for 4 minutes. | Pip pulses while it works, goes steady when done, **does not turn red** while idle. |
| 10 | Unplug and replug the board. | Panel back within a minute, no reboot loop, no reset of the board each time (`%USERPROFILE%\.blink\bridge.log` says `answered; not resetting it`). |
| 10a | **The ROM loader, which nothing else here covers.** Elevated is not needed. `schtasks /end /tn "Blink bridge"`, then `taskkill /f /im blink.exe`, then `& "$env:USERPROFILE\.blink\bin\blink.exe" -m pc.efuse_probe --port COMn`, then `schtasks /run /tn "Blink bridge"`. | `Connecting.....`, `Detecting chip type... ESP32`, `flash_encryption disabled`, **in about a second**. This is the one operation a USB firmware update needs and every other step avoids: Windows has only esptool's sequential `ClassicReset`, and on this board's cross-coupled circuit that releases EN before IO0 goes low. A machine that takes ~26 s and fails cannot install firmware at all -- see `docs/open-bugs/windows-usb-ota.md`. Measured 2026-09-08: 13/13 in ~1 s on the desk here, and a customer's machine failing every time. |
| 10b | `.\blink.exe theme`. In the builder, change a colour; then Save. | The default browser opens on the builder by itself (if not, the printed link works, once). Each change shows on the panel within a few seconds -- the preview goes through the running bridge, not the builder. Save prints `Saved.` and the theme's name, `%USERPROFILE%\.blink\theme.json` holds it, and the panel keeps it after an unplug and replug. Closing the tab instead of saving puts the board back on its saved theme. This and Settings > Theme > Edit on computer are the two ways `blink.exe` opens a browser: this one from the command, the other from the running bridge (`blink.exe run`, as the service). CI never opens one, so check both here: after Save, tap Edit on computer on the board: it shows **Opened on your computer** for about 3 s, reads **Edit on computer** again, and the builder opens again. |
| 10c | On the board, Settings > Theme > Edit on computer; the builder opens. Watch the slot, then close that browser tab and wait about 20 s. Then tap Edit on computer again. | The slot shows **Opened on your computer** for about 3 s and then reads **Edit on computer** again on its own, whether or not the builder is still open. About 5-15 s after the tab closes, `%USERPROFILE%\.blink\bridge.log` says `[theme] the builder closed; told the board`: that is the `theme_edit done:1` line reaching the board, which the slot no longer shows now that it comes back by itself. The tap after that opens a new builder. A second tap in the instant after a first, while the slot says **Opening on your computer**, sends nothing on purpose: that is the only guard left. Before the fix the slot held **Opened on your computer** and a tap did nothing for 10 s, or until the builder ended (contract Addendum 6). |
| 10d | Tap Edit on computer, leave the builder open, put another window over the browser, wait until the slot reads Edit on computer again (about 3 s), and tap it. | A second tab opens on the same builder. **Write down whether the browser comes to the front** or only flashes on the taskbar: Windows lets a background process (the bridge) open a tab but usually not raise the window, and no standard-library call can override that (see `open_url` in pc/theme_server.py). Flashing is the known limit, not a failure; a tab that never opens is. |
| 11 | `.\blink.exe uninstall` | Status line and hooks gone from `%USERPROFILE%\.claude\settings.json`, task gone (`schtasks /query /tn "Blink bridge"` fails), `%USERPROFILE%\.blink\bin` gone a moment after the window closes. |

Paste the outputs of steps 2, 4, 6 and 10 into the release notes. Anything
unexpected: the `bridge.log` and the output of `status --wire` are what a bug
report needs.

## Windows on Arm

Windows on Arm gets its own driver package (`vendor/ch341ser-arm64`,
WCH 4.0.2026.02), picked by `pc/win_driver.py` from the machine Windows
runs on. The steps above on an x64 PC never touch it. The Apple-silicon
Mac's UTM VM, Windows 11 ARM64 (`~/Projects/Blink/test-machines`), is the
place to check it. Passing the board through to the VM takes it from the
Mac's own daemon, so the panel says HOST LOST until it is handed back.

| # | Do | Expect |
|---|---|---|
| A1 | Steps 3 and 7 above, in the VM, with the board passed through. | `[5/6] USB driver ... installed`. `pnputil /enum-drivers` shows `ch341ser.inf` at `4.0.2026.2`, signer Microsoft Windows Hardware Compatibility Publisher. Device Manager -> Ports: `USB-SERIAL CH340 (COMn)`. |
| A2 | Step 7a, in the VM. | The same as on x64. |

Measured 2026-10-19: elevated `pnputil /add-driver` of this package exits 0
on the VM, where the x64 package exits 2 ("The system cannot find the file
specified" -- its `CH341M64.SYS` was never in it). A1 passed the same day,
with the board passed through: `USB-SERIAL CH340 (COM3)`, status OK, bound to
this package, and the daemon connected and exchanged welcome, time and usage
with the board. Later that day, the CI-built `blink-windows-latest` from
949fcbb (the frozen x64 `blink.exe`, emulated) on the same VM with the
driver deleted: `blink.exe driver` said `installed`, and `pnputil
/enum-drivers` showed `ch341ser.inf` at `4.0.2026.2` -- so the shipped
build picks the Arm package, not only the source tree. A2 not yet: it needs
the board passed through, which only the VM window's menu can do.

Passing the board through: UTM's command-line `utmctl usb connect` answered
"The device cannot be found" every time, so use the VM window's USB icon ->
USB Serial -> Connect, and Disconnect from the same menu to hand it back.
The VM needs `UsbSharing` on in its settings, and the Mac's daemon stopped
first so the port is free.

## Why not a VM on the Mac

Tried 2026-08-28 on an Intel Mac running macOS 26: QEMU's HVF accelerator
hangs the emulator's main loop on this host (every CPU model, entitlement
present), so Windows 11 only ran under pure software emulation -- the
install took two hours, the desktop was too slow to use, and the evaluation
build's licence read as expired, which shuts Windows down on a timer.
VirtualBox/Parallels were not tried. A real Windows PC -- any laptop, ten
minutes -- is the practical way to run this list. The unattended VM
definition is kept under `~/vm` on that Mac in case a working hypervisor
turns up.

An Apple-silicon Mac runs Windows 11 under UTM at a usable speed (set up
2026-09-26), but that Windows is ARM64, running the x64 `blink.exe` under
emulation. It covers the Windows on Arm section above and the installer;
it is not the x64 machine most customers have, so this list still wants a
real one.
