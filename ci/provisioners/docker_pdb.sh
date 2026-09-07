#!/usr/bin/env bash
set -euo pipefail

# Reference disposable runner. It creates a fresh Oracle Free database, installs
# the pinned APEX fixture, starts ORDS, creates isolated controller/payload
# schemas, and saves only run-scoped SQLcl aliases. Teams may replace this
# executable without changing ci_replay's argv contract.
IMAGE="container-registry.oracle.com/database/free@sha256:696eee2ee8985af25ef0dc4cbcac14cdaadfd4545150a87d82d9724ce43c7a77"
ORDS_IMAGE="container-registry.oracle.com/database/ords@sha256:5f53eb398569e729e881f9f74d89f03910af168625a7927409214fa73d279bcb"
APEX_URL="${TEAM_CI_APEX_URL:-https://download.oracle.com/otn_software/apex/apex_26.1.zip}"
APEX_SHA256="${TEAM_CI_APEX_SHA256:-06df14e7c8465747ee45c36c5a0b09821bf2b244db0e201d3cc44abb0403f023}"
NAME_PREFIX="team-ci-pdb-"
ORDS_PREFIX="team-ci-ords-"
NETWORK_PREFIX="team-ci-network-"
LABEL_KEY="io.codex.team-ci"

fail() { echo "docker-pdb: $*" >&2; exit 2; }

require_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || fail "run id must be a UUID"
}

usage() {
  cat >&2 <<'EOF'
usage:
  docker_pdb.sh create --run-id UUID --out DIRECTORY
  docker_pdb.sh destroy --run-id UUID --instance-token TOKEN
EOF
  exit 2
}

command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v curl >/dev/null 2>&1 || fail "curl is required to obtain the pinned APEX fixture"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required to verify the APEX fixture"
command -v sql >/dev/null 2>&1 || fail "SQLcl is required to save disposable connections"
command -v timeout >/dev/null 2>&1 || fail "timeout is required for bounded SQLcl cleanup"
[[ $# -ge 1 ]] || usage
ACTION="$1"
shift
RUN_ID=""
OUT=""
TOKEN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) [[ $# -ge 2 ]] || usage; RUN_ID="$2"; shift 2 ;;
    --out) [[ $# -ge 2 ]] || usage; OUT="$2"; shift 2 ;;
    --instance-token) [[ $# -ge 2 ]] || usage; TOKEN="$2"; shift 2 ;;
    *) usage ;;
  esac
done
require_uuid "$RUN_ID"
NAME="${NAME_PREFIX}${RUN_ID}"
ORDS_NAME="${ORDS_PREFIX}${RUN_ID}"
NETWORK="${NETWORK_PREFIX}${RUN_ID}"

save_connection() {
  local alias="$1"
  local user="$2"
  local port="$3"
  timeout 30s sql -S -noupdates /nolog <<SQL >/dev/null
conn -save ${alias} -savepwd ${user}/\"${TEAM_CI_ORACLE_PASSWORD:-oracle}\"@//localhost:${port}/FREEPDB1
exit
SQL
}

delete_connection() {
  local alias="$1"
  timeout 30s sql -S -noupdates /nolog <<SQL >/dev/null 2>&1 || true
connmgr delete -conn ${alias}
exit
SQL
}

cleanup_create() {
  docker rm --force "$ORDS_NAME" >/dev/null 2>&1 || true
  docker rm --force "$NAME" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}

cleanup_create_on_error() {
  local status=$?
  echo "docker-pdb: create failed near shell line ${BASH_LINENO[0]:-unknown} (status ${status})" >&2
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$NAME"; then
    docker logs --tail 80 "$NAME" >&2 || true
  fi
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$ORDS_NAME"; then
    docker logs --tail 80 "$ORDS_NAME" >&2 || true
  fi
  cleanup_create
  exit "$status"
}

if [[ "$ACTION" == "create" ]]; then
  [[ -n "$OUT" ]] || fail "create requires --out"
  [[ ! -e "$OUT" || -d "$OUT" ]] || fail "output is not a directory"
  mkdir -p -- "$OUT"
  [[ ! -e "$OUT/replay.json" ]] || fail "output already contains a result"
  PASSWORD="${TEAM_CI_ORACLE_PASSWORD:-oracle}"
  mkdir -p "$OUT/ords-config" "$OUT/apex-images"
  docker network create --label "${LABEL_KEY}=true" --label "${LABEL_KEY}.run-id=${RUN_ID}" "$NETWORK" >/dev/null
  docker run --detach --name "$NAME" \
    --label "${LABEL_KEY}=true" --label "${LABEL_KEY}.run-id=${RUN_ID}" \
    --network "$NETWORK" --network-alias db \
    --publish 0:1521 \
    -e "ORACLE_PWD=${PASSWORD}" -e ORACLE_CHARACTERSET=AL32UTF8 \
    "$IMAGE" >/dev/null
  trap cleanup_create_on_error ERR
  for attempt in $(seq 1 120); do
    if docker exec "$NAME" /opt/oracle/checkDBStatus.sh >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 120 ]] || fail "Oracle Free container did not become healthy"
    sleep 2
  done
  PORT="$(docker port "$NAME" 1521/tcp 2>/dev/null | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)"
  [[ "$PORT" =~ ^[0-9]+$ ]] || fail "could not discover the disposable listener"

  APEX_ARCHIVE="${TEAM_CI_APEX_ARCHIVE:-$OUT/apex_26.1.zip}"
  if [[ -n "${TEAM_CI_APEX_ARCHIVE:-}" ]]; then
    [[ -f "$APEX_ARCHIVE" && ! -L "$APEX_ARCHIVE" ]] || fail "TEAM_CI_APEX_ARCHIVE is not a regular file"
  else
    curl --fail --location --retry 2 --max-time 900 --output "$APEX_ARCHIVE" "$APEX_URL"
  fi
  [[ "$(sha256sum "$APEX_ARCHIVE" | awk '{print $1}')" == "$APEX_SHA256" ]] || fail "APEX fixture checksum does not match the pinned 26.1 checksum"
  docker cp "$APEX_ARCHIVE" "$NAME:/tmp/apex_26.1.zip"
  docker exec "$NAME" bash -lc 'unzip -q /tmp/apex_26.1.zip -d /tmp && test -f /tmp/apex/apexins.sql'
  cat <<'SQL' | docker exec -i "$NAME" sqlplus -s / as sysdba >/dev/null
WHENEVER SQLERROR EXIT SQL.SQLCODE
CREATE BIGFILE TABLESPACE TBS_APEX DATAFILE 'tbs_apex_root.dbf' SIZE 100M AUTOEXTEND ON NEXT 50M MAXSIZE 3G;
ALTER SESSION SET CONTAINER=FREEPDB1;
CREATE BIGFILE TABLESPACE TBS_APEX DATAFILE 'tbs_apex_pdb.dbf' SIZE 100M AUTOEXTEND ON NEXT 50M MAXSIZE 3G;
EXIT
SQL
  docker exec "$NAME" bash -lc 'cd /tmp/apex && sqlplus -s / as sysdba @apexins.sql TBS_APEX TBS_APEX TEMP /i/ >/tmp/apex-install.log 2>&1'
  APEX_VERSION="$(printf '%s\n' 'set heading off feedback off pages 0 verify off echo off' 'alter session set container=FREEPDB1;' "select 'TEAM_APEX|' || version_no from apex_release;" 'exit' | docker exec -i "$NAME" sqlplus -s / as sysdba | sed -n 's/.*TEAM_APEX|//p' | tr -d '[:space:]' | tail -n 1)"
  [[ "$APEX_VERSION" == 26.1.* ]] || fail "APEX installation did not produce a 26.1 engine"
  printf '%s\n' 'ADMIN' 'ADMIN' "$PASSWORD" | docker exec -i "$NAME" bash -lc 'cd /tmp/apex && sqlplus -s / as sysdba @apxchpwd.sql' >/dev/null
  cat <<SQL | docker exec -i "$NAME" sqlplus -s / as sysdba >/dev/null
WHENEVER SQLERROR EXIT SQL.SQLCODE
ALTER SESSION SET CONTAINER=FREEPDB1;
DECLARE
  PROCEDURE create_user_if_missing(p_name VARCHAR2) IS
  BEGIN
    EXECUTE IMMEDIATE 'CREATE USER ' || p_name || ' IDENTIFIED BY oracle DEFAULT TABLESPACE USERS TEMPORARY TABLESPACE TEMP';
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE NOT IN (-1920, -1918) THEN RAISE; END IF;
  END;
BEGIN
  create_user_if_missing('DEMO');
  create_user_if_missing('DEMO_META');
END;
/
ALTER USER DEMO IDENTIFIED BY oracle ACCOUNT UNLOCK;
ALTER USER DEMO_META IDENTIFIED BY oracle ACCOUNT UNLOCK;
GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE SEQUENCE, CREATE TRIGGER, CREATE TYPE, CREATE SYNONYM TO DEMO;
GRANT UNLIMITED TABLESPACE TO DEMO;
GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE, CREATE SEQUENCE TO DEMO_META;
GRANT UNLIMITED TABLESPACE TO DEMO_META;
BEGIN
  APEX_INSTANCE_ADMIN.ADD_WORKSPACE(p_workspace => 'DEMO', p_primary_schema => 'DEMO');
  APEX_UTIL.SET_WORKSPACE(p_workspace => 'DEMO');
  APEX_UTIL.CREATE_USER(
    p_user_name => 'ADMIN', p_web_password => 'oracle', p_email_address => 'admin@example.invalid',
    p_developer_privs => 'ADMIN:CREATE:DATA_LOADER:EDIT:HELP:MONITOR:SQL',
    p_change_password_on_first_use => 'N', p_default_schema => 'DEMO');
  COMMIT;
END;
/
EXIT
SQL
  WORKSPACE_ID="$(printf '%s\n' 'set heading off feedback off pages 0 verify off echo off' 'alter session set container=FREEPDB1;' "select 'TEAM_WORKSPACE|' || to_char(workspace_id, 'FM99999999999999999999999999999999999999') from apex_workspaces where workspace = 'DEMO';" 'exit' | docker exec -i "$NAME" sqlplus -s / as sysdba | sed -n 's/.*TEAM_WORKSPACE|//p' | tr -d '[:space:]' | tail -n 1)"
  [[ "$WORKSPACE_ID" =~ ^[0-9]+$ ]] || fail "could not discover the isolated APEX workspace ID"
  docker cp "$NAME:/tmp/apex/images/." "$OUT/apex-images/"
  # The ORDS image runs as a non-root user and must initialize this mounted
  # directory during its first start. It is run-scoped disposable state, not
  # a credential or source directory.
  chmod 777 "$OUT/ords-config"
  docker run --detach --name "$ORDS_NAME" \
    --label "${LABEL_KEY}=true" --label "${LABEL_KEY}.run-id=${RUN_ID}" \
    --network "$NETWORK" --network-alias ords \
    --publish 0:8080 \
    -e "DB_CONN_BASE=${NAME}" -e "DB_CONN_NAME=${NAME}-sys" \
    -e "CONTAINER_NAME=${NAME}" -e DBHOST=db -e DBPORT=1521 -e DBSERVICENAME=FREEPDB1 \
    -e "ORACLE_PASSWORD=${PASSWORD}" -e "ORACLE_PWD=${PASSWORD}" \
    -e SECURE_MODE=false -e WORKSPACE_USE_INTERNAL_PASSWORD=true \
    -e DEBUG=true -e DEBUG_TO_SCREEN=true \
    -v "$OUT/ords-config:/etc/ords/config" \
    -v "$OUT/apex-images:/opt/oracle/apex/images" \
    "$ORDS_IMAGE" >/dev/null
  for attempt in $(seq 1 120); do
    ORDS_PORT_RAW="$(docker port "$ORDS_NAME" 8080/tcp 2>/dev/null || true)"
    ORDS_PORT="$(printf '%s\n' "$ORDS_PORT_RAW" | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)"
    if [[ "$ORDS_PORT" =~ ^[0-9]+$ ]] && curl --fail --silent --show-error --max-time 5 "http://localhost:${ORDS_PORT}/ords/" >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 120 ]] || fail "ORDS/APEX disposable fixture did not become ready"
    sleep 2
  done
  save_connection "ci-${RUN_ID}-demo" DEMO "$PORT"
  save_connection "ci-${RUN_ID}-meta" DEMO_META "$PORT"
  cat > "$OUT/replay.env" <<EOF
PROJECT_NAME=team-ci
TARGET_ROLE=replay
DB_ENVIRONMENT=test
APEX_APPS=employee-self-service:100
TABLES_SCHEMA=DEMO
CODE_SCHEMA=DEMO
APEX_PARSING_SCHEMA=DEMO
METADATA_SCHEMA=DEMO_META
APEX_WORKSPACE_ID=${WORKSPACE_ID}
APP_OWNERSHIP_MODE=shared
TABLES_SQLCL_CONNECTION=ci-${RUN_ID}-demo
TABLES_EXPECTED_USER=DEMO
TABLES_EXPECTED_CURRENT_SCHEMA=DEMO
TABLES_EXPECTED_DB_NAME=FREEPDB1
TABLES_EXPECTED_SERVICE=freepdb1
TABLES_EXPECTED_INSTANCE_ID=FREE
CODE_SQLCL_CONNECTION=ci-${RUN_ID}-demo
CODE_EXPECTED_USER=DEMO
CODE_EXPECTED_CURRENT_SCHEMA=DEMO
CODE_EXPECTED_DB_NAME=FREEPDB1
CODE_EXPECTED_SERVICE=freepdb1
CODE_EXPECTED_INSTANCE_ID=FREE
APEX_SQLCL_CONNECTION=ci-${RUN_ID}-demo
APEX_EXPECTED_USER=DEMO
APEX_EXPECTED_CURRENT_SCHEMA=DEMO
APEX_EXPECTED_DB_NAME=FREEPDB1
APEX_EXPECTED_SERVICE=freepdb1
APEX_EXPECTED_INSTANCE_ID=FREE
METADATA_SQLCL_CONNECTION=ci-${RUN_ID}-meta
METADATA_EXPECTED_USER=DEMO_META
METADATA_EXPECTED_CURRENT_SCHEMA=DEMO_META
METADATA_EXPECTED_DB_NAME=FREEPDB1
METADATA_EXPECTED_SERVICE=freepdb1
METADATA_EXPECTED_INSTANCE_ID=FREE
VERIFY_SQLCL_CONNECTION=ci-${RUN_ID}-demo
VERIFY_EXPECTED_USER=DEMO
VERIFY_EXPECTED_CURRENT_SCHEMA=DEMO
VERIFY_EXPECTED_DB_NAME=FREEPDB1
VERIFY_EXPECTED_SERVICE=freepdb1
VERIFY_EXPECTED_INSTANCE_ID=FREE
EOF
  cat > "$OUT/replay.json" <<EOF
{
  "version": 1,
  "status": "disposable",
  "instance_token": "${NAME}",
  "env_path": "${OUT}/replay.env",
  "workspace_id": ${WORKSPACE_ID},
  "app_ids": {"employee-self-service": 100},
  "fixture_ids": ["workspace:DEMO"],
  "ords_base_url": "http://localhost:${ORDS_PORT}/ords/",
  "container_name": "${NAME}",
  "ords_container_name": "${ORDS_NAME}",
  "network": "${NETWORK}",
  "image": "${IMAGE}",
  "ords_image": "${ORDS_IMAGE}",
  "apex_version": "${APEX_VERSION}"
}
EOF
  trap - ERR
  cat "$OUT/replay.json"
  exit 0
fi

if [[ "$ACTION" == "destroy" ]]; then
  [[ -n "$TOKEN" && "$TOKEN" == "$NAME" ]] || fail "instance token does not match the verified run id"
  for resource in "$NAME" "$ORDS_NAME"; do
    exists="$(docker ps -aq --filter "name=^/${resource}$" | head -n 1)"
    [[ -n "$exists" ]] || continue
    label="$(docker inspect --format '{{ index .Config.Labels "io.codex.team-ci" }}' "$resource" 2>/dev/null || true)"
    run_label="$(docker inspect --format '{{ index .Config.Labels "io.codex.team-ci.run-id" }}' "$resource" 2>/dev/null || true)"
    [[ "$label" == "true" && "$run_label" == "$RUN_ID" ]] || fail "refusing to destroy an unverified disposable resource"
  done
  docker rm --force "$ORDS_NAME" "$NAME" >/dev/null 2>&1 || true
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    docker network rm "$NETWORK" >/dev/null
  fi
  delete_connection "ci-${RUN_ID}-demo"
  delete_connection "ci-${RUN_ID}-meta"
  printf '{"version":1,"destroyed":true}\n'
  exit 0
fi
usage
