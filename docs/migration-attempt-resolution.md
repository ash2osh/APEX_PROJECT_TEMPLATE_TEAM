# Migration attempt resolution

A dated migration folder whose apply failed, or ended with an unknown result, keeps
its attempt locked. Nothing in the tool can say what happened on the target, so the
owner has to. `scripts/team.sh reconcile-migration` records the owner's statement, with
the evidence file's digest, so that the folder can be archived. It never makes a folder
verified, and it never edits the folder, its receipts or its earlier evidence.

## The problem

A revision whose apply failed stays in `migrations/` with a `pending` attempt event and
no receipt, even after a later revision of the same family is verified. `archive-migrations --all`
then refused the whole schema. The only way out was `--allow-unverified-attempts`, which
archived the folder as `attempt-without-verified-receipt (accepted)`: nobody had said what
happened on the target.

## The three outcomes

| Outcome | Use it when | Evidence the owner must have | Archive |
| --- | --- | --- | --- |
| `failed-no-target-effect` | The apply failed and the owner has checked the target: nothing from the folder exists there. | A read-only check of the target, sanitized, showing the expected objects are absent. | Archived as `resolved:failed-no-target-effect:<env>`. |
| `failed-target-effect-unknown` | The apply failed and the owner cannot prove either way: some objects may exist. | The read-only check that shows what was found, and why the state is still unknown. | Archived as `resolved:failed-target-effect-unknown:<env>`. |
| `superseded` | A later revision of the same family carries the fix and is verified for the same environment. | The later folder's `status.<env>.json` must validate. Name it with `--superseded-by`. | Archived as `resolved:superseded:<env>`. |

The outcome is the owner's statement. Choose it on evidence, not on what the tool would
accept. `failed-target-effect-unknown` says that no one can vouch for the target, so it
is the honest choice when a check was not possible.

## The command

```bash
scripts/team.sh reconcile-migration <folder> --env dev|staging|prod \
  --outcome failed-no-target-effect|failed-target-effect-unknown|superseded \
  --evidence <file> [--attempt <attempt-id>] [--superseded-by <folder>]
```

- `<folder>` is `migrations/<SCHEMA>/<dated-folder>` (or `migrations/<dated-folder>` in a flat layout).
- `--evidence` is a file in the repository, outside `migrations/` and `scratch/`, that is not a
  symbolic link. The command reads it, computes its SHA-256 and stores the path and digest. It
  does not copy the file. Write sanitized output only: no credentials, no personal data.
- `--attempt` names the attempt (or the scratch run id) when more than one is pending.
- The command prints the folder, environment, attempt id, run id, the failure state from the
  retained run manifest when there is one, the outcome and the evidence digest. Then it asks
  for the typed acknowledgment `RECONCILE-MIGRATION <attempt-id>`. For a scratch-only attempt
  the acknowledgment names the run id. The developer types it at a terminal. Redirected input
  is refused.
- An agent never types the acknowledgment.

It reads local files only. It starts no process, runs no SQLcl and connects to no database.

### Worked example: failed with no target effect

The apply of `migrations/APP_ONE/2026-10-12_archive-family-r002` to DEV stopped part-way.
The owner ran a read-only query on the APP_ONE schema: `DEMO_TABLE` does not exist, and
no other object from the folder exists. The owner saved the sanitized result as
`evidence/APP_ONE-r002-owner-check.txt` and runs:

```bash
scripts/team.sh reconcile-migration migrations/APP_ONE/2026-10-12_archive-family-r002 \
  --env dev --outcome failed-no-target-effect \
  --evidence evidence/APP_ONE-r002-owner-check.txt
```

```text
Folder: migrations/APP_ONE/2026-10-12_archive-family-r002
Environment: dev
Attempt: <attempt-id>
Run: migration-attempt-<run-id>
Recorded failure state: apply-failed-or-unknown
Outcome: failed-no-target-effect
Evidence: evidence/APP_ONE-r002-owner-check.txt sha256=<digest>
Type RECONCILE-MIGRATION <attempt-id> to record this owner resolution. Only the owner types it.
Acknowledgment: <typed by the developer>
Recorded owner resolution: migrations/APP_ONE/2026-10-12_archive-family-r002 dev failed-no-target-effect
  resolution record: migrations/APP_ONE/2026-10-12_archive-family-r002/resolution.dev.json
  closing attempt event: <event-id> (attempt <attempt-id>)
```

The folder stays unchanged, and it stays locked: `migrate` never applies it again. The next
revision carries the fix (`scripts/team.sh revise`).

### Worked example: target effect unknown

The apply of `migrations/APP_ONE/2026-10-12_archive-family-r002` stopped after a `CREATE`.
The owner's check found `DEMO_TABLE` but could not confirm its columns, so the owner
records the outcome as unknown:

```bash
scripts/team.sh reconcile-migration migrations/APP_ONE/2026-10-12_archive-family-r002 \
  --env dev --outcome failed-target-effect-unknown \
  --evidence evidence/APP_ONE-r002-owner-check.txt
```

The record says what the owner could not establish. Archive keeps that statement; it does
not turn it into a verified state.

### Worked example: superseded by a later verified revision

`migrations/APP_ONE/2026-10-13_archive-family-r003` carries the fix. Its
`status.dev.json` is present and validates, so the folder is verified for DEV:

```bash
scripts/team.sh reconcile-migration migrations/APP_ONE/2026-10-12_archive-family-r002 \
  --env dev --outcome superseded --superseded-by 2026-10-13_archive-family-r003 \
  --evidence evidence/APP_ONE-r002-owner-check.txt
```

The command refuses unless the later folder is a later revision of the same family and
schema, has a `status.dev.json` that `validate_receipt` accepts for DEV and the APP_ONE
schema, and is either visible or in the archive index for that schema. The record stores the
SHA-256 of that receipt. In practice the later folder is visible while you resolve the
superseded one: the folder loader refuses a visible revision below an archived one, so a
superseded folder can never sit beside an archived later revision.

## Scratch-only attempts

A retained run under `scratch/migration-attempt-<run-id>/` can name a folder whose attempt
events are not in this checkout (for example, a teammate's run, or events you did not pull).
The command then resolves the run: the record's `attemptId` is `null`, the acknowledgment is
`RECONCILE-MIGRATION migration-attempt-<run-id>`, and no closing event is written, because a
chain cannot be closed without its pending root. Other checkouts read the sidecar and treat
the run as resolved.

## What the command never does

- It does not connect to a database, start SQLcl or read the target.
- It does not check that the target matches the outcome. It records the owner's statement and
  the evidence's digest. See the limits below.
- It does not edit or delete the folder's SQL, `checks.json`, receipts or earlier attempt events.
- It does not verify a folder. A folder with a verified receipt is refused.
- It does not replace an existing resolution for the same environment.
- It does not accept an acknowledgment from a pipe, a file or an agent, except the test-only
  `TEAM_ACK_FROM_STDIN=1` path used by the opt-in live tests.

## Refusals

Nothing is written on any refusal. Each refusal exits 2 and says why:

- a wrong or empty acknowledgment;
- an evidence file that is missing, a symbolic link, outside the repository, under
  `migrations/` or `scratch/`, or changed between the check and the typed answer;
- a folder with a verified receipt, or with a receipt that does not validate;
- an attempt that is not pending: `verified`, or `apply-not-started` (it wrote nothing);
- no pending attempt at all in this checkout;
- several pending attempts without `--attempt`;
- a resolution already present for that environment;
- folder bytes that differ from the attempt's payload digest;
- `--superseded-by` that does not name a later verified revision of the same family and schema.

## How archive treats each state

| State | Archive without a flag | Plan evidence |
| --- | --- | --- |
| Verified receipt, no attempt | Yes | `status.<env>.json (verified)` |
| Resolution recorded, no verified receipt | Yes | `resolved:<outcome>:<env>` |
| Pending attempt, no resolution | No: refused, the refusal names the command | With `--preserve-unresolved`: `unresolved (preserved)`. With `--allow-unverified-attempts`: `attempt-without-verified-receipt (accepted)` |
| `apply-not-started` attempt with its retained no-write log | Yes | `apply-not-started (no write)` |
| Attempt whose evidence cannot be read (unknown) | No | With `--preserve-unresolved`: `unresolved (preserved)`. With `--allow-unverified-attempts`: `attempt-without-verified-receipt (accepted)` |
| Resolution that is unreadable, stale, or in conflict with a receipt | No. No flag accepts it | Refused with the states listed |

Only `--allow-unverified-attempts` writes the word "accepted". `--preserve-unresolved` never
calls an attempt failed: it says the folder was archived with no resolution.

`--all` evaluates every selected folder. When any is refused it prints every refusal with
its command and archives nothing (exit 2). `--skip-refused` archives the eligible folders
and lists the rest. The prefix rule still applies: within a family the archive takes the
low prefix up to the first refused revision and skips the rest. Each new index record
carries an `evidence` list with the plan's evidence strings. Older records without that key load unchanged.

## Writes, in order

1. `resolution.<env>.json` is installed first, with no overwrite. It is committed with the
   folder and it is what other checkouts, `revise --check`, `migrate` and archive read.
2. The closing `attempt-event.<id>.json` is written second. It is local and ignored by Git,
   like every attempt event.

If the second write fails, the command exits 2 and says so. The sidecar stands: the lock report
treats a pending chain that a valid sidecar names as closed with that outcome, so the folder
reads as resolved. Do not run the command again: a second resolution for the same environment
is refused. A closing event whose sidecar is missing is reported as `resolution-record-missing`,
and archive refuses it until the record is restored.

## Upgrade note

Every developer upgrades the template (`scripts/team.sh upgrade-template`) before anyone
commits a resolution. A checkout on an older template refuses a folder that holds
`resolution.<env>.json`, because it reads the file as a non-SQL migration entry. That blocks
the folder for that developer until the upgrade.

## Limits

- One resolution covers one environment of one folder. A folder with two unresolved
  attempts for the same environment cannot be fully resolved with this command: the first
  resolution blocks the second. Raise it with the maintainers before you archive that folder.
- The record stores the owner's statement and the digest of the evidence file at the time the
  command ran. Archive does not re-read the evidence file, so an edit made later goes unnoticed.
  Commit the evidence with the record, and treat it as read-only.
- `apply-not-started` attempts (no payload statement began) are not resolutions and need none.
  While the retained no-write log under `scratch/` is still in the checkout, the folder is
  unlocked and archives without a flag, with the evidence `apply-not-started (no write)`. When
  that log is gone the state is unknown: archive refuses it, and `--preserve-unresolved`
  archives it as `unresolved (preserved)`.
- `team.ps1` routes the command to the same Bash entry point. This change did not run it on
  native Windows; that check is still open.
