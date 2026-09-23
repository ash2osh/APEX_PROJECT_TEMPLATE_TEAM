# Coordinated publish pause and all-clear

Whole-application imports overwrite the shared applications the team is editing in App Builder.
To avoid overwriting uncaptured teammate work, publishing uses a coordinated two-stage surface:
`prepare-publish` followed by `publish-app`.

## Preparation: `prepare-publish`

Preparation is a read-only coordination step that gathers evidence, verifies prerequisites,
and creates a durable record without modifying App Builder:

```bash
scripts/team.sh prepare-publish hr payroll --ref <40-hex-commit> \
  [--manual-lock-report hr:locks.json] \
  [--replace-from hr:<recovery-id>]
```

1. **Exact App Selection:** Only the specified application aliases are paused. Publishing HR pauses HR, not Payroll.
2. **Offline APEXlang Validation:** Each selected application's Git tree is materialized in an isolated directory and validated offline via SQLcl (`sql /nolog` running `apex validate -input <dir>`). The validation requires explicit `Validation successful.` confirmation and zero errors.
3. **Builder Capture and Baseline Check:** Each selected app is captured read-only and compared with its verified baseline, reviewed recovery receipt, or explicit `--replace-from` capture. Unreconciled changes refuse preparation.
4. **Lock Ownership Inspection:** Inspects locked pages for each selected app from `APEX_APPLICATION_LOCKED_PAGES`. If the view is inaccessible, status is marked `UNKNOWN` and preparation refuses.
5. **Durable Evidence Record:** Atomically writes a canonical JSON record under `.sync-state/publish/<preparation-id>/prepare.json` with a SHA-256 tamper-evident digest. The record contains no credentials.
6. **Pause Notice:** Formats and prints an app-scoped pause notice including registered checkouts, differences from baseline, selected commit, and page lock owners. A human operator posts this notice to the team communication channel.

`announce-import` remains available for standalone recovery diagnostic drafting, but does not authorize or perform application publishing.

## Page lock ownership and manual review

Before preparing or executing a publish, the tool inspects locked pages via the
`APEX_APPLICATION_LOCKED_PAGES` dictionary view. A page locked by the importer
is reported with its owner, never filtered out. If the view is inaccessible or
returns malformed data, status is marked `UNKNOWN` and publish is refused.

When automated queries are unavailable, an operator may provide a recent manual
JSON report via `--manual-lock-report <alias>:<path>`. The operator is responsible
for checking the APEX Builder UI (under **Application > Utilities > Page Locks**)
and recording the report within 5 minutes of capture. The report is explicitly
labeled `MANUAL BUILDER REVIEW` and never masquerades as an automated query.

## All-clear notice

An all-clear is drafted only from a verified import result where all selected applications
succeeded and re-exported cleanly. Until then, the selected applications remain paused.
