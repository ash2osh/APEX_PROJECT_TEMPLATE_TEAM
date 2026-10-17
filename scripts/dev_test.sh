#!/usr/bin/env bash
# Launch the DEV test module from the repository root without preloading .env.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$REPO_ROOT"
exec python3 -m scripts.dev_test "$@"
