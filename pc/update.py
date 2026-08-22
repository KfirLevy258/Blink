"""The daemon updating itself.

The board could always update itself; this half never could. A customer's
binary stayed at whatever they downloaded, forever, while the firmware feed it
drives moved on -- and since the daemon is what decides and performs firmware
updates, the stale half was the one in charge.

Three things guard this path, because it is the highest-privilege operation in
the product: a login agent on someone's machine, replacing its own executable
with bytes from the internet.

  1. The manifest is SIGNED, and this refuses to read the daemon block out of
     one that does not verify. The firmware has had this all along -- MCUboot
     will not boot an image signed by anything but the release key -- so a
     compromised release feed could never push firmware. It could have pushed
     a daemon. The key here is deliberately NOT MCUboot's: one key, one job.
  2. The download is checked against the size and sha256 the manifest names.
  3. The replacement is SELF-TESTED before it becomes the service's target --
     the new binary has to run and report the version it claims. A corrupt
     binary that reaches the login service is a device that never comes back
     and a customer who has to start over; 200 ms of proof is cheap next to
     that.

And one brake that is not technical: `daemon.auto` in the manifest. Ship with
it false, turn it on when the path has earned it, turn it off within minutes if
a release goes wrong. Auto-update without a remote off switch is a mechanism
with no brakes.
"""
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys

from pc import ota
from pc.version import RELEASE_VERSION

MANIFEST_URL = ota.RELEASE_BASE + "manifest.json"
SIG_URL = ota.RELEASE_BASE + "manifest.json.sig"

# The public half of ~/.clauge/release_signing_key_p256.pem. Signing happens in
# tools/release.sh; losing the private half means no fielded daemon will ever
# accept another update, so it is backed up the same way MCUboot's key is.
RELEASE_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEs++ur2jHlamykVsPeCvtT/VE5Awy
JK1K9T7tpqx6hxXWLKxorhWH6Pkxs8Bd/jzv4Zsk2yTOhaUE+dZmSt24Yw==
-----END PUBLIC KEY-----
"""

# Opt out regardless of what the manifest says. A file rather than only an
# environment variable because the daemon is started by launchd/systemd, where
# nobody's shell exports anything.
NO_AUTO_ENV = "CLAUGE_NO_AUTO_UPDATE"

# Verify against a different public key. This exists so the update path can be
# exercised end to end against a local feed -- tests/ci/check_update.sh signs a
# throwaway manifest and points this at the matching public half -- because the
# real key lives on one machine and CI must never have it.
#
# It is not a weakening: setting an environment variable for this process means
# already being able to run code as this user, at which point the binary itself
# is writable. What it must never become is a way to skip verification, so an
# unreadable file falls back to the embedded key rather than to trusting
# whatever arrived.
PUBKEY_ENV = "CLAUGE_RELEASE_PUBKEY_FILE"


def _pubkey():
    path = os.environ.get(PUBKEY_ENV)
    if path:
        try:
            with open(path) as f:
                return f.read()
        except OSError as e:
            print(f"[update] cannot read {path}: {e}; using the built-in key",
                  file=sys.stderr)
    return RELEASE_PUBKEY_PEM


def auto_update_allowed(clauge_home) -> bool:
    if os.environ.get(NO_AUTO_ENV) == "1":
        return False
    return not os.path.exists(os.path.join(clauge_home, "no-auto-update"))


def platform_key():
    """Which release artifact this process should be replaced with, or None.

    Keyed off the RUNNING process, not the silicon: an x86_64 build under
    Rosetta reports x86_64, and replacing it with an arm64 binary would be an
    architecture change rather than an update.
    """
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    if system == "Linux":
        return "linux-x86_64" if machine in ("x86_64", "amd64") else None
    if system == "Windows":
        return "windows-x86_64.exe"
    return None


# --- the signed manifest ------------------------------------------------

def verify_signature(raw: bytes, sig: bytes, pubkey_pem=RELEASE_PUBKEY_PEM) -> bool:
    """True if `sig` is a valid P-256/SHA-256 signature over `raw`.

    Uses openssl when the library is missing so a source checkout without the
    dependency still verifies rather than silently skipping the check. The
    shipped binary always has the library -- pc/requirements.txt pins it and
    PyInstaller freezes it in -- so the fallback is a developer convenience,
    never the customer's path.
    """
    try:
        import ecdsa
    except ImportError:
        return _verify_with_openssl(raw, sig, pubkey_pem)
    try:
        vk = ecdsa.VerifyingKey.from_pem(pubkey_pem)
        return vk.verify(sig, raw, hashfunc=hashlib.sha256,
                         sigdecode=ecdsa.util.sigdecode_der)
    except Exception:
        return False


def _verify_with_openssl(raw, sig, pubkey_pem):
    import shutil
    import tempfile
    if not shutil.which("openssl"):
        return False
    d = tempfile.mkdtemp(prefix="clauge-verify-")
    try:
        paths = {}
        for name, data in (("pub.pem", pubkey_pem.encode()),
                           ("sig", sig), ("data", raw)):
            paths[name] = os.path.join(d, name)
            with open(paths[name], "wb") as f:
                f.write(data)
        r = subprocess.run(["openssl", "dgst", "-sha256",
                            "-verify", paths["pub.pem"],
                            "-signature", paths["sig"], paths["data"]],
                           capture_output=True)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        import shutil as sh
        sh.rmtree(d, ignore_errors=True)


def fetch_signed_manifest(get=ota._get):
    """The manifest, or None if it is absent, malformed or not properly signed.

    None on a bad signature rather than an exception: the caller's job is to
    keep the bridge running, and a feed we cannot trust is a reason to do
    nothing, not a reason to fall over.
    """
    try:
        raw = get(MANIFEST_URL)
        sig = get(SIG_URL)
    except Exception:
        return None
    if not verify_signature(raw, sig, _pubkey()):
        print("[update] manifest signature did not verify -- ignoring the feed",
              file=sys.stderr)
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def available(manifest, current=RELEASE_VERSION, key=None):
    """(version, artifact) for a newer build of this platform, else None."""
    if not manifest:
        return None
    daemon = manifest.get("daemon") or {}
    version = daemon.get("version")
    if not ota.is_newer(version, current):
        return None
    key = key or platform_key()
    artifact = (daemon.get("artifacts") or {}).get(key)
    if not artifact or "sha256" not in artifact or "size" not in artifact:
        return None
    return version, artifact


def download(key, artifact, get=ota._get):
    """The new binary's bytes, checked against the manifest. Raises on either
    mismatch -- there is nothing sensible to do with a download we cannot
    identify, and running it is certainly not it."""
    blob = get(ota.RELEASE_BASE + "clauge-" + key, timeout=300)
    if len(blob) != artifact["size"]:
        raise ValueError(f"size {len(blob)} != manifest {artifact['size']}")
    digest = hashlib.sha256(blob).hexdigest()
    want = str(artifact["sha256"]).strip().lower()
    if digest != want:
        raise ValueError(f"sha256 {digest} != manifest {want}")
    return blob


# --- putting it in place ------------------------------------------------

# Generous, because the cost of being wrong is asymmetric.
#
# The binary needs about two seconds of CPU to answer --version (it unpacks
# itself first), but wall-clock is what a timeout measures. Measured at 97 s on
# a machine under load average 89 -- a developer's laptop mid-build, which is
# exactly when someone might run this. Timing out means refusing a good update,
# so the limit is set where only a genuinely hung binary reaches it.
SELF_TEST_TIMEOUT_S = 300


def _self_test(path, expect_version, run=subprocess.run) -> bool:
    try:
        r = run([path, "--version"], capture_output=True, text=True,
                timeout=SELF_TEST_TIMEOUT_S)
    except Exception:
        return False
    return r.returncode == 0 and expect_version in (r.stdout or "")


def apply(blob, target, version, run=subprocess.run):
    """Replace the binary at `target`. Returns (ok, message).

    Does NOT restart anything: which supervisor owns this process, and whether
    it is this process, is the caller's business (see cli.restart_service).
    """
    d = os.path.dirname(target)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        return False, f"cannot write to {d}: {e}"

    new, old = target + ".new", target + ".old"
    try:
        with open(new, "wb") as f:
            f.write(blob)
        os.chmod(new, os.stat(new).st_mode | stat.S_IXUSR | stat.S_IXGRP
                 | stat.S_IXOTH)
    except OSError as e:
        _rm(new)
        return False, f"could not stage the download: {e}"

    if not _self_test(new, version, run=run):
        _rm(new)
        return False, ("the downloaded binary did not run -- keeping the"
                       " current one")

    try:
        _rm(old)
        if os.path.exists(target):
            # Windows will not overwrite a running executable, but it will
            # rename one out of the way. Everywhere else this is just the
            # rollback copy.
            os.replace(target, old)
        os.replace(new, target)
    except OSError as e:
        # Put back whatever we moved, so a half-applied update is not left
        # pointing the login service at nothing.
        if not os.path.exists(target) and os.path.exists(old):
            try:
                os.replace(old, target)
            except OSError:
                pass
        _rm(new)
        return False, f"could not replace {target}: {e}"
    return True, f"updated to {version}"


def recover(target) -> bool:
    """Restore the previous binary if the current one is missing or empty.

    Called on every start. The window it covers is small -- between the two
    renames in apply() -- but what it prevents is not: a login service whose
    program does not exist never runs again, and nothing on the board or the
    computer would explain why.
    """
    old = target + ".old"
    try:
        healthy = os.path.exists(target) and os.path.getsize(target) > 0
    except OSError:
        healthy = False
    if healthy or not os.path.exists(old):
        return False
    try:
        os.replace(old, target)
        print(f"[update] restored the previous binary at {target}",
              file=sys.stderr)
        return True
    except OSError:
        return False


def restart_from_daemon(target):
    """Hand over to the freshly written binary, from inside the daemon itself.

    On macOS and Linux this just exits: launchd's KeepAlive and systemd's
    Restart=always exist for crashes, and an update is the one time we want
    them. Windows has neither -- `schtasks /sc onlogon` fires at logon and
    never again -- so the replacement is started explicitly before this one
    goes away. The task still points at the same path, so nothing is orphaned
    beyond the current session.
    """
    if sys.platform == "win32":
        DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_GROUP
        try:
            subprocess.Popen([target, "run"], creationflags=DETACHED,
                             close_fds=True)
        except Exception as e:
            print(f"[update] could not start the new binary: {e}",
                  file=sys.stderr)
            return                            # stay up rather than vanish
    print("[update] restarting into the new version", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def cleanup(target):
    """Drop the rollback copy once we are running and healthy."""
    _rm(target + ".old")


class PendingFirmware:
    """The firmware version the user already approved, across our own restart.

    A pair update is one tap on the board, but it is two operations and the
    daemon replaces itself in the middle of them -- so the consent has to
    outlive the process that received it. One line in a file does that.

    take() clears before the caller acts on it, deliberately. If flashing goes
    wrong the right outcome is a board still running its old firmware and a
    user who can tap again, not a daemon that retries the same failing install
    on every reconnect for the rest of its life.
    """

    def __init__(self, path):
        self.path = path

    def set(self, version):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump({"version": version}, f)
        except OSError as e:
            print(f"[update] could not record the pending update: {e}",
                  file=sys.stderr)

    def take(self):
        try:
            with open(self.path) as f:
                version = json.load(f).get("version")
        except (OSError, ValueError):
            return None
        _rm(self.path)
        return version or None


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass
