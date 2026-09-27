#!/usr/bin/env bash
# Run SQLcl only from a generated working directory with trusted script paths.
# Callers must pass a newly-created staging directory, never application source.

invoke_sqlcl_safe() {
  [ "$#" -ge 2 ] || {
    printf 'SQLcl launcher requires a working directory and arguments\n' >&2
    return 2
  }

  local working_directory="$1"
  shift
  [ -d "$working_directory" ] || {
    printf 'SQLcl working directory does not exist: %s\n' "$working_directory" >&2
    return 2
  }

  # SQLcl searches both its current directory and SQLPATH/ORACLE_PATH for
  # login.sql. Every caller supplies a private staging directory as CWD; this
  # empty child directory also prevents a user-configured search path from
  # injecting startup commands.
  local safe_sql_path="$working_directory/.sqlcl-path"
  mkdir -p -- "$safe_sql_path"
  (
    cd -- "$working_directory"
    SQLPATH="$safe_sql_path" ORACLE_PATH="$safe_sql_path" command sql "$@"
  )
}
