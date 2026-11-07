# WiFi OTA: what has been proved on a board

Written 2026-11-07 from the transport task ledger. The bench steps ran
between 2026-11-05, when the work started, and 2026-11-07; the ledger does not
date each one separately. Nothing here names a network, address, board or key.

## How the route works

Over WiFi the board writes the new image to the spare slot (slot 1), hashes
it itself and asks MCUboot to test-boot it. If the new image has not
confirmed itself within 90 seconds, the board reboots and MCUboot reverts to
the old one. The cable route is different and is unchanged: esptool writes the
running slot and reads it back, with no rollback.

## Proved on a bench board

- **A full WiFi update completes.** 53 to 57 s for the transfer, about
  21 kB/s, then verified, swapped and confirmed.
- **The revert works.** A forced failure to confirm reverted to the old image.
- **The refusals work.** A begin while an image is unconfirmed is refused, an
  update with no consent tap is refused, and every abort path ends cleanly.
- **Frame lengths.** Every length up to the 512-byte line limit passes.
- **With the real daemon.** After the transfer the board joined WiFi about
  66 s after the end of the transfer, the daemon reconnected 1 s later, the new
  image confirmed about 68 s after the end of the transfer and the first usage
  frame arrived 6 s after reconnect, inside the 90 s window.
- **A logo over WiFi.** A GIF logo took 5.6 s, and every preference field
  applied.
- **Charger-only desk run, 2026-11-07.** About 9.7 hours on WiFi with no
  cable. The daemon log shows four drops, each explained (a daemon restart,
  two Mac sleeps and a router restart, and plugging the cable in), and none
  unexplained, so no silent board reset was seen.
- **Final image.** The final firmware was flashed and boot-verified, and the
  stack and heap measurements were taken on it.

## Not proved, still owed

- **A fused unit.** No encrypted-flash board has taken a WiFi update. Fused
  units exist, so this comes first.
- **Power cuts.** Losing power mid-transfer and during the test boot, on a
  real board.
- **A router cut mid-transfer.**
- **The 24 hour soak.** The desk run was stopped at about 9.7 hours, on an
  image that was not the final one. The 24 hours on the final image is still
  to do.
- **A silent restart shorter than 35 s.** The board does not report uptime, so
  a reboot inside the daemon's 35 s silence window would have gone unseen.
- **A slow router.** The 90 s window was kept as it is on purpose. On a slow
  network a good update can read as failed and revert; the revert itself is
  safe.
- **A newer image arriving during a test boot**, and the heap-low restart.

The failure modes found while proving this are in `docs/open-bugs/`.
