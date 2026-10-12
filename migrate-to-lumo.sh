#!/bin/bash
# migrate-to-lumo.sh: move Lumo sessions from the shared store (~/.claude) into the
# separate data store (~/.claude-lumo/data). Thin wrapper around separate_data.py.
# See scripts/separate_data.py for what moves and what stays.
set -e
PY="python"
command -v "$PY" >/dev/null 2>&1 || PY="python3"
exec "$PY" "$(dirname "$0")/scripts/separate_data.py" migrate "$@"
