# Feature ownership and DEV drift

Status: implemented (`feature doctor`, `feature capture --move-from`, the refresh guard and the freeze gate).

A release capsule installs the **feature file**, not what is on DEV. Two things can therefore put an old
version of a stored unit into a release without any command complaining:

1. **Two features hold the same unit.** The copies drift apart; the release installs whichever feature it selects.
2. **A feature file is older than DEV.** DEV was changed (compiled by hand, by a teammate, by another feature)
   and the file was not refreshed.

## What the template does today

| Situation | Result today |
| --- | --- |
| A release selects both copies of a package | `feature freeze` refuses: `duplicate stored unit identity across selected release checkpoints` |
| A release selects only the older copy | `feature freeze` succeeds and the capsule installs the older package |
| Two files of one feature define the same object | `feature doctor` reports `DUPLICATE FILE` (error); `feature freeze` refuses it for a selected feature, with a clear message; the loader already refuses two selected copies of one stored unit |
| A feature SQL file cannot be analysed (CRLF line endings, a BOM, non-UTF-8 bytes, SQL the analyser cannot read) | `feature doctor` reports `NOT ANALYSED` (error, exit 1); `feature freeze` refuses before any DEV read, for any feature under `features/`. Before, the doctor printed a note and exited 0 |
| A second feature runs `feature capture` for a package another feature holds | Refused, unless `--move-from` names the holder |
| `feature capture --refresh` over a file that has lines DEV does not have | Overwrites without showing what disappears |
| A feature file is older than DEV | Not detected: `freeze` and `rollout --prepare` compare the capsule with the *target*, never with DEV |
| An owner decides a feature file keeps a line DEV lacks | There is nowhere to record it |

## Design

### D1. One command for the repository: `feature doctor`

`scripts/team.sh feature doctor [--feature <slug>] [--live]`

Offline checks (always):

- **Ownership.** Every claim is a stored-unit file (identity from the template's own source analysis:
  schema, type, name; spec and body are separate units) or an object created in a `ddl.sql`. A claim made by more
  than one feature is an error (`DOUBLE OWNERSHIP`). `ALTER TABLE ... ADD COLUMN c` claims the pair (table, column), so two features may
  add different columns to one table.
- **Same feature, two files.** Two different files of one feature that define the same object (for example two files
  that both create package `P`, or two `ddl.sql` files that both create table `T`) are an error (`DUPLICATE FILE`). One
  file that names an object twice is not reported here; the database rejects that when the file runs.
- **Unanalysed files.** A SQL file under `features/` whose objects cannot be determined (CRLF line endings, a BOM,
  non-UTF-8 bytes, or SQL the analyser cannot read) is an error (`NOT ANALYSED`) that names the file and the reason.
  Its objects are unknown, so an ownership conflict inside it cannot be seen. Before this check it was only a note, and
  the doctor still exited 0. Fix the file (LF line endings, UTF-8 without a BOM, SQL the analyser reads), or move it out
  of `features/`.
- **Orphan checks.** A check in a feature's `checks.json` that names a stored unit (`OBJECT_NAME`/`OBJECT_TYPE`)
  whose file is not in that feature is reported as a warning. After a move, this is how a forgotten check shows up.
- Exit code 0 clean, 1 findings, 2 could not run. `--feature <slug>` limits every check, including `NOT ANALYSED`, to that feature.

`feature freeze` runs the ownership checks first and refuses (exit 1) when:

- an object is held by two features and one of them is selected by the manifest (a duplicate between features the
  release does not select is left to `feature doctor`);
- a selected feature defines one object in two of its files (`DUPLICATE FILE`);
- any SQL file under `features/` cannot be analysed (`NOT ANALYSED`). This applies to every feature, not only the selected
  ones, because a file whose objects are unknown makes every claim set incomplete. The freeze stops before any DEV read.
  A selected input that the manifest loader rejects outright (for example CRLF line endings) is refused earlier, with
  exit 2 and the loader's message.

When the checks pass, the freeze record in `scratch/freeze-checks/` states `analysis - every feature SQL file was analysed`.
A refused freeze writes no record, because no capsule is built.

### D2. What counts as a claim

| Source | Claim |
| --- | --- |
| `packages/*.pks.sql`, `*.pkb.sql`, `triggers/*.sql`, stored source files | `(schema, type, name)` |
| `CREATE TABLE`, `SEQUENCE`, `INDEX`, `VIEW`, `SYNONYM` in `ddl.sql` | `(schema, type, name)` |
| `ALTER TABLE t ADD (c ...)` in `ddl.sql` | `(schema, "COLUMN", t.c)` |

### D3. Moving a unit between features: `feature capture --move-from <slug>`

`feature capture` for a unit another feature holds is refused and names the owner. `--move-from <slug>` names it as the
source of a deliberate move. The command compares the old file with the current DEV capture first:

| Old file compared with DEV | Move does |
| --- | --- |
| Equal | Proceeds: removes the old files and the old feature's checks for the unit, writes the DEV capture |
| Behind DEV (DEV has more) | Proceeds and prints the old-to-new diff for review |
| Ahead of DEV, diverged, or an accepted difference exists | Refuses and lists the lines the capture would erase |
| Unit not on DEV | Refuses to capture |

Ways forward after a refusal: apply the lines to DEV and repeat; `--discard-unapplied` to erase them knowingly;
`--keep-file` to move the old bytes unchanged (an accepted-difference entry travels with the file).

### D4. Drift against DEV: `feature doctor --live`

Read-only. Reads the DEV spec and body of every package file of the selected features with the same guarded DEV
capture `feature capture` uses (the `CODE_*` profile, identity check, one session per schema), and compares each
file with it, ignoring comments, blank lines, whitespace and letter case. Each file is equal, `BEHIND DEV`,
`AHEAD OF DEV`, `DIVERGED`, `NOT ON DEV` or `ACCEPTED`.

The comparison is three-way when the retained DEV baseline of the file exists (`scratch/feature-source/...`, written by
`feature capture`): the file, DEV now, and DEV as it was when the file was captured. An edit to existing code is the
file's own work, not a loss, as long as DEV has not changed since the capture. What a release would undo are the DEV
changes made since then: lines DEV added that the file lacks, or lines DEV removed that the file still holds. Without a
baseline (a file written by hand, or moved with `--keep-file`) the two-way comparison is used, which cannot tell an edit
from a DEV change, so an edited line shows as `DIVERGED` until a capture with `--refresh` retains a baseline.

| State | Meaning | Level |
| --- | --- | --- |
| `BEHIND DEV` | DEV changed since the capture and the file lacks or still holds those lines; a release would undo them | error |
| `DIVERGED` | the same, and the file also has edits of its own (or, without a baseline, both sides differ) | error |
| `NOT ON DEV` | the package is not in the schema on DEV | error |
| `AHEAD OF DEV` | the file has edits or additions DEV lacks and DEV has not changed since the capture: new work not compiled yet (the template has no DEV apply path, so this is normal) | warning |
| `ACCEPTED` | the differing lines are exactly those recorded in `accepted-differences.json` | info |

Only errors stop a freeze or give `feature doctor` a non-zero exit.

`feature freeze` runs the comparison for the packages of the selected features when a DEV profile is configured.
`--no-live "<reason>"` skips it. What the freeze checked, or why it did not, is written to
`scratch/freeze-checks/<capsule directory name>.json` (outside the capsule and its digest). Without a DEV profile
the freeze says the comparison did not run; if a profile exists but DEV cannot be read, the freeze stops (exit 2)
and names `--no-live`. A schema without a DEV profile is reported by `doctor --live` as `LIVE SKIPPED`. When some selected
schemas have a DEV profile and others do not, the freeze prints a `note:` line naming the schemas it did not compare, and
the freeze record lists them under `not compared (no DEV profile)`.

### D5. Differences the owner decided on: `features/<slug>/accepted-differences.json`

```json
[{"schema": "APP_CODE", "type": "PACKAGE BODY", "name": "ITEM_LOOKUP",
  "decision": "who decided, when, where it is recorded",
  "featureOnly": ["normalized line present only in the file"],
  "liveOnly": ["normalized line present only on DEV"]}]
```

A difference is `ACCEPTED` only while the differing lines equal the recorded lines; any other drift still fails.
A move with `--keep-file` carries the entry to the receiving feature; a move that takes the DEV version drops it.

### D6. `feature capture --refresh`

`--refresh` applies the D3 table to the file it replaces: lines the file has and DEV does not (unapplied edits, or an
accepted difference) stop the refresh unless `--discard-unapplied` is given.

## Examples

Names are generic.

| # | Before | Command | Result | After |
| --- | --- | --- | --- | --- |
| 1 | `inventory` holds `ITEM_LOOKUP`, equal to DEV | `capture orders --package ITEM_LOOKUP --move-from inventory` | Moves | `orders` holds it, equal to DEV; `inventory` has no file and no check for it |
| 2 | `inventory`'s file is 700 lines older than DEV | same | Moves, prints the 700-line diff | `orders` holds the DEV version |
| 3 | `inventory`'s file has 6 lines not compiled on DEV | same | Refuses, lists the 6 lines | unchanged; compile them on DEV and repeat, or `--discard-unapplied` |
| 4 | file has a new block and DEV has a new procedure | same | Refuses, shows both sides | unchanged |
| 5 | an accepted difference is recorded for the file | `capture ... --refresh` or a move | Refuses; with `--keep-file` the file and the entry move | `orders` keeps the decision |
| 6 | two features hold `ITEM_LOOKUP` | `feature doctor` | Reports it and which copy is closer to DEV | delete the other copy |
| 7 | `orders` runs `capture` for a unit `inventory` holds, no flag | `capture orders --package ITEM_LOOKUP` | Refuses and names `inventory` | unchanged |
| 8 | file never compiled on DEV | `--move-from` | Cannot capture; refuses unless `--keep-file` | file moves unchanged |

## Not covered

- Views, DDL files and data scripts are checked for ownership but not compared with DEV.
- The live comparison covers every stored unit `feature capture` can read: packages and types (spec and body), triggers, procedures and functions.
- `--keep-file` does not retain a DEV baseline; run a capture with `--refresh` once the file equals DEV.
- A unit claimed by a feature and also by an old `migrations/` folder is not detected.

## Tests

`tests/test_feature_ownership.py` covers each row of the tables with a faked DEV capture, and was mutation-checked
(each rule removed in turn makes a test fail). Live checks (opt-in, Docker) in `tests/test_live_feature_release.py`
exercise `doctor --live`, a move and a refused refresh against a real schema, as described in
[live feature-release checks](live-feature-tests.md).
