#!/usr/bin/env bash
# Run SQLcl only from a generated working directory with trusted script paths.
# Callers must pass a newly-created staging directory, never application source.

# Print the form of a path that SQLcl itself can open. SQLcl is a native program:
# under Git Bash (MSYS) a path such as /c/Users/me/work [1]/scripts/doctor.sql is
# unknown to it ("SP2-0310: unable to open file"). MSYS rewrites plain /c/...
# arguments for native programs, but not the @script form, and not a path that
# holds glob characters such as [1]. cygpath -m gives C:/Users/... Everywhere
# else the path is printed unchanged.
sqlcl_native_path() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1; then
        cygpath -m -- "$1"
        return
      fi
      ;;
  esac
  printf '%s\n' "$1"
}

invoke_sqlcl_safe() {
  [ "$#" -ge 2 ] || {
    printf 'SQLcl launcher requires a working directory and arguments\n' >&2
    return 2
  }

  local working_directory="$1"
  shift

  # Convert absolute POSIX paths that exist (or whose parent directory exists, as
  # an output location does) so a native SQLcl can open them. Only Windows shells
  # change anything.
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      local sqlcl_arg sqlcl_args=()
      for sqlcl_arg in "$@"; do
        case "$sqlcl_arg" in
          @/*) sqlcl_args+=("@$(sqlcl_native_path "${sqlcl_arg#@}")") ;;
          /*)
            if [ -e "$sqlcl_arg" ] || [ -d "${sqlcl_arg%/*}" ]; then
              sqlcl_args+=("$(sqlcl_native_path "$sqlcl_arg")")
            else
              sqlcl_args+=("$sqlcl_arg")
            fi
            ;;
          *) sqlcl_args+=("$sqlcl_arg") ;;
        esac
      done
      set -- "${sqlcl_args[@]}"
      ;;
  esac
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
  local native_sql_path
  native_sql_path="$(sqlcl_native_path "$safe_sql_path")"
  (
    cd -- "$working_directory"
    SQLPATH="$native_sql_path" ORACLE_PATH="$native_sql_path" command sql "$@"
  )
}

# Print the parsing schema that owns an APEX application, or fail. The caller
# supplies a newly-created work directory (never application source) and has
# REPO_ROOT and DB_ENVIRONMENT set. An optional sixth argument overrides the
# environment classification for a selected staging or production target.
# NOT_FOUND is reported as a failure.
sqlcl_app_parsing_schema() {
  [ "$#" -ge 5 ] && [ "$#" -le 6 ] || {
    printf 'usage: sqlcl_app_parsing_schema <connection> <expected-user> <schema> <app-id> <work-dir> [environment]\n' >&2
    return 2
  }
  local connection="$1" expected_user="$2" schema="$3" app_id="$4" work_dir="$5"
  local environment="${6:-$DB_ENVIRONMENT}"
  local stdin_file="$work_dir/.sqlcl-stdin" output_file="$work_dir/lookup-output.log" owner
  mkdir -p -- "$work_dir"
  : > "$stdin_file"
  if ! invoke_sqlcl_safe "$work_dir" \
    -S -noupdates -name "$connection" \
    "@$REPO_ROOT/scripts/lookup_app_schema.sql" \
    "$schema" "$app_id" "$environment" "$expected_user" \
    < "$stdin_file" > "$output_file" 2>&1; then
    cat "$output_file" >&2
    printf 'could not look up the parsing schema of application %s\n' "$app_id" >&2
    return 1
  fi
  if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:' "$output_file"; then
    cat "$output_file" >&2
    printf 'the parsing-schema lookup for application %s reported an error\n' "$app_id" >&2
    return 1
  fi
  owner="$(sed -n "s/^[[:space:]]*APEX_APP_SCHEMA:${app_id}:\\(.*[^[:space:]]\\)[[:space:]]*\$/\\1/p" "$output_file" | tail -n 1)"
  if [ -z "$owner" ]; then
    printf 'the parsing-schema lookup for application %s returned no result\n' "$app_id" >&2
    return 1
  fi
  if [ "$owner" = NOT_FOUND ]; then
    printf 'application %s was not found in the workspace visible to this connection\n' "$app_id" >&2
    return 1
  fi
  printf '%s\n' "$owner"
}
