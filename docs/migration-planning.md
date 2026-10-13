# Plan a migration batch

Use `migration-plan` to inspect the migration folders in this checkout before
you choose an ordered batch. It is an inventory and review aid; it never applies
SQL, rehearses DML, writes receipts, or claims that a folder is ready or safe to
rewrite.

```bash
# Local inventory, grouped by schema and sorted for display only.
scripts/team.sh migration-plan --format text

# Filter an offline inventory, or assert the owner for flat folders.
scripts/team.sh migration-plan --schema SALES --format markdown
scripts/team.sh migration-plan migrations/2026-10-12_add-status-r001 \
  --schema SALES --format json

# Keep an intended batch in the exact order you pass.
scripts/team.sh migration-plan \
  migrations/SALES/2026-10-12_create-orders-r001 \
  migrations/SALES/2026-10-13_add-order-state-r001 --format text

# Ask for the existing guarded, read-only live preflight.
scripts/team.sh migration-plan \
  migrations/SALES/2026-10-12_create-orders-r001 \
  migrations/SALES/2026-10-13_add-order-state-r001 \
  --live --env dev --format markdown
```

## Evidence and ordering

Without folders, the command reports a local inventory. It reads only this
checkout: it does not read `.env`, start SQLcl, connect to Oracle, write files,
or access the network. The stable JSON report has `schemaVersion: 1`; its
summary includes folder and check counts, immutable and unknown-evidence
counts, and `singleSqlFileFolderCount`, the count of Single-SQL-file folders
for review. It is not a recommendation to combine them.

The daily workflow uses Python 3.10 or newer and the standard library only.
Check the runtime with `python3 --version`. On Windows, `team.ps1` resolves a
3.10+ Python runtime and supplies Git Bash with a temporary `python3` shim when
needed; running `team.sh` directly in Git Bash requires `python3` on `PATH`.

Offline `--schema NAME` filters explicit `migrations/NAME/` folders. For a flat
folder, `--schema` is a caller assertion and appears as `caller-asserted`; the
command does not verify it against `.env`. Without that assertion a flat
folder's schema remains unresolved. An explicit schema directory mismatch is
an error.

Discovered rows use a deterministic sorted display order, grouped by schema;
the order is never inferred as a dependency or execution order. Explicit
folder arguments retain their caller-supplied order and are checked with the
existing batch rules, including duplicate selection and consecutive revision
order. For cross-schema work, make separate one-owner batches and coordinate
their sequence explicitly.

The report's local evidence describes only this repository. `unlocked` means no
local attempt or receipt was found; it cannot prove that another checkout did
not attempt the folder. A verified receipt is historical evidence, not current
database state. Unknown or unreadable local evidence remains a refusal. No other repository's pending work is discovered.

## Live preflight boundary

`--live` requires explicit folders and `--env dev|staging|prod`. The selection
must be valid, have known local evidence, and resolve to one migration owner.
The command reads `PROJECT_ENV_FILE` or root `.env` only after those arguments
and local folders validate. It then uses the existing SQLcl 26.3.0.0-or-newer
runtime guard and guarded read-only preflight. An unavailable or older runtime
refuses before database preflight. Live reporting never applies SQL, rehearses
DML, repairs database state, or creates migration receipts.

The target is shown only when the preflight captured complete observed identity;
a refusal or incomplete observation does not get a synthetic target. The
preflight evaluates only the first selected folder's preconditions before any
write. Later folders with preconditions appear as deferred: `migrate` checks
each at its own apply boundary, after earlier folders have run. Opaque SQL
manual-review notices remain visible even when structural preflight succeeds.

JSON, text, and Markdown reports keep structured condition codes, check IDs,
relative paths, and validated Oracle error codes where available. Arbitrary
preflight error text, nested SQL/result payloads, and SQL source are omitted;
rendered messages use fixed status descriptions. Reports contain no SQL bodies,
credentials, or full logs. This is a report-only policy and does not reword the
existing migration command diagnostics.

Exit 0 means the inventory is complete, or a live preflight found no conflicts
or errors and no selected locked folder. Exit 1 means a live known conflict or
selected immutable folder. Exit 2 means malformed/incomplete local evidence,
invalid selection, target refusal, or incomplete live observation. The report
is retained on refusal when a valid output format was selected.

## Choose the folders, then execute explicitly

There is no automatic draft squasher. A low single-file-folder count does not
authorize combining folders. Once an apply may have started, keep the folder
name and SQL/check bytes immutable; put a correction or recovery in the next
revision and reconcile the live target first. Never delete, rename, squash, or
archive an attempted or applied folder to make the report look simpler.

After review, use the existing explicit-order commands. For an ordinary
selected batch:

```bash
scripts/team.sh check-conflicts \
  migrations/SALES/2026-10-12_create-orders-r001 \
  migrations/SALES/2026-10-13_add-order-state-r001 --env dev
scripts/team.sh migrate \
  migrations/SALES/2026-10-12_create-orders-r001 \
  migrations/SALES/2026-10-13_add-order-state-r001 --env dev
```

For a reviewed ordered release, use the existing rollout manifest rather than
turning display order into an execution plan:

```bash
scripts/team.sh rollout docs/rollout-manifest.example.json --env staging --dry-run
```

See [migration rules](migration-rules.md) for naming, preflight, immutability,
recovery, rollout and detection limits.
