#!/usr/bin/env bash
# Check cross-developer DDL collisions, then run reviewed migration files via SQLcl.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/migrate.sh <migrations/<developer>/<file>.sql> [...]

Checks every developer migration for conflicts before applying the selected
file(s) to the configured development database.
USAGE
}

fail() { printf 'migration error: %s\n' "$*" >&2; exit 2; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi
[ "$#" -gt 0 ] || fail "provide at least one migration file"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 "$REPO_ROOT/scripts/check_conflicts.py"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write code

migrations=()
for migration in "$@"; do
  [[ "$migration" =~ ^migrations/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.sql$ ]] || \
    fail "use a repository-relative migrations/<developer>/<file>.sql path: $migration"
  [[ "$migration" != *"/../"* && "$migration" != *"/./"* ]] || \
    fail "migration path must not contain dot segments: $migration"
  python3 - "$REPO_ROOT" "$migration" <<'PY' || exit 2
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
relative = Path(sys.argv[2])
candidate = root / relative
current = root
for part in relative.parts:
    current = current / part
    if current.is_symlink():
        print(f"migration error: symbolic links are not allowed: {relative}", file=sys.stderr)
        raise SystemExit(1)
try:
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
except (OSError, ValueError):
    print(f"migration error: migration file is missing or outside the repository: {relative}", file=sys.stderr)
    raise SystemExit(1)
if not resolved.is_file():
    print(f"migration error: migration path is not a file: {relative}", file=sys.stderr)
    raise SystemExit(1)
PY
  migrations+=("$migration")
done

mkdir -p "$REPO_ROOT/scratch"
staging_dir="$(mktemp -d "$REPO_ROOT/scratch/sql-migration.XXXXXX")"
cleanup() { rm -rf -- "$staging_dir"; }
trap cleanup EXIT
sqlcl_stdin="$staging_dir/.sqlcl-stdin"
: > "$sqlcl_stdin"

for migration in "${migrations[@]}"; do
  (
    cd "$REPO_ROOT/scripts"
    sql -S -noupdates -name "$CODE_SQLCL_CONNECTION" \
      "@$REPO_ROOT/scripts/migrate.sql" \
      "$CODE_SCHEMA" "$DB_ENVIRONMENT" "$CODE_EXPECTED_USER" "../$migration" \
      < "$sqlcl_stdin"
  )
  printf 'Applied migration %s.\n' "$migration"
done
