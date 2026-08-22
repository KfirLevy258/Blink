"""The release manifest: one definition, deliberately frozen.

This shape is a contract with software we cannot reach. Once a customer's app
has installed itself, it reads this document to decide whether to install
another one -- so a key that moves strands exactly the installs that would need
telling. The window in which the shape is a free choice closes with the first
published release.

The layout, and why it is this way:

    version / size / sha256   THE FIRMWARE, at the top level. Not nested under
                              "fw" for symmetry, because pc/ota.py has read
                              them from there since before any of the rest
                              existed and every board's update check goes
                              through that path. Symmetry is worth less than
                              not moving them.

    schema                    An integer, so a reader can tell what it is
                              looking at without guessing from which keys are
                              present.

    fw.proto_min              The protocol version an app must speak to be
                              allowed to install this firmware. Absent means no
                              floor, which is how every release before this one
                              reads.

    daemon.version            The app half of the same release. Same number as
    daemon.proto              the firmware's by construction -- they ship from
                              one tag and tests/ci/check_versions.sh enforces
                              it -- but written out, because a reader should
                              not have to know that to use this.

    daemon.auto               Whether an installed app may update itself
                              without being asked. The remote brake.

    daemon.artifacts          Per-platform size and sha256, keyed by the names
                              pc/update.py:platform_key() produces.

Everything after the first three keys is additive: a reader that predates a
field ignores it, which is what lets this grow without stranding anyone. Adding
is safe. Moving and renaming are not, and tests/pc/test_manifest_contract.py
exists to make either of those a deliberate act rather than an accident.
"""

SCHEMA = 2

# The platform names the daemon asks for. Ordered for a stable document.
ARTIFACT_KEYS = ("macos-arm64", "macos-x86_64", "linux-x86_64",
                 "windows-x86_64.exe")


def build(version, fw_size, fw_sha256, proto, artifacts, auto=False):
    """The manifest document, as published.

    `artifacts` maps a platform key to {"size": int, "sha256": str}.
    """
    return {
        # Firmware. These three keys never move -- see the module docstring.
        "version": version,
        "size": int(fw_size),
        "sha256": fw_sha256,

        "schema": SCHEMA,
        "fw": {"proto_min": int(proto)},
        "daemon": {
            "version": version,
            "proto": int(proto),
            "auto": bool(auto),
            "artifacts": {k: {"size": int(artifacts[k]["size"]),
                              "sha256": artifacts[k]["sha256"]}
                          for k in ARTIFACT_KEYS if k in artifacts},
        },
    }
