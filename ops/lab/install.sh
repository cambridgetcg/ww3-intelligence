#!/bin/bash
# Install the lab Mac's archive jobs as LaunchAgents (ops/ARCHIVE.md).
#
# Copies ops/lab_archive.py and the daily wrapper to a stable directory outside
# any checkout, renders the two plist templates into ~/Library/LaunchAgents, and
# (re)loads them in this user's GUI session. Running it again replaces the copies
# and reloads the jobs. It never touches the archive's contents or its keyring.
#
#   WW3_LIBEXEC        where the copies go          (default ~/.local/libexec/ww3)
#   WW3_PYTHON         Python 3.11 or newer         (default: python3.13, 3.12 or 3.11 on PATH)
#   WW3_ARCHIVE_ROOT   the archive                  (default ~/ww3-archive)
#   WW3_GH, WW3_GPG    the gh and gpg executables   (default: found on PATH)
#   WW3_REPO           the repository               (default cambridgetcg/ww3-intelligence)
#   WW3_LAUNCHAGENTS   where the plists go          (default ~/Library/LaunchAgents)
#
#   ops/lab/install.sh            install and load both jobs
#   ops/lab/install.sh --no-load  copy and render only, to read the result first

set -euo pipefail
umask 077

PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

load=1
case "${1:-}" in
  "") ;;
  --no-load) load=0 ;;
  *) echo "usage: $0 [--no-load]" >&2; exit 2 ;;
esac

here="$(cd "$(dirname "$0")" && pwd)"
ops="$(dirname "$here")"
labels=(dev.ww3-intelligence.lab.archive-pull dev.ww3-intelligence.lab.dispatch-ingest)

libexec="${WW3_LIBEXEC:-$HOME/.local/libexec/ww3}"
root="${WW3_ARCHIVE_ROOT:-$HOME/ww3-archive}"
agents="${WW3_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
repo="${WW3_REPO:-cambridgetcg/ww3-intelligence}"
gh_bin="${WW3_GH:-$(command -v gh || true)}"
gpg_bin="${WW3_GPG:-$(command -v gpg || true)}"
python_bin="${WW3_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
      python_bin="$(command -v "$candidate")"
      break
    fi
  done
fi

# macOS ships bash 3.2, so this stays within what it understands.
for pair in "PYTHON:$python_bin" "GH:$gh_bin" "GPG:$gpg_bin"; do
  name="${pair%%:*}" path="${pair#*:}"
  if [[ -z "$path" || "$path" != /* || ! -x "$path" ]]; then
    echo "No usable executable for WW3_$name (set it to an absolute path)." >&2
    exit 2
  fi
done
if ! "$python_bin" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "$python_bin is older than Python 3.11." >&2
  exit 2
fi
for path in "$libexec" "$root" "$agents"; do
  if [[ "$path" != /* ]]; then
    echo "Paths must be absolute: $path" >&2
    exit 2
  fi
done

# The archive itself is set up by a person (ops/ARCHIVE.md); only its logs
# directory is needed here, for launchd's stdout and stderr.
mkdir -p "$root/logs"
chmod 700 "$root" "$root/logs"
if [[ ! -d "$root/gnupg" ]]; then
  echo "warning: $root/gnupg does not exist yet; pull cannot decrypt until the archive key is in place." >&2
fi

mkdir -p "$libexec" "$agents"
install -m 0700 "$ops/lab_archive.py" "$libexec/lab_archive.py"
install -m 0700 "$here/archive-daily.sh" "$libexec/archive-daily.sh"

uid="$(id -u)"
for label in "${labels[@]}"; do
  target="$agents/$label.plist"
  # Values reach the renderer through the environment, are XML-escaped as they
  # go in, and nothing may be left unfilled.
  FILL_HOME="$HOME" FILL_PYTHON="$python_bin" FILL_LIBEXEC="$libexec" \
  FILL_ARCHIVE_ROOT="$root" FILL_GH="$gh_bin" FILL_GPG="$gpg_bin" FILL_REPO="$repo" \
    "$python_bin" -I - "$here/$label.plist.in" "$target" <<'PYEOF'
import os, re, sys
from xml.sax.saxutils import escape

text = open(sys.argv[1], encoding="utf-8").read()
for key in ("HOME", "PYTHON", "LIBEXEC", "ARCHIVE_ROOT", "GH", "GPG", "REPO"):
    text = text.replace(f"@@{key}@@", escape(os.environ[f"FILL_{key}"]))
left = re.findall(r"@@[A-Z_]+@@", text)
if left:
    sys.exit(f"unfilled placeholders: {sorted(set(left))}")
fd = os.open(sys.argv[2] + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
os.replace(sys.argv[2] + ".tmp", sys.argv[2])
PYEOF
  chmod 0644 "$target"
  plutil -lint -s "$target"

  if [[ "$load" -eq 1 ]]; then
    if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
      launchctl bootout "gui/$uid/$label" || true
      # bootout returns before the job is gone; bootstrap fails until it is.
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "gui/$uid/$label" >/dev/null 2>&1 || break
        sleep 0.5
      done
    fi
    # A label someone once ran `launchctl disable` on stays disabled across
    # bootouts and reboots, and bootstrap then fails; installing means wanting
    # it to run. If enabling fails, bootstrap below still says why.
    launchctl enable "gui/$uid/$label" || true
    launchctl bootstrap "gui/$uid" "$target"
    echo "loaded $label"
  else
    echo "rendered $target (not loaded)"
  fi
done
