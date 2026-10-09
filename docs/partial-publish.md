# Publish selected existing pages to shared DEV

Use canonical APEX 26.2 source and SQLcl 26.3.0.0 or newer. Both team wrappers
accept repeatable application-relative page paths:

```bash
scripts/team.sh publish 100 --env dev --file pages/p00001-home.apx
scripts/team.sh publish 100 --file pages/p00001-home.apx --file pages/p00002-report.apx
```

The PowerShell commands use the same arguments. Select exact existing page
files; wildcards, new/deleted pages, page 0, application/shared-component files,
and unselected local edits are refused. Partial mode refuses `--force`,
`--describe`, and staging/production. There is no full-import fallback.

Before editing, run an authorized `export` or Builder-mode `upgrade-apexlang`
to capture a schema-2 content baseline. Coordinate ordinary shared DEV imports
with the team. The wrapper acquires a short native application lock, exports a
stable fresh whole-app snapshot, and compares only the selected live pages
against their baseline. It merges selected local edits into that snapshot and
validates the complete application against current shared components. Unrelated
saved live changes are preserved and become part of the synchronized local
source; review them afterwards.

SQLcl imports only the selected page files. The wrapper stamps the version
through public `APEX_APPLICATION_ADMIN.SET_APPLICATION_VERSION`, then verifies
the entire re-export, effective deployment, and exact selected Builder lock
identities. A generated URL cutoff may change only to the observed live version
update timestamp; all other deployment changes refuse verification. Explicitly
selected protection settings must remain exactly equal. After verification and
run-owned lock release, the wrapper atomically installs canonical source and
advances the baseline. It refuses replacement if local source changed during
the operation or an ignored local file would be lost.

## Routine publishes without a team notice

Acquire the page locks in Builder **before capturing the baseline and editing**.
SQLcl can inspect `APEX_APPLICATION_LOCKED_PAGES`; no supported page-lock setter
was found in the installed public APIs. An application lock is a different lock.
Full imports removed Builder page locks in qualification; selected-page imports
preserved them, including their IDs, owners, comments and timestamps.

```bash
scripts/team.sh publish 100 --file pages/p00001-home.apx --no-team-notice
```

This option asserts that the team has agreed to the guarded workflow and uses
separate workspace developer accounts. It is available only when:

- Every selected page has the same exact Builder lock captured before editing,
  owned by the configured `APEX_WORKSPACE_USERNAME`.
- All selected pages still match the live baseline; every unselected local file
  remains unchanged, including generated artifacts and deployment descriptors.
- `deployments/dev.json` explicitly supplies both `app.sessionStateProtection`
  values `checksumSalt` and `allowUrlsCreatedAfter`, equal to the live export.
  Configure these values through a separately coordinated, verified change;
  preserve the app's own salt and cutoff. Never copy an example salt.
- The merged app validates, all effective settings/source verify after import,
  and those protection values and exact page locks stay unchanged.

The checks cannot prove that another person does not share your account or that
raw SQLcl writers follow the team agreement. Do not use this option with shared
accounts, concurrent runs from one account, unguarded imports, or an unqualified
protection configuration. Failed eligibility refuses before import; rerun the
ordinary coordinated route only after resolving the cause with the developer.
Builder-first work, full imports, shared-component/global changes, initial app
creation and promotion retain their coordination requirements.

## Recovery

Partial publish also compares scoped automation/workflow/task state before and
after source verification. The no-notice eligibility rules do not bypass this
gate. `partial.json` distinguishes `sourceVerified` from `lifecycleStatus`;
attention or an unavailable post-read retains the old baseline and native lock.
Follow the [runtime recovery rules](publish-rules.md#runtime-verification-after-import).

Before an import attempt, failure releases only a proven run-owned application
lock and keeps local edits. After an attempted but unverified write, failure
retains the old source/baseline, application lock and private recovery evidence
under the reported `scratch/apex-partial.*` directory. The database may have
changed; do not retry blindly. Inform the team, inspect the live app and
canonical export, reconcile the selected edits and unrelated changes, then
release only the reconciled owner's lock with `app-unlock`. A verified database
write followed by a local replacement refusal retains its canonical staging
and receipt for recovery; it does not restore the database.

Matching source bytes includes binary static files and
`generated-artifacts/translations.sql`. Do not edit generated artifacts. Page
alias changes refuse before import because they can rename the canonical file;
use a coordinated full change for page identity/layout changes.
