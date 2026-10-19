# Daily change record

A package migration holds the whole package source, so Git cannot show what changed:
the commit lists thousands of added lines for a few edited ones. The daily change
record keeps the readable diff and the day's evidence in one dated folder.

## Rule

- Each day with code work has a folder `ai_generate/YYYY-MM-DD/` (today's date; create
  it when missing). It holds that day's diffs and one `WALKTHROUGH.md`.
- For each migration, write its diff with
  `python3 scripts/migration_diff.py migrations/<SCHEMA>/<folder>`. The script compares
  every SQL step with the mirrored source it replaces and writes
  `ai_generate/YYYY-MM-DD/<folder>.diff`. It reads only local files. Run it before
  `scripts/team.sh backup-db` refreshes the mirror, or pass `--base <git-ref>` to read
  the mirror from a commit. `--date` sets another dated folder.
- Other changes (APEX pages, scripts, documents) go into the same folder as `git diff`
  output.
- `WALKTHROUGH.md` tracks the day: what changed and why, the files and diffs, the
  commands run and their results, what was not verified, and what is open. Add to it as
  the day goes on. One walkthrough per day, never a second file.
- Commit the folder with the work it describes.

`ai_generate/` is project data: `scripts/team.sh upgrade-template` never overwrites it.

## What the script matches

A step is matched to its mirror file by the object in its first lines
(`CREATE OR REPLACE ... PACKAGE [BODY] "SCHEMA"."NAME"`), which gives
`database/<SCHEMA>/packages/<NAME>_SPEC.sql` or `_BODY.sql` (`$` in a name is `-S-`, as
`backup-db` writes it). A step with no such counterpart, such as `ALTER TABLE`, is
written in full as new content. The diff header lists `+added -removed` per step.

## Walkthrough skeleton

```markdown
# Walkthrough YYYY-MM-DD

## 1. <change title>
**Request / decision:** ... (link the decision record, do not restate it)
**Before (observation):** ...
**What changed:** table of file and change, with the diff file linked
**Commands and results:** command, timing, result
**Not verified:** ...
**Open:** ...
```

Label observations from a development database as observations: they may be test data.
Owner decisions belong in the project's decision record, results of record in its
deployment log; the walkthrough links to both.
