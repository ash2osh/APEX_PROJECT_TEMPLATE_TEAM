# Daily change record

A collected `02-code/` unit holds the whole package source, so Git cannot always
show what changed since the local database mirror. The daily change record
keeps readable diffs and the day's evidence in one dated folder.

## Rule

- Each day with code work has a folder `ai_generate/YYYY-MM-DD/` (today's date; create
  it when missing). It holds that day's diffs and one `WALKTHROUGH.md`.
- Save patch or mirror diffs (`git diff`, unified diffs against `database/<SCHEMA>/`,
  or patch diffs written to `scratch/release-diffs/`) and release run reports from
  `scratch/release-reports/` as links in the day's one `WALKTHROUGH.md`.
- Other changes (APEX pages, scripts, documents) go into the same folder as `git diff`
  output.
- `WALKTHROUGH.md` tracks the day: what changed and why, the files and diffs, the
  commands run and their results, what was not verified, and what is open. Add to it as
  the day goes on. One walkthrough per day, never a second file.
- Commit the folder with the work it describes.

`ai_generate/` is project data: `scripts/team.sh upgrade-template` never overwrites it.

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
