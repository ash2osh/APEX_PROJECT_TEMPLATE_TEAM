#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "Python 3.10+ is required" >&2
  exit 2
fi
exec "$PYTHON_BIN" "$REPO_ROOT/scripts/team.py" "$@"
