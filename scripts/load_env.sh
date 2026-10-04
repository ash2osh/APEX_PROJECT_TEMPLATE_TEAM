#!/usr/bin/env bash
# Source this file to load a strict KEY=VALUE .env file without executing it.

project_env_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${1:-${PROJECT_ENV_FILE:-$project_env_repo_root/.env}}"
unset PROD_SQLCL_CONNECTION PROD_EXPECTED_USER PROD_SCHEMA
unset STAGING_SQLCL_CONNECTION STAGING_EXPECTED_USER STAGING_SCHEMA
unset ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER
unset INSTALL_UC_APX UC_APX_SKILLS_AGENT

# README.md documents a relative PROJECT_ENV_FILE. Resolve it against the
# repository root when it is not found relative to the caller's directory, so a
# wrapper run from a subdirectory finds the same file as one run from the root.
# The drive-letter arm keeps Git Bash from treating C:/... as relative.
case "$PROJECT_ENV_FILE" in
  /*|[A-Za-z]:[/\\]*) ;;
  *)
    if [ ! -f "$PROJECT_ENV_FILE" ] && [ -f "$project_env_repo_root/$PROJECT_ENV_FILE" ]; then
      PROJECT_ENV_FILE="$project_env_repo_root/$PROJECT_ENV_FILE"
    fi
    ;;
esac

# Git Bash (MSYS) hands a native Windows program such as python.exe an unconverted
# /c/... path when the path holds glob characters like [1], and python then looks
# for C:\c\.... Shadow python3 on Windows shells only: absolute POSIX paths that
# exist (or whose parent exists, for an output location) go through cygpath -m.
# Every other argument, and every other platform, is passed through untouched.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    if command -v cygpath >/dev/null 2>&1; then
      python3() {
        local python_arg python_args=()
        for python_arg in "$@"; do
          case "$python_arg" in
            /*)
              if [ -e "$python_arg" ] || [ -d "${python_arg%/*}" ]; then
                python_args+=("$(cygpath -m -- "$python_arg")")
              else
                python_args+=("$python_arg")
              fi
              ;;
            *) python_args+=("$python_arg") ;;
          esac
        done
        command python3 "${python_args[@]}"
      }
    fi
    ;;
esac

project_env_fail() {
  unset project_env_repo_root
  echo "project environment error: $*" >&2
  return 1
}

# Bash matches [A-Z], [A-Za-z] and [[:alnum:]] by the user's locale: under
# en_US.UTF-8 they accept accented letters, which load_env.ps1 and the Python
# resolver reject. Match in the C locale so all three agree.
project_env_match() {
  local LC_ALL=C
  [[ "$1" =~ $2 ]]
}

if [ ! -f "$PROJECT_ENV_FILE" ]; then
  project_env_fail "configuration file not found: $PROJECT_ENV_FILE (copy .env.example to .env)"
  return 1 2>/dev/null || exit 1
fi

# Windows PowerShell 5.1 writes UTF-16 for `>` and Out-File, and PowerShell
# decodes it, but Bash would read garbage and fail with an unreadable
# "invalid line" message. Say what is wrong instead.
project_env_bom="$(head -c 2 "$PROJECT_ENV_FILE" | od -An -tx1 | tr -d ' \n')"
if [ "$project_env_bom" = fffe ] || [ "$project_env_bom" = feff ]; then
  project_env_fail "$PROJECT_ENV_FILE is UTF-16; save it as UTF-8 (a UTF-8 byte-order mark is fine)"
  unset project_env_bom
  return 1 2>/dev/null || exit 1
fi
unset project_env_bom

project_env_seen_keys=()
project_env_first_line=true
project_env_oracle_identifier_regex='^[A-Z][A-Z0-9_$#]{0,127}$'
project_env_oracle_prefix_regex='^[A-Z][A-Z0-9_$#]*(,[A-Z][A-Z0-9_$#]*)*$'
while IFS= read -r project_env_line || [ -n "$project_env_line" ]; do
  project_env_line="${project_env_line%$'\r'}"
  if [ "$project_env_first_line" = true ]; then
    project_env_line="${project_env_line#$'\xEF\xBB\xBF'}"
    project_env_first_line=false
  fi
  case "$project_env_line" in
    '#'*) continue ;;
  esac
  # A line of only whitespace is not a configuration error. PowerShell's
  # IsNullOrWhiteSpace already skips one, so Bash must agree, or a .env
  # authored on Windows loads there and fails on Linux.
  if [ -z "${project_env_line//[[:space:]]/}" ]; then
    continue
  fi
  if ! project_env_match "$project_env_line" '^([A-Z][A-Z0-9_]*)=(.*)$'; then
    project_env_fail "invalid line in $PROJECT_ENV_FILE: $project_env_line"
    return 1 2>/dev/null || exit 1
  fi
  project_env_key="${BASH_REMATCH[1]}"
  project_env_value="${BASH_REMATCH[2]}"
  case "$project_env_key" in
    PROJECT_NAME|DEVELOPER_NAME|DB_ENVIRONMENT|APEX_APP_ID|\
    TABLES_SCHEMA|TABLES_PREFIXES|TABLES_SQLCL_CONNECTION|TABLES_EXPECTED_USER|\
    CODE_SCHEMA|CODE_PREFIXES|CODE_SQLCL_CONNECTION|CODE_EXPECTED_USER|\
    APEX_PARSING_SCHEMA|APEX_SQLCL_CONNECTION|APEX_EXPECTED_USER|\
    INSTALL_UC_APX|UC_APX_SKILLS_AGENT|\
    PROD_SQLCL_CONNECTION|PROD_EXPECTED_USER|PROD_SCHEMA|\
    STAGING_SQLCL_CONNECTION|STAGING_EXPECTED_USER|STAGING_SCHEMA|\
    ORDS_SCHEMA|ORDS_SQLCL_CONNECTION|ORDS_EXPECTED_USER) ;;
    *)
      project_env_fail "unsupported setting in $PROJECT_ENV_FILE: $project_env_key"
      return 1 2>/dev/null || exit 1
      ;;
  esac
  for project_env_seen_key in "${project_env_seen_keys[@]}"; do
    if [ "$project_env_seen_key" = "$project_env_key" ]; then
      project_env_fail "duplicate setting in $PROJECT_ENV_FILE: $project_env_key"
      return 1 2>/dev/null || exit 1
    fi
  done
  project_env_quoted=false
  if [ "${#project_env_value}" -ge 2 ]; then
    if [[ "$project_env_value" == \"*\" ]] || [[ "$project_env_value" == \'*\' ]]; then
      project_env_value="${project_env_value:1:${#project_env_value}-2}"
      project_env_quoted=true
    fi
  elif [[ "$project_env_value" == \" || "$project_env_value" == \' ]]; then
    project_env_fail "$project_env_key has an unterminated quoted value"
    return 1 2>/dev/null || exit 1
  fi
  # Values are parsed literally, so an unquoted inline comment would be stored
  # verbatim. For the settings with a format rule that surfaces as a confusing
  # error; for PROJECT_NAME, which has none, it is stored silently.
  if [ "$project_env_quoted" != true ] && project_env_match "$project_env_value" '[[:space:]]#'; then
    project_env_fail "$project_env_key has an inline comment; .env values are parsed literally, so put the comment on its own line, or quote the value to keep a literal '#'"
    return 1 2>/dev/null || exit 1
  fi
  export "$project_env_key=$project_env_value"
  project_env_seen_keys+=("$project_env_key")
done < "$PROJECT_ENV_FILE"

# Optional uc-apx settings keep older .env files valid while making the
# effective defaults explicit to project skills.
if [ "${INSTALL_UC_APX+x}" != x ]; then INSTALL_UC_APX=false; fi
if [ "${UC_APX_SKILLS_AGENT+x}" != x ]; then UC_APX_SKILLS_AGENT=universal; fi
export INSTALL_UC_APX UC_APX_SKILLS_AGENT

project_env_required=(
  PROJECT_NAME DEVELOPER_NAME DB_ENVIRONMENT APEX_APP_ID
  TABLES_SCHEMA TABLES_PREFIXES TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER
  CODE_SCHEMA CODE_PREFIXES CODE_SQLCL_CONNECTION CODE_EXPECTED_USER
  APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER
)
for project_env_key in "${project_env_required[@]}"; do
  project_env_seen_present=false
  for project_env_seen_key in "${project_env_seen_keys[@]}"; do
    if [ "$project_env_seen_key" = "$project_env_key" ]; then
      project_env_seen_present=true
      break
    fi
  done
  project_env_value="${!project_env_key:-}"
  if [ "$project_env_seen_present" != true ] || [ -z "${project_env_value//[[:space:]]/}" ]; then
    project_env_fail "$project_env_key is required in $PROJECT_ENV_FILE"
    return 1 2>/dev/null || exit 1
  fi
done

project_env_validate_unique_csv() {
  local project_env_csv_key="$1"
  local project_env_csv_value="$2"
  local project_env_csv_item project_env_csv_seen_item
  local project_env_csv_items=() project_env_csv_seen_items=()
  IFS=',' read -r -a project_env_csv_items <<< "$project_env_csv_value"
  for project_env_csv_item in "${project_env_csv_items[@]}"; do
    for project_env_csv_seen_item in "${project_env_csv_seen_items[@]}"; do
      if [ "$project_env_csv_item" = "$project_env_csv_seen_item" ]; then
        project_env_fail "$project_env_csv_key must not contain duplicate values: $project_env_csv_item"
        return 1
      fi
    done
    project_env_csv_seen_items+=("$project_env_csv_item")
  done
}


# Comma lists. One value is the classic single-schema setup; several values
# are position-aligned across a profile's schema, connection and user keys.
project_env_csv_shape_ok() {
  # `read -a` silently drops a trailing empty field, so reject empties here.
  case "$1" in ,*|*,|*,,*) return 1 ;; esac
  return 0
}
project_env_split_csv() {
  local -n project_env_split_out="$1"
  project_env_split_out=()
  [ -z "$2" ] || IFS=',' read -r -a project_env_split_out <<< "$2"
  return 0
}
project_env_count() {
  local -a project_env_count_items=()
  project_env_split_csv project_env_count_items "$1"
  printf '%s' "${#project_env_count_items[@]}"
}
project_env_check_list() {
  local key="$1" kind="$2" value item
  local -a items=()
  value="${!key:-}"
  [ -n "$value" ] || return 0
  project_env_csv_shape_ok "$value" || { project_env_fail "$key must not contain empty entries"; return 1; }
  project_env_split_csv items "$value"
  for item in "${items[@]}"; do
    if [ "$kind" = identifier ] && ! project_env_match "$item" "$project_env_oracle_identifier_regex"; then
      project_env_fail "$key must be an uppercase Oracle identifier"
      return 1
    fi
    if [ "$kind" = alias ] && ! project_env_match "$item" '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
      project_env_fail "$key contains unsupported characters"
      return 1
    fi
  done
}
project_env_check_aligned() {
  local schema_key="$1" connection_key="$2" user_key="$3"
  local -a schemas=() connections=() users=()
  project_env_split_csv schemas "${!schema_key:-}"
  project_env_split_csv connections "${!connection_key:-}"
  project_env_split_csv users "${!user_key:-}"
  if [ "${#schemas[@]}" -ne "${#connections[@]}" ] || [ "${#schemas[@]}" -ne "${#users[@]}" ]; then
    project_env_fail "$connection_key, $user_key and $schema_key must list the same number of entries"
    return 1
  fi
  project_env_validate_unique_csv "$schema_key" "${!schema_key}"
}

for project_env_prefix in PROD STAGING; do
  project_env_connection_key="${project_env_prefix}_SQLCL_CONNECTION"
  project_env_user_key="${project_env_prefix}_EXPECTED_USER"
  project_env_schema_key="${project_env_prefix}_SCHEMA"
  project_env_connection_seen=false
  project_env_user_seen=false
  project_env_schema_seen=false
  for project_env_seen_key in "${project_env_seen_keys[@]}"; do
    [ "$project_env_seen_key" = "$project_env_connection_key" ] && project_env_connection_seen=true
    [ "$project_env_seen_key" = "$project_env_user_key" ] && project_env_user_seen=true
    [ "$project_env_seen_key" = "$project_env_schema_key" ] && project_env_schema_seen=true
  done
  if [ "$project_env_connection_seen" != "$project_env_user_seen" ]; then
    project_env_fail "$project_env_connection_key and $project_env_user_key must be configured together"
    return 1 2>/dev/null || exit 1
  fi
  if [ "$project_env_schema_seen" = true ] && [ "$project_env_connection_seen" != true ]; then
    project_env_fail "$project_env_schema_key requires $project_env_connection_key and $project_env_user_key"
    return 1 2>/dev/null || exit 1
  fi
  if [ "$project_env_connection_seen" = true ]; then
    project_env_connection_value="${!project_env_connection_key:-}"
    project_env_user_value="${!project_env_user_key:-}"
    if [ -z "${project_env_connection_value//[[:space:]]/}" ] || \
       [ -z "${project_env_user_value//[[:space:]]/}" ]; then
      project_env_fail "$project_env_connection_key and $project_env_user_key must not be empty"
      return 1 2>/dev/null || exit 1
    fi
    project_env_check_list "$project_env_connection_key" alias || { return 1 2>/dev/null || exit 1; }
    project_env_check_list "$project_env_user_key" identifier || { return 1 2>/dev/null || exit 1; }
    if [ "$project_env_schema_seen" = true ]; then
      project_env_check_aligned "$project_env_schema_key" "$project_env_connection_key" "$project_env_user_key" || {
        return 1 2>/dev/null || exit 1
      }
    else
      if [ "$(project_env_count "$project_env_connection_value")" -ne "$(project_env_count "$project_env_user_value")" ]; then
        project_env_fail "$project_env_connection_key and $project_env_user_key must list the same number of entries"
        return 1 2>/dev/null || exit 1
      fi
      if [ "$(project_env_count "$project_env_connection_value")" -gt 1 ]; then
        project_env_fail "$project_env_schema_key is required when $project_env_connection_key lists several connections"
        return 1 2>/dev/null || exit 1
      fi
    fi
  fi
done
# The optional ORDS metadata profile. All three keys omitted means ORDS is
# disabled and an existing project behaves exactly as before; any one of them
# without the others is a configuration error, never a silent partial setup.
project_env_ords_present=0
for project_env_key in ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER; do
  for project_env_seen_key in "${project_env_seen_keys[@]}"; do
    [ "$project_env_seen_key" = "$project_env_key" ] && project_env_ords_present=$((project_env_ords_present + 1))
  done
done
if [ "$project_env_ords_present" -ne 0 ] && [ "$project_env_ords_present" -ne 3 ]; then
  project_env_fail "ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER must be configured together (all three, or none to leave ORDS disabled)"
  return 1 2>/dev/null || exit 1
fi
PROJECT_ORDS_CONFIGURED=false
if [ "$project_env_ords_present" -eq 3 ]; then
  for project_env_key in ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER; do
    project_env_value="${!project_env_key:-}"
    if [ -z "${project_env_value//[[:space:]]/}" ]; then
      project_env_fail "$project_env_key must not be empty; remove all three ORDS_* settings to leave ORDS disabled"
      return 1 2>/dev/null || exit 1
    fi
  done
  project_env_check_list ORDS_SCHEMA identifier || { return 1 2>/dev/null || exit 1; }
  project_env_check_list ORDS_EXPECTED_USER identifier || { return 1 2>/dev/null || exit 1; }
  project_env_check_list ORDS_SQLCL_CONNECTION alias || { return 1 2>/dev/null || exit 1; }
  project_env_check_aligned ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }
  # ORDS authorizes the actual login user, so the session user must be the REST
  # schema owner itself: a profile whose expected user differs can never succeed.
  project_env_ords_schemas=()
  project_env_ords_users=()
  project_env_split_csv project_env_ords_schemas "$ORDS_SCHEMA"
  project_env_split_csv project_env_ords_users "$ORDS_EXPECTED_USER"
  for ((project_env_ords_index = 0; project_env_ords_index < ${#project_env_ords_schemas[@]}; project_env_ords_index++)); do
    if [ "${project_env_ords_schemas[$project_env_ords_index]}" != "${project_env_ords_users[$project_env_ords_index]}" ]; then
      project_env_fail "ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry (found ${project_env_ords_users[$project_env_ords_index]} for ${project_env_ords_schemas[$project_env_ords_index]}): the ORDS export logs in as the REST schema owner"
      return 1 2>/dev/null || exit 1
    fi
  done
  PROJECT_ORDS_CONFIGURED=true
else
  ORDS_SCHEMA=""
  ORDS_SQLCL_CONNECTION=""
  ORDS_EXPECTED_USER=""
fi
export ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER PROJECT_ORDS_CONFIGURED
project_env_match "$APEX_APP_ID" '^[1-9][0-9]{0,17}(,[1-9][0-9]{0,17})*$' || {
  project_env_fail "APEX_APP_ID must be a comma-separated list of positive integers of at most 18 digits, without spaces"
  return 1 2>/dev/null || exit 1
}

project_env_validate_unique_csv APEX_APP_ID "$APEX_APP_ID" || {
  return 1 2>/dev/null || exit 1
}

for project_env_key in TABLES_PREFIXES CODE_PREFIXES; do
  project_env_value="${!project_env_key}"
  if [ "$project_env_value" = "*" ]; then
    continue
  fi
  if ! project_env_match "$project_env_value" "$project_env_oracle_prefix_regex"; then
    project_env_fail "$project_env_key must be * or a comma-separated list of uppercase Oracle identifier prefixes without spaces"
    return 1 2>/dev/null || exit 1
  fi
  project_env_validate_unique_csv "$project_env_key" "$project_env_value" || {
    return 1 2>/dev/null || exit 1
  }
  IFS=',' read -r -a project_env_prefix_items <<< "$project_env_value"
  for project_env_prefix_item in "${project_env_prefix_items[@]}"; do
    if [ "${#project_env_prefix_item}" -gt 128 ]; then
      project_env_fail "$project_env_key prefixes must be at most 128 characters"
      return 1 2>/dev/null || exit 1
    fi
  done
done
# DEV publish stamps this name into the app version tag, where '-' separates it
# from the date.
if ! project_env_match "$DEVELOPER_NAME" '^[A-Z][A-Z0-9_]{0,29}$'; then
  project_env_fail "DEVELOPER_NAME must be uppercase letters, digits, or underscores (at most 30), such as ASHARIF"
  return 1 2>/dev/null || exit 1
fi
case "$DB_ENVIRONMENT" in
  development|test|staging|production) ;;
  *)
    project_env_fail "DB_ENVIRONMENT must be development, test, staging, or production"
    return 1 2>/dev/null || exit 1
    ;;
esac
case "$INSTALL_UC_APX" in
  true|false) ;;
  *)
    project_env_fail "INSTALL_UC_APX must be true or false"
    return 1 2>/dev/null || exit 1
    ;;
esac
case "$UC_APX_SKILLS_AGENT" in
  universal|claude-code) ;;
  *)
    project_env_fail "UC_APX_SKILLS_AGENT must be universal or claude-code"
    return 1 2>/dev/null || exit 1
    ;;
esac
for project_env_key in TABLES_SCHEMA TABLES_EXPECTED_USER CODE_SCHEMA \
  CODE_EXPECTED_USER APEX_PARSING_SCHEMA APEX_EXPECTED_USER STAGING_SCHEMA PROD_SCHEMA; do
  project_env_check_list "$project_env_key" identifier || { return 1 2>/dev/null || exit 1; }
done
for project_env_key in TABLES_SQLCL_CONNECTION CODE_SQLCL_CONNECTION APEX_SQLCL_CONNECTION; do
  project_env_check_list "$project_env_key" alias || { return 1 2>/dev/null || exit 1; }
done
project_env_check_aligned TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }
project_env_check_aligned CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }
project_env_check_aligned APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER || { return 1 2>/dev/null || exit 1; }

# The configured schemas, and whether any list names more than one.
project_env_union=()
project_env_multi=false
for project_env_key in TABLES_SCHEMA CODE_SCHEMA APEX_PARSING_SCHEMA ORDS_SCHEMA; do
  project_env_items=()
  project_env_split_csv project_env_items "${!project_env_key}"
  [ "${#project_env_items[@]}" -le 1 ] || project_env_multi=true
  for project_env_item in "${project_env_items[@]}"; do
    project_env_known=false
    for project_env_union_item in ${project_env_union[@]+"${project_env_union[@]}"}; do
      [ "$project_env_union_item" = "$project_env_item" ] && project_env_known=true
    done
    [ "$project_env_known" = true ] || project_env_union+=("$project_env_item")
  done
done
for project_env_key in STAGING_SCHEMA PROD_SCHEMA STAGING_SQLCL_CONNECTION PROD_SQLCL_CONNECTION; do
  if [ "$(project_env_count "${!project_env_key:-}")" -gt 1 ]; then project_env_multi=true; fi
done
# The project's one DEV schema (CODE_SCHEMA with exactly one entry), captured
# before narrowing rewrites it. Only that schema may map to a differently named
# staging or production schema; the Python resolver applies the same rule.
project_env_dev_schema=""
[ "$(project_env_count "$CODE_SCHEMA")" -ne 1 ] || project_env_dev_schema="$CODE_SCHEMA"
PROJECT_SCHEMAS="$(IFS=,; printf '%s' "${project_env_union[*]}")"
PROJECT_MULTI_SCHEMA="$project_env_multi"
PROJECT_CODE_SCHEMAS="$CODE_SCHEMA"
export PROJECT_SCHEMAS PROJECT_MULTI_SCHEMA PROJECT_CODE_SCHEMAS

project_env_narrow() {
  # <schema-key> <connection-key> <user-key> <strict|lenient>
  local schema_key="$1" connection_key="$2" user_key="$3" mode="$4"
  local index=-1 i
  local -a schemas=() connections=() users=()
  project_env_split_csv schemas "${!schema_key:-}"
  project_env_split_csv connections "${!connection_key:-}"
  project_env_split_csv users "${!user_key:-}"
  for ((i = 0; i < ${#schemas[@]}; i++)); do
    if [ "${schemas[$i]}" = "$PROJECT_SCHEMA" ]; then index="$i"; break; fi
  done
  if [ "$index" -ge 0 ]; then
    export "$schema_key=${schemas[$index]}" "$connection_key=${connections[$index]}" "$user_key=${users[$index]}"
  elif [ "$mode" = lenient ] && [ "$project_env_multi" != true ] && [ "${#schemas[@]}" -eq 1 ] \
      && [ -n "$project_env_dev_schema" ] && [ "$PROJECT_SCHEMA" = "$project_env_dev_schema" ]; then
    # A project with one DEV schema may name staging or production differently,
    # for that schema only.
    :
  else
    export "$schema_key=" "$connection_key=" "$user_key="
  fi
}

if [ -n "${PROJECT_SCHEMA:-}" ]; then
  if ! project_env_match "$PROJECT_SCHEMA" "$project_env_oracle_identifier_regex"; then
    project_env_fail "PROJECT_SCHEMA must be an uppercase Oracle identifier"
    return 1 2>/dev/null || exit 1
  fi
  project_env_selected_known=false
  project_env_items=()
  project_env_split_csv project_env_items "$PROJECT_SCHEMAS"
  for project_env_item in "${project_env_items[@]}"; do
    [ "$project_env_item" = "$PROJECT_SCHEMA" ] && project_env_selected_known=true
  done
  if [ "$project_env_selected_known" != true ]; then
    project_env_fail "schema $PROJECT_SCHEMA is not configured; configured schemas: $PROJECT_SCHEMAS"
    return 1 2>/dev/null || exit 1
  fi
  project_env_narrow TABLES_SCHEMA TABLES_SQLCL_CONNECTION TABLES_EXPECTED_USER strict
  project_env_narrow CODE_SCHEMA CODE_SQLCL_CONNECTION CODE_EXPECTED_USER strict
  project_env_narrow APEX_PARSING_SCHEMA APEX_SQLCL_CONNECTION APEX_EXPECTED_USER strict
  project_env_narrow ORDS_SCHEMA ORDS_SQLCL_CONNECTION ORDS_EXPECTED_USER strict
  for project_env_prefix in STAGING PROD; do
    project_env_schema_key="${project_env_prefix}_SCHEMA"
    if [ -n "${!project_env_schema_key:-}" ]; then
      project_env_narrow "$project_env_schema_key" "${project_env_prefix}_SQLCL_CONNECTION" "${project_env_prefix}_EXPECTED_USER" lenient
    fi
  done
fi

# Kept after the load so a script can refuse to guess between schemas.
project_env_require_single() {
  if [ "${PROJECT_MULTI_SCHEMA:-false}" = true ] && [ -z "${PROJECT_SCHEMA:-}" ]; then
    project_env_fail "$1 needs one schema because several are configured ($PROJECT_SCHEMAS); pass --schema <NAME>"
    return 1
  fi
  return 0
}
unset project_env_line project_env_key project_env_value project_env_required
unset project_env_seen_keys project_env_seen_key project_env_seen_present
unset project_env_prefix_items project_env_prefix_item project_env_quoted
unset project_env_prefix project_env_connection_key project_env_user_key
unset project_env_connection_seen project_env_user_seen project_env_connection_value
unset project_env_user_value project_env_schema_key project_env_schema_seen
unset project_env_first_line project_env_oracle_identifier_regex project_env_oracle_prefix_regex
unset project_env_union project_env_multi project_env_items project_env_item
unset project_env_known project_env_union_item project_env_selected_known project_env_count_items
unset project_env_dev_schema project_env_ords_present project_env_ords_schemas project_env_ords_users project_env_ords_index
unset project_env_repo_root
# project_env_fail and project_env_require_single stay defined for callers that
# refuse an ambiguous schema; the parsing helpers do not.
unset -f project_env_validate_unique_csv project_env_csv_shape_ok project_env_split_csv \
  project_env_count project_env_check_list project_env_check_aligned project_env_narrow \
  project_env_match
