#!/usr/bin/env bash
set -euo pipefail

# Reference disposable runner.  It deliberately uses a fixed image digest and
# labels every container so destroy can prove it is removing only its own
# target.  Teams may replace this executable without changing ci_replay's argv
# contract.
IMAGE="container-registry.oracle.com/database/free@sha256:696eee2ee8985af25ef0dc4cbcac14cdaadfd4545150a87d82d9724ce43c7a77"
NAME_PREFIX="team-ci-pdb-"
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

if [[ "$ACTION" == "create" ]]; then
  [[ -n "$OUT" ]] || fail "create requires --out"
  [[ ! -e "$OUT" || -d "$OUT" ]] || fail "output is not a directory"
  mkdir -p -- "$OUT"
  [[ ! -e "$OUT/replay.json" ]] || fail "output already contains a result"
  PASSWORD="${TEAM_CI_ORACLE_PASSWORD:-oracle}"
  docker run --detach --rm --name "$NAME" \
    --label "${LABEL_KEY}=true" --label "${LABEL_KEY}.run-id=${RUN_ID}" \
    --publish 0:1521 \
    -e "ORACLE_PWD=${PASSWORD}" -e ORACLE_CHARACTERSET=AL32UTF8 \
    "$IMAGE" >/dev/null
  cleanup_create() { docker rm --force "$NAME" >/dev/null 2>&1 || true; }
  trap cleanup_create ERR
  for attempt in $(seq 1 120); do
    if docker exec "$NAME" healthcheck.sh >/dev/null 2>&1; then break; fi
    [[ "$attempt" -lt 120 ]] || fail "Oracle Free container did not become healthy"
    sleep 2
  done
  PORT="$(docker port "$NAME" 1521/tcp 2>/dev/null | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)"
  [[ "$PORT" =~ ^[0-9]+$ ]] || fail "could not discover the disposable listener"
  cat > "$OUT/replay.env" <<EOF
PROJECT_NAME=team-ci
TARGET_ROLE=replay
DB_ENVIRONMENT=test
APEX_APPS=employee-self-service:100
TABLES_SCHEMA=DEMO
CODE_SCHEMA=DEMO
APEX_PARSING_SCHEMA=DEMO
METADATA_SCHEMA=DEMO_META
APEX_WORKSPACE_ID=5402650006222933
APP_OWNERSHIP_MODE=shared
TABLES_SQLCL_CONNECTION=ci-${RUN_ID}
TABLES_EXPECTED_USER=DEMO
TABLES_EXPECTED_CURRENT_SCHEMA=DEMO
TABLES_EXPECTED_DB_NAME=FREEPDB1
TABLES_EXPECTED_SERVICE=localhost:${PORT}/FREEPDB1
TABLES_EXPECTED_INSTANCE_ID=${NAME}
CODE_SQLCL_CONNECTION=ci-${RUN_ID}
CODE_EXPECTED_USER=DEMO
CODE_EXPECTED_CURRENT_SCHEMA=DEMO
CODE_EXPECTED_DB_NAME=FREEPDB1
CODE_EXPECTED_SERVICE=localhost:${PORT}/FREEPDB1
CODE_EXPECTED_INSTANCE_ID=${NAME}
APEX_SQLCL_CONNECTION=ci-${RUN_ID}
APEX_EXPECTED_USER=DEMO
APEX_EXPECTED_CURRENT_SCHEMA=DEMO
APEX_EXPECTED_DB_NAME=FREEPDB1
APEX_EXPECTED_SERVICE=localhost:${PORT}/FREEPDB1
APEX_EXPECTED_INSTANCE_ID=${NAME}
METADATA_SQLCL_CONNECTION=ci-${RUN_ID}
METADATA_EXPECTED_USER=DEMO_META
METADATA_EXPECTED_CURRENT_SCHEMA=DEMO_META
METADATA_EXPECTED_DB_NAME=FREEPDB1
METADATA_EXPECTED_SERVICE=localhost:${PORT}/FREEPDB1
METADATA_EXPECTED_INSTANCE_ID=${NAME}
VERIFY_SQLCL_CONNECTION=ci-${RUN_ID}
VERIFY_EXPECTED_USER=DEMO
VERIFY_EXPECTED_CURRENT_SCHEMA=DEMO
VERIFY_EXPECTED_DB_NAME=FREEPDB1
VERIFY_EXPECTED_SERVICE=localhost:${PORT}/FREEPDB1
VERIFY_EXPECTED_INSTANCE_ID=${NAME}
EOF
  cat > "$OUT/replay.json" <<EOF
{
  "version": 1,
  "status": "disposable",
  "instance_token": "${NAME}",
  "env_path": "${OUT}/replay.env",
  "workspace_id": 5402650006222933,
  "app_ids": {"employee-self-service": 100},
  "fixture_ids": [],
  "ords_base_url": null,
  "container_name": "${NAME}",
  "image": "${IMAGE}"
}
EOF
  trap - ERR
  cat "$OUT/replay.json"
  exit 0
fi

if [[ "$ACTION" == "destroy" ]]; then
  [[ -n "$TOKEN" && "$TOKEN" == "$NAME" ]] || fail "instance token does not match the verified run id"
  exists="$(docker ps -aq --filter "name=^/${NAME}$" | head -n 1)"
  [[ -n "$exists" ]] || { printf '{"version":1,"destroyed":true,"already_absent":true}\n'; exit 0; }
  label="$(docker inspect --format '{{ index .Config.Labels "io.codex.team-ci" }}' "$NAME" 2>/dev/null || true)"
  run_label="$(docker inspect --format '{{ index .Config.Labels "io.codex.team-ci.run-id" }}' "$NAME" 2>/dev/null || true)"
  [[ "$label" == "true" && "$run_label" == "$RUN_ID" ]] || fail "refusing to destroy an unverified container"
  docker rm --force "$NAME" >/dev/null
  printf '{"version":1,"destroyed":true}\n'
  exit 0
fi
usage
