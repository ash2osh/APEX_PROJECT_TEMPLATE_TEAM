# Promotion and production handoff

Promotion moves one immutable `release.tar`. Build it from a resolved Git
commit and verify the archive bytes offline. Release tags are `schema/v<semver>`
for shared migrations and `app/<alias>/v<semver>` for single application releases:

```text
# Schema release (applies shared migrations once per environment)
scripts/team.py build-release --kind schema --ref schema/v1.0.0 --version 1.0.0 --out scratch/release
scripts/team.py verify-release scratch/release/release.tar

# Single application release (e.g. hr; checks schema prerequisites without deploying siblings)
scripts/team.py build-release --kind app --alias hr --ref app/hr/v1.0.0 --version 1.0.0 --out scratch/release
scripts/team.py verify-release scratch/release/release.tar
```

The manifest is closed and payload-derived:
- `kind: schema` packages only migration SQL and schema contracts; it applies once per environment.
- `kind: app` packages exactly one application tree, its checks and required migration IDs/checksums, without migration SQL or sibling apps.
Symlinks, credentials, deployment bindings, logs, and sync state are not
packaged. Production `apply-release` remains refused. No automated production
write is authorized.

**Production read-only is enforced by the database, not by this tool.** Before a
production read, `team.py` refuses any driver statement that is not a query or a
display setting. That check is a keyword-level safety net: a `SELECT` that calls
a function with side effects, or a query reaching `DBMS_SQL`, can still pass it.
Give the production SQLcl connection a dedicated account with `CREATE SESSION` and
`SELECT`/`READ` on the dictionary and APEX views it needs, and nothing else — no
`EXECUTE` on packages with side effects, no DML or DDL privileges. With that
account the guard can only ever be redundant.

## Protected test run

The release workflow serializes protected test use with concurrency group
`example-team-apex-test` and `cancel-in-progress: false`; it never interrupts a
target between migration, application deployment, and qualification. It
downloads and verifies the same archive, then runs on
`runs-on: [self-hosted, team-apex, test]` in environment `test`. It materializes
the protected profile, public trust key, and production history below
`$RUNNER_TEMP` with mode 0600, and removes those bounded files in an `always()`
cleanup step. The signing key is written only inside the signing step, after the
evidence exists, and deleted when that step exits, so the command that produces
the evidence never runs next to the key that signs it. Every action is pinned by
commit SHA and every checkout sets `persist-credentials: false`, so no GitHub
token is left in a self-hosted runner's workspace. It invokes exactly one online
command:

```text
scripts/team.py --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"
```

The command derives the archive source commit and aliases, verifies that the
target contract and config have `role: test`, reads live metadata history,
recomputes pending work, refuses destructive migrations before controller setup,
applies and verifies non-destructive work under the metadata mutex, deploys
packaged applications, and emits unsigned evidence. The apply result is passed
in memory; operators do not transport a plan, history, or intermediate result
file between steps.

Evidence version 2 is canonical compact UTF-8 JSON with one LF terminator. It
contains `source_commit`, `archive_digest`, `toolchain_digest`, complete
`target_identity`, `qualification_identity` with `target_kind: persistent`,
`observation_sequence`, `observation_digest` from the accepted after inventory,
`history_digest`, application-check coverage, and PASS/FAIL result fields.
Staging and test targets are persistent observations; they do not prove a fresh
installation, isolation, or arbitrary-DML coverage.

## Sign and hand off

Signing is a separate privilege and uses only the protected test private key:

```text
scripts/team.py sign-test-evidence \
  --evidence "$RUNNER_TEMP/test-evidence.json" \
  --archive scratch/release/release.tar \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

`sign-test-evidence` rejects non-canonical, failed, incomplete, non-persistent,
non-`role: test`, or archive/check-binding mismatched evidence. It binds
the manifest's `kind` and `alias`; a signed test report for HR cannot be reused
for Payroll or a schema archive. The public trust key is independently supplied;
it is never derived from the private key. Missing signer or trust material
leaves diagnostics available but cannot create a handoff.

Generate the offline production-owner runbook:

```text
scripts/team.sh gen-runbook scratch/release/release.tar \
  --history "$RUNNER_TEMP/production-history.json" \
  --target targets/production.json \
  --test-evidence "$RUNNER_TEMP/test-evidence.json" \
  --signature "$RUNNER_TEMP/test-evidence.sig" \
  --trust-key "$RUNNER_TEMP/test-trust-key.pem" \
  --out scratch/PRODUCTION_RUNBOOK.md
```

The runbook repeats the archive/source/application-check binding verification;
it is an owner-reviewed document, not a production apply switch.
- For a schema release, the runbook lists pending migrations in dependency order and owner checklist steps for migration application only.
- For an application release, the runbook lists exact prerequisite migrations (verified against destination history) and deployment instructions for the selected application only, with zero sibling app deployments.
The production owner re-reads identity and history
under the metadata mutex before any separately authorized action. Database undo
does not roll back an APEX Builder import. Production writes remain refused.

## Independent release sequence on shared schema

When multiple applications (such as HR and Payroll) share a single database and TABLES/CODE schema stream:
1. **Schema release applies once:** `schema/v1.0.0` migrates shared database tables and code. It deploys zero applications and forces no sibling app rebuilds.
2. **Application releases deploy independently:** `app/hr/v2.0.0` verifies that its required migration IDs and checksums exist in target history before deploying. Payroll remains completely untouched at v1, and its deployment adapter is never called.
3. **Master component dependencies are qualified without auto-deployment:** If HR declares a master theme or component dependency on Payroll in `targets/masters.json`, the deployment preflight resolves the master component against the target APEX dictionary views. If the master is absent or on the wrong target, HR deployment is refused. If present and qualified, HR deploys while Payroll remains unchanged. There is no automatic dependency deployment.
4. **Independent evidence and handoff:** Test evidence and runbooks are generated and signed per release kind. If a protected runner or browser check is unavailable in the environment, it is recorded as `UNKNOWN`, never a fake `PASS`.
