#!/bin/sh
# Prove the shipped binary can replace itself -- and refuses to when the feed
# is not properly signed.
#
#   tests/ci/check_update.sh [work-dir]
#
# The unit tests cover apply()/recover()/verify_signature() hermetically. What
# they cannot cover is the frozen artifact: whether the signature library
# actually made it into the bundle, whether the binary can overwrite itself on
# this platform, and whether the replacement runs afterwards. Those are exactly
# the failures that only appear on a customer's machine.
#
# POSIX only. The stand-in for "a newer release" is a script that prints a
# version, which Windows cannot execute under a .exe name; the Windows-specific
# half (renaming a running executable out of the way) is unit-tested instead.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"
ci_label update
ci_binary

WORK="${1:-${TMPDIR:-/tmp}/blink-ci-update}"

case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
	echo "SKIP [update] not run on Windows -- see the header" >&2
	exit 0
	;;
esac
command -v openssl >/dev/null 2>&1 || { echo "need openssl" >&2; exit 1; }


rm -rf "$WORK"
HOME="$WORK/home"; export HOME
FEED="$WORK/feed"
mkdir -p "$HOME" "$FEED"

# Which artifact this platform would download.
case "$(uname -s)/$(uname -m)" in
Darwin/arm64) KEY=macos-arm64 ;;
Darwin/*) KEY=macos-x86_64 ;;
Linux/x86_64 | Linux/amd64) KEY=linux-x86_64 ;;
*) echo "SKIP [update] no published build for $(uname -s)/$(uname -m)" >&2; exit 0 ;;
esac

printf '== update (HOME=%s, artifact=%s)\n' "$HOME" "$KEY"

BLINK_SKIP_SERVICE=1 "$BIN" >"$WORK/install.txt" 2>&1 ||
	{ cat "$WORK/install.txt" >&2; fail "install exited non-zero"; }
INSTALLED="$HOME/.blink/bin/blink"
[ -x "$INSTALLED" ] || fail "the binary did not install itself"

# The stand-in for a newer release. It only has to satisfy the self-test --
# run, and say it is the version the manifest promised.
cat >"$FEED/blink-$KEY" <<'EOF'
#!/bin/sh
echo "blink 99.0.0"
EOF
chmod 755 "$FEED/blink-$KEY"

openssl ecparam -name prime256v1 -genkey -noout -out "$WORK/test-key.pem" 2>/dev/null
openssl ec -in "$WORK/test-key.pem" -pubout -out "$WORK/test-pub.pem" 2>/dev/null

write_manifest() {
	FEED="$FEED" KEY="$KEY" python3 - >"$FEED/manifest.json" <<'EOF'
import hashlib, json, os
feed, key = os.environ["FEED"], os.environ["KEY"]
blob = open(os.path.join(feed, "blink-" + key), "rb").read()
print(json.dumps({
    "version": "0.0.1", "size": 1, "sha256": "00" * 32,   # firmware: unused here
    "schema": 2,
    "daemon": {"version": "99.0.0", "proto": 2, "auto": False,
               "artifacts": {key: {"size": len(blob),
                                   "sha256": hashlib.sha256(blob).hexdigest()}}},
}))
EOF
	openssl dgst -sha256 -sign "$WORK/test-key.pem" \
		-out "$FEED/manifest.json.sig" "$FEED/manifest.json"
}
write_manifest

export BLINK_OTA_DIR="$FEED"

# --- 1. an unsigned feed must do nothing -------------------------------------
# The first thing to prove, because it is the one that matters: if this passes
# by accident the rest of the test is checking a door with no lock.
BLINK_SKIP_SERVICE=1 "$INSTALLED" update >"$WORK/unsigned.txt" 2>&1 && rc=0 || rc=$?
[ "$rc" -ne 0 ] || fail "update succeeded against a feed signed by an unknown key"
grep -qi "signed" "$WORK/unsigned.txt" ||
	fail "no explanation given: $(cat "$WORK/unsigned.txt")"
"$INSTALLED" --version | grep -qv 99.0.0 || fail "an unsigned feed replaced the binary"
ok "a manifest signed by an unknown key is refused"

# --- 2. correctly signed: it replaces itself ---------------------------------
export BLINK_RELEASE_PUBKEY_FILE="$WORK/test-pub.pem"
BLINK_SKIP_SERVICE=1 "$INSTALLED" update >"$WORK/update.txt" 2>&1 ||
	{ cat "$WORK/update.txt" >&2; fail "update exited non-zero"; }
"$INSTALLED" --version | grep -q "99.0.0" ||
	fail "the binary was not replaced: $("$INSTALLED" --version)"
ok "a signed release replaces the running binary"
[ -f "$INSTALLED.old" ] || fail "no rollback copy kept"
ok "the previous binary is kept for rollback"
[ ! -e "$INSTALLED.new" ] || fail "a staging file was left behind"

# --- 3. a tampered manifest must be refused ----------------------------------
cp "$INSTALLED.old" "$INSTALLED"          # back to the real binary
sed 's/99\.0\.0/99.0.1/' "$FEED/manifest.json" >"$FEED/manifest.tmp"
mv "$FEED/manifest.tmp" "$FEED/manifest.json"   # signature no longer covers it
BLINK_SKIP_SERVICE=1 "$INSTALLED" update >"$WORK/tampered.txt" 2>&1 && rc=0 || rc=$?
[ "$rc" -ne 0 ] || fail "update accepted a manifest that had been edited"
"$INSTALLED" --version | grep -qv 99.0.0 || fail "a tampered manifest replaced the binary"
ok "an edited manifest is refused"

# --- 4. a binary that does not match its hash --------------------------------
write_manifest
# Same length, different bytes: appending would trip the size check first and
# never reach the hash comparison this step exists to prove.
sed 's/blink 99/blinK 99/' "$FEED/blink-$KEY" >"$FEED/tmp-bin"
mv "$FEED/tmp-bin" "$FEED/blink-$KEY"
chmod 755 "$FEED/blink-$KEY"
BLINK_SKIP_SERVICE=1 "$INSTALLED" update >"$WORK/badhash.txt" 2>&1 && rc=0 || rc=$?
[ "$rc" -ne 0 ] || fail "update accepted a download whose hash did not match"
grep -qi "sha256" "$WORK/badhash.txt" ||
	fail "no explanation given: $(cat "$WORK/badhash.txt")"
ok "a download that does not match its hash is refused"

printf 'PASS [update]\n'
