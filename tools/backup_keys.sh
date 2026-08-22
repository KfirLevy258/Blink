#!/bin/sh
# Make a verified, portable backup of the two signing keys.
#
#   tools/backup_keys.sh [destination-dir]
#
# There are two, they live only in ~/.clauge, and neither can be regenerated:
#
#   ota_signing_key_p256.pem      MCUboot's. Every board ever flashed accepts
#                                 firmware signed with this and nothing else.
#   release_signing_key_p256.pem  Signs manifest.json. Every app ever installed
#                                 refuses an update whose manifest does not
#                                 verify against this key's public half, which
#                                 is compiled into the binary.
#
# Losing either strands every device that has already shipped -- and the
# devices that would need telling are exactly the ones that stopped listening.
#
# This script does the part a copy cannot: it PROVES the backup restores, by
# signing with the copied key and verifying with the public half the shipped
# software actually carries. A file that turns out to be truncated is not a
# backup, and the moment to find that out is now.
#
# It deliberately does not encrypt, upload, or mail anything. Choose a
# passphrase yourself and put the result somewhere that is not this laptop --
# the command is printed at the end.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
SRC="${CLAUGE_HOME:-$HOME/.clauge}"
STAMP=$(date +%Y-%m-%d)
DEST="${1:-$HOME/clauge-key-backup-$STAMP}"

OTA="ota_signing_key_p256.pem"
REL="release_signing_key_p256.pem"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf '  ok   %s\n' "$*"; }

for k in "$OTA" "$REL"; do
	[ -f "$SRC/$k" ] || fail "no $k in $SRC"
done
command -v openssl >/dev/null 2>&1 || fail "openssl not found"

umask 077
mkdir -p "$DEST"
chmod 700 "$DEST"
cp "$SRC/$OTA" "$SRC/$REL" "$DEST/"
chmod 600 "$DEST/$OTA" "$DEST/$REL"

# Public halves alongside, so the backup can be identified without being used.
openssl ec -in "$DEST/$OTA" -pubout -out "$DEST/ota_public.pem" 2>/dev/null
openssl ec -in "$DEST/$REL" -pubout -out "$DEST/release_public.pem" 2>/dev/null
ok "copied both keys and derived their public halves"

# --- prove the copies work -------------------------------------------------
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
printf 'clauge backup round trip %s' "$STAMP" >"$TMP/probe"

for pair in "$OTA:ota_public.pem" "$REL:release_public.pem"; do
	key=${pair%%:*}
	pub=${pair#*:}
	openssl dgst -sha256 -sign "$DEST/$key" -out "$TMP/sig" "$TMP/probe" ||
		fail "$key in the backup cannot sign -- it is not a usable key"
	openssl dgst -sha256 -verify "$DEST/$pub" -signature "$TMP/sig" \
		"$TMP/probe" >/dev/null ||
		fail "$key signed but its own public half will not verify it"
	ok "$key restores and signs"
done

# --- and prove the RELEASE key is the one the shipped binary trusts ---------
# The public half is compiled into pc/update.py. A backup of the wrong key
# would pass every check above and still be worthless.
ROOT="$ROOT" DEST="$DEST" TMP="$TMP" python3 - <<'PYEOF' || fail "the backed-up release key is NOT the one the app trusts"
import os, subprocess, sys
sys.path.insert(0, os.environ["ROOT"])
from pc import update

tmp, dest = os.environ["TMP"], os.environ["DEST"]
subprocess.run(["openssl", "dgst", "-sha256", "-sign",
                os.path.join(dest, "release_signing_key_p256.pem"),
                "-out", os.path.join(tmp, "sig2"),
                os.path.join(tmp, "probe")], check=True)
raw = open(os.path.join(tmp, "probe"), "rb").read()
sig = open(os.path.join(tmp, "sig2"), "rb").read()
sys.exit(0 if update.verify_signature(raw, sig) else 1)
PYEOF
ok "the release key matches the public half compiled into the app"

( cd "$DEST" && shasum -a 256 ./*.pem >SHA256SUMS )

cat >"$DEST/README.txt" <<EOF
Clauge signing keys, backed up $STAMP.

  $OTA
      MCUboot's key. Firmware images are signed with it by tools/release.sh.
      Every board already flashed rejects anything else. Restore to:
          ~/.clauge/$OTA

  $REL
      Signs manifest.json, which is what tells an installed app that an
      update is genuine. The matching public half is compiled into the app
      as RELEASE_PUBKEY_PEM in pc/update.py. Restore to:
          ~/.clauge/$REL

Restore is a copy: put the file back at the path above with mode 600.
Verify a restore by running tools/backup_keys.sh again -- it signs with the
copy and checks the result against the key the shipped software carries.

Check integrity of this backup:   shasum -a 256 -c SHA256SUMS

These are private keys. Anyone holding them can publish firmware and app
updates that every Clauge device will accept as yours.
EOF

printf '\nBacked up to %s\n' "$DEST"
printf '\nThis is still one machine. To finish:\n'
printf '  1. Encrypt it (choose your own passphrase):\n'
printf '       tar -C "%s" -czf - . | openssl enc -aes-256-cbc -pbkdf2 -out ~/clauge-keys-%s.tar.gz.enc\n' "$DEST" "$STAMP"
printf '  2. Put the encrypted file on TWO things that are not this laptop.\n'
printf '  3. Store the passphrase somewhere the laptop is not needed to read.\n'
printf '\nRestore later:\n'
printf '     openssl enc -d -aes-256-cbc -pbkdf2 -in ~/clauge-keys-%s.tar.gz.enc | tar -xzf - -C <dir>\n' "$STAMP"
