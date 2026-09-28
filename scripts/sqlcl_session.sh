#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  printf 'SQLcl session requires a run directory, saved alias, and driver path\n' >&2
  exit 2
fi

bridge_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/sqlcl_safe.sh
source "$bridge_dir/sqlcl_safe.sh"

working_directory=$1
connection_alias=$2
driver_path=$3

case "$connection_alias" in
  ''|*[!A-Za-z0-9._-]*)
    printf 'SQLcl saved alias contains unsupported characters\n' >&2
    exit 2
    ;;
esac

case "$driver_path" in
  "$working_directory"/*) ;;
  *)
    printf 'SQLcl driver must be inside its private run directory\n' >&2
    exit 2
    ;;
esac

invoke_sqlcl_safe "$working_directory" -name "$connection_alias" "@$driver_path"
