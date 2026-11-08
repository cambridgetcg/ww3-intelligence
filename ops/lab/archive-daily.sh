#!/bin/sh
# The lab Mac's daily archive job (ops/ARCHIVE.md): request and collect an export,
# then report on the collector. watch runs even when pull fails, because a failed
# pull is when the rest of the picture matters most. Exits non-zero if either did.

set -u

here="$(cd "$(dirname "$0")" && pwd)"
python_bin="${WW3_PYTHON:?WW3_PYTHON is not set}"

"$python_bin" -I "$here/lab_archive.py" pull --dispatch
pull=$?
"$python_bin" -I "$here/lab_archive.py" watch
watch=$?

[ "$pull" -eq 0 ] && [ "$watch" -eq 0 ]
