#!/bin/bash

set -euo pipefail
umask 077

PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

app_name="ww3-intelligence"
flyctl_bin="${WW3_FLYCTL_BIN:-/opt/homebrew/bin/flyctl}"
jq_bin="${WW3_JQ_BIN:-/usr/bin/jq}"
python_bin="${WW3_PYTHON_BIN:-/opt/homebrew/bin/python3}"
backup_dir="${WW3_BACKUP_DIR:-/Users/yu/Backups/ww3-intelligence}"
retention_days="${WW3_BACKUP_RETENTION_DAYS:-30}"
stamp="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
remote_path="/tmp/ww3-backup-${stamp}-$$.db"
local_tmp=""
machine_id=""

case "$retention_days" in
  ''|*[!0-9]*)
    echo "WW3_BACKUP_RETENTION_DAYS must be a non-negative integer." >&2
    exit 2
    ;;
esac

for required in "$flyctl_bin" "$jq_bin" "$python_bin" /usr/bin/ruby; do
  if [[ ! -x "$required" ]]; then
    echo "Required executable is unavailable: $required" >&2
    exit 2
  fi
done

if [[ -n "${FLY_API_TOKEN:-}" ]]; then
  fly_token="$FLY_API_TOKEN"
else
  fly_config="${FLY_CONFIG_PATH:-/Users/yu/.fly/config.yml}"
  if [[ ! -r "$fly_config" ]]; then
    echo "Fly credential file is not readable: $fly_config" >&2
    exit 2
  fi
  fly_token="$(/usr/bin/ruby -ryaml -e '
    config = YAML.load_file(ARGV.fetch(0))
    token = config.is_a?(Hash) ? config["access_token"] : nil
    abort "Fly credential file has no access_token" unless token.is_a?(String) && !token.empty?
    print token
  ' "$fly_config")"
fi

cleanup() {
  if [[ -n "$machine_id" ]]; then
    FLY_API_TOKEN="$fly_token" "$flyctl_bin" ssh console \
      --app "$app_name" --machine "$machine_id" \
      --command "python -c 'import os; p=\"$remote_path\"; os.path.exists(p) and os.unlink(p)'" \
      >/dev/null 2>&1 || true
  fi
  if [[ -n "$local_tmp" && -e "$local_tmp" ]]; then
    /bin/rm -f -- "$local_tmp"
  fi
  unset fly_token
}
trap cleanup EXIT INT TERM

/bin/mkdir -p "$backup_dir"
/bin/chmod 700 "$backup_dir"

machine_id="$(
  FLY_API_TOKEN="$fly_token" "$flyctl_bin" machine list --app "$app_name" --json \
    | "$jq_bin" -r '[.[] | select(.state == "started")][0].id // empty'
)"
if [[ -z "$machine_id" ]]; then
  echo "No started $app_name Machine is available for an online backup." >&2
  exit 1
fi

FLY_API_TOKEN="$fly_token" "$flyctl_bin" ssh console \
  --app "$app_name" --machine "$machine_id" \
  --command "python -c 'import os,sqlite3; src=sqlite3.connect(\"file:/data/ww3.db?mode=ro\",uri=True); dst=sqlite3.connect(\"$remote_path\"); src.backup(dst); result=dst.execute(\"PRAGMA quick_check\").fetchone()[0]; src.close(); dst.close(); os.chmod(\"$remote_path\",0o600); assert result == \"ok\"'"

local_tmp="$backup_dir/.ww3-${stamp}-$$.partial"
if [[ -e "$local_tmp" ]]; then
  echo "Refusing to overwrite an existing backup temporary file: $local_tmp" >&2
  exit 1
fi
FLY_API_TOKEN="$fly_token" "$flyctl_bin" ssh sftp get \
  --app "$app_name" --machine "$machine_id" \
  "$remote_path" "$local_tmp"

"$python_bin" - "$local_tmp" <<'PY'
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1])
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
finally:
    connection.close()
if quick_check is None or quick_check[0] != "ok":
    raise SystemExit("downloaded backup failed SQLite quick_check")
if violations:
    raise SystemExit("downloaded backup has foreign-key violations")
PY

final_path="$backup_dir/ww3-${stamp}.db"
/bin/mv -- "$local_tmp" "$final_path"
local_tmp=""
/bin/chmod 600 "$final_path"

"$python_bin" - "$backup_dir" "$retention_days" <<'PY'
import re
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
cutoff = time.time() - int(sys.argv[2]) * 86400
pattern = re.compile(r"ww3-[0-9]{8}T[0-9]{6}Z\.db")
for candidate in root.iterdir():
    if candidate.is_file() and pattern.fullmatch(candidate.name) and candidate.stat().st_mtime < cutoff:
        candidate.unlink()
PY

echo "Verified WW3 SQLite backup: $final_path"
