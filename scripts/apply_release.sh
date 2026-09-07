#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHONPATH="$REPO_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m teamlib.release_adapter "$@"
