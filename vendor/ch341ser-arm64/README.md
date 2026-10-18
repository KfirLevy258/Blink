# CH340 driver for Windows on Arm (WCH CH341SER)

The same driver as `vendor/ch341ser`, in the package that also carries an
Arm64 build. `pc/win_driver.py` installs this one only when Windows itself
runs on Arm64 (`native_machine()`); every x64 PC keeps the package in
`vendor/ch341ser`, which is the one the release desk proved.

## Why a second package

The x64 package came out of a Windows 10 driver store, so it holds only
what that machine needed. Its `.INF` declares an `NTARM64` install that
copies `CH341M64.SYS`, and its Microsoft-signed `.CAT` lists that file -- but
the file itself was never in the store copy. On a Windows 11 ARM64 VM,
2026-09-30, `pnputil /add-driver` failed with exit 2, `ERROR_FILE_NOT_FOUND`.
That was read as "WCH publishes no Arm64 driver", and for a release the
install told Arm customers no driver exists. A review (2026-10-19) found
the `NTARM64` section.

The `.SYS` cannot be added to the old package: the catalogue holds a hash of
each file, and WCH's current download is a newer version whose files differ.
So this is WCH's current package, whole.

## Provenance

- **Vendor:** WCH, <https://www.wch-ic.com/downloads/CH341SER_EXE.html>
- **Download:** `https://www.wch-ic.com/download/file?id=65`, 2026-10-19:
  `CH341SER.EXE`, a RAR self-extractor, sha256
  `458c37bdafbe4ce3cd0baf728c232b4b765b36d7463956e9b94cdf099212cad1`.
  These are the nine files of its `WIN 1X/` folder, unchanged.
- **Version:** 4.0.2026.02, `DriverVer` 02/11/2026
- **Signed by:** Microsoft Windows Hardware Compatibility Publisher
- **`CH341M64.sys`:** PE32+, Aarch64

## Measured

Windows 11 ARM64 VM (UTM), 2026-10-19, elevated:

| Package | `pnputil /add-driver` |
|---|---|
| `vendor/ch341ser` (3.9.2024.09) | exit 2, "The system cannot find the file specified" |
| this one (4.0.2026.02) | exit 0, "Driver package added successfully", signer Microsoft Windows Hardware Compatibility Publisher |

That proves Windows accepts the package on Arm64. It does not prove a board
binds to it: that needs the board passed through to an Arm machine, and is
the check to run before saying Windows on Arm works (see
`docs/windows-check.md`).

## Bytes

`.gitattributes` marks this directory binary. The `.CAT` holds a hash of the
`.INF`, and the repository's LF rule would otherwise rewrite the `.INF` at
checkout and break the signature.
