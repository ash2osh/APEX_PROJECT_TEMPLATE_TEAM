# Promotion and production handoff

Releases are cut from the shared development database ledger and live Builder state. For app releases, Builder capture supplies the selected application's source; the building checkout supplies checks and master contracts. METADATA supplies the complete migration history and immutable migration bytes. No designated Git repository or release tags are used.

## Build locally

Use the registered developer checkout and its development `.env`. The build checks the SQLcl pin, observed schema frontier, migration mutex, registered checkout identity, and (for apps) the app-scoped pause and page-lock report. App capture must be known lock-free and stable across two captures.

```text
# Shared schema release
scripts/team.sh --env .env build-release --kind schema --version 1.1.0 --out scratch/release

# One application only
scripts/team.sh --env .env build-release --kind app --alias hr --version 2.0.0 --out scratch/release

scripts/team.sh verify-release scratch/release/release.tar
```

`TEAM_RELEASE` binds a version to one archive digest. A repeated cut of the same ledger/app source is deterministic; changing the cut or app inputs under an already-used version is refused. Schema releases contain the ordered ledger events and stored migration bundles. App releases contain one captured app, its required migrations at the schema cut, checks and master contract digests. Release tests validate masters against the contract packaged in the archive, never the operator checkout's `targets/masters.json`. If SQLcl loses the outcome of the `TEAM_RELEASE` write, the builder keeps the archive and reports its path and digest; check `TEAM_RELEASE` for that version before building it again, because an app rebuild will not reproduce the same digest. `apply-release` re-reads the target's live metadata history and refuses a `--history` file that no longer matches it. `verify-release` remains offline and can read existing format 2 handoffs as well as format 3 archives.

## Run, sign and generate the owner runbook

Keep the tested archive and evidence together in the operator's local scratch directory. The test profile must identify a non-production `role: test` target. Test qualification uses the archive bytes, current target history and app checks; it refuses destructive work before executing payloads.

```text
scripts/team.sh --env .env.test run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out scratch/test-evidence.json

scripts/team.sh sign-test-evidence \
  --evidence scratch/test-evidence.json \
  --archive scratch/release/release.tar \
  --private-key /secure/local/test-signing-key.pem \
  --out scratch/test-evidence.sig

scripts/team.sh gen-runbook scratch/release/release.tar \
  --history scratch/production-history.json \
  --target targets/production.json \
  --test-evidence scratch/test-evidence.json \
  --signature scratch/test-evidence.sig \
  --trust-key /secure/local/test-trust-key.pem \
  --out scratch/PRODUCTION_RUNBOOK.md
```

Signing and runbook generation run on the operator's machine with local key material. Never put the private key in the repository or an untrusted test process. The signed evidence binds the archive, target, release kind, selected app and app-check digest. `gen-runbook` verifies the trust signature and archive binding, then emits owner-reviewed steps; it does not enable a production write.

A missing test runner, browser flow, trust key or other required evidence is `UNKNOWN` or a refusal, never a synthetic `PASS`. Persistent test qualification does not prove a fresh installation, isolation or arbitrary-DML coverage.

## Production boundary

Production writes remain strictly refused. Production profile audits require read-only database privileges and refuse incomplete privilege evidence, effective write privileges or owned objects. Oracle's baseline grants to `PUBLIC` on Oracle-maintained objects are counted, not refused: `SELECT`/`READ`, `EXECUTE` on packages such as `DBMS_METADATA`, `DBMS_LOB` and `UTL_ENCODE` (which schema reads use), and DML on Oracle temporary tables. `PUBLIC` `EXECUTE` on side-effect packages (`DBMS_ADVISOR`, `DBMS_BACKUP_RESTORE`, `DBMS_FILE_TRANSFER`, `DBMS_IJOB`, `DBMS_JOB`, `DBMS_LDAP`, `DBMS_PIPE`, `DBMS_SYS_SQL`, `HTTPURITYPE`, `UTL_FILE`, `UTL_HTTP`, `UTL_INADDR`, `UTL_MAIL`, `UTL_SMTP`, `UTL_TCP`), DML on permanent Oracle tables, any other privilege, and any sequence grant are refused; revoke them from `PUBLIC` on the production database. Any grant to `PUBLIC` on an application object, and any grant made directly to the account or its roles, must still be read-only. Every role granted to the account must be enabled by default: a non-default role is invisible to the session audit but could be enabled later with `SET ROLE`, so it is refused. The keyword-level driver guard is only a safety net; use a dedicated read-only account without side-effect package execution privileges. See [METADATA backup and restore](metadata-backup-restore.md) for release-ledger recovery guidance.

For one shared schema, schema releases apply migrations once per environment and deploy zero apps. An app release verifies its exact required migration IDs and checksums, deploys only its selected application, and never deploys sibling apps. Master component dependencies are qualified against the target and do not trigger sibling deployment.
