#!/bin/bash
# Remove the lab Mac's archive jobs (ops/ARCHIVE.md): unload both LaunchAgents and
# delete their plists and the copied scripts. The archive itself (ciphertexts,
# restored databases, keyring, logs) is never touched.
#
# Honours the same WW3_LIBEXEC and WW3_LAUNCHAGENTS as install.sh.

set -euo pipefail

PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

libexec="${WW3_LIBEXEC:-$HOME/.local/libexec/ww3}"
agents="${WW3_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
uid="$(id -u)"

for label in dev.ww3-intelligence.lab.archive-pull dev.ww3-intelligence.lab.dispatch-ingest; do
  if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$uid/$label" || true
    echo "unloaded $label"
  fi
  rm -f "$agents/$label.plist"
done
rm -f "$libexec/lab_archive.py" "$libexec/archive-daily.sh"
rmdir "$libexec" 2>/dev/null || true
echo "The archive and its keyring are untouched."
