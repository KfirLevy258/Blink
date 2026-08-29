#!/bin/sh
# Stand-in for espefuse.py: a summary with FLASH_CRYPT_CNT set from
# FAKE_EFUSE_BITS (0000000 = plaintext chip, 0000001 = fused). One line per
# fuse, value on the same line, which is what lib_efuse.sh parses.
echo "espefuse $*" >>"${FAKE_TOOL_LOG:?}"
bits="${FAKE_EFUSE_BITS:-0000000}"
cat <<SUMMARY
Identity fuses:
MAC (BLOCK0)                                       Factory MAC Address = 20:50:0d:33:40:dc (OK) R/W
Security fuses:
FLASH_CRYPT_CNT (BLOCK0)                           Flash encryption mode counter = 0 R/W (0b$bits)
SUMMARY
exit 0
