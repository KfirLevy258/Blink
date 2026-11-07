# Plugging the cable into a board running on WiFi restarts it

Found on the owner's desk on 2026-11-07, during the hands-on pass before the
WiFi transport PR. The board was on its phone charger and live over WiFi, and
the cable went in. Four times out of five the board started up again about
4 s after the daemon opened the port. The log showed `connected on
/dev/cu.usbserial-…`, then `/dev/cu.usbserial-… answered; not resetting it`,
then `[boot] logo: company` at +4 s.

## What has been ruled out

None of the firmware's own restarts fired. The heap-low restart, the OTA
revert, the post-install reboot and the fatal handler each print a line on
the console, and none of those lines reached `bridge.log`. The board
answered the daemon's probe before it restarted, so it was alive when the
port opened.

## What it probably is

The reset wiring. The auto-reset circuit is cross-coupled: EN is low while RTS
is asserted and DTR is not (CLAUDE.md, `claude_usage_bridge.py`,
`docs/open-bugs/windows-usb-ota.md`). Some step of plugging in the cable can
open that window for an instant: USB enumeration, the macOS CH340 driver's
line state at open or close, or the daemon's probe-then-reopen. On a
USB-only board it was never visible, because plugging in the cable is what
powers the board up. A WiFi board on a charger is already running when the
cable arrives, so a restart now shows.

## Cost today

The board rejoins WiFi about 8 s later and carries on over USB. There is no
data loss and nothing a customer must do, but the restart animation plays.

## Next step

Read the reset cause `hello` carries (`"reset":"0x…"`, `hwinfo_get_reset_cause`)
at the moment of the restart: is it a pin reset or a power-on? Then capture
the DTR/RTS line states the daemon's probe produces on macOS. If the probe
causes it, the fix belongs in the port-open path. If enumeration causes it,
nothing on the Mac can avoid it.
