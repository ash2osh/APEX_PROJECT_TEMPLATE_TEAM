# Local improvements: inline review and delivery record

Implementation is on `codex/template-local-improvements`. The developer stopped
AGY and requested the remaining work inline. No subagents or AGY sessions were
used after that switch. The original template checkout and the prior maintenance
fixes were preserved. No staging, commits, pushes, merges or Natrec writes were
performed for this changeset.

## Delivered behavior

- Native skills inspection and offline `verify-local`, with literal/redacted
  configuration, descriptor/folder validation, upgrade/recovery diagnostics and
  optional read-only `doctor` integration.
- Explicit release/ref upgrade pinning. A separate 26.1 maintenance patch is
  prepared and tested; the actual `codex/apex-26.1` branch has not advanced.
- Independent all-or-none DEV/staging/production migration profiles, preserving
  read/backup targets and strict schema/identity selection in both loaders.
- Public APEX 26.2 lifecycle snapshots and an independent publish/deployment
  gate, including selected pages and the manual promotion runbook.
- Qualified compressed catalog transport and grouped identity-sequence lookup,
  preserving catalog/subobject/signature semantics.
- Pinned project-only Graphify setup, static readiness and verified extraction;
  downstream provenance guidance and preservation regressions.
- Consolidated read-only qualification plus the existing disposable write probe's
  lifecycle scenario and exact inventory/identity cleanup.

## Review fixes

Focused regressions reproduced the following failures before corrections:

1. Unbounded gzip finalization/truncation could evade bounded decoder progress.
   Decoding now uses bounded chunks, validates EOF/trailer/CRC and refuses trailing
   data/multiple streams. SQLcl presentation-only trailing blank lines are handled
   outside the strict decoder.
2. Lifecycle application visibility was implicit, initial app creation falsely
   refused a new automation, and capture omitted parsing-schema selection. The
   field is now required, initial/new definitions are recorded without implying
   loss, and the target schema is selected before the read-only transaction.
3. Disposable cleanup did not verify removal of workflow/task rows. Exact
   run-created inventory, alias and owner checks now precede app removal, followed
   by zero-row verification for all three types. Interruption/ambiguous writes
   retain inventory and recovery evidence.
4. Read-only runtime qualification could report success for an absent app. It
   now requires exactly one visible application before further observations.
5. Graphify preparation permitted broad scratch/name exceptions. Only the
   canonical project environment may be patched. External directory links are
   rejected before installation; atomic replacements avoid shared hardlink writes.
6. Graphify output paths were resolved against the caller, successful process exit
   could conceal missing output, and the checker could inspect the wrong root.
   Extraction now requires canonical output, checks actual output and domain
   coverage, and supplies the explicit corpus root to the isolated interpreter.
7. Routine extraction forced a full rescan. It is incremental; explicit full
   rebuild arguments use `extract -- --force`. Both modes passed real extraction.
8. A configured original template was treated as downstream for lock validation.
   Local Git origin metadata now distinguishes the original empty template,
   without a subprocess/network call; real apps still require a downstream lock.

Wrapper and test-fixture corrections preserved the production contracts rather
than weakening source verification, runtime checks or ownership guards. A final
documentation regression restored the explicit Graphify upgrade reminder.
No unresolved actionable functional finding remains from the inline review.

## Evidence and limits

The 26.1 prepared patch has SHA-256
`594a20bd8e9f7fcb934f0e7c4acc463261697878fd2e0070b1b19f35f3f8af76`, against
base `5178f57a56d919337d1e7d23fc809275cfb8488e`. Exact clean application matched
434 files/modes and three deletions; its recorded full suite passed 970 tests,
with 21 skips. Root independently reviewed patch/tree equality and focused
regressions. This is prepared backport evidence, not branch integration or live
26.1 qualification.

Release A's root full suite passed 1,148 tests with 21 skips. Focused migration,
wrapper, transport, Graphify and readiness checks passed after their changes.
The final reviewed Python 3.10.20 suite passed **1,245 tests in 405.087 seconds**,
with 21 platform-specific skips. The host Python focused final checks passed
114 tests. Logs are `root-final-python310-reviewed-full.log` and
`root-final-host-focused.log` under the ignored evidence directory. The earlier
full run had one documentation failure; it was corrected before this fresh run.
Ruff, Bash/PowerShell parsing, example configuration, JSON and LF checks passed.

[Docker qualification](../../apex-26.2-qualification.md#local-improvements-qualification-on-2026-10-09)
records exact catalog model equality, >1 MiB Unicode transport, positive runtime
fixtures, actual full/partial wrapper success, failure recovery and verified
cleanup. Compressed output was about 90.5% smaller; total Docker timing did not
improve. Read-only observations and fixture imports are distinct evidence.
All 96 fingerprinted shared Graphify files remained unchanged. Native skills
inspection found ten current installations dated 2026-10-08; no new sync ran.

Native Windows, live APEX 26.1 and staging/production database imports remain
unavailable in this qualification. Linux PowerShell and offline promotion tests
do not establish those behaviors. Compressed transport was not backported to 26.1.
Evidence remains under ignored `scratch/agy-implementation-20261009/` and
`.superpowers/sdd/2026-10-09-local-template-improvements/` because this work has
not been committed.

## Rulings and deferred work

- Follow the user's inline-only instruction, preserving earlier prepared artifacts.
  Cost: the final review has no independent reviewer.
- Prepare the 26.1 patch without switching the dirty root checkout or advancing
  branch history. Cost: the backport requires a separate authorized integration.
- Remove noncanonical Graphify patch exceptions and require the isolated pinned
  environment. Cost: users of older shared installations must set up the local
  environment before extraction.

Deferred functional minors: none. Git integration is a pending user decision,
not an unverified implementation success. Preserve recovery/review artifacts
until integration provides the durable record.
