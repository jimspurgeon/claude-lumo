#!/bin/bash
# migrate-from-lumo.sh: move sessions from the separate data store (~/.claude-lumo/data)
# back into the shared store (~/.claude), undoing migrate-to-lumo.sh. Thin wrapper around
# separate_data.py. See scripts/separate_data.py for details.
set -e
PY="python"
command -v "$PY" >/dev/null 2>&1 || PY="python3"
exec "$PY" "$(dirname "$0")/scripts/separate_data.py" revert-migrate "$@"
