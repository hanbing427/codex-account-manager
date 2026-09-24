#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONUTF8=1
for executable in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"; do
    if command -v "$executable" >/dev/null 2>&1 && "$executable" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
        exec "$executable" "$SCRIPT_DIR/launcher.py" "$@"
    fi
done
printf '%s\n' 'Python 3.11+ is required. Install Python, then run this launcher again.' >&2
exit 1
