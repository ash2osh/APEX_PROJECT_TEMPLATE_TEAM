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

## Execution: `publish-app`

Once the pause notice has been posted and teammates have acknowledged the pause,
the operator proceeds with guarded publishing:

```bash
scripts/team.sh publish-app --prepared <id> --confirm-pause \
  --ack hr:<omar-checkout-uuid> \
  --ack payroll:<layla-checkout-uuid>
```

1. **Explicit Pause Confirmation:** The `--confirm-pause` flag confirms that the pause notice was posted and editing ceased.
2. **Mandatory Acknowledgements:** Every registered checkout UUID recorded in the preparation for each selected application must be explicitly acknowledged via `--ack <alias>:<checkout-uuid>`. The tool never fabricates acknowledgements.
3. **All-App Preflight:** Before any import begins, preflight runs across **all** selected applications:
   - Validates preparation record integrity against its SHA-256 digest (refusing tampered records).
   - Verifies target bindings, clean working tree, and exact source commit.
   - Recaptures each selected app from Builder and confirms identity with the preparation snapshot.
   - Refreshes page-lock reports; any newly locked page or unavailable lock view aborts execution.
   - If any check fails on any selected app, execution aborts with **zero writes** performed.
4. **Sequential Imports:** Applications are imported one at a time via the qualified `import_app` safety core (retaining per-app physical mutex, double-capture, and post-import verification).
5. **Partial Failure & Unknown State Retention:** If an import fails or times out, subsequent imports are halted immediately. The failure is recorded in `.sync-state/publish/<id>/result.json`, recovery evidence is retained, selected apps remain paused, and no automatic rollback or unlock is attempted.

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
If any application encounters an error or timeout (`UNKNOWN` status), no all-clear notice
can be drafted until the target is inspected and recovered.
